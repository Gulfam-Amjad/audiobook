"""Build five vertical Mighty Chapters shorts.

Illustrated history, original music, burned-in captions, 1080x1920.
Facts stay in the narration. The pictures are original art, and the
upload notes say so.

    python scripts/make_mighty_shorts.py
    python scripts/make_mighty_shorts.py --only 01
    python scripts/make_mighty_shorts.py --pilot
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(
    r"C:\Users\Gulfam\.cursor\projects\c-Users-Gulfam-Desktop-youtube\assets"
)
WORK = Path(r"D:\AIVideoManager\films\mighty-chapters-shorts")
OUT = ROOT / "mighty-chapters-shorts"
PIPER = Path(r"D:\AIVideoManager\tools\piper\piper.exe")
VOICE = Path(r"D:\AIVideoManager\voices\en_US-norman-medium\en_US-norman-medium.onnx")
VOICE_CONFIG = VOICE.with_suffix(".onnx.json")
FONT_BODY = Path(r"D:\AIVideoManager\fonts\Inter.ttf")
FONT_DISPLAY = Path(r"D:\AIVideoManager\fonts\BebasNeue-Regular.ttf")

FPS = 30
SR = 48000
FRAME_SAMPLES = SR // FPS  # 1600
WIDTH, HEIGHT = 1080, 1920
TARGET_S = 53.0
MAX_S = 59.0
NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Spoken line can differ from the on-screen caption when a name is hard to say.
Line = dict


def L(text, image, move="push", style="body", say=None, accent=False):
    return {
        "text": text,
        "say": say or text,
        "image": image,
        "move": move,
        "style": style,
        "accent": accent,
    }


SHORTS: list[dict] = [
    {
        "id": "01",
        "slug": "01-the-floor-that-opened",
        "chapter": "CHAPTER 01",
        "title": "The Floor That Opened",
        "youtube_title": "Lions Rose Out of the Colosseum Floor",
        "mood": "rome",
        "pinned": "Would you have watched the fight, or tried to see what was under the floor?",
        "description": """Lions did not walk out of a gate. Under the Colosseum, lifts brought animals up through trapdoors in a wooden floor covered with sand. Sand, in Latin, is harena. That is where the word arena comes from.

The basement was added under the emperor Domitian. Ancient writers, including Cassius Dio, describe a naval show at the opening games, before that basement made flooding the floor impossible.

Illustrated. These pictures are original art for Mighty Chapters, not footage from the time.

Every chapter holds a story.

