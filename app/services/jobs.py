"""Job lifecycle: creation, state transitions and history.

The state machine is explicit and enforced. Every move is checked against
:data:`ALLOWED_TRANSITIONS` and recorded as a :class:`JobEvent`, which is what
makes "why did this job fail?" answerable from the UI without reading logs.

The rule that matters most: a job whose submit outcome is unknown goes to
``uncertain`` and stops. It is never retried here and never moved to another
provider, because either action risks paying twice for one clip.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    ACTIVE_JOB_STATES,
    TERMINAL_JOB_STATES,
    ErrorClass,
    ImageStatus,
    JobStatus,
    ProviderKind,
)
from app.models import Batch, Image, Job, JobEvent
from app.providers import registry
from app.services import prompts as prompt_service
from app.services import settings_store
from app.services.generation import JobSettings, match_to_provider
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

#: Which status may follow which. Anything absent is a programming error.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    JobStatus.QUEUED: frozenset(
        {
            JobStatus.SUBMITTING,
            JobStatus.AWAITING_MANUAL,
            JobStatus.PAUSED,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.SUBMITTING: frozenset(
        {
            JobStatus.PROCESSING,
            JobStatus.DOWNLOADING,
            JobStatus.QUEUED,
            JobStatus.FAILED,
            JobStatus.UNCERTAIN,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PROCESSING: frozenset(
        {
            JobStatus.DOWNLOADING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.QUEUED,
            JobStatus.UNCERTAIN,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.DOWNLOADING: frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.PROCESSING,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.AWAITING_MANUAL: frozenset(
        {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.QUEUED}
    ),
    JobStatus.PAUSED: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.UNCERTAIN: frozenset(
        {
            JobStatus.PROCESSING,
            JobStatus.DOWNLOADING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.QUEUED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.CANCELLED: frozenset({JobStatus.QUEUED}),
}


class JobError(ValueError):
    """Raised for invalid queue operations."""


class InvalidTransition(JobError):
    pass


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def new_client_token() -> str:
    """Idempotency key, generated once per job and reused on every retry."""
    return f"avm-{secrets.token_hex(16)}"


def new_manual_token() -> str:
    """Short code the user can put in a downloaded filename."""
    return f"avm-{secrets.token_hex(3)}"


@dataclass
class EnqueueResult:
    created: list[Job]
    skipped: list[tuple[int, str]]
    batch: Batch | None
    adjustments: list[str]

    @property
    def summary(self) -> str:
        parts = [f"{len(self.created)} job(s) queued"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.adjustments:
            parts.append(f"{len(self.adjustments)} setting adjustment(s)")
        return ", ".join(parts)


async def enqueue_images(
    session: AsyncSession,
    images: list[Image],
    *,
    settings: JobSettings,
    provider_key: str | None = None,
    account_id: int | None = None,
    template_id: int | None = None,
    literal_prompt: str | None = None,
    literal_negative: str | None = None,
    batch_name: str | None = None,
    priority: int = 100,
    allow_duplicate_jobs: bool = False,
) -> EnqueueResult:
    """Create one job per image.

    ``provider_key`` pins the provider and locks it against fallback. Leaving
    it ``None`` defers the choice to the selection engine at submit time, so
    the decision uses the credit and availability picture that exists then
    rather than when the button was pressed.
    """
    created: list[Job] = []
    skipped: list[tuple[int, str]] = []
    adjustments: list[str] = []

    batch: Batch | None = None
    if batch_name:
        batch = Batch(name=batch_name.strip()[:200])
        session.add(batch)
        await session.flush()

    effective_settings = settings
    if provider_key:
        match = match_to_provider(provider_key, settings)
        if not match.ok:
            raise JobError(match.summary())
        effective_settings = match.settings
        adjustments.extend(a.describe() for a in match.adjustments)

    for image in images:
        if image.status == ImageStatus.MISSING:
            skipped.append((image.id, "File is missing from disk."))
            continue

        if not allow_duplicate_jobs:
            existing = await _active_job_for_image(session, image.id)
            if existing is not None:
                skipped.append(
                    (
                        image.id,
                        f"Job #{existing.id} for this image is already {existing.status}.",
                    )
                )
                continue

        try:
            resolved = await prompt_service.resolve_for_image(
                session,
                image,
                template_id=template_id,
                literal_prompt=literal_prompt,
                literal_negative=literal_negative,
            )
        except prompt_service.PromptError as exc:
            skipped.append((image.id, str(exc)))
            continue

        job = Job(
            image_id=image.id,
            batch_id=batch.id if batch else None,
            prompt=resolved.prompt,
            negative_prompt=resolved.negative_prompt,
            settings=effective_settings.to_dict(),
            provider_key=provider_key,
            account_id=account_id,
            provider_locked=bool(provider_key),
            status=JobStatus.QUEUED,
            priority=priority,
            client_token=new_client_token(),
            manual_token=new_manual_token(),
            excluded_providers=[],
        )
        session.add(job)
        await session.flush()

        await record_event(
            session,
            job,
            "created",
            f"Queued from {image.filename}."
            + (f" Provider pinned to {provider_key}." if provider_key else ""),
            {
                "prompt_source": resolved.source,
                "template": resolved.template_name,
                "unresolved_tokens": list(resolved.unresolved_tokens),
                "settings": effective_settings.to_dict(),
            },
        )
        if resolved.unresolved_tokens:
            await record_event(
                session,
                job,
                "prompt_warning",
                "Prompt contains tokens with no value: "
                + ", ".join(resolved.unresolved_tokens),
            )

        image.status = ImageStatus.QUEUED
        created.append(job)

    await session.flush()
    return EnqueueResult(created=created, skipped=skipped, batch=batch, adjustments=adjustments)


async def _active_job_for_image(session: AsyncSession, image_id: int) -> Job | None:
    """Find a job for this image that is not finished, to block re-queueing."""
    blocking = (
        set(ACTIVE_JOB_STATES)
        | {JobStatus.QUEUED, JobStatus.PAUSED, JobStatus.UNCERTAIN, JobStatus.AWAITING_MANUAL}
    )
    return (
        await session.execute(
            select(Job)
            .where(Job.image_id == image_id, Job.status.in_(tuple(blocking)))
            .order_by(Job.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


async def record_event(
    session: AsyncSession,
    job: Job,
    event: str,
    message: str | None = None,
    detail: dict | None = None,
) -> JobEvent:
    row = JobEvent(
        job_id=job.id, event=event, message=message, detail=detail or {}
    )
    session.add(row)
    await session.flush()
    return row


async def transition(
    session: AsyncSession,
    job: Job,
    new_status: str,
    *,
    event: str | None = None,
    message: str | None = None,
    detail: dict | None = None,
    error_class: ErrorClass | None = None,
) -> Job:
    """Move a job to a new state, enforcing the state machine."""
    old_status = job.status
    if old_status == new_status:
        if message:
            await record_event(session, job, event or "note", message, detail)
        return job

    permitted = ALLOWED_TRANSITIONS.get(old_status, frozenset())
    if new_status not in permitted:
        raise InvalidTransition(
            f"Job #{job.id} cannot move from '{old_status}' to '{new_status}'."
        )

    job.status = new_status
    job.updated_at = utcnow()

    if new_status == JobStatus.SUBMITTING:
        job.attempts += 1
    if new_status in (JobStatus.PROCESSING, JobStatus.DOWNLOADING) and job.submitted_at is None:
        job.submitted_at = utcnow()
    if new_status in TERMINAL_JOB_STATES:
        job.completed_at = utcnow()
    if new_status == JobStatus.QUEUED:
        # A fresh attempt starts with a clean error surface.
        job.last_error = None
        job.error_class = None
    else:
        # The classification must stick for every non-success state, not just
        # ``failed``. ``uncertain`` in particular is what stops the selection
        # engine from routing a job that may already have been charged for.
        if error_class is not None:
            job.error_class = error_class
        if new_status in (JobStatus.FAILED, JobStatus.UNCERTAIN):
            job.last_error = message or job.last_error

    await record_event(
        session,
        job,
        event or f"{old_status}->{new_status}",
        message,
        {**(detail or {}), "from": old_status, "to": new_status},
    )
    log.info(
        "Job transition",
        extra={
            "job_id": job.id,
            "from": old_status,
            "to": new_status,
            "provider": job.provider_key,
        },
    )
    await _sync_image_status(session, job)
    return job


async def _sync_image_status(session: AsyncSession, job: Job) -> None:
    """Keep the image's own status roughly in step with its latest job."""
    image = await session.get(Image, job.image_id)
    if image is None or image.status == ImageStatus.MISSING:
        return

    if job.status == JobStatus.COMPLETED:
        image.status = ImageStatus.COMPLETED
    elif job.status in ACTIVE_JOB_STATES:
        image.status = ImageStatus.PROCESSING
    elif job.status == JobStatus.FAILED:
        # Only mark the image failed when nothing else is still in flight.
        other = await _active_job_for_image(session, image.id)
        if other is None:
            image.status = ImageStatus.FAILED
    elif job.status in (JobStatus.QUEUED, JobStatus.PAUSED, JobStatus.AWAITING_MANUAL):
        image.status = ImageStatus.QUEUED
    elif job.status == JobStatus.CANCELLED:
        other = await _active_job_for_image(session, image.id)
        if other is None:
            image.status = ImageStatus.NEW


