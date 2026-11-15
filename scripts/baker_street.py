"""Build the Baker Street video from the stills and the supplied lines.

The pictures were converted from the JFIF files in the zip, because the
library only scans JPEG, PNG, WebP and BMP. One file in the zip is an
exact duplicate of Hugo's feast and is not used.

Every speaker is read by the one installed voice. There is no second
voice on this machine, so Holmes, Watson, Mortimer and Sir Henry are not
distinct voices.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.db import session_scope
from app.models import ImageFolder
from app.services import images as image_service
from app.services import projects as project_service
from app.services import publishing
from app.services import render as render_service
from app.services import routing as routing_service
from app.services import scripts as script_service
from app.services import speech as speech_service

STILLS = r"D:\youtube_library\baker-street-stills"
NAME = "Baker Street"

# (key, purpose, image filename, narration, dialogue[(speaker, line)])
SCENES = [
    ("s01", "Title", "Gemini_Generated_Image_8546xe8546xe8546.jpg",
     "Dartmoor... an ancient land swallowed by fog, where dark legends never truly die.", []),
    ("s02", "221B Baker Street", "Gemini_Generated_Image_65yn0k65yn0k65yn.jpg",
     "Far from the moor, night settles over the rain-slicked streets of Victorian London.", []),
    ("s03", "Evening at 221B", "Gemini_Generated_Image_65yn0k65yn0k65yn (1).jpg",
     "", [("Watson", "A quiet evening, Holmes?"),
          ("Holmes", "Never quiet, Watson... merely the pause before a storm.")]),
    ("s04", "A visitor", "Gemini_Generated_Image_65yn0k65yn0k65yn (2).jpg",
     "", [("Dr. Mortimer", "Mr. Holmes! Forgive the intrusion, but I bring a matter of utter terror!")]),
    ("s05", "The stick", "Gemini_Generated_Image_65yn0k65yn0k65yn (3).jpg",
     "", [("Holmes", "A country doctor's walking stick... worn, yet full of secrets. What brings you to London, Dr. Mortimer?")]),
    ("s06", "The old manuscript", "Gemini_Generated_Image_65yn0k65yn0k65yn (4).jpg",
     "", [("Dr. Mortimer", "This manuscript from 1742 tells the grim origin of the Baskerville curse...")]),
    ("s07", "Hugo's feast", "Gemini_Generated_Image_65yn0k65yn0k65yn (5).jpg",
     "", [("Dr. Mortimer", "It began with Hugo Baskerville—a wicked, drunken cavalier who defied God and man.")]),
    ("s08", "The escape", "Gemini_Generated_Image_65yn0k65yn0k65yn (7).jpg",
     "", [("Dr. Mortimer", "He imprisoned a maiden, but under cover of night, she fled into the cold moor.")]),
    ("s09", "The chase", "Gemini_Generated_Image_65yn0k65yn0k65yn (8).jpg",
     "", [("Dr. Mortimer", "Hugo gave chase with his hounds in a frenzy of drunken rage.")]),
    ("s10", "The legend's end", "Gemini_Generated_Image_65yn0k65yn0k65yn (9).jpg",
     "", [("Dr. Mortimer", "At dawn, they found him dead... tore open by a monstrous hound of hell!")]),
    ("s11", "Sir Charles", "Gemini_Generated_Image_65yn0k65yn0k65yn (10).jpg",
     "", [("Dr. Mortimer", "Last week, Sir Charles Baskerville died mysteriously at the end of the yew alley.")]),
    ("s12", "The footprints", "Gemini_Generated_Image_7ytvf77ytvf77ytv.jpg",
     "", [("Dr. Mortimer", "On the damp gravel were tracks... the prints of a man running for his life.")]),
    ("s13", "A gigantic hound", "Gemini_Generated_Image_p15m57p15m57p15m.jpg",
     "", [("Holmes", "Human footprints?"),
          ("Dr. Mortimer", "Mr. Holmes... they were the footprints of a gigantic hound!")]),
    ("s14", "The decision", "Gemini_Generated_Image_hocgcohocgcohocg.jpg",
     "", [("Holmes", "A dangerous game is afoot, Watson. Will you accompany Sir Henry to Devonshire?"),
          ("Watson", "To the very end, Holmes.")]),
    ("s15", "Sir Henry arrives", "Gemini_Generated_Image_uly7zsuly7zsuly7.jpg",
     "", [("Sir Henry", "Look at this letter sent to my hotel! Cut-out newspaper words warning me away from the moor!")]),
    ("s16", "The paper clue", "Gemini_Generated_Image_wvibmqwvibmqwvib.jpg",
     "", [("Holmes", "Pasted words from yesterday's Times... except the word 'moor', written in ink.")]),
    ("s17", "The missing boot", "Gemini_Generated_Image_8t57ch8t57ch8t57.jpg",
     "", [("Sir Henry", "And that's not all! Someone has stolen one of my new boots right out of my room!")]),
    ("s18", "The bearded spy", "Gemini_Generated_Image_exr8srexr8srexr8.jpg",
     "", [("Holmes", "Watson, look! That cab! A black-bearded man is spying on us!")]),
    ("s19", "Chase through London", "Gemini_Generated_Image_snmelvsnmelvsnme.jpg",
     "", [("Holmes", "Driver! Follow that cab! Full gallop—don't let him slip away in the fog!")]),
]


def plan() -> dict:
    scenes = []
    for index, (key, purpose, image, narration, lines) in enumerate(SCENES, 1):
        speakers = []
        for speaker, _line in lines:
            if speaker not in speakers:
                speakers.append(speaker)
        scenes.append({
            "scene_key": key,
            "purpose": purpose,
            "target_duration_s": 5.0,
            "narration": narration,
            "dialogue": [{"character": speaker, "line": line} for speaker, line in lines],
            "characters": speakers,
            "image_hint": image,
            "camera": "slow push in" if index % 2 else "gentle pan right",
            "transition": "cut",
            "preferred_method": "camera_move",
        })
    return {
        "title": "Baker Street",
        "logline": "A country doctor brings Sherlock Holmes the Baskerville curse.",
        "synopsis": "From a quiet evening at 221B to a cab chase through the London fog.",
        "scenes": scenes,
    }


async def main() -> None:
    async with session_scope() as session:
        folder = (
            await session.execute(select(ImageFolder).where(ImageFolder.path == STILLS))
        ).scalar_one_or_none()
        if folder is None:
            folder = ImageFolder(path=STILLS, recursive=False)
            session.add(folder)
            await session.flush()
        progress = await image_service.scan_folder(session, folder)
        print("scan:", progress.state, progress.message, flush=True)
        voice = await speech_service.default_voice(session)
        if voice is None:
            raise SystemExit("No installed voice. The lines cannot be spoken.")
        print("voice:", voice.name, flush=True)

    async with session_scope() as session:
        from app.models import Project

        project = (
            await session.execute(select(Project).where(Project.name == NAME))
        ).scalar_one_or_none()
        folder = (
            await session.execute(select(ImageFolder).where(ImageFolder.path == STILLS))
        ).scalar_one()
        voice = await speech_service.default_voice(session)
        if project is None:
            project = await project_service.create_project(
                session,
                name=NAME,
                niche="classic mystery",
                audience="general",
                story_style="period drama",
                target_duration_s=95,
                resolution="1080p",
                aspect_ratio="16:9",
                fps=24,
                brief="The opening of the Baskerville case, told in the supplied lines.",
                image_folder_id=folder.id,
                narrator_voice_id=voice.id,
                stills_ai_generated=True,
            )
            print("created project", project.id)
        else:
            await project_service.update_project(session, project.id, {
                "target_duration_s": 95,
                "resolution": "1080p",
                "aspect_ratio": "16:9",
                "fps": 24,
                "image_folder_id": folder.id,
                "narrator_voice_id": voice.id,
                "stills_ai_generated": True,
            })
            print("using project", project.id)
        project_id = project.id

        script = await script_service.accept_paste(session, project_id, json.dumps(plan()))
        print("script", script.id, "valid", script.is_valid, "est", script.estimated_duration_s)
        if not script.is_valid:
            print(json.dumps(script.validation.get("errors"), indent=2))
            raise SystemExit(1)
        await script_service.approve(session, script.id)
        print("approved")

    async with session_scope() as session:
        speech_plan = await speech_service.plan_segments(session, project_id)
        print("speech plan", speech_plan)
        outcome = await speech_service.synthesise_project(session, project_id)
        print("speech", outcome)

    async with session_scope() as session:
        routed = await routing_service.plan_shots(session, project_id)
        print("routed", routed)
        run = await routing_service.render_local_shots(session, project_id)
        print(
            "shots",
            getattr(run, "rendered", None),
            "reused",
            getattr(run, "reused", None),
            "failed",
            getattr(run, "failed", None),
            "errors",
            getattr(run, "errors", None),
        )

    async with session_scope() as session:
        result = await render_service.render_project(session, project_id)
        print("render", result.status, result.duration_s, result.output_path)
        print("problems", result.problems)
        if result.succeeded and result.render_id:
            assets = await publishing.prepare(session, result.render_id)
            print("title", assets.title)
            print("disclosure", assets.disclosure)


if __name__ == "__main__":
    asyncio.run(main())
