"""Subtitles, mixing, assembly and the checks on the finished file.

Two things are being defended here.

The first is that subtitle timing is *measured*, never estimated. A line
whose audio was never measured gets no cue at all, because the only way
to give it one would be to invent a duration.

The second is that a render is not a success merely because FFmpeg
returned zero. The checks run on the delivered file, and a file that is
the wrong length, the wrong pixel format, silent when it should not be,
or full of black frames is recorded as a failure. There is a test for
each of those, plus one marked ``real_ffprobe`` that assembles a genuine
video with genuine FFmpeg and measures the result.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    AnimationMethod,
    AudioStatus,
    ClipStatus,
    PipelineStage,
    RenderStatus,
    StageStatus,
)
from app.models import (
    AudioSegment,
    Image,
    ImageFolder,
    Render,
    Scene,
    SceneClip,
    StageCheckpoint,
)
from app.services import mixing, subtitles
from app.services import projects as project_service
from app.services import render as render_service
from app.services import scripts as script_service
from app.services.ffmpeg import FFmpegStatus, MediaInfo
from tests.conftest import write_image

pytestmark = pytest.mark.usefixtures("data_root")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def ffmpeg_present(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if "real_ffprobe" in request.keywords:
        return

    async def fake_detect(
        ffmpeg_override: str = "", ffprobe_override: str = ""
    ) -> FFmpegStatus:
        return FFmpegStatus(
            ffmpeg_path="C:/ffmpeg/ffmpeg.exe",
            ffprobe_path="C:/ffmpeg/ffprobe.exe",
            source="PATH",
            message="FFmpeg and FFprobe are available.",
        )

    monkeypatch.setattr("app.services.ffmpeg.detect", fake_detect)


@pytest.fixture
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every FFmpeg invocation and write a plausible output file."""
    calls: list[list[str]] = []

    async def run(command: list[str], *, timeout: int = 0) -> tuple[int, str]:
        calls.append(command)
        if "-f" in command and "null" in command:
            # A measuring pass: answer with a summary rather than a file.
            if "ebur128" in " ".join(command):
                return 0, "  Integrated loudness:\n    I:         -16.2 LUFS\n"
            return 0, ""
        destination = Path(command[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\0" * 8192)
        return 0, ""

    monkeypatch.setattr(render_service, "_run", run)
    return calls


def scene_dict(key: str, narration: str, **extra: Any) -> dict[str, Any]:
    base = {
        "scene_key": key,
        "purpose": f"Beat {key}",
        "target_duration_s": 8.0,
        "narration": narration,
        "dialogue": [],
        "characters": [],
        "preferred_method": "camera_move",
    }
    base.update(extra)
    return base


async def build_project(
    session: AsyncSession,
    tmp_path: Path,
    *,
    lines: list[str] | None = None,
    clip_durations: list[float] | None = None,
    segment_durations: list[float] | None = None,
    selected: bool = True,
    measured_clips: bool = True,
    generated: bool = False,
    **project_values: Any,
) -> int:
    """A project with clips in the edit and measured narration behind them."""
    lines = lines or [
        "The first thing happens here and it takes a moment.",
        "Then the second thing happens, rather later on.",
    ]
    clip_durations = clip_durations or [4.0, 4.0]
    segment_durations = segment_durations or [3.4, 3.4]

    scenes = [scene_dict(f"s{i:02d}", line) for i, line in enumerate(lines)]
    planned = sum(float(s["target_duration_s"]) for s in scenes)
    values = {"name": "Render test", "target_duration_s": max(15, round(planned))}
    values.update(project_values)
    project = await project_service.create_project(session, **values)
    script = await script_service.accept_paste(
        session,
        project.id,
        json.dumps({"title": "T", "logline": "L", "synopsis": "S", "scenes": scenes}),
    )
    await script_service.approve(session, script.id)

    folder = ImageFolder(path=str(tmp_path))
    session.add(folder)
    await session.flush()

    rows = (
        await session.execute(
            select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index)
        )
    ).scalars().all()

    for index, scene in enumerate(rows):
        still = write_image(tmp_path / f"still-{index}.jpg")
        image = Image(
            folder_id=folder.id,
            path=str(still),
            filename=still.name,
            extension=".jpg",
            sha256=f"hash-{index}",
            width=1600,
            height=1200,
        )
        session.add(image)
        await session.flush()
        scene.image_id = image.id
        scene.assigned_method = (
            AnimationMethod.REMOTE_I2V if generated else AnimationMethod.CAMERA_MOVE
        )

        # A test that made real media with FFmpeg already put it here; only
        # stand in for it when there is nothing to use.
        clip_path = tmp_path / f"clip-{index}.mp4"
        if not clip_path.exists():
            clip_path.write_bytes(b"\0" * 4096)
        session.add(
            SceneClip(
                scene_id=scene.id,
                project_id=project.id,
                method=scene.assigned_method,
                provider_key="ffmpeg",
                file_path=str(clip_path),
                duration_s=clip_durations[index] if measured_clips else None,
                is_generated_motion=generated,
                selected=selected,
                status=ClipStatus.READY,
                validation={"requested_duration_s": clip_durations[index]},
            )
        )

        audio_path = tmp_path / f"line-{index}.wav"
        if not audio_path.exists():
            audio_path.write_bytes(b"\0" * 2048)
        session.add(
            AudioSegment(
                scene_id=scene.id,
                project_id=project.id,
                order_index=0,
                text=lines[index],
                file_path=str(audio_path),
                duration_s=segment_durations[index],
                status=AudioStatus.READY,
            )
        )

    await session.commit()
    return project.id


# ---------------------------------------------------------------------------
# Timestamps and wrapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00:00,000"),
        (1.5, "00:00:01,500"),
        (61.25, "00:01:01,250"),
        (3661.125, "01:01:01,125"),
        (-4.0, "00:00:00,000"),
    ],
)
def test_a_timestamp_is_formatted_the_way_srt_wants(
    seconds: float, expected: str
) -> None:
    assert subtitles.timestamp(seconds) == expected


