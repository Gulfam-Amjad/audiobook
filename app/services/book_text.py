"""Getting a book's text ready to be read aloud.

Three kinds of book come through here, and they need different things:

* ``public-domain``: a Project Gutenberg plain-text file. It arrives
  wrapped in licence boilerplate, a table of contents, footnotes,
  illustration markers and ``_italic_`` underscores, none of which should
  be spoken, and its chapter headings are in a dozen house styles.
* ``summary`` and ``original``: scripts written for the channel in
  markdown, where ``#`` already marks each chapter. They only need the
  spoken-word fixes and the opening and closing lines.

The result is markdown with one ``# heading`` per chapter, which
:mod:`app.services.audiobook` splits and speaks, and whose headings become
the YouTube chapter timestamps.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

GUTENBERG_URLS = (
    "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt",
    "https://www.gutenberg.org/ebooks/{id}.txt.utf-8",
)
USER_AGENT = "AIVideoManager/0.1 (local audiobook tool)"
HTTP_TIMEOUT_S = 60
FETCH_ATTEMPTS = 3

KINDS = ("public-domain", "summary", "original")
LANGUAGES = ("en", "ur", "hi")

_START = re.compile(r"^\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*$", re.M | re.I)
_END = re.compile(r"^\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*$", re.M | re.I)
_META = re.compile(r"^(Title|Author|Translator|Language|Release date|Editor):\s*(.+)$", re.M | re.I)

_ROMAN = r"[IVXLCDM]+"
_ORDINALS = (
    "FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH|ELEVENTH|TWELFTH|"
    "THIRTEENTH|FOURTEENTH|FIFTEENTH|SIXTEENTH|SEVENTEENTH|EIGHTEENTH|NINETEENTH|TWENTIETH"
)
_NUMBER_WORDS = (
    "ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|ELEVEN|TWELVE|THIRTEEN|FOURTEEN|"
    "FIFTEEN|SIXTEEN|SEVENTEEN|EIGHTEEN|NINETEEN|TWENTY"
)
#: Headings that name themselves: "CHAPTER IV.", "THE FIRST BOOK",
#: "I. A SCANDAL IN BOHEMIA", "PREFACE".
_TITLED = (
    re.compile(
        rf"^(?:CHAPTER|BOOK|PART|LETTER|ADVENTURE|STAVE|CANTO|SECTION|VOLUME)\s+"
        rf"(?:{_ROMAN}|\d+|{_NUMBER_WORDS}|THE\s+(?:{_ORDINALS}))\b[.:]?.*$",
        re.I,
    ),
    re.compile(rf"^THE\s+(?:{_ORDINALS})\s+(?:BOOK|PART|CHAPTER)\.?$", re.I),
    re.compile(rf"^{_ROMAN}\.\s+[^a-z]+$"),
    re.compile(
        r"^(?:PREFACE|INTRODUCTION|PROLOGUE|EPILOGUE|CONCLUSION|AFTERWORD|FOREWORD|"
        r"TRANSLATOR'S PREFACE|AUTHOR'S PREFACE)\.?$",
        re.I,
    ),
)
#: A line of capitals with no number, as in "THOUGHT AND CHARACTER". Only
#: used when a book has too few titled headings.
_CAPITALS = re.compile(r"^[A-Z][A-Z ,.'’:;\-!?&]{2,60}$")
#: A bare "II." - a chapter in some books, a section inside a story in others.
_BARE_ROMAN = re.compile(rf"^{_ROMAN}\.?$")
#: Back matter that is not read: everything from the first of these on.
_BACK_MATTER = re.compile(r"^(?:NOTES|FOOTNOTES|ENDNOTES|APPENDIX|GLOSSARY|INDEX)\b[^a-z]{0,40}$")
#: A heading is a short line. Anything longer is a sentence that happens
#: to begin with "Part" or a Roman numeral.
MAX_HEADING_CHARS = 90
#: A real chapter has prose under it before the next heading. In a table
#: of contents every "heading" is followed straight away by another.
MIN_SECTION_CHARS = 300
MIN_PROSE_CHARS = 180
LOOKAHEAD_PARAGRAPHS = 6
#: Fewer headings than this and the next style is tried. Titled headings
#: name themselves, so two are trusted; the weaker styles need three.
MIN_TITLED = 2
MIN_HEADINGS = 3

_SPOKEN = (
    (re.compile(r"\bMr\.\s"), "Mister "),
    (re.compile(r"\bMrs\.\s"), "Missus "),
    (re.compile(r"\bMessrs\.\s"), "Messieurs "),
    (re.compile(r"\bDr\.\s"), "Doctor "),
    (re.compile(r"\bSt\.\s(?=[A-Z])"), "Saint "),
    (re.compile(r"\bProf\.\s"), "Professor "),
    (re.compile(r"\bCapt\.\s"), "Captain "),
    (re.compile(r"\bCol\.\s"), "Colonel "),
    (re.compile(r"\bGen\.\s"), "General "),
    (re.compile(r"\bLieut\.\s"), "Lieutenant "),
    (re.compile(r"\bNo\.\s(?=\d)"), "Number "),
    (re.compile(r"&c\.?"), "et cetera"),
    (re.compile(r"\bi\.\s?e\.,?", re.I), "that is,"),
    (re.compile(r"\be\.\s?g\.,?", re.I), "for example,"),
    (re.compile(r"\bviz\.,?"), "namely,"),
    (re.compile(r"\s&\s"), " and "),
)

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


class BookTextError(Exception):
    """The text could not be fetched or made readable."""


@dataclass
class Book:
    title: str
    author: str = ""
    translator: str = ""
    language: str = "en"
    kind: str = "public-domain"
    source: str = ""
    #: Markdown with one ``# heading`` per chapter.
    text: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def chapter_count(self) -> int:
        return sum(1 for line in self.text.splitlines() if line.startswith("# "))

    @property
    def words(self) -> int:
        return len(self.text.split())


# --- Fetching ---------------------------------------------------------------


def fetch_gutenberg(book_id: int, cache_dir: Path | None = None) -> str:
    """The book's plain text, from the cache when it was fetched before."""
    cached = cache_dir / f"pg{book_id}.txt" if cache_dir else None
    if cached and cached.is_file() and cached.stat().st_size > 1000:
        return cached.read_bytes().decode("utf-8", "replace")
    errors = []
    # IPv4 only, as in app.providers.http: IPv6 routes from this network hang.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True, transport=transport,
                      headers={"User-Agent": USER_AGENT}) as client:
        for attempt in range(FETCH_ATTEMPTS):
            for template in GUTENBERG_URLS:
                url = template.format(id=book_id)
                try:
                    response = client.get(url)
                except httpx.HTTPError as exc:
                    errors.append(f"{url}: {exc}")
                    continue
                if response.status_code == 200 and len(response.content) > 1000:
                    response.encoding = "utf-8"
                    if cached:
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        cached.write_bytes(response.content)
                    return response.text
                errors.append(f"{url}: HTTP {response.status_code}")
            time.sleep(3 * (attempt + 1))
    raise BookTextError(f"Project Gutenberg book {book_id} could not be downloaded. " + "; ".join(errors[-2:]))


