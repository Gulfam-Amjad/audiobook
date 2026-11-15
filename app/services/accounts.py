"""Actions the Credits screen can take on accounts and allowances.

Two rules shape everything here.

**A balance never becomes a fact by accident.** Every figure carries where it
came from. Typing one in records it as yours; reading one from a provider
records it as theirs; anything else stays unknown. The app will not infer a
figure from arithmetic it was not given.

**Nothing paid happens without a deliberate act.** Authorising an account for
paid use is a separate, explicit step from enabling the provider, and it
still does nothing unless paid generation is switched on globally and a
monthly cap is set. Switching the global toggle off does not quietly leave
accounts authorised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import BalanceSource, ProviderKind, RefreshInterval
from app.models import Account, Entitlement, Provider
from app.providers import registry
from app.services import credits, settings_store, spending
from app.services.timeutil import (
    has_reset_elapsed,
    next_utc_day,
    next_utc_month,
    utcnow,
)

log = logging.getLogger(__name__)

#: Units are free text because every provider counts differently, but the
#: common ones are offered so the same allowance is not recorded as both
#: "credit" and "credits" and then fail to match a cost estimate.
COMMON_UNITS = ("credits", "seconds", "tokens", "usd", "generations", "clips")

MAX_LABEL = 120
MAX_NOTE = 2000


class AccountError(Exception):
    """Something the user asked for cannot be done, with a reason to show."""


@dataclass(slots=True)
class ResetView:
    """How a reset boundary should be described, without inventing one."""

    when: datetime | None
    interval: str
    #: True when the stated boundary has passed and the balance is therefore
    #: stale rather than refilled.
    elapsed: bool

    @property
    def description(self) -> str:
        if self.interval == RefreshInterval.NONE or self.when is None:
            if self.interval == RefreshInterval.NONE:
                return "Does not refresh. This is a one-off allowance."
            return "No reset time recorded, so none is assumed."
        if self.elapsed:
            return (
                "The stated reset time has passed, so the figure below is out of date. "
                "Re-read or re-enter it."
            )
        return f"Next stated reset: {self.when:%Y-%m-%d %H:%M} UTC."


def _validate_unit(unit: str) -> str:
    cleaned = (unit or "").strip().lower()
    if not cleaned:
        return "credits"
    if len(cleaned) > 40:
        raise AccountError("Unit names must be 40 characters or fewer.")
    return cleaned


def _validate_balance(balance: float | None) -> float | None:
    if balance is None:
        return None
    if balance < 0:
        raise AccountError("A balance cannot be negative.")
    if balance > 1_000_000_000:
        raise AccountError("That balance is implausibly large; check the figure.")
    return float(balance)


async def list_accounts(session: AsyncSession) -> list[Account]:
    return list(
        (
            await session.execute(
                select(Account).order_by(Account.provider_key, Account.priority, Account.id)
            )
        )
        .scalars()
        .all()
    )


async def create_account(
    session: AsyncSession, provider_key: str, label: str
) -> Account:
    """Add a second account on a provider the user genuinely holds one with.

    This is not a way to multiply a free trial: providers forbid that, and
    the app says so on screen. It exists for people who legitimately have a
    separate work and personal key.
    """
    provider = await session.get(Provider, provider_key)
    if provider is None:
        raise AccountError(f"'{provider_key}' is not a known provider.")

    cleaned = (label or "").strip()
    if not cleaned:
        raise AccountError("Give the account a label so you can tell them apart.")
    if len(cleaned) > MAX_LABEL:
        raise AccountError(f"Labels must be {MAX_LABEL} characters or fewer.")

    clash = (
        await session.execute(
            select(Account.id).where(
                Account.provider_key == provider_key, Account.label == cleaned
            )
        )
    ).first()
    if clash:
        raise AccountError(f"{provider.display_name} already has an account called '{cleaned}'.")

    caps = registry.get_capabilities(provider_key)
    account = Account(
        provider_key=provider_key,
        label=cleaned,
        enabled=True,
        concurrency_limit=caps.default_concurrency if caps else 1,
        requests_per_minute=caps.default_requests_per_minute if caps else None,
        notes="Added by hand.",
    )
    session.add(account)
    await session.flush()
    await credits.ensure_entitlement(session, account)
    return account


async def set_account_enabled(
    session: AsyncSession, account_id: int, enabled: bool
) -> str:
    account = await _require(session, account_id)
    account.enabled = enabled
    state = "will receive jobs" if enabled else "will not receive jobs"
    return f"'{account.label}' {state}."


async def update_limits(
    session: AsyncSession,
    account_id: int,
    *,
    concurrency_limit: int | None,
    requests_per_minute: int | None,
    daily_job_cap: int | None,
) -> str:
    """Tighten the provider's documented limits, never loosen past sanity."""
    account = await _require(session, account_id)

    if concurrency_limit is not None and concurrency_limit < 1:
        raise AccountError("Concurrency must be at least 1, or blank for the default.")
    if requests_per_minute is not None and requests_per_minute < 1:
        raise AccountError("Requests per minute must be at least 1, or blank for no limit.")
    if daily_job_cap is not None and daily_job_cap < 0:
        raise AccountError("A daily cap cannot be negative. Use 0 to stop all jobs.")

    caps = registry.get_capabilities(account.provider_key)
    warning = ""
    if (
        caps is not None
        and concurrency_limit is not None
        and concurrency_limit > caps.default_concurrency
    ):
        warning = (
            f" Note that {caps.display_name} documents a limit of "
            f"{caps.default_concurrency}, so a higher figure is likely to be rejected."
        )

    account.concurrency_limit = concurrency_limit
    account.requests_per_minute = requests_per_minute
    account.daily_job_cap = daily_job_cap
    return f"Limits saved for '{account.label}'.{warning}"


