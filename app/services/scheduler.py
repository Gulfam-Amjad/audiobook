"""The generation queue engine.

A single asyncio loop drives four phases in order on each tick: claim and
submit queued jobs, poll running tasks, download finished ones, and time out
anything stuck. State lives entirely in SQLite, so stopping the process mid-run
loses nothing and :func:`app.services.jobs.recover_interrupted` repairs the
in-flight rows on the next start.

Concurrency is deliberately conservative. Each provider advertises a limit and
the engine never exceeds it; the global default is one job at a time until the
user raises it. Nothing here launches unbounded parallel work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ACTIVE_JOB_STATES, ErrorClass, JobStatus, ProviderKind
from app.db import session_scope
from app.models import Account, Image, Job, Provider
from app.providers import registry
from app.providers.base import ProviderError
from app.providers.http import sanitize_provider_message
from app.services import (
    credits,
    downloads,
    jobs as job_service,
    provider_sync,
    selection,
    settings_store,
)
from app.services.generation import JobSettings, build_request
from app.services.spending import record_actual, record_estimate, release_estimate
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

TICK_SECONDS = 3.0
#: Upper bound on jobs examined per phase per tick, so one tick stays short.
CLAIM_BATCH = 8
POLL_BATCH = 25
DOWNLOAD_BATCH = 3


@dataclass
class RateLimiter:
    """Sliding-window request limiter, per provider."""

    windows: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def allow(self, key: str, requests_per_minute: int | None) -> tuple[bool, float]:
        if not requests_per_minute or requests_per_minute <= 0:
            return True, 0.0
        now = time.monotonic()
        window = self.windows[key]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= requests_per_minute:
            return False, 60.0 - (now - window[0])
        return True, 0.0

    def record(self, key: str) -> None:
        self.windows[key].append(time.monotonic())


@dataclass
class SchedulerStats:
    ticks: int = 0
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    downloads: int = 0
    last_tick_at: str = ""
    last_error: str = ""


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.rate_limiter = RateLimiter()
        self.stats = SchedulerStats()
        self.running = False

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()

        async with session_scope() as session:
            recovery = await job_service.recover_interrupted(session)
            await credits.apply_known_resets(session)
        if any(recovery.values()):
            log.warning("Startup recovery applied", extra=recovery)

        self._task = asyncio.create_task(self._loop(), name="avm-scheduler")
        self.running = True
        log.info("Scheduler started")

    async def stop(self) -> None:
        self.running = False
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        log.info("Scheduler stopped")

    def wake(self) -> None:
        """Ask the loop to run a tick immediately."""
        self._wake.set()

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- main loop --------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the queue
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Scheduler tick failed")

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
            except TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def _tick(self) -> None:
        self.stats.ticks += 1
        self.stats.last_tick_at = utcnow().isoformat(timespec="seconds")

        async with session_scope() as session:
            paused = bool(await settings_store.get(session, "queue_paused"))

        # Polling and downloading continue while paused: those jobs have
        # already been submitted and may already have been charged for, so
        # abandoning them would waste credits.
        await self._poll_phase()
        await self._download_phase()
        await self._timeout_phase()
        if not paused:
            await self._submit_phase()

    # -- phase 1: submit --------------------------------------------------

    async def _submit_phase(self) -> None:
        async with session_scope() as session:
            claimable = await job_service.claimable_jobs(session, CLAIM_BATCH)
            if not claimable:
                return
            job_ids = [job.id for job in claimable]

        for job_id in job_ids:
            if self._stop.is_set():
                return
            try:
                await self._submit_one(job_id)
            except Exception:  # noqa: BLE001
                log.exception("Submit failed unexpectedly", extra={"job_id": job_id})

    async def _submit_one(self, job_id: int) -> None:
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return

            choice = await selection.select_provider(session, job)
            if not choice.ok:
                await self._handle_no_provider(session, job, choice)
                return

            candidate = choice.candidate
            assert candidate is not None

            allowed, wait_s = self.rate_limiter.allow(
                candidate.provider_key,
                candidate.account.requests_per_minute
                or registry.get_capabilities(candidate.provider_key).default_requests_per_minute,
            )
            if not allowed:
                job.next_attempt_at = utcnow() + timedelta(seconds=max(1.0, wait_s))
                await job_service.record_event(
                    session,
                    job,
                    "rate_limited_locally",
                    f"Holding back {wait_s:.0f}s to stay inside "
                    f"{candidate.provider_name}'s request limit.",
                )
                return

            job.provider_key = candidate.provider_key
            job.account_id = candidate.account.id
            job.settings = candidate.settings.to_dict()
            job.cost_estimate_usd = candidate.estimate.usd
            job.cost_estimate_units = candidate.estimate.units
            job.cost_estimate_unit = candidate.estimate.unit

            if candidate.match.was_adjusted:
                await job_service.record_event(
                    session,
                    job,
                    "settings_adjusted",
                    f"Adjusted for {candidate.provider_name}: {candidate.match.summary()}",
                )

            # Manual providers never touch the network: the user generates the
            # clip themselves, so the job waits for a file to appear.
            if candidate.is_manual:
                await job_service.transition(
                    session,
                    job,
                    JobStatus.AWAITING_MANUAL,
                    event="awaiting_manual",
                    message=(
                        f"Listed on the {candidate.account.label} worksheet. Generate it on "
                        f"the site yourself, then drop the download into the inbox. "
                        f"Match code: {job.manual_token}."
                    ),
                )
                return

            image = await session.get(Image, job.image_id)
            if image is None or not Path(image.path).exists():
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event="source_missing",
                    message="The source image is no longer on disk.",
                    error_class=ErrorClass.INVALID_INPUT,
                )
                return

            request, problems = build_request(
                job_id=job.id,
                client_token=job.client_token,
                image_path=image.path,
                prompt=job.prompt,
                negative_prompt=job.negative_prompt,
                settings=candidate.settings,
                provider_key=candidate.provider_key,
            )
            if problems:
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event="validation_failed",
                    message="; ".join(problems),
                    error_class=ErrorClass.INVALID_INPUT,
                )
                return

            await record_estimate(session, job, candidate.estimate)
            await job_service.transition(
                session,
                job,
                JobStatus.SUBMITTING,
                event="submitting",
                message=(
                    f"Submitting to {candidate.provider_name} as "
                    f"{candidate.settings.model_id} "
                    f"({candidate.settings.duration_s}s, {candidate.settings.resolution}). "
                    f"{candidate.estimate.basis or 'No published price.'}"
                ),
                detail={"estimate_usd": candidate.estimate.usd},
            )
            provider_key = candidate.provider_key
            account_id = candidate.account.id

        # The network call happens outside the transaction so a slow provider
        # never holds a database write lock.
        adapter = registry.get_adapter(provider_key)
        if adapter is None:
            async with session_scope() as session:
                job = await session.get(Job, job_id)
                if job is not None:
                    await release_estimate(session, job, "adapter unavailable")
                    await job_service.transition(
                        session,
                        job,
                        JobStatus.FAILED,
                        event="adapter_missing",
                        message=f"Adapter '{provider_key}' disappeared before submission.",
                        error_class=ErrorClass.UNKNOWN,
                    )
            return

        submit_timeout = 120
        async with session_scope() as session:
            submit_timeout = int(await settings_store.get(session, "submit_timeout_seconds"))

        try:
            result = await asyncio.wait_for(adapter.submit(request), timeout=submit_timeout)
        except TimeoutError:
            await self._after_uncertain_submit(
                job_id,
                f"The submit request to {provider_key} timed out after {submit_timeout}s.",
            )
            return
        except ProviderError as exc:
            await self._after_submit_error(job_id, provider_key, account_id, exc)
            return
        except Exception as exc:  # noqa: BLE001
            await self._after_submit_error(
                job_id,
                provider_key,
                account_id,
                ProviderError(
                    sanitize_provider_message(str(exc)) or type(exc).__name__,
                    ErrorClass.UNKNOWN,
                ),
            )
            return

        self.rate_limiter.record(provider_key)
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            if job.status == JobStatus.CANCELLED:
                await job_service.record_event(
                    session,
                    job,
                    "cancelled_after_submit",
                    "The job was cancelled while the provider was accepting it; any credits "
                    "it consumed are not recoverable.",
                )
                return

            if not result.accepted:
                await release_estimate(session, job, "provider refused the request")
                await job_service.schedule_retry(
                    session,
                    job,
                    reason=f"{provider_key} refused the request: {result.message}",
                    error_class=ErrorClass.TRANSIENT,
                )
                return

            job.task_ref = result.task_ref
            self.stats.submitted += 1

            if result.video_url:
                # Some providers return the finished clip synchronously.
                await job_service.transition(
                    session,
                    job,
                    JobStatus.DOWNLOADING,
                    event="submitted_sync",
                    message="Provider returned a finished video immediately.",
                    detail={"video_url_present": True},
                )
                job.settings = {**(job.settings or {}), "_video_url": result.video_url}
            else:
                await job_service.transition(
                    session,
                    job,
                    JobStatus.PROCESSING,
                    event="submitted",
                    message=f"Accepted as task {result.task_ref}. {result.message}".strip(),
                )

    async def _handle_no_provider(
        self, session: AsyncSession, job: Job, choice: selection.SelectionResult
    ) -> None:
        """Park a job that currently has nowhere to go."""
        reason = choice.explain()
        if choice.all_permanent:
            await job_service.transition(
                session,
                job,
                JobStatus.PAUSED,
                event="no_eligible_provider",
                message=(
                    f"Paused: no eligible provider, and every blocker is permanent. {reason}"
                ),
            )
        else:
            job.next_attempt_at = utcnow() + timedelta(seconds=60)
            await job_service.record_event(
                session,
                job,
                "waiting_for_provider",
                f"Waiting for capacity. {reason}",
            )

    async def _after_submit_error(
        self, job_id: int, provider_key: str, account_id: int, exc: ProviderError
    ) -> None:
        if exc.error_class == ErrorClass.UNCERTAIN:
            await self._after_uncertain_submit(job_id, exc.message)
            return

        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            await release_estimate(session, job, f"submit failed: {exc.error_class}")

            if exc.error_class == ErrorClass.AUTH:
                account = await session.get(Account, account_id)
                if account is not None:
                    account.auth_status = "failed"
                    account.last_auth_message = exc.message
                    account.last_auth_check_at = utcnow()
                await job_service.exclude_provider(
                    session, job, provider_key, "authentication rejected"
                )
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event="auth_failed",
                    message=(
                        f"{provider_key} rejected the credentials, so this will not be "
                        f"retried: {exc.message}"
                    ),
                    error_class=ErrorClass.AUTH,
                )
                return

            if exc.error_class == ErrorClass.INSUFFICIENT_CREDIT:
                await job_service.exclude_provider(
                    session, job, provider_key, "insufficient credit"
                )
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event="insufficient_credit",
                    message=(
                        f"{provider_key} reports insufficient credit, so this will not be "
                        f"retried on that account: {exc.message}"
                    ),
                    error_class=ErrorClass.INSUFFICIENT_CREDIT,
                )
                return

            if exc.error_class in (ErrorClass.INVALID_INPUT, ErrorClass.MODERATION):
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event=str(exc.error_class),
                    message=f"{provider_key} rejected this request permanently: {exc.message}",
                    error_class=exc.error_class,
                )
                return

            # Transient or rate limited: back off and try again, same provider.
            await job_service.schedule_retry(
                session,
                job,
                reason=exc.message,
                error_class=exc.error_class,
                retry_after_s=exc.retry_after_s,
            )

    async def _after_uncertain_submit(self, job_id: int, reason: str) -> None:
        """Handle a submit whose outcome we could not observe.

        The provider may already have created and charged for the task. The
        job is parked and reconciliation is attempted once; it is never
        resubmitted blindly.
        """
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            provider_key = job.provider_key
            client_token = job.client_token
            task_ref = job.task_ref
            await job_service.mark_uncertain(session, job, reason)

        adapter = registry.get_adapter(provider_key) if provider_key else None
        if adapter is None:
            return

        try:
            status = await adapter.reconcile(client_token, task_ref)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Reconcile attempt failed",
                extra={"job_id": job_id, "error": sanitize_provider_message(str(exc))},
            )
            return

        if status is None:
            async with session_scope() as session:
                job = await session.get(Job, job_id)
                if job is not None:
                    await job_service.record_event(
                        session,
                        job,
                        "reconcile_inconclusive",
                        f"{provider_key} could not tell us whether this task exists. "
                        "Resolve it by hand on the provider's dashboard.",
                    )
            return

        await self._apply_task_status(job_id, status, from_reconcile=True)

    # -- phase 2: poll ----------------------------------------------------

    async def _poll_phase(self) -> None:
        async with session_scope() as session:
            pollable = await job_service.pollable_jobs(session, POLL_BATCH)
            interval = int(await settings_store.get(session, "poll_interval_seconds"))
            plan = [
                (job.id, job.provider_key, job.task_ref, job.last_polled_at)
                for job in pollable
            ]

        now = utcnow()
        for job_id, provider_key, task_ref, last_polled in plan:
            if self._stop.is_set() or not provider_key or not task_ref:
                continue

            caps = registry.get_capabilities(provider_key)
            min_interval = max(interval, caps.min_poll_interval_s if caps else interval)
            if last_polled is not None and (now - last_polled).total_seconds() < min_interval:
                continue

            adapter = registry.get_adapter(provider_key)
            if adapter is None:
                continue

            try:
                status = await adapter.poll(task_ref)
            except ProviderError as exc:
                await self._note_poll_problem(job_id, exc.message, exc.error_class)
                continue
            except Exception as exc:  # noqa: BLE001
                await self._note_poll_problem(
                    job_id, sanitize_provider_message(str(exc)), ErrorClass.TRANSIENT
                )
                continue

            await self._apply_task_status(job_id, status)

    async def _note_poll_problem(
        self, job_id: int, message: str, error_class: ErrorClass
    ) -> None:
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            job.last_polled_at = utcnow()
            job.poll_count += 1
            if error_class in (ErrorClass.AUTH, ErrorClass.INSUFFICIENT_CREDIT):
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event="poll_failed",
                    message=f"Polling stopped: {message}",
                    error_class=error_class,
                )
            else:
                await job_service.record_event(
                    session, job, "poll_error", f"Status check failed, will retry: {message}"
                )

    async def _apply_task_status(
        self, job_id: int, status, *, from_reconcile: bool = False
    ) -> None:
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status in (JobStatus.CANCELLED, JobStatus.COMPLETED):
                return

            job.last_polled_at = utcnow()
            job.poll_count += 1

            # A reconcile may be how we first learn the task id, when the
            # submit response was lost. Adopting it is what makes the job
            # pollable instead of needing a second, chargeable submit.
            if status.task_ref and not job.task_ref:
                job.task_ref = status.task_ref

            if status.state in ("pending", "running"):
                if from_reconcile:
                    await job_service.transition(
                        session,
                        job,
                        JobStatus.PROCESSING,
                        event="reconciled",
                        message=(
                            "The provider does have this task and it is still running, so no "
                            "duplicate was created."
                        ),
                    )
                return

            if status.state == "succeeded":
                if not status.video_url:
                    await job_service.transition(
                        session,
                        job,
                        JobStatus.FAILED,
                        event="no_output_url",
                        message="The provider reported success but returned no video URL.",
                        error_class=ErrorClass.UNKNOWN,
                    )
                    return
                if status.actual_cost_usd is not None:
                    await record_actual(session, job, status.actual_cost_usd)
                job.settings = {**(job.settings or {}), "_video_url": status.video_url}
                await job_service.transition(
                    session,
                    job,
                    JobStatus.DOWNLOADING,
                    event="generation_finished",
                    message="Generation finished; downloading the video.",
                )
                return

            if status.state == "failed":
                error_class = status.error_class or ErrorClass.UNKNOWN
                await release_estimate(session, job, "provider reported failure")
                if selection.can_fall_back(error_class) and not job.provider_locked:
                    await job_service.exclude_provider(
                        session, job, job.provider_key or "", f"task failed: {error_class}"
                    )
                    job.task_ref = None
                    await job_service.schedule_retry(
                        session,
                        job,
                        reason=f"Provider task failed: {status.message}",
                        error_class=error_class,
                    )
                else:
                    await job_service.transition(
                        session,
                        job,
                        JobStatus.FAILED,
                        event="generation_failed",
                        message=status.message or "The provider reported a failure.",
                        error_class=error_class,
                    )
                self.stats.failed += 1
                return

            if status.state == "not_found":
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event="task_not_found",
                    message=(
                        "The provider no longer recognises this task id. It may have expired "
                        "from their history."
                    ),
                    error_class=ErrorClass.UNKNOWN,
                )
                return

            await job_service.record_event(
                session,
                job,
                "status_unknown",
                f"The provider returned an unrecognised state: {status.message}",
            )

    # -- phase 3: download ------------------------------------------------

    async def _download_phase(self) -> None:
        async with session_scope() as session:
            pending = await job_service.downloadable_jobs(session, DOWNLOAD_BATCH)
            plan = [(job.id, (job.settings or {}).get("_video_url")) for job in pending]

        for job_id, url in plan:
            if self._stop.is_set():
                return
            if not url:
                async with session_scope() as session:
                    job = await session.get(Job, job_id)
                    if job is not None:
                        await job_service.transition(
                            session,
                            job,
                            JobStatus.FAILED,
                            event="no_download_url",
                            message="No video URL was recorded for this job.",
                            error_class=ErrorClass.UNKNOWN,
                        )
                continue
            await self._download_one(job_id, url)

    async def _download_one(self, job_id: int, url: str) -> None:
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status != JobStatus.DOWNLOADING:
                return

            result = await downloads.download_to_library(session, job, url)
            if result.ok:
                await downloads.record_output(session, job, result, source_url=url)
                settings = JobSettings.from_dict(job.settings)
                account = (
                    await session.get(Account, job.account_id) if job.account_id else None
                )
                if account is not None and job.cost_estimate_units:
                    await credits.consume(
                        session,
                        account,
                        job.cost_estimate_units,
                        job.cost_estimate_unit or "credits",
                    )
                cleaned = dict(job.settings or {})
                cleaned.pop("_video_url", None)
                job.settings = cleaned
                await job_service.transition(
                    session,
                    job,
                    JobStatus.COMPLETED,
                    event="completed",
                    message=(
                        f"Saved {Path(result.path).name} "
                        f"({result.size_bytes / 1_048_576:.1f} MB"
                        + (
                            f", {result.media.duration_s:.2f}s measured"
                            if result.media and result.media.duration_s
                            else ", duration unmeasured"
                        )
                        + f"). Requested {settings.duration_s}s."
                    ),
                )
                # This is the only place a provider earns "verified". A
                # connection test only proves the credentials are accepted;
                # this proves the whole path works, start to finish.
                await provider_sync.mark_verified(
                    session,
                    job.provider_key,
                    f"Produced a real video for job #{job.id}.",
                )
                self.stats.completed += 1
                self.stats.downloads += 1
                return

            job.download_attempts += 1
            caps = registry.get_capabilities(job.provider_key or "")
            expiry = downloads.expiry_warning(
                caps.result_url_ttl_hours if caps else None, job.submitted_at
            )
            detail = f"{result.message} {expiry or ''}".strip()

            if job.download_attempts >= 3:
                await job_service.transition(
                    session,
                    job,
                    JobStatus.FAILED,
                    event="download_failed",
                    message=(
                        f"Could not download the generated video after "
                        f"{job.download_attempts} attempts. {detail} The provider may still "
                        "have the file; check its dashboard before regenerating, because "
                        "regenerating will spend again."
                    ),
                    error_class=ErrorClass.DOWNLOAD,
                )
            else:
                await job_service.record_event(
                    session,
                    job,
                    "download_retry",
                    f"Download attempt {job.download_attempts} failed: {detail}",
                )

    # -- phase 4: timeouts ------------------------------------------------

    async def _timeout_phase(self) -> None:
        async with session_scope() as session:
            limit_minutes = int(await settings_store.get(session, "job_timeout_minutes"))
            cutoff = utcnow() - timedelta(minutes=limit_minutes)
            stale = (
                await session.execute(
                    select(Job).where(
                        Job.status == JobStatus.PROCESSING,
                        Job.submitted_at.is_not(None),
                        Job.submitted_at < cutoff,
                    )
                )
            ).scalars().all()

            for job in stale:
                await job_service.mark_uncertain(
                    session,
                    job,
                    (
                        f"This task has been running for over {limit_minutes} minutes without "
                        f"finishing."
                    ),
                )

    # -- introspection ----------------------------------------------------

    async def describe(self) -> dict[str, object]:
        async with session_scope() as session:
            counts = await job_service.status_counts(session)
            paused = bool(await settings_store.get(session, "queue_paused"))
            providers = (
                await session.execute(select(Provider).where(Provider.enabled.is_(True)))
            ).scalars().all()
        return {
            "alive": self.alive,
            "paused": paused,
            "stats": self.stats,
            "counts": counts,
            "active": sum(counts.get(state, 0) for state in ACTIVE_JOB_STATES),
            "enabled_providers": [
                {"key": p.key, "name": p.display_name, "kind": p.kind} for p in providers
            ],
            "manual_providers": [
                key
                for key, caps in registry.all_capabilities().items()
                if caps.kind == ProviderKind.MANUAL
            ],
        }


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


def reset_scheduler() -> None:
    """Drop the singleton; used by tests."""
    global _scheduler
    _scheduler = None