#Shorts #History #Colosseum #AncientRome #MightyChapters""",
        "lines": [
            L("Lions did not walk into the Colosseum.", "mc-s1-lift.jpg", "push", "hook", accent=True),
            L("The floor opened, and they rose out of the dark.", "mc-s1-lift.jpg", "push"),
            L("The arena was wood, covered in sand.", "mc-s1-sand.jpg", "right"),
            L(
                "Sand, in Latin, is harena. That is why it is called an arena.",
                "mc-s1-sand.jpg",
                "left",
                say="Sand, in Latin, is hah-ray-nah. That is why it is called an arena.",
            ),
            L(
                "Under the emperor Domitian, a basement was built beneath that floor.",
                "mc-s1-hypogeum.jpg",
                "push",
                say="Under the emperor Doh-mish-un, a basement was built beneath that floor.",
            ),
            L("Two levels of cages, ramps, and lifts, all hidden from the crowd.", "mc-s1-hypogeum.jpg", "left"),
            L("Animals waited in the dark until a trapdoor opened above them.", "mc-s1-lift.jpg", "rise"),
            L("The audience never saw the machine. They only saw the animal appear.", "mc-s1-crowd.jpg", "pull"),
            L("Before that basement existed, the same arena could be flooded.", "mc-s1-exterior.jpg", "right"),
            L("One building. Two impossible shows.", "mc-s1-exterior.jpg", "push", "hook"),
            L("The crowd looked down. They never looked under.", "mc-s1-today.jpg", "pull", "hook", accent=True),
        ],
    },
    {
        "id": "02",
        "slug": "02-the-room-with-no-door",
        "chapter": "CHAPTER 02",
        "title": "The Room With No Door",
        "youtube_title": "A Room in the Great Pyramid No One Has Entered",
        "mood": "void",
        "pinned": "Should they find a way into the void, or leave it sealed?",
        "description": """In 2017 a team called ScanPyramids found a void at least 30 meters long above the Grand Gallery inside Khufu's pyramid. They did not drill. Cosmic particles called muons passed through the stone, and the journal Nature published the result.

In 2023 a camera saw a smaller corridor behind the north face. That corridor is not the big void. The big void still has no door.

Illustrated. These pictures are original art for Mighty Chapters, not footage from inside the pyramid.

Every chapter holds a story.

#Shorts #History #Pyramid #AncientEgypt #MightyChapters""",
        "lines": [
            L("In 2017, scientists found a room inside the Great Pyramid.", "mc-s2-night.jpg", "push", "hook", accent=True),
            L("No one has ever walked into it.", "mc-s2-void.jpg", "rise", "hook"),
            L(
                "It sits above the Grand Gallery, in Khufu's pyramid at Giza.",
                "mc-s2-gallery.jpg",
                "rise",
                say="It sits above the Grand Gallery, in Koo-foo's pyramid at Giza.",
            ),
            L("The team was called ScanPyramids.", "mc-s2-muons.jpg", "left", say="The team was called Scan Pyramids."),
            L(
                "They did not drill. They watched cosmic particles, called muons, pass through the stone.",
                "mc-s2-muons.jpg",
                "push",
                say="They did not drill. They watched cosmic particles, called myoo-ons, pass through the stone.",
            ),
            L("The void is at least thirty meters long.", "mc-s2-void.jpg", "pull"),
            L("The science journal Nature published the result.", "mc-s2-gallery.jpg", "right"),
            L("In 2023, a camera saw a smaller corridor behind the north face.", "mc-s2-corridor.jpg", "push"),
            L("That is not this room.", "mc-s2-void.jpg", "rise", "hook"),
            L("The big void still has no door.", "mc-s2-seal.jpg", "pull", "hook", accent=True),
            L(
                "Four and a half thousand years later, the largest pyramid on Earth is still keeping a secret.",
                "mc-s2-night.jpg",
                "push",
                "hook",
            ),
        ],
    },
    {
        "id": "03",
        "slug": "03-tamam-shud",
        "chapter": "CHAPTER 03",
        "title": "Tamam Shud",
        "youtube_title": "The Dead Man and the Two-Word Code",
        "mood": "noir",
        "pinned": "The code is still unsolved. What do you think it was for?",
        "description": """On December 1, 1948, a man was found dead on Somerton Beach in Australia. The labels were cut out of his clothes. A hidden pocket held a scrap torn from the Rubaiyat, a book of Persian poems. The words Tamam Shud mean "it is ended."

Another copy of the book held a phone number and a code that has never been broken. In 2022, DNA research pointed to an electrical engineer named Carl Webb. The code is still unread.

Illustrated. These pictures are original art for Mighty Chapters, not crime-scene photographs.

Every chapter holds a story.

#Shorts #Mystery #History #Unsolved #MightyChapters""",
        "lines": [
            L("On December first, 1948, a man was found dead on an Australian beach.", "mc-s3-beach.jpg", "push", "hook", accent=True),
            L("He had no wallet. Every label had been cut out of his clothes.", "mc-s3-labels.jpg", "right"),
            L("In a hidden pocket, police found a scrap of paper.", "mc-s3-pocket.jpg", "push"),
            L("Two words. Tamam Shud.", "mc-s3-book.jpg", "left", "hook", say="Two words. Tah-mahm shood."),
            L("They mean, it is ended.", "mc-s3-book.jpg", "pull"),
            L(
                "The scrap was torn from a book of Persian poems called the Rubaiyat.",
                "mc-s3-book.jpg",
                "right",
                say="The scrap was torn from a book of Persian poems called the Roo-bye-aht.",
            ),
            L("Inside another copy, detectives found a phone number and a code.", "mc-s3-code.jpg", "push"),
            L("The code has never been broken.", "mc-s3-code.jpg", "left", "hook"),
            L("For seventy-four years, the man had no name.", "mc-s3-beach.jpg", "pull"),
            L("In 2022, DNA research pointed to an engineer named Carl Webb.", "mc-s3-lab.jpg", "push", accent=True),
            L("They found his name. They still do not know what the code says.", "mc-s3-beach.jpg", "rise", "hook"),
        ],
    },
    {
        "id": "04",
        "slug": "04-the-dancing-plague",
        "chapter": "CHAPTER 04",
        "title": "The Dancing Plague",
        "youtube_title": "The City That Could Not Stop Dancing",
        "mood": "plague",
        "pinned": "Poisoned bread, or a fear that spread from person to person? Which one do you believe?",
        "description": """In July 1518, in Strasbourg, a woman the chronicles call Frau Troffea began to dance and could not stop. Dozens joined her, then hundreds. Doctors blamed overheated blood, and the city built a stage and hired musicians, believing more dancing was the cure.

Contemporary writers say some of the dancers collapsed and died. Modern historians do not agree on a number, or on a single cause. Ergot in the rye is one idea. A fear that spread through a starving city is another.

Illustrated. These pictures are original art for Mighty Chapters, not drawings from 1518.

Every chapter holds a story.

#Shorts #History #Medieval #Mystery #MightyChapters""",
        "lines": [
            L(
                "In July 1518, a woman stepped into a street in Strasbourg and began to dance.",
                "mc-s4-woman.jpg",
                "push",
                "hook",
                say="In July 1518, a woman stepped into a street in Strasburg and began to dance.",
                accent=True,
            ),
            L("She could not stop.", "mc-s4-woman.jpg", "rise", "hook"),
            L(
                "Her name was Frau Troffea.",
                "mc-s4-woman.jpg",
                "left",
                say="Her name was Frow Troff-eh-ah.",
            ),
            L("Within a week, dozens of people had joined her.", "mc-s4-crowd.jpg", "right"),
            L("Within a month, the city chronicles say hundreds were dancing.", "mc-s4-crowd.jpg", "push"),
            L("Doctors blamed overheated blood.", "mc-s4-stage.jpg", "left"),
            L("And their cure was more dancing.", "mc-s4-stage.jpg", "pull", "hook"),
            L("So the city built a stage, and hired musicians.", "mc-s4-stage.jpg", "right"),
            L("People collapsed. The chronicles say some did not get back up.", "mc-s4-feet.jpg", "rise", accent=True),
            L("Then it ended, as suddenly as it began.", "mc-s4-empty.jpg", "pull"),
            L("Poisoned bread. A fear that jumped from body to body. Nobody agrees.", "mc-s4-chronicle.jpg", "left"),
            L("The records are real. The explanation is still a blank page.", "mc-s4-chronicle.jpg", "push", "hook"),
        ],
    },
    {
        "id": "05",
        "slug": "05-the-hammer-and-the-feather",
        "chapter": "CHAPTER 05",
        "title": "The Hammer and the Feather",
        "youtube_title": "A Hammer and a Feather Fell Together on the Moon",
        "mood": "moon",
        "pinned": "If you were standing there, what else would you drop?",
        "description": """On August 2, 1971, near the end of Apollo 15's last moonwalk, Commander David Scott dropped a geology hammer and a falcon feather. With no air to slow the feather, they landed together. Galileo had argued, centuries earlier, that without air the heavier object does not win.

Scott's words were, "How about that." The feather is still on the Moon.

Illustrated. These pictures are original art for Mighty Chapters, not the NASA film of the drop. The real footage is in the Apollo 15 record.

Every chapter holds a story.

#Shorts #Space #Apollo #Science #MightyChapters""",
        "lines": [
            L("On the Moon, an astronaut dropped a hammer and a feather.", "mc-s5-hands.jpg", "push", "hook", accent=True),
            L("They hit the ground at the same time.", "mc-s5-fall.jpg", "rise", "hook"),
            L("August second, 1971. Apollo 15.", "mc-s5-scott.jpg", "left"),
            L("Commander David Scott stood in the lunar dust.", "mc-s5-scott.jpg", "push"),
            L("In one hand, a geology hammer. In the other, a falcon feather.", "mc-s5-hands.jpg", "right"),
            L("Galileo had argued this, centuries earlier.", "mc-s5-earth.jpg", "pull"),
            L("Take the air away, and weight does not decide who falls first.", "mc-s5-fall.jpg", "rise"),
            L("Scott let go.", "mc-s5-fall.jpg", "push", "hook", accent=True),
            L("On a silent camera, they landed as one.", "mc-s5-landed.jpg", "pull"),
            L("Centuries of argument, settled in the dust.", "mc-s5-landed.jpg", "left"),
            L("He said, how about that.", "mc-s5-scott.jpg", "push"),
            L("The feather is still up there.", "mc-s5-feather.jpg", "rise", "hook"),
        ],
    },
]


