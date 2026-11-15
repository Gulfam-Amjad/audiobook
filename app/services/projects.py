"""Projects: the unit of work for one finished video.

A project owns its editorial settings, its script versions, its scenes and
its renders. The pipeline reads settings from here rather than from the
global settings table, so two projects can target different lengths,
languages and voices without fighting each other.

Stage checkpoints also live here. They are what make an interrupted run
resumable: each stage records what it finished and what it still owes, so a
restart never re-spends credits on work that already succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    PIPELINE_ORDER,
    AudioStatus,
    ClipStatus,
    PipelineStage,
    ProjectStatus,
    RenderStatus,
    SceneStatus,
    StageStatus,
)
from app.models import (
    AudioSegment,
    ImageFolder,
    Project,
    Render,
    Scene,
    SceneClip,
    Script,
    StageCheckpoint,
    VoiceProfile,
)
from app.services.timeutil import utcnow

#: Format choices the renderer can actually honour.
ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16", "1:1")
RESOLUTIONS: tuple[str, ...] = ("480p", "540p", "720p", "1080p")
FRAME_RATES: tuple[int, ...] = (24, 25, 30)

#: Pixel dimensions per resolution label at 16:9. Other ratios are derived.
_HEIGHTS: dict[str, int] = {"480p": 480, "540p": 540, "720p": 720, "1080p": 1080}

MIN_DURATION_S = 15
MAX_DURATION_S = 1800
MIN_WPM = 80
MAX_WPM = 220


class ProjectError(Exception):
    """A project could not be created or changed, with the reason why."""


@dataclass(frozen=True)
class ProjectProgress:
    """Counts for one project, for the list and detail screens."""

    scenes_total: int = 0
    scenes_ready: int = 0
    audio_total: int = 0
    audio_ready: int = 0
    clips_selected: int = 0
    #: Seconds of clip that were genuinely generated, not camera-moved.
    generated_motion_s: float = 0.0
    #: Seconds of selected clip overall.
    assembled_s: float = 0.0
    measured_audio_s: float = 0.0
    latest_render_status: str | None = None
    latest_render_path: str | None = None


def frame_size(resolution: str, aspect_ratio: str) -> tuple[int, int]:
    """Pixel dimensions for a resolution label, rounded to even numbers.

    H.264 with yuv420p needs even dimensions on both axes, so the width is
    rounded rather than left to FFmpeg to reject later.
    """
    height = _HEIGHTS.get(resolution)
    if height is None:
        raise ProjectError(f"Unknown resolution '{resolution}'.")
    try:
        num, den = (int(part) for part in aspect_ratio.split(":", 1))
    except ValueError as exc:
        raise ProjectError(f"Unknown aspect ratio '{aspect_ratio}'.") from exc
    if num <= 0 or den <= 0:
        raise ProjectError(f"Unknown aspect ratio '{aspect_ratio}'.")
    width = round(height * num / den)
    return (width + (width % 2), height + (height % 2))


def estimate_speech_seconds(word_count: int, wpm: int) -> float:
    """Rough spoken length before any audio exists.

    Only ever used to size a draft script. Once Piper has run, every
    duration in the system comes from ffprobe instead.
    """
    if word_count <= 0 or wpm <= 0:
        return 0.0
    return round(word_count / wpm * 60.0, 2)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def _validate(
    session: AsyncSession, values: dict[str, Any], *, project_id: int | None = None
) -> dict[str, Any]:
    """Coerce and check submitted settings. Raises on the first problem."""
    clean: dict[str, Any] = {}

    if "name" in values:
        name = str(values["name"] or "").strip()
        if not name:
            raise ProjectError("A project needs a name.")
        if len(name) > 200:
            raise ProjectError("That name is too long (200 characters maximum).")
        stmt = select(Project.id).where(Project.name == name)
        if project_id is not None:
            stmt = stmt.where(Project.id != project_id)
        if (await session.execute(stmt)).scalar_one_or_none() is not None:
            raise ProjectError(f"A project called '{name}' already exists.")
        clean["name"] = name

    for field in ("niche", "audience", "story_style", "music_preference"):
        if field in values:
            clean[field] = str(values[field] or "").strip()[:200]

    if "language" in values:
        clean["language"] = (str(values["language"] or "en").strip() or "en")[:16]

    if "brief" in values:
        brief = str(values["brief"] or "").strip()
        clean["brief"] = brief or None

    if "notes" in values:
        notes = str(values["notes"] or "").strip()
        clean["notes"] = notes or None

    if "target_duration_s" in values:
        duration = _as_int(values["target_duration_s"], "Target duration")
        if not MIN_DURATION_S <= duration <= MAX_DURATION_S:
            raise ProjectError(
                f"Target duration must be between {MIN_DURATION_S} and "
                f"{MAX_DURATION_S} seconds."
            )
        clean["target_duration_s"] = duration

    if "aspect_ratio" in values:
        ratio = str(values["aspect_ratio"] or "").strip()
        if ratio not in ASPECT_RATIOS:
            raise ProjectError(f"Aspect ratio must be one of {', '.join(ASPECT_RATIOS)}.")
        clean["aspect_ratio"] = ratio

    if "resolution" in values:
        resolution = str(values["resolution"] or "").strip()
        if resolution not in RESOLUTIONS:
            raise ProjectError(f"Resolution must be one of {', '.join(RESOLUTIONS)}.")
        clean["resolution"] = resolution

    if "fps" in values:
        fps = _as_int(values["fps"], "Frame rate")
        if fps not in FRAME_RATES:
            raise ProjectError(
                f"Frame rate must be one of {', '.join(str(f) for f in FRAME_RATES)}."
            )
        clean["fps"] = fps

    if "speaking_rate_wpm" in values:
        wpm = _as_int(values["speaking_rate_wpm"], "Speaking rate")
        if not MIN_WPM <= wpm <= MAX_WPM:
            raise ProjectError(
                f"Speaking rate must be between {MIN_WPM} and {MAX_WPM} words per minute."
            )
        clean["speaking_rate_wpm"] = wpm

    if "max_generated_motion_s" in values:
        budget = _as_float(values["max_generated_motion_s"], "Generated motion budget")
        if budget < 0:
            raise ProjectError("The generated motion budget cannot be negative.")
        clean["max_generated_motion_s"] = budget

    if "music_gain_db" in values:
        gain = _as_float(values["music_gain_db"], "Music level")
        if not -60.0 <= gain <= 0.0:
            raise ProjectError("Music level must be between -60 dB and 0 dB.")
        clean["music_gain_db"] = gain

    if "subtitles_enabled" in values:
        clean["subtitles_enabled"] = _as_bool(values["subtitles_enabled"])

    if "stills_ai_generated" in values:
        # Three states, not two. An empty value means you have not said, and
        # the publishing disclosure then makes no claim about the stills
        # rather than guessing one.
        raw = str(values["stills_ai_generated"] or "").strip().lower()
        if raw in {"", "unknown", "none"}:
            clean["stills_ai_generated"] = None
        else:
            clean["stills_ai_generated"] = _as_bool(raw)

    if "music_path" in values:
        raw = str(values["music_path"] or "").strip().strip('"')
        if raw:
            path = Path(raw)
            if not path.is_file():
                raise ProjectError(f"No music file at {path}.")
            clean["music_path"] = str(path.resolve())
        else:
            clean["music_path"] = None

    if "narrator_voice_id" in values:
        voice_id = _as_optional_int(values["narrator_voice_id"])
        if voice_id is not None and await session.get(VoiceProfile, voice_id) is None:
            raise ProjectError("That voice profile no longer exists.")
        clean["narrator_voice_id"] = voice_id

    if "image_folder_id" in values:
        folder_id = _as_optional_int(values["image_folder_id"])
        if folder_id is not None and await session.get(ImageFolder, folder_id) is None:
            raise ProjectError("That image folder no longer exists.")
        clean["image_folder_id"] = folder_id

    if "subtitle_style" in values and isinstance(values["subtitle_style"], dict):
        clean["subtitle_style"] = values["subtitle_style"]

    return clean


def _as_int(value: Any, label: str) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise ProjectError(f"{label} must be a number.") from exc


def _as_float(value: Any, label: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ProjectError(f"{label} must be a number.") from exc


def _as_optional_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text or text in {"0", "none", "None"}:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError) as exc:
        raise ProjectError("Expected a numeric id.") from exc


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


DEFAULT_SUBTITLE_STYLE: dict[str, Any] = {
    "font": "Arial",
    "size": 42,
    "primary_colour": "&H00FFFFFF",
    "outline_colour": "&H00000000",
    "outline": 2,
    "shadow": 0,
    "margin_v": 60,
    "max_chars_per_line": 42,
    "max_lines": 2,
}


async def create_project(session: AsyncSession, **values: Any) -> Project:
    """Create a project and its full set of pending stage checkpoints."""
    values.setdefault("name", "")
    clean = await _validate(session, values)
    project = Project(**clean)
    if not project.subtitle_style:
        project.subtitle_style = dict(DEFAULT_SUBTITLE_STYLE)
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:  # pragma: no cover - guarded above
        await session.rollback()
        raise ProjectError("That project name is already taken.") from exc

    await ensure_checkpoints(session, project.id)
    await session.flush()
    return project


async def update_project(
    session: AsyncSession, project_id: int, values: dict[str, Any]
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectError("That project no longer exists.")
    clean = await _validate(session, values, project_id=project_id)
    for field, value in clean.items():
        setattr(project, field, value)
    await session.flush()
    return project


async def delete_project(session: AsyncSession, project_id: int) -> str:
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectError("That project no longer exists.")
    name = project.name
    await session.delete(project)
    await session.flush()
    return name


async def get_project(session: AsyncSession, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectError("That project no longer exists.")
    return project


async def list_projects(session: AsyncSession) -> list[Project]:
    stmt = (
        select(Project)
        .options(selectinload(Project.narrator_voice))
        .order_by(Project.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


async def progress(session: AsyncSession, project_id: int) -> ProjectProgress:
    """Measured counts only. Nothing here is projected or assumed."""
    scenes_total = await _scalar(
        session, select(func.count(Scene.id)).where(Scene.project_id == project_id)
    )
    scenes_ready = await _scalar(
        session,
        select(func.count(Scene.id)).where(
            Scene.project_id == project_id, Scene.status == SceneStatus.READY
        ),
    )
    audio_total = await _scalar(
        session,
        select(func.count(AudioSegment.id)).where(AudioSegment.project_id == project_id),
    )
    audio_ready = await _scalar(
        session,
        select(func.count(AudioSegment.id)).where(
            AudioSegment.project_id == project_id,
            AudioSegment.status == AudioStatus.READY,
        ),
    )
    measured = await _scalar(
        session,
        select(func.coalesce(func.sum(AudioSegment.duration_s), 0.0)).where(
            AudioSegment.project_id == project_id,
            AudioSegment.status == AudioStatus.READY,
        ),
    )

    selected = (
        await session.execute(
            select(SceneClip).where(
                SceneClip.project_id == project_id,
                SceneClip.selected.is_(True),
                SceneClip.status == ClipStatus.READY,
            )
        )
    ).scalars().all()

    generated = sum(c.duration_s or 0.0 for c in selected if c.is_generated_motion)
    assembled = sum(c.duration_s or 0.0 for c in selected)

    latest = (
        await session.execute(
            select(Render)
            .where(Render.project_id == project_id)
            .order_by(Render.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return ProjectProgress(
        scenes_total=scenes_total,
        scenes_ready=scenes_ready,
        audio_total=audio_total,
        audio_ready=audio_ready,
        clips_selected=len(selected),
        generated_motion_s=round(generated, 2),
        assembled_s=round(assembled, 2),
        measured_audio_s=round(float(measured or 0.0), 2),
        latest_render_status=latest.status if latest else None,
        latest_render_path=latest.output_path if latest else None,
    )


async def _scalar(session: AsyncSession, stmt: Any) -> Any:
    return (await session.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------
# Stage checkpoints
# ---------------------------------------------------------------------------


async def ensure_checkpoints(session: AsyncSession, project_id: int) -> int:
    """Create any missing stage rows. Safe to call repeatedly."""
    existing = {
        row.stage
        for row in (
            await session.execute(
                select(StageCheckpoint).where(StageCheckpoint.project_id == project_id)
            )
        ).scalars()
    }
    created = 0
    for stage in PIPELINE_ORDER:
        if stage.value in existing:
            continue
        session.add(
            StageCheckpoint(
                project_id=project_id, stage=stage.value, status=StageStatus.PENDING
            )
        )
        created += 1
    if created:
        await session.flush()
    return created


async def get_checkpoints(session: AsyncSession, project_id: int) -> list[StageCheckpoint]:
    """All stages in pipeline order, creating any that are missing."""
    await ensure_checkpoints(session, project_id)
    rows = (
        await session.execute(
            select(StageCheckpoint).where(StageCheckpoint.project_id == project_id)
        )
    ).scalars().all()
    order = {stage.value: index for index, stage in enumerate(PIPELINE_ORDER)}
    return sorted(rows, key=lambda row: order.get(row.stage, 99))


async def get_checkpoint(
    session: AsyncSession, project_id: int, stage: PipelineStage
) -> StageCheckpoint:
    await ensure_checkpoints(session, project_id)
    row = (
        await session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.project_id == project_id,
                StageCheckpoint.stage == stage.value,
            )
        )
    ).scalar_one()
    return row


async def mark_stage(
    session: AsyncSession,
    project_id: int,
    stage: PipelineStage,
    status: StageStatus,
    *,
    message: str | None = None,
    blocked_reason: str | None = None,
    payload: dict[str, Any] | None = None,
) -> StageCheckpoint:
    """Record where a stage got to.

    ``payload`` is merged rather than replaced so a stage can accumulate
    what it has finished across several partial runs.
    """
    row = await get_checkpoint(session, project_id, stage)
    row.status = status
    if message is not None:
        row.message = message
    row.blocked_reason = blocked_reason if status == StageStatus.BLOCKED else None
    if payload:
        merged = dict(row.payload or {})
        merged.update(payload)
        row.payload = merged
    if status == StageStatus.RUNNING:
        row.started_at = utcnow()
        row.attempt += 1
        row.finished_at = None
    elif status in {
        StageStatus.COMPLETED,
        StageStatus.FAILED,
        StageStatus.PARTIAL,
        StageStatus.SKIPPED,
    }:
        row.finished_at = utcnow()
    await session.flush()
    return row


async def next_stage(session: AsyncSession, project_id: int) -> PipelineStage | None:
    """The first stage that is not finished, or None when all are done.

    A stage counts as finished when it is completed or deliberately
    skipped. ``partial`` does not count: it means work remains.
    """
    rows = {row.stage: row for row in await get_checkpoints(session, project_id)}
    for stage in PIPELINE_ORDER:
        row = rows.get(stage.value)
        if row is None:
            return stage
        if row.status not in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
            return stage
    return None


async def refresh_status(session: AsyncSession, project_id: int) -> str:
    """Derive the project's headline status from its checkpoints."""
    project = await get_project(session, project_id)
    rows = {row.stage: row.status for row in await get_checkpoints(session, project_id)}

    if any(status == StageStatus.FAILED for status in rows.values()):
        project.status = ProjectStatus.FAILED
    elif rows.get(PipelineStage.RENDER.value) == StageStatus.COMPLETED:
        project.status = ProjectStatus.COMPLETED
    elif rows.get(PipelineStage.MIX.value) in {StageStatus.RUNNING, StageStatus.COMPLETED}:
        project.status = ProjectStatus.ASSEMBLING
    elif rows.get(PipelineStage.MOTION.value) in {
        StageStatus.RUNNING,
        StageStatus.PARTIAL,
        StageStatus.COMPLETED,
    }:
        project.status = ProjectStatus.ANIMATING
    elif rows.get(PipelineStage.SPEECH.value) == StageStatus.COMPLETED:
        project.status = ProjectStatus.VOICED
    elif rows.get(PipelineStage.SCRIPT.value) == StageStatus.COMPLETED:
        project.status = ProjectStatus.SCRIPTED
    else:
        project.status = ProjectStatus.DRAFT

    await session.flush()
    return project.status


