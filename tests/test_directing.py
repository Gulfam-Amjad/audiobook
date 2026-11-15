"""The director's notes. They warn. They do not reject a plan."""

from app.services.directing import review_plan


def test_a_short_problem_opening_draws_no_note() -> None:
    notes = review_plan({
        "scenes": [
            {"narration": "He has two hours. Then the lander leaves."},
            {"narration": "The engines lit, and Florida dropped away."},
        ]
    })
    assert notes == []


def test_an_announcement_and_a_long_first_sentence_are_named() -> None:
    notes = review_plan({
        "scenes": [{
            "narration": (
                "Welcome to the story of a rocket that left Florida on a July "
                "morning and flew for four days toward a place no one had stood."
            ),
        }]
    })
    assert any("announces itself" in note for note in notes)
    assert any("opening sentence" in note for note in notes)


def test_too_many_sentences_for_one_picture_are_named() -> None:
    notes = review_plan({
        "scenes": [{
            "narration": "One. Two. Three. Four. Five.",
        }]
    })
    assert any("5 sentences" in note for note in notes)
