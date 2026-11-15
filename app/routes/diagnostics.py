"""Health screen and a diagnostic download that contains no secrets."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import get_settings
from app.db import get_session, healthcheck
from app.logging_setup import redact
from app.main import STARTUP_PROBLEMS
from app.models import Provider
from app.providers import registry
from app.routes.common import page
from app.security import require_local_access
from app.services import ffmpeg, settings_store

router = APIRouter()


async def _context(session: AsyncSession) -> dict[str, object]:
    ok, db_message = await healthcheck()
    providers = list(
        (await session.execute(select(Provider).order_by(Provider.key))).scalars().all()
    )
    settings = get_settings()
    return {
        "version": __version__,
        "db_ok": ok,
        "db_message": db_message,
        "data_root": str(settings.data_root),
        "startup_problems": list(STARTUP_PROBLEMS),
        "import_errors": dict(registry.IMPORT_ERRORS),
        "providers": providers,
        "ffmpeg": await ffmpeg.detect(),
        "install": ffmpeg.install_instructions(),
        "stored_settings": await settings_store.get_all(session),
    }


@router.get("/diagnostics", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "diagnostics.html", await _context(session))


@router.get(
    "/diagnostics/export",
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def export(session: AsyncSession = Depends(get_session)) -> FileResponse:
    """A JSON file safe to paste into a bug report. Credentials are not read."""
    context = await _context(session)
    ffmpeg_status = context["ffmpeg"]
    payload = {
        "version": context["version"],
        "database": {"ok": context["db_ok"], "message": context["db_message"]},
        "data_root": context["data_root"],
        "startup_problems": context["startup_problems"],
        "import_errors": context["import_errors"],
        "ffmpeg": {
            "available": ffmpeg_status.available,
            "message": ffmpeg_status.message,
            "version": ffmpeg_status.ffmpeg_version,
        },
        "settings": context["stored_settings"],
        "providers": [
            {
                "key": row.key,
                "enabled": row.enabled,
                "verification_status": row.verification_status,
                "last_check_message": row.last_check_message,
            }
            for row in context["providers"]
        ],
    }
    text = redact(json.dumps(payload, indent=2, default=str))
    path = Path(get_settings().diagnostics_dir) / "diagnostic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return FileResponse(path, filename="avm-diagnostic.json", media_type="application/json")
