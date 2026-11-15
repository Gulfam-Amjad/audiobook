"""Manual assist: worksheets and inbox matching.

No test opens a website or spends a credit. Files are local, and the
suite-wide ffprobe stub in conftest keeps a machine with FFmpeg installed
from rejecting the stand-in video.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import JobStatus
from app.models import Account, Image, InboxItem, Job
from app.providers.manual_sites import SITES
from app.services import manual_assist, prompts, provider_sync
from app.services.generation import JobSettings
from app.services.manual_assist import ManualAssistError
from tests.conftest import write_image

pytestmark = pytest.mark.usefixtures("data_root")


async def ready(session: AsyncSession) -> None:
    await provider_sync.sync_providers(session)
    await prompts.ensure_starter_templates(session)
    await session.commit()


async def add_image(session: AsyncSession, folder: Path, name: str) -> Image:
    path = write_image(folder / name)
    row = Image(
        path=str(path),
        filename=name,
        extension="jpg",
        sha256=f"sha-{name}",
        size_bytes=path.stat().st_size,
        width=640,
        height=480,
        status="new",
    )
    session.add(row)
    await session.flush()
    return row


def drop_video(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\x00" * (12 * 1024))
    return path


async def test_every_site_gets_an_account_and_an_allowance(session: AsyncSession) -> None:
    await ready(session)
    views = await manual_assist.site_views(session)
    assert {view.site.key for view in views} == {site.key for site in SITES}
    assert all(view.entitlement.balance is None for view in views)
    assert all(view.entitlement.source == "unknown" for view in views)


async def test_preparing_a_worksheet_does_not_submit_anything(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    image = await add_image(session, tmp_path, "a.jpg")

    message = await manual_assist.prepare_batch(
        session, "pixverse", [image], JobSettings()
    )

    job = (await session.execute(select(Job))).scalar_one()
    assert job.status == JobStatus.AWAITING_MANUAL
    assert job.provider_key == "manual"
    assert job.provider_locked
    assert job.manual_token
    assert "pixverse" in message
    provider_enabled = (
        await session.execute(select(Account).where(Account.id == job.account_id))
    ).scalar_one()
    assert manual_assist.site_key_of(provider_enabled) == "pixverse"


async def test_a_match_code_in_the_filename_files_the_right_job(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    image = await add_image(session, tmp_path, "b.jpg")
    await manual_assist.prepare_batch(session, "kling", [image], JobSettings())
    job = (await session.execute(select(Job))).scalar_one()

    # Dropped in the wrong site's folder on purpose. The code still wins.
    drop_video(manual_assist.site_inbox("luma"), f"take-{job.manual_token}.mp4")
    notes = await manual_assist.scan_inbox(session)

    await session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert any(str(job.id) in note for note in notes)
    assert "4.00s measured" in notes[0]


async def test_a_code_for_a_finished_job_is_not_reused(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    image = await add_image(session, tmp_path, "c.jpg")
    await manual_assist.prepare_batch(session, "kling", [image], JobSettings())
    job = (await session.execute(select(Job))).scalar_one()
    token = job.manual_token
    drop_video(manual_assist.site_inbox("kling"), f"{token}.mp4")
    await manual_assist.scan_inbox(session)

    drop_video(manual_assist.site_inbox("kling"), f"again-{token}.mp4")
    await manual_assist.scan_inbox(session)

    unmatched = await manual_assist.unmatched_items(session)
    assert len(unmatched) == 1
    assert "no longer waiting" in (unmatched[0].message or "")


async def test_a_file_with_no_code_goes_to_the_oldest_job_in_that_folder(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    first = await add_image(session, tmp_path, "d1.jpg")
    second = await add_image(session, tmp_path, "d2.jpg")
    await manual_assist.prepare_batch(session, "hailuo", [first], JobSettings())
    await manual_assist.prepare_batch(session, "hailuo", [second], JobSettings())
    jobs = list(
        (await session.execute(select(Job).order_by(Job.id))).scalars().all()
    )

    drop_video(manual_assist.site_inbox("hailuo"), "download.mp4")
    await manual_assist.scan_inbox(session)

    await session.refresh(jobs[0])
    await session.refresh(jobs[1])
    assert jobs[0].status == JobStatus.COMPLETED
    assert jobs[1].status == JobStatus.AWAITING_MANUAL


async def test_a_wrong_code_is_not_guessed_by_arrival_order(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    image = await add_image(session, tmp_path, "e.jpg")
    await manual_assist.prepare_batch(session, "pika", [image], JobSettings())

    drop_video(manual_assist.site_inbox("pika"), "clip-avm-ffffff.mp4")
    await manual_assist.scan_inbox(session)

    job = (await session.execute(select(Job))).scalar_one()
    assert job.status == JobStatus.AWAITING_MANUAL
    unmatched = await manual_assist.unmatched_items(session)
    assert len(unmatched) == 1


async def test_a_file_in_the_inbox_root_is_not_guessed(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    image = await add_image(session, tmp_path, "f.jpg")
    await manual_assist.prepare_batch(session, "vidu", [image], JobSettings())

    drop_video(manual_assist.inbox_root(), "loose.mp4")
    await manual_assist.scan_inbox(session)

    job = (await session.execute(select(Job))).scalar_one()
    assert job.status == JobStatus.AWAITING_MANUAL


async def test_the_user_can_attach_an_unmatched_file(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    image = await add_image(session, tmp_path, "g.jpg")
    await manual_assist.prepare_batch(session, "seaart", [image], JobSettings())
    job = (await session.execute(select(Job))).scalar_one()
    drop_video(manual_assist.inbox_root(), "loose.mp4")
    await manual_assist.scan_inbox(session)
    item = (await manual_assist.unmatched_items(session))[0]

    message = await manual_assist.assign_inbox_item(session, item.id, job.id)

    await session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert "measured" in message


async def test_assigning_to_a_job_that_is_not_waiting_is_refused(
    session: AsyncSession, tmp_path: Path
) -> None:
    await ready(session)
    drop_video(manual_assist.inbox_root(), "loose.mp4")
    await manual_assist.scan_inbox(session)
    item = (await manual_assist.unmatched_items(session))[0]

    with pytest.raises(ManualAssistError):
        await manual_assist.assign_inbox_item(session, item.id, 99999)


async def test_a_second_scan_does_not_duplicate_an_unmatched_row(
    session: AsyncSession,
) -> None:
    await ready(session)
    drop_video(manual_assist.inbox_root(), "loose.mp4")
    await manual_assist.scan_inbox(session)
    await manual_assist.scan_inbox(session)

    rows = (await session.execute(select(InboxItem))).scalars().all()
    assert len(rows) == 1


async def test_a_non_video_and_a_tiny_file_are_ignored(session: AsyncSession) -> None:
    await ready(session)
    root = manual_assist.inbox_root()
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "partial.mp4").write_bytes(b"\x00" * 20)
    notes = await manual_assist.scan_inbox(session)
    assert notes == []
    assert (await session.execute(select(InboxItem))).scalars().all() == []


async def test_dismissing_leaves_the_file_and_stops_offering_it(
    session: AsyncSession,
) -> None:
    await ready(session)
    path = drop_video(manual_assist.inbox_root(), "loose.mp4")
    await manual_assist.scan_inbox(session)
    item = (await manual_assist.unmatched_items(session))[0]

    await manual_assist.dismiss_inbox_item(session, item.id)
    await manual_assist.scan_inbox(session)

    assert path.exists()
    assert await manual_assist.unmatched_items(session) == []
