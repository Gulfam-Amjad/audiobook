"""A one-minute documentary built from free library media, with no hand-picking.

Every shot carries a search query. The stock media service searches the
free libraries, keeps only files whose licence allows a monetised
derivative video, and saves each one with its credit. Footage becomes
real moving shots; photographs get the usual camera move.

Run with ``--fetch-only`` to search and download without rendering, so
the picks can be looked at first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
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

NAME = "Apollo 11 in One Minute"
FOLDER = Path(r"D:\youtube_library\apollo-11-stock")
PICKS = FOLDER / "picks.json"
MUSIC = r"D:\AIVideoManager\music\Aftermath.mp3"
DESKTOP = Path(r"C:\Users\Gulfam\Desktop\Apollo 11 - one minute.mp4")
NARRATOR = "en_GB-cori-medium"

# (key, purpose, narration, search query, preferred kind, footage alignment)
# "end" uses a short clip's last seconds, where a launch clip's liftoff is.
SCENES = [
    ("s01", "The rocket",
     "In May nineteen sixty-nine, a Saturn Five rocket rolls out to its launch pad in Florida. Its job is to carry three men to the Moon.",
     "Apollo 11 rollout", "image", "start"),
    ("s02", "The crew",
     "Neil Armstrong, Michael Collins, and Buzz Aldrin have trained for years for these eight days.",
     "Apollo 11 crew portrait", "image", "start"),
    ("s03", "Liftoff",
     "On July sixteenth, at nine thirty-two in the morning, five huge engines ignite, and the rocket climbs into the sky.",
     "Apollo 11 launch", "video", "end"),
    ("s04", "The lunar module",
     "Four days later, Armstrong and Aldrin undock in the lunar module Eagle, while Collins stays behind in orbit.",
     "Apollo 11 Lunar Module Eagle", "image", "start"),
    ("s05", "The landing",
     "Low on fuel, Armstrong steers past a field of boulders and sets down in the Sea of Tranquility.",
     "Apollo 11 Lunar Module footpad lunar surface", "image", "start"),
    ("s06", "The first step",
     "Hours later, he climbs down the ladder and becomes the first person to stand on the Moon.",
     "Apollo 11 first step", "video", "start"),
    ("s07", "The flag",
     "Aldrin follows. They plant a flag, set up experiments, and gather about twenty-two kilograms of rock.",
     "Aldrin flag Apollo 11 Moon", "image", "start"),
    ("s08", "Coming home",
     "Eight days after launch, Columbia splashes down in the Pacific, and all three men come home.",
     "Apollo 11 splashdown recovery", "video", "start"),
]
ALIGN = {key: align for key, *_rest, align in SCENES}


async def fetch_all() -> dict[str, dict]:
    status = await ffmpeg.detect()
    if not status.ffmpeg_path:
        raise SystemExit("FFmpeg is needed to cut the footage.")
    used: set[str] = set()
    picks: dict[str, dict] = {}
    for key, _purpose, _narration, query, kind, _align in SCENES:
        kinds = [kind, "image"] if kind == "video" else [kind]
        chosen = None
        for wanted in kinds:
            for candidate in await stock_media.search(query, wanted):
                if candidate.page_url in used:
                    continue
                try:
                    fetched = await stock_media.fetch(
                        candidate, FOLDER, ffmpeg_path=status.ffmpeg_path, name=key
                    )
                except stock_media.StockMediaError as exc:
                    print(f"{key}: skipped {candidate.title[:70]}: {exc}", flush=True)
                    continue
                chosen = fetched
                break
            if chosen:
                break
        if chosen is None:
            print(f"{key}: nothing usable for '{query}'. This shot has no picture.", flush=True)
            continue
        used.add(chosen.candidate.page_url)
        picks[key] = {
            "kind": chosen.candidate.kind,
            "title": chosen.candidate.title,
            "licence": chosen.candidate.licence,
            "credit": chosen.candidate.credit_line,
            "still": chosen.still_path.name,
            "footage": str(chosen.footage_path) if chosen.footage_path else None,
            "source_duration_s": chosen.candidate.duration_s,
            "excerpt_start_s": chosen.candidate.extra.get("excerpt_start_s"),
        }
        print(f"{key}: {chosen.candidate.kind} {chosen.candidate.licence} | {chosen.candidate.title[:90]}", flush=True)
    PICKS.write_text(json.dumps(picks, indent=2), encoding="utf-8")
    return picks


def plan(picks: dict[str, dict]) -> dict:
    scenes = []
    for index, (key, purpose, narration, _query, _kind, _align) in enumerate(SCENES):
        scenes.append({
            "scene_key": key,
            "purpose": purpose,
            "target_duration_s": 7.0,
            "narration": narration,
            "dialogue": [],
            "characters": [],
            "image_hint": picks[key]["still"],
            "camera": "slow push in" if index % 2 else "gentle pan right",
            "transition": "cut",
            "preferred_method": "camera_move",
        })
    return {
        "title": "Apollo 11 in One Minute",
        "logline": "Eight days in July 1969, told with NASA's own film and photographs.",
        "synopsis": "From the launch pad in Florida to the Sea of Tranquility and home again.",
        "scenes": scenes,
    }


async def build(picks: dict[str, dict]) -> None:
    async with session_scope() as session:
        folder = (
            await session.execute(select(ImageFolder).where(ImageFolder.path == str(FOLDER)))
        ).scalar_one_or_none()
        if folder is None:
            folder = ImageFolder(path=str(FOLDER), recursive=False)
            session.add(folder)
            await session.flush()
        progress = await image_service.scan_folder(session, folder)
        print("scan:", progress.state, progress.message, flush=True)

    async with session_scope() as session:
        folder = (
            await session.execute(select(ImageFolder).where(ImageFolder.path == str(FOLDER)))
        ).scalar_one()
        voice = (
            await session.execute(select(VoiceProfile).where(VoiceProfile.name == NARRATOR))
        ).scalar_one()
        project = (
            await session.execute(select(Project).where(Project.name == NAME))
        ).scalar_one_or_none()
        values = dict(
            target_duration_s=60,
            resolution="1080p",
            aspect_ratio="16:9",
            fps=24,
            image_folder_id=folder.id,
            narrator_voice_id=voice.id,
            stills_ai_generated=False,
            music_path=MUSIC,
            music_gain_db=-20,
        )
        if project is None:
            project = await project_service.create_project(
                session,
                name=NAME,
                niche="history documentary",
                audience="general",
                story_style="documentary",
                brief="The Apollo 11 mission in one minute, told with public domain NASA media.",
                **values,
            )
            print("created project", project.id, flush=True)
        else:
            await project_service.update_project(session, project.id, values)
            print("using project", project.id, flush=True)
        project_id = project.id

        script = await script_service.accept_paste(session, project_id, json.dumps(plan(picks)))
        print("script", script.id, "valid", script.is_valid, "est", script.estimated_duration_s, flush=True)
        if not script.is_valid:
            print(json.dumps(script.validation.get("errors"), indent=2))
            raise SystemExit(1)
        await script_service.approve(session, script.id)

    async with session_scope() as session:
        speech_plan = await speech_service.plan_segments(session, project_id)
        print("speech plan", speech_plan, flush=True)
    async with session_scope() as session:
        outcome = await speech_service.synthesise_project(session, project_id)
        print("speech", outcome, flush=True)
        if outcome.failed or not outcome.measured_s:
            raise SystemExit("The narration was not spoken, so there is nothing to render.")

    async with session_scope() as session:
        await routing_service.plan_shots(session, project_id)
        run = await routing_service.render_local_shots(session, project_id)
        print("shots", run.rendered, "failed", run.failed, run.errors, flush=True)

    status = await ffmpeg.detect()
    async with session_scope() as session:
        project = await project_service.get_project(session, project_id)
        script = await script_service.approved_script(session, project_id)
        width, height = project_service.frame_size(project.resolution, project.aspect_ratio)
        scenes = (
            await session.execute(select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index))
        ).scalars().all()
        clips_dir = routing_service.clips_dir(project)
        for scene in scenes:
            pick = picks.get(scene.scene_key) or {}
            if not pick.get("footage"):
                continue
            current = await routing_service._selected_clip(session, scene.id)
            if current is None or current.duration_s is None:
                raise SystemExit(f"{scene.scene_key}: no camera-move clip to take the timing from.")
            source = Path(pick["footage"])
            info = await ffmpeg.probe(source)
            source_s = float(info.duration_s or 0.0)
            align = ALIGN.get(scene.scene_key, "start")
            start = max(source_s - float(current.duration_s), 0.0) if align == "end" else 0.0
            destination = clips_dir / f"{scene.scene_key}-footage.mp4"
            await stock_media.render_footage(
                status.ffmpeg_path, source, destination,
                width=width, height=height, fps=project.fps,
                duration_s=float(current.duration_s), source_s=source_s, start_s=start,
            )
            made = await ffmpeg.probe(destination)
            if made.duration_s is None or abs(made.duration_s - current.duration_s) > 0.05:
                raise SystemExit(f"{scene.scene_key}: footage shot is {made.duration_s}s, needs {current.duration_s}s.")
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
                    "credit": pick["credit"],
                    "licence": pick["licence"],
                    "source_title": pick["title"],
                    "excerpt_start_s": pick.get("excerpt_start_s"),
                    "shot_start_in_excerpt_s": round(start, 3),
                    "motion": "real footage, not generated",
                },
            ))
            print(f"{scene.scene_key}: footage shot {made.duration_s}s", flush=True)

    async with session_scope() as session:
        result = await render_service.render_project(session, project_id)
        print("render", result.status, result.duration_s, result.output_path, flush=True)
        print("problems", result.problems, flush=True)
        print("checks", json.dumps(result.checks, default=str)[:1500], flush=True)
        if not result.succeeded or not result.render_id:
            raise SystemExit("The render did not pass its checks.")
        assets = await publishing.prepare(session, result.render_id)
        print("disclosure", assets.disclosure, flush=True)
        print("description\n" + assets.description, flush=True)
    shutil.copy2(result.output_path, DESKTOP)
    print("desktop", DESKTOP, DESKTOP.stat().st_size, flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--reuse-picks", action="store_true")
    args = parser.parse_args()
    if args.reuse_picks and PICKS.exists():
        picks = json.loads(PICKS.read_text(encoding="utf-8"))
    else:
        picks = await fetch_all()
    missing = [key for key, *_ in SCENES if key not in picks]
    if missing:
        raise SystemExit(f"No usable media for {missing}.")
    if args.fetch_only:
        return
    await build(picks)


if __name__ == "__main__":
    asyncio.run(main())
