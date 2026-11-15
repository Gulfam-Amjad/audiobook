"""A director's pass over a script, before anyone is recorded.

Two jobs. ``review_plan`` writes notes when the shape of the writing is a
list of facts rather than a story. ``fill_defaults`` turns the writer's
beats into the editing decisions a human editor would make, so nobody has
to place a whoosh or a chapter card by hand.

Nothing here blocks a render. A note is a note.
"""

from __future__ import annotations

import re
from typing import Any

from app.services import transitions

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[\w']+")

#: Openings that announce the video instead of starting the event.
_ANNOUNCEMENTS = (
    "welcome",
    "hello",
    "hey ",
    "in this video",
    "in today's video",
    "today we",
    "this video",
    "this is the story",
)

#: A hook longer than this is a paragraph, and the first picture cannot hold it.
HOOK_WORDS = 20
#: One picture, one idea. More sentences than this and the shot is a list.
MAX_SENTENCES_PER_SHOT = 3

BEATS = ("hook", "build", "reveal", "countdown", "outro")
FORMATS = ("documentary", "countdown")
MOODS = ("tense", "steady", "epic", "driving", "reflective", "dark", "hopeful")
SOUND_TAGS = ("whoosh", "riser", "impact", "boom", "tick", "drone")
AMBIENCES = ("sea", "wind", "crowd", "rain", "fire", "radio", "room", "night", "city", "battle")

#: The longest a writer may hold the picture after the words stop.
MAX_PAUSE_S = 2.0

_MOOD_BY_BEAT = {
    "hook": "tense",
    "build": "steady",
    "reveal": "epic",
    "countdown": "driving",
    "outro": "reflective",
}
_PAUSE_BY_BEAT = {"hook": 0.6, "reveal": 0.7}

#: Words in a shot's setting that name an ambience. The first match wins,
#: so the more specific entries come first.
_AMBIENCE_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("mission control", "radio", "transmission", "cockpit", "capsule", "orbit", "lunar", "moon"), "radio"),
    (("battle", "gunfire", "artillery", "shelling", "bombard", "trench"), "battle"),
    (("ocean", "sea", "beach", "shore", "waves", "harbor", "harbour", "ship", "deck"), "sea"),
    (("rain", "storm", "thunder"), "rain"),
    (("fire", "burning", "flames", "blaze"), "fire"),
    (("crowd", "cheering", "stadium", "rally", "parade"), "crowd"),
    (("street", "city", "traffic", "town"), "city"),
    (("desert", "mountain", "wind", "plain", "ice", "snow", "arctic"), "wind"),
    (("night", "dark", "midnight"), "night"),
    (("room", "office", "hall", "court", "inside"), "room"),
)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def review_plan(plan: dict[str, Any]) -> list[str]:
    """Notes for the writer. Empty when the shape is already a documentary."""
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return []

    notes: list[str] = []
    first = scenes[0] if isinstance(scenes[0], dict) else {}
    opening = str(first.get("narration") or "").strip()
    if opening:
        notes.extend(_opening_notes(opening))

    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        narration = str(scene.get("narration") or "").strip()
        if narration:
            count = len(_sentences(narration))
            if count > MAX_SENTENCES_PER_SHOT:
                notes.append(
                    f"Scene {index} has {count} sentences for one picture. "
                    "Keep the sentence that says what the picture cannot show, "
                    "and cut the rest."
                )
        beat = str(scene.get("beat") or "").strip().lower()
        if beat and beat not in BEATS:
            notes.append(f"Scene {index} has an unknown beat '{beat}'; it is treated as build.")
        if beat == "countdown" and not _number(scene.get("countdown_number")):
            notes.append(f"Scene {index} is a countdown entry with no countdown_number.")

    if str(plan.get("format") or "").strip().lower() == "countdown":
        numbers = [
            _number(s.get("countdown_number")) for s in scenes if isinstance(s, dict)
        ]
        found = [n for n in numbers if n]
        if not found:
            notes.append("A countdown film has no numbered entries.")
        elif found != sorted(found, reverse=True):
            notes.append("The countdown numbers do not run downwards in playing order.")
    return notes


