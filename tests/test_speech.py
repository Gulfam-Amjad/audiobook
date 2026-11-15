"""Narration: sentence splitting, licences, and measured synthesis.

No test here runs the real piper.exe. Synthesis is driven by a fake
provider that writes a real file, and the conftest ffprobe stub reports a
fixed duration, so what is actually being tested is the pipeline's
bookkeeping: that a duration always comes from a measurement, that
unchanged lines are not re-recorded, and that a voice whose licence was
not recognised is never reported as safe to publish.
"""

from __future__ import annotations

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

from app.constants import AudioStatus, CommercialUse, PipelineStage, StageStatus
from app.models import AudioSegment, Character, Scene, VoiceProfile
from app.providers.speech.base import (
    SpeechProviderError,
    SpeechResult,
    VoiceSettings,
)
from app.services import piper as piper_tool
from app.services import projects as project_service
from app.services import scripts as script_service
from app.services import speech as speech_service

pytestmark = pytest.mark.usefixtures("data_root")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeProvider:
    """Writes a real, small file so the measuring path is genuinely used."""

    def __init__(self, *, available: bool = True, fail_on: str = "") -> None:
        self.available = available
        self.fail_on = fail_on
        self.calls: list[tuple[str, VoiceSettings, Path]] = []

    async def is_available(self) -> tuple[bool, str]:
        return self.available, "" if self.available else "Piper is not installed."

    async def synthesise(
        self, text: str, voice: VoiceSettings, destination: Path
    ) -> SpeechResult:
        self.calls.append((text, voice, destination))
        if self.fail_on and self.fail_on in text:
            raise SpeechProviderError("Piper exited with code 1.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF" + b"\0" * 2048)
        return SpeechResult(path=destination, size_bytes=2052)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(speech_service.speech_registry, "get", lambda key="piper": fake)
    return fake