def test_webvtt_uses_a_full_stop_not_a_comma() -> None:
    assert subtitles.timestamp(1.5, separator=".") == "00:00:01.500"


def test_a_short_line_is_left_on_one_line() -> None:
    assert subtitles.wrap("Short enough.") == "Short enough."


def test_a_long_line_is_broken_on_a_word_boundary() -> None:
    wrapped = subtitles.wrap(
        "This sentence is definitely longer than a single subtitle line should be."
    )
    assert "\n" in wrapped
    assert all(len(line) <= 42 for line in wrapped.split("\n"))
    # Nothing is lost in the wrapping.
    assert wrapped.replace("\n", " ").split() == (
        "This sentence is definitely longer than a single subtitle line should be."
    ).split()


def test_a_very_long_line_keeps_every_word_rather_than_truncating() -> None:
    text = " ".join(["word"] * 60)
    wrapped = subtitles.wrap(text)
    assert len(wrapped.split("\n")) == 2
    assert wrapped.replace("\n", " ").split() == text.split()


def test_an_empty_line_wraps_to_nothing() -> None:
    assert subtitles.wrap("   ") == ""


# ---------------------------------------------------------------------------
# Cue building
# ---------------------------------------------------------------------------


def test_cues_start_and_end_where_their_audio_does() -> None:
    sheet = subtitles.build(
        [
            {"text": "First line.", "start_s": 0.3, "duration_s": 3.4},
            {"text": "Second line.", "start_s": 4.3, "duration_s": 2.1},
        ]
    )
    assert sheet.count == 2
    assert sheet.cues[0].start_s == pytest.approx(0.3)
    assert sheet.cues[0].end_s == pytest.approx(3.7)
    assert sheet.cues[1].end_s == pytest.approx(6.4)


def test_a_line_with_no_measured_audio_gets_no_cue() -> None:
    """Inventing a duration is the only other option, so it is not taken."""
    sheet = subtitles.build(
        [
            {"text": "Measured.", "start_s": 0.0, "duration_s": 2.0},
            {"text": "Never recorded.", "start_s": 2.0, "duration_s": None},
        ]
    )
    assert sheet.count == 1
    assert len(sheet.gaps) == 1
    assert "no measured audio" in sheet.gaps[0]


def test_a_line_with_no_start_time_gets_no_cue() -> None:
    sheet = subtitles.build([{"text": "Floating.", "start_s": None, "duration_s": 2.0}])
    assert sheet.count == 0
    assert sheet.gaps


def test_an_empty_line_is_skipped_without_complaint() -> None:
    sheet = subtitles.build([{"text": "   ", "start_s": 0.0, "duration_s": 2.0}])
    assert sheet.count == 0
    assert sheet.gaps == []


def test_a_cue_too_quick_to_read_is_flagged_not_stretched() -> None:
    sheet = subtitles.build([{"text": "Hi.", "start_s": 0.0, "duration_s": 0.3}])
    assert sheet.cues[0].duration_s == pytest.approx(0.3)
    assert any("too quick to read" in w for w in sheet.warnings)


def test_a_cue_that_reads_too_fast_is_flagged() -> None:
    sheet = subtitles.build(
        [{"text": "A" * 120, "start_s": 0.0, "duration_s": 2.0}]
    )
    assert any("characters a second" in w for w in sheet.warnings)


def test_overlapping_cues_are_flagged() -> None:
    sheet = subtitles.build(
        [
            {"text": "First line here.", "start_s": 0.0, "duration_s": 4.0},
            {"text": "Second line here.", "start_s": 2.0, "duration_s": 3.0},
        ]
    )
    assert any("on screen at once" in w for w in sheet.warnings)


def test_cues_are_numbered_from_one_in_order() -> None:
    sheet = subtitles.build(
        [{"text": f"Line {i}.", "start_s": i * 2.0, "duration_s": 1.5} for i in range(4)]
    )
    assert [cue.index for cue in sheet.cues] == [1, 2, 3, 4]


def test_the_sheet_knows_where_the_last_cue_ends() -> None:
    sheet = subtitles.build(
        [{"text": "Only line.", "start_s": 1.0, "duration_s": 2.5}]
    )
    assert sheet.last_end_s == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# Subtitle files
# ---------------------------------------------------------------------------


