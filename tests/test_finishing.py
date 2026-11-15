"""The finishing pass: easing, transitions, direction, captions, maps, sound."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import camera, captions, directing, maps, sound_design, transitions
from app.services.sound_library import Sound

# --- easing ---------------------------------------------------------------


def _eval(expression: str, progress: float) -> float:
    return eval(  # noqa: S307 - our own expression, numbers only
        expression.replace("PI", str(math.pi)).replace("PROGRESS", str(progress)),
        {"cos": math.cos, "pow": pow},
    )


@pytest.mark.parametrize("curve", ["inout", "out"])
def test_ease_runs_from_zero_to_one(curve: str) -> None:
    expression = camera.ease("PROGRESS", curve)
    assert _eval(expression, 0.0) == pytest.approx(0.0, abs=1e-9)
    assert _eval(expression, 1.0) == pytest.approx(1.0, abs=1e-9)
    values = [_eval(expression, i / 20) for i in range(21)]
    assert values == sorted(values)


def test_inout_starts_slower_than_out() -> None:
    assert _eval(camera.ease("PROGRESS", "inout"), 0.1) < _eval(camera.ease("PROGRESS", "out"), 0.1)


def test_focus_moves_take_a_focus_point() -> None:
    graph = camera.build_filter("push_focus", width=1280, height=720, fps=24, duration_s=4.0,
                                focus=(0.3, 0.4))
    assert "zoompan" in graph and "d=96" in graph


# --- transitions ----------------------------------------------------------


def test_pieces_add_up_to_the_timeline() -> None:
    durations = [4.0, 3.0, 5.0]
    names = ["dissolve", "whip", None]
    fps = 24
    built = transitions.pieces(durations, names, fps)
    overlaps = transitions.overlap_frames(durations, names, fps)
    total_frames = sum(round(d * fps) for d in durations) - sum(f for f, _ in overlaps)
    assert sum(p.frames for p in built) == total_frames
    assert [p.kind for p in built] == ["body", "overlap", "body", "overlap", "body"]
    assert overlaps[0] == (round(0.6 * fps), "fade")
    assert overlaps[1] == (round(0.35 * fps), "smoothleft")


def test_short_clip_falls_back_to_a_cut() -> None:
    overlaps = transitions.overlap_frames([0.5, 4.0], ["dip"], 24)
    assert overlaps == [(0, "")]


def test_unknown_transition_is_a_cut_and_aliases_resolve() -> None:
    assert transitions.resolve("spiral").key == "cut"
    assert transitions.resolve("Fade to black").key == "dip"
    assert transitions.resolve("whip pan").sound == "whoosh"


# --- direction ------------------------------------------------------------


def test_fill_defaults_derives_editing_from_beats() -> None:
    scenes = [
        {"narration": "The ramps fell.", "environment": "a beach under grey sea"},
        {"narration": "Number three.", "countdown_number": 3},
        {"narration": "It was not a drill.", "beat": "reveal"},
        {"narration": "It ended."},
    ]
    out = directing.fill_defaults(scenes, film_format="countdown")
    assert [d["beat"] for d in out] == ["hook", "countdown", "reveal", "outro"]
    assert out[0]["transition"] == "whip"  # into a countdown entry
    assert out[1]["transition"] == "flash"  # into a reveal
    assert out[-1]["transition"] == "cut"
    assert out[1]["chapter_title"] == "Number 3"
    assert out[0]["ambience"] == "sea"
    assert {"tag": "drone", "at": "under"} in out[0]["cues"]
    assert {"tag": "whoosh", "at": "end"} in out[0]["cues"]
    assert {"tag": "riser", "at": "before"} in out[2]["cues"]
    assert out[0]["new_music_cue"] and out[1]["new_music_cue"] and not out[2]["new_music_cue"]
    assert out[2]["camera_move"] == "reveal_push"


def test_explicit_transition_is_kept() -> None:
    out = directing.fill_defaults([{"narration": "a", "transition": "wipe"}, {"narration": "b"}])
    assert out[0]["transition"] == "wipe"


# --- captions -------------------------------------------------------------


def test_align_keeps_script_words_and_fills_misheard_ones() -> None:
    heard = [captions.Word("At", 0.0, 0.2), captions.Word("6.30", 0.2, 0.8),
             captions.Word("the", 0.9, 1.0), captions.Word("ramps", 1.0, 1.4)]
    words = captions.align("At six thirty the ramps", heard, 1.6)
    assert [w.text for w in words] == ["At", "six", "thirty", "the", "ramps"]
    assert words[1].start_s >= words[0].end_s - 1e-6
    assert words[3].start_s == pytest.approx(0.9)
    starts = [w.start_s for w in words]
    assert starts == sorted(starts)


def test_ass_lights_one_word_at_a_time() -> None:
    words = captions.spread("The sea turned red.", 2.0)
    text = captions.to_ass(words, width=1920, height=1080, measured=False)
    assert "ESTIMATED" in text
    dialogue = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue) == 4
    assert all(line.count("&H3AB0E8&") == 1 for line in dialogue)
    assert captions.ass_time(3725.456) == "1:02:05.46"


def test_groups_break_after_punctuation() -> None:
    words = captions.spread("Stop. Then go on now", 2.0)
    assert [len(g) for g in captions.groups(words)] == [1, 3, 1]


# --- maps -----------------------------------------------------------------


def test_landmark_is_found_from_the_lower_third() -> None:
    spot = maps.locate({"lower_third": "Omaha Beach, June 1944"})
    assert spot is not None and spot["label"] == "Omaha Beach"
    assert spot["lat"] == pytest.approx(49.369)


def test_explicit_map_point_and_no_place() -> None:
    assert maps.locate({"map": {"lat": 1, "lon": 2, "label": "X"}})["lon"] == 2.0
    assert maps.locate({"lower_third": ""}) is None


# --- sound placement ------------------------------------------------------


def _picker(tmp: Path):
    def pick(kind: str, tag: str, *, seed: int = 0) -> Sound:
        return Sound(str(tmp / f"{kind}-{tag}-{seed}.wav"), kind, tag, "Public domain", "Generated", "")
    return pick


def _music(mood: str, *, avoid=None, seed: int = 0) -> Sound:
    return Sound(f"/m/{mood}-{seed}.mp3", "music", mood, "CC BY 4.0", "incompetech",
                 f'"{mood}" Kevin MacLeod (incompetech.com)', 180.0, mood)


def test_sound_events_land_on_the_measured_times(tmp_path: Path) -> None:
    directions = directing.fill_defaults(
        [{"narration": "a", "environment": "rain on the roof"},
         {"narration": "b", "countdown_number": 2},
         {"narration": "c", "beat": "reveal"},
         {"narration": "d"}],
        film_format="countdown",
    )
    durations = [6.0, 6.0, 6.0, 6.0]
    overlaps = [0.375, 0.375, 0.0, 0.0]
    starts = [0.0]
    for i in range(3):
        starts.append(starts[-1] + durations[i] - overlaps[i])
    total = starts[-1] + durations[-1]
    card = {"kind": "count", "start_s": starts[1] + 0.3, "end_s": starts[1] + 2.9}
    plan = sound_design.plan(
        directions, shot_starts=starts, clip_durations=durations, overlaps=overlaps,
        cards=[card], total_s=total,
        settings={"effects_gain_db": -10, "ambience_gain_db": -26, "music_from_library": True},
        picker=_picker(tmp_path), music_picker=_music,
    )
    by_tag: dict[str, list[sound_design.Event]] = {}
    for event in plan.events:
        by_tag.setdefault(event.tag, []).append(event)

    # The whoosh is centred on the whip's overlap.
    whoosh = by_tag["whoosh"][0]
    assert whoosh.start_s == pytest.approx(starts[1] + 0.375 / 2 - 1.0 * sound_design.WHOOSH_PEAK, abs=0.01)
    # Three ticks a second apart under the countdown card.
    ticks = [e.start_s for e in by_tag["tick"]]
    assert ticks == pytest.approx([card["start_s"] + 0.15 + i for i in range(3)], abs=0.01)
    # The riser is placed to end where the reveal is fully on screen.
    riser = by_tag["riser"][0]
    assert riser.start_s + 1.0 == pytest.approx(starts[2] + overlaps[1], abs=0.01)
    # Rain under the first shot only, as a looping bed.
    rain = [e for e in plan.on("bed") if e.tag == "rain"]
    assert rain and rain[0].length_s == pytest.approx(starts[1])
    # Two acts, two tracks, crossfaded, each credited.
    music = plan.on("music")
    assert len(music) == 2 and music[0].path != music[1].path
    assert music[1].start_s < starts[1] < music[0].start_s + music[0].length_s
    assert len([c for c in plan.credits if "MacLeod" in c]) == 2


def test_music_holds_through_short_or_same_mood_chapters() -> None:
    directions = [
        {"new_music_cue": True, "music_mood": "tense"},
        {"new_music_cue": True, "music_mood": "driving"},
        {"new_music_cue": True, "music_mood": "driving"},
        {"new_music_cue": True, "music_mood": "epic"},
    ]
    assert sound_design._acts(directions, [0.0, 20.0, 40.0, 60.0], 65.0) == [0, 1]
    assert sound_design._acts(directions, [0.0, 20.0, 40.0, 60.0], 90.0) == [0, 1, 3]


def test_user_music_wins_over_the_library(tmp_path: Path) -> None:
    plan = sound_design.plan(
        [{"cues": []}], shot_starts=[0.0], clip_durations=[5.0], overlaps=[0.0], cards=[],
        total_s=5.0, settings={"music_from_library": True}, user_music=tmp_path / "bed.mp3",
        picker=_picker(tmp_path), music_picker=_music,
    )
    assert [e.path.name for e in plan.on("music")] == ["bed.mp3"]


@pytest_asyncio.fixture
async def client(session: AsyncSession):
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_project_screen_offers_the_finished_film(client: AsyncClient) -> None:
    created = await client.post("/projects", data={"name": "Film button", "niche": "history"})
    assert created.status_code == 200
    detail = await client.get("/projects/1")
    assert detail.status_code == 200
    assert "Make the finished film" in detail.text
    assert 'value="archive" selected' in detail.text
    panel = await client.get("/partials/projects/1/film")
    assert panel.status_code == 200 and "Approve a script first." in panel.text


def test_mix_graph_ducks_beds_under_the_voice(tmp_path: Path) -> None:
    plan = sound_design.SoundPlan(events=[
        sound_design.Event(tmp_path / "a.wav", 1.0, -10, "impact"),
        sound_design.Event(tmp_path / "b.wav", 0.0, -26, "sea", length_s=5.0, fade_in_s=0.8,
                           fade_out_s=0.8, bus="bed"),
    ])
    graph = sound_design.mix_graph(plan)
    assert "sidechaincompress" in graph and "loudnorm" in graph
    assert "adelay=1000:all=1" in graph and "atrim=0:5.000" in graph
    command = sound_design.mix_command("ffmpeg", tmp_path / "s.wav", plan, tmp_path / "o.wav", total_s=5)
    assert command.count("-stream_loop") == 1