@pytest.fixture(autouse=True)
def piper_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the engine is installed. Detection itself is tested separately."""

    async def fake_detect(override: str = "") -> piper_tool.PiperStatus:
        return piper_tool.PiperStatus(
            exe_path="D:/AIVideoManager/tools/piper/piper.exe",
            source="managed",
            message="Piper is available.",
        )

    monkeypatch.setattr(piper_tool, "detect", fake_detect)


async def make_voice(
    session: AsyncSession,
    name: str = "test-voice",
    *,
    commercial_use: str = CommercialUse.ALLOWED,
    default: bool = True,
    **values: Any,
) -> VoiceProfile:
    voice = VoiceProfile(
        name=name,
        model_path=f"D:/voices/{name}.onnx",
        config_path=f"D:/voices/{name}.onnx.json",
        commercial_use=commercial_use,
        installed=True,
        is_default=default,
        **values,
    )
    session.add(voice)
    await session.flush()
    return voice


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


async def make_project_with_script(
    session: AsyncSession, scenes: list[dict[str, Any]], **project_values: Any
) -> int:
    """A project with an approved script.

    The target length is derived from the shots so the script validator,
    which rejects a plan more than 35% off target, has nothing to object
    to. These tests are about narration, not about validation.
    """
    planned = sum(float(scene["target_duration_s"]) for scene in scenes)
    values = {"name": "Voiced", "target_duration_s": max(15, round(planned))}
    values.update(project_values)
    project = await project_service.create_project(session, **values)
    plan = {
        "title": "A Story",
        "logline": "Someone wants something.",
        "synopsis": "Beginning, middle, end.",
        "scenes": scenes,
    }
    script = await script_service.accept_paste(session, project.id, json.dumps(plan))
    await script_service.approve(session, script.id)
    await session.flush()
    return project.id


# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------


def test_a_passage_becomes_one_cue_per_sentence() -> None:
    cues = speech_service.split_sentences(
        "The kettle screamed. She did not move. Outside, the wind kept going."
    )
    assert cues == [
        "The kettle screamed.",
        "She did not move.",
        "Outside, the wind kept going.",
    ]


def test_an_abbreviation_does_not_end_a_sentence() -> None:
    cues = speech_service.split_sentences("Dr. Hale arrived late. Nobody noticed.")
    assert cues == ["Dr. Hale arrived late.", "Nobody noticed."]


def test_empty_text_produces_no_cues() -> None:
    assert speech_service.split_sentences("") == []
    assert speech_service.split_sentences("   \n  ") == []


def test_whitespace_is_normalised_inside_a_cue() -> None:
    assert speech_service.split_sentences("One   two\n\tthree.") == ["One two three."]


def test_an_overlong_sentence_is_broken_at_clause_boundaries() -> None:
    long_line = (
        "She walked through the door, past the table where the lamp was still burning, "
        "around the chair that nobody ever sat in, and out towards the garden gate "
        "where the hinge had rusted through years ago."
    )
    cues = speech_service.split_sentences(long_line)
    assert len(cues) > 1
    assert all(len(cue) <= speech_service.MAX_CUE_CHARS for cue in cues)
    # Nothing is lost in the split.
    assert " ".join(cues).replace("  ", " ") == long_line


def test_a_clause_with_no_commas_is_broken_by_width() -> None:
    long_line = " ".join(["word"] * 80) + "."
    cues = speech_service.split_sentences(long_line)
    assert len(cues) > 1
    assert all(len(cue) <= speech_service.MAX_CUE_CHARS for cue in cues)


def test_a_tiny_fragment_is_absorbed_rather_than_flashed_alone() -> None:
    cues = speech_service.split_sentences("The door opened. No. Nothing was there.")
    assert "No." not in cues
    assert any("No." in cue for cue in cues)


# ---------------------------------------------------------------------------
# Licences, read rather than assumed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("licence", "expected"),
    [
        ("CC0", CommercialUse.ALLOWED),
        ("public domain", CommercialUse.ALLOWED),
        ("CC BY 4.0", CommercialUse.CONDITIONAL),
        ("https://creativecommons.org/licenses/by/4.0/", CommercialUse.CONDITIONAL),
        ("CC-BY-SA 4.0 International", CommercialUse.CONDITIONAL),
        ("CC BY-NC-SA 4.0", CommercialUse.RESTRICTED),
        ("https://creativecommons.org/licenses/by-nc-sa/4.0/deed.en", CommercialUse.RESTRICTED),
        ("See URL", CommercialUse.UNKNOWN),
        ("https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html",
         CommercialUse.UNKNOWN),
        ("", CommercialUse.UNKNOWN),
        ("Some bespoke terms nobody has parsed", CommercialUse.UNKNOWN),
    ],
)
def test_licence_strings_are_classified_conservatively(licence: str, expected: str) -> None:
    verdict, reason = piper_tool.classify_licence(licence)
    assert verdict == expected
    assert reason


def test_a_non_commercial_licence_is_not_mistaken_for_plain_attribution() -> None:
    """'by-nc-sa' contains 'by' and 'sa'; the NC must win."""
    verdict, _ = piper_tool.classify_licence("CC BY-NC-SA 4.0")
    assert verdict == CommercialUse.RESTRICTED


def test_a_real_model_card_is_parsed_as_written() -> None:
    """Text taken verbatim from the libritts_r medium card."""
    card = piper_tool.parse_model_card(
        "# Model card for libritts_r (medium)\n"
        "\n"
        "* Language: en_US (English, United States)\n"
        "* Speakers: 904\n"
        "* Quality: medium\n"
        "* Samplerate: 22,050Hz\n"
        "\n"
        "## Dataset\n"
        "\n"
        "* URL: http://www.openslr.org/141/\n"
        "* License: CC BY 4.0\n"
        "\n"
        "## Training\n"
        "\n"
        "Fine-tuned from English lessac medium on train-clean-360.\n"
    )
    assert card.licence == "CC BY 4.0"
    assert card.dataset_url == "http://www.openslr.org/141/"
    assert card.speakers == 904
    assert card.sample_rate == 22050
    assert card.commercial_use == CommercialUse.CONDITIONAL


def test_a_fine_tuned_voice_carries_a_lineage_caveat() -> None:
    card = piper_tool.parse_model_card(
        "* License: CC BY 4.0\n\nFine-tuned from English lessac medium on train-clean-360.\n"
    )
    assert card.has_lineage_caveat is True
    assert any("lessac" in warning for warning in card.warnings)


def test_trained_from_scratch_is_not_treated_as_a_caveat() -> None:
    card = piper_tool.parse_model_card("* License: CC0\n\nTrained from scratch.\n")
    assert card.has_lineage_caveat is False
    assert card.warnings == []


def test_a_missing_model_card_leaves_the_licence_unknown() -> None:
    card = piper_tool.parse_model_card("")
    assert card.commercial_use == CommercialUse.UNKNOWN
    assert card.warnings


def test_an_attribution_line_names_the_voice_and_its_licence() -> None:
    voice = piper_tool.CATALOGUE_BY_KEY["en_US-joe-medium"]
    card = piper_tool.parse_model_card(
        "* URL: https://github.com/OHF-Voice/voice-datasets\n* License: CC0\n"
    )
    line = piper_tool.attribution_for(voice, card)
    assert "en_US-joe-medium" in line
    assert "CC0" in line


def test_the_catalogue_declares_no_licences_of_its_own() -> None:
    """Licences come from model cards at install time, never from this table."""
    for entry in piper_tool.CATALOGUE:
        assert not hasattr(entry, "licence")
        assert not hasattr(entry, "commercial_use")


def test_licence_warnings_say_what_each_voice_permits() -> None:
    voices = [
        VoiceProfile(name="free", commercial_use=CommercialUse.ALLOWED),
        VoiceProfile(name="credit", commercial_use=CommercialUse.CONDITIONAL, licence="CC BY 4.0"),
        VoiceProfile(name="research", commercial_use=CommercialUse.RESTRICTED, licence="CC BY-NC-SA"),
        VoiceProfile(name="mystery", commercial_use=CommercialUse.UNKNOWN),
    ]
    warnings = speech_service.licence_warnings(voices)
    joined = " ".join(warnings)
    # The unconditional one needs no warning at all.
    assert "free" not in joined
    assert "non-commercial" in joined
    assert "not recognised" in joined
    assert "attribution" in joined


# ---------------------------------------------------------------------------
# Reuse hashing
# ---------------------------------------------------------------------------


def test_the_same_line_and_voice_hash_the_same() -> None:
    voice = VoiceSettings(model_path="D:/voices/a.onnx")
    assert speech_service.segment_hash("Hello.", voice) == speech_service.segment_hash(
        "Hello.", voice
    )


def test_changing_the_words_changes_the_hash() -> None:
    voice = VoiceSettings(model_path="D:/voices/a.onnx")
    assert speech_service.segment_hash("Hello.", voice) != speech_service.segment_hash(
        "Goodbye.", voice
    )


@pytest.mark.parametrize(
    "change",
    [
        {"length_scale": 1.2},
        {"noise_scale": 0.5},
        {"noise_w": 0.9},
        {"sentence_silence_s": 0.4},
        {"speaker_id": 3},
        {"model_path": "D:/voices/b.onnx"},
    ],
)
def test_changing_a_voice_knob_changes_the_hash(change: dict[str, Any]) -> None:
    base = VoiceSettings(model_path="D:/voices/a.onnx")
    altered = VoiceSettings(**{**base.__dict__, **change})
    assert speech_service.segment_hash("Hello.", base) != speech_service.segment_hash(
        "Hello.", altered
    )


def test_the_config_path_does_not_change_the_hash() -> None:
    """Moving a voice's sidecar file does not change how it sounds."""
    base = VoiceSettings(model_path="D:/voices/a.onnx", config_path="D:/a.json")
    moved = VoiceSettings(model_path="D:/voices/a.onnx", config_path="E:/elsewhere.json")
    assert speech_service.segment_hash("Hi.", base) == speech_service.segment_hash("Hi.", moved)