# --- Numbers ----------------------------------------------------------------


def number_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" and {number_words(rest)}" if rest else "")
    thousands, rest = divmod(n, 1000)
    return f"{number_words(thousands)} thousand" + (f" {number_words(rest)}" if rest else "")


_STRICT_ROMAN = re.compile(r"^M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$")
_NUMBERED = {"CHAPTER", "BOOK", "PART", "LETTER", "ADVENTURE", "STAVE", "CANTO", "SECTION", "VOLUME"}


def roman_value(token: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    token = token.upper()
    if not token or not _STRICT_ROMAN.match(token):
        return None
    total = 0
    for index, ch in enumerate(token):
        value = values[ch]
        if index + 1 < len(token) and values[token[index + 1]] > value:
            total -= value
        else:
            total += value
    return total if 0 < total < 4000 else None


# --- Cleaning a Gutenberg file ----------------------------------------------


def parse_gutenberg(raw: str, *, source: str = "") -> Book:
    """Strip the boilerplate and front matter, and mark the chapters."""
    raw = raw.replace("\r\n", "\n").replace("\ufeff", "")
    meta = {key.lower(): value.strip() for key, value in _META.findall(raw[:20_000])}
    start = _START.search(raw)
    end = _END.search(raw)
    body = raw[start.end() if start else 0: end.start() if end else len(raw)]
    book = Book(
        title=meta.get("title", "Untitled").split("\n")[0],
        author=meta.get("author", ""),
        translator=meta.get("translator", ""),
        language="en",
        kind="public-domain",
        source=source,
    )
    if not start:
        book.warnings.append("No Project Gutenberg start marker was found; the whole file was used.")

    body = _strip_markup(body)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    paragraphs = [p for p in paragraphs if not _is_boilerplate(p)]
    book.text = _mark_chapters(paragraphs, book)
    return book


def _strip_markup(body: str) -> str:
    body = re.sub(r"\[(?:Illustration|ILLUSTRATION)[^\]]*\]", "", body, flags=re.S)
    body = re.sub(r"\[(?:Footnote|FOOTNOTE)[^\]]*\]", "", body, flags=re.S)
    body = re.sub(r"\[(?:\d{1,3}|[A-Z])\]", "", body)
    body = re.sub(r"\{\d+\}", "", body)
    body = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", body)
    body = re.sub(r"(?m)^\s*(?:\*\s*){3,}$", "", body)
    body = re.sub(r"(?m)^\s*[-=_]{4,}\s*$", "", body)
    return body


def _is_boilerplate(paragraph: str) -> bool:
    first = paragraph.lstrip().lower()
    return first.startswith((
        "produced by", "transcriber's note", "transcriber’s note", "[transcriber",
        "e-text prepared by", "this ebook was produced", "updated editions will replace",
        "end of the project gutenberg", "end of project gutenberg",
    ))


def _short_lines(paragraph: str) -> list[str] | None:
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if not lines or len(lines) > 3 or any(len(line) > MAX_HEADING_CHARS for line in lines):
        return None
    return lines


def _style(paragraph: str) -> str:
    """Which kind of heading a paragraph looks like: titled, capitals, roman, or ''."""
    lines = _short_lines(paragraph)
    if lines is None:
        return ""
    first = lines[0]
    if any(pattern.match(first) for pattern in _TITLED):
        return "titled"
    if _BARE_ROMAN.match(first):
        return "roman"
    if len(lines) == 1 and _CAPITALS.match(first) and roman_value(first.strip(". ")) is None:
        return "capitals"
    return ""


def _has_prose_after(paragraphs: list[str], index: int, styles: list[str], allowed: set[str]) -> bool:
    total = 0
    for offset in range(1, LOOKAHEAD_PARAGRAPHS + 1):
        position = index + offset
        if position >= len(paragraphs):
            break
        if styles[position] in allowed and offset > 1:
            break
        if len(paragraphs[position]) >= MIN_PROSE_CHARS:
            return True
        total += len(paragraphs[position])
    return total >= MIN_SECTION_CHARS


def _find_headings(paragraphs: list[str], book: Book) -> tuple[dict[int, list[str]], set[int]]:
    """Pick the heading style the book actually uses.

    Returns the accepted headings, and the paragraphs that are section
    markers in a style not used for chapters (dropped rather than read).
    """
    styles = [_style(p) for p in paragraphs]
    for allowed in ({"titled"}, {"titled", "capitals"}, {"titled", "roman"}):
        accepted = {
            index: _short_lines(paragraphs[index]) or []
            for index, style in enumerate(styles)
            if style in allowed and _has_prose_after(paragraphs, index, styles, allowed)
        }
        accepted = _drop_front_matter(accepted, book)
        if len(accepted) >= (MIN_TITLED if allowed == {"titled"} else MIN_HEADINGS):
            markers = {i for i, style in enumerate(styles) if style == "roman" and "roman" not in allowed}
            if "capitals" in allowed:
                book.warnings.append("Chapters were taken from lines in capitals; check the names.")
            return accepted, markers
    return {}, set()


def _drop_front_matter(accepted: dict[int, list[str]], book: Book) -> dict[int, list[str]]:
    """Drop headings that are the title page or a contents list.

    A heading whose words appear again later was listed before the chapter
    itself, so the later one is the chapter. The book's own title and its
    author's name are a title page, not a chapter.
    """
    def key(lines: list[str]) -> str:
        return re.sub(r"[^a-z0-9]", "", " ".join(lines).lower())

    own = {key([book.title]), key([book.author])} - {""}
    last_seen: dict[str, int] = {}
    for index in sorted(accepted):
        last_seen[key(accepted[index])] = index
    return {
        index: lines for index, lines in accepted.items()
        if last_seen[key(lines)] == index and key(lines) not in own
    }


def _mark_chapters(paragraphs: list[str], book: Book) -> str:
    """Turn real headings into ``# Title`` and drop everything before the first."""
    heading_at, markers = _find_headings(paragraphs, book)
    if heading_at:
        first = min(heading_at)
        for index in range(first + 1, len(paragraphs)):
            if _BACK_MATTER.match(paragraphs[index].strip()) and _short_lines(paragraphs[index]):
                paragraphs = paragraphs[:index]
                heading_at = {i: v for i, v in heading_at.items() if i < index}
                book.warnings.append("Back matter (notes, appendix, index) was left out.")
                break

    if not heading_at:
        book.warnings.append(
            "No chapter headings were recognised; the book is split every ten minutes "
            "instead. Add '# ' headings to the text to get named YouTube chapters."
        )
        start = 0
        for index, paragraph in enumerate(paragraphs):
            if len(paragraph) >= MIN_PROSE_CHARS:
                start = index
                break
        return "\n\n".join(_unwrap(p) for p in paragraphs[start:])

    out: list[str] = []
    first = min(heading_at)
    skip_next = False
    for index in range(first, len(paragraphs)):
        if skip_next:
            skip_next = False
            continue
        if index in markers:
            continue
        if index in heading_at:
            lines = heading_at[index]
            following = paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
            if (len(lines) == 1 and following and len(following) < MAX_HEADING_CHARS
                    and index + 1 not in heading_at and index + 1 not in markers
                    and _style(following) in ("", "capitals") and not following.rstrip().endswith((".", "!", "?", '"'))):
                lines = lines + [following.strip()]
                skip_next = True
            out.append("# " + spoken_heading(lines))
        else:
            out.append(_unwrap(paragraphs[index]))
    return "\n\n".join(out)


def _unwrap(paragraph: str) -> str:
    return " ".join(line.strip() for line in paragraph.splitlines() if line.strip())


def spoken_heading(lines: list[str]) -> str:
    """``CHAPTER IV.`` + ``THE RED-HEADED LEAGUE`` -> ``Chapter Four. The Red-Headed League``."""
    parts = []
    for line in lines:
        line = line.strip().rstrip(".:")
        words: list[str] = []
        tokens = line.split()
        for position, token in enumerate(tokens):
            bare = token.strip(".,:;")
            numbered = position == 0 or tokens[position - 1].strip(".,:;").upper() in _NUMBERED
            value = roman_value(bare) if numbered and bare.isupper() else None
            stop = "." if token.endswith((".", ":")) and position < len(tokens) - 1 else ""
            if value is not None:
                words.append(number_words(value).title() + stop)
            elif bare.isdigit():
                words.append(number_words(int(bare)).title() + stop)
            elif bare.isupper() and len(bare) > 1 or line.isupper():
                word = _title_word(token)
                if not words or words[-1].endswith("."):
                    word = word[:1].upper() + word[1:]
                words.append(word)
            else:
                words.append(token)
        parts.append(" ".join(words))
    return ". ".join(part for part in parts if part)


def _title_word(token: str) -> str:
    small = {"a", "an", "the", "of", "and", "in", "on", "to", "for", "at", "by", "with", "or"}
    lower = token.lower()
    return lower if lower in small else "-".join(piece.capitalize() for piece in lower.split("-"))


# --- Spoken-word fixes --------------------------------------------------------


def speakable(text: str, language: str = "en") -> str:
    """Rewrite what a voice would misread. Headings are left as they are."""
    out = []
    for block in text.split("\n\n"):
        if block.startswith("#"):
            out.append(block)
            continue
        out.append(_speakable_paragraph(block, language))
    return "\n\n".join(out)


def _speakable_paragraph(text: str, language: str) -> str:
    text = re.sub(r"\s*(?:--|—|―)\s*", ", ", text)
    text = re.sub(r"[\[\]{}<>|*]", "", text)
    if language == "en":
        for pattern, replacement in _SPOKEN:
            text = pattern.sub(replacement, text)
        text = re.sub(r"\b([A-Z]{4,})\b", lambda m: m.group(1).capitalize()
                      if roman_value(m.group(1)) is None else m.group(1), text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# --- Scripts written for the channel ----------------------------------------


def parse_script(raw: str, *, title: str, author: str, language: str, kind: str) -> Book:
    """A markdown script. A first ``# heading`` matching the title is dropped."""
    text = raw.replace("\r\n", "\n").replace("\ufeff", "").strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("# ") and lines[0][2:].strip().lower() == title.strip().lower():
        text = "\n".join(lines[1:]).strip()
    text = re.sub(r"(?m)^#{2,6}\s+", "# ", text)
    book = Book(title=title, author=author, language=language, kind=kind, text=text)
    if not book.chapter_count:
        book.warnings.append("The script has no '# ' chapter headings, so it will have no YouTube chapters.")
    return book


# --- Opening and closing lines ------------------------------------------------


def opening(book: Book, channel: str) -> tuple[str, str]:
    """The heading and words spoken before the first chapter."""
    by = f", by {book.author}" if book.author else ""
    if book.language == "ur":
        author = f"، از {book.author}" if book.author else ""
        if book.kind == "summary":
            body = f"{book.title}{author}۔ اس کتاب کا مکمل خلاصہ، {channel} کی طرف سے۔"
        elif book.kind == "original":
            body = f"{book.title}۔ {channel} کی ایک نئی کہانی۔"
        else:
            body = f"{book.title}{author}۔ مکمل آڈیو کتاب، {channel} کی طرف سے۔"
        return "تعارف", body + " آرام سے بیٹھیے اور سنیے۔"
    if book.language == "hi":
        author = f", लेखक {book.author}" if book.author else ""
        if book.kind == "summary":
            body = f"{book.title}{author}। इस किताब का पूरा सारांश, {channel} की ओर से।"
        elif book.kind == "original":
            body = f"{book.title}। {channel} की एक नई कहानी।"
        else:
            body = f"{book.title}{author}। पूरी ऑडियोबुक, {channel} की ओर से।"
        return "परिचय", body + " आराम से बैठिए और सुनिए।"
    translated = f" Translated by {book.translator}." if book.translator else ""
    if book.kind == "summary":
        body = (f"{book.title}{by}. A complete summary of the book's key ideas, "
                f"presented by {channel}.")
    elif book.kind == "original":
        body = f"{book.title}. An original story from {channel}."
    else:
        body = f"{book.title}{by}.{translated} The complete, unabridged audiobook, presented by {channel}."
    return "Introduction", body + " Sit back, relax, and enjoy."


def closing(book: Book, channel: str) -> tuple[str, str]:
    by = f", by {book.author}" if book.author else ""
    if book.language == "ur":
        return "اختتام", (f"یہاں {book.title} ختم ہوتی ہے۔ سننے کا شکریہ۔ "
                          f"اگر آپ کو یہ پسند آئی تو {channel} کو سبسکرائب کیجیے، نئی کتاب کے لیے۔")
    if book.language == "hi":
        return "समापन", (f"यहाँ {book.title} समाप्त होती है। सुनने के लिए धन्यवाद। "
                         f"अगर आपको यह पसंद आई तो {channel} को सब्सक्राइब कीजिए, नई किताब के लिए।")
    return "Closing", (f"This is the end of {book.title}{by}. Thank you for listening. "
                       f"If you enjoyed it, subscribe to {channel} for a new audiobook every day.")


def finished_script(book: Book, channel: str) -> str:
    """The whole text to be spoken, opening and closing included."""
    open_title, open_body = opening(book, channel)
    close_title, close_body = closing(book, channel)
    body = speakable(book.text, book.language)
    return f"# {open_title}\n\n{open_body}\n\n{body}\n\n# {close_title}\n\n{close_body}\n"