def backoff_delay(attempts: int, *, base: float = 20.0, cap: float = 900.0) -> float:
    """Exponential backoff in seconds, bounded so a job is not parked forever."""
    delay = base * (2 ** max(0, attempts - 1))
    return min(delay, cap)


async def schedule_retry(
    session: AsyncSession,
    job: Job,
    *,
    reason: str,
    error_class: ErrorClass,
    retry_after_s: float | None = None,
) -> Job:
    """Return a job to the queue with a backoff, or fail it if out of attempts."""
    max_attempts = int(await settings_store.get(session, "max_attempts"))
    if job.attempts >= max_attempts:
        return await transition(
            session,
            job,
            JobStatus.FAILED,
            event="retries_exhausted",
            message=f"{reason} Giving up after {job.attempts} attempt(s).",
            error_class=error_class,
        )

    delay = retry_after_s if retry_after_s is not None else backoff_delay(job.attempts)
    job.next_attempt_at = utcnow() + timedelta(seconds=delay)
    job.task_ref = None
    return await transition(
        session,
        job,
        JobStatus.QUEUED,
        event="retry_scheduled",
        message=f"{reason} Retrying in {int(delay)}s (attempt {job.attempts + 1} of {max_attempts}).",
        detail={"error_class": str(error_class), "delay_s": delay},
    )