# ---------------------------------------------------------------------------
# Planning segments
# ---------------------------------------------------------------------------


async def test_planning_creates_one_segment_per_sentence(session: AsyncSession) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session,
        [
            scene_dict("s01", "The kettle screamed. She did not move."),
            scene_dict("s02", "Outside, the wind kept going all night long."),
        ],
    )
    plan = await speech_service.plan_segments(session, project_id)

    assert plan.total == 3
    assert plan.to_synthesise == 3
    assert plan.reusable == 0
    rows = (
        await session.execute(
            select(AudioSegment).where(AudioSegment.project_id == project_id)
        )
    ).scalars().all()
    assert [row.text for row in rows] == [
        "The kettle screamed.",
        "She did not move.",
        "Outside, the wind kept going all night long.",
    ]


async def test_planning_without_an_approved_script_is_refused(
    session: AsyncSession,
) -> None:
    await make_voice(session)
    project = await project_service.create_project(session, name="No script")
    with pytest.raises(speech_service.SpeechError, match="No approved script"):
        await speech_service.plan_segments(session, project.id)


async def test_a_draft_script_is_not_used_even_when_it_is_valid(
    session: AsyncSession,
) -> None:
    """Only the approved version may drive a stage that writes files."""
    await make_voice(session)
    project = await project_service.create_project(session, name="Draft only")
    plan = {
        "title": "T",
        "logline": "L",
        "synopsis": "S",
        "scenes": [scene_dict("s01", "One."), scene_dict("s02", "Two.")],
    }
    await script_service.accept_paste(session, project.id, json.dumps(plan))
    with pytest.raises(speech_service.SpeechError, match="No approved script"):
        await speech_service.plan_segments(session, project.id)


