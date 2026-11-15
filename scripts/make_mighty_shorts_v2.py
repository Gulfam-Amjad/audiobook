"""Mighty Chapters shorts, version 2.

Real, licensed stills and clips (see fetch_shorts_assets.py), a five-second
hook that tells the whole story in dialogue, and a tight edit: trimmed voice,
no dead air between lines.

    python scripts/make_mighty_shorts_v2.py
    python scripts/make_mighty_shorts_v2.py --only apollo13
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_mighty_shorts as v1  # noqa: E402  (helpers: piper, music, ffmpeg)

ASSETS = Path(r"D:\AIVideoManager\films\shorts-v2\assets")
WORK = Path(r"D:\AIVideoManager\films\shorts-v2\work")
OUT = Path(r"C:\Users\Gulfam\Desktop\youtube\mighty-chapters-shorts-v2")

FPS = 30
SR = v1.SR
GAP_S = 0.10
END_HOLD_S = 0.9
TRIM_DB = 0.012  # about -38 dBFS; below this counts as silence
LENGTH_SCALE = 0.93  # a little faster than the v1 render; fewer drawn-out words

# Each line: text on screen, say = spoken form, asset = tag in ASSET_MAP,
# move = camera move, style = "hook" (big, first five seconds) or "body".
# A video asset may set start=seconds to pick the moment.

STILLS = Path(r"D:\AIVideoManager\films\shorts-v2\stills")

ASSET_MAP: dict[str, dict[str, dict]] = {
    "apollo13": {
        "launch": {"file": "nasa-0101247.jpg", "kind": "image"},
        "dmg1": {"file": "nasa-as13-58-8464.jpg", "kind": "image"},
        "dmg2": {"file": "nasa-as13-59-8500.jpg", "kind": "image"},
        "dmg3": {"file": "nasa-as13-59-8501.jpg", "kind": "image"},
        "aq": {"file": "nasa-S70-34685.jpg", "kind": "image"},
        "crew": {"file": "nasa-7008012.jpg", "kind": "image"},
        "cm": {"file": "wiki-20180502-Apollo-13-command-module-Cosmosphere-1.jpg", "kind": "image"},
        "cm2": {"file": "wiki-20180502-Apollo-13-command-module-Cosmosphere-2.jpg", "kind": "image"},
        "mailbox": {"file": str(STILLS / "a13-mailbox.jpg"), "kind": "image"},
        "splash": {"file": "nasa-S69-27468.jpg", "kind": "image"},
        "splash2": {"file": "nasa-S69-27746.jpg", "kind": "image"},
        "heli": {"file": "nasa-S69-27919.jpg", "kind": "image"},
        "vets": {
            "file": "clip-jsc2020m001403_NASA_Apollo_13_Veterans_Recall_Mission_50_Years_Later-201027.mp4",
            "kind": "video",
            "start": 600,
        },
    },
    "parmitano": {
        "eva23": {"file": str(STILLS / "eva-23.jpg"), "kind": "image"},
        "eva23b": {"file": str(STILLS / "eva-23b.jpg"), "kind": "image"},
        "eva23c": {"file": str(STILLS / "eva-23c.jpg"), "kind": "image"},
        "airlock": {"file": str(STILLS / "eva-airlock.jpg"), "kind": "image"},
        "iss019555": {"file": "nasa-iss036e019555.jpg", "kind": "image"},
        "iss019886": {"file": "nasa-iss036e019886.jpg", "kind": "image"},
        "iss020862": {"file": "nasa-iss036e020862.jpg", "kind": "image"},
        "iss017539": {"file": "nasa-iss036e017539.jpg", "kind": "image"},
        "parm-portrait": {"file": "wiki-Artemis-III-pilot-Luca-Parmitano-poses-for-an-official-portrait-jsc2026e391926-alt-.jpg", "kind": "image"},
        "mc1": {"file": "nasa-jsc2013e066467.jpg", "kind": "image"},
        "mc2": {"file": "nasa-jsc2013e066455.jpg", "kind": "image"},
        "mc3": {"file": "nasa-jsc2013e066476.jpg", "kind": "image"},
    },
    "gemini4": {
        "gem-eva": {"file": "nasa-S65-30428.jpg", "kind": "image"},
        "gem-eva2": {"file": "nasa-S65-34635.jpg", "kind": "image"},
        "gun": {"file": "nasa-s65-27331.jpg", "kind": "image"},
        "inside": {"file": "nasa-S65-29642.jpg", "kind": "image"},
        "suit": {"file": "nasa-s65-29645.jpg", "kind": "image"},
        "wh-close": {"file": "wiki-S65-30429-Close-up-of-Ed-White-over-the-Gulf-of-Mexico.jpg", "kind": "image"},
        "white-portrait": {"file": "wiki-EdwardWhite.jpeg", "kind": "image"},
        "apollo1-crew": {"file": "wiki-KSC-67pc-0016-67PC-0016-.jpg", "kind": "image"},
        "apollo1-prayer": {"file": "wiki-Apollo-1-prayer.jpg", "kind": "image"},
    },
    "semmelweis": {
        "sem-portrait": {"file": str(STILLS / "semmelweis.jpg"), "kind": "image"},
        "prof": {"file": "wiki-Das-Wiener-Professoren-Kollegium-1853.jpg", "kind": "image"},
        "sem-chart": {"file": "wiki-Yearly-mortality-rates-1841-1846-two-clinics.svg", "kind": "image"},
    },
    "gage": {
        "gage-dag": {"file": "wiki-Phineas-Gage-Cased-Daguerreotype-WilgusPhoto2008-12-19-EnhancedRetouched-Color.jpg", "kind": "image"},
        "gage-mill": {"file": "wiki-Phineas-Gage-GageMillerPhoto2010-02-17-Unretouched-Color.jpg", "kind": "image"},
        "gage-countway": {"file": "wiki-Phineas-Gage-Countway-Library.jpg", "kind": "image"},
        "gage-skull": {"file": "wiki-Phineas-Gage-s-skull.png", "kind": "image"},
    },
}


def L(text, asset, move="push", style="body", say=None, start=None, accent=False):
    return {
        "text": text,
        "say": say or text,
        "asset": asset,
        "move": move,
        "style": style,
        "start": start,
        "accent": accent,
    }


SHORTS: list[dict] = [
    {
        "id": "apollo13",
        "slug": "01-the-square-peg",
        "chapter": "CHAPTER 01",
        "title": "The Square Peg",
        "youtube_title": "Apollo 13: The Square Peg That Nearly Killed Three Men",
        "mood": "moon",
        "pinned": "Would you have trusted a cardboard fix to keep you alive?",
        "description": """In April 1970, an oxygen tank exploded on Apollo 13. The crew moved into the lunar module, Aquarius, and carbon dioxide began to build up. The command module's filters were round, and Aquarius's slot was square. Engineers on the ground built an adapter from plastic bags, duct tape, cardboard, and a spacesuit hose. The crew built it and came home on April 17, 1970.

