"""What the Book Video page needs beyond the pipeline itself.

* **Check** a pasted script before anything is spent: words, length,
  chapters, how long it will take on this laptop, disk space, warnings.
* **Covers**: an uploaded picture, one from the Image Library, or one picked
  from a Wikimedia Commons search, kept under ``book-video/covers``.
* **Queue** a book (or a fifteen-second preview) as a ``make_book.py`` job.
* **Jobs**: each queued book with its progress, and once finished the
  upload kit: the video, MP3, thumbnail, title, description and tags.
* **Disk**: free space, working files that can be cleaned, and the cleanup.
* **Remembered settings**: the last voice, template and options used.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services import book_package as pack
from app.services import book_queue
from app.services import book_text
from app.services import book_video
from app.services import files as files_service
from app.services import video_templates
from app.services import visualizer
from app.services.paths import safe_filename

STUDIO_DIR = "book-video"
SETTINGS_FILE = "settings.json"
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
MAX_COVER_BYTES = 25 * 1024 * 1024
COVER_MAX_SIDE = 2000
COMMONS_HOSTS = ("upload.wikimedia.org", "thumb.wikimedia.org")
DEFAULTS: dict[str, Any] = {
    "voice": "piper:en_US-joe-medium", "template": video_templates.DEFAULT_ID, "kind": "summary",
    "language": "en", "speed": 1.0, "height": 1080, "channel": book_video.DEFAULT_CHANNEL,
    "style": "", "accent": "", "video": "animated",
}
#: Disk the page warns about: a book needs WAVs and segments while it is made.
LOW_DISK_GB = 3.0


class StudioError(Exception):
    """Something on the page cannot be done as asked."""


def studio_dir(*parts: str) -> Path:
    folder = get_settings().data_root / STUDIO_DIR
    for part in parts:
        folder = folder / part
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# --- Remembered settings --------------------------------------------------------------


def load_settings() -> dict[str, Any]:
    try:
        saved = json.loads((studio_dir() / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(values: dict[str, Any]) -> None:
    keep = {k: values[k] for k in DEFAULTS if k in values}
    book_video.write_json(studio_dir() / SETTINGS_FILE, {**load_settings(), **keep})


# --- The script check ------------------------------------------------------------------


def check_script(text: str, *, title: str, author: str = "", kind: str = "summary", language: str = "en",
                 voice: str = "", template: str = "", height: int = 1080, video: str = "animated",
                 channel: str = book_video.DEFAULT_CHANNEL) -> dict[str, Any]:
    """Everything worth knowing about a script before it is made."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "empty": True, "errors": ["Paste or upload a script first."], "warnings": []}
    errors: list[str] = []
    warnings: list[str] = []
    if kind not in book_text.KINDS:
        kind = "summary"
    if language not in book_text.LANGUAGES:
        language = "en"
    book = book_text.parse_script(text, title=title or "Untitled", author=author, language=language, kind=kind)
    full = book_text.finished_script(book, channel)
    engine = (voice or book_video.DEFAULT_VOICES.get(language, "kokoro:x")).partition(":")[0]
    animated = video == "animated" and language == "en"
    look = video_templates.look_for(video_templates.get(template), height=height) if animated else None
    info = book_video.plan(book, full, engine, studio_dir(), look)
    if not title.strip():
        errors.append("Give the book a title.")
    if video == "animated" and language != "en":
        warnings.append("Live captions are English only for now, so this book gets the chapter-card video.")
    if info["words"] < 120:
        warnings.append("The script is very short; the video will be under a minute.")
    if info["audio_s"] > 10 * 3600:
        warnings.append("That is more than ten hours of audio. Consider splitting it into parts.")
    if language == "en" and _mostly_non_latin(text):
        warnings.append("The script does not look like English. Pick its language so the right voice reads it.")
    if language != "en" and engine in ("kokoro", "piper") and not voice.startswith("kokoro:h"):
        warnings.append("This voice speaks English; pick an Urdu or Hindi voice for this script.")
    if not info["disk_ok"]:
        warnings.append(f"Only {info['free_gb']:.1f} GB is free on {info['drive']}; the book needs about "
                        f"{info['wav_gb'] + info['video_gb'] + 1:.1f} GB while it is made.")
    warnings += info["warnings"]
    est = info["estimates"]
    return {
        "ok": not errors, "empty": False, "errors": errors, "warnings": warnings,
        "words": info["words"], "characters": info["characters"],
        "audio": pack.duration_label(info["audio_s"]),
        "work": pack.duration_label(info["total_work_s"]),
        "speak": pack.duration_label(est["speak_s"]), "draw": pack.duration_label(est["draw_s"]),
        "chapters": info["chapter_titles"], "free_gb": round(info["free_gb"], 1),
        "need_gb": round(info["wav_gb"] + info["video_gb"], 2), "animated": animated,
        "engine": engine,
    }


