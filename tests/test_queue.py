"""Queue engine tests.

Everything here runs against :class:`MockProvider`, so no credits are spent
and no network is touched. The scheduler's phases are driven directly rather
than through its timer loop, which keeps the assertions deterministic.

The tests that matter most are the ones about money: a job whose outcome is
unknown must never be resubmitted or moved elsewhere, and one client token
must never produce two provider tasks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ErrorClass, ImageStatus, JobStatus
from app.models import Account, Image, Job, JobEvent, Output, Provider
from app.providers import registry
from app.providers.mock import MockBehaviour, MockProvider
from app.services import jobs as job_service
from app.services import selection, settings_store
from app.services.generation import JobSettings
from app.services.provider_sync import sync_providers
from app.services.scheduler import Scheduler, reset_scheduler
from app.services.timeutil import utcnow
from tests.conftest import write_image

VIDEO_URL = "https://mock.invalid/clip.mp4"
#: Comfortably above downloads.MIN_PLAUSIBLE_VIDEO_BYTES.
FAKE_VIDEO = b"\x00\x00\x00\x20ftypmp42" + b"\x11" * 40_000


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def behaviour(session: AsyncSession) -> AsyncIterator[MockBehaviour]:
    """Enable the mock provider and hand the test control of its behaviour."""
    registry.reset_cache()
    spec = MockBehaviour(generation_seconds=0.0, video_url=VIDEO_URL)
    registry.install("mock", MockProvider(behaviour=spec))

    await sync_providers(session)
    provider = await session.get(Provider, "mock")
    provider.enabled = True
    provider.priority = 1
    await settings_store.set_value(session, "queue_paused", False)
    await session.commit()

    yield spec

    registry.reset_cache()
    reset_scheduler()


@pytest.fixture
def scheduler() -> Scheduler:
    """A scheduler whose phases the test drives by hand."""
    return Scheduler()


async def sync_view(session: AsyncSession) -> None:
    """Make the test session see what the scheduler committed elsewhere.

    The scheduler writes through its own sessions, so the test session is
    holding a stale snapshot until its transaction is closed and its identity
    map invalidated.
    """
    await session.commit()
    session.expire_all()


async def make_image(session: AsyncSession, folder: Path, name: str = "shot.jpg") -> Image:
    path = write_image(folder / name)
    row = Image(
        path=str(path),
        filename=path.name,
        extension="jpg",
        sha256=f"sha-{name}",
        size_bytes=path.stat().st_size,
        width=640,
        height=480,
        mtime=path.stat().st_mtime,
        status=ImageStatus.NEW,
    )
    session.add(row)
    await session.flush()
    return row


async def queue_one(
    session: AsyncSession,
    folder: Path,
    *,
    name: str = "shot.jpg",
    provider_key: str | None = None,
    duration_s: int = 5,
) -> int:
    """Queue one image and return the new job's id."""
    image = await make_image(session, folder, name)
    result = await job_service.enqueue_images(
        session,
        [image],
        settings=JobSettings(duration_s=duration_s, resolution="720p", aspect_ratio="16:9"),
        provider_key=provider_key,
        literal_prompt="A slow push in on the subject.",
    )
    job_id = result.created[0].id
    await session.commit()
    return job_id


async def reload_job(session: AsyncSession, job_id: int) -> Job:
    await sync_view(session)
    job = await session.get(Job, job_id)
    assert job is not None
    return job


async def event_names(session: AsyncSession, job_id: int) -> list[str]:
    await sync_view(session)
    rows = (
        await session.execute(select(JobEvent.event).where(JobEvent.job_id == job_id))
    ).scalars().all()
    return list(rows)


def mock_download() -> respx.MockRouter:
    router = respx.mock(assert_all_called=False)
    router.get(VIDEO_URL).mock(
        return_value=httpx.Response(
            200, content=FAKE_VIDEO, headers={"content-type": "video/mp4"}
        )
    )
    return router


# ---------------------------------------------------------------------------
# Enqueueing
# ---------------------------------------------------------------------------


