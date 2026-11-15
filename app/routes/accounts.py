"""Credits screen: allowances, where each figure came from, and spending."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import accounts as account_service
from app.services.accounts import AccountError

router = APIRouter()

PARTIAL = "partials/credits_body.html"


def _optional_int(raw: str) -> int | None:
    """Blank means "no limit of our own", which is not the same as zero."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"'{raw}' is not a number.") from exc


def _optional_float(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"'{raw}' is not a number.") from exc


def _optional_date(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"'{raw}' is not a date. Use YYYY-MM-DD."
        ) from exc


async def _respond(
    request: Request, session: AsyncSession, message: str | None = None
) -> HTMLResponse:
    return fragment(
        request, PARTIAL, await account_service.screen_context(session), flash=message
    )


@router.get("/accounts", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    context = await account_service.screen_context(session)
    await session.commit()  # apply_known_resets may have moved a boundary
    return page(request, "credits.html", context)


@router.get("/partials/credits", response_class=HTMLResponse, include_in_schema=False)
async def body(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    context = await account_service.screen_context(session)
    await session.commit()
    return fragment(request, PARTIAL, context)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@router.post(
    "/accounts/new",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def create(
    request: Request,
    provider_key: str = Form(...),
    label: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        account = await account_service.create_account(session, provider_key, label)
    except AccountError as exc:
        await session.rollback()
        return await _respond(request, session, str(exc))
    await session.commit()
    return await _respond(request, session, f"Added account '{account.label}'.")


@router.post(
    "/accounts/{account_id}/enabled",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def set_enabled(
    request: Request,
    account_id: int,
    enabled: bool = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        message = await account_service.set_account_enabled(session, account_id, enabled)
    except AccountError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return await _respond(request, session, message)


@router.post(
    "/accounts/{account_id}/limits",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def set_limits(
    request: Request,
    account_id: int,
    concurrency_limit: str = Form(""),
    requests_per_minute: str = Form(""),
    daily_job_cap: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        message = await account_service.update_limits(
            session,
            account_id,
            concurrency_limit=_optional_int(concurrency_limit),
            requests_per_minute=_optional_int(requests_per_minute),
            daily_job_cap=_optional_int(daily_job_cap),
        )
    except AccountError as exc:
        await session.rollback()
        return await _respond(request, session, str(exc))
    await session.commit()
    return await _respond(request, session, message)


@router.post(
    "/accounts/{account_id}/paid",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def set_paid(
    request: Request,
    account_id: int,
    authorized: bool = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Authorise or withdraw real spending on one account."""
    try:
        message = await account_service.set_paid_authorized(session, account_id, authorized)
    except AccountError as exc:
        await session.rollback()
        return await _respond(request, session, str(exc))
    await session.commit()
    return await _respond(request, session, message)


@router.post(
    "/accounts/{account_id}/refresh",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def refresh(
    request: Request, account_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        message = await account_service.refresh_balance(session, account_id)
    except AccountError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return await _respond(request, session, message)


# ---------------------------------------------------------------------------
# Allowances
# ---------------------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/allowances",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def add_allowance(
    request: Request,
    account_id: int,
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        row = await account_service.add_entitlement(session, account_id, name)
    except AccountError as exc:
        await session.rollback()
        return await _respond(request, session, str(exc))
    await session.commit()
    return await _respond(request, session, f"Now tracking '{row.name}'.")


@router.post(
    "/allowances/{entitlement_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def save_allowance(
    request: Request,
    entitlement_id: int,
    balance: str = Form(""),
    unit: str = Form("credits"),
    refresh_interval: str = Form("none"),
    expires_at: str = Form(""),
    unit_cost_usd: str = Form(""),
    is_paid: bool = Form(False),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        message = await account_service.save_balance(
            session,
            entitlement_id,
            balance=_optional_float(balance),
            unit=unit,
            refresh_interval=refresh_interval,
            expires_at=_optional_date(expires_at),
            unit_cost_usd=_optional_float(unit_cost_usd),
            is_paid=is_paid,
            note=note,
        )
    except AccountError as exc:
        await session.rollback()
        return await _respond(request, session, str(exc))
    await session.commit()
    return await _respond(request, session, message)


@router.post(
    "/allowances/{entitlement_id}/reset",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def record_reset(
    request: Request, entitlement_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        message = await account_service.record_manual_reset(session, entitlement_id)
    except AccountError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return await _respond(request, session, message)


@router.delete(
    "/allowances/{entitlement_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def delete_allowance(
    request: Request, entitlement_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        message = await account_service.delete_entitlement(session, entitlement_id)
    except AccountError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return await _respond(request, session, message)
