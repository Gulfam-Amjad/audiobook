"""Narration through the Piper executable.

Piper reads the line to speak on stdin and writes a WAV to the path given
by ``--output_file``. The flags used here were read from the binary's own
``--help`` output on this machine, which is why
:attr:`SpeechCapabilities.invocation_verified` is set.

One process per sentence. That is slightly wasteful of start-up time and
entirely worth it: one WAV per sentence is what lets subtitle cues be
measured with ffprobe instead of estimated from word counts.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.constants import ErrorClass
from app.providers.speech.base import (
    SpeechCapabilities,
    SpeechProvider,
    SpeechProviderError,
    SpeechResult,
    VoiceSettings,
)
from app.services import piper as piper_tool

log = logging.getLogger(__name__)

#: A long sentence at a slow length_scale is still only seconds of audio.
#: Anything beyond this means piper has hung rather than that the line is
#: long.
SYNTH_TIMEOUT_S = 180
#: Below this a WAV is a header and nothing else.
MIN_WAV_BYTES = 1024


class PiperSpeechProvider(SpeechProvider):
    key = "piper"

    def __init__(self, exe_path: str = "") -> None:
        self._exe_path = exe_path

    def capabilities(self) -> SpeechCapabilities:
        return SpeechCapabilities(
            key=self.key,
            display_name="Piper",
            local=True,
            output_format="wav",
            automated=True,
            docs_url=piper_tool.PIPER_RELEASE_URL,
            licence=piper_tool.PIPER_ENGINE_LICENCE,
            licence_note=(
                "The engine is MIT licensed. Each voice carries its own separate "
                "licence, read from that voice's model card at install time."
            ),
            checked_on="2026-10-29",
            invocation_verified=True,
            notes=(
                "Runs on the CPU. No GPU, no torch, no onnxruntime wheel needed.",
                "One process per sentence, so every subtitle cue has a measured length.",
            ),
        )

    async def _resolve_exe(self) -> str:
        if self._exe_path:
            return self._exe_path
        status = await piper_tool.detect()
        if not status.available or status.exe_path is None:
            raise SpeechProviderError(status.message, ErrorClass.INVALID_INPUT)
        self._exe_path = status.exe_path
        return self._exe_path

    async def is_available(self) -> tuple[bool, str]:
        status = await piper_tool.detect(self._exe_path)
        return status.available, status.message

    def _arguments(self, voice: VoiceSettings, destination: Path) -> list[str]:
        args = [
            "--model", voice.model_path,
            "--output_file", str(destination),
            "--length_scale", f"{voice.length_scale:g}",
            "--noise_scale", f"{voice.noise_scale:g}",
            "--noise_w", f"{voice.noise_w:g}",
            "--sentence_silence", f"{voice.sentence_silence_s:g}",
        ]
        if voice.config_path:
            args += ["--config", voice.config_path]
        if voice.speaker_id is not None:
            args += ["--speaker", str(voice.speaker_id)]
        return args

    async def synthesise(
        self, text: str, voice: VoiceSettings, destination: Path
    ) -> SpeechResult:
        line = " ".join((text or "").split())
        if not line:
            raise SpeechProviderError(
                "There is nothing to say: the line is empty.", ErrorClass.INVALID_INPUT
            )
        if not voice.model_path or not Path(voice.model_path).is_file():
            raise SpeechProviderError(
                f"The voice file is missing: {voice.model_path or '(none set)'}",
                ErrorClass.INVALID_INPUT,
            )

        exe = await self._resolve_exe()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name so a failed run cannot leave a file
        # that a later pass would mistake for finished audio.
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)

        try:
            process = await asyncio.create_subprocess_exec(
                exe,
                *self._arguments(voice, partial),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SpeechProviderError(
                f"Piper could not be started: {exc}", ErrorClass.INVALID_INPUT
            ) from exc

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(line.encode("utf-8")), timeout=SYNTH_TIMEOUT_S
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            partial.unlink(missing_ok=True)
            raise SpeechProviderError(
                f"Piper did not finish within {SYNTH_TIMEOUT_S}s and was stopped.",
                ErrorClass.TRANSIENT,
            ) from None

        detail = stderr.decode("utf-8", "replace").strip()
        if process.returncode:
            partial.unlink(missing_ok=True)
            raise SpeechProviderError(
                f"Piper exited with code {process.returncode}. {detail[:400]}",
                ErrorClass.UNKNOWN,
            )
        if not partial.exists():
            raise SpeechProviderError(
                f"Piper reported success but wrote no file. {detail[:400]}",
                ErrorClass.UNKNOWN,
            )

        size = partial.stat().st_size
        if size < MIN_WAV_BYTES:
            partial.unlink(missing_ok=True)
            raise SpeechProviderError(
                f"Piper wrote only {size} bytes, which is an empty WAV. {detail[:200]}",
                ErrorClass.UNKNOWN,
            )

        destination.unlink(missing_ok=True)
        partial.replace(destination)
        return SpeechResult(
            path=destination,
            size_bytes=size,
            engine_version=piper_tool.PIPER_RELEASE_TAG,
        )
