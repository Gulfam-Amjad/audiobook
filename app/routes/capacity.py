"""Daily capacity: what the free allowances can and cannot reach."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import page
from app.services import capacity

router = APIRouter()


@router.get("/capacity", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "capacity.html", await capacity.report(session))