async def test_dialogue_is_split_and_attributed_to_its_character(
    session: AsyncSession,
) -> None:
    narrator = await make_voice(session, "narrator")
    mara_voice = await make_voice(session, "mara-voice", default=False)
    project_id = await make_project_with_script(
        session,
        [
            scene_dict(
                "s01",
                "The room was quiet.",
                characters=["Mara"],
                dialogue=[{"character": "Mara", "line": "You came back. I did not expect that."}],
            ),
            scene_dict("s02", "She closed the door behind her."),
        ],
    )
    mara = (
        await session.execute(select(Character).where(Character.name == "Mara"))
    ).scalar_one()
    mara.voice_profile_id = mara_voice.id
    await session.flush()

    await speech_service.plan_segments(session, project_id)
    rows = (
        await session.execute(
            select(AudioSegment)
            .where(AudioSegment.project_id == project_id)
            .order_by(AudioSegment.scene_id, AudioSegment.order_index)
        )
    ).scalars().all()

    dialogue = [row for row in rows if row.kind == "dialogue"]
    assert len(dialogue) == 2
    assert all(row.voice_profile_id == mara_voice.id for row in dialogue)
    assert all(row.character_id == mara.id for row in dialogue)
    narration = [row for row in rows if row.kind == "narration"]
    assert all(row.voice_profile_id == narrator.id for row in narration)


async def test_planning_twice_does_not_duplicate_segments(session: AsyncSession) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing happens."), scene_dict("s02", "Then another.")]
    )
    first = await speech_service.plan_segments(session, project_id)
    second = await speech_service.plan_segments(session, project_id)
    assert second.total == first.total


# ---------------------------------------------------------------------------
# Synthesis, and the measurements it produces
# ---------------------------------------------------------------------------


async def test_every_duration_comes_from_a_measurement(
    session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing happens."), scene_dict("s02", "Then another.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    run = await speech_service.synthesise_project(session, project_id)

    assert run.synthesised == 2
    assert run.failed == 0
    # The conftest stub reports 4.0s per file; the total is the sum of real
    # measurements rather than anything derived from word count.
    assert run.measured_s == pytest.approx(8.0)
    rows = (
        await session.execute(
            select(AudioSegment).where(AudioSegment.project_id == project_id)
        )
    ).scalars().all()
    assert all(row.status == AudioStatus.READY for row in rows)
    assert all(row.duration_s == pytest.approx(4.0) for row in rows)
    assert all(Path(row.file_path).exists() for row in rows)


async def test_the_speech_stage_is_completed_when_everything_is_recorded(
    session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One."), scene_dict("s02", "Two.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)

    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SPEECH
    )
    assert checkpoint.status == StageStatus.COMPLETED
    assert "measured" in checkpoint.message


async def test_one_failed_line_leaves_the_stage_partial_not_completed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvider(fail_on="broken")
    monkeypatch.setattr(speech_service.speech_registry, "get", lambda key="piper": fake)
    await make_voice(session)
    project_id = await make_project_with_script(
        session,
        [scene_dict("s01", "This line is fine."), scene_dict("s02", "This line is broken.")],
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    run = await speech_service.synthesise_project(session, project_id)

    assert run.synthesised == 1
    assert run.failed == 1
    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SPEECH
    )
    assert checkpoint.status == StageStatus.PARTIAL


