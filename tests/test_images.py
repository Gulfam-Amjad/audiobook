"""Image library: scanning, dedupe, missing detection."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ImageStatus
from app.models import Image, ImageFolder
from app.services import images as image_service
from tests.conftest import write_image


async def _register(session: AsyncSession, path: Path, recursive: bool = True) -> ImageFolder:
    folder = ImageFolder(path=str(path), recursive=recursive)
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    return folder


async def test_scan_imports_supported_formats(session: AsyncSession, image_dir: Path) -> None:
    write_image(image_dir / "a.jpg")
    write_image(image_dir / "b.png")
    write_image(image_dir / "nested" / "c.jpg", colour="blue")
    (image_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    folder = await _register(session, image_dir)
    progress = await image_service.scan_folder(session, folder)

    assert progress.state == "done"
    assert progress.added == 3, "the .txt file must not be imported"
    rows = (await session.execute(select(Image))).scalars().all()
    assert {row.filename for row in rows} == {"a.jpg", "b.png", "c.jpg"}
    assert all(row.status == ImageStatus.NEW for row in rows)
    assert all(row.sha256 for row in rows)
    assert all(row.width == 640 and row.height == 480 for row in rows)


async def test_scan_is_idempotent(session: AsyncSession, image_dir: Path) -> None:
    write_image(image_dir / "a.jpg")
    write_image(image_dir / "b.jpg", colour="green")
    folder = await _register(session, image_dir)

    first = await image_service.scan_folder(session, folder)
    second = await image_service.scan_folder(session, folder)

    assert first.added == 2
    assert second.added == 0, "re-scanning must not duplicate rows"
    assert second.updated == 0, "unchanged files must not be re-hashed into updates"
    total = (await session.execute(select(Image))).scalars().all()
    assert len(total) == 2


async def test_non_recursive_scan_skips_subfolders(
    session: AsyncSession, image_dir: Path
) -> None:
    write_image(image_dir / "top.jpg")
    write_image(image_dir / "deep" / "inner.jpg")

    folder = await _register(session, image_dir, recursive=False)
    progress = await image_service.scan_folder(session, folder)

    assert progress.added == 1
    rows = (await session.execute(select(Image))).scalars().all()
    assert [row.filename for row in rows] == ["top.jpg"]


async def test_identical_content_is_flagged_as_duplicate(
    session: AsyncSession, image_dir: Path
) -> None:
    original = write_image(image_dir / "original.jpg")
    copy_path = image_dir / "copy-with-different-name.jpg"
    shutil.copyfile(original, copy_path)

    folder = await _register(session, image_dir)
    progress = await image_service.scan_folder(session, folder)

    assert progress.added == 2
    assert progress.duplicates == 1

    rows = (await session.execute(select(Image).order_by(Image.id))).scalars().all()
    hashes = {row.sha256 for row in rows}
    assert len(hashes) == 1, "identical files must hash identically"
    flagged = [row for row in rows if row.duplicate_of_id is not None]
    assert len(flagged) == 1
    assert flagged[0].duplicate_of_id == rows[0].id


async def test_duplicates_are_excluded_from_selection(
    session: AsyncSession, image_dir: Path
) -> None:
    original = write_image(image_dir / "original.jpg")
    shutil.copyfile(original, image_dir / "dupe.jpg")
    folder = await _register(session, image_dir)
    await image_service.scan_folder(session, folder)

    selected = await image_service.resolve_selection(
        session, select_all_matching=image_service.ImageQuery()
    )
    assert len(selected) == 1, "a bulk action must not queue the same picture twice"


async def test_missing_file_is_reported(session: AsyncSession, image_dir: Path) -> None:
    keep = write_image(image_dir / "keep.jpg")
    gone = write_image(image_dir / "gone.jpg", colour="yellow")
    folder = await _register(session, image_dir)
    await image_service.scan_folder(session, folder)

    gone.unlink()
    progress = await image_service.scan_folder(session, folder)

    assert progress.missing_marked == 1
    rows = {
        row.filename: row for row in (await session.execute(select(Image))).scalars().all()
    }
    assert rows["gone.jpg"].status == ImageStatus.MISSING
    assert rows["gone.jpg"].missing_since is not None
    assert rows["keep.jpg"].status == ImageStatus.NEW
    assert keep.exists(), "scanning must never delete the user's files"


async def test_restored_file_stops_being_missing(
    session: AsyncSession, image_dir: Path
) -> None:
    path = write_image(image_dir / "flaky.jpg")
    folder = await _register(session, image_dir)
    await image_service.scan_folder(session, folder)

    backup = path.read_bytes()
    path.unlink()
    await image_service.scan_folder(session, folder)
    path.write_bytes(backup)
    await image_service.scan_folder(session, folder)

    row = (await session.execute(select(Image))).scalar_one()
    assert row.status == ImageStatus.NEW
    assert row.missing_since is None


async def test_verify_present_detects_deletions(
    session: AsyncSession, image_dir: Path
) -> None:
    write_image(image_dir / "one.jpg")
    target = write_image(image_dir / "two.jpg", colour="blue")
    folder = await _register(session, image_dir)
    await image_service.scan_folder(session, folder)

    target.unlink()
    marked = await image_service.verify_present(session)

    assert marked == 1
    missing = (
        await session.execute(select(Image).where(Image.status == ImageStatus.MISSING))
    ).scalars().all()
    assert [row.filename for row in missing] == ["two.jpg"]


async def test_missing_images_are_excluded_from_selection(
    session: AsyncSession, image_dir: Path
) -> None:
    write_image(image_dir / "present.jpg")
    absent = write_image(image_dir / "absent.jpg", colour="blue")
    folder = await _register(session, image_dir)
    await image_service.scan_folder(session, folder)
    absent.unlink()
    await image_service.verify_present(session)

    selected = await image_service.resolve_selection(
        session, select_all_matching=image_service.ImageQuery()
    )
    assert [row.filename for row in selected] == ["present.jpg"]


async def test_thumbnails_are_written_and_shared_by_hash(
    session: AsyncSession, image_dir: Path
) -> None:
    original = write_image(image_dir / "a.jpg", size=(2000, 1500))
    shutil.copyfile(original, image_dir / "b.jpg")
    folder = await _register(session, image_dir)
    await image_service.scan_folder(session, folder)

    rows = (await session.execute(select(Image))).scalars().all()
    thumbs = {row.thumb_path for row in rows}
    assert len(thumbs) == 1, "identical content should reuse one thumbnail file"

    thumb = Path(rows[0].thumb_path)
    assert thumb.exists()
    assert thumb.stat().st_size < original.stat().st_size

    from PIL import Image as PILImage

    with PILImage.open(thumb) as img:
        assert max(img.size) <= 320, "thumbnails must be bounded in size"
    # The original must be untouched at full resolution.
    with PILImage.open(original) as img:
        assert img.size == (2000, 1500)


async def test_unreadable_file_is_counted_not_fatal(
    session: AsyncSession, image_dir: Path
) -> None:
    write_image(image_dir / "good.jpg")
    image_dir.joinpath("broken.jpg").write_bytes(b"this is not a jpeg")

    folder = await _register(session, image_dir)
    progress = await image_service.scan_folder(session, folder)

    assert progress.state == "done", "one bad file must not abort the scan"
    assert progress.unreadable == 1
    rows = {
        row.filename: row for row in (await session.execute(select(Image))).scalars().all()
    }
    assert rows["broken.jpg"].thumb_path is None
    assert rows["broken.jpg"].notes
    assert rows["good.jpg"].thumb_path is not None


async def test_scan_of_vanished_folder_fails_cleanly(
    session: AsyncSession, tmp_path: Path
) -> None:
    folder = await _register(session, tmp_path / "never-existed")
    progress = await image_service.scan_folder(session, folder)

    assert progress.state == "failed"
    assert "no longer exists" in progress.message


async def test_filters_and_paging(session: AsyncSession, image_dir: Path) -> None:
    for index in range(7):
        write_image(image_dir / f"img-{index}.jpg", colour=f"#{index}{index}00{index}{index}")
    folder = await _register(session, image_dir)
    await image_service.scan_folder(session, folder)

    first_page, total = await image_service.list_images(
        session, image_service.ImageQuery(page=1, page_size=5)
    )
    second_page, _ = await image_service.list_images(
        session, image_service.ImageQuery(page=2, page_size=5)
    )
    assert total == 7
    assert len(first_page) == 5
    assert len(second_page) == 2

    searched, search_total = await image_service.list_images(
        session, image_service.ImageQuery(search="img-3")
    )
    assert search_total == 1
    assert searched[0].filename == "img-3.jpg"
