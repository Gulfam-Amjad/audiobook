"""Spending controls.

The guiding rule from the brief: the application must never consume paid
credits unintentionally. Three independent switches all have to be on before
a single cent can be committed:

1. ``paid_enabled`` in Settings (off by default).
2. A monthly cap greater than zero.
3. ``paid_authorized`` on the specific account.

A job whose estimated cost is zero - the mock provider, or a provider running
purely on a free grant - is unaffected by any of this.

Because a provider's real charge is only known afterwards, the cap is
enforced against *committed* spend: the confirmed charge for a job where one
exists, otherwise its estimate. That is deliberately conservative; it can
hold back a job that would have been slightly cheaper than predicted, which
is the right direction to err.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Job, SpendEntry
from app.providers.base import CostEstimate
from app.services import settings_store
from app.services.timeutil import start_of_utc_month, utcnow

log = logging.getLogger(__name__)


@dataclass
class BudgetVerdict:
    allowed: bool
    reason: str
    #: True when the block is permanent for this configuration, so the
    #: scheduler should stop rather than retry.
    permanent: bool = False
    committed_usd: float = 0.0
    cap_usd: float = 0.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.committed_usd)


async def committed_this_month(session: AsyncSession, now: datetime | None = None) -> float:
    """Confirmed charges, plus estimates for jobs with no confirmation yet."""
    month_start = start_of_utc_month(now or utcnow())

    actual_total = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(SpendEntry.amount_usd), 0.0)).where(
                    SpendEntry.ts >= month_start, SpendEntry.kind == "actual"
                )
            )
        ).scalar()
        or 0.0
    )

    # Jobs whose actual cost has landed are represented by the figure above,
    # so their estimates must not be counted a second time.
    confirmed_job_ids = select(SpendEntry.job_id).where(
        SpendEntry.ts >= month_start,
        SpendEntry.kind == "actual",
        SpendEntry.job_id.is_not(None),
    )
    estimate_total = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(SpendEntry.amount_usd), 0.0)).where(
                    SpendEntry.ts >= month_start,
                    SpendEntry.kind == "estimate",
                    SpendEntry.job_id.not_in(confirmed_job_ids),
                )
            )
        ).scalar()
        or 0.0
    )

    return round(actual_total + estimate_total, 6)


async def check_budget(
    session: AsyncSession,
    *,
    estimate: CostEstimate,
    account: Account | None,
    provider_key: str,
) -> BudgetVerdict:
    """Decide whether one generation may proceed."""
    app_settings = await settings_store.get_all(session)
    cap = float(app_settings["monthly_spend_cap_usd"])
    paid_enabled = bool(app_settings["paid_enabled"])
    committed = await committed_this_month(session)

    if estimate.confidence == "unknown":
        return BudgetVerdict(
            allowed=False,
            reason=(
                f"No published price is known for this {provider_key} request, so the cost "
                "cannot be bounded. Pick a model and resolution the adapter has a verified "
                "price for, or run it on a provider with a known rate."
            ),
            permanent=True,
            committed_usd=committed,
            cap_usd=cap,
        )

    if estimate.usd <= 0:
        return BudgetVerdict(
            allowed=True,
            reason="No cost: this generation draws on a free allowance.",
            committed_usd=committed,
            cap_usd=cap,
        )

    if not paid_enabled:
        return BudgetVerdict(
            allowed=False,
            reason=(
                f"This job would cost about ${estimate.usd:.4f} and paid generation is "
                "switched off. Enable it in Settings and set a monthly cap if you want to "
                "spend money."
            ),
            permanent=True,
            committed_usd=committed,
            cap_usd=cap,
        )

    if cap <= 0:
        return BudgetVerdict(
            allowed=False,
            reason=(
                "Paid generation is enabled but the monthly cap is still $0, so nothing "
                "can be spent. Set a cap in Settings."
            ),
            permanent=True,
            committed_usd=committed,
            cap_usd=cap,
        )

    if account is not None and not account.paid_authorized:
        return BudgetVerdict(
            allowed=False,
            reason=(
                f"Account '{account.label}' on {provider_key} is not authorised for paid "
                "use. Authorise it on the Credits screen to spend against it."
            ),
            permanent=True,
            committed_usd=committed,
            cap_usd=cap,
        )

    if committed + estimate.usd > cap:
        return BudgetVerdict(
            allowed=False,
            reason=(
                f"This job's estimated ${estimate.usd:.4f} would take the month to "
                f"${committed + estimate.usd:.2f}, over the ${cap:.2f} cap. "
                f"${max(0.0, cap - committed):.2f} remains."
            ),
            permanent=True,
            committed_usd=committed,
            cap_usd=cap,
        )

    return BudgetVerdict(
        allowed=True,
        reason=(
            f"Within budget: ${estimate.usd:.4f} estimated, "
            f"${committed:.2f} of ${cap:.2f} committed this month."
        ),
        committed_usd=committed,
        cap_usd=cap,
    )


async def record_estimate(
    session: AsyncSession, job: Job, estimate: CostEstimate, *, note: str = ""
) -> SpendEntry | None:
    """Log the predicted cost of a submission."""
    if estimate.usd <= 0:
        return None
    entry = SpendEntry(
        account_id=job.account_id,
        job_id=job.id,
        provider_key=job.provider_key,
        amount_usd=estimate.usd,
        units=estimate.units,
        unit=estimate.unit,
        kind="estimate",
        note=note or estimate.basis,
    )
    session.add(entry)
    await session.flush()
    return entry


async def record_actual(
    session: AsyncSession,
    job: Job,
    amount_usd: float,
    *,
    units: float | None = None,
    unit: str | None = None,
    note: str = "",
) -> SpendEntry:
    """Log a charge the provider actually confirmed."""
    entry = SpendEntry(
        account_id=job.account_id,
        job_id=job.id,
        provider_key=job.provider_key,
        amount_usd=amount_usd,
        units=units,
        unit=unit,
        kind="actual",
        note=note or "Reported by the provider.",
    )
    session.add(entry)
    await session.flush()
    log.info(
        "Confirmed provider charge",
        extra={"job_id": job.id, "provider": job.provider_key, "usd": amount_usd},
    )
    return entry


async def release_estimate(session: AsyncSession, job: Job, reason: str) -> None:
    """Cancel a reserved estimate when a submission never happened.

    Recorded as a negative entry rather than a delete so the history of what
    the app intended to spend stays intact.
    """
    reserved = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(SpendEntry.amount_usd), 0.0)).where(
                    SpendEntry.job_id == job.id, SpendEntry.kind == "estimate"
                )
            )
        ).scalar()
        or 0.0
    )
    if reserved <= 0:
        return
    session.add(
        SpendEntry(
            account_id=job.account_id,
            job_id=job.id,
            provider_key=job.provider_key,
            amount_usd=-reserved,
            kind="estimate",
            note=f"Reservation released: {reason}",
        )
    )
    await session.flush()


@dataclass
class SpendSummary:
    month_committed_usd: float
    month_actual_usd: float
    month_estimated_usd: float
    cap_usd: float
    paid_enabled: bool

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.month_committed_usd)

    @property
    def cap_used_pct(self) -> float:
        if self.cap_usd <= 0:
            return 0.0
        return min(100.0, 100.0 * self.month_committed_usd / self.cap_usd)


async def summary(session: AsyncSession) -> SpendSummary:
    app_settings = await settings_store.get_all(session)
    month_start = start_of_utc_month()
    rows = (
        await session.execute(
            select(SpendEntry.kind, func.coalesce(func.sum(SpendEntry.amount_usd), 0.0))
            .where(SpendEntry.ts >= month_start)
            .group_by(SpendEntry.kind)
        )
    ).all()
    totals = {kind: float(total or 0.0) for kind, total in rows}
    return SpendSummary(
        month_committed_usd=await committed_this_month(session),
        month_actual_usd=totals.get("actual", 0.0),
        month_estimated_usd=totals.get("estimate", 0.0),
        cap_usd=float(app_settings["monthly_spend_cap_usd"]),
        paid_enabled=bool(app_settings["paid_enabled"]),
    )
