"""Shots screen: how each scene gets its picture, and what it really is."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import routing as routing_service
from app.services.projects import ProjectError
from app.services.routing import RoutingError

router = APIRouter()


async def _context(session: AsyncSession, project_id: int) -> dict:
    try:
        return await routing_service.screen_context(session, project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/projects/{project_id}/shots", response_class=HTMLResponse, include_in_schema=False
)
async def index(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return page(request, "shots.html", await _context(session, project_id))


@router.get(
    "/partials/projects/{project_id}/shots",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def body(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(request, "partials/shots_body.html", await _context(session, project_id))


@router.post(
    "/projects/{project_id}/shots/plan",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def plan(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        result = await routing_service.plan_shots(session, project_id)
    except (RoutingError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    flash = (
        f"{len(result.decisions)} shot(s) planned. "
        f"{result.motion_assigned_s:.1f}s of the {result.motion_budget_s:.0f}s motion "
        f"budget was used; the rest are camera moves over stills."
    )
    return fragment(
        request,
        "partials/shots_body.html",
        await _context(session, project_id),
        flash=flash,
    )


@router.post(
    "/projects/{project_id}/shots/render",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def render(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        await routing_service.plan_shots(session, project_id)
        await session.commit()
        run = await routing_service.render_local_shots(session, project_id)
    except (RoutingError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    report = await routing_service.motion_report(session, project_id)
    if run.errors and not run.rendered:
        flash = run.errors[0]
    else:
        flash = (
            f"{run.rendered} rendered, {run.reused} unchanged, {run.failed} failed. "
            + routing_service.honest_summary(report)
        )
    return fragment(
        request,
        "partials/shots_body.html",
        await _context(session, project_id),
        flash=flash,
    )


@router.post(
    "/clips/{clip_id}/select",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def select(
    request: Request, clip_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        clip = await routing_service.select_clip(session, clip_id)
    except RoutingError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    kind = "generated motion" if clip.is_generated_motion else "a camera move over a still"
    return fragment(
        request,
        "partials/shots_body.html",
        await _context(session, clip.project_id),
        flash=f"That take is now in the edit. It is {kind}.",
    )
