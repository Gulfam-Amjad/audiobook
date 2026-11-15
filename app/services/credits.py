"""Credit and entitlement tracking.

The hard requirement here is honesty. A balance is one of three things:

* ``api_verified`` - read from the provider during this session.
* ``user_entered`` - typed in by the user, shown as such, never as fact.
* ``unknown`` - the provider does not expose it and nobody has supplied one.

An unknown balance never blocks a free generation (that would make the app
unusable with providers that hide their balance) but it also never counts as
capacity in the planner. Resets are equally strict: the app only believes a
refresh happened when the provider states a reset time that has passed, or
the user records one. It never assumes midnight did it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import BalanceSource, JobStatus, RefreshInterval, VerificationStatus
from app.models import Account, Entitlement, Job, Provider
from app.providers import registry
from app.providers.base import CostEstimate
from app.services.timeutil import (
    has_reset_elapsed,
    next_utc_day,
    next_utc_month,
    start_of_utc_day,
    start_of_utc_month,
    utcnow,
)

log = logging.getLogger(__name__)


@dataclass
class CapacityVerdict:
    allowed: bool
    reason: str
    permanent: bool = False
    #: ``None`` when the balance is genuinely unknown.
    remaining_units: float | None = None
    unit: str = "credits"


async def entitlements_for(session: AsyncSession, account_id: int) -> list[Entitlement]:
    return list(
        (
            await session.execute(
                select(Entitlement)
                .where(Entitlement.account_id == account_id)
                .order_by(Entitlement.is_paid, Entitlement.id)
            )
        )
        .scalars()
        .all()
    )


async def ensure_entitlement(
    session: AsyncSession, account: Account, name: str = "default"
) -> Entitlement:
    existing = (
        await session.execute(
            select(Entitlement).where(
                Entitlement.account_id == account.id, Entitlement.name == name
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    caps = registry.get_capabilities(account.provider_key)
    row = Entitlement(
        account_id=account.id,
        name=name,
        unit="credits",
        balance=None,
        source=BalanceSource.UNKNOWN,
        note=(caps.free_tier_note if caps else "") or None,
    )
    session.add(row)
    await session.flush()
    return row


async def set_manual_balance(
    session: AsyncSession,
    entitlement_id: int,
    *,
    balance: float | None,
    unit: str,
    refresh_interval: str,
    next_reset_at: datetime | None = None,
    expires_at: datetime | None = None,
    note: str | None = None,
    unit_cost_usd: float | None = None,
    is_paid: bool = False,
) -> Entitlement:
    """Record a figure the user supplied, flagged as such."""
    row = await session.get(Entitlement, entitlement_id)
    if row is None:
        raise ValueError("Entitlement not found.")

    row.balance = balance
    row.unit = unit or "credits"
    row.refresh_interval = refresh_interval or RefreshInterval.NONE
    row.is_paid = is_paid
    row.unit_cost_usd = unit_cost_usd
    row.expires_at = expires_at
    row.note = note
    row.source = BalanceSource.USER_ENTERED if balance is not None else BalanceSource.UNKNOWN
    row.last_refresh_at = utcnow()

    # Derive the next reset only when the user chose a recurring interval and
    # did not state the boundary themselves.
    if next_reset_at is not None:
        row.next_reset_at = next_reset_at
    elif refresh_interval == RefreshInterval.DAILY:
        row.next_reset_at = next_utc_day()
    elif refresh_interval == RefreshInterval.MONTHLY:
        row.next_reset_at = next_utc_month()
    else:
        row.next_reset_at = None

    await session.flush()
    return row


async def refresh_from_provider(session: AsyncSession, account: Account) -> CapacityVerdict:
    """Ask the provider for a real balance, if it offers one."""
    adapter = registry.get_adapter(account.provider_key)
    if adapter is None:
        return CapacityVerdict(False, "Provider adapter is unavailable.", permanent=True)

    caps = adapter.capabilities()
    if not caps.exposes_balance:
        entitlement = await ensure_entitlement(session, account)
        if entitlement.source != BalanceSource.USER_ENTERED:
            entitlement.source = BalanceSource.UNKNOWN
            entitlement.balance = None
            entitlement.note = (
                f"{caps.display_name} does not expose a balance through its API. "
                "Enter one manually if you want it tracked."
            )
            await session.flush()
        return CapacityVerdict(
            True,
            f"{caps.display_name} does not report balances; the figure stays manually managed.",
            remaining_units=entitlement.balance,
            unit=entitlement.unit,
        )

    result = await adapter.fetch_balance()
    entitlement = await ensure_entitlement(session, account)

    if result.source == BalanceSource.UNKNOWN:
        if entitlement.source != BalanceSource.USER_ENTERED:
            entitlement.source = BalanceSource.UNKNOWN
            entitlement.balance = None
        entitlement.note = result.message or entitlement.note
        await session.flush()
        return CapacityVerdict(True, result.message or "Balance unknown.", remaining_units=None)

    entitlement.balance = result.balance
    entitlement.unit = result.unit
    entitlement.source = BalanceSource.API_VERIFIED
    entitlement.last_refresh_at = utcnow()
    if result.next_reset_at:
        entitlement.next_reset_at = result.next_reset_at
    if result.expires_at:
        entitlement.expires_at = result.expires_at
    entitlement.note = result.message or None
    await session.flush()

    return CapacityVerdict(
        True,
        f"Balance read from {caps.display_name}.",
        remaining_units=result.balance,
        unit=result.unit,
    )


async def apply_known_resets(session: AsyncSession, now: datetime | None = None) -> int:
    """Roll forward reset boundaries the provider or user actually stated.

    Only the *boundary* moves. The balance is cleared to unknown rather than
    refilled, because the app has no basis for asserting the new figure - it
    must be re-read or re-entered.
    """
    now = now or utcnow()
    rows = (
        await session.execute(
            select(Entitlement).where(
                Entitlement.refresh_interval.in_(
                    (RefreshInterval.DAILY, RefreshInterval.MONTHLY)
                ),
                Entitlement.next_reset_at.is_not(None),
            )
        )
    ).scalars().all()

    changed = 0
    for row in rows:
        if not has_reset_elapsed(row.next_reset_at, now):
            continue
        row.next_reset_at = (
            next_utc_day(now)
            if row.refresh_interval == RefreshInterval.DAILY
            else next_utc_month(now)
        )
        if row.source == BalanceSource.API_VERIFIED:
            # Force a re-read rather than guessing the refilled amount.
            row.source = BalanceSource.UNKNOWN
            row.balance = None
            row.note = "Reset time passed; balance needs re-reading from the provider."
        else:
            row.note = (
                "Reset time passed. The previous figure was your own entry, so it is no "
                "longer trusted - re-enter the current balance."
            )
            row.source = BalanceSource.UNKNOWN
            row.balance = None
        changed += 1

    if changed:
        await session.flush()
        log.info("Applied stated credit resets", extra={"entitlements": changed})
    return changed


async def check_capacity(
    session: AsyncSession, account: Account, estimate: CostEstimate
) -> CapacityVerdict:
    """Can this account afford the request from a non-paid allowance?"""
    rows = await entitlements_for(session, account.id)
    if not rows:
        return CapacityVerdict(
            True,
            "No allowance is being tracked for this account, so it cannot be checked.",
            remaining_units=None,
        )

    if estimate.units is None:
        known = [r for r in rows if r.balance is not None and r.balance > 0]
        if known:
            return CapacityVerdict(
                True,
                "Cost in provider units is unknown; a positive balance is recorded.",
                remaining_units=known[0].balance,
                unit=known[0].unit,
            )
        return CapacityVerdict(
            True, "Neither the cost nor the balance is known for this account."
        )

    now = utcnow()
    usable = [
        row
        for row in rows
        if row.balance is not None
        and (row.expires_at is None or row.expires_at > now)
        and row.unit == estimate.unit
    ]

    if not usable:
        matching_unit = [r for r in rows if r.unit == estimate.unit]
        if not matching_unit:
            return CapacityVerdict(
                True,
                (
                    f"No allowance recorded in '{estimate.unit}', so the cost cannot be "
                    "checked against a balance."
                ),
            )
        expired = [r for r in matching_unit if r.expires_at and r.expires_at <= now]
        if expired:
            return CapacityVerdict(
                False,
                f"The tracked allowance for '{account.label}' expired on "
                f"{expired[0].expires_at:%Y-%m-%d}.",
                permanent=True,
            )
        return CapacityVerdict(
            True, "Balance is unknown for this account, so it cannot be checked."
        )

    total = sum(row.balance or 0.0 for row in usable)
    if total < estimate.units:
        sources = {row.source for row in usable}
        qualifier = (
            " (based on a figure you entered)"
            if sources == {BalanceSource.USER_ENTERED}
            else ""
        )
        return CapacityVerdict(
            False,
            (
                f"'{account.label}' has {total:,.2f} {estimate.unit} left{qualifier} but this "
                f"job needs {estimate.units:,.2f}."
            ),
            permanent=True,
            remaining_units=total,
            unit=estimate.unit,
        )

    return CapacityVerdict(
        True,
        f"{total:,.2f} {estimate.unit} available, {estimate.units:,.2f} required.",
        remaining_units=total,
        unit=estimate.unit,
    )


async def consume(
    session: AsyncSession, account: Account, units: float, unit: str
) -> float | None:
    """Draw down a locally tracked balance after a successful generation.

    Only balances the app is responsible for are decremented. An
    ``api_verified`` figure is left alone and re-read instead, so the app's
    arithmetic can never drift away from the provider's own number.
    """
    if units <= 0:
        return None
    rows = [
        row
        for row in await entitlements_for(session, account.id)
        if row.unit == unit
        and row.balance is not None
        and row.source == BalanceSource.USER_ENTERED
    ]
    if not rows:
        return None

    remaining = units
    for row in rows:
        if remaining <= 0:
            break
        take = min(row.balance or 0.0, remaining)
        row.balance = max(0.0, (row.balance or 0.0) - take)
        remaining -= take
    await session.flush()
    return sum(row.balance or 0.0 for row in rows)


# ---------------------------------------------------------------------------
# Usage reporting
# ---------------------------------------------------------------------------


@dataclass
class AccountUsage:
    account_id: int
    successes: int = 0
    failures: int = 0
    jobs_today: int = 0
    jobs_month: int = 0
    seconds_today: float = 0.0

    @property
    def total_finished(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float | None:
        if self.total_finished == 0:
            return None
        return self.successes / self.total_finished


async def usage_for_accounts(session: AsyncSession) -> dict[int, AccountUsage]:
    day_start = start_of_utc_day()
    month_start = start_of_utc_month()
    usage: dict[int, AccountUsage] = {}

    def row_for(account_id: int) -> AccountUsage:
        return usage.setdefault(account_id, AccountUsage(account_id=account_id))

    outcome_rows = (
        await session.execute(
            select(Job.account_id, Job.status, func.count())
            .where(
                Job.account_id.is_not(None),
                Job.status.in_((JobStatus.COMPLETED, JobStatus.FAILED)),
            )
            .group_by(Job.account_id, Job.status)
        )
    ).all()
    for account_id, status, count in outcome_rows:
        entry = row_for(account_id)
        if status == JobStatus.COMPLETED:
            entry.successes = count
        else:
            entry.failures = count

    for since, attribute in ((day_start, "jobs_today"), (month_start, "jobs_month")):
        rows = (
            await session.execute(
                select(Job.account_id, func.count())
                .where(Job.account_id.is_not(None), Job.created_at >= since)
                .group_by(Job.account_id)
            )
        ).all()
        for account_id, count in rows:
            setattr(row_for(account_id), attribute, count)

    return usage


async def accounts_with_context(session: AsyncSession) -> list[dict[str, object]]:
    """Everything the Credits screen needs, in one pass."""
    accounts = (
        await session.execute(select(Account).order_by(Account.provider_key, Account.label))
    ).scalars().all()
    providers = {
        row.key: row for row in (await session.execute(select(Provider))).scalars().all()
    }
    usage = await usage_for_accounts(session)

    result: list[dict[str, object]] = []
    for account in accounts:
        provider = providers.get(account.provider_key)
        caps = registry.get_capabilities(account.provider_key)
        result.append(
            {
                "account": account,
                "provider": provider,
                "provider_name": provider.display_name if provider else account.provider_key,
                "kind": caps.kind if caps else "",
                "exposes_balance": caps.exposes_balance if caps else False,
                "free_tier_note": caps.free_tier_note if caps else "",
                "entitlements": await entitlements_for(session, account.id),
                "usage": usage.get(account.id, AccountUsage(account_id=account.id)),
                "verified": (
                    provider is not None
                    and provider.verification_status == VerificationStatus.VERIFIED
                ),
            }
        )
    return result
