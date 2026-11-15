"""Remote screen: work sent to borrowed GPU time, and what came back."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import RemotePurpose
from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import remote as remote_service
from app.services.projects import ProjectError
from app.services.remote import RemoteError

router = APIRouter()


async def _context(session: AsyncSession, project_id: int | None) -> dict:
    try:
        return await remote_service.screen_context(session, project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/remote", response_class=HTMLResponse, include_in_schema=False)
async def index(
    request: Request,
    project_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return page(request, "remote.html", await _context(session, project_id))


@router.get("/partials/remote", response_class=HTMLResponse, include_in_schema=False)
async def body(
    request: Request,
    project_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return fragment(request, "partials/remote_body.html", await _context(session, project_id))


@router.post(
    "/projects/{project_id}/remote/submit",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def submit(
    request: Request,
    project_id: int,
    purpose: str = Form(RemotePurpose.IMAGE_TO_VIDEO),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        task = await remote_service.submit_batch(session, project_id, purpose)
    except (RemoteError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    shots = len((task.payload or {}).get("shots", []))
    if task.status in ("failed", "uncertain"):
        flash = task.message or "The batch could not be sent."
    else:
        flash = (
            f"{shots} shot(s) went to {task.backend} as task {task.id}. "
            "Nothing is animated until it comes back and is measured."
        )
    return fragment(
        request, "partials/remote_body.html", await _context(session, project_id), flash=flash
    )


@router.post(
    "/remote/{task_id}/poll",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def poll(
    request: Request, task_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        result = await remote_service.poll_task(session, task_id)
    except RemoteError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.clips_made:
        flash = (
            f"{result.clips_made} clip(s) came back, {result.generated_motion_s:.1f}s "
            "of it genuinely animated and measured."
        )
    elif result.problems:
        flash = result.problems[0]
    else:
        flash = f"Task {task_id} is {result.status}."

    task = await session.get(remote_service.RemoteTask, task_id)
    return fragment(
        request,
        "partials/remote_body.html",
        await _context(session, task.project_id if task else None),
        flash=flash,
    )


@router.post(
    "/remote/reconcile",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def reconcile(
    request: Request,
    project_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    results = await remote_service.reconcile(session)
    if not results:
        flash = "Nothing is outstanding."
    else:
        finished = sum(1 for r in results if r.status in ("completed", "failed"))
        flash = f"Checked {len(results)} outstanding task(s); {finished} have now settled."
    return fragment(
        request, "partials/remote_body.html", await _context(session, project_id), flash=flash
    )


@router.post(
    "/remote/{task_id}/cancel",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def cancel(
    request: Request, task_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        task = await remote_service.cancel_task(session, task_id)
    except RemoteError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fragment(
        request,
        "partials/remote_body.html",
        await _context(session, task.project_id),
        flash=f"Task {task_id} is marked cancelled. {task.message or ''}".strip(),
    )


@router.get("/remote/quota", response_class=HTMLResponse, include_in_schema=False)
async def quota(
    request: Request,
    project_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    report = await remote_service.quota_report()
    known = [row for row in report["backends"] if row.get("known")]
    if known:
        row = known[0]
        remaining = row.get("remaining_s")
        flash = (
            f"{row['display_name']}: {remaining / 60:.0f} minutes left this period."
            if remaining is not None
            else f"{row['display_name']}: {row['detail']}"
        )
    else:
        details = "; ".join(
            f"{row['display_name']}: {row['detail']}" for row in report["backends"]
        )
        flash = f"No backend reported a figure. {details}" if details else (
            "No backend is configured, so there is no quota to read."
        )
    return fragment(
        request, "partials/remote_body.html", await _context(session, project_id), flash=flash
    )
