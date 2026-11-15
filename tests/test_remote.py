"""Borrowed GPU time: submitting, polling, and refusing to overstate results.

Every HTTP call here is mocked. No test in this file touches Kaggle or a
Hugging Face Space, because doing so would spend a free weekly allowance
to assert something a mock can assert for nothing, and because neither
backend can be reached without the user's own credentials.

The tests that matter most are the accounting ones. A run that exits
cleanly having produced nothing must be recorded as a failure; a file
that cannot be measured must not become a clip; and a batch must never be
sent twice, because a duplicate submission spends the allowance twice for
a single result.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config
from app.constants import (
    AnimationMethod,
    ErrorClass,
    PipelineStage,
    RemotePurpose,
    RemoteTaskStatus,
    StageStatus,
)
from app.models import Image, ImageFolder, RemoteTask, Scene, SceneClip, StageCheckpoint
from app.providers import compute
from app.providers.compute import hf_space, kaggle
from app.providers.compute.base import ComputeError, ComputeJobSpec, OutputFile
from app.services import notebooks
from app.services import projects as project_service
from app.services import remote as remote_service
from app.services import scripts as script_service
from app.services.ffmpeg import FFmpegStatus
from tests.conftest import write_image

pytestmark = pytest.mark.usefixtures("data_root")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def ffmpeg_present(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture(autouse=True)
def fresh_registry() -> AsyncIterator[None]:
    """The provider registry caches instances; tests must not share them."""
    compute.reset_cache()
    yield
    compute.reset_cache()


@pytest.fixture
def kaggle_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVM_KAGGLE_USERNAME", "tester")
    monkeypatch.setenv("AVM_KAGGLE_KEY", "secret-key-value")
    config.get_settings.cache_clear()
    compute.reset_cache()


@pytest.fixture
def space_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVM_KAGGLE_USERNAME", "")
    monkeypatch.setenv("AVM_KAGGLE_KEY", "")
    monkeypatch.setenv("AVM_HF_SPACE_I2V", "someone/animator")
    config.get_settings.cache_clear()
    compute.reset_cache()


def scene_dict(key: str, **extra: Any) -> dict[str, Any]:
    base = {
        "scene_key": key,
        "purpose": f"Beat {key}",
        "target_duration_s": 10.0,
        "narration": f"Something quite specific happens during beat {key}.",
        "dialogue": [],
        "characters": [],
        "preferred_method": "remote_i2v",
    }
    base.update(extra)
    return base


async def add_image(session: AsyncSession, tmp_path: Path, name: str) -> Image:
    folder = (
        await session.execute(
            select(ImageFolder).where(ImageFolder.path == str(tmp_path))
        )
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
        width=1024,
        height=768,
    )
    session.add(image)
    await session.flush()
    return image


async def make_project(
    session: AsyncSession,
    tmp_path: Path,
    count: int = 2,
    *,
    method: str = AnimationMethod.REMOTE_I2V,
    with_images: bool = True,
    **project_values: Any,
) -> int:
    """A project whose first ``count`` scenes are routed to ``method``.

    The script validator insists on at least two scenes, so a test that
    wants a single remote shot still gets a two-scene script; the extra
    scene is routed to a camera move and stays out of the batch.
    """
    scenes = [scene_dict(f"s{i}") for i in range(max(2, count))]
    planned = sum(float(s["target_duration_s"]) for s in scenes)
    values = {"name": "Remote test", "target_duration_s": max(15, round(planned))}
    values.update(project_values)
    project = await project_service.create_project(session, **values)
    script = await script_service.accept_paste(
        session,
        project.id,
        json.dumps({"title": "T", "logline": "L", "synopsis": "S", "scenes": scenes}),
    )
    await script_service.approve(session, script.id)

    rows = (
        await session.execute(
            select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index)
        )
    ).scalars().all()
    for index, scene in enumerate(rows):
        scene.assigned_method = (
            method if index < count else AnimationMethod.CAMERA_MOVE
        )
        if with_images:
            image = await add_image(session, tmp_path, f"still-{index}.jpg")
            scene.image_id = image.id
    await session.flush()
    await session.commit()
    return project.id


class FakeProvider:
    """A compute backend that does exactly what a test tells it to."""

    key = "kaggle"

    def __init__(self) -> None:
        self.credentials = {"username": "tester", "key": "k"}
        self.submitted: list[ComputeJobSpec] = []
        self.ref = "tester/avm-run"
        self.submit_error: ComputeError | None = None
        self.poll_error: ComputeError | None = None
        self.state = RemoteTaskStatus.COMPLETED
        self.outputs: list[OutputFile] = []
        self.payloads: dict[str, bytes] = {}
        self.cancelled: list[str] = []

    def capabilities(self) -> Any:
        from app.providers.compute.base import ComputeCapabilities

        return ComputeCapabilities(
            key="kaggle",
            display_name="Kaggle",
            purposes=(RemotePurpose.IMAGE_TO_VIDEO, RemotePurpose.LIPSYNC),
            required_credentials=("username", "key"),
            endpoint_verified=True,
        )

    @property
    def is_configured(self) -> bool:
        return True

    async def submit(self, spec: ComputeJobSpec) -> Any:
        from app.providers.compute.base import SubmitOutcome

        if self.submit_error:
            raise self.submit_error
        self.submitted.append(spec)
        return SubmitOutcome(ref=self.ref, url="https://kaggle.test/run", version=1)

    async def poll(self, ref: str) -> Any:
        from app.providers.compute.base import ComputeStatus

        if self.poll_error:
            raise self.poll_error
        return ComputeStatus(state=self.state, message=f"state {self.state}")

    async def list_outputs(self, ref: str) -> list[OutputFile]:
        return list(self.outputs)

    async def download(self, file: OutputFile, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self.payloads.get(file.name, b"\0" * 2048)
        destination.write_bytes(data)
        return len(data)

    async def quota(self) -> Any:
        from app.providers.compute.base import QuotaReport

        return QuotaReport(known=False, note="Not asked in this test.")

    async def cancel(self, ref: str) -> bool:
        self.cancelled.append(ref)
        return True

    async def aclose(self) -> None:
        return None


def good_manifest(keys: list[str], *, model_ran: bool = True) -> bytes:
    return json.dumps(
        {
            "purpose": RemotePurpose.IMAGE_TO_VIDEO,
            "model_ran": model_ran,
            "problems": [],
            "shots": [
                {
                    "scene_key": key,
                    "file": f"{key}.mp4",
                    "frames": 96,
                    "fps": 24,
                    "requested_duration_s": 4.0,
                    "generated_motion": True,
                }
                for key in keys
            ],
        }
    ).encode("utf-8")


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr(
        remote_service.compute, "preferred", lambda purpose: provider
    )
    monkeypatch.setattr(
        remote_service.compute, "get", lambda key, purpose=None: provider
    )
    return provider


# ---------------------------------------------------------------------------
# Batch identity: the rule that stops the allowance being spent twice
# ---------------------------------------------------------------------------


def test_the_same_batch_always_gets_the_same_token() -> None:
    first = remote_service.batch_token(1, RemotePurpose.IMAGE_TO_VIDEO, ["a", "b"])
    second = remote_service.batch_token(1, RemotePurpose.IMAGE_TO_VIDEO, ["b", "a"])
    assert first == second


def test_a_different_batch_gets_a_different_token() -> None:
    first = remote_service.batch_token(1, RemotePurpose.IMAGE_TO_VIDEO, ["a", "b"])
    assert first != remote_service.batch_token(
        1, RemotePurpose.IMAGE_TO_VIDEO, ["a", "c"]
    )
    assert first != remote_service.batch_token(2, RemotePurpose.IMAGE_TO_VIDEO, ["a", "b"])
    assert first != remote_service.batch_token(1, RemotePurpose.LIPSYNC, ["a", "b"])


def test_the_token_carries_no_clock_so_a_retry_lands_on_the_same_slot() -> None:
    args = (3, RemotePurpose.IMAGE_TO_VIDEO, ["x"])
    assert remote_service.batch_token(*args) == remote_service.batch_token(*args)


# ---------------------------------------------------------------------------
# Planning a batch
# ---------------------------------------------------------------------------


async def test_planning_without_a_backend_is_blocked_not_guessed(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(session, tmp_path)
    plan = await remote_service.plan_batch(session, project_id)
    assert not plan.ready
    assert "No borrowed GPU backend is configured" in plan.blocked_reason


async def test_planning_collects_the_scenes_routed_to_the_remote_method(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=3)
    plan = await remote_service.plan_batch(session, project_id)
    assert plan.ready
    assert [shot["scene_key"] for shot in plan.shots] == ["scene000", "scene001", "scene002"]
    assert len(plan.inputs) == 3


async def test_a_scene_routed_to_a_camera_move_is_left_alone(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(
        session, tmp_path, count=2, method=AnimationMethod.CAMERA_MOVE
    )
    plan = await remote_service.plan_batch(session, project_id)
    assert plan.shots == []
    assert "No scene is routed to borrowed GPU time" in plan.blocked_reason


async def test_a_scene_with_no_still_is_reported_not_skipped_silently(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=2, with_images=False)
    plan = await remote_service.plan_batch(session, project_id)
    assert plan.shots == []
    assert any("has no still to animate" in item for item in plan.skipped)


async def test_a_still_missing_from_disk_is_reported(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    scene = (await session.execute(select(Scene))).scalars().first()
    image = await session.get(Image, scene.image_id)
    Path(image.path).unlink()
    plan = await remote_service.plan_batch(session, project_id)
    assert any("missing from disk" in item for item in plan.skipped)


async def test_a_long_script_is_split_rather_than_risked_in_one_session(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(
        session, tmp_path, count=remote_service.MAX_SHOTS_PER_BATCH + 2
    )
    plan = await remote_service.plan_batch(session, project_id)
    assert len(plan.shots) == remote_service.MAX_SHOTS_PER_BATCH
    assert any("held back for the next run" in item for item in plan.skipped)


async def test_an_unmeasured_scene_says_so_rather_than_claiming_a_real_length(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    plan = await remote_service.plan_batch(session, project_id)
    assert any("has not been measured" in note for note in plan.notes)


async def test_a_measured_scene_sends_the_measured_length(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    scene = (await session.execute(select(Scene))).scalars().first()
    scene.measured_audio_s = 6.0
    await session.commit()
    plan = await remote_service.plan_batch(session, project_id)
    assert plan.shots[0]["duration_s"] == pytest.approx(6.6, abs=0.01)
    assert plan.notes == []


async def test_planning_an_unsupported_purpose_is_refused(
    session: AsyncSession, tmp_path: Path
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    with pytest.raises(remote_service.RemoteError):
        await remote_service.plan_batch(session, project_id, "assemble_the_whole_film")


async def test_planning_without_an_approved_script_is_blocked(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project = await project_service.create_project(session, name="Empty")
    await session.commit()
    plan = await remote_service.plan_batch(session, project.id)
    assert "No approved script" in plan.blocked_reason


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


async def test_submitting_records_the_task_and_what_went_with_it(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=2)
    task = await remote_service.submit_batch(session, project_id)
    assert task.status == RemoteTaskStatus.QUEUED
    assert task.ref == "tester/avm-run"
    assert len(task.payload["shots"]) == 2
    assert task.attempts == 1
    assert task.submitted_at is not None


async def test_the_notebook_sent_carries_the_images_inside_it(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    await remote_service.submit_batch(session, project_id)
    spec = fake_provider.submitted[0]
    assert "scene000" in spec.notebook
    # The inputs ride along base64-encoded, so a run needs no second upload.
    assert "b64" in spec.notebook


async def test_the_notebook_is_kept_on_disk_for_inspection(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    project = await project_service.get_project(session, project_id)
    saved = remote_service.remote_dir(project) / f"{task.client_token}.ipynb"
    assert saved.exists()
    assert json.loads(saved.read_text(encoding="utf-8"))["nbformat"] == 4


async def test_sending_the_same_batch_twice_is_refused(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    await remote_service.submit_batch(session, project_id)
    with pytest.raises(remote_service.RemoteError) as excinfo:
        await remote_service.submit_batch(session, project_id)
    assert "already out at a run" in str(excinfo.value)


async def test_a_scene_already_out_is_not_included_in_the_next_batch(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=2)
    await remote_service.submit_batch(session, project_id)
    plan = await remote_service.plan_batch(session, project_id)
    assert plan.shots == []
    assert all("already out at a remote run" in item for item in plan.skipped)


async def test_a_lost_submit_response_parks_the_task_instead_of_resending(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    fake_provider.submit_error = ComputeError("connection dropped", ErrorClass.UNCERTAIN)
    task = await remote_service.submit_batch(session, project_id)
    assert task.status == RemoteTaskStatus.UNCERTAIN
    assert task.finished_at is None
    assert fake_provider.submitted == []


async def test_a_clearly_rejected_submit_is_recorded_as_failed(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    fake_provider.submit_error = ComputeError("bad credentials", ErrorClass.AUTH)
    task = await remote_service.submit_batch(session, project_id)
    assert task.status == RemoteTaskStatus.FAILED
    assert task.error_class == ErrorClass.AUTH
    assert task.finished_at is not None


async def test_submitting_moves_the_stage_to_running(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    await remote_service.submit_batch(session, project_id)
    checkpoint = (
        await session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.project_id == project_id,
                StageCheckpoint.stage == PipelineStage.MOTION,
            )
        )
    ).scalar_one()
    assert checkpoint.status == StageStatus.RUNNING


async def test_submitting_with_nothing_to_send_raises_rather_than_sending_empty(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(
        session, tmp_path, count=1, method=AnimationMethod.CAMERA_MOVE
    )
    with pytest.raises(remote_service.RemoteError):
        await remote_service.submit_batch(session, project_id)


# ---------------------------------------------------------------------------
# Polling and collecting: where honesty is enforced
# ---------------------------------------------------------------------------


async def test_a_still_running_task_reports_its_state_and_makes_no_clips(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.state = RemoteTaskStatus.RUNNING
    result = await remote_service.poll_task(session, task.id)
    assert result.status == RemoteTaskStatus.RUNNING
    assert result.clips_made == 0
    assert (await session.execute(select(SceneClip))).scalars().all() == []


async def test_a_finished_run_becomes_measured_clips(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [
        OutputFile(name="manifest.json", url="https://x/manifest.json"),
        OutputFile(name="scene000.mp4", url="https://x/scene000.mp4"),
    ]
    fake_provider.payloads = {"manifest.json": good_manifest(["scene000"])}

    result = await remote_service.poll_task(session, task.id)
    assert result.clips_made == 1
    # 4.0s is what the stubbed ffprobe measures, which is the only figure
    # allowed to reach the edit.
    assert result.generated_motion_s == pytest.approx(4.0)

    clip = (await session.execute(select(SceneClip))).scalars().one()
    assert clip.duration_s == pytest.approx(4.0)
    assert clip.is_generated_motion is True
    assert clip.provider_key == "kaggle"
    assert clip.method == AnimationMethod.REMOTE_I2V


async def test_a_clip_records_both_what_was_asked_for_and_what_arrived(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [
        OutputFile(name="manifest.json", url="https://x/manifest.json"),
        OutputFile(name="scene000.mp4", url="https://x/scene000.mp4"),
    ]
    fake_provider.payloads = {"manifest.json": good_manifest(["scene000"])}
    await remote_service.poll_task(session, task.id)

    clip = (await session.execute(select(SceneClip))).scalars().one()
    assert clip.validation["measured_duration_s"] == pytest.approx(4.0)
    # The scene's narration was never measured, so what went out was the
    # script's own estimate, unpadded.
    assert clip.validation["requested_duration_s"] == pytest.approx(10.0)
    assert clip.validation["model_ran"] is True


async def test_a_run_that_produced_nothing_is_a_failure_not_a_success(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [OutputFile(name="manifest.json", url="https://x/m.json")]
    fake_provider.payloads = {
        "manifest.json": json.dumps(
            {
                "model_ran": False,
                "problems": ["CUDA ran out of memory."],
                "shots": [
                    {"scene_key": "scene000", "file": None, "error": "OutOfMemoryError"}
                ],
            }
        ).encode("utf-8")
    }

    result = await remote_service.poll_task(session, task.id)
    assert result.clips_made == 0

    refreshed = await session.get(RemoteTask, task.id)
    assert refreshed.status == RemoteTaskStatus.FAILED
    assert "no usable clip" in (refreshed.message or "")


async def test_a_run_with_no_manifest_admits_it_does_not_know_what_happened(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [OutputFile(name="scene000.mp4", url="https://x/a.mp4")]

    result = await remote_service.poll_task(session, task.id)
    assert result.clips_made == 0
    assert any("no manifest" in problem for problem in result.problems)


async def test_a_file_named_in_the_manifest_but_never_returned_is_reported(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [OutputFile(name="manifest.json", url="https://x/m.json")]
    fake_provider.payloads = {"manifest.json": good_manifest(["scene000"])}

    result = await remote_service.poll_task(session, task.id)
    assert result.clips_made == 0
    assert any("did not return" in problem for problem in result.problems)


async def test_an_unmeasurable_file_does_not_become_a_clip(
    session: AsyncSession,
    tmp_path: Path,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.ffmpeg import MediaInfo

    async def broken_probe(path: Path, ffprobe_path: str | None = None) -> MediaInfo:
        return MediaInfo(error="moov atom not found")

    monkeypatch.setattr("app.services.ffmpeg.probe", broken_probe)

    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [
        OutputFile(name="manifest.json", url="https://x/m.json"),
        OutputFile(name="scene000.mp4", url="https://x/a.mp4"),
    ]
    fake_provider.payloads = {"manifest.json": good_manifest(["scene000"])}

    result = await remote_service.poll_task(session, task.id)
    assert result.clips_made == 0
    assert any("moov atom" in problem for problem in result.problems)
    assert (await session.execute(select(SceneClip))).scalars().all() == []


async def test_a_clip_is_not_called_generated_motion_when_no_model_ran(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    """The manifest's per-shot flag is not enough on its own.

    A notebook that assembled frames without the model loading could set
    ``generated_motion`` while ``model_ran`` stayed false. The run-level
    flag wins, because that is the one the model itself sets.
    """
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [
        OutputFile(name="manifest.json", url="https://x/m.json"),
        OutputFile(name="scene000.mp4", url="https://x/a.mp4"),
    ]
    fake_provider.payloads = {
        "manifest.json": good_manifest(["scene000"], model_ran=False)
    }

    result = await remote_service.poll_task(session, task.id)
    assert result.generated_motion_s == 0.0
    clip = (await session.execute(select(SceneClip))).scalars().one()
    assert clip.is_generated_motion is False


async def test_an_unexpected_scene_in_the_manifest_is_reported(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [OutputFile(name="manifest.json", url="https://x/m.json")]
    fake_provider.payloads = {"manifest.json": good_manifest(["scene999"])}

    result = await remote_service.poll_task(session, task.id)
    assert any("not in this batch" in problem for problem in result.problems)


async def test_the_run_time_is_measured_not_estimated(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.outputs = [
        OutputFile(name="manifest.json", url="https://x/m.json"),
        OutputFile(name="scene000.mp4", url="https://x/a.mp4"),
    ]
    fake_provider.payloads = {"manifest.json": good_manifest(["scene000"])}
    await remote_service.poll_task(session, task.id)

    refreshed = await session.get(RemoteTask, task.id)
    assert refreshed.gpu_seconds is not None
    assert refreshed.gpu_seconds >= 0


async def test_a_failed_poll_leaves_the_task_alone(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    """A network problem says nothing about the run, so nothing changes."""
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    fake_provider.poll_error = ComputeError("DNS failed", ErrorClass.TRANSIENT)

    result = await remote_service.poll_task(session, task.id)
    assert result.status == RemoteTaskStatus.QUEUED
    refreshed = await session.get(RemoteTask, task.id)
    assert refreshed.status == RemoteTaskStatus.QUEUED
    assert refreshed.last_error == "DNS failed"


async def test_polling_a_finished_task_does_nothing(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    task.status = RemoteTaskStatus.COMPLETED
    await session.commit()
    result = await remote_service.poll_task(session, task.id)
    assert "already finished" in result.problems[0]


async def test_polling_a_task_that_was_never_accepted_says_so(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    fake_provider.submit_error = ComputeError("lost", ErrorClass.UNCERTAIN)
    task = await remote_service.submit_batch(session, project_id)
    result = await remote_service.poll_task(session, task.id)
    assert any("never accepted" in problem for problem in result.problems)


async def test_polling_an_unknown_task_raises(session: AsyncSession) -> None:
    with pytest.raises(remote_service.RemoteError):
        await remote_service.poll_task(session, 9999)


# ---------------------------------------------------------------------------
# Reconciling and cancelling
# ---------------------------------------------------------------------------


async def test_reconcile_polls_outstanding_work_without_resubmitting(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    await remote_service.submit_batch(session, project_id)
    fake_provider.state = RemoteTaskStatus.RUNNING
    sent_before = len(fake_provider.submitted)

    results = await remote_service.reconcile(session)
    assert len(results) == 1
    assert results[0].status == RemoteTaskStatus.RUNNING
    assert len(fake_provider.submitted) == sent_before


async def test_reconcile_writes_off_a_task_that_never_reached_the_backend(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    fake_provider.submit_error = ComputeError("lost", ErrorClass.UNCERTAIN)
    task = await remote_service.submit_batch(session, project_id)

    results = await remote_service.reconcile(session)
    refreshed = await session.get(RemoteTask, task.id)
    assert refreshed.status == RemoteTaskStatus.FAILED
    assert "safe to send again" in (refreshed.message or "")
    assert results[0].status == RemoteTaskStatus.FAILED


async def test_reconcile_with_nothing_outstanding_is_quiet(
    session: AsyncSession,
) -> None:
    assert await remote_service.reconcile(session) == []


async def test_cancelling_tells_the_backend_and_records_it(
    session: AsyncSession, tmp_path: Path, fake_provider: FakeProvider
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    cancelled = await remote_service.cancel_task(session, task.id)
    assert cancelled.status == RemoteTaskStatus.CANCELLED
    assert fake_provider.cancelled == ["tester/avm-run"]


async def test_cancelling_an_unknown_task_raises(session: AsyncSession) -> None:
    with pytest.raises(remote_service.RemoteError):
        await remote_service.cancel_task(session, 4242)


# ---------------------------------------------------------------------------
# The notebook templates
# ---------------------------------------------------------------------------


def test_the_image_to_video_notebook_is_valid_nbformat() -> None:
    document = notebooks.render(
        notebooks.NotebookSpec(
            purpose=RemotePurpose.IMAGE_TO_VIDEO,
            inputs_json=base64.b64encode(b"[]").decode(),
            shots=[{"scene_key": "a", "image": "a.jpg", "duration_s": 4.0}],
        )
    )
    parsed = json.loads(document)
    assert parsed["nbformat"] == 4
    assert len(parsed["cells"]) == 4
    assert all(cell["cell_type"] == "code" for cell in parsed["cells"])


def test_the_lipsync_notebook_is_valid_nbformat() -> None:
    document = notebooks.render(
        notebooks.NotebookSpec(
            purpose=RemotePurpose.LIPSYNC,
            inputs_json=base64.b64encode(b"[]").decode(),
            shots=[{"scene_key": "a", "video": "a.mp4", "audio": "a.wav"}],
        )
    )
    assert json.loads(document)["nbformat"] == 4


def test_an_unknown_purpose_has_no_notebook() -> None:
    with pytest.raises(ValueError):
        notebooks.render(
            notebooks.NotebookSpec(purpose="colourise", inputs_json="", shots=[])
        )


def test_the_notebook_prefers_cached_weights_over_downloading_them() -> None:
    document = notebooks.render(
        notebooks.NotebookSpec(
            purpose=RemotePurpose.IMAGE_TO_VIDEO,
            inputs_json=base64.b64encode(b"[]").decode(),
            shots=[],
            weights_dir="/kaggle/input/avm-model-weights",
        )
    )
    assert "/kaggle/input/avm-model-weights" in document
    assert "dataset:" in document


def test_the_notebook_never_substitutes_a_still_for_a_failed_clip() -> None:
    document = notebooks.render(
        notebooks.NotebookSpec(
            purpose=RemotePurpose.IMAGE_TO_VIDEO,
            inputs_json=base64.b64encode(b"[]").decode(),
            shots=[],
        )
    )
    source = "".join(
        "".join(cell["source"]) for cell in json.loads(document)["cells"]
    )
    assert '"generated_motion": False' in source
    assert "No substitute is written" in source


def test_the_notebook_reports_whether_it_got_a_gpu() -> None:
    document = notebooks.render(
        notebooks.NotebookSpec(
            purpose=RemotePurpose.IMAGE_TO_VIDEO,
            inputs_json=base64.b64encode(b"[]").decode(),
            shots=[],
        )
    )
    assert "cuda.is_available" in document
    assert "No GPU was attached" in document


def test_an_unreadable_manifest_is_not_taken_as_success() -> None:
    parsed = notebooks.parse_manifest("{not json at all")
    assert parsed["readable"] is False
    assert parsed["model_ran"] is False
    assert parsed["shots"] == []
    assert parsed["problems"]


def test_a_manifest_that_is_not_an_object_is_rejected() -> None:
    parsed = notebooks.parse_manifest("[1, 2, 3]")
    assert parsed["readable"] is False


def test_a_readable_manifest_keeps_its_fields() -> None:
    parsed = notebooks.parse_manifest(
        json.dumps({"model_ran": True, "shots": [{"scene_key": "a"}]})
    )
    assert parsed["readable"] is True
    assert parsed["model_ran"] is True
    assert parsed["shots"][0]["scene_key"] == "a"


# ---------------------------------------------------------------------------
# The Kaggle adapter, against mocked HTTP
# ---------------------------------------------------------------------------


def test_slugify_makes_a_name_kaggle_will_accept() -> None:
    assert kaggle.slugify("AVM My Film! (take 2)") == "avm-my-film-take-2"
    assert kaggle.slugify("") == "avm-task"


def test_a_protobuf_duration_string_is_understood() -> None:
    assert kaggle._duration_seconds("3600s") == pytest.approx(3600.0)
    assert kaggle._duration_seconds(1800) == pytest.approx(1800.0)
    assert kaggle._duration_seconds(None) is None
    assert kaggle._duration_seconds("not a duration") is None
    assert kaggle._duration_seconds({"seconds": 108000, "nanos": 0}) == pytest.approx(108000.0)


def test_the_push_payload_uses_the_field_names_kaggle_expects() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    payload = provider.build_payload(
        ComputeJobSpec(
            purpose=RemotePurpose.IMAGE_TO_VIDEO,
            title="AVM Test",
            notebook='{"cells": []}',
            datasets=["tester/weights"],
            client_token="tok",
        ),
        "tester",
    )
    # camelCase, not snake_case: the API rejects the latter silently.
    assert payload["slug"] == "tester/avm-test"
    assert payload["kernelType"] == "notebook"
    assert payload["enableGpu"] is True
    assert payload["machineShape"] == kaggle.DEFAULT_ACCELERATOR
    assert payload["datasetDataSources"] == ["tester/weights"]
    assert payload["isPrivate"] is True


def test_an_unconfigured_kaggle_adapter_reports_what_is_missing() -> None:
    provider = kaggle.KaggleCompute({})
    assert provider.is_configured is False


async def test_a_kaggle_push_returns_the_slug_it_was_given() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.post(kaggle.PUSH_PATH).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ref": "tester/avm-test",
                    "versionNumber": 3,
                    "url": "https://www.kaggle.com/code/tester/avm-test",
                },
            )
        )
        outcome = await provider.submit(
            ComputeJobSpec(
                purpose=RemotePurpose.IMAGE_TO_VIDEO,
                title="AVM Test",
                notebook='{"cells": []}',
                client_token="tok",
            )
        )
    assert outcome.ref == "tester/avm-test"
    assert outcome.version == 3
    await provider.aclose()


async def test_bad_kaggle_credentials_are_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        route = mock.post(kaggle.PUSH_PATH).mock(return_value=httpx.Response(401))
        with pytest.raises(ComputeError) as excinfo:
            await provider.submit(
                ComputeJobSpec(
                    purpose=RemotePurpose.IMAGE_TO_VIDEO,
                    title="T",
                    notebook="{}",
                )
            )
    assert excinfo.value.error_class == ErrorClass.AUTH
    assert route.call_count == 1
    await provider.aclose()


async def test_a_kaggle_error_message_never_contains_the_key() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "super-secret-key"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.post(kaggle.PUSH_PATH).mock(
            return_value=httpx.Response(500, text="failed for super-secret-key")
        )
        with pytest.raises(ComputeError) as excinfo:
            await provider.submit(
                ComputeJobSpec(
                    purpose=RemotePurpose.IMAGE_TO_VIDEO, title="T", notebook="{}"
                )
            )
    assert "super-secret-key" not in str(excinfo.value)
    await provider.aclose()


@pytest.mark.parametrize(
    ("kaggle_state", "expected"),
    [
        ("QUEUED", RemoteTaskStatus.QUEUED),
        ("RUNNING", RemoteTaskStatus.RUNNING),
        ("COMPLETE", RemoteTaskStatus.COMPLETED),
        ("ERROR", RemoteTaskStatus.FAILED),
        ("CANCEL_ACKNOWLEDGED", RemoteTaskStatus.CANCELLED),
    ],
)
async def test_kaggle_statuses_map_to_our_own(
    kaggle_state: str, expected: str
) -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.STATUS_PATH).mock(
            return_value=httpx.Response(200, json={"status": kaggle_state})
        )
        status = await provider.poll("tester/avm-test")
    assert status.state == expected
    await provider.aclose()


async def test_an_unknown_kaggle_status_is_not_guessed() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.STATUS_PATH).mock(
            return_value=httpx.Response(200, json={"status": "SOMETHING_NEW"})
        )
        status = await provider.poll("tester/avm-test")
    assert status.state == RemoteTaskStatus.UNCERTAIN
    await provider.aclose()


async def test_the_kaggle_quota_is_read_live_not_remembered() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.QUOTA_PATH).mock(
            return_value=httpx.Response(
                200,
                json={
                    "gpuQuota": {
                        "timeUsed": "3600s",
                        "timeReserved": "0s",
                        "totalTimeAllowed": "108000s",
                    }
                },
            )
        )
        report = await provider.quota()
    assert report.known is True
    assert report.remaining_s == pytest.approx(104400.0)
    await provider.aclose()


async def test_an_unreadable_quota_is_unknown_not_zero() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.QUOTA_PATH).mock(return_value=httpx.Response(500))
        report = await provider.quota()
    assert report.known is False
    assert report.remaining_s is None
    assert "unknown" in report.note.lower()
    await provider.aclose()


async def test_a_quota_reply_without_a_gpu_section_is_unknown() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.QUOTA_PATH).mock(return_value=httpx.Response(200, json={}))
        report = await provider.quota()
    assert report.known is False
    await provider.aclose()


async def test_kaggle_outputs_are_listed_from_the_api() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.OUTPUT_PATH).mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {"fileName": "manifest.json", "url": "https://x/manifest.json"},
                        {"fileName": "scene000.mp4", "url": "https://x/scene000.mp4"},
                    ]
                },
            )
        )
        files = await provider.list_outputs("tester/avm-test")
    assert [f.name for f in files] == ["manifest.json", "scene000.mp4"]
    await provider.aclose()


async def test_kaggle_rate_limiting_is_classified_as_such() -> None:
    provider = kaggle.KaggleCompute({"username": "tester", "key": "k"})
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.STATUS_PATH).mock(
            return_value=httpx.Response(429, headers={"retry-after": "30"})
        )
        with pytest.raises(ComputeError) as excinfo:
            await provider.poll("tester/avm-test")
    assert excinfo.value.error_class == ErrorClass.RATE_LIMITED
    assert excinfo.value.retry_after_s == pytest.approx(30.0)
    await provider.aclose()


def test_the_kaggle_adapter_does_not_claim_to_be_proven_here() -> None:
    """Until a real credentialed call succeeds, it says so."""
    caps = kaggle.KaggleCompute({"username": "t", "key": "k"}).capabilities()
    assert caps.endpoint_verified is True
    assert caps.live_verified is False


# ---------------------------------------------------------------------------
# The Hugging Face Space fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("someone/animator", "https://someone-animator.hf.space"),
        ("someone-animator", "https://someone-animator.hf.space"),
        ("https://custom.example/app", "https://custom.example/app"),
    ],
)
def test_a_space_name_becomes_a_url(given: str, expected: str) -> None:
    assert hf_space.space_url(given) == expected


def test_an_empty_space_name_is_refused() -> None:
    with pytest.raises(ComputeError):
        hf_space.space_url("")


def test_a_completed_event_stream_is_read() -> None:
    event, payload = hf_space.parse_event_stream(
        'event: complete\ndata: [{"url": "https://x/a.mp4"}]\n\n'
    )
    assert event == "complete"
    assert payload == [{"url": "https://x/a.mp4"}]


def test_an_error_event_stream_is_read() -> None:
    event, payload = hf_space.parse_event_stream('event: error\ndata: "it broke"\n\n')
    assert event == "error"
    assert payload == "it broke"


def test_a_heartbeat_is_not_mistaken_for_completion() -> None:
    event, payload = hf_space.parse_event_stream("event: heartbeat\ndata: null\n\n")
    assert event == "heartbeat"
    assert payload is None


async def test_a_space_submission_returns_an_event_id() -> None:
    provider = hf_space.HuggingFaceSpaceCompute(
        {"space": "someone/animator", "api_name": "predict"}
    )
    with respx.mock as mock:
        mock.post(
            "https://someone-animator.hf.space/gradio_api/call/predict"
        ).mock(return_value=httpx.Response(200, json={"event_id": "abc123"}))
        outcome = await provider.submit(
            ComputeJobSpec(
                purpose=RemotePurpose.IMAGE_TO_VIDEO,
                title="T",
                notebook='["hello"]',
            )
        )
    assert "abc123" in outcome.ref
    await provider.aclose()


async def test_an_older_space_path_is_tried_before_giving_up() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    with respx.mock as mock:
        mock.post(
            "https://someone-animator.hf.space/gradio_api/call/predict"
        ).mock(return_value=httpx.Response(404))
        older = mock.post("https://someone-animator.hf.space/call/predict").mock(
            return_value=httpx.Response(200, json={"event_id": "xyz"})
        )
        outcome = await provider.submit(
            ComputeJobSpec(purpose=RemotePurpose.IMAGE_TO_VIDEO, title="T", notebook="[]")
        )
    assert older.called
    assert "xyz" in outcome.ref
    await provider.aclose()


async def test_a_space_with_no_such_endpoint_says_so() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    with respx.mock as mock:
        mock.post(url__regex=r".*call/predict").mock(return_value=httpx.Response(404))
        with pytest.raises(ComputeError) as excinfo:
            await provider.submit(
                ComputeJobSpec(
                    purpose=RemotePurpose.IMAGE_TO_VIDEO, title="T", notebook="[]"
                )
            )
    assert excinfo.value.error_class == ErrorClass.INVALID_INPUT
    assert "no callable endpoint" in str(excinfo.value)
    await provider.aclose()


async def test_a_sleeping_space_is_reported_as_transient() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    with respx.mock as mock:
        mock.post(url__regex=r".*call/predict").mock(return_value=httpx.Response(503))
        with pytest.raises(ComputeError) as excinfo:
            await provider.submit(
                ComputeJobSpec(
                    purpose=RemotePurpose.IMAGE_TO_VIDEO, title="T", notebook="[]"
                )
            )
    assert excinfo.value.error_class == ErrorClass.TRANSIENT
    await provider.aclose()


async def test_a_space_that_serves_html_instead_of_json_is_reported() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    with respx.mock as mock:
        mock.post(url__regex=r".*gradio_api/call/predict").mock(
            return_value=httpx.Response(200, text="<!DOCTYPE html>")
        )
        with pytest.raises(ComputeError) as excinfo:
            await provider.submit(
                ComputeJobSpec(
                    purpose=RemotePurpose.IMAGE_TO_VIDEO, title="T", notebook="[]"
                )
            )
    assert "not JSON" in str(excinfo.value)
    await provider.aclose()


async def test_a_space_poll_turns_file_results_into_outputs() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    ref = "https://someone-animator.hf.space|/gradio_api/call/predict|abc"
    with respx.mock as mock:
        mock.get(
            "https://someone-animator.hf.space/gradio_api/call/predict/abc"
        ).mock(
            return_value=httpx.Response(
                200,
                text='event: complete\ndata: [{"path": "tmp/out.mp4", "orig_name": "out.mp4"}]\n\n',
            )
        )
        files = await provider.list_outputs(ref)
    assert [f.name for f in files] == ["out.mp4"]
    assert files[0].url.startswith("https://someone-animator.hf.space/gradio_api/file=")
    await provider.aclose()


async def test_a_space_that_is_still_working_is_not_reported_as_finished() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    ref = "https://someone-animator.hf.space|/gradio_api/call/predict|abc"
    with respx.mock as mock:
        mock.get(url__regex=r".*predict/abc").mock(
            return_value=httpx.Response(200, text="event: heartbeat\ndata: null\n\n")
        )
        status = await provider.poll(ref)
    assert status.state == RemoteTaskStatus.RUNNING
    await provider.aclose()


async def test_a_space_read_timeout_means_still_running_not_failed() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    ref = "https://someone-animator.hf.space|/gradio_api/call/predict|abc"
    with respx.mock as mock:
        mock.get(url__regex=r".*predict/abc").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        status = await provider.poll(ref)
    assert status.state == RemoteTaskStatus.RUNNING
    await provider.aclose()


async def test_a_malformed_space_reference_is_refused() -> None:
    provider = hf_space.HuggingFaceSpaceCompute({"space": "someone/animator"})
    with pytest.raises(ComputeError):
        await provider.poll("not-a-reference")
    await provider.aclose()


def test_the_space_adapter_admits_its_quota_is_unknown() -> None:
    caps = hf_space.HuggingFaceSpaceCompute({"space": "a/b"}).capabilities()
    assert "unknown" in caps.free_tier_note.lower()
    assert caps.live_verified is False


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_kaggle_comes_before_a_space_in_the_preference_order() -> None:
    assert compute.PROVIDER_KEYS.index("kaggle") < compute.PROVIDER_KEYS.index("hf_space")


def test_no_backend_is_preferred_when_nothing_is_configured(data_root: Path) -> None:
    assert compute.preferred(RemotePurpose.IMAGE_TO_VIDEO) is None


def test_kaggle_is_preferred_once_its_credentials_are_present(
    kaggle_configured: None,
) -> None:
    provider = compute.preferred(RemotePurpose.IMAGE_TO_VIDEO)
    assert provider is not None
    assert provider.key == "kaggle"


def test_a_space_is_used_when_kaggle_is_not_configured(space_configured: None) -> None:
    provider = compute.preferred(RemotePurpose.IMAGE_TO_VIDEO)
    assert provider is not None
    assert provider.key == "hf_space"


def test_the_registry_describes_what_each_backend_needs(data_root: Path) -> None:
    rows = compute.describe()
    kaggle_row = next(row for row in rows if row["key"] == "kaggle")
    assert kaggle_row["configured"] is False
    assert set(kaggle_row["missing"]) == {"username", "key"}
    assert kaggle_row["live_verified"] is False


def test_the_registry_keeps_spaces_separate_per_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVM_HF_SPACE_I2V", "a/animator")
    monkeypatch.setenv("AVM_HF_SPACE_LIPSYNC", "b/talker")
    config.get_settings.cache_clear()
    compute.reset_cache()
    first = compute.get("hf_space", RemotePurpose.IMAGE_TO_VIDEO)
    second = compute.get("hf_space", RemotePurpose.LIPSYNC)
    assert first is not second
    assert first.credentials["space"] == "a/animator"
    assert second.credentials["space"] == "b/talker"


# ---------------------------------------------------------------------------
# Quota reporting through the service
# ---------------------------------------------------------------------------


async def test_quota_reporting_says_nothing_when_nothing_is_configured(
    data_root: Path,
) -> None:
    report = await remote_service.quota_report()
    assert all(row["known"] is False for row in report["backends"])
    assert all("Not configured" in row["detail"] for row in report["backends"])


async def test_quota_reporting_passes_through_a_live_figure(
    kaggle_configured: None,
) -> None:
    with respx.mock(base_url=kaggle.BASE_URL) as mock:
        mock.get(kaggle.QUOTA_PATH).mock(
            return_value=httpx.Response(
                200,
                json={
                    "gpuQuota": {
                        "timeUsed": "1800s",
                        "totalTimeAllowed": "108000s",
                    }
                },
            )
        )
        report = await remote_service.quota_report()
    row = next(r for r in report["backends"] if r["backend"] == "kaggle")
    assert row["known"] is True
    assert row["remaining_s"] == pytest.approx(106200.0)
    await compute.close_all()


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


async def test_the_remote_screen_renders_with_no_backend(
    client: AsyncClient,
) -> None:
    response = await client.get("/remote")
    assert response.status_code == 200
    assert "Borrowed GPU time" in response.text
    assert "Nothing has been sent yet" in response.text


async def test_the_screen_warns_that_no_call_has_been_proven_here(
    client: AsyncClient,
) -> None:
    response = await client.get("/remote")
    assert "No call has yet succeeded from this machine" in response.text


async def test_the_screen_shows_missing_credentials_rather_than_a_dead_button(
    client: AsyncClient,
) -> None:
    response = await client.get("/remote")
    assert "needs username, key" in response.text


async def test_the_screen_lists_a_submitted_run(
    client: AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
    fake_provider: FakeProvider,
) -> None:
    project_id = await make_project(session, tmp_path, count=1)
    task = await remote_service.submit_batch(session, project_id)
    response = await client.get(f"/remote?project_id={project_id}")
    assert response.status_code == 200
    assert task.client_token[:12] in response.text
    assert "tester/avm-run" in response.text


async def test_the_screen_never_prints_a_credential(
    client: AsyncClient, kaggle_configured: None
) -> None:
    response = await client.get("/remote")
    assert "secret-key-value" not in response.text
