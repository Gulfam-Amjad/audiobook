"""Credits, allowances and the spending guard.

These tests exist to make two promises from the brief enforceable rather
than aspirational:

* The app never invents a credit balance and never assumes a reset happened.
* The app cannot spend money unintentionally - three independent switches
  must all be on, and a cap is enforced against committed spend.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import BalanceSource, JobStatus, RefreshInterval
from app.models import Account, Entitlement, Job, Provider, SpendEntry
from app.providers import registry
from app.providers.base import BalanceResult, CostEstimate
from app.services import (
    accounts as account_service,
    credits,
    provider_sync,
    settings_store,
    spending,
)
from app.services.accounts import AccountError
from app.services.timeutil import start_of_utc_month, utcnow
from tests.conftest import write_image

pytestmark = pytest.mark.usefixtures("data_root")


async def setup_accounts(session: AsyncSession) -> Account:
    await provider_sync.sync_providers(session)
    await session.commit()
    return (
        await session.execute(select(Account).where(Account.provider_key == "mock"))
    ).scalar_one()


def estimate(usd: float, units: float | None = None, unit: str = "credits") -> CostEstimate:
    return CostEstimate(
        usd=usd,
        units=units,
        unit=unit,
        confidence="verified" if usd or units else "verified",
        basis="test",
        source_url="https://example.invalid",
    )


async def make_job(session: AsyncSession, account: Account, tmp_path) -> Job:
    """A minimal job to hang spend entries and usage counts from.

    The image path, hash and client token are all unique per call because
    each is a unique column, and a test that queues two jobs must not be
    tripped up by a constraint that has nothing to do with what it checks.
    """
    from app.models import Image

    unique = uuid4().hex[:12]
    image = Image(
        path=str(write_image(tmp_path / f"{unique}.jpg")),
        filename=f"{unique}.jpg",
        extension=".jpg",
        sha256=unique,
    )
    session.add(image)
    await session.flush()
    job = Job(
        image_id=image.id,
        prompt="test",
        client_token=f"tok-{unique}",
        provider_key=account.provider_key,
        account_id=account.id,
    )
    session.add(job)
    await session.flush()
    return job


# ---------------------------------------------------------------------------
# Where a balance came from
# ---------------------------------------------------------------------------


async def test_a_new_allowance_starts_as_unknown_not_zero(session: AsyncSession) -> None:
    """Zero would mean "empty"; unknown means "nobody has told us"."""
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)

    assert row.balance is None
    assert row.source == BalanceSource.UNKNOWN


async def test_a_typed_balance_is_recorded_as_the_users_own_figure(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)

    message = await account_service.save_balance(
        session,
        row.id,
        balance=120,
        unit="credits",
        refresh_interval=RefreshInterval.DAILY,
    )

    assert row.source == BalanceSource.USER_ENTERED
    assert row.balance == 120
    assert "you entered" in message
    assert "not treat this as verified" in message


async def test_a_balance_read_from_the_provider_is_marked_verified(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await setup_accounts(session)
    adapter = registry.get_adapter("mock")
    assert adapter is not None

    async def balance() -> BalanceResult:
        return BalanceResult(
            source=BalanceSource.API_VERIFIED, balance=42, unit="mock-credits"
        )

    monkeypatch.setattr(adapter, "fetch_balance", balance)
    await credits.refresh_from_provider(session, account)

    row = await credits.ensure_entitlement(session, account)
    assert row.source == BalanceSource.API_VERIFIED
    assert row.balance == 42


async def test_a_provider_that_hides_its_balance_leaves_it_unknown(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await setup_accounts(session)
    adapter = registry.get_adapter("mock")
    assert adapter is not None

    async def no_balance() -> BalanceResult:
        return BalanceResult(source=BalanceSource.UNKNOWN, message="Not exposed.")

    monkeypatch.setattr(adapter, "fetch_balance", no_balance)
    await credits.refresh_from_provider(session, account)

    row = await credits.ensure_entitlement(session, account)
    assert row.balance is None
    assert row.source == BalanceSource.UNKNOWN


async def test_refreshing_never_overwrites_a_figure_the_user_entered(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Their number is the only one anybody has, so it must survive."""
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=30, unit="credits", refresh_interval=RefreshInterval.NONE
    )

    adapter = registry.get_adapter("mock")
    assert adapter is not None

    async def no_balance() -> BalanceResult:
        return BalanceResult(source=BalanceSource.UNKNOWN, message="Not exposed.")

    monkeypatch.setattr(adapter, "fetch_balance", no_balance)
    await credits.refresh_from_provider(session, account)

    assert row.balance == 30
    assert row.source == BalanceSource.USER_ENTERED


