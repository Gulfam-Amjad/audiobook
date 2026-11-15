"""Upload assets: what gets prepared, and what deliberately does not happen.

Two things are being defended here.

The first is that nothing in this module publishes. There is no upload
path, the manifest records ``published: false``, and a render that failed
its checks is refused assets at all, so a broken file cannot be dressed up
for an upload.

The second is that the disclosure tells the truth about what a model made.
When no model animated anything, it has to say so, and it must not be
possible to read it as claiming otherwise. There is a test for the
no-motion wording, the partial-motion wording, and for the thumbnail being
a real frame of the real file rather than generated artwork.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import RenderStatus
from app.models import AudioSegment, Project, Render, Scene, VoiceProfile
from app.services import publishing
from app.services import render as render_service
from app.services.ffmpeg import FFmpegStatus
from tests.test_render import build_project, ffmpeg_present  # noqa: F401

pytestmark = pytest.mark.usefixtures("data_root")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def fake_thumbnail(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Let the thumbnail step "work" without needing FFmpeg installed."""
    calls: list[list[str]] = []

    async def extract(
        source: Path, destination: Path, *, ffmpeg_path: str, at_s: float
    ) -> Path | None:
        calls.append([str(source), str(destination), f"{at_s:.3f}"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\xff\xd8\xff" + b"\0" * 512)
        return destination

    monkeypatch.setattr(publishing, "extract_thumbnail", extract)
    return calls


def scene_rows(session: AsyncSession, keys: list[str]) -> list[Scene]:
    return [Scene(scene_key=key, purpose=f"Beat {key}") for key in keys]


async def completed_render(
    session: AsyncSession,
    tmp_path: Path,
    *,
    generated_motion_s: float = 0.0,
    duration_s: float = 8.0,
    with_subtitles: bool = True,
    **build: Any,
) -> Render:
    """A project with a render row that passed, and a file to match."""
    project_id = await build_project(session, tmp_path, **build)
    plan = await render_service.plan_render(session, project_id)

    output = tmp_path / "out" / "video.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"\0" * 4096)
    subtitle = output.parent / "subtitles.srt"
    if with_subtitles:
        subtitle.write_text("1\r\n00:00:00,300 --> 00:00:03,700\r\nLine.\r\n\r\n")

    record = Render(
        project_id=project_id,
        status=RenderStatus.COMPLETED,
        output_path=str(output),
        subtitle_path=str(subtitle) if with_subtitles else None,
        duration_s=duration_s or plan.planned_duration_s,
        generated_motion_s=generated_motion_s,
        width=1280,
        height=720,
        fps=24,
        validation={"failures": []},
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------


def test_a_short_title_is_left_alone() -> None:
    assert publishing.trim("A plain title", 100) == "A plain title"


def test_a_long_title_stops_at_a_whole_word() -> None:
    text = "An extremely long title that will certainly not fit inside the limit"
    cut = publishing.trim(text, 30)
    assert len(cut) <= 30
    assert text.startswith(cut)
    # No ellipsis: in a title that reads as a cliffhanger, not as a trim.
    assert not cut.endswith("...")


def test_trimming_tidies_whitespace() -> None:
    assert publishing.trim("  two   spaces\nhere ", 100) == "two spaces here"


def test_a_trim_does_not_end_on_punctuation() -> None:
    assert not publishing.trim("one, two, three, four", 12).endswith(",")


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------


def test_with_no_generated_motion_the_disclosure_says_so_plainly() -> None:
    text = publishing.disclosure(0.0, 60.0, "en_US-joe-medium")
    assert "No part of the picture was animated by an AI model" in text
    assert "camera move over still images" in text
    assert "editing rather than animation" in text


def test_the_disclosure_always_names_the_voice_as_synthetic() -> None:
    text = publishing.disclosure(12.0, 48.0, "en_US-joe-medium")
    assert "text-to-speech" in text
    assert "en_US-joe-medium" in text
    assert "no person speaks" in text


def test_partial_motion_is_disclosed_as_both_figures() -> None:
    text = publishing.disclosure(12.0, 48.0, "a voice")
    assert "12 seconds" in text
    assert "48 seconds" in text
    assert "animated by an AI video model" in text


def test_the_disclosure_never_claims_motion_that_did_not_happen() -> None:
    text = publishing.disclosure(0.0, 30.0, "a voice")
    assert "animated by an AI video model" not in text


def test_nothing_is_claimed_about_stills_that_were_never_declared() -> None:
    text = publishing.disclosure(0.0, 30.0, "a voice", stills_ai_generated=None)
    # The app cannot tell an AI still from a photograph, so it says neither.
    assert "still images were generated" not in text
    assert "not AI-generated" not in text


def test_declared_ai_stills_are_disclosed() -> None:
    text = publishing.disclosure(0.0, 30.0, "a voice", stills_ai_generated=True)
    assert "still images were generated by an AI image model" in text


def test_stills_declared_not_ai_are_disclosed_as_such() -> None:
    text = publishing.disclosure(0.0, 30.0, "a voice", stills_ai_generated=False)
    assert "not AI-generated" in text


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------


def test_keywords_come_from_the_projects_own_words() -> None:
    project = Project(
        name="P", niche="restaurant operations", audience="owners", story_style=""
    )
    scenes = scene_rows(None, ["s01", "s02"])
    scenes[0].narration = "Inventory moves with every sale. Inventory is the point."
    scenes[1].narration = "Inventory again, because inventory decides the margin."
    tags = publishing.keywords(project, "Inventory", scenes)
    assert "restaurant operations" in tags
    assert "inventory" in tags


def test_a_word_used_once_is_not_a_tag() -> None:
    project = Project(name="P", niche="", audience="", story_style="")
    scenes = scene_rows(None, ["s01"])
    scenes[0].narration = "A singular unrepeated peculiar word."
    assert "peculiar" not in publishing.keywords(project, "T", scenes)


def test_common_words_are_never_tags() -> None:
    project = Project(name="P", niche="", audience="", story_style="")
    scenes = scene_rows(None, ["s01", "s02"])
    scenes[0].narration = "This is what they said about that."
    scenes[1].narration = "This is what they said about that again."
    tags = publishing.keywords(project, "T", scenes)
    assert "this" not in tags
    assert "about" not in tags


def test_tags_stay_inside_the_platform_limits() -> None:
    project = Project(name="P", niche="x" * 40, audience="", story_style="")
    scenes = scene_rows(None, ["s01", "s02"])
    body = " ".join(f"token{i} token{i}" for i in range(60))
    scenes[0].narration = body
    scenes[1].narration = body
    tags = publishing.keywords(project, "T", scenes)
    assert len(tags) <= publishing.MAX_KEYWORDS
    assert all(len(tag) <= publishing.KEYWORD_LIMIT for tag in tags)
    assert sum(len(tag) + 1 for tag in tags) <= publishing.KEYWORDS_TOTAL_LIMIT
    # The over-long niche is dropped rather than cut into a nonsense tag.
    assert "x" * 40 not in tags


def test_filler_words_from_a_narration_are_not_tags() -> None:
    project = Project(name="P", niche="", audience="", story_style="")
    scenes = scene_rows(None, ["s01", "s02"])
    scenes[0].narration = "Still before the first landing, already down."
    scenes[1].narration = "Still before the first landing, already down again."
    tags = publishing.keywords(project, "T", scenes)
    assert "still" not in tags
    assert "before" not in tags
    assert "already" not in tags
    assert "landing" in tags


def test_the_thumbnail_is_taken_before_the_first_caption() -> None:
    assert publishing.thumbnail_at(256.0, 0.3) == pytest.approx(0.12)
    assert publishing.thumbnail_at(100.0, None) == pytest.approx(100.0 * publishing.THUMBNAIL_AT)


def test_a_music_licence_note_is_quoted_and_a_missing_one_is_only_the_filename(tmp_path: Path) -> None:
    track = tmp_path / "Aftermath.mp3"
    track.write_bytes(b"")
    note = tmp_path / "Aftermath-license.txt"
    note.write_text("Aftermath by Indieteur. License: CC0.\n", encoding="utf-8")
    assert publishing.music_credit(str(track)) == "Music: Aftermath by Indieteur. License: CC0."
    bare = tmp_path / "bed.mp3"
    bare.write_bytes(b"")
    assert publishing.music_credit(str(bare)) == "Music file: bed.mp3"
    assert publishing.music_credit(None) is None


def test_there_are_no_duplicate_tags() -> None:
    project = Project(name="P", niche="inventory", audience="inventory", story_style="")
    scenes = scene_rows(None, ["s01", "s02"])
    scenes[0].narration = "inventory inventory"
    scenes[1].narration = "inventory inventory"
    tags = publishing.keywords(project, "T", scenes)
    assert len(tags) == len(set(tags))


# ---------------------------------------------------------------------------
# Chapters
# ---------------------------------------------------------------------------


def test_shot_starts_accumulate_the_measured_clip_lengths() -> None:
    starts = publishing.shot_starts_from(
        [
            {"scene_key": "s01", "clip_duration_s": 4.0},
            {"scene_key": "s02", "clip_duration_s": 6.5},
            {"scene_key": "s03", "clip_duration_s": 2.0},
        ]
    )
    assert starts == {"s01": 0.0, "s02": 4.0, "s03": 10.5}


def test_an_unmeasured_clip_stops_the_starts_there() -> None:
    starts = publishing.shot_starts_from(
        [
            {"scene_key": "s01", "clip_duration_s": 4.0},
            {"scene_key": "s02", "clip_duration_s": None},
            {"scene_key": "s03", "clip_duration_s": 2.0},
        ]
    )
    # Once a length is unknown, no later start is knowable.
    assert starts == {"s01": 0.0}


def test_chapters_need_three_marks() -> None:
    scenes = scene_rows(None, ["s01", "s02"])
    starts = {"s01": 0.0, "s02": 30.0}
    assert publishing.chapters_from(scenes, starts) == []


def test_chapters_must_begin_at_zero() -> None:
    scenes = scene_rows(None, ["s01", "s02", "s03"])
    starts = {"s01": 12.0, "s02": 30.0, "s03": 50.0}
    assert publishing.chapters_from(scenes, starts) == []


def test_chapters_too_close_together_are_merged_away() -> None:
    scenes = scene_rows(None, ["s01", "s02", "s03", "s04"])
    starts = {"s01": 0.0, "s02": 2.0, "s03": 20.0, "s04": 40.0}
    marks = publishing.chapters_from(scenes, starts)
    assert [start for start, _ in marks] == [0.0, 20.0, 40.0]


def test_a_usable_chapter_list_is_labelled_from_the_shots() -> None:
    scenes = scene_rows(None, ["s01", "s02", "s03"])
    starts = {"s01": 0.0, "s02": 30.0, "s03": 60.0}
    marks = publishing.chapters_from(scenes, starts)
    assert [label for _, label in marks] == ["Beat s01", "Beat s02", "Beat s03"]


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------


def test_the_description_ends_with_the_disclosure() -> None:
    body = publishing.description(
        title="T",
        logline="A logline.",
        synopsis="A synopsis.",
        chapters=[(0.0, "Start"), (65.0, "Middle")],
        disclosure_text="DISCLOSURE HERE",
    )
    assert body.rstrip().endswith("DISCLOSURE HERE")
    assert "0:00 Start" in body
    assert "1:05 Middle" in body


def test_a_synopsis_identical_to_the_logline_is_not_repeated() -> None:
    body = publishing.description(
        title="T", logline="Same.", synopsis="Same.", chapters=[], disclosure_text="D"
    )
    assert body.count("Same.") == 1


def test_an_over_long_description_keeps_the_disclosure_whole() -> None:
    disclosure_text = "D" * 300
    body = publishing.description(
        title="T",
        logline="L" * 6000,
        synopsis="",
        chapters=[],
        disclosure_text=disclosure_text,
    )
    assert len(body) <= publishing.DESCRIPTION_LIMIT
    assert body.endswith(disclosure_text)


# ---------------------------------------------------------------------------
# The thumbnail command
# ---------------------------------------------------------------------------


def test_the_thumbnail_is_one_frame_of_the_finished_file() -> None:
    command = publishing.thumbnail_command(
        "ffmpeg", Path("in.mp4"), Path("out.jpg"), 10.5
    )
    assert command[command.index("-i") - 1] == "10.500"
    assert command[command.index("-i") + 1] == "in.mp4"
    assert "-frames:v" in command
    assert command[command.index("-frames:v") + 1] == "1"
    assert command[-1] == "out.jpg"


def test_a_negative_seek_is_clamped_to_the_start() -> None:
    command = publishing.thumbnail_command(
        "ffmpeg", Path("in.mp4"), Path("out.jpg"), -3.0
    )
    assert command[command.index("-ss") + 1] == "0.000"


# ---------------------------------------------------------------------------
# Preparing, end to end
# ---------------------------------------------------------------------------


async def test_assets_are_prepared_beside_the_video(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path)
    assets = await publishing.prepare(session, record.id)

    manifest = Path(assets.manifest_path)
    assert manifest.exists()
    assert manifest.parent == Path(record.output_path).parent
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["title"]
    assert payload["disclosure"]
    assert payload["video_path"] == record.output_path


async def test_nothing_is_recorded_as_published(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path)
    assets = await publishing.prepare(session, record.id)
    payload = json.loads(Path(assets.manifest_path).read_text(encoding="utf-8"))
    assert payload["published"] is False


async def test_the_prepared_disclosure_matches_what_the_render_measured(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path, generated_motion_s=0.0)
    assets = await publishing.prepare(session, record.id)
    assert "No part of the picture was animated" in assets.disclosure
    assert assets.generated_motion_s == 0.0


async def test_the_voice_is_named_from_the_projects_own_profile(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    voice = VoiceProfile(
        name="en_US-joe-medium",
        engine="piper",
        model_path=str(tmp_path / "joe.onnx"),
    )
    session.add(voice)
    await session.flush()
    record = await completed_render(session, tmp_path, narrator_voice_id=voice.id)
    assets = await publishing.prepare(session, record.id)
    assert "en_US-joe-medium" in assets.disclosure


async def test_the_voice_comes_from_the_recorded_segments_not_the_setting(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    """The setting can change after a render; the file cannot."""
    recorded = VoiceProfile(name="the-voice-that-recorded", engine="piper")
    later = VoiceProfile(name="a-voice-chosen-afterwards", engine="piper")
    session.add_all([recorded, later])
    await session.flush()

    record = await completed_render(session, tmp_path, narrator_voice_id=later.id)
    segments = (
        await session.execute(
            select(AudioSegment).where(AudioSegment.project_id == record.project_id)
        )
    ).scalars().all()
    for segment in segments:
        segment.voice_profile_id = recorded.id
    await session.commit()

    assets = await publishing.prepare(session, record.id)
    assert "the-voice-that-recorded" in assets.disclosure
    assert "a-voice-chosen-afterwards" not in assets.disclosure


async def test_an_undeclared_still_source_is_reported_as_a_note(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path)
    assets = await publishing.prepare(session, record.id)
    assert "still images came from" in " ".join(assets.notes)
    assert "still images were generated" not in assets.disclosure


async def test_a_declared_still_source_reaches_the_disclosure(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path, stills_ai_generated=True)
    assets = await publishing.prepare(session, record.id)
    assert "generated by an AI image model" in assets.disclosure
    assert not any("still images came from" in note for note in assets.notes)


async def test_the_thumbnail_is_taken_from_inside_the_video(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path, duration_s=100.0)
    assets = await publishing.prepare(session, record.id)

    source, destination, at_s = fake_thumbnail[0]
    assert source == record.output_path
    assert Path(destination).exists()
    # The fixture's first caption starts at 0.3s, so the frame is taken in
    # the lead-in, before the burned subtitle is on screen.
    assert float(at_s) == pytest.approx(0.12)
    await session.refresh(record)
    assert record.thumbnail_path == assets.thumbnail_path


async def test_without_ffmpeg_the_missing_thumbnail_is_reported(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_ffmpeg(
        ffmpeg_override: str = "", ffprobe_override: str = ""
    ) -> FFmpegStatus:
        return FFmpegStatus(message="FFmpeg is not installed.")

    monkeypatch.setattr("app.services.ffmpeg.detect", no_ffmpeg)
    record = await completed_render(session, tmp_path)
    assets = await publishing.prepare(session, record.id)
    assert assets.thumbnail_path == ""
    assert any("No thumbnail" in note for note in assets.notes)


async def test_a_failed_render_gets_no_assets(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path)
    record.status = RenderStatus.FAILED
    await session.commit()

    with pytest.raises(publishing.PublishError, match="did not pass its checks"):
        await publishing.prepare(session, record.id)


async def test_a_missing_file_gets_no_assets(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path)
    Path(record.output_path).unlink()

    with pytest.raises(publishing.PublishError, match="no longer on disk"):
        await publishing.prepare(session, record.id)


async def test_an_unknown_render_is_refused(session: AsyncSession) -> None:
    with pytest.raises(publishing.PublishError, match="no render with that id"):
        await publishing.prepare(session, 999)


async def test_chapters_are_dropped_when_the_shots_no_longer_match_the_file(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(
        session,
        tmp_path,
        duration_s=900.0,
        lines=["One line here.", "Two lines here.", "Three lines here."],
        clip_durations=[30.0, 30.0, 30.0],
        segment_durations=[4.0, 4.0, 4.0],
    )
    assets = await publishing.prepare(session, record.id)
    assert assets.chapters == []
    assert any("shots have changed" in note for note in assets.notes)


async def test_preparing_twice_overwrites_rather_than_duplicating(
    session: AsyncSession, tmp_path: Path, fake_thumbnail: list[list[str]]
) -> None:
    record = await completed_render(session, tmp_path)
    first = await publishing.prepare(session, record.id)
    second = await publishing.prepare(session, record.id)
    assert first.manifest_path == second.manifest_path
    folder = Path(record.output_path).parent
    assert len(list(folder.glob("publish*.json"))) == 1


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


async def test_preparing_assets_over_http_says_nothing_was_uploaded(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
    fake_thumbnail: list[list[str]],
) -> None:
    record = await completed_render(session, tmp_path)
    response = await client.post(f"/renders/{record.id}/publish-assets")
    assert response.status_code == 200
    assert "Nothing has been uploaded" in response.headers["X-AVM-Flash"]


async def test_the_render_screen_offers_no_way_to_publish(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
    fake_thumbnail: list[list[str]],
) -> None:
    record = await completed_render(session, tmp_path)
    await publishing.prepare(session, record.id)
    body = (await client.get(f"/projects/{record.project_id}/render")).text
    assert "Nothing is uploaded" in body
    assert "Recorded as not published" in body
    # There is no upload endpoint to call, and no button pretending there is.
    assert "/publish\"" not in body
    assert "upload-to-youtube" not in body


async def test_the_manifest_can_be_downloaded(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
    fake_thumbnail: list[list[str]],
) -> None:
    record = await completed_render(session, tmp_path)
    await publishing.prepare(session, record.id)
    response = await client.get(f"/renders/{record.id}/publish-assets")
    assert response.status_code == 200
    assert json.loads(response.text)["published"] is False


async def test_downloading_assets_that_were_never_prepared_is_a_404(
    client: AsyncClient, session: AsyncSession, tmp_path: Path
) -> None:
    record = await completed_render(session, tmp_path)
    response = await client.get(f"/renders/{record.id}/publish-assets")
    assert response.status_code == 404


async def test_preparing_assets_for_an_unknown_render_is_a_404(
    client: AsyncClient,
) -> None:
    assert (await client.post("/renders/4242/publish-assets")).status_code == 404