async def mark_uncertain(session: AsyncSession, job: Job, reason: str) -> Job:
    """Park a job whose provider-side outcome is unknown.

    Deliberately terminal for automation. Resubmitting could pay twice, and
    moving it to another provider could pay twice on two providers, so the
    only ways out are a successful reconcile or an explicit human decision.
    """
    return await transition(
        session,
        job,
        JobStatus.UNCERTAIN,
        event="uncertain",
        message=(
            f"{reason} The provider may or may not have accepted this job, so it will "
            "not be retried automatically. Reconcile it or resolve it by hand."
        ),
        error_class=ErrorClass.UNCERTAIN,
    )


# ---------------------------------------------------------------------------
# User actions
# ---------------------------------------------------------------------------


async def cancel_job(session: AsyncSession, job: Job) -> Job:
    if job.status in TERMINAL_JOB_STATES:
        raise JobError(f"Job #{job.id} is already {job.status}.")
    if job.status in ACTIVE_JOB_STATES:
        # The provider is already working; we stop tracking but cannot
        # un-spend whatever it has charged.
        return await transition(
            session,
            job,
            JobStatus.CANCELLED,
            event="cancelled",
            message=(
                "Cancelled locally. The provider was already working on this task, "
                "so any credits it consumed are not recoverable."
            ),
        )
    return await transition(
        session, job, JobStatus.CANCELLED, event="cancelled", message="Cancelled before submission."
    )


async def retry_job(session: AsyncSession, job: Job, *, clear_provider: bool = False) -> Job:
    """Put a finished-but-unsuccessful job back in the queue."""
    if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        raise JobError(
            f"Only failed or cancelled jobs can be retried. Job #{job.id} is {job.status}."
        )
    if job.error_class == ErrorClass.UNCERTAIN:
        raise JobError(
            f"Job #{job.id} has an unknown provider outcome. Reconcile it first so a "
            "retry cannot pay for the same clip twice."
        )

    job.attempts = 0
    job.download_attempts = 0
    job.next_attempt_at = None
    job.task_ref = None
    if clear_provider and not job.provider_locked:
        job.provider_key = None
        job.account_id = None
        job.excluded_providers = []

    return await transition(
        session,
        job,
        JobStatus.QUEUED,
        event="retry_requested",
        message="Requeued by the user.",
    )


async def pause_job(session: AsyncSession, job: Job) -> Job:
    if job.status != JobStatus.QUEUED:
        raise JobError(f"Only queued jobs can be paused. Job #{job.id} is {job.status}.")
    return await transition(
        session, job, JobStatus.PAUSED, event="paused", message="Paused by the user."
    )


async def resume_job(session: AsyncSession, job: Job) -> Job:
    if job.status != JobStatus.PAUSED:
        raise JobError(f"Job #{job.id} is not paused.")
    return await transition(
        session, job, JobStatus.QUEUED, event="resumed", message="Resumed by the user."
    )