async def test_a_half_recorded_scene_reports_no_measured_length(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial total would read as a short scene, which is worse than none."""
    fake = FakeProvider(fail_on="second")
    monkeypatch.setattr(speech_service.speech_registry, "get", lambda key="piper": fake)
    await make_voice(session)
    project_id = await make_project_with_script(
        session,
        [
            scene_dict("s01", "The first sentence works. But the second one fails."),
            scene_dict("s02", "An entirely separate shot."),
        ],
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)

    scenes = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().all()
    assert scenes[0].measured_audio_s is None
    assert scenes[1].measured_audio_s == pytest.approx(4.0)


async def test_a_missing_engine_blocks_rather_than_fails(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocked means waiting on the user; failed would imply we should retry."""
    fake = FakeProvider(available=False)
    monkeypatch.setattr(speech_service.speech_registry, "get", lambda key="piper": fake)
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One."), scene_dict("s02", "Two.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    run = await speech_service.synthesise_project(session, project_id)

    assert run.synthesised == 0
    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SPEECH
    )
    assert checkpoint.status == StageStatus.BLOCKED
    assert checkpoint.blocked_reason


async def test_narration_is_blocked_when_durations_cannot_be_measured(
    session: AsyncSession, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ffprobe the cues would have to be guessed, so we stop instead."""
    from app.services.ffmpeg import FFmpegStatus

    async def no_ffprobe(
        ffmpeg_override: str = "", ffprobe_override: str = ""
    ) -> FFmpegStatus:
        return FFmpegStatus(message="FFprobe was not found.")

    monkeypatch.setattr(speech_service.ffmpeg, "detect", no_ffprobe)
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One."), scene_dict("s02", "Two.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    run = await speech_service.synthesise_project(session, project_id)

    assert run.synthesised == 0
    assert provider.calls == []
    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SPEECH
    )
    assert checkpoint.status == StageStatus.BLOCKED
    assert "measured" in checkpoint.blocked_reason


async def test_a_file_that_cannot_be_measured_is_not_counted_as_ready(
    session: AsyncSession, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.ffmpeg import MediaInfo

    async def unmeasurable(path: Path, ffprobe_path: str | None = None) -> MediaInfo:
        return MediaInfo(error="FFprobe could not determine a duration for this file.")

    monkeypatch.setattr("app.services.ffmpeg.probe", unmeasurable)
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One."), scene_dict("s02", "Two.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    run = await speech_service.synthesise_project(session, project_id)

    assert run.failed == 2
    assert run.measured_s == 0.0
    rows = (
        await session.execute(
            select(AudioSegment).where(AudioSegment.project_id == project_id)
        )
    ).scalars().all()
    assert all(row.status == AudioStatus.FAILED for row in rows)
    assert all(row.duration_s is None for row in rows)


async def test_a_line_with_no_voice_fails_rather_than_being_skipped_quietly(
    session: AsyncSession, provider: FakeProvider
) -> None:
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One."), scene_dict("s02", "Two.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    run = await speech_service.synthesise_project(session, project_id)

    assert run.failed == 2
    assert provider.calls == []
    assert any("No voice" in error for error in run.errors)


# ---------------------------------------------------------------------------
# Reuse and single-line regeneration
# ---------------------------------------------------------------------------


async def test_an_unchanged_line_is_not_recorded_twice(
    session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)
    assert len(provider.calls) == 2

    plan = await speech_service.plan_segments(session, project_id)
    await session.commit()
    assert plan.reusable == 2
    assert plan.to_synthesise == 0

    run = await speech_service.synthesise_project(session, project_id)
    assert len(provider.calls) == 2
    assert run.attempted == 0


async def test_editing_one_line_re_records_only_that_line(
    session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)
    provider.calls.clear()

    scene = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().first()
    scene.narration = "One thing, rewritten."
    await session.commit()

    plan = await speech_service.plan_segments(session, project_id)
    await session.commit()
    assert plan.reusable == 1
    assert plan.to_synthesise == 1

    await speech_service.synthesise_project(session, project_id)
    assert [call[0] for call in provider.calls] == ["One thing, rewritten."]


async def test_a_new_script_version_keeps_a_line_that_did_not_change(
    session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)
    provider.calls.clear()

    plan = {
        "title": "A Story",
        "logline": "Someone wants something.",
        "synopsis": "Beginning, middle, end.",
        "scenes": [
            scene_dict("s01", "One thing."),
            scene_dict("s02", "A different thing."),
        ],
    }
    script = await script_service.accept_paste(session, project_id, json.dumps(plan))
    await script_service.approve(session, script.id)
    await session.commit()

    speech_plan = await speech_service.plan_segments(session, project_id)
    await session.commit()
    assert speech_plan.reusable == 1
    assert speech_plan.to_synthesise == 1

    await speech_service.synthesise_project(session, project_id)
    assert [call[0] for call in provider.calls] == ["A different thing."]


async def test_changing_the_voice_speed_re_records_everything(
    session: AsyncSession, provider: FakeProvider
) -> None:
    voice = await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)
    provider.calls.clear()

    voice.length_scale = 1.15
    await session.commit()

    plan = await speech_service.plan_segments(session, project_id)
    assert plan.to_synthesise == 2
    assert plan.reusable == 0


async def test_a_deleted_scene_takes_its_segments_with_it(
    session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    scene = (
        await session.execute(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
        )
    ).scalars().first()
    scene.narration = ""
    await session.commit()

    plan = await speech_service.plan_segments(session, project_id)
    assert plan.removed == 1
    remaining = (
        await session.execute(
            select(AudioSegment).where(AudioSegment.project_id == project_id)
        )
    ).scalars().all()
    assert len(remaining) == 1


async def test_re_recording_one_line_leaves_the_others_untouched(
    session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)
    provider.calls.clear()

    target = (
        await session.execute(
            select(AudioSegment)
            .where(AudioSegment.project_id == project_id)
            .order_by(AudioSegment.id)
        )
    ).scalars().first()

    segment = await speech_service.regenerate_segment(session, target.id)

    assert len(provider.calls) == 1
    assert provider.calls[0][0] == target.text
    assert segment.status == AudioStatus.READY
    assert segment.duration_s == pytest.approx(4.0)


async def test_re_recording_an_unchanged_line_still_runs(
    session: AsyncSession, provider: FakeProvider
) -> None:
    """The button means 'that take was wrong', so the hash must not skip it."""
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)
    provider.calls.clear()

    target = (
        await session.execute(select(AudioSegment).order_by(AudioSegment.id))
    ).scalars().first()
    await speech_service.regenerate_segment(session, target.id)
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Voice profile management
# ---------------------------------------------------------------------------


async def test_the_first_installed_voice_becomes_the_default(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "en_US-joe-medium.onnx"
    model.write_bytes(b"\0" * 16)

    async def fake_install(key: str, *, client: Any = None) -> piper_tool.InstalledVoice:
        return piper_tool.InstalledVoice(
            voice=piper_tool.CATALOGUE_BY_KEY[key],
            model_path=model,
            config_path=tmp_path / "en_US-joe-medium.onnx.json",
            card=piper_tool.parse_model_card("* License: CC0\n\nTrained from scratch.\n"),
        )

    monkeypatch.setattr(piper_tool, "install_voice", fake_install)
    voice = await speech_service.install_catalogue_voice(session, "en_US-joe-medium")

    assert voice.is_default is True
    assert voice.installed is True
    assert voice.licence == "CC0"
    assert voice.commercial_use == CommercialUse.ALLOWED
    assert voice.attribution


async def test_installing_a_voice_records_what_its_card_says_not_a_guess(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = tmp_path / "v.onnx"
    model.write_bytes(b"\0" * 16)

    async def fake_install(key: str, *, client: Any = None) -> piper_tool.InstalledVoice:
        return piper_tool.InstalledVoice(
            voice=piper_tool.CATALOGUE_BY_KEY[key],
            model_path=model,
            config_path=tmp_path / "v.onnx.json",
            card=piper_tool.parse_model_card(
                "* Samplerate: 22,050Hz\n* URL: https://example.invalid/data\n"
                "* License: CC BY-NC-SA 4.0\n"
            ),
        )

    monkeypatch.setattr(piper_tool, "install_voice", fake_install)
    voice = await speech_service.install_catalogue_voice(session, "en_US-kristin-medium")

    assert voice.commercial_use == CommercialUse.RESTRICTED
    assert voice.sample_rate == 22050
    assert "non-commercial" in (voice.notes or "")


async def test_a_voice_in_use_by_a_project_cannot_be_deleted(
    session: AsyncSession,
) -> None:
    voice = await make_voice(session)
    project = await project_service.create_project(session, name="Uses the voice")
    project.narrator_voice_id = voice.id
    await session.flush()

    with pytest.raises(speech_service.SpeechError, match="still the narrator"):
        await speech_service.delete_voice(session, voice.id)


async def test_making_a_voice_default_demotes_the_previous_one(
    session: AsyncSession,
) -> None:
    first = await make_voice(session, "first")
    second = await make_voice(session, "second", default=False)

    await speech_service.set_default_voice(session, second.id)

    await session.refresh(first)
    assert first.is_default is False
    assert second.is_default is True


@pytest.mark.parametrize(
    ("field", "value"),
    [("length_scale", 9.0), ("noise_scale", -1.0), ("sentence_silence_s", 60.0)],
)
async def test_a_voice_knob_outside_its_range_is_refused(
    session: AsyncSession, field: str, value: float
) -> None:
    voice = await make_voice(session)
    with pytest.raises(speech_service.SpeechError, match="must be between"):
        await speech_service.update_voice(session, voice.id, **{field: value})


async def test_licence_fields_cannot_be_edited_through_the_update_form(
    session: AsyncSession,
) -> None:
    """A voice's licence is whatever its card said, not what a form posts."""
    voice = await make_voice(session, commercial_use=CommercialUse.RESTRICTED)
    voice.licence = "CC BY-NC-SA 4.0"
    await session.flush()

    await speech_service.update_voice(
        session, voice.id, commercial_use="allowed", licence="CC0", length_scale=1.1
    )

    assert voice.commercial_use == CommercialUse.RESTRICTED
    assert voice.licence == "CC BY-NC-SA 4.0"
    assert voice.length_scale == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# Installing the engine
# ---------------------------------------------------------------------------


def test_an_archive_cannot_write_outside_its_destination(tmp_path: Path) -> None:
    import zipfile

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escaped.txt", "nope")

    with pytest.raises(piper_tool.PiperInstallError, match="unsafe path"):
        piper_tool._extract(archive, tmp_path / "unpack")  # noqa: SLF001

    assert not (tmp_path / "escaped.txt").exists()


@respx.mock
async def test_a_truncated_engine_download_installs_nothing(data_root: Path) -> None:
    respx.get(piper_tool.PIPER_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=b"not really a zip")
    )
    with pytest.raises(piper_tool.PiperInstallError, match="pinned release asset"):
        await piper_tool.install_engine()
    assert not piper_tool.managed_exe().exists()


