"""Video library: what was actually generated, measured, and filed."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import downloads, library
from app.services.library import LibraryError

router = APIRouter()
PARTIAL = "partials/library_body.html"


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"'{piece}' is not a clip id.") from exc
    return ids


async def _context(session: AsyncSession) -> dict[str, object]:
    return {"outputs": await library.list_outputs(session)}


@router.get("/outputs", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "library.html", await _context(session))


@router.get("/partials/outputs", response_class=HTMLResponse, include_in_schema=False)
async def body(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return fragment(request, PARTIAL, await _context(session))


@router.get("/outputs/{output_id}/media", include_in_schema=False)
async def media(output_id: int, session: AsyncSession = Depends(get_session)) -> FileResponse:
    try:
        _output, path = await library.output_file(session, output_id)
    except LibraryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


@router.post(
    "/outputs/{output_id}/reprobe",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def reprobe(
    request: Request, output_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    from app.models import Output

    output = await session.get(Output, output_id)
    if output is None:
        raise HTTPException(status_code=404, detail="That clip is not in the library.")
    media_info = await downloads.reprobe_output(session, output)
    await session.commit()
    message = (
        f"Measured {media_info.duration_s:.2f}s."
        if media_info.duration_s
        else f"Still unmeasured: {media_info.error}"
    )
    return fragment(request, PARTIAL, await _context(session), flash=message)


@router.post(
    "/outputs/{output_id}/open",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def open_folder(
    request: Request, output_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        _output, path = await library.output_file(session, output_id)
    except LibraryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    os.startfile(path.parent)  # noqa: S606 - path already confined to a library root
    return fragment(request, PARTIAL, await _context(session), flash=f"Opened {path.parent}.")


@router.post(
    "/outputs/export",
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def export(
    output_ids: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    destination = get_settings().tmp_dir / "library-export.zip"
    try:
        archive = await library.export_zip(session, _parse_ids(output_ids), destination)
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(archive, filename="clips.zip", media_type="application/zip")
