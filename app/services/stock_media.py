"""Real photos and footage from free media libraries, fetched by search.

A shot that names something real - a place, a launch, a person - can use
a real photograph or a slice of real footage instead of an AI still. This
module finds one by search, checks its licence, saves it with its credit,
and never asks for a file to be picked by hand.

**The licence gate is the point of the module.** Only public domain, CC0,
CC BY and the Pixabay Content License pass. Share-alike, non-commercial,
no-derivatives, GFDL-only and anything unrecognised are refused, because
a documentary is a derivative work that may be monetised. A file marked
as trademarked is refused too. Every accepted file is written with a
``.credit.json`` sidecar so the credit line reaches the video description.

Wikimedia Commons is searched through Wikipedia's API. That API reaches
the shared Commons file repository, so it works on networks where the
Commons site itself is unreachable. Its etiquette requires a User-Agent
with a contact address; requests without one are refused with HTTP 403.

Pixabay is used only when ``AVM_PIXABAY_API_KEY`` is set. Its terms ask
for search responses to be cached for 24 hours, which is done here.

Long films are not downloaded whole. FFmpeg reads only the needed seconds
over HTTP range requests from the library's own transcode.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Image, Scene, SceneClip

log = logging.getLogger(__name__)

USER_AGENT = (
    "AIVideoManager/0.1 (https://www.kaggle.com/gulfamamjad; local desktop video tool)"
)
WIKI_API = "https://en.wikipedia.org/w/api.php"
PIXABAY_VIDEO_API = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_API = "https://pixabay.com/api/"
PIXABAY_CACHE_S = 24 * 3600
HTTP_TIMEOUT_S = 40
SEARCH_ATTEMPTS = 3
FFMPEG_TIMEOUT_S = 600

#: How many results to look at per search. Enough to skip junk, few enough
#: to stay polite.
SEARCH_LIMIT = 20
MIN_IMAGE_WIDTH = 1200
MIN_VIDEO_WIDTH = 400
#: Footage longer than this is a whole film; a random slice of it is a guess
#: at what is on screen, so short clips are preferred.
SHORT_CLIP_S = 240.0
EXCERPT_S = 24.0
#: How many seconds of a long film a caller may take, once they have named
#: the moment. A blind search still refuses anything over SHORT_CLIP_S.
MAX_TIMED_WINDOW_S = 40.0

_MODERN_YEAR = re.compile(r"\b(?:19[7-9]\d|20\d\d)\b")
_JUNK_WORDS = (
    "logo", "icon", "banner", "diagram", "chart", "graph", "fundraising",
    "animation", "collage", "montage", "seal", "signature", "stamp", "coin",
    "anniversary", "ceremony", "remembrance", "reenact", "commemorat",
    "statue", "cropped", " edit", "honors", "festivities", "surfer", "leisure",
    "battle walk", "memorial", "dvids", "veteran", " cover",
)
#: Footage that cuts between many moments. Any slice of it is a guess at
#: which moment is on screen, so it is never used for a shot.
_COMPILATION_WORDS = (
    "introduction", "highlights", "overview", "compilation", "together",
    "anniversary", "b roll", "broll", "documentary", "workshop", "mission video",
)
#: Share of the query's words that must appear in a title.
MIN_TITLE_MATCH = 0.5
#: Mean brightness (0-255) below which an excerpt is too dark to read.
MIN_EXCERPT_LUMA = 32.0
#: Mean brightness above which a "photo" is almost always a scanned page.
MAX_PHOTO_LUMA = 195.0


class StockMediaError(Exception):
    """A search or download that could not be completed."""


# ---------------------------------------------------------------------------
# Licence gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LicenceVerdict:
    allowed: bool
    label: str
    needs_credit: bool
    reason: str = ""


_CC_BY = re.compile(r"^cc[\s-]*by(?:[\s-]*(\d(?:\.\d)?))?(?:[\s-]*(?:international|generic|unported))?$")


def classify_licence(short_name: str, usage_terms: str = "", restrictions: str = "") -> LicenceVerdict:
    """Whether a file may go into a monetisable derivative video.

    Deliberately narrow. A refusal costs one shot a real picture; a wrong
    acceptance costs the channel a copyright strike.
    """
    name = re.sub(r"\s+", " ", (short_name or "").strip())
    lowered = name.lower()
    terms = (usage_terms or "").lower()
    if "trademark" in (restrictions or "").lower():
        return LicenceVerdict(False, name, False, "marked as a trademark")
    if not lowered:
        return LicenceVerdict(False, name, False, "no licence stated")
    if any(bad in lowered for bad in ("-nc", " nc", "noncommercial", "non-commercial")):
        return LicenceVerdict(False, name, False, "non-commercial only")
    if any(bad in lowered for bad in ("-nd", " nd", "noderiv")):
        return LicenceVerdict(False, name, False, "no derivatives allowed")
    if "sa" in re.split(r"[\s-]+", lowered) or "sharealike" in lowered or "share alike" in lowered:
        return LicenceVerdict(False, name, False, "share-alike would bind the whole video")
    if lowered.startswith("gfdl") or lowered == "gfdl":
        return LicenceVerdict(False, name, False, "GFDL requires the licence text with the work")
    if lowered in {"cc0", "cc-zero", "cc0 1.0", "cc zero"} or lowered.startswith("cc0"):
        return LicenceVerdict(True, "CC0", False)
    if lowered.startswith("public domain") or lowered in {"pd", "pdm", "public domain mark"}:
        return LicenceVerdict(True, "Public domain", False)
    if lowered.startswith("pd-") or lowered.startswith("pd "):
        return LicenceVerdict(True, "Public domain", False)
    if _CC_BY.match(lowered):
        return LicenceVerdict(True, name.upper().replace("CC-BY", "CC BY"), True)
    if lowered == "pixabay content license":
        return LicenceVerdict(True, "Pixabay Content License", False)
    if "public domain" in terms and "share" not in terms:
        return LicenceVerdict(True, "Public domain", False)
    return LicenceVerdict(False, name, False, "licence not on the accepted list")


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    source: str
    kind: str  # "video" or "image"
    title: str
    page_url: str
    media_url: str
    width: int
    height: int
    duration_s: float | None
    licence: str
    needs_credit: bool
    artist: str
    restrictions: str = ""
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def credit_line(self) -> str:
        who = self.artist or "unknown author"
        name = re.sub(r"^File:", "", self.title)
        return f"\"{name}\" by {who}, {self.licence}, via {self.source} - {self.page_url}"


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _artist(value: str) -> str:
    """The author as the file page states it, without wiki boilerplate."""
    text = _strip_html(value)
    text = re.sub(r"\bCreator:\s*", "", text)
    if not text or text.lower().startswith("unknown author"):
        return "author not stated"
    return text[:160]


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


def title_match(candidate: Candidate, query: str) -> float:
    wanted = _tokens(query)
    return len(wanted & _tokens(candidate.title)) / max(len(wanted), 1)


def usable(candidate: Candidate, query: str) -> bool:
    """Whether a result is about this shot at all, before it is ranked."""
    if title_match(candidate, query) < MIN_TITLE_MATCH:
        return False
    title = candidate.title.lower().replace("-", " ").replace('"', "")
    if any(word in title for word in _JUNK_WORDS):
        return False
    # A 2019 ceremony is not a photograph of 1944. A year counts only when
    # the search itself asked for that year.
    modern = set(_MODERN_YEAR.findall(candidate.title))
    if modern - set(_MODERN_YEAR.findall(query)):
        return False
    # "Apollo 11" must not be answered with Apollo 10: numbers name the thing.
    numbers = {t for t in _tokens(query) if t.isdigit()}
    if numbers and not numbers <= _tokens(candidate.title):
        return False
    if candidate.kind == "video":
        title = candidate.title.lower().replace("-", " ").replace('"', "")
        if any(word in title for word in _COMPILATION_WORDS):
            return False
        if not candidate.duration_s or candidate.duration_s > SHORT_CLIP_S:
            return False
    return True


def timed_excerpt_ok(candidate: Candidate, start_s: float, window_s: float) -> bool:
    """Whether a chosen window of a long film may be used.

    A search still refuses a film longer than four minutes, because a
    random slice of it is a guess at what is on screen. A window is
    allowed when the caller names where it starts, it is at most
    ``MAX_TIMED_WINDOW_S`` long, it sits inside the file, and the file
    would have passed every other rule (licence already checked, title,
    junk words) had it been short.
    """
    if candidate.kind != "video":
        return False
    if window_s <= 0 or window_s > MAX_TIMED_WINDOW_S:
        return False
    if start_s < 0 or candidate.duration_s is None:
        return False
    if start_s >= candidate.duration_s:
        return False
    short = Candidate(
        source=candidate.source,
        kind=candidate.kind,
        title=candidate.title,
        page_url=candidate.page_url,
        media_url=candidate.media_url,
        width=candidate.width,
        height=candidate.height,
        duration_s=min(candidate.duration_s, SHORT_CLIP_S),
        licence=candidate.licence,
        needs_credit=candidate.needs_credit,
        artist=candidate.artist,
        restrictions=candidate.restrictions,
    )
    return usable(short, candidate.title)


def _score(candidate: Candidate, query: str) -> float:
    score = title_match(candidate, query) * 10.0
    if candidate.width and candidate.height:
        aspect = candidate.width / candidate.height
        if aspect >= 1.3:
            score += 2.0
        elif aspect < 1.0:
            score -= 3.0
    if candidate.kind == "video" and candidate.duration_s:
        if candidate.duration_s <= SHORT_CLIP_S:
            score += 4.0
        elif candidate.duration_s > 900:
            score -= 2.0
    if any(word in candidate.title.lower() for word in _JUNK_WORDS):
        score -= 8.0
    return score


# ---------------------------------------------------------------------------
# Wikimedia Commons (through Wikipedia's API)
# ---------------------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT_S,
        follow_redirects=True,
    )


async def search_commons(query: str, kind: str, *, limit: int = SEARCH_LIMIT) -> list[Candidate]:
    """Licence-checked Commons files matching a query, best first."""
    filetype = "video" if kind == "video" else "bitmap"
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": f"{query} filetype:{filetype}",
        "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "videoinfo",
        "viprop": "url|size|mime|extmetadata|derivatives",
        "viurlwidth": "1920",
        "maxlag": "5",
    }
    response = None
    async with _client() as client:
        for attempt in range(SEARCH_ATTEMPTS):
            try:
                response = await client.get(WIKI_API, params=params)
                break
            except httpx.TransportError as exc:
                # A read-only search, so retrying a dropped connection is safe.
                if attempt == SEARCH_ATTEMPTS - 1:
                    raise StockMediaError(f"Commons search failed: {exc}") from exc
                await asyncio.sleep(2 * (attempt + 1))
    assert response is not None
    if response.status_code != 200:
        raise StockMediaError(f"Commons search returned HTTP {response.status_code}.")
    pages = (response.json().get("query") or {}).get("pages") or []

    found: list[Candidate] = []
    for page in pages:
        infos = page.get("videoinfo") or []
        if not infos:
            continue
        info = infos[0]
        ext = info.get("extmetadata") or {}
        value = lambda key: str((ext.get(key) or {}).get("value") or "")  # noqa: E731
        verdict = classify_licence(value("LicenseShortName"), value("UsageTerms"), value("Restrictions"))
        if not verdict.allowed:
            continue
        mime = str(info.get("mime") or "")
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        title = str(page.get("title") or "")
        page_url = str(info.get("descriptionurl") or "")
        if "commons.wikimedia.org" not in page_url:
            page_url = "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")

        if kind == "video":
            if not mime.startswith("video/") or width < MIN_VIDEO_WIDTH:
                continue
            derivatives = [
                d for d in (info.get("derivatives") or [])
                if "webm" in str(d.get("type")) and int(d.get("height") or 0) <= 1080
            ]
            derivatives.sort(key=lambda d: int(d.get("height") or 0))
            media_url = str(derivatives[-1]["src"]) if derivatives else str(info.get("url") or "")
            duration = float(info.get("duration") or 0.0) or None
        else:
            if mime not in {"image/jpeg", "image/png", "image/webp"} or width < MIN_IMAGE_WIDTH:
                continue
            media_url = str(info.get("thumburl") or info.get("url") or "")
            duration = None
        if not media_url:
            continue

        candidate = Candidate(
            source="Wikimedia Commons",
            kind=kind,
            title=title,
            page_url=page_url,
            media_url=media_url,
            width=width,
            height=height,
            duration_s=duration,
            licence=verdict.label,
            needs_credit=verdict.needs_credit,
            artist=_artist(value("Artist")),
            restrictions=value("Restrictions"),
        )
        candidate.score = _score(candidate, query)
        found.append(candidate)
    found.sort(key=lambda c: c.score, reverse=True)
    return found


# ---------------------------------------------------------------------------
# Pixabay (optional, needs a free key)
# ---------------------------------------------------------------------------


def _pixabay_cache(kind: str, query: str) -> Path:
    digest = hashlib.sha256(f"{kind}:{query}".encode()).hexdigest()[:20]
    return get_settings().tmp_dir / "stock-cache" / f"pixabay-{digest}.json"


async def search_pixabay(query: str, kind: str, *, limit: int = SEARCH_LIMIT) -> list[Candidate]:
    key = get_settings().pixabay_api_key
    if not key:
        return []
    cache = _pixabay_cache(kind, query)
    payload: dict[str, Any] | None = None
    if cache.exists() and time.time() - cache.stat().st_mtime < PIXABAY_CACHE_S:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    if payload is None:
        url = PIXABAY_VIDEO_API if kind == "video" else PIXABAY_IMAGE_API
        params = {"key": key, "q": query[:100], "per_page": str(max(3, min(limit, 200))), "safesearch": "true"}
        if kind == "image":
            params.update({"image_type": "photo", "orientation": "horizontal", "min_width": str(MIN_IMAGE_WIDTH)})
        async with _client() as client:
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise StockMediaError(f"Pixabay search failed: {exc}") from exc
        if response.status_code == 429:
            raise StockMediaError("Pixabay's rate limit was reached. Try again in a minute.")
        if response.status_code != 200:
            raise StockMediaError(f"Pixabay search returned HTTP {response.status_code}.")
        payload = response.json()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload), encoding="utf-8")

    found: list[Candidate] = []
    for hit in payload.get("hits") or []:
        if kind == "video":
            files = hit.get("videos") or {}
            best = files.get("large") or files.get("medium") or {}
            if not best.get("url"):
                continue
            width, height = int(best.get("width") or 0), int(best.get("height") or 0)
            media_url, duration = str(best["url"]), float(hit.get("duration") or 0) or None
        else:
            media_url = str(hit.get("largeImageURL") or "")
            width, height = int(hit.get("imageWidth") or 0), int(hit.get("imageHeight") or 0)
            duration = None
        if not media_url:
            continue
        candidate = Candidate(
            source="Pixabay",
            kind=kind,
            title=str(hit.get("tags") or query),
            page_url=str(hit.get("pageURL") or ""),
            media_url=media_url,
            width=width,
            height=height,
            duration_s=duration,
            licence="Pixabay Content License",
            needs_credit=False,
            artist=str(hit.get("user") or ""),
        )
        candidate.score = _score(candidate, query) + 1.0
        found.append(candidate)
    found.sort(key=lambda c: c.score, reverse=True)
    return found


async def search(query: str, kind: str) -> list[Candidate]:
    """Every source that is available, best result first."""
    results: list[Candidate] = []
    problems: list[str] = []
    for searcher in (search_commons, search_pixabay):
        try:
            results.extend(c for c in await searcher(query, kind) if usable(c, query))
        except StockMediaError as exc:
            problems.append(str(exc))
    if not results and problems:
        raise StockMediaError("; ".join(problems))
    results.sort(key=lambda c: c.score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@dataclass
class Fetched:
    candidate: Candidate
    #: The file the library scans: the photo itself, or a frame of the footage.
    still_path: Path
    #: A short local excerpt of the footage, when the shot is footage.
    footage_path: Path | None = None
    footage_s: float | None = None


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", re.sub(r"^file:", "", text.lower())).strip("-")
    return cleaned[:60] or "media"


def write_credit(path: Path, candidate: Candidate) -> None:
    data = asdict(candidate)
    data["credit_line"] = candidate.credit_line
    path.with_name(path.name + ".credit.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_credit(path: Path | str | None) -> dict[str, Any] | None:
    if not path:
        return None
    sidecar = Path(str(path) + ".credit.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _run(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 1, "FFmpeg timed out."
    return process.returncode or 0, stderr.decode("utf-8", "replace")


async def _download(url: str, destination: Path) -> None:
    partial = destination.with_suffix(destination.suffix + ".part")
    async with _client() as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise StockMediaError(f"Download returned HTTP {response.status_code}.")
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(1 << 16):
                    handle.write(chunk)
    partial.replace(destination)


async def mean_luma(ffmpeg_path: str, path: Path) -> float | None:
    """Average brightness of one frame a second, or None if unmeasurable."""
    code, detail = await _run([
        ffmpeg_path, "-v", "info", "-i", str(path),
        "-vf", "fps=1,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
        "-f", "null", "-",
    ])
    values = [float(v) for v in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", detail)]
    if code or not values:
        return None
    return sum(values) / len(values)


def excerpt_start(duration_s: float | None, wanted_s: float) -> float:
    """Where to cut from: past any opening slate, inside the file."""
    if not duration_s or duration_s <= wanted_s:
        return 0.0
    if duration_s <= SHORT_CLIP_S:
        return round(min(duration_s * 0.15, duration_s - wanted_s), 2)
    # A whole film: its middle is likelier to be the event than its titles.
    return round(min(duration_s * 0.35, duration_s - wanted_s), 2)


async def fetch(
    candidate: Candidate,
    folder: Path,
    *,
    ffmpeg_path: str,
    name: str,
    start_s: float | None = None,
) -> Fetched:
    """Save one accepted file locally, with its credit, ready for the edit."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{_slug(candidate.title)}"
    if candidate.kind == "image":
        suffix = Path(candidate.media_url.split("?")[0]).suffix.lower() or ".jpg"
        still = folder / f"{stem}{suffix if suffix in {'.jpg', '.jpeg', '.png', '.webp'} else '.jpg'}"
        if not still.exists():
            await _download(candidate.media_url, still)
            luma = await mean_luma(ffmpeg_path, still)
            if luma is not None and luma > MAX_PHOTO_LUMA:
                still.unlink(missing_ok=True)
                raise StockMediaError(
                    f"This looks like a scanned page, not a photo (brightness {luma:.0f} of 255)."
                )
        write_credit(still, candidate)
        return Fetched(candidate, still)

    footage = folder / f"{stem}.mp4"
    start = excerpt_start(candidate.duration_s, EXCERPT_S) if start_s is None else start_s
    candidate.extra["excerpt_start_s"] = start
    if not footage.exists():
        partial = footage.with_suffix(".part.mp4")
        code, detail = await _run([
            ffmpeg_path, "-y", "-v", "error",
            "-user_agent", USER_AGENT,
            "-ss", f"{start:.2f}",
            "-i", candidate.media_url,
            "-t", f"{EXCERPT_S:.2f}",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-threads", "2",
            str(partial),
        ])
        if code or not partial.exists():
            partial.unlink(missing_ok=True)
            raise StockMediaError(f"The footage could not be cut: {detail.strip()[-300:]}")
        luma = await mean_luma(ffmpeg_path, partial)
        if luma is not None and luma < MIN_EXCERPT_LUMA:
            partial.unlink(missing_ok=True)
            raise StockMediaError(
                f"The excerpt is too dark to read (mean brightness {luma:.0f} of 255)."
            )
        partial.replace(footage)
    write_credit(footage, candidate)

    still = folder / f"{stem}.jpg"
    if not still.exists():
        code, detail = await _run([
            ffmpeg_path, "-y", "-v", "error", "-ss", "3", "-i", str(footage),
            "-frames:v", "1", "-q:v", "2", str(still),
        ])
        if code or not still.exists():
            raise StockMediaError(f"No frame could be taken from the footage: {detail.strip()[-300:]}")
    write_credit(still, candidate)
    return Fetched(candidate, still, footage)


