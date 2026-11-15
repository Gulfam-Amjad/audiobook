"""Sending work to borrowed GPU time, and bringing it back honestly.

Free GPU allowances are small and non-refundable: a wasted Kaggle session
is a wasted week. Three habits in this module exist because of that.

**A resubmit never duplicates work.** Every batch gets a ``client_token``
derived from what it contains, so the same work resubmitted produces the
same token and the same kernel slug. A push to an existing slug makes a
new version of that notebook rather than a second one, and a task whose
submit response was lost is parked as ``UNCERTAIN`` for
:func:`reconcile` rather than fired off again.

**Finishing is not the same as succeeding.** A notebook can exit cleanly
having produced nothing. This module reads the run's own
``manifest.json`` and the files it brought back, and only counts a clip
as generated motion when the manifest says a model ran *and* ffprobe can
measure the file. A completed task with no usable output is a failure
with a readable reason.

**Nothing is published or paid for here.** Borrowed GPU time is free, so
no spending gate applies, but the work still only enters the edit through
a ``SceneClip`` the user can see and swap out.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.constants import (
    GENERATED_MOTION_METHODS,
    AnimationMethod,
    ClipStatus,
    ErrorClass,
    PipelineStage,
    RemotePurpose,
    RemoteTaskStatus,
    SceneStatus,
    StageStatus,
)
from app.models import Project, RemoteTask, Scene, SceneClip
from app.providers import compute
from app.providers.compute.base import ComputeError, ComputeJobSpec, ComputeStatus
from app.services import ffmpeg, notebooks
from app.services import projects as project_service
from app.services import routing
from app.services import scripts as script_service
from app.services.paths import safe_filename
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)


class RemoteError(Exception):
    """Something the user has to resolve before remote work can run."""


#: Per batch. A free session is time-limited, so a long queue of shots is
#: better split across several runs than risked in one that times out.
MAX_SHOTS_PER_BATCH = 6
#: Kaggle rejects an oversized push outright, so inputs are capped here
#: with a readable message instead.
MAX_PAYLOAD_BYTES = 18 * 1024 * 1024

#: Statuses that mean "do not touch this, it is somebody else's problem now".
LIVE_STATUSES = (
    RemoteTaskStatus.SUBMITTING,
    RemoteTaskStatus.QUEUED,
    RemoteTaskStatus.RUNNING,
    RemoteTaskStatus.UNCERTAIN,
)

_METHOD_FOR_PURPOSE = {
    RemotePurpose.IMAGE_TO_VIDEO: AnimationMethod.REMOTE_I2V,
    RemotePurpose.LIPSYNC: AnimationMethod.LIPSYNC,
}

_STAGE_FOR_PURPOSE = {
    RemotePurpose.IMAGE_TO_VIDEO: PipelineStage.MOTION,
    RemotePurpose.LIPSYNC: PipelineStage.LIPSYNC,
}


def batch_token(project_id: int, purpose: str, scene_keys: list[str]) -> str:
    """A stable name for one batch of work.

    Derived from the content rather than the clock, so retrying the same
    batch reuses the same remote slot instead of creating a second one.
    """
    digest = hashlib.sha256(
        json.dumps(
            {"project": project_id, "purpose": purpose, "scenes": sorted(scene_keys)},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"{purpose[:4]}-{project_id}-{digest[:16]}"


def remote_dir(project: Project) -> Path:
    folder = safe_filename(
        f"{project.id:04d}-{project.name}", fallback=f"{project.id:04d}"
    )
    return get_settings().remote_dir / folder


def scene_key(scene: Scene) -> str:
    return f"scene{scene.order_index:03d}"


@dataclass
class BatchPlan:
    """What a remote run would contain, before anything is sent."""

    purpose: str
    backend: str = ""
    shots: list[dict[str, Any]] = field(default_factory=list)
    inputs: list[tuple[str, Path]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    blocked_reason: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.shots) and not self.blocked_reason


async def plan_batch(
    session: AsyncSession, project_id: int, purpose: str = RemotePurpose.IMAGE_TO_VIDEO
) -> BatchPlan:
    """Work out which scenes would go to borrowed GPU time, and with what.

    Nothing is sent. A scene is included only when its routed method is
    the remote one and the inputs it needs are genuinely on disk.
    """
    if purpose not in _METHOD_FOR_PURPOSE:
        raise RemoteError(f"'{purpose}' is not something that runs remotely.")

    plan = BatchPlan(purpose=purpose)
    project = await project_service.get_project(session, project_id)
    script = await script_service.approved_script(session, project_id)
    if script is None:
        plan.blocked_reason = "No approved script, so there are no shots to send."
        return plan

    provider = compute.preferred(purpose)
    if provider is None:
        plan.blocked_reason = (
            "No borrowed GPU backend is configured. Add Kaggle credentials "
            "(AVM_KAGGLE_USERNAME and AVM_KAGGLE_KEY) or point AVM_HF_SPACE_I2V "
            "at a Space."
        )
        return plan
    plan.backend = provider.key

    scenes = list(
        (
            await session.execute(
                select(Scene)
                .options(selectinload(Scene.image))
                .where(Scene.script_id == script.id)
                .order_by(Scene.order_index)
            )
        )
        .scalars()
        .all()
    )

    wanted = _METHOD_FOR_PURPOSE[purpose]
    busy = await _busy_scene_ids(session, project_id, purpose)
    total_bytes = 0

    for scene in scenes:
        if scene.assigned_method != wanted:
            continue
        key = scene_key(scene)
        if scene.id in busy:
            plan.skipped.append(f"{key}: already out at a remote run.")
            continue
        if len(plan.shots) >= MAX_SHOTS_PER_BATCH:
            plan.skipped.append(f"{key}: held back for the next run.")
            continue

        image = scene.image
        if image is None or not image.path:
            plan.skipped.append(f"{key}: has no still to animate.")
            continue
        source = Path(image.path)
        if not source.exists():
            plan.skipped.append(f"{key}: its still is missing from disk.")
            continue

        size = source.stat().st_size
        # Base64 inflates by roughly a third, which is what actually
        # travels in the push body.
        if total_bytes + size * 4 // 3 > MAX_PAYLOAD_BYTES:
            plan.skipped.append(f"{key}: would push this batch over the size limit.")
            continue
        total_bytes += size * 4 // 3

        duration, measured = routing.shot_duration(scene)
        if not measured:
            plan.notes.append(
                f"{key}: its narration has not been measured, so the length sent "
                "is the script's estimate."
            )
        name = f"{key}{source.suffix.lower() or '.png'}"
        plan.inputs.append((name, source))
        plan.shots.append(
            {
                "scene_key": key,
                "scene_id": scene.id,
                "image": name,
                "duration_s": round(duration, 2),
                "prompt": (scene.motion_prompt or scene.action or "")[:400],
                "seed": scene.id,
            }
        )

    if not plan.shots:
        if any("already out at a remote run" in item for item in plan.skipped):
            # Saying "nothing is routed here" would be misleading: the work
            # is routed, it is simply still away being done.
            plan.blocked_reason = (
                "Every shot routed to borrowed GPU time is already out at a run. "
                "Check on those rather than sending them again."
            )
        else:
            plan.blocked_reason = (
                "No scene is routed to borrowed GPU time. Plan the shots first, or "
                "raise the project's generated-motion budget."
            )
    return plan


async def _busy_scene_ids(
    session: AsyncSession, project_id: int, purpose: str
) -> set[int]:
    rows = (
        await session.execute(
            select(RemoteTask).where(
                RemoteTask.project_id == project_id,
                RemoteTask.purpose == purpose,
                RemoteTask.status.in_(tuple(LIVE_STATUSES)),
            )
        )
    ).scalars().all()
    busy: set[int] = set()
    for task in rows:
        for shot in (task.payload or {}).get("shots", []):
            if shot.get("scene_id"):
                busy.add(int(shot["scene_id"]))
    return busy


async def submit_batch(
    session: AsyncSession, project_id: int, purpose: str = RemotePurpose.IMAGE_TO_VIDEO
) -> RemoteTask:
    """Send one batch and record it before the response can be lost.

    The row is written as ``SUBMITTING`` *before* the HTTP call, so a
    crash mid-request leaves evidence that something may be running
    remotely. That row becomes ``UNCERTAIN``, never a silent resubmit.
    """
    plan = await plan_batch(session, project_id, purpose)
    if plan.blocked_reason:
        raise RemoteError(plan.blocked_reason)

    project = await project_service.get_project(session, project_id)
    provider = compute.preferred(purpose)
    if provider is None:  # pragma: no cover - plan_batch already checked
        raise RemoteError("No borrowed GPU backend is configured.")

    token = batch_token(project_id, purpose, [s["scene_key"] for s in plan.shots])

    existing = (
        await session.execute(
            select(RemoteTask).where(RemoteTask.client_token == token)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status in LIVE_STATUSES:
        raise RemoteError(
            f"This exact batch is already out as task {existing.id} "
            f"({existing.status}). Poll it rather than sending it twice."
        )

    task = existing or RemoteTask(client_token=token)
    task.project_id = project_id
    task.purpose = purpose
    task.backend = provider.key
    task.status = RemoteTaskStatus.SUBMITTING
    task.payload = {
        "shots": plan.shots,
        "backend": provider.key,
        "skipped": plan.skipped,
        "notes": plan.notes,
    }
    task.attempts = (task.attempts or 0) + 1
    task.last_error = None
    task.error_class = None
    task.message = f"Submitting {len(plan.shots)} shot(s) to {provider.key}."
    task.submitted_at = utcnow()
    session.add(task)
    await session.commit()
    await session.refresh(task)

    settings = get_settings()
    notebook = notebooks.render(
        notebooks.NotebookSpec(
            purpose=purpose,
            inputs_json=_encode_inputs(plan.inputs),
            shots=plan.shots,
            weights_dir=(
                f"/kaggle/input/{settings.kaggle_weights_dataset.split('/')[-1]}"
                if settings.kaggle_weights_dataset
                else ""
            ),
        )
    )

    # Kept so a run can be inspected or re-pushed by hand if the app is
    # not around to do it.
    folder = remote_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{token}.ipynb").write_text(notebook, encoding="utf-8")

    datasets = (
        [settings.kaggle_weights_dataset] if settings.kaggle_weights_dataset else []
    )
    spec = ComputeJobSpec(
        purpose=purpose,
        title=f"AVM {project.name[:40]} {purpose}",
        notebook=notebook,
        datasets=datasets,
        enable_internet=True,
        client_token=token,
    )

    try:
        outcome = await provider.submit(spec)
    except ComputeError as exc:
        # An uncertain outcome means something may be running up there.
        # Parking it is the only safe answer; resubmitting could burn a
        # second slice of the weekly allowance for nothing.
        task.status = (
            RemoteTaskStatus.UNCERTAIN
            if exc.error_class == ErrorClass.UNCERTAIN
            else RemoteTaskStatus.FAILED
        )
        task.last_error = str(exc)
        task.error_class = exc.error_class
        task.message = str(exc)
        if task.status == RemoteTaskStatus.FAILED:
            task.finished_at = utcnow()
        await session.commit()
        await session.refresh(task)
        return task

    task.ref = outcome.ref
    task.version = str(outcome.version) if outcome.version is not None else None
    task.status = RemoteTaskStatus.QUEUED
    task.message = f"Queued at {provider.key}."
    if outcome.url:
        task.payload = {**(task.payload or {}), "url": outcome.url}
    await session.commit()
    await session.refresh(task)

    stage = _STAGE_FOR_PURPOSE[purpose]
    await project_service.mark_stage(
        session,
        project_id,
        stage,
        StageStatus.RUNNING,
        message=f"{len(plan.shots)} shot(s) are out at {provider.key}.",
        payload={"remote_task_ids": [task.id]},
    )
    return task


def _encode_inputs(inputs: list[tuple[str, Path]]) -> str:
    items = [
        {"name": name, "b64": base64.b64encode(path.read_bytes()).decode("ascii")}
        for name, path in inputs
    ]
    return base64.b64encode(json.dumps(items).encode("utf-8")).decode("ascii")


@dataclass
class CollectResult:
    """What came back from one finished run, after measuring it."""

    task_id: int
    status: str = ""
    clips_made: int = 0
    generated_motion_s: float = 0.0
    files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    model_ran: bool = False


async def poll_task(session: AsyncSession, task_id: int) -> CollectResult:
    """Ask the backend how a task is going, and collect it if it finished."""
    task = await session.get(RemoteTask, task_id)
    if task is None:
        raise RemoteError(f"There is no remote task {task_id}.")

    result = CollectResult(task_id=task_id, status=task.status)
    if task.status in (
        RemoteTaskStatus.COMPLETED,
        RemoteTaskStatus.FAILED,
        RemoteTaskStatus.CANCELLED,
    ):
        result.problems.append("This task has already finished.")
        return result
    if not task.ref:
        result.problems.append(
            "This task has no remote reference, so there is nothing to poll. "
            "It was never accepted."
        )
        return result

    provider = compute.get(task.backend, task.purpose)
    if provider is None:
        result.problems.append(f"The '{task.backend}' backend is not available.")
        return result

    try:
        status = await provider.poll(task.ref)
    except ComputeError as exc:
        task.last_polled_at = utcnow()
        task.last_error = str(exc)
        task.error_class = exc.error_class
        # A failed poll says nothing about the run itself, so the task
        # keeps its status rather than being written off.
        await session.commit()
        result.problems.append(str(exc))
        result.status = task.status
        return result

    task.last_polled_at = utcnow()
    task.status = status.state
    task.message = status.message
    if status.state == RemoteTaskStatus.FAILED:
        task.last_error = status.message
        task.finished_at = utcnow()
    result.status = status.state

    if status.state != RemoteTaskStatus.COMPLETED:
        await session.commit()
        if status.state == RemoteTaskStatus.FAILED:
            await _mark_stage_failure(session, task, status.message)
        return result

    await session.commit()
    return await collect(session, task_id)


async def collect(session: AsyncSession, task_id: int) -> CollectResult:
    """Download a finished run's files and turn the real ones into clips."""
    task = await session.get(RemoteTask, task_id)
    if task is None:
        raise RemoteError(f"There is no remote task {task_id}.")
    result = CollectResult(task_id=task_id, status=task.status)

    provider = compute.get(task.backend, task.purpose)
    if provider is None or not task.ref:
        result.problems.append("Nothing to collect from.")
        return result

    project = await project_service.get_project(session, task.project_id or 0)
    folder = remote_dir(project) / task.client_token
    folder.mkdir(parents=True, exist_ok=True)

    try:
        files = await provider.list_outputs(task.ref)
    except ComputeError as exc:
        task.last_error = str(exc)
        task.error_class = exc.error_class
        await session.commit()
        result.problems.append(str(exc))
        return result

    manifest: dict[str, Any] = {}
    downloaded: dict[str, Path] = {}
    for item in files:
        target = folder / safe_filename(item.name, fallback="output.bin")
        try:
            await provider.download(item, target)
        except ComputeError as exc:
            result.problems.append(f"{item.name}: {exc}")
            continue
        downloaded[item.name] = target
        if item.name.endswith(notebooks.MANIFEST_NAME):
            manifest = notebooks.parse_manifest(
                target.read_text(encoding="utf-8", errors="replace")
            )

    if not manifest:
        manifest = notebooks.parse_manifest("")
        result.problems.append(
            "The run brought back no manifest, so what it actually did is unknown."
        )
    result.model_ran = bool(manifest.get("model_ran"))
    result.problems.extend(str(p) for p in manifest.get("problems", []))
    result.files = sorted(downloaded)

    made, motion_s, troubles = await _clips_from_manifest(
        session, task, manifest, downloaded
    )
    result.clips_made = made
    result.generated_motion_s = motion_s
    result.problems.extend(troubles)

    task.artifacts = [
        {"name": name, "path": str(path)} for name, path in sorted(downloaded.items())
    ]
    task.finished_at = utcnow()
    if made:
        task.status = RemoteTaskStatus.COMPLETED
        task.message = f"Brought back {made} usable clip(s)."
    else:
        # A clean exit that produced nothing is a failure, not a success.
        task.status = RemoteTaskStatus.FAILED
        task.message = (
            "The run finished but produced no usable clip. "
            + (result.problems[0] if result.problems else "")
        ).strip()
        task.last_error = task.message
    if task.submitted_at and task.finished_at:
        task.gpu_seconds = round(
            (task.finished_at - task.submitted_at).total_seconds(), 1
        )
    result.status = task.status
    await session.commit()

    if made:
        await project_service.mark_stage(
            session,
            task.project_id or 0,
            _STAGE_FOR_PURPOSE[task.purpose],
            StageStatus.PARTIAL if result.problems else StageStatus.COMPLETED,
            message=f"{made} clip(s) came back with {motion_s:.1f}s of generated motion.",
            payload={"collected": [task.id]},
        )
    else:
        await _mark_stage_failure(session, task, task.message or "")
    return result


