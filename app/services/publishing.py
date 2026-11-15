"""Preparing what a video needs to be published, without publishing it.

Nothing in this module uploads anything, contacts YouTube, or takes any
action that cannot be undone. It writes text and an image to disk next to
the render, and that is all. Publishing remains something you do yourself,
deliberately, after reading what is here.

Two things are deliberate rather than convenient:

**The disclosure is derived from the render, not written by hand.** It
states which parts of the video a model animated and which parts are a
camera move over a still, using the figures the render actually measured.
If no model produced motion, it says so plainly. A disclosure that could
be quoted to imply more AI generation than happened would be worse than
none, and so would one implying less.

**The thumbnail is a real frame of the real file.** It is extracted with
FFmpeg from the finished video, so it cannot show something the video does
not contain. There is no generated artwork here and no text burnt over it;
a thumbnail that promises a frame the video never shows is the oldest lie
on the platform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import RenderStatus
from app.models import AudioSegment, Project, Render, Scene, VoiceProfile
from app.services import ffmpeg
from app.services import projects as project_service
from app.services import render as render_service
from app.services import routing as routing_service
from app.services import scripts as script_service
from app.services import stock_media
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

#: YouTube rejects a title over this, so the builder trims to fit rather
#: than handing over something that will be refused on upload.
TITLE_LIMIT = 100
#: Description limit on the same platform.
DESCRIPTION_LIMIT = 5000
#: Total characters across all tags, which is the real constraint; the
#: per-tag limit is 30.
KEYWORDS_TOTAL_LIMIT = 460
KEYWORD_LIMIT = 30
MAX_KEYWORDS = 15

#: Where in the video to take the thumbnail from, as a fraction of its
#: length. A little past the opening, which is usually a title card.
THUMBNAIL_AT = 0.18
THUMBNAIL_TIMEOUT_S = 120

#: Words too common to be worth a tag.
_STOPWORDS = frozenset(
    """a about an and are as at be been but by can could did do does for from had has
    have he her his how i if in into is it its just like me more much my no not of
    on once one only or our out over own said she should some such than that the
    their them then there these they this those to too up us very was we were what
    when where which who why will with would you your
    still down before after already first again back same whole just onto""".split()
)


class PublishError(Exception):
    """Something that must be fixed before assets can be prepared."""


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def trim(text: str, limit: int) -> str:
    """Cut to a limit on a word boundary, without an ellipsis.

    An ellipsis in a title reads as a cliffhanger rather than as a length
    problem, so the text simply stops at the last whole word that fits.
    """
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-")


def keywords(project: Project, title: str, scenes: list[Scene]) -> list[str]:
    """Tags drawn only from words this project actually used.

    Nothing here is researched, trending, or inferred from search volume;
    no such data is available offline, and inventing it would be worse than
    a short list. These are the project's own recurring words.
    """
    seeds: list[str] = []
    for phrase in (project.niche, project.audience, project.story_style):
        phrase = (phrase or "").strip().lower()
        if phrase and len(phrase) <= KEYWORD_LIMIT:
            seeds.append(phrase)

    counts: dict[str, int] = {}
    body = " ".join(
        [title] + [scene.narration or "" for scene in scenes] + [
            scene.purpose or "" for scene in scenes
        ]
    ).lower()
    for word in re.findall(r"[a-z][a-z'-]{3,}", body):
        word = word.strip("'-")
        if word in _STOPWORDS or len(word) > KEYWORD_LIMIT:
            continue
        counts[word] = counts.get(word, 0) + 1

    ranked = [word for word, count in sorted(
        counts.items(), key=lambda item: (-item[1], item[0])
    ) if count > 1]

    out: list[str] = []
    budget = KEYWORDS_TOTAL_LIMIT
    for candidate in seeds + ranked:
        if candidate in out or len(candidate) + 1 > budget:
            continue
        out.append(candidate)
        budget -= len(candidate) + 1
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def disclosure(
    generated_motion_s: float,
    camera_move_s: float,
    voice: str,
    *,
    stills_ai_generated: bool | None = None,
    footage_s: float = 0.0,
) -> str:
    """A truthful statement of what in this video was machine-made.

    Written from measured seconds, so it cannot drift from the file. The
    narration is always named as synthetic, because it is, and a synthetic
    voice is the part a viewer is most likely to be misled by.

    Where the stills came from is the one thing here the app cannot
    measure. If nothing has been recorded about them, the disclosure says
    nothing about them, because a sentence claiming AI-generated artwork
    would be a guess, and so would one denying it.
    """
    parts = [
        "This video was assembled with software and includes synthetic content.",
        f"The narration is a text-to-speech voice ({voice}); no person speaks in it.",
    ]
    if generated_motion_s > 0:
        parts.append(
            f"{generated_motion_s:.0f} seconds of the picture were animated by an "
            f"AI video model, and {camera_move_s:.0f} seconds are a camera move "
            "over a still image, which is editing rather than animation."
        )
    elif footage_s > 0:
        parts.append(
            "No part of the picture was animated by an AI model. "
            f"{footage_s:.0f} seconds are real footage from openly licensed media "
            f"libraries, and {camera_move_s:.0f} seconds are a camera move over still "
            "images, which is editing rather than animation."
        )
    else:
        parts.append(
            "No part of the picture was animated by an AI model. The movement you "
            f"see is a camera move over still images ({camera_move_s:.0f} seconds "
            "of it), which is editing rather than animation."
        )
    if stills_ai_generated is True:
        parts.append("The still images were generated by an AI image model.")
    elif stills_ai_generated is False:
        parts.append("The still images are not AI-generated.")
    return " ".join(parts)


def description(
    *,
    title: str,
    logline: str,
    synopsis: str,
    chapters: list[tuple[float, str]],
    disclosure_text: str,
    credits: list[str] | None = None,
    music: str | None = None,
) -> str:
    """The description body, with chapters and the disclosure at the end.

    Credits sit next to the disclosure and survive a trim with it: a CC BY
    file used without its credit line is a licence breach, not a style lapse.
    """
    blocks: list[str] = []
    if logline.strip():
        blocks.append(logline.strip())
    if synopsis.strip() and synopsis.strip() != logline.strip():
        blocks.append(synopsis.strip())
    if chapters:
        lines = ["Chapters:"]
        for start_s, label in chapters:
            minutes, seconds = divmod(int(start_s), 60)
            lines.append(f"{minutes}:{seconds:02d} {label}")
        blocks.append("\n".join(lines))
    tail = disclosure_text
    if credits:
        tail = disclosure_text + "\n\nMedia credits:\n" + "\n".join(credits)
    if music:
        tail = tail + "\n\n" + music
    blocks.append(tail)
    body = "\n\n".join(blocks)
    if len(body) <= DESCRIPTION_LIMIT:
        return body
    # The disclosure and credits must survive a trim, so they are kept whole
    # and the synopsis above them gives way instead.
    room = DESCRIPTION_LIMIT - len(tail) - 2
    head = "\n\n".join(blocks[:-1])
    return f"{trim(head, max(room, 0))}\n\n{tail}"


#: A chapter list only displays if the first mark is at zero and there are
#: at least this many, so a shorter list is reported as none at all.
MIN_CHAPTERS = 3
#: Chapters closer together than this are not usable as chapters.
MIN_CHAPTER_GAP_S = 10.0


def chapters_from(
    scenes: list[Scene], shot_starts: dict[str, float]
) -> list[tuple[float, str]]:
    """Chapter marks at the shot boundaries the render actually used.

    ``shot_starts`` maps a scene key to where that shot begins, worked out
    from the measured clip lengths rather than from the target durations.
    Shots that are too close together are merged away, because a chapter
    every four seconds is worse than no chapters.
    """
    marks: list[tuple[float, str]] = []
    for scene in scenes:
        start = shot_starts.get(scene.scene_key)
        if start is None:
            continue
        label = trim((scene.purpose or scene.narration or scene.scene_key or ""), 60)
        if not label:
            continue
        if marks and start - marks[-1][0] < MIN_CHAPTER_GAP_S:
            continue
        marks.append((float(start), label))

    if len(marks) < MIN_CHAPTERS or marks[0][0] > 0.5:
        return []
    marks[0] = (0.0, marks[0][1])
    return marks


def shot_starts_from(plan_scenes: list[dict[str, Any]]) -> dict[str, float]:
    """Where each shot begins, from the clip lengths that were measured.

    A shot with no measured clip is left out, and everything after it is
    left out too: once one length is unknown, no later start is knowable.
    """
    starts: dict[str, float] = {}
    cursor = 0.0
    for entry in plan_scenes:
        clip_s = entry.get("clip_duration_s")
        if clip_s is None:
            break
        key = str(entry.get("scene_key") or "")
        if key:
            starts[key] = round(cursor, 3)
        cursor += float(clip_s) - float(entry.get("overlap_after_s") or 0.0)
    return starts


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------


async def narrating_voice(
    session: AsyncSession, project_id: int, project: Project
) -> str:
    """The voice that actually recorded this project's narration.

    Read from the recorded segments rather than from the project's current
    setting, because the setting can be changed after a render and the
    disclosure has to describe the file that exists.
    """
    voice_id = (
        await session.execute(
            select(AudioSegment.voice_profile_id)
            .where(
                AudioSegment.project_id == project_id,
                AudioSegment.voice_profile_id.is_not(None),
            )
            .distinct()
        )
    ).scalars().all()
    ids = [row for row in voice_id if row is not None]
    if not ids:
        ids = [project.narrator_voice_id] if project.narrator_voice_id else []
    names: list[str] = []
    for voice_id in ids:
        voice = await session.get(VoiceProfile, voice_id)
        if voice is not None and voice.name and voice.name not in names:
            names.append(voice.name)
    if not names:
        return "an unnamed synthetic voice"
    return " and ".join(names)


def thumbnail_at(duration_s: float, first_cue_s: float | None = None) -> float:
    """Where to take the thumbnail, in seconds.

    A frame taken while someone is speaking has the burned caption on it.
    The lead-in before the first cue is the picture by itself. When the
    first word starts immediately, or the cue times are unknown, the frame
    is taken a little way into the video instead.
    """
    if first_cue_s is not None and first_cue_s >= 0.2:
        return round(min(0.12, first_cue_s * 0.4), 3)
    if duration_s <= 0:
        return 0.0
    return round(min(duration_s * THUMBNAIL_AT, max(duration_s - 0.05, 0.0)), 3)


def first_cue_start_s(srt_path: str | None) -> float | None:
    """The start of the first caption, read from the subtitle file."""
    if not srt_path:
        return None
    path = Path(srt_path)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)", text)
    if match is None:
        return None
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def music_credit(music_path: str | None) -> str | None:
    """The music line for the description, from the file beside the track.

    A licence note named like the audio file (``Aftermath-license.txt`` next
    to ``Aftermath.mp3``) is quoted as its first line. With no note, only
    the file name is given. The author is never guessed.
    """
    if not music_path:
        return None
    path = Path(music_path)
    note = path.with_name(path.stem + "-license.txt")
    if note.is_file():
        for line in note.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                return f"Music: {line}"
    if path.name:
        return f"Music file: {path.name}"
    return None


def sound_credits(validation: dict[str, Any] | None) -> str | None:
    """Every music and effect credit the finished mix actually used."""
    lines = [str(line) for line in (validation or {}).get("sound_credits") or [] if line]
    if not lines:
        return None
    music = [line for line in lines if "incompetech" in line or line.startswith('"')]
    rest = [line for line in lines if line not in music]
    parts = [f"Music: {line}" for line in music] + [
        line if line.lower().startswith("sound") else f"Sound: {line}" for line in rest
    ]
    return "\n".join(parts)


def thumbnail_command(
    ffmpeg_path: str, source: Path, destination: Path, at_s: float
) -> list[str]:
    """One frame of the finished video, written as a JPEG."""
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(at_s, 0):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(destination),
    ]


async def extract_thumbnail(
    source: Path, destination: Path, *, ffmpeg_path: str, at_s: float
) -> Path | None:
    """Pull a frame out of the video, or return None having said why."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = thumbnail_command(ffmpeg_path, source, destination, at_s)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=THUMBNAIL_TIMEOUT_S
        )
    except TimeoutError:
        process.kill()
        log.warning("Extracting a thumbnail from %s timed out", source)
        return None
    if process.returncode != 0 or not destination.exists():
        log.warning(
            "Could not take a thumbnail frame: %s",
            stderr.decode("utf-8", "replace").strip()[-200:],
        )
        return None
    return destination


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