# ---------------------------------------------------------------------------
# Building a shot from footage
# ---------------------------------------------------------------------------


def footage_command(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    source_s: float,
    start_s: float = 0.0,
) -> list[str]:
    """Fill the frame with footage of any shape, for exactly ``duration_s``.

    Old 4:3 film is not stretched or cropped to death: it sits at full
    height over a blurred copy of itself. A source shorter than the shot is
    slowed, at most to two-thirds speed, and then looped.
    """
    frames = int(round(duration_s * fps))
    remaining = max(source_s - start_s, 0.0) if source_s else 0.0
    slow = 1.0
    if remaining and remaining < duration_s:
        slow = min(duration_s / remaining, 1.5)
    graph = (
        f"[0:v]setpts={slow:.4f}*PTS,fps={fps},split[a][b];"
        f"[a]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=24:2,eq=brightness=-0.08[bg];"
        f"[b]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )
    return [
        ffmpeg_path, "-y", "-v", "error",
        "-stream_loop", "-1",
        "-ss", f"{start_s:.3f}",
        "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]",
        "-an",
        "-frames:v", str(frames),
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-threads", "2",
        str(destination),
    ]


async def render_footage(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    source_s: float,
    start_s: float = 0.0,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part.mp4")
    command = footage_command(
        ffmpeg_path, source, partial,
        width=width, height=height, fps=fps, duration_s=duration_s,
        source_s=source_s, start_s=start_s,
    )
    code, detail = await _run(command)
    if code or not partial.exists():
        partial.unlink(missing_ok=True)
        raise StockMediaError(f"The footage shot could not be made: {detail.strip()[-300:]}")
    destination.unlink(missing_ok=True)
    partial.replace(destination)


# ---------------------------------------------------------------------------
# Credits for publishing
# ---------------------------------------------------------------------------


async def credits_for_scenes(session: AsyncSession, scenes: list[Scene]) -> list[str]:
    """One credit line per library file that is in the edit, in shot order."""
    lines: list[str] = []
    for scene in scenes:
        clip = (
            await session.execute(
                select(SceneClip)
                .where(SceneClip.scene_id == scene.id, SceneClip.selected.is_(True))
                .order_by(SceneClip.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        credit = None
        if clip is not None:
            credit = (clip.validation or {}).get("credit")
        if credit is None and scene.image_id is not None:
            image = await session.get(Image, scene.image_id)
            data = read_credit(image.path if image else None)
            credit = data.get("credit_line") if data else None
        if credit and credit not in lines:
            lines.append(str(credit))
    return lines