async def test_a_negative_balance_is_refused(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    with pytest.raises(AccountError):
        await account_service.save_balance(
            session, row.id, balance=-5, unit="credits", refresh_interval="none"
        )


async def test_an_absurd_balance_is_refused(session: AsyncSession) -> None:
    """A mistyped figure must not become the basis of a capacity claim."""
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    with pytest.raises(AccountError):
        await account_service.save_balance(
            session, row.id, balance=5e12, unit="credits", refresh_interval="none"
        )


async def test_clearing_a_balance_returns_it_to_unknown(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=10, unit="credits", refresh_interval="none"
    )

    message = await account_service.save_balance(
        session, row.id, balance=None, unit="credits", refresh_interval="none"
    )
    assert row.source == BalanceSource.UNKNOWN
    assert "will not count as capacity" in message


async def test_an_unrecognised_refresh_interval_is_refused(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    with pytest.raises(AccountError):
        await account_service.save_balance(
            session, row.id, balance=1, unit="credits", refresh_interval="hourly"
        )


# ---------------------------------------------------------------------------
# Resets
# ---------------------------------------------------------------------------


async def test_a_reset_is_not_assumed_just_because_a_day_passed(
    session: AsyncSession,
) -> None:
    """Nothing refills on its own. Only a stated boundary counts."""
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=5, unit="credits", refresh_interval=RefreshInterval.NONE
    )

    changed = await credits.apply_known_resets(session, utcnow() + timedelta(days=30))
    assert changed == 0
    assert row.balance == 5


async def test_an_elapsed_boundary_blanks_the_balance_rather_than_refilling_it(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=5, unit="credits", refresh_interval=RefreshInterval.DAILY
    )
    original_boundary = row.next_reset_at

    changed = await credits.apply_known_resets(session, utcnow() + timedelta(days=2))

    assert changed == 1
    assert row.balance is None, "the app guessed what the allowance refilled to"
    assert row.source == BalanceSource.UNKNOWN
    assert row.next_reset_at != original_boundary


async def test_recording_a_reset_by_hand_also_refuses_to_guess(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=5, unit="credits", refresh_interval=RefreshInterval.DAILY
    )

    message = await account_service.record_manual_reset(session, row.id)

    assert row.balance is None
    assert row.source == BalanceSource.UNKNOWN
    assert "cannot know what it refilled to" in message


async def test_a_one_off_allowance_describes_itself_as_not_refreshing(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=50, unit="seconds", refresh_interval=RefreshInterval.NONE
    )

    view = account_service.reset_view(row)
    assert "one-off" in view.description
    assert row.next_reset_at is None


async def test_an_elapsed_boundary_is_described_as_stale_not_as_refilled(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=5, unit="credits", refresh_interval=RefreshInterval.DAILY
    )
    row.next_reset_at = utcnow() - timedelta(hours=1)

    view = account_service.reset_view(row)
    assert view.elapsed
    assert "out of date" in view.description


# ---------------------------------------------------------------------------
# Capacity checks
# ---------------------------------------------------------------------------


async def test_an_unknown_balance_does_not_block_a_free_job(
    session: AsyncSession,
) -> None:
    """Blocking would make every balance-hiding provider unusable."""
    account = await setup_accounts(session)
    await credits.ensure_entitlement(session, account)

    verdict = await credits.check_capacity(session, account, estimate(0.0, units=None))
    assert verdict.allowed
    assert verdict.remaining_units is None


async def test_an_insufficient_balance_blocks_permanently(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=2, unit="credits", refresh_interval="none"
    )

    verdict = await credits.check_capacity(session, account, estimate(0.0, units=10))
    assert not verdict.allowed
    assert verdict.permanent
    assert "you entered" in verdict.reason


async def test_an_expired_allowance_is_refused(session: AsyncSession) -> None:
    """Alibaba's free seconds expire after 90 days; spending them then fails."""
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session,
        row.id,
        balance=50,
        unit="seconds",
        refresh_interval="none",
        expires_at=utcnow() - timedelta(days=1),
    )

    verdict = await credits.check_capacity(session, account, estimate(0.0, units=5, unit="seconds"))
    assert not verdict.allowed
    assert "expired" in verdict.reason


