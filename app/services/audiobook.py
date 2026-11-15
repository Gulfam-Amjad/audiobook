"""Audiobooks: a long script spoken into one MP3, outside the film pipeline.

The film project caps a target at 30 minutes and 80 shots, because it cuts
a documentary into shots. A book has no shots. It is split into chapters
of about ten minutes of speech, each chapter is spoken into one WAV, and
the WAVs are joined into ``audiobook.mp3``.

Piper is given a whole chapter through one process, so the voice model is
loaded once per chapter instead of once per sentence. Starting Piper costs
about seven seconds on this laptop, which across a five-hour book is most
of the time the old per-sentence path took. Kokoro and Edge are driven a
sentence at a time through their existing providers; Kokoro keeps its
model loaded in-process, and Edge is a network call either way.

A chapter whose WAV is already on disk and whose text and voice settings
hash the same is not spoken again, so a stopped or failed run continues
from the first missing chapter.

One book is made at a time. Each lives in its own folder under
``audiobooks/`` with ``book.json`` holding its settings and progress, so
the state survives a restart of the app.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models import VoiceProfile
from app.providers import speech as speech_registry
from app.providers.speech.base import SpeechProviderError, VoiceSettings
from app.services import ffmpeg as ffmpeg_tool
from app.services import mixing
from app.services import piper as piper_tool
from app.services.paths import is_within, safe_filename
from app.services.speech import split_sentences
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

#: About ten minutes of narration at the pace measured on this machine.
#: A chapter is the unit that has to be spoken again after a stop.
MAX_CHAPTER_CHARS = 12_000
#: A ten-hour book is well under a megabyte of text. Anything far larger
#: is almost certainly the wrong file.
MAX_SCRIPT_CHARS = 2_000_000
MIN_WAV_BYTES = 1024
PIPER_SAMPLE_RATE = 22_050
MP3_BITRATE = "128k"
MP3_SAMPLE_RATE = 44_100
SPEED_MIN, SPEED_MAX = 0.5, 2.0
STILL_LOOP_S = 10
EDGE_ATTEMPTS = 6
EDGE_RETRY_DELAY_S = 3.0
#: Edge opens a connection per request, which costs about as long as the
#: speech itself for one sentence, so it is sent paragraphs of this size.
#: Urdu and Hindi take two to three UTF-8 bytes a character, and edge-tts
#: splits anything over 4096 bytes itself.
EDGE_CHUNK_CHARS = 1200
#: Requests to Edge in flight at once. The work is on Microsoft's servers,
#: so this laptop's two cores are not the limit; the connection is.
EDGE_CONCURRENCY = 4
HEADING = re.compile(r"^#{1,6}\s+(\S.*)$")

#: Seconds of compute per second of audio, measured on this laptop
#: (i7-6600U) on 2026-11-11. Edge runs on Microsoft's servers, so its time
#: depends on the connection and is not estimated.
COMPUTE_FACTOR: dict[str, float] = {"piper": 0.81, "kokoro": 4.13}
#: Used for the estimate until a voice has a measured pace.
DEFAULT_CHARS_PER_S = 16.5

LANGUAGE_LABELS: dict[str, str] = {
    "en_US": "English (US)",
    "en_GB": "English (UK)",
    "hi_IN": "Hindi",
    "ur_PK": "Urdu (Pakistan)",
    "ur_IN": "Urdu (India)",
}
ENGINE_LABELS: dict[str, str] = {"piper": "Piper", "kokoro": "Kokoro", "edge": "Edge (online)"}
_EDGE_GENDER: dict[str, str] = {
    "hi-IN-MadhurNeural": "male",
    "hi-IN-SwaraNeural": "female",
    "ur-PK-AsadNeural": "male",
    "ur-PK-UzmaNeural": "female",
    "ur-IN-SalmanNeural": "male",
    "ur-IN-GulNeural": "female",
}

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
STOPPED = "stopped"
ACTIVE_STATUSES = frozenset({QUEUED, RUNNING})


class AudiobookError(Exception):
    """The book could not be made, with the reason in the message."""


class _Cancelled(Exception):
    pass


# --- Splitting the text ---------------------------------------------------


def chapter_sentences(text: str) -> list[list[str]]:
    """Split a book into chapters of sentences.

    A markdown heading starts a chapter and is spoken as its title. Blank
    lines break paragraphs. Paragraphs accumulate until the next heading
    or until another sentence would pass ``MAX_CHAPTER_CHARS``.
    """
    chapters: list[list[str]] = []
    current: list[str] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if current:
            chapters.append(current)
            current = []
            current_chars = 0

    for is_heading, block in _blocks(text):
        if is_heading and current:
            flush()
        for sentence in split_sentences(block):
            if current and current_chars + len(sentence) > MAX_CHAPTER_CHARS:
                flush()
            current.append(sentence)
            current_chars += len(sentence)
    flush()
    return chapters


def chapter_titles(text: str) -> list[str]:
    """The heading that opens each chapter of :func:`chapter_sentences`.

    A chapter that only exists because the one before it ran past
    ``MAX_CHAPTER_CHARS`` gets an empty string, so callers can tell a
    real chapter start from a continuation.
    """
    titles: list[str] = []
    pending = ""
    current_chars = 0
    started = False

    for is_heading, block in _blocks(text):
        if is_heading:
            if started:
                started = False
                current_chars = 0
            pending = block
        for sentence in split_sentences(block):
            if started and current_chars + len(sentence) > MAX_CHAPTER_CHARS:
                started = False
                current_chars = 0
            if not started:
                titles.append(pending)
                pending = ""
                started = True
            current_chars += len(sentence)
    return titles


def _blocks(text: str) -> list[tuple[bool, str]]:
    """Return ``(is_heading, text)`` for headings and paragraphs."""
    blocks: list[tuple[bool, str]] = []
    for part in re.split(r"\n\s*\n", (text or "").strip()):
        lines: list[str] = []
        for raw in part.splitlines():
            line = raw.strip()
            if not line:
                continue
            heading = HEADING.match(line)
            if heading:
                if lines:
                    blocks.append((False, " ".join(lines)))
                    lines = []
                blocks.append((True, heading.group(1).strip()))
            else:
                lines.append(line)
        if lines:
            blocks.append((False, " ".join(lines)))
    return blocks


def first_heading(text: str) -> str:
    for is_heading, block in _blocks(text):
        if is_heading:
            return block
    return ""


# --- Voices ---------------------------------------------------------------


def clamp_speed(speed: float) -> float:
    try:
        value = float(speed)
    except (TypeError, ValueError):
        value = 1.0
    return round(min(max(value, SPEED_MIN), SPEED_MAX), 3)


def voice_snapshot(profile: VoiceProfile, speed: float = 1.0) -> dict[str, Any]:
    """The voice settings a book is recorded with, frozen into ``book.json``.

    Kept separate from the database row so a resumed book sounds the same
    even if the voice profile is edited in the meantime.
    """
    base = profile.length_scale or 1.0
    return {
        "engine": profile.engine or "piper",
        "name": profile.name,
        "language": profile.language or "",
        "model_path": profile.model_path or "",
        "config_path": profile.config_path or "",
        "speaker_id": profile.speaker_id,
        "length_scale": round(base / clamp_speed(speed), 4),
        "noise_scale": profile.noise_scale if profile.noise_scale is not None else 0.667,
        "noise_w": profile.noise_w if profile.noise_w is not None else 0.8,
        "sentence_silence_s": (
            profile.sentence_silence_s if profile.sentence_silence_s is not None else 0.2
        ),
        "sample_rate": profile.sample_rate,
    }


def piper_voice(name: str, speed: float = 1.0) -> dict[str, Any]:
    """A snapshot for a Piper voice on disk, by key or by ``.onnx`` path."""
    candidate = Path(name)
    if candidate.suffix == ".onnx":
        model, key = candidate, candidate.stem
    else:
        key = name.strip()
        model = piper_tool.voices_dir() / key / f"{key}.onnx"
    if not model.is_file():
        raise AudiobookError(f"The voice file is missing: {model}")
    return {
        "engine": "piper",
        "name": key,
        "language": "",
        "model_path": str(model),
        "config_path": "",
        "speaker_id": None,
        "length_scale": round(1.0 / clamp_speed(speed), 4),
        "noise_scale": 0.667,
        "noise_w": 0.8,
        "sentence_silence_s": 0.2,
        "sample_rate": None,
    }


def settings_from(voice: dict[str, Any]) -> VoiceSettings:
    engine = voice.get("engine") or "piper"
    return VoiceSettings(
        model_path=voice.get("model_path") or "",
        config_path=voice.get("config_path") or "",
        speaker_id=voice.get("speaker_id"),
        length_scale=float(voice.get("length_scale") or 1.0),
        noise_scale=float(voice.get("noise_scale") or 0.667),
        noise_w=float(voice.get("noise_w") or 0.8),
        sentence_silence_s=float(voice.get("sentence_silence_s") or 0.0),
        engine=engine,
        voice_key=voice.get("name", "") if engine in ("kokoro", "edge") else "",
    )


def gender_for(profile: VoiceProfile) -> str:
    engine = profile.engine or "piper"
    name = profile.name or ""
    if engine == "kokoro" and len(name) > 1:
        return {"f": "female", "m": "male"}.get(name[1], "")
    if engine == "edge":
        return _EDGE_GENDER.get(name, "")
    for entry in piper_tool.CATALOGUE:
        family = entry.key.rsplit("-", 1)[0]
        if name == entry.key or name.startswith(family + "-"):
            match = re.search(r"(female|male)\)$", entry.display_name)
            return match.group(1) if match else ""
    return ""


def voices_for_page(profiles: list[VoiceProfile]) -> list[dict[str, Any]]:
    """Installed voices that can narrate, with what the form needs to show."""
    from app.services.voice_catalog import SPEED_NOTE, demo_dir

    samples = demo_dir()
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        engine = profile.engine or "piper"
        if engine == "piper" and not (profile.model_path and Path(profile.model_path).is_file()):
            continue
        gender = gender_for(profile)
        language = profile.language or ""
        label = " - ".join(
            part for part in (profile.name, gender, ENGINE_LABELS.get(engine, engine)) if part
        )
        demo = samples / f"{engine}-{profile.name}.mp3"
        rows.append(
            {
                "id": profile.id,
                "name": profile.name,
                "engine": engine,
                "engine_label": ENGINE_LABELS.get(engine, engine),
                "language": language,
                "language_label": LANGUAGE_LABELS.get(language, language or "Unknown"),
                "gender": gender,
                "label": label,
                "cps": profile.measured_chars_per_s or DEFAULT_CHARS_PER_S,
                "factor": COMPUTE_FACTOR.get(engine),
                "commercial_use": profile.commercial_use,
                "note": (profile.notes or "").splitlines()[0] if profile.notes else "",
                "speed_note": SPEED_NOTE.get(engine, ""),
                "demo": demo.name if demo.is_file() else "",
                "is_default": bool(profile.is_default),
            }
        )
    rows.sort(key=lambda row: (
        row["language_label"],
        {"piper": 0, "edge": 1, "kokoro": 2}.get(row["engine"], 9),
        row["name"],
    ))
    return rows


# --- Book folders ---------------------------------------------------------


_STATE_LOCK = threading.Lock()


def audiobooks_dir() -> Path:
    return get_settings().data_root / "audiobooks"


def book_dir(book_id: str) -> Path:
    if not book_id or safe_filename(book_id, fallback="") != book_id:
        raise AudiobookError(f"'{book_id}' is not an audiobook.")
    folder = audiobooks_dir() / book_id
    if not is_within(folder, audiobooks_dir()) or not (folder / "book.json").is_file():
        raise AudiobookError(f"'{book_id}' is not an audiobook.")
    return folder


def _load(folder: Path) -> dict[str, Any]:
    with _STATE_LOCK:
        try:
            return json.loads((folder / "book.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AudiobookError(f"{folder.name}: book.json could not be read ({exc}).") from exc


def _save(folder: Path, data: dict[str, Any]) -> None:
    with _STATE_LOCK:
        target = folder / "book.json"
        partial = folder / "book.json.part"
        partial.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        partial.replace(target)


def _update(folder: Path, log_line: Callable[[str], None] | None = None, **values: Any) -> dict[str, Any]:
    data = _load(folder)
    data.update(values)
    data["updated_at"] = utcnow().isoformat(timespec="seconds")
    _save(folder, data)
    if log_line and values.get("message"):
        log_line(str(values["message"]))
    return data


def prepare_folder(
    folder: Path,
    title: str,
    text: str,
    voice: dict[str, Any],
    speed: float,
    mp3_bitrate: str = MP3_BITRATE,
) -> dict[str, Any]:
    """Write the script and settings, keeping any chapters already recorded."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise AudiobookError("There is nothing to read: the script is empty.")
    if len(cleaned) > MAX_SCRIPT_CHARS:
        raise AudiobookError(
            f"The script is {len(cleaned):,} characters. The limit is {MAX_SCRIPT_CHARS:,}."
        )
    chapters = chapter_sentences(cleaned)
    if not chapters:
        raise AudiobookError("No sentences were found in the script.")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "script.txt").write_text(cleaned, encoding="utf-8")
    now = utcnow().isoformat(timespec="seconds")
    previous: dict[str, Any] = {}
    if (folder / "book.json").is_file():
        try:
            previous = _load(folder)
        except AudiobookError:
            previous = {}
    data = {
        "id": folder.name,
        "title": title.strip() or folder.name,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "status": QUEUED,
        "message": "Waiting to start.",
        "error": None,
        "voice": voice,
        "speed": clamp_speed(speed),
        "mp3_bitrate": mp3_bitrate,
        "language": voice.get("language") or "",
        "chars": len(cleaned),
        "chapters_total": len(chapters),
        "chapters_done": 0,
        "audio_s": 0.0,
        "mp3": None,
        "duration_s": None,
    }
    _save(folder, data)
    return data


