"""Script screen: generate or paste a scene plan, then approve it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import scripts as script_service
from app.services.projects import ProjectError
from app.services.scripts import ScriptError

router = APIRouter()


async def _context(session: AsyncSession, project_id: int) -> dict:
    try:
        return await script_service.screen_context(session, project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/projects/{project_id}/script", response_class=HTMLResponse, include_in_schema=False
)
async def index(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return page(request, "script.html", await _context(session, project_id))


@router.get(
    "/partials/projects/{project_id}/script",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def body(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(request, "partials/script_body.html", await _context(session, project_id))


@router.post(
    "/projects/{project_id}/script/generate",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def generate(
    request: Request,
    project_id: int,
    provider_key: str = Form(""),
    model_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        script = await script_service.generate(
            session, project_id, provider_key=provider_key, model_id=model_id
        )
    except (ScriptError, ProjectError) as exc:
        # The attempt may still have been stored with its problems attached,
        # so the stage status is committed before the error is raised.
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()

    if script.is_valid:
        flash = (
            f"Version {script.version} passed validation. Read it, then approve it "
            "before anything is generated."
        )
    else:
        count = len((script.validation or {}).get("errors") or [])
        flash = f"Version {script.version} came back with {count} problem(s). Nothing was spent."
    return fragment(
        request,
        "partials/script_body.html",
        await _context(session, project_id),
        flash=flash,
    )


@router.post(
    "/projects/{project_id}/script/paste",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def paste(
    request: Request,
    project_id: int,
    pasted: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        script = await script_service.accept_paste(session, project_id, pasted)
    except (ScriptError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()

    count = len((script.validation or {}).get("errors") or [])
    flash = (
        f"Version {script.version} passed validation."
        if script.is_valid
        else f"Version {script.version} has {count} problem(s) to fix."
    )
    return fragment(
        request, "partials/script_body.html", await _context(session, project_id), flash=flash
    )


@router.post(
    "/scripts/{script_id}/approve",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def approve(
    request: Request,
    script_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        script = await script_service.approve(session, script_id)
    except ScriptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/script_body.html",
        await _context(session, script.project_id),
        flash=f"Version {script.version} approved. Later stages will use it.",
    )


@router.post(
    "/scripts/{script_id}/unapprove",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def unapprove(
    request: Request,
    script_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        script = await script_service.unapprove(session, script_id)
    except ScriptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/script_body.html",
        await _context(session, script.project_id),
        flash=f"Version {script.version} is no longer approved.",
    )