def test_srt_output_has_the_shape_players_expect() -> None:
    sheet = subtitles.build([{"text": "Hello there.", "start_s": 0.5, "duration_s": 2.0}])
    text = subtitles.to_srt(sheet)
    assert text.startswith("1\r\n")
    assert "00:00:00,500 --> 00:00:02,500" in text
    assert "Hello there." in text


def test_vtt_output_starts_with_its_header() -> None:
    sheet = subtitles.build([{"text": "Hello there.", "start_s": 0.0, "duration_s": 2.0}])
    assert subtitles.to_vtt(sheet).startswith("WEBVTT")


def test_writing_subtitles_with_no_cues_is_refused(tmp_path: Path) -> None:
    with pytest.raises(subtitles.SubtitleError):
        subtitles.write(subtitles.CueSheet(), tmp_path / "subs.srt")


def test_an_srt_file_is_written_where_asked(tmp_path: Path) -> None:
    sheet = subtitles.build([{"text": "A line.", "start_s": 0.0, "duration_s": 1.5}])
    path = subtitles.write(sheet, tmp_path / "out" / "subs.srt")
    assert path.exists()
    assert "00:00:00,000" in path.read_text(encoding="utf-8")


def test_a_vtt_file_is_written_when_asked_for(tmp_path: Path) -> None:
    sheet = subtitles.build([{"text": "A line.", "start_s": 0.0, "duration_s": 1.5}])
    path = subtitles.write(sheet, tmp_path / "subs.vtt")
    assert path.read_text(encoding="utf-8").startswith("WEBVTT")


def test_a_windows_path_is_escaped_for_the_subtitles_filter() -> None:
    """libass reads the filename as a filter argument, so a colon matters."""
    escaped = subtitles.filter_path(Path(r"D:\AIVideoManager\subs.srt"))
    assert escaped == "D\\:/AIVideoManager/subs.srt"


def test_the_burn_in_filter_names_the_file_and_a_style() -> None:
    graph = subtitles.burn_in_filter(Path(r"D:\subs.srt"), {"FontSize": 30})
    assert graph.startswith("subtitles='D\\:/subs.srt'")
    assert "FontSize=30" in graph


# ---------------------------------------------------------------------------
# Placing audio on the timeline
# ---------------------------------------------------------------------------


def test_each_scene_s_lines_sit_inside_its_own_shot() -> None:
    timeline = mixing.plan_timeline(
        [
            {
                "scene_key": "s00",
                "clip_duration_s": 4.0,
                "segments": [
                    {"text": "One.", "duration_s": 3.4, "file_path": __file__},
                ],
            },
            {
                "scene_key": "s01",
                "clip_duration_s": 5.0,
                "segments": [
                    {"text": "Two.", "duration_s": 4.1, "file_path": __file__},
                ],
            },
        ]
    )
    assert timeline.total_s == pytest.approx(9.0)
    assert timeline.placements[0].start_s == pytest.approx(0.3)
    assert timeline.placements[1].start_s == pytest.approx(4.3)


def test_several_lines_in_one_scene_follow_each_other() -> None:
    timeline = mixing.plan_timeline(
        [
            {
                "scene_key": "s00",
                "clip_duration_s": 6.0,
                "segments": [
                    {"text": "One.", "duration_s": 2.0, "file_path": __file__},
                    {"text": "Two.", "duration_s": 2.0, "file_path": __file__},
                ],
            }
        ]
    )
    assert [p.start_s for p in timeline.placements] == [
        pytest.approx(0.3),
        pytest.approx(2.3),
    ]


def test_a_scene_with_no_clip_cannot_hold_its_narration() -> None:
    timeline = mixing.plan_timeline(
        [{"scene_key": "s00", "clip_duration_s": None, "segments": []}]
    )
    assert timeline.placements == []
    assert any("nowhere to go" in item for item in timeline.unplaced)


def test_a_line_with_no_audio_file_is_reported() -> None:
    timeline = mixing.plan_timeline(
        [
            {
                "scene_key": "s00",
                "clip_duration_s": 4.0,
                "segments": [{"text": "Missing.", "duration_s": 2.0, "file_path": None}],
            }
        ]
    )
    assert timeline.placements == []
    assert any("no measured audio" in item for item in timeline.unplaced)


def test_a_line_whose_file_vanished_is_reported(tmp_path: Path) -> None:
    timeline = mixing.plan_timeline(
        [
            {
                "scene_key": "s00",
                "clip_duration_s": 4.0,
                "segments": [
                    {
                        "text": "Gone.",
                        "duration_s": 2.0,
                        "file_path": str(tmp_path / "never-existed.wav"),
                    }
                ],
            }
        ]
    )
    assert any("missing its audio file" in item for item in timeline.unplaced)


def test_narration_longer_than_its_shot_is_a_warning_not_a_stretch() -> None:
    timeline = mixing.plan_timeline(
        [
            {
                "scene_key": "s00",
                "clip_duration_s": 2.0,
                "segments": [{"text": "Long.", "duration_s": 5.0, "file_path": __file__}],
            }
        ]
    )
    assert any("does not fit" in w for w in timeline.warnings)
    # The audio keeps its measured length regardless.
    assert timeline.placements[0].duration_s == pytest.approx(5.0)