async def exclude_provider(session: AsyncSession, job: Job, provider_key: str, reason: str) -> None:
    """Rule a provider out for this job so fallback does not loop back to it."""
    excluded = list(job.excluded_providers or [])
    if provider_key not in excluded:
        excluded.append(provider_key)
        job.excluded_providers = excluded
        await record_event(
            session,
            job,
            "provider_excluded",
            f"{provider_key} ruled out for this job: {reason}",
        )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@dataclass
class JobQuery:
    status: str | None = None
    provider_key: str | None = None
    batch_id: int | None = None
    page: int = 1
    page_size: int = 50

    @property
    def offset(self) -> int:
        return max(0, (self.page - 1) * self.page_size)


async def list_jobs(session: AsyncSession, query: JobQuery) -> tuple[list[Job], int]:
    stmt = select(Job)
    count_stmt = select(func.count(Job.id))
    conditions = []
    if query.status:
        conditions.append(Job.status == query.status)
    if query.provider_key:
        conditions.append(Job.provider_key == query.provider_key)
    if query.batch_id:
        conditions.append(Job.batch_id == query.batch_id)
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = int((await session.execute(count_stmt)).scalar() or 0)
    rows = (
        await session.execute(
            stmt.order_by(Job.id.desc()).offset(query.offset).limit(query.page_size)
        )
    ).scalars().all()
    return list(rows), total


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(Job.status, func.count()).group_by(Job.status))).all()
    return {status: count for status, count in rows}


async def job_events(session: AsyncSession, job_id: int, limit: int = 60) -> list[JobEvent]:
    rows = (
        await session.execute(
            select(JobEvent)
            .where(JobEvent.job_id == job_id)
            .order_by(JobEvent.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def claimable_jobs(session: AsyncSession, limit: int = 20) -> list[Job]:
    """Queued jobs whose backoff has elapsed, highest priority first."""
    now = utcnow()
    rows = (
        await session.execute(
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED,
                (Job.next_attempt_at.is_(None)) | (Job.next_attempt_at <= now),
            )
            .order_by(Job.priority, Job.id)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def pollable_jobs(session: AsyncSession, limit: int = 50) -> list[Job]:
    rows = (
        await session.execute(
            select(Job)
            .where(Job.status == JobStatus.PROCESSING, Job.task_ref.is_not(None))
            .order_by(Job.last_polled_at.is_not(None), Job.last_polled_at, Job.id)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def downloadable_jobs(session: AsyncSession, limit: int = 10) -> list[Job]:
    rows = (
        await session.execute(
            select(Job)
            .where(Job.status == JobStatus.DOWNLOADING)
            .order_by(Job.id)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def active_count(session: AsyncSession, provider_key: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Job.id)).where(
                    Job.provider_key == provider_key,
                    Job.status.in_(tuple(ACTIVE_JOB_STATES)),
                )
            )
        ).scalar()
        or 0
    )


async def is_manual_provider(provider_key: str | None) -> bool:
    if not provider_key:
        return False
    caps = registry.get_capabilities(provider_key)
    return caps is not None and caps.kind == ProviderKind.MANUAL


async def recover_interrupted(session: AsyncSession) -> dict[str, int]:
    """Repair job states left mid-flight by a crash or a hard shutdown.

    Called once at startup. ``submitting`` is the dangerous state: the process
    died without learning whether the provider accepted the job, so those go
    to ``uncertain`` rather than being retried.
    """
    summary = {"submitting_to_uncertain": 0, "downloading_requeued": 0}

    stranded = (
        await session.execute(select(Job).where(Job.status == JobStatus.SUBMITTING))
    ).scalars().all()
    for job in stranded:
        if job.task_ref:
            # We did get a task id, so polling can establish the truth.
            await transition(
                session,
                job,
                JobStatus.PROCESSING,
                event="recovered",
                message="Recovered after restart; a task reference exists, so polling resumes.",
            )
        else:
            await mark_uncertain(
                session,
                job,
                "The application stopped while this job was being submitted.",
            )
            summary["submitting_to_uncertain"] += 1

    # A download that was cut off is safe to repeat: the provider has already
    # finished and charged, and partial files are discarded.
    interrupted = (
        await session.execute(select(Job).where(Job.status == JobStatus.DOWNLOADING))
    ).scalars().all()
    for job in interrupted:
        await record_event(
            session,
            job,
            "recovered",
            "Download was interrupted by a restart; it will be attempted again.",
        )
        summary["downloading_requeued"] += 1

    if any(summary.values()):
        log.warning("Recovered interrupted jobs", extra=summary)
    return summary