async def _mark_stage_failure(
    session: AsyncSession, task: RemoteTask, message: str
) -> None:
    await project_service.mark_stage(
        session,
        task.project_id or 0,
        _STAGE_FOR_PURPOSE.get(task.purpose, PipelineStage.MOTION),
        StageStatus.FAILED,
        message=message
        or "The remote run failed. The affected scenes still need a picture.",
    )


async def _clips_from_manifest(
    session: AsyncSession,
    task: RemoteTask,
    manifest: dict[str, Any],
    downloaded: dict[str, Path],
) -> tuple[int, float, list[str]]:
    """Turn measured files into clips. Unmeasurable files are not clips."""
    by_key = {
        shot.get("scene_key"): shot for shot in (task.payload or {}).get("shots", [])
    }
    status = await ffmpeg.detect()
    probe = status.ffprobe_path if status.available else None

    made = 0
    motion_s = 0.0
    problems: list[str] = []

    for entry in manifest.get("shots", []):
        key = entry.get("scene_key")
        shot = by_key.get(key)
        if shot is None:
            problems.append(f"The run returned '{key}', which was not in this batch.")
            continue
        if not entry.get("file"):
            problems.append(
                f"{key}: the run could not animate it ({entry.get('error', 'no reason given')})."
            )
            continue

        path = next(
            (p for name, p in downloaded.items() if Path(name).name == entry["file"]),
            None,
        )
        if path is None or not path.exists():
            problems.append(f"{key}: the manifest names a file the run did not return.")
            continue

        info = None
        if probe:
            info = await ffmpeg.probe(path, ffprobe_path=probe)
            if not info.ok:
                problems.append(
                    f"{key}: {info.error or 'came back with no playable video in it.'}"
                )
                info = None
        else:
            problems.append(
                f"{key}: FFmpeg is not installed, so this clip's length is unverified."
            )

        measured = info.duration_s if info else None
        if measured is None or measured <= 0:
            # Without a measurement there is no honest duration to put in
            # the edit, so this file does not become a clip.
            continue

        scene_id = int(shot["scene_id"])
        generated = bool(entry.get("generated_motion")) and bool(
            manifest.get("model_ran")
        )
        method = _METHOD_FOR_PURPOSE[task.purpose]
        session.add(
            SceneClip(
                scene_id=scene_id,
                project_id=task.project_id,
                method=method,
                provider_key=task.backend,
                remote_task_id=task.id,
                source_image_id=None,
                file_path=str(path),
                duration_s=measured,
                width=info.width if info else None,
                height=info.height if info else None,
                fps=info.fps if info else None,
                is_generated_motion=generated and method in GENERATED_MOTION_METHODS,
                selected=False,
                status=ClipStatus.READY,
                validation={
                    "requested_duration_s": shot.get("duration_s"),
                    "measured_duration_s": measured,
                    "manifest_frames": entry.get("frames"),
                    "model_ran": bool(manifest.get("model_ran")),
                    "backend": task.backend,
                    "video_codec": info.video_codec if info else None,
                },
            )
        )
        made += 1
        if generated:
            motion_s += measured

        scene = await session.get(Scene, scene_id)
        if scene is not None:
            scene.status = SceneStatus.READY

    await session.commit()
    return made, round(motion_s, 2), problems