async def test_a_cost_in_a_unit_with_no_allowance_is_not_pretended_to_be_checked(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=100, unit="credits", refresh_interval="none"
    )

    verdict = await credits.check_capacity(
        session, account, estimate(0.0, units=5, unit="tokens")
    )
    assert verdict.allowed
    assert "cannot be checked" in verdict.reason


async def test_only_a_locally_tracked_balance_is_decremented(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider's own figure is re-read, never recalculated by us."""
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    row.balance = 100
    row.unit = "credits"
    row.source = BalanceSource.API_VERIFIED
    await session.flush()

    await credits.consume(session, account, 10, "credits")
    assert row.balance == 100, "an api-verified balance was altered by local arithmetic"

    row.source = BalanceSource.USER_ENTERED
    await credits.consume(session, account, 10, "credits")
    assert row.balance == 90


async def test_consuming_never_drives_a_balance_below_zero(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=3, unit="credits", refresh_interval="none"
    )

    await credits.consume(session, account, 10, "credits")
    assert row.balance == 0


# ---------------------------------------------------------------------------
# The spending guard
# ---------------------------------------------------------------------------


async def test_a_paid_job_is_refused_on_a_fresh_install(session: AsyncSession) -> None:
    """The default configuration must be incapable of spending."""
    account = await setup_accounts(session)

    verdict = await spending.check_budget(
        session, estimate=estimate(0.25), account=account, provider_key="mock"
    )
    assert not verdict.allowed
    assert verdict.permanent
    assert "switched off" in verdict.reason


async def test_a_free_job_is_unaffected_by_the_spending_guard(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    verdict = await spending.check_budget(
        session, estimate=estimate(0.0), account=account, provider_key="mock"
    )
    assert verdict.allowed
    assert "free allowance" in verdict.reason


async def test_enabling_paid_generation_alone_is_not_enough(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    await settings_store.set_value(session, "paid_enabled", True)

    verdict = await spending.check_budget(
        session, estimate=estimate(0.25), account=account, provider_key="mock"
    )
    assert not verdict.allowed
    assert "cap is still $0" in verdict.reason


async def test_a_cap_without_account_authorisation_is_not_enough(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    await settings_store.set_value(session, "paid_enabled", True)
    await settings_store.set_value(session, "monthly_spend_cap_usd", 10.0)

    verdict = await spending.check_budget(
        session, estimate=estimate(0.25), account=account, provider_key="mock"
    )
    assert not verdict.allowed
    assert "not authorised" in verdict.reason


async def test_all_three_gates_together_allow_a_paid_job(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    await settings_store.set_value(session, "paid_enabled", True)
    await settings_store.set_value(session, "monthly_spend_cap_usd", 10.0)
    account.paid_authorized = True
    await session.flush()

    verdict = await spending.check_budget(
        session, estimate=estimate(0.25), account=account, provider_key="mock"
    )
    assert verdict.allowed


async def test_an_unpriced_job_is_refused_rather_than_assumed_free(
    session: AsyncSession,
) -> None:
    """Not knowing the price is not the same as the price being zero."""
    account = await setup_accounts(session)
    await settings_store.set_value(session, "paid_enabled", True)
    await settings_store.set_value(session, "monthly_spend_cap_usd", 10.0)
    account.paid_authorized = True
    await session.flush()

    unknown = CostEstimate(
        usd=0.0, units=None, unit="credits", confidence="unknown", basis="no published price"
    )
    verdict = await spending.check_budget(
        session, estimate=unknown, account=account, provider_key="mock"
    )
    assert not verdict.allowed
    assert verdict.permanent
    assert "cannot be bounded" in verdict.reason


async def test_the_cap_counts_committed_spend_not_just_confirmed_charges(
    session: AsyncSession, tmp_path
) -> None:
    account = await setup_accounts(session)
    await settings_store.set_value(session, "paid_enabled", True)
    await settings_store.set_value(session, "monthly_spend_cap_usd", 1.0)
    account.paid_authorized = True
    job = await make_job(session, account, tmp_path)

    await spending.record_estimate(session, job, estimate(0.90))

    verdict = await spending.check_budget(
        session, estimate=estimate(0.25), account=account, provider_key="mock"
    )
    assert not verdict.allowed
    assert "over the $1.00 cap" in verdict.reason


async def test_a_confirmed_charge_replaces_its_estimate_rather_than_adding_to_it(
    session: AsyncSession, tmp_path
) -> None:
    """Counting both would halve the usable cap for no reason."""
    account = await setup_accounts(session)
    job = await make_job(session, account, tmp_path)

    await spending.record_estimate(session, job, estimate(0.50))
    await spending.record_actual(session, job, 0.40)

    assert await spending.committed_this_month(session) == pytest.approx(0.40)


async def test_a_released_reservation_frees_the_budget_again(
    session: AsyncSession, tmp_path
) -> None:
    account = await setup_accounts(session)
    job = await make_job(session, account, tmp_path)

    await spending.record_estimate(session, job, estimate(0.50))
    await spending.release_estimate(session, job, "submit failed")

    assert await spending.committed_this_month(session) == pytest.approx(0.0)


async def test_releasing_a_reservation_keeps_the_history(
    session: AsyncSession, tmp_path
) -> None:
    """A deleted row would hide what the app intended to spend."""
    account = await setup_accounts(session)
    job = await make_job(session, account, tmp_path)

    await spending.record_estimate(session, job, estimate(0.50))
    await spending.release_estimate(session, job, "submit failed")

    entries = (
        await session.execute(select(SpendEntry).where(SpendEntry.job_id == job.id))
    ).scalars().all()
    assert len(entries) == 2
    assert any("released" in (e.note or "") for e in entries)


async def test_last_months_spending_does_not_count_against_this_month(
    session: AsyncSession, tmp_path
) -> None:
    account = await setup_accounts(session)
    job = await make_job(session, account, tmp_path)
    entry = await spending.record_actual(session, job, 5.0)
    entry.ts = start_of_utc_month() - timedelta(days=1)
    await session.flush()

    assert await spending.committed_this_month(session) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Paid authorisation
# ---------------------------------------------------------------------------


async def test_authorising_while_paid_generation_is_off_says_so(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    message = await account_service.set_paid_authorized(session, account.id, True)

    assert account.paid_authorized
    assert "still switched off" in message


async def test_authorising_with_a_zero_cap_says_so(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    await settings_store.set_value(session, "paid_enabled", True)

    message = await account_service.set_paid_authorized(session, account.id, True)
    assert "cap is $0" in message


async def test_withdrawing_authorisation_takes_effect_immediately(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    await account_service.set_paid_authorized(session, account.id, True)

    await account_service.set_paid_authorized(session, account.id, False)
    assert not account.paid_authorized

    await settings_store.set_value(session, "paid_enabled", True)
    await settings_store.set_value(session, "monthly_spend_cap_usd", 10.0)
    verdict = await spending.check_budget(
        session, estimate=estimate(0.25), account=account, provider_key="mock"
    )
    assert not verdict.allowed


async def test_turning_paid_generation_off_withdraws_every_authorisation(
    session: AsyncSession,
) -> None:
    """Otherwise re-enabling it later silently re-arms forgotten accounts."""
    account = await setup_accounts(session)
    account.paid_authorized = True
    await session.flush()

    revoked = await account_service.revoke_all_paid_authorisations(session)

    assert revoked == 1
    assert not account.paid_authorized


async def test_a_manual_provider_cannot_be_authorised_to_charge(
    session: AsyncSession,
) -> None:
    await provider_sync.sync_providers(session)
    account = Account(provider_key="manual", label="sites")
    session.add(account)
    await session.flush()

    with pytest.raises(AccountError) as caught:
        await account_service.set_paid_authorized(session, account.id, True)
    assert "nothing to authorise" in str(caught.value)
    assert not account.paid_authorized


# ---------------------------------------------------------------------------
# Accounts and limits
# ---------------------------------------------------------------------------


async def test_a_second_account_needs_a_distinct_label(session: AsyncSession) -> None:
    await setup_accounts(session)
    with pytest.raises(AccountError) as caught:
        await account_service.create_account(session, "mock", "default")
    assert "already has an account" in str(caught.value)


async def test_an_account_on_an_unknown_provider_is_refused(
    session: AsyncSession,
) -> None:
    await setup_accounts(session)
    with pytest.raises(AccountError):
        await account_service.create_account(session, "nonexistent", "x")


async def test_a_new_account_gets_an_allowance_to_track(session: AsyncSession) -> None:
    await setup_accounts(session)
    account = await account_service.create_account(session, "mock", "second key")

    rows = await credits.entitlements_for(session, account.id)
    assert len(rows) == 1
    assert rows[0].source == BalanceSource.UNKNOWN


async def test_a_daily_cap_of_zero_actually_stops_the_account(
    session: AsyncSession, tmp_path
) -> None:
    """Zero is falsy, so an earlier version of the guard ignored it entirely."""
    from app.services import selection

    account = await setup_accounts(session)
    provider = await session.get(Provider, "mock")
    assert provider is not None
    provider.enabled = True
    await account_service.update_limits(
        session,
        account.id,
        concurrency_limit=1,
        requests_per_minute=None,
        daily_job_cap=0,
    )
    await session.flush()

    eligible, reason, permanent = await selection._account_is_eligible(  # noqa: SLF001
        session,
        job=await make_job(session, account, tmp_path),
        provider=provider,
        caps_key="mock",
        account=account,
        estimate=estimate(0.0),
        concurrency_limit=1,
        is_manual=False,
    )

    assert not eligible
    assert permanent
    assert "daily cap is 0" in reason


async def test_a_blank_daily_cap_means_no_cap(session: AsyncSession, tmp_path) -> None:
    from app.services import selection

    account = await setup_accounts(session)
    provider = await session.get(Provider, "mock")
    assert provider is not None
    provider.enabled = True
    await account_service.update_limits(
        session,
        account.id,
        concurrency_limit=1,
        requests_per_minute=None,
        daily_job_cap=None,
    )
    await session.flush()

    eligible, _, _ = await selection._account_is_eligible(  # noqa: SLF001
        session,
        job=await make_job(session, account, tmp_path),
        provider=provider,
        caps_key="mock",
        account=account,
        estimate=estimate(0.0),
        concurrency_limit=1,
        is_manual=False,
    )
    assert eligible


async def test_exceeding_a_documented_concurrency_limit_is_flagged(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    message = await account_service.update_limits(
        session,
        account.id,
        concurrency_limit=50,
        requests_per_minute=None,
        daily_job_cap=None,
    )
    assert "documents a limit" in message


async def test_a_concurrency_below_one_is_refused(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    with pytest.raises(AccountError):
        await account_service.update_limits(
            session,
            account.id,
            concurrency_limit=0,
            requests_per_minute=None,
            daily_job_cap=None,
        )


async def test_a_duplicate_allowance_name_is_refused(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    await credits.ensure_entitlement(session, account)
    with pytest.raises(AccountError):
        await account_service.add_entitlement(session, account.id, "default")


async def test_deleting_an_allowance_stops_tracking_it(session: AsyncSession) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.delete_entitlement(session, row.id)

    remaining = (
        await session.execute(
            select(Entitlement).where(Entitlement.account_id == account.id)
        )
    ).scalars().all()
    assert remaining == []


# ---------------------------------------------------------------------------
# Usage counters
# ---------------------------------------------------------------------------


async def test_usage_counts_successes_and_failures_separately(
    session: AsyncSession, tmp_path
) -> None:
    account = await setup_accounts(session)
    for index, status in enumerate(
        (JobStatus.COMPLETED, JobStatus.COMPLETED, JobStatus.FAILED)
    ):
        job = await make_job(session, account, tmp_path / str(index))
        job.status = status
    await session.flush()

    usage = (await credits.usage_for_accounts(session))[account.id]
    assert usage.successes == 2
    assert usage.failures == 1
    assert usage.success_rate == pytest.approx(2 / 3)


async def test_a_success_rate_is_unknown_before_anything_finishes(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    usage = credits.AccountUsage(account_id=account.id)
    assert usage.success_rate is None


async def test_the_screen_context_reports_where_each_figure_came_from(
    session: AsyncSession,
) -> None:
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=7, unit="credits", refresh_interval=RefreshInterval.DAILY
    )

    context = await account_service.screen_context(session)

    assert context["paid_enabled"] is False
    assert context["authorized_accounts"] == []
    entry = next(r for r in context["rows"] if r["account"].id == account.id)
    assert entry["entitlements"][0].source == BalanceSource.USER_ENTERED
    assert row.id in entry["resets"]


async def test_the_screen_applies_an_elapsed_reset_before_rendering(
    session: AsyncSession,
) -> None:
    """A "next reset" in the past would be nonsense on screen."""
    account = await setup_accounts(session)
    row = await credits.ensure_entitlement(session, account)
    await account_service.save_balance(
        session, row.id, balance=7, unit="credits", refresh_interval=RefreshInterval.DAILY
    )
    row.next_reset_at = utcnow() - timedelta(hours=2)
    await session.flush()

    await account_service.screen_context(session)

    assert row.next_reset_at > utcnow()
    assert row.balance is None