def test_the_timeline_separates_speech_from_silence() -> None:
    timeline = mixing.plan_timeline(
        [
            {
                "scene_key": "s00",
                "clip_duration_s": 10.0,
                "segments": [{"text": "A.", "duration_s": 4.0, "file_path": __file__}],
            }
        ]
    )
    assert timeline.speech_s == pytest.approx(4.0)
    assert timeline.silence_s == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# The mix filter graphs
# ---------------------------------------------------------------------------


def test_the_speech_graph_delays_each_line_to_its_own_offset() -> None:
    placements = [
        mixing.Placement(path=Path("a.wav"), start_s=0.3, duration_s=2.0),
        mixing.Placement(path=Path("b.wav"), start_s=4.342, duration_s=2.0),
    ]
    graph = mixing.speech_graph(placements, 10.0)
    assert "adelay=300:all=1" in graph
    assert "adelay=4342:all=1" in graph
    # normalize=0 stops amix quietening the track as lines are added.
    assert "amix=inputs=2:normalize=0:dropout_transition=0" in graph


def test_the_speech_graph_needs_something_to_place() -> None:
    with pytest.raises(mixing.MixError):
        mixing.speech_graph([], 10.0)


def test_the_speech_command_pins_the_track_to_the_planned_length() -> None:
    command = mixing.speech_command(
        "ffmpeg",
        [mixing.Placement(path=Path("a.wav"), start_s=0.0, duration_s=2.0)],
        Path("out.wav"),
        total_s=21.25,
    )
    assert "-t" in command
    assert command[command.index("-t") + 1] == "21.250"


def test_the_ducking_graph_drives_the_compressor_from_the_voice() -> None:
    """Feeding it the mix would duck the music against itself."""
    graph = mixing.duck_graph(music_gain_db=-18.0)
    assert "asplit=2[sc][voice]" in graph
    assert "[bed][sc]sidechaincompress=" in graph
    assert "volume=-18dB[bed]" in graph


def test_the_ducking_graph_normalises_loudness_last() -> None:
    graph = mixing.duck_graph(music_gain_db=-18.0)
    assert graph.index("sidechaincompress") < graph.index("loudnorm")
    assert f"I={mixing.TARGET_LUFS}" in graph


def test_normalisation_can_be_switched_off() -> None:
    assert "loudnorm" not in mixing.duck_graph(music_gain_db=-18.0, loudnorm=False)
    assert "loudnorm" not in mixing.speech_only_graph(loudnorm=False)


def test_a_mix_without_music_still_gets_normalised() -> None:
    command = mixing.mix_command("ffmpeg", Path("s.wav"), Path("m.wav"))
    graph = command[command.index("-filter_complex") + 1]
    assert "sidechaincompress" not in graph
    assert "loudnorm" in graph


def test_a_music_bed_is_looped_so_a_short_track_does_not_run_out() -> None:
    command = mixing.mix_command(
        "ffmpeg", Path("s.wav"), Path("out.wav"), music=Path("bed.mp3")
    )
    assert "-stream_loop" in command
    assert command[command.index("-stream_loop") + 1] == "-1"


# ---------------------------------------------------------------------------
# Loudness measurement
# ---------------------------------------------------------------------------


def test_the_integrated_loudness_is_read_from_the_summary() -> None:
    stderr = (
        "[Parsed_ebur128_0 @ 0] Summary:\n"
        "  Integrated loudness:\n"
        "    I:         -16.6 LUFS\n"
        "    Threshold: -27.4 LUFS\n"
        "  Loudness range:\n"
        "    LRA:         3.2 LU\n"
    )
    parsed = mixing.parse_loudness(stderr)
    assert parsed["integrated_lufs"] == pytest.approx(-16.6)
    assert parsed["lra_lu"] == pytest.approx(3.2)


def test_an_absent_loudness_summary_reads_as_unknown_not_zero() -> None:
    parsed = mixing.parse_loudness("nothing useful here")
    assert parsed["integrated_lufs"] is None
    assert parsed["lra_lu"] is None


def test_loudness_close_to_target_passes() -> None:
    ok, message = mixing.loudness_verdict(-16.6)
    assert ok is True
    assert "-16.6 LUFS" in message


def test_loudness_far_from_target_fails() -> None:
    ok, message = mixing.loudness_verdict(-28.0)
    assert ok is False
    assert "from the" in message


def test_unmeasured_loudness_is_not_treated_as_on_target() -> None:
    ok, message = mixing.loudness_verdict(None)
    assert ok is False
    assert "unknown" in message


# ---------------------------------------------------------------------------
# Planning a render
# ---------------------------------------------------------------------------


async def test_a_ready_project_plans_a_complete_render(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path)
    plan = await render_service.plan_render(session, project_id)
    assert plan.ready
    assert len(plan.clips) == 2
    assert plan.planned_duration_s == pytest.approx(8.0)
    assert plan.sheet is not None
    assert plan.sheet.count == 2


async def test_a_shot_with_nothing_in_the_edit_blocks_the_render(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path, selected=False)
    plan = await render_service.plan_render(session, project_id)
    assert not plan.ready
    assert any("no clip is in the edit" in p for p in plan.problems)