def _mostly_non_latin(text: str) -> bool:
    sample = re.sub(r"\s+", "", text[:3000])
    if not sample:
        return False
    other = sum(1 for ch in sample if ord(ch) > 0x024F)
    return other / len(sample) > 0.5


# --- Covers --------------------------------------------------------------------------------


def save_cover(data: bytes, filename: str = "") -> Path:
    """Keep an uploaded picture as a JPEG no larger than 2000 px a side."""
    from PIL import Image, UnidentifiedImageError

    if not data:
        raise StudioError("The picture is empty.")
    if len(data) > MAX_COVER_BYTES:
        raise StudioError("That picture is larger than 25 MB.")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise StudioError(f"That file is not a picture this app can read: {exc}") from exc
    image = image.convert("RGB")
    image.thumbnail((COVER_MAX_SIDE, COVER_MAX_SIDE))
    if min(image.size) < 300:
        raise StudioError("That picture is too small; use one at least 300 pixels on its short side.")
    digest = hashlib.sha1(data).hexdigest()[:14]
    stem = safe_filename(Path(filename).stem, fallback="cover")[:40] if filename else "cover"
    destination = studio_dir("covers") / f"{stem}-{digest}.jpg"
    if not destination.is_file():
        partial = destination.with_suffix(".part.jpg")
        image.save(partial, quality=92)
        partial.replace(destination)
    return destination


def cover_rel(path: Path) -> str:
    try:
        return files_service.relative(path)
    except ValueError:
        return ""


