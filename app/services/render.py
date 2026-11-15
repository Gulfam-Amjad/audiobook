"""Putting the finished video together, and then checking it.

The assembly itself is unremarkable: join the selected clips, build the
audio, burn or attach subtitles, mux. What matters is the step after it.

**A render is not finished until it has been measured.** Nothing in this
module reports success from an exit code. The file is probed, its streams
are compared against each other and against the plan, it is scanned for
black runs, and its loudness is measured. A render whose checks fail is
recorded as failed even though FFmpeg returned zero, because a file that
exists is not the same as a file that is right.

**The two kinds of length stay separate to the end.** ``duration_s`` is
how long the video is; ``generated_motion_s`` is how much of it a model
animated. The second is carried into the render row and into the report,
so no summary can quote the first and imply the second.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.constants import (
    AnimationMethod,
    AudioStatus,
    ClipStatus,
    PipelineStage,
    ProjectStatus,
    RenderStatus,
    StageStatus,
)
from app.models import AudioSegment, Project, Render, Scene, SceneClip
from app.services import ffmpeg, graphics, mixing, production, subtitles, transitions
from app.services import projects as project_service
from app.services import routing as routing_service
from app.services import scripts as script_service
from app.services.paths import safe_filename
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

#: Generous, because a five-minute 1080p encode on this hardware is not
#: quick, but bounded so a wedged FFmpeg does not hang the app forever.
ENCODE_TIMEOUT_S = 3 * 3600
PROBE_TIMEOUT_S = 300

#: How far the finished file may sit from the planned length before it is
#: treated as wrong. One frame either side of each cut is normal.
DURATION_TOLERANCE_S = 0.5
#: Audio and video must end within this of each other or the file drifts.
SYNC_TOLERANCE_S = 0.25
#: A black run longer than this is a hole in the edit, not a transition.
BLACK_RUN_S = 0.5

RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}


class RenderError(Exception):
    """Something the user must fix before a render can start."""


def frame_size(project: Project) -> tuple[int, int]:
    width, height = RESOLUTIONS.get(project.resolution, RESOLUTIONS["720p"])
    if project.aspect_ratio == "9:16":
        return height, width
    if project.aspect_ratio == "1:1":
        return height, height
    return width, height


def render_dir(project: Project) -> Path:
    folder = safe_filename(
        f"{project.id:04d}-{project.name}", fallback=f"{project.id:04d}"
    )
    return get_settings().output_dir / folder


# ---------------------------------------------------------------------------
# Gathering what exists
# ---------------------------------------------------------------------------


@dataclass
class RenderPlan:
    """Everything the render needs, and everything missing from it."""

    scenes: list[dict[str, Any]] = field(default_factory=list)
    clips: list[Path] = field(default_factory=list)
    timeline: mixing.Timeline | None = None
    sheet: subtitles.CueSheet | None = None
    width: int = 0
    height: int = 0
    fps: int = 24
    generated_motion_s: float = 0.0
    camera_move_s: float = 0.0
    #: Real footage from a media library: moving, but not generated.
    footage_s: float = 0.0
    #: The finishing pass settings, empty when the project does not use it.
    finishing: dict[str, Any] = field(default_factory=dict)
    #: Per shot, in order: the director's fields after defaults.
    directions: list[dict[str, Any]] = field(default_factory=list)
    #: Where each shot starts on the finished timeline.
    shot_starts: list[float] = field(default_factory=list)
    #: Graphics actually laid, with absolute times, for the sound design.
    cards: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def planned_duration_s(self) -> float:
        return self.timeline.total_s if self.timeline else 0.0

    @property
    def ready(self) -> bool:
        return bool(self.clips) and not self.problems


async def plan_render(session: AsyncSession, project_id: int) -> RenderPlan:
    """Work out what the finished video would be, without making it."""
    project = await project_service.get_project(session, project_id)
    plan = RenderPlan()
    plan.width, plan.height = frame_size(project)
    plan.fps = project.fps

    script = await script_service.approved_script(session, project_id)
    if script is None:
        plan.problems.append("No approved script, so there is nothing to assemble.")
        return plan

    scenes = list(
        (
            await session.execute(
                select(Scene)
                .where(Scene.script_id == script.id)
                .order_by(Scene.order_index)
            )
        )
        .scalars()
        .all()
    )
    if not scenes:
        plan.problems.append("The approved script has no shots in it.")
        return plan

    for scene in scenes:
        clip = await _selected_clip(session, scene.id)
        entry: dict[str, Any] = {
            "scene_key": scene.scene_key,
            "scene_id": scene.id,
            "clip_duration_s": None,
            "segments": [],
        }

        if clip is None:
            plan.problems.append(
                f"{scene.scene_key}: no clip is in the edit for this shot. "
                "Render the shots first."
            )
        elif not clip.file_path or not Path(clip.file_path).exists():
            plan.problems.append(
                f"{scene.scene_key}: the clip in the edit is missing from disk."
            )
        elif clip.duration_s is None:
            # An unmeasured clip would make every later cue wrong.
            plan.problems.append(
                f"{scene.scene_key}: the clip in the edit was never measured, "
                "so nothing after it could be timed."
            )
        else:
            entry["clip_duration_s"] = float(clip.duration_s)
            plan.clips.append(Path(clip.file_path))
            if clip.is_generated_motion:
                plan.generated_motion_s += float(clip.duration_s)
            elif clip.method == AnimationMethod.STOCK_FOOTAGE:
                plan.footage_s += float(clip.duration_s)
            else:
                plan.camera_move_s += float(clip.duration_s)

        segments = list(
            (
                await session.execute(
                    select(AudioSegment)
                    .where(AudioSegment.scene_id == scene.id)
                    .order_by(AudioSegment.order_index)
                )
            )
            .scalars()
            .all()
        )
        for segment in segments:
            entry["segments"].append(
                {
                    "text": segment.text,
                    "duration_s": segment.duration_s,
                    "file_path": segment.file_path,
                    "status": segment.status,
                }
            )
            if segment.status != AudioStatus.READY or segment.duration_s is None:
                plan.warnings.append(
                    f"{scene.scene_key}: a line has no finished audio, so it will be "
                    "silent and will carry no subtitle."
                )
        plan.scenes.append(entry)
        plan.directions.append(dict(scene.direction or {}))

    plan.finishing = production.settings(project)
    if (
        plan.finishing.get("transitions")
        and not plan.problems
        and all(entry["clip_duration_s"] for entry in plan.scenes)
    ):
        overlaps = transitions.overlap_frames(
            [float(entry["clip_duration_s"]) for entry in plan.scenes],
            [d.get("transition") for d in plan.directions],
            plan.fps,
        )
        for entry, (frames, _name) in zip(plan.scenes, overlaps):
            entry["overlap_after_s"] = frames / plan.fps

    cursor = 0.0
    for entry in plan.scenes:
        plan.shot_starts.append(round(cursor, 4))
        cursor += float(entry["clip_duration_s"] or 0.0) - float(entry.get("overlap_after_s") or 0.0)

    plan.timeline = mixing.plan_timeline(plan.scenes)
    plan.warnings.extend(plan.timeline.warnings)

    plan.sheet = subtitles.build(
        [
            {
                "text": placement.text,
                "start_s": placement.start_s,
                "duration_s": placement.duration_s,
                "scene_key": placement.scene_key,
            }
            for placement in plan.timeline.placements
        ]
    )
    plan.warnings.extend(plan.sheet.warnings)
    plan.warnings.extend(plan.timeline.unplaced)

    if not plan.timeline.placements:
        spoken = any(
            (scene.narration or "").strip() or scene.dialogue for scene in scenes
        )
        if spoken:
            # The script has words to say. A silent cut of it is not a
            # finished video, however cleanly it encodes.
            plan.problems.append(
                "The script has narration or dialogue, but none of it has been "
                "spoken yet. Synthesise the speech before rendering."
            )
        else:
            plan.warnings.append(
                "No measured narration was found, so the video would have no voice track."
            )

    plan.generated_motion_s = round(plan.generated_motion_s, 2)
    plan.camera_move_s = round(plan.camera_move_s, 2)
    plan.footage_s = round(plan.footage_s, 2)
    return plan


async def _selected_clip(session: AsyncSession, scene_id: int) -> SceneClip | None:
    return (
        await session.execute(
            select(SceneClip)
            .where(
                SceneClip.scene_id == scene_id,
                SceneClip.selected.is_(True),
                SceneClip.status == ClipStatus.READY,
            )
            .order_by(SceneClip.id.desc())
        )
    ).scalars().first()


# ---------------------------------------------------------------------------
# Running FFmpeg
# ---------------------------------------------------------------------------


async def _run(command: list[str], *, timeout: int = ENCODE_TIMEOUT_S) -> tuple[int, str]:
    """Run FFmpeg, returning its exit code and stderr. Never a shell string."""
    log.info("ffmpeg: %s", " ".join(command[:8]))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 1, f"FFmpeg did not finish within {timeout}s and was stopped."
    return process.returncode or 0, (stderr or b"").decode("utf-8", "replace")


def normalise_command(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    fps: int,
    look: str = "",
) -> list[str]:
    """One clip forced to the project's shape, so concat can stream-copy.

    Letterboxing rather than cropping: a clip that came back from a remote
    model at a different aspect ratio should lose nothing. ``look`` is the
    finishing grade, applied after the frame is the right shape.
    """
    return [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-vf",
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},"
            + (f"{look}," if look else "")
            + "scale=in_range=auto:out_range=tv,format=yuv420p"
        ),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-an",
        str(destination),
    ]


def mux_command(
    ffmpeg_path: str,
    video: Path,
    audio: Path | None,
    destination: Path,
    *,
    subtitle_path: Path | None = None,
    style: dict[str, Any] | None = None,
    burn_filter: str | None = None,
) -> list[str]:
    """Put picture, sound and subtitles into the delivered file.

    ``burn_filter`` is a ready-made caption filter, such as the word
    captions, and takes the place of the SRT burn-in.
    """
    command = [ffmpeg_path, "-hide_banner", "-y", "-i", str(video)]
    if audio is not None:
        command += ["-i", str(audio)]

    if burn_filter:
        command += [
            "-filter_complex",
            f"[0:v]{burn_filter},format=yuv420p[v]",
            "-map",
            "[v]",
        ]
    elif subtitle_path is not None:
        command += [
            "-filter_complex",
            f"[0:v]{subtitles.burn_in_filter(subtitle_path, style)},format=yuv420p[v]",
            "-map",
            "[v]",
        ]
    else:
        command += ["-map", "0:v", "-vf", "format=yuv420p"]

    if audio is not None:
        command += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    command += [
        "-c:v",
        "libx264",
        # ultrafast and one thread. veryfast ran out of memory on this
        # 8 GB machine while burning subtitles over the full picture.
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        # The video is the authority on length; a mix that ran long is cut
        # rather than allowed to extend the file past the last shot.
        "-shortest",
        str(destination),
    ]
    return command


def black_command(ffmpeg_path: str, source: Path) -> list[str]:
    return [
        ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-vf",
        f"blackdetect=d={BLACK_RUN_S}:pic_th=0.98",
        "-f",
        "null",
        "-",
    ]


def parse_black_runs(stderr: str) -> list[dict[str, float]]:
    """Read blackdetect's findings. An unparseable line is skipped, not faked."""
    runs: list[dict[str, float]] = []
    for line in stderr.splitlines():
        if "black_start" not in line:
            continue
        values: dict[str, float] = {}
        for token in line.split():
            if ":" not in token:
                continue
            key, _, raw = token.partition(":")
            if key in {"black_start", "black_end", "black_duration"}:
                try:
                    values[key] = float(raw)
                except ValueError:
                    continue
        if "black_start" in values:
            runs.append(values)
    return runs


