"""Apollo 11, told with the NASA film itself.

The pictures were checked frame by frame in "The Eagle Has Landed" (1969,
National Archives, public domain) and in the pad camera of the launch.
Each line is written for the stretch of film under it. A missing close-up
does not get a rewritten sentence. The shot stays on that stretch, slowed
a little if the line is longer than the picture.

The narrator is Norman, a male LibriVox voice. Piper cannot perform
excitement, so the pace is only tightened between sentences. The energy
is in the opening, which starts on the Moon and only then rewinds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from sqlalchemy import select

from app.constants import AnimationMethod, ClipStatus
from app.db import session_scope
from app.models import ImageFolder, Project, Scene, SceneClip, VoiceProfile
from app.services import ffmpeg
from app.services import images as image_service
from app.services import projects as project_service
from app.services import publishing
from app.services import render as render_service
from app.services import routing as routing_service
from app.services import scripts as script_service
from app.services import speech as speech_service
from app.services import stock_media
from app.services.timeutil import utcnow

NAME = "One Step on the Moon"
FOLDER = Path(r"D:\youtube_library\apollo-11-film")
STILLS = FOLDER / "stills"
EAGLE = FOLDER / "eagle-1969.webm"
JACK = FOLDER / "jack-king.webm"
MUSIC = Path(r"D:\AIVideoManager\music\Aftermath.mp3")
DESKTOP = Path(r"C:\Users\Gulfam\Desktop\One Step on the Moon.mp4")
SHORT = Path(r"C:\Users\Gulfam\Desktop\One Step on the Moon - short.mp4")
THUMB = Path(r"C:\Users\Gulfam\Desktop\One Step on the Moon - thumbnail.jpg")
FONT = Path(r"C:\Windows\Fonts\arialbd.ttf")
NARRATOR_BASE = "en_US-norman-medium"
NARRATOR = "en_US-norman-doc"
CREDIT_EAGLE = (
    '"The Eagle Has Landed, The Flight of Apollo 11, 1969.webm" by US National Archives, '
    "Public domain, via Wikimedia Commons - "
    "https://commons.wikimedia.org/wiki/File:The_Eagle_Has_Landed,_The_Flight_of_Apollo_11,_1969.webm"
)
CREDIT_JACK = (
    "\"Jack King's Apollo 11 Launch Commentary\" by NASA Kennedy Space Center, "
    "Public domain, via Wikimedia Commons - "
    "https://commons.wikimedia.org/wiki/File:Jack_King%27s_Apollo_11_Launch_Commentary_o4OBKOlgmfo.webm"
)

# key, chapter, narration, source, start in that file, seconds of picture to stay on
SCENES = [
    ("s01", "On the Moon",
     "A man is standing on the Moon. No one has ever stood there. The ground under his boots is dust, and he does not have long. The picture coming back to Earth is rough. Hundreds of millions of people are watching it anyway. The shadow of the ladder lies across the ground in front of him, and he is looking at his feet.",
     "eagle", 815, 18),
    ("s02", "Two hours",
     "He has a little more than two hours. Then the lander has to leave, or he does not come home. The landing itself was the dangerous minute. Everything before this shadow on the dust was just getting the lander this close. This is how he got here.",
     "eagle", 690, 10),
    ("s03", "The rocket",
     "Four days earlier, a rocket stood on a pad in Florida. It was the Saturn five, the tallest rocket anyone had flown with a crew on top. Most of it was fuel. The men sat at the very end. Up the side, in letters you could read from the ground, was the name of the country that built it.",
     "eagle", 145, 10),
    ("s04", "The crew",
     "Three men were going. One would stay in the ship that circled the Moon. Two would take the lander down. This is one of them, already shut inside the helmet. From this moment the air he breathes is the air they carried with them. There is no opening that visor until he is home.",
     "eagle", 190, 8),
    ("s05", "Liftoff",
     "July sixteenth, nineteen sixty-nine. The engines lit. The clamps let go. For a few seconds the rocket seemed to stand on its own fire, and then Florida dropped away. Four days of flight began in that light. The Moon was still a long way from here.",
     "jack", 0, 0),
    ("s06", "The checklist",
     "Inside, the cabin was a wall of switches and a stack of paper. They worked the list, line by line. Every switch had a name. Missing one was how a flight ended early. There was no other way to fly a machine this complicated.",
     "eagle", 390, 8),
    ("s07", "Home, from orbit",
     "From orbit they could see home. Cities showed as small lights on the night side. The whole world they knew was one curve of the window. They were the first crew sent to land. The Earth got smaller, and it kept getting smaller.",
     "eagle", 490, 8),
    ("s08", "The Moon in the window",
     "Then the Moon took the window. Craters, the edge of their own ship, and nothing else. No runway. No flat field marked out for them. Just rock, and a ship small enough to lose against it.",
     "eagle", 540, 8),
    ("s09", "Looking for a place",
     "Armstrong flew the lander by hand. The fuel was almost gone, and the ground under him was a field of craters. The computer was taking them toward one that was too big to land in. He flew past it, looking for one patch that would not tip the lander over.",
     "eagle", 640, 8),
    ("s10", "The step",
     "A boot came down the leg of the lander and into the dust. The ladder was short. The last drop to the footpad was not. He let go, and he was standing on the Moon. That was the step.",
     "eagle", 840, 8),
    ("s11", "The flag",
     "They put up a flag. There is no wind on the Moon, so the cloth cannot fly. A rod held it out, and the man beside it turned so the camera could see them both. It was a marker, not a claim that the ground was theirs.",
     "eagle", 880, 8),
    ("s12", "The clock",
     "Then it was work. Boxes, rocks, a camera. They had trained for these same boxes in a hangar on Earth. On the Moon the boxes were lighter, and the clock was not. Two men, and the time running out the whole time.",
     "eagle", 990, 8),
    ("s13", "What they left",
     "The lander sat in the open with its experiments beside it. A small machine in front of it was there to keep listening after they were gone. They had not come only to walk. When the men left, the lander stayed.",
     "eagle", 1040, 10),
    ("s14", "Leaving the dust",
     "They lifted off the dust, and the craters slid under them again. The bottom half of the lander stayed behind. It is still there. Only the top half could fly, and the Moon did not look any different for their having stood on it.",
     "eagle", 1140, 8),
    ("s15", "The ride home",
     "A small ship was crossing above the Moon. One ship had to find the other. That meeting was the way home. Miss it, and the two men who had walked had no way back to Earth.",
     "eagle", 1240, 8),
    ("s16", "Earthrise",
     "On the way, the Earth rose over the rim of the Moon. Blue and white, and small enough to cover with a thumb. Everyone they knew was on that curve. The only thing of theirs still on the ground below was the lander they had left.",
     "eagle", 1335, 8),
    ("s17", "The helicopter",
     "A recovery helicopter sat on the ship, rotors stopped, waiting. The ocean landing was the Navy's job. This aircraft's job was the last few feet, from the water up to the deck, once three men were floating in the Pacific.",
     "eagle", 1435, 8),
    ("s18", "Behind glass",
     "When they were back, they were sealed behind glass. People could look at them. They could not touch them. The dust was the reason. They stayed in that trailer, and then in a building, until the doctors were sure they had not brought anything home.",
     "eagle", 1485, 8),
    ("s19", "One world",
     "This is what they came home to. The flight had lasted eight days. The walk had lasted about two and a half hours. One world in the window, and three men who had walked on the other one.",
     "eagle", 1635, 8),
    ("s20", "Still in the dust",
     "The lander is still in that dust. In the film, the man is still beside it, packing up a visit that is almost over. The step was the hard part. Everything after it was just getting home.",
     "eagle", 1585, 8),
]


def plan(stills: dict[str, str]) -> dict:
    scenes = []
    for index, (key, purpose, narration, *_rest) in enumerate(SCENES):
        scenes.append({
            "scene_key": key,
            "purpose": purpose,
            "target_duration_s": 18,
            "narration": narration,
            "dialogue": [],
            "characters": [],
            "image_hint": stills[key],
            "camera": "slow push in" if index % 2 else "gentle pan right",
            "transition": "cut",
            "preferred_method": "camera_move",
        })
    return {
        "title": NAME,
        "logline": "The first landing on the Moon, from the moment a man is already standing there.",
        "synopsis": "A cold open on the lunar surface, then the rocket, the flight, the step, and the way home.",
        "scenes": scenes,
    }


def _stills(ffmpeg_path: str) -> dict[str, str]:
    STILLS.mkdir(parents=True, exist_ok=True)
    names: dict[str, str] = {}
    for key, _purpose, _narration, source, start, _window in SCENES:
        film = JACK if source == "jack" else EAGLE
        at = 16.0 if source == "jack" else start + 2.0
        dest = STILLS / f"{key}.jpg"
        if not dest.exists():
            subprocess.check_call([
                ffmpeg_path, "-y", "-v", "error",
                "-ss", f"{at:.2f}", "-i", str(film),
                "-frames:v", "1", "-q:v", "2", str(dest),
            ])
        names[key] = dest.name
    return names


def _thumbnail(ffmpeg_path: str, still: Path, dest: Path) -> None:
    font_copy = Path(r"D:\AIVideoManager\tmp\arialbd.ttf")
    if not font_copy.exists():
        shutil.copy2(FONT, font_copy)
    subprocess.check_call(
        [
            ffmpeg_path, "-y", "-v", "error", "-i", str(still),
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
            "drawtext=fontfile=arialbd.ttf:text='ONE STEP':fontsize=86:"
            "fontcolor=white:borderw=4:bordercolor=black:x=(w-text_w)/2:y=h-130",
            "-frames:v", "1", str(dest),
        ],
        cwd=str(font_copy.parent),
    )


def _short(ffmpeg_path: str, video: Path) -> None:
    subprocess.check_call([
        ffmpeg_path, "-y", "-v", "error", "-i", str(video), "-t", "45",
        "-vf", "crop=608:1080:(in_w-608)/2:0,scale=1080:1920",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "1",
        "-c:a", "aac", "-b:a", "160k", str(SHORT),
    ])


async def _voice(session) -> VoiceProfile:
    base = (
        await session.execute(select(VoiceProfile).where(VoiceProfile.name == NARRATOR_BASE))
    ).scalar_one()
    row = (
        await session.execute(select(VoiceProfile).where(VoiceProfile.name == NARRATOR))
    ).scalar_one_or_none()
    if row is None:
        row = VoiceProfile(name=NARRATOR)
        session.add(row)
    row.engine = base.engine
    row.model_path = base.model_path
    row.config_path = base.config_path
    row.language = base.language
    row.speaker_id = base.speaker_id
    row.sample_rate = base.sample_rate
    row.licence = base.licence
    row.licence_url = base.licence_url
    row.attribution = base.attribution
    row.commercial_use = base.commercial_use
    row.installed = True
    # Same recording. Slightly shorter gaps so the open does not drag.
    # The words themselves are not sped up: faster words make the captions unreadable.
    row.length_scale = 1.0
    row.noise_scale = base.noise_scale
    row.noise_w = base.noise_w
    row.sentence_silence_s = 0.08
    row.notes = "Norman, male. Tighter sentence gaps only. Piper cannot act excitement."
    await session.flush()
    return row


async def build() -> None:
    if not EAGLE.exists() or not JACK.exists():
        raise SystemExit("The NASA films are not on disk.")
    if not MUSIC.exists():
        raise SystemExit("The music bed is missing.")
    status = await ffmpeg.detect()
    stills = _stills(status.ffmpeg_path)

    async with session_scope() as session:
        folder = (
            await session.execute(select(ImageFolder).where(ImageFolder.path == str(STILLS)))
        ).scalar_one_or_none()
        if folder is None:
            folder = ImageFolder(path=str(STILLS), recursive=False)
            session.add(folder)
            await session.flush()
        progress = await image_service.scan_folder(session, folder)
        print("scan:", progress.state, progress.message, flush=True)
        voice = await _voice(session)
        project = (
            await session.execute(select(Project).where(Project.name == NAME))
        ).scalar_one_or_none()
        values = dict(
            target_duration_s=420,
            resolution="1080p",
            aspect_ratio="16:9",
            fps=24,
            image_folder_id=folder.id,
            narrator_voice_id=voice.id,
            speaking_rate_wpm=165,
            stills_ai_generated=False,
            music_path=str(MUSIC),
            music_gain_db=-22,
        )
        if project is None:
            project = await project_service.create_project(
                session, name=NAME, niche="history documentary", audience="general",
                story_style="documentary",
                brief="Apollo 11, opened on the lunar surface, told with the NASA film.",
                **values,
            )
        else:
            await project_service.update_project(session, project.id, values)
        project_id = project.id
        script = await script_service.accept_paste(session, project_id, json.dumps(plan(stills)))
        print(
            "script", script.id, "valid", script.is_valid,
            "words", script.word_count, "est", script.estimated_duration_s, flush=True,
        )
        if not script.is_valid:
            print(json.dumps((script.validation or {}).get("errors"), indent=2))
            raise SystemExit(1)
        await script_service.approve(session, script.id)

    async with session_scope() as session:
        speech_plan = await speech_service.plan_segments(session, project_id)
        print("speech plan", speech_plan, flush=True)
    async with session_scope() as session:
        outcome = await speech_service.synthesise_project(session, project_id)
        print("speech", outcome, flush=True)
        if outcome.failed or not outcome.measured_s:
            raise SystemExit("The narration was not spoken.")

    async with session_scope() as session:
        await routing_service.plan_shots(session, project_id)
        run = await routing_service.render_local_shots(session, project_id)
        print("shots", run.rendered, "failed", run.failed, run.errors, flush=True)
        if run.failed:
            raise SystemExit("A shot failed.")

    by_key = {row[0]: row for row in SCENES}
    async with session_scope() as session:
        project = await project_service.get_project(session, project_id)
        script = await script_service.approved_script(session, project_id)
        width, height = project_service.frame_size(project.resolution, project.aspect_ratio)
        scenes = (
            await session.execute(
                select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index)
            )
        ).scalars().all()
        clips_dir = routing_service.clips_dir(project)
        for scene in scenes:
            key, _purpose, _narration, source, start_s, window = by_key[scene.scene_key]
            current = await routing_service._selected_clip(session, scene.id)
            if current is None or current.duration_s is None:
                raise SystemExit(f"{key}: no timing.")
            film = JACK if source == "jack" else EAGLE
            info = await ffmpeg.probe(film)
            full = float(info.duration_s or 0.0)
            if source == "jack":
                start = max(full - float(current.duration_s), 0.0)
                source_s = full
            else:
                start = float(start_s)
                source_s = start + float(window)
            destination = clips_dir / f"{scene.scene_key}-footage.mp4"
            await stock_media.render_footage(
                status.ffmpeg_path, film, destination,
                width=width, height=height, fps=project.fps,
                duration_s=float(current.duration_s), source_s=source_s, start_s=start,
            )
            made = await ffmpeg.probe(destination)
            if made.duration_s is None or abs(made.duration_s - current.duration_s) > 0.08:
                raise SystemExit(f"{key}: footage is {made.duration_s}s, needs {current.duration_s}s.")
            await routing_service._deselect_others(session, scene.id)
            session.add(SceneClip(
                scene_id=scene.id,
                project_id=project.id,
                method=AnimationMethod.STOCK_FOOTAGE,
                provider_key="wikimedia_commons",
                source_image_id=scene.image_id,
                file_path=str(destination),
                duration_s=current.duration_s,
                width=made.width,
                height=made.height,
                fps=made.fps,
                is_generated_motion=False,
                selected=True,
                status=ClipStatus.READY,
                completed_at=utcnow(),
                validation={
                    "credit": CREDIT_JACK if source == "jack" else CREDIT_EAGLE,
                    "licence": "Public domain",
                    "source_title": film.name,
                    "excerpt_start_s": round(start, 3),
                    "motion": "real NASA film, not generated",
                },
            ))
            print(f"{key}: film from {start:.1f}s", flush=True)

    async with session_scope() as session:
        result = await render_service.render_project(session, project_id)
        print("render", result.status, result.duration_s, result.output_path, flush=True)
        print("problems", result.problems, flush=True)
        if not result.succeeded or not result.render_id:
            raise SystemExit("The render did not pass.")
        assets = await publishing.prepare(session, result.render_id)
        print("disclosure", assets.disclosure, flush=True)
        out = Path(result.output_path)
        duration = float(result.duration_s or 0)

    shutil.copy2(out, DESKTOP)
    _thumbnail(status.ffmpeg_path, STILLS / "s01.jpg", THUMB)
    _short(status.ffmpeg_path, out)
    print("desktop", DESKTOP, DESKTOP.stat().st_size, "duration", duration, flush=True)


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    asyncio.run(build())