def cover_from(value: str) -> Path | None:
    """The cover the form names: a path under the data root, or an Image Library file."""
    value = (value or "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        try:
            path = files_service.resolve(value)
        except files_service.FilesError as exc:
            raise StudioError(f"The cover picture was not found: {exc}") from exc
    if not path.is_file():
        raise StudioError(f"The cover picture {path} is missing.")
    return path


async def commons_search(query: str) -> list[dict[str, Any]]:
    from app.services import stock_media

    if not query.strip():
        return []
    try:
        found = await stock_media.search_commons(query.strip(), "image")
    except stock_media.StockMediaError as exc:
        raise StudioError(str(exc)) from exc
    rows = []
    for candidate in found[:12]:
        thumb = re.sub(r"/\d+px-", "/330px-", candidate.media_url) if "/thumb/" in candidate.media_url \
            else candidate.media_url
        rows.append({"title": re.sub(r"^File:", "", candidate.title), "thumb": thumb, "url": candidate.media_url,
                     "credit": candidate.credit_line, "licence": candidate.licence,
                     "size": f"{candidate.width}x{candidate.height}"})
    return rows


async def commons_fetch(url: str, credit: str) -> Path:
    import httpx

    host = re.sub(r"^https://([^/]+)/.*$", r"\1", url or "")
    if host not in COMMONS_HOSTS:
        raise StudioError("Only pictures from Wikimedia Commons can be fetched here.")
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
    headers = {"User-Agent": "AIVideoManager/1.0 (local audiobook tool)"}
    async with httpx.AsyncClient(transport=transport, timeout=60, follow_redirects=True, headers=headers) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise StudioError(f"The picture could not be downloaded: {exc}") from exc
    path = save_cover(response.content, Path(url).name)
    path.with_suffix(".credit.txt").write_text(credit or "", encoding="utf-8")
    return path


def cover_credit(path: Path | None) -> str:
    if path is None:
        return ""
    note = path.with_suffix(".credit.txt")
    try:
        return note.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def template_preview(template_id: str, cover: Path | None) -> Path:
    return video_templates.preview(video_templates.get(template_id), cover)


# --- Queueing a book ------------------------------------------------------------------------


@dataclass
class Order:
    script: str
    title: str
    author: str = ""
    kind: str = "summary"
    language: str = "en"
    voice: str = ""
    speed: float = 1.0
    template: str = video_templates.DEFAULT_ID
    style: str = ""
    accent: str = ""
    height: int = 1080
    video: str = "animated"
    channel: str = book_video.DEFAULT_CHANNEL
    cover: str = ""
    art_query: str = ""
    about: str = ""
    name: str = ""
    preview: bool = False


def _validate(order: Order) -> None:
    if not order.script.strip():
        raise StudioError("Paste or upload a script first.")
    if len(order.script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise StudioError("The script is larger than 8 MB of text.")
    if not order.title.strip():
        raise StudioError("Give the book a title.")
    if order.kind not in book_text.KINDS:
        raise StudioError(f"Kind must be one of {', '.join(book_text.KINDS)}.")
    if order.language not in book_text.LANGUAGES:
        raise StudioError(f"Language must be one of {', '.join(book_text.LANGUAGES)}.")
    if order.template not in video_templates.BY_ID:
        raise StudioError("Pick one of the templates.")
    if order.style and order.style not in visualizer.STYLES:
        raise StudioError(f"Ring style must be one of {', '.join(visualizer.STYLES)}.")
    if order.accent:
        try:
            visualizer.parse_accent(order.accent)
        except ValueError as exc:
            raise StudioError(str(exc)) from exc
    if order.height not in (720, 1080):
        raise StudioError("Height must be 720 or 1080.")
    if order.video not in ("animated", "cards"):
        raise StudioError("Video must be animated or cards.")
    if order.voice:
        book_video.voice_for(order.voice, order.speed)
    if not 0.5 <= order.speed <= 2.0:
        raise StudioError("Speed must be between 0.5 and 2.")


def argv_for(order: Order, script_path: Path, cover: Path | None) -> list[str]:
    name = order.name.strip() or f"{order.title.strip()} - {order.language}"
    argv = ["--text", str(script_path), "--title", order.title.strip(), "--author", order.author.strip(),
            "--kind", order.kind, "--lang", order.language, "--speed", f"{order.speed:g}",
            "--template", order.template, "--height", str(order.height), "--video", order.video,
            "--channel", order.channel.strip() or book_video.DEFAULT_CHANNEL, "--name", name]
    if order.voice:
        argv += ["--voice", order.voice]
    if order.style:
        argv += ["--style", order.style]
    if order.accent:
        argv += ["--accent", order.accent]
    if cover is not None:
        argv += ["--art", str(cover)]
        credit = cover_credit(cover)
        if credit:
            argv += ["--art-credit", credit]
    elif order.art_query.strip():
        argv += ["--art-query", order.art_query.strip()]
    if order.about.strip():
        argv += ["--about", order.about.strip()]
    if order.preview:
        argv += ["--preview"]
    return argv


def queue_book(order: Order) -> dict[str, Any]:
    """Save the script, queue the job, start the runner. Returns the job id and folder."""
    try:
        _validate(order)
    except book_video.BookVideoError as exc:
        raise StudioError(str(exc)) from exc
    cover = cover_from(order.cover)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = safe_filename(order.title, fallback="book")[:50].strip(" .") or "book"
    script_path = studio_dir("scripts") / f"{slug}-{stamp}.md"
    script_path.write_text(order.script.replace("\r\n", "\n"), encoding="utf-8")
    argv = argv_for(order, script_path, cover)
    name = argv[argv.index("--name") + 1]
    folder = book_video.folder_for(book_video.BookVideoRequest(name=name, preview=order.preview))
    folder.mkdir(parents=True, exist_ok=True)
    label = f"{order.title.strip()} (preview)" if order.preview else order.title.strip()
    job_id = book_queue.add(argv, book_folder=folder, label=label, preview=order.preview)
    save_settings({"voice": order.voice, "template": order.template, "kind": order.kind,
                   "language": order.language, "speed": order.speed, "height": order.height,
                   "channel": order.channel, "style": order.style, "accent": order.accent,
                   "video": order.video})
    started = book_queue.ensure_runner()
    return {"job": job_id, "folder": str(folder), "runner_started": started}


# --- Jobs and the upload kit -----------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _rel(path: Path) -> str:
    try:
        return files_service.relative(path) if path.is_file() else ""
    except ValueError:
        return ""


def upload_kit(folder: Path) -> dict[str, Any] | None:
    video = folder / "audiobook.mp4"
    if not video.is_file():
        return None
    qa: dict[str, Any] = {}
    try:
        qa = json.loads((folder / "qa.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "video": _rel(video), "mp3": _rel(folder / "audiobook.mp3"), "thumbnail": _rel(folder / "thumbnail.jpg"),
        "title": _read_text(folder / "title.txt"), "description": _read_text(folder / "description.txt"),
        "tags": _read_text(folder / "tags.txt"), "duration": pack.duration_label(float(qa.get("duration_s") or 0)),
        "loudness": qa.get("loudness_note", ""), "loudness_ok": qa.get("loudness_ok"),
        "silences": len(qa.get("long_silences") or []), "folder_rel": cover_rel(folder),
        "size_mb": round(video.stat().st_size / 1e6, 1),
    }


def _clock(seconds: float | None) -> str:
    total = int(seconds or 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _saved_chapters(folder: Path) -> tuple[int, int, list[str]]:
    """Chapters already spoken, and the playable WAV of each finished one."""
    done = total = 0
    book_file = folder / "book.json"
    if book_file.is_file():
        try:
            data = json.loads(book_file.read_text(encoding="utf-8"))
            done = int(data.get("chapters_done") or 0)
            total = int(data.get("chapters_total") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    playable: list[str] = []
    work = folder / "chapters"
    if work.is_dir():
        for meta in sorted(work.glob("[0-9][0-9][0-9].json")):
            wav = meta.with_suffix(".wav")
            if wav.is_file() and wav.stat().st_size >= 1024:
                rel = cover_rel(wav)
                if rel:
                    playable.append(rel)
    return done, total, playable


#: The opening and closing lines the pipeline adds around every script.
FRAME_CHARS = 400
PREVIEW_WORK_S = 90.0


def _argv_option(argv: list[str], name: str, default: str = "") -> str:
    for i, item in enumerate(argv[:-1]):
        if item == name:
            return argv[i + 1]
    return default


@functools.lru_cache(maxsize=64)
def _script_chars(path: str, mtime: float) -> int:
    return len(Path(path).read_text(encoding="utf-8", errors="replace"))


def work_left(job: dict[str, Any], done: int = 0, total: int = 0) -> float | None:
    """Seconds of work a waiting job still needs, from its script length and options."""
    argv = job.get("argv") or []
    if "--preview" in argv or job.get("preview"):
        return PREVIEW_WORK_S
    script = Path(_argv_option(argv, "--text"))
    if not script.name:
        return None
    try:
        chars = _script_chars(str(script), script.stat().st_mtime) + FRAME_CHARS
    except OSError:
        return None
    language = _argv_option(argv, "--lang", "en")
    engine = _argv_option(argv, "--voice", book_video.DEFAULT_VOICES.get(language, "")).partition(":")[0]
    try:
        height = int(_argv_option(argv, "--height", "1080"))
    except ValueError:
        height = 1080
    animated = _argv_option(argv, "--video", "animated") == "animated" and language == "en"
    template = video_templates.get(_argv_option(argv, "--template", video_templates.DEFAULT_ID))
    look = video_templates.look_for(template, height=height) if animated else None
    est = book_video.estimates_for_chars(chars, engine, look)
    spoken = done / total if total else 0.0
    return est["speak_s"] * (1 - spoken) + est["timing_s"] + est["draw_s"] + est["pack_s"]


def when_label(moment: float, now: float | None = None) -> str:
    """``today 4:30 PM``, ``tomorrow 9:05 AM`` or ``Tue 3:00 PM``."""
    now = time.time() if now is None else now
    at, today = time.localtime(moment), time.localtime(now)
    clock = time.strftime("%I:%M %p", at).lstrip("0")
    days = (time.mktime((at.tm_year, at.tm_mon, at.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
    if round(days) == 0:
        return f"today {clock}"
    if round(days) == 1:
        return f"tomorrow {clock}"
    return time.strftime("%a ", at) + clock


def job_rows() -> list[dict[str, Any]]:
    rows = []
    for job in book_queue.list_jobs():
        folder = Path(job.get("folder") or "")
        named = bool(str(job.get("folder") or ""))
        exists = named and folder.is_dir()
        progress = book_video.read_progress(folder) if exists else None
        kit = upload_kit(folder) if job["state"] == "done" and exists else None
        left = progress.get("seconds_left") if progress else None
        done, total, playable = _saved_chapters(folder) if exists else (0, 0, [])
        if job["state"] == "running":
            left_s = float(left) if left else None
        elif job["state"] == "waiting":
            left_s = work_left(job, done, total)
        else:
            left_s = None
        rows.append({
            "id": job["id"], "state": job["state"], "title": job.get("title") or job["id"],
            "preview": bool(job.get("preview")), "attempts": job.get("attempts", 0),
            "added": job.get("added", ""), "finished": job.get("finished", ""),
            "error": job.get("last_error", ""), "progress": progress,
            "left": pack.duration_label(left) if left else "",
            "left_s": left_s, "work": pack.duration_label(left_s) if left_s else "", "ready_at": "",
            "elapsed": _clock(progress.get("elapsed_s")) if progress else "",
            "folder": str(folder) if named else "",
            "folder_rel": cover_rel(folder) if exists else "",
            "chapters_done": done, "chapters_total": total, "playable": playable,
            "kit": kit,
            "work_mb": round(book_video.work_bytes(folder) / 1e6) if exists else 0,
            "log": book_queue.log_tail(job["id"], 8) if job["state"] in ("running", "failed") else "",
        })
    return rows


def queue_summary(rows: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """The running job, and when each waiting job should be ready if they run in order."""
    now = time.time() if now is None else now
    active = sorted((r for r in rows if r["state"] in ("running", "waiting")),
                    key=lambda r: r["state"] != "running")
    total, known = 0.0, True
    for row in active:
        if row.get("left_s") is None:
            known = False
            continue
        total += row["left_s"]
        row["ready_at"] = when_label(now + total, now)
    return {
        "running": next((r for r in rows if r["state"] == "running"), None),
        "waiting": sum(1 for r in rows if r["state"] == "waiting"),
        "runner": bool(book_queue.runner_pid()),
        "total_left": pack.duration_label(total) if active and total else "",
        "finish_at": when_label(now + total, now) if active and total else "",
        "estimate_complete": known,
        "books_rel": cover_rel(get_settings().data_root / "audiobooks"),
    }


def queue_status() -> dict[str, Any]:
    """What the bar on every page shows: the book being made, and the queue behind it."""
    rows = []
    for job in book_queue.list_jobs(limit_finished=0):
        folder = Path(job.get("folder") or "")
        exists = bool(str(job.get("folder") or "")) and folder.is_dir()
        progress = book_video.read_progress(folder) if exists and job["state"] == "running" else None
        if job["state"] == "running":
            left = progress.get("seconds_left") if progress else None
            left_s = float(left) if left else None
        else:
            done, total, _ = _saved_chapters(folder) if exists else (0, 0, [])
            left_s = work_left(job, done, total)
        rows.append({"id": job["id"], "state": job["state"], "title": job.get("title") or job["id"],
                     "progress": progress, "left_s": left_s,
                     "left": pack.duration_label(left_s) if left_s else ""})
    return {**queue_summary(rows), "jobs": rows}


# --- Disk and cleanup ----------------------------------------------------------------------------


def disk_status() -> dict[str, Any]:
    root = get_settings().data_root
    usage = shutil.disk_usage(root.anchor or str(root))
    books = root / "audiobooks"
    work = 0
    if books.is_dir():
        for folder in books.iterdir():
            if folder.is_dir() and not _is_active(folder):
                work += book_video.work_bytes(folder)
    free_gb = usage.free / 1e9
    return {"drive": root.anchor, "free_gb": round(free_gb, 1), "total_gb": round(usage.total / 1e9, 1),
            "low": free_gb < LOW_DISK_GB, "work_gb": round(work / 1e9, 2)}


def _is_active(folder: Path) -> bool:
    """True while a waiting or running job will still need this folder's working files."""
    for job in book_queue.list_jobs(limit_finished=0):
        if Path(job.get("folder") or "") == folder:
            return True
    return False


def clean(job_id: str | None = None) -> int:
    """Delete working files of one finished job, or of every book not waiting in the queue."""
    root = get_settings().data_root / "audiobooks"
    if job_id:
        found = book_queue.find(job_id)
        if found is None:
            raise StudioError("That job was not found.")
        if found[0] == "waiting":
            raise StudioError("That book is still in the queue; its working files are needed.")
        data = json.loads(found[1].read_text(encoding="utf-8"))
        folder = Path(data.get("folder") or "")
        if not folder.is_dir():
            return 0
        return book_video.clean_work_files(folder)
    freed = 0
    if root.is_dir():
        for folder in root.iterdir():
            if folder.is_dir() and not _is_active(folder):
                freed += book_video.clean_work_files(folder)
    return freed