async def test_an_unmeasured_clip_blocks_the_render(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Everything after it would be timed against a length nobody knows."""
    project_id = await build_project(session, tmp_path, measured_clips=False)
    plan = await render_service.plan_render(session, project_id)
    assert not plan.ready
    assert any("never measured" in p for p in plan.problems)


async def test_a_missing_clip_file_blocks_the_render(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path)
    clip = (await session.execute(select(SceneClip))).scalars().first()
    Path(clip.file_path).unlink()
    plan = await render_service.plan_render(session, project_id)
    assert any("missing from disk" in p for p in plan.problems)


async def test_a_project_without_an_approved_script_cannot_be_rendered(
    session: AsyncSession, tmp_path: Path
) -> None:
    project = await project_service.create_project(session, name="Bare")
    await session.commit()
    plan = await render_service.plan_render(session, project.id)
    assert any("No approved script" in p for p in plan.problems)


async def test_generated_and_assembled_length_are_counted_apart(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path, generated=True)
    plan = await render_service.plan_render(session, project_id)
    assert plan.generated_motion_s == pytest.approx(8.0)
    assert plan.camera_move_s == pytest.approx(0.0)


async def test_camera_moves_are_never_counted_as_generated_motion(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path, generated=False)
    plan = await render_service.plan_render(session, project_id)
    assert plan.generated_motion_s == pytest.approx(0.0)
    assert plan.camera_move_s == pytest.approx(8.0)


async def test_an_unfinished_line_is_warned_about_not_silently_dropped(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path)
    segment = (await session.execute(select(AudioSegment))).scalars().first()
    segment.duration_s = None
    segment.status = AudioStatus.PENDING
    await session.commit()

    plan = await render_service.plan_render(session, project_id)
    assert plan.sheet.count == 1
    assert any("will be silent" in w for w in plan.warnings)


async def test_the_summary_always_names_both_kinds_of_length(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path, generated=False)
    plan = await render_service.plan_render(session, project_id)
    summary = render_service.summarise(plan, 8.0)
    assert "none of it was animated by a model" in summary
    assert "editing rather than animation" in summary


async def test_the_summary_reports_a_genuine_mix_of_both(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path, generated=True)
    plan = await render_service.plan_render(session, project_id)
    plan.camera_move_s = 4.0
    summary = render_service.summarise(plan, 12.0)
    assert "animated by a model" in summary
    assert "camera moving over still pictures" in summary


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_normalising_a_clip_letterboxes_rather_than_crops() -> None:
    command = render_service.normalise_command(
        "ffmpeg", Path("a.mp4"), Path("b.mp4"), width=1280, height=720, fps=24
    )
    graph = command[command.index("-vf") + 1]
    assert "force_original_aspect_ratio=decrease" in graph
    assert "pad=1280:720" in graph
    # Full-range input would otherwise come out tagged yuvj420p.
    assert "out_range=tv" in graph
    assert "format=yuv420p" in graph


def test_the_mux_burns_subtitles_when_given_them() -> None:
    command = render_service.mux_command(
        "ffmpeg", Path("v.mp4"), Path("a.wav"), Path("o.mp4"),
        subtitle_path=Path("s.srt"),
    )
    graph = command[command.index("-filter_complex") + 1]
    assert "subtitles=" in graph
    assert "-map" in command


def test_the_mux_without_subtitles_still_forces_a_safe_pixel_format() -> None:
    command = render_service.mux_command(
        "ffmpeg", Path("v.mp4"), Path("a.wav"), Path("o.mp4")
    )
    assert "-pix_fmt" in command
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


def test_the_mux_lets_the_picture_decide_the_length() -> None:
    command = render_service.mux_command(
        "ffmpeg", Path("v.mp4"), Path("a.wav"), Path("o.mp4")
    )
    assert "-shortest" in command


def test_a_video_with_no_soundtrack_still_muxes() -> None:
    command = render_service.mux_command("ffmpeg", Path("v.mp4"), None, Path("o.mp4"))
    assert "-c:a" not in command


@pytest.mark.parametrize(
    ("resolution", "aspect", "expected"),
    [
        ("720p", "16:9", (1280, 720)),
        ("1080p", "16:9", (1920, 1080)),
        ("480p", "16:9", (854, 480)),
        ("1080p", "9:16", (1080, 1920)),
        ("720p", "1:1", (720, 720)),
    ],
)
async def test_the_output_frame_follows_the_project(
    session: AsyncSession,
    tmp_path: Path,
    resolution: str,
    aspect: str,
    expected: tuple[int, int],
) -> None:
    project = await project_service.create_project(
        session, name=f"Frame {resolution} {aspect}", resolution=resolution,
        aspect_ratio=aspect,
    )
    await session.commit()
    assert render_service.frame_size(project) == expected


# ---------------------------------------------------------------------------
# Black-frame parsing
# ---------------------------------------------------------------------------


def test_black_runs_are_read_from_the_detector() -> None:
    stderr = (
        "[blackdetect @ 0] black_start:2.5 black_end:4.0 black_duration:1.5\n"
        "[blackdetect @ 0] black_start:9.0 black_end:9.8 black_duration:0.8\n"
    )
    runs = render_service.parse_black_runs(stderr)
    assert len(runs) == 2
    assert runs[0]["black_duration"] == pytest.approx(1.5)


def test_a_clean_video_reports_no_black_runs() -> None:
    assert render_service.parse_black_runs("frame=  510 fps=120\n") == []


def test_an_unparseable_detector_line_is_skipped_not_invented() -> None:
    runs = render_service.parse_black_runs(
        "[blackdetect @ 0] black_start:oops black_duration:also-oops\n"
    )
    assert runs == []


# ---------------------------------------------------------------------------
# Validating the delivered file
# ---------------------------------------------------------------------------


def good_info(**overrides: Any) -> MediaInfo:
    values: dict[str, Any] = {
        "duration_s": 8.0,
        "width": 1280,
        "height": 720,
        "fps": 24.0,
        "video_codec": "h264",
        "has_audio": True,
        "raw": {
            "video_stream": {"pix_fmt": "yuv420p"},
            "streams": [
                {"codec_type": "video", "duration": "8.0"},
                {"codec_type": "audio", "duration": "8.0"},
            ],
        },
    }
    values.update(overrides)
    return MediaInfo(**values)


@pytest.fixture
def stub_probe(monkeypatch: pytest.MonkeyPatch):
    def install(info: MediaInfo) -> None:
        async def probe(
            path: Path, ffprobe_path: str | None = None, *, timeout: int = 0
        ) -> MediaInfo:
            return info

        monkeypatch.setattr("app.services.ffmpeg.probe", probe)

    return install


@pytest.fixture
def no_black(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(command: list[str], *, timeout: int = 0) -> tuple[int, str]:
        return 0, "frame=  192 fps=96\n"

    monkeypatch.setattr(render_service, "_run", run)


async def validate(path: Path, **kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "expected_duration_s": 8.0,
        "expected_width": 1280,
        "expected_height": 720,
        "expects_audio": True,
    }
    defaults.update(kwargs)
    return await render_service.validate_output(path, **defaults)


async def test_a_good_file_passes_every_check(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    stub_probe(good_info())
    target = tmp_path / "out.mp4"
    target.write_bytes(b"\0" * 4096)
    checks = await validate(target)
    assert checks["failures"] == []
    assert checks["measured_duration_s"] == pytest.approx(8.0)
    assert checks["av_offset_s"] == pytest.approx(0.0)


async def test_a_missing_file_fails_immediately(tmp_path: Path) -> None:
    checks = await validate(tmp_path / "never-made.mp4")
    assert "does not exist" in checks["failures"][0]


async def test_an_empty_file_fails(tmp_path: Path) -> None:
    target = tmp_path / "empty.mp4"
    target.write_bytes(b"")
    checks = await validate(target)
    assert "is empty" in checks["failures"][0]


async def test_an_unprobeable_file_is_a_failure_not_a_pass(
    tmp_path: Path, stub_probe: Any
) -> None:
    stub_probe(MediaInfo(error="moov atom not found"))
    target = tmp_path / "broken.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("unknown whether it is playable" in f for f in checks["failures"])


async def test_the_wrong_length_fails(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    stub_probe(good_info(duration_s=5.0))
    target = tmp_path / "short.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("planned" in f for f in checks["failures"])
    assert checks["duration_drift_s"] == pytest.approx(3.0)


async def test_a_frame_off_by_one_is_within_tolerance(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    stub_probe(good_info(duration_s=8.04))
    target = tmp_path / "ok.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert checks["failures"] == []


async def test_the_wrong_frame_size_fails(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    stub_probe(good_info(width=640, height=360))
    target = tmp_path / "small.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("not the 1280x720 asked for" in f for f in checks["failures"])


async def test_the_wrong_pixel_format_fails(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    """yuvj420p is exactly what a JPEG-sourced encode produces by accident."""
    info = good_info()
    info.raw["video_stream"]["pix_fmt"] = "yuvj420p"
    stub_probe(info)
    target = tmp_path / "fullrange.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("yuvj420p" in f for f in checks["failures"])


async def test_an_unusual_codec_is_a_note_rather_than_a_failure(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    stub_probe(good_info(video_codec="vp9"))
    target = tmp_path / "vp9.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert checks["failures"] == []
    assert any("vp9" in note for note in checks["notes"])


async def test_a_silent_video_that_should_have_sound_fails(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    stub_probe(good_info(has_audio=False))
    target = tmp_path / "silent.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("has none" in f for f in checks["failures"])


async def test_drifting_sound_fails(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    info = good_info()
    info.raw["streams"][1]["duration"] = "6.5"
    stub_probe(info)
    target = tmp_path / "drift.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("drift apart" in f for f in checks["failures"])


async def test_unreported_stream_lengths_are_admitted_not_assumed(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    info = good_info()
    info.raw["streams"] = [
        {"codec_type": "video"},
        {"codec_type": "audio"},
    ]
    stub_probe(info)
    target = tmp_path / "nolen.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("could not be compared" in note for note in checks["notes"])


async def test_black_stretches_fail_the_render(
    tmp_path: Path, stub_probe: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_probe(good_info())

    async def run(command: list[str], *, timeout: int = 0) -> tuple[int, str]:
        return 0, "[blackdetect @ 0] black_start:2.0 black_end:4.0 black_duration:2.0\n"

    monkeypatch.setattr(render_service, "_run", run)
    target = tmp_path / "holes.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("black stretch" in f for f in checks["failures"])


async def test_a_failed_black_scan_is_admitted_not_treated_as_clean(
    tmp_path: Path, stub_probe: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_probe(good_info())

    async def run(command: list[str], *, timeout: int = 0) -> tuple[int, str]:
        return 1, "the scan died"

    monkeypatch.setattr(render_service, "_run", run)
    target = tmp_path / "unscanned.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target)
    assert any("not ruled out" in note for note in checks["notes"])


async def test_loudness_off_target_is_reported_in_the_checks(
    tmp_path: Path, stub_probe: Any, no_black: None
) -> None:
    stub_probe(good_info())
    target = tmp_path / "quiet.mp4"
    target.write_bytes(b"\0" * 1024)
    checks = await validate(target, loudness={"integrated_lufs": -30.0, "lra_lu": 4.0})
    assert checks["integrated_lufs"] == pytest.approx(-30.0)
    assert any("LUFS" in note for note in checks["notes"])


# ---------------------------------------------------------------------------
# A whole render, with FFmpeg stubbed
# ---------------------------------------------------------------------------


async def test_a_render_that_passes_is_recorded_as_completed(
    session: AsyncSession,
    tmp_path: Path,
    fake_ffmpeg: list[list[str]],
    stub_probe: Any,
) -> None:
    stub_probe(good_info())
    project_id = await build_project(session, tmp_path)
    outcome = await render_service.render_project(session, project_id)
    assert outcome.succeeded
    assert outcome.duration_s == pytest.approx(8.0)
    assert Path(outcome.subtitle_path).exists()

    record = (await session.execute(select(Render))).scalars().one()
    assert record.status == RenderStatus.COMPLETED
    assert record.report_path
    assert json.loads(Path(record.report_path).read_text())["status"] == "completed"


async def test_a_file_that_fails_its_checks_is_not_called_a_success(
    session: AsyncSession,
    tmp_path: Path,
    fake_ffmpeg: list[list[str]],
    stub_probe: Any,
) -> None:
    """FFmpeg returned zero; the file is still the wrong length."""
    stub_probe(good_info(duration_s=3.0))
    project_id = await build_project(session, tmp_path)
    outcome = await render_service.render_project(session, project_id)
    assert not outcome.succeeded
    record = (await session.execute(select(Render))).scalars().one()
    assert record.status == RenderStatus.FAILED
    assert record.output_path  # the file exists, it is simply not right
    assert "planned" in (record.error or "")


async def test_a_blocked_render_writes_a_checkpoint_saying_why(
    session: AsyncSession, tmp_path: Path, fake_ffmpeg: list[list[str]]
) -> None:
    project_id = await build_project(session, tmp_path, selected=False)
    outcome = await render_service.render_project(session, project_id)
    assert outcome.status == RenderStatus.FAILED
    checkpoint = (
        await session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.project_id == project_id,
                StageCheckpoint.stage == PipelineStage.RENDER,
            )
        )
    ).scalar_one()
    assert checkpoint.status == StageStatus.BLOCKED
    assert checkpoint.blocked_reason


async def test_a_blocked_render_creates_no_render_row(
    session: AsyncSession, tmp_path: Path, fake_ffmpeg: list[list[str]]
) -> None:
    project_id = await build_project(session, tmp_path, selected=False)
    await render_service.render_project(session, project_id)
    assert (await session.execute(select(Render))).scalars().all() == []


async def test_the_report_keeps_the_two_kinds_of_length_apart(
    session: AsyncSession,
    tmp_path: Path,
    fake_ffmpeg: list[list[str]],
    stub_probe: Any,
) -> None:
    stub_probe(good_info())
    project_id = await build_project(session, tmp_path, generated=False)
    await render_service.render_project(session, project_id)
    record = (await session.execute(select(Render))).scalars().one()
    report = json.loads(Path(record.report_path).read_text())
    assert report["generated_motion_s"] == 0.0
    assert report["camera_move_s"] == pytest.approx(8.0)
    assert "editing rather than animation" in report["honest_summary"]


async def test_a_missing_music_bed_degrades_to_narration_only(
    session: AsyncSession,
    tmp_path: Path,
    fake_ffmpeg: list[list[str]],
    stub_probe: Any,
) -> None:
    stub_probe(good_info())
    # The project is created with a bed that exists, because the project
    # screen refuses one that does not; it then disappears, which is the
    # case this is about.
    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"\0" * 1024)
    project_id = await build_project(session, tmp_path, music_path=str(bed))
    bed.unlink()
    outcome = await render_service.render_project(session, project_id)
    assert outcome.succeeded
    assert any("music bed" in problem for problem in outcome.problems)


async def test_subtitles_can_be_left_as_a_sidecar_instead_of_burnt_in(
    session: AsyncSession,
    tmp_path: Path,
    fake_ffmpeg: list[list[str]],
    stub_probe: Any,
) -> None:
    stub_probe(good_info())
    project_id = await build_project(session, tmp_path)
    outcome = await render_service.render_project(
        session, project_id, burn_subtitles=False
    )
    assert Path(outcome.subtitle_path).exists()
    muxes = [c for c in fake_ffmpeg if "-movflags" in c]
    assert muxes
    assert all("subtitles=" not in " ".join(c) for c in muxes)


async def test_a_project_with_subtitles_disabled_gets_none(
    session: AsyncSession,
    tmp_path: Path,
    fake_ffmpeg: list[list[str]],
    stub_probe: Any,
) -> None:
    stub_probe(good_info())
    project_id = await build_project(session, tmp_path, subtitles_enabled=False)
    outcome = await render_service.render_project(session, project_id)
    assert outcome.subtitle_path == ""


async def test_a_render_without_ffmpeg_is_blocked(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(
        ffmpeg_override: str = "", ffprobe_override: str = ""
    ) -> FFmpegStatus:
        return FFmpegStatus(message="FFmpeg is not installed.")

    monkeypatch.setattr("app.services.ffmpeg.detect", missing)
    project_id = await build_project(session, tmp_path)
    outcome = await render_service.render_project(session, project_id)
    assert outcome.status == RenderStatus.FAILED
    assert "FFmpeg is not installed" in outcome.problems[0]


async def test_an_ffmpeg_failure_mid_render_is_recorded_against_the_render(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(command: list[str], *, timeout: int = 0) -> tuple[int, str]:
        return 1, "Invalid data found when processing input"

    monkeypatch.setattr(render_service, "_run", failing)
    project_id = await build_project(session, tmp_path)
    outcome = await render_service.render_project(session, project_id)
    assert outcome.status == RenderStatus.FAILED
    record = (await session.execute(select(Render))).scalars().one()
    assert "could not be prepared" in (record.error or "")


# ---------------------------------------------------------------------------
# One genuine render, with real FFmpeg
# ---------------------------------------------------------------------------


@pytest.mark.real_ffprobe
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not on PATH here.",
)
async def test_a_real_render_comes_out_the_right_length_and_in_sync(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The whole chain, for real: clips, narration, subtitles, checks.

    The sources are synthesised with FFmpeg rather than committed, so this
    proves the commands work on this machine without shipping media in the
    repository.
    """
    import asyncio

    ffmpeg_path = shutil.which("ffmpeg")
    assert ffmpeg_path

    async def make(args: list[str]) -> None:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        assert process.returncode == 0, (stderr or b"").decode()[-600:]

    clip_lengths = [3.0, 2.0]
    line_lengths = [2.0, 1.2]
    for index, length in enumerate(clip_lengths):
        await make(
            [
                ffmpeg_path, "-hide_banner", "-y",
                "-f", "lavfi",
                "-i", f"testsrc=size=640x360:rate=24:duration={length}",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(tmp_path / f"clip-{index}.mp4"),
            ]
        )
    for index, length in enumerate(line_lengths):
        await make(
            [
                ffmpeg_path, "-hide_banner", "-y",
                "-f", "lavfi",
                "-i", f"sine=frequency=440:duration={length}:sample_rate=48000",
                "-ac", "1", "-c:a", "pcm_s16le",
                str(tmp_path / f"line-{index}.wav"),
            ]
        )

    project_id = await build_project(
        session,
        tmp_path,
        clip_durations=clip_lengths,
        segment_durations=line_lengths,
        resolution="480p",
        fps=24,
    )

    outcome = await render_service.render_project(session, project_id)

    assert outcome.succeeded, outcome.problems
    # 3.0 + 2.0 of picture, measured by ffprobe on the delivered file.
    assert outcome.duration_s == pytest.approx(5.0, abs=0.2)
    assert outcome.checks["width"] == 854
    assert outcome.checks["height"] == 480
    assert outcome.checks["video_codec"] == "h264"
    assert outcome.checks["pixel_format"] == "yuv420p"
    assert outcome.checks["has_audio"] is True
    assert outcome.checks["av_offset_s"] <= render_service.SYNC_TOLERANCE_S
    assert outcome.checks["black_runs"] == []
    # Nothing here was animated, and the summary has to say so.
    assert outcome.generated_motion_s == 0.0

    delivered = Path(outcome.output_path)
    assert delivered.exists() and delivered.stat().st_size > 10_000
    srt = Path(outcome.subtitle_path).read_text(encoding="utf-8")
    assert "00:00:00,300 --> 00:00:02,300" in srt


# ---------------------------------------------------------------------------
# The screen, rendered for real
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_the_render_screen_renders(
    client: AsyncClient, session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path)
    response = await client.get(f"/projects/{project_id}/render")
    assert response.status_code == 200
    assert "Camera moves" in response.text
    assert "Editing, not animation" in response.text


async def test_the_screen_lists_the_timed_cues(
    client: AsyncClient, session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path)
    response = await client.get(f"/projects/{project_id}/render")
    assert "with ffprobe" in response.text
    assert "The first thing happens here" in response.text


async def test_the_screen_explains_why_it_cannot_assemble(
    client: AsyncClient, session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await build_project(session, tmp_path, selected=False)
    response = await client.get(f"/projects/{project_id}/render")
    assert "Why it cannot be assembled yet" in response.text
    assert "no clip is in the edit" in response.text


async def test_an_unknown_project_has_no_render_screen(client: AsyncClient) -> None:
    response = await client.get("/projects/9999/render")
    assert response.status_code == 404
