"""Build the Panama Canal documentary for Mighty Chapters.

    python scripts/panama_canal.py fetch   # licensed stills and footage
    python scripts/panama_canal.py sheet   # contact sheet for review
    python scripts/panama_canal.py spec    # write the film spec for make_film.py

Fewer, longer shots. Real footage where the libraries have it. Soft music.
Nothing is uploaded.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ffmpeg, stock_media  # noqa: E402

FILM_DIR = Path(r"D:\AIVideoManager\films\panama-canal")
STILLS = Path(r"D:\youtube_library\panama-canal-stills")
SPEC = FILM_DIR / "spec.json"
PICKS = FILM_DIR / "picks.json"
SHEET = FILM_DIR / "contact-sheet"
SPEAKING_WPM = 150


@dataclass
class S:
    key: str
    query: str
    narration: str
    kind: str = "image"  # image or video
    beat: str = "build"
    mood: str = "steady"
    chapter: str | None = None
    lower: str | None = None
    transition: str = "cut"
    ambience: str = ""
    fallback: str = "Panama Canal"


SCENES: list[S] = [
    # Cold open: a ship caught between oceans. No lecture first.
    S("s01", "Panama Canal locks ship",
      "A steel ship, longer than a football field, is hanging in a concrete box of water. "
      "On one side of this box is the Atlantic. On the other side is the Pacific. In a few "
      "minutes the water will rise, the gates will open, and the ship will keep climbing a "
      "mountain that is not a mountain at all.",
      kind="video", beat="hook", mood="tense", ambience="sea",
      chapter="Between two oceans"),
    S("s02", "Panama Canal",
      "This is the Panama Canal. Forty miles of dug earth and locked water that cut the "
      "journey between oceans from weeks around Cape Horn to less than a day. Every year "
      "about fourteen thousand ships pay for the privilege of floating through a country "
      "that once nearly broke every nation that tried to open it.",
      kind="video", mood="tense", ambience="sea"),
    S("s03", "Cape Horn sailing ship",
      "Before the canal, a ship from New York to San Francisco had to go south until the map "
      "ran out. Cape Horn. Ice, storms, and a graveyard of hulls. Captains called it the "
      "worst corner of the sea. The world was waiting for a shortcut. Panama was fifty miles "
      "of jungle sitting in the way.",
      mood="dark", ambience="sea", lower="Cape Horn"),

    # The French disaster
    S("s04", "Ferdinand de Lesseps Panama",
      "In eighteen eighty-one, a Frenchman who had already cut the Suez Canal arrived with "
      "certainty and a plan. Ferdinand de Lesseps. He wanted a sea-level ditch, the same "
      "trick that worked in Egypt. Panama was not Egypt. Panama was rain, landslides, and a "
      "disease that no excavator could dig out.",
      mood="dark", ambience="rain", chapter="The French", lower="Ferdinand de Lesseps"),
    S("s05", "Canal de Panama French works",
      "Steam diggers tore at the hills. French money poured into the jungle. Workers arrived "
      "from Europe and the Caribbean, and the death lists arrived with them. The company "
      "promised a canal by eighteen eighty-eight. What it produced was mud, debt, and a "
      "scandal that would shake France for a generation.",
      mood="dark", ambience="rain"),
    S("s06", "yellow fever mosquito historical",
      "The real enemy was not rock. It was a mosquito. Yellow fever and malaria killed "
      "thousands of workers before anyone understood why. Hospitals filled. Camps emptied. "
      "Men died in numbers that the company tried to hide from investors. Digging stopped "
      "when there was nobody left healthy enough to dig.",
      mood="dark", ambience="night", lower="Yellow fever"),
    S("s07", "Panama Canal construction French excavator",
      "By eighteen eighty-nine the French company collapsed. De Lesseps was ruined. More "
      "than twenty thousand people are estimated to have died during the French attempt. "
      "The jungle closed over the unfinished cuts. The dream of a sea-level canal was "
      "buried with them.",
      mood="dark", ambience="rain"),

    # America takes over
    S("s08", "Theodore Roosevelt Panama Canal steam shovel",
      "Then America decided it would finish what France could not. In nineteen hundred and "
      "three, Panama declared independence from Colombia with United States warships "
      "offshore. Days later, Washington signed a treaty for a canal zone ten miles wide. "
      "President Theodore Roosevelt made the canal his personal monument.",
      mood="driving", ambience="city", chapter="America", lower="1903"),
    S("s09", "William Gorgas Panama",
      "The first American victory was not engineering. It was medicine. Colonel William "
      "Gorgas believed mosquitoes carried yellow fever. He drained standing water, screened "
      "houses, and fumigated rooms. Critics called him obsessed. Within two years, yellow "
      "fever was gone from the Canal Zone. Without Gorgas, there would be no canal to "
      "finish.",
      mood="hopeful", ambience="room", lower="William Gorgas"),
    S("s10", "Ancon Hospital Panama Canal Zone",
      "Ancon Hospital became the front line of that war. Screens, screens, and more screens. "
      "Oil on puddles. Inspections of every barrel that could hold rainwater. Gorgas treated "
      "the Zone like a city under quarantine. Workers who had watched friends die began to "
      "believe they might live long enough to see the first ship through.",
      mood="hopeful", ambience="room"),
    S("s11", "George Goethals Panama",
      "With disease under control, the Army put Colonel George Washington Goethals in charge "
      "of the dig. He was a builder who hated speeches. He ran the canal like a railway "
      "timetable. Dig, dump, dig again. Every day the trains hauled spoil out of a cut that "
      "kept trying to fill itself back in.",
      mood="driving", ambience="wind", lower="George W. Goethals"),

    # The Cut and the Dam
    S("s12", "Culebra Cut Panama",
      "The hardest place was Culebra Cut, later called Gaillard Cut. Nine miles of continental "
      "divide that had to be sliced open. The hills did not want to be moved. After every "
      "rain, slopes slumped into the trench. Steam shovels worked for weeks clearing a "
      "slide that had erased a month of digging overnight.",
      mood="driving", ambience="wind", chapter="The Cut", lower="Culebra Cut"),
    S("s13", "Gaillard Cut Panama steam shovel",
      "At the peak, more than a hundred steam shovels worked the cut. Trains ran on schedules "
      "measured in minutes. Men stood in the mud with dynamite and picks, and the sound of "
      "blasting carried for miles. The Cut became a factory of noise, heat, and rock. "
      "Panama was being rebuilt one carload at a time.",
      mood="driving", ambience="wind"),
    S("s14", "Labourers at work on the panama canal at Culebra Cut",
      "Most of the labourers were West Indian. They dug, blasted, and bled for wages that "
      "would never make them rich. The official histories spent decades naming the engineers. "
      "The canal sits on the backs of men whose names were written mainly on pay sheets and "
      "hospital lists.",
      mood="reflective", ambience="wind"),
    S("s15", "Gatun Dam Panama",
      "While the Cut was being dug, another idea saved the project. Stop trying to dig to sea "
      "level. Raise ships instead. Dam the Chagres River, create Gatun Lake, and lift vessels "
      "eighty-five feet up to the lake, then down again on the other side. The French plan "
      "had been a ditch. The American plan was a staircase of water.",
      mood="steady", ambience="sea", chapter="The locks", lower="Gatun Dam"),
    S("s16", "Gatun Lock Panama",
      "Gatun Locks alone used enough concrete to pave a road from New York to San Francisco. "
      "Each chamber is a thousand feet long. The gates weigh hundreds of tons and are so "
      "precisely balanced that a small electric motor can swing them. Water does the heavy "
      "lifting. No pumps. Gravity fills and empties the chambers.",
      mood="steady", ambience="sea"),
    S("s17", "Pedro Miguel Lock Panama",
      "Pedro Miguel and Miraflores Locks complete the Pacific side. A ship enters from Gatun "
      "Lake, drops through Pedro Miguel, crosses Miraflores Lake, then drops again through "
      "Miraflores to the Pacific. The whole transit is a carefully timed ballet of water, "
      "gates, and locomotives that keep the hull from scraping the walls.",
      mood="steady", ambience="sea", lower="Pedro Miguel and Miraflores"),
    S("s18", "Miraflores Lock Panama ship",
      "Those little engines on the rails are called mules. They do not pull the ship so much "
      "as hold it steady while the water rises. A captain who fights the mules can smash his "
      "hull against concrete. A captain who trusts them floats through like a cork in a "
      "bathtub built by giants.",
      mood="steady", ambience="sea"),

    # Opening day
    S("s19", "Opening of the Panama Canal SS Ancon 1914",
      "On August fifteenth, nineteen fourteen, the steamship Ancon made the first official "
      "transit. There was no grand parade of nations. Europe had just plunged into the First "
      "World War. The canal opened almost quietly, and then became the most important "
      "shipping lane on Earth.",
      mood="hopeful", ambience="sea", chapter="1914", lower="SS Ancon, 15 August 1914",
      beat="reveal"),
    S("s20", "SS Panama Canal Zone Cristobal",
      "A journey that had taken two months around Cape Horn could now be done in under ten "
      "hours. Freight schedules changed. Navies changed. The map of power changed. Whoever "
      "controlled the canal controlled a shortcut between the world's two greatest oceans.",
      mood="hopeful", ambience="sea"),
    S("s21", "1914 Panama Canal Zone map",
      "The Canal Zone was not Panama. It was a strip of American jurisdiction cut through a "
      "sovereign country. For most of the twentieth century, that arrangement produced "
      "prosperity for shipping and resentment on the ground. Flags matter when they fly over "
      "someone else's earth.",
      mood="reflective", ambience="room"),

    # How a transit works today
    S("s22", "Future USS Omaha Panama Canal",
      "Watch a modern transit and you are watching the same physics the builders designed "
      "a century ago. A ship approaches. The lower gates close. Valves open. Water from the "
      "lake floods the chamber. The ship rises. The upper gates open. The ship moves forward "
      "into the next box. Climb. Cross. Descend. Done.",
      kind="video", mood="driving", ambience="sea", chapter="A transit"),
    S("s23", "Continuing Promise Panama Canal",
      "Pilots who know every bend of the Cut board the ship before it enters. Local knowledge "
      "is not optional. The canal does not forgive a captain who thinks a chart is enough. "
      "Wind, current, and the sheer length of a modern vessel can turn a mistake into a "
      "blocked waterway that stops world trade for days.",
      kind="video", mood="driving", ambience="sea"),
    S("s24", "Canal de Panama Panama webm",
      "About fourteen thousand vessels still make the crossing every year. Container ships, "
      "tankers, car carriers, warships. Each one pays a toll measured in the hundreds of "
      "thousands of dollars. The canal is a machine that converts geography into money, and "
      "Panama into a nation whose budget depends on ships that never unload there.",
      kind="video", mood="steady", ambience="sea"),
    S("s25", "Panama Canal Timelapse Caribbean to Pacific",
      "From above, the canal looks almost gentle. A silver thread through green hills. On "
      "the ground it is concrete, steel, and a lake held back by a dam that must never fail. "
      "If Gatun Dam broke, ships would be stranded, towns flooded, and a century of trade "
      "rewritten overnight.",
      kind="video", mood="reflective", ambience="wind"),

    # Expansion and today
    S("s26", "New Panama Canal expansion project",
      "In two thousand and sixteen, Panama opened a third set of locks. Neopanamax ships, "
      "too wide for the old chambers, began to pass. The expansion cost more than five "
      "billion dollars and years of digging. The old canal still runs. The new one runs "
      "beside it. Two staircases for a world that keeps building bigger hulls.",
      mood="hopeful", ambience="sea", chapter="Bigger ships", lower="2016 expansion"),
    S("s27", "panama canal locks cargo ship",
      "A Neopanamax vessel can carry more than twice the cargo of the ships the original "
      "locks were built for. That matters for the price of everything that moves by sea. "
      "Grain, cars, oil, phones. Invisible savings travel through those gates every day, "
      "and almost nobody watching a supermarket shelf thinks of Panama.",
      mood="steady", ambience="sea"),
    S("s28", "panama canal miraflores cargo",
      "Drought can stop even this machine. When the rains fail, Gatun Lake drops. The canal "
      "restricts draft. Ships wait. Tolls rise. In recent dry seasons, the world discovered "
      "again that the canal is not a guarantee. It is a system that depends on rain falling "
      "on a watershed in Central America.",
      mood="dark", ambience="rain"),
    S("s29", "panama canal ships panorama",
      "That is the quiet truth of the Panama Canal. It looks like a triumph of concrete. It "
      "is also a triumph of weather, labour, and medicine. Remove any one of those, and the "
      "shortcut closes. Keep them, and a ship can still hang between two oceans, waiting for "
      "the water to rise.",
      mood="reflective", ambience="sea", beat="reveal"),

    # Closing
    S("s30", "Pres. Theodore Roosevelt on steam-powered digging machine",
      "In nineteen hundred and six, Roosevelt himself came to Panama. He posed on a steam "
      "shovel in a white suit, mud on his boots, grinning for the cameras. It was politics "
      "and theatre. It was also a message. The President of the United States had put his "
      "body into the Cut, and the Cut would be finished.",
      mood="driving", ambience="wind", lower="Roosevelt, 1906"),
    S("s31", "Panama Canal Dredging Gatun Lake",
      "Dredges worked the lake and the Cut for years after the first transit. Spoil never "
      "stopped arriving. The canal was opened, then kept open by a fleet that treated "
      "silt like an enemy that never slept. Opening day was a beginning, not an ending.",
      mood="steady", ambience="sea"),
    S("s32", "panama canal ships panorama",
      "By the late twentieth century, the canal had become a political question as much as "
      "an engineering one. Panama wanted its waterway back. Treaties were signed. On the "
      "last day of nineteen ninety-nine, the United States handed the canal to Panama. "
      "The locks kept working. The flag over them changed.",
      mood="reflective", ambience="sea", chapter="Handover", lower="31 December 1999"),
    S("s33", "Culebra Cut Panama Canal",
      "Stand in Gaillard Cut today and you are standing inside a wound that never fully "
      "healed. The hills still slide after heavy rain. Dredgers still work. The canal is "
      "not finished in the way a bridge is finished. It is maintained, minute by minute, "
      "against the jungle's wish to reclaim it.",
      mood="reflective", ambience="wind", chapter="What remains"),
    S("s34", "panama canal cruise ship lock",
      "Tourists watch from the Miraflores visitor centre as a cruise ship climbs the steps. "
      "Children press against the glass. The ship looks impossible up close, too big for "
      "the box that holds it. Then the gates open, and the impossible becomes ordinary. "
      "That is engineering at its best. Making a miracle feel like a timetable.",
      mood="hopeful", ambience="sea"),
    S("s35", "Gatun Lock Panama canal ship",
      "Count the cost in lives and the canal is one of the most expensive shortcuts ever "
      "built. Count the cost in days saved at sea and it is one of the cheapest. Both "
      "numbers are true. The water does not care which one you prefer. It only rises, "
      "lifts, and lets go.",
      mood="reflective", ambience="sea"),
    S("s36", "Panama Canal",
      "So the next time a ship disappears from one ocean and appears in another a day later, "
      "remember what that day cost. French failure. American ambition. West Indian labour. "
      "Mosquitoes defeated one puddle at a time. A mountain cut open. A lake raised. And a "
      "steel hull, hanging in a concrete box, waiting for the water to do what human backs "
      "could never do alone.",
      kind="video", beat="outro", mood="reflective", ambience="sea", transition="dip"),
]


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def shot_seconds(scene: S) -> float:
    return round(max(8.0, min(30.0, _words(scene.narration) / SPEAKING_WPM * 60.0 + 2.5)), 1)


def build_spec(picks: dict[str, dict]) -> dict:
    scenes = []
    for scene in SCENES:
        pick = picks[scene.key]
        entry = {
            "scene_key": scene.key,
            "purpose": scene.lower or scene.query,
            "target_duration_s": shot_seconds(scene),
            "narration": scene.narration,
            "dialogue": [],
            "characters": [],
            "image_hint": pick["still"],
            "camera": "",
            "transition": scene.transition,
            "preferred_method": "camera_move",
            "beat": scene.beat,
            "music_mood": scene.mood,
            "ambience": scene.ambience,
        }
        if scene.chapter:
            entry["chapter_title"] = scene.chapter
        if scene.lower:
            entry["lower_third"] = scene.lower
        if pick.get("footage"):
            entry["footage"] = {
                "path": pick["footage"],
                "credit": pick["credit"],
                "licence": pick["licence"],
                "start_s": pick.get("excerpt_start_s"),
            }
        scenes.append(entry)
    total = round(sum(s["target_duration_s"] for s in scenes))
    return {
        "name": "Panama Canal",
        "niche": "history documentary",
        "story_style": "documentary",
        "audience": "general",
        "brief": "How the Panama Canal was dug, locked, and opened between two oceans.",
        "stills_folder": str(STILLS),
        "stills_ai_generated": False,
        "voice": "kokoro:am_michael",
        "music_path": None,
        "music_gain_db": -28,
        "target_duration_s": total,
        "resolution": "1080p",
        "aspect_ratio": "16:9",
        "fps": 24,
        "finishing": {
            "look": "archive",
            "end_card": "Thanks for watching",
            "title_card": "",
            "ambience_gain_db": -32,
            "effects_gain_db": -20,
        },
        "title": "A Ship Hanging Between Two Oceans",
        "logline": "The Panama Canal lifts ships over a mountain of mud, using water, "
                   "concrete, and a century of human cost.",
        "synopsis": "From the French disaster and yellow fever to Gorgas, Goethals, "
                    "Culebra Cut, the locks, and the ships that still climb between oceans.",
        "format": "documentary",
        "scenes": scenes,
    }


async def fetch_all(only: set[str] | None = None) -> dict[str, dict]:
    status = await ffmpeg.detect()
    if not status.ffmpeg_path:
        raise SystemExit("FFmpeg is needed to cut footage.")
    STILLS.mkdir(parents=True, exist_ok=True)
    FILM_DIR.mkdir(parents=True, exist_ok=True)
    picks: dict[str, dict] = (
        json.loads(PICKS.read_text(encoding="utf-8")) if only and PICKS.exists() else {}
    )
    used: set[str] = {v["page"] for k, v in picks.items() if k not in (only or set())}
    for scene in SCENES:
        if only and scene.key not in only:
            continue
        for old in STILLS.glob(f"{scene.key}-*"):
            old.unlink()
        kinds = [scene.kind, "image"] if scene.kind == "video" else ["image"]
        chosen = None
        for kind in kinds:
            for query in (scene.query, scene.fallback):
                try:
                    found = await stock_media.search(query, kind)
                except stock_media.StockMediaError as exc:
                    print(scene.key, kind, "search failed:", exc, flush=True)
                    continue
                for candidate in [c for c in found if c.page_url not in used][:10]:
                    try:
                        fetched = await stock_media.fetch(
                            candidate, STILLS, ffmpeg_path=status.ffmpeg_path, name=scene.key
                        )
                    except stock_media.StockMediaError as exc:
                        print(scene.key, "skipped:", exc, flush=True)
                        continue
                    chosen = (candidate, fetched)
                    break
                if chosen:
                    break
            if chosen:
                break
        if chosen is None:
            raise SystemExit(f"No licensed media for {scene.key}.")
        candidate, fetched = chosen
        used.add(candidate.page_url)
        picks[scene.key] = {
            "query": scene.query,
            "kind": candidate.kind,
            "still": fetched.still_path.name,
            "footage": str(fetched.footage_path) if fetched.footage_path else None,
            "page": candidate.page_url,
            "licence": candidate.licence,
            "credit": candidate.credit_line,
            "title": candidate.title,
            "source_duration_s": candidate.duration_s,
            "excerpt_start_s": candidate.extra.get("excerpt_start_s"),
        }
        print(
            scene.key, candidate.kind, candidate.licence,
            "->", fetched.still_path.name,
            flush=True,
        )
    PICKS.write_text(json.dumps(picks, indent=2), encoding="utf-8")
    return picks


def contact_sheet(picks: dict[str, dict]) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    FILM_DIR.mkdir(parents=True, exist_ok=True)
    cell_w, cell_h, cols, per_sheet = 384, 216, 4, 16
    font = ImageFont.load_default()
    sheets: list[Path] = []
    keys = [s.key for s in SCENES]
    for start in range(0, len(keys), per_sheet):
        batch = keys[start:start + per_sheet]
        rows = (len(batch) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + 22)), (18, 18, 18))
        draw = ImageDraw.Draw(sheet)
        for index, key in enumerate(batch):
            x, y = (index % cols) * cell_w, (index // cols) * (cell_h + 22)
            image = Image.open(STILLS / picks[key]["still"]).convert("RGB")
            image.thumbnail((cell_w - 6, cell_h))
            sheet.paste(image, (x + 3, y + 22))
            label = f"{key}  {picks[key]['kind']}  {picks[key]['query'][:28]}"
            draw.text((x + 6, y + 5), label, fill=(235, 235, 235), font=font)
        path = SHEET.with_name(f"{SHEET.name}-{start // per_sheet + 1}.jpg")
        sheet.save(path, quality=90)
        sheets.append(path)
    return sheets


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "spec"
    if command == "fetch":
        only = set(sys.argv[2:]) or None
        asyncio.run(fetch_all(only))
        return
    if command == "sheet":
        picks = json.loads(PICKS.read_text(encoding="utf-8"))
        for path in contact_sheet(picks):
            print(path)
        return
    if command == "spec":
        picks = json.loads(PICKS.read_text(encoding="utf-8"))
        missing = [s.key for s in SCENES if s.key not in picks]
        if missing:
            raise SystemExit(f"Missing picks for {missing}. Run fetch first.")
        spec = build_spec(picks)
        FILM_DIR.mkdir(parents=True, exist_ok=True)
        SPEC.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        words = sum(_words(s.narration) for s in SCENES)
        print(f"{len(SCENES)} shots, {words} words, planned {spec['target_duration_s']}s -> {SPEC}")
        return
    raise SystemExit(f"Unknown command {command!r}.")


if __name__ == "__main__":
    main()
