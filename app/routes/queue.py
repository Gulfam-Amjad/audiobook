"""Generation queue screen and actions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TERMINAL_JOB_STATES, JobStatus
from app.db import get_session
from app.models import Account, Batch, Image, Job, Output, Provider
from app.providers import registry
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import credits, images as image_service, jobs as job_service
from app.services import prompts as prompt_service
from app.services import selection, settings_store, spending
from app.services.generation import JobSettings
from app.services.jobs import JobError
from app.services.scheduler import get_scheduler

log = logging.getLogger(__name__)
router = APIRouter()


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in (raw or "").replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"'{chunk}' is not a valid image id."
            ) from exc
    return ids


async def _queue_context(
    session: AsyncSession, query: job_service.JobQuery
) -> dict[str, object]:
    rows, total = await job_service.list_jobs(session, query)
    image_ids = {job.image_id for job in rows}
    images = {}
    if image_ids:
        images = {
            row.id: row
            for row in (
                await session.execute(select(Image).where(Image.id.in_(image_ids)))
            ).scalars().all()
        }
    providers = {
        row.key: row for row in (await session.execute(select(Provider))).scalars().all()
    }
    accounts = {
        row.id: row for row in (await session.execute(select(Account))).scalars().all()
    }
    scheduler = get_scheduler()
    app_settings = await settings_store.get_all(session)

    return {
        "jobs": rows,
        "total": total,
        "query": query,
        "images": images,
        "providers": providers,
        "accounts": accounts,
        "counts": await job_service.status_counts(session),
        "queue_paused": bool(app_settings["queue_paused"]),
        "scheduler_alive": scheduler.alive,
        "scheduler_stats": scheduler.stats,
        "statuses": [s.value for s in JobStatus],
    }


@router.get("/queue", response_class=HTMLResponse, include_in_schema=False)
async def index(
    request: Request,
    status: str | None = None,
    provider_key: str | None = None,
    batch_id: int | None = None,
    page_no: int = Query(1, alias="page"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    query = job_service.JobQuery(
        status=status or None,
        provider_key=provider_key or None,
        batch_id=batch_id or None,
        page=max(1, page_no),
    )
    context = await _queue_context(session, query)
    app_settings = await settings_store.get_all(session)
    default_settings = JobSettings(
        duration_s=int(app_settings["default_duration_seconds"]),
        resolution=str(app_settings["default_resolution"]),
        aspect_ratio=str(app_settings["default_aspect_ratio"]),
    )
    context.update(
        {
            "templates": await prompt_service.list_templates(session),
            "eligibility": await selection.preview_eligibility(session, default_settings),
            "default_settings": default_settings,
            "spend": await spending.summary(session),
            "batches": (
                await session.execute(select(Batch).order_by(Batch.id.desc()).limit(25))
            ).scalars().all(),
        }
    )
    return page(request, "queue.html", context)


@router.get("/partials/queue/table", response_class=HTMLResponse, include_in_schema=False)
async def table(
    request: Request,
    status: str | None = None,
    provider_key: str | None = None,
    batch_id: int | None = None,
    page_no: int = Query(1, alias="page"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    query = job_service.JobQuery(
        status=status or None,
        provider_key=provider_key or None,
        batch_id=batch_id or None,
        page=max(1, page_no),
    )
    return fragment(request, "partials/queue_table.html", await _queue_context(session, query))


@router.post(
    "/queue/add",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def add(
    request: Request,
    image_ids: str = Form(""),
    select_all: bool = Form(False),
    status_filter: str = Form(""),
    folder_filter: int | None = Form(None),
    search_filter: str = Form(""),
    provider_key: str = Form(""),
    template_id: int | None = Form(None),
    duration_s: int | None = Form(None),
    resolution: str = Form(""),
    aspect_ratio: str = Form(""),
    batch_name: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    app_settings = await settings_store.get_all(session)
    settings = JobSettings(
        duration_s=duration_s or int(app_settings["default_duration_seconds"]),
        resolution=resolution or str(app_settings["default_resolution"]),
        aspect_ratio=aspect_ratio or str(app_settings["default_aspect_ratio"]),
    )

    if select_all:
        images = await image_service.resolve_selection(
            session,
            select_all_matching=image_service.ImageQuery(
                status=status_filter or None,
                folder_id=folder_filter or None,
                search=(search_filter or "").strip() or None,
            ),
        )
    else:
        images = await image_service.resolve_selection(
            session, image_ids=_parse_ids(image_ids)
        )

    if not images:
        raise HTTPException(
            status_code=400,
            detail="No usable images were selected. Missing files and duplicates are skipped.",
        )

    try:
        result = await job_service.enqueue_images(
            session,
            images,
            settings=settings,
            provider_key=provider_key or None,
            template_id=template_id or None,
            batch_name=batch_name or None,
        )
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await session.commit()
    get_scheduler().wake()

    message = result.summary
    if result.skipped:
        first = "; ".join(reason for _, reason in result.skipped[:2])
        message += f". Skipped: {first}"
    if result.adjustments:
        message += f". {result.adjustments[0]}"

    return fragment(request, "partials/flash.html", {"message": message, "tone": "good"})


@router.post(
    "/queue/pause",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def pause_queue(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    await settings_store.set_value(session, "queue_paused", True)
    await session.commit()
    return fragment(
        request,
        "partials/queue_controls.html",
        {"queue_paused": True, "scheduler_alive": get_scheduler().alive},
        flash="Queue paused. Jobs already with a provider keep being tracked.",
    )


@router.post(
    "/queue/resume",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def resume_queue(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    await settings_store.set_value(session, "queue_paused", False)
    await session.commit()
    get_scheduler().wake()
    return fragment(
        request,
        "partials/queue_controls.html",
        {"queue_paused": False, "scheduler_alive": get_scheduler().alive},
        flash="Queue started.",
    )


@router.post(
    "/queue/jobs/{job_id}/{action}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def job_action(
    request: Request,
    job_id: int,
    action: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    try:
        if action == "cancel":
            await job_service.cancel_job(session, job)
            message = f"Job #{job_id} cancelled."
        elif action == "retry":
            await job_service.retry_job(session, job)
            message = f"Job #{job_id} requeued."
        elif action == "retry-elsewhere":
            await job_service.retry_job(session, job, clear_provider=True)
            message = f"Job #{job_id} requeued; the provider will be chosen again."
        elif action == "pause":
            await job_service.pause_job(session, job)
            message = f"Job #{job_id} paused."
        elif action == "resume":
            await job_service.resume_job(session, job)
            message = f"Job #{job_id} resumed."
        elif action == "reconcile":
            message = await _reconcile(session, job)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await session.commit()
    get_scheduler().wake()
    return fragment(
        request,
        "partials/job_row.html",
        await _job_row_context(session, job_id),
        flash=message,
    )


async def _reconcile(session: AsyncSession, job: Job) -> str:
    """Ask the provider what really happened to an uncertain job."""
    if job.status != JobStatus.UNCERTAIN:
        raise JobError(f"Job #{job.id} is not in an uncertain state.")
    if not job.provider_key:
        raise JobError("This job was never assigned to a provider.")

    adapter = registry.get_adapter(job.provider_key)
    if adapter is None:
        raise JobError(f"Adapter '{job.provider_key}' is unavailable.")

    try:
        status = await adapter.reconcile(job.client_token, job.task_ref)
    except Exception as exc:  # noqa: BLE001
        raise JobError(f"Reconcile failed: {exc}") from exc

    if status is None:
        await job_service.record_event(
            session,
            job,
            "reconcile_inconclusive",
            "The provider could not confirm whether this task exists.",
        )
        return (
            f"{job.provider_key} could not confirm job #{job.id}. Check the provider's own "
            "dashboard before retrying, so you do not pay twice."
        )

    if status.state in ("pending", "running"):
        await job_service.transition(
            session,
            job,
            JobStatus.PROCESSING,
            event="reconciled",
            message="Confirmed with the provider: the task exists and is still running.",
        )
        return f"Job #{job.id} is alive at the provider; tracking resumed."

    if status.state == "succeeded" and status.video_url:
        job.settings = {**(job.settings or {}), "_video_url": status.video_url}
        await job_service.transition(
            session,
            job,
            JobStatus.DOWNLOADING,
            event="reconciled",
            message="Confirmed with the provider: the task finished. Downloading now.",
        )
        return f"Job #{job.id} had in fact completed; downloading the result."

    if status.state in ("failed", "not_found"):
        await job_service.transition(
            session,
            job,
            JobStatus.FAILED,
            event="reconciled",
            message=f"Confirmed with the provider: {status.message or status.state}.",
            error_class=status.error_class,
        )
        return f"Job #{job.id} did not succeed at the provider; it can now be retried safely."

    return f"The provider returned an unclear state for job #{job.id}."


@router.post(
    "/queue/bulk/{action}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def bulk_action(
    request: Request,
    action: str,
    status: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Cancel or retry every job in one status."""
    if not status:
        raise HTTPException(status_code=400, detail="Pick a status to act on.")

    rows = (await session.execute(select(Job).where(Job.status == status))).scalars().all()
    changed = 0
    problems: list[str] = []

    for job in rows:
        try:
            if action == "cancel":
                if job.status not in TERMINAL_JOB_STATES:
                    await job_service.cancel_job(session, job)
                    changed += 1
            elif action == "retry":
                await job_service.retry_job(session, job)
                changed += 1
            else:
                raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")
        except JobError as exc:
            problems.append(str(exc))

    await session.commit()
    get_scheduler().wake()

    message = f"{action.capitalize()} applied to {changed} job(s)."
    if problems:
        message += f" {len(problems)} could not be changed: {problems[0]}"
    query = job_service.JobQuery(status=status or None)
    return fragment(
        request, "partials/queue_table.html", await _queue_context(session, query), flash=message
    )


@router.get("/queue/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def job_detail(
    request: Request, job_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    image = await session.get(Image, job.image_id)
    account = await session.get(Account, job.account_id) if job.account_id else None
    provider = (
        await session.get(Provider, job.provider_key) if job.provider_key else None
    )
    entitlements = await credits.entitlements_for(session, account.id) if account else []
    output = (
        await session.execute(select(Output).where(Output.job_id == job.id))
    ).scalar_one_or_none()

    return fragment(
        request,
        "partials/job_detail.html",
        {
            "job": job,
            "image": image,
            "account": account,
            "provider": provider,
            "entitlements": entitlements,
            "events": await job_service.job_events(session, job_id),
            "settings": JobSettings.from_dict(job.settings),
            "output": output,
        },
    )


async def _job_row_context(session: AsyncSession, job_id: int) -> dict[str, object]:
    job = await session.get(Job, job_id)
    image = await session.get(Image, job.image_id) if job else None
    provider = await session.get(Provider, job.provider_key) if job and job.provider_key else None
    return {
        "job": job,
        "images": {image.id: image} if image else {},
        "providers": {provider.key: provider} if provider else {},
        "accounts": {},
    }