@respx.mock
async def test_a_download_retries_a_dropped_connection(tmp_path: Path) -> None:
    """Home DNS here intermittently fails, so a single attempt is not enough."""
    route = respx.get("https://example.invalid/file.bin")
    route.side_effect = [
        httpx.ConnectError("getaddrinfo failed"),
        httpx.Response(200, content=b"payload"),
    ]
    destination = tmp_path / "file.bin"
    async with httpx.AsyncClient() as client:
        written = await piper_tool._download(client, "https://example.invalid/file.bin", destination)  # noqa: SLF001

    assert written == 7
    assert destination.read_bytes() == b"payload"
    assert route.call_count == 2


@respx.mock
async def test_a_download_that_never_connects_leaves_no_partial_file(
    tmp_path: Path,
) -> None:
    respx.get("https://example.invalid/file.bin").mock(
        side_effect=httpx.ConnectError("getaddrinfo failed")
    )
    destination = tmp_path / "file.bin"
    with pytest.raises(piper_tool.PiperInstallError, match="after 4 attempts"):
        async with httpx.AsyncClient() as client:
            await piper_tool._download(client, "https://example.invalid/file.bin", destination)  # noqa: SLF001

    assert not destination.exists()
    assert not destination.with_suffix(".bin.part").exists()


@respx.mock
async def test_a_voice_file_far_too_small_to_be_a_model_is_rejected(
    data_root: Path,
) -> None:
    voice = piper_tool.CATALOGUE_BY_KEY["en_US-joe-medium"]
    respx.get(voice.model_url).mock(return_value=httpx.Response(200, content=b"tiny"))
    respx.get(voice.config_url).mock(return_value=httpx.Response(200, content=b"{}"))
    respx.get(voice.model_card_url).mock(
        return_value=httpx.Response(200, content=b"* License: CC0\n")
    )

    with pytest.raises(piper_tool.PiperInstallError, match="far too small"):
        await piper_tool.install_voice("en_US-joe-medium")


