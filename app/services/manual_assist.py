"""Manual assist: worksheets, site accounts, and filing inbox downloads.

The app never drives the websites. It decides which image goes to which
site, prints the prompt and a short match code, and files whatever video
the user drops into the inbox.

Matching order, and only this order:

1. A match code in the filename wins, even if the file is in the wrong
   site folder. A code that belongs to no waiting job is left unmatched
   rather than guessed.
2. A file in a site folder with no code at all is attached to the oldest
   waiting job for that site.
3. Anything else stays in the unmatched tray until the user assigns it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import (
    BalanceSource,
    ImageStatus,
    JobStatus,
    OutputSource,
    RefreshInterval,
)
from app.models import Account, Entitlement, Image, InboxItem, Job, Provider
from app.providers.manual_sites import SITES, ManualSite, get_site
from app.services import credits, downloads, jobs as job_service
from app.services.generation import JobSettings
from app.services.paths import is_within
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

#: ``token_hex(3)`` is six hex characters. The negative lookahead stops a
#: longer client token, which starts the same way, from being misread as one.
TOKEN_RE = re.compile(r"avm-[0-9a-f]{6}(?![0-9a-f])", re.IGNORECASE)

SITE_NOTE_PREFIX = "site:"

#: Files still being written, and Windows junk, are not videos.
_IGNORE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


class ManualAssistError(Exception):
    """Shown to the user when a worksheet or inbox action cannot be done."""


@dataclass(slots=True)
class SiteAccount:
    site: ManualSite
    account: Account
    entitlement: Entitlement
    waiting: list[Job]


def site_key_of(account: Account) -> str | None:
    notes = (account.notes or "").strip()
    if not notes.startswith(SITE_NOTE_PREFIX):
        return None
    return notes[len(SITE_NOTE_PREFIX) :].strip() or None


def inbox_root() -> Path:
    root = get_settings().inbox_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def site_inbox(site_key: str) -> Path:
    """The folder the user drops one site's downloads into."""
    site = get_site(site_key)
    if site is None:
        raise ManualAssistError(f"'{site_key}' is not one of the ten manual sites.")
    folder = inbox_root() / site.key
    folder.mkdir(parents=True, exist_ok=True)
    return folder


async def ensure_site_accounts(session: AsyncSession) -> None:
    """One account and one allowance per site, created once and then left alone.

    These accounts are not enabled for the automatic queue. A job reaches a
    site only when a worksheet is prepared for it, so turning manual assist
    on cannot silently divert every queued image onto a website.
    """
    provider = await session.get(Provider, "manual")
    if provider is None:
        return

    existing = (
        await session.execute(select(Account).where(Account.provider_key == "manual"))
    ).scalars().all()
    by_key = {site_key_of(account): account for account in existing}

    for index, site in enumerate(SITES):
        account = by_key.get(site.key)
        if account is None:
            account = Account(
                provider_key="manual",
                label=site.name,
                enabled=True,
                # Later than a real API account, so an unpinned job prefers
                # an API or the mock over a website the user drives by hand.
                priority=500 + index,
                notes=f"{SITE_NOTE_PREFIX}{site.key}",
            )
            session.add(account)
            await session.flush()
        entitlement = await credits.ensure_entitlement(session, account, name=site.credit_unit)
        if entitlement.unit == "credits" and site.credit_unit != "credits":
            entitlement.unit = site.credit_unit[:40]
        if not entitlement.note:
            entitlement.note = site.free_allowance[:2000]
        site_inbox(site.key)

    await session.flush()


async def site_views(session: AsyncSession) -> list[SiteAccount]:
    await ensure_site_accounts(session)
    accounts = (
        await session.execute(
            select(Account).where(Account.provider_key == "manual").order_by(Account.priority)
        )
    ).scalars().all()

    waiting = (
        await session.execute(
            select(Job)
            .where(Job.status == JobStatus.AWAITING_MANUAL)
            .order_by(Job.created_at, Job.id)
        )
    ).scalars().all()
    by_account: dict[int, list[Job]] = {}
    for job in waiting:
        if job.account_id is not None:
            by_account.setdefault(job.account_id, []).append(job)

    views: list[SiteAccount] = []
    for account in accounts:
        key = site_key_of(account)
        site = get_site(key or "")
        if site is None:
            continue
        entitlements = await credits.entitlements_for(session, account.id)
        entitlement = entitlements[0] if entitlements else await credits.ensure_entitlement(
            session, account, name=site.credit_unit
        )
        views.append(
            SiteAccount(
                site=site,
                account=account,
                entitlement=entitlement,
                waiting=by_account.get(account.id, []),
            )
        )
    return views


