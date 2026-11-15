"""Manual assist: worksheets and the inbox tray.

Nothing on this screen talks to a provider. Preparing a batch only writes
jobs, and scanning the inbox only files videos the user downloaded by hand.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Image
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import accounts as account_service
from app.services import manual_assist, settings_store
from app.services.accounts import AccountError
from app.services.generation import JobSettings
from app.services.jobs import JobError
from app.services.manual_assist import ManualAssistError
from app.services.paths import is_within

router = APIRouter()
PARTIAL = "partials/manual_body.html"


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for piece in (raw or "").replace(" ", "").split(","):
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"'{piece}' is not an image id.") from exc
    return ids


async def _respond(
    request: Request,
    session: AsyncSession,
    site: str | None,
    message: str | None = None,
) -> HTMLResponse:
    return fragment(
        request,
        PARTIAL,
        await manual_assist.screen_context(session, site),
        flash=message,
    )


@router.get("/manual", response_class=HTMLResponse, include_in_schema=False)
async def index(
    request: Request,
    site: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    context = await manual_assist.screen_context(session, site)
    await session.commit()
    return page(request, "manual.html", context)


@router.get("/partials/manual", response_class=HTMLResponse, include_in_schema=False)
async def body(
    request: Request,
    site: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    context = await manual_assist.screen_context(session, site)
    await session.commit()
    return fragment(request, PARTIAL, context)


@router.post(
    "/manual/prepare",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def prepare(
    request: Request,
    site_key: str = Form(...),
    image_ids: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    app_settings = await settings_store.get_all(session)
    settings = JobSettings(
        duration_s=int(app_settings["default_duration_seconds"]),
        resolution=str(app_settings["default_resolution"]),
        aspect_ratio=str(app_settings["default_aspect_ratio"]),
    )
    ids = _parse_ids(image_ids)
    images: list[Image] = []
    if ids:
        images = list(
            (await session.execute(select(Image).where(Image.id.in_(ids)))).scalars().all()
        )
    try:
        message = await manual_assist.prepare_batch(session, site_key, images, settings)
    except (ManualAssistError, JobError) as exc:
        await session.rollback()
        return await _respond(request, session, site_key, str(exc))
    await session.commit()
    return await _respond(request, session, site_key, message)


@router.post(
    "/manual/scan",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def scan(
    request: Request,
    site: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    notes = await manual_assist.scan_inbox(session)
    await session.commit()
    message = (
        f"Checked the inbox. {len(notes)} file(s) needed a decision."
        if notes
        else "Checked the inbox. Nothing new to file."
    )
    return await _respond(request, session, site or None, message)


@router.post(
    "/manual/inbox/{item_id}/assign",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def assign(
    request: Request,
    item_id: int,
    job_id: int = Form(...),
    site: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        message = await manual_assist.assign_inbox_item(session, item_id, job_id)
    except ManualAssistError as exc:
        await session.rollback()
        return await _respond(request, session, site or None, str(exc))
    await session.commit()
    return await _respond(request, session, site or None, message)


@router.post(
    "/manual/inbox/{item_id}/dismiss",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def dismiss(
    request: Request,
    item_id: int,
    site: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        message = await manual_assist.dismiss_inbox_item(session, item_id)
    except ManualAssistError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return await _respond(request, session, site or None, message)


@router.post(
    "/manual/open-folder",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def open_folder(
    request: Request,
    site_key: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Open the inbox in Explorer. Only a folder we created ourselves."""
    try:
        folder = manual_assist.site_inbox(site_key) if site_key else manual_assist.inbox_root()
    except ManualAssistError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not is_within(folder, manual_assist.inbox_root()):
        raise HTTPException(status_code=400, detail="That folder is not inside the inbox.")
    os.startfile(folder)  # noqa: S606 - local app, path is confined to the inbox
    return await _respond(request, session, site_key or None, f"Opened {folder}.")


@router.post(
    "/manual/sites/{site_key}/balance",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def save_site_balance(
    request: Request,
    site_key: str,
    entitlement_id: int = Form(...),
    balance: str = Form(""),
    refresh_interval: str = Form("daily"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Record a figure the user read off the site. It stays marked as theirs."""
    from app.providers.manual_sites import get_site
    from app.routes.accounts import _optional_float

    site = get_site(site_key)
    if site is None:
        raise HTTPException(status_code=404, detail="Unknown site.")
    try:
        message = await account_service.save_balance(
            session,
            entitlement_id,
            balance=_optional_float(balance),
            unit=site.credit_unit,
            refresh_interval=refresh_interval,
            note=f"Entered for {site.name}. This is your figure, not one the site reported to the app.",
        )
    except AccountError as exc:
        await session.rollback()
        return await _respond(request, session, site_key, str(exc))
    await session.commit()
    return await _respond(request, session, site_key, message)
