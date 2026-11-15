"""FastAPI application factory and lifespan.

Everything runs in one process: the HTTP server, the generation scheduler and
the manual-assist inbox watcher. That keeps the deployment story on Windows
to "run one command", and because all queue state lives in SQLite rather
than in memory, stopping the process never loses work.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import get_settings
from app.db import dispose_engine, healthcheck, session_scope
from app.logging_setup import configure_logging
from app.providers import registry
from app.providers import script as script_registry
from app.services import migrate
from app.services import prompts as prompt_service
from app.services.provider_sync import sync_providers
from app.templating import templates

log = logging.getLogger(__name__)

STARTUP_PROBLEMS: list[str] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.logs_dir, settings.log_level)
    log.info(
        "Starting AI Video Generation Manager",
        extra={"version": __version__, "data_root": str(settings.data_root)},
    )

    STARTUP_PROBLEMS.clear()

    try:
        before, after = migrate.upgrade_to_head()
        app.state.schema_revision = after
        if before != after:
            log.info("Schema migrated", extra={"from": before, "to": after})
    except Exception as exc:  # noqa: BLE001 - surfaced, then re-raised
        STARTUP_PROBLEMS.append(f"Database migration failed: {exc}")
        log.critical("Migration failed", exc_info=True)
        raise

    async with session_scope() as session:
        actions = await sync_providers(session)
        # A fresh install has no prompts, and a job cannot be queued without
        # one, so seed the examples rather than dead-ending the first run.
        seeded = await prompt_service.ensure_starter_templates(session)
    log.info("Provider registry synced", extra={"providers": actions, "prompts_seeded": seeded})
    for key, error in registry.IMPORT_ERRORS.items():
        STARTUP_PROBLEMS.append(f"Provider '{key}' failed to load: {error}")

    from app.services.scheduler import get_scheduler

    scheduler = get_scheduler()
    await scheduler.start()
    app.state.scheduler = scheduler

    from app.services.inbox_watcher import get_inbox_watcher

    watcher = get_inbox_watcher()
    await watcher.start()
    app.state.inbox_watcher = watcher

    from app.services import book_queue

    parked = book_queue.park_orphans()
    if parked:
        log.info("Parked interrupted books so they can continue", extra={"jobs": parked})
    if book_queue.ensure_runner():
        log.info("Book queue runner started")

    try:
        yield
    finally:
        log.info("Shutting down")
        await watcher.stop()
        await scheduler.stop()
        await registry.close_all()
        await script_registry.close_all()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI Video Generation Manager",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    from app.routes import (
        accounts,
        assembly,
        audiobooks,
        book_video,
        capacity,
        dashboard,
        diagnostics,
        files,
        images,
        manual,
        outputs,
        projects,
        prompts,
        providers,
        queue,
        remote,
        render,
        scripts,
        settings_routes,
        shots,
        voices,
    )

    app.include_router(book_video.router)
    app.include_router(dashboard.router)
    app.include_router(projects.router)
    app.include_router(scripts.router)
    app.include_router(voices.router)
    app.include_router(audiobooks.router)
    app.include_router(files.router)
    app.include_router(shots.router)
    app.include_router(remote.router)
    app.include_router(render.router)
    app.include_router(images.router)
    app.include_router(prompts.router)
    app.include_router(providers.router)
    app.include_router(accounts.router)
    app.include_router(queue.router)
    app.include_router(manual.router)
    app.include_router(outputs.router)
    app.include_router(capacity.router)
    app.include_router(assembly.router)
    app.include_router(settings_routes.router)
    app.include_router(diagnostics.router)

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        db_ok, db_message = await healthcheck()
        payload = {
            "status": "ok" if db_ok and not STARTUP_PROBLEMS else "degraded",
            "version": __version__,
            "database": {"ok": db_ok, "message": db_message},
            "data_root": str(settings.data_root),
            "problems": STARTUP_PROBLEMS,
        }
        return JSONResponse(payload, status_code=200 if db_ok else 503)

    @app.exception_handler(404)
    async def not_found(request: Request, _exc) -> HTMLResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return templates.TemplateResponse(
            request, "error.html", {"code": 404, "message": "Page not found."}, status_code=404
        )

    return app


app = create_app()
