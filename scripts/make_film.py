"""Make a finished film from a spec file, or from a project already set up.

    python scripts/make_film.py spec.json
    python scripts/make_film.py --project-id 12

The spec is the script the app already accepts (title, logline, synopsis,
scenes with narration and image hints, and the director's fields: beat,
chapter_title, countdown_number, lower_third, music_mood, ambience,
pause_after_s), plus what the project needs around it:

    {
      "name": "...", "niche": "history documentary", "brief": "...",
      "stills_folder": "D:/youtube_library/some-film/stills",
      "voice": "en_US-norman-medium"   or   "kokoro:am_michael",
      "character_voices": {"Lena": "kokoro:af_heart"},   # optional, per character
      "music_path": null,               # null: music comes from the library
      "finishing": {"look": "archive", "end_card": "Thanks for watching"},
      "title": "...", "logline": "...", "synopsis": "...", "format": "documentary",
      "scenes": [{"scene_key": "s01", "narration": "...", "image_hint": "s01.jpg",
                  "beat": "hook",
                  "footage": {"path": "D:/.../film.webm", "credit": "...",
                              "licence": "Public domain", "start_s": null}}]
    }

A scene's ``footage`` puts real film under that line; with no ``start_s``
the longest clean shot in the file is found with PySceneDetect.

Everything lands in the render folder with ``publish.json`` beside it.
Nothing is uploaded and nothing is copied to the Desktop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Character, ImageFolder, Project, VoiceProfile  # noqa: E402
from app.services import film  # noqa: E402
from app.services import images as image_service  # noqa: E402
from app.services import projects as project_service  # noqa: E402
from app.services import scripts as script_service  # noqa: E402
from app.services import speech as speech_service  # noqa: E402

SCRIPT_KEYS = ("title", "logline", "synopsis", "format", "scenes")


async def _voice(session, name: str | None) -> VoiceProfile | None:
    if not name:
        return await speech_service.default_voice(session)
    if name.startswith("kokoro:"):
        return await speech_service.install_kokoro_voice(session, name.split(":", 1)[1])
    row = (
        await session.execute(select(VoiceProfile).where(VoiceProfile.name == name))
    ).scalar_one_or_none()
    if row is None or not row.installed:
        raise SystemExit(f"The voice {name!r} is not installed.")
    return row


async def from_spec(spec: dict) -> tuple[int, dict[str, film.Footage]]:
    footage: dict[str, film.Footage] = {}
    for scene in spec.get("scenes") or []:
        item = scene.pop("footage", None)
        if item:
            footage[scene["scene_key"]] = film.Footage(
                path=Path(item["path"]),
                credit=item["credit"],
                licence=item.get("licence") or "Public domain",
                start_s=item.get("start_s"),
                window_s=float(item.get("window_s") or 0.0),
            )

    async with session_scope() as session:
        folder_id = None
        if spec.get("stills_folder"):
            path = str(Path(spec["stills_folder"]))
            folder = (
                await session.execute(select(ImageFolder).where(ImageFolder.path == path))
            ).scalar_one_or_none()
            if folder is None:
                folder = ImageFolder(path=path, recursive=False)
                session.add(folder)
                await session.flush()
            progress = await image_service.scan_folder(session, folder)
            print("stills:", progress.state, progress.message, flush=True)
            folder_id = folder.id
        voice = await _voice(session, spec.get("voice"))
        values = {
            "target_duration_s": int(spec.get("target_duration_s") or 300),
            "resolution": spec.get("resolution") or "1080p",
            "aspect_ratio": spec.get("aspect_ratio") or "16:9",
            "fps": int(spec.get("fps") or 24),
            "image_folder_id": folder_id,
            "narrator_voice_id": voice.id if voice else None,
            "stills_ai_generated": spec.get("stills_ai_generated", False),
            "music_path": spec.get("music_path"),
            "music_gain_db": float(spec.get("music_gain_db") or -20),
        }
        project = (
            await session.execute(select(Project).where(Project.name == spec["name"]))
        ).scalar_one_or_none()
        if project is None:
            project = await project_service.create_project(
                session, name=spec["name"], niche=spec.get("niche") or "documentary",
                audience=spec.get("audience") or "general",
                story_style=spec.get("story_style") or "documentary",
                brief=spec.get("brief") or spec.get("logline") or spec["name"],
                **values,
            )
        else:
            await project_service.update_project(session, project.id, values)
        project_id = project.id
        script = await script_service.accept_paste(
            session, project_id, json.dumps({k: spec[k] for k in SCRIPT_KEYS if k in spec})
        )
        print("script", script.id, "valid", script.is_valid, "words", script.word_count, flush=True)
        if not script.is_valid:
            print(json.dumps((script.validation or {}).get("errors"), indent=2))
            raise SystemExit(1)
        await script_service.approve(session, script.id)
        # A character can have a voice of its own; the narrator speaks the rest.
        for name, voice_name in (spec.get("character_voices") or {}).items():
            row = await _voice(session, voice_name)
            character = (
                await session.execute(
                    select(Character).where(Character.project_id == project_id, Character.name == name)
                )
            ).scalar_one_or_none()
            if character is None or row is None:
                raise SystemExit(f"The script has no character named {name!r}, or the voice is missing.")
            character.voice_profile_id = row.id
            print("character", name, "speaks as", row.name, flush=True)
    return project_id, footage


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("spec", nargs="?", type=Path, help="film spec JSON")
    parser.add_argument("--project-id", type=int, help="an existing project with an approved script")
    args = parser.parse_args()
    if not args.spec and not args.project_id:
        parser.error("give a spec file or --project-id")

    footage: dict[str, film.Footage] = {}
    finishing = None
    if args.spec:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        finishing = spec.get("finishing")
        project_id, footage = await from_spec(spec)
    else:
        project_id = args.project_id

    run = await film.make_film(project_id, finishing=finishing, footage=footage)
    for line in run.log:
        print(line, flush=True)
    print(json.dumps(run.as_dict(), indent=2), flush=True)
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
