"""From a script (or a Gutenberg number) to a folder ready for YouTube.

Steps: read and clean the text, add the opening and closing lines, speak
it chapter by chapter, join the MP3, time every word, draw the animated
video in the chosen template (or chapter cards), then the thumbnail,
title, description with chapter timestamps, tags and a check report.

Every stage keeps what it finished: chapters, word timings and 60-second
video segments, so running the same request again carries on from where
it stopped. ``progress.json`` in the book's folder says which stage is
running, how far it is, and roughly how long is left; the Book Video page
reads it.

A *preview* request speaks only the first few sentences, with no opening
or closing, into its own folder: about fifteen seconds of the real look.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services import audiobook
from app.services import book_package as pack
from app.services import book_text
from app.services import ffmpeg as ffmpeg_tool
from app.services import video_templates
from app.services import visualizer
from app.services import word_timing
from app.services.paths import safe_filename
from app.services.speech import split_sentences
from app.services.timeutil import utcnow

DEFAULT_CHANNEL = os.environ.get("AVM_CHANNEL_NAME", "BookInAFlash")
DEFAULT_VOICES = {"en": "piper:en_US-joe-medium", "ur": "edge:ur-PK-AsadNeural", "hi": "edge:hi-IN-MadhurNeural"}
ENGINES = ("piper", "kokoro", "edge")
#: Seconds of compute per second of audio on this laptop, measured with
#: scripts/voice_samples.py on 2026-11-11. Edge is a network figure.
SPEED_FACTOR = {"piper": 0.3, "kokoro": 1.4, "edge": 0.4}
SAMPLE_RATES = {"piper": 22_050, "kokoro": 24_000, "edge": 24_000}
DEFAULT_CPS = audiobook.DEFAULT_CHARS_PER_S
#: Seconds of Whisper tiny.en per second of narration, measured on this laptop
#: (7.1s for a 63s chapter, 2026-11-13). base.en was about as slow as the audio.
WHISPER_FACTOR = 0.12
RUN_FILE = "make_book.json"
PROGRESS_FILE = "progress.json"
PREVIEW_CHARS = 240
PREVIEW_SUFFIX = " - preview"

LogLine = Callable[[str], None]


class BookVideoError(Exception):
    """The request cannot be made as given."""


@dataclass
class BookVideoRequest:
    gutenberg: int | None = None
    text_path: Path | None = None
    title: str = ""
    author: str | None = None
    kind: str = "public-domain"
    language: str = "en"
    voice: str = ""
    speed: float = 1.0
    channel: str = DEFAULT_CHANNEL
    display_title: str = ""
    display_author: str | None = None
    art: Path | None = None
    art_credit: str = ""
    art_query: str = ""
    about: str = ""
    bitrate: str = "96k"
    video: str = "animated"
    template: str = video_templates.DEFAULT_ID
    #: Empty means the template's own ring and accent.
    style: str = ""
    accent: str = ""
    height: int = 1080
    encoder: str = "x264"
    fps: int = 0
    use_whisper: bool = True
    name: str = ""
    out: Path | None = None
    max_chapters: int = 0
    plan_only: bool = False
    no_video: bool = False
    keep_wavs: bool = False
    preview: bool = False
    #: The folder (``out``) already holds a finished audiobook from the Audiobook
    #: page: keep its script and voice, and only time, draw and package it.
    package_only: bool = False
    #: The command line that made this request, saved so ``--resume`` can repeat it.
    argv: list[str] = field(default_factory=list)


def log_to_stdout(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --- Voices and looks ------------------------------------------------------------


def voice_for(spec: str, speed: float) -> dict:
    engine, _, name = spec.partition(":")
    if engine not in ENGINES or not name:
        raise BookVideoError(
            f"A voice looks like kokoro:bm_george, edge:ur-PK-AsadNeural or piper:en_US-joe-medium, not {spec!r}."
        )
    if engine == "piper":
        voice = audiobook.piper_voice(name, speed)
        voice["sentence_silence_s"] = 0.35
        return voice
    return {
        "engine": engine,
        "name": name,
        "model_path": "",
        "config_path": "",
        "length_scale": round(1.0 / audiobook.clamp_speed(speed), 4),
        "sentence_silence_s": 0.3 if engine == "kokoro" else 0.25,
    }


def look_from(request: BookVideoRequest) -> visualizer.Look:
    template = video_templates.get(request.template)
    accent = visualizer.parse_accent(request.accent) if request.accent else None
    style = request.style or template.ring
    fps = request.fps or (visualizer.STILL_FPS if style == "still" else 24)
    return video_templates.look_for(template, style=style, accent=accent,
                                    height=request.height, fps=fps, encoder=request.encoder)


def folder_for(request: BookVideoRequest, book_title: str = "") -> Path:
    if request.out:
        return Path(request.out)
    name = request.name or f"{book_title or request.title} - {request.language}"
    slug = safe_filename(name, fallback="Audiobook")[:80].strip(" .") or "Audiobook"
    if request.preview:
        slug = slug[:70].rstrip(" .") + PREVIEW_SUFFIX
    return get_settings().data_root / "audiobooks" / slug


# --- The text ---------------------------------------------------------------------


def load_book(request: BookVideoRequest) -> book_text.Book:
    settings = get_settings()
    if request.gutenberg:
        raw = book_text.fetch_gutenberg(request.gutenberg, settings.data_root / "tmp" / "gutenberg")
        book = book_text.parse_gutenberg(raw, source=f"gutenberg:{request.gutenberg}")
        book.language = request.language
    else:
        if not request.title:
            raise BookVideoError("A title is needed with a script.")
        if not request.text_path or not Path(request.text_path).is_file():
            raise BookVideoError(f"The script {request.text_path} was not found.")
        raw = Path(request.text_path).read_text(encoding="utf-8-sig")
        book = book_text.parse_script(raw, title=request.title, author=request.author or "",
                                      language=request.language, kind=request.kind)
    if request.title:
        book.title = request.title
    if request.author is not None:
        book.author = request.author
    if request.gutenberg:
        book.kind = "public-domain"
    return book


def first_chapters(text: str, count: int) -> str:
    """Keep the first ``count`` chapters, for a quick end-to-end test."""
    blocks = text.split("\n\n")
    out, seen = [], 0
    for block in blocks:
        if block.startswith("# "):
            seen += 1
            if seen > count:
                break
        out.append(block)
    return "\n\n".join(out)


def preview_script(book: book_text.Book, limit: int = PREVIEW_CHARS) -> str:
    """The first heading and the first sentences after it, about fifteen seconds of speech."""
    text = book_text.speakable(book.text, book.language)
    heading = ""
    taken: list[str] = []
    length = 0
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("# "):
            if taken:
                break
            heading = heading or block
            continue
        for sentence in split_sentences(block):
            if taken and length + len(sentence) > limit:
                break
            taken.append(sentence)
            length += len(sentence) + 1
        if length >= limit * 0.6:
            break
    if not taken:
        raise BookVideoError("The script has no sentences to preview.")
    body = " ".join(taken)
    return f"{heading}\n\n{body}\n" if heading else f"# {book.title}\n\n{body}\n"


def recorded_book(request: BookVideoRequest) -> tuple[book_text.Book, str, dict]:
    """The script and voice of an audiobook already finished in ``request.out``."""
    folder = Path(request.out or "")
    try:
        data = json.loads((folder / "book.json").read_text(encoding="utf-8"))
        full = (folder / "script.txt").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise BookVideoError(f"{folder} has no finished audiobook to package: {exc}") from exc
    if data.get("status") != audiobook.DONE or not (folder / "audiobook.mp3").is_file():
        raise BookVideoError(f"The audiobook in {folder} is not finished yet ({data.get('status')}).")
    source = f"gutenberg:{request.gutenberg}" if request.gutenberg else ""
    book = book_text.Book(title=request.title or str(data.get("title") or folder.name),
                          author=request.author or "", language=request.language,
                          kind="public-domain" if request.gutenberg else request.kind, source=source)
    return book, full, dict(data.get("voice") or {})


def full_script(request: BookVideoRequest, book: book_text.Book) -> str:
    if request.preview:
        return preview_script(book)
    full = book_text.finished_script(book, request.channel)
    if request.max_chapters:
        full = first_chapters(full, request.max_chapters + 1)
    return full


# --- Estimates ----------------------------------------------------------------------


def estimates(full: str, engine: str, look: visualizer.Look | None) -> dict[str, float]:
    """Seconds of audio, and seconds of work per stage, for this text."""
    return estimates_for_chars(len(full), engine, look)


def estimates_for_chars(chars: int, engine: str, look: visualizer.Look | None) -> dict[str, float]:
    seconds = chars / DEFAULT_CPS
    speak = seconds * SPEED_FACTOR.get(engine, 1.0)
    timing = seconds * WHISPER_FACTOR if look is not None and engine != "edge" else 0.0
    draw = visualizer.estimate_seconds(seconds, look) if look is not None else max(30.0, seconds / 200)
    return {"audio_s": seconds, "speak_s": speak, "timing_s": timing, "draw_s": draw, "pack_s": 20.0}


def plan(book: book_text.Book, full: str, engine: str, folder: Path,
         look: visualizer.Look | None = None) -> dict[str, Any]:
    titles = audiobook.chapter_titles(full)
    chapters = audiobook.chapter_sentences(full)
    est = estimates(full, engine, look)
    wav_gb = est["audio_s"] * SAMPLE_RATES.get(engine, 24_000) * 2 / 1e9
    video_gb = est["audio_s"] * (0.12 if look is not None else 0.02) / 1e3
    anchor = folder.anchor or str(get_settings().data_root.anchor)
    free_gb = shutil.disk_usage(anchor).free / 1e9
    return {
        "title": book.title, "author": book.author, "kind": book.kind, "language": book.language,
        "words": len(full.split()), "characters": len(full),
        "audio_s": est["audio_s"], "estimates": est,
        "total_work_s": est["speak_s"] + est["timing_s"] + est["draw_s"] + est["pack_s"],
        "wav_gb": wav_gb, "video_gb": video_gb, "free_gb": free_gb, "drive": anchor,
        "disk_ok": free_gb > wav_gb + video_gb + 1.0,
        "chapter_titles": [t for t in titles if t], "chunks": len(chapters),
        "warnings": list(book.warnings),
    }


def plan_report(book: book_text.Book, full: str, engine: str, folder: Path,
                look: visualizer.Look | None = None) -> str:
    info = plan(book, full, engine, folder, look)
    est = info["estimates"]
    video_line = "Video:     chapter cards at 1 fps, a few minutes"
    if look is not None:
        video_line = (f"Video:     {look.theme.id} template, {look.style} ring, {look.height}p {look.fps} fps: "
                      f"about {pack.duration_label(est['draw_s'])} to draw, "
                      f"{pack.duration_label(max(est['timing_s'], 60))} of word timing")
        if info["audio_s"] > 3 * 3600 and look.height >= 1080 and est["draw_s"] > info["audio_s"]:
            video_line += "\n           (long book: --height 720 or --fps 20 roughly halves that)"
    lines = [
        f"Title:     {book.title}",
        f"Author:    {book.author or '-'}",
        f"Kind:      {book.kind}   Language: {book.language}",
        f"Words:     {info['words']:,}   Characters: {info['characters']:,}",
        f"Length:    about {pack.duration_label(info['audio_s'])} of audio (estimate)",
        f"Compute:   about {pack.duration_label(est['speak_s'])} with {engine}",
        video_line,
        f"Disk:      about {info['wav_gb']:.1f} GB of temporary WAV, {info['free_gb']:.1f} GB free on {info['drive']}",
        f"Chapters:  {len(info['chapter_titles'])} named, {info['chunks']} recording chunks",
        "",
    ]
    lines += [f"  {i:3d}. {t}" for i, t in enumerate(info["chapter_titles"], start=1)]
    if info["warnings"]:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in info["warnings"]]
    return "\n".join(lines)


# --- Progress -------------------------------------------------------------------------

STAGES = ("text", "speaking", "timing", "drawing", "packaging", "done")
STAGE_LABELS = {
    "text": "Reading the script", "speaking": "Speaking the narration", "timing": "Timing every word",
    "drawing": "Drawing the video", "packaging": "Thumbnail and upload text", "done": "Finished",
    "failed": "Stopped with an error",
}


class Progress:
    """Writes ``progress.json``: stage, share of the stage done, and overall share by estimated work."""

    def __init__(self, folder: Path, est: dict[str, float] | None = None) -> None:
        self.folder = folder
        self.started = time.monotonic()
        self.weights = {"text": 2.0, "speaking": 1.0, "timing": 0.0, "drawing": 1.0, "packaging": 20.0}
        if est:
            self.weights.update(speaking=est["speak_s"], timing=est["timing_s"],
                                drawing=est["draw_s"], packaging=est["pack_s"])
        self.stage = "text"
        self.data: dict[str, Any] = {}

    def update(self, stage: str, fraction: float = 0.0, detail: str = "", left_s: float | None = None) -> None:
        self.stage = stage
        fraction = min(max(fraction, 0.0), 1.0)
        order = [s for s in STAGES if s != "done"]
        total = sum(self.weights.values()) or 1.0
        if stage == "done":
            overall = 1.0
        elif stage in order:
            before = sum(self.weights[s] for s in order[:order.index(stage)])
            overall = (before + self.weights[stage] * fraction) / total
        else:
            overall = float(self.data.get("percent", 0)) / 100
        if left_s is None and stage in order:
            rest = sum(self.weights[s] for s in order[order.index(stage) + 1:])
            left_s = self.weights[stage] * (1 - fraction) + rest
        self.data = {
            "stage": stage,
            "label": STAGE_LABELS.get(stage, stage),
            "stage_fraction": round(fraction, 3),
            "percent": round(overall * 100, 1),
            "detail": detail,
            "seconds_left": None if stage in ("done", "failed") else round(max(0.0, left_s or 0.0)),
            "elapsed_s": round(time.monotonic() - self.started),
            "updated_at": utcnow().isoformat(timespec="seconds"),
            "pid": os.getpid(),
        }
        write_json(self.folder / PROGRESS_FILE, self.data)

    def fail(self, detail: str) -> None:
        self.update("failed", detail=detail)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)


def read_progress(folder: Path) -> dict[str, Any] | None:
    try:
        return json.loads((folder / PROGRESS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- Art and word timing ---------------------------------------------------------------


async def find_art(query: str, folder: Path, ffmpeg_path: str, log: LogLine) -> tuple[Path | None, str]:
    from app.services import stock_media

    try:
        candidates = await stock_media.search_commons(query, "image")
    except stock_media.StockMediaError as exc:
        log(f"Cover art search failed: {exc}")
        return None, ""
    for candidate in candidates[:6]:
        try:
            fetched = await stock_media.fetch(candidate, folder, ffmpeg_path=ffmpeg_path, name="art")
        except stock_media.StockMediaError as exc:
            log(f"Skipped {candidate.title}: {exc}")
            continue
        return fetched.still_path, candidate.credit_line
    log("No usable cover art was found; a plain background is used.")
    return None, ""


def _chapter_wav(folder: Path, index: int, start: float, duration: float, ffmpeg_path: str) -> Path | None:
    """The chapter's WAV, or a cut from the MP3 when a finished run already removed it."""
    wav = folder / "chapters" / f"{index:03d}.wav"
    if wav.is_file():
        return wav
    mp3 = folder / "audiobook.mp3"
    if not mp3.is_file():
        return None
    cut = folder / "chapters" / f"{index:03d}.cut.wav"
    if not cut.is_file():
        partial = cut.with_name(cut.stem + ".part.wav")
        subprocess.run([ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
                        "-t", f"{duration:.3f}", "-i", str(mp3), "-ac", "1", "-ar", "16000", str(partial)],
                       check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not partial.is_file():
            return None
        partial.replace(cut)
    return cut


def book_timeline(folder: Path, language: str, duration: float, ffmpeg_path: str, use_whisper: bool,
                  log: LogLine = log_to_stdout,
                  progress: Callable[[int, int], None] | None = None) -> visualizer.Timeline:
    """Word times for every chapter (cached per chapter), joined into the book's timeline."""
    text = (folder / "script.txt").read_text(encoding="utf-8")
    chunks_text = audiobook.chapter_sentences(text)
    titles = audiobook.chapter_titles(text)
    chunks = []
    offset = 0.0
    sources: dict[str, int] = {}
    started = time.monotonic()
    for index, sentences in enumerate(chunks_text, start=1):
        meta = json.loads((folder / "chapters" / f"{index:03d}.json").read_text(encoding="utf-8"))
        length = float(meta.get("duration_s") or 0.0)
        cached = word_timing.load_cached(folder, index, str(meta.get("hash", "")))
        if cached is None:
            log(f"Timing the words of chapter {index} of {len(chunks_text)}.")
            wav = _chapter_wav(folder, index, offset, length, ffmpeg_path) if use_whisper else None
            cached = word_timing.chapter_words(folder, index, sentences, meta, wav, language, use_whisper)
            if wav is not None and wav.name.endswith(".cut.wav"):
                wav.unlink(missing_ok=True)
        sources[cached["source"]] = sources.get(cached["source"], 0) + 1
        title = titles[index - 1] if index - 1 < len(titles) else ""
        chunks.append((title, offset, cached))
        offset += length
        if progress:
            progress(index, len(chunks_text))
    word_timing.release_models()
    summary = ", ".join(f"{n} by {s}" for s, n in sources.items())
    log(f"Word timing ready in {time.monotonic() - started:.0f}s ({summary}).")
    return visualizer.build_timeline(chunks, duration)


def already_recorded(folder: Path, full: str, voice: dict) -> bool:
    try:
        data = json.loads((folder / "book.json").read_text(encoding="utf-8"))
        script = (folder / "script.txt").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return False
    return (data.get("status") == audiobook.DONE and (folder / "audiobook.mp3").is_file()
            and script == full.strip() and data.get("voice") == voice)


def _speaking_fraction(folder: Path) -> tuple[float, str]:
    try:
        data = json.loads((folder / "book.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0, ""
    done, total = int(data.get("chapters_done") or 0), int(data.get("chapters_total") or 0)
    return (done / total if total else 0.0), (f"Chapter {done} of {total}" if total else "")


# --- The whole run ------------------------------------------------------------------------


async def make(request: BookVideoRequest, log: LogLine = log_to_stdout) -> int:
    """Make the book's folder. Returns 0 when it is ready (or the plan was written), 1 when not."""
    tools = await ffmpeg_tool.detect()
    if not tools.ffmpeg_path:
        log(tools.message or "FFmpeg is not installed.")
        return 1

    try:
        if request.package_only:
            book, full, voice = recorded_book(request)
            engine = str(voice.get("engine") or "piper")
        else:
            book = load_book(request)
            full = full_script(request, book)
            spec = request.voice or DEFAULT_VOICES.get(book.language, DEFAULT_VOICES["en"])
            engine = spec.partition(":")[0]
            voice = voice_for(spec, request.speed)
    except (BookVideoError, book_text.BookTextError, OSError) as exc:
        log(str(exc))
        return 1
    animated = request.video == "animated" and not request.no_video
    if animated and book.language != "en":
        log("Live captions are English only for now; this book gets the chapter-card video.")
        animated = False
    look = look_from(request) if animated else None
    template = video_templates.get(request.template)

    folder = folder_for(request, book.title)
    folder.mkdir(parents=True, exist_ok=True)
    est = estimates(full, engine, look)
    progress = Progress(folder, est)
    progress.update("text", 0.5, "Reading the script")
    (folder / "script.md").write_text(full, encoding="utf-8")
    run = {"argv": request.argv, "request": _request_json(request)}
    write_json(folder / RUN_FILE, run)
    report = plan_report(book, full, engine, folder, look)
    (folder / "plan.txt").write_text(report, encoding="utf-8")
    for line in report.splitlines():
        if line.strip():
            log(line)
    log(f"Folder: {folder}")
    if request.plan_only:
        progress.update("done", 1.0, "Plan written")
        return 0

    try:
        return await _produce(request, book, full, voice, engine, look, template, folder, tools, progress, log)
    except Exception as exc:  # noqa: BLE001 - the page must hear why it stopped
        progress.fail(str(exc)[:400])
        raise


def _request_json(request: BookVideoRequest) -> dict[str, Any]:
    data = asdict(request)
    for key, value in data.items():
        if isinstance(value, Path):
            data[key] = str(value)
    data.pop("argv", None)
    return data


async def _produce(request, book, full, voice, engine, look, template, folder, tools, progress: Progress,
                   log: LogLine) -> int:
    def speaking_log(message: str) -> None:
        log(message)
        fraction, detail = _speaking_fraction(folder)
        progress.update("speaking", fraction, detail or message)

    progress.update("speaking", 0.0, "Starting the voice")
    if already_recorded(folder, full, voice):
        log("The narration is already recorded with this text and voice; packaging only.")
    else:
        audiobook.prepare_folder(folder, book.title, full, voice, request.speed, request.bitrate)
        result = await audiobook.run_book(folder, log_line=speaking_log)
        if result.get("status") != audiobook.DONE:
            why = result.get("error") or result.get("message") or "The narration was not finished."
            log(why)
            progress.fail(str(why))
            return 1
    mp3 = folder / "audiobook.mp3"

    titles = audiobook.chapter_titles(full)
    chapters = pack.chapters_from(folder, titles)
    info = await ffmpeg_tool.probe(mp3, tools.ffprobe_path)
    duration = info.duration_s if info.ok and info.duration_s else sum(c.duration_s for c in chapters)

    art, credit = (Path(request.art), request.art_credit) if request.art else (None, "")
    if art is not None and not art.is_file():
        log(f"The cover picture {art} is missing; drawing without it.")
        art = None
    if not art and request.art_query:
        art, credit = await find_art(request.art_query, folder / "art", tools.ffmpeg_path, log)

    source_note = ""
    if book.source.startswith("gutenberg:"):
        number = book.source.split(":", 1)[1]
        source_note = (f"Text: Project Gutenberg eBook #{number} (https://www.gutenberg.org/ebooks/{number}), "
                       "public domain in the United States.")
    elif book.kind == "summary":
        source_note = (f"This is an original summary written by {request.channel}. It is not affiliated with "
                       "the author or publisher; buy the book to support the author.")
    narrator_note = f"Narrated with an AI voice ({engine.title()}: {voice.get('name', '')})."
    theme = template.theme
    pkg = pack.Package(
        title=book.title, author=book.author, kind=book.kind, language=book.language,
        channel=request.channel,
        display_title=request.display_title or book.title,
        display_author=request.display_author if request.display_author is not None else book.author,
        duration_s=duration, chapters=chapters, art=art, art_credit=credit,
        source_note=source_note, narrator_note=narrator_note, about=request.about or "",
        accent=look.accent if look is not None else template.accent,
        display_font=template.thumb_font, backdrop=(theme.bg_bottom, theme.bg_top),
    )

    video = folder / "audiobook.mp4"
    if look is not None:
        def timing_progress(done: int, total: int) -> None:
            progress.update("timing", done / total if total else 1.0, f"Chapter {done} of {total}")

        progress.update("timing", 0.0, "Listening to the narration")
        timeline = await asyncio.to_thread(book_timeline, folder, book.language, duration,
                                           tools.ffmpeg_path, request.use_whisper, log, timing_progress)
        log(f"Building the animated video: {template.name} template, {look.style} ring, {look.height}p at "
            f"{look.fps} fps, about {pack.duration_label(visualizer.estimate_seconds(duration, look))}.")

        def draw_progress(done: int, total: int, left: float | None) -> None:
            progress.update("drawing", done / total if total else 1.0, f"Segment {done} of {total}",
                            None if left is None else left + progress.weights["packaging"])

        progress.update("drawing", 0.0, "Measuring the voice for the ring")
        started = time.monotonic()
        try:
            await asyncio.to_thread(
                pack.build_video, pkg, mp3, video, folder / "video-work", tools.ffmpeg_path, look.fps,
                mode="animated", look=look, timeline=timeline,
                ffprobe_path=tools.ffprobe_path or "ffprobe", log_line=log, progress=draw_progress,
            )
        except visualizer.VisualizerError as exc:
            log(f"The video failed: {exc}. Run it again to continue from the last segment.")
            progress.fail(str(exc))
            return 1
        log(f"Video built in {pack.duration_label(time.monotonic() - started)}.")
    elif not request.no_video:
        fps = request.fps or 1
        progress.update("drawing", 0.0, f"{len(chapters)} chapter cards")
        log(f"Building the video: {len(chapters)} chapter cards at {fps} fps.")
        started = time.monotonic()
        await asyncio.to_thread(pack.build_video, pkg, mp3, video, folder / "video-work", tools.ffmpeg_path, fps)
        log(f"Video built in {time.monotonic() - started:.0f}s.")

    progress.update("packaging", 0.2, "Thumbnail")
    log("Drawing the thumbnail.")
    pack.thumbnail(pkg, folder / "thumbnail.jpg", tools.ffmpeg_path)
    pack.card(pkg, "", folder / "cover.png", tools.ffmpeg_path)
    title = pack.youtube_title(pkg)
    desc = pack.description(pkg)
    tag_list = pack.tags(pkg)
    (folder / "title.txt").write_text(title, encoding="utf-8")
    (folder / "description.txt").write_text(desc, encoding="utf-8")
    (folder / "tags.txt").write_text(", ".join(tag_list), encoding="utf-8")

    progress.update("packaging", 0.6, "Checking loudness and silences")
    log("Checking loudness and silences.")
    checks = await asyncio.to_thread(pack.check_audio, tools.ffmpeg_path, mp3)
    checks["duration_s"] = round(duration, 1)
    checks["chapters"] = len(chapters)
    checks["youtube_chapters"] = len(pack.youtube_chapters(chapters))
    write_json(folder / "qa.json", checks)
    upload = {
        "title": title, "tags": tag_list, "video": video.name if video.is_file() else None,
        "thumbnail": "thumbnail.jpg", "duration": pack.duration_label(duration),
        "made_for_kids": False, "altered_or_synthetic": "Yes (AI narration voice)",
        "category": "Education" if book.kind == "summary" else "Entertainment",
        "language": book.language, "template": template.id, "preview": request.preview,
    }
    write_json(folder / "upload.json", upload)
    badge = f"{pack._badge(book.kind, book.language)} • {pack.duration_label(duration).upper()}"
    thumb_lines = [pkg.display_title.upper()]
    if pkg.display_author:
        thumb_lines.append(pkg.display_author.upper())
    thumb_lines.append(badge)
    subtitles = "subtitles.srt" if (folder / "subtitles.srt").is_file() else "not in this folder yet"
    pack.write_upload_details(folder, pack.upload_details_text(
        title=title, description=desc, tags=tag_list, duration=pack.duration_label(duration),
        category=upload["category"], language=book.language, made_for_kids=False,
        altered=upload["altered_or_synthetic"], video=upload["video"] or "audiobook.mp4",
        thumbnail="thumbnail.jpg", subtitles=subtitles, book_title=book.title, author=book.author,
        kind=book.kind, channel=request.channel, voice=narrator_note, template=template.name,
        cover=credit, source=source_note, thumbnail_text=" / ".join(thumb_lines),
    ))

    if not request.keep_wavs:
        clean_work_files(folder)

    log(f"Done: {pack.duration_label(duration)}, {checks['youtube_chapters']} YouTube chapters.")
    log(checks["loudness_note"])
    if checks["long_silences"]:
        log(f"Long silences at: {', '.join(pack.stamp(s['start_s'], True) for s in checks['long_silences'][:10])}")
    log(f"Upload folder: {folder}")
    progress.update("done", 1.0, f"{pack.duration_label(duration)} ready")
    return 0


# --- Cleanup -----------------------------------------------------------------------------


def work_files(folder: Path) -> list[Path]:
    """Files only needed while a book is being made: chapter WAVs and video segments."""
    out = list((folder / "chapters").glob("*.wav")) if (folder / "chapters").is_dir() else []
    work = folder / "video-work"
    if work.is_dir():
        out += [p for p in work.rglob("*") if p.is_file()]
    return out


def work_bytes(folder: Path) -> int:
    total = 0
    for path in work_files(folder):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def clean_work_files(folder: Path) -> int:
    """Delete the WAVs and video segments; the MP3, MP4, timings and upload files stay."""
    freed = work_bytes(folder)
    for wav in (folder / "chapters").glob("*.wav") if (folder / "chapters").is_dir() else []:
        wav.unlink(missing_ok=True)
    shutil.rmtree(folder / "video-work", ignore_errors=True)
    return freed
