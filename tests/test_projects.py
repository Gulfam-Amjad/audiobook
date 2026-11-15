"""Projects, stage checkpoints and the measured-progress accounting."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    AnimationMethod,
    AudioStatus,
    ClipStatus,
    PipelineStage,
    ProjectStatus,
    SceneStatus,
    StageStatus,
)
from app.models import AudioSegment, ImageFolder, Scene, SceneClip, Script
from app.services import projects as project_service
from app.services.projects import ProjectError

pytestmark = pytest.mark.usefixtures("data_root")


async def make_project(session: AsyncSession, **overrides) -> int:
    values = {"name": "Test project", "target_duration_s": 300}
    values.update(overrides)
    project = await project_service.create_project(session, **values)
    await session.commit()
    return project.id


# ---------------------------------------------------------------------------
# Creation and validation
# ---------------------------------------------------------------------------


async def test_creating_a_project_seeds_every_pipeline_stage(session: AsyncSession) -> None:
    project_id = await make_project(session)
    checkpoints = await project_service.get_checkpoints(session, project_id)

    assert [c.stage for c in checkpoints] == [s.value for s in project_service.PIPELINE_ORDER]
    assert {c.status for c in checkpoints} == {StageStatus.PENDING}


async def test_a_new_project_is_a_draft_with_nothing_generated(session: AsyncSession) -> None:
    project_id = await make_project(session)
    project = await project_service.get_project(session, project_id)
    progress = await project_service.progress(session, project_id)

    assert project.status == ProjectStatus.DRAFT
    assert progress.generated_motion_s == 0.0
    assert progress.assembled_s == 0.0
    assert progress.measured_audio_s == 0.0


async def test_duplicate_names_are_refused(session: AsyncSession) -> None:
    await make_project(session, name="Folk tale")
    with pytest.raises(ProjectError, match="already exists"):
        await project_service.create_project(session, name="Folk tale")


async def test_a_blank_name_is_refused(session: AsyncSession) -> None:
    with pytest.raises(ProjectError, match="needs a name"):
        await project_service.create_project(session, name="   ")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target_duration_s", 5, "between"),
        ("target_duration_s", 5000, "between"),
        ("aspect_ratio", "21:9", "Aspect ratio"),
        ("resolution", "4k", "Resolution"),
        ("fps", 60, "Frame rate"),
        ("speaking_rate_wpm", 500, "Speaking rate"),
        ("max_generated_motion_s", -5, "negative"),
        ("music_gain_db", 12, "between -60"),
    ],
)
async def test_out_of_range_settings_are_refused(
    session: AsyncSession, field: str, value: object, message: str
) -> None:
    with pytest.raises(ProjectError, match=message):
        await project_service.create_project(session, name="Bad", **{field: value})


async def test_a_music_path_that_does_not_exist_is_refused(session: AsyncSession) -> None:
    project_id = await make_project(session)
    with pytest.raises(ProjectError, match="No music file"):
        await project_service.update_project(
            session, project_id, {"music_path": "D:/nowhere/absent.mp3"}
        )


async def test_a_real_music_path_is_accepted_and_resolved(
    session: AsyncSession, tmp_path
) -> None:
    track = tmp_path / "bed.mp3"
    track.write_bytes(b"not really audio, but it exists")
    project_id = await make_project(session)

    project = await project_service.update_project(
        session, project_id, {"music_path": str(track)}
    )

    assert project.music_path == str(track.resolve())


async def test_an_unknown_image_folder_is_refused(session: AsyncSession) -> None:
    with pytest.raises(ProjectError, match="image folder no longer exists"):
        await project_service.create_project(session, name="Orphan", image_folder_id=4242)


async def test_a_real_image_folder_is_accepted(session: AsyncSession) -> None:
    folder = ImageFolder(path="D:/stills")
    session.add(folder)
    await session.flush()

    project = await project_service.create_project(
        session, name="With folder", image_folder_id=folder.id
    )

    assert project.image_folder_id == folder.id


async def test_renaming_to_its_own_name_is_allowed(session: AsyncSession) -> None:
    project_id = await make_project(session, name="Keeper")
    project = await project_service.update_project(
        session, project_id, {"name": "Keeper", "niche": "folklore"}
    )
    assert project.niche == "folklore"


# ---------------------------------------------------------------------------
# Format maths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resolution", "ratio", "expected"),
    [
        ("720p", "16:9", (1280, 720)),
        ("1080p", "16:9", (1920, 1080)),
        ("720p", "9:16", (406, 720)),
        ("720p", "1:1", (720, 720)),
        ("480p", "16:9", (854, 480)),
    ],
)
def test_frame_size_is_always_even(resolution, ratio, expected) -> None:
    width, height = project_service.frame_size(resolution, ratio)
    assert (width, height) == expected
    # H.264 with yuv420p cannot encode odd dimensions.
    assert width % 2 == 0 and height % 2 == 0


def test_an_unknown_resolution_raises() -> None:
    with pytest.raises(ProjectError, match="Unknown resolution"):
        project_service.frame_size("8k", "16:9")


def test_speech_estimate_is_zero_without_words() -> None:
    assert project_service.estimate_speech_seconds(0, 150) == 0.0


def test_speech_estimate_uses_the_projects_rate() -> None:
    # 300 words at 150 wpm is two minutes.
    assert project_service.estimate_speech_seconds(300, 150) == 120.0


# ---------------------------------------------------------------------------
# Stage checkpoints
# ---------------------------------------------------------------------------


async def test_next_stage_starts_at_script(session: AsyncSession) -> None:
    project_id = await make_project(session)
    assert await project_service.next_stage(session, project_id) == PipelineStage.SCRIPT


async def test_a_completed_stage_advances_the_cursor(session: AsyncSession) -> None:
    project_id = await make_project(session)
    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.COMPLETED
    )
    assert await project_service.next_stage(session, project_id) == PipelineStage.CAST


async def test_a_partial_stage_does_not_advance_the_cursor(session: AsyncSession) -> None:
    """Partial means work remains, so a resume must come back to it."""
    project_id = await make_project(session)
    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.COMPLETED
    )
    await project_service.mark_stage(
        session, project_id, PipelineStage.CAST, StageStatus.COMPLETED
    )
    await project_service.mark_stage(
        session, project_id, PipelineStage.SPEECH, StageStatus.PARTIAL
    )

    assert await project_service.next_stage(session, project_id) == PipelineStage.SPEECH


async def test_a_skipped_stage_does_advance_the_cursor(session: AsyncSession) -> None:
    project_id = await make_project(session)
    for stage in (PipelineStage.SCRIPT, PipelineStage.CAST, PipelineStage.SPEECH):
        await project_service.mark_stage(session, project_id, stage, StageStatus.COMPLETED)
    await project_service.mark_stage(
        session, project_id, PipelineStage.ROUTING, StageStatus.SKIPPED
    )

    assert await project_service.next_stage(session, project_id) == PipelineStage.MOTION


async def test_stage_payload_is_merged_not_replaced(session: AsyncSession) -> None:
    """A stage accumulates what it finished across several partial runs."""
    project_id = await make_project(session)
    await project_service.mark_stage(
        session, project_id, PipelineStage.MOTION, StageStatus.PARTIAL, payload={"done": [1, 2]}
    )
    await project_service.mark_stage(
        session,
        project_id,
        PipelineStage.MOTION,
        StageStatus.PARTIAL,
        payload={"failed": [3]},
    )

    row = await project_service.get_checkpoint(session, project_id, PipelineStage.MOTION)
    assert row.payload == {"done": [1, 2], "failed": [3]}


async def test_running_a_stage_counts_an_attempt(session: AsyncSession) -> None:
    project_id = await make_project(session)
    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.RUNNING
    )
    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.FAILED, message="boom"
    )
    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.RUNNING
    )

    row = await project_service.get_checkpoint(session, project_id, PipelineStage.SCRIPT)
    assert row.attempt == 2


async def test_a_blocked_reason_is_cleared_once_the_stage_moves_on(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    await project_service.mark_stage(
        session,
        project_id,
        PipelineStage.SCRIPT,
        StageStatus.BLOCKED,
        blocked_reason="Add a Gemini key.",
    )
    row = await project_service.get_checkpoint(session, project_id, PipelineStage.SCRIPT)
    assert row.blocked_reason == "Add a Gemini key."

    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.COMPLETED
    )
    row = await project_service.get_checkpoint(session, project_id, PipelineStage.SCRIPT)
    assert row.blocked_reason is None


async def test_ensure_checkpoints_is_idempotent(session: AsyncSession) -> None:
    project_id = await make_project(session)
    created = await project_service.ensure_checkpoints(session, project_id)
    assert created == 0
    assert len(await project_service.get_checkpoints(session, project_id)) == len(
        project_service.PIPELINE_ORDER
    )


async def test_status_follows_the_checkpoints(session: AsyncSession) -> None:
    project_id = await make_project(session)

    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.COMPLETED
    )
    assert await project_service.refresh_status(session, project_id) == ProjectStatus.SCRIPTED

    await project_service.mark_stage(
        session, project_id, PipelineStage.SPEECH, StageStatus.COMPLETED
    )
    assert await project_service.refresh_status(session, project_id) == ProjectStatus.VOICED

    await project_service.mark_stage(
        session, project_id, PipelineStage.MOTION, StageStatus.PARTIAL
    )
    assert await project_service.refresh_status(session, project_id) == ProjectStatus.ANIMATING


async def test_any_failed_stage_makes_the_project_failed(session: AsyncSession) -> None:
    project_id = await make_project(session)
    await project_service.mark_stage(
        session, project_id, PipelineStage.SCRIPT, StageStatus.COMPLETED
    )
    await project_service.mark_stage(
        session, project_id, PipelineStage.SPEECH, StageStatus.FAILED
    )
    assert await project_service.refresh_status(session, project_id) == ProjectStatus.FAILED


# ---------------------------------------------------------------------------
# Progress accounting: the motion-versus-editing distinction
# ---------------------------------------------------------------------------


async def _scene_with_clips(session: AsyncSession, project_id: int) -> Scene:
    script = Script(project_id=project_id, version=1, title="T")
    session.add(script)
    await session.flush()
    scene = Scene(script_id=script.id, project_id=project_id, order_index=0, scene_key="s01")
    session.add(scene)
    await session.flush()
    return scene


async def test_a_camera_move_is_not_counted_as_generated_motion(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    scene = await _scene_with_clips(session, project_id)

    session.add(
        SceneClip(
            scene_id=scene.id,
            project_id=project_id,
            method=AnimationMethod.CAMERA_MOVE,
            file_path="D:/clips/pan.mp4",
            duration_s=8.0,
            is_generated_motion=False,
            selected=True,
            status=ClipStatus.READY,
        )
    )
    await session.flush()

    progress = await project_service.progress(session, project_id)
    assert progress.assembled_s == 8.0
    assert progress.generated_motion_s == 0.0


async def test_generated_and_edited_clips_are_totalled_separately(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    scene = await _scene_with_clips(session, project_id)

    session.add_all(
        [
            SceneClip(
                scene_id=scene.id,
                project_id=project_id,
                method=AnimationMethod.REMOTE_I2V,
                duration_s=5.0,
                is_generated_motion=True,
                selected=True,
                status=ClipStatus.READY,
            ),
            SceneClip(
                scene_id=scene.id,
                project_id=project_id,
                method=AnimationMethod.CAMERA_MOVE,
                duration_s=7.0,
                is_generated_motion=False,
                selected=True,
                status=ClipStatus.READY,
            ),
        ]
    )
    await session.flush()

    progress = await project_service.progress(session, project_id)
    assert progress.generated_motion_s == 5.0
    assert progress.assembled_s == 12.0


async def test_unselected_and_unfinished_clips_are_not_counted(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    scene = await _scene_with_clips(session, project_id)

    session.add_all(
        [
            # Superseded by a later attempt.
            SceneClip(
                scene_id=scene.id,
                project_id=project_id,
                method=AnimationMethod.REMOTE_I2V,
                duration_s=5.0,
                is_generated_motion=True,
                selected=False,
                status=ClipStatus.READY,
            ),
            # Selected but the file was never produced.
            SceneClip(
                scene_id=scene.id,
                project_id=project_id,
                method=AnimationMethod.API_I2V,
                duration_s=5.0,
                is_generated_motion=True,
                selected=True,
                status=ClipStatus.FAILED,
            ),
        ]
    )
    await session.flush()

    progress = await project_service.progress(session, project_id)
    assert progress.generated_motion_s == 0.0
    assert progress.assembled_s == 0.0


async def test_only_ready_audio_counts_towards_measured_narration(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    scene = await _scene_with_clips(session, project_id)

    session.add_all(
        [
            AudioSegment(
                scene_id=scene.id,
                project_id=project_id,
                order_index=0,
                text="Spoken.",
                duration_s=3.5,
                status=AudioStatus.READY,
            ),
            AudioSegment(
                scene_id=scene.id,
                project_id=project_id,
                order_index=1,
                text="Not yet synthesised.",
                duration_s=None,
                status=AudioStatus.PENDING,
            ),
        ]
    )
    await session.flush()

    progress = await project_service.progress(session, project_id)
    assert progress.measured_audio_s == 3.5
    assert progress.audio_total == 2
    assert progress.audio_ready == 1


async def test_scene_counts_track_ready_scenes(session: AsyncSession) -> None:
    project_id = await make_project(session)
    scene = await _scene_with_clips(session, project_id)
    scene.status = SceneStatus.READY
    await session.flush()

    progress = await project_service.progress(session, project_id)
    assert progress.scenes_total == 1
    assert progress.scenes_ready == 1


async def test_deleting_a_project_removes_its_scenes_and_checkpoints(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    await _scene_with_clips(session, project_id)
    await session.commit()

    await project_service.delete_project(session, project_id)
    await session.commit()

    assert await project_service.progress(session, project_id) == project_service.ProjectProgress()
    with pytest.raises(ProjectError):
        await project_service.get_project(session, project_id)
