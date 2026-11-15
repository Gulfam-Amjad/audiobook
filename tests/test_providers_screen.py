"""The Providers screen and the rules behind its verification badge.

The point of these tests is that the word "verified" cannot be earned by
anything short of a real video coming back, and that an adapter with no
credentials cannot be switched on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ProviderKind, VerificationStatus
from app.models import Account, Provider
from app.providers import registry
from app.providers.base import AuthResult
from app.services import provider_sync, providers_view
from app.services.providers_view import ProviderActionError

pytestmark = pytest.mark.usefixtures("data_root")


async def synced(session: AsyncSession) -> None:
    await provider_sync.sync_providers(session)
    await session.commit()


async def test_a_fresh_sync_creates_one_row_per_adapter(session: AsyncSession) -> None:
    await synced(session)
    rows = (await session.execute(select(Provider))).scalars().all()
    assert {row.key for row in rows} == set(registry.ADAPTER_PATHS)


async def test_every_provider_starts_disabled(session: AsyncSession) -> None:
    """Nothing may be able to spend on a first run without a deliberate act."""
    await synced(session)
    rows = (await session.execute(select(Provider))).scalars().all()
    assert not any(row.enabled for row in rows)


async def test_nothing_is_verified_on_a_fresh_install(session: AsyncSession) -> None:
    await synced(session)
    rows = (await session.execute(select(Provider))).scalars().all()
    assert not any(row.verification_status == VerificationStatus.VERIFIED for row in rows)


async def test_a_configured_provider_gets_an_account_with_an_allowance_row(
    session: AsyncSession,
) -> None:
    """Without one, the Credits screen has nowhere to put a figure."""
    from app.services import credits

    await synced(session)
    account = (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalar_one()

    rows = await credits.entitlements_for(session, account.id)
    assert len(rows) == 1
    assert rows[0].balance is None, "a fresh allowance must not claim a figure"


async def test_an_account_predating_the_allowance_step_is_backfilled(
    session: AsyncSession,
) -> None:
    from app.models import Entitlement
    from app.services import credits

    await synced(session)
    account = (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalar_one()
    for row in await credits.entitlements_for(session, account.id):
        await session.delete(row)
    await session.flush()

    await synced(session)

    remaining = (
        await session.execute(
            select(Entitlement).where(Entitlement.account_id == account.id)
        )
    ).scalars().all()
    assert len(remaining) == 1


async def test_syncing_twice_does_not_duplicate_accounts_or_allowances(
    session: AsyncSession,
) -> None:
    from app.models import Entitlement

    await synced(session)
    await synced(session)

    accounts = (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalars().all()
    assert len(accounts) == 1
    entitlements = (
        await session.execute(
            select(Entitlement).where(Entitlement.account_id == accounts[0].id)
        )
    ).scalars().all()
    assert len(entitlements) == 1


async def test_a_newly_created_row_gets_a_real_status_not_a_null(
    session: AsyncSession,
) -> None:
    """The column default is only applied at flush, so it is set explicitly.

    Without that, a provider created on the very first run kept the
    'not configured' default forever, even once it was usable.
    """
    await synced(session)
    manual = await session.get(Provider, "manual")
    assert manual is not None
    # Manual assist needs no credentials, so it must not look unconfigured.
    assert manual.verification_status == VerificationStatus.UNVERIFIED


async def test_an_api_provider_without_credentials_reads_as_not_configured(
    session: AsyncSession,
) -> None:
    await synced(session)
    runway = await session.get(Provider, "runway")
    assert runway is not None
    assert runway.verification_status == VerificationStatus.NOT_CONFIGURED


async def test_a_provider_with_no_credentials_cannot_be_enabled(
    session: AsyncSession,
) -> None:
    await synced(session)
    with pytest.raises(ProviderActionError) as caught:
        await providers_view.set_enabled(session, "runway", True)
    assert "api_key" in str(caught.value)

    runway = await session.get(Provider, "runway")
    assert runway is not None and not runway.enabled


async def test_enabling_an_unverified_provider_says_it_is_still_unproven(
    session: AsyncSession,
) -> None:
    await synced(session)
    message = await providers_view.set_enabled(session, "mock", True)
    assert "unverified" in message.lower()


async def test_enabling_an_unknown_provider_is_refused(session: AsyncSession) -> None:
    await synced(session)
    with pytest.raises(ProviderActionError):
        await providers_view.set_enabled(session, "not-a-provider", True)


async def test_priority_must_stay_in_range(session: AsyncSession) -> None:
    await synced(session)
    with pytest.raises(ProviderActionError):
        await providers_view.set_priority(session, "mock", 0)
    await providers_view.set_priority(session, "mock", 5)
    provider = await session.get(Provider, "mock")
    assert provider is not None and provider.priority == 5


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------


async def test_a_passing_connection_test_does_not_make_a_provider_verified(
    session: AsyncSession,
) -> None:
    """An accepted key proves authentication, not that generation works."""
    await synced(session)
    account = (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalar_one()

    message = await providers_view.test_connection(session, account.id)

    assert account.auth_status == VerificationStatus.VERIFIED
    provider = await session.get(Provider, "mock")
    assert provider is not None
    assert provider.verification_status == VerificationStatus.UNVERIFIED
    assert "still unverified" in message


async def test_a_rejected_key_marks_both_the_account_and_the_provider(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await synced(session)
    account = (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalar_one()

    adapter = registry.get_adapter("mock")
    assert adapter is not None

    async def rejected() -> AuthResult:
        return AuthResult(False, "Key not recognised.", reached_provider=True)

    monkeypatch.setattr(adapter, "check_auth", rejected)
    message = await providers_view.test_connection(session, account.id)

    assert account.auth_status == VerificationStatus.FAILED
    provider = await session.get(Provider, "mock")
    assert provider is not None
    assert provider.verification_status == VerificationStatus.FAILED
    assert "rejected" in message.lower()


async def test_a_provider_that_could_not_be_reached_is_not_called_rejected(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our network failing is not the provider saying no."""
    await synced(session)
    account = (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalar_one()

    adapter = registry.get_adapter("mock")
    assert adapter is not None

    async def unreachable() -> AuthResult:
        return AuthResult(False, "Connection refused.", reached_provider=False)

    monkeypatch.setattr(adapter, "check_auth", unreachable)
    await providers_view.test_connection(session, account.id)

    provider = await session.get(Provider, "mock")
    assert provider is not None
    assert provider.verification_status != VerificationStatus.FAILED


async def test_an_adapter_that_throws_does_not_break_the_screen(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await synced(session)
    account = (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalar_one()

    adapter = registry.get_adapter("mock")
    assert adapter is not None

    async def explode() -> AuthResult:
        raise RuntimeError("Bearer sk-secret-123 was rejected")

    monkeypatch.setattr(adapter, "check_auth", explode)
    message = await providers_view.test_connection(session, account.id)

    assert "failed" in message.lower()
    assert "sk-secret-123" not in message, "the message was not sanitised"
    assert account.auth_status == VerificationStatus.FAILED


async def test_testing_a_missing_account_is_refused(session: AsyncSession) -> None:
    await synced(session)
    with pytest.raises(ProviderActionError):
        await providers_view.test_connection(session, 99999)


# ---------------------------------------------------------------------------
# Verification only on a real result
# ---------------------------------------------------------------------------


async def test_mark_verified_records_the_reason_and_the_time(
    session: AsyncSession,
) -> None:
    await synced(session)
    await provider_sync.mark_verified(session, "mock", "Produced a real video for job #1.")

    provider = await session.get(Provider, "mock")
    assert provider is not None
    assert provider.verification_status == VerificationStatus.VERIFIED
    assert provider.verified_at is not None
    assert "job #1" in (provider.last_check_message or "")


async def test_mark_verified_keeps_the_first_verification_date(
    session: AsyncSession,
) -> None:
    await synced(session)
    await provider_sync.mark_verified(session, "mock", "first")
    provider = await session.get(Provider, "mock")
    assert provider is not None
    first = provider.verified_at

    await provider_sync.mark_verified(session, "mock", "second")
    assert provider.verified_at == first
    assert provider.last_check_message == "second"


async def test_a_later_sync_never_downgrades_a_verified_provider(
    session: AsyncSession,
) -> None:
    await synced(session)
    await provider_sync.mark_verified(session, "mock", "proved it")
    await session.commit()

    await synced(session)
    provider = await session.get(Provider, "mock")
    assert provider is not None
    assert provider.verification_status == VerificationStatus.VERIFIED


# ---------------------------------------------------------------------------
# The view model
# ---------------------------------------------------------------------------


async def test_the_view_lists_every_adapter_with_its_blockers(
    session: AsyncSession,
) -> None:
    await synced(session)
    views = await providers_view.build_views(session)

    assert {v.key for v in views} == set(registry.ADAPTER_PATHS)
    runway = next(v for v in views if v.key == "runway")
    assert not runway.configured
    assert any("api_key" in blocker for blocker in runway.blockers)


async def test_a_mock_or_manual_provider_is_not_described_as_unverified(
    session: AsyncSession,
) -> None:
    """Neither can ever produce a verified real call, so saying so is noise."""
    await synced(session)
    views = {v.key: v for v in await providers_view.build_views(session)}

    assert "fake" in views["mock"].status_label.lower()
    assert "generate" in views["manual"].status_label.lower()
    for key in ("mock", "manual"):
        assert "untested" not in views[key].status_explanation


async def test_an_api_adapter_is_described_as_unproven_until_it_proves_itself(
    session: AsyncSession,
) -> None:
    await synced(session)
    views = {v.key: v for v in await providers_view.build_views(session)}
    assert "never proven" in views["runway"].status_explanation or (
        "not set in .env" in views["runway"].status_explanation
    )


async def test_the_summary_counts_what_the_header_claims(session: AsyncSession) -> None:
    await synced(session)
    views = await providers_view.build_views(session)
    summary = providers_view.summarise(views)

    assert summary["total"] == len(registry.ADAPTER_PATHS)
    assert summary["enabled"] == 0
    assert summary["verified"] == 0
    # The mock and manual adapters need no credentials, so both count.
    assert summary["configured"] == 2


async def test_the_summary_names_every_adapter_with_an_inferred_endpoint(
    session: AsyncSession,
) -> None:
    await synced(session)
    views = await providers_view.build_views(session)
    summary = providers_view.summarise(views)

    expected = {
        caps.display_name
        for key, caps in registry.all_capabilities().items()
        if caps.kind == ProviderKind.API and not caps.endpoint_verified
    }
    assert set(summary["endpoint_unverified"]) == expected
    assert expected, "the research left several endpoints unconfirmed; say so"


async def test_an_unpriced_provider_says_so_rather_than_showing_zero(
    session: AsyncSession,
) -> None:
    await synced(session)
    views = await providers_view.build_views(session)
    for view in views:
        if view.cheapest_label == "price unpublished":
            continue
        assert "$" in view.cheapest_label or view.cheapest_label == "free"


async def test_estimated_prices_are_labelled_as_estimates(
    session: AsyncSession,
) -> None:
    await synced(session)
    views = {v.key: v for v in await providers_view.build_views(session)}
    for model in views["vidu"].models:
        assert "(est.)" in model.price_label
    for model in views["runway"].models:
        assert "(est.)" not in model.price_label
