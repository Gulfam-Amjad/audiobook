"""Shot routing, camera moves, and the motion-versus-editing distinction.

The tests that matter most here are the accounting ones. A camera move
over a still must never be counted, labelled, or described as motion a
model generated, and a paid API must never become available just because
a scene asked for it.

The FFmpeg command builder is tested by inspecting the command rather
than by running it, with one real render at the end to prove the recipe
works on this machine. Everything else uses a stub so the suite does not
depend on FFmpeg being installed.
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
    GENERATED_MOTION_METHODS,
    AnimationMethod,
    ClipStatus,
    PipelineStage,
    SceneStatus,
    StageStatus,
)
from app.models import Image, ImageFolder, Scene, SceneClip
from app.services import camera
from app.services import projects as project_service
from app.services import routing as routing_service
from app.services import scripts as script_service
from app.services.ffmpeg import FFmpegStatus
from tests.conftest import write_image

pytestmark = pytest.mark.usefixtures("data_root")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def ffmpeg_present(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pretend FFmpeg is installed, so the suite does not depend on it.

    Tests marked ``real_ffprobe`` want the genuine article and opt out,
    matching how the conftest ffprobe stub behaves.
    """
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
def fake_render(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record what would have been rendered and write a placeholder file."""
    calls: list[dict[str, Any]] = []

    async def render(
        ffmpeg_path: str, source: Path, destination: Path, **kwargs: Any
    ) -> camera.CameraRenderResult:
        calls.append({"source": source, "destination": destination, **kwargs})
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\0" * 4096)
        return camera.CameraRenderResult(True, path=destination)

    monkeypatch.setattr(routing_service.camera, "render", render)
    return calls


def scene_dict(key: str, **extra: Any) -> dict[str, Any]:
    base = {
        "scene_key": key,
        "purpose": f"Beat {key}",
        "target_duration_s": 10.0,
        "narration": f"Something quite specific happens during beat {key}.",
        "dialogue": [],
        "characters": [],
        "preferred_method": "camera_move",
    }
    base.update(extra)
    return base


async def add_image(session: AsyncSession, tmp_path: Path, name: str) -> Image:
    folder = (
        await session.execute(select(ImageFolder).where(ImageFolder.path == str(tmp_path)))
    ).scalars().first()
    if folder is None:
        folder = ImageFolder(path=str(tmp_path))
        session.add(folder)
        await session.flush()
    path = write_image(tmp_path / name)
    image = Image(
        folder_id=folder.id,
        path=str(path),
        filename=name,
        extension=".jpg",
        sha256=name,
        width=1600,
        height=1200,
    )
    session.add(image)
    await session.flush()
    return image


async def make_project(
    session: AsyncSession,
    scenes: list[dict[str, Any]],
    *,
    tmp_path: Path | None = None,
    with_images: bool = True,
    **project_values: Any,
) -> int:
    planned = sum(float(s["target_duration_s"]) for s in scenes)
    values = {"name": "Shot test", "target_duration_s": max(15, round(planned))}
    values.update(project_values)
    project = await project_service.create_project(session, **values)
    plan = {
        "title": "T",
        "logline": "L",
        "synopsis": "S",
        "scenes": scenes,
    }
    script = await script_service.accept_paste(session, project.id, json.dumps(plan))
    await script_service.approve(session, script.id)

    if with_images and tmp_path is not None:
        rows = (
            await session.execute(
                select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index)
            )
        ).scalars().all()
        for index, scene in enumerate(rows):
            image = await add_image(session, tmp_path, f"still-{index}.jpg")
            scene.image_id = image.id
    await session.flush()
    return project.id


async def set_measured(session: AsyncSession, project_id: int, seconds: list[float]) -> None:
    rows = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().all()
    for scene, value in zip(rows, seconds, strict=False):
        scene.measured_audio_s = value
    await session.flush()


# ---------------------------------------------------------------------------
# Camera moves: the filter and the command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("slow push through the doorway", "push_in"),
        ("Zoom in on her hands", "push_in"),
        ("pull out to reveal the room", "pull_out"),
        ("pan left across the table", "pan_left"),
        ("track right past the window", "pan_right"),
        ("tilt up to the ceiling", "tilt_up"),
        ("crane down onto the floor", "tilt_down"),
        ("locked off, static", "hold"),
        ("", "push_in"),
        ("something the writer invented", "push_in"),
    ],
)
def test_a_camera_note_is_turned_into_a_move_we_can_render(
    note: str, expected: str
) -> None:
    assert camera.move_for(note) == expected


@pytest.mark.parametrize("move", [m.key for m in camera.MOVES])
def test_every_move_produces_a_runnable_command(move: str) -> None:
    command = camera.build_command(
        "ffmpeg", Path("in.jpg"), Path("out.mp4"),
        move=move, width=1280, height=720, fps=24, duration_s=4.0,
    )
    assert command[0] == "ffmpeg"
    assert "-loop" in command
    assert command[command.index("-t") + 1] == "4.000"
    assert command[-1] == "out.mp4"
    filtergraph = command[command.index("-vf") + 1]
    assert "format=yuv420p" in filtergraph
    assert f"fps={24}" in filtergraph or move == "hold"


def test_the_frame_size_reaches_the_filter_unchanged() -> None:
    filtergraph = camera.build_filter("push_in", width=1920, height=1080, fps=30, duration_s=5.0)
    assert "s=1920x1080" in filtergraph
    assert "fps=30" in filtergraph


def test_a_hold_contains_no_zoom_at_all() -> None:
    filtergraph = camera.build_filter("hold", width=1280, height=720, fps=24, duration_s=3.0)
    assert "zoompan" not in filtergraph


def test_the_still_is_prescaled_so_the_move_does_not_step() -> None:
    filtergraph = camera.build_filter("push_in", width=1280, height=720, fps=24, duration_s=4.0)
    big = camera.prescale_width(1280)
    assert big > 1280
    assert f"scale={big}:" in filtergraph


def test_the_prescale_is_capped_for_memory() -> None:
    assert camera.prescale_width(3840) <= camera.MAX_PRESCALE_WIDTH


def test_prescaled_dimensions_stay_even() -> None:
    """Odd dimensions break H.264 with yuv420p."""
    for width in (1279, 1280, 1281, 854, 641):
        assert camera.prescale_width(width) % 2 == 0


def test_an_unknown_move_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="Unknown camera move"):
        camera.build_command(
            "ffmpeg", Path("in.jpg"), Path("out.mp4"),
            move="barrel_roll", width=1280, height=720, fps=24, duration_s=4.0,
        )


@pytest.mark.parametrize("duration", [0.0, 0.1, 120.0])
def test_an_impossible_duration_is_refused(duration: float) -> None:
    with pytest.raises(ValueError, match="must be between"):
        camera.build_command(
            "ffmpeg", Path("in.jpg"), Path("out.mp4"),
            move="push_in", width=1280, height=720, fps=24, duration_s=duration,
        )


async def test_a_missing_still_fails_without_running_ffmpeg(tmp_path: Path) -> None:
    result = await camera.render(
        "ffmpeg", tmp_path / "nope.jpg", tmp_path / "out.mp4",
        move="push_in", width=1280, height=720, fps=24, duration_s=4.0,
    )
    assert result.ok is False
    assert "missing" in result.message


@pytest.mark.parametrize("move", [m.key for m in camera.MOVES])
def test_a_move_is_described_without_calling_it_animation(move: str) -> None:
    text = camera.describe(move)
    assert "does not move" in text
    for word in ("animat", "generated motion", "AI motion"):
        assert word not in text.lower()


def test_camera_moves_are_not_in_the_generated_motion_set() -> None:
    assert not (camera.PRODUCES & GENERATED_MOTION_METHODS)


# ---------------------------------------------------------------------------
# Method availability
# ---------------------------------------------------------------------------


async def test_a_paid_api_is_unavailable_until_spending_is_armed(
    session: AsyncSession,
) -> None:
    """The backend half of 'no paid service without explicit approval'."""
    statuses = await routing_service.method_availability(session)
    paid = statuses[AnimationMethod.API_I2V]
    assert paid.available is False
    assert paid.costs_money is True
    assert "switched off" in paid.reason


async def test_borrowed_gpu_time_is_unavailable_without_credentials(
    session: AsyncSession,
) -> None:
    statuses = await routing_service.method_availability(session)
    for method in (AnimationMethod.REMOTE_I2V, AnimationMethod.LIPSYNC):
        assert statuses[method].available is False
        assert "Kaggle" in statuses[method].reason


async def test_camera_moves_are_available_whenever_ffmpeg_is(
    session: AsyncSession,
) -> None:
    statuses = await routing_service.method_availability(session)
    assert statuses[AnimationMethod.CAMERA_MOVE].available is True
    assert statuses[AnimationMethod.CAMERA_MOVE].generates_motion is False


async def test_nothing_that_costs_money_is_available_by_default(
    session: AsyncSession,
) -> None:
    statuses = await routing_service.method_availability(session)
    for status in statuses.values():
        if status.costs_money:
            assert status.available is False


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


async def test_planning_needs_an_approved_script(session: AsyncSession) -> None:
    project = await project_service.create_project(session, name="Empty")
    with pytest.raises(routing_service.RoutingError, match="No approved script"):
        await routing_service.plan_shots(session, project.id)


async def test_measured_narration_sets_the_shot_length(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await set_measured(session, project_id, [6.4, 3.1])

    plan = await routing_service.plan_shots(session, project_id)

    assert plan.decisions[0].duration_measured is True
    assert plan.decisions[0].duration_s == pytest.approx(6.4 + routing_service.SHOT_PADDING_S)
    assert plan.decisions[1].duration_s == pytest.approx(3.1 + routing_service.SHOT_PADDING_S)


async def test_an_unrecorded_shot_falls_back_to_the_planned_length(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    plan = await routing_service.plan_shots(session, project_id)

    assert all(d.duration_measured is False for d in plan.decisions)
    assert plan.decisions[0].duration_s == pytest.approx(10.0)
    assert any("measured audio" in note for note in plan.notes)


async def test_a_very_short_line_still_gets_a_watchable_shot(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await set_measured(session, project_id, [0.2, 5.0])
    plan = await routing_service.plan_shots(session, project_id)
    assert plan.decisions[0].duration_s >= routing_service.MIN_SHOT_S


async def test_a_request_for_a_paid_api_falls_back_to_a_camera_move(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session,
        [scene_dict("s01", preferred_method="api_i2v"), scene_dict("s02")],
        tmp_path=tmp_path,
    )
    plan = await routing_service.plan_shots(session, project_id)

    assert plan.decisions[0].assigned == AnimationMethod.CAMERA_MOVE
    assert plan.decisions[0].requested == AnimationMethod.API_I2V
    assert "switched off" in plan.decisions[0].reason
    assert plan.motion_assigned_s == 0.0


async def test_the_motion_budget_stops_at_its_ceiling(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Manual image-to-video is available, so the budget is what limits it."""
    scenes = [scene_dict(f"s{i:02d}", preferred_method="manual_i2v") for i in range(1, 7)]
    project_id = await make_project(
        session, scenes, tmp_path=tmp_path, max_generated_motion_s=25.0
    )
    plan = await routing_service.plan_shots(session, project_id)

    assert plan.motion_assigned_s <= 25.0
    generated = [d for d in plan.decisions if d.is_generated_motion]
    fell_back = [d for d in plan.decisions if not d.is_generated_motion]
    assert len(generated) == 2
    assert len(fell_back) == 4
    assert all(d.assigned == AnimationMethod.CAMERA_MOVE for d in fell_back)
    assert any("budget is spent" in d.reason for d in fell_back)


async def test_the_budget_is_spent_on_the_earliest_shots(
    session: AsyncSession, tmp_path: Path
) -> None:
    scenes = [scene_dict(f"s{i:02d}", preferred_method="manual_i2v") for i in range(1, 7)]
    project_id = await make_project(
        session, scenes, tmp_path=tmp_path, max_generated_motion_s=25.0
    )
    plan = await routing_service.plan_shots(session, project_id)
    assert [d.is_generated_motion for d in plan.decisions] == [
        True, True, False, False, False, False
    ]


async def test_a_zero_budget_routes_everything_to_camera_moves(
    session: AsyncSession, tmp_path: Path
) -> None:
    scenes = [scene_dict(f"s{i:02d}", preferred_method="manual_i2v") for i in range(1, 4)]
    project_id = await make_project(
        session, scenes, tmp_path=tmp_path, max_generated_motion_s=0.0
    )
    plan = await routing_service.plan_shots(session, project_id)

    assert plan.motion_assigned_s == 0.0
    assert all(d.assigned == AnimationMethod.CAMERA_MOVE for d in plan.decisions)
    assert any("camera move" in note and "not animation" in note for note in plan.notes)


async def test_every_shot_keeps_a_camera_move_as_its_last_resort(
    session: AsyncSession, tmp_path: Path
) -> None:
    scenes = [scene_dict(f"s{i:02d}", preferred_method="manual_i2v") for i in range(1, 4)]
    project_id = await make_project(
        session, scenes, tmp_path=tmp_path, max_generated_motion_s=500.0
    )
    plan = await routing_service.plan_shots(session, project_id)
    for decision in plan.decisions:
        assert decision.fallbacks[-1] == AnimationMethod.CAMERA_MOVE


async def test_an_unknown_requested_method_does_not_break_planning(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    scene = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().first()
    scene.preferred_method = "interpretive_dance"
    await session.flush()

    plan = await routing_service.plan_shots(session, project_id)
    assert plan.decisions[0].assigned == AnimationMethod.CAMERA_MOVE
    assert "not a method" in plan.decisions[0].reason


async def test_planning_marks_the_routing_stage_complete(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.ROUTING
    )
    assert checkpoint.status == StageStatus.COMPLETED
    assert checkpoint.payload["shots"] == 2


async def test_the_camera_note_chosen_at_planning_is_kept_for_rendering(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session,
        [scene_dict("s01", camera="pan left past the sink"), scene_dict("s02")],
        tmp_path=tmp_path,
    )
    plan = await routing_service.plan_shots(session, project_id)
    assert plan.decisions[0].camera_move == "pan_left"
    scene = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().first()
    assert scene.validation["camera_move"] == "pan_left"


# ---------------------------------------------------------------------------
# Rendering, and what gets counted
# ---------------------------------------------------------------------------


async def test_rendering_produces_one_selected_clip_per_shot(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()

    run = await routing_service.render_local_shots(session, project_id)

    assert run.rendered == 2
    assert run.failed == 0
    clips = (
        await session.execute(select(SceneClip).where(SceneClip.project_id == project_id))
    ).scalars().all()
    assert len(clips) == 2
    assert all(clip.selected for clip in clips)
    assert all(clip.status == ClipStatus.READY for clip in clips)


async def test_a_camera_move_is_never_counted_as_generated_motion(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    report = await routing_service.motion_report(session, project_id)
    assert report["assembled_s"] > 0
    assert report["generated_motion_s"] == 0.0
    assert report["camera_move_s"] == report["assembled_s"]
    assert report["generated_share"] == 0.0

    clips = (
        await session.execute(select(SceneClip).where(SceneClip.project_id == project_id))
    ).scalars().all()
    assert all(clip.is_generated_motion is False for clip in clips)


async def test_the_summary_says_plainly_when_nothing_was_animated(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    text = routing_service.honest_summary(
        await routing_service.motion_report(session, project_id)
    )
    assert "none of it generated motion" in text
    assert "camera move over a still" in text


def test_the_summary_separates_the_two_numbers_when_both_exist() -> None:
    text = routing_service.honest_summary(
        {"assembled_s": 300.0, "generated_motion_s": 45.0}
    )
    assert "300.0s assembled" in text
    assert "45.0s is motion a model generated" in text
    assert "255.0s is camera moves over stills" in text


def test_the_summary_refuses_to_report_anything_before_a_render() -> None:
    text = routing_service.honest_summary({"assembled_s": 0.0, "generated_motion_s": 0.0})
    assert "Nothing has been rendered yet" in text


async def test_a_generated_clip_is_counted_separately_from_an_edited_one(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    # Stand in for a clip that a model really did animate.
    scene = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().first()
    await routing_service._deselect_others(session, scene.id)  # noqa: SLF001
    session.add(
        SceneClip(
            scene_id=scene.id,
            project_id=project_id,
            method=AnimationMethod.REMOTE_I2V,
            file_path=str(tmp_path / "animated.mp4"),
            duration_s=4.0,
            is_generated_motion=True,
            selected=True,
            status=ClipStatus.READY,
        )
    )
    await session.flush()

    report = await routing_service.motion_report(session, project_id)
    assert report["generated_motion_s"] == pytest.approx(4.0)
    assert report["camera_move_s"] == pytest.approx(report["assembled_s"] - 4.0)
    assert report["generated_clips"] == 1
    assert report["camera_move_clips"] == 1


async def test_only_the_selected_take_is_counted(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)
    before = await routing_service.motion_report(session, project_id)

    scene_id = (
        await session.execute(select(Scene.id).where(Scene.project_id == project_id))
    ).scalars().first()
    session.add(
        SceneClip(
            scene_id=scene_id,
            project_id=project_id,
            method=AnimationMethod.REMOTE_I2V,
            duration_s=99.0,
            is_generated_motion=True,
            selected=False,
            status=ClipStatus.READY,
        )
    )
    await session.flush()

    after = await routing_service.motion_report(session, project_id)
    assert after == before


async def test_an_unfinished_clip_is_not_counted(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    scene_id = (
        await session.execute(select(Scene.id).where(Scene.project_id == project_id))
    ).scalars().first()
    session.add(
        SceneClip(
            scene_id=scene_id,
            project_id=project_id,
            method=AnimationMethod.REMOTE_I2V,
            duration_s=30.0,
            is_generated_motion=True,
            selected=True,
            status=ClipStatus.RENDERING,
        )
    )
    await session.flush()

    report = await routing_service.motion_report(session, project_id)
    assert report["generated_motion_s"] == 0.0
    assert report["assembled_s"] == 0.0


async def test_choosing_a_different_take_replaces_the_old_one(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    scene_id = (
        await session.execute(select(Scene.id).where(Scene.project_id == project_id))
    ).scalars().first()
    alternative = SceneClip(
        scene_id=scene_id,
        project_id=project_id,
        method=AnimationMethod.REMOTE_I2V,
        duration_s=5.0,
        is_generated_motion=True,
        selected=False,
        status=ClipStatus.READY,
    )
    session.add(alternative)
    await session.flush()

    await routing_service.select_clip(session, alternative.id)

    selected = (
        await session.execute(
            select(SceneClip).where(
                SceneClip.scene_id == scene_id, SceneClip.selected.is_(True)
            )
        )
    ).scalars().all()
    assert len(selected) == 1
    assert selected[0].id == alternative.id
    superseded = (
        await session.execute(
            select(SceneClip).where(
                SceneClip.scene_id == scene_id, SceneClip.id != alternative.id
            )
        )
    ).scalars().all()
    assert all(clip.status == ClipStatus.REPLACED for clip in superseded)


async def test_an_unfinished_clip_cannot_be_put_in_the_edit(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    scene_id = (
        await session.execute(select(Scene.id).where(Scene.project_id == project_id))
    ).scalars().first()
    clip = SceneClip(
        scene_id=scene_id,
        project_id=project_id,
        method=AnimationMethod.CAMERA_MOVE,
        status=ClipStatus.FAILED,
    )
    session.add(clip)
    await session.flush()

    with pytest.raises(routing_service.RoutingError, match="finished clip"):
        await routing_service.select_clip(session, clip.id)


async def test_a_shot_with_no_still_is_reported_not_padded_with_black(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    scene = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().first()
    scene.image_id = None
    await session.flush()
    await routing_service.plan_shots(session, project_id)
    await session.commit()

    run = await routing_service.render_local_shots(session, project_id)

    assert run.failed == 1
    assert run.rendered == 1
    assert any("no still chosen" in error for error in run.errors)
    report = await routing_service.motion_report(session, project_id)
    assert report["scenes_without_clip"] == 1


async def test_a_failed_shot_leaves_the_stage_partial(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    scene = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().first()
    scene.image_id = None
    await session.flush()
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.MOTION
    )
    assert checkpoint.status == StageStatus.PARTIAL


async def test_a_missing_ffmpeg_blocks_rather_than_fails(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )

    async def no_ffmpeg(
        ffmpeg_override: str = "", ffprobe_override: str = ""
    ) -> FFmpegStatus:
        return FFmpegStatus(message="ffmpeg was not found.")

    monkeypatch.setattr("app.services.ffmpeg.detect", no_ffmpeg)
    run = await routing_service.render_local_shots(session, project_id)

    assert run.rendered == 0
    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.MOTION
    )
    assert checkpoint.status == StageStatus.BLOCKED


async def test_an_unchanged_shot_is_not_re_rendered(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)
    assert len(fake_render) == 2

    await routing_service.plan_shots(session, project_id)
    await session.commit()
    run = await routing_service.render_local_shots(session, project_id)

    assert len(fake_render) == 2
    assert run.reused == 2
    assert run.rendered == 0


async def test_a_shot_whose_length_changed_is_re_rendered(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)
    fake_render.clear()

    # Only the first shot now has recorded narration, so only its length
    # changes. The second keeps the script's planned length.
    await set_measured(session, project_id, [7.5])
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    run = await routing_service.render_local_shots(session, project_id)

    assert run.rendered == 1
    assert run.reused == 1
    assert len(fake_render) == 1


async def test_a_shot_waiting_on_an_outside_service_is_skipped_not_faked(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    scenes = [scene_dict(f"s{i:02d}", preferred_method="manual_i2v") for i in range(1, 4)]
    project_id = await make_project(
        session, scenes, tmp_path=tmp_path, max_generated_motion_s=500.0
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()

    run = await routing_service.render_local_shots(session, project_id)

    assert run.skipped == 3
    assert run.rendered == 0
    report = await routing_service.motion_report(session, project_id)
    assert report["generated_motion_s"] == 0.0
    assert report["scenes_without_clip"] == 3


async def test_the_rendered_length_comes_from_probing_the_file(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    """The conftest stub reports 4.0s regardless of what was asked for."""
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    clips = (
        await session.execute(select(SceneClip).where(SceneClip.project_id == project_id))
    ).scalars().all()
    assert all(clip.duration_s == pytest.approx(4.0) for clip in clips)
    assert all(
        clip.validation["requested_duration_s"] == pytest.approx(10.0) for clip in clips
    )


async def test_a_rendered_shot_records_which_move_was_used(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session,
        [scene_dict("s01", camera="tilt up to the window"), scene_dict("s02")],
        tmp_path=tmp_path,
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    assert fake_render[0]["move"] == "tilt_up"
    clip = (
        await session.execute(select(SceneClip).order_by(SceneClip.id))
    ).scalars().first()
    assert clip.validation["camera_move"] == "tilt_up"
    assert "does not move" in clip.validation["description"]


async def test_rendering_uses_the_projects_frame_size_and_rate(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session,
        [scene_dict("s01"), scene_dict("s02")],
        tmp_path=tmp_path,
        resolution="1080p",
        aspect_ratio="9:16",
        fps=30,
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    expected = project_service.frame_size("1080p", "9:16")
    assert fake_render[0]["width"] == expected[0]
    assert fake_render[0]["height"] == expected[1]
    assert fake_render[0]["fps"] == 30


async def test_a_scene_is_marked_ready_once_it_has_a_clip(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    scenes = (
        await session.execute(select(Scene).where(Scene.project_id == project_id))
    ).scalars().all()
    assert all(scene.status == SceneStatus.READY for scene in scenes)


# ---------------------------------------------------------------------------
# One real render, to prove the recipe works here
# ---------------------------------------------------------------------------


@pytest.mark.real_ffprobe
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not on PATH")
async def test_a_real_camera_move_comes_out_the_requested_length(
    tmp_path: Path,
) -> None:
    from app.services import ffmpeg as ffmpeg_service

    source = write_image(tmp_path / "still.jpg", size=(1600, 1200), colour="steelblue")
    destination = tmp_path / "move.mp4"
    result = await camera.render(
        shutil.which("ffmpeg"), source, destination,
        move="push_in", width=640, height=360, fps=24, duration_s=2.0,
    )
    assert result.ok is True, result.message

    info = await ffmpeg_service.probe(destination)
    assert info.error is None
    assert info.duration_s == pytest.approx(2.0, abs=0.08)
    assert (info.width, info.height) == (640, 360)
    assert info.raw["video_stream"]["pix_fmt"] == "yuv420p"


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_the_shots_page_renders_before_a_script_exists(
    client: AsyncClient, session: AsyncSession
) -> None:
    await project_service.create_project(session, name="Fresh")
    await session.commit()

    response = await client.get("/projects/1/shots")
    assert response.status_code == 200
    assert "No approved script" in response.text


async def test_the_shots_page_separates_motion_from_camera_moves(
    client: AsyncClient, session: AsyncSession, tmp_path: Path, fake_render: list[Any]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await session.commit()
    await routing_service.render_local_shots(session, project_id)

    response = await client.get(f"/projects/{project_id}/shots")
    assert response.status_code == 200
    assert "Generated motion" in response.text
    assert "Camera moves" in response.text
    assert "Editing, not animation" in response.text
    assert "none of it generated motion" in response.text


async def test_the_shots_page_says_a_paid_api_is_switched_off(
    client: AsyncClient, session: AsyncSession
) -> None:
    await project_service.create_project(session, name="Fresh")
    await session.commit()

    response = await client.get("/projects/1/shots")
    assert "can cost money" in response.text
    assert "switched off" in response.text


async def test_rendering_over_http_reports_what_was_actually_made(
    client: AsyncClient, session: AsyncSession, tmp_path: Path, fake_render: list[Any]
) -> None:
    project_id = await make_project(
        session, [scene_dict("s01"), scene_dict("s02")], tmp_path=tmp_path
    )
    await session.commit()

    response = await client.post(f"/projects/{project_id}/shots/render")

    assert response.status_code == 200
    flash = response.headers.get("X-AVM-Flash", "")
    assert "2 rendered" in flash
    assert "none of it generated motion" in flash


async def test_rendering_without_a_script_is_refused_over_http(
    client: AsyncClient, session: AsyncSession
) -> None:
    await project_service.create_project(session, name="Fresh")
    await session.commit()

    response = await client.post("/projects/1/shots/render")
    assert response.status_code == 400
    assert "approved script" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Found by running the real pipeline
# ---------------------------------------------------------------------------


async def test_clips_from_a_replaced_script_are_not_counted(
    session: AsyncSession, tmp_path: Path, fake_render: list[dict[str, Any]]
) -> None:
    """Rewriting a script leaves the old version's clips in the table.

    Counting those made a sixty-second cut report as a hundred and fifty
    seconds assembled. The render itself was right, because it walks the
    approved script's shots; only the report was wrong.
    """
    project_id = await make_project(
        session, [scene_dict("a"), scene_dict("b")], tmp_path=tmp_path
    )
    await routing_service.plan_shots(session, project_id)
    await routing_service.render_local_shots(session, project_id)
    first = await routing_service.motion_report(session, project_id)
    assert first["clips_ready"] == 2

    replacement = await script_service.accept_paste(
        session,
        project_id,
        json.dumps(
            {
                "title": "T2",
                "logline": "L",
                "synopsis": "S",
                "scenes": [scene_dict("c"), scene_dict("d")],
            }
        ),
    )
    await script_service.approve(session, replacement.id)
    rows = (
        await session.execute(
            select(Scene)
            .where(Scene.script_id == replacement.id)
            .order_by(Scene.order_index)
        )
    ).scalars().all()
    for index, scene in enumerate(rows):
        image = await add_image(session, tmp_path, f"replacement-{index}.jpg")
        scene.image_id = image.id
    await session.commit()

    await routing_service.plan_shots(session, project_id)
    await routing_service.render_local_shots(session, project_id)

    second = await routing_service.motion_report(session, project_id)
    assert len((await session.execute(select(SceneClip))).scalars().all()) == 4
    assert second["clips_ready"] == 2
    assert second["assembled_s"] == pytest.approx(first["assembled_s"])