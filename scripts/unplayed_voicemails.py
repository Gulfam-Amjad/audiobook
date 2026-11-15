"""Build the "1,094 Unplayed Voicemails" film: a fiction, told in voicemails.

    python scripts/unplayed_voicemails.py stills    # fetch licensed stills
    python scripts/unplayed_voicemails.py spec      # write the film spec
    python scripts/unplayed_voicemails.py sheet     # contact sheet to review stills

Every person pictured is a stock model. The story is invented. The stills
come from Pixabay (Pixabay Content License) through the app's licence gate,
and each one is saved with its credit beside it.

Voices: Ethan narrates and speaks in one voice; Lena and Maya have their
own. Lena's voicemails are played as her own speech.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ffmpeg, stock_media  # noqa: E402

FILM_DIR = Path(r"D:\AIVideoManager\films\unplayed-voicemails")
STILLS = Path(r"D:\youtube_library\unplayed-voicemails-stills")
SPEC = FILM_DIR / "spec.json"
SHEET = FILM_DIR / "contact-sheet"
SPEAKING_WPM = 150
FALLBACK_QUERY = "couple close portrait face"


@dataclass
class S:
    """One shot: what is said, what is seen, and how it is directed."""

    key: str
    query: str
    narration: str = ""
    dialogue: list[tuple[str, str]] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    beat: str = "build"
    mood: str = "reflective"
    chapter: str | None = None
    lower: str | None = None
    transition: str = "cut"
    ambience: str = ""
    fallback: str = FALLBACK_QUERY


def _load_scenes() -> list[S]:
    namespace: dict = {"S": S}
    exec(Path(__file__).with_name("voicemail_scenes.py").read_text(encoding="utf-8"), namespace)
    return namespace["SCENES"]


SCENES: list[S] = _load_scenes()

#: Hand picks after a contact-sheet review. Scene key -> (query, result index).
PICK: dict[str, tuple[str, int]] = {
    "s02": ("man looking down sad portrait", 1),
    "s03": ("happy couple together portrait", 2),
    "s05": ("happy couple together portrait", 1),
    "s08": ("angry couple relationship", 1),
    "s12": ("insomnia man bedroom", 1),
    "s14": ("man and woman cafe conversation", 0),
    "s15": ("man and woman cafe conversation", 1),
    "s16": ("serious man headphones", 1),
    "s25": ("man and woman cafe conversation", 4),
    "s31": ("man studio headphones portrait", 2),
    "s40": ("couple smiling at camera", 0),
    "s42": ("couple face to face smiling", 1),
    "s47": ("couple on pier sunset", 5),
    "s52": ("couple face to face smiling", 5),
}


def _fits(tags: str, query: str) -> bool:
    """Drop stock photos that fight the shot: cartoons, weddings, animals, silhouettes."""
    text = f" {tags.lower()} "
    ask = query.lower()
    if any(word in text for word in (
        "cartoon", "illustration", "vector", "anime", "logo", " icon", "diagram",
        "collage", "clipart", "render", "painting", "drawing",
    )):
        return False
    rules = (
        ("wedding", ("wedding", "bride")),
        ("bride", ("wedding", "bride")),
        ("groom", ("wedding", "bride")),
        ("kiss", ("kiss",)),
        ("baby", ("baby", "child", "boy")),
        ("toddler", ("baby", "child", "boy")),
        (" dog", ("dog",)),
        (" cat", ("cat",)),
        ("pizza", ("pizza",)),
        ("silhouette", ("silhouette",)),
        ("from behind", ("behind", "back")),
        ("child", ("child", "boy", "kid")),
        ("children", ("child", "boy", "kid")),
        (" kid", ("child", "boy", "kid")),
        ("kids", ("child", "boy", "kid")),
        ("little girl", ("boy", "child")),
        ("little boy", ("boy", "child")),
        ("piano", ("piano",)),
        ("guitar", ("guitar",)),
        ("bicycle", ("bicycle", "bike")),
        (" bike", ("bike", "bicycle")),
        ("shopping", ("shopping",)),
        ("elderly", ("elderly", "old")),
        ("grandfather", ("old",)),
        ("upside", ("upside",)),
    )
    for word, allowed in rules:
        if word in text and not any(ok in ask for ok in allowed):
            return False
    return True


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def shot_seconds(scene: S) -> float:
    words = _words(scene.narration) + sum(_words(line) for _, line in scene.dialogue)
    return round(max(3.0, min(30.0, words / SPEAKING_WPM * 60.0 + 2.0)), 1)


def build_spec(image_hints: dict[str, str]) -> dict:
    scenes = []
    for index, scene in enumerate(SCENES, 1):
        speakers: list[str] = []
        for speaker, _line in scene.dialogue:
            if speaker not in speakers:
                speakers.append(speaker)
        for name in scene.characters:
            if name not in speakers:
                speakers.append(name)
        entry = {
            "scene_key": scene.key,
            "purpose": scene.lower or scene.query,
            "target_duration_s": shot_seconds(scene),
            "narration": scene.narration,
            "dialogue": [{"character": who, "line": line} for who, line in scene.dialogue],
            "characters": scene.characters or speakers,
            "image_hint": image_hints[scene.key],
            "camera": "",
            "transition": scene.transition,
            "preferred_method": "camera_move",
            "beat": scene.beat,
            "music_mood": scene.mood,
        }
        if scene.chapter:
            entry["chapter_title"] = scene.chapter
        if scene.lower:
            entry["lower_third"] = scene.lower
        if scene.ambience:
            entry["ambience"] = scene.ambience
        scenes.append(entry)
    total = round(sum(s["target_duration_s"] for s in scenes))
    return {
        "name": "Unplayed Voicemails",
        "niche": "romance drama",
        "story_style": "fiction",
        "audience": "general",
        "brief": "Original fiction. A love story told through voicemails. The people pictured are "
                 "stock models, not the characters.",
        "stills_folder": str(STILLS),
        "stills_ai_generated": False,
        "voice": "kokoro:am_michael",
        "character_voices": {"Lena": "kokoro:af_heart", "Maya": "kokoro:af_sarah"},
        "music_path": None,
        # Soft bed. The voices lead; the music only holds the room.
        "music_gain_db": -30,
        "target_duration_s": total,
        "resolution": "1080p",
        "aspect_ratio": "16:9",
        "fps": 24,
        "finishing": {
            "look": "clean",
            # No title or end card over the faces. The thumbnail carries the title.
            "end_card": "",
            "title_card": "",
            "ambience_gain_db": -34,
            "effects_gain_db": -22,
        },
        "title": "1,094 Unplayed Voicemails",
        "logline": "A man has deleted 1,094 voicemails from the woman he left, unplayed. "
                   "Then one new message arrives at 3:12 in the morning.",
        "synopsis": "Ethan, a sound engineer, has a rule: he never listens to Lena's voicemails. "
                    "When a new message arrives, he finally listens to all of them, and drives to the lake where she is.",
        "format": "documentary",
        "scenes": scenes,
    }


async def pick_stills(only: set[str] | None = None) -> dict[str, dict]:
    """Search Pixabay for each shot, keep the best licensed photo, save it with its credit.

    With ``only``, just those shots are picked again and the rest are kept.
    """
    status = await ffmpeg.detect()
    STILLS.mkdir(parents=True, exist_ok=True)
    picks_path = FILM_DIR / "picks.json"
    picks: dict[str, dict] = (
        json.loads(picks_path.read_text(encoding="utf-8")) if only and picks_path.exists() else {}
    )
    # A photo is used once: its Pixabay page is the identity.
    used: set[str] = {value["page"] for key, value in picks.items() if key not in (only or set())}
    for scene in SCENES:
        if only and scene.key not in only:
            continue
        for old in STILLS.glob(f"{scene.key}-*"):
            old.unlink()
        chosen = None
        for query in ([PICK[scene.key][0]] if scene.key in PICK else [scene.query, scene.fallback]):
            try:
                found = await stock_media.search_pixabay(query, "image")
            except stock_media.StockMediaError as exc:
                print(scene.key, "search failed:", exc, flush=True)
                continue
            if scene.key in PICK:
                index = PICK[scene.key][1]
                order = [found[index]] if index < len(found) and found[index].page_url not in used else []
            else:
                order = [c for c in found if c.page_url not in used and _fits(c.title, query)]
            for candidate in order[:12]:
                try:
                    fetched = await stock_media.fetch(
                        candidate, STILLS, ffmpeg_path=status.ffmpeg_path, name=scene.key
                    )
                except stock_media.StockMediaError as exc:
                    print(scene.key, "skipped one:", exc, flush=True)
                    continue
                chosen = (candidate, fetched)
                break
            if chosen:
                break
        if chosen is None:
            raise SystemExit(f"No licensed still was found for {scene.key}.")
        candidate, fetched = chosen
        used.add(candidate.page_url)
        picks[scene.key] = {
            "query": scene.query,
            "file": fetched.still_path.name,
            "source": candidate.source,
            "page": candidate.page_url,
            "licence": candidate.licence,
            "tags": candidate.title,
        }
        print(scene.key, scene.query, "->", fetched.still_path.name, flush=True)
    (FILM_DIR).mkdir(parents=True, exist_ok=True)
    (FILM_DIR / "picks.json").write_text(json.dumps(picks, indent=2), encoding="utf-8")
    return picks


def contact_sheet(picks: dict[str, dict]) -> list[Path]:
    """Two labelled sheets of every still, so the cut can be checked at a glance."""
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
            image = Image.open(STILLS / picks[key]["file"]).convert("RGB")
            image.thumbnail((cell_w - 6, cell_h))
            sheet.paste(image, (x + 3, y + 22))
            draw.text((x + 6, y + 5), f"{key}  {picks[key]['query']}", fill=(235, 235, 235), font=font)
        path = SHEET.with_name(f"{SHEET.name}-{start // per_sheet + 1}.jpg")
        sheet.save(path, quality=90)
        sheets.append(path)
    return sheets


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "spec"
    if command == "stills":
        only = set(sys.argv[2:]) or None
        asyncio.run(pick_stills(only))
        return
    picks_path = FILM_DIR / "picks.json"
    if command == "sheet":
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        for path in contact_sheet(picks):
            print(path)
        return
    if command == "spec":
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        spec = build_spec({key: value["file"] for key, value in picks.items()})
        FILM_DIR.mkdir(parents=True, exist_ok=True)
        SPEC.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        words = sum(_words(s.narration) + sum(_words(line) for _, line in s.dialogue) for s in SCENES)
        print(f"{len(SCENES)} shots, {words} words, planned {spec['target_duration_s']}s -> {SPEC}")
        return
    raise SystemExit(f"Unknown command {command!r}.")


if __name__ == "__main__":
    main()