Photos: NASA (public domain). The clip is NASA footage.

Every chapter holds a story.

#Shorts #Apollo13 #Space #History #MightyChapters""",
        "lines": [
            L("Houston, we've had a problem.", "dmg2", "push", "hook", accent=True),
            L("Three men are breathing poison. The fix is a square peg.", "dmg1", "pull", "hook"),
            L("An oxygen tank exploded on April 13, 1970.", "dmg3", "push"),
            L("Lovell, Swigert and Haise ran into the lunar module, Aquarius.", "aq", "drift"),
            L("Aquarius was built to keep two men alive for about two days.", "crew", "push"),
            L("Now it had to keep three men alive for four.", "cm", "pull"),
            L("Carbon dioxide from their breathing began to build up.", "mailbox", "push"),
            L("The command module's filters were round.", "cm2", "left"),
            L("The slot in Aquarius was square.", "mailbox", "right"),
            L("The filters would not fit.", "mailbox", "push", accent=True),
            L("Engineers on the ground had one job: build an adapter from what was on board.", "vets", "pull", start=600),
            L("Plastic bags. Duct tape. Cardboard. A spacesuit hose.", "dmg3", "rise"),
            L("The crew built it step by step, while the air got worse.", "mailbox", "drift"),
            L("The carbon dioxide dropped. They came home on April 17.", "splash", "push"),
            L("The mission never landed on the Moon. The crew came home.", "heli", "pull"),
            L("Would you have trusted a cardboard fix to keep you alive?", "launch", "push", "hook", accent=True),
        ],
    },
    {
        "id": "parmitano",
        "slug": "02-the-helmet-full-of-water",
        "chapter": "CHAPTER 02",
        "title": "The Helmet Full of Water",
        "youtube_title": "The Astronaut Who Almost Drowned in Space",
        "mood": "void",
        "pinned": "If the water was rising over your face, what would you do first?",
        "description": """On July 16, 2013, Italian astronaut Luca Parmitano was outside the International Space Station, about 250 miles up. Water from his suit's cooling system leaked and drifted into his helmet. He could not see, and water was close to his face. His partner Chris Cassidy helped guide him back to the airlock, and NASA called off the spacewalk to investigate the leak.

Photos: NASA (public domain).

Every chapter holds a story.

#Shorts #Space #Astronaut #ISS #MightyChapters""",
        "lines": [
            L("Water fills his helmet in orbit, and he nearly drowns.", "eva23", "push", "hook", accent=True),
            L("July 16, 2013. Luca Parmitano is outside the International Space Station.", "parm-portrait", "push"),
            L("About 250 miles above Earth.", "iss019555", "drift"),
            L("His suit has a water-cooling system, and it starts to leak.", "eva23b", "left"),
            L("The water does not fall. It drifts.", "iss019886", "rise"),
            L("It floats up over his face, and into his visor.", "eva23c", "pull"),
            L("He cannot see, and water is close to his face.", "eva23", "push"),
            L("Mission control tells him to come back in.", "mc1", "push"),
            L("His partner, Chris Cassidy, guides him toward the airlock.", "iss020862", "drift"),
            L("Back inside the airlock, the danger is not over.", "airlock", "push"),
            L("NASA calls off the spacewalk and investigates the leak.", "mc2", "pull"),
            L("He nearly drowned in a spacesuit, in orbit.", "eva23c", "rise"),
            L("The most dangerous ocean an astronaut can face is inside his own helmet.", "eva23b", "push", "hook", accent=True),
        ],
    },
    {
        "id": "gemini4",
        "slug": "03-the-first-walk-in-space",
        "chapter": "CHAPTER 03",
        "title": "The First Walk in Space",
        "youtube_title": "He Walked in Space First, Then Died in a Fire",
        "mood": "moon",
        "pinned": "Would you have wanted to come back in?",
        "description": """On June 3, 1965, Ed White became the first American to walk in space. He was tethered to Gemini 4 and moved with a handheld gun that fired bursts of oxygen. After about twenty-one minutes, mission control called him back in. On January 27, 1967, White died in a fire inside the Apollo 1 capsule during a launch rehearsal, along with Gus Grissom and Roger Chaffee.

Photos: NASA (public domain), including Ed White and Apollo 1 images from Wikimedia Commons (public domain).

Every chapter holds a story.

#Shorts #Space #EdWhite #Gemini #MightyChapters""",
        "lines": [
            L("Ed White walked in space in 1965. Then he died in a fire.", "gem-eva", "push", "hook", accent=True),
            L("June 3, 1965. Gemini 4 is orbiting Earth.", "inside", "push"),
            L("Ed White becomes the first American to walk in space.", "white-portrait", "left"),
            L("A golden tether links him to the capsule.", "gem-eva2", "drift"),
            L("He moves with a handheld gun that fires bursts of oxygen.", "gun", "right"),
            L("Each burst pushes him a little farther away.", "gem-eva", "push"),
            L("Earth rolls beneath him, more than a hundred miles down.", "gem-eva2", "pull"),
            L("After about twenty-one minutes, mission control calls him back.", "gem-eva", "push"),
            L("He does not want to come back in. But he does.", "inside", "pull"),
            L("Four days after launch, the crew splashes down safely.", "suit", "push"),
            L("January 27, 1967. White is preparing for a launch rehearsal on Apollo 1.", "apollo1-crew", "left"),
            L("A fire breaks out inside the capsule.", "apollo1-prayer", "push"),
            L("White dies, with Gus Grissom and Roger Chaffee.", "apollo1-prayer", "rise"),
            L("His only spacewalk was the one that made him famous.", "gem-eva2", "push", "hook", accent=True),
        ],
    },
    {
        "id": "semmelweis",
        "slug": "04-the-doctor-who-washed-his-hands",
        "chapter": "CHAPTER 04",
        "title": "The Doctor Who Washed His Hands",
        "youtube_title": "The Doctor Who Was Ignored for Saying Wash Your Hands",
        "mood": "plague",
        "pinned": "Would you have listened to a doctor who said your own hands were the danger?",
        "description": """In 1847, Ignaz Semmelweis worked in Vienna's General Hospital, where women in the doctors' maternity ward died of childbed fever far more often than in the midwives' ward. He noticed that the doctors came straight from autopsies. When a colleague died after cutting himself during an autopsy, Semmelweis ordered doctors to wash their hands in chlorinated lime. Deaths in the first ward dropped sharply. Many senior doctors rejected his idea. He died in 1865.

Photos: Wikimedia Commons (public domain).

Every chapter holds a story.

#Shorts #History #Medicine #Science #MightyChapters""",
        "lines": [
            L("Unwashed hands killed mothers. His warning was ignored.", "sem-portrait", "push", "hook", accent=True),
            L("Vienna General Hospital had two maternity wards.", "prof", "push"),
            L("The first was run by doctors. The second by midwives.", "sem-chart", "left"),
            L("Women in the doctors' ward died of childbed fever far more often.", "sem-chart", "drift"),
            L("Ignaz Semmelweis noticed the doctors came straight from autopsies.", "sem-portrait", "push"),
            L("A colleague cut his finger during an autopsy, and died with the same symptoms as the mothers.", "sem-portrait", "pull"),
            L("So Semmelweis ordered doctors to wash their hands in chlorinated lime.", "prof", "right"),
            L("Deaths in the first ward dropped sharply.", "sem-chart", "push"),
            L("Senior doctors were insulted. Many would not accept he was right.", "prof", "left"),
            L("He lost his post, and his ideas were ignored for years.", "sem-portrait", "pull"),
            L("In 1865, after an injury became infected, he died in an asylum.", "sem-portrait", "push"),
            L("Later research, by Pasteur and Lister, explained why he was right.", "prof", "pull"),
            L("Wash your hands. A doctor's warning, ignored for decades.", "sem-portrait", "push", "hook", accent=True),
        ],
    },
    {
        "id": "gage",
        "slug": "05-the-iron-rod",
        "chapter": "CHAPTER 05",
        "title": "The Iron Rod",
        "youtube_title": "The Iron Rod That Went Through a Man's Head, and He Lived",
        "mood": "noir",
        "pinned": "If you were his friend, would you have noticed the change?",
        "description": """On September 13, 1848, Phineas Gage was a railroad foreman in Vermont. An explosion drove an iron tamping rod through his left cheek and out the top of his skull. He survived, but friends said he was no longer himself. Doctors studied him for years. His case became an early clue that personality depends on the brain. He died in 1860. The rod is kept at Harvard.

Photos: Wikimedia Commons. Daguerreotype with the rod: Jack and Beverly Wilgus (CC BY-SA 3.0). Countway Library photo: That Coptic Guy (CC BY 4.0). Skull photo: Kelley DJ et al. (CC BY 2.5). Other photos: public domain.

Every chapter holds a story.

#Shorts #History #Science #Brain #MightyChapters""",
        "lines": [
            L("In 1848, an iron rod went through Phineas Gage's head.", "gage-dag", "push", "hook", accent=True),
            L("He lived, but he was never the same man.", "gage-mill", "pull", "hook"),
            L("September 13, 1848. A railroad crew in Vermont.", "gage-dag", "push"),
            L("Gage, a foreman, is packing explosive powder with an iron tamping rod.", "gage-dag", "drift"),
            L("A spark sets the powder off.", "gage-mill", "push"),
            L("The rod, over three feet long, shoots through his left cheek.", "gage-skull", "push"),
            L("It exits through the top of his skull, and lands yards away.", "gage-skull", "pull"),
            L("He is conscious, sitting up, and talking soon after.", "gage-mill", "left"),
            L("Doctor John Harlow cleans the wound. Gage survives.", "gage-dag", "right"),
            L("But the man who recovers is impatient, rude, and unable to keep plans.", "gage-countway", "push"),
            L("Doctors study him for years.", "gage-countway", "pull"),
            L("His case becomes an early clue that personality depends on the brain.", "gage-skull", "drift"),
            L("He later drives stagecoaches in Chile.", "gage-dag", "push"),
            L("He dies in 1860, twelve years after the accident.", "gage-mill", "drift"),
            L("One rod. One man. A new idea about the mind.", "gage-countway", "push", "hook", accent=True),
        ],
    },
]