@respx.mock
async def test_a_voice_installs_with_the_licence_from_its_own_card(
    data_root: Path,
) -> None:
    voice = piper_tool.CATALOGUE_BY_KEY["en_US-joe-medium"]
    respx.get(voice.model_url).mock(
        return_value=httpx.Response(200, content=b"\0" * 2_000_000)
    )
    respx.get(voice.config_url).mock(return_value=httpx.Response(200, content=b"{}"))
    respx.get(voice.model_card_url).mock(
        return_value=httpx.Response(
            200,
            content=b"* Samplerate: 22,050Hz\n* URL: https://example.invalid\n* License: CC0\n",
        )
    )

    installed = await piper_tool.install_voice("en_US-joe-medium")

    assert installed.card.licence == "CC0"
    assert installed.card.commercial_use == CommercialUse.ALLOWED
    assert installed.model_path.exists()


@respx.mock
async def test_a_voice_with_no_model_card_is_installed_but_stays_unknown(
    data_root: Path,
) -> None:
    voice = piper_tool.CATALOGUE_BY_KEY["en_US-joe-medium"]
    respx.get(voice.model_url).mock(
        return_value=httpx.Response(200, content=b"\0" * 2_000_000)
    )
    respx.get(voice.config_url).mock(return_value=httpx.Response(200, content=b"{}"))
    respx.get(voice.model_card_url).mock(return_value=httpx.Response(404))

    installed = await piper_tool.install_voice("en_US-joe-medium")

    assert installed.card.commercial_use == CommercialUse.UNKNOWN
    assert installed.card.warnings


