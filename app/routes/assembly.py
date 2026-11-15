"""Assembly screen. Rendering runs in the background; the page polls status."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.routes.outputs import _parse_ids
from app.security import require_local_access
from app.services import assembly as assembly_service
from app.services import ffmpeg, library
from app.services.assembly import AssemblyError

router = APIRouter()
PARTIAL = "partials/assembly_body.html"


async def _context(session: AsyncSession) -> dict[str, object]:
    return {
        "outputs": await library.list_outputs(session),
        "assemblies": await assembly_service.list_assemblies(session),
        "ffmpeg": await ffmpeg.detect(),
        "install": ffmpeg.install_instructions(),
    }


@router.get("/assembly", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "assembly.html", await _context(session))


@router.get("/partials/assembly", response_class=HTMLResponse, include_in_schema=False)
async def body(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return fragment(request, PARTIAL, await _context(session))


@router.post(
    "/assembly",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def create(
    request: Request,
    name: str = Form("Assembly"),
    output_ids: str = Form(""),
    width: int = Form(1280),
    height: int = Form(720),
    fps: int = Form(24),
    fade_s: float = Form(0.0),
    audio_path: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    status = await ffmpeg.detect()
    if not status.available:
        return fragment(
            request,
            PARTIAL,
            await _context(session),
            flash=status.message or "Install FFmpeg before assembling.",
        )
    try:
        row = await assembly_service.create_assembly(
            session,
            name=name,
            output_ids=_parse_ids(output_ids),
            width=width,
            height=height,
            fps=fps,
            fade_s=fade_s,
            audio_path=audio_path,
        )
    except AssemblyError as exc:
        await session.rollback()
        return fragment(request, PARTIAL, await _context(session), flash=str(exc))
    await session.commit()
    assembly_id = row.id
    asyncio.create_task(assembly_service.run_assembly(assembly_id))
    return fragment(
        request,
        PARTIAL,
        await _context(session),
        flash=f"Assembly #{assembly_id} started. This page updates on its own.",
    )


@router.post(
    "/assembly/{assembly_id}/cancel",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def cancel(
    request: Request, assembly_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    assembly_service.request_cancel(assembly_id)
    return fragment(
        request,
        PARTIAL,
        await _context(session),
        flash=f"Cancellation requested for assembly #{assembly_id}.",
    )