# --- audio ---------------------------------------------------------------


def synthesise_trimmed(text: str, destination: Path) -> np.ndarray:
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = destination.with_suffix(".raw.wav")
    v1.synthesise(text, raw)
    audio = v1.load_wav(raw)
    level = np.max(np.abs(audio), axis=1)
    voiced = np.where(level > TRIM_DB)[0]
    if voiced.size == 0:
        return audio[:1]
    pad = int(0.03 * SR)
    start = max(0, voiced[0] - pad)
    end = min(len(audio), voiced[-1] + pad)
    trimmed = audio[start:end].copy()
    v1.fade_edges(trimmed, 0.006)
    return trimmed


# --- picture -------------------------------------------------------------


def asset_path(short_id: str, asset: str) -> Path:
    info = ASSET_MAP[short_id][asset]
    return ASSETS / short_id / info["file"]


def image_filter(move: str, frames: int) -> str:
    """Blurred fill with the picture fitted on top, then a slow move."""
    last = max(frames - 1, 1)
    if move == "pull":
        z = f"max(1.10-0.10*on/{last}\\,1.00)"
    elif move == "drift":
        z = "1.08"
    else:  # push
        z = f"min(1.00+0.10*on/{last}\\,1.10)"
    x = "iw/2-(iw/zoom/2)" if move != "drift" else f"(iw-iw/zoom)*on/{last}"
    y = "ih/2-(ih/zoom/2)"
    return (
        "[0:v]split=2[bgsrc][fgsrc];"
        "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=24:6,eq=brightness=-0.16:saturation=0.75[bg];"
        "[fgsrc]scale=1040:1500:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s=1080x1920:fps=30,"
        "eq=contrast=1.06:saturation=0.9,vignette=PI/6[pic]"
    )


