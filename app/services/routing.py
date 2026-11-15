"""Deciding how each shot gets its picture, and being honest about it.

The router answers one question per scene: given what is actually
available right now, how will this shot be produced? It then records both
the choice and the ordered fallbacks, so a scene whose first method fails
can be retried without re-planning the whole video.

Two rules run through the whole module.

**Generated motion is rationed.** Free image-to-video capacity is small
and the project carries a hard ceiling in ``max_generated_motion_s``.
Scenes that asked for generated motion get it in script order until that
ceiling is reached; the rest fall back to a camera move. The budget is
spent on the shots the writer put first, which are the ones it chose to
open on.

**A camera move is not animation.** A push over a still is editing. It is
recorded with ``is_generated_motion = False``, reported under assembled
length, and described in words that do not imply a model animated
anything. :func:`motion_report` always returns both numbers together so
they cannot be quoted separately by accident.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    GENERATED_MOTION_METHODS,
    AnimationMethod,
    ClipStatus,
    PipelineStage,
    SceneStatus,
    StageStatus,
)
from app.models import Project, Scene, SceneClip
from app.services import camera, ffmpeg, production
from app.services import projects as project_service
from app.services import scripts as script_service
from app.services.paths import safe_filename
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)


class RoutingError(Exception):
    """Something the user must fix before shots can be planned."""


#: A shot shorter than this reads as a glitch rather than a cut.
MIN_SHOT_S = 1.5
#: Breathing room added around measured narration so a line is not clipped
#: by the cut. Applied to the shot, never to the audio.
SHOT_PADDING_S = 0.6


@dataclass
class MethodStatus:
    """Whether one production method can be used at this moment, and why not."""

    method: str
    available: bool
    reason: str
    #: True when using it means the user has to go and do something.
    needs_user: bool = False
    #: True when using it could cost money.
    costs_money: bool = False
    generates_motion: bool = False


async def method_availability(session: AsyncSession) -> dict[str, MethodStatus]:
    """What each method can do right now. Nothing here is assumed.

    Paid APIs report unavailable unless spending is actually armed, which
    is the backend half of the rule that a paid service is never used
    without explicit approval. A UI that hid the button would not be
    enough on its own.
    """
    from app.services import spending

    statuses: dict[str, MethodStatus] = {}

    spend = await spending.summary(session)
    if not spend.paid_enabled:
        reason = "Paid generation is switched off, so no paid API will be called."
    elif spend.cap_usd <= 0:
        reason = "Paid generation is on but the monthly cap is zero, so nothing may be spent."
    else:
        reason = "Paid generation is armed. A scene routed here may cost money."
    statuses[AnimationMethod.API_I2V] = MethodStatus(
        method=AnimationMethod.API_I2V,
        available=bool(spend.paid_enabled and spend.cap_usd > 0),
        reason=reason,
        costs_money=True,
        generates_motion=True,
    )

    statuses[AnimationMethod.MANUAL_I2V] = MethodStatus(
        method=AnimationMethod.MANUAL_I2V,
        available=True,
        reason=(
            "Free websites, driven by hand. A worksheet is prepared and the "
            "finished clip is dropped into the inbox."
        ),
        needs_user=True,
        generates_motion=True,
    )

    # Borrowed GPU time needs credentials the user supplies. Without them
    # this reports unavailable rather than routing shots at nothing.
    from app.config import get_settings

    settings = get_settings()
    kaggle_ready = bool(
        getattr(settings, "kaggle_username", "") and getattr(settings, "kaggle_key", "")
    )
    remote_reason = (
        "Borrowed GPU time is configured."
        if kaggle_ready
        else "No Kaggle credentials, so there is no borrowed GPU time to use."
    )
    for method in (AnimationMethod.REMOTE_I2V, AnimationMethod.LIPSYNC):
        statuses[method] = MethodStatus(
            method=method,
            available=kaggle_ready,
            reason=remote_reason,
            generates_motion=True,
        )

    ff = await ffmpeg.detect()
    local_reason = (
        "FFmpeg is available." if ff.available else "FFmpeg is not installed."
    )
    for method in (AnimationMethod.CAMERA_MOVE, AnimationMethod.STILL):
        statuses[method] = MethodStatus(
            method=method,
            available=ff.available,
            reason=local_reason,
            generates_motion=False,
        )
    return statuses


@dataclass
class ShotDecision:
    """What the router decided for one scene, and why."""

    scene_id: int
    scene_key: str
    duration_s: float
    #: Whether ``duration_s`` came from measured audio or the script's plan.
    duration_measured: bool
    requested: str
    assigned: str
    fallbacks: list[str]
    camera_move: str
    reason: str

    @property
    def is_generated_motion(self) -> bool:
        return self.assigned in GENERATED_MOTION_METHODS


@dataclass
class RoutingPlan:
    decisions: list[ShotDecision] = field(default_factory=list)
    motion_budget_s: float = 0.0
    motion_assigned_s: float = 0.0
    #: Total of every shot, generated or edited.
    assembled_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def generated_count(self) -> int:
        return sum(1 for d in self.decisions if d.is_generated_motion)


def shot_duration(scene: Scene) -> tuple[float, bool]:
    """How long this shot has to be, and whether that is a measurement.

    Measured narration wins over the script's guess every time: the
    picture has to last as long as the words actually take.
    """
    # The finishing pass sets ``extra_s``: a held beat after the words, plus
    # the overlap the transition out of this shot will take back.
    extra = float((scene.direction or {}).get("extra_s") or 0.0)
    if scene.measured_audio_s is not None and scene.measured_audio_s > 0:
        return max(MIN_SHOT_S, scene.measured_audio_s + SHOT_PADDING_S + extra), True
    return max(MIN_SHOT_S, float(scene.target_duration_s or MIN_SHOT_S)) + extra, False


def _fallback_chain(statuses: dict[str, MethodStatus], requested: str) -> list[str]:
    """Methods to try after the assigned one, best first.

    Camera move is always last and always present, which is what
    guarantees every scene ends up with a picture of the right length.
    """
    order = [
        AnimationMethod.REMOTE_I2V,
        AnimationMethod.API_I2V,
        AnimationMethod.MANUAL_I2V,
        AnimationMethod.CAMERA_MOVE,
    ]
    chain = [
        method
        for method in order
        if method != requested and statuses.get(method, None) and statuses[method].available
    ]
    if AnimationMethod.CAMERA_MOVE not in chain:
        chain.append(AnimationMethod.CAMERA_MOVE)
    return chain


async def plan_shots(session: AsyncSession, project_id: int) -> RoutingPlan:
    """Choose a method for every scene, within the project's motion budget."""
    project = await project_service.get_project(session, project_id)
    script = await script_service.approved_script(session, project_id)
    if script is None:
        raise RoutingError(
            "No approved script. Approve a script version before planning shots."
        )

    scenes = list(
        (
            await session.execute(
                select(Scene)
                .where(Scene.script_id == script.id)
                .order_by(Scene.order_index)
            )
        ).scalars().all()
    )
    if not scenes:
        raise RoutingError("The approved script has no shots in it.")

    statuses = await method_availability(session)
    plan = RoutingPlan(motion_budget_s=float(project.max_generated_motion_s or 0.0))

    if not statuses[AnimationMethod.CAMERA_MOVE].available:
        plan.notes.append(
            "FFmpeg is not installed, so no shot can be rendered locally. "
            "Every scene would depend on an outside service."
        )

    finishing = production.settings(project)
    if finishing:
        production.apply_holds(
            scenes,
            project.fps,
            with_transitions=bool(finishing.get("transitions")),
            end_card=bool(finishing.get("end_card")) and bool(finishing.get("cards")),
        )

    for scene in scenes:
        duration, measured = shot_duration(scene)
        requested = scene.preferred_method or AnimationMethod.CAMERA_MOVE
        status = statuses.get(requested)

        if status is None:
            assigned = AnimationMethod.CAMERA_MOVE
            reason = f"'{requested}' is not a method this app knows, so a camera move was used."
        elif not status.available:
            assigned = AnimationMethod.CAMERA_MOVE
            reason = f"{requested} is unavailable: {status.reason}"
        elif requested in GENERATED_MOTION_METHODS:
            remaining = plan.motion_budget_s - plan.motion_assigned_s
            if duration <= remaining:
                assigned = requested
                plan.motion_assigned_s += duration
                reason = status.reason
            else:
                assigned = AnimationMethod.CAMERA_MOVE
                reason = (
                    f"The {plan.motion_budget_s:.0f}s motion budget is spent "
                    f"({remaining:.1f}s left, this shot needs {duration:.1f}s), "
                    "so this is a camera move over the still."
                )
        else:
            assigned = requested
            reason = status.reason

        move = camera.move_for(scene.camera)
        if finishing:
            directed = str((scene.direction or {}).get("camera_move") or "")
            if directed in camera.MOVES_BY_KEY:
                move = directed
            elif not (scene.camera or "").strip():
                move = production.VARIETY[scene.order_index % len(production.VARIETY)]
        if assigned == AnimationMethod.STILL:
            move = "hold"

        scene.assigned_method = assigned
        scene.fallback_methods = _fallback_chain(statuses, assigned)
        scene.validation = {
            **(scene.validation or {}),
            "routing_reason": reason,
            "camera_move": move,
            "duration_measured": measured,
        }
        if scene.status in (SceneStatus.PLANNED, SceneStatus.VOICED):
            scene.status = SceneStatus.ROUTED

        plan.assembled_s += duration
        plan.decisions.append(
            ShotDecision(
                scene_id=scene.id,
                scene_key=scene.scene_key or f"{scene.order_index + 1}",
                duration_s=duration,
                duration_measured=measured,
                requested=requested,
                assigned=assigned,
                fallbacks=list(scene.fallback_methods),
                camera_move=move,
                reason=reason,
            )
        )

    unmeasured = sum(1 for d in plan.decisions if not d.duration_measured)
    if unmeasured:
        plan.notes.append(
            f"{unmeasured} shot(s) have no recorded narration yet, so their length "
            "comes from the script's plan rather than from measured audio."
        )
    if plan.generated_count == 0:
        plan.notes.append(
            "No shot was routed to generated motion. Every shot in this cut is a "
            "camera move over a still, which is editing, not animation."
        )

    await project_service.mark_stage(
        session,
        project_id,
        PipelineStage.ROUTING,
        StageStatus.COMPLETED,
        message=(
            f"{len(plan.decisions)} shot(s), {plan.assembled_s:.1f}s assembled, "
            f"{plan.motion_assigned_s:.1f}s routed to generated motion."
        ),
        payload={
            "shots": len(plan.decisions),
            "assembled_s": round(plan.assembled_s, 2),
            "generated_motion_planned_s": round(plan.motion_assigned_s, 2),
        },
    )
    await session.flush()
    return plan