# ---------------------------------------------------------------------------
# Screen context
# ---------------------------------------------------------------------------


async def current_script(session: AsyncSession, project_id: int) -> Script | None:
    """The newest script version for a project, approved or not."""
    return (
        await session.execute(
            select(Script)
            .where(Script.project_id == project_id)
            .order_by(Script.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_context(session: AsyncSession) -> dict[str, Any]:
    projects = await list_projects(session)
    rows = []
    for project in projects:
        rows.append(
            {
                "project": project,
                "progress": await progress(session, project.id),
                "next_stage": await next_stage(session, project.id),
            }
        )
    folders = (
        await session.execute(select(ImageFolder).order_by(ImageFolder.path))
    ).scalars().all()
    voices = (
        await session.execute(select(VoiceProfile).order_by(VoiceProfile.name))
    ).scalars().all()
    return {
        "rows": rows,
        "folders": list(folders),
        "voices": list(voices),
        "aspect_ratios": ASPECT_RATIOS,
        "resolutions": RESOLUTIONS,
        "frame_rates": FRAME_RATES,
    }


async def detail_context(session: AsyncSession, project_id: int) -> dict[str, Any]:
    project = await get_project(session, project_id)
    script = await current_script(session, project_id)
    scenes: list[Scene] = []
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
    folders = (
        await session.execute(select(ImageFolder).order_by(ImageFolder.path))
    ).scalars().all()
    voices = (
        await session.execute(select(VoiceProfile).order_by(VoiceProfile.name))
    ).scalars().all()
    renders = (
        await session.execute(
            select(Render)
            .where(Render.project_id == project_id)
            .order_by(Render.created_at.desc())
            .limit(10)
        )
    ).scalars().all()

    width, height = frame_size(project.resolution, project.aspect_ratio)
    return {
        "project": project,
        "script": script,
        "scenes": scenes,
        "progress": await progress(session, project_id),
        "checkpoints": await get_checkpoints(session, project_id),
        "next_stage": await next_stage(session, project_id),
        "renders": list(renders),
        "folders": list(folders),
        "voices": list(voices),
        "aspect_ratios": ASPECT_RATIOS,
        "resolutions": RESOLUTIONS,
        "frame_rates": FRAME_RATES,
        "frame_width": width,
        "frame_height": height,
        "render_statuses": RenderStatus,
    }
