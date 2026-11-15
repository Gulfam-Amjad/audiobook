"""When each word of a recorded chapter is spoken, for the live captions.

The words on screen are always the script's own words. Timing comes from
three places, best first:

* **Edge's word boundaries.** Microsoft's service reports each word as it
  speaks, so those times are exact.
* **Whisper.** ``faster-whisper`` transcribes the chapter WAV offline with
  word timestamps. Its words are lined up with the script by
  ``difflib``; words it misheard or skipped are placed between the
  neighbours it did match.
* **Even spread.** Each word gets a share of its sentence's time weighted
  by syllables, with a beat after commas and full stops. Used when
  neither of the above is available or when Whisper matched too little.

Every voice but Piper is recorded a sentence (or a paragraph, for Edge)
at a time, and the measured start of each piece is kept in the chapter's
JSON. Those anchors bound every word, so a long book cannot drift: an
error is at worst inside one sentence.

The result is cached next to the chapter as ``NNN.words.json``, keyed by
the chapter's recording hash, so a resumed run never redoes it.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

#: tiny.en, not base. Measured on this laptop: base.en took about as long as
#: the audio itself, tiny.en about a ninth of it, and the script alignment
#: covers the words it mishears.
WHISPER_MODEL = "tiny.en"
WHISPER_RATE = 16_000
#: Below this share of a piece's words matched, Whisper's timing for that
#: piece is not trusted and the even spread is used instead.
MIN_MATCHED_SHARE = 0.35
#: A matched word this far outside its piece's measured span was matched
#: to the wrong place.
ANCHOR_SLACK_S = 0.35
MIN_WORD_S = 0.06
COMMA_BEAT = 0.9
STOP_BEAT = 1.8
CACHE_VERSION = 3

_TOKEN = re.compile(r"[^\w']+", re.UNICODE)
_VOWELS = re.compile(r"[aeiouy]+", re.IGNORECASE)

_MODEL_LOCK = threading.Lock()
_MODELS: dict[str, Any] = {}


@dataclass
class Piece:
    """One recorded unit of a chapter: its first sentence, how many, and where it sits."""

    first: int
    count: int
    start_s: float
    end_s: float
    speech_end_s: float
    words: list[tuple[str, float, float]] | None = None


def models_dir() -> Path:
    return get_settings().data_root / "models" / "whisper"


def norm(token: str) -> str:
    return _TOKEN.sub("", token.lower()).strip("'")


def syllables(token: str) -> int:
    core = norm(token)
    if not core:
        return 0
    if not core.isascii():
        return max(1, len(core) // 3)
    count = len(_VOWELS.findall(core))
    if core.endswith("e") and count > 1 and not core.endswith(("le", "ee")):
        count -= 1
    return max(1, count) + (len(core) > 9)


def beat_after(token: str) -> float:
    if token.endswith((".", "!", "?", "…", ":", ";", '."', '?"', '!"', ".”", "?”", "!”")):
        return STOP_BEAT
    if token.endswith((",", "—", "–", ",”", ',"')):
        return COMMA_BEAT
    return 0.0


def spread(tokens: list[str], start: float, end: float) -> list[tuple[float, float]]:
    """Share ``start``..``end`` among ``tokens`` by syllables, with beats at punctuation."""
    if not tokens:
        return []
    end = max(end, start + MIN_WORD_S * len(tokens))
    weights = [float(max(1, syllables(t))) for t in tokens]
    beats = [beat_after(t) for t in tokens[:-1]] + [0.0]
    total = sum(weights) + sum(beats)
    unit = (end - start) / total if total else 0.0
    out, cursor = [], start
    for weight, beat in zip(weights, beats):
        word_end = cursor + weight * unit
        out.append((cursor, word_end))
        cursor = word_end + beat * unit
    return out


# --- Whisper ----------------------------------------------------------------


def _model(name: str = WHISPER_MODEL) -> Any:
    with _MODEL_LOCK:
        if name not in _MODELS:
            from faster_whisper import WhisperModel

            root = models_dir()
            root.mkdir(parents=True, exist_ok=True)
            try:
                _MODELS[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                             download_root=str(root), local_files_only=True)
            except Exception:  # noqa: BLE001 - not downloaded yet; needs the internet once
                _MODELS[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                             download_root=str(root))
        return _MODELS[name]


def release_models() -> None:
    """Drop the loaded Whisper models so the video that follows has the memory."""
    import gc

    with _MODEL_LOCK:
        _MODELS.clear()
    gc.collect()


def read_wav_16k(wav: Path):
    """Mono float samples at 16 kHz, the rate Whisper expects.

    faster-whisper's own loader goes through PyAV, whose API changed under
    it here, so the WAV is read with the standard library instead.
    """
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as handle:
        channels, width, rate = handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{wav.name} is not 16-bit PCM.")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != WHISPER_RATE and len(samples):
        count = int(len(samples) * WHISPER_RATE / rate)
        samples = np.interp(np.arange(count) * (rate / WHISPER_RATE), np.arange(len(samples)), samples)
    return samples.astype(np.float32)


def whisper_words(wav: Path, language: str = "en") -> list[tuple[str, float, float]]:
    """Every word Whisper hears in ``wav``, with its start and end in seconds."""
    model = _model()
    segments, _ = model.transcribe(
        read_wav_16k(wav), language=language, word_timestamps=True, beam_size=1,
        condition_on_previous_text=False, vad_filter=False,
    )
    words: list[tuple[str, float, float]] = []
    for segment in segments:
        for word in segment.words or []:
            text = word.word.strip()
            if text:
                words.append((text, float(word.start), float(word.end)))
    return words


# --- Alignment --------------------------------------------------------------


def align(
    sentences: list[list[str]],
    pieces: list[Piece],
    heard: list[tuple[str, float, float]] | None,
) -> tuple[list[list[tuple[str, float, float]]], float]:
    """Time every script word. Returns the words per sentence and the share matched."""
    flat: list[tuple[int, int, str]] = [
        (si, wi, token) for si, words in enumerate(sentences) for wi, token in enumerate(words)
    ]
    times: list[tuple[float, float] | None] = [None] * len(flat)
    matched = 0
    if heard:
        script_keys = [norm(t) for _, _, t in flat]
        heard_keys = [norm(t) for t, _, _ in heard]
        matcher = difflib.SequenceMatcher(None, script_keys, heard_keys, autojunk=False)
        for block in matcher.get_matching_blocks():
            for k in range(block.size):
                if script_keys[block.a + k]:
                    _, start, end = heard[block.b + k]
                    times[block.a + k] = (start, end)

    piece_of: list[int] = []
    for p_index, piece in enumerate(pieces):
        for si in range(piece.first, piece.first + piece.count):
            piece_of.extend([p_index] * len(sentences[si]) if si < len(sentences) else [])
    while len(piece_of) < len(flat):
        piece_of.append(len(pieces) - 1)

    result: list[list[tuple[str, float, float]]] = [[] for _ in sentences]
    for p_index, piece in enumerate(pieces):
        members = [i for i in range(len(flat)) if piece_of[i] == p_index]
        if not members:
            continue
        lo, hi = piece.start_s - ANCHOR_SLACK_S, piece.speech_end_s + ANCHOR_SLACK_S
        known = [times[i] if times[i] and lo <= times[i][0] <= hi else None for i in members]
        share = sum(1 for k in known if k) / len(members)
        if share < MIN_MATCHED_SHARE:
            known = [None] * len(members)
        else:
            matched += sum(1 for k in known if k)
        filled = _fill(
            [flat[i][2] for i in members], known, piece.start_s, piece.speech_end_s
        )
        for i, (start, end) in zip(members, filled):
            si, _, token = flat[i]
            result[si].append((token, round(start, 3), round(end, 3)))
    return result, (matched / len(flat) if flat else 0.0)


def _fill(
    tokens: list[str], known: list[tuple[float, float] | None], start: float, end: float
) -> list[tuple[float, float]]:
    """Keep the known times, spread the rest between their neighbours, never go backwards."""
    out: list[tuple[float, float]] = []
    i = 0
    cursor = start
    while i < len(tokens):
        if known[i]:
            s, e = known[i]
            s = max(s, cursor)
            e = max(e, s + MIN_WORD_S)
            out.append((s, e))
            cursor = e
            i += 1
            continue
        j = i
        while j < len(tokens) and not known[j]:
            j += 1
        gap_end = known[j][0] if j < len(tokens) else end
        gap_end = max(gap_end, cursor + MIN_WORD_S * (j - i))
        out.extend(spread(tokens[i:j], cursor, gap_end))
        cursor = out[-1][1]
        i = j
    return out


# --- One chapter, cached ------------------------------------------------------


def pieces_from(meta: dict[str, Any], sentence_count: int, duration_s: float) -> list[Piece]:
    raw = meta.get("pieces") or []
    pieces = []
    for item in raw:
        words = item.get("words")
        pieces.append(Piece(
            first=int(item["first"]), count=int(item["count"]),
            start_s=float(item["start_s"]), end_s=float(item["end_s"]),
            speech_end_s=float(item.get("speech_end_s", item["end_s"])),
            words=[(w[0], float(w[1]), float(w[2])) for w in words] if words else None,
        ))
    if not pieces or sum(p.count for p in pieces) != sentence_count:
        return [Piece(0, sentence_count, 0.0, duration_s, duration_s)]
    return pieces


def cache_path(folder: Path, index: int) -> Path:
    return folder / "chapters" / f"{index:03d}.words.json"


def load_cached(folder: Path, index: int, digest: str) -> dict[str, Any] | None:
    try:
        data = json.loads(cache_path(folder, index).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("hash") != digest or data.get("version") != CACHE_VERSION:
        return None
    return data


def chapter_words(
    folder: Path,
    index: int,
    sentences: list[str],
    meta: dict[str, Any],
    wav: Path | None,
    language: str = "en",
    use_whisper: bool = True,
) -> dict[str, Any]:
    """Word times for chapter ``index`` (1-based), from the cache or worked out now."""
    digest = str(meta.get("hash", ""))
    cached = load_cached(folder, index, digest)
    if cached:
        return cached
    duration = float(meta.get("duration_s") or 0.0)
    tokens = [s.split() for s in sentences]
    pieces = pieces_from(meta, len(sentences), duration)

    heard: list[tuple[str, float, float]] | None = None
    source = "even"
    whisper_failed = False
    if all(p.words for p in pieces):
        heard = [(t, p.start_s + s, p.start_s + e) for p in pieces for t, s, e in p.words or []]
        source = "edge"
    elif use_whisper and language == "en" and wav is not None and wav.is_file():
        try:
            heard = whisper_words(wav, language)
            source = "whisper"
        except Exception as exc:  # noqa: BLE001 - captions fall back to the even spread
            log.warning("Whisper failed on chapter %s: %s", index, exc)
            whisper_failed = True
            heard = None
    words, share = align(tokens, pieces, heard)
    if heard and share == 0.0:
        source = "even"
    data = {
        "version": CACHE_VERSION, "hash": digest, "source": source,
        "matched": round(share, 3), "duration_s": duration, "sentences": words,
    }
    # A failed Whisper run is not cached, so the next attempt tries again
    # instead of keeping the guessed times forever.
    if whisper_failed:
        return data
    target = cache_path(folder, index)
    partial = target.with_suffix(".json.part")
    partial.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    partial.replace(target)
    return data