# --- Rendering the local shots -------------------------------------------


def clips_dir(project: Project) -> Path:
    from app.config import get_settings

    folder = safe_filename(f"{project.id:04d}-{project.name}", fallback=f"{project.id:04d}")
    return get_settings().data_root / "clips" / folder


@dataclass
class ClipRun:
    """The measured outcome of a rendering pass."""

    rendered: int = 0
    reused: int = 0
    failed: int = 0
    skipped: int = 0
    #: Seconds of finished picture, from probing the files.
    assembled_s: float = 0.0
    #: Of that, how much a model actually animated. Zero from this module.
    generated_motion_s: float = 0.0
    errors: list[str] = field(default_factory=list)


async def render_local_shots(
    session: AsyncSession, project_id: int, *, force: bool = False
) -> ClipRun:
    """Render every scene routed to a camera move or a held frame.

    Scenes waiting on an outside service are skipped, not faked. A scene
    with no still is reported as a problem rather than filled with black.
    """
    project = await project_service.get_project(session, project_id)
    script = await script_service.approved_script(session, project_id)
    if script is None:
        raise RoutingError("No approved script.")

    status = await ffmpeg.detect()
    run = ClipRun()
    if not status.available or not status.ffmpeg_path:
        message = status.message or "FFmpeg is not installed."
        await project_service.mark_stage(
            session, project_id, PipelineStage.MOTION, StageStatus.BLOCKED,
            message=message, blocked_reason=message,
        )
        run.errors.append(message)
        return run

    await project_service.mark_stage(
        session, project_id, PipelineStage.MOTION, StageStatus.RUNNING
    )

    scenes = list(
        (
            await session.execute(
                select(Scene)
                .options(selectinload(Scene.image))
                .where(Scene.script_id == script.id)
                .order_by(Scene.order_index)
            )
        ).scalars().all()
    )
    width, height = project_service.frame_size(project.resolution, project.aspect_ratio)
    destination_dir = clips_dir(project)

    for scene in scenes:
        method = scene.assigned_method or AnimationMethod.CAMERA_MOVE
        if method not in camera.PRODUCES:
            run.skipped += 1
            continue
        await _render_one(
            session, project, scene, status.ffprobe_path, status.ffmpeg_path,
            destination_dir, width, height, run, force=force,
        )
        await session.commit()

    totals = await motion_report(session, project_id)
    run.assembled_s = totals["assembled_s"]
    run.generated_motion_s = totals["generated_motion_s"]

    outstanding = totals["scenes_without_clip"]
    if run.failed or outstanding:
        stage_status = StageStatus.PARTIAL if totals["clips_ready"] else StageStatus.FAILED
        message = (
            f"{totals['clips_ready']} shot(s) rendered, {run.failed} failed, "
            f"{outstanding} still without a clip."
        )
    else:
        stage_status = StageStatus.COMPLETED
        message = (
            f"{totals['clips_ready']} shot(s), {totals['assembled_s']:.1f}s assembled, "
            f"{totals['generated_motion_s']:.1f}s of that generated motion."
        )
    await project_service.mark_stage(
        session, project_id, PipelineStage.MOTION, stage_status,
        message=message,
        payload={
            "clips_ready": totals["clips_ready"],
            "assembled_s": round(totals["assembled_s"], 2),
            "generated_motion_s": round(totals["generated_motion_s"], 2),
        },
    )
    await project_service.refresh_status(session, project_id)
    await session.commit()
    return run


