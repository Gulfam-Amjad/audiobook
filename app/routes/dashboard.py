"""Overview dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.services import stats

router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    summary = await stats.collect(session)
    health = await stats.provider_health(session)
    return page(
        request,
        "overview.html",
        {"stats": summary, "provider_health": health},
    )


@router.get("/partials/overview-stats", response_class=HTMLResponse, include_in_schema=False)
async def overview_stats(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    summary = await stats.collect(session)
    return fragment(request, "partials/overview_stats.html", {"stats": summary})
