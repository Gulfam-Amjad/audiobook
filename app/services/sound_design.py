"""Turning the director's cues into sounds at exact times, and mixing them.

Everything is placed from numbers the render already measured: where each
shot starts on the finished timeline, how long each transition overlaps,
and when each card is on screen. Nothing is placed by ear.

The placements:

* a fast transition's whoosh peaks in the middle of its overlap
* a riser ends exactly where the reveal shot starts
* an impact lands where a reveal or countdown entry becomes visible
* a countdown card ticks once a second while it is up
* a drone sits under the hook
* an ambience bed runs under every run of shots that share a setting
* music changes at every chapter, one track per act, crossfaded, with the
  last one faded out under the end card

The mix keeps the narration on top: the music and ambience are ducked
against the voice, so they rise by themselves in the pauses, and the
effects are kept below the voice by their own gain. The whole thing is
loudness-normalised to the same target as the plain mix.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.services import mixing, sound_library
from app.services.sound_library import Sound

#: Seconds of crossfade between two acts' music.
MUSIC_CROSSFADE_S = 2.0
MUSIC_FADE_IN_S = 1.5
MUSIC_FADE_OUT_S = 3.0
AMBIENCE_FADE_S = 0.8
#: The drone is a bed under the hook, not a cue, so it fades.
DRONE_FADE_S = 1.2
#: Where the loudest part of a whoosh falls, as a share of its length.
WHOOSH_PEAK = 0.5
#: Clock ticks under a countdown card.
TICK_EVERY_S = 1.0

#: How the render names a countdown card it laid.
COUNTDOWN_CARDS = ("count", "countdown")
#: An act shorter than this keeps the previous act's music rather than
#: changing track for a few seconds.
MIN_ACT_S = 8.0

#: Settings the director uses that have no bed of their own.
AMBIENCE_FALLBACK = {"crowd": "city"}

#: Per-tag trims on top of the effects gain, so a boom does not swamp
#: the voice and a tick is still heard.
TAG_TRIM_DB = {"whoosh": 0.0, "riser": -2.0, "boom": -1.0, "impact": 0.0, "tick": -4.0, "drone": -8.0}


@dataclass
class Event:
    """One sound file at one place on the timeline."""

    path: Path
    start_s: float
    gain_db: float
    tag: str
    #: Set for beds: how long to play, looping the file if it is shorter.
    length_s: float | None = None
    fade_in_s: float = 0.0
    fade_out_s: float = 0.0
    bus: str = "fx"  # "fx", "bed" or "music"
    credit: str = ""


@dataclass
class SoundPlan:
    events: list[Event] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def credits(self) -> list[str]:
        seen: list[str] = []
        for event in self.events:
            if event.credit and event.credit not in seen:
                seen.append(event.credit)
        return seen

    def on(self, bus: str) -> list[Event]:
        return [e for e in self.events if e.bus == bus]


def file_seconds(path: Path) -> float:
    """Length of a WAV from its header; other formats are not measured here."""
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate())
    except (wave.Error, OSError, EOFError):
        return 0.0


def _sound_length(sound: Sound) -> float:
    return sound.duration_s or file_seconds(Path(sound.path)) or 1.0


def _acts(directions: list[dict[str, Any]], starts: list[float], total_s: float) -> list[int]:
    """The shots where the music changes.

    A cue keeps playing through a chapter in the same mood, and a chapter
    too short to establish a new track stays on the old one.
    """
    acts = [0]
    for index in range(1, len(directions)):
        if not (directions[index] or {}).get("new_music_cue"):
            continue
        mood = (directions[index] or {}).get("music_mood")
        if mood == (directions[acts[-1]] or {}).get("music_mood"):
            continue
        acts.append(index)
    kept = [acts[0]]
    for number, first in enumerate(acts[1:], start=1):
        finish = starts[acts[number + 1]] if number + 1 < len(acts) else total_s
        if finish - starts[first] >= MIN_ACT_S:
            kept.append(first)
    return kept


def plan(
    directions: list[dict[str, Any]],
    *,
    shot_starts: list[float],
    clip_durations: list[float],
    overlaps: list[float],
    cards: list[dict[str, Any]],
    total_s: float,
    settings: dict[str, Any],
    music_gain_db: float = -18.0,
    user_music: Path | None = None,
    picker: Callable[..., Sound | None] = sound_library.pick,
    music_picker: Callable[..., Sound | None] | None = sound_library.music_for,
) -> SoundPlan:
    """Every sound in the film, with its absolute start."""
    result = SoundPlan()
    count = min(len(directions), len(shot_starts), len(clip_durations))
    if count == 0:
        return result
    fx_gain = float(settings.get("effects_gain_db", -10))
    bed_gain = float(settings.get("ambience_gain_db", -26))
    used: dict[str, int] = {}

    def choose(kind: str, tag: str) -> Sound | None:
        number = used.get(f"{kind}:{tag}", 0)
        used[f"{kind}:{tag}"] = number + 1
        sound = picker(kind, tag, seed=number)
        if sound is None:
            note = f"No {tag} sound in the library, so that cue is silent."
            if note not in result.notes:
                result.notes.append(note)
        return sound

    def add(sound: Sound, start: float, tag: str, **extra: Any) -> None:
        start = max(0.0, min(start, total_s - 0.05))
        result.events.append(
            Event(Path(sound.path), round(start, 3), extra.pop("gain", fx_gain + TAG_TRIM_DB.get(tag, 0.0)),
                  tag, credit=sound.credit if "Kenney" in sound.credit or sound.kind == "music" else "",
                  **extra)
        )

    for index in range(count):
        direction = directions[index] or {}
        start = float(shot_starts[index])
        length = float(clip_durations[index])
        end = start + length
        overlap_in = float(overlaps[index - 1]) if index > 0 else 0.0
        overlap_out = float(overlaps[index]) if index < len(overlaps) else 0.0
        for cue in direction.get("cues") or []:
            tag = str(cue.get("tag") or "")
            at = str(cue.get("at") or "start")
            if at == "under":
                sound = choose("sfx", tag)
                if sound:
                    add(sound, start, tag, length_s=max(1.0, length - overlap_out),
                        fade_in_s=DRONE_FADE_S, fade_out_s=DRONE_FADE_S)
            elif at == "before":
                sound = choose("sfx", tag)
                if sound and index > 0:
                    # Ends where the new shot is fully on screen.
                    add(sound, start + overlap_in - _sound_length(sound), tag)
            elif at == "start":
                sound = choose("sfx", tag)
                if sound:
                    add(sound, start + overlap_in / 2, tag)
            elif at == "card":
                for card in cards:
                    if start <= float(card["start_s"]) < end and card.get("kind") in COUNTDOWN_CARDS:
                        moment = float(card["start_s"]) + 0.15
                        while moment < float(card["end_s"]) - 0.2:
                            sound = choose("sfx", tag)
                            if sound:
                                add(sound, moment, tag)
                            moment += TICK_EVERY_S
            elif at == "end":
                if overlap_out <= 0 or index + 1 >= count:
                    continue
                sound = choose("sfx", tag)
                if sound:
                    middle = float(shot_starts[index + 1]) + overlap_out / 2
                    add(sound, middle - _sound_length(sound) * WHOOSH_PEAK, tag)

    # --- ambience beds over runs of shots with the same setting ------------
    run_tag, run_start = "", 0.0
    for index in range(count + 1):
        tag = ""
        if index < count:
            tag = str((directions[index] or {}).get("ambience") or "")
            tag = AMBIENCE_FALLBACK.get(tag, tag)
        if tag == run_tag:
            continue
        here = float(shot_starts[index]) if index < count else total_s
        if run_tag:
            sound = choose("ambience", run_tag)
            if sound:
                add(sound, run_start, run_tag, gain=bed_gain, bus="bed",
                    length_s=max(1.0, here - run_start),
                    fade_in_s=AMBIENCE_FADE_S, fade_out_s=AMBIENCE_FADE_S)
        run_tag, run_start = tag, here

    # --- music: one track per act -------------------------------------------
    if user_music is not None:
        result.events.append(
            Event(user_music, 0.0, music_gain_db, "music", length_s=total_s,
                  fade_in_s=MUSIC_FADE_IN_S, fade_out_s=MUSIC_FADE_OUT_S, bus="music")
        )
    elif settings.get("music_from_library") and music_picker is not None:
        acts = _acts(directions[:count], shot_starts[:count], total_s)
        avoid: set[str] = set()
        for number, first in enumerate(acts):
            mood = str((directions[first] or {}).get("music_mood") or "steady")
            begin = float(shot_starts[first])
            finish = float(shot_starts[acts[number + 1]]) if number + 1 < len(acts) else total_s
            last = number + 1 == len(acts)
            try:
                track = music_picker(mood, avoid=avoid, seed=number)
            except Exception as exc:  # network or catalogue trouble
                track = None
                result.notes.append(f"Music for the {mood} act could not be fetched ({exc}).")
            if track is None:
                result.notes.append(f"No {mood} music was available, so that act has none.")
                continue
            avoid.add(track.path)
            lead = MUSIC_CROSSFADE_S / 2 if number else 0.0
            tail = 0.0 if last else MUSIC_CROSSFADE_S / 2
            result.events.append(
                Event(Path(track.path), round(max(0.0, begin - lead), 3), music_gain_db, "music",
                      length_s=round(finish - begin + lead + tail, 3),
                      fade_in_s=MUSIC_FADE_IN_S if number == 0 else MUSIC_CROSSFADE_S,
                      fade_out_s=MUSIC_FADE_OUT_S if last else MUSIC_CROSSFADE_S,
                      bus="music", credit=track.credit)
            )
    result.events.sort(key=lambda e: (e.bus, e.start_s))
    return result


# ---------------------------------------------------------------------------
# Mixing
# ---------------------------------------------------------------------------

#: Narration finishing chain: rumble cut, gentle levelling, de-essing,
#: a little presence, and a short, quiet room so it does not sound boxed.
VOICE_CHAIN = (
    "highpass=f=75,"
    "acompressor=threshold=0.1:ratio=2.5:attack=12:release=180:makeup=1.6,"
    "deesser=i=0.35:m=0.5:f=0.5,"
    "equalizer=f=3200:t=q:w=1.2:g=2,"
    "equalizer=f=200:t=q:w=1:g=-1.5,"
    "aecho=0.9:0.9:23|41:0.07|0.04"
)

_FORMAT = f"aresample={mixing.SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"


def _chain(index: int, event: Event, label: str) -> str:
    parts = [f"[{index}:a]{_FORMAT}"]
    if event.length_s is not None:
        parts.append(f"atrim=0:{event.length_s:.3f},asetpts=PTS-STARTPTS")
        if event.fade_in_s:
            parts.append(f"afade=t=in:d={event.fade_in_s:.2f}")
        if event.fade_out_s:
            begin = max(0.0, event.length_s - event.fade_out_s)
            parts.append(f"afade=t=out:st={begin:.3f}:d={event.fade_out_s:.2f}")
    parts.append(f"volume={event.gain_db:g}dB")
    parts.append(f"adelay={int(round(event.start_s * 1000))}:all=1")
    return ",".join(parts) + f"[{label}]"


def _bus(labels: list[str], out: str) -> str:
    if len(labels) == 1:
        return f"[{labels[0]}]anull[{out}]"
    joined = "".join(f"[{label}]" for label in labels)
    return f"{joined}amix=inputs={len(labels)}:normalize=0:dropout_transition=0[{out}]"


def mix_graph(sound: SoundPlan, *, voice_chain: bool = True, loudnorm: bool = True) -> str:
    """Input 0 is the narration; inputs 1.. are the events in order."""
    chains = [f"[0:a]{_FORMAT}" + (f",{VOICE_CHAIN}" if voice_chain else "") + ",asplit=2[sc][voice]"]
    buses: dict[str, list[str]] = {"fx": [], "bed": [], "music": []}
    for number, event in enumerate(sound.events, start=1):
        label = f"e{number}"
        chains.append(_chain(number, event, label))
        buses[event.bus].append(label)

    under = buses["bed"] + buses["music"]
    final = ["[voice]"]
    if under:
        chains.append(_bus(under, "under"))
        chains.append(
            f"[under][sc]sidechaincompress=threshold={mixing.DUCK_THRESHOLD}:"
            f"ratio={mixing.DUCK_RATIO}:attack={mixing.DUCK_ATTACK_MS}:"
            f"release={mixing.DUCK_RELEASE_MS}[ducked]"
        )
        final.append("[ducked]")
    else:
        chains.append("[sc]anullsink")
    if buses["fx"]:
        chains.append(_bus(buses["fx"], "fx"))
        final.append("[fx]")
    tail = (
        f",loudnorm=I={mixing.TARGET_LUFS}:TP={mixing.TARGET_TRUE_PEAK}:LRA={mixing.TARGET_LRA}"
        if loudnorm
        else ""
    )
    chains.append(
        f"{''.join(final)}amix=inputs={len(final)}:duration=first:normalize=0:"
        f"dropout_transition=0{tail}[mix]"
    )
    return ";".join(chains)


def mix_command(
    ffmpeg_path: str,
    speech: Path,
    sound: SoundPlan,
    destination: Path,
    *,
    total_s: float,
    voice_chain: bool = True,
    graph_file: Path | None = None,
) -> list[str]:
    command = [ffmpeg_path, "-hide_banner", "-y", "-i", str(speech)]
    for event in sound.events:
        if event.length_s is not None:
            # Beds loop so a short file still covers its stretch.
            command += ["-stream_loop", "-1"]
        command += ["-i", str(event.path)]
    graph = mix_graph(sound, voice_chain=voice_chain)
    if graph_file is not None:
        # The graph of a whole film runs past the Windows command-line limit,
        # so it is read from a file. ``-/filter_complex`` is the file form.
        graph_file.write_text(graph, encoding="utf-8")
        command += ["-/filter_complex", str(graph_file)]
    else:
        command += ["-filter_complex", graph]
    command += [
        "-map", "[mix]",
        "-t", f"{total_s:.3f}",
        "-c:a", "pcm_s16le",
        "-ar", str(mixing.SAMPLE_RATE),
        "-ac", str(mixing.CHANNELS),
        str(destination),
    ]
    return command
