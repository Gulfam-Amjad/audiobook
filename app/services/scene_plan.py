"""The scene plan: its JSON schema, its prompt, and its validator.

The validator is the gate that stands between a generated script and any
spending. A plan with errors is never turned into jobs, because every scene
that reaches the motion stage costs either a website credit, borrowed GPU
time, or real money.

Errors block. Warnings do not: a slightly short script or an unusual shot
length is a judgement call, and refusing those outright would make the app
unusable without telling the user anything new.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.constants import AnimationMethod
from app.providers.script.base import ImageHint, ScriptRequest

#: Methods a writer is allowed to ask for. Lip-sync is deliberately absent:
#: it is derived from whether a scene has dialogue and a visible speaker,
#: not something the writer declares.
REQUESTABLE_METHODS: tuple[str, ...] = (
    AnimationMethod.API_I2V,
    AnimationMethod.MANUAL_I2V,
    AnimationMethod.REMOTE_I2V,
    AnimationMethod.CAMERA_MOVE,
    AnimationMethod.STILL,
)

TRANSITIONS: tuple[str, ...] = (
    "cut", "fade", "dissolve", "dip", "whip", "wipe", "zoom", "flash",
)
BEATS: tuple[str, ...] = ("hook", "build", "reveal", "countdown", "outro")
MOODS: tuple[str, ...] = (
    "tense", "steady", "epic", "driving", "reflective", "dark", "hopeful",
)

MIN_SCENE_S = 2.0
MAX_SCENE_S = 30.0
MIN_SCENES = 2
#: Above this, a five-minute video is being cut into unwatchable fragments.
MAX_SCENES = 80

#: How far the planned total may drift from the target before it is an
#: error rather than a note. Narration timing is an estimate at this stage,
#: so a tight tolerance would reject usable scripts.
DURATION_TOLERANCE = 0.35

_WORD_RE = re.compile(r"[\w']+")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def scene_plan_schema() -> dict[str, Any]:
    """JSON Schema for one scene plan.

    Sent to providers that enforce a schema server-side, and used as the
    written contract for those that do not.
    """
    return {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "A specific, non-generic title for the finished video.",
            },
            "logline": {
                "type": "string",
                "description": "One sentence describing the story.",
            },
            "synopsis": {
                "type": "string",
                "description": "A short paragraph covering beginning, middle and end.",
            },
            "format": {
                "type": "string",
                "enum": ["documentary", "countdown"],
                "description": "documentary for one story, countdown for a numbered top-N film.",
            },
            "scenes": {
                "type": "array",
                "description": "Shots in playing order.",
                "minItems": MIN_SCENES,
                "maxItems": MAX_SCENES,
                "items": {
                    "type": "object",
                    "properties": {
                        "scene_key": {
                            "type": "string",
                            "description": "Short stable id such as s01. Unique.",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "What this shot does for the story.",
                        },
                        "target_duration_s": {
                            "type": "number",
                            "minimum": MIN_SCENE_S,
                            "maximum": MAX_SCENE_S,
                            "description": "Intended on-screen length in seconds.",
                        },
                        "narration": {
                            "type": ["string", "null"],
                            "description": "Voice-over for this shot, or null.",
                        },
                        "dialogue": {
                            "type": "array",
                            "description": "Spoken lines in delivery order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "character": {"type": "string"},
                                    "line": {"type": "string"},
                                },
                                "required": ["character", "line"],
                            },
                        },
                        "characters": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Characters visible in this shot.",
                        },
                        "image_hint": {
                            "type": "string",
                            "description": (
                                "Filename of the still to use, chosen from the "
                                "supplied list. Empty if none fits."
                            ),
                        },
                        "action": {
                            "type": "string",
                            "description": "What physically happens in the shot.",
                        },
                        "expression": {
                            "type": "string",
                            "description": "Facial expression and gesture.",
                        },
                        "camera": {
                            "type": "string",
                            "description": "Camera framing and movement.",
                        },
                        "environment": {
                            "type": "string",
                            "description": "Setting and background.",
                        },
                        "sfx": {
                            "type": "string",
                            "description": "Sound effect cue, or empty.",
                        },
                        "transition": {
                            "type": "string",
                            "enum": list(TRANSITIONS),
                            "description": (
                                "Transition into the next shot. Leave as cut unless "
                                "the story changes there; the editor adds the rest."
                            ),
                        },
                        "beat": {
                            "type": "string",
                            "enum": list(BEATS),
                            "description": (
                                "hook for the cold open, reveal for the turn the "
                                "film builds to, countdown for a numbered entry, "
                                "outro for the last shot, build otherwise."
                            ),
                        },
                        "chapter_title": {
                            "type": "string",
                            "description": "Two to four words on a title card that opens a new chapter, or empty.",
                        },
                        "countdown_number": {
                            "type": ["integer", "null"],
                            "description": "The entry number in a countdown film, counting down. Null otherwise.",
                        },
                        "lower_third": {
                            "type": "string",
                            "description": "Place and date label, e.g. 'Omaha Beach, 6 June 1944', or empty.",
                        },
                        "music_mood": {
                            "type": "string",
                            "enum": list(MOODS),
                            "description": "Mood of the music from this shot on.",
                        },
                        "ambience": {
                            "type": "string",
                            "description": "Background sound of the place: sea, wind, crowd, rain, fire, radio, room, night, city, battle, or empty.",
                        },
                        "pause_after_s": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 2,
                            "description": "Seconds to hold the picture in silence after the words, for a beat to land.",
                        },
                        "preferred_method": {
                            "type": "string",
                            "enum": list(REQUESTABLE_METHODS),
                            "description": (
                                "How this shot should be animated. Ask for "
                                "generated motion only where movement carries "
                                "the story; otherwise a camera move."
                            ),
                        },
                    },
                    "required": [
                        "scene_key",
                        "purpose",
                        "target_duration_s",
                        "characters",
                        "preferred_method",
                    ],
                },
            },
        },
        "required": ["title", "logline", "scenes"],
    }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_prompt(request: ScriptRequest, motion_budget_s: float) -> str:
    """Instructions for the writer.

    The motion budget is stated plainly so the writer rations generated
    motion itself, rather than asking for forty animated shots that the
    router then has to downgrade.
    """
    lines: list[str] = [
        "You are writing an original short film for a YouTube channel.",
        "",
        f"Channel niche: {request.niche or 'unspecified'}",
        f"Audience: {request.audience or 'general'}",
        f"Storytelling style: {request.story_style or 'unspecified'}",
        f"Language: {request.language}",
        f"Target length: {request.target_duration_s} seconds of finished video.",
        f"Narration is spoken at about {request.speaking_rate_wpm} words per minute.",
        f"Aim for roughly {request.scene_target_s:.0f} seconds per shot, "
        f"and no more than {request.max_scenes} shots.",
        "",
        "Requirements:",
        "- Tell one complete story: a clear opening, real development, and an ending that lands.",
        "- The first sentence is a problem already happening, in fewer than twenty words. No welcome and no 'in this video'.",
        "- One shot carries one idea. The picture is the noun in the sentence. The sentence is the risk, the clock, or the consequence, not a description of what the picture already shows.",
        "- Mark the beats: the first shot is the hook, the turn the film builds to is a reveal, the last shot is the outro. Give a chapter_title only where a new chapter starts, and a lower_third where the place or date changes.",
        "- For a countdown film set format to countdown and number the entries downwards, ending on 1.",
        "- Write specific, concrete prose. No filler, no stock phrases, no 'in this video'.",
        "- Do not repeat narration lines or re-describe the same beat twice.",
        "- Every shot must carry either narration or dialogue. Silent shots are not allowed.",
        "- Keep characters consistent: same names, same described appearance throughout.",
        (
            "- Write narration to fit the shot length. At "
            f"{request.speaking_rate_wpm} words per minute, a {request.scene_target_s:.0f}"
            f"-second shot holds about "
            f"{int(request.scene_target_s * request.speaking_rate_wpm / 60)} words."
        ),
    ]

    if motion_budget_s > 0:
        lines.append(
            f"- Generated motion is rationed: at most about {motion_budget_s:.0f} "
            "seconds across the whole video. Ask for api_i2v, manual_i2v or "
            "remote_i2v only on the few shots where movement genuinely carries "
            "the story. Use camera_move for everything else."
        )
    else:
        lines.append(
            "- No generated motion is available for this video. Use camera_move "
            "or still for every shot, and write so that stillness reads as "
            "deliberate rather than as a slideshow."
        )

    if request.character_names:
        lines += ["", "Characters already defined: " + ", ".join(request.character_names)]

    if request.available_images:
        lines += [
            "",
            "Available stills. Choose image_hint from these filenames only, "
            "and leave it empty when none fits:",
        ]
        for hint in request.available_images:
            size = f" ({hint.width}x{hint.height})" if hint.width and hint.height else ""
            note = f" - {hint.note}" if hint.note else ""
            lines.append(f"  - {hint.filename}{size}{note}")

    if request.brief:
        lines += ["", "The user's brief, which takes priority:", request.brief]

    if request.previous_problems:
        lines += [
            "",
            "A previous attempt was rejected for these reasons. Fix all of them:",
        ]
        lines += [f"  - {problem}" for problem in request.previous_problems]

    lines += ["", "Reply with JSON matching the supplied schema and nothing else."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    word_count: int = 0
    estimated_duration_s: float = 0.0
    planned_duration_s: float = 0.0
    scene_count: int = 0
    generated_motion_requested_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "word_count": self.word_count,
            "estimated_duration_s": self.estimated_duration_s,
            "planned_duration_s": self.planned_duration_s,
            "scene_count": self.scene_count,
            "generated_motion_requested_s": self.generated_motion_requested_s,
        }


def count_words(text: str | None) -> int:
    if not text:
        return 0
    return len(_WORD_RE.findall(text))


def scene_speech(scene: dict[str, Any]) -> str:
    """Every spoken word in a scene, narration and dialogue together."""
    parts: list[str] = []
    narration = scene.get("narration")
    if isinstance(narration, str) and narration.strip():
        parts.append(narration.strip())
    for entry in scene.get("dialogue") or []:
        if isinstance(entry, dict):
            line = entry.get("line")
            if isinstance(line, str) and line.strip():
                parts.append(line.strip())
    return " ".join(parts)


def validate(
    plan: dict[str, Any],
    *,
    target_duration_s: int,
    speaking_rate_wpm: int,
    motion_budget_s: float,
    known_images: tuple[ImageHint, ...] = (),
) -> ValidationReport:
    """Check a plan hard enough that nothing downstream has to re-check it."""
    report = ValidationReport()

    if not isinstance(plan, dict):
        report.errors.append("The reply was not a JSON object.")
        return report

    if not str(plan.get("title") or "").strip():
        report.errors.append("The plan has no title.")

    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        report.errors.append("The plan has no scenes.")
        return report

    report.scene_count = len(scenes)
    if len(scenes) < MIN_SCENES:
        report.errors.append(f"A plan needs at least {MIN_SCENES} scenes.")
    if len(scenes) > MAX_SCENES:
        report.errors.append(
            f"{len(scenes)} scenes is more than the {MAX_SCENES} this pipeline will render."
        )

    known_filenames = {hint.filename for hint in known_images}
    seen_keys: set[str] = set()
    seen_narration: dict[str, int] = {}
    declared_characters: set[str] = set()

    for index, scene in enumerate(scenes):
        label = f"Scene {index + 1}"
        if not isinstance(scene, dict):
            report.errors.append(f"{label} is not an object.")
            continue

        key = str(scene.get("scene_key") or "").strip()
        if not key:
            report.errors.append(f"{label} has no scene_key.")
        elif key in seen_keys:
            report.errors.append(f"{label} reuses the scene_key '{key}'.")
        else:
            seen_keys.add(key)

        duration = scene.get("target_duration_s")
        if not isinstance(duration, (int, float)):
            report.errors.append(f"{label} has no numeric target_duration_s.")
            duration = 0.0
        elif not MIN_SCENE_S <= float(duration) <= MAX_SCENE_S:
            report.errors.append(
                f"{label} is {float(duration):.1f}s, outside the "
                f"{MIN_SCENE_S:.0f}-{MAX_SCENE_S:.0f}s a shot may run."
            )
        report.planned_duration_s += float(duration or 0.0)

        speech = scene_speech(scene)
        if not speech:
            report.errors.append(f"{label} has neither narration nor dialogue.")
        else:
            words = count_words(speech)
            report.word_count += words
            # A shot whose narration cannot physically fit will either rush
            # or overrun, and both wreck the edit.
            spoken_s = words / max(speaking_rate_wpm, 1) * 60.0
            if duration and spoken_s > float(duration) * 1.6:
                report.warnings.append(
                    f"{label} has about {words} words ({spoken_s:.1f}s of speech) "
                    f"for a {float(duration):.1f}s shot. It will have to run longer."
                )

        narration = str(scene.get("narration") or "").strip().lower()
        if narration:
            if narration in seen_narration:
                report.errors.append(
                    f"{label} repeats the narration of scene {seen_narration[narration]}."
                )
            else:
                seen_narration[narration] = index + 1

        characters = scene.get("characters")
        if not isinstance(characters, list):
            report.errors.append(f"{label} has no characters list.")
            characters = []
        declared_characters.update(str(name).strip() for name in characters if str(name).strip())

        for entry in scene.get("dialogue") or []:
            if not isinstance(entry, dict):
                report.errors.append(f"{label} has a malformed dialogue entry.")
                continue
            speaker = str(entry.get("character") or "").strip()
            if not speaker:
                report.errors.append(f"{label} has a dialogue line with no speaker.")
            elif speaker not in {str(name).strip() for name in characters}:
                report.errors.append(
                    f"{label} gives a line to '{speaker}', who is not listed "
                    "among the characters visible in that shot."
                )

        method = str(scene.get("preferred_method") or "").strip()
        if method not in REQUESTABLE_METHODS:
            report.errors.append(
                f"{label} asks for method '{method or 'nothing'}', which is not one of "
                f"{', '.join(REQUESTABLE_METHODS)}."
            )
        elif method in {
            AnimationMethod.API_I2V,
            AnimationMethod.MANUAL_I2V,
            AnimationMethod.REMOTE_I2V,
        }:
            report.generated_motion_requested_s += float(duration or 0.0)

        transition = str(scene.get("transition") or "").strip()
        if transition and transition not in TRANSITIONS:
            report.warnings.append(
                f"{label} asks for an unknown transition '{transition}'; a cut will be used."
            )

        hint = str(scene.get("image_hint") or "").strip()
        if hint and known_filenames and hint not in known_filenames:
            report.warnings.append(
                f"{label} names a still '{hint}' that is not in the library; "
                "one will be chosen for it."
            )

    report.estimated_duration_s = round(
        report.word_count / max(speaking_rate_wpm, 1) * 60.0, 2
    )
    report.planned_duration_s = round(report.planned_duration_s, 2)
    report.generated_motion_requested_s = round(report.generated_motion_requested_s, 2)

    if target_duration_s > 0 and report.planned_duration_s:
        drift = abs(report.planned_duration_s - target_duration_s) / target_duration_s
        if drift > DURATION_TOLERANCE:
            report.errors.append(
                f"The shots add up to {report.planned_duration_s:.0f}s against a "
                f"{target_duration_s}s target, which is more than "
                f"{DURATION_TOLERANCE:.0%} out."
            )
        elif drift > DURATION_TOLERANCE / 2:
            report.warnings.append(
                f"The shots add up to {report.planned_duration_s:.0f}s against a "
                f"{target_duration_s}s target."
            )

    if report.generated_motion_requested_s > motion_budget_s:
        report.warnings.append(
            f"The plan asks for {report.generated_motion_requested_s:.0f}s of generated "
            f"motion against a {motion_budget_s:.0f}s budget. The router will "
            "downgrade the lowest-priority shots to camera moves."
        )

    if len(declared_characters) > 12:
        report.warnings.append(
            f"{len(declared_characters)} named characters is a lot to keep "
            "visually consistent."
        )

    return report