async def set_paid_authorized(
    session: AsyncSession, account_id: int, authorized: bool
) -> str:
    """Authorise, or withdraw authorisation for, real spending on one account.

    Authorising is reported together with the state of the two global gates,
    because an account authorised while paid generation is off still cannot
    spend - and the user should not be left thinking otherwise.
    """
    account = await _require(session, account_id)
    caps = registry.get_capabilities(account.provider_key)
    name = caps.display_name if caps else account.provider_key

    if not authorized:
        account.paid_authorized = False
        log.info("paid authorisation withdrawn", extra={"account_id": account.id})
        return f"'{account.label}' can no longer be charged. Free allowances still work."

    if caps is not None and caps.kind == ProviderKind.MANUAL:
        # Manual assist has no API to bill against, and the site credits it
        # uses cannot be bought from here. Letting it be "authorised" would
        # imply this app can unlock paid generation on those sites.
        raise AccountError(
            f"{name} has no API to charge, so there is nothing to authorise. Buying "
            "credits on those sites is something you do on the site itself."
        )

    account.paid_authorized = True
    log.info("paid authorisation granted", extra={"account_id": account.id})

    app_settings = await settings_store.get_all(session)
    cap = float(app_settings["monthly_spend_cap_usd"])
    if not app_settings["paid_enabled"]:
        return (
            f"'{account.label}' is authorised, but paid generation is still switched off "
            "globally, so nothing can be charged yet. Turn it on in Settings."
        )
    if cap <= 0:
        return (
            f"'{account.label}' is authorised and paid generation is on, but the monthly "
            "cap is $0, so nothing can be charged yet. Set a cap in Settings."
        )
    return (
        f"'{account.label}' is authorised to spend real money on {name}, up to the "
        f"${cap:,.2f} monthly cap shared by every provider."
    )


async def revoke_all_paid_authorisations(session: AsyncSession) -> int:
    """Clear every account authorisation. Used when paid generation is turned off.

    Leaving them set would mean re-enabling the global switch silently
    re-arms spending on accounts the user may have forgotten about.
    """
    rows = (
        await session.execute(select(Account).where(Account.paid_authorized.is_(True)))
    ).scalars().all()
    for account in rows:
        account.paid_authorized = False
    if rows:
        await session.flush()
        log.info("all paid authorisations revoked", extra={"accounts": len(rows)})
    return len(rows)


# ---------------------------------------------------------------------------
# Allowances
# ---------------------------------------------------------------------------


async def add_entitlement(
    session: AsyncSession, account_id: int, name: str
) -> Entitlement:
    """Track a second allowance, such as a free grant beside a paid balance."""
    account = await _require(session, account_id)
    cleaned = (name or "").strip() or "default"
    if len(cleaned) > MAX_LABEL:
        raise AccountError(f"Names must be {MAX_LABEL} characters or fewer.")

    clash = (
        await session.execute(
            select(Entitlement.id).where(
                Entitlement.account_id == account.id, Entitlement.name == cleaned
            )
        )
    ).first()
    if clash:
        raise AccountError(f"'{account.label}' already tracks an allowance called '{cleaned}'.")

    row = Entitlement(
        account_id=account.id,
        name=cleaned,
        unit="credits",
        balance=None,
        source=BalanceSource.UNKNOWN,
    )
    session.add(row)
    await session.flush()
    return row


