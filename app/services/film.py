"""One button from an approved script to a finished, checked film.

The chain is the one a person would run by hand: switch the finishing pass
on, speak every line, cut every shot to its measured line, lay real
footage where the project has it, render, check, and write the upload
assets. Then the extras an upload needs: a titled thumbnail, a 45-second
vertical Short and a contact sheet to look the cut over at a glance.

Nothing is uploaded. ``publish.json`` says ``"published": false`` and the
files sit in the render folder until a person decides.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.constants import AnimationMethod, ClipStatus
from app.db import session_scope
from app.models import Scene, SceneClip
from app.services import directing, ffmpeg, graphics, production, publishing
from app.services import projects as project_service
from app.services import render as render_service
from app.services import routing as routing_service
from app.services import scripts as script_service
from app.services import speech as speech_service
from app.services import stock_media
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

SHORT_S = 45.0
SHEET_FRAMES = 12
#: A source longer than this is searched for its cleanest shot.
LONG_SOURCE_S = 60.0


@dataclass
class Footage:
    """Real film for one shot, with the credit that must travel with it."""

    path: Path
    credit: str
    licence: str = "Public domain"
    #: Where to start in the file; None lets shot detection choose.
    start_s: float | None = None
    #: Seconds of picture to stay on; 0 means as long as the line.
    window_s: float = 0.0
    provider_key: str = "local_footage"


@dataclass
class FilmRun:
    project_id: int
    stage: str = "starting"
    ok: bool = False
    render_id: int | None = None
    video: str = ""
    short: str = ""
    thumbnail: str = ""
    contact_sheet: str = ""
    manifest: str = ""
    duration_s: float | None = None
    problems: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def note(self, stage: str, message: str) -> None:
        self.stage = stage
        self.log.append(f"{stage}: {message}")
        log.info("film %s %s: %s", self.project_id, stage, message)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: The latest run per project, for the dashboard to show.
RUNS: dict[int, FilmRun] = {}


def _scene_fields(scene: Scene) -> dict[str, Any]:
    direction = dict(scene.direction or {})
    return {
        **direction,
        "narration": scene.narration,
        "purpose": scene.purpose,
        "environment": scene.environment,
        "transition": direction.get("transition") or scene.transition,
        "sfx": direction.get("sfx") or scene.sfx_cue,
    }


async def _direct(project_id: int, run: FilmRun, finishing: dict[str, Any] | None) -> None:
    """Switch the finishing pass on and make sure every shot is directed."""
    async with session_scope() as session:
        project = await project_service.get_project(session, project_id)
        script = await script_service.approved_script(session, project_id)
        if script is None:
            raise FilmError("Approve a script first: there is nothing to film.")
        settings = {
            **production.DEFAULTS,
            "look": production.default_look(project.niche or ""),
            "title_card": script.title or project.name,
            **(project.production or {}),
            **(finishing or {}),
            "enabled": True,
        }
        project.production = settings
        scenes = list(
            (
                await session.execute(
                    select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index)
                )
            ).scalars().all()
        )
        if any(not scene.direction for scene in scenes):
            film_format = str(settings.get("format") or "documentary")
            filled = directing.fill_defaults([_scene_fields(s) for s in scenes], film_format=film_format)
            for scene, direction in zip(scenes, filled):
                scene.direction = {**direction, **(scene.direction or {})}
        await session.commit()
        run.note("direct", f"{len(scenes)} shot(s), look {settings['look']}.")


async def _speak(project_id: int, run: FilmRun) -> None:
    async with session_scope() as session:
        await speech_service.plan_segments(session, project_id)
    async with session_scope() as session:
        outcome = await speech_service.synthesise_project(session, project_id)
    if outcome.failed or not outcome.measured_s:
        raise FilmError(
            "The narration was not fully spoken: "
            + ("; ".join(outcome.errors[:3]) or "no audio was measured.")
        )
    run.note("speech", f"{outcome.measured_s:.1f}s measured, {outcome.synthesised} new line(s).")


async def _shoot(project_id: int, run: FilmRun) -> None:
    async with session_scope() as session:
        await routing_service.plan_shots(session, project_id)
        shots = await routing_service.render_local_shots(session, project_id)
    if shots.failed:
        raise FilmError(f"{shots.failed} shot(s) failed: {'; '.join(shots.errors[:3])}")
    run.note("shots", f"{shots.rendered} shot(s) rendered.")


def _best_start(path: Path, need_s: float, length_s: float) -> float:
    """The start of the longest detected shot that can hold the line."""
    try:
        from app.services import shot_detect

        shots = shot_detect.detect_shots(path)
    except Exception as exc:  # noqa: BLE001 - detection is a nicety, not a requirement
        log.warning("Shot detection failed on %s: %s", path, exc)
        return 0.0
    spans = [(s["start_s"], s["end_s"]) for s in shots]
    fitting = [s for s in spans if s[1] - s[0] >= min(need_s, length_s)]
    pool = fitting or spans
    if not pool:
        return 0.0
    start, _end = max(pool, key=lambda s: s[1] - s[0])
    return max(0.0, min(start, length_s - need_s))


async def _lay_footage(project_id: int, footage: dict[str, Footage], run: FilmRun) -> None:
    if not footage:
        return
    status = await ffmpeg.detect()
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
        laid = 0
        for scene in scenes:
            item = footage.get(scene.scene_key)
            if item is None or not item.path.exists():
                continue
            current = await routing_service._selected_clip(session, scene.id)
            if current is None or current.duration_s is None:
                raise FilmError(f"{scene.scene_key}: no measured shot to replace with footage.")
            need = float(current.duration_s)
            info = await ffmpeg.probe(item.path)
            length = float(info.duration_s or 0.0)
            start = item.start_s
            if start is None:
                start = _best_start(item.path, need, length) if length > LONG_SOURCE_S else 0.0
            window = float(item.window_s or need)
            destination = clips_dir / f"{scene.scene_key}-footage.mp4"
            await stock_media.render_footage(
                status.ffmpeg_path, item.path, destination,
                width=width, height=height, fps=project.fps,
                duration_s=need, source_s=min(length, start + window), start_s=start,
            )
            made = await ffmpeg.probe(destination)
            if made.duration_s is None or abs(made.duration_s - need) > 0.08:
                raise FilmError(f"{scene.scene_key}: footage came out {made.duration_s}s, needs {need}s.")
            await routing_service._deselect_others(session, scene.id)
            session.add(SceneClip(
                scene_id=scene.id, project_id=project.id,
                method=AnimationMethod.STOCK_FOOTAGE, provider_key=item.provider_key,
                source_image_id=scene.image_id, file_path=str(destination),
                duration_s=current.duration_s, width=made.width, height=made.height,
                fps=made.fps, is_generated_motion=False, selected=True,
                status=ClipStatus.READY, completed_at=utcnow(),
                validation={
                    "credit": item.credit, "licence": item.licence,
                    "source_title": item.path.name, "excerpt_start_s": round(start, 3),
                    "motion": "real footage, not generated",
                },
            ))
            laid += 1
        await session.commit()
    run.note("footage", f"{laid} shot(s) on real footage.")


def _thumbnail(ffmpeg_path: str, video: Path, at_s: float, title: str, dest: Path) -> None:
    """A frame from the film with the title in the film's own typeface."""
    from PIL import Image, ImageDraw, ImageFont

    frame = dest.with_name("thumbnail-frame.jpg")
    subprocess.run(
        [ffmpeg_path, "-y", "-v", "error", "-ss", f"{at_s:.2f}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
         str(frame)],
        check=True,
    )
    image = Image.open(frame).convert("RGB")
    shade = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shade)
    for row in range(image.height // 2, image.height):
        alpha = int(200 * (row - image.height / 2) / (image.height / 2))
        draw.line([(0, row), (image.width, row)], fill=(0, 0, 0, alpha))
    image = Image.alpha_composite(image.convert("RGBA"), shade)
    words = title.upper().split()
    text = " ".join(words[:5])
    font_path = graphics.fonts_dir() / "BebasNeue-Regular.ttf"
    size = 150
    font = ImageFont.truetype(str(font_path), size) if font_path.exists() else ImageFont.load_default()
    draw = ImageDraw.Draw(image)
    while font_path.exists() and draw.textlength(text, font=font) > image.width - 100 and size > 60:
        size -= 8
        font = ImageFont.truetype(str(font_path), size)
    width = draw.textlength(text, font=font)
    draw.text(((image.width - width) / 2, image.height - size - 50), text, font=font,
              fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
    image.convert("RGB").save(dest, quality=92)
    frame.unlink(missing_ok=True)


def _short(ffmpeg_path: str, video: Path, dest: Path, width: int, height: int) -> None:
    crop_w = int(height * 9 / 16) // 2 * 2
    subprocess.run(
        [ffmpeg_path, "-y", "-v", "error", "-i", str(video), "-t", f"{SHORT_S:g}",
         "-vf", f"crop={crop_w}:{height}:(in_w-{crop_w})/2:0,scale=1080:1920,"
                f"fade=t=out:st={SHORT_S - 1.0:g}:d=1",
         "-af", f"afade=t=out:st={SHORT_S - 1.5:g}:d=1.5",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2",
         "-c:a", "aac", "-b:a", "160k", str(dest)],
        check=True,
    )


def _contact_sheet(ffmpeg_path: str, video: Path, duration_s: float, dest: Path) -> None:
    every = max(duration_s / SHEET_FRAMES, 0.5)
    subprocess.run(
        [ffmpeg_path, "-y", "-v", "error", "-i", str(video),
         "-vf", f"fps=1/{every:.3f},scale=480:-2,tile=4x3:padding=6:color=black",
         "-frames:v", "1", str(dest)],
        check=True,
    )


class FilmError(Exception):
    """A step of the chain could not be completed."""


async def make_film(
    project_id: int,
    *,
    finishing: dict[str, Any] | None = None,
    footage: dict[str, Footage] | None = None,
) -> FilmRun:
    """Run the whole chain for a project with an approved script."""
    run = FilmRun(project_id=project_id)
    RUNS[project_id] = run
    try:
        await _direct(project_id, run, finishing)
        await _speak(project_id, run)
        await _shoot(project_id, run)
        await _lay_footage(project_id, footage or {}, run)

        run.note("render", "Assembling, mixing and checking.")
        async with session_scope() as session:
            result = await render_service.render_project(session, project_id)
            run.problems.extend(result.problems)
            run.render_id = result.render_id
            if not result.succeeded or not result.render_id:
                raise FilmError(result.problems[0] if result.problems else "The render did not pass.")
            run.video = result.output_path
            run.duration_s = result.duration_s
            assets = await publishing.prepare(session, result.render_id)
            run.manifest = assets.manifest_path
            run.thumbnail = assets.thumbnail_path
            title = assets.title
            project = await project_service.get_project(session, project_id)
            width, height = project_service.frame_size(project.resolution, project.aspect_ratio)

        run.note("extras", "Thumbnail, Short and contact sheet.")
        status = await ffmpeg.detect()
        video = Path(run.video)
        folder = video.parent
        duration = float(run.duration_s or 0.0)
        try:
            thumb = folder / "thumbnail-titled.jpg"
            _thumbnail(status.ffmpeg_path, video, min(4.0, duration / 3), title, thumb)
            run.thumbnail = str(thumb)
        except Exception as exc:  # noqa: BLE001 - the plain frame from prepare() stays
            run.problems.append(f"The titled thumbnail could not be made ({exc}).")
        if duration >= SHORT_S + 5 and height <= width:
            short = folder / f"{video.stem} - short.mp4"
            _short(status.ffmpeg_path, video, short, width, height)
            run.short = str(short)
        sheet = folder / "contact-sheet.jpg"
        _contact_sheet(status.ffmpeg_path, video, duration, sheet)
        run.contact_sheet = str(sheet)

        manifest = Path(run.manifest)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload.update({
            "thumbnail_path": run.thumbnail,
            "short_path": run.short,
            "contact_sheet_path": run.contact_sheet,
            "published": False,
        })
        manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        run.ok = True
        run.note("done", f"{duration:.1f}s film, checked. Nothing uploaded.")
    except (FilmError, speech_service.SpeechError, render_service.RenderError,
            publishing.PublishError) as exc:
        if str(exc) not in run.problems:
            run.problems.insert(0, str(exc))
        run.note("failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - reported on the dashboard, logged in full
        log.exception("make_film failed for project %s", project_id)
        run.problems.insert(0, f"Unexpected error: {exc}")
        run.note("failed", str(exc))
    return run