async def reconcile(session: AsyncSession) -> list[CollectResult]:
    """Chase up tasks whose outcome was never established.

    These are the dangerous ones: something may be running on a free
    allowance with nobody watching. They are polled, never resubmitted.
    """
    tasks = (
        await session.execute(
            select(RemoteTask)
            .where(RemoteTask.status.in_(tuple(LIVE_STATUSES)))
            .order_by(RemoteTask.id)
        )
    ).scalars().all()

    results: list[CollectResult] = []
    for task in tasks:
        if not task.ref:
            # Submitted without ever getting a reference back. There is
            # nothing to poll, and guessing a slug could collide with
            # somebody else's notebook.
            task.status = RemoteTaskStatus.FAILED
            task.message = (
                "This task was never accepted by the backend, so nothing ran. "
                "It is safe to send again."
            )
            task.finished_at = utcnow()
            await session.commit()
            results.append(
                CollectResult(
                    task_id=task.id,
                    status=task.status,
                    problems=[task.message],
                )
            )
            continue
        results.append(await poll_task(session, task.id))
    return results


async def cancel_task(session: AsyncSession, task_id: int) -> RemoteTask:
    task = await session.get(RemoteTask, task_id)
    if task is None:
        raise RemoteError(f"There is no remote task {task_id}.")
    provider = compute.get(task.backend, task.purpose)
    if provider is not None and task.ref:
        try:
            await provider.cancel(task.ref)
        except ComputeError as exc:
            task.message = f"Cancelling could not be confirmed: {exc}"
    task.status = RemoteTaskStatus.CANCELLED
    task.finished_at = utcnow()
    await session.commit()
    await session.refresh(task)
    return task