async def _render_one(
    session: AsyncSession,
    project: Project,
    scene: Scene,
    ffprobe_path: str | None,
    ffmpeg_path: str,
    destination_dir: Path,
    width: int,
    height: int,
    run: ClipRun,
    *,
    force: bool,
) -> None:
    duration, _ = shot_duration(scene)
    move = (scene.validation or {}).get("camera_move") or camera.move_for(scene.camera)
    if scene.assigned_method == AnimationMethod.STILL:
        move = "hold"

    existing = await _selected_clip(session, scene.id)
    if not force and existing is not None and _still_current(existing, scene, move, duration):
        run.reused += 1
        return

    if scene.image is None or not scene.image.path:
        run.failed += 1
        run.errors.append(
            f"Shot {scene.scene_key or scene.order_index + 1} has no still chosen, "
            "so there is nothing to move the camera over."
        )
        _record_failure(session, scene, project, move, "No still is attached to this shot.")
        return

    source = Path(scene.image.path)
    destination = destination_dir / f"s{scene.order_index:03d}-{scene.scene_key or 'shot'}.mp4"
    focus: tuple[float, float] | None = None
    if production.settings(project) and move in camera.FOCUS_MOVES:
        from app.services import faces

        focus = await asyncio.to_thread(faces.find_focus, source)
    result = await camera.render(
        ffmpeg_path, source, destination,
        move=move, width=width, height=height, fps=project.fps, duration_s=duration,
        focus=focus,
    )
    if not result.ok:
        run.failed += 1
        run.errors.append(f"Shot {scene.scene_key}: {result.message}")
        _record_failure(session, scene, project, move, result.message)
        return

    info = await ffmpeg.probe(destination, ffprobe_path)
    if info.error or info.duration_s is None:
        run.failed += 1
        problem = info.error or "the clip could not be measured."
        run.errors.append(f"Shot {scene.scene_key}: {problem}")
        _record_failure(session, scene, project, move, problem)
        return

    await _deselect_others(session, scene.id)
    clip = SceneClip(
        scene_id=scene.id,
        project_id=project.id,
        method=scene.assigned_method or AnimationMethod.CAMERA_MOVE,
        provider_key="ffmpeg",
        source_image_id=scene.image_id,
        file_path=str(destination),
        duration_s=info.duration_s,
        width=info.width,
        height=info.height,
        fps=info.fps,
        # The whole point of this module: a camera move never counts as
        # motion a model generated.
        is_generated_motion=False,
        selected=True,
        status=ClipStatus.READY,
        completed_at=utcnow(),
        validation={
            "camera_move": move,
            "description": camera.describe(move),
            "focus": list(focus) if focus else None,
            "requested_duration_s": round(duration, 3),
            "measured_duration_s": round(info.duration_s, 3),
        },
    )
    session.add(clip)
    scene.status = SceneStatus.READY
    run.rendered += 1
    await session.flush()


