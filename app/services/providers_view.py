"""What the Providers screen shows, and the actions it can take.

The screen has one job beyond listing adapters: make it impossible to
mistake an adapter that was *written* from documentation for one that has
been *proven* to work. Three separate facts are tracked and shown apart:

* ``endpoint_verified`` - was the request shape read off the provider's own
  docs, or inferred?
* ``Account.auth_status`` - have the credentials been accepted by a live
  call?
* ``Provider.verification_status`` - has a real video actually come back?

Only the last one earns the word "verified" in the table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ProviderKind, VerificationStatus
from app.models import Account, Provider
from app.providers import registry
from app.providers.base import ModelSpec, ProviderCapabilities
from app.providers.http import sanitize_provider_message
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)


class ProviderActionError(Exception):
    """Something the user asked for cannot be done, with a reason to show."""


@dataclass(slots=True)
class ModelView:
    spec: ModelSpec
    price_label: str
    price_detail: str
    confidence: str
    #: Lowest published rate for this model, used only to pick which model's
    #: label represents the provider in the collapsed row. None when the
    #: provider publishes no usable figure.
    cheapest_usd: float | None = None


@dataclass(slots=True)
class ProviderView:
    key: str
    name: str
    kind: str
    caps: ProviderCapabilities
    row: Provider | None
    accounts: list[Account] = field(default_factory=list)
    models: list[ModelView] = field(default_factory=list)
    configured: bool = False
    missing_credentials: tuple[str, ...] = ()
    cheapest_label: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.row and self.row.enabled)

    @property
    def status(self) -> str:
        return self.row.verification_status if self.row else VerificationStatus.NOT_CONFIGURED

    @property
    def status_badge(self) -> str:
        if self.kind in (ProviderKind.MOCK, ProviderKind.MANUAL):
            # Neither can ever be "verified", so colouring them amber as if
            # something were outstanding would be misleading.
            return "info"
        return {
            VerificationStatus.VERIFIED: "good",
            VerificationStatus.FAILED: "bad",
            VerificationStatus.UNVERIFIED: "warn",
            VerificationStatus.NOT_CONFIGURED: "neutral",
        }.get(self.status, "neutral")

    @property
    def status_label(self) -> str:
        if self.kind == ProviderKind.MOCK:
            return "Local fake"
        if self.kind == ProviderKind.MANUAL:
            return "You generate"
        return {
            VerificationStatus.VERIFIED: "Verified",
            VerificationStatus.FAILED: "Rejected",
            VerificationStatus.UNVERIFIED: "Unverified",
            VerificationStatus.NOT_CONFIGURED: "No credentials",
        }.get(self.status, self.status)

    @property
    def kind_label(self) -> str:
        return {
            ProviderKind.API: "API",
            ProviderKind.MANUAL: "Manual",
            ProviderKind.MOCK: "Mock",
        }.get(self.kind, self.kind)

    @property
    def status_explanation(self) -> str:
        """Spelled out, because a one-word badge invites the wrong reading."""
        if self.kind == ProviderKind.MOCK:
            return (
                "A local fake for development. It calls nothing and generates nothing, "
                "so it can never be verified and never costs anything."
            )
        if self.kind == ProviderKind.MANUAL:
            return (
                "Not an API. Jobs sent here wait for you to generate the clip on the "
                "site and drop the download into the inbox folder."
            )
        if self.status == VerificationStatus.VERIFIED:
            return "A real video has been generated and downloaded through this adapter."
        if self.status == VerificationStatus.FAILED:
            return "A live call was attempted and the provider rejected it."
        if self.status == VerificationStatus.NOT_CONFIGURED:
            missing = ", ".join(self.missing_credentials) or "credentials"
            return f"Nothing to call with yet: {missing} is not set in .env."
        return (
            "Written from the provider's documentation but never proven. No video has "
            "come back through it, so treat everything here as untested."
        )

    @property
    def blockers(self) -> list[str]:
        """Why a job could not be sent here right now."""
        out: list[str] = []
        if not self.enabled:
            out.append("Disabled on this screen.")
        if not self.configured:
            out.append(f"Missing {', '.join(self.missing_credentials)} in .env.")
        if not self.accounts and self.kind != ProviderKind.MANUAL:
            out.append("No account record.")
        elif self.accounts and not any(a.enabled for a in self.accounts):
            out.append("Every account is switched off.")
        return out

    @property
    def can_test(self) -> bool:
        return self.configured and self.kind == ProviderKind.API


def _price_view(model: ModelSpec) -> ModelView:
    """Turn a PriceSpec into something a table cell can hold honestly."""
    price = model.price
    cheapest: float | None = None
    if price.usd_per_second:
        cheapest = min(price.usd_per_second.values())
        label = f"${cheapest:.4f}/s" if cheapest else "free"
    elif price.usd_per_video:
        cheapest = min(price.usd_per_video.values())
        label = f"${cheapest:.2f}/clip" if cheapest else "free"
    else:
        label = "unpublished"

    if price.confidence == "estimated" and label != "unpublished":
        label += " (est.)"
    return ModelView(
        spec=model,
        price_label=label,
        price_detail=price.notes or "",
        confidence=price.confidence,
        cheapest_usd=cheapest,
    )


async def build_views(session: AsyncSession) -> list[ProviderView]:
    """One row per registered adapter, joined to whatever the database knows."""
    rows = {
        row.key: row for row in (await session.execute(select(Provider))).scalars().all()
    }
    accounts: dict[str, list[Account]] = {}
    for account in (await session.execute(select(Account))).scalars().all():
        accounts.setdefault(account.provider_key, []).append(account)

    views: list[ProviderView] = []
    for key, caps in registry.all_capabilities().items():
        adapter = registry.get_adapter(key)
        missing = tuple(
            name
            for name in caps.required_credentials
            if not (adapter and adapter.credentials.get(name))
        )
        view = ProviderView(
            key=key,
            name=caps.display_name,
            kind=caps.kind,
            caps=caps,
            row=rows.get(key),
            accounts=sorted(accounts.get(key, []), key=lambda a: (a.priority, a.id)),
            models=[_price_view(model) for model in caps.models],
            configured=bool(adapter and adapter.is_configured),
            missing_credentials=missing or tuple(caps.required_credentials),
        )
        priced = [m for m in view.models if m.cheapest_usd is not None]
        cheapest = min(priced, key=lambda m: m.cheapest_usd or 0.0, default=None)
        view.cheapest_label = cheapest.price_label if cheapest else "price unpublished"
        views.append(view)

    # Usable things first, then alphabetically. A long list of unconfigured
    # adapters should not push the working one off the top of the screen.
    def sort_key(view: ProviderView) -> tuple[int, int, str]:
        return (0 if view.enabled else 1, 0 if view.configured else 1, view.name.lower())

    views.sort(key=sort_key)
    return views


def summarise(views: list[ProviderView]) -> dict[str, object]:
    return {
        "total": len(views),
        "enabled": sum(1 for v in views if v.enabled),
        "configured": sum(1 for v in views if v.configured),
        "verified": sum(1 for v in views if v.status == VerificationStatus.VERIFIED),
        "endpoint_unverified": sorted(
            v.name for v in views if v.kind == ProviderKind.API and not v.caps.endpoint_verified
        ),
    }


async def set_enabled(session: AsyncSession, key: str, enabled: bool) -> str:
    provider = await session.get(Provider, key)
    if provider is None:
        raise ProviderActionError(f"'{key}' is not a known provider.")

    if enabled:
        adapter = registry.get_adapter(key)
        caps = registry.get_capabilities(key)
        if adapter is None or caps is None:
            raise ProviderActionError(f"'{key}' failed to load and cannot be enabled.")
        if not adapter.is_configured:
            missing = ", ".join(
                name
                for name in caps.required_credentials
                if not adapter.credentials.get(name)
            )
            raise ProviderActionError(
                f"{caps.display_name} has no {missing}. Add it to .env and restart, "
                "then enable it here."
            )

    provider.enabled = enabled
    caps = registry.get_capabilities(key)
    name = caps.display_name if caps else key
    if not enabled:
        return f"{name} is off. Queued jobs will not be sent there."
    if provider.verification_status == VerificationStatus.VERIFIED:
        return f"{name} is on."
    return (
        f"{name} is on, but still unverified - no video has come back through it yet. "
        "The first job you send is the test."
    )


async def set_priority(session: AsyncSession, key: str, priority: int) -> str:
    provider = await session.get(Provider, key)
    if provider is None:
        raise ProviderActionError(f"'{key}' is not a known provider.")
    if not 1 <= priority <= 999:
        raise ProviderActionError("Priority must be between 1 and 999.")
    provider.priority = priority
    return f"{provider.display_name} priority set to {priority} (lower is tried first)."


async def test_connection(session: AsyncSession, account_id: int) -> str:
    """Make the cheapest real call that proves the credentials work.

    This never generates anything, so it cannot spend a generation credit.
    It also never promotes the provider to "verified" - that would overstate
    what an auth probe shows.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise ProviderActionError("That account no longer exists.")

    adapter = registry.get_adapter(account.provider_key)
    caps = registry.get_capabilities(account.provider_key)
    if adapter is None or caps is None:
        raise ProviderActionError(
            f"The {account.provider_key} adapter failed to load, so it cannot be tested."
        )

    name = caps.display_name
    try:
        result = await adapter.check_auth()
    except Exception as exc:  # noqa: BLE001 - a bad adapter must not kill the page
        log.warning("auth check raised key=%s", account.provider_key, exc_info=True)
        account.auth_status = VerificationStatus.FAILED
        account.last_auth_check_at = utcnow()
        account.last_auth_message = sanitize_provider_message(str(exc))
        return f"{name} connection test failed: {account.last_auth_message}"

    account.last_auth_check_at = utcnow()
    account.last_auth_message = sanitize_provider_message(result.message)

    if not result.ok:
        account.auth_status = VerificationStatus.FAILED
        provider = await session.get(Provider, account.provider_key)
        if provider is not None and result.reached_provider:
            # The provider itself said no, so record that against the
            # provider too - selection treats a rejected provider as a
            # permanent blocker rather than retrying into it.
            provider.verification_status = VerificationStatus.FAILED
            provider.last_check_at = utcnow()
            provider.last_check_message = account.last_auth_message
        return f"{name} rejected the credentials: {account.last_auth_message}"

    if not result.reached_provider:
        account.auth_status = VerificationStatus.UNVERIFIED
        return (
            f"{name} was not actually contacted: {account.last_auth_message} "
            "Nothing has been verified."
        )

    account.auth_status = VerificationStatus.VERIFIED
    provider = await session.get(Provider, account.provider_key)
    if provider is not None:
        provider.last_check_at = utcnow()
        provider.last_check_message = f"Credential check passed: {account.last_auth_message}"
        if provider.verification_status == VerificationStatus.FAILED:
            # A key that works again clears the rejection, but does not skip
            # straight to verified - no video has been produced yet.
            provider.verification_status = VerificationStatus.UNVERIFIED

    suffix = (
        ""
        if provider is not None
        and provider.verification_status == VerificationStatus.VERIFIED
        else " The adapter is still unverified until a real video comes back."
    )
    return f"{name} accepted the credentials.{suffix}"