async def save_balance(
    session: AsyncSession,
    entitlement_id: int,
    *,
    balance: float | None,
    unit: str,
    refresh_interval: str,
    expires_at: datetime | None = None,
    unit_cost_usd: float | None = None,
    is_paid: bool = False,
    note: str | None = None,
) -> str:
    """Record the figure the user read off the provider's own dashboard."""
    row = await session.get(Entitlement, entitlement_id)
    if row is None:
        raise AccountError("That allowance no longer exists.")

    if refresh_interval not in set(RefreshInterval):
        raise AccountError(f"'{refresh_interval}' is not a refresh interval this app knows.")
    if note is not None and len(note) > MAX_NOTE:
        raise AccountError(f"Notes must be {MAX_NOTE} characters or fewer.")
    if unit_cost_usd is not None and unit_cost_usd < 0:
        raise AccountError("A unit cost cannot be negative.")

    if row.source == BalanceSource.API_VERIFIED and balance is not None:
        # Overwriting a figure the provider itself gave us with a typed one
        # is allowed, but it is a downgrade and the message says so.
        downgrade = (
            " This replaces a balance read from the provider, so it is now recorded as "
            "your figure rather than a verified one."
        )
    else:
        downgrade = ""

    await credits.set_manual_balance(
        session,
        entitlement_id,
        balance=_validate_balance(balance),
        unit=_validate_unit(unit),
        refresh_interval=refresh_interval,
        expires_at=expires_at,
        note=(note or "").strip() or None,
        unit_cost_usd=unit_cost_usd,
        is_paid=is_paid,
    )

    if balance is None:
        return f"'{row.name}' is now recorded as unknown, and will not count as capacity.{downgrade}"
    return (
        f"'{row.name}' recorded as {balance:,.0f} {row.unit} that you entered. The app will "
        f"not treat this as verified.{downgrade}"
    )


async def delete_entitlement(session: AsyncSession, entitlement_id: int) -> str:
    row = await session.get(Entitlement, entitlement_id)
    if row is None:
        raise AccountError("That allowance no longer exists.")
    name = row.name
    await session.delete(row)
    await session.flush()
    return f"Stopped tracking '{name}'."


async def refresh_balance(session: AsyncSession, account_id: int) -> str:
    """Ask the provider for the real figure, if it publishes one."""
    account = await _require(session, account_id)
    verdict = await credits.refresh_from_provider(session, account)
    return verdict.reason


async def record_manual_reset(session: AsyncSession, entitlement_id: int) -> str:
    """Note that the user saw the allowance refill, and move the boundary.

    The balance is deliberately not refilled to a guessed number: only the
    user can say what it actually went back to, so it goes to unknown and
    waits for them.
    """
    row = await session.get(Entitlement, entitlement_id)
    if row is None:
        raise AccountError("That allowance no longer exists.")

    now = utcnow()
    if row.refresh_interval == RefreshInterval.DAILY:
        row.next_reset_at = next_utc_day(now)
    elif row.refresh_interval == RefreshInterval.MONTHLY:
        row.next_reset_at = next_utc_month(now)
    elif row.next_reset_at is not None:
        # An "other" interval with a known boundary: step it by the same
        # distance the user originally described rather than guessing.
        row.next_reset_at = row.next_reset_at + timedelta(days=1)

    row.last_refresh_at = now
    row.balance = None
    row.source = BalanceSource.UNKNOWN
    row.note = "You recorded a reset. Enter the new balance when you have read it."
    await session.flush()
    return (
        f"Reset recorded for '{row.name}'. The balance is unknown until you enter the "
        "new figure, because the app cannot know what it refilled to."
    )


async def _require(session: AsyncSession, account_id: int) -> Account:
    account = await session.get(Account, account_id)
    if account is None:
        raise AccountError("That account no longer exists.")
    return account


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def reset_view(entitlement: Entitlement, now: datetime | None = None) -> ResetView:
    return ResetView(
        when=entitlement.next_reset_at,
        interval=entitlement.refresh_interval,
        elapsed=has_reset_elapsed(entitlement.next_reset_at, now or utcnow()),
    )


async def screen_context(session: AsyncSession) -> dict[str, object]:
    """Everything the Credits screen renders, gathered once.

    Reset boundaries that have already passed are applied first, so the page
    never shows a stale "next reset" in the past - but applying one only
    moves the boundary and blanks the figure. It never refills a balance.
    """
    await credits.apply_known_resets(session)
    rows = await credits.accounts_with_context(session)
    app_settings = await settings_store.get_all(session)
    spend = await spending.summary(session)

    now = utcnow()
    for row in rows:
        entitlements = row["entitlements"]
        row["resets"] = {e.id: reset_view(e, now) for e in entitlements}
        row["unknown_count"] = sum(
            1 for e in entitlements if e.source == BalanceSource.UNKNOWN
        )

    providers_without_accounts = [
        caps
        for key, caps in registry.all_capabilities().items()
        if caps.kind == ProviderKind.API
        and not any(row["account"].provider_key == key for row in rows)
    ]

    return {
        "rows": rows,
        "spend": spend,
        "paid_enabled": bool(app_settings["paid_enabled"]),
        "monthly_cap_usd": float(app_settings["monthly_spend_cap_usd"]),
        "authorized_accounts": [
            row["account"].label for row in rows if row["account"].paid_authorized
        ],
        "providers_without_accounts": providers_without_accounts,
        "units": COMMON_UNITS,
        "intervals": list(RefreshInterval),
    }
