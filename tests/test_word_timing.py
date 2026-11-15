import json
from pathlib import Path

import pytest

from app.services import audiobook
from app.services import word_timing as wt


def test_spread_stays_inside_the_span_and_pauses_at_punctuation():
    tokens = ["Hello,", "my", "extraordinary", "friend."]
    times = wt.spread(tokens, 1.0, 4.0)
    assert times[0][0] == pytest.approx(1.0)
    assert times[-1][1] == pytest.approx(4.0)
    assert all(b >= a for a, b in times)
    assert all(times[i + 1][0] >= times[i][1] for i in range(3))
    # The comma leaves a gap; "my" and "extraordinary" follow each other directly.
    assert times[1][0] - times[0][1] > 0.1
    assert times[2][0] - times[1][1] == pytest.approx(0.0)
    # Longer words get more time.
    assert (times[2][1] - times[2][0]) > (times[1][1] - times[1][0])


def test_whisper_times_are_kept_and_misheard_words_fill_the_gap():
    sentences = [["The", "Siren", "offers", "a", "fantasy."]]
    pieces = [wt.Piece(0, 1, 0.0, 3.0, 2.8)]
    heard = [("The", 0.10, 0.25), ("siren", 0.30, 0.70), ("offerz", 0.75, 1.10),
             ("a", 1.15, 1.20), ("fantasy", 1.25, 1.90)]
    words, share = wt.align(sentences, pieces, heard)
    times = words[0]
    assert [w for w, _, _ in times] == sentences[0]
    assert times[1][1:] == (0.30, 0.70)
    assert times[4][1:] == (1.25, 1.90)
    # The word Whisper misheard sits between its matched neighbours.
    assert 0.70 <= times[2][1] < times[2][2] <= 1.15
    assert share == pytest.approx(0.8)


def test_a_piece_with_too_few_matches_falls_back_to_the_even_spread():
    sentences = [["Completely", "different", "words", "here."]]
    pieces = [wt.Piece(0, 1, 2.0, 5.0, 4.7)]
    heard = [("nothing", 0.1, 0.2), ("alike", 0.3, 0.4)]
    words, share = wt.align(sentences, pieces, heard)
    assert share == 0.0
    assert words[0][0][1] == pytest.approx(2.0)
    assert words[0][-1][2] == pytest.approx(4.7)


def test_matches_outside_their_sentence_are_not_trusted():
    sentences = [["One", "two."], ["three", "four."]]
    pieces = [wt.Piece(0, 1, 0.0, 1.0, 0.9), wt.Piece(1, 1, 1.0, 2.0, 1.9)]
    # "three four" heard far past the second sentence's measured span.
    heard = [("one", 0.1, 0.4), ("two", 0.5, 0.8), ("three", 7.0, 7.3), ("four", 7.4, 7.8)]
    words, _ = wt.align(sentences, pieces, heard)
    assert words[1][0][1] >= 1.0 and words[1][-1][2] <= 1.9


def test_pieces_fall_back_to_one_span_when_counts_disagree():
    meta = {"pieces": [{"first": 0, "count": 1, "start_s": 0, "end_s": 1}]}
    pieces = wt.pieces_from(meta, 3, 9.0)
    assert len(pieces) == 1 and pieces[0].count == 3 and pieces[0].end_s == 9.0


def test_group_counts_match_group_sentences():
    sentences = ["a" * 50, "b" * 60, "c" * 10, "d" * 200, "e" * 5]
    groups = audiobook.group_sentences(sentences, 120)
    counts = audiobook.group_counts(sentences, 120)
    assert len(groups) == len(counts) and sum(counts) == len(sentences)
    start = 0
    for group, count in zip(groups, counts):
        assert group == " ".join(sentences[start:start + count])
        start += count


def test_chapter_words_prefer_edge_and_are_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "chapters").mkdir()
    sentences = ["Hello there.", "General idea."]
    meta = {"hash": "abc", "duration_s": 3.0, "pieces": [
        {"first": 0, "count": 2, "start_s": 0.0, "end_s": 3.0, "speech_end_s": 2.8,
         "words": [["Hello", 0.1, 0.4], ["there", 0.45, 0.8], ["General", 1.2, 1.6], ["idea", 1.7, 2.1]]},
    ]}
    calls = []
    monkeypatch.setattr(wt, "whisper_words", lambda *a, **k: calls.append(a) or [])
    data = wt.chapter_words(tmp_path, 1, sentences, meta, None)
    assert data["source"] == "edge" and not calls
    assert data["sentences"][1][0] == ["General", 1.2, 1.6] or tuple(data["sentences"][1][0]) == ("General", 1.2, 1.6)
    saved = json.loads(wt.cache_path(tmp_path, 1).read_text(encoding="utf-8"))
    assert saved["hash"] == "abc"
    # A second call reads the cache; a changed recording does not.
    assert wt.chapter_words(tmp_path, 1, sentences, meta, None)["source"] == "edge"
    assert wt.load_cached(tmp_path, 1, "different") is None


def test_chapter_words_use_whisper_on_the_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "chapters").mkdir()
    wav = tmp_path / "chapters" / "001.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(wt, "whisper_words", lambda path, language="en": [("Hi", 0.2, 0.5), ("you", 0.6, 0.9)])
    data = wt.chapter_words(tmp_path, 1, ["Hi you."], {"hash": "h", "duration_s": 1.5}, wav)
    assert data["source"] == "whisper"
    assert [tuple(w) for w in data["sentences"][0]] == [("Hi", 0.2, 0.5), ("you.", 0.6, 0.9)]