async def prepare_batch(
    session: AsyncSession,
    site_key: str,
    images: list[Image],
    settings: JobSettings,
) -> str:
    """Pin images to one site and move them straight onto its worksheet.

    The queue does not have to be running. Nothing is submitted anywhere.
    """
    site = get_site(site_key)
    if site is None:
        raise ManualAssistError(f"'{site_key}' is not one of the ten manual sites.")
    if not images:
        raise ManualAssistError(
            "No images were selected. Add a folder on the Image Library screen first, "
            "then pick the stills you want on this worksheet."
        )

    views = {view.site.key: view for view in await site_views(session)}
    view = views[site.key]

    result = await job_service.enqueue_images(
        session,
        images,
        settings=settings,
        provider_key="manual",
        account_id=view.account.id,
        batch_name=f"{site.name} worksheet",
    )
    for job in result.created:
        await job_service.transition(
            session,
            job,
            JobStatus.AWAITING_MANUAL,
            event="worksheet",
            message=(
                f"On the {site.name} worksheet. Generate it yourself and name the "
                f"download so it contains {job.manual_token}, or drop it into the "
                f"{site.key} inbox folder."
            ),
        )

    if not result.created:
        detail = "; ".join(reason for _, reason in result.skipped[:3])
        raise ManualAssistError(detail or "Nothing was added to the worksheet.")

    folder = site_inbox(site.key)
    skipped = f" {len(result.skipped)} skipped." if result.skipped else ""
    return (
        f"{len(result.created)} clip(s) are on the {site.name} worksheet.{skipped} "
        f"Drop the downloads into {folder}."
    )


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def _site_of_path(path: Path) -> str | None:
    """A file in inbox/<site>/ belongs to that site. The inbox root does not."""
    try:
        relative = path.resolve().relative_to(inbox_root().resolve())
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    return relative.parts[0] if get_site(relative.parts[0]) else None


def _safe_video(path: Path) -> str | None:
    """Return a reason to ignore this path, or None when it is a candidate."""
    if not path.is_file():
        return "not a file"
    if path.name.lower() in _IGNORE_NAMES or path.name.endswith(".part"):
        return "ignored"
    if path.suffix.lower() not in {".mp4", ".webm", ".mov", ".mkv", ".m4v"}:
        return "not a video"
    try:
        resolved = path.resolve()
    except OSError:
        return "unreadable"
    if not is_within(resolved, inbox_root()):
        return "outside the inbox"
    return None


async def _waiting_jobs(session: AsyncSession, account_id: int | None = None) -> list[Job]:
    query = select(Job).where(Job.status == JobStatus.AWAITING_MANUAL)
    if account_id is not None:
        query = query.where(Job.account_id == account_id)
    query = query.order_by(Job.created_at, Job.id)
    return list((await session.execute(query)).scalars().all())


async def _job_for_token(session: AsyncSession, token: str) -> Job | None:
    return (
        await session.execute(
            select(Job).where(Job.manual_token == token.lower()).limit(1)
        )
    ).scalar_one_or_none()


async def _file_job(
    session: AsyncSession, job: Job, path: Path
) -> tuple[bool, str]:
    """Move a dropped file into the library and complete the job."""
    if job.status != JobStatus.AWAITING_MANUAL:
        return False, f"Job #{job.id} is {job.status}, so this file was not attached."

    result = await downloads.import_local_file(session, job, path, move=True)
    if not result.ok or result.path is None:
        return False, result.message

    output = await downloads.record_output(
        session, job, result, source=OutputSource.MANUAL
    )
    measured = (
        f"{output.duration_s:.2f}s measured"
        if output.duration_s
        else "duration unmeasured (FFprobe is not installed)"
    )
    requested = JobSettings.from_dict(job.settings).duration_s
    await job_service.transition(
        session,
        job,
        JobStatus.COMPLETED,
        event="manual_import",
        message=(
            f"Filed {result.path.name} from the inbox ({measured}). "
            f"The worksheet asked for {requested}s. This counts as generated video "
            "only for the seconds actually measured."
        ),
    )
    return True, f"Attached to job #{job.id}: {measured}."


