"""The Files page: every finished audiobook and film, playable in place."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.routes.common import flash_only, fragment, page
from app.security import require_local_access
from app.services import files
from app.services.files import FilesError

router = APIRouter()
PARTIAL = "partials/files_body.html"


def _context() -> dict:
    return {
        "audiobooks": files.audiobook_rows(),
        "projects": files.project_rows(),
        "data_root": str(files.data_root()),
    }


@router.get("/files", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    return page(request, "files.html", _context())


@router.get("/partials/files", response_class=HTMLResponse, include_in_schema=False)
async def body(request: Request) -> HTMLResponse:
    return fragment(request, PARTIAL, _context())


@router.get("/files/media", include_in_schema=False)
async def media(path: str) -> FileResponse:
    try:
        target = files.resolve(path)
    except FilesError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(target, media_type=files.media_type(target))


@router.get("/files/download", include_in_schema=False)
async def download(path: str) -> FileResponse:
    try:
        target = files.resolve(path)
    except FilesError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(target, filename=target.name, media_type=files.media_type(target))


@router.post(
    "/files/open",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def open_folder(path: str = Form("")) -> HTMLResponse:
    try:
        folder = files.folder_for(path)
    except FilesError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    os.startfile(folder)  # noqa: S606 - confined to the data root by folder_for
    return flash_only(f"Opened {folder}.")
