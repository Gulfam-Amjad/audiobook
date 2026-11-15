"""Script generation: ask a writer, validate hard, then persist.

Order matters here. A plan is validated *before* any row is written and
long before any scene reaches the motion stage, because that is the last
point at which rejecting it is free.

A failed attempt is still recorded as a Script row with ``is_valid`` false
and its problems attached. Throwing it away would lose the one thing that
makes the next attempt better: the list of what was wrong.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import ErrorClass, ImageStatus, PipelineStage, SceneStatus, StageStatus
from app.models import Character, Image, Project, Scene, Script
from app.providers import script as script_registry
from app.providers.script.base import (
    ImageHint,
    ScriptProviderError,
    ScriptRequest,
    ScriptResult,
)
from app.providers.script.manual_paste import ManualPasteScriptProvider
from app.services import projects as project_service
from app.services import scene_plan

log = logging.getLogger(__name__)

#: How many stills to offer the writer. The whole library would bloat the
#: prompt and, on a small free quota, waste the request.
MAX_IMAGE_HINTS = 60

#: Default shot length when the project does not imply something else.
DEFAULT_SCENE_TARGET_S = 8.0


class ScriptError(Exception):
    """Something went wrong generating or storing a script."""


# ---------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------


async def available_images(session: AsyncSession, project: Project) -> list[Image]:
    """Usable stills for a project, newest library entries last.

    Missing files and duplicates are excluded: offering the writer an image
    that is not on disk produces a plan that cannot be rendered.
    """
    stmt = select(Image).where(
        Image.status != ImageStatus.MISSING,
        Image.duplicate_of_id.is_(None),
    )
    if project.image_folder_id is not None:
        stmt = stmt.where(Image.folder_id == project.image_folder_id)
    stmt = stmt.order_by(Image.filename).limit(MAX_IMAGE_HINTS)
    return list((await session.execute(stmt)).scalars().all())


async def build_request(
    session: AsyncSession, project: Project, *, previous_problems: tuple[str, ...] = ()
) -> ScriptRequest:
    images = await available_images(session, project)
    hints = tuple(
        ImageHint(
            image_id=image.id,
            filename=image.filename,
            width=image.width,
            height=image.height,
            note=(image.notes or "").strip()[:160],
        )
        for image in images
    )

    characters = (
        await session.execute(
            select(Character.name).where(Character.project_id == project.id)
        )
    ).scalars().all()

    scene_target = DEFAULT_SCENE_TARGET_S
    max_scenes = max(
        scene_plan.MIN_SCENES,
        min(scene_plan.MAX_SCENES, int(project.target_duration_s / scene_target) + 4),
    )

    # Written to the rate the narrator was actually measured at, where one
    # is known. Writing to an assumed rate is what made a sixty-second
    # brief come out as ninety-four seconds of finished video.
    from app.services import speech as speech_service

    rate_wpm, rate_note = await speech_service.writing_rate_wpm(session, project)
    log.info("Writing to %s wpm. %s", rate_wpm, rate_note)

    return ScriptRequest(
        project_name=project.name,
        niche=project.niche,
        audience=project.audience,
        story_style=project.story_style,
        language=project.language,
        brief=project.brief or "",
        target_duration_s=project.target_duration_s,
        speaking_rate_wpm=rate_wpm,
        scene_target_s=scene_target,
        max_scenes=max_scenes,
        motion_budget_s=project.max_generated_motion_s,
        available_images=hints,
        character_names=tuple(characters),
        previous_problems=previous_problems,
    )


async def prompt_for(session: AsyncSession, project_id: int) -> str:
    """The exact prompt, for the paste-it-yourself flow."""
    project = await project_service.get_project(session, project_id)
    request = await build_request(session, project)
    return scene_plan.build_prompt(request, motion_budget_s=project.max_generated_motion_s)


# ---------------------------------------------------------------------------
# Persisting
# ---------------------------------------------------------------------------


async def _next_version(session: AsyncSession, project_id: int) -> int:
    current = (
        await session.execute(
            select(func.max(Script.version)).where(Script.project_id == project_id)
        )
    ).scalar_one_or_none()
    return int(current or 0) + 1


def _resolve_image(
    hint: str, images: list[Image], used: set[int]
) -> Image | None:
    """Match a writer's filename to a real row, preferring an unused one.

    Reusing one still for every shot is the fastest way to make a video
    look automated, so an exact match that has already been used loses to
    an unused one only when the writer gave no hint at all.
    """
    cleaned = (hint or "").strip().lower()
    if cleaned:
        for image in images:
            if image.filename.lower() == cleaned:
                return image
        # A writer sometimes returns a path or a near-miss on the extension.
        for image in images:
            if cleaned in image.filename.lower() or image.filename.lower() in cleaned:
                return image
    for image in images:
        if image.id not in used:
            return image
    return images[0] if images else None


async def store(
    session: AsyncSession,
    project: Project,
    result: ScriptResult,
    *,
    provider_key: str,
) -> Script:
    """Validate a result and write it, valid or not, as a new version."""
    images = await available_images(session, project)
    hints = tuple(
        ImageHint(image_id=i.id, filename=i.filename, width=i.width, height=i.height)
        for i in images
    )

    # Imported here rather than at module scope: the speech service needs
    # this one to find a project's approved script.
    from app.services import speech as speech_service

    report = scene_plan.validate(
        {
            "title": result.title,
            "logline": result.logline,
            "synopsis": result.synopsis,
            "scenes": result.scenes,
        },
        target_duration_s=project.target_duration_s,
        # Judged against the measured rate too, so a plan is not approved
        # as fitting when it only fits at a speed this voice never reaches.
        speaking_rate_wpm=(
            await speech_service.writing_rate_wpm(session, project)
        )[0],
        motion_budget_s=project.max_generated_motion_s,
        known_images=hints,
    )
    from app.services import directing

    film_format = str((result.raw or {}).get("format") or "documentary").strip().lower()
    report.warnings.extend(
        directing.review_plan(
            {
                "title": result.title,
                "format": film_format,
                "scenes": result.scenes,
            }
        )
    )
    directions = directing.fill_defaults(list(result.scenes), film_format=film_format)

    script = Script(
        project_id=project.id,
        version=await _next_version(session, project.id),
        title=result.title[:300],
        logline=result.logline or None,
        synopsis=result.synopsis or None,
        raw_json=result.raw,
        provider_key=provider_key,
        model_id=result.model_id or None,
        word_count=report.word_count,
        estimated_duration_s=report.estimated_duration_s,
        validation=report.as_dict(),
        is_valid=report.ok,
        approved=False,
    )
    session.add(script)
    await session.flush()

    # Scenes are written even for an invalid plan so the user can see what
    # was produced and judge the problems for themselves.
    used_images: set[int] = set()
    for index, raw_scene in enumerate(result.scenes):
        image = _resolve_image(str(raw_scene.get("image_hint") or ""), images, used_images)
        if image is not None:
            used_images.add(image.id)

        dialogue = [
            {"character": str(d.get("character", "")).strip(), "line": str(d.get("line", "")).strip()}
            for d in (raw_scene.get("dialogue") or [])
            if isinstance(d, dict)
        ]
        characters = [
            str(name).strip()
            for name in (raw_scene.get("characters") or [])
            if str(name).strip()
        ]
        preferred = str(raw_scene.get("preferred_method") or "").strip()

        session.add(
            Scene(
                script_id=script.id,
                project_id=project.id,
                order_index=index,
                scene_key=str(raw_scene.get("scene_key") or f"s{index + 1:02d}")[:40],
                purpose=str(raw_scene.get("purpose") or "") or None,
                target_duration_s=float(raw_scene.get("target_duration_s") or 0.0),
                narration=str(raw_scene.get("narration") or "").strip() or None,
                dialogue=dialogue,
                character_names=characters,
                image_id=image.id if image else None,
                action=str(raw_scene.get("action") or "") or None,
                expression=str(raw_scene.get("expression") or "") or None,
                camera=str(raw_scene.get("camera") or "") or None,
                environment=str(raw_scene.get("environment") or "") or None,
                sfx_cue=str(raw_scene.get("sfx") or "") or None,
                transition=str(raw_scene.get("transition") or "cut")[:40],
                direction=directions[index] if index < len(directions) else {},
                preferred_method=preferred or None,
                status=SceneStatus.PLANNED,
            )
        )

    await _sync_characters(session, project, result.scenes)
    await session.flush()
    return script


async def _sync_characters(
    session: AsyncSession, project: Project, scenes: list[dict[str, Any]]
) -> int:
    """Create a Character row for every new name the plan introduces."""
    existing = {
        name
        for name in (
            await session.execute(
                select(Character.name).where(Character.project_id == project.id)
            )
        ).scalars()
    }
    found: list[str] = []
    for scene in scenes:
        for name in scene.get("characters") or []:
            cleaned = str(name).strip()
            if cleaned and cleaned not in existing and cleaned not in found:
                found.append(cleaned)

    for name in found:
        session.add(Character(project_id=project.id, name=name[:120]))
    if found:
        await session.flush()
    return len(found)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def generate(
    session: AsyncSession,
    project_id: int,
    *,
    provider_key: str = "",
    model_id: str = "",
) -> Script:
    """Ask an automated writer for a plan and store the result."""
    project = await project_service.get_project(session, project_id)
    key = provider_key or script_registry.default_key()
    provider = script_registry.get(key)
    if provider is None:
        raise ScriptError(f"No script provider called '{key}' is available.")

    caps = provider.capabilities()
    if not caps.automated:
        raise ScriptError(
            f"{caps.display_name} does not generate anything by itself. "
            "Copy the prompt and paste the reply back instead."
        )
    if not provider.is_configured:
        await project_service.mark_stage(
            session,
            project_id,
            PipelineStage.SCRIPT,
            StageStatus.BLOCKED,
            blocked_reason=(
                f"{caps.display_name} has no credentials. Add a key, or use the "
                "paste-it-yourself flow."
            ),
        )
        raise ScriptError(f"{caps.display_name} has no credentials configured.")

    previous = await _recent_problems(session, project_id)
    request = await build_request(session, project, previous_problems=previous)

    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.RUNNING
    )

    try:
        result = await provider.write(request, model_id=model_id or caps.default_model)
    except ScriptProviderError as exc:
        status = (
            StageStatus.BLOCKED
            if exc.error_class in {ErrorClass.AUTH, ErrorClass.INSUFFICIENT_CREDIT}
            else StageStatus.FAILED
        )
        await project_service.mark_stage(
            session,
            project_id,
            PipelineStage.SCRIPT,
            status,
            message=exc.message,
            blocked_reason=exc.message if status == StageStatus.BLOCKED else None,
        )
        raise ScriptError(exc.message) from exc

    script = await store(session, project, result, provider_key=key)
    await _mark_outcome(session, project_id, script)
    return script


async def accept_paste(
    session: AsyncSession, project_id: int, pasted: str
) -> Script:
    """Validate and store a plan the user produced elsewhere."""
    project = await project_service.get_project(session, project_id)
    provider = ManualPasteScriptProvider()
    try:
        result = provider.parse(pasted)
    except ScriptProviderError as exc:
        raise ScriptError(exc.message) from exc

    script = await store(session, project, result, provider_key="manual_paste")
    await _mark_outcome(session, project_id, script)
    return script


async def _mark_outcome(session: AsyncSession, project_id: int, script: Script) -> None:
    """A valid plan completes the stage; an invalid one fails it loudly."""
    if script.is_valid:
        await project_service.mark_stage(
            session,
            project_id,
            PipelineStage.SCRIPT,
            StageStatus.COMPLETED,
            message=f"Version {script.version} passed validation.",
            payload={"script_id": script.id, "version": script.version},
        )
    else:
        problems = (script.validation or {}).get("errors") or []
        await project_service.mark_stage(
            session,
            project_id,
            PipelineStage.SCRIPT,
            StageStatus.FAILED,
            message=f"Version {script.version} failed validation: {len(problems)} problem(s).",
            payload={"script_id": script.id, "version": script.version},
        )
    await project_service.refresh_status(session, project_id)


async def _recent_problems(session: AsyncSession, project_id: int) -> tuple[str, ...]:
    """Errors from the newest rejected attempt, to feed the retry."""
    latest = await project_service.current_script(session, project_id)
    if latest is None or latest.is_valid:
        return ()
    errors = (latest.validation or {}).get("errors") or []
    return tuple(str(e) for e in errors[:12])


async def approve(session: AsyncSession, script_id: int) -> Script:
    """Mark a script usable. Only an approved script may spend anything."""
    script = await session.get(Script, script_id)
    if script is None:
        raise ScriptError("That script no longer exists.")
    if not script.is_valid:
        problems = (script.validation or {}).get("errors") or []
        first = problems[0] if problems else "it did not pass validation"
        raise ScriptError(f"That script cannot be approved because {first}")
    script.approved = True
    await session.flush()
    return script


async def unapprove(session: AsyncSession, script_id: int) -> Script:
    script = await session.get(Script, script_id)
    if script is None:
        raise ScriptError("That script no longer exists.")
    script.approved = False
    await session.flush()
    return script


async def approved_script(session: AsyncSession, project_id: int) -> Script | None:
    """The approved version, which is the only one later stages may read."""
    return (
        await session.execute(
            select(Script)
            .where(Script.project_id == project_id, Script.approved.is_(True))
            .order_by(Script.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Screen context
# ---------------------------------------------------------------------------


async def screen_context(session: AsyncSession, project_id: int) -> dict[str, Any]:
    project = await project_service.get_project(session, project_id)
    script = await project_service.current_script(session, project_id)
    scenes: list[Scene] = []
    if script is not None:
        scenes = list(
            (
                await session.execute(
                    # The template shows each scene's still, and a lazy load
                    # during sync template rendering cannot reach the event
                    # loop, so the image has to come back with the scene.
                    select(Scene)
                    .options(selectinload(Scene.image))
                    .where(Scene.script_id == script.id)
                    .order_by(Scene.order_index)
                )
            ).scalars().all()
        )

    versions = list(
        (
            await session.execute(
                select(Script)
                .where(Script.project_id == project_id)
                .order_by(Script.version.desc())
                .limit(10)
            )
        ).scalars().all()
    )

    images = await available_images(session, project)
    return {
        "project": project,
        "script": script,
        "scenes": scenes,
        "versions": versions,
        "providers": script_registry.describe(),
        "default_provider": script_registry.default_key(),
        "image_count": len(images),
        "prompt": await prompt_for(session, project_id),
        "validation": (script.validation if script else {}) or {},
    }
