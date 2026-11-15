"""Image library: scanning, hashing, thumbnails and missing-file detection.

Memory discipline matters here - this runs on an 8 GB laptop against folders
that may hold thousands of photos. So:

* Files are hashed in 1 MiB chunks, never read whole.
* Thumbnails use Pillow's ``draft()`` to decode JPEGs at a reduced size, so a
  24-megapixel source never becomes a full-size bitmap in memory.
* Exactly one image is open at a time, and the walk is a generator.
* Blocking work happens in a worker thread so the event loop keeps serving
  the dashboard during a scan.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import SUPPORTED_IMAGE_EXTENSIONS, ImageStatus
from app.models import Image, ImageFolder
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

HASH_CHUNK = 1024 * 1024
THUMB_MAX = (320, 320)
#: Commit every N files so a long scan shows progress and survives a crash.
COMMIT_EVERY = 40


@dataclass
class ScanProgress:
    folder_id: int
    folder_path: str
    state: str = "running"  # running | done | failed | cancelled
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    duplicates: int = 0
    unreadable: int = 0
    missing_marked: int = 0
    message: str = ""
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self.state == "running"


#: folder_id -> progress for the most recent scan of that folder.
_PROGRESS: dict[int, ScanProgress] = {}
_CANCEL: set[int] = set()
#: One scan at a time keeps disk and memory pressure predictable.
_SCAN_LOCK = asyncio.Lock()


def get_progress(folder_id: int) -> ScanProgress | None:
    return _PROGRESS.get(folder_id)


def all_progress() -> dict[int, ScanProgress]:
    return dict(_PROGRESS)


def request_cancel(folder_id: int) -> bool:
    progress = _PROGRESS.get(folder_id)
    if progress and progress.is_running:
        _CANCEL.add(folder_id)
        return True
    return False


# ---------------------------------------------------------------------------
# Filesystem helpers (run in a worker thread)
# ---------------------------------------------------------------------------


def iter_image_files(root: Path, recursive: bool = True) -> Iterator[Path]:
    """Yield supported image files, skipping unreadable directories."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and not entry.name.startswith("."):
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if Path(entry.name).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
                                yield Path(entry.path)
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, OSError) as exc:
            log.warning("Skipping folder", extra={"path": str(current), "error": str(exc)})


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def thumb_path_for(sha256: str) -> Path:
    """Shard thumbnails two levels deep so no directory holds 100k files."""
    thumbs = get_settings().thumbs_dir
    return thumbs / sha256[:2] / f"{sha256}.jpg"


