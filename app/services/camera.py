"""Camera moves over a still, rendered with FFmpeg.

This is the pipeline's floor: whatever else fails, a scene can always be
given a slow push or a drift across its still and come out the right
length. It is what makes a five-minute video achievable on a machine with
no usable GPU.

It is **not animation**, and this module is deliberate about saying so.
Nothing here invents motion that was not in the source picture; it moves a
window around a photograph. Clips produced here are recorded with
``is_generated_motion = False`` and are counted under assembled length,
never under generated motion.

On the filter choice: ``zoompan`` computes its crop window in integers, so
on a source the size of the output it visibly stutters. Pre-scaling the
still before it reaches ``zoompan`` gives those integers somewhere finer
to land. The scale factor is capped because this machine has 8 GB of RAM
and ``zoompan`` holds a whole upscaled frame at a time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from app.constants import AnimationMethod

log = logging.getLogger(__name__)

RENDER_TIMEOUT_S = 900

#: How much finer than the output the still is made before ``zoompan``
#: sees it. Measured on this machine: 2.5x removes the visible stepping on
#: a slow push without pushing a 720p render past a few hundred MB of RAM.
PRESCALE = 2.5
#: Hard ceiling on the pre-scaled width, for the same memory reason.
MAX_PRESCALE_WIDTH = 3840

#: Fraction of the frame a push travels over the whole shot. Kept small:
#: a move the viewer notices is a move that looks like a slideshow.
DEFAULT_ZOOM = 0.12
DEFAULT_PAN = 0.10

MIN_DURATION_S = 0.5
MAX_DURATION_S = 60.0


@dataclass(frozen=True)
class CameraMove:
    """One named move, with the human words for describing it honestly."""

    key: str
    display_name: str
    #: What this actually does, for the UI and the production report.
    description: str


MOVES: tuple[CameraMove, ...] = (
    CameraMove("push_in", "Slow push in", "The frame closes in on the centre of the still."),
    CameraMove("pull_out", "Slow pull out", "The frame opens out from the centre of the still."),
    CameraMove("pan_left", "Pan left", "The frame travels left across the still."),
    CameraMove("pan_right", "Pan right", "The frame travels right across the still."),
    CameraMove("tilt_up", "Tilt up", "The frame travels up the still."),
    CameraMove("tilt_down", "Tilt down", "The frame travels down the still."),
    CameraMove("hold", "Held frame", "No movement at all. The still is simply held."),
    CameraMove("drift_up", "Drift up and across", "The frame drifts diagonally up across the still while closing in a little."),
    CameraMove("drift_down", "Drift down and across", "The frame drifts diagonally down across the still while closing in a little."),
    CameraMove("push_focus", "Push to a face", "The frame closes in on the face or subject found in the still."),
    CameraMove("reveal_push", "Fast push", "The frame closes in quickly at the start of the shot, then settles."),
)
MOVES_BY_KEY: dict[str, CameraMove] = {move.key: move for move in MOVES}
DEFAULT_MOVE = "push_in"

#: Words a scene's camera note might use, mapped to a move. A note that
#: matches nothing falls back to the default rather than being guessed at.
_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("push in", "push-in", "dolly in", "zoom in", "closer", "close in"), "push_in"),
    (("pull out", "pull-out", "zoom out", "dolly out", "wider", "pull back"), "pull_out"),
    (("pan left", "track left", "move left"), "pan_left"),
    (("pan right", "track right", "move right"), "pan_right"),
    (("tilt up", "crane up", "look up", "rise"), "tilt_up"),
    (("tilt down", "crane down", "look down", "descend"), "tilt_down"),
    (("static", "locked", "still", "hold", "no movement"), "hold"),
    (("drift up", "float up", "diagonal up"), "drift_up"),
    (("drift down", "float down", "diagonal"), "drift_down"),
    (("on his face", "on her face", "to the face", "on the face", "close on"), "push_focus"),
    (("snap", "fast push", "crash zoom", "punch in"), "reveal_push"),
)

#: How far a push toward a face travels. A face is smaller than the frame,
#: so the move has to go further than a centred push to read.
FOCUS_ZOOM = 0.20
REVEAL_ZOOM = 0.18

#: Moves that end on a point rather than the centre, when one is known.
FOCUS_MOVES = frozenset({"push_in", "push_focus", "reveal_push"})


def ease(progress: str, curve: str = "inout") -> str:
    """An FFmpeg expression easing a 0..1 progress value.

    ``inout`` starts slowly, travels, and settles, like a camera operator
    leaning on a dolly. ``out`` is front-loaded for a reveal.
    """
    if curve == "out":
        return f"(1-pow(1-{progress},3))"
    return f"(0.5-0.5*cos(PI*{progress}))"


def move_for(camera_note: str | None, *, default: str = DEFAULT_MOVE) -> str:
    """Pick a move from the script's camera direction.

    The script writes prose like "slow push through the doorway"; this
    turns that into one of the moves we can actually render. Anything
    unrecognised gets the default, which is a gentle push.
    """
    note = (camera_note or "").strip().lower()
    if not note:
        return default
    for words, key in _KEYWORDS:
        if any(word in note for word in words):
            return key
    return default


def _even(value: int) -> int:
    """H.264 with yuv420p needs even dimensions in both axes."""
    return value if value % 2 == 0 else value + 1


def prescale_width(width: int) -> int:
    return _even(min(int(width * PRESCALE), MAX_PRESCALE_WIDTH))


def build_filter(
    move: str,
    *,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    focus: tuple[float, float] | None = None,
) -> str:
    """The filtergraph for one camera move.

    ``zoompan`` is given an explicit frame count so the clip comes out at
    exactly the requested length rather than however long the filter feels
    like running. Every move is eased: it starts slowly and settles.

    ``focus`` is a point in the still, as fractions of its width and
    height, that a push should end on instead of the centre.
    """
    frames = max(1, round(duration_s * fps))
    big_w = prescale_width(width)
    big_h = _even(round(big_w * height / width))

    # Fill the frame first, then crop: a still with a different aspect
    # ratio gets covered rather than letterboxed or stretched.
    cover = (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={big_w}:{big_h},setsar=1"
    )

    # A JPEG is full-range, and an encoder left to itself tags the result
    # yuvj420p, which some players read as washed out. Converting to
    # limited range here means the file is plain broadcast-range yuv420p.
    finish = "scale=in_range=auto:out_range=tv,format=yuv420p"

    if move == "hold":
        return f"{cover},scale={width}:{height},fps={fps},{finish}"

    centre_x = "iw/2-(iw/zoom/2)"
    centre_y = "ih/2-(ih/zoom/2)"
    progress = f"min(on/{max(1, frames - 1)},1)"
    smooth = ease(progress)
    travel_x = "(iw-iw/zoom)"
    travel_y = "(ih-ih/zoom)"

    def toward(point: tuple[float, float], curve: str) -> tuple[str, str]:
        # The window's centre slides from the middle of the still to the
        # point, and is clamped so it never shows past the still's edge.
        fx = min(max(point[0], 0.0), 1.0)
        fy = min(max(point[1], 0.0), 1.0)
        amount = ease(progress, curve)
        cx = f"(0.5+{fx - 0.5:.4f}*{amount})"
        cy = f"(0.5+{fy - 0.5:.4f}*{amount})"
        return (
            f"clip({cx}*iw-iw/zoom/2,0,{travel_x})",
            f"clip({cy}*ih-ih/zoom/2,0,{travel_y})",
        )

    if move == "push_in":
        expr_z = f"1+{DEFAULT_ZOOM:.4f}*{smooth}"
        expr_x, expr_y = toward(focus, "inout") if focus else (centre_x, centre_y)
    elif move == "push_focus":
        expr_z = f"1+{FOCUS_ZOOM:.4f}*{smooth}"
        expr_x, expr_y = toward(focus or (0.5, 0.4), "inout")
    elif move == "reveal_push":
        expr_z = f"1+{REVEAL_ZOOM:.4f}*{ease(progress, 'out')}"
        expr_x, expr_y = toward(focus, "out") if focus else (centre_x, centre_y)
    elif move == "pull_out":
        expr_z = f"1+{DEFAULT_ZOOM:.4f}*(1-{smooth})"
        expr_x, expr_y = centre_x, centre_y
    elif move in ("drift_up", "drift_down"):
        # A slight push while the window crosses the still on a diagonal.
        expr_z = f"{1.0 + DEFAULT_PAN:.4f}+0.04*{smooth}"
        expr_x = f"{travel_x}*(0.15+0.7*{smooth})"
        if move == "drift_up":
            expr_y = f"{travel_y}*(0.85-0.7*{smooth})"
        else:
            expr_y = f"{travel_y}*(0.15+0.7*{smooth})"
    else:
        # A pan holds a fixed magnification and slides the window across.
        expr_z = f"{1.0 + DEFAULT_PAN:.4f}"
        if move == "pan_left":
            expr_x, expr_y = f"{travel_x}*(1-{smooth})", centre_y
        elif move == "pan_right":
            expr_x, expr_y = f"{travel_x}*{smooth}", centre_y
        elif move == "tilt_up":
            expr_x, expr_y = centre_x, f"{travel_y}*(1-{smooth})"
        elif move == "tilt_down":
            expr_x, expr_y = centre_x, f"{travel_y}*{smooth}"
        else:
            raise ValueError(f"Unknown camera move: {move}")

    return (
        f"{cover},"
        f"zoompan=z='{expr_z}':x='{expr_x}':y='{expr_y}'"
        f":d={frames}:s={width}x{height}:fps={fps},"
        f"{finish}"
    )


def build_command(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    *,
    move: str,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    focus: tuple[float, float] | None = None,
) -> list[str]:
    """A complete FFmpeg invocation for one camera move.

    Silent on purpose. Audio is mixed in once, over the whole timeline,
    rather than being carried by individual clips.
    """
    if move not in MOVES_BY_KEY:
        raise ValueError(f"Unknown camera move: {move}")
    if not MIN_DURATION_S <= duration_s <= MAX_DURATION_S:
        raise ValueError(
            f"A camera move must be between {MIN_DURATION_S}s and {MAX_DURATION_S}s, "
            f"not {duration_s:.2f}s."
        )

    return [
        ffmpeg_path,
        "-y",
        "-loop", "1",
        "-i", str(source),
        "-t", f"{duration_s:.3f}",
        "-vf", build_filter(
            move, width=width, height=height, fps=fps, duration_s=duration_s, focus=focus,
        ),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-an",
        str(destination),
    ]


@dataclass
class CameraRenderResult:
    ok: bool
    path: Path | None = None
    message: str = ""
    command: list[str] | None = None


async def render(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    *,
    move: str,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    focus: tuple[float, float] | None = None,
) -> CameraRenderResult:
    """Render one camera move. Does not probe the result; the caller does."""
    if not source.exists():
        return CameraRenderResult(False, message=f"The still is missing: {source}")

    try:
        command = build_command(
            ffmpeg_path, source, destination,
            move=move, width=width, height=height, fps=fps, duration_s=duration_s,
            focus=focus,
        )
    except ValueError as exc:
        return CameraRenderResult(False, message=str(exc))

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part.mp4")
    command[-1] = str(partial)
    partial.unlink(missing_ok=True)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return CameraRenderResult(False, message=f"FFmpeg could not be started: {exc}")

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=RENDER_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        partial.unlink(missing_ok=True)
        return CameraRenderResult(
            False, message=f"FFmpeg did not finish within {RENDER_TIMEOUT_S}s."
        )

    detail = stderr.decode("utf-8", "replace").strip()
    if process.returncode or not partial.exists():
        partial.unlink(missing_ok=True)
        tail = "\n".join(detail.splitlines()[-4:])
        return CameraRenderResult(
            False, message=f"FFmpeg exited with code {process.returncode}. {tail}"[:600]
        )

    destination.unlink(missing_ok=True)
    partial.replace(destination)
    return CameraRenderResult(True, path=destination, command=command)


def describe(move: str) -> str:
    """A sentence for the production report. Never calls this animation."""
    entry = MOVES_BY_KEY.get(move)
    if entry is None:
        return "An unrecognised camera move over a still image."
    return f"{entry.description} The picture itself does not move."


#: Methods this module can produce. Both are editing, not generation.
PRODUCES: frozenset[str] = frozenset(
    {AnimationMethod.CAMERA_MOVE, AnimationMethod.STILL}
)