def run(cmd: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NOWINDOW,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace")[-1800:]
        raise RuntimeError(f"{cmd[0]} failed ({result.returncode})\n{err}")
    return result


def ff_escape(path: Path) -> str:
    return path.as_posix().replace(":", r"\:")


def wrap(text: str, limit: int) -> str:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if current and len(trial) > limit:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return "\n".join(lines)


def synthesise(text: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part.wav")
    partial.unlink(missing_ok=True)
    cmd = [
        str(PIPER),
        "--model", str(VOICE),
        "--config", str(VOICE_CONFIG),
        "--output_file", str(partial),
        "--length_scale", "0.93",
        "--noise_scale", "0.667",
        "--noise_w", "0.8",
        "--sentence_silence", "0.02",
    ]
    result = subprocess.run(
        cmd,
        input=text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NOWINDOW,
        check=False,
    )
    if result.returncode != 0 or not partial.exists():
        err = result.stderr.decode("utf-8", "replace")[-800:]
        raise RuntimeError(f"Piper failed on {text!r}\n{err}")
    partial.replace(destination)


def load_wav(path: Path) -> np.ndarray:
    raw = run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-ar", str(SR), "-ac", "2", "-f", "s16le", "-",
        ]
    ).stdout
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size == 0:
        return np.zeros((1, 2), dtype=np.float32)
    return audio.reshape(-1, 2)


