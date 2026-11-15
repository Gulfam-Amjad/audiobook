"""The speech provider contract.

A third contract alongside :mod:`app.providers.base` (image-to-video) and
:mod:`app.providers.script.base` (writers), for the same reason they are
separate from each other: a text-to-speech engine runs locally, costs
nothing per call, and produces one file per sentence. None of the polling,
pricing or balance machinery of a paid video API applies.

The house rules still hold. Licences are recorded per voice rather than per
engine and are read from the voice's own model card, never assumed.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path

from app.constants import ErrorClass


@dataclass(frozen=True)
class VoiceSettings:
    """Everything needed to make a voice sound identical on every run.

    Taken from a ``voice_profiles`` row rather than passed around loose, so
    a line re-synthesised next week matches the one recorded today.
    """

    model_path: str
    config_path: str = ""
    speaker_id: int | None = None
    length_scale: float = 1.0
    noise_scale: float = 0.667
    noise_w: float = 0.8
    sentence_silence_s: float = 0.2
    engine: str = "piper"
    #: The named voice inside a multi-voice model (Kokoro's ``am_michael``).
    voice_key: str = ""

    def fingerprint(self) -> str:
        """A stable summary of the knobs that change the waveform.

        The model path is included because two voices can share settings.
        Used to decide whether an existing WAV can be reused as-is. Piper
        voices keep their original fingerprint so recorded lines are reused.
        """
        parts = [
            Path(self.model_path).name,
            str(self.speaker_id if self.speaker_id is not None else ""),
            f"{self.length_scale:.4f}",
            f"{self.noise_scale:.4f}",
            f"{self.noise_w:.4f}",
            f"{self.sentence_silence_s:.4f}",
        ]
        if self.engine != "piper":
            parts += [self.engine, self.voice_key]
        return "|".join(parts)


@dataclass
class SpeechResult:
    """One synthesised file.

    ``duration_s`` is left None here on purpose. The provider knows it
    wrote a file; only ffprobe knows how long it is, and the caller is what
    measures it. Nothing in this pipeline estimates a duration it could
    have measured.
    """

    path: Path
    sample_rate: int | None = None
    size_bytes: int | None = None
    duration_s: float | None = None
    engine_version: str = ""


@dataclass(frozen=True)
class SpeechCapabilities:
    key: str
    display_name: str
    #: True when synthesis happens on this machine at no per-call cost.
    local: bool = True
    #: Output format the engine writes, before any conversion.
    output_format: str = "wav"
    #: False for anything needing a human in the loop.
    automated: bool = True
    docs_url: str = ""
    licence: str = ""
    licence_note: str = ""
    checked_on: str = ""
    #: True only when the command line was read off the engine's own
    #: documentation or its ``--help`` output rather than inferred.
    invocation_verified: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


class SpeechProviderError(Exception):
    """Synthesis failed, classified so the caller can decide what to do."""

    def __init__(
        self, message: str, error_class: ErrorClass = ErrorClass.UNKNOWN
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_class = error_class


class SpeechProvider(abc.ABC):
    key: str = ""

    @abc.abstractmethod
    def capabilities(self) -> SpeechCapabilities:
        """Describe the engine. Must not do any I/O."""

    @abc.abstractmethod
    async def is_available(self) -> tuple[bool, str]:
        """Whether the engine can run right now, and why not if it cannot."""

    @abc.abstractmethod
    async def synthesise(
        self, text: str, voice: VoiceSettings, destination: Path
    ) -> SpeechResult:
        """Write one utterance to ``destination``.

        Raise :class:`SpeechProviderError` rather than returning a result
        for a file that was not actually written.
        """