# ---------------------------------------------------------------------------
# The render itself
# ---------------------------------------------------------------------------


@dataclass
class RenderOutcome:
    """What a render attempt produced, measured rather than assumed."""

    render_id: int | None = None
    status: str = RenderStatus.PENDING
    output_path: str = ""
    subtitle_path: str = ""
    duration_s: float | None = None
    generated_motion_s: float = 0.0
    problems: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == RenderStatus.COMPLETED


async def render_project(
    session: AsyncSession, project_id: int, *, burn_subtitles: bool = True
) -> RenderOutcome:
    """Assemble, mux, then check. Only a file that passes is a success."""
    project = await project_service.get_project(session, project_id)
    plan = await plan_render(session, project_id)
    outcome = RenderOutcome(generated_motion_s=plan.generated_motion_s)

    if not plan.ready:
        outcome.status = RenderStatus.FAILED
        outcome.problems = list(plan.problems) or [
            "There is nothing to assemble."
        ]
        await project_service.mark_stage(
            session,
            project_id,
            PipelineStage.RENDER,
            StageStatus.BLOCKED,
            message=outcome.problems[0],
            blocked_reason=outcome.problems[0],
        )
        return outcome

    status = await ffmpeg.detect()
    if not status.available or not status.ffmpeg_path:
        message = status.message or "FFmpeg is not installed."
        outcome.status = RenderStatus.FAILED
        outcome.problems = [message]
        await project_service.mark_stage(
            session,
            project_id,
            PipelineStage.RENDER,
            StageStatus.BLOCKED,
            message=message,
            blocked_reason=message,
        )
        return outcome

    record = Render(
        project_id=project_id,
        status=RenderStatus.RENDERING,
        generated_motion_s=plan.generated_motion_s,
        width=plan.width,
        height=plan.height,
        fps=plan.fps,
        stage_message="Preparing the shots.",
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    outcome.render_id = record.id

    await project_service.mark_stage(
        session, project_id, PipelineStage.RENDER, StageStatus.RUNNING
    )

    folder = render_dir(project)
    # AVM_RENDER_WORK names an earlier attempt's folder to resume from: finished
    # clips and cards there are reused, not rendered again.
    resume = os.environ.get("AVM_RENDER_WORK")
    work = Path(resume) if resume else folder / f"render-{record.id:04d}"
    work.mkdir(parents=True, exist_ok=True)

    try:
        result = await _assemble(
            session,
            project,
            record,
            plan,
            work,
            status.ffmpeg_path,
            status.ffprobe_path or "",
            burn_subtitles=burn_subtitles,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        log.exception("Render %s failed", record.id)
        record.status = RenderStatus.FAILED
        record.error = f"{type(exc).__name__}: {exc}"
        record.completed_at = utcnow()
        await session.commit()
        outcome.status = RenderStatus.FAILED
        outcome.problems = [record.error]
        await project_service.mark_stage(
            session,
            project_id,
            PipelineStage.RENDER,
            StageStatus.FAILED,
            message=record.error,
        )
        return outcome

    return result


#: Narration lines per ffmpeg call. Windows caps a command line near 32,000
#: characters, and a film of this length has a few hundred spoken lines.
SPEECH_BATCH = 60


async def _speech_track(
    ffmpeg_path: str, timeline: "mixing.Timeline", speech: Path, work: Path
) -> tuple[int, str]:
    """The narration track, built in batches and summed, so no command grows too long."""
    placements = timeline.placements
    if len(placements) <= SPEECH_BATCH:
        return await _run(mixing.speech_command(
            ffmpeg_path, placements, speech, total_s=timeline.total_s
        ))
    parts: list[Path] = []
    for number, start in enumerate(range(0, len(placements), SPEECH_BATCH)):
        part = work / f"speech-part-{number:02d}.wav"
        code, stderr = await _run(mixing.speech_command(
            ffmpeg_path, placements[start:start + SPEECH_BATCH], part, total_s=timeline.total_s
        ))
        if code != 0 or not part.exists():
            return code or 1, stderr
        parts.append(part)
    command = [ffmpeg_path, "-hide_banner", "-y"]
    for part in parts:
        command += ["-i", str(part)]
    graph = "".join(f"[{i}:a]" for i in range(len(parts)))
    graph += f"amix=inputs={len(parts)}:duration=longest:normalize=0[speech]"
    command += [
        "-filter_complex", graph, "-map", "[speech]",
        "-t", f"{timeline.total_s:.3f}",
        "-c:a", "pcm_s16le", "-ar", str(mixing.SAMPLE_RATE), "-ac", str(mixing.CHANNELS),
        str(speech),
    ]
    return await _run(command)


def _done(path: Path) -> bool:
    """True when an earlier attempt at this render finished the file."""
    return path.exists() and path.stat().st_size > 0


async def _lay_graphics(
    plan: RenderPlan,
    project: Project,
    normalised: list[Path],
    work: Path,
    ffmpeg_path: str,
) -> list[Path]:
    """Chapter, countdown, place and title cards, one shot at a time."""
    result: list[Path] = []
    last = len(normalised) - 1
    for index, clip in enumerate(normalised):
        entry = plan.scenes[index]
        direction = plan.directions[index] if index < len(plan.directions) else {}
        overlays = graphics.plan_overlays(
            direction,
            clip_s=float(entry["clip_duration_s"]),
            index=index,
            last=index == last,
            film_title=str(plan.finishing.get("title_card") or ""),
            end_card=str(plan.finishing.get("end_card") or ""),
            work=work,
            width=plan.width,
            height=plan.height,
            overlap_in=float(plan.scenes[index - 1].get("overlap_after_s") or 0.0) if index else 0.0,
            overlap_out=float(entry.get("overlap_after_s") or 0.0),
        )
        if not overlays:
            result.append(clip)
            continue
        target = work / f"card-{index:03d}.mp4"
        if _done(target):
            code, stderr = 0, ""
        else:
            code, stderr = await _run(
                graphics.overlay_command(
                    ffmpeg_path, clip, target, overlays, width=plan.width, height=plan.height
                )
            )
        if code != 0 or not target.exists():
            # A missing card is a note, not a broken film.
            plan.warnings.append(
                f"{entry['scene_key']}: the card could not be drawn ({stderr.strip()[-160:]})."
            )
            result.append(clip)
            continue
        start = plan.shot_starts[index] if index < len(plan.shot_starts) else 0.0
        for overlay in overlays:
            plan.cards.append(
                {
                    "kind": overlay.png.stem.rsplit("-", 1)[-1],
                    "start_s": round(start + overlay.start_s, 3),
                    "end_s": round(start + overlay.end_s, 3),
                    "dim": overlay.dim_background,
                }
            )
        result.append(target)
    return result


async def _transition_pieces(
    plan: RenderPlan, normalised: list[Path], work: Path, ffmpeg_path: str
) -> list[Path]:
    """Bodies and overlaps, rendered one at a time, in playing order."""
    durations = [float(entry["clip_duration_s"]) for entry in plan.scenes]
    names = [d.get("transition") for d in plan.directions]
    files: list[Path] = []
    for number, piece in enumerate(transitions.pieces(durations, names, plan.fps)):
        target = work / f"piece-{number:04d}.mp4"
        whole = round(durations[piece.index] * plan.fps)
        if piece.kind == "body" and piece.start_frame == 0 and piece.frames == whole:
            files.append(normalised[piece.index])
            continue
        if _done(target):
            files.append(target)
            continue
        if piece.kind == "body":
            command = transitions.body_command(
                ffmpeg_path, normalised[piece.index], target, piece=piece
            )
        else:
            command = transitions.overlap_command(
                ffmpeg_path,
                normalised[piece.index],
                normalised[piece.index + 1],
                target,
                piece=piece,
            )
        code, stderr = await _run(command)
        if code != 0 or not target.exists():
            raise RenderError(
                f"A transition piece could not be made: {stderr.strip()[-300:]}"
            )
        files.append(target)
    return files


async def _assemble(
    session: AsyncSession,
    project: Project,
    record: Render,
    plan: RenderPlan,
    work: Path,
    ffmpeg_path: str,
    ffprobe_path: str,
    *,
    burn_subtitles: bool,
) -> RenderOutcome:
    outcome = RenderOutcome(
        render_id=record.id, generated_motion_s=plan.generated_motion_s
    )

    # --- picture ----------------------------------------------------------
    record.stage_message = "Matching every shot to the output frame."
    record.progress = 0.1
    await session.commit()

    normalised: list[Path] = []
    for index, source in enumerate(plan.clips):
        target = work / f"clip-{index:03d}.mp4"
        if _done(target):
            # Finished by an earlier attempt at this same render: resume, do not redo.
            normalised.append(target)
            continue
        code, stderr = await _run(
            normalise_command(
                ffmpeg_path,
                source,
                target,
                width=plan.width,
                height=plan.height,
                fps=plan.fps,
                look=production.look_filter(plan.finishing),
            )
        )
        if code != 0 or not target.exists():
            raise RenderError(
                f"{source.name} could not be prepared: {stderr.strip()[-300:]}"
            )
        normalised.append(target)

    if plan.finishing.get("cards"):
        record.stage_message = "Laying the title cards and labels."
        record.progress = 0.25
        await session.commit()
        normalised = await _lay_graphics(plan, project, normalised, work, ffmpeg_path)

    record.stage_message = "Joining the shots."
    record.progress = 0.35
    await session.commit()

    join_list = normalised
    if plan.finishing.get("transitions") and any(
        entry.get("overlap_after_s") for entry in plan.scenes
    ):
        join_list = await _transition_pieces(plan, normalised, work, ffmpeg_path)

    listing = work / "clips.txt"
    listing.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in join_list),
        encoding="utf-8",
    )
    joined = work / "picture.mp4"
    code, stderr = await _run(
        [
            ffmpeg_path, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(joined),
        ]
    )
    if code != 0 or not joined.exists():
        raise RenderError(f"The shots could not be joined: {stderr.strip()[-300:]}")

    # --- sound ------------------------------------------------------------
    record.stage_message = "Laying the narration onto the timeline."
    record.progress = 0.5
    await session.commit()

    audio: Path | None = None
    loudness: dict[str, float | None] = {}
    sound_credits: list[str] = []
    sound_events = 0
    assert plan.timeline is not None
    if plan.timeline.placements:
        speech = work / "speech.wav"
        code, stderr = await _speech_track(ffmpeg_path, plan.timeline, speech, work)
        if code != 0 or not speech.exists():
            raise RenderError(
                f"The narration track could not be built: {stderr.strip()[-300:]}"
            )

        music = Path(project.music_path) if project.music_path else None
        if music is not None and not music.exists():
            plan.warnings.append(
                f"The music bed at {music} is missing, so the mix is narration only."
            )
            music = None

        audio = work / "mix.wav"
        code, stderr = 1, ""
        if plan.finishing.get("sound_design"):
            record.stage_message = "Placing the music, ambience and effects."
            await session.commit()
            from app.services import sound_design

            sound = await asyncio.to_thread(
                sound_design.plan,
                plan.directions,
                shot_starts=plan.shot_starts,
                clip_durations=[float(e["clip_duration_s"] or 0.0) for e in plan.scenes],
                overlaps=[float(e.get("overlap_after_s") or 0.0) for e in plan.scenes],
                cards=plan.cards,
                total_s=plan.timeline.total_s,
                settings=plan.finishing,
                music_gain_db=project.music_gain_db,
                user_music=music,
            )
            plan.warnings.extend(sound.notes)
            code, stderr = await _run(
                sound_design.mix_command(
                    ffmpeg_path,
                    speech,
                    sound,
                    audio,
                    total_s=plan.timeline.total_s,
                    voice_chain=bool(plan.finishing.get("voice_chain")),
                    graph_file=work / "sound-graph.txt",
                )
            )
            if code == 0 and audio.exists():
                sound_credits = sound.credits
                sound_events = len(sound.events)
            else:
                plan.warnings.append(
                    "The designed mix failed, so the plain narration mix was used "
                    f"({stderr.strip()[-160:]})."
                )
        if code != 0 or not audio.exists():
            code, stderr = await _run(
                mixing.mix_command(
                    ffmpeg_path,
                    speech,
                    audio,
                    music=music,
                    music_gain_db=project.music_gain_db,
                    total_s=plan.timeline.total_s,
                )
            )
        if code != 0 or not audio.exists():
            raise RenderError(f"The mix could not be built: {stderr.strip()[-300:]}")

        _, measure_err = await _run(
            mixing.loudness_command(ffmpeg_path, audio), timeout=PROBE_TIMEOUT_S
        )
        loudness = mixing.parse_loudness(measure_err)

    # --- subtitles --------------------------------------------------------
    subtitle_file: Path | None = None
    assert plan.sheet is not None
    if project.subtitles_enabled and plan.sheet.cues:
        record.stage_message = "Writing subtitles from the measured lines."
        record.progress = 0.6
        await session.commit()
        subtitle_file = subtitles.write(plan.sheet, work / "subtitles.srt")

    burn_filter: str | None = None
    caption_mode = plan.finishing.get("captions")
    if burn_subtitles and caption_mode == "off":
        burn_subtitles = False
    if (
        burn_subtitles
        and caption_mode == "words"
        and subtitle_file is not None
        and plan.timeline.placements
    ):
        record.stage_message = "Timing every word against the recording."
        record.progress = 0.65
        await session.commit()
        from app.services import captions

        ass_file, measured = await asyncio.to_thread(
            captions.build,
            ffmpeg_path,
            plan.timeline.placements,
            work / "captions.ass",
            width=plan.width,
            height=plan.height,
        )
        if not measured:
            plan.warnings.append(
                "Word timings were estimated from word length, not measured from the voice."
            )
        burn_filter = captions.ass_filter(ass_file, graphics.fonts_dir())

    # --- delivery ---------------------------------------------------------
    record.stage_message = "Encoding the final file."
    record.progress = 0.7
    await session.commit()

    final = work / f"{safe_filename(project.name, fallback='video')}.mp4"
    code, stderr = await _run(
        mux_command(
            ffmpeg_path,
            joined,
            audio,
            final,
            subtitle_path=subtitle_file if burn_subtitles else None,
            style=project.subtitle_style or None,
            burn_filter=burn_filter,
        )
    )
    if code != 0 or not final.exists():
        raise RenderError(f"The final encode failed: {stderr.strip()[-300:]}")

    # --- checking ---------------------------------------------------------
    record.status = RenderStatus.VALIDATING
    record.stage_message = "Checking the finished file."
    record.progress = 0.9
    await session.commit()

    checks = await validate_output(
        final,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        expected_duration_s=plan.planned_duration_s,
        expected_width=plan.width,
        expected_height=plan.height,
        expects_audio=audio is not None,
        loudness=loudness,
        dark_windows=[(c["start_s"], c["end_s"]) for c in plan.cards if c.get("dim")],
    )
    if plan.finishing.get("enabled"):
        checks["sound_credits"] = sound_credits
        checks["sound_events"] = sound_events
        checks["cards"] = plan.cards

    record.output_path = str(final)
    record.subtitle_path = str(subtitle_file) if subtitle_file else None
    record.duration_s = checks.get("measured_duration_s")
    record.file_size = final.stat().st_size if final.exists() else None
    record.validation = checks
    record.progress = 1.0
    record.completed_at = utcnow()

    failures = [str(item) for item in checks.get("failures", [])]
    if failures:
        # FFmpeg returned zero, but the file is wrong. That is a failure.
        record.status = RenderStatus.FAILED
        record.error = failures[0]
        record.stage_message = "The file was produced but did not pass its checks."
    else:
        record.status = RenderStatus.COMPLETED
        record.stage_message = "Finished and checked."

    await session.commit()

    # The report is written last so it reflects the verdict.
    report = work / "report.json"
    report.write_text(
        json.dumps(
            {
                "render_id": record.id,
                "project": project.name,
                "status": record.status,
                "measured_duration_s": record.duration_s,
                "generated_motion_s": plan.generated_motion_s,
                "camera_move_s": plan.camera_move_s,
                "real_footage_s": plan.footage_s,
                "honest_summary": summarise(plan, record.duration_s),
                "checks": checks,
                "warnings": plan.warnings,
                "subtitle_cues": plan.sheet.count,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    record.report_path = str(report)
    await session.commit()

    if record.status == RenderStatus.COMPLETED:
        await project_service.mark_stage(
            session,
            project.id,
            PipelineStage.RENDER,
            StageStatus.PARTIAL if plan.warnings else StageStatus.COMPLETED,
            message=summarise(plan, record.duration_s),
        )
        project.status = ProjectStatus.COMPLETED
        await session.commit()
    else:
        await project_service.mark_stage(
            session,
            project.id,
            PipelineStage.RENDER,
            StageStatus.FAILED,
            message=record.error or "The finished file did not pass its checks.",
        )

    outcome.status = record.status
    outcome.output_path = record.output_path or ""
    outcome.subtitle_path = record.subtitle_path or ""
    outcome.duration_s = record.duration_s
    outcome.checks = checks
    outcome.problems = failures + plan.warnings
    return outcome


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def validate_output(
    path: Path,
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
    expected_duration_s: float,
    expected_width: int,
    expected_height: int,
    expects_audio: bool,
    loudness: dict[str, float | None] | None = None,
    dark_windows: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Measure the delivered file against what was asked for.

    ``dark_windows`` are the stretches where a card was laid over a
    deliberately dimmed picture; black there is the design, not a hole.

    Returns the measurements and two lists: ``failures``, which make the
    render a failure, and ``notes``, which are worth knowing but do not.
    Anything that could not be measured goes in ``failures`` as unknown
    rather than being passed by default.
    """
    checks: dict[str, Any] = {
        "file": str(path),
        "expected_duration_s": round(expected_duration_s, 3),
        "failures": [],
        "notes": [],
    }
    failures: list[str] = checks["failures"]
    notes: list[str] = checks["notes"]
    dark_windows = list(dark_windows or [])

    if not path.exists():
        failures.append("The output file does not exist.")
        return checks
    checks["size_bytes"] = path.stat().st_size
    if checks["size_bytes"] == 0:
        failures.append("The output file is empty.")
        return checks

    info = await ffmpeg.probe(
        path, ffprobe_path=ffprobe_path or None, timeout=PROBE_TIMEOUT_S
    )
    if not info.ok:
        failures.append(
            f"The output could not be measured, so it is unknown whether it is "
            f"playable: {info.error}"
        )
        return checks

    checks["measured_duration_s"] = round(float(info.duration_s or 0.0), 3)
    checks["width"] = info.width
    checks["height"] = info.height
    checks["fps"] = info.fps
    checks["video_codec"] = info.video_codec
    checks["has_audio"] = info.has_audio
    checks["pixel_format"] = ((info.raw or {}).get("video_stream") or {}).get("pix_fmt")

    # Duration
    drift = abs(checks["measured_duration_s"] - expected_duration_s)
    checks["duration_drift_s"] = round(drift, 3)
    if expected_duration_s > 0 and drift > DURATION_TOLERANCE_S:
        failures.append(
            f"The finished video is {checks['measured_duration_s']:.2f}s but the edit "
            f"planned {expected_duration_s:.2f}s, a {drift:.2f}s difference."
        )

    # Frame
    if expected_width and info.width and info.width != expected_width:
        failures.append(
            f"The frame is {info.width}x{info.height}, not the "
            f"{expected_width}x{expected_height} asked for."
        )
    elif expected_height and info.height and info.height != expected_height:
        failures.append(
            f"The frame is {info.width}x{info.height}, not the "
            f"{expected_width}x{expected_height} asked for."
        )

    # Codec and pixel format: anything else will not play everywhere.
    if info.video_codec and info.video_codec != "h264":
        notes.append(
            f"The video is {info.video_codec} rather than h264, which not every "
            "player and platform accepts."
        )
    pix_fmt = checks.get("pixel_format")
    if pix_fmt and pix_fmt != "yuv420p":
        failures.append(
            f"The pixel format is {pix_fmt}, not yuv420p. Players that only handle "
            "yuv420p will show the wrong colours or nothing at all."
        )

    # Audio, and whether it drifts against the picture
    if expects_audio and not info.has_audio:
        failures.append("The video was supposed to have a soundtrack but has none.")
    if expects_audio and info.has_audio:
        video_s, audio_s = _stream_durations(info.raw or {})
        checks["video_stream_s"] = video_s
        checks["audio_stream_s"] = audio_s
        if video_s is not None and audio_s is not None:
            offset = abs(video_s - audio_s)
            checks["av_offset_s"] = round(offset, 3)
            if offset > SYNC_TOLERANCE_S:
                failures.append(
                    f"The soundtrack ends {offset:.2f}s away from the picture, so the "
                    "two drift apart."
                )
        else:
            notes.append(
                "The individual stream lengths were not reported, so audio and video "
                "could not be compared directly."
            )

    # Loudness, if it was measured upstream
    if loudness:
        checks["integrated_lufs"] = loudness.get("integrated_lufs")
        checks["lra_lu"] = loudness.get("lra_lu")
        on_target, message = mixing.loudness_verdict(loudness.get("integrated_lufs"))
        checks["loudness_note"] = message
        if not on_target:
            notes.append(message)

    # Black runs
    if ffmpeg_path:
        code, stderr = await _run(
            black_command(ffmpeg_path, path), timeout=PROBE_TIMEOUT_S
        )
        if code == 0:
            runs = parse_black_runs(stderr)
            checks["black_runs"] = runs
            intended = [
                run for run in runs
                if any(
                    start - 0.15 <= run.get("black_start", 0.0)
                    and run.get("black_end", run.get("black_start", 0.0)) <= end + 0.15
                    for start, end in dark_windows
                )
            ]
            if intended:
                notes.append(
                    f"{len(intended)} dark stretch(es) fall under a card laid over a "
                    "dimmed picture, which is intended, not a missing shot."
                )
                runs = [run for run in runs if run not in intended]
            if runs:
                worst = max(runs, key=lambda r: r.get("black_duration", 0.0))
                failures.append(
                    f"There are {len(runs)} black stretch(es) in the video, the longest "
                    f"{worst.get('black_duration', 0):.2f}s at "
                    f"{worst.get('black_start', 0):.2f}s. A shot is missing its picture."
                )
        else:
            notes.append(
                "The black-frame scan did not complete, so holes in the picture were "
                "not ruled out."
            )

    return checks


def _stream_durations(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    """Per-stream lengths, which is what reveals drift between them."""
    video_s: float | None = None
    audio_s: float | None = None
    for stream in raw.get("streams", []) or []:
        try:
            value = float(stream.get("duration"))
        except (TypeError, ValueError):
            continue
        if stream.get("codec_type") == "video" and video_s is None:
            video_s = round(value, 3)
        elif stream.get("codec_type") == "audio" and audio_s is None:
            audio_s = round(value, 3)
    return video_s, audio_s


def summarise(plan: RenderPlan, measured_s: float | None) -> str:
    """One sentence that cannot be quoted misleadingly.

    The generated and assembled figures always appear together, with the
    camera moves named as editing.
    """
    length = (
        f"{measured_s:.1f}s" if measured_s else f"{plan.planned_duration_s:.1f}s planned"
    )
    if plan.generated_motion_s > 0:
        motion = (
            f"{plan.generated_motion_s:.1f}s of it was animated by a model and "
            f"{plan.camera_move_s:.1f}s is the camera moving over still pictures, "
            "which is editing rather than animation"
        )
    elif plan.footage_s > 0:
        motion = (
            f"none of it was animated by a model; {plan.footage_s:.1f}s is real "
            f"library footage and {plan.camera_move_s:.1f}s is the camera moving over "
            "still pictures, which is editing rather than animation"
        )
    else:
        motion = (
            f"none of it was animated by a model; all {plan.camera_move_s:.1f}s is the "
            "camera moving over still pictures, which is editing rather than animation"
        )
    cues = plan.sheet.count if plan.sheet else 0
    return (
        f"The video is {length}, and {motion}. "
        f"{cues} subtitle cue(s) were timed from the measured narration."
    )


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------


async def list_renders(
    session: AsyncSession, project_id: int, limit: int = 20
) -> list[Render]:
    return list(
        (
            await session.execute(
                select(Render)
                .where(Render.project_id == project_id)
                .order_by(Render.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def screen_context(session: AsyncSession, project_id: int) -> dict[str, Any]:
    project = await project_service.get_project(session, project_id)
    plan = await plan_render(session, project_id)
    renders = await list_renders(session, project_id)
    report = await routing_service.motion_report(session, project_id)

    return {
        "project": project,
        "plan": plan,
        "renders": renders,
        "report": report,
        "summary": summarise(plan, None),
        "frame": f"{plan.width}x{plan.height}",
        "latest": renders[0] if renders else None,
        "publishable": next(
            (item for item in renders if item.status == RenderStatus.COMPLETED), None
        ),
    }
