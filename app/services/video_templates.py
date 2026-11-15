"""The looks a book video can have, one per kind of book.

A template is a :class:`visualizer.Theme` (colours, fonts, cover shape,
backdrop) plus what the page shows about it: a name, the genres it suits,
the ring it starts with and the font on its thumbnail. "Mindset" is the
original look.

The fonts are open-licensed (SIL OFL or Apache 2.0) and are fetched once
into the fonts folder; a template whose font is missing still draws, with
Inter in its place.

Preview stills are drawn at 540p from a sample page of text and cached
under ``template-previews/``, keyed by the template and the cover picture.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings
from app.services import graphics
from app.services import visualizer
from app.services.visualizer import Theme

log = logging.getLogger(__name__)

PREVIEW_DIR = "template-previews"
PREVIEW_HEIGHT = 540
PREVIEW_TIME_S = 9.0
PREVIEW_VERSION = 1
GOOGLE_FONTS = "https://raw.githubusercontent.com/google/fonts/main/"
#: Font file in the fonts folder -> where it is published.
FONT_SOURCES = {
    "Montserrat.ttf": GOOGLE_FONTS + "ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Nunito.ttf": GOOGLE_FONTS + "ofl/nunito/Nunito%5Bwght%5D.ttf",
    "CormorantGaramond.ttf": GOOGLE_FONTS + "ofl/cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf",
    "Cinzel.ttf": GOOGLE_FONTS + "ofl/cinzel/Cinzel%5Bwght%5D.ttf",
    "PlayfairDisplay.ttf": GOOGLE_FONTS + "ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf",
    "SpecialElite.ttf": GOOGLE_FONTS + "apache/specialelite/SpecialElite-Regular.ttf",
}


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    genres: str
    blurb: str
    theme: Theme
    accent: tuple[int, int, int]
    ring: str
    thumb_font: str = ""

    @property
    def accent_hex(self) -> str:
        return "#{:02x}{:02x}{:02x}".format(*self.accent)

    @property
    def swatch(self) -> list[str]:
        return ["#{:02x}{:02x}{:02x}".format(*c) for c in (self.theme.bg_top, self.theme.bg_bottom, self.accent)]

    def fonts(self) -> set[str]:
        return {self.theme.caption_font, self.theme.heading_font, self.theme.label_font, self.thumb_font} - {""}


def _t(id: str, name: str, genres: str, blurb: str, *, accent, ring: str, thumb_font: str = "", **theme) -> Template:
    return Template(id, name, genres, blurb, Theme(id=id, **theme), accent, ring, thumb_font)


TEMPLATES: list[Template] = [
    _t("boardroom", "Boardroom", "Business, money, leadership",
       "Clean sans type on deep navy, a rounded cover and level bars. Reads like a keynote.",
       accent=(86, 180, 255), ring="bars", thumb_font="Montserrat.ttf",
       bg_top=(20, 32, 56), bg_bottom=(5, 9, 20), background="gradient",
       caption_font="Montserrat.ttf", caption_weight="Medium", heading_font="Montserrat.ttf",
       heading_weight="ExtraBold", label_font="Montserrat.ttf", cover_shape="rounded",
       cover_colors=((86, 180, 255), (40, 70, 160), (12, 20, 48))),
    _t("growth", "Growth", "Self-improvement, habits, productivity",
       "Fresh green on forest dark, friendly rounded type, a soft pulsing ring.",
       accent=(124, 226, 140), ring="balloon", thumb_font="Montserrat.ttf",
       bg_top=(16, 44, 36), bg_bottom=(4, 14, 12), background="blur",
       caption_font="Nunito.ttf", caption_weight="Medium", heading_font="Montserrat.ttf",
       heading_weight="Bold", label_font="Montserrat.ttf",
       cover_colors=((124, 226, 140), (32, 140, 110), (10, 40, 34))),
    _t("mindset", "Mindset", "Psychology, persuasion, strategy",
       "The original look: midnight blue, amber ring, book-serif captions.",
       accent=(255, 179, 71), ring="balloon"),
    _t("calm", "Calm", "Sleep stories, meditation, wellbeing",
       "Lavender on dusk blue with an elegant serif and a slow waveform.",
       accent=(186, 172, 255), ring="wave", thumb_font="CormorantGaramond.ttf",
       bg_top=(42, 46, 78), bg_bottom=(12, 14, 30), background="blur",
       caption_font="CormorantGaramond.ttf", caption_weight="Medium", heading_font="CormorantGaramond.ttf",
       heading_weight="SemiBold", heading_upper=False, muted=(160, 160, 190),
       cover_colors=((186, 172, 255), (90, 96, 170), (24, 28, 60))),
    _t("storybook", "Storybook", "Children's stories, fairy tales, fables",
       "Warm plum and gold, round playful letters, a bouncy rounded cover.",
       accent=(255, 207, 92), ring="balloon", thumb_font="Nunito.ttf",
       bg_top=(70, 40, 92), bg_bottom=(24, 14, 42), background="blur",
       caption_font="Nunito.ttf", caption_weight="SemiBold", heading_font="Nunito.ttf",
       heading_weight="ExtraBold", heading_upper=False, label_font="Nunito.ttf", cover_shape="rounded",
       cover_colors=((255, 150, 180), (255, 207, 92), (110, 70, 160))),
    _t("midnight", "Midnight Tale", "Horror, ghost stories, thrillers",
       "Blood red on black, typewriter captions over a dark smoky backdrop of the cover.",
       accent=(230, 57, 70), ring="wave", thumb_font="Cinzel.ttf",
       bg_top=(30, 8, 12), bg_bottom=(3, 1, 3), background="backdrop",
       caption_font="SpecialElite.ttf", heading_font="Cinzel.ttf", heading_weight="Bold",
       ink=(250, 240, 240), text=(232, 222, 222), brand=(200, 170, 170), muted=(150, 110, 110),
       cover_shape="book", cover_colors=((120, 10, 20), (40, 4, 8), (8, 2, 4))),
    _t("chronicle", "Chronicle", "History, biographies, war",
       "Parchment brown and old gold, carved capitals, a book-shaped cover.",
       accent=(222, 170, 90), ring="bars", thumb_font="Cinzel.ttf",
       bg_top=(56, 42, 26), bg_bottom=(18, 12, 8), background="backdrop",
       caption_font="Lora.ttf", heading_font="Cinzel.ttf", heading_weight="Bold",
       text=(240, 230, 210), brand=(220, 200, 160), muted=(170, 150, 120), cover_shape="book",
       cover_colors=((222, 170, 90), (120, 80, 40), (40, 26, 14))),
    _t("stoic", "Stoic", "Philosophy, classics, wisdom",
       "Graphite and marble, Roman capitals and a quiet silver ring.",
       accent=(214, 210, 200), ring="balloon", thumb_font="Cinzel.ttf",
       bg_top=(38, 38, 40), bg_bottom=(10, 10, 12), background="gradient",
       caption_font="CormorantGaramond.ttf", caption_weight="Medium", heading_font="Cinzel.ttf",
       heading_weight="Bold", muted=(140, 140, 140),
       cover_colors=((214, 210, 200), (110, 110, 112), (30, 30, 32))),
    _t("noir", "Noir", "Crime, detective, mystery",
       "Yellow-tape accent on near black, typewriter text, the cover as a dim backdrop.",
       accent=(245, 197, 24), ring="bars", thumb_font="Oswald.ttf",
       bg_top=(24, 24, 26), bg_bottom=(2, 2, 3), background="backdrop",
       caption_font="SpecialElite.ttf", heading_font="Oswald.ttf", heading_weight="Bold",
       cover_shape="rounded", cover_colors=((245, 197, 24), (60, 60, 60), (10, 10, 10))),
    _t("rose", "Rose", "Romance, relationships, love stories",
       "Deep wine and rose pink with a graceful display serif.",
       accent=(255, 128, 170), ring="balloon", thumb_font="PlayfairDisplay.ttf",
       bg_top=(70, 22, 46), bg_bottom=(24, 6, 16), background="blur",
       caption_font="PlayfairDisplay.ttf", heading_font="PlayfairDisplay.ttf", heading_weight="Bold",
       heading_upper=False, muted=(200, 150, 170),
       cover_colors=((255, 128, 170), (170, 50, 100), (50, 10, 30))),
    _t("dawn", "Dawn", "Spirituality, faith, inspiration",
       "Sunrise peach over warm dusk, a calm serif and a gentle waveform.",
       accent=(255, 190, 120), ring="wave", thumb_font="PlayfairDisplay.ttf",
       bg_top=(74, 46, 32), bg_bottom=(24, 14, 20), background="blur",
       caption_font="Lora.ttf", heading_font="PlayfairDisplay.ttf", heading_weight="Bold",
       heading_upper=False, cover_colors=((255, 210, 150), (230, 120, 90), (70, 30, 50))),
    _t("circuit", "Circuit", "Science, technology, AI, future",
       "Electric cyan on deep space, crisp modern sans, bars that read like a meter.",
       accent=(0, 229, 255), ring="bars", thumb_font="Montserrat.ttf",
       bg_top=(8, 24, 42), bg_bottom=(2, 6, 14), background="gradient",
       caption_font="Inter.ttf", heading_font="Montserrat.ttf", heading_weight="Bold",
       label_font="Montserrat.ttf", cover_shape="rounded",
       cover_colors=((0, 229, 255), (40, 80, 200), (6, 12, 40))),
]
BY_ID = {t.id: t for t in TEMPLATES}
DEFAULT_ID = "mindset"


def get(template_id: str | None) -> Template:
    return BY_ID.get(template_id or DEFAULT_ID, BY_ID[DEFAULT_ID])


def look_for(template: Template, *, style: str | None = None, accent: tuple[int, int, int] | None = None,
             height: int = 1080, fps: int = 24, encoder: str = "x264") -> visualizer.Look:
    return visualizer.Look(style=style or template.ring, accent=accent or template.accent,
                           height=height, fps=fps, encoder=encoder, theme=template.theme)


# --- Fonts ---------------------------------------------------------------------


def missing_fonts() -> list[str]:
    folder = graphics.fonts_dir()
    return [name for name in FONT_SOURCES if not (folder / name).is_file()]


def ensure_fonts(timeout_s: float = 60.0) -> list[str]:
    """Download any template font not yet on disk. Returns the names still missing."""
    folder = graphics.fonts_dir()
    folder.mkdir(parents=True, exist_ok=True)
    still: list[str] = []
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    with httpx.Client(transport=transport, timeout=timeout_s, follow_redirects=True) as client:
        for name in missing_fonts():
            try:
                response = client.get(FONT_SOURCES[name])
                response.raise_for_status()
                if len(response.content) < 10_000:
                    raise ValueError("the file is too small to be a font")
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Font %s could not be fetched: %s", name, exc)
                still.append(name)
                continue
            partial = folder / (name + ".part")
            partial.write_bytes(response.content)
            partial.replace(folder / name)
    return still


# --- Preview stills --------------------------------------------------------------

SAMPLE_TITLE = "The Sample Book"
SAMPLE_AUTHOR = "A. Writer"
SAMPLE_HEADING = "The First Idea"
SAMPLE_TEXT = [
    SAMPLE_HEADING,
    "Every great change begins with one small step that nobody else notices.",
    "You do not rise to the level of your goals; you fall to the level of your systems.",
    "So start where you are, use what you have, and do what you can today.",
]


def _sample_timeline() -> visualizer.Timeline:
    sentences = []
    t = 0.4
    for text in SAMPLE_TEXT:
        words = []
        for word in text.split():
            length = 0.12 + 0.045 * len(word)
            words.append([word, round(t, 3), round(t + length, 3)])
            t += length + 0.05
        sentences.append(words)
        t += 0.5
    return visualizer.build_timeline([(SAMPLE_HEADING, 0.0, {"sentences": sentences})], t + 30)


def preview_path(template: Template, art: Path | None) -> Path:
    art_key = ""
    if art and art.is_file():
        stat = art.stat()
        art_key = f"{art.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    key = hashlib.sha1(f"{template.theme.key()}|{template.accent}|{template.ring}|{art_key}|v{PREVIEW_VERSION}"
                       .encode("utf-8")).hexdigest()[:12]
    return get_settings().data_root / PREVIEW_DIR / f"{template.id}-{key}.jpg"


def preview(template: Template, art: Path | None = None, *, channel: str = "BookInAFlash",
            title: str = SAMPLE_TITLE, author: str = SAMPLE_AUTHOR) -> Path:
    """A still of the template mid-sentence, drawn once and cached."""
    import numpy as np
    from PIL import Image

    destination = preview_path(template, art)
    if destination.is_file() and title == SAMPLE_TITLE:
        return destination
    look = look_for(template, height=PREVIEW_HEIGHT, fps=12)
    tl = _sample_timeline()
    frames = int(tl.duration_s * look.fps) + 2
    amp = (0.55 + 0.35 * np.sin(np.linspace(0, 40, frames))).astype(np.float32)
    bands = np.tile(np.linspace(0.9, 0.25, visualizer.BANDS, dtype=np.float32), (frames, 1))
    bands *= amp[:, None]
    renderer = visualizer.Renderer(tl, look, amp, bands, art=art, title=title, author=author,
                                   channel=channel, badge="Book Summary", levels=8)
    frame = renderer.draw(int(PREVIEW_TIME_S * look.fps))
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part.jpg")
    Image.fromarray(frame).save(partial, quality=86)
    partial.replace(destination)
    return destination
