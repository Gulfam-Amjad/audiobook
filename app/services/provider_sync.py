"""Keep the ``providers`` table in step with the adapter registry.

The code is the source of truth for what a provider *can* do; the table only
stores the user's decisions (enabled, priority, notes) and the verification
history. Syncing therefore overwrites descriptive fields and preserves
everything the user set.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ProviderKind, VerificationStatus
from app.models import Account, Provider
from app.providers import registry
from app.services import credits
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)


def _is_configured(key: str) -> bool:
    """Ask the adapter itself, not the environment.

    The mock and manual-assist adapters hold no real credentials yet are
    perfectly usable, so reading ``.env`` alone would wrongly mark them as
    unconfigured and deny them an account row.
    """
    adapter = registry.get_adapter(key)
    return adapter is not None and adapter.is_configured


async def sync_providers(session: AsyncSession) -> dict[str, str]:
    """Upsert a row per adapter. Returns ``{key: action}`` for logging."""
    actions: dict[str, str] = {}
    capabilities = registry.all_capabilities()

    existing = {
        row.key: row for row in (await session.execute(select(Provider))).scalars().all()
    }

    for key, caps in capabilities.items():
        configured = _is_configured(key)
        row = existing.get(key)
        if row is None:
            # The status is set explicitly rather than left to the column
            # default, which SQLAlchemy does not apply until flush - so
            # reading it back below would see None and skip the upgrade.
            row = Provider(
                key=key,
                enabled=False,
                verification_status=(
                    VerificationStatus.UNVERIFIED
                    if configured
                    else VerificationStatus.NOT_CONFIGURED
                ),
            )
            session.add(row)
            actions[key] = "created"
        else:
            actions[key] = "updated"

        # Descriptive fields always follow the adapter.
        row.display_name = caps.display_name
        row.kind = caps.kind
        row.docs_url = caps.docs_url or None
        row.signup_url = caps.signup_url or None

        # Verification status only moves backwards when credentials vanish.
        if not configured:
            row.verification_status = VerificationStatus.NOT_CONFIGURED
        elif row.verification_status in (None, VerificationStatus.NOT_CONFIGURED):
            row.verification_status = VerificationStatus.UNVERIFIED

    # Adapters that disappeared from the registry are left in the table but
    # flagged, so historical jobs still resolve a provider name.
    for key, row in existing.items():
        if key not in capabilities and row.verification_status != VerificationStatus.FAILED:
            row.last_check_message = (
                "Adapter is no longer present in this build. Historical jobs are unaffected."
            )
            row.enabled = False
            actions[key] = "orphaned"

    await session.flush()
    await _ensure_default_accounts(session, list(capabilities))
    # Imported here so this module and the manual-assist service never import
    # each other at load time.
    from app.services.manual_assist import ensure_site_accounts

    await ensure_site_accounts(session)
    return actions


async def mark_verified(session: AsyncSession, key: str | None, reason: str) -> None:
    """Record that this provider has actually produced a result.

    Only ever called after a real generated video has been downloaded. A
    successful credential check is *not* enough: it proves the key is
    accepted, not that the endpoint, payload shape and result handling are
    right. Keeping the bar here is what makes the "verified" badge mean
    something.
    """
    if not key:
        return
    provider = await session.get(Provider, key)
    if provider is None:
        return
    provider.last_check_at = utcnow()
    provider.last_check_message = reason
    if provider.verification_status != VerificationStatus.VERIFIED:
        provider.verification_status = VerificationStatus.VERIFIED
        provider.verified_at = utcnow()
        log.info("provider.verified key=%s reason=%s", key, reason)


async def _ensure_default_accounts(session: AsyncSession, keys: list[str]) -> None:
    """Give every configured API provider one account to hang state from."""
    for key in keys:
        caps = registry.get_capabilities(key)
        if caps is None or caps.kind == ProviderKind.MANUAL:
            continue
        if not _is_configured(key):
            continue

        existing = (
            await session.execute(
                select(Account).where(Account.provider_key == key).order_by(Account.id).limit(1)
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = Account(
                provider_key=key,
                label="default",
                enabled=True,
                credential_status=VerificationStatus.UNVERIFIED,
                auth_status=VerificationStatus.UNVERIFIED,
                concurrency_limit=caps.default_concurrency,
                requests_per_minute=caps.default_requests_per_minute,
                notes="Created automatically because credentials were found in .env.",
            )
            session.add(existing)
            await session.flush()

        # Done on every sync, not just on creation, so an account that
        # predates this step still gets somewhere to record a figure rather
        # than asking the user to invent a name for the obvious allowance.
        await credits.ensure_entitlement(session, existing)
    await session.flush()
