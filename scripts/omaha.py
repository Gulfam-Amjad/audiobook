"""Eight minutes on Omaha Beach, told with public-domain photographs.

The cold open is the landing. The story then goes back to the night
before and comes forward again. Every picture is chosen by a search
that must hit a known 1944 file, so a modern ceremony photo cannot
take the shot. One line is read by a second voice: Eisenhower's
order, a United States government text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from sqlalchemy import select

from app.db import session_scope
from app.models import Character, ImageFolder, Project, VoiceProfile
from app.services import ffmpeg
from app.services import images as image_service
from app.services import projects as project_service
from app.services import publishing
from app.services import render as render_service
from app.services import routing as routing_service
from app.services import scripts as script_service
from app.services import speech as speech_service
from app.services import stock_media

NAME = "The Morning the Ramps Fell"
FOLDER = Path(r"D:\youtube_library\omaha-beach")
PICKS = FOLDER / "picks.json"
MUSIC = Path(r"D:\AIVideoManager\music\omaha-bed.mp3")
AFTERMATH = Path(r"D:\AIVideoManager\music\Aftermath.mp3")
BATTLE = Path(r"D:\AIVideoManager\music\Battle.mp3")
DESKTOP = Path(r"C:\Users\Gulfam\Desktop\The Morning the Ramps Fell.mp4")
SHORT = Path(r"C:\Users\Gulfam\Desktop\The Morning the Ramps Fell - short.mp4")
NARRATOR = "en_GB-cori-medium"
QUOTE_VOICE = "en_US-norman-medium"
FONT = r"C:\Windows\Fonts\arialbd.ttf"

# key, purpose, narration, quote or "", search, title must contain
SCENES = [
    ("s01", "The ramp",
     "The ramp fell open, and the men behind it could see Omaha Beach. The sea in front of them was already full of the soldiers who had gone down first. Nobody in the boat could tell which of those men was still alive. A Coast Guard photographer, standing in one of those boats, made this picture.",
     "", "Into the Jaws of Death", "Into the Jaws of Death"),
    ("s02", "The morning",
     "It is the morning of June sixth, nineteen forty-four. Along fifty miles of French coast, the Allies have come to take back Europe. This beach is the one that is going wrong. The plan said the defenses would be broken before the ramps went down. On this beach, they were not. The bombers had been sent to smash the guns. Most of the bombs fell inland, behind them.",
     "", "Infantry landing craft approach Omaha", "Infantry landing craft approach Omaha"),
    ("s03", "The fleet",
     "Pressed against the shingle, the men lay in holes the shells had made. There was nowhere else to go. The guns on the bluff could see the whole beach, and from the beach those guns were only a sound. The order was to get off the sand and up the hill. For a long time, nobody moved. Some companies had already lost the officers who were supposed to give that order.",
     "", "80-G-45715 Normandy", "80-G-45715"),
    ("s04", "The order",
     "The night before, General Eisenhower sent one order to every soldier, sailor, and airman who was about to go. He had written it knowing the weather might still call the whole landing off. Then he sent them anyway. The order was read on the transports in the night. By morning it was in the pocket of men who were already wet.",
     "You are about to embark upon the Great Crusade, toward which we have striven these many months. The eyes of the world are upon you.",
     "Eisenhower talks 502", "Eisenhower talks with 502"),
    ("s05", "The paratroopers",
     "Hours before he wrote it, he had walked among the paratroopers of the 101st Airborne and asked them where they were from. After midnight, those men jumped into the fields behind the beaches. They were meant to hold the roads before the boats arrived. In the dark, many came down miles from the field they had studied. Scattered, they still cut roads and held bridges the boats would need if the beach ever opened.",
     "", "gliders over Normandy June 1944", "gliders over Normandy"),
    ("s06", "The balloons",
     "At first light the landing craft started in. Barrage balloons pulled above them, so a strafing plane would have to climb or hit the cable. The crossing had taken hours in a sea that came over the sides, and a great many of the men were already sick. Each craft had a numbered stretch of sand. The current pushed many of them far from it.",
     "", "LCIs barrage balloons Normandy", "barrage balloons"),
    ("s07", "The ferries",
     "Rhino ferries, barges lashed into rafts, carried the tanks. From the water, the beach was a thin dark line, and the guns were in the bluff above it. Some of the tanks that were meant to swim in ahead of the infantry had launched too far out. The crews went down with them. The few swimming tanks that reached the sand were almost the only armor the first infantry had.",
     "", "Rhino ferry Normandy", "Rhino ferry"),
    ("s08", "Into the water",
     "Then the ramps dropped. Men stepped into water up to their chests. Machine guns on the hill were shooting down the length of the beach, and many were hit before their boots found sand. The life belts held the wounded up. They also held them where the guns could see them. The Navy's photographers were in the same boats. This is one of the frames they carried home.",
     "", "Troops wade ashore Omaha", "wade ashore"),
    ("s09", "The last yards",
     "The ones who kept going waded the last yards among sinking boats and men who could no longer stand. There was no cover. The obstacles meant to stop the craft were still standing. A man who stopped to help became a second target, so most of them kept walking. Engineers were meant to blow the obstacles before the tide covered them. On much of this beach, the tide won.",
     "", "American assault troops wade Omaha", "assault troops"),
    ("s10", "The fire",
     "From the deck of a landing ship, the shore was a wall of smoke. German positions were burning. The boats kept starting in. The smoke hid the bluff for a moment, and then the wind moved and the hill was there again. Destroyers came in close and fired at single pillboxes a spotter could point to.",
     "", "Burning German Defenses Omaha", "Burning German Defenses"),
    ("s11", "The pillbox",
     "By the middle of the morning a few officers had a command post in the shadow of one captured pillbox. The roads off the beach were still closed, and the tide was coming in over the wounded. The tide was the other enemy. It climbed the sand and took anyone who could not crawl above it. From the shadow of that pillbox, a few men started moving along the bluff instead of straight at it.",
     "", "command post pillbox Omaha", "command post"),
    ("s12", "The other beaches",
     "Off the other beaches, the heavy ships were still firing over the men on the sand. Omaha was the worst of the five landings. The day did not rest on this one slope alone. On the beaches to the east, troops were off the sand by midday. Here, midday still looked like the first hour. Utah, Gold, Juno, and Sword each had their own disasters. None of them stalled the way this beach did.",
     "", "British Forces Invasion of Normandy 6 June 1944", "British Forces during the Invasion"),
    ("s13", "The next morning",
     "The next day, on a stretch of sand called Easy Red, the 2nd Infantry Division came through a draw that the first wave had died trying to open. That draw was a gap in the bluff, and it was the only way a vehicle could leave the beach. Once trucks could use it, the sand stopped being a trap.",
     "", "Easy Red Omaha June 7 1944", "Easy Red"),
    ("s14", "Inside the gun",
     "A soldier could then climb to a German gun position and look back down at the beach he had crossed. From up there, the boats looked small, and the reason for the morning was plain. The concrete was thick. The naval shells had scarred it and had not broken it. Men had taken the position from the side. A day earlier, the gun he is standing in had been firing down the length of the beach.",
     "", "soldier inspects German pillbox Omaha", "inspects a German pillbox"),
    ("s15", "The harbor",
     "There was no port. So the Allies had built one in England, in pieces, and towed it across. Within days, men were bolting a harbor together on the open sea. A storm later would break the American one. On these days, the pieces were still arriving. The bridges in this picture were the road from the ships to the sand.",
     "", "Mulberry harbor bridges Normandy", "Mulberry harbor bridges"),
    ("s16", "The breakwater",
     "Landing ships put their bows on the sand and opened the doors. What the first wave could not carry began to roll off: vehicles, ammunition, and the men who would have to fight the next morning. When those doors opened onto sand instead of deep water, the beach had finally changed. A ship that could open its bow on the sand was worth more that afternoon than another gun offshore.",
     "", "Normandy Invasion Mulberries June 1944", "Mulberries"),
    ("s17", "The king",
     "On June sixteenth, ten days after the landing, the King crossed the Channel with his admirals to see what was holding. The beach was no longer a battle. It was a port. He walked where, ten days earlier, men had been lying in the shingle with no way up the hill. He stayed a few hours. The port kept working after he left.",
     "", "King George VI Normandy", "King George VI"),
    ("s18", "The prisoners",
     "German soldiers taken in those first days were shipped back to England. The men who had fired from the bluff arrived as prisoners on the same sea they had been shooting at. The defense that was supposed to throw the invasion back into the water was, for them, already over. The bluff they had held was already filling with American supplies.",
     "", "German soldiers captured Normandy June 1944", "captured in Normandy"),
    ("s19", "Inland",
     "Through that summer the tanks pushed inland, toward towns like Vire. The beach was behind them. Ahead were hedges, lanes, and a gun at the end of each one. Vire was not a beach objective. It was a town at the end of a road, and the road was the fight. These tanks were photographed in August, on the way toward Vire, two months after the ramps fell.",
     "", "Sherman tank Normandy 1944", "Sherman tanks"),
    ("s20", "The hedgerows",
     "In the weeks after, the tanks were fitted with steel blades just to push through those banks. The beach had been one morning. The country behind it was a different war. A tank that stayed in the lane was a target. A tank that crossed the hedge could meet a gun at ten yards. The blades were a field invention. Nobody had built a tank for hedges.",
     "", "hedgerow tank Normandy", "hedgerow"),
    ("s21", "What they could see",
     "None of that was visible from the ramp of a landing craft at half past six. A man stepping into the water could see the bluff, the smoke, and the men ahead of him. That was the whole war. He could not see the harbor, or the king, or the road inland. He followed the man in front of him. The boat is still full. The picture can show the choice, and nothing that came after it.",
     "", "Omaha Beach Landing Craft Approaches", "Landing Craft Approaches"),
    ("s22", "Ashore",
     "By nightfall they are ashore on every beach. More than two thousand Americans are dead, wounded, or missing on this one alone. These are the men who crossed, packed truck against truck, a balloon overhead, with no view of what was waiting. The number belongs to one beach and one day. The picture is the hour before any of them knew it. Two thousand is the count for this beach, not for the whole coast. The other four paid their own.",
     "", "80-G-252203 Normandy Invasion", "80-G-252203"),
    ("s23", "Whether they can stay",
     "The question is whether they can stay. The ramp is still down in the photograph. The men are still walking into the water. Everything that followed had to begin there. Stay, and the road across France is open. Fail, and every man in that water has been left there for nothing. The photograph does not know which one it will be. It only knows the ramp, the men, and the water.",
     "", "Into the Jaws of Death National Archives", "Into the Jaws of Death"),
]


def plan(picks: dict[str, dict]) -> dict:
    scenes = []
    for index, (key, purpose, narration, quote, _query, _must) in enumerate(SCENES):
        dialogue = []
        characters = []
        if quote:
            dialogue.append({"character": "Eisenhower", "line": quote})
            characters.append("Eisenhower")
        scenes.append({
            "scene_key": key,
            "purpose": purpose,
            "target_duration_s": 20,
            "narration": narration,
            "dialogue": dialogue,
            "characters": characters,
            "image_hint": picks[key]["still"],
            "camera": "slow push in" if index % 2 else "gentle pan right",
            "transition": "cut",
            "preferred_method": "camera_move",
        })
    return {
        "title": "The Morning the Ramps Fell",
        "logline": "June 6, 1944, on the one beach where the landing nearly failed.",
        "synopsis": "From the ramp of a landing craft to the night the Allies were ashore.",
        "scenes": scenes,
    }


async def _bed(ffmpeg_path: str) -> None:
    if MUSIC.exists():
        return
    MUSIC.parent.mkdir(parents=True, exist_ok=True)
    # Two CC0 tracks, alternated, so eight minutes is not one two-minute loop.
    pieces = [AFTERMATH, BATTLE, AFTERMATH, BATTLE, AFTERMATH]
    command = [ffmpeg_path, "-y", "-v", "error"]
    for piece in pieces:
        command += ["-i", str(piece)]
    chain = "".join(
        f"[{index}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{index}];"
        for index in range(len(pieces))
    )
    current = "[a0]"
    for index in range(1, len(pieces)):
        target = "[bed]" if index == len(pieces) - 1 else f"[m{index}]"
        chain += f"{current}[a{index}]acrossfade=d=4:c1=tri:c2=tri{target};"
        current = target
    command += ["-filter_complex", chain, "-map", "[bed]", "-c:a", "libmp3lame", "-q:a", "4", str(MUSIC)]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await process.communicate()
    if process.returncode:
        raise SystemExit(err.decode("utf-8", "replace")[-400:])
    print("music bed", MUSIC, flush=True)


async def fetch_all() -> dict[str, dict]:
    status = await ffmpeg.detect()
    if not status.ffmpeg_path:
        raise SystemExit("FFmpeg is needed.")
    await _bed(status.ffmpeg_path)
    FOLDER.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    picks: dict[str, dict] = {}
    for key, _purpose, _narration, _quote, query, must in SCENES:
        found = [
            c for c in await stock_media.search_commons(query, "image", limit=20)
            if stock_media.usable(c, query) and must.lower() in c.title.lower() and c.page_url not in used
        ]
        if not found:
            print(f"{key}: no 1944 file for '{query}'", flush=True)
            continue
        try:
            fetched = await stock_media.fetch(found[0], FOLDER, ffmpeg_path=status.ffmpeg_path, name=key)
        except stock_media.StockMediaError as exc:
            print(f"{key}: {exc}", flush=True)
            continue
        used.add(found[0].page_url)
        picks[key] = {
            "title": found[0].title,
            "licence": found[0].licence,
            "credit": found[0].credit_line,
            "still": fetched.still_path.name,
        }
        print(f"{key}: {found[0].licence} | {found[0].title[:100]}", flush=True)
    PICKS.write_text(json.dumps(picks, indent=2), encoding="utf-8")
    _sheet(status.ffmpeg_path, picks)
    return picks


def _sheet(ffmpeg_path: str, picks: dict[str, dict]) -> None:
    files = [str(FOLDER / picks[key]["still"]) for key, *_ in SCENES if key in picks]
    if len(files) < 2:
        return
    dest = FOLDER / "sheet.jpg"
    command = [ffmpeg_path, "-y", "-v", "error"]
    for name in files:
        command += ["-i", name]
    chain = "".join(
        f"[{i}:v]scale=320:180:force_original_aspect_ratio=decrease,"
        f"pad=320:180:(ow-iw)/2:(oh-ih)/2[v{i}];"
        for i in range(len(files))
    )
    cols = 4
    layout = "|".join(
        f"{(i % cols) * 320}_{(i // cols) * 180}" for i in range(len(files))
    )
    chain += "".join(f"[v{i}]" for i in range(len(files)))
    chain += f"xstack=inputs={len(files)}:layout={layout}[out]"
    command += ["-filter_complex", chain, "-map", "[out]", "-frames:v", "1", str(dest)]
    import subprocess
    subprocess.check_call(command)
    print("sheet", dest, flush=True)


async def build(picks: dict[str, dict]) -> None:
    missing = [key for key, *_ in SCENES if key not in picks]
    if missing:
        raise SystemExit(f"Missing pictures: {missing}")
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
            target_duration_s=480,
            resolution="1080p",
            aspect_ratio="16:9",
            fps=24,
            image_folder_id=folder.id,
            narrator_voice_id=voice.id,
            stills_ai_generated=False,
            music_path=str(MUSIC),
            music_gain_db=-20,
        )
        if project is None:
            project = await project_service.create_project(
                session, name=NAME, niche="history documentary", audience="general",
                story_style="documentary",
                brief="Omaha Beach, June 6 1944, told with public-domain photographs.",
                **values,
            )
        else:
            await project_service.update_project(session, project.id, values)
        project_id = project.id
        script = await script_service.accept_paste(session, project_id, json.dumps(plan(picks)))
        print("script", script.id, "valid", script.is_valid, "words", script.word_count, "est", script.estimated_duration_s, flush=True)
        if not script.is_valid:
            print(json.dumps((script.validation or {}).get("errors"), indent=2))
            raise SystemExit(1)
        await script_service.approve(session, script.id)
        eisenhower = (
            await session.execute(
                select(Character).where(Character.project_id == project_id, Character.name == "Eisenhower")
            )
        ).scalar_one()
        norman = (
            await session.execute(select(VoiceProfile).where(VoiceProfile.name == QUOTE_VOICE))
        ).scalar_one()
        eisenhower.voice_profile_id = norman.id

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

    status = await ffmpeg.detect()
    shutil.copy2(out, DESKTOP)
    _thumbnail(status.ffmpeg_path, out, out.parent / "thumbnail-title.jpg")
    shutil.copy2(out.parent / "thumbnail-title.jpg", Path(r"C:\Users\Gulfam\Desktop\The Morning the Ramps Fell - thumbnail.jpg"))
    _short(status.ffmpeg_path, out)
    print("desktop", DESKTOP, DESKTOP.stat().st_size, "duration", duration, flush=True)


def _thumbnail(ffmpeg_path: str, video: Path, dest: Path) -> None:
    # The hook frame, with three words. The picture is the photograph itself.
    subprocess_text = "THE RAMPS FELL"
    import subprocess
    # A drive-letter colon breaks FFmpeg's filter parser, so the font is
    # copied beside the job and named with no colon in the path.
    font_copy = Path(r"D:\AIVideoManager\tmp\arialbd.ttf")
    font_copy.parent.mkdir(parents=True, exist_ok=True)
    if not font_copy.exists():
        shutil.copy2(FONT, font_copy)
    subprocess.check_call(
        [
            ffmpeg_path, "-y", "-v", "error", "-ss", "0.15", "-i", str(video),
            "-frames:v", "1",
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
            f"drawtext=fontfile=arialbd.ttf:text='{subprocess_text}':fontsize=72:"
            "fontcolor=white:borderw=4:bordercolor=black:x=(w-text_w)/2:y=h-140",
            str(dest),
        ],
        cwd=str(font_copy.parent),
    )


def _short(ffmpeg_path: str, video: Path) -> None:
    """The opening, under a minute, cropped vertical. A Short over a minute
    is blocked worldwide if anyone files a claim, so this stays at 45 seconds."""
    import subprocess
    subprocess.check_call([
        ffmpeg_path, "-y", "-v", "error", "-i", str(video), "-t", "45",
        "-vf", "crop=608:1080:(in_w-608)/2:0,scale=1080:1920",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "1",
        "-c:a", "aac", "-b:a", "160k", str(SHORT),
    ])
    print("short", SHORT, flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--reuse-picks", action="store_true")
    args = parser.parse_args()
    if args.reuse_picks and PICKS.exists():
        picks = json.loads(PICKS.read_text(encoding="utf-8"))
    else:
        picks = await fetch_all()
    if args.fetch_only:
        return
    await build(picks)


if __name__ == "__main__":
    asyncio.run(main())
