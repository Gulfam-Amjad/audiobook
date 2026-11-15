"""Word-by-word captions that follow the voice.

The words on screen are the script's words, never a transcript: a
speech recogniser is used only to find *when* each word is said in the
tool's own recording. faster-whisper (MIT) with the ``base.en`` model
(MIT) runs on the processor at roughly a second of work per second of
speech on this machine, and each line's timings are cached beside its
WAV so a re-render does not redo them.

If the recogniser is not installed, the timings are spread across the
line by word length and the caption file says so in its header. That is
an estimate, and is labelled as one.

The ASS file shows one to three words at a time, pops each group in, and
lights up the word being spoken. The plain SRT is still written for
upload as closed captions.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

MODEL = "base.en"
_WORD = re.compile(r"[A-Za-z0-9']+")
#: A group of words on screen is cut at this many words or characters.
MAX_GROUP_WORDS = 3
MAX_GROUP_CHARS = 18
#: A group stays up this long after its last word if nothing follows.
TAIL_S = 0.25


@dataclass
class Word:
    text: str
    start_s: float
    end_s: float


def _norm(token: str) -> str:
    return "".join(_WORD.findall(token.lower()))


_model = None


def _load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(
            MODEL,
            device="cpu",
            compute_type="int8",
            cpu_threads=4,
            download_root=str(get_settings().data_root / "models" / "whisper"),
        )
    return _model


def _decode(ffmpeg_path: str, wav: Path):
    import numpy as np

    raw = subprocess.run(
        [ffmpeg_path, "-v", "error", "-i", str(wav), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def spread(text: str, duration_s: float) -> list[Word]:
    """Times shared out by word length. Used only without the recogniser."""
    tokens = text.split()
    if not tokens:
        return []
    weights = [max(1, len(_norm(t))) + 1 for t in tokens]
    total = sum(weights)
    words: list[Word] = []
    cursor = 0.0
    for token, weight in zip(tokens, weights):
        length = duration_s * weight / total
        words.append(Word(token, round(cursor, 3), round(cursor + length, 3)))
        cursor += length
    return words


def align(script_text: str, heard: list[Word], duration_s: float) -> list[Word]:
    """The script's words, each given the time the recogniser heard it.

    A script word the recogniser missed or misheard takes its time from
    its neighbours, so the caption never shows a word the voice did not
    say and never drops one it did.
    """
    tokens = script_text.split()
    if not tokens:
        return []
    if not heard:
        return spread(script_text, duration_s)

    a = [_norm(t) for t in tokens]
    b = [_norm(w.text) for w in heard]
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            word = heard[block.b + offset]
            times[block.a + offset] = (word.start_s, word.end_s)

    # Fill the gaps by sharing the time between the known neighbours.
    index = 0
    while index < len(tokens):
        if times[index] is not None:
            index += 1
            continue
        gap_end = index
        while gap_end < len(tokens) and times[gap_end] is None:
            gap_end += 1
        left = times[index - 1][1] if index > 0 else 0.0
        right = times[gap_end][0] if gap_end < len(tokens) else duration_s
        right = max(right, left + 0.05 * (gap_end - index))
        step = (right - left) / (gap_end - index)
        for k in range(index, gap_end):
            start = left + step * (k - index)
            times[k] = (start, start + step)
        index = gap_end

    return [Word(t, round(s, 3), round(e, 3)) for t, (s, e) in zip(tokens, times)]


def line_words(ffmpeg_path: str, wav: Path, text: str, duration_s: float) -> tuple[list[Word], bool]:
    """Word timings for one recorded line, and whether they were measured."""
    cache = wav.with_suffix(".words.json")
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("text") == text:
                return [Word(**w) for w in data["words"]], bool(data.get("measured"))
        except (ValueError, KeyError, TypeError):
            pass

    measured = True
    try:
        model = _load_model()
        segments, _ = model.transcribe(
            _decode(ffmpeg_path, wav), word_timestamps=True, beam_size=5,
            language="en", vad_filter=False,
        )
        heard = [
            Word(w.word.strip(), float(w.start), float(w.end))
            for segment in segments
            for w in (segment.words or [])
        ]
        words = align(text, heard, duration_s)
    except Exception as exc:  # noqa: BLE001 - the estimate is labelled as one
        log.warning("Word timing failed for %s: %s", wav, exc)
        words = spread(text, duration_s)
        measured = False

    cache.write_text(
        json.dumps(
            {"text": text, "measured": measured, "words": [w.__dict__ for w in words]}
        ),
        encoding="utf-8",
    )
    return words, measured


# ---------------------------------------------------------------------------
# ASS
# ---------------------------------------------------------------------------


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def groups(words: list[Word]) -> list[list[Word]]:
    """One to three words at a time, broken after punctuation."""
    result: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        chars = sum(len(w.text) + 1 for w in current) + len(word.text)
        if current and (len(current) >= MAX_GROUP_WORDS or chars > MAX_GROUP_CHARS):
            result.append(current)
            current = []
        current.append(word)
        if word.text[-1:] in ".,;:!?" or word.text.endswith("--"):
            result.append(current)
            current = []
    if current:
        result.append(current)
    return result


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def to_ass(
    words: list[Word],
    *,
    width: int,
    height: int,
    measured: bool,
    font: str = "Bebas Neue",
) -> str:
    """An ASS file with the spoken word lit inside each group."""
    vertical = height > width
    size = int(height * (0.052 if vertical else 0.078))
    margin = int(height * (0.30 if vertical else 0.09))
    header = [
        "[Script Info]",
        "; Word timings " + ("measured from the recording." if measured else "ESTIMATED from word length."),
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Word,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        f"0,0,0,0,100,100,1,0,1,{max(2, size // 16)},{max(1, size // 30)},2,60,60,{margin},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lit = "{\\c&H3AB0E8&}"
    plain = "{\\c&HFFFFFF&}"
    events: list[str] = []
    grouped = groups(words)
    for g_index, group in enumerate(grouped):
        following = grouped[g_index + 1][0].start_s if g_index + 1 < len(grouped) else None
        group_end = group[-1].end_s + TAIL_S
        if following is not None:
            group_end = min(group_end, following)
        for w_index, word in enumerate(group):
            start = word.start_s
            end = group[w_index + 1].start_s if w_index + 1 < len(group) else group_end
            if end <= start:
                continue
            pop = "{\\fscx82\\fscy82\\t(0,110,\\fscx100\\fscy100)}" if w_index == 0 else ""
            parts = [
                (lit if k == w_index else plain) + _escape(w.text.upper())
                for k, w in enumerate(group)
            ]
            events.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Word,,0,0,0,,{pop}{' '.join(parts)}"
            )
    return "\r\n".join(header + events) + "\r\n"


def ass_filter(path: Path, fonts_dir: Path) -> str:
    """The burn-in filter for an ASS file, with the OFL fonts available."""
    from app.services.subtitles import filter_path

    return f"ass='{filter_path(path)}':fontsdir='{filter_path(fonts_dir)}'"


def build(
    ffmpeg_path: str,
    placements: list,
    destination: Path,
    *,
    width: int,
    height: int,
) -> tuple[Path, bool]:
    """Time every placed line and write the ASS file. Returns (path, measured)."""
    words: list[Word] = []
    all_measured = True
    for placement in placements:
        line, measured = line_words(
            ffmpeg_path, Path(placement.path), placement.text, placement.duration_s
        )
        all_measured = all_measured and measured
        for word in line:
            words.append(
                Word(
                    word.text,
                    round(placement.start_s + word.start_s, 3),
                    round(placement.start_s + min(word.end_s, placement.duration_s), 3),
                )
            )
    destination.write_text(
        to_ass(words, width=width, height=height, measured=all_measured),
        encoding="utf-8",
        newline="",
    )
    return destination, all_measured
