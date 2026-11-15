"""The video library: listing, preview, and export.

A file is served only when it sits inside a directory this app owns. Output
rows remember a path, and a path can be edited in the database, so the check
is against the file's real location rather than against the row.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import Output
from app.services import settings_store
from app.services.paths import is_within


class LibraryError(Exception):
    """A library action cannot be done, with a reason to show."""


async def list_outputs(session: AsyncSession) -> list[Output]:
    return list(
        (
            await session.execute(
                select(Output)
                .options(selectinload(Output.job))
                .order_by(Output.downloaded_at.desc(), Output.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def media_roots(session: AsyncSession) -> list[Path]:
    """Directories a library file is allowed to live in."""
    settings = get_settings()
    roots = [settings.data_root, settings.output_dir, settings.assemblies_dir]
    stored = await settings_store.get_all(session)
    override = str(stored.get("output_dir_override") or "").strip()
    if override:
        roots.append(Path(override))
    return roots


def assert_servable(path: Path, roots: list[Path]) -> Path:
    if not path.exists() or not path.is_file():
        raise LibraryError("That file is no longer on disk.")
    resolved = path.resolve()
    if not any(is_within(resolved, root) for root in roots):
        raise LibraryError("That file is outside the folders this app is allowed to open.")
    return resolved


async def output_file(session: AsyncSession, output_id: int) -> tuple[Output, Path]:
    output = (
        await session.execute(
            select(Output).where(Output.id == output_id).options(selectinload(Output.job))
        )
    ).scalar_one_or_none()
    if output is None:
        raise LibraryError("That clip is not in the library.")
    path = assert_servable(Path(output.file_path), await media_roots(session))
    return output, path


async def export_zip(session: AsyncSession, output_ids: list[int], destination: Path) -> Path:
    """Copy the chosen clips into one zip. Missing or unsafe paths are skipped."""
    if not output_ids:
        raise LibraryError("Select at least one clip to export.")
    roots = await media_roots(session)
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for output_id in output_ids:
            output = await session.get(Output, output_id)
            if output is None:
                continue
            path = Path(output.file_path)
            try:
                safe = assert_servable(path, roots)
            except LibraryError:
                continue
            archive.write(safe, arcname=safe.name)
            written += 1
    if written == 0:
        destination.unlink(missing_ok=True)
        raise LibraryError("None of the selected clips could be read.")
    return destination