def write_wav(path: Path, stereo: np.ndarray) -> None:
    pcm = np.clip(stereo, -1.0, 1.0)
    pcm_i16 = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(pcm_i16.tobytes())


def fade_edges(audio: np.ndarray, seconds: float = 0.008) -> None:
    n = min(int(seconds * SR), max(1, len(audio) // 8))
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    audio[:n] *= ramp[:, None]
    audio[-n:] *= ramp[::-1, None]


def plan_timing(voice_lengths: list[float]) -> tuple[list[float], list[float]]:
    count = len(voice_lengths)
    leads = [0.16] + [0.0] * (count - 1)
    pads = [0.18] * count
    pads[0] = 0.24
    pads[-1] = 0.85
    total = sum(leads) + sum(voice_lengths) + sum(pads)
    extra = TARGET_S - total
    if extra > 0:
        weights = np.array([1.25] + [1.0] * (count - 2) + [2.1], dtype=np.float64)
        weights /= weights.sum()
        for index in range(count):
            pads[index] = min(1.25, pads[index] + extra * float(weights[index]))
    else:
        deficit = -extra
        for index in range(count - 1):
            room = max(0.0, pads[index] - 0.08)
            take = min(room, deficit)
            pads[index] -= take
            deficit -= take
        if deficit > 0:
            pads[-1] = max(0.45, pads[-1] - deficit)
    total = sum(leads) + sum(voice_lengths) + sum(pads)
    if total > MAX_S:
        scale = (MAX_S - sum(voice_lengths) - sum(leads)) / max(sum(pads), 0.01)
        scale = max(0.35, min(1.0, scale))
        pads = [pad * scale for pad in pads]
    return leads, pads


def motion_filter(move: str, frames: int) -> str:
    last = max(frames - 1, 1)
    if move == "push":
        z = f"min(1.04+0.14*on/{last}\\,1.18)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif move == "pull":
        z = f"max(1.18-0.14*on/{last}\\,1.04)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif move == "right":
        z = "1.12"
        x = f"(iw-iw/zoom)*on/{last}"
        y = "(ih-ih/zoom)*0.30"
    elif move == "left":
        z = "1.12"
        x = f"(iw-iw/zoom)*(1-on/{last})"
        y = "(ih-ih/zoom)*0.36"
    elif move == "rise":
        z = "1.14"
        x = "(iw-iw/zoom)*0.5"
        y = f"(ih-ih/zoom)*(1-on/{last})"
    else:
        z, x, y = "1.08", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return (
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s=1080x1920:fps=30,"
        "eq=contrast=1.05:brightness=-0.02:saturation=0.86,vignette=PI/5"
    )


def drawtext(font: Path, textfile: Path, size: int, color: str, y: str, box: bool, border: int) -> str:
    parts = [
        f"fontfile='{ff_escape(font)}'",
        f"textfile='{ff_escape(textfile)}'",
        f"fontsize={size}",
        f"fontcolor={color}",
        f"borderw={border}",
        "bordercolor=black@0.82",
        "x=(w-text_w)/2",
        f"y={y}",
        "line_spacing=8",
    ]
    if box:
        parts.append("box=1")
        parts.append("boxcolor=black@0.46")
        parts.append("boxborderw=18")
    return "drawtext=" + ":".join(parts)


def render_shot(image: Path, line: dict, frames: int, shot_dir: Path, index: int, chapter: str, last: bool) -> Path:
    duration = frames / FPS
    shot = shot_dir / f"shot-{index:02d}.mp4"
    cap_path = shot_dir / f"cap-{index:02d}.txt"
    brand_path = shot_dir / "brand.txt"
    chapter_path = shot_dir / "chapter.txt"
    tag_path = shot_dir / "tag.txt"
    brand_path.write_text("MIGHTY CHAPTERS", encoding="utf-8")
    chapter_path.write_text(chapter, encoding="utf-8")
    tag_path.write_text("EVERY CHAPTER HOLDS A STORY", encoding="utf-8")
    limit = 22 if line["style"] == "hook" else 32
    wrapped = wrap(line["text"], limit)
    cap_path.write_text(wrapped, encoding="utf-8")
    rows = wrapped.count("\n") + 1

    filters = [motion_filter(line["move"], frames)]
    filters.append(drawtext(FONT_DISPLAY, brand_path, 36, "0xE8C872", "86", False, 3))
    filters.append(drawtext(FONT_DISPLAY, chapter_path, 24, "0xF4EBD0", "132", False, 2))
    if line["style"] == "hook":
        size = 58 if rows <= 2 else 46 if rows == 3 else 40
        y = "h*0.56" if last and rows >= 4 else "h*0.62"
        filters.append(drawtext(FONT_DISPLAY, cap_path, size, "0xF6E2A8", y, False, 6))
    else:
        size = 44 if rows <= 3 else 38
        filters.append(drawtext(FONT_BODY, cap_path, size, "0xF7F1E4", "h*0.64", True, 0))
    if last:
        filters.append(drawtext(FONT_DISPLAY, tag_path, 30, "0xE8C872", "h*0.88", False, 3))

    run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-loop", "1", "-framerate", "30", "-i", str(image),
            "-vf", ",".join(filters),
            "-t", f"{duration:.4f}",
            "-r", str(FPS),
            "-fps_mode", "cfr",
            "-frames:v", str(frames),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-bf", "0",
            "-g", "30",
            "-an",
            str(shot),
        ]
    )
    return shot