def _still_current(
    clip: SceneClip, scene: Scene, move: str, duration: float
) -> bool:
    """Whether an existing clip already is what this shot now asks for.

    The comparison is against what was *requested* when the clip was made,
    not against its measured length: an encoder lands a frame either side
    of the target, and that difference is not a reason to re-render.
    """
    if clip.status != ClipStatus.READY or clip.method != scene.assigned_method:
        return False
    if not clip.file_path or not Path(clip.file_path).exists():
        return False
    validation = clip.validation or {}
    if validation.get("camera_move") != move:
        return False
    requested = validation.get("requested_duration_s")
    return requested is not None and abs(float(requested) - duration) < 0.05


def _record_failure(
    session: AsyncSession, scene: Scene, project: Project, move: str, problem: str
) -> None:
    session.add(
        SceneClip(
            scene_id=scene.id,
            project_id=project.id,
            method=scene.assigned_method or AnimationMethod.CAMERA_MOVE,
            provider_key="ffmpeg",
            is_generated_motion=False,
            selected=False,
            status=ClipStatus.FAILED,
            error=problem,
            validation={"camera_move": move},
        )
    )
    scene.status = SceneStatus.FAILED


async def _selected_clip(session: AsyncSession, scene_id: int) -> SceneClip | None:
    return (
        await session.execute(
            select(SceneClip)
            .where(SceneClip.scene_id == scene_id, SceneClip.selected.is_(True))
            .order_by(SceneClip.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _deselect_others(session: AsyncSession, scene_id: int) -> None:
    rows = (
        await session.execute(
            select(SceneClip).where(
                SceneClip.scene_id == scene_id, SceneClip.selected.is_(True)
            )
        )
    ).scalars().all()
    for row in rows:
        row.selected = False
        if row.status == ClipStatus.READY:
            row.status = ClipStatus.REPLACED


async def select_clip(session: AsyncSession, clip_id: int) -> SceneClip:
    """Make one clip the chosen take for its scene, replacing another."""
    clip = await session.get(SceneClip, clip_id)
    if clip is None:
        raise RoutingError(f"Clip {clip_id} does not exist.")
    if clip.status != ClipStatus.READY:
        raise RoutingError("Only a finished clip can be chosen for the edit.")
    await _deselect_others(session, clip.scene_id)
    clip.selected = True
    clip.status = ClipStatus.READY
    await session.flush()
    return clip


# --- Accounting -----------------------------------------------------------


async def motion_report(session: AsyncSession, project_id: int) -> dict[str, Any]:
    """The two numbers, always together.

    ``generated_motion_s`` counts only clips a model actually animated.
    ``assembled_s`` is the length of the cut. A camera move adds to the
    second and never to the first, which is the distinction the whole
    pipeline is built to keep straight.
    """
    script = await script_service.approved_script(session, project_id)
    # Scoped to the approved script's own shots. Rewriting a script leaves
    # the previous version's clips behind, and counting those made a
    # sixty-second cut report as a hundred and fifty.
    scene_ids = (
        list(
            (
                await session.execute(select(Scene.id).where(Scene.script_id == script.id))
            ).scalars().all()
        )
        if script is not None
        else []
    )
    scene_count = len(scene_ids)

    clips: list[SceneClip] = []
    if scene_ids:
        clips = list(
            (
                await session.execute(
                    select(SceneClip).where(
                        SceneClip.project_id == project_id,
                        SceneClip.scene_id.in_(scene_ids),
                        SceneClip.selected.is_(True),
                        SceneClip.status == ClipStatus.READY,
                    )
                )
            ).scalars().all()
        )

    generated = [c for c in clips if c.is_generated_motion]
    footage = [
        c for c in clips
        if not c.is_generated_motion and c.method == AnimationMethod.STOCK_FOOTAGE
    ]
    edited = [
        c for c in clips
        if not c.is_generated_motion and c.method != AnimationMethod.STOCK_FOOTAGE
    ]
    assembled_s = sum(c.duration_s or 0.0 for c in clips)
    generated_s = sum(c.duration_s or 0.0 for c in generated)
    return {
        "clips_ready": len(clips),
        "scene_count": scene_count,
        "scenes_without_clip": max(0, scene_count - len(clips)),
        "assembled_s": round(assembled_s, 3),
        "generated_motion_s": round(generated_s, 3),
        "camera_move_s": round(sum(c.duration_s or 0.0 for c in edited), 3),
        "real_footage_s": round(sum(c.duration_s or 0.0 for c in footage), 3),
        "generated_clips": len(generated),
        "camera_move_clips": len(edited),
        "real_footage_clips": len(footage),
        "generated_share": round(generated_s / assembled_s, 4) if assembled_s else 0.0,
    }


def honest_summary(report: dict[str, Any]) -> str:
    """One sentence that cannot be misread as a claim of animation."""
    assembled = report["assembled_s"]
    generated = report["generated_motion_s"]
    footage = report.get("real_footage_s", 0.0)
    if assembled <= 0:
        return "Nothing has been rendered yet."
    if generated <= 0 and footage > 0:
        return (
            f"{assembled:.1f}s assembled, none of it generated motion. "
            f"{footage:.1f}s is real library footage and the remaining "
            f"{assembled - footage:.1f}s is camera moves over stills."
        )
    if generated <= 0:
        return (
            f"{assembled:.1f}s assembled, none of it generated motion. "
            "Every shot is a camera move over a still image."
        )
    return (
        f"{assembled:.1f}s assembled, of which {generated:.1f}s is motion a model "
        f"generated. The remaining {assembled - generated:.1f}s is camera moves over stills."
    )


# --- Screen context -------------------------------------------------------


async def screen_context(session: AsyncSession, project_id: int) -> dict[str, Any]:
    project = await project_service.get_project(session, project_id)
    script = await script_service.approved_script(session, project_id)
    statuses = await method_availability(session)
    report = await motion_report(session, project_id)

    scenes: list[Scene] = []
    clips: dict[int, list[SceneClip]] = {}
    if script is not None:
        scenes = list(
            (
                await session.execute(
                    select(Scene)
                    .options(selectinload(Scene.image))
                    .where(Scene.script_id == script.id)
                    .order_by(Scene.order_index)
                )
            ).scalars().all()
        )
        for row in (
            await session.execute(
                select(SceneClip)
                .where(SceneClip.project_id == project_id)
                .order_by(SceneClip.id)
            )
        ).scalars().all():
            clips.setdefault(row.scene_id, []).append(row)

    width, height = project_service.frame_size(project.resolution, project.aspect_ratio)
    return {
        "project": project,
        "script": script,
        "scenes": scenes,
        "clips": clips,
        "methods": list(statuses.values()),
        "report": report,
        "summary": honest_summary(report),
        "routing": await project_service.get_checkpoint(
            session, project_id, PipelineStage.ROUTING
        ),
        "motion": await project_service.get_checkpoint(
            session, project_id, PipelineStage.MOTION
        ),
        "frame": f"{width}x{height}",
        "moves": camera.MOVES,
        "durations": {
            scene.id: shot_duration(scene) for scene in scenes
        },
    }
