"""Everything around a finished audiobook MP3 that YouTube needs.

* **Cards.** One 1920x1080 picture per chapter: the cover art, the title,
  and the chapter now playing. A viewer who opens the tab mid-book can see
  where they are, which a single still for ten hours does not offer.
* **Video.** The cards cut to each chapter's measured length and encoded
  at one frame a second with x264's still-image tuning, the MP3 copied in
  untouched. Ten hours of 1080p comes out small and in minutes, where a
  24 fps encode would take most of a day on this laptop.
* **Thumbnail**, 1280x720.
* **Upload text.** Title, description with chapter timestamps (YouTube
  turns them into chapters), tags, credits and the AI-narration line.
* **Checks.** Measured length, loudness, and any silence long enough to
  sound like a fault.

Latin text is drawn with Pillow. Urdu and Hindi need shaping that this
Pillow build lacks (no libraqm), so those strings are drawn by FFmpeg's
``drawtext``, which is built with HarfBuzz and FriBidi here.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services import graphics
from app.services import mixing

WIDTH, HEIGHT = 1920, 1080
THUMB_W, THUMB_H = 1280, 720
ACCENT = (232, 176, 58)
INK = (12, 12, 16)
#: Noto Nastaliq Urdu draws empty boxes through this FFmpeg's FreeType;
#: Naskh shapes correctly and stays legible at thumbnail size.
URDU_FONT = "NotoNaskhArabic.ttf"
DEVANAGARI_FONT = "NotoSansDevanagari.ttf"
#: YouTube ignores a chapter list unless every chapter is at least this long.
MIN_YT_CHAPTER_S = 10.0
YT_TITLE_MAX = 100
YT_DESCRIPTION_MAX = 5000
YT_TAGS_MAX = 500
#: Silence this long inside a book is a fault, not a pause.
SILENCE_FAULT_S = 4.0
SILENCE_FLOOR_DB = -50


class PackageError(Exception):
    """A part of the upload package could not be made."""


@dataclass
class Chapter:
    title: str
    start_s: float
    duration_s: float


@dataclass
class Package:
    title: str
    author: str
    kind: str
    language: str
    channel: str
    display_title: str
    display_author: str
    duration_s: float = 0.0
    chapters: list[Chapter] = field(default_factory=list)
    art: Path | None = None
    art_credit: str = ""
    source_note: str = ""
    narrator_note: str = ""
    about: str = ""
    #: The template's look on the thumbnail and cards.
    accent: tuple[int, int, int] = ACCENT
    display_font: str = ""
    backdrop: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None


# --- Chapters ---------------------------------------------------------------


def chapters_from(folder: Path, titles: list[str]) -> list[Chapter]:
    """Chapter starts from the measured length of each recorded chapter WAV."""
    chapters: list[Chapter] = []
    start = 0.0
    for index, title in enumerate(titles, start=1):
        meta = folder / "chapters" / f"{index:03d}.json"
        try:
            duration = float(json.loads(meta.read_text(encoding="utf-8"))["duration_s"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise PackageError(f"Chapter {index} has no measured length ({meta.name}): {exc}") from exc
        if title or not chapters:
            chapters.append(Chapter(title or "Start", start, duration))
        else:
            chapters[-1].duration_s += duration
        start += duration
    return chapters


def youtube_chapters(chapters: list[Chapter]) -> list[Chapter]:
    """Merge anything shorter than YouTube's minimum into its neighbour."""
    merged: list[Chapter] = []
    for chapter in chapters:
        if merged and merged[-1].duration_s < MIN_YT_CHAPTER_S:
            merged[-1].duration_s += chapter.duration_s
            if len(merged) == 1:
                # A short opening becomes part of the first real chapter,
                # which still has to start at 0:00.
                merged[-1].title = chapter.title
            continue
        merged.append(Chapter(chapter.title, chapter.start_s, chapter.duration_s))
    if len(merged) > 1 and merged[-1].duration_s < MIN_YT_CHAPTER_S:
        tail = merged.pop()
        merged[-1].duration_s += tail.duration_s
    return merged


def stamp(seconds: float, long_form: bool) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if long_form else f"{minutes}:{secs:02d}"