def lowpass(samples: np.ndarray, cutoff: float) -> np.ndarray:
    dt = 1.0 / SR
    rc = 1.0 / (2.0 * math.pi * cutoff)
    alpha = dt / (rc + dt)
    out = np.empty_like(samples)
    acc = 0.0
    for index, sample in enumerate(samples):
        acc += alpha * (float(sample) - acc)
        out[index] = acc
    return out


def kick(seconds: float = 0.22, start_hz: float = 92.0) -> np.ndarray:
    count = int(SR * seconds)
    time = np.arange(count) / SR
    frequency = start_hz * np.exp(-time * 14.0) + 38.0
    phase = 2.0 * np.pi * np.cumsum(frequency) / SR
    return (np.sin(phase) * np.exp(-time * 9.0)).astype(np.float32)


def compose_music(mood: str, seconds: float, hits: list[float]) -> np.ndarray:
    count = int(seconds * SR)
    time = np.arange(count) / SR
    drone = (
        0.22 * np.sin(2 * np.pi * 73.42 * time)
        + 0.10 * np.sin(2 * np.pi * 110.00 * time)
        + 0.04 * np.sin(2 * np.pi * 146.83 * time)
    )
    if mood == "rome":
        bed = drone * (0.55 + 0.08 * np.sin(2 * np.pi * 0.12 * time))
        pulse = np.zeros(count, dtype=np.float32)
        hit = kick(0.20, 78.0) * 0.55
        step = int(1.5 * SR)
        for start in range(0, count - len(hit), step):
            pulse[start:start + len(hit)] += hit
        bed = bed + pulse
        cutoff = 700
    elif mood == "void":
        shimmer = 0.05 * np.sin(2 * np.pi * 220.3 * time + 2.0 * np.sin(2 * np.pi * 0.07 * time))
        bed = drone * (0.42 + 0.18 * np.sin(2 * np.pi * 0.06 * time)) + shimmer
        cutoff = 1100
    elif mood == "noir":
        bed = np.zeros(count, dtype=np.float64)
        tones = [73.42, 87.31, 98.00, 110.00, 130.81]
        pluck_n = int(0.9 * SR)
        pluck_t = np.arange(pluck_n) / SR
        step = int(1.35 * SR)
        for index, start in enumerate(range(0, count - pluck_n, step)):
            freq = tones[index % len(tones)]
            pluck = np.sin(2 * np.pi * freq * pluck_t) * np.exp(-pluck_t * 3.2)
            bed[start:start + pluck_n] += pluck * 0.28
        bed += drone * 0.18
        cutoff = 900
    elif mood == "plague":
        dissonant = 0.12 * np.sin(2 * np.pi * 77.78 * time)
        bed = drone * 0.28 + dissonant
        pulse = np.zeros(count, dtype=np.float32)
        thump = kick(0.12, 60.0) * 0.4
        pair = int(0.18 * SR)
        step = int(60 / 78 * SR)  # about 78 beats a minute
        for start in range(0, count - pair - len(thump), step):
            pulse[start:start + len(thump)] += thump
            second = start + pair
            pulse[second:second + len(thump)] += thump * 0.7
        bed = bed + pulse
        cutoff = 520
    else:  # moon
        bed = (
            0.16 * np.sin(2 * np.pi * 73.42 * time)
            + 0.10 * np.sin(2 * np.pi * 110.0 * time)
            + 0.08 * np.sin(2 * np.pi * 185.0 * time)
        )
        bed *= 0.55 + 0.45 * np.clip(time / max(seconds, 1.0), 0.0, 1.0)
        cutoff = 1400
    filtered = lowpass(bed.astype(np.float32), cutoff)
    for hit_at in hits:
        start = int(hit_at * SR)
        boom = kick(0.45, 55.0) * 0.85
        end = min(count, start + len(boom))
        if 0 <= start < count:
            filtered[start:end] += boom[: end - start]
    filtered = lowpass(filtered, 1200)
    left = filtered
    right = np.roll(filtered, int(0.011 * SR))
    stereo = np.stack([left, right], axis=1).astype(np.float32)
    peak = float(np.max(np.abs(stereo))) or 1.0
    stereo *= 0.32 / peak
    fade = int(0.35 * SR)
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    stereo[:fade] *= ramp[:, None]
    stereo[-fade:] *= ramp[::-1, None]
    return stereo


