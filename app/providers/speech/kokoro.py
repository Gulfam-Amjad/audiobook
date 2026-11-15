"""Narration through Kokoro-82M, run in-process with onnxruntime.

Licences, read from their sources on 2026-11-08:

* ``kokoro-onnx`` (the Python wrapper): MIT, from the package's LICENSE.
* Kokoro-82M weights (hexgrad/Kokoro-82M): Apache-2.0, from the model
  card. The ONNX export used here (fastrtc/kokoro-onnx, the same files as
  the wrapper's own release) is MIT.
* The phonemizer it calls (``phonemizer-fork`` with eSpeak NG) is GPL-3.
  That governs redistributing the software, not the audio it produces;
  Piper uses the same eSpeak NG phonemes.

Kokoro takes a speed rather than Piper's ``length_scale``; one is the
inverse of the other, so a voice profile means the same thing whichever
engine reads it. The model is loaded once and kept, because loading it
takes longer than speaking a line.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import wave
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.constants import ErrorClass
from app.providers.speech.base import (
    SpeechCapabilities,
    SpeechProvider,
    SpeechProviderError,
    SpeechResult,
    VoiceSettings,
)

log = logging.getLogger(__name__)

MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
SAMPLE_RATE = 24_000
ENGINE_VERSION = "kokoro-v1.0"
#: Kokoro refuses speeds outside this range.
MIN_SPEED, MAX_SPEED = 0.5, 2.0
MIN_WAV_BYTES = 1024

#: Male narrators worth offering for documentaries, American then British.
NARRATORS = ("am_michael", "am_adam", "am_onyx", "am_eric", "bm_george", "bm_lewis", "bm_daniel")
#: Hindi voices in the v1.0 release. ``hf`` is female, ``hm`` male.
HINDI_VOICES = ("hf_alpha", "hf_beta", "hm_omega", "hm_psi")

_LOCK = threading.Lock()
_ENGINES: dict[str, Any] = {}


def models_dir() -> Path:
    return get_settings().data_root / "models" / "kokoro"


def language_for(voice_key: str) -> str:
    """Kokoro's voice names start with the language: a = American, b = British, h = Hindi.

    Hindi is passed to espeak-ng as ``hi``. Checked on this machine: the
    Hindi voices synthesise through kokoro-onnx with that code.
    """
    prefix = voice_key[:1]
    if prefix == "h":
        return "hi"
    return "en-gb" if prefix == "b" else "en-us"


def speed_for(length_scale: float) -> float:
    speed = 1.0 / length_scale if length_scale > 0 else 1.0
    return round(min(max(speed, MIN_SPEED), MAX_SPEED), 3)


def _engine(model_path: str, voices_path: str) -> Any:
    key = f"{model_path}|{voices_path}"
    with _LOCK:
        if key not in _ENGINES:
            from kokoro_onnx import Kokoro

            _ENGINES[key] = Kokoro(model_path, voices_path)
        return _ENGINES[key]


def _write_wav(samples: Any, sample_rate: int, destination: Path, silence_s: float) -> None:
    import numpy as np

    pad = np.zeros(int(sample_rate * max(silence_s, 0.0)), dtype=np.float32)
    signal = np.concatenate([np.asarray(samples, dtype=np.float32), pad])
    pcm = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


class KokoroSpeechProvider(SpeechProvider):
    key = "kokoro"

    def capabilities(self) -> SpeechCapabilities:
        return SpeechCapabilities(
            key=self.key,
            display_name="Kokoro-82M",
            local=True,
            output_format="wav",
            automated=True,
            docs_url="https://huggingface.co/hexgrad/Kokoro-82M",
            licence="Apache-2.0 (weights), MIT (kokoro-onnx)",
            licence_note=(
                "Weights Apache-2.0 per the hexgrad/Kokoro-82M model card; the "
                "wrapper is MIT. Its phonemizer uses eSpeak NG (GPL-3), which "
                "does not extend to the audio produced."
            ),
            checked_on="2026-11-08",
            invocation_verified=True,
            notes=(
                "Runs on the CPU through onnxruntime; about 1.8x real time here.",
                "One file per sentence, measured with ffprobe like Piper's.",
                "Hindi voices are phonemised by espeak-ng, not checked by ear.",
            ),
        )

    async def is_available(self) -> tuple[bool, str]:
        folder = models_dir()
        missing = [name for name in (MODEL_FILE, VOICES_FILE) if not (folder / name).is_file()]
        if missing:
            return False, f"Kokoro's model files are missing from {folder}: {', '.join(missing)}."
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError as exc:
            return False, f"kokoro-onnx is not installed ({exc})."
        return True, "Kokoro-82M is installed."

    async def synthesise(self, text: str, voice: VoiceSettings, destination: Path) -> SpeechResult:
        line = " ".join((text or "").split())
        if not line:
            raise SpeechProviderError("There is nothing to say: the line is empty.", ErrorClass.INVALID_INPUT)
        if not voice.voice_key:
            raise SpeechProviderError("No Kokoro voice is named on this profile.", ErrorClass.INVALID_INPUT)
        model_path = voice.model_path or str(models_dir() / MODEL_FILE)
        voices_path = voice.config_path or str(models_dir() / VOICES_FILE)
        for path in (model_path, voices_path):
            if not Path(path).is_file():
                raise SpeechProviderError(f"The Kokoro file is missing: {path}", ErrorClass.INVALID_INPUT)

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)

        def run() -> None:
            engine = _engine(model_path, voices_path)
            samples, rate = engine.create(
                line,
                voice=voice.voice_key,
                speed=speed_for(voice.length_scale),
                lang=language_for(voice.voice_key),
            )
            _write_wav(samples, rate, partial, voice.sentence_silence_s)

        try:
            await asyncio.to_thread(run)
        except SpeechProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - onnxruntime and phonemizer raise many kinds
            partial.unlink(missing_ok=True)
            raise SpeechProviderError(f"Kokoro failed: {exc}", ErrorClass.UNKNOWN) from exc

        size = partial.stat().st_size if partial.exists() else 0
        if size < MIN_WAV_BYTES:
            partial.unlink(missing_ok=True)
            raise SpeechProviderError(f"Kokoro wrote only {size} bytes.", ErrorClass.UNKNOWN)
        destination.unlink(missing_ok=True)
        partial.replace(destination)
        return SpeechResult(path=destination, sample_rate=SAMPLE_RATE, size_bytes=size,
                            engine_version=ENGINE_VERSION)