async def ingest_path(session: AsyncSession, path: Path) -> str | None:
    """Consider one inbox file. Returns a user-facing note, or None if ignored."""
    try:
        path = path.resolve()
    except OSError:
        return None
    reason = _safe_video(path)
    if reason:
        return None
    if path.stat().st_size < downloads.MIN_PLAUSIBLE_VIDEO_BYTES:
        return None

    existing = (
        await session.execute(select(InboxItem).where(InboxItem.file_path == str(path)))
    ).scalar_one_or_none()
    if existing is not None and existing.status in ("matched", "dismissed"):
        return None

    token_match = TOKEN_RE.search(path.name)
    token = token_match.group(0).lower() if token_match else None
    site_key = _site_of_path(path)

    job: Job | None = None
    how = ""
    if token:
        job = await _job_for_token(session, token)
        if job is None:
            how = ""
        elif job.status != JobStatus.AWAITING_MANUAL:
            job = None
            how = f"Code {token} belongs to a job that is no longer waiting."
        else:
            how = "match code"
    elif site_key:
        account_id = await _account_id_for_site(session, site_key)
        waiting = await _waiting_jobs(session, account_id)
        if waiting:
            job = waiting[0]
            how = "oldest waiting clip for this site"

    if job is None:
        message = how or (
            "No match code in the filename, and this folder has no waiting clip to "
            "attach it to in order."
            if site_key
            else "No match code, and the file is not inside a site folder, so it was not guessed."
        )
        await _remember_unmatched(session, existing, path, site_key, token, message)
        return f"{path.name}: {message}"

    ok, message = await _file_job(session, job, path)
    if not ok:
        await _remember_unmatched(session, existing, path, site_key, token, message)
        return f"{path.name}: {message}"

    item = existing or InboxItem(file_path=str(path))
    item.site_hint = site_key
    item.detected_token = token
    item.matched_job_id = job.id
    item.status = "matched"
    item.file_size = 0
    item.message = message
    item.seen_at = utcnow()
    if existing is None:
        session.add(item)
    await session.flush()
    log.info("inbox.matched file=%s job=%s via=%s", path.name, job.id, how)
    return f"{path.name}: {message}"


async def _account_id_for_site(session: AsyncSession, site_key: str) -> int | None:
    accounts = (
        await session.execute(select(Account).where(Account.provider_key == "manual"))
    ).scalars().all()
    for account in accounts:
        if site_key_of(account) == site_key:
            return account.id
    return None


async def _remember_unmatched(
    session: AsyncSession,
    existing: InboxItem | None,
    path: Path,
    site_key: str | None,
    token: str | None,
    message: str,
) -> None:
    item = existing or InboxItem(file_path=str(path))
    item.site_hint = site_key
    item.detected_token = token
    item.status = "unmatched"
    item.file_size = path.stat().st_size if path.exists() else 0
    item.message = message
    item.seen_at = utcnow()
    if existing is None:
        session.add(item)
    await session.flush()


async def scan_inbox(session: AsyncSession) -> list[str]:
    """Walk the inbox and file anything that can be matched."""
    notes: list[str] = []
    root = inbox_root()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    for path in files:
        note = await ingest_path(session, path)
        if note:
            notes.append(note)
    return notes


async def assign_inbox_item(
    session: AsyncSession, item_id: int, job_id: int
) -> str:
    item = await session.get(InboxItem, item_id)
    if item is None or item.status != "unmatched":
        raise ManualAssistError("That file is no longer waiting to be assigned.")
    path = Path(item.file_path)
    if not path.exists() or not is_within(path.resolve(), inbox_root()):
        raise ManualAssistError("The file is no longer in the inbox.")

    job = await session.get(Job, job_id)
    if job is None or job.status != JobStatus.AWAITING_MANUAL:
        raise ManualAssistError("Pick a clip that is still waiting on a worksheet.")

    ok, message = await _file_job(session, job, path)
    if not ok:
        raise ManualAssistError(message)
    item.matched_job_id = job.id
    item.status = "matched"
    item.message = message
    item.seen_at = utcnow()
    await session.flush()
    return message


async def dismiss_inbox_item(session: AsyncSession, item_id: int) -> str:
    item = await session.get(InboxItem, item_id)
    if item is None:
        raise ManualAssistError("That inbox entry no longer exists.")
    item.status = "dismissed"
    item.message = "Left in the inbox folder and ignored."
    await session.flush()
    return f"Ignored {Path(item.file_path).name}. The file is still in the inbox."


async def unmatched_items(session: AsyncSession) -> list[InboxItem]:
    return list(
        (
            await session.execute(
                select(InboxItem)
                .where(InboxItem.status == "unmatched")
                .order_by(InboxItem.seen_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def new_images(session: AsyncSession, limit: int = 40) -> list[Image]:
    return list(
        (
            await session.execute(
                select(Image)
                .where(Image.status == ImageStatus.NEW, Image.duplicate_of_id.is_(None))
                .order_by(Image.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def screen_context(session: AsyncSession, site_key: str | None) -> dict[str, object]:
    views = await site_views(session)
    selected = next((view for view in views if view.site.key == site_key), None)
    if selected is None and views:
        selected = max(views, key=lambda view: len(view.waiting))
        if not selected.waiting:
            selected = views[0]

    images_by_id: dict[int, Image] = {}
    if selected and selected.waiting:
        ids = [job.image_id for job in selected.waiting]
        rows = (
            await session.execute(select(Image).where(Image.id.in_(ids)))
        ).scalars().all()
        images_by_id = {image.id: image for image in rows}

    return {
        "sites": views,
        "selected": selected,
        "images": images_by_id,
        "new_images": await new_images(session),
        "unmatched": await unmatched_items(session),
        "waiting_jobs": await _waiting_jobs(session),
        "inbox": str(inbox_root()),
        "intervals": list(RefreshInterval),
        "balance_sources": list(BalanceSource),
    }
