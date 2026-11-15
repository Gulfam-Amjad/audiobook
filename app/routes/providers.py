"""Providers screen: what each adapter can do, and how far it has been proven."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.providers import registry
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import providers_view
from app.services.providers_view import ProviderActionError

router = APIRouter()


async def _context(session: AsyncSession) -> dict[str, object]:
    views = await providers_view.build_views(session)
    return {
        "providers": views,
        "summary": providers_view.summarise(views),
        "import_errors": registry.IMPORT_ERRORS,
    }


@router.get("/providers", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "providers.html", await _context(session))


@router.get("/partials/providers/table", response_class=HTMLResponse, include_in_schema=False)
async def table(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return fragment(request, "partials/provider_table.html", await _context(session))


@router.post(
    "/providers/{key}/enabled",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def set_enabled(
    request: Request,
    key: str,
    enabled: bool = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        message = await providers_view.set_enabled(session, key, enabled)
    except ProviderActionError as exc:
        # Rolled back so a refused enable leaves no trace, then reported in
        # the same partial rather than as a raw error page.
        await session.rollback()
        return fragment(
            request, "partials/provider_table.html", await _context(session), flash=str(exc)
        )
    await session.commit()
    return fragment(
        request, "partials/provider_table.html", await _context(session), flash=message
    )


@router.post(
    "/providers/{key}/priority",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def set_priority(
    request: Request,
    key: str,
    priority: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        message = await providers_view.set_priority(session, key, priority)
    except ProviderActionError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request, "partials/provider_table.html", await _context(session), flash=message
    )


@router.post(
    "/providers/accounts/{account_id}/test",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def test_connection(
    request: Request, account_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Check credentials only. This never generates and never spends."""
    try:
        message = await providers_view.test_connection(session, account_id)
    except ProviderActionError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request, "partials/provider_table.html", await _context(session), flash=message
    )