def _opening_notes(narration: str) -> list[str]:
    notes: list[str] = []
    lowered = narration.lower()
    for phrase in _ANNOUNCEMENTS:
        if lowered.startswith(phrase):
            notes.append(
                "The film announces itself. Start on the event already happening."
            )
            break
    first = _sentences(narration)[0]
    words = len(_WORD.findall(first))
    if words > HOOK_WORDS:
        notes.append(
            f"The opening sentence is {words} words. "
            f"Make it one problem, in {HOOK_WORDS} words or fewer."
        )
    return notes


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_END.split(text.strip()) if part.strip()]
    return parts or [text.strip()]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def _number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _text(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _tags(value: Any, allowed: tuple[str, ...]) -> list[str]:
    """Sound tags from a list or from free text such as 'distant whoosh'."""
    if isinstance(value, list):
        words = " ".join(str(item) for item in value)
    else:
        words = str(value or "")
    lowered = words.lower()
    return [tag for tag in allowed if tag in lowered]


def infer_ambience(*texts: Any) -> str:
    joined = " ".join(str(t or "") for t in texts).lower()
    for words, tag in _AMBIENCE_WORDS:
        if any(re.search(rf"\b{re.escape(word)}", joined) for word in words):
            return tag
    return ""


def fill_defaults(
    scenes: list[dict[str, Any]], *, film_format: str = "documentary"
) -> list[dict[str, Any]]:
    """The editing decisions for every shot, filled in from the beats.

    Whatever the writer set explicitly is kept. Everything else follows
    a small set of rules an editor would recognise:

    * the first shot is the hook and the last is the outro
    * a chapter change dips to black, gets a low boom and a new music cue
    * a countdown entry whips in, with a hit on the number and a tick under it
    * a reveal is preceded by a riser, lands on an impact and holds a beat
    * a shot that names a new place or date dissolves in
    * every other change of shot is a cut
    """
    film_format = film_format if film_format in FORMATS else "documentary"
    last = len(scenes) - 1
    result: list[dict[str, Any]] = []

    for index, scene in enumerate(scenes):
        scene = scene if isinstance(scene, dict) else {}
        number = _number(scene.get("countdown_number"))
        beat = _text(scene.get("beat"), 20).lower()
        if beat not in BEATS:
            if number:
                beat = "countdown"
            elif index == 0:
                beat = "hook"
            elif index == last and last > 0:
                beat = "outro"
            else:
                beat = "build"

        chapter = _text(scene.get("chapter_title"), 60)
        if beat == "countdown" and number and not chapter:
            chapter = f"Number {number}"

        mood = _text(scene.get("music_mood"), 20).lower()
        if mood not in MOODS:
            mood = _MOOD_BY_BEAT.get(beat, "steady")

        pause = scene.get("pause_after_s")
        try:
            pause_s = float(pause) if pause is not None else _PAUSE_BY_BEAT.get(beat, 0.0)
        except (TypeError, ValueError):
            pause_s = _PAUSE_BY_BEAT.get(beat, 0.0)
        pause_s = round(min(max(pause_s, 0.0), MAX_PAUSE_S), 2)

        ambience = _text(scene.get("ambience"), 20).lower()
        if ambience not in AMBIENCES:
            ambience = infer_ambience(
                scene.get("environment"), scene.get("purpose"), scene.get("narration")
            )

        result.append(
            {
                "beat": beat,
                "chapter_title": chapter,
                "countdown_number": number,
                "lower_third": _text(scene.get("lower_third"), 80),
                "music_mood": mood,
                "pause_after_s": pause_s,
                "ambience": ambience,
                "sfx": _tags(scene.get("sfx"), SOUND_TAGS),
                "camera_move": "",
                "transition": _text(scene.get("transition"), 20).lower(),
            }
        )

    for index, direction in enumerate(result):
        following = result[index + 1] if index < last else None
        written = direction["transition"]
        if following is None:
            direction["transition"] = "cut"
        elif written and written != "cut" and transitions.resolve(written).key != "cut":
            direction["transition"] = transitions.resolve(written).key
        elif following["chapter_title"] and following["beat"] == "countdown":
            direction["transition"] = "whip"
        elif following["chapter_title"]:
            direction["transition"] = "dip"
        elif following["beat"] == "reveal":
            direction["transition"] = "flash"
        elif following["lower_third"]:
            direction["transition"] = "dissolve"
        else:
            direction["transition"] = "cut"

        # The music changes only where the story does.
        direction["new_music_cue"] = index == 0 or bool(direction["chapter_title"])
        if direction["beat"] == "reveal" and not direction["camera_move"]:
            direction["camera_move"] = "reveal_push"

        cues: list[dict[str, Any]] = []
        if index == 0 and direction["beat"] == "hook":
            cues.append({"tag": "drone", "at": "under"})
        if direction["beat"] == "reveal":
            cues.append({"tag": "riser", "at": "before"})
            cues.append({"tag": "impact", "at": "start"})
        if direction["beat"] == "countdown":
            cues.append({"tag": "impact", "at": "start"})
            cues.append({"tag": "tick", "at": "card"})
        elif direction["chapter_title"] and index > 0:
            cues.append({"tag": "boom", "at": "start"})
        sound = transitions.resolve(direction["transition"]).sound
        if sound and following is not None and following["beat"] != "reveal":
            cues.append({"tag": sound, "at": "end"})
        for tag in direction["sfx"]:
            if not any(cue["tag"] == tag for cue in cues):
                cues.append({"tag": tag, "at": "start"})
        direction["cues"] = cues

    # A countdown film's chapter cards are its numbers; the mood is driving
    # from the first entry on unless a scene said otherwise.
    if film_format == "countdown":
        for direction in result:
            if direction["beat"] == "build" and direction["music_mood"] == "steady":
                direction["music_mood"] = "driving"
    return result