def duration_label(seconds: float) -> str:
    hours, minutes = divmod(max(1, int(round(seconds / 60))), 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return "1 hour" if hours == 1 else f"{hours} hours"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


# --- Drawing ----------------------------------------------------------------


def is_latin(text: str) -> bool:
    return all(ord(ch) < 0x0250 or ch in "‘’“”—–…•·" for ch in text)


def _font_file(text: str) -> Path:
    if re.search(r"[\u0600-\u06FF]", text):
        return graphics.fonts_dir() / URDU_FONT
    if re.search(r"[\u0900-\u097F]", text):
        return graphics.fonts_dir() / DEVANAGARI_FONT
    return graphics.fonts_dir() / graphics.HEADING_FONT


@dataclass
class Text:
    text: str
    role: str  # display | heading | body
    size: int
    x: float
    y: float
    #: "l" anchors the left edge at x, "m" centres on x. y is the middle.
    align: str = "l"
    color: tuple[int, int, int] = (255, 255, 255)
    max_width: float = 0.0
    #: A font file in the fonts folder instead of the role's usual one.
    font: str = ""


def _pillow_font(role: str, size: int, font: str = ""):
    name = font or {"display": graphics.DISPLAY_FONT, "heading": graphics.HEADING_FONT}.get(role, graphics.BODY_FONT)
    loaded = graphics._font(name, size)
    if font:
        try:
            loaded.set_variation_by_name("Bold")
        except (OSError, ValueError, AttributeError):
            pass
    return loaded


def _fit(draw, item: Text):
    size = item.size
    font = _pillow_font(item.role, size, item.font)
    while item.max_width and draw.textlength(item.text, font=font) > item.max_width and size > 18:
        size -= 4
        font = _pillow_font(item.role, size, item.font)
    return font


def wrap_lines(text: str, role: str, size: int, max_width: float, max_lines: int = 3,
               font_file: str = "") -> tuple[list[str], int]:
    """Break a title into lines, shrinking the type until it fits."""
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    words = text.split()
    while True:
        font = _pillow_font(role, size, font_file)
        lines, line = [], ""
        for word in words:
            trial = f"{line} {word}".strip()
            if line and draw.textlength(trial, font=font) > max_width:
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
        if len(lines) <= max_lines or size <= 40:
            return lines, size
        size -= 8


def _background(art: Path | None, width: int, height: int, *, blur: int, darken: float,
                colours: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None):
    from PIL import Image, ImageFilter

    if art and art.is_file():
        image = Image.open(art).convert("RGB")
        scale = max(width / image.width, height / image.height)
        image = image.resize((int(image.width * scale) + 1, int(image.height * scale) + 1), Image.LANCZOS)
        left = (image.width - width) // 2
        top = (image.height - height) // 2
        image = image.crop((left, top, left + width, top + height))
        if blur:
            image = image.filter(ImageFilter.GaussianBlur(blur))
    else:
        low, high = colours or ((24, 22, 30), (70, 46, 28))
        image = Image.new("RGB", (width, height), low)
        top = Image.new("RGB", (width, height), high)
        mask = Image.linear_gradient("L").resize((width, height))
        image = Image.composite(image, top, mask)
    shade = Image.new("RGB", (width, height), INK)
    return Image.blend(image, shade, darken)


def _cover_panel(art: Path | None, height: int, accent: tuple[int, int, int] = ACCENT):
    """The art unblurred, as a book-cover rectangle with a thin accent frame."""
    from PIL import Image, ImageDraw

    panel_h = int(height * 0.74)
    panel_w = int(panel_h * 0.68)
    if art and art.is_file():
        image = Image.open(art).convert("RGB")
        scale = max(panel_w / image.width, panel_h / image.height)
        image = image.resize((int(image.width * scale) + 1, int(image.height * scale) + 1), Image.LANCZOS)
        left = (image.width - panel_w) // 2
        top = (image.height - panel_h) // 2
        image = image.crop((left, top, left + panel_w, top + panel_h))
    else:
        image = Image.new("RGB", (panel_w, panel_h), (40, 32, 28))
    ImageDraw.Draw(image).rectangle([0, 0, panel_w - 1, panel_h - 1], outline=accent, width=4)
    return image


def render(base, items: list[Text], destination: Path, ffmpeg_path: str) -> Path:
    """Draw the texts onto ``base`` and save it. Non-Latin text goes through FFmpeg."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(base)
    shaped: list[Text] = []
    for item in items:
        if not item.text:
            continue
        if not is_latin(item.text):
            shaped.append(item)
            continue
        font = _fit(draw, item)
        anchor = "mm" if item.align == "m" else "lm"
        draw.text((item.x, item.y), item.text, font=font, fill=item.color, anchor=anchor,
                  stroke_width=max(1, item.size // 40), stroke_fill=INK)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not shaped:
        base.save(destination)
        return destination

    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / "base.png"
        base.save(source)
        filters = []
        for index, item in enumerate(shaped):
            text_file = Path(work) / f"t{index}.txt"
            text_file.write_text(item.text, encoding="utf-8")
            size = item.size
            if item.max_width:
                # drawtext cannot measure before drawing; about 0.55 em a character.
                size = min(size, int(item.max_width / max(1, len(item.text)) / 0.55))
            x = f"{item.x}-text_w/2" if item.align == "m" else f"{item.x}"
            color = "0x{:02x}{:02x}{:02x}".format(*item.color)
            filters.append(
                "drawtext=fontfile='{font}':textfile='{tf}':fontsize={size}:fontcolor={color}:"
                "x={x}:y={y}-text_h/2:borderw=2:bordercolor=0x0c0c10".format(
                    font=_ff_path(_font_file(item.text)), tf=_ff_path(text_file),
                    size=max(size, 18), color=color, x=x, y=item.y,
                )
            )
        _run([ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
              "-vf", ",".join(filters), "-frames:v", "1", str(destination)])
    return destination


def _ff_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def _run(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    process = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode:
        detail = (process.stderr or b"").decode("utf-8", "replace").strip()
        raise PackageError(detail[-600:] or f"{Path(args[0]).name} exited with {process.returncode}.")
    return process


def _badge(kind: str, language: str) -> str:
    if kind == "summary":
        return "BOOK SUMMARY"
    if kind == "original":
        return "ORIGINAL STORY"
    return "FULL AUDIOBOOK"


def card(pkg: Package, chapter_title: str, destination: Path, ffmpeg_path: str) -> Path:
    """The picture on screen while one chapter plays."""
    accent = pkg.accent
    base = _background(pkg.art, WIDTH, HEIGHT, blur=18, darken=0.62, colours=pkg.backdrop)
    panel = _cover_panel(pkg.art, HEIGHT, accent)
    panel_x = WIDTH - panel.width - 130
    base.paste(panel, (panel_x, (HEIGHT - panel.height) // 2 - 30))
    text_w = panel_x - 230
    items = [Text(pkg.channel.upper(), "heading", 40, 140, 150, color=accent, max_width=text_w),
             Text(_badge(pkg.kind, pkg.language), "heading", 34, 140, 210, color=(220, 220, 220))]
    lines, size = wrap_lines(pkg.display_title.upper(), "display", 150, text_w, font_file=pkg.display_font)
    y = 420 - (len(lines) - 1) * size * 0.45
    for line in lines:
        items.append(Text(line, "display", size, 140, y, max_width=text_w, font=pkg.display_font))
        y += size * 0.95
    if pkg.display_author:
        items.append(Text(pkg.display_author, "heading", 56, 140, y + 30, color=accent, max_width=text_w))
    if chapter_title:
        items.append(Text("NOW PLAYING", "heading", 30, 140, HEIGHT - 190, color=(200, 200, 200)))
        items.append(Text(chapter_title, "heading", 54, 140, HEIGHT - 135, max_width=text_w + 60))
    return render(base, items, destination, ffmpeg_path)


def thumbnail(pkg: Package, destination: Path, ffmpeg_path: str) -> Path:
    from PIL import ImageDraw

    accent = pkg.accent
    base = _background(pkg.art, THUMB_W, THUMB_H, blur=0, darken=0.35, colours=pkg.backdrop)
    shade = ImageDraw.Draw(base, "RGBA")
    shade.rectangle([0, 0, int(THUMB_W * 0.62), THUMB_H], fill=(*INK, 190))
    text_w = THUMB_W * 0.56
    lines, size = wrap_lines(pkg.display_title.upper(), "display", 150, text_w, max_lines=3,
                             font_file=pkg.display_font)
    y = THUMB_H * 0.40 - (len(lines) - 1) * size * 0.48
    items = []
    for line in lines:
        items.append(Text(line, "display", size, 50, y, max_width=text_w, font=pkg.display_font))
        y += size * 0.96
    if pkg.display_author:
        items.append(Text(pkg.display_author.upper(), "heading", 46, 52, y + 18, color=accent, max_width=text_w))
    badge = f"{_badge(pkg.kind, pkg.language)}  •  {duration_label(pkg.duration_s).upper()}"
    shade.rectangle([40, THUMB_H - 110, 40 + 26 * len(badge) + 30, THUMB_H - 44], fill=(*accent, 255))
    items.append(Text(badge, "heading", 40, 58, THUMB_H - 77, color=INK))
    return render(base, items, destination, ffmpeg_path)


# --- Video ------------------------------------------------------------------


def badge_label(kind: str) -> str:
    return _badge(kind, "").title()


def build_video(
    pkg: Package,
    mp3: Path,
    destination: Path,
    work: Path,
    ffmpeg_path: str,
    fps: int = 1,
    *,
    mode: str = "cards",
    look: Any = None,
    timeline: Any = None,
    ffprobe_path: str = "",
    log_line: Any = None,
    progress: Any = None,
) -> Path:
    """The upload video: chapter cards at ``fps``, or the animated pulse-and-captions look.

    ``animated`` needs ``look`` (a :class:`visualizer.Look`), the book's
    ``timeline`` of word times, and ffprobe to check each segment.
    """
    if mode == "animated":
        from app.services import visualizer

        visualizer.render_video(
            timeline, look, mp3, destination, work, ffmpeg_path, ffprobe_path,
            art=pkg.art, title=pkg.display_title, author=pkg.display_author,
            channel=pkg.channel, badge=badge_label(pkg.kind), log_line=log_line, progress=progress,
        )
        return destination
    return _card_video(pkg, mp3, destination, work, ffmpeg_path, fps)


def _card_video(
    pkg: Package,
    mp3: Path,
    destination: Path,
    work: Path,
    ffmpeg_path: str,
    fps: int = 1,
) -> Path:
    """One card per chapter, cut to its measured length, over the MP3."""
    cards_dir = work / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    listing: list[str] = []
    last = None
    for index, chapter in enumerate(pkg.chapters, start=1):
        png = card(pkg, chapter.title, cards_dir / f"{index:03d}.png", ffmpeg_path)
        escaped = png.resolve().as_posix().replace("'", r"'\''")
        listing += [f"file '{escaped}'", f"duration {chapter.duration_s:.3f}"]
        last = escaped
    # The concat demuxer drops the last duration unless the file is repeated.
    listing.append(f"file '{last}'")
    concat = work / "cards.txt"
    concat.write_text("\n".join(listing) + "\n", encoding="utf-8")
    partial = destination.with_name(destination.stem + ".part.mp4")
    partial.unlink(missing_ok=True)
    _run([
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-i", str(mp3),
        "-map", "0:v", "-map", "1:a",
        "-vf", f"fps={fps},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "22",
        # The picture only changes between chapters. A keyframe every five
        # minutes keeps ten hours near the size of the audio; YouTube
        # re-encodes for playback, so seeking is unaffected.
        "-g", str(max(fps * 300, 1)),
        "-c:a", "copy", "-shortest", "-movflags", "+faststart",
        str(partial),
    ])
    destination.unlink(missing_ok=True)
    partial.replace(destination)
    return destination


# --- Upload text --------------------------------------------------------------


def youtube_title(pkg: Package) -> str:
    by = f" by {pkg.author}" if pkg.author else ""
    if pkg.kind == "summary":
        title = f"{pkg.title}{by} | Book Summary"
    elif pkg.kind == "original":
        title = f"{pkg.title} | Full Story | {pkg.channel}"
    else:
        title = f"{pkg.title}{by} | Full Audiobook"
        if pkg.duration_s >= 3600:
            title += f" ({duration_label(pkg.duration_s)})"
    return title[:YT_TITLE_MAX]


def _hashtag(text: str) -> str:
    return "#" + re.sub(r"[^0-9A-Za-z\u0600-\u06FF\u0900-\u097F]", "", text.title())


def description(pkg: Package) -> str:
    chapters = youtube_chapters(pkg.chapters)
    long_form = pkg.duration_s >= 3600
    lines = []
    by = f" by {pkg.author}" if pkg.author else ""
    kind_line = {
        "summary": f"A complete summary of {pkg.title}{by}: every key idea, chapter by chapter.",
        "original": f"{pkg.title}, an original story from {pkg.channel}.",
    }.get(pkg.kind, f"{pkg.title}{by}, the complete and unabridged audiobook.")
    lines.append(kind_line)
    if pkg.about:
        lines += ["", pkg.about.strip()]
    if len(chapters) >= 3:
        lines += ["", "CHAPTERS"]
        for chapter in chapters:
            lines.append(f"{stamp(chapter.start_s, long_form)} {chapter.title[:70]}")
    lines += ["", "ABOUT THIS RECORDING"]
    if pkg.source_note:
        lines.append(pkg.source_note)
    if pkg.narrator_note:
        lines.append(pkg.narrator_note)
    if pkg.art_credit:
        lines.append(f"Cover art: {pkg.art_credit}")
    lines += ["", f"Subscribe to {pkg.channel} for a new audiobook regularly."]
    lines += ["", " ".join(_hashtag(t) for t in tags(pkg)[:8] if t)]
    text = "\n".join(lines)
    if len(text) > YT_DESCRIPTION_MAX and pkg.about:
        return description(Package(**{**pkg.__dict__, "about": ""}))
    return text[:YT_DESCRIPTION_MAX]


def tags(pkg: Package) -> list[str]:
    words = [pkg.title, pkg.author, f"{pkg.title} audiobook", f"{pkg.author} audiobook",
             "audiobook", "full audiobook", "audiobooks free", "unabridged audiobook",
             "audiobook for sleep", "classic literature"]
    if pkg.kind == "summary":
        words = [pkg.title, pkg.author, f"{pkg.title} summary", "book summary",
                 "audiobook summary", "self improvement", "book review"]
    if pkg.language == "ur":
        words += ["urdu audiobook", "urdu kahani", "urdu novel"]
    if pkg.language == "hi":
        words += ["hindi audiobook", "hindi kahani", "hindi story"]
    out: list[str] = []
    for word in words:
        word = word.strip()
        if word and word.lower() not in {w.lower() for w in out} and len(", ".join(out + [word])) <= YT_TAGS_MAX:
            out.append(word)
    return out


UPLOAD_DETAILS_NAME = "youtube-upload-details.txt"
_LANGUAGE_NAMES = {"en": "English", "en_us": "English", "ur": "Urdu", "hi": "Hindi"}


def language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get((code or "").replace("-", "_").lower(), code or "English")


def upload_details_text(
    *, title: str, description: str, tags: list[str], duration: str, category: str,
    language: str, made_for_kids: bool, altered: str, video: str, thumbnail: str,
    subtitles: str, book_title: str, author: str, kind: str, channel: str, voice: str,
    template: str, cover: str, source: str, thumbnail_text: str,
) -> str:
    """One file to hand to another model when rewriting the YouTube listing."""
    if kind == "summary":
        kind_note = ("Original summary written for this channel. It is not the full book. "
                     "Do not call it an unabridged audiobook.")
    elif kind == "original":
        kind_note = "An original story from this channel."
    else:
        kind_note = "The complete unabridged audiobook of a public-domain text."
    tag_line = ", ".join(tags)
    kids = "Yes" if made_for_kids else "No"
    return "\n".join([
        "YOUTUBE UPLOAD DETAILS",
        "",
        "Hand this one file to another model when you want a stronger title, description,",
        "tags, or thumbnail wording. Keep every chapter timestamp exactly as written.",
        f"A title must stay under {YT_TITLE_MAX} characters. The description must stay under "
        f"{YT_DESCRIPTION_MAX} characters. The tags together must stay under {YT_TAGS_MAX} characters.",
        "",
        "TITLE",
        title.strip(),
        "",
        "DESCRIPTION",
        description.strip(),
        "",
        "TAGS",
        tag_line,
        "",
        "LISTING",
        f"Video file: {video}",
        f"Thumbnail file: {thumbnail}",
        f"Subtitles file: {subtitles}",
        f"Duration: {duration}",
        f"Category: {category}",
        f"Language: {language_name(language)}",
        f"Made for kids: {kids}",
        f"Altered or synthetic content: {altered}",
        f"Channel: {channel}",
        "",
        "ABOUT THIS VIDEO",
        f"Book: {book_title}",
        f"Author: {author or 'Unknown'}",
        f"Kind: {kind_note}",
        f"Voice: {voice or 'Not recorded'}",
        f"Template: {template or 'Not set'}",
        f"Cover art: {cover or 'None'}",
        f"Source: {source or 'Not stated'}",
        f"Thumbnail text now: {thumbnail_text}",
        "",
    ])


def write_upload_details(folder: Path, text: str) -> Path:
    """The single listing file, beside the video."""
    path = folder / UPLOAD_DETAILS_NAME
    path.write_text(text, encoding="utf-8")
    return path


# --- Checks -------------------------------------------------------------------


def check_audio(ffmpeg_path: str, mp3: Path) -> dict[str, Any]:
    """Loudness and long silences, measured in one decode of the MP3."""
    process = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-nostats", "-i", str(mp3), "-af",
         f"ebur128=framelog=quiet,silencedetect=noise={SILENCE_FLOOR_DB}dB:d={SILENCE_FAULT_S}",
         "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stderr = (process.stderr or b"").decode("utf-8", "replace")
    loudness = mixing.parse_loudness(stderr)
    ok, verdict = mixing.loudness_verdict(loudness.get("integrated_lufs"))
    silences = []
    for match in re.finditer(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", stderr):
        end, length = float(match.group(1)), float(match.group(2))
        silences.append({"start_s": round(end - length, 1), "length_s": round(length, 1)})
    return {
        "integrated_lufs": loudness.get("integrated_lufs"),
        "loudness_ok": ok,
        "loudness_note": verdict,
        "long_silences": silences,
        "silence_ok": not silences,
    }
