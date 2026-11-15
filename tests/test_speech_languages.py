"""Hindi and Urdu narration: sentence breaks, licences, and the new engines.

The engines that need the network or a model are replaced with fakes here.
The live Edge and Kokoro checks were run by hand and are not in the suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import AudioStatus, CommercialUse, ErrorClass
from app.models import AudioSegment
from app.providers.speech import edge, kokoro
from app.providers.speech.base import SpeechProviderError, VoiceSettings
from app.services import piper as piper_tool
from app.services import speech as speech_service
from app.services.ffmpeg import FFmpegStatus
from tests.test_speech import FakeProvider, make_project_with_script, scene_dict

pytestmark = pytest.mark.usefixtures("data_root")


# ---------------------------------------------------------------------------
# Sentence breaks
# ---------------------------------------------------------------------------


def test_urdu_sentences_end_at_the_urdu_full_stop_and_question_mark() -> None:
    cues = speech_service.split_sentences(
        "یہ ایک سادہ جملہ ہے۔ کیا آپ ٹھیک ہیں؟ میں ٹھیک ہوں۔"
    )
    assert len(cues) == 3
    assert cues[0].endswith("۔")
    assert cues[1].endswith("؟")


def test_hindi_sentences_end_at_the_danda() -> None:
    cues = speech_service.split_sentences(
        "नमस्ते दोस्तों, आज हम एक बहुत दिलचस्प कहानी सुनाने वाले हैं। "
        "यह कहानी एक पुराने शहर की है, जहाँ हर रात एक नया रहस्य जागता था।"
    )
    assert len(cues) == 2
    assert cues[0].endswith("।")


def test_an_overlong_urdu_passage_breaks_at_the_urdu_comma() -> None:
    clause = "یہ ایک طویل جملہ ہے جس میں بہت سی باتیں ہیں، "
    cues = speech_service.split_sentences(clause * 6 + "۔")
    assert len(cues) > 1
    assert all(len(cue) <= speech_service.MAX_CUE_CHARS for cue in cues)


# ---------------------------------------------------------------------------
# Licences for the Hindi and Urdu Piper voices
# ---------------------------------------------------------------------------


def test_the_catalogue_offers_urdu_and_hindi_voices() -> None:
    for key in (
        "ur_PK-fasih-medium",
        "hi_IN-rohan-medium",
        "hi_IN-pratham-medium",
        "hi_IN-priyamvada-medium",
    ):
        entry = piper_tool.CATALOGUE_BY_KEY[key]
        assert entry.approx_bytes > 1_000_000
        assert entry.repo_path.startswith(("ur/", "hi/"))


def test_a_model_card_licence_under_a_heading_is_read() -> None:
    card = piper_tool.parse_model_card("# Urdu voice\n\n## License\n\nMIT\n")
    assert card.licence == "MIT"


def test_a_model_card_licence_in_the_yaml_header_is_read() -> None:
    card = piper_tool.parse_model_card("---\nlicense: mit\n---\n# Urdu voice\n")
    assert card.licence == "mit"


def test_hindi_pratham_card_is_non_commercial() -> None:
    card = piper_tool.parse_model_card(
        "# Model card for pratham (medium)\n"
        "* Language: hi_IN (Hindi, India)\n"
        "## Dataset\n"
        "* License: http:// creativecommons.org/licenses/ by-nc-sa/4.0/\n"
    )
    assert card.commercial_use == CommercialUse.RESTRICTED


async def _install_with_card(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, card: str) -> Any:
    """Run the real installer against a fake download that writes the card given."""
    monkeypatch.setattr(piper_tool, "voices_dir", lambda: tmp_path)

    async def fake_download(client: Any, url: str, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith("MODEL_CARD"):
            destination.write_text(card, encoding="utf-8")
        elif url.endswith(".onnx"):
            destination.write_bytes(b"\0" * 1_000_001)
        else:
            destination.write_text("{}", encoding="utf-8")
        return destination.stat().st_size

    monkeypatch.setattr(piper_tool, "_download", fake_download)
    return await piper_tool.install_voice(key, client=object())


async def test_the_urdu_voice_stays_unknown_even_though_its_card_says_mit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed = await _install_with_card(
        monkeypatch,
        tmp_path,
        "ur_PK-fasih-medium",
        "---\nlicense: mit\n---\n# Urdu voice\n\n## License\n\nMIT\n\n"
        "Base model: v1 of this voice, from the Hindi Rohan checkpoint.\n",
    )
    assert installed.card.licence.lower() == "mit"
    assert installed.card.commercial_use == CommercialUse.UNKNOWN
    assert "Rohan" in installed.card.licence_reason


async def test_a_voice_without_a_caution_keeps_its_cards_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installed = await _install_with_card(
        monkeypatch,
        tmp_path,
        "hi_IN-pratham-medium",
        "* License: http:// creativecommons.org/licenses/ by-nc-sa/4.0/\n",
    )
    assert installed.card.commercial_use == CommercialUse.RESTRICTED


# ---------------------------------------------------------------------------
# Kokoro's Hindi voices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("voice", "language"),
    [
        ("hf_alpha", "hi"),
        ("hm_psi", "hi"),
        ("am_michael", "en-us"),
        ("bm_george", "en-gb"),
    ],
)
def test_kokoro_voice_prefix_selects_the_language(voice: str, language: str) -> None:
    assert kokoro.language_for(voice) == language


# ---------------------------------------------------------------------------
# Microsoft's online voices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("length_scale", "rate"),
    [(1.0, "+0%"), (1.25, "-20%"), (0.8, "+25%"), (0.01, "+100%"), (50.0, "-50%")],
)
def test_edge_rate_follows_length_scale_and_is_clamped(length_scale: float, rate: str) -> None:
    assert edge.rate_for(length_scale) == rate


def test_edge_language_comes_from_the_voice_name() -> None:
    assert edge.language_for("ur-PK-UzmaNeural") == "ur_PK"
    assert edge.language_for("hi-IN-SwaraNeural") == "hi_IN"


async def test_an_edge_voice_is_recorded_with_its_commercial_use_unknown(
    session: AsyncSession,
) -> None:
    voice = await speech_service.install_edge_voice(session, "hi-IN-SwaraNeural")
    assert voice.engine == "edge"
    assert voice.model_path == ""
    assert voice.commercial_use == CommercialUse.UNKNOWN
    assert voice.installed is True
    settings = speech_service.voice_settings(voice)
    assert settings.engine == "edge"
    assert settings.voice_key == "hi-IN-SwaraNeural"


async def test_only_offered_edge_voices_can_be_added(session: AsyncSession) -> None:
    with pytest.raises(speech_service.SpeechError, match="not one of the Edge voices"):
        await speech_service.install_edge_voice(session, "en-US-GuyNeural")


async def test_a_kokoro_hindi_voice_is_recorded_as_hindi(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "kokoro"
    folder.mkdir()
    (folder / kokoro.MODEL_FILE).write_bytes(b"0")
    (folder / kokoro.VOICES_FILE).write_bytes(b"0")
    monkeypatch.setattr(kokoro, "models_dir", lambda: folder)

    voice = await speech_service.install_kokoro_voice(session, "hf_alpha")
    assert voice.language == "hi_IN"
    assert voice.engine == "kokoro"


async def test_edge_synthesis_refuses_an_empty_line_without_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.services.ffmpeg.detect",
        _fake_ffmpeg_detect,
    )
    provider = edge.EdgeSpeechProvider()
    with pytest.raises(SpeechProviderError) as caught:
        await provider.synthesise("   ", _edge_settings("ur-PK-UzmaNeural"), tmp_path / "x.wav")
    assert caught.value.error_class == ErrorClass.INVALID_INPUT


async def test_an_edge_outage_is_reported_as_transient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import edge_tts

    monkeypatch.setattr("app.services.ffmpeg.detect", _fake_ffmpeg_detect)

    class BrokenCommunicate:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def stream(self):
            raise OSError("connection reset by peer")
            yield {}

    monkeypatch.setattr(edge_tts, "Communicate", BrokenCommunicate)
    provider = edge.EdgeSpeechProvider()
    with pytest.raises(SpeechProviderError, match="connection reset") as caught:
        await provider.synthesise("مرحبا", _edge_settings("ur-PK-UzmaNeural"), tmp_path / "x.wav")
    assert caught.value.error_class == ErrorClass.TRANSIENT


async def test_a_narration_line_on_an_edge_voice_needs_no_piper_model(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path: an Edge voice with no model file records every line."""
    fake = FakeProvider()
    monkeypatch.setattr(speech_service.speech_registry, "get", lambda key="piper": fake)

    voice = await speech_service.install_edge_voice(session, "ur-PK-UzmaNeural")
    await speech_service.set_default_voice(session, voice.id)
    project_id = await make_project_with_script(
        session,
        [
            scene_dict("s01", "یہ ایک سادہ جملہ ہے۔ کیا آپ ٹھیک ہیں؟"),
            scene_dict("s02", "میں ٹھیک ہوں، شکریہ۔"),
        ],
    )
    await speech_service.plan_segments(session, project_id)
    await session.commit()

    run = await speech_service.synthesise_project(session, project_id)

    assert run.failed == 0, run.errors
    assert run.synthesised == 3
    rows = (
        await session.execute(select(AudioSegment).where(AudioSegment.project_id == project_id))
    ).scalars().all()
    assert all(row.status == AudioStatus.READY for row in rows)
    assert {call[1].engine for call in fake.calls} == {"edge"}
    assert {call[1].voice_key for call in fake.calls} == {"ur-PK-UzmaNeural"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fake_ffmpeg_detect(*args: Any, **kwargs: Any) -> FFmpegStatus:
    return FFmpegStatus(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe")


def _edge_settings(voice_key: str) -> VoiceSettings:
    return VoiceSettings(model_path="", engine="edge", voice_key=voice_key)