def make_thumbnail(source: Path, sha256: str) -> tuple[Path | None, int | None, int | None]:
    """Write a thumbnail and report the source's true dimensions."""
    target = thumb_path_for(sha256)
    try:
        with PILImage.open(source) as img:
            width, height = img.size
            # draft() lets the JPEG decoder skip most of the work; it is a
            # no-op for formats that do not support it.
            img.draft("RGB", THUMB_MAX)
            thumb = ImageOps.exif_transpose(img)
            thumb = thumb.convert("RGB")
            thumb.thumbnail(THUMB_MAX, PILImage.Resampling.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            thumb.save(target, "JPEG", quality=80, optimize=True)
        return target, width, height
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.warning("Thumbnail failed", extra={"path": str(source), "error": str(exc)})
        return None, None, None


@dataclass
class FileFacts:
    path: Path
    size: int
    mtime: float
    sha256: str
    width: int | None
    height: int | None
    thumb: Path | None
    error: str | None = None


def inspect_file(path: Path, *, need_thumb: bool) -> FileFacts:
    """Gather everything about one file. Runs in a worker thread."""
    stat = path.stat()
    sha256 = hash_file(path)
    width = height = None
    thumb: Path | None = None
    error: str | None = None

    existing_thumb = thumb_path_for(sha256)
    if existing_thumb.exists() and not need_thumb:
        thumb = existing_thumb
        try:
            with PILImage.open(path) as img:
                width, height = img.size
        except (UnidentifiedImageError, OSError) as exc:
            error = str(exc)
    else:
        thumb, width, height = make_thumbnail(path, sha256)
        if thumb is None:
            error = "Could not read this file as an image."

    return FileFacts(
        path=path,
        size=stat.st_size,
        mtime=stat.st_mtime,
        sha256=sha256,
        width=width,
        height=height,
        thumb=thumb,
        error=error,
    )


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


async def scan_folder(session: AsyncSession, folder: ImageFolder) -> ScanProgress:
    """Scan one registered folder into the library.

    Re-scanning is idempotent: a file whose size and mtime are unchanged is
    only touched to refresh ``last_seen_at``, and a hash that already exists
    under another path is recorded as a duplicate rather than imported twice.
    """
    progress = ScanProgress(folder_id=folder.id, folder_path=folder.path)
    _PROGRESS[folder.id] = progress
    _CANCEL.discard(folder.id)

    root = Path(folder.path)
    if not root.exists():
        progress.state = "failed"
        progress.message = f"Folder no longer exists: {root}"
        progress.finished_at = utcnow()
        return progress

    async with _SCAN_LOCK:
        try:
            await _run_scan(session, folder, root, progress)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            progress.state = "failed"
            progress.message = f"{type(exc).__name__}: {exc}"
            log.exception("Scan failed", extra={"folder": folder.path})
            await session.rollback()
        finally:
            progress.finished_at = utcnow()
            _CANCEL.discard(folder.id)
    return progress


async def _run_scan(
    session: AsyncSession, folder: ImageFolder, root: Path, progress: ScanProgress
) -> None:
    existing_rows = (
        await session.execute(select(Image).where(Image.folder_id == folder.id))
    ).scalars().all()
    by_path: dict[str, Image] = {row.path: row for row in existing_rows}
    seen_paths: set[str] = set()

    paths = await asyncio.to_thread(lambda: list(iter_image_files(root, folder.recursive)))
    log.info("Scan started", extra={"folder": str(root), "candidates": len(paths)})

    pending_commit = 0
    for path in paths:
        if folder.id in _CANCEL:
            progress.state = "cancelled"
            progress.message = "Cancelled before finishing."
            await session.commit()
            return

        progress.files_seen += 1
        key = str(path)
        seen_paths.add(key)
        row = by_path.get(key)

        try:
            quick_stat = await asyncio.to_thread(path.stat)
        except OSError as exc:
            progress.unreadable += 1
            log.warning("Unreadable file", extra={"path": key, "error": str(exc)})
            continue

        unchanged = (
            row is not None
            and row.size_bytes == quick_stat.st_size
            and abs(row.mtime - quick_stat.st_mtime) < 1.0
            and row.status != ImageStatus.MISSING
            and row.thumb_path
            and Path(row.thumb_path).exists()
        )
        if unchanged:
            row.last_seen_at = utcnow()
            continue

        facts = await asyncio.to_thread(inspect_file, path, need_thumb=row is None)
        if facts.error and facts.thumb is None:
            progress.unreadable += 1

        duplicate_of = await _find_duplicate(session, facts.sha256, exclude_path=key)

        if row is None:
            row = Image(
                folder_id=folder.id,
                path=key,
                filename=path.name,
                extension=path.suffix.lower(),
                sha256=facts.sha256,
                size_bytes=facts.size,
                width=facts.width,
                height=facts.height,
                mtime=facts.mtime,
                thumb_path=str(facts.thumb) if facts.thumb else None,
                status=ImageStatus.NEW,
                duplicate_of_id=duplicate_of,
                notes=facts.error,
            )
            session.add(row)
            by_path[key] = row
            progress.added += 1
        else:
            row.sha256 = facts.sha256
            row.size_bytes = facts.size
            row.width = facts.width
            row.height = facts.height
            row.mtime = facts.mtime
            row.thumb_path = str(facts.thumb) if facts.thumb else row.thumb_path
            row.duplicate_of_id = duplicate_of
            row.notes = facts.error
            if row.status == ImageStatus.MISSING:
                row.status = ImageStatus.NEW
                row.missing_since = None
            progress.updated += 1

        row.last_seen_at = utcnow()
        if duplicate_of is not None:
            progress.duplicates += 1

        pending_commit += 1
        if pending_commit >= COMMIT_EVERY:
            await session.commit()
            pending_commit = 0

    # Anything previously in this folder that the walk did not reach is gone.
    for key, row in by_path.items():
        if key in seen_paths:
            continue
        if row.status != ImageStatus.MISSING:
            row.status = ImageStatus.MISSING
            row.missing_since = utcnow()
            progress.missing_marked += 1

    folder.last_scan_at = utcnow()
    folder.last_scan_found = progress.files_seen
    folder.last_scan_added = progress.added
    await session.commit()

    progress.state = "done"
    progress.message = (
        f"{progress.added} added, {progress.updated} updated, "
        f"{progress.duplicates} duplicate, {progress.missing_marked} now missing, "
        f"{progress.unreadable} unreadable"
    )
    log.info("Scan finished", extra={"folder": str(root), "summary": progress.message})


async def _find_duplicate(
    session: AsyncSession, sha256: str, *, exclude_path: str
) -> int | None:
    """Return the id of an earlier image with the same content, if any."""
    result = await session.execute(
        select(Image.id)
        .where(Image.sha256 == sha256, Image.path != exclude_path)
        .order_by(Image.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def rescan_all(session: AsyncSession) -> list[ScanProgress]:
    folders = (
        await session.execute(select(ImageFolder).where(ImageFolder.enabled.is_(True)))
    ).scalars().all()
    results = []
    for folder in folders:
        results.append(await scan_folder(session, folder))
    return results


async def verify_present(session: AsyncSession) -> int:
    """Check every non-missing image still exists on disk.

    Cheaper than a full scan because it only stats known paths, so it is safe
    to offer as a quick button.
    """
    rows = (
        await session.execute(select(Image).where(Image.status != ImageStatus.MISSING))
    ).scalars().all()
    paths = [row.path for row in rows]
    present = await asyncio.to_thread(lambda: [os.path.exists(p) for p in paths])

    marked = 0
    for row, exists in zip(rows, present, strict=True):
        if not exists:
            row.status = ImageStatus.MISSING
            row.missing_since = utcnow()
            marked += 1
    if marked:
        await session.commit()
    return marked


# ---------------------------------------------------------------------------
# Queries for the UI
# ---------------------------------------------------------------------------


@dataclass
class ImageQuery:
    status: str | None = None
    folder_id: int | None = None
    search: str | None = None
    hide_duplicates: bool = True
    page: int = 1
    page_size: int = 60

    @property
    def offset(self) -> int:
        return max(0, (self.page - 1) * self.page_size)


async def list_images(session: AsyncSession, query: ImageQuery) -> tuple[list[Image], int]:
    stmt = select(Image)
    count_stmt = select(func.count(Image.id))

    conditions = []
    if query.status:
        conditions.append(Image.status == query.status)
    if query.folder_id:
        conditions.append(Image.folder_id == query.folder_id)
    if query.search:
        conditions.append(Image.filename.ilike(f"%{query.search}%"))
    if query.hide_duplicates:
        conditions.append(Image.duplicate_of_id.is_(None))

    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = int((await session.execute(count_stmt)).scalar() or 0)
    stmt = stmt.order_by(Image.id).offset(query.offset).limit(query.page_size)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows), total


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(select(Image.status, func.count()).group_by(Image.status))
    ).all()
    return {status: count for status, count in rows}


async def resolve_selection(
    session: AsyncSession,
    *,
    image_ids: list[int] | None = None,
    select_all_matching: ImageQuery | None = None,
) -> list[Image]:
    """Turn either an explicit id list or a filter into concrete rows.

    Rows that are missing from disk or flagged as duplicates are excluded so
    a bulk action cannot queue a file that is not there.
    """
    if select_all_matching is not None:
        broad = ImageQuery(
            status=select_all_matching.status,
            folder_id=select_all_matching.folder_id,
            search=select_all_matching.search,
            hide_duplicates=True,
            page=1,
            page_size=100_000,
        )
        rows, _ = await list_images(session, broad)
    elif image_ids:
        rows = (
            await session.execute(select(Image).where(Image.id.in_(image_ids)))
        ).scalars().all()
    else:
        return []

    return [
        row
        for row in rows
        if row.status != ImageStatus.MISSING and row.duplicate_of_id is None
    ]