async def quota_report(purpose: str = RemotePurpose.IMAGE_TO_VIDEO) -> dict[str, Any]:
    """What the backends say about remaining capacity.

    Figures come from a live call or not at all. Nothing here is a
    remembered limit.
    """
    rows: list[dict[str, Any]] = []
    for provider in compute.all_providers(purpose):
        caps = provider.capabilities()
        row: dict[str, Any] = {
            "backend": caps.key,
            "display_name": caps.display_name,
            "configured": provider.is_configured,
            "known": False,
            "detail": "",
        }
        if not provider.is_configured:
            row["detail"] = "Not configured, so nothing can be asked."
            rows.append(row)
            continue
        try:
            report = await provider.quota()
        except ComputeError as exc:
            row["detail"] = f"Could not be read: {exc}"
            rows.append(row)
            continue
        row.update(
            {
                "known": report.known,
                "detail": report.note,
                "used_s": report.used_s,
                "remaining_s": report.remaining_s,
                "allowed_s": report.allowed_s,
                "refresh_at": report.refresh_at.isoformat() if report.refresh_at else None,
            }
        )
        rows.append(row)
    return {"backends": rows}


async def list_tasks(
    session: AsyncSession, project_id: int | None = None, limit: int = 50
) -> list[RemoteTask]:
    query = select(RemoteTask).order_by(RemoteTask.id.desc()).limit(limit)
    if project_id is not None:
        query = query.where(RemoteTask.project_id == project_id)
    return list((await session.execute(query)).scalars().all())


async def screen_context(
    session: AsyncSession, project_id: int | None = None
) -> dict[str, Any]:
    """Everything the remote screen shows, with no invented numbers."""
    tasks = await list_tasks(session, project_id)
    backends = compute.describe()

    live = [t for t in tasks if t.status in LIVE_STATUSES]
    unverified = [b["display_name"] for b in backends if not b["live_verified"]]

    notes: list[str] = []
    if unverified:
        notes.append(
            "No call has yet succeeded from this machine for: "
            + ", ".join(unverified)
            + ". Until one does, these adapters are built to a published API "
            "but unproven here."
        )
    if any(b["configured"] is False for b in backends):
        notes.append(
            "A backend with missing credentials is switched off rather than "
            "guessed at."
        )

    plan: BatchPlan | None = None
    if project_id:
        try:
            plan = await plan_batch(session, project_id)
        except Exception as exc:  # noqa: BLE001 - shown, not raised at the user
            notes.append(f"The next batch could not be worked out: {exc}")

    return {
        "tasks": tasks,
        "backends": backends,
        "live_count": len(live),
        "project_id": project_id,
        "plan": plan,
        "notes": notes,
    }
