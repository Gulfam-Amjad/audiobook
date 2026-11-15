"""How one shot hands over to the next.

A cut is the default and stays the default. A film that dissolves on
every shot looks like a slideshow, so a transition is chosen only where
the story changes: a jump in time, a new chapter, a countdown entry, a
reveal.

Each transition is an overlap. The tail of shot A plays over the head of
shot B for ``overlap_s``. The router lengthens shot A by that overlap, so
every line still starts and ends where it would on a cut.

The overlaps are rendered one at a time, never as one filtergraph over the
whole film. On this 8 GB machine a single xfade chain across eighty 1080p
inputs would hold every decoder open at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Transition:
    key: str
    #: FFmpeg ``xfade`` transition name. Empty for a cut.
    xfade: str
    overlap_s: float
    #: A sound tag that belongs with this transition, or empty.
    sound: str = ""


TRANSITIONS: dict[str, Transition] = {
    "cut": Transition("cut", "", 0.0),
    # A jump in time or place.
    "dissolve": Transition("dissolve", "fade", 0.6),
    # Between chapters. The dip is short enough that blackdetect does not
    # read it as a hole in the edit.
    "dip": Transition("dip", "fadeblack", 0.8),
    # Into a countdown entry.
    "whip": Transition("whip", "smoothleft", 0.35, sound="whoosh"),
    "wipe": Transition("wipe", "wiperight", 0.5, sound="whoosh"),
    "zoom": Transition("zoom", "zoomin", 0.5, sound="whoosh"),
    # Into a reveal.
    "flash": Transition("flash", "fadewhite", 0.4, sound="impact"),
}

#: Words a writer might use, mapped onto the catalogue.
ALIASES = {
    "fade": "dip",
    "fade to black": "dip",
    "crossfade": "dissolve",
    "cross dissolve": "dissolve",
    "slide": "whip",
    "whip pan": "whip",
    "white flash": "flash",
}


def resolve(name: str | None) -> Transition:
    """The transition a name means. Anything unknown is a cut."""
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    return TRANSITIONS.get(key, TRANSITIONS["cut"])


def overlap_for(name: str | None) -> float:
    return resolve(name).overlap_s


@dataclass(frozen=True)
class Piece:
    """One file in the final join: a body of a shot, or an overlap.

    Everything is counted in frames, so a long film of short pieces cannot
    drift a frame per piece away from the sound.
    """

    kind: str  # "body" or "overlap"
    #: For a body, the clip index. For an overlap, the outgoing clip index.
    index: int
    start_frame: int
    frames: int
    fps: int
    xfade: str = ""

    @property
    def start_s(self) -> float:
        return self.start_frame / self.fps

    @property
    def duration_s(self) -> float:
        return self.frames / self.fps


#: A body shorter than this is not worth keeping a transition for.
MIN_BODY_FRAMES = 5


def overlap_frames(
    durations: list[float], transitions_out: list[str | None], fps: int
) -> list[tuple[int, str]]:
    """The overlap after each clip, in frames, with its xfade name.

    ``transitions_out[i]`` is the transition from clip ``i`` into clip
    ``i + 1``. A clip too short for the overlaps either side of it gets a
    cut instead, because there would be no body left to show.
    """
    frames = [max(1, round(d * fps)) for d in durations]
    result: list[tuple[int, str]] = []
    for index in range(len(frames) - 1):
        name = transitions_out[index] if index < len(transitions_out) else None
        chosen = resolve(name)
        result.append((round(chosen.overlap_s * fps), chosen.xfade))

    for index in range(len(result)):
        head = result[index - 1][0] if index > 0 else 0
        overlap = result[index][0]
        if frames[index] - head - overlap < MIN_BODY_FRAMES or frames[index + 1] - overlap < MIN_BODY_FRAMES:
            result[index] = (0, "")
    return result


def pieces(
    durations: list[float], transitions_out: list[str | None], fps: int
) -> list[Piece]:
    """Split the film into bodies and overlaps, in playing order."""
    frames = [max(1, round(d * fps)) for d in durations]
    overlaps = overlap_frames(durations, transitions_out, fps)
    result: list[Piece] = []
    for index, count in enumerate(frames):
        head = overlaps[index - 1][0] if index > 0 else 0
        tail = overlaps[index][0] if index < len(overlaps) else 0
        result.append(Piece("body", index, head, count - head - tail, fps))
        if tail > 0:
            result.append(
                Piece("overlap", index, count - tail, tail, fps, overlaps[index][1])
            )
    return result


def total_s(durations: list[float], transitions_out: list[str | None], fps: int) -> float:
    return round(sum(p.frames for p in pieces(durations, transitions_out, fps)) / fps, 3)


def overlap_command(
    ffmpeg_path: str,
    outgoing: Path,
    incoming: Path,
    destination: Path,
    *,
    piece: Piece,
) -> list[str]:
    """The tail of one clip over the head of the next, as its own file."""
    end = piece.start_frame + piece.frames
    # xfade needs a duration strictly inside its inputs, so the fade runs
    # one frame short of the piece and the last frame is pure shot B.
    fade_s = max(1, piece.frames - 1) / piece.fps
    return [
        ffmpeg_path, "-hide_banner", "-y",
        "-i", str(outgoing),
        "-i", str(incoming),
        "-filter_complex",
        (
            f"[0:v]trim=start_frame={piece.start_frame}:end_frame={end},"
            "setpts=PTS-STARTPTS,settb=AVTB[a];"
            f"[1:v]trim=end_frame={piece.frames},setpts=PTS-STARTPTS,settb=AVTB[b];"
            f"[a][b]xfade=transition={piece.xfade}:duration={fade_s:.4f}:offset=0,"
            "format=yuv420p[v]"
        ),
        "-map", "[v]",
        "-frames:v", str(piece.frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2",
        "-an", str(destination),
    ]


def body_command(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    *,
    piece: Piece,
) -> list[str]:
    """The part of a clip that no transition touches, cut frame-exact."""
    return [
        ffmpeg_path, "-hide_banner", "-y",
        "-i", str(source),
        "-vf", (
            f"trim=start_frame={piece.start_frame}:end_frame={piece.start_frame + piece.frames},"
            "setpts=PTS-STARTPTS,format=yuv420p"
        ),
        "-frames:v", str(piece.frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2",
        "-an", str(destination),
    ]
