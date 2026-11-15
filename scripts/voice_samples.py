"""Speak one passage with every candidate voice, to choose a narrator by ear.

    python scripts/voice_samples.py
    python scripts/voice_samples.py --engine kokoro --lang en

Each sample goes through the same code as a real book (one Piper process per
chapter, Kokoro and Edge a sentence at a time, then the loudness-normalised
MP3 join), so what is heard here is what a ten-hour book will sound like.
The samples land in ``voice-samples/`` under the data root with an
``index.html`` that plays them side by side, and the compute time of each is
measured, because that decides whether a voice can keep up with a daily book.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.providers import speech as speech_registry  # noqa: E402
from app.services import audiobook  # noqa: E402
from app.services import ffmpeg as ffmpeg_tool  # noqa: E402
from app.services import piper as piper_tool  # noqa: E402
from app.services.speech import split_sentences  # noqa: E402

# Marcus Aurelius, Meditations II.1, George Long's 1862 translation, and the
# opening of A Scandal in Bohemia (1891). Both are public domain.
PASSAGES: dict[str, str] = {
    "en": (
        "You are listening to Meditations, by Marcus Aurelius. Book two. "
        "Begin the morning by saying to thyself, I shall meet with the busy-body, "
        "the ungrateful, arrogant, deceitful, envious, unsocial. All these things "
        "happen to them by reason of their ignorance of what is good and evil. "
        "For we are made for co-operation, like feet, like hands, like eyelids. "
        "To Sherlock Holmes she is always the woman. I have seldom heard him "
        "mention her under any other name. \"Wedlock suits you,\" he remarked. "
        "\"I think, Watson, that you have put on seven and a half pounds since I saw you.\""
    ),
    "ur": (
        "آپ سن رہے ہیں ایک نئی آڈیو کتاب۔ کہتے ہیں کہ کتاب انسان کی سب سے اچھی دوست ہوتی ہے۔ "
        "وہ کبھی شکایت نہیں کرتی، کبھی ساتھ نہیں چھوڑتی، اور ہر بار کچھ نیا سکھا جاتی ہے۔ "
        "آج کی کہانی ایک ایسے نوجوان کی ہے جو ایک چھوٹے سے گاؤں سے بڑے شہر آیا، "
        "اپنے خواب پورے کرنے کے لیے۔ راستہ مشکل تھا، مگر اس کا حوصلہ اس سے بھی بڑا تھا۔"
    ),
    "hi": (
        "आप सुन रहे हैं एक नई ऑडियोबुक। कहते हैं कि किताब इंसान की सबसे अच्छी दोस्त होती है। "
        "वह कभी शिकायत नहीं करती, कभी साथ नहीं छोड़ती, और हर बार कुछ नया सिखा जाती है। "
        "आज की कहानी एक ऐसे नौजवान की है जो एक छोटे से गाँव से बड़े शहर आया, "
        "अपने सपने पूरे करने के लिए। रास्ता मुश्किल था, मगर उसका हौसला उससे भी बड़ा था।"
    ),
}

LANGUAGE_NAMES = {"en": "English", "ur": "Urdu", "hi": "Hindi"}


@dataclass(frozen=True)
class Candidate:
    lang: str
    engine: str
    voice: str
    label: str


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("en", "piper", "en_US-norman-medium", "male, US"),
    Candidate("en", "piper", "en_US-joe-medium", "male, US"),
    Candidate("en", "piper", "en_US-john-medium", "male, US"),
    Candidate("en", "piper", "en_US-kristin-medium", "female, US"),
    Candidate("en", "piper", "en_GB-cori-medium", "female, UK"),
    Candidate("en", "kokoro", "am_michael", "male, US"),
    Candidate("en", "kokoro", "am_fenrir", "male, US"),
    Candidate("en", "kokoro", "bm_george", "male, UK"),
    Candidate("en", "kokoro", "bm_fable", "male, UK"),
    Candidate("en", "kokoro", "af_heart", "female, US"),
    Candidate("en", "kokoro", "bf_emma", "female, UK"),
    Candidate("en", "edge", "en-US-AndrewMultilingualNeural", "male, US"),
    Candidate("en", "edge", "en-US-BrianMultilingualNeural", "male, US"),
    Candidate("en", "edge", "en-US-ChristopherNeural", "male, US"),
    Candidate("en", "edge", "en-GB-RyanNeural", "male, UK"),
    Candidate("en", "edge", "en-US-AvaMultilingualNeural", "female, US"),
    Candidate("en", "edge", "en-GB-SoniaNeural", "female, UK"),
    Candidate("ur", "edge", "ur-PK-AsadNeural", "male, Pakistan"),
    Candidate("ur", "edge", "ur-PK-UzmaNeural", "female, Pakistan"),
    Candidate("ur", "edge", "ur-IN-SalmanNeural", "male, India"),
    Candidate("hi", "edge", "hi-IN-MadhurNeural", "male"),
    Candidate("hi", "edge", "hi-IN-SwaraNeural", "female"),
    Candidate("hi", "kokoro", "hm_omega", "male"),
    Candidate("hi", "kokoro", "hf_alpha", "female"),
)


def voice_dict(candidate: Candidate) -> dict:
    if candidate.engine == "piper":
        voice = audiobook.piper_voice(candidate.voice)
        voice["sentence_silence_s"] = 0.35
        return voice
    return {
        "engine": candidate.engine,
        "name": candidate.voice,
        "model_path": "",
        "config_path": "",
        "length_scale": 1.0,
        "sentence_silence_s": 0.35,
    }


async def make_sample(candidate: Candidate, ffmpeg_path: str, out: Path) -> tuple[float, float]:
    """Write one MP3. Return (seconds of audio, seconds of compute)."""
    voice = voice_dict(candidate)
    settings = audiobook.settings_from(voice)
    sentences = split_sentences(PASSAGES[candidate.lang])
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    stem = f"{candidate.lang}-{candidate.engine}-{candidate.voice}"
    wav = work / f"{stem}.wav"
    cancel = threading.Event()
    started = time.perf_counter()
    if candidate.engine == "piper":
        status = await piper_tool.detect()
        if not status.available or not status.exe_path:
            raise audiobook.AudiobookError(status.message or "Piper is not installed.")
        rate = audiobook._piper_sample_rate(voice)
        audio_s = await asyncio.to_thread(
            audiobook._piper_chapter, status.exe_path, settings, sentences, wav, rate, cancel
        )
    else:
        provider = speech_registry.get(candidate.engine)
        if provider is None:
            raise audiobook.AudiobookError(f"The {candidate.engine} engine failed to load.")
        available, why = await provider.is_available()
        if not available:
            raise audiobook.AudiobookError(why)
        audio_s = await audiobook._provider_chapter(provider, settings, sentences, wav, cancel)
    compute_s = time.perf_counter() - started
    await asyncio.to_thread(audiobook.join_mp3, ffmpeg_path, [wav], out / f"{stem}.mp3", work)
    wav.unlink(missing_ok=True)
    return audio_s, compute_s


def write_index(out: Path, rows: list[dict]) -> Path:
    sections = []
    for lang, name in LANGUAGE_NAMES.items():
        items = [row for row in rows if row["lang"] == lang]
        if not items:
            continue
        lines = []
        for row in items:
            if row.get("error"):
                detail = f"<span class='err'>failed: {html.escape(row['error'])}</span>"
            else:
                factor = row["compute_s"] / row["audio_s"] if row["audio_s"] else 0
                ten_hours = factor * 10
                local = row["engine"] != "edge"
                speed = (
                    f"{factor:.2f}s of compute per second of audio, so a 10-hour book takes "
                    f"about {ten_hours:.1f} hours on this laptop"
                    if local
                    else f"{row['compute_s']:.0f}s over the network (Microsoft's servers)"
                )
                detail = (
                    f"<audio controls preload='none' src='{html.escape(row['file'])}'></audio>"
                    f"<div class='meta'>{speed}</div>"
                )
            lines.append(
                f"<li><b>{html.escape(row['engine'].title())}</b> - {html.escape(row['voice'])} "
                f"<i>({html.escape(row['label'])})</i><br>{detail}</li>"
            )
        sections.append(f"<h2>{name}</h2><ol>{''.join(lines)}</ol>")
    page = out / "index.html"
    page.write_text(
        "<!doctype html><meta charset='utf-8'><title>Voice samples</title>"
        "<style>body{font-family:sans-serif;max-width:900px;margin:2em auto}"
        "li{margin:1em 0}audio{width:100%}.meta{color:#666;font-size:.9em}.err{color:#b00}</style>"
        "<h1>Narrator samples</h1><p>Same passage, same pipeline as a full book. "
        "Pick a favourite per language.</p>" + "".join(sections),
        encoding="utf-8",
    )
    return page


async def main_async(args: argparse.Namespace) -> int:
    tools = await ffmpeg_tool.detect()
    if not tools.ffmpeg_path:
        print(tools.message or "FFmpeg is not installed.", file=sys.stderr)
        return 1
    out = get_settings().data_root / "voice-samples"
    out.mkdir(parents=True, exist_ok=True)
    chosen = [
        c for c in CANDIDATES
        if (not args.engine or c.engine == args.engine) and (not args.lang or c.lang == args.lang)
        and (not args.voice or c.voice == args.voice)
    ]
    rows: list[dict] = []
    for index, candidate in enumerate(chosen, start=1):
        row = {"lang": candidate.lang, "engine": candidate.engine,
               "voice": candidate.voice, "label": candidate.label}
        print(f"[{index}/{len(chosen)}] {candidate.lang} {candidate.engine} {candidate.voice} ...", flush=True)
        try:
            audio_s, compute_s = await make_sample(candidate, tools.ffmpeg_path, out)
            row.update(audio_s=audio_s, compute_s=compute_s,
                       file=f"{candidate.lang}-{candidate.engine}-{candidate.voice}.mp3")
            print(f"    {audio_s:.1f}s of audio in {compute_s:.1f}s", flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad voice should not stop the rest
            row["error"] = str(exc)[:300]
            print(f"    failed: {row['error']}", flush=True)
        rows.append(row)
    results = out / "results.json"
    previous = json.loads(results.read_text(encoding="utf-8")) if results.is_file() else []
    fresh = {(r["lang"], r["engine"], r["voice"]) for r in rows}
    order = {(c.lang, c.engine, c.voice): i for i, c in enumerate(CANDIDATES)}
    rows = [r for r in previous if (r["lang"], r["engine"], r["voice"]) not in fresh] + rows
    rows.sort(key=lambda r: order.get((r["lang"], r["engine"], r["voice"]), len(order)))
    results.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    page = write_index(out, rows)
    print(f"Open {page}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Speak one passage with every candidate voice.")
    parser.add_argument("--engine", choices=("piper", "kokoro", "edge"), default=None)
    parser.add_argument("--lang", choices=tuple(LANGUAGE_NAMES), default=None)
    parser.add_argument("--voice", default=None, help="Only this voice, e.g. am_michael.")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