def duck(music: np.ndarray, voice: np.ndarray) -> np.ndarray:
    energy = np.mean(np.abs(voice), axis=1)
    window = np.ones(int(0.06 * SR), dtype=np.float32)
    window /= window.sum()
    envelope = np.convolve(energy, window, mode="same")
    peak = float(np.max(envelope)) or 1.0
    amount = np.clip(envelope / peak * 2.4, 0.0, 1.0)
    gain = (1.0 - 0.78 * amount).astype(np.float32)
    return music * gain[:, None]


def probe(path: Path) -> dict:
    raw = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size",
            "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels",
            "-of", "json",
            str(path),
        ]
    ).stdout
    return json.loads(raw)


def loudness(path: Path) -> str:
    result = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(path), "-af", "loudnorm=print_format=summary", "-f", "null", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=NOWINDOW,
        check=False,
    )
    text = result.stderr.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if "loudnorm" in line.lower() or "Input Integrated" in line or "I:" in line]
    for line in text.splitlines():
        if "Input Integrated" in line or line.strip().startswith("I:"):
            return line.strip()
    return "unmeasured"


def build(short: dict, pilot: bool) -> dict:
    work = WORK / "work" / short["slug"]
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    voices: list[np.ndarray] = []
    lengths: list[float] = []
    print(f"\n{short['id']}  {short['title']}", flush=True)
    lines = short["lines"][:2] if pilot else short["lines"]
    for index, line in enumerate(lines, start=1):
        wav_path = work / f"line-{index:02d}.wav"
        synthesise(line["say"], wav_path)
        audio = load_wav(wav_path)
        fade_edges(audio)
        voices.append(audio)
        lengths.append(len(audio) / SR)
        print(f"  line {index:02d}  {lengths[-1]:.2f}s  {line['text']}", flush=True)

    leads, pads = plan_timing(lengths)
    segments: list[np.ndarray] = []
    shots: list[Path] = []
    hits: list[float] = []
    cursor = 0.0
    for index, (line, voice, lead, pad) in enumerate(zip(lines, voices, leads, pads), start=1):
        lead_n = int(round(lead * SR))
        pad_n = int(round(pad * SR))
        samples = lead_n + len(voice) + pad_n
        frames = max(8, math.ceil(samples / FRAME_SAMPLES))
        samples = frames * FRAME_SAMPLES
        segment = np.zeros((samples, 2), dtype=np.float32)
        end = min(len(voice), samples - lead_n)
        segment[lead_n:lead_n + end] = voice[:end]
        segments.append(segment)
        if line["accent"]:
            hits.append(cursor)
        cursor += samples / SR
        image = WORK / "stills" / line["image"]
        if not image.exists():
            raise SystemExit(f"Missing still: {image}")
        print(f"  shot {index:02d}  {frames / FPS:.2f}s  {line['move']}", flush=True)
        shots.append(
            render_shot(
                image, line, frames, work, index, short["chapter"],
                last=(index == len(lines) and not pilot),
            )
        )
        if pilot:
            break

    if pilot:
        pilot_out = OUT / "_pilot.mp4"
        OUT.mkdir(parents=True, exist_ok=True)
        voice_track = segments[0]
        music = compose_music(short["mood"], len(voice_track) / SR, [0.0])
        mix = voice_track * 0.95 + duck(music[: len(voice_track)], voice_track)
        mix_path = work / "mix.wav"
        write_wav(mix_path, mix)
        run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", str(shots[0]), "-i", str(mix_path),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart",
                str(pilot_out),
            ]
        )
        print(f"  pilot {pilot_out}", flush=True)
        return {"pilot": str(pilot_out), "duration": lengths[0]}

    voice_track = np.concatenate(segments, axis=0)
    music = compose_music(short["mood"], len(voice_track) / SR, hits)
    if len(music) < len(voice_track):
        music = np.pad(music, ((0, len(voice_track) - len(music)), (0, 0)))
    music = music[: len(voice_track)]
    mix = voice_track * 0.95 + duck(music, voice_track)
    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 0.98:
        mix *= 0.98 / peak
    mix_path = work / "mix.wav"
    write_wav(mix_path, mix)

    concat_list = work / "concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{shot.as_posix()}'" for shot in shots),
        encoding="utf-8",
    )
    silent = work / "silent.mp4"
    run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", str(silent),
        ]
    )
    final = OUT / f"{short['slug']}.mp4"
    OUT.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(silent), "-i", str(mix_path),
            "-map", "0:v", "-map", "1:a",
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart",
            str(final),
        ]
    )
    cover = OUT / f"{short['slug']}-cover.jpg"
    run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-ss", "0.35", "-i", str(final),
            "-frames:v", "1", "-q:v", "2", str(cover),
        ]
    )
    sheet = OUT / f"{short['slug']}-sheet.jpg"
    run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(final),
            "-vf", "fps=1/7,scale=270:480,tile=8x1",
            "-frames:v", "1", "-q:v", "3", str(sheet),
        ]
    )
    info = probe(final)
    duration = float(info["format"]["duration"])
    level = loudness(final)
    notes = OUT / f"{short['slug']}-description.txt"
    notes.write_text(
        f"TITLE\n{short['youtube_title']}\n\n"
        f"DESCRIPTION\n{short['description']}\n\n"
        f"PIN THIS COMMENT\n{short['pinned']}\n",
        encoding="utf-8",
    )
    meta = {
        "file": str(final),
        "title": short["youtube_title"],
        "duration_s": round(duration, 3),
        "loudness": level,
        "bytes": int(info["format"]["size"]),
        "probe": info,
    }
    (OUT / f"{short['slug']}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  done {duration:.2f}s  {level}", flush=True)
    return meta


