"""Every narrator voice the Book Video page offers, with a ten-second demo of each.

* **Kokoro** voices are read from ``voices-v1.0.bin`` (English and Hindi
  ones; Kokoro's other languages are not narrated here). Offline.
* **Piper** voices are the ones installed under the Piper voices folder. Offline.
* **Edge** voices are a short list of Microsoft's neural voices for English,
  Urdu and Hindi. They need the internet while speaking.

Demos are made with the same code as a real book, cut to ten seconds with a
fade, normalised, and kept in ``voice-demos/<engine>-<voice>.mp3``, so each
is made once. ``make_missing`` makes the absent ones in the background and
``status()`` says how far it got.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.speech import split_sentences

log = logging.getLogger(__name__)

DEMO_DIR = "voice-demos"
DEMO_S = 10.0
LANGUAGE_NAMES = {"en": "English", "ur": "Urdu", "hi": "Hindi"}
DEMO_TEXT = {
    "en": ("Welcome back to the channel. Tonight's book will change the way you see the world. "
           "So sit back, relax, and let every chapter come alive, one idea at a time."),
    "ur": ("خوش آمدید۔ آج کی کتاب آپ کے سوچنے کا انداز بدل دے گی۔ "
           "تو آرام سے بیٹھیے، اور ہر باب کو ایک ایک کر کے سنیے۔"),
    "hi": ("स्वागत है। आज की किताब आपके सोचने का तरीका बदल देगी। "
           "तो आराम से बैठिए, और हर अध्याय को एक एक करके सुनिए।"),
}
#: Kokoro's own quality notes rank these highest for long narration.
KOKORO_PICKS = {"af_heart", "af_bella", "am_michael", "am_fenrir", "bm_george", "bm_fable", "bf_emma"}
KOKORO_ACCENTS = {"a": ("en", "American"), "b": ("en", "British"), "h": ("hi", "Hindi")}
EDGE_VOICES = (
    ("en", "en-US-AndrewMultilingualNeural", "male", "American", True),
    ("en", "en-US-BrianMultilingualNeural", "male", "American", True),
    ("en", "en-US-ChristopherNeural", "male", "American", False),
    ("en", "en-US-GuyNeural", "male", "American", False),
    ("en", "en-GB-RyanNeural", "male", "British", False),
    ("en", "en-GB-ThomasNeural", "male", "British", False),
    ("en", "en-US-AvaMultilingualNeural", "female", "American", True),
    ("en", "en-US-EmmaMultilingualNeural", "female", "American", False),
    ("en", "en-US-JennyNeural", "female", "American", False),
    ("en", "en-GB-SoniaNeural", "female", "British", False),
    ("en", "en-GB-LibbyNeural", "female", "British", False),
    ("ur", "ur-PK-AsadNeural", "male", "Pakistan", True),
    ("ur", "ur-PK-UzmaNeural", "female", "Pakistan", True),
    ("ur", "ur-IN-SalmanNeural", "male", "India", False),
    ("ur", "ur-IN-GulNeural", "female", "India", False),
    ("hi", "hi-IN-MadhurNeural", "male", "India", True),
    ("hi", "hi-IN-SwaraNeural", "female", "India", True),
)
SPEED_NOTE = {
    "piper": "Fast, offline. A 5-hour book is about an hour of speaking.",
    "kokoro": "Slow on this laptop: about 4 hours of waiting for each hour of audio.",
    "edge": "Fast, but it needs the internet the whole time.",
}
ENGINE_ORDER = {"piper": 0, "edge": 1, "kokoro": 2}


@dataclass
class Voice:
    engine: str
    name: str
    language: str
    gender: str
    accent: str
    recommended: bool = False

    @property
    def spec(self) -> str:
        return f"{self.engine}:{self.name}"

    @property
    def label(self) -> str:
        if self.engine == "kokoro":
            return self.name.split("_", 1)[-1].title()
        if self.engine == "edge":
            core = self.name.split("-", 2)[-1].replace("Neural", "")
            return core.replace("Multilingual", " (multilingual)")
        return self.name.replace("_", " ").replace("-medium", "").replace("-", " ")

    @property
    def offline(self) -> bool:
        return self.engine != "edge"

    def demo_path(self) -> Path:
        return demo_dir() / f"{self.engine}-{self.name}.mp3"

    def as_row(self) -> dict[str, Any]:
        demo = self.demo_path()
        return {
            "spec": self.spec, "engine": self.engine, "name": self.name, "label": self.label,
            "language": self.language, "language_name": LANGUAGE_NAMES.get(self.language, self.language),
            "gender": self.gender, "accent": self.accent, "offline": self.offline,
            "recommended": self.recommended, "note": SPEED_NOTE.get(self.engine, ""),
            "demo": demo.name if demo.is_file() else "",
        }


def demo_dir() -> Path:
    return get_settings().data_root / DEMO_DIR


def kokoro_voices() -> list[Voice]:
    from app.providers.speech import kokoro

    path = kokoro.models_dir() / kokoro.VOICES_FILE
    if not path.is_file():
        return []
    try:
        import numpy as np

        with np.load(path) as data:
            names = sorted(data.files)
    except Exception as exc:  # noqa: BLE001 - a damaged voices file just means no Kokoro voices
        log.warning("Kokoro voices could not be read: %s", exc)
        return []
    out = []
    for name in names:
        if len(name) < 4 or name[2] != "_" or name[0] not in KOKORO_ACCENTS:
            continue
        language, accent = KOKORO_ACCENTS[name[0]]
        gender = "female" if name[1] == "f" else "male"
        out.append(Voice("kokoro", name, language, gender, accent, name in KOKORO_PICKS))
    return out


def piper_voices() -> list[Voice]:
    from app.services import piper

    folder = piper.voices_dir()
    if not folder.is_dir():
        return []
    out = []
    for sub in sorted(folder.iterdir()):
        model = sub / f"{sub.name}.onnx"
        if not model.is_file():
            continue
        lang = sub.name[:2]
        if lang not in LANGUAGE_NAMES:
            continue
        accent = {"en_US": "American", "en_GB": "British"}.get(sub.name[:5], sub.name[:5])
        out.append(Voice("piper", sub.name, lang, "", accent, sub.name == "en_US-joe-medium"))
    return out


def edge_voices() -> list[Voice]:
    return [Voice("edge", name, lang, gender, accent, pick) for lang, name, gender, accent, pick in EDGE_VOICES]


def catalog() -> list[Voice]:
    return kokoro_voices() + piper_voices() + edge_voices()


def rows(language: str | None = None) -> list[dict[str, Any]]:
    voices = [v for v in catalog() if not language or v.language == language]
    voices.sort(key=lambda v: (
        v.language != "en", not v.recommended, ENGINE_ORDER.get(v.engine, 9), v.name,
    ))
    return [v.as_row() for v in voices]


def find(spec: str) -> Voice | None:
    return next((v for v in catalog() if v.spec == spec), None)


# --- Demos -----------------------------------------------------------------------------


async def make_demo(voice: Voice, ffmpeg_path: str) -> Path:
    """Speak the demo line with ``voice`` and keep ten seconds of it as an MP3."""
    from app.providers import speech as speech_registry
    from app.services import audiobook, book_video
    from app.services import piper as piper_tool

    destination = voice.demo_path()
    work = demo_dir() / "work"
    work.mkdir(parents=True, exist_ok=True)
    wav = work / f"{voice.engine}-{voice.name}.wav"
    spec = book_video.voice_for(voice.spec, 1.0)
    settings = audiobook.settings_from(spec)
    sentences = split_sentences(DEMO_TEXT.get(voice.language, DEMO_TEXT["en"]))
    cancel = threading.Event()
    if voice.engine == "piper":
        status = await piper_tool.detect()
        if not status.available or not status.exe_path:
            raise audiobook.AudiobookError(status.message or "Piper is not installed.")
        rate = audiobook._piper_sample_rate(spec)
        await asyncio.to_thread(audiobook._piper_chapter, status.exe_path, settings, sentences, wav, rate, cancel)
    else:
        provider = speech_registry.get(voice.engine)
        if provider is None:
            raise audiobook.AudiobookError(f"The {voice.engine} engine failed to load.")
        available, why = await provider.is_available()
        if not available:
            raise audiobook.AudiobookError(why)
        await audiobook._provider_chapter(provider, settings, sentences, wav, cancel)
    partial = destination.with_name(destination.stem + ".part.mp3")
    fade_at = DEMO_S - 0.8
    process = await asyncio.to_thread(
        subprocess.run,
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-t", f"{DEMO_S}",
         "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=out:st={fade_at}:d=0.8",
         "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "64k", str(partial)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    wav.unlink(missing_ok=True)
    wav.with_suffix(".words.json").unlink(missing_ok=True)
    if process.returncode or not partial.is_file():
        partial.unlink(missing_ok=True)
        detail = (process.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise audiobook.AudiobookError(f"The demo could not be encoded: {detail}")
    partial.replace(destination)
    return destination


_STATUS: dict[str, Any] = {"running": False, "done": 0, "total": 0, "current": "", "errors": []}
_LOCK = asyncio.Lock()


def status() -> dict[str, Any]:
    missing = sum(1 for v in catalog() if not v.demo_path().is_file())
    return {**_STATUS, "missing": missing}


async def make_missing(language: str | None = None, engines: tuple[str, ...] = ("kokoro", "piper", "edge")) -> dict:
    """Make every demo not yet on disk, one at a time. Safe to call twice; the second waits."""
    from app.services import ffmpeg as ffmpeg_tool

    if _LOCK.locked():
        return status()
    async with _LOCK:
        tools = await ffmpeg_tool.detect()
        todo = [v for v in catalog() if v.engine in engines and (not language or v.language == language)
                and not v.demo_path().is_file()]
        _STATUS.update(running=True, done=0, total=len(todo), current="", errors=[])
        try:
            if not tools.ffmpeg_path:
                _STATUS["errors"].append(tools.message or "FFmpeg is not installed.")
                return status()
            for voice in todo:
                _STATUS["current"] = voice.spec
                try:
                    await make_demo(voice, tools.ffmpeg_path)
                except Exception as exc:  # noqa: BLE001 - one voice failing should not stop the rest
                    _STATUS["errors"].append(f"{voice.spec}: {str(exc)[:160]}")
                    log.warning("Demo for %s failed: %s", voice.spec, exc)
                _STATUS["done"] += 1
        finally:
            _STATUS.update(running=False, current="")
    return status()
