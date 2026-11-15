"""Voice library and per-project narration.

Two screens. ``/voices`` installs the Piper engine and voices and shows
what each voice's model card says about its licence. ``/projects/{id}/voice``
synthesises one project's narration and lists every measured segment.

Installs are explicit button presses. Nothing downloads on start-up.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import speech as speech_service
from app.services.piper import PiperInstallError
from app.services.projects import ProjectError
from app.services.speech import SpeechError

router = APIRouter()


async def _voices_context(session: AsyncSession) -> dict:
    return await speech_service.voices_context(session)


async def _narration_context(session: AsyncSession, project_id: int) -> dict:
    try:
        return await speech_service.narration_context(session, project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- The voice library ----------------------------------------------------


@router.get("/voices", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "voices.html", await _voices_context(session))


@router.get("/partials/voices", response_class=HTMLResponse, include_in_schema=False)
async def body(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return fragment(request, "partials/voices_body.html", await _voices_context(session))


@router.post(
    "/voices/engine/install",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def install_engine(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    from app.services import piper as piper_tool

    try:
        status = await piper_tool.install_engine()
    except PiperInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fragment(
        request,
        "partials/voices_body.html",
        await _voices_context(session),
        flash=f"Piper installed. {status.message}",
    )


@router.post(
    "/voices/install",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def install_voice(
    request: Request,
    key: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        voice = await speech_service.install_catalogue_voice(session, key)
    except PiperInstallError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    # The licence is reported as the card states it, including when that
    # means it could not be recognised.
    reason = (voice.notes or "").splitlines()[0] if voice.notes else ""
    return fragment(
        request,
        "partials/voices_body.html",
        await _voices_context(session),
        flash=f"Installed {voice.name}. {reason}".strip(),
    )


@router.post(
    "/voices/install-kokoro",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def install_kokoro(
    request: Request,
    key: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Record a Kokoro voice from the model files already on disk."""
    try:
        voice = await speech_service.install_kokoro_voice(session, key)
    except SpeechError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/voices_body.html",
        await _voices_context(session),
        flash=f"Installed Kokoro voice {voice.name}.",
    )


@router.post(
    "/voices/install-edge",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def install_edge(
    request: Request,
    key: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Record a Microsoft online voice. Nothing is downloaded."""
    try:
        voice = await speech_service.install_edge_voice(session, key)
    except SpeechError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/voices_body.html",
        await _voices_context(session),
        flash=f"Added {voice.name}. Its terms are unknown, so check them before publishing.",
    )


@router.post(
    "/voices/{voice_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def update(
    request: Request,
    voice_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    form = await request.form()
    fields = ("name", "speaker_id", "length_scale", "noise_scale", "noise_w", "sentence_silence_s")
    values = {name: form.get(name) for name in fields if name in form}
    try:
        voice = await speech_service.update_voice(session, voice_id, **values)
    except SpeechError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/voices_body.html",
        await _voices_context(session),
        flash=f"Updated {voice.name}.",
    )


@router.post(
    "/voices/{voice_id}/default",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def make_default(
    request: Request, voice_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        voice = await speech_service.set_default_voice(session, voice_id)
    except SpeechError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/voices_body.html",
        await _voices_context(session),
        flash=f"{voice.name} is now the default narrator.",
    )


@router.post(
    "/voices/{voice_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def remove(
    request: Request, voice_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        name = await speech_service.delete_voice(session, voice_id)
    except SpeechError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/voices_body.html",
        await _voices_context(session),
        flash=f"Removed {name}. Its downloaded files were left on disk.",
    )


# --- Narration for one project -------------------------------------------


@router.get(
    "/projects/{project_id}/voice", response_class=HTMLResponse, include_in_schema=False
)
async def narration(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return page(request, "narration.html", await _narration_context(session, project_id))


@router.get(
    "/partials/projects/{project_id}/voice",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def narration_body(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(
        request, "partials/narration_body.html", await _narration_context(session, project_id)
    )


@router.post(
    "/projects/{project_id}/voice/plan",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def plan(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        result = await speech_service.plan_segments(session, project_id)
    except (SpeechError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    flash = (
        f"{result.total} segment(s): {result.reusable} already recorded, "
        f"{result.to_synthesise} to record"
        + (f", {result.removed} no longer needed." if result.removed else ".")
    )
    return fragment(
        request,
        "partials/narration_body.html",
        await _narration_context(session, project_id),
        flash=flash,
    )


@router.post(
    "/projects/{project_id}/voice/synthesise",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def synthesise(
    request: Request, project_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        await speech_service.plan_segments(session, project_id)
        await session.commit()
        run = await speech_service.synthesise_project(session, project_id)
    except (SpeechError, ProjectError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if run.attempted == 0 and not run.errors:
        flash = "Everything was already recorded, so nothing was re-synthesised."
    elif run.failed:
        flash = (
            f"{run.synthesised} recorded, {run.failed} failed. "
            f"{run.measured_s:.1f}s measured. {run.errors[0] if run.errors else ''}"
        ).strip()
    elif run.errors:
        flash = run.errors[0]
    else:
        flash = f"{run.synthesised} segment(s) recorded, {run.measured_s:.1f}s measured."
    return fragment(
        request,
        "partials/narration_body.html",
        await _narration_context(session, project_id),
        flash=flash,
    )


@router.post(
    "/segments/{segment_id}/regenerate",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def regenerate(
    request: Request, segment_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        segment = await speech_service.regenerate_segment(session, segment_id)
    except SpeechError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/narration_body.html",
        await _narration_context(session, segment.project_id),
        flash=f"Re-recorded one line, now {segment.duration_s:.2f}s.",
    )