def write_plan(results: list[dict]) -> None:
    order = [
        "02-the-room-with-no-door",
        "05-the-hammer-and-the-feather",
        "03-tamam-shud",
        "01-the-floor-that-opened",
        "04-the-dancing-plague",
    ]
    by_slug = {item["slug"]: item for item in SHORTS}
    lines = [
        "MIGHTY CHAPTERS — FIVE SHORTS",
        "",
        "Channel: Mighty Chapters  (@MightyChapters)",
        "Line: Every chapter holds a story.",
        "",
        "These are five different stories. They are not clips of one 40-minute video.",
        "Each one is a finished chapter, about 50 seconds, vertical, with captions,",
        "because most people watch Shorts with the sound low at first.",
        "The pictures are illustrated. The narration is the true part. Do not tell",
        "viewers the pictures are archival footage. The description already says illustrated.",
        "",
        "WHY THESE FIVE",
        "The banner is Rome, a pyramid, a detective, a dark old city, and an astronaut.",
        "Each short is one of those worlds, and each one opens on a fact a stranger",
        "can understand in a single second. That is what gets watched to the end.",
        "",
        "HOW LONG THEY CAME OUT",
    ]
    for result in results:
        lines.append(f"  {result['duration_s']:.1f}s   {Path(result['file']).name}   {result['loudness']}")
    lines += [
        "",
        "UPLOAD ORDER",
        "Schedule one a day, for 7:00 p.m. New York time. That is when the biggest",
        "English-speaking audience is scrolling. In YouTube Studio, set the visibility",
        "to Scheduled. On the phone, YouTube lets you schedule from the upload screen.",
        "The file is already vertical and under 60 seconds, so YouTube treats it as a Short.",
        "Paste the title and the description from the text file beside each video.",
        "Then pin the comment that is written in that same file.",
        "",
    ]
    for day, slug in enumerate(order, start=1):
        item = by_slug[slug]
        lines.append(f"Day {day}. {item['youtube_title']}")
        lines.append(f"        file: {slug}.mp4")
        lines.append(f"        comment: {item['pinned']}")
        lines.append("")
    lines += [
        "THE 40-MINUTE VIDEO",
        "Make it a sixth story. If the long video retells one of these shorts, people",
        "who already saw the short will leave, and the short will have spent the best minute.",
        "",
        "The strongest 40-minute chapter for this channel is the Halifax Explosion,",
        "December 6, 1917. Two ships meet in the harbor. There is a warning. Then the",
        "blast, a wave, and a blizzard the same night. Boston sent a relief train, so",
        "American viewers have a way into a story they may not know. It is not one of",
        "these five, so the shorts can run while you finish it.",
        "",
        "If the long video is going to be Apollo instead, post the hammer-and-feather",
        "short after that video, not before it.",
        "",
        "WHAT IS ON SCREEN",
        "Gold and black, like the banner. The name sits at the top of every frame.",
        "The last frame carries the line: Every chapter holds a story.",
        "Cuts are hard, on the sentence, because a slow fade is where people swipe away.",
        "The music is original, written for these five, in the same key, so the channel",
        "sounds like one place. There is no copyrighted track under the voice.",
        "",
        "Voice: Norman, American English.",
        "",
    ]
    (OUT / "UPLOAD-PLAN.txt").write_text("\n".join(lines), encoding="utf-8")


