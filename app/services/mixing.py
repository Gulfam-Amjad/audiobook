"""Laying narration onto a timeline and ducking music under it.

The filter graphs here were run against real narration on this machine
before anything was built on them, and the measurements are recorded in
the comments rather than assumed:

* placing six real lines with ``adelay`` and mixing them produced a track
  of exactly 21.250s against a 21.250s target
* ``sidechaincompress`` driven by a split of the voice, followed by
  single-pass ``loudnorm=I=-16``, measured -16.6 LUFS integrated

Two rules hold throughout. Narration is never time-stretched to fit a
shot: the shot was cut to the measured line in the first place, so
stretching would mean the measurement was wrong. And the music bed is
only ever an input the user supplied; nothing here downloads a track or
assumes one is licensed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2

#: EBU R128 targets. YouTube normalises to roughly -14 LUFS, so -16 with
#: 1.5 dB of true-peak headroom survives the platform's own pass without
#: being pushed around.
TARGET_LUFS = -16.0
TARGET_TRUE_PEAK = -1.5
TARGET_LRA = 11.0

#: Measured tolerance for single-pass loudnorm. Two-pass would be tighter
#: but needs an analysis run; on this hardware one pass landed 0.6 LU out,
#: so anything inside this band is treated as on target.
LOUDNESS_TOLERANCE_LU = 1.5

#: Sidechain ducking. The threshold is linear amplitude, not dB.
DUCK_THRESHOLD = 0.05
DUCK_RATIO = 8
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 400


class MixError(Exception):
    """The mix cannot be built from what is on disk."""


@dataclass
class Placement:
    """One measured line, and where it sits on the finished timeline."""

    path: Path
    start_s: float
    duration_s: float
    text: str = ""
    scene_key: str = ""

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass
class Timeline:
    """Where every line sits, and what could not be placed."""

    placements: list[Placement] = field(default_factory=list)
    total_s: float = 0.0
    #: Lines with no measured audio. Reported, never approximated.
    unplaced: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def speech_s(self) -> float:
        return round(sum(p.duration_s for p in self.placements), 3)

    @property
    def silence_s(self) -> float:
        return round(max(0.0, self.total_s - self.speech_s), 3)


def plan_timeline(
    scenes: list[dict[str, object]], *, lead_in_s: float = 0.3
) -> Timeline:
    """Place each scene's lines inside that scene's shot.

    ``scenes`` carries, in order, each scene's clip length and its
    segments. A line starts ``lead_in_s`` after its shot begins so the cut
    is not stepped on; the remaining padding falls at the end.

    A scene whose lines are longer than its shot is a real problem and is
    reported. It cannot happen when the shot was cut from the same
    measurements, so it means something changed underneath.
    """
    timeline = Timeline()
    cursor = 0.0

    for scene in scenes:
        scene_key = str(scene.get("scene_key") or "")
        clip_s = scene.get("clip_duration_s")
        if clip_s is None:
            timeline.unplaced.append(
                f"{scene_key}: has no rendered clip, so its narration has nowhere to go."
            )
            continue
        clip_s = float(clip_s)

        offset = cursor + lead_in_s
        segments = list(scene.get("segments") or [])
        for segment in segments:
            text = str(segment.get("text") or "")
            duration = segment.get("duration_s")
            path = segment.get("file_path")
            if duration is None or not path:
                timeline.unplaced.append(
                    f"{scene_key}: \"{text[:48]}\" has no measured audio."
                )
                continue
            source = Path(str(path))
            if not source.exists():
                timeline.unplaced.append(
                    f"{scene_key}: \"{text[:48]}\" is missing its audio file."
                )
                continue
            duration = float(duration)
            timeline.placements.append(
                Placement(
                    path=source,
                    start_s=round(offset, 3),
                    duration_s=duration,
                    text=text,
                    scene_key=scene_key,
                )
            )
            offset += duration

        spoken = offset - (cursor + lead_in_s)
        if spoken > clip_s + 0.001:
            timeline.warnings.append(
                f"{scene_key}: {spoken:.2f}s of narration does not fit a {clip_s:.2f}s "
                "shot. The shot was cut to this audio, so one of them has changed; "
                "re-plan the shots rather than speeding the voice up."
            )
        # A transition out of this shot overlaps the next one, so the next
        # shot starts that much earlier on the finished timeline.
        cursor += clip_s - float(scene.get("overlap_after_s") or 0.0)

    timeline.total_s = round(cursor, 3)
    return timeline


def speech_graph(placements: list[Placement], total_s: float) -> str:
    """Place every line at its own offset and sum them.

    ``normalize=0`` keeps amix from quietening the whole track as inputs
    are added, and ``dropout_transition=0`` stops it rebalancing when a
    line ends. ``apad`` holds the track out to the full length so the
    mixer is not the thing that decides where the video stops.
    """
    if not placements:
        raise MixError("There are no measured lines to place.")

    chains: list[str] = []
    for index, placement in enumerate(placements):
        delay_ms = int(round(placement.start_s * 1000))
        chains.append(
            f"[{index}:a]aresample={SAMPLE_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}:all=1[a{index}]"
        )
    labels = "".join(f"[a{i}]" for i in range(len(placements)))
    chains.append(
        f"{labels}amix=inputs={len(placements)}:normalize=0:dropout_transition=0,"
        f"apad[speech]"
    )
    return ";".join(chains)


def speech_command(
    ffmpeg_path: str,
    placements: list[Placement],
    destination: Path,
    *,
    total_s: float,
) -> list[str]:
    """Render the narration track on its own, so it can be checked."""
    command = [ffmpeg_path, "-hide_banner", "-y"]
    for placement in placements:
        command += ["-i", str(placement.path)]
    command += [
        "-filter_complex",
        speech_graph(placements, total_s),
        "-map",
        "[speech]",
        "-t",
        f"{total_s:.3f}",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        str(destination),
    ]
    return command


def duck_graph(*, music_gain_db: float, loudnorm: bool = True) -> str:
    """Narration over a music bed, with the bed pushed down under it.

    Input 0 is the speech track, input 1 the music. The sidechain is a
    split of the speech rather than the mix, because feeding the compressor
    its own output would duck the music against itself.
    """
    tail = (
        f",loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}"
        if loudnorm
        else ""
    )
    return (
        f"[1:a]aresample={SAMPLE_RATE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={music_gain_db:g}dB[bed];"
        f"[0:a]aresample={SAMPLE_RATE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[sc][voice];"
        f"[bed][sc]sidechaincompress=threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
        f"attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[ducked];"
        f"[voice][ducked]amix=inputs=2:normalize=0:dropout_transition=0{tail}[mix]"
    )


def speech_only_graph(*, loudnorm: bool = True) -> str:
    """The same normalisation, with no music to duck."""
    tail = (
        f",loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}"
        if loudnorm
        else ""
    )
    return (
        f"[0:a]aresample={SAMPLE_RATE},"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo{tail}[mix]"
    )


def mix_command(
    ffmpeg_path: str,
    speech: Path,
    destination: Path,
    *,
    music: Path | None = None,
    music_gain_db: float = -18.0,
    loudnorm: bool = True,
    total_s: float | None = None,
) -> list[str]:
    """Produce the finished audio track."""
    command = [ffmpeg_path, "-hide_banner", "-y", "-i", str(speech)]
    if music is not None:
        # The bed loops so a short track does not leave the end dry, and
        # is cut to the video's length rather than the other way round.
        command += ["-stream_loop", "-1", "-i", str(music)]
        graph = duck_graph(music_gain_db=music_gain_db, loudnorm=loudnorm)
    else:
        graph = speech_only_graph(loudnorm=loudnorm)
    command += ["-filter_complex", graph, "-map", "[mix]"]
    if total_s:
        command += ["-t", f"{total_s:.3f}"]
    command += [
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        str(destination),
    ]
    return command


def loudness_command(ffmpeg_path: str, source: Path) -> list[str]:
    """Measure what the mix actually came out at."""
    return [
        ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-af",
        "ebur128=framelog=quiet",
        "-f",
        "null",
        "-",
    ]


def parse_loudness(stderr: str) -> dict[str, float | None]:
    """Read the integrated loudness out of ebur128's summary.

    Returns None for anything the summary did not state, rather than a
    zero that would read as "measured and silent".
    """
    result: dict[str, float | None] = {"integrated_lufs": None, "lra_lu": None}
    lines = [line.strip() for line in stderr.splitlines()]
    for position, line in enumerate(lines):
        if line.startswith("I:") and "LUFS" in line:
            result["integrated_lufs"] = _number(line)
        elif line.startswith("LRA:") and "LU" in line:
            result["lra_lu"] = _number(line)
        elif line == "Integrated loudness:" and position + 1 < len(lines):
            value = _number(lines[position + 1])
            if value is not None:
                result["integrated_lufs"] = value
        elif line == "Loudness range:" and position + 1 < len(lines):
            value = _number(lines[position + 1])
            if value is not None:
                result["lra_lu"] = value
    return result


def _number(line: str) -> float | None:
    for token in line.replace(":", " ").split():
        try:
            return float(token)
        except ValueError:
            continue
    return None


def loudness_verdict(integrated: float | None) -> tuple[bool, str]:
    """Whether the measured loudness is close enough to the target."""
    if integrated is None:
        return False, (
            "The finished loudness could not be measured, so whether it is at "
            "broadcast level is unknown."
        )
    difference = abs(integrated - TARGET_LUFS)
    if difference <= LOUDNESS_TOLERANCE_LU:
        return True, (
            f"Measured {integrated:.1f} LUFS against a {TARGET_LUFS:.0f} LUFS target."
        )
    return False, (
        f"Measured {integrated:.1f} LUFS, {difference:.1f} LU from the "
        f"{TARGET_LUFS:.0f} LUFS target."
    )
