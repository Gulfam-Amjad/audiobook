"""The finishing pass: what turns a cut into an edited film.

A project's ``production`` settings switch it on. Projects made before it
existed have none, and render exactly as they did.
"""

from __future__ import annotations

from typing import Any

from app.models import Project, Scene
from app.services import transitions

LOOKS = ("none", "archive", "clean", "night")
CAPTIONS = ("words", "lines", "off")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "format": "documentary",
    "look": "clean",
    "captions": "words",
    "transitions": True,
    "cards": True,
    "sound_design": True,
    #: Levels relative to the voice, in dB before the final loudness pass.
    "effects_gain_db": -10.0,
    "ambience_gain_db": -26.0,
    #: Music cues come from the library by mood. A project with its own
    #: ``music_path`` keeps that single bed instead.
    "music_from_library": True,
    "end_card": "",
    "voice_chain": True,
}

#: When a shot has no camera note, the moves are varied in this order so
#: a film is not eighty identical pushes.
VARIETY = ("push_in", "drift_up", "pan_right", "push_focus", "pull_out", "drift_down", "pan_left")


#: Colour and texture per look. Applied to every shot as it is matched to
#: the output frame, so stills and footage get the same grade.
LOOK_FILTERS: dict[str, str] = {
    "none": "",
    # Warm, slightly faded, with moving grain and a soft vignette.
    "archive": (
        "eq=contrast=1.06:saturation=0.82:gamma=0.97,"
        "colorbalance=rs=0.04:gs=0.01:bs=-0.04:rm=0.03:bm=-0.03,"
        "noise=alls=6:allf=t,vignette=angle=PI/5"
    ),
    "clean": "eq=contrast=1.05:saturation=1.06,unsharp=5:5:0.35:5:5:0.0,vignette=angle=PI/7",
    "night": (
        "eq=contrast=1.08:saturation=0.72:brightness=-0.02,"
        "colorbalance=rs=-0.03:bs=0.06:bm=0.04,vignette=angle=PI/4"
    ),
}


def look_filter(finishing: dict[str, Any]) -> str:
    return LOOK_FILTERS.get(str(finishing.get("look") or "none"), "")


def settings(project: Project) -> dict[str, Any]:
    """The project's finishing settings, or an empty dict when it is off."""
    raw = dict(project.production or {})
    if not raw.get("enabled"):
        return {}
    merged = {**DEFAULTS, **raw}
    if merged["look"] not in LOOKS:
        merged["look"] = "clean"
    if merged["captions"] not in CAPTIONS:
        merged["captions"] = "words"
    return merged


def default_look(niche: str) -> str:
    lowered = (niche or "").lower()
    if any(word in lowered for word in ("history", "war", "past", "archive", "event")):
        return "archive"
    if any(word in lowered for word in ("crime", "mystery", "horror", "dark")):
        return "night"
    return "clean"


#: The last picture is held after the last word so the music can finish,
#: and held longer when an end card has to be read over it.
ENDING_HOLD_S = 1.5
ENDING_HOLD_CARD_S = 3.0


def apply_holds(
    scenes: list[Scene], fps: int, *, with_transitions: bool = True, end_card: bool = False
) -> None:
    """Lengthen each shot by its held beat and its transition overlap.

    The overlap is given back on the finished timeline when the next shot
    starts over this one's tail, so every line still lands where it would
    on a plain cut.
    """
    last = len(scenes) - 1
    for index, scene in enumerate(scenes):
        direction = dict(scene.direction or {})
        if not direction:
            continue
        overlap = (
            transitions.overlap_for(direction.get("transition")) if with_transitions else 0.0
        )
        overlap = round(overlap * fps) / fps
        pause = float(direction.get("pause_after_s") or 0.0)
        if index == last:
            pause = max(pause, ENDING_HOLD_CARD_S if end_card else ENDING_HOLD_S)
        direction["extra_s"] = round(pause + overlap, 4)
        scene.direction = direction
