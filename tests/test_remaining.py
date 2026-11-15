"""Capacity maths, library path guards, and the assembly command builder."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Output
from app.services import assembly, capacity, library
from app.services.assembly import AssemblyError
from app.services.library import LibraryError

pytestmark = pytest.mark.usefixtures("data_root")


def test_even_the_optimistic_day_misses_twenty_minutes() -> None:
    bands = {band.label: band.seconds for band in capacity.manual_bands()}
    assert bands["Conservative"] <= bands["Expected"] <= bands["Optimistic"]
    assert bands["Optimistic"] < 20 * 60
    assert bands["Conservative"] > 0


def test_the_expected_band_ignores_unverified_sites() -> None:
    from app.providers.manual_sites import SITES

    verified = [site for site in SITES if site.allowance_verified]
    expected = next(band for band in capacity.manual_bands() if band.label == "Expected")
    midpoint = (
        sum(site.seconds_per_day_low for site in verified)
        + sum(site.seconds_per_day_high for site in verified)
    ) / 2
    assert expected.seconds == pytest.approx(midpoint)


async def test_the_report_states_the_shortfall_and_keeps_assembly_separate(
    session: AsyncSession,
) -> None:
    report = await capacity.report(session)
    assert report["reachable"] is False
    assert report["shortfall_seconds"] > 0
    assert report["generated_seconds"] == 0
    assert report["assembled_output_seconds"] == 0
    assert report["alibaba_seconds_once"] == 200
    assert report["byteplus_tokens_once"] == 4_000_000


async def test_a_clip_outside_the_data_folders_cannot_be_served(
    session: AsyncSession, tmp_path: Path, data_root: Path
) -> None:
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"nope")
    with pytest.raises(LibraryError):
        library.assert_servable(outside, [data_root])


async def test_a_clip_inside_the_data_root_can_be_exported(
    session: AsyncSession, data_root: Path
) -> None:
    folder = data_root / "output"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "clip.mp4"
    path.write_bytes(b"video-bytes")
    output = Output(
        file_path=str(path), file_size=path.stat().st_size, duration_s=3.5, source="manual"
    )
    session.add(output)
    await session.flush()

    archive = await library.export_zip(session, [output.id], data_root / "tmp" / "out.zip")
    assert archive.exists()
    assert archive.stat().st_size > 0


def test_a_fade_shortens_the_result_by_the_overlaps(tmp_path: Path) -> None:
    graph, duration = assembly.fade_filter([5.0, 5.0, 5.0], 0.5)
    assert duration == pytest.approx(14.0)
    assert graph.count("xfade") == 2
    assert "[vout]" in graph


def test_a_fade_longer_than_a_clip_is_refused() -> None:
    with pytest.raises(AssemblyError):
        assembly.fade_filter([1.0, 5.0], 2.0)


def test_joining_without_a_fade_copies_the_stream(tmp_path: Path) -> None:
    listing = tmp_path / "concat.txt"
    command = assembly.join_args(
        "ffmpeg",
        [tmp_path / "a.mp4", tmp_path / "b.mp4"],
        tmp_path / "out.mp4",
        durations=[4.0, 4.0],
        fade_s=0,
        audio_path="",
        listing=listing,
    )
    assert command[0] == "ffmpeg"
    assert "-c" in command and "copy" in command
    assert isinstance(command, list)


def test_normalising_never_writes_the_source_path_as_the_output(tmp_path: Path) -> None:
    source = tmp_path / "original.mp4"
    dest = tmp_path / "work" / "001.mp4"
    command = assembly.normalize_args("ffmpeg", source, dest, width=1280, height=720, fps=24)
    assert command[-1] == str(dest)
    assert str(source) in command
    assert command[-1] != str(source)


async def test_an_assembly_of_one_clip_is_refused(session: AsyncSession, data_root: Path) -> None:
    path = data_root / "output" / "only.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    output = Output(file_path=str(path), file_size=1, duration_s=4, source="api")
    session.add(output)
    await session.flush()
    with pytest.raises(AssemblyError):
        await assembly.create_assembly(session, name="one", output_ids=[output.id])


async def test_an_unmeasured_clip_cannot_be_assembled(
    session: AsyncSession, data_root: Path
) -> None:
    folder = data_root / "output"
    folder.mkdir(parents=True, exist_ok=True)
    ids = []
    for name in ("a.mp4", "b.mp4"):
        path = folder / name
        path.write_bytes(b"x")
        output = Output(file_path=str(path), file_size=1, duration_s=None, source="api")
        session.add(output)
        await session.flush()
        ids.append(output.id)
    with pytest.raises(AssemblyError) as caught:
        await assembly.create_assembly(session, name="nope", output_ids=ids)
    assert "no measured duration" in str(caught.value)


def test_a_diagnostic_payload_redacts_a_bearer_token() -> None:
    from app.logging_setup import redact

    dumped = redact('{"note": "Authorization: Bearer sk-abcdef1234567890"}')
    assert "sk-abcdef1234567890" not in dumped
    assert "***" in dumped