def clip_filter() -> str:
    return (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "eq=contrast=1.05:saturation=0.9,vignette=PI/6[pic]"
    )


def captions(short: dict, line: dict, text_dir: Path, index: int, last: bool) -> list[str]:
    cap_path = text_dir / f"cap-{index:02d}.txt"
    brand = text_dir / "brand.txt"
    chapter = text_dir / "chapter.txt"
    tag = text_dir / "tag.txt"
    brand.write_text("MIGHTY CHAPTERS", encoding="utf-8")
    chapter.write_text(short["chapter"], encoding="utf-8")
    tag.write_text("EVERY CHAPTER HOLDS A STORY", encoding="utf-8")
    limit = 20 if line["style"] == "hook" else 30
    wrapped = v1.wrap(line["text"], limit)
    cap_path.write_text(wrapped, encoding="utf-8")
    rows = wrapped.count("\n") + 1
    parts = [
        v1.drawtext(v1.FONT_DISPLAY, brand, 36, "0xE8C872", "86", False, 3),
        v1.drawtext(v1.FONT_DISPLAY, chapter, 24, "0xF4EBD0", "132", False, 2),
    ]
    if line["style"] == "hook":
        size = 60 if rows <= 2 else 50 if rows == 3 else 42
        parts.append(v1.drawtext(v1.FONT_DISPLAY, cap_path, size, "0xF6E2A8", "h*0.60", False, 6))
    else:
        size = 46 if rows <= 2 else 40
        parts.append(v1.drawtext(v1.FONT_BODY, cap_path, size, "0xF7F1E4", "h*0.66", True, 0))
    if last:
        parts.append(v1.drawtext(v1.FONT_DISPLAY, tag, 30, "0xE8C872", "h*0.90", False, 3))
    return parts


