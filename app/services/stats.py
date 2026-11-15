"""Aggregate figures for the overview dashboard.

The headline number is *generated AI motion* - the summed real duration of
clips a provider actually produced. It is deliberately kept separate from
assembled output length, because concatenating the same clips into a longer
montage creates no new generated video and must never inflate the figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    ACTIVE_JOB_STATES,
    ImageStatus,
    JobStatus,
    VerificationStatus,
)
from app.models import Account, Image, InboxItem, Job, Output, Provider, SpendEntry
from app.services import settings_store
from app.services.timeutil import start_of_utc_day, start_of_utc_month, utcnow


@dataclass
class DashboardStats:
    images_total: int = 0
    images_by_status: dict[str, int] = field(default_factory=dict)
    images_missing: int = 0

    jobs_by_status: dict[str, int] = field(default_factory=dict)
    jobs_pending: int = 0
    jobs_running: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    jobs_awaiting_manual: int = 0
    jobs_uncertain: int = 0

    clips_total: int = 0
    clips_today: int = 0
    #: Real duration of generated clips, in seconds. ``None`` entries are
    #: excluded, so this is a floor when FFprobe is unavailable.
    generated_seconds_total: float = 0.0
    generated_seconds_today: float = 0.0
    #: Clips whose duration could not be probed, so the total understates.
    clips_without_duration: int = 0

    daily_target_minutes: float = 20.0
    queue_paused: bool = True

    providers_total: int = 0
    providers_verified: int = 0
    providers_configured: int = 0
    providers_enabled: int = 0

    paid_enabled: bool = False
    monthly_cap_usd: float = 0.0
    spend_month_usd: float = 0.0
    spend_estimated_month_usd: float = 0.0

    inbox_unmatched: int = 0

    @property
    def generated_minutes_today(self) -> float:
        return self.generated_seconds_today / 60.0

    @property
    def target_progress_pct(self) -> float:
        target_seconds = self.daily_target_minutes * 60.0
        if target_seconds <= 0:
            return 0.0
        return min(100.0, 100.0 * self.generated_seconds_today / target_seconds)

    @property
    def shortfall_seconds_today(self) -> float:
        return max(0.0, self.daily_target_minutes * 60.0 - self.generated_seconds_today)


async def collect(session: AsyncSession, now: datetime | None = None) -> DashboardStats:
    now = now or utcnow()
    day_start = start_of_utc_day(now)
    month_start = start_of_utc_month(now)
    stats = DashboardStats()

    image_rows = (
        await session.execute(select(Image.status, func.count()).group_by(Image.status))
    ).all()
    stats.images_by_status = {status: count for status, count in image_rows}
    stats.images_total = sum(stats.images_by_status.values())
    stats.images_missing = stats.images_by_status.get(ImageStatus.MISSING, 0)

    job_rows = (
        await session.execute(select(Job.status, func.count()).group_by(Job.status))
    ).all()
    stats.jobs_by_status = {status: count for status, count in job_rows}
    stats.jobs_pending = stats.jobs_by_status.get(JobStatus.QUEUED, 0)
    stats.jobs_running = sum(stats.jobs_by_status.get(s, 0) for s in ACTIVE_JOB_STATES)
    stats.jobs_completed = stats.jobs_by_status.get(JobStatus.COMPLETED, 0)
    stats.jobs_failed = stats.jobs_by_status.get(JobStatus.FAILED, 0)
    stats.jobs_awaiting_manual = stats.jobs_by_status.get(JobStatus.AWAITING_MANUAL, 0)
    stats.jobs_uncertain = stats.jobs_by_status.get(JobStatus.UNCERTAIN, 0)

    totals = (
        await session.execute(
            select(func.count(Output.id), func.coalesce(func.sum(Output.duration_s), 0.0))
        )
    ).one()
    stats.clips_total = int(totals[0] or 0)
    stats.generated_seconds_total = float(totals[1] or 0.0)

    today = (
        await session.execute(
            select(func.count(Output.id), func.coalesce(func.sum(Output.duration_s), 0.0)).where(
                Output.downloaded_at >= day_start
            )
        )
    ).one()
    stats.clips_today = int(today[0] or 0)
    stats.generated_seconds_today = float(today[1] or 0.0)

    stats.clips_without_duration = int(
        (
            await session.execute(
                select(func.count(Output.id)).where(Output.duration_s.is_(None))
            )
        ).scalar()
        or 0
    )

    provider_rows = (await session.execute(select(Provider))).scalars().all()
    stats.providers_total = len(provider_rows)
    stats.providers_verified = sum(
        1 for p in provider_rows if p.verification_status == VerificationStatus.VERIFIED
    )
    stats.providers_configured = sum(
        1 for p in provider_rows if p.verification_status != VerificationStatus.NOT_CONFIGURED
    )
    stats.providers_enabled = sum(1 for p in provider_rows if p.enabled)

    spend_rows = (
        await session.execute(
            select(SpendEntry.kind, func.coalesce(func.sum(SpendEntry.amount_usd), 0.0))
            .where(SpendEntry.ts >= month_start)
            .group_by(SpendEntry.kind)
        )
    ).all()
    spend = {kind: float(total or 0.0) for kind, total in spend_rows}
    stats.spend_month_usd = spend.get("actual", 0.0)
    stats.spend_estimated_month_usd = spend.get("estimate", 0.0)

    stats.inbox_unmatched = int(
        (
            await session.execute(
                select(func.count(InboxItem.id)).where(InboxItem.status == "unmatched")
            )
        ).scalar()
        or 0
    )

    app_settings = await settings_store.get_all(session)
    stats.daily_target_minutes = float(app_settings["daily_target_minutes"])
    stats.queue_paused = bool(app_settings["queue_paused"])
    stats.paid_enabled = bool(app_settings["paid_enabled"])
    stats.monthly_cap_usd = float(app_settings["monthly_spend_cap_usd"])

    return stats


async def provider_health(session: AsyncSession) -> list[dict[str, object]]:
    """Per-provider status rows for the overview and diagnostics screens."""
    providers = (await session.execute(select(Provider))).scalars().all()
    account_counts = dict(
        (
            await session.execute(
                select(Account.provider_key, func.count()).group_by(Account.provider_key)
            )
        ).all()
    )

    active_counts = dict(
        (
            await session.execute(
                select(Job.provider_key, func.count())
                .where(Job.status.in_(tuple(ACTIVE_JOB_STATES)))
                .group_by(Job.provider_key)
            )
        ).all()
    )

    rows: list[dict[str, object]] = []
    for provider in sorted(providers, key=lambda p: (not p.enabled, p.priority, p.key)):
        rows.append(
            {
                "key": provider.key,
                "name": provider.display_name,
                "kind": provider.kind,
                "enabled": provider.enabled,
                "status": provider.verification_status,
                "message": provider.last_check_message or "",
                "accounts": account_counts.get(provider.key, 0),
                "active_jobs": active_counts.get(provider.key, 0),
                "verified_at": provider.verified_at,
            }
        )
    return rows


async def success_rate(session: AsyncSession, provider_key: str) -> tuple[int, int]:
    """Return ``(successes, failures)`` for a provider's finished jobs."""
    rows = (
        await session.execute(
            select(Job.status, func.count())
            .where(
                Job.provider_key == provider_key,
                Job.status.in_((JobStatus.COMPLETED, JobStatus.FAILED)),
            )
            .group_by(Job.status)
        )
    ).all()
    counts = {status: count for status, count in rows}
    return counts.get(JobStatus.COMPLETED, 0), counts.get(JobStatus.FAILED, 0)