async def test_enqueue_creates_a_job_and_marks_the_image_queued(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job_id = await queue_one(session, image_dir)
    job = await reload_job(session, job_id)

    assert job.status == JobStatus.QUEUED
    assert job.client_token.startswith("avm-")
    assert job.manual_token
    assert job.provider_key is None, "the provider is chosen at submit time, not queue time"

    image = await session.get(Image, job.image_id)
    assert image.status == ImageStatus.QUEUED
    assert "created" in await event_names(session, job_id)


async def test_pinning_a_provider_locks_it(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job = await reload_job(session, await queue_one(session, image_dir, provider_key="mock"))

    assert job.provider_key == "mock"
    assert job.provider_locked is True
    assert job.settings["model_id"] == "mock-v1"


async def test_enqueue_will_not_duplicate_an_active_job(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job_id = await queue_one(session, image_dir)
    job = await reload_job(session, job_id)
    image = await session.get(Image, job.image_id)

    result = await job_service.enqueue_images(
        session,
        [image],
        settings=JobSettings(duration_s=5, resolution="720p", aspect_ratio="16:9"),
        literal_prompt="Another take.",
    )

    assert result.created == []
    assert len(result.skipped) == 1
    assert f"#{job_id}" in result.skipped[0][1]


async def test_enqueue_skips_a_missing_file(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    image = await make_image(session, image_dir)
    image.status = ImageStatus.MISSING
    await session.flush()

    result = await job_service.enqueue_images(
        session,
        [image],
        settings=JobSettings(duration_s=5, resolution="720p", aspect_ratio="16:9"),
        literal_prompt="Anything.",
    )

    assert result.created == []
    assert "missing" in result.skipped[0][1].lower()


async def test_settings_are_snapped_to_the_pinned_provider_and_reported(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    """An unsupported duration is adjusted, never silently accepted."""
    image = await make_image(session, image_dir)

    result = await job_service.enqueue_images(
        session,
        [image],
        settings=JobSettings(duration_s=7, resolution="4k", aspect_ratio="16:9"),
        provider_key="mock",
        literal_prompt="Anything.",
    )

    job = result.created[0]
    assert job.settings["duration_s"] == 8
    assert job.settings["resolution"] == "1080p"
    assert any("duration" in note for note in result.adjustments)
    assert any("resolution" in note for note in result.adjustments)


async def test_pinning_an_unknown_provider_is_refused_at_queue_time(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    image = await make_image(session, image_dir)

    with pytest.raises(job_service.JobError, match="not available"):
        await job_service.enqueue_images(
            session,
            [image],
            settings=JobSettings(duration_s=5, resolution="720p", aspect_ratio="16:9"),
            provider_key="no-such-provider",
            literal_prompt="Anything.",
        )


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


async def test_illegal_transition_is_refused(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job_id = await queue_one(session, image_dir)
    job = await reload_job(session, job_id)

    with pytest.raises(job_service.InvalidTransition):
        await job_service.transition(session, job, JobStatus.COMPLETED)

    assert (await reload_job(session, job_id)).status == JobStatus.QUEUED


async def test_a_job_that_was_never_submitted_cannot_become_uncertain(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    """There is no unknown outcome when no request was ever made."""
    job = await reload_job(session, await queue_one(session, image_dir))

    with pytest.raises(job_service.InvalidTransition):
        await job_service.mark_uncertain(session, job, "Nothing happened yet.")


async def test_every_status_has_a_transition_rule() -> None:
    """A missing rule would make a job unmovable at runtime."""
    for status in JobStatus:
        assert status in job_service.ALLOWED_TRANSITIONS, status


async def test_backoff_grows_then_stops_growing() -> None:
    assert job_service.backoff_delay(1) == 20.0
    assert job_service.backoff_delay(2) == 40.0
    assert job_service.backoff_delay(3) == 80.0
    assert job_service.backoff_delay(50) == 900.0


async def test_retry_is_scheduled_until_attempts_run_out(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job = await reload_job(session, await queue_one(session, image_dir))
    await settings_store.set_value(session, "max_attempts", 2)

    job.attempts = 1
    await job_service.schedule_retry(
        session, job, reason="Network wobble.", error_class=ErrorClass.TRANSIENT
    )
    assert job.status == JobStatus.QUEUED
    assert job.next_attempt_at is not None

    job.attempts = 2
    await job_service.schedule_retry(
        session, job, reason="Network wobble.", error_class=ErrorClass.TRANSIENT
    )
    assert job.status == JobStatus.FAILED
    assert "Giving up" in job.last_error


async def test_pause_resume_and_cancel(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job = await reload_job(session, await queue_one(session, image_dir))

    await job_service.pause_job(session, job)
    assert job.status == JobStatus.PAUSED

    await job_service.resume_job(session, job)
    assert job.status == JobStatus.QUEUED

    await job_service.cancel_job(session, job)
    assert job.status == JobStatus.CANCELLED

    with pytest.raises(job_service.JobError):
        await job_service.cancel_job(session, job)


# ---------------------------------------------------------------------------
# The expensive mistakes this design exists to prevent
# ---------------------------------------------------------------------------


async def test_an_uncertain_job_is_never_routed_anywhere(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job = await reload_job(session, await queue_one(session, image_dir))
    await job_service.transition(session, job, JobStatus.SUBMITTING)
    await job_service.mark_uncertain(session, job, "The submit timed out.")
    await session.commit()

    assert job.error_class == ErrorClass.UNCERTAIN
    result = await selection.select_provider(session, job)

    assert not result.ok
    assert result.all_permanent
    assert "paid for twice" in result.explain()


async def test_an_uncertain_job_cannot_be_retried_by_hand(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job = await reload_job(session, await queue_one(session, image_dir))
    await job_service.transition(session, job, JobStatus.SUBMITTING)
    await job_service.mark_uncertain(session, job, "The submit timed out.")
    await job_service.transition(
        session,
        job,
        JobStatus.FAILED,
        message="Given up on.",
        error_class=ErrorClass.UNCERTAIN,
    )

    with pytest.raises(job_service.JobError, match="Reconcile it first"):
        await job_service.retry_job(session, job)


async def test_fallback_is_allowed_only_after_a_provably_harmless_failure() -> None:
    assert selection.can_fall_back(ErrorClass.AUTH)
    assert selection.can_fall_back(ErrorClass.INSUFFICIENT_CREDIT)
    assert selection.can_fall_back(ErrorClass.RATE_LIMITED)
    assert selection.can_fall_back(ErrorClass.TRANSIENT)

    assert not selection.can_fall_back(ErrorClass.UNCERTAIN)
    assert not selection.can_fall_back(ErrorClass.DOWNLOAD)
    assert not selection.can_fall_back(ErrorClass.MODERATION)
    assert not selection.can_fall_back(None)


async def test_a_lost_submit_response_parks_the_job_and_reconciles_once(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    """The provider created the task but we never saw the reply.

    The job must not be resubmitted. Reconciliation should find the task that
    already exists and adopt it, leaving exactly one task on the provider.
    """
    behaviour.lose_submit_response = True
    behaviour.generation_seconds = 999.0
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()

    assert behaviour.submit_count == 1, "the job must not have been submitted twice"
    job = await reload_job(session, job_id)
    assert job.status == JobStatus.PROCESSING
    assert job.task_ref == behaviour.tokens[job.client_token]

    events = await event_names(session, job_id)
    assert "uncertain" in events
    assert "reconciled" in events


async def test_reconcile_that_finds_nothing_leaves_the_job_for_a_human(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.lose_submit_response = True
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()

    # Wipe the provider's memory of the token so reconcile cannot resolve it.
    behaviour.tokens.clear()
    job = await reload_job(session, job_id)
    job.task_ref = None
    job.status = JobStatus.UNCERTAIN
    await session.commit()

    await scheduler._after_uncertain_submit(job_id, "Testing an unresolvable outcome.")

    assert (await reload_job(session, job_id)).status == JobStatus.UNCERTAIN
    assert "reconcile_inconclusive" in await event_names(session, job_id)


async def test_one_client_token_only_ever_produces_one_provider_task(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.generation_seconds = 999.0
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()
    first_ref = (await reload_job(session, job_id)).task_ref
    assert first_ref

    # Force a second submit with the same token, as a crash-recovery path
    # would. The provider must return the same task, not start a new one.
    job = await reload_job(session, job_id)
    job.status = JobStatus.QUEUED
    job.task_ref = None
    await session.commit()
    await scheduler._submit_phase()

    assert behaviour.submit_count == 2, "two calls were made"
    assert len(behaviour.tokens) == 1, "but only one task exists at the provider"
    assert (await reload_job(session, job_id)).task_ref == first_ref


# ---------------------------------------------------------------------------
# Error handling at submit time
# ---------------------------------------------------------------------------


async def test_an_auth_failure_is_not_retried(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.fail_submit_with = ErrorClass.AUTH
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_class == ErrorClass.AUTH
    assert "mock" in (job.excluded_providers or [])

    account = (await session.execute(select(Account))).scalars().first()
    assert account.auth_status == "failed"


async def test_insufficient_credit_is_not_retried(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.fail_submit_with = ErrorClass.INSUFFICIENT_CREDIT
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_class == ErrorClass.INSUFFICIENT_CREDIT
    assert "mock" in (job.excluded_providers or [])


async def test_a_transient_failure_is_retried_after_a_backoff(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.fail_submit_with = ErrorClass.TRANSIENT
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.QUEUED
    assert job.next_attempt_at is not None
    assert job.next_attempt_at > utcnow()
    assert "retry_scheduled" in await event_names(session, job_id)


async def test_a_job_whose_source_image_vanished_fails_without_calling_out(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    job_id = await queue_one(session, image_dir)
    job = await reload_job(session, job_id)
    Path((await session.get(Image, job.image_id)).path).unlink()
    await session.commit()

    await scheduler._submit_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_class == ErrorClass.INVALID_INPUT
    assert behaviour.submit_count == 0


async def test_a_paused_queue_submits_nothing(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    await queue_one(session, image_dir)
    await settings_store.set_value(session, "queue_paused", True)
    await session.commit()

    await scheduler._tick()

    assert behaviour.submit_count == 0


async def test_a_paused_queue_still_tracks_work_already_submitted(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    """Abandoning an in-flight job would throw away credits already spent."""
    job_id = await queue_one(session, image_dir)
    await scheduler._submit_phase()

    await settings_store.set_value(session, "queue_paused", True)
    await session.commit()

    with mock_download():
        await scheduler._tick()
        await scheduler._tick()

    assert (await reload_job(session, job_id)).status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# Polling, downloading and timeouts
# ---------------------------------------------------------------------------


async def test_a_job_runs_from_queued_to_completed(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()
    job = await reload_job(session, job_id)
    assert job.status == JobStatus.PROCESSING
    assert job.provider_key == "mock"
    assert job.account_id is not None
    assert job.cost_estimate_usd == 0.0

    await scheduler._poll_phase()
    assert (await reload_job(session, job_id)).status == JobStatus.DOWNLOADING

    with mock_download():
        await scheduler._download_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.COMPLETED
    assert "_video_url" not in (job.settings or {})

    output = (
        await session.execute(select(Output).where(Output.job_id == job_id))
    ).scalar_one()
    stored = Path(output.file_path)
    assert stored.exists()
    assert output.file_size == len(FAKE_VIDEO)
    assert stored.with_suffix(".mp4.json").exists(), "sidecar metadata is written"

    image = await session.get(Image, job.image_id)
    assert image.status == ImageStatus.COMPLETED


async def test_a_generation_failure_falls_back_rather_than_giving_up(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.fail_task_with = ErrorClass.TRANSIENT
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()
    await scheduler._poll_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.QUEUED
    assert "mock" in (job.excluded_providers or [])
    assert job.task_ref is None


async def test_a_moderation_failure_is_final(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.fail_task_with = ErrorClass.MODERATION
    job_id = await queue_one(session, image_dir)

    await scheduler._submit_phase()
    await scheduler._poll_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.FAILED
    assert job.error_class == ErrorClass.MODERATION


async def test_a_download_is_attempted_three_times_before_failing(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    job_id = await queue_one(session, image_dir)
    await scheduler._submit_phase()
    await scheduler._poll_phase()

    with respx.mock(assert_all_called=False) as router:
        router.get(VIDEO_URL).mock(return_value=httpx.Response(404))
        for _ in range(3):
            await scheduler._download_phase()

    job = await reload_job(session, job_id)
    assert job.download_attempts == 3
    assert job.status == JobStatus.FAILED
    assert job.error_class == ErrorClass.DOWNLOAD
    assert "regenerating will spend again" in job.last_error


async def test_a_tiny_response_body_is_rejected_as_not_a_video(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    job_id = await queue_one(session, image_dir)
    await scheduler._submit_phase()
    await scheduler._poll_phase()

    with respx.mock(assert_all_called=False) as router:
        router.get(VIDEO_URL).mock(
            return_value=httpx.Response(200, html="<html>Rate limited</html>")
        )
        await scheduler._download_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.DOWNLOADING, "still retryable, not given up on"
    assert job.download_attempts == 1
    assert (
        await session.execute(select(Output).where(Output.job_id == job_id))
    ).scalar_one_or_none() is None


async def test_a_task_running_far_too_long_becomes_uncertain_not_failed(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    """A stuck task may still be charged for, so it is parked, not retried."""
    behaviour.generation_seconds = 999.0
    job_id = await queue_one(session, image_dir)
    await scheduler._submit_phase()

    job = await reload_job(session, job_id)
    job.submitted_at = utcnow() - timedelta(hours=4)
    await session.commit()

    await scheduler._timeout_phase()

    job = await reload_job(session, job_id)
    assert job.status == JobStatus.UNCERTAIN
    assert job.error_class == ErrorClass.UNCERTAIN


async def test_polling_respects_the_minimum_interval(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    behaviour.generation_seconds = 999.0
    job_id = await queue_one(session, image_dir)
    await scheduler._submit_phase()

    await settings_store.set_value(session, "poll_interval_seconds", 600)
    await session.commit()

    await scheduler._poll_phase()
    first = (await reload_job(session, job_id)).poll_count
    assert first == 1

    await scheduler._poll_phase()
    assert (await reload_job(session, job_id)).poll_count == first


# ---------------------------------------------------------------------------
# Crash recovery - the M4 acceptance criterion
# ---------------------------------------------------------------------------


async def test_a_crash_during_submit_leaves_the_job_uncertain(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    """No task reference means we never learned whether the provider accepted."""
    job_id = await queue_one(session, image_dir)
    job = await reload_job(session, job_id)
    job.status = JobStatus.SUBMITTING
    job.provider_key = "mock"
    job.task_ref = None
    await session.commit()

    summary = await job_service.recover_interrupted(session)
    await session.commit()

    assert summary["submitting_to_uncertain"] == 1
    job = await reload_job(session, job_id)
    assert job.status == JobStatus.UNCERTAIN
    assert job.error_class == ErrorClass.UNCERTAIN


async def test_a_crash_after_submit_resumes_polling(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    job_id = await queue_one(session, image_dir)
    job = await reload_job(session, job_id)
    job.status = JobStatus.SUBMITTING
    job.provider_key = "mock"
    job.task_ref = "mock-task-1"
    await session.commit()

    await job_service.recover_interrupted(session)
    await session.commit()

    assert (await reload_job(session, job_id)).status == JobStatus.PROCESSING
    assert "recovered" in await event_names(session, job_id)


async def test_a_crash_mid_download_is_simply_repeated(
    session: AsyncSession,
    behaviour: MockBehaviour,
    scheduler: Scheduler,
    image_dir: Path,
) -> None:
    """The provider already finished, so re-downloading costs nothing."""
    job_id = await queue_one(session, image_dir)
    await scheduler._submit_phase()
    await scheduler._poll_phase()
    assert (await reload_job(session, job_id)).status == JobStatus.DOWNLOADING

    await job_service.recover_interrupted(session)
    await session.commit()

    assert (await reload_job(session, job_id)).status == JobStatus.DOWNLOADING

    with mock_download():
        await scheduler._download_phase()
    assert (await reload_job(session, job_id)).status == JobStatus.COMPLETED


async def test_a_full_restart_resumes_every_in_flight_job(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    """Kill the process mid-run with jobs in several live states."""
    behaviour.generation_seconds = 999.0
    first = await queue_one(session, image_dir, name="a.jpg")
    second = await queue_one(session, image_dir, name="b.jpg")
    third = await queue_one(session, image_dir, name="c.jpg")

    await Scheduler()._submit_phase()

    # Job two is caught mid-submit with a task reference, job three without.
    second_row = await reload_job(session, second)
    second_row.status = JobStatus.SUBMITTING
    second_row.task_ref = "mock-task-99"
    third_row = await reload_job(session, third)
    third_row.status = JobStatus.SUBMITTING
    third_row.task_ref = None
    await session.commit()

    # "Restart": a brand new scheduler over the same database.
    restarted = Scheduler()
    await restarted.start()
    await restarted.stop()

    await sync_view(session)
    states = {
        row.id: row.status
        for row in (await session.execute(select(Job))).scalars().all()
    }

    assert states[first] == JobStatus.PROCESSING, "already submitted, keeps going"
    assert states[second] == JobStatus.PROCESSING, "has a task id, so polling resumes"
    assert states[third] == JobStatus.UNCERTAIN, "outcome unknown, so it is parked"


async def test_queue_state_survives_a_fresh_read(
    session: AsyncSession, behaviour: MockBehaviour, image_dir: Path
) -> None:
    """Nothing about the queue lives only in memory."""
    job_id = await queue_one(session, image_dir)
    counts = await job_service.status_counts(session)
    assert counts[JobStatus.QUEUED] == 1

    await sync_view(session)
    reread = await session.get(Job, job_id)
    assert reread.prompt == "A slow push in on the subject."
    assert reread.settings["duration_s"] == 5