def render_shot(short: dict, line: dict, frames: int, shot_dir: Path, index: int, last: bool) -> Path:
    duration = frames / FPS
    shot = shot_dir / f"shot-{index:02d}.mp4"
    source = asset_path(short["id"], line["asset"])
    info = ASSET_MAP[short["id"]][line["asset"]]
    texts = captions(short, line, shot_dir, index, last)
    overlay = ",".join(texts)
    if info["kind"] == "video":
        start = float(line.get("start") or info.get("start") or 0.0)
        graph = f"{clip_filter()};[pic]{overlay}[out]"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-stream_loop", "-1", "-ss", f"{start:.3f}", "-i", str(source),
            "-filter_complex", graph, "-map", "[out]",
            "-t", f"{duration:.4f}", "-r", str(FPS), "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-an", str(shot),
        ]
    else:
        graph = f"{image_filter(line['move'], frames)};[pic]{overlay}[out]"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-loop", "1", "-framerate", "30", "-i", str(source),
            "-filter_complex", graph, "-map", "[out]",
            "-t", f"{duration:.4f}", "-r", str(FPS), "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-an", str(shot),
        ]
    v1.run(cmd)
    return shot


# --- timing and build ----------------------------------------------------


def build(short: dict) -> dict:
    work = WORK / short["slug"]
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    print(f"\n{short['id']}  {short['title']}", flush=True)

    voices: list[np.ndarray] = []
    for index, line in enumerate(short["lines"], start=1):
        voice = synthesise_trimmed(line["say"], work / f"line-{index:02d}.wav")
        voices.append(voice)
        print(f"  line {index:02d}  {len(voice) / SR:.2f}s  {line['text']}", flush=True)

    segments: list[np.ndarray] = []
    shots: list[Path] = []
    hits: list[float] = []
    cursor = 0.0
    for index, (line, voice) in enumerate(zip(short["lines"], voices), start=1):
        hold = END_HOLD_S if index == len(short["lines"]) else GAP_S
        span = len(voice) / SR + hold
        frames = max(12, math.ceil(span * FPS))
        samples = int(round(frames / FPS * SR))
        segment = np.zeros((samples, 2), dtype=np.float32)
        segment[: len(voice)] = voice[: samples]
        segments.append(segment)
        if line["accent"]:
            hits.append(cursor)
        cursor += samples / SR
        shots.append(render_shot(short, line, frames, work, index, last=index == len(short["lines"])))
        print(f"  shot {index:02d}  {frames / FPS:.2f}s  {line['move']}  {line['asset']}", flush=True)

    voice_track = np.concatenate(segments, axis=0)
    music = v1.compose_music(short["mood"], len(voice_track) / SR, hits)
    if len(music) < len(voice_track):
        music = np.pad(music, ((0, len(voice_track) - len(music)), (0, 0)))
    music = music[: len(voice_track)]
    mix = voice_track * 0.95 + v1.duck(music, voice_track)
    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 0.98:
        mix *= 0.98 / peak
    mix_path = work / "mix.wav"
    v1.write_wav(mix_path, mix)

    concat_list = work / "concat.txt"
    concat_list.write_text("\n".join(f"file '{s.as_posix()}'" for s in shots), encoding="utf-8")
    silent = work / "silent.mp4"
    v1.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(silent)])

    OUT.mkdir(parents=True, exist_ok=True)
    final = OUT / f"{short['slug']}.mp4"
    v1.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(silent), "-i", str(mix_path),
            "-map", "0:v", "-map", "1:a",
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart", str(final),
        ]
    )
    info = v1.probe(final)
    duration = float(info["format"]["duration"])
    level = v1.loudness(final)
    (OUT / f"{short['slug']}-description.txt").write_text(
        f"TITLE\n{short['youtube_title']}\n\nDESCRIPTION\n{short['description']}\n\nPIN THIS COMMENT\n{short['pinned']}\n",
        encoding="utf-8",
    )
    run_cover = OUT / f"{short['slug']}-cover.jpg"
    v1.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.35", "-i", str(final), "-frames:v", "1", "-q:v", "2", str(run_cover)])
    print(f"  done {duration:.2f}s  {level}", flush=True)
    return {"slug": short["slug"], "file": str(final), "duration_s": round(duration, 2), "loudness": level}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="One short by slug, e.g. apollo13")
    args = parser.parse_args()
    if not SHORTS:
        raise SystemExit("No shorts defined yet.")
    chosen = [s for s in SHORTS if not args.only or s["id"] == args.only]
    results = [build(short) for short in chosen]
    for result in results:
        print(f"{result['duration_s']:.2f}s  {result['file']}", flush=True)


if __name__ == "__main__":
    main()
