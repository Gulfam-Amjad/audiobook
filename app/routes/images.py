"""Image library screen."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session, session_scope
from app.models import Image, ImageFolder
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import images as image_service
from app.services.paths import UnsafePathError, is_within, require_existing_directory

log = logging.getLogger(__name__)
router = APIRouter()


async def _folder_rows(session: AsyncSession) -> list[ImageFolder]:
    return list(
        (await session.execute(select(ImageFolder).order_by(ImageFolder.id))).scalars().all()
    )


def _build_query(
    status: str | None, folder_id: int | None, search: str | None, page_no: int, show_dupes: bool
) -> image_service.ImageQuery:
    return image_service.ImageQuery(
        status=status or None,
        folder_id=folder_id or None,
        search=(search or "").strip() or None,
        hide_duplicates=not show_dupes,
        page=max(1, page_no),
    )


@router.get("/images", response_class=HTMLResponse, include_in_schema=False)
async def index(
    request: Request,
    status: str | None = None,
    folder_id: int | None = None,
    search: str | None = None,
    page_no: int = Query(1, alias="page"),
    show_dupes: bool = False,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    query = _build_query(status, folder_id, search, page_no, show_dupes)
    rows, total = await image_service.list_images(session, query)
    return page(
        request,
        "images.html",
        {
            "folders": await _folder_rows(session),
            "images": rows,
            "total": total,
            "query": query,
            "counts": await image_service.status_counts(session),
            "scans": image_service.all_progress(),
        },
    )


@router.get("/partials/images/grid", response_class=HTMLResponse, include_in_schema=False)
async def grid(
    request: Request,
    status: str | None = None,
    folder_id: int | None = None,
    search: str | None = None,
    page_no: int = Query(1, alias="page"),
    show_dupes: bool = False,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    query = _build_query(status, folder_id, search, page_no, show_dupes)
    rows, total = await image_service.list_images(session, query)
    return fragment(
        request,
        "partials/image_grid.html",
        {"images": rows, "total": total, "query": query},
    )


@router.get("/partials/images/scans", response_class=HTMLResponse, include_in_schema=False)
async def scan_status(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(
        request,
        "partials/scan_status.html",
        {"folders": await _folder_rows(session), "scans": image_service.all_progress()},
    )


@router.post(
    "/images/folders",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def add_folder(
    request: Request,
    folder_path: str = Form(...),
    recursive: bool = Form(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        resolved = require_existing_directory(folder_path)
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = (
        await session.execute(select(ImageFolder).where(ImageFolder.path == str(resolved)))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=400, detail=f"Folder already registered: {resolved}")

    folder = ImageFolder(path=str(resolved), recursive=recursive)
    session.add(folder)
    await session.commit()
    await session.refresh(folder)

    _launch_scan(folder.id)
    return fragment(
        request,
        "partials/scan_status.html",
        {"folders": await _folder_rows(session), "scans": image_service.all_progress()},
        flash=f"Added {resolved}. Scanning now.",
    )


@router.post(
    "/images/folders/{folder_id}/scan",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def rescan(
    request: Request, folder_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    folder = await session.get(ImageFolder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found.")

    progress = image_service.get_progress(folder_id)
    if progress and progress.is_running:
        raise HTTPException(status_code=409, detail="A scan of this folder is already running.")

    _launch_scan(folder_id)
    return fragment(
        request,
        "partials/scan_status.html",
        {"folders": await _folder_rows(session), "scans": image_service.all_progress()},
        flash=f"Scanning {folder.path}.",
    )


@router.post(
    "/images/folders/{folder_id}/cancel",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def cancel_scan(
    request: Request, folder_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    cancelled = image_service.request_cancel(folder_id)
    return fragment(
        request,
        "partials/scan_status.html",
        {"folders": await _folder_rows(session), "scans": image_service.all_progress()},
        flash="Cancelling after the current file." if cancelled else "No scan is running.",
    )


@router.delete(
    "/images/folders/{folder_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def remove_folder(
    request: Request,
    folder_id: int,
    forget_images: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    folder = await session.get(ImageFolder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found.")
    if image_service.get_progress(folder_id) and image_service.get_progress(folder_id).is_running:
        raise HTTPException(status_code=409, detail="Stop the running scan first.")

    path = folder.path
    if forget_images:
        # Only library rows are removed. The user's files are never touched.
        await session.execute(delete(Image).where(Image.folder_id == folder_id))
    await session.delete(folder)
    await session.commit()

    note = " Library entries removed; your files were not touched." if forget_images else ""
    return fragment(
        request,
        "partials/scan_status.html",
        {"folders": await _folder_rows(session), "scans": image_service.all_progress()},
        flash=f"Removed {path} from the library.{note}",
    )


@router.post(
    "/images/verify",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def verify(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    marked = await image_service.verify_present(session)
    message = (
        f"{marked} image(s) are no longer on disk and were marked missing."
        if marked
        else "Every library image is still present."
    )
    return fragment(
        request,
        "partials/scan_status.html",
        {"folders": await _folder_rows(session), "scans": image_service.all_progress()},
        flash=message,
    )


@router.get("/images/{image_id}/thumb", include_in_schema=False)
async def thumbnail(
    image_id: int, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    image = await session.get(Image, image_id)
    if image is None or not image.thumb_path:
        raise HTTPException(status_code=404, detail="No thumbnail for this image.")

    path = Path(image.thumb_path)
    # Thumbnails are generated by this app, so they must live under its own
    # thumbnail directory. Anything else is rejected rather than served.
    if not is_within(path, get_settings().thumbs_dir) or not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file is missing.")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/images/{image_id}/full", include_in_schema=False)
async def full_image(
    image_id: int, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    """Serve an original image, confined to registered library folders."""
    image = await session.get(Image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found.")

    roots = [Path(row.path) for row in await _folder_rows(session)]
    path = Path(image.path)
    if not any(is_within(path, root) for root in roots):
        raise HTTPException(status_code=403, detail="Image is outside every registered folder.")
    if not path.exists():
        raise HTTPException(status_code=404, detail="File is no longer on disk.")
    return FileResponse(path)


def _launch_scan(folder_id: int) -> None:
    """Run a scan in the background with its own database session."""

    async def runner() -> None:
        async with session_scope() as session:
            folder = await session.get(ImageFolder, folder_id)
            if folder is None:
                return
            await image_service.scan_folder(session, folder)

    task = asyncio.create_task(runner())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


#: Strong references so the event loop does not garbage-collect running scans.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
