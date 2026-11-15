"""The licence gate and footage shots for library media."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import publishing
from app.services import stock_media
from app.services.stock_media import Candidate, classify_licence


@pytest.mark.parametrize(
    "name",
    ["Public domain", "CC0", "CC BY 4.0", "CC BY 2.0", "CC-BY-3.0", "PD-USGov-NASA", "Pixabay Content License"],
)
def test_licences_a_monetised_video_may_use_are_accepted(name: str) -> None:
    assert classify_licence(name).allowed is True


@pytest.mark.parametrize(
    "name",
    ["CC BY-SA 4.0", "CC BY-NC 2.0", "CC BY-ND 3.0", "CC BY-NC-SA 4.0", "GFDL", "", "All rights reserved", "Fair use"],
)
def test_licences_that_would_bind_or_forbid_the_video_are_refused(name: str) -> None:
    assert classify_licence(name).allowed is False


def test_a_trademarked_file_is_refused_even_when_public_domain() -> None:
    verdict = classify_licence("Public domain", restrictions="trademarked")
    assert verdict.allowed is False


def test_cc_by_is_marked_as_needing_a_credit() -> None:
    assert classify_licence("CC BY 4.0").needs_credit is True
    assert classify_licence("Public domain").needs_credit is False


def _candidate(**overrides) -> Candidate:
    values = dict(
        source="Wikimedia Commons", kind="video", title="File:Apollo 11 launch.webm",
        page_url="https://commons.wikimedia.org/wiki/File:Apollo_11_launch.webm",
        media_url="https://upload.wikimedia.org/x.webm", width=854, height=480,
        duration_s=60.0, licence="Public domain", needs_credit=False, artist="NASA",
    )
    values.update(overrides)
    return Candidate(**values)


def test_a_short_matching_clip_outranks_a_long_film_and_junk() -> None:
    query = "Apollo 11 launch"
    short = _candidate()
    film = _candidate(title="File:Apollo 11 launch film.webm", duration_s=2800.0)
    junk = _candidate(title="File:Apollo 11 launch logo animation.webm")
    ranked = sorted(
        [film, junk, short], key=lambda c: stock_media._score(c, query), reverse=True
    )
    assert ranked[0] is short
    assert ranked[-1] is junk


def test_footage_must_be_a_short_clip_about_the_shot() -> None:
    query = "Apollo 11 launch"
    assert stock_media.usable(_candidate(), query) is True
    assert stock_media.usable(_candidate(duration_s=2800.0), query) is False
    assert stock_media.usable(_candidate(title="File:Apollo 11 Introduction launch.webm"), query) is False
    assert stock_media.usable(_candidate(title="File:Gemini 4 spacewalk.webm"), query) is False


def test_a_modern_ceremony_is_not_a_picture_of_the_event() -> None:
    query = "Eisenhower D-Day"
    original = _candidate(title="File:Eisenhower speaks with paratroopers before D-Day.jpg", kind="image", duration_s=None)
    ceremony = _candidate(title="File:80th anniversary of D-Day Eisenhower statue ceremony 2024.jpg", kind="image", duration_s=None)
    assert stock_media.usable(original, query) is True
    assert stock_media.usable(ceremony, query) is False


def test_a_long_film_can_be_used_only_as_a_named_window() -> None:
    film = _candidate(title="File:Apollo 11 launch film.webm", duration_s=1713.0)
    assert stock_media.usable(film, "Apollo 11 launch") is False
    assert stock_media.timed_excerpt_ok(film, 800.0, 12.0) is True
    assert stock_media.timed_excerpt_ok(film, 800.0, 90.0) is False
    assert stock_media.timed_excerpt_ok(film, 1800.0, 10.0) is False
    documentary = _candidate(
        title="File:Apollo 11 launch documentary.webm", duration_s=1713.0
    )
    assert stock_media.timed_excerpt_ok(documentary, 10.0, 10.0) is False


def test_the_excerpt_starts_inside_the_file() -> None:
    assert stock_media.excerpt_start(10.0, 24.0) == 0.0
    start = stock_media.excerpt_start(60.0, 24.0)
    assert 0 < start <= 36.0


def test_a_footage_shot_is_exactly_the_shot_length_and_silent() -> None:
    command = stock_media.footage_command(
        "ffmpeg", Path("in.mp4"), Path("out.mp4"),
        width=1920, height=1080, fps=24, duration_s=6.5, source_s=24.0,
    )
    assert command[command.index("-frames:v") + 1] == "156"
    assert "-an" in command
    assert "setpts=1.0000*PTS" in command[command.index("-filter_complex") + 1]


def test_a_short_source_is_slowed_no_more_than_two_thirds_speed() -> None:
    command = stock_media.footage_command(
        "ffmpeg", Path("in.mp4"), Path("out.mp4"),
        width=1920, height=1080, fps=24, duration_s=10.0, source_s=4.0,
    )
    assert "setpts=1.5000*PTS" in command[command.index("-filter_complex") + 1]


def test_the_credit_sidecar_round_trips(tmp_path: Path) -> None:
    still = tmp_path / "shot.jpg"
    still.write_bytes(b"x")
    stock_media.write_credit(still, _candidate(licence="CC BY 4.0", needs_credit=True))
    data = stock_media.read_credit(still)
    assert data is not None
    assert "CC BY 4.0" in data["credit_line"]
    assert "commons.wikimedia.org" in data["credit_line"]


def test_real_footage_is_disclosed_as_footage_not_animation() -> None:
    text = publishing.disclosure(0.0, 12.0, "Cori", footage_s=40.0)
    assert "40 seconds are real footage" in text
    assert "No part of the picture was animated" in text


def test_credits_survive_a_trimmed_description() -> None:
    body = publishing.description(
        title="T", logline="x" * 6000, synopsis="", chapters=[],
        disclosure_text="Disclosure.", credits=["\"A\" by B, CC BY 4.0, via Wikimedia Commons - url"],
    )
    assert len(body) <= publishing.DESCRIPTION_LIMIT
    assert body.endswith("CC BY 4.0, via Wikimedia Commons - url")