def _unique_folder(title: str) -> Path:
    base = safe_filename(title, fallback="Audiobook")[:80].strip(" .") or "Audiobook"
    root = audiobooks_dir()
    candidate = root / base
    index = 2
    while candidate.exists():
        candidate = root / f"{base} ({index})"
        index += 1
    return candidate


# --- Speaking a chapter ---------------------------------------------------


def chapter_hash(sentences: list[str], settings: VoiceSettings) -> str:
    """Identity of one chapter's waveform: its text and every voice knob."""
    payload = "\n".join([settings.engine, settings.voice_key, settings.fingerprint(), *sentences])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _chapter_ready(wav: Path, meta_path: Path, digest: str) -> bool:
    if not wav.is_file() or not meta_path.is_file() or wav.stat().st_size < MIN_WAV_BYTES:
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return meta.get("hash") == digest and meta.get("wav_bytes") == wav.stat().st_size


def _meta_duration(meta_path: Path) -> float:
    try:
        return float(json.loads(meta_path.read_text(encoding="utf-8")).get("duration_s") or 0.0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def _piper_sample_rate(voice: dict[str, Any]) -> int:
    if voice.get("sample_rate"):
        return int(voice["sample_rate"])
    config = Path(voice.get("config_path") or (str(voice.get("model_path") or "") + ".json"))
    if config.is_file():
        try:
            meta = json.loads(config.read_text(encoding="utf-8"))
            return int((meta.get("audio") or {}).get("sample_rate") or PIPER_SAMPLE_RATE)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return PIPER_SAMPLE_RATE


def _feed(stream: Any, data: bytes) -> None:
    try:
        stream.write(data)
    except OSError:
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _piper_chapter(
    exe: str,
    settings: VoiceSettings,
    sentences: list[str],
    wav: Path,
    sample_rate: int,
    cancel: threading.Event,
) -> float:
    """Speak every sentence with one Piper process. Return seconds of audio."""
    wav.parent.mkdir(parents=True, exist_ok=True)
    raw = wav.with_name(wav.stem + ".raw.part")
    errors = wav.with_name(wav.stem + ".piper.log")
    raw.unlink(missing_ok=True)
    args = [
        exe,
        "--model", settings.model_path,
        "--output_raw",
        "--length_scale", f"{settings.length_scale:g}",
        "--noise_scale", f"{settings.noise_scale:g}",
        "--noise_w", f"{settings.noise_w:g}",
        "--sentence_silence", f"{settings.sentence_silence_s:g}",
    ]
    if settings.config_path:
        args += ["--config", settings.config_path]
    if settings.speaker_id is not None:
        args += ["--speaker", str(settings.speaker_id)]
    text = ("\n".join(sentences) + "\n").encode("utf-8")
    # Several times the time measured for this much text on this laptop.
    deadline = time.monotonic() + max(900, int(len(text) * 0.5) + 180)
    stopped = False

    with raw.open("wb") as audio, errors.open("wb") as log_file:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=audio,
            stderr=log_file,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Piper reads a line, speaks it, then reads the next, so a whole
        # chapter can block a direct write. A thread keeps cancel responsive.
        writer = threading.Thread(target=_feed, args=(process.stdin, text), daemon=True)
        writer.start()
        while True:
            try:
                process.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_set() or time.monotonic() > deadline:
                    process.kill()
                    process.wait()
                    stopped = True
                    break
        writer.join(timeout=5)

    # Windows refuses to delete a file that is still open, so cleanup waits
    # until the handles above are closed.
    if stopped:
        raw.unlink(missing_ok=True)
        errors.unlink(missing_ok=True)
        if cancel.is_set():
            raise _Cancelled()
        raise AudiobookError(f"Piper stopped responding on {wav.name}.")
    if process.returncode:
        raw.unlink(missing_ok=True)
        detail = errors.read_text(encoding="utf-8", errors="replace").strip()[-400:]
        raise AudiobookError(f"Piper exited with code {process.returncode} on {wav.name}. {detail}")
    errors.unlink(missing_ok=True)
    size = raw.stat().st_size if raw.exists() else 0
    if size < MIN_WAV_BYTES:
        raw.unlink(missing_ok=True)
        raise AudiobookError(f"Piper wrote only {size} bytes for {wav.name}.")
    size -= size % 2
    partial = wav.with_name(wav.stem + ".part.wav")
    with raw.open("rb") as source, wave.open(str(partial), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(source.read(size))
    raw.unlink(missing_ok=True)
    wav.unlink(missing_ok=True)
    partial.replace(wav)
    return (size // 2) / sample_rate


async def _provider_chapter(
    provider: Any,
    settings: VoiceSettings,
    sentences: list[str],
    wav: Path,
    cancel: threading.Event,
    pieces_out: list[dict[str, Any]] | None = None,
) -> float:
    """Speak a chapter one sentence at a time and join the sentences in order.

    ``pieces_out`` receives where each recorded piece starts and ends in the
    chapter, and Edge's word timings when it sent them, for the captions.
    """
    parts = wav.with_name(wav.stem + "-parts")
    parts.mkdir(parents=True, exist_ok=True)
    counts = [1] * len(sentences)
    if settings.engine == "edge":
        counts = group_counts(sentences, EDGE_CHUNK_CHARS)
        sentences = group_sentences(sentences, EDGE_CHUNK_CHARS)
        await _speak_concurrently(provider, settings, sentences, parts, cancel)
    pieces: list[dict[str, Any]] = []
    first = 0
    partial = wav.with_name(wav.stem + ".part.wav")
    partial.unlink(missing_ok=True)
    writer: wave.Wave_write | None = None
    params: tuple[int, int, int] | None = None
    frames = 0
    try:
        for index, sentence in enumerate(sentences, start=1):
            if cancel.is_set():
                raise _Cancelled()
            piece = _piece_path(parts, index, sentence, settings)
            if not _piece_ready(piece):
                await _speak_sentence(provider, settings, sentence, piece, index)
            with wave.open(str(piece), "rb") as source:
                shape = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if writer is None:
                    writer = wave.open(str(partial), "wb")
                    writer.setnchannels(shape[0])
                    writer.setsampwidth(shape[1])
                    writer.setframerate(shape[2])
                    params = shape
                elif shape != params:
                    raise AudiobookError(
                        f"Sentence {index} came back in a different audio format from the rest."
                    )
                count = source.getnframes()
                writer.writeframes(source.readframes(count))
                rate = float(shape[2])
                start_s, end_s = frames / rate, (frames + count) / rate
                record: dict[str, Any] = {
                    "first": first, "count": counts[index - 1],
                    "start_s": round(start_s, 3), "end_s": round(end_s, 3),
                    "speech_end_s": round(max(start_s + 0.1, end_s - settings.sentence_silence_s), 3),
                }
                words = _piece_words(piece)
                if words:
                    record["words"] = words
                pieces.append(record)
                first += counts[index - 1]
                frames += count
            piece.unlink(missing_ok=True)
            piece.with_suffix(".words.json").unlink(missing_ok=True)
    except BaseException:
        if writer is not None:
            writer.close()
        partial.unlink(missing_ok=True)
        raise
    if writer is not None:
        writer.close()
    shutil.rmtree(parts, ignore_errors=True)
    if params is None:
        raise AudiobookError(f"No audio was produced for {wav.name}.")
    wav.unlink(missing_ok=True)
    partial.replace(wav)
    if pieces_out is not None:
        pieces_out[:] = pieces
    return frames / params[2]


def _piece_words(piece: Path) -> list[list[Any]] | None:
    """Edge's word timings for one piece, written beside its WAV, if any."""
    try:
        words = json.loads(piece.with_suffix(".words.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return words if isinstance(words, list) and words else None


def _piece_path(parts: Path, index: int, text: str, settings: VoiceSettings) -> Path:
    """Named by its text and voice, so a piece left from an edited run is never reused."""
    payload = f"{settings.fingerprint()}\n{text}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
    return parts / f"{index:05d}-{digest}.wav"


def _piece_ready(piece: Path) -> bool:
    return piece.is_file() and piece.stat().st_size >= MIN_WAV_BYTES


async def _speak_concurrently(
    provider: Any,
    settings: VoiceSettings,
    pieces: list[str],
    parts: Path,
    cancel: threading.Event,
) -> None:
    """Fetch several pieces from a network voice at once.

    Pieces already on disk from an interrupted run are kept, so a dropped
    connection costs one piece rather than the chapter.
    """
    gate = asyncio.Semaphore(EDGE_CONCURRENCY)

    async def one(index: int, text: str) -> None:
        piece = _piece_path(parts, index, text, settings)
        if _piece_ready(piece):
            return
        async with gate:
            if cancel.is_set():
                raise _Cancelled()
            await _speak_sentence(provider, settings, text, piece, index)

    try:
        async with asyncio.TaskGroup() as group:
            for index, text in enumerate(pieces, start=1):
                group.create_task(one(index, text))
    except ExceptionGroup as errors:
        raise errors.exceptions[0] from None


def group_sentences(sentences: list[str], limit: int) -> list[str]:
    """Join consecutive sentences into pieces of at most ``limit`` characters."""
    groups: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > limit:
            groups.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        groups.append(current)
    return groups


def group_counts(sentences: list[str], limit: int) -> list[int]:
    """How many sentences went into each piece of :func:`group_sentences`."""
    counts: list[int] = []
    length = 0
    for sentence in sentences:
        if counts and length + 1 + len(sentence) <= limit:
            counts[-1] += 1
            length += 1 + len(sentence)
        else:
            counts.append(1)
            length = len(sentence)
    return counts


async def _speak_sentence(
    provider: Any, settings: VoiceSettings, sentence: str, piece: Path, index: int
) -> None:
    attempts = EDGE_ATTEMPTS if settings.engine == "edge" else 1
    for attempt in range(1, attempts + 1):
        try:
            await provider.synthesise(sentence, settings, piece)
            return
        except SpeechProviderError as exc:
            if attempt >= attempts:
                raise AudiobookError(f"Sentence {index}: {exc.message}") from exc
            await asyncio.sleep(EDGE_RETRY_DELAY_S * attempt)


# --- Joining and finishing ------------------------------------------------


def _run_ffmpeg(ffmpeg_path: str, args: list[str]) -> None:
    process = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode:
        detail = (process.stderr or b"").decode("utf-8", "replace").strip()
        raise AudiobookError(detail[-500:] or f"ffmpeg exited with code {process.returncode}.")


def join_mp3(
    ffmpeg_path: str, wavs: list[Path], destination: Path, work: Path, bitrate: str = MP3_BITRATE
) -> None:
    """Concatenate the chapter WAVs into one loudness-normalised MP3."""
    listing = work / "chapters.txt"
    lines = []
    for wav in wavs:
        # The concat demuxer quotes paths with single quotes.
        escaped = wav.resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Keep a recognised extension, or FFmpeg cannot pick the format.
    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    partial.unlink(missing_ok=True)
    _run_ffmpeg(
        ffmpeg_path,
        [
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-af",
            f"loudnorm=I={mixing.TARGET_LUFS}:TP={mixing.TARGET_TRUE_PEAK}:LRA={mixing.TARGET_LRA}",
            "-ar", str(MP3_SAMPLE_RATE), "-ac", "1",
            "-c:a", "libmp3lame", "-b:a", bitrate,
            str(partial),
        ],
    )
    destination.unlink(missing_ok=True)
    partial.replace(destination)


def attach_still(ffmpeg_path: str, still: Path, audio: Path, destination: Path, work: Path) -> None:
    """Loop one picture for the length of the narration, without re-encoding it."""
    if not still.is_file():
        raise AudiobookError(f"The picture is missing: {still}")
    loop = work / "still-loop.mp4"
    _run_ffmpeg(
        ffmpeg_path,
        [
            "-loop", "1", "-i", str(still),
            "-t", str(STILL_LOOP_S),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p,fps=24",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an", str(loop),
        ],
    )
    partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
    partial.unlink(missing_ok=True)
    _run_ffmpeg(
        ffmpeg_path,
        [
            "-stream_loop", "-1", "-i", str(loop),
            "-i", str(audio),
            "-map", "0:v", "-map", "1:a",
            "-c", "copy", "-shortest",
            str(partial),
        ],
    )
    destination.unlink(missing_ok=True)
    partial.replace(destination)


async def run_book(
    folder: Path,
    cancel: threading.Event | None = None,
    log_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Speak every chapter not yet recorded, then build ``audiobook.mp3``.

    Never raises for an ordinary failure: the outcome is written to
    ``book.json`` and returned, so the caller reads one place either way.
    """
    cancel = cancel or threading.Event()
    data = _load(folder)
    try:
        text = (folder / "script.txt").read_text(encoding="utf-8")
    except OSError as exc:
        return _update(folder, status=FAILED, error=str(exc), message=f"script.txt is missing: {exc}")
    chapters = chapter_sentences(text)
    voice = data.get("voice") or {}
    settings = settings_from(voice)
    total = len(chapters)
    _update(
        folder, log_line,
        status=RUNNING, error=None, chapters_total=total, chapters_done=0, audio_s=0.0,
        message=f"Starting {total} chapter(s) with {voice.get('name', 'the voice')}.",
    )
    try:
        tools = await ffmpeg_tool.detect()
        if not tools.ffmpeg_path:
            raise AudiobookError(tools.message or "FFmpeg is not installed.")

        exe = ""
        provider = None
        sample_rate = PIPER_SAMPLE_RATE
        if settings.engine == "piper":
            status = await piper_tool.detect()
            if not status.available or not status.exe_path:
                raise AudiobookError(status.message or "Piper is not installed.")
            if not Path(settings.model_path).is_file():
                raise AudiobookError(f"The voice file is missing: {settings.model_path}")
            exe = status.exe_path
            sample_rate = _piper_sample_rate(voice)
        else:
            provider = speech_registry.get(settings.engine)
            if provider is None:
                raise AudiobookError(f"The {settings.engine} engine failed to load.")
            available, why = await provider.is_available()
            if not available:
                raise AudiobookError(why)

        work = folder / "chapters"
        work.mkdir(parents=True, exist_ok=True)
        wavs: list[Path] = []
        audio_s = 0.0
        for index, sentences in enumerate(chapters, start=1):
            if cancel.is_set():
                raise _Cancelled()
            digest = chapter_hash(sentences, settings)
            wav = work / f"{index:03d}.wav"
            meta_path = work / f"{index:03d}.json"
            wavs.append(wav)
            if _chapter_ready(wav, meta_path, digest):
                audio_s += _meta_duration(meta_path)
                _update(
                    folder, log_line, chapters_done=index, audio_s=round(audio_s, 1),
                    message=f"Chapter {index} of {total} was already recorded.",
                )
                continue
            _update(
                folder, log_line,
                message=f"Speaking chapter {index} of {total} ({len(sentences)} sentences).",
            )
            pieces: list[dict[str, Any]] = []
            if provider is None:
                duration = await asyncio.to_thread(
                    _piper_chapter, exe, settings, sentences, wav, sample_rate, cancel
                )
            else:
                duration = await _provider_chapter(provider, settings, sentences, wav, cancel, pieces)
            meta = {
                "hash": digest,
                "sentences": len(sentences),
                "wav_bytes": wav.stat().st_size,
                "duration_s": round(duration, 3),
            }
            if pieces:
                meta["pieces"] = pieces
            meta_partial = meta_path.with_suffix(".json.part")
            meta_partial.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            meta_partial.replace(meta_path)
            audio_s += duration
            _update(
                folder, log_line, chapters_done=index, audio_s=round(audio_s, 1),
                message=f"Chapter {index} of {total}: {duration:.1f}s of audio.",
            )

        _update(folder, log_line, message="Joining the chapters into an MP3.")
        mp3 = folder / "audiobook.mp3"
        bitrate = str(data.get("mp3_bitrate") or MP3_BITRATE)
        await asyncio.to_thread(join_mp3, tools.ffmpeg_path, wavs, mp3, work, bitrate)
        info = await ffmpeg_tool.probe(mp3, tools.ffprobe_path)
        duration_s = round(info.duration_s, 1) if info.ok and info.duration_s else None
        length = f"{duration_s / 60:.1f} minutes" if duration_s else "length not measured"
        return _update(
            folder, log_line,
            status=DONE, mp3=mp3.name, duration_s=duration_s,
            finished_at=utcnow().isoformat(timespec="seconds"),
            message=f"Finished: {length}.",
        )
    except _Cancelled:
        return _update(
            folder, log_line,
            status=CANCELLED,
            message="Stopped. Finished chapters are kept; Resume continues from the next one.",
        )
    except AudiobookError as exc:
        return _update(folder, log_line, status=FAILED, error=str(exc), message=str(exc))
    except Exception as exc:  # noqa: BLE001 - recorded on the book, not lost in a task
        log.exception("Audiobook %s failed", folder.name)
        detail = f"{type(exc).__name__}: {exc}"
        return _update(folder, log_line, status=FAILED, error=detail, message=detail)


# --- The one background job -----------------------------------------------


@dataclass
class _Job:
    book_id: str
    task: asyncio.Task
    cancel: threading.Event


_JOB: _Job | None = None


def _queue_is_speaking(folder: Path) -> bool:
    """The overnight queue records in its own process, not in this web process."""
    from app.services import book_queue

    return book_queue.is_speaking(folder)


def current_book_id() -> str | None:
    if _JOB is not None and not _JOB.task.done():
        return _JOB.book_id
    return None


def _refuse_if_busy() -> None:
    busy = current_book_id()
    if busy:
        raise AudiobookError(
            f"'{busy}' is still being made. One audiobook runs at a time, because two "
            "would share this laptop's two cores and both would take twice as long."
        )


def start(book_id: str) -> None:
    """Make or resume one book in the background."""
    global _JOB
    _refuse_if_busy()
    folder = book_dir(book_id)
    cancel = threading.Event()
    task = asyncio.get_running_loop().create_task(run_book(folder, cancel))
    _JOB = _Job(book_id=book_id, task=task, cancel=cancel)


async def create_book(title: str, text: str, profile: VoiceProfile, speed: float) -> str:
    """Save a new book and start making it. Returns its id."""
    _refuse_if_busy()
    engine = profile.engine or "piper"
    if engine == "piper" and not (profile.model_path and Path(profile.model_path).is_file()):
        raise AudiobookError(f"{profile.name} has no voice file on disk. Install it on the Voices page.")
    name = title.strip() or first_heading(text) or "Audiobook"
    folder = _unique_folder(name)
    prepare_folder(folder, name, text, voice_snapshot(profile, speed), speed)
    start(folder.name)
    return folder.name


def cancel_current() -> str | None:
    if _JOB is None or _JOB.task.done():
        return None
    _JOB.cancel.set()
    return _JOB.book_id


def resume(book_id: str) -> None:
    data = _load(book_dir(book_id))
    if data.get("status") == DONE and (book_dir(book_id) / "audiobook.mp3").is_file():
        raise AudiobookError("That audiobook is already finished.")
    start(book_id)


def delete_book(book_id: str) -> str:
    folder = book_dir(book_id)
    if current_book_id() == book_id:
        raise AudiobookError("Stop this audiobook before deleting it.")
    title = _load(folder).get("title") or book_id
    shutil.rmtree(folder)
    return title


def list_books() -> list[dict[str, Any]]:
    """Every book on disk, newest first, with progress for the page."""
    root = audiobooks_dir()
    if not root.is_dir():
        return []
    running = current_book_id()
    books: list[dict[str, Any]] = []
    for folder in root.iterdir():
        if not folder.is_dir() or not (folder / "book.json").is_file():
            continue
        try:
            data = _load(folder)
        except AudiobookError:
            continue
        if data.get("status") in ACTIVE_STATUSES and data.get("id") != running and not _queue_is_speaking(folder):
            data = _update(
                folder,
                status=STOPPED,
                message="Interrupted when the app stopped. Resume continues from the next chapter.",
            )
        total = int(data.get("chapters_total") or 0)
        done = int(data.get("chapters_done") or 0)
        data["progress_pct"] = round(100 * done / total, 1) if total else 0.0
        data["is_running"] = data.get("id") == running
        mp3 = folder / "audiobook.mp3"
        video = folder / "audiobook.mp4"
        data["mp3_rel"] = f"audiobooks/{folder.name}/{mp3.name}" if mp3.is_file() else None
        data["mp4_rel"] = f"audiobooks/{folder.name}/{video.name}" if video.is_file() else None
        data["size_bytes"] = mp3.stat().st_size if mp3.is_file() else None
        voice = data.get("voice") or {}
        data["voice_label"] = " - ".join(
            part
            for part in (
                voice.get("name"),
                ENGINE_LABELS.get(voice.get("engine") or "", voice.get("engine") or ""),
            )
            if part
        )
        data["language_label"] = LANGUAGE_LABELS.get(data.get("language") or "", data.get("language") or "")
        books.append(data)
    books.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return books
