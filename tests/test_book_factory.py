from app.services import audiobook
from app.services import book_package as pack
from app.services import book_text as bt

PROSE = ("It was a bright cold day in April, and the clocks were striking. " * 6).strip()

GUTENBERG = f"""Title: The Test Book
Author: Jane Writer

*** START OF THE PROJECT GUTENBERG EBOOK THE TEST BOOK ***

Produced by Someone

CONTENTS

I. THE FIRST CASE

II. THE SECOND CASE

I. THE FIRST CASE

I.

{PROSE} [Illustration: a dog] She said _no_ to Mr. Holmes.

II.

{PROSE}

II. THE SECOND CASE

{PROSE}

NOTES

1. A note that is not read.

*** END OF THE PROJECT GUTENBERG EBOOK THE TEST BOOK ***
"""


def test_gutenberg_chapters_skip_contents_markers_and_notes():
    book = bt.parse_gutenberg(GUTENBERG)
    headings = [line for line in book.text.splitlines() if line.startswith("# ")]
    assert headings == ["# One. The First Case", "# Two. The Second Case"]
    assert "Produced by" not in book.text
    assert "Illustration" not in book.text
    assert "_no_" not in book.text and "said no to" in book.text
    assert "A note that is not read" not in book.text
    assert book.title == "The Test Book" and book.author == "Jane Writer"


def test_spoken_heading_and_words():
    assert bt.spoken_heading(["CHAPTER IV.", "THE RED-HEADED LEAGUE"]) == "Chapter Four. The Red-Headed League"
    assert bt.spoken_heading(["THE FIRST BOOK"]) == "The First Book"
    assert bt.number_words(1891) == "one thousand eight hundred and ninety-one"
    assert bt.roman_value("DID") is None
    assert "Mister Holmes" in bt.speakable("Mr. Holmes -- the detective.")


def test_chapter_titles_line_up_with_recorded_chapters():
    long_chapter = " ".join([PROSE] * 40)
    text = f"# Intro\n\nHello there, listener.\n\n# One\n\n{long_chapter}\n\n# Two\n\n{PROSE}"
    titles = audiobook.chapter_titles(text)
    chapters = audiobook.chapter_sentences(text)
    assert len(titles) == len(chapters)
    assert titles[0] == "Intro" and titles[1] == "One"
    assert "" in titles  # the long chapter was split into continuation chunks
    assert titles[-1] == "Two"


def test_youtube_chapters_fold_a_short_opening_into_chapter_one():
    chapters = [pack.Chapter("Introduction", 0, 7), pack.Chapter("One", 7, 300),
                pack.Chapter("Two", 307, 200), pack.Chapter("Closing", 507, 5)]
    merged = pack.youtube_chapters(chapters)
    assert [c.title for c in merged] == ["One", "Two"]
    assert merged[0].start_s == 0 and merged[0].duration_s == 307
    assert merged[-1].duration_s == 205
    assert pack.stamp(3725, True) == "1:02:05"
    assert pack.duration_label(60) == "1 minute"


def test_group_sentences_respects_limit():
    groups = audiobook.group_sentences(["a" * 50] * 10, 120)
    assert all(len(g) <= 120 for g in groups)
    assert sum(len(g.replace(" ", "")) for g in groups) == 500