async def test_an_unknown_catalogue_key_is_refused() -> None:
    with pytest.raises(piper_tool.PiperInstallError, match="not in the voice catalogue"):
        await piper_tool.install_voice("en_US-nobody-medium")


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_the_voices_page_renders(client: AsyncClient) -> None:
    response = await client.get("/voices")
    assert response.status_code == 200
    assert "Available to install" in response.text
    for entry in piper_tool.CATALOGUE:
        assert entry.display_name in response.text


async def test_the_voices_page_shows_what_a_licence_permits(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_voice(session, "restricted-voice", commercial_use=CommercialUse.RESTRICTED)
    await session.commit()

    response = await client.get("/voices")
    assert "non-commercial" in response.text


async def test_the_narration_page_renders_before_any_script_exists(
    client: AsyncClient, session: AsyncSession
) -> None:
    await project_service.create_project(session, name="Fresh")
    await session.commit()

    response = await client.get("/projects/1/voice")
    assert response.status_code == 200
    assert "No approved script" in response.text


async def test_the_narration_page_lists_measured_segments(
    client: AsyncClient, session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()
    await speech_service.synthesise_project(session, project_id)

    response = await client.get(f"/projects/{project_id}/voice")
    assert response.status_code == 200
    assert "One thing." in response.text
    assert "4.0s" in response.text
    assert "8.0s" in response.text


async def test_recording_over_http_reports_measured_seconds(
    client: AsyncClient, session: AsyncSession, provider: FakeProvider
) -> None:
    await make_voice(session)
    project_id = await make_project_with_script(
        session, [scene_dict("s01", "One thing."), scene_dict("s02", "Another thing.")]
    )
    await session.commit()

    response = await client.post(f"/projects/{project_id}/voice/synthesise")

    assert response.status_code == 200
    assert "measured" in response.headers.get("X-AVM-Flash", "")


async def test_recording_without_an_approved_script_is_refused_over_http(
    client: AsyncClient, session: AsyncSession, provider: FakeProvider
) -> None:
    await project_service.create_project(session, name="Fresh")
    await session.commit()

    response = await client.post("/projects/1/voice/synthesise")
    assert response.status_code == 400
    assert "approved script" in response.json()["detail"]