def require_tools() -> None:
    missing = [str(path) for path in (PIPER, VOICE, VOICE_CONFIG, FONT_BODY, FONT_DISPLAY) if not path.exists()]
    if missing:
        raise SystemExit("Missing:\n" + "\n".join(missing))
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise SystemExit("ffmpeg and ffprobe need to be on PATH.")


def stage_stills() -> None:
    destination = WORK / "stills"
    destination.mkdir(parents=True, exist_ok=True)
    needed = {line["image"] for short in SHORTS for line in short["lines"]}
    missing = []
    for name in sorted(needed):
        source = ASSETS / name
        if not source.exists():
            missing.append(name)
            continue
        target = destination / name
        if not target.exists() or source.stat().st_mtime > target.stat().st_mtime:
            shutil.copy2(source, target)
    if missing:
        raise SystemExit("Missing stills in assets:\n" + "\n".join(missing))
    masters = WORK / "masters"
    masters.mkdir(parents=True, exist_ok=True)
    for name in sorted(needed):
        master = masters / name
        source = destination / name
        if master.exists() and master.stat().st_mtime >= source.stat().st_mtime:
            continue
        run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(source),
                "-vf", "scale=1620:2880:force_original_aspect_ratio=increase:flags=lanczos,crop=1620:2880",
                "-frames:v", "1", "-q:v", "2", str(master),
            ]
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Render one short by id, for example 01")
    parser.add_argument("--pilot", action="store_true", help="Render the first shot only, for a check")
    args = parser.parse_args()
    require_tools()
    stage_stills()
    chosen = SHORTS
    if args.only:
        chosen = [short for short in SHORTS if short["id"] == args.only]
        if not chosen:
            raise SystemExit(f"No short with id {args.only}")
    results = [build(short, pilot=args.pilot) for short in chosen]
    if not args.pilot and not args.only:
        write_plan(results)
        print("\nFinished.", flush=True)
        for result in results:
            print(f"{result['duration_s']:.2f}s  {result['file']}", flush=True)


if __name__ == "__main__":
    main()
