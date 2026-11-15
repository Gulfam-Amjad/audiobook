"""Provider selection and fallback.

Picking a provider is an elimination process, not a guess. Every candidate
must clear the same gates - capability, credentials, concurrency, rate limit,
allowance, budget - and every rejection is recorded with a readable reason so
the queue screen can explain exactly why a job is stuck.

Two safety rules override any preference:

* A job pinned to a provider is never moved off it.
* A job whose previous attempt had an unknown outcome is never offered to a
  different provider, because that could pay twice for one clip. The
  scheduler enforces this by leaving such jobs in ``uncertain``; this module
  additionally refuses to route them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    ErrorClass,
    JobStatus,
    ProviderKind,
    VerificationStatus,
)
from app.models import Account, Job, Provider
from app.providers import registry
from app.providers.base import CostEstimate
from app.services import credits, spending
from app.services.generation import JobSettings, MatchResult, match_to_provider
from app.services.jobs import active_count
from app.services.timeutil import start_of_utc_day

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    provider_key: str
    provider_name: str
    account: Account
    match: MatchResult
    estimate: CostEstimate
    is_manual: bool
    free: bool
    priority: int
    success_rate: float | None
    active_jobs: int
    concurrency_limit: int

    @property
    def settings(self) -> JobSettings:
        return self.match.settings

    def score(self) -> tuple:
        """Lower sorts first.

        Free capacity comes before paid, then the user's own priority, then a
        provider that has actually been verified, then observed reliability,
        then whichever has the most headroom right now.
        """
        return (
            0 if self.free else 1,
            self.priority,
            self.account.priority,
            -(self.success_rate if self.success_rate is not None else 0.5),
            self.active_jobs,
            self.estimate.usd,
            self.provider_key,
        )


@dataclass
class SelectionResult:
    candidate: Candidate | None
    rejections: list[tuple[str, str]] = field(default_factory=list)
    #: True when every blocker is permanent, so waiting will not help.
    all_permanent: bool = False

    @property
    def ok(self) -> bool:
        return self.candidate is not None

    def explain(self) -> str:
        if self.candidate is not None:
            return (
                f"Selected {self.candidate.provider_name} "
                f"(account '{self.candidate.account.label}')."
            )
        if not self.rejections:
            return (
                "No provider is enabled with a configured account. Enable one on the "
                "Providers screen, or use manual assist."
            )
        return " | ".join(f"{key}: {reason}" for key, reason in self.rejections)


async def select_provider(
    session: AsyncSession, job: Job, *, settings: JobSettings | None = None
) -> SelectionResult:
    """Choose where to run ``job``, or explain why nowhere is eligible."""
    requested = settings or JobSettings.from_dict(job.settings)
    rejections: list[tuple[str, str]] = []
    permanent_flags: list[bool] = []

    if job.error_class == ErrorClass.UNCERTAIN:
        return SelectionResult(
            None,
            [(
                job.provider_key or "unknown",
                "The previous attempt's outcome is unknown. This job will not be routed "
                "anywhere until that is resolved, so one clip cannot be paid for twice.",
            )],
            all_permanent=True,
        )

    providers = {
        row.key: row
        for row in (await session.execute(select(Provider).where(Provider.enabled.is_(True))))
        .scalars()
        .all()
    }

    if job.provider_locked and job.provider_key:
        if job.provider_key not in providers:
            return SelectionResult(
                None,
                [(
                    job.provider_key,
                    "This job is pinned to a provider that is currently disabled. Enable it "
                    "or clear the pin to let the queue choose another.",
                )],
                all_permanent=True,
            )
        providers = {job.provider_key: providers[job.provider_key]}

    excluded = set(job.excluded_providers or [])
    candidates: list[Candidate] = []

    for key, provider in providers.items():
        if key in excluded:
            rejections.append((key, "Already ruled out for this job by an earlier failure."))
            permanent_flags.append(True)
            continue

        caps = registry.get_capabilities(key)
        adapter = registry.get_adapter(key)
        if caps is None or adapter is None:
            rejections.append((key, "Adapter failed to load in this build."))
            permanent_flags.append(True)
            continue

        match = match_to_provider(key, requested)
        if not match.ok:
            rejections.append((key, match.summary()))
            permanent_flags.append(True)
            continue

        if caps.required_credentials and not adapter.is_configured:
            missing = ", ".join(
                name for name in caps.required_credentials if not adapter.credentials.get(name)
            )
            rejections.append((key, f"Missing credentials: {missing}."))
            permanent_flags.append(True)
            continue

        accounts = (
            await session.execute(
                select(Account)
                .where(Account.provider_key == key, Account.enabled.is_(True))
                .order_by(Account.priority, Account.id)
            )
        ).scalars().all()
        if not accounts:
            rejections.append((key, "No enabled account is configured."))
            permanent_flags.append(True)
            continue

        if job.account_id is not None:
            accounts = [a for a in accounts if a.id == job.account_id] or list(accounts)

        request = _build_estimate_request(job, match.settings)
        estimate = adapter.estimate_cost(request)
        is_manual = caps.kind == ProviderKind.MANUAL

        for account in accounts:
            verdict = await _account_is_eligible(
                session,
                job=job,
                provider=provider,
                caps_key=key,
                account=account,
                estimate=estimate,
                concurrency_limit=account.concurrency_limit or caps.default_concurrency,
                is_manual=is_manual,
            )
            if not verdict[0]:
                rejections.append((f"{key}/{account.label}", verdict[1]))
                permanent_flags.append(verdict[2])
                continue

            successes, failures = await _success_counts(session, key)
            total = successes + failures
            candidates.append(
                Candidate(
                    provider_key=key,
                    provider_name=caps.display_name,
                    account=account,
                    match=match,
                    estimate=estimate,
                    is_manual=is_manual,
                    free=estimate.usd <= 0,
                    priority=provider.priority,
                    success_rate=(successes / total) if total else None,
                    active_jobs=await active_count(session, key),
                    concurrency_limit=account.concurrency_limit or caps.default_concurrency,
                )
            )

    if not candidates:
        return SelectionResult(
            None,
            rejections,
            all_permanent=bool(permanent_flags) and all(permanent_flags),
        )

    candidates.sort(key=Candidate.score)
    return SelectionResult(candidates[0], rejections)


def _build_estimate_request(job: Job, settings: JobSettings):
    from app.providers.base import GenerationRequest

    return GenerationRequest(
        job_id=job.id,
        client_token=job.client_token,
        image_path="",
        prompt=job.prompt,
        negative_prompt=job.negative_prompt,
        model_id=settings.model_id,
        duration_s=settings.duration_s,
        resolution=settings.resolution,
        aspect_ratio=settings.aspect_ratio,
        seed=settings.seed,
        extra=dict(settings.extra),
    )


async def _account_is_eligible(
    session: AsyncSession,
    *,
    job: Job,
    provider: Provider,
    caps_key: str,
    account: Account,
    estimate: CostEstimate,
    concurrency_limit: int,
    is_manual: bool,
) -> tuple[bool, str, bool]:
    """Return ``(eligible, reason, permanent)`` for one provider/account pair."""
    if account.auth_status == VerificationStatus.FAILED:
        return (
            False,
            "The last connection test for this account failed. Fix the credentials and "
            "test again.",
            True,
        )

    if not is_manual and provider.verification_status == VerificationStatus.FAILED:
        return False, "The provider's last connection test failed.", True

    # Tested against None, not truthiness: a cap of 0 is a deliberate hard
    # stop on the account, and treating it as "no cap" would do the opposite
    # of what was asked.
    if account.daily_job_cap is not None:
        if account.daily_job_cap == 0:
            return (
                False,
                "This account's daily cap is 0, so it is held back until you raise it.",
                True,
            )
        today = await _jobs_today(session, account.id)
        if today >= account.daily_job_cap:
            return (
                False,
                f"Daily cap of {account.daily_job_cap} job(s) for this account is already used "
                f"({today} today).",
                False,
            )

    if not is_manual:
        in_flight = await active_count(session, caps_key)
        if in_flight >= concurrency_limit:
            return (
                False,
                f"At its concurrency limit ({in_flight} of {concurrency_limit} running).",
                False,
            )

    capacity = await credits.check_capacity(session, account, estimate)
    if not capacity.allowed:
        return False, capacity.reason, capacity.permanent

    budget = await spending.check_budget(
        session, estimate=estimate, account=account, provider_key=caps_key
    )
    if not budget.allowed:
        return False, budget.reason, budget.permanent

    return True, budget.reason, False


async def _jobs_today(session: AsyncSession, account_id: int) -> int:
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count(Job.id)).where(
                    Job.account_id == account_id,
                    Job.created_at >= start_of_utc_day(),
                    Job.status != JobStatus.CANCELLED,
                )
            )
        ).scalar()
        or 0
    )


async def _success_counts(session: AsyncSession, provider_key: str) -> tuple[int, int]:
    from sqlalchemy import func

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


def can_fall_back(error_class: ErrorClass | None) -> bool:
    """Is it safe to try a different provider after this failure?

    Only when we are certain the previous provider did no work. A timeout or
    an unknown outcome is explicitly excluded.
    """
    return error_class in {
        ErrorClass.AUTH,
        ErrorClass.INSUFFICIENT_CREDIT,
        ErrorClass.RATE_LIMITED,
        ErrorClass.TRANSIENT,
    }


async def preview_eligibility(
    session: AsyncSession, settings: JobSettings
) -> list[dict[str, object]]:
    """Explain, per provider, whether a job with these settings could run.

    Used by the queue screen before anything is submitted, so the user finds
    out about a blocker at queue time rather than after 200 failures.
    """
    rows: list[dict[str, object]] = []
    providers = {
        row.key: row for row in (await session.execute(select(Provider))).scalars().all()
    }

    for key, caps in registry.all_capabilities().items():
        provider = providers.get(key)
        adapter = registry.get_adapter(key)
        match = match_to_provider(key, settings)
        estimate = None
        if adapter is not None and match.ok:
            estimate = adapter.estimate_cost(
                _build_estimate_request(
                    Job(id=0, client_token="preview", prompt="", image_id=0), match.settings
                )
            )

        blockers: list[str] = []
        if provider is None or not provider.enabled:
            blockers.append("Disabled.")
        if caps.required_credentials and (adapter is None or not adapter.is_configured):
            blockers.append("No credentials configured.")
        if not match.ok:
            blockers.append(match.summary())
        if provider is not None and provider.verification_status == VerificationStatus.UNVERIFIED:
            blockers.append("Never completed a real call (unverified).")

        rows.append(
            {
                "key": key,
                "name": caps.display_name,
                "kind": caps.kind,
                "match": match,
                "estimate": estimate,
                "blockers": blockers,
                "eligible": not blockers,
                "free_tier_note": caps.free_tier_note,
                "status": provider.verification_status if provider else "not_configured",
            }
        )

    rows.sort(key=lambda row: (not row["eligible"], row["name"]))
    return rows
