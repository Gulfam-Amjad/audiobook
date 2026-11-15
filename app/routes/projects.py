"""Projects screen: create a video, set its format, watch its stages."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import film as film_service
from app.services import production
from app.services import projects as project_service
from app.services.projects import ProjectError

router = APIRouter()

#: Strong references so the event loop does not garbage-collect a running film.
_FILM_TASKS: set[asyncio.Task[Any]] = set()


async def _detail(session: AsyncSession, project_id: int) -> dict[str, Any]:
    try:
        context = await project_service.detail_context(session, project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    project = context["project"]
    context["film_run"] = film_service.RUNS.get(project_id)
    context["film_settings"] = {
        **production.DEFAULTS,
        "look": production.default_look(project.niche or ""),
        **(project.production or {}),
    }
    context["film_looks"] = production.LOOKS
    context["film_captions"] = production.CAPTIONS
    return context


@router.get("/projects", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "projects.html", await project_service.list_context(session))


@router.get("/partials/projects", response_class=HTMLResponse, include_in_schema=False)
async def list_partial(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(request, "partials/project_list.html", await project_service.list_context(session))


@router.post(
    "/projects",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def create(
    request: Request,
    name: str = Form(...),
    niche: str = Form(""),
    audience: str = Form(""),
    story_style: str = Form(""),
    language: str = Form("en"),
    target_duration_s: str = Form("300"),
    aspect_ratio: str = Form("16:9"),
    resolution: str = Form("720p"),
    fps: str = Form("24"),
    image_folder_id: str = Form(""),
    brief: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        project = await project_service.create_project(
            session,
            name=name,
            niche=niche,
            audience=audience,
            story_style=story_style,
            language=language,
            target_duration_s=target_duration_s,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            fps=fps,
            image_folder_id=image_folder_id,
            brief=brief,
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/project_list.html",
        await project_service.list_context(session),
        flash=f"Created '{project.name}'. Nothing has been generated yet.",
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False)
async def detail(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return page(request, "project_detail.html", await _detail(session, project_id))


@router.get(
    "/partials/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False
)
async def detail_partial(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(request, "partials/project_detail_body.html", await _detail(session, project_id))


@router.get(
    "/partials/projects/{project_id}/film", response_class=HTMLResponse, include_in_schema=False
)
async def film_partial(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(request, "partials/film_panel.html", await _detail(session, project_id))


@router.post(
    "/projects/{project_id}/film",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def make_film(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Start the whole chain in the background. Nothing is uploaded."""
    running = film_service.RUNS.get(project_id)
    if running and running.stage not in ("done", "failed"):
        raise HTTPException(status_code=409, detail="A film is already being made for this project.")
    form = await request.form()
    finishing: dict[str, Any] = {}
    if form.get("look") in production.LOOKS:
        finishing["look"] = form["look"]
    if form.get("captions") in production.CAPTIONS:
        finishing["captions"] = form["captions"]
    if "end_card" in form:
        finishing["end_card"] = str(form["end_card"])[:80]
    film_service.RUNS[project_id] = film_service.FilmRun(project_id=project_id, stage="queued")
    task = asyncio.create_task(film_service.make_film(project_id, finishing=finishing))
    _FILM_TASKS.add(task)
    task.add_done_callback(_FILM_TASKS.discard)
    return fragment(request, "partials/film_panel.html", await _detail(session, project_id))


@router.post(
    "/projects/{project_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def update(
    request: Request,
    project_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    form = await request.form()
    # Only fields actually present in the submitted form are touched, so a
    # partial form never blanks a setting it did not render.
    allowed = {
        "name",
        "niche",
        "audience",
        "story_style",
        "language",
        "brief",
        "notes",
        "target_duration_s",
        "aspect_ratio",
        "resolution",
        "fps",
        "speaking_rate_wpm",
        "max_generated_motion_s",
        "music_preference",
        "music_path",
        "music_gain_db",
        "narrator_voice_id",
        "image_folder_id",
        "stills_ai_generated",
    }
    values: dict[str, Any] = {k: v for k, v in form.items() if k in allowed}
    # An unchecked checkbox is absent from the form, which means "off" here
    # rather than "leave alone", because the field is always rendered.
    values["subtitles_enabled"] = form.get("subtitles_enabled") is not None

    try:
        await project_service.update_project(session, project_id, values)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/project_detail_body.html",
        await _detail(session, project_id),
        flash="Project settings saved.",
    )


@router.post(
    "/projects/{project_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def delete(
    request: Request,
    project_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        name = await project_service.delete_project(session, project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/project_list.html",
        await project_service.list_context(session),
        flash=f"Deleted '{name}'. Files already written to disk were left alone.",
    )