@dataclass
class PublishAssets:
    """Everything prepared for an upload you would still do by hand."""

    render_id: int
    title: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    disclosure: str = ""
    chapters: list[tuple[float, str]] = field(default_factory=list)
    thumbnail_path: str = ""
    manifest_path: str = ""
    video_path: str = ""
    subtitle_path: str = ""
    duration_s: float | None = None
    generated_motion_s: float = 0.0
    camera_move_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "render_id": self.render_id,
            "title": self.title,
            "description": self.description,
            "keywords": self.keywords,
            "disclosure": self.disclosure,
            "chapters": [
                {"start_s": start, "label": label} for start, label in self.chapters
            ],
            "video_path": self.video_path,
            "subtitle_path": self.subtitle_path,
            "thumbnail_path": self.thumbnail_path,
            "duration_s": self.duration_s,
            "generated_motion_s": self.generated_motion_s,
            "camera_move_s": self.camera_move_s,
            "notes": self.notes,
            "published": False,
            "prepared_at": utcnow().isoformat(),
        }


async def prepare(session: AsyncSession, render_id: int) -> PublishAssets:
    """Write the upload assets for a render that passed its checks.

    A render that failed its checks is refused. Preparing a tidy title and
    thumbnail for a broken file only makes it likelier to be uploaded.
    """
    record = await session.get(Render, render_id)
    if record is None:
        raise PublishError("There is no render with that id.")
    if record.status != RenderStatus.COMPLETED:
        raise PublishError(
            "That render did not pass its checks, so there is nothing worth "
            "publishing yet."
        )
    if not record.output_path or not Path(record.output_path).exists():
        raise PublishError("The rendered file is no longer on disk.")

    project = await project_service.get_project(session, record.project_id)
    script = await script_service.approved_script(session, record.project_id)
    if script is None:
        raise PublishError("The approved script has gone, so there is no title.")

    scenes = list(
        (
            await session.execute(
                select(Scene)
                .where(Scene.script_id == script.id)
                .order_by(Scene.order_index)
            )
        )
        .scalars()
        .all()
    )
    report = await routing_service.motion_report(session, record.project_id)
    camera_move_s = float(report.get("camera_move_s") or 0.0)
    footage_s = float(report.get("real_footage_s") or 0.0)
    credits = await stock_media.credits_for_scenes(session, scenes)

    voice_name = await narrating_voice(session, record.project_id, project)

    assets = PublishAssets(
        render_id=record.id,
        video_path=record.output_path or "",
        subtitle_path=record.subtitle_path or "",
        duration_s=record.duration_s,
        generated_motion_s=record.generated_motion_s,
        camera_move_s=camera_move_s,
    )

    assets.title = trim(script.title or project.name, TITLE_LIMIT)
    assets.disclosure = disclosure(
        record.generated_motion_s,
        camera_move_s,
        voice_name,
        stills_ai_generated=project.stills_ai_generated,
        footage_s=footage_s,
    )
    if project.stills_ai_generated is None:
        assets.notes.append(
            "The disclosure says nothing about where the still images came from, "
            "because the project does not record it. Set it on the project if the "
            "platform you are uploading to wants that stated."
        )
    plan = await render_service.plan_render(session, record.project_id)
    starts = shot_starts_from(plan.scenes)
    if record.duration_s and abs(plan.planned_duration_s - record.duration_s) > 1.0:
        # The project has been edited since this file was made, so the
        # current shot lengths no longer describe it and chapter marks
        # taken from them would point at the wrong moments.
        starts = {}
        assets.notes.append(
            "No chapters: the shots have changed since this file was rendered, so "
            "their start times no longer match it. Render again to get chapters."
        )
    assets.chapters = chapters_from(scenes, starts)
    if not assets.chapters and starts:
        assets.notes.append(
            f"No chapters: a chapter list needs at least {MIN_CHAPTERS} marks "
            f"starting at zero and at least {MIN_CHAPTER_GAP_S:.0f}s apart, and "
            "this cut does not have them."
        )
    assets.description = description(
        title=assets.title,
        logline=script.logline or "",
        synopsis=script.synopsis or "",
        chapters=assets.chapters,
        disclosure_text=assets.disclosure,
        credits=credits,
        music=sound_credits(record.validation) or music_credit(project.music_path),
    )
    assets.keywords = keywords(project, assets.title, scenes)
    if not assets.keywords:
        assets.notes.append(
            "No keywords: nothing in the script repeats often enough to be worth "
            "a tag, and no search data is available offline to invent them from."
        )

    folder = Path(record.output_path).parent
    status = await ffmpeg.detect()
    if status.available and status.ffmpeg_path and record.duration_s:
        frame = await extract_thumbnail(
            Path(record.output_path),
            folder / "thumbnail.jpg",
            ffmpeg_path=status.ffmpeg_path,
            at_s=thumbnail_at(
                float(record.duration_s),
                first_cue_start_s(record.subtitle_path),
            ),
        )
        if frame is not None:
            assets.thumbnail_path = str(frame)
            record.thumbnail_path = str(frame)
        else:
            assets.notes.append(
                "No thumbnail: the frame could not be taken from the video."
            )
    else:
        assets.notes.append(
            "No thumbnail: FFmpeg is needed to take a frame from the video."
        )

    manifest = folder / "publish.json"
    payload = assets.as_dict()
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    assets.manifest_path = str(manifest)
    payload["manifest_path"] = assets.manifest_path

    record.publish_assets = payload
    await session.commit()
    log.info("Prepared publishing assets for render %s", record.id)
    return assets


async def assets_for(session: AsyncSession, render_id: int) -> dict[str, Any] | None:
    """What was prepared last time, if anything was."""
    record = await session.get(Render, render_id)
    if record is None:
        return None
    return record.publish_assets or None
