"""Render screen: assembling the final file, and what the checks found."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Render
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import publishing as publish_service
from app.services import render as render_service
from app.services.projects import ProjectError
from app.services.publishing import PublishError
from app.services.render import RenderError

router = APIRouter()


async def _context(session: AsyncSession, project_id: int) -> dict:
    try:
        return await render_service.screen_context(session, project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/projects/{project_id}/render", response_class=HTMLResponse, include_in_schema=False
)
async def index(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return page(request, "render.html", await _context(session, project_id))


@router.get(
    "/partials/projects/{project_id}/render",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def body(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(
        request, "partials/render_body.html", await _context(session, project_id)
    )


@router.post(
    "/projects/{project_id}/render",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def start(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        outcome = await render_service.render_project(session, project_id)
    except (RenderError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if outcome.succeeded:
        flash = (
            f"Finished and checked: {outcome.duration_s:.1f}s, "
            f"{outcome.generated_motion_s:.1f}s of it genuinely animated."
        )
    elif outcome.problems:
        flash = outcome.problems[0]
    else:
        flash = "The render did not finish."
    return fragment(
        request,
        "partials/render_body.html",
        await _context(session, project_id),
        flash=flash,
    )


@router.get(
    "/renders/{render_id}/report", response_class=HTMLResponse, include_in_schema=False
)
async def report(
    render_id: int, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    record = await session.get(Render, render_id)
    if record is None or not record.report_path:
        raise HTTPException(status_code=404, detail="That render has no report.")
    path = Path(record.report_path)
    if not path.exists():
        raise HTTPException(
            status_code=404, detail="The report file is no longer on disk."
        )
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.post(
    "/renders/{render_id}/publish-assets",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def publish_assets(
    request: Request, render_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Write the title, description, tags, disclosure and thumbnail.

    This prepares files on disk. It does not upload anything, and there is
    deliberately no endpoint here that does.
    """
    record = await session.get(Render, render_id)
    if record is None:
        raise HTTPException(status_code=404, detail="There is no render with that id.")
    project_id = record.project_id
    try:
        assets = await publish_service.prepare(session, render_id)
    except (PublishError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    flash = (
        f"Upload assets written beside the video. Nothing has been uploaded: "
        f"{len(assets.keywords)} tag(s), {len(assets.chapters)} chapter(s)."
    )
    return fragment(
        request,
        "partials/render_body.html",
        await _context(session, project_id),
        flash=flash,
    )


@router.get(
    "/renders/{render_id}/publish-assets",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def publish_manifest(
    render_id: int, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    record = await session.get(Render, render_id)
    if record is None or not (record.publish_assets or {}).get("manifest_path"):
        raise HTTPException(
            status_code=404, detail="No upload assets have been prepared for that render."
        )
    path = Path(record.publish_assets["manifest_path"])
    if not path.exists():
        raise HTTPException(
            status_code=404, detail="The assets file is no longer on disk."
        )
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.get(
    "/renders/{render_id}/subtitles",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def subtitle_file(
    render_id: int, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    record = await session.get(Render, render_id)
    if record is None or not record.subtitle_path:
        raise HTTPException(status_code=404, detail="That render has no subtitles.")
    path = Path(record.subtitle_path)
    if not path.exists():
        raise HTTPException(
            status_code=404, detail="The subtitle file is no longer on disk."
        )
    return FileResponse(path, media_type="text/plain", filename=path.name)
