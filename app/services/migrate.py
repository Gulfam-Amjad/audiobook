"""Run Alembic migrations from inside the application.

The user should never have to remember an ``alembic upgrade head`` step, so
the server brings its own schema up to date at startup. Failures are fatal -
running against a half-migrated database would corrupt job state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.config import PROJECT_ROOT, get_settings

log = logging.getLogger(__name__)


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().sync_db_url)
    return cfg


def current_revision() -> str | None:
    settings = get_settings()
    if not Path(settings.db_path).exists():
        return None
    engine = create_engine(settings.sync_db_url)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def head_revision() -> str | None:
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def upgrade_to_head() -> tuple[str | None, str | None]:
    """Apply all pending migrations. Returns ``(before, after)`` revisions."""
    settings = get_settings()
    settings.ensure_dirs()
    before = current_revision()
    head = head_revision()
    if before == head:
        log.info("Database schema up to date", extra={"revision": before})
        return before, head
    log.info("Applying migrations", extra={"from": before, "to": head})
    command.upgrade(_alembic_config(), "head")
    after = current_revision()
    log.info("Migrations applied", extra={"revision": after})
    return before, after
