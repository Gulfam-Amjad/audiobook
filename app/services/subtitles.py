"""Subtitles built from measured audio, not from guesswork.

Most subtitle generators either run a forced-alignment model or estimate
timing from word counts. Neither happens here. Narration is synthesised
one sentence at a time, each sentence is a separate WAV, and ffprobe
measures every one. A cue's start is the running total of the lines
before it; its end is that plus its own measured length. The timing is
therefore exact by construction, with no model and no estimate anywhere
in the chain.

What this module will not do is invent a cue. A line whose audio was
never measured has no honest place on the timeline, so it is reported as
a gap rather than given a plausible-looking slot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Below this a cue flashes past before it can be read. Checked, not fixed:
#: the fix would be to re-record the line, not to stretch the subtitle past
#: the audio it belongs to.
MIN_READABLE_S = 0.7
#: Characters per second a viewer can comfortably follow. Used only to
#: warn, never to alter timing.
COMFORTABLE_CPS = 21.0
#: Two cues closer than this read as one flicker.
MIN_GAP_S = 0.04


class SubtitleError(Exception):
    """Subtitles were asked for in a state where they cannot be honest."""


@dataclass
class Cue:
    """One subtitle, timed from the audio it belongs to."""

    index: int
    start_s: float
    end_s: float
    text: str
    #: Which scene it came from, so a problem can be pointed at a shot.
    scene_key: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    @property
    def chars_per_second(self) -> float:
        length = self.duration_s
        return len(self.text) / length if length > 0 else float("inf")


@dataclass
class CueSheet:
    """Every cue for one project, plus what is wrong with them."""

    cues: list[Cue] = field(default_factory=list)
    #: Lines that could not be timed. Never silently dropped.
    gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.cues)

    @property
    def last_end_s(self) -> float:
        return self.cues[-1].end_s if self.cues else 0.0


def timestamp(seconds: float, *, separator: str = ",") -> str:
    """``HH:MM:SS,mmm``. SRT uses a comma, WebVTT a full stop."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, total_ms = divmod(total_ms, 3_600_000)
    minutes, total_ms = divmod(total_ms, 60_000)
    secs, millis = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def wrap(text: str, width: int = 42, max_lines: int = 2) -> str:
    """Break a cue over at most two lines, on word boundaries.

    Two lines is the broadcast convention and what players expect. A cue
    that will not fit is left long rather than truncated: losing half a
    sentence is worse than an overfull line.
    """
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    # Too long for the convention: rebalance onto max_lines rather than
    # dropping words.
    joined = " ".join(lines)
    per_line = max(1, len(joined) // max_lines + 1)
    rebuilt: list[str] = []
    current = ""
    for word in joined.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= per_line or not current:
            current = candidate
        else:
            rebuilt.append(current)
            current = word
    if current:
        rebuilt.append(current)
    return "\n".join(rebuilt)


def build(
    placements: list[dict[str, object]],
    *,
    width: int = 42,
) -> CueSheet:
    """Turn placed audio segments into a cue sheet.

    Each placement needs ``text``, ``start_s`` and ``duration_s``, where
    the duration came from ffprobe. A placement missing its duration is a
    gap, because the only honest alternative would be to make one up.
    """
    sheet = CueSheet()
    index = 1
    previous_end = 0.0

    for item in placements:
        text = str(item.get("text") or "").strip()
        scene_key = str(item.get("scene_key") or "")
        if not text:
            continue

        duration = item.get("duration_s")
        start = item.get("start_s")
        if duration is None or start is None:
            sheet.gaps.append(
                f"{scene_key or 'a line'}: \"{text[:48]}\" has no measured audio, "
                "so it cannot be timed and has been left out."
            )
            continue

        start_f = float(start)
        end_f = start_f + float(duration)

        if start_f < previous_end - MIN_GAP_S:
            sheet.warnings.append(
                f"Cue {index} starts at {start_f:.2f}s, before cue {index - 1} ends "
                f"at {previous_end:.2f}s. Two lines would be on screen at once."
            )

        cue = Cue(
            index=index,
            start_s=start_f,
            end_s=end_f,
            text=wrap(text, width=width),
            scene_key=scene_key,
        )

        if cue.duration_s < MIN_READABLE_S:
            sheet.warnings.append(
                f"Cue {index} is only {cue.duration_s:.2f}s long, which is too quick "
                "to read. The line itself is that short, so shortening the text or "
                "re-recording it is the only honest fix."
            )
        elif cue.chars_per_second > COMFORTABLE_CPS:
            sheet.warnings.append(
                f"Cue {index} runs at {cue.chars_per_second:.0f} characters a second, "
                f"above the {COMFORTABLE_CPS:.0f} most viewers follow comfortably."
            )

        sheet.cues.append(cue)
        previous_end = end_f
        index += 1

    return sheet


def to_srt(sheet: CueSheet) -> str:
    """SubRip. CRLF line endings, which is what players expect."""
    blocks: list[str] = []
    for cue in sheet.cues:
        blocks.append(
            f"{cue.index}\r\n"
            f"{timestamp(cue.start_s)} --> {timestamp(cue.end_s)}\r\n"
            f"{cue.text.replace(chr(10), chr(13) + chr(10))}\r\n"
        )
    return "\r\n".join(blocks)


def to_vtt(sheet: CueSheet) -> str:
    """WebVTT, for anywhere that wants it instead of SubRip."""
    blocks = ["WEBVTT", ""]
    for cue in sheet.cues:
        blocks.append(str(cue.index))
        blocks.append(
            f"{timestamp(cue.start_s, separator='.')} --> "
            f"{timestamp(cue.end_s, separator='.')}"
        )
        blocks.append(cue.text)
        blocks.append("")
    return "\n".join(blocks)


def write(sheet: CueSheet, destination: Path) -> Path:
    """Write the cue sheet beside the video it belongs to."""
    if not sheet.cues:
        raise SubtitleError(
            "There are no timed cues, so there is no subtitle file to write. "
            "Synthesise and measure the narration first."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".vtt":
        destination.write_text(to_vtt(sheet), encoding="utf-8")
    else:
        destination.write_text(to_srt(sheet), encoding="utf-8", newline="")
    return destination


def filter_path(path: Path) -> str:
    """Escape a path for FFmpeg's ``subtitles`` filter.

    Verified on this machine: libass takes the filename as a filter
    argument, where a backslash escapes and a colon separates options, so
    a plain Windows path is parsed as nonsense. Forward slashes with the
    drive colon escaped is what actually loads.
    """
    text = str(path).replace("\\", "/")
    return text.replace(":", "\\:").replace("'", "\\'")


def burn_in_filter(path: Path, style: dict[str, object] | None = None) -> str:
    """The video filter that renders a subtitle file onto the picture."""
    settings = {
        "FontName": "Arial",
        "FontSize": 22,
        "PrimaryColour": "&H00FFFFFF",
        "OutlineColour": "&H80000000",
        "BorderStyle": 3,
        "Outline": 1,
        "Shadow": 0,
        "MarginV": 40,
    }
    settings.update(style or {})
    force = ",".join(f"{key}={value}" for key, value in settings.items())
    return f"subtitles='{filter_path(path)}':force_style='{force}'"
