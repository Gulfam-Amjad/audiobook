"""The animated audiobook video: a ring that breathes with the voice, and live captions.

The layout follows ``Audio Summary Video Template.html``. On the left the
cover sits in a circle with a ring around it that swells with the
narrator's loudness and settles in the pauses. On the right the script
scrolls past line by line: the sentence being read is bright, the word
being spoken sits on an accent-coloured box, words already read turn
white, and the sentences around it are dimmed. A chapter label sits
above the captions and a progress bar runs along the bottom.

Hours of 1080p have to be drawn on a two-core laptop, so almost nothing
is drawn per frame:

* The background and everything static are drawn once.
* For the balloon style, the ring with the cover on top is drawn at 48
  loudness levels up front; a frame copies one of them.
* Captions are only redrawn when the spoken word changes or the text is
  scrolling, about a third of the frames.

Frames are piped into FFmpeg as raw RGB and cut into 60-second segment
files. A segment is written to ``.part`` and renamed once FFmpeg has
finished and ffprobe agrees with its length, so a crash, a power cut or
a closed laptop costs at most the segment being drawn. The segments are
joined with the MP3 without re-encoding.

Everything on screen is a function of the time alone (no state carried
between frames), which is what lets any segment be drawn on its own.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import math
import subprocess
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings
from app.services import graphics
from app.services.speech import split_sentences

log = logging.getLogger(__name__)

#: still: the template and live captions with a ring that does not move. It is
#: drawn at a low frame rate, so long books take minutes per hour, not hours.
STYLES = ("balloon", "bars", "wave", "still")
STILL_GLOW = 0.18
STILL_FPS = 12
ENCODERS = ("x264", "qsv")
SEGMENT_S = 60
#: Loudness levels pre-drawn for the balloon ring.
LEVELS = 48
BANDS = 48
#: Samples per video frame for the audio analysis; the sample rate is fps * HOP.
HOP = 640
WINDOW = 1024
#: dBFS mapped to an empty and a full ring. The MP3 is normalised to -16 LUFS.
QUIET_DB, LOUD_DB = -46.0, -14.0
BAND_QUIET_DB, BAND_LOUD_DB = -72.0, -30.0
SCROLL_S = 0.5
FADE_S = 0.4
HOLD_S = 0.08
SUBSCRIBE_S = 12.0
CAPTION_FONT = "Lora.ttf"
DIM_ALPHA = 0.28
UPCOMING_ALPHA = 0.62
INK = (20, 17, 10)
TEXT = (243, 241, 234)
SPEED_FILE = "visualizer-speed.json"
RENDER_VERSION = 3


class VisualizerError(Exception):
    """The animated video could not be made."""


COVER_SHAPES = ("circle", "rounded", "book")
BACKGROUNDS = ("gradient", "blur", "backdrop")


@dataclass(frozen=True)
class Theme:
    """Everything about a video's look except its size: colours, fonts, cover and backdrop.

    The defaults are the original look, kept as the "Mindset" template.
    """

    id: str = "mindset"
    bg_top: tuple[int, int, int] = (29, 37, 64)
    bg_bottom: tuple[int, int, int] = (11, 14, 26)
    #: gradient: colours only. blur: the cover blurred faintly into them.
    #: backdrop: the cover, blurred and darkened, fills the frame.
    background: str = "blur"
    text: tuple[int, int, int] = TEXT
    ink: tuple[int, int, int] = INK
    brand: tuple[int, int, int] = (205, 203, 196)
    muted: tuple[int, int, int] = (150, 152, 165)
    caption_font: str = CAPTION_FONT
    caption_weight: str = "Regular"
    heading_font: str = "Oswald.ttf"
    heading_weight: str = "Bold"
    heading_upper: bool = True
    label_font: str = "Inter.ttf"
    cover_shape: str = "circle"
    #: The picture drawn in the cover when the book has none.
    cover_colors: tuple[tuple[int, int, int], ...] = ((255, 138, 92), (123, 75, 255), (43, 47, 107))

    def key(self) -> str:
        return hashlib.sha1(repr(self).encode("utf-8")).hexdigest()[:10]


@dataclass
class Look:
    style: str = "balloon"
    accent: tuple[int, int, int] = (255, 179, 71)
    height: int = 1080
    fps: int = 24
    encoder: str = "x264"
    theme: Theme = field(default_factory=Theme)

    @property
    def width(self) -> int:
        return int(round(self.height * 16 / 9 / 2)) * 2

    def fingerprint(self) -> str:
        return (f"{self.style}|{self.accent}|{self.height}|{self.fps}|{self.encoder}|"
                f"{self.theme.id}:{self.theme.key()}|v{RENDER_VERSION}")


def parse_accent(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"An accent colour looks like #ffb347, not {value!r}.")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


# --- What is said when ---------------------------------------------------------


@dataclass
class Sentence:
    first: int
    last: int
    chapter: int
    heading: bool
    start_s: float
    end_s: float


@dataclass
class Timeline:
    words: list[str] = field(default_factory=list)
    starts: list[float] = field(default_factory=list)
    ends: list[float] = field(default_factory=list)
    sentence_of: list[int] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)
    chapter_titles: list[str] = field(default_factory=list)
    chapter_starts: list[float] = field(default_factory=list)
    duration_s: float = 0.0

    def digest(self) -> str:
        payload = json.dumps([self.words, self.starts, self.chapter_titles, round(self.duration_s, 2)])
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def build_timeline(chunks: list[tuple[str, float, dict[str, Any]]], duration_s: float) -> Timeline:
    """Join every recorded chapter's word times into one book.

    ``chunks`` is ``(title, start in the book, words.json data)`` per
    recording chunk; an empty title continues the chapter before it.
    """
    tl = Timeline(duration_s=duration_s)
    chapter = -1
    for title, offset, data in chunks:
        if title or chapter < 0:
            chapter += 1
            tl.chapter_titles.append(title or "")
            tl.chapter_starts.append(offset)
        heading_count = len(split_sentences(title)) if title else 0
        for index, words in enumerate(data.get("sentences") or []):
            if not words:
                continue
            first = len(tl.words)
            for text, start, end in words:
                tl.words.append(str(text))
                tl.starts.append(offset + float(start))
                tl.ends.append(offset + float(end))
                tl.sentence_of.append(len(tl.sentences))
            tl.sentences.append(Sentence(
                first=first, last=len(tl.words) - 1, chapter=chapter,
                heading=index < heading_count,
                start_s=tl.starts[first], end_s=tl.ends[-1],
            ))
    for i in range(1, len(tl.starts)):
        if tl.starts[i] < tl.starts[i - 1]:
            tl.starts[i] = tl.starts[i - 1]
        tl.ends[i] = max(tl.ends[i], tl.starts[i])
    return tl


# --- How loud, frame by frame --------------------------------------------------


def _smooth(values: np.ndarray, fps: int, attack: float = 0.55, release: float = 0.14) -> np.ndarray:
    """Rise fast, fall slowly, like a meter. Rates are per frame at 24 fps."""
    scale = 24.0 / fps
    up = 1.0 - (1.0 - attack) ** scale
    down = 1.0 - (1.0 - release) ** scale
    out = np.empty_like(values)
    level = np.zeros(values.shape[1:], dtype=np.float32)
    for i in range(values.shape[0]):
        target = values[i]
        rate = np.where(target > level, up, down) if values.ndim > 1 else (up if target > level else down)
        level = level + (target - level) * rate
        out[i] = level
    return out


def audio_features(ffmpeg_path: str, mp3: Path, fps: int, cache: Path) -> tuple[np.ndarray, np.ndarray]:
    """Loudness (0-1) and 48 frequency bands (0-1) for every video frame, cached."""
    stat = mp3.stat()
    key = f"{stat.st_size}|{int(stat.st_mtime)}|{fps}|{HOP}|v2"
    if cache.is_file():
        try:
            with np.load(cache) as data:
                if str(data["key"]) == key:
                    return data["amp"].astype(np.float32), data["bands"].astype(np.float32)
        except (OSError, KeyError, ValueError):
            pass
    rate = fps * HOP
    process = subprocess.Popen(
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-i", str(mp3),
         "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    freqs = np.fft.rfftfreq(WINDOW, 1.0 / rate)
    edges = np.geomspace(90.0, min(6000.0, rate / 2 - 1), BANDS + 1)
    band_index = np.clip(np.searchsorted(edges, freqs) - 1, -1, BANDS)
    valid = (band_index >= 0) & (band_index < BANDS)
    counts = np.maximum(np.bincount(band_index[valid], minlength=BANDS), 1)
    hann = np.hanning(WINDOW).astype(np.float32)
    tail = np.zeros(WINDOW - HOP, dtype=np.float32)
    amps: list[np.ndarray] = []
    bands: list[np.ndarray] = []
    # Ten-second blocks, and the per-frame results kept as float16: a ten-hour
    # book is close to a million frames, and this laptop has 8 GB.
    block_bytes = HOP * fps * 10 * 2
    assert process.stdout is not None
    while True:
        raw = process.stdout.read(block_bytes)
        if not raw:
            break
        samples = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2").astype(np.float32) / 32768.0
        usable = len(samples) - len(samples) % HOP
        if usable == 0:
            continue
        samples = samples[:usable]
        frames = samples.reshape(-1, HOP)
        rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
        amps.append((20 * np.log10(rms)).astype(np.float32))
        buffer = np.concatenate([tail, samples])
        windows = np.lib.stride_tricks.sliding_window_view(buffer, WINDOW)[::HOP][: frames.shape[0]]
        spectrum = (np.abs(np.fft.rfft(windows * hann, axis=1)) / (WINDOW / 4)).astype(np.float32)
        power = np.zeros((spectrum.shape[0], BANDS), dtype=np.float32)
        for b in range(BANDS):
            columns = valid & (band_index == b)
            if columns.any():
                power[:, b] = spectrum[:, columns].sum(axis=1) / counts[b]
        del spectrum, windows
        bands.append((20 * np.log10(power + 1e-9)).astype(np.float16))
        tail = buffer[-(WINDOW - HOP):].copy()
    process.wait()
    if not amps:
        raise VisualizerError(f"No audio could be read from {mp3.name}.")
    amp_db = np.concatenate(amps).astype(np.float32)
    band = np.concatenate(bands).astype(np.float32)
    del amps, bands
    amp = np.clip((amp_db - QUIET_DB) / (LOUD_DB - QUIET_DB), 0.0, 1.0) ** 0.85
    band -= BAND_QUIET_DB
    band /= BAND_LOUD_DB - BAND_QUIET_DB
    np.clip(band, 0.0, 1.0, out=band)
    amp = _smooth(amp.astype(np.float32), fps)
    band = _smooth(band.astype(np.float32), fps, attack=0.6, release=0.2)
    cache.parent.mkdir(parents=True, exist_ok=True)
    partial = cache.with_name(cache.stem + ".part.npz")
    np.savez(partial, key=np.array(key), amp=amp.astype(np.float16), bands=band.astype(np.float16))
    partial.replace(cache)
    return amp, band


# --- Drawing -------------------------------------------------------------------


def _font(name: str, size: int, weight: str | None = None):
    from PIL import ImageFont

    path = graphics.fonts_dir() / name
    if not path.is_file():
        return graphics._font(graphics.BODY_FONT, size)
    font = ImageFont.truetype(str(path), size)
    if weight:
        try:
            font.set_variation_by_name(weight)
        except (OSError, ValueError):
            pass
    return font


def _ease(p: float) -> float:
    p = min(max(p, 0.0), 1.0)
    return 4 * p * p * p if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


@dataclass
class _Line:
    sentence: int
    top: float
    words: list[tuple[int, float, float]]  # word index, x, width


class Renderer:
    """Draws any frame of the book from its time alone."""

    def __init__(self, tl: Timeline, look: Look, amp: np.ndarray, bands: np.ndarray, *,
                 art: Path | None, title: str, author: str, channel: str, badge: str,
                 levels: int = LEVELS) -> None:
        self.tl, self.look, self.amp, self.bands = tl, look, amp, bands
        self.theme = look.theme
        self.level_count = max(2, levels)
        self.W, self.H = look.width, look.height
        self.u = self.H / 1080.0
        self.accent = look.accent
        self.channel, self.title, self.author, self.badge = channel, title, author, badge
        self.frame_count = max(1, int(math.ceil(tl.duration_s * look.fps)))
        self._starts = tl.starts
        self._layout_geometry()
        self._background(art)
        self._ring_setup(art)
        self._caption_setup()
        self._focus_events()
        self.frame = self.base.copy()
        self._caption_key: Any = None
        self._chapter_shown = -2
        self._sub_alpha = -1.0

    # Geometry and static layers -------------------------------------------------

    def _layout_geometry(self) -> None:
        W, H, u = self.W, self.H, self.u
        self.cx, self.cy = int(W * 0.23), int(H * 0.5)
        self.R = int(205 * u)
        self.ring_half = min(int(W * 0.23) - 6, int(H * 0.5) - int(115 * u))
        self.cap_x0, self.cap_x1 = int(W * 0.47), int(W * 0.95)
        self.view_top, self.view_h = int(275 * u), int(610 * u)
        self.label_y = int(222 * u)
        self.bar_h = max(4, int(10 * u))

    def _background(self, art: Path | None) -> None:
        from PIL import Image, ImageDraw, ImageFilter

        W, H, u = self.W, self.H, self.u
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d = np.sqrt(((xx - 0.2 * W) / (1.2 * W)) ** 2 + ((yy - 0.2 * H) / (1.2 * H)) ** 2)
        t = np.clip(d / 0.7, 0, 1)[..., None]
        theme = self.theme
        c0, c1 = np.array(theme.bg_top, np.float32), np.array(theme.bg_bottom, np.float32)
        grad = c0 * (1 - t) + c1 * t
        if art and art.is_file() and theme.background != "gradient":
            image = Image.open(art).convert("RGB")
            scale = max(W / image.width, H / image.height)
            image = image.resize((int(image.width * scale) + 1, int(image.height * scale) + 1), Image.LANCZOS)
            left, top = (image.width - W) // 2, (image.height - H) // 2
            blur = 40 if theme.background == "blur" else 22
            image = image.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(blur * u))
            pixels = np.asarray(image, np.float32)
            if theme.background == "blur":
                grad = grad * 0.78 + pixels * 0.22 * 0.55
            else:
                grad = grad * 0.45 + pixels * 0.55 * 0.42
        vignette = 1.0 - 0.35 * np.clip(np.sqrt(((xx - W / 2) / W) ** 2 + ((yy - H / 2) / H) ** 2) * 1.6 - 0.3, 0, 1)
        base = Image.fromarray(np.clip(grad * vignette[..., None], 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(base)
        label = _font(theme.label_font, int(30 * u), "SemiBold")
        brand = " ".join(self.channel.upper())
        draw.text((int(W * 0.04), int(H * 0.075)), brand, font=label, fill=theme.brand, anchor="lm")
        small = _font(theme.label_font, int(24 * u), "Medium")
        top_line = f"{self.badge}  ·  {self.title}".upper()
        if self.author:
            top_line += f"  ·  {self.author.upper()}"
        while draw.textlength(top_line, font=small) > self.cap_x1 - self.cap_x0 and len(top_line) > 20:
            top_line = top_line[:-4].rstrip() + "…"
        draw.text((self.cap_x0, int(150 * u)), top_line, font=small, fill=theme.muted, anchor="lm")
        self.base = np.asarray(base, dtype=np.uint8).copy()
        self.bar_bg = self.base[H - self.bar_h:H].copy()

    def _shape_box(self, D: int, s: int) -> tuple[list[float], int]:
        """Where the cover sits inside its D x D square, at ``s`` times size, and its corner radius."""
        shape = self.theme.cover_shape
        if shape == "book":
            w = D * 0.70
            return [(D - w) / 2 * s, 0, (D + w) / 2 * s - 1, D * s - 1], int(10 * s * self.u)
        if shape == "rounded":
            inset = D * 0.06
            return [inset * s, inset * s, (D - inset) * s - 1, (D - inset) * s - 1], int(D * 0.12 * s)
        return [0, 0, D * s - 1, D * s - 1], -1

    def _cover(self, art: Path | None):
        """The art in the template's cover shape, as RGB plus a soft-edged mask, at ring size."""
        from PIL import Image, ImageDraw

        D, s = 2 * self.R, 3
        box, radius = self._shape_box(D, s)
        if art and art.is_file():
            image = Image.open(art).convert("RGB")
            book = self.theme.cover_shape == "book"
            shape_w = int(round((box[2] - box[0] + 1) / s)) if book else D
            ratio = shape_w / D
            if image.width / image.height > ratio:
                crop_w, crop_h = int(image.height * ratio), image.height
            else:
                crop_w, crop_h = image.width, int(image.width / ratio)
            left, top = (image.width - crop_w) // 2, (image.height - crop_h) // 3
            image = image.crop((left, top, left + crop_w, top + crop_h)).resize((shape_w, D), Image.LANCZOS)
            if book:
                canvas = Image.new("RGB", (D, D), (0, 0, 0))
                canvas.paste(image, ((D - shape_w) // 2, 0))
                image = canvas
        else:
            yy, xx = np.mgrid[0:D, 0:D].astype(np.float32) / D
            t = np.clip((xx + yy) / 2, 0, 1)[..., None]
            colours = self.theme.cover_colors
            a, b, c = (np.array(colours[i % len(colours)], np.float32) for i in range(3))
            grad = np.where(t < 0.6, a + (b - a) * (t / 0.6), b + (c - b) * ((t - 0.6) / 0.4))
            image = Image.fromarray(grad.astype(np.uint8))
            draw = ImageDraw.Draw(image)
            w = max(3, int(D * 0.035))
            k = D / 64
            for path in ([(8, 14), (32, 16), (32, 50), (8, 48)], [(56, 14), (32, 16), (32, 50), (56, 48)]):
                pts = [(x * k * 0.7 + D * 0.15, y * k * 0.7 + D * 0.15) for x, y in path]
                draw.line(pts + [pts[0]], fill=(255, 255, 255), width=w, joint="curve")

        def outline(fill: int | None, width: int = 0) -> Image.Image:
            layer = Image.new("L", (D * s, D * s), 0)
            draw = ImageDraw.Draw(layer)
            if radius < 0:
                draw.ellipse(box, fill=fill, outline=255 if width else None, width=width)
            else:
                draw.rounded_rectangle(box, radius=radius, fill=fill, outline=255 if width else None, width=width)
            return layer.resize((D, D), Image.LANCZOS)

        mask = outline(255)
        rim = outline(None, int(4 * s * self.u))
        rgb = np.asarray(image, np.float32)
        rim_a = np.asarray(rim, np.float32)[..., None] / 255 * 0.35
        rgb = rgb * (1 - rim_a) + 255 * rim_a
        return rgb, np.asarray(mask, np.float32)[..., None] / 255

    def _ring_setup(self, art: Path | None) -> None:
        h = self.ring_half
        self.ring_box = (self.cy - h, self.cy + h, self.cx - h, self.cx + h)
        y0, y1, x0, x1 = self.ring_box
        self.ring_bg = self.base[y0:y1, x0:x1].copy()
        cover_rgb, cover_mask = self._cover(art)
        self.cover_rgb, self.cover_mask = cover_rgb, cover_mask
        o = h - self.R
        self.cover_slice = (slice(o, o + 2 * self.R), slice(o, o + 2 * self.R))
        with_cover = self.ring_bg.astype(np.float32)
        sy, sx = self.cover_slice
        with_cover[sy, sx] = with_cover[sy, sx] * (1 - cover_mask) + cover_rgb * cover_mask
        self.ring_plain = np.clip(with_cover, 0, 255).astype(np.uint8)
        self.cover_hard = np.zeros((2 * h, 2 * h), bool)
        self.cover_hard[sy, sx] = cover_mask[..., 0] > 0.5
        self._accent16 = np.array(self.accent, np.uint16)
        self.levels: list[np.ndarray] = []
        if self.look.style == "balloon":
            n = self.level_count
            self.levels = [self._balloon(i / (n - 1)) for i in range(n)]
        elif self.look.style == "still":
            self.levels = [self._balloon(STILL_GLOW)]

    def _balloon(self, a: float) -> np.ndarray:
        """Four soft discs behind the cover, sized and lit by loudness ``a``."""
        from PIL import Image, ImageDraw, ImageFilter

        h, R, s = self.ring_half, self.R, 2
        layer = np.zeros((2 * h, 2 * h), np.float32)
        for i in range(3, -1, -1):
            r = R * (1.06 + i * 0.12) + a * R * (0.22 + i * 0.12)
            r = min(r, h - 4)
            alpha = 0.10 + 0.05 * (3 - i) * (0.4 + a)
            disc = Image.new("L", (2 * h * s, 2 * h * s), 0)
            ImageDraw.Draw(disc).ellipse([(h - r) * s, (h - r) * s, (h + r) * s, (h + r) * s], fill=255)
            disc = disc.resize((2 * h, 2 * h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3 * self.u))
            d = np.asarray(disc, np.float32) / 255 * alpha
            layer = layer + d * (1 - layer)
        glow = layer[..., None]
        out = self.ring_bg.astype(np.float32) * (1 - glow) + np.array(self.accent, np.float32) * glow
        sy, sx = self.cover_slice
        out[sy, sx] = out[sy, sx] * (1 - self.cover_mask) + self.cover_rgb * self.cover_mask
        return np.clip(out, 0, 255).astype(np.uint8)

    def _drawn_ring(self, f: int, t: float, a: float) -> np.ndarray:
        """Bars or a wave ring, drawn for this frame at double size and reduced."""
        from PIL import Image, ImageDraw

        h, R, s = self.ring_half, self.R, 2
        mask = Image.new("L", (2 * h * s, 2 * h * s), 0)
        draw = ImageDraw.Draw(mask)
        c = h * s
        if self.look.style == "bars":
            n = 64
            width = max(2, int(7 * self.u * s))
            band = self.bands[min(f, len(self.bands) - 1)]
            for k in range(n):
                ang = k / n * 2 * math.pi - math.pi / 2
                d = min(k, n - k) / (n / 2)
                v = float(band[int(d * (BANDS - 1) * 0.8)]) * (0.35 + 0.65 * a)
                r1 = R * 1.12 * s
                r2 = r1 + R * (0.05 + v * 0.72) * s
                p1 = (c + math.cos(ang) * r1, c + math.sin(ang) * r1)
                p2 = (c + math.cos(ang) * r2, c + math.sin(ang) * r2)
                draw.line([p1, p2], fill=255, width=width)
                for px, py in (p1, p2):
                    draw.ellipse([px - width / 2, py - width / 2, px + width / 2, py + width / 2], fill=255)
        else:
            n = 180
            for ring, (scale, shade, phase) in enumerate(((1.22, 255, 0.0), (1.34, 120, 1.7))):
                pts = []
                for i in range(n + 1):
                    ag = i / n * 2 * math.pi
                    wob = 0.5 * math.sin(ag * 7 + t * 5 + phase) + 0.5 * math.sin(ag * 13 - t * 3 + phase)
                    rr = (R * scale + a * R * 0.30 * wob) * s
                    pts.append((c + math.cos(ag) * rr, c + math.sin(ag) * rr))
                draw.line(pts, fill=shade, width=max(2, int((5 - ring * 2) * self.u * s)), joint="curve")
        m = np.array(mask.reduce(s), np.uint8)
        m[self.cover_hard] = 0
        out = self.ring_plain.copy()
        flat = out.reshape(-1, 3)
        idx = np.flatnonzero(m)
        weight = m.ravel()[idx, None].astype(np.uint16)
        under = flat[idx].astype(np.uint16)
        flat[idx] = ((under * (255 - weight) + self._accent16 * weight) // 255).astype(np.uint8)
        return out

    # Captions -----------------------------------------------------------------

    def _caption_setup(self) -> None:
        from PIL import Image

        u = self.u
        theme = self.theme
        self.cap_font = _font(theme.caption_font, int(50 * u), theme.caption_weight)
        self.head_font = _font(theme.heading_font, int(64 * u), theme.heading_weight)
        self.label_font = _font(theme.label_font, int(34 * u), "SemiBold")
        self.line_h = int(50 * u * 1.48)
        self.head_line_h = int(64 * u * 1.22)
        self.gap = int(30 * u)
        width = self.cap_x1 - self.cap_x0
        self.cap_w = width
        lines: list[_Line] = []
        self.sent_top: list[float] = []
        self.sent_lines: list[tuple[int, int]] = []
        self.word_line: list[int] = [0] * len(self.tl.words)
        space = self.cap_font.getlength(" ")
        cursor = 0.0
        for si, sentence in enumerate(self.tl.sentences):
            font = self.head_font if sentence.heading else self.cap_font
            line_h = self.head_line_h if sentence.heading else self.line_h
            gap = font.getlength(" ") if sentence.heading else space
            first_line = len(lines)
            self.sent_top.append(cursor)
            x = 0.0
            current = _Line(si, cursor, [])
            for wi in range(sentence.first, sentence.last + 1):
                text = self._shown(wi, sentence)
                w = font.getlength(text)
                if current.words and x + w > width:
                    lines.append(current)
                    cursor += line_h
                    current = _Line(si, cursor, [])
                    x = 0.0
                current.words.append((wi, x, w))
                self.word_line[wi] = len(lines)
                x += w + gap
            lines.append(current)
            cursor += line_h + self.gap
            self.sent_lines.append((first_line, len(lines)))
        self.lines = lines
        self.line_center = [
            ln.top + (self.head_line_h if self.tl.sentences[ln.sentence].heading else self.line_h) / 2
            for ln in lines
        ]
        x0, x1 = self.cap_x0 - int(14 * u), self.cap_x1 + int(14 * u)
        self.cap_box = (self.view_top, self.view_top + self.view_h, x0, x1)
        self.cap_pad = self.cap_x0 - x0
        bg = self.base[self.view_top:self.view_top + self.view_h, x0:x1]
        self.cap_bg = Image.fromarray(bg).convert("RGBA")
        fade = np.ones(self.view_h, np.float32)
        edge = int(self.view_h * 0.2)
        ramp = np.linspace(1.0, 0.0, edge)
        fade[:edge] = ramp
        fade[-edge:] = ramp[::-1]
        cover = ((1.0 - fade) * 255).astype(np.uint8)[:, None].repeat(bg.shape[1], axis=1)
        overlay = np.dstack([bg, cover])
        self.cap_fade = Image.fromarray(overlay.astype(np.uint8), "RGBA")
        self._images: OrderedDict[Any, Any] = OrderedDict()
        lab_y = self.label_y - int(28 * u)
        self.label_box = (lab_y, lab_y + int(56 * u), self.cap_x0, self.cap_x1)

    def _shown(self, wi: int, sentence: Sentence) -> str:
        text = self.tl.words[wi]
        return text.upper() if sentence.heading and self.theme.heading_upper else text

    def _sentence_image(self, si: int, upto: int, now: bool):
        """Sentence ``si`` with words before ``upto`` read; ``-1`` means not yet started."""
        from PIL import Image, ImageDraw

        key = (si, upto, now)
        cached = self._images.get(key)
        if cached is not None:
            self._images.move_to_end(key)
            return cached
        sentence = self.tl.sentences[si]
        heading = sentence.heading
        font = self.head_font if heading else self.cap_font
        line_h = self.head_line_h if heading else self.line_h
        first, last = self.sent_lines[si]
        height = int(self.lines[last - 1].top - self.lines[first].top + line_h)
        image = Image.new("RGBA", (self.cap_w + 2 * self.cap_pad, height + 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        top0 = self.lines[first].top
        text_rgb, ink = self.theme.text, self.theme.ink
        upcoming = (*text_rgb, int(255 * UPCOMING_ALPHA)) if upto >= 0 else (*text_rgb, 255)
        read = tuple(min(255, int(c + (255 - c) * 0.6)) for c in text_rgb)
        for line in self.lines[first:last]:
            y = line.top - top0 + line_h / 2
            for wi, x, w in line.words:
                text = self._shown(wi, sentence)
                px = x + self.cap_pad
                if heading:
                    draw.text((px, y), text, font=font, fill=(*self.accent, 255), anchor="lm")
                elif now and wi == upto:
                    pad = 5 * self.u
                    draw.rounded_rectangle(
                        [px - pad, y - line_h * 0.36, px + w + pad, y + line_h * 0.34],
                        radius=int(7 * self.u), fill=(*self.accent, 255),
                    )
                    draw.text((px, y), text, font=font, fill=(*ink, 255), anchor="lm")
                elif upto >= 0 and wi <= upto:
                    draw.text((px, y), text, font=font, fill=(*read, 255), anchor="lm")
                else:
                    draw.text((px, y), text, font=font, fill=upcoming, anchor="lm")
        self._images[key] = image
        if len(self._images) > 160:
            self._images.popitem(last=False)
        return image

    @staticmethod
    def _faded(image, factor: float):
        if factor >= 0.999:
            return image
        out = image.copy()
        alpha = out.getchannel("A").point(lambda v: int(v * factor))
        out.putalpha(alpha)
        return out

    def _focus_events(self) -> None:
        times, lines = [-1e9], [0]
        for wi, start in enumerate(self.tl.starts):
            line = self.word_line[wi]
            if line != lines[-1]:
                times.append(start)
                lines.append(line)
        self.ev_times, self.ev_lines = times, lines

    def _scroll(self, t: float) -> float:
        k = bisect.bisect_right(self.ev_times, t) - 1
        if not self.lines:
            return 0.0
        target = self.line_center[self.ev_lines[k]]
        if k == 0:
            return target
        previous = self.line_center[self.ev_lines[k - 1]]
        return previous + (target - previous) * _ease((t - self.ev_times[k]) / SCROLL_S)

    def _draw_captions(self, t: float) -> None:
        tl = self.tl
        wi = bisect.bisect_right(self._starts, t) - 1
        if tl.words:
            current_si = tl.sentence_of[wi] if wi >= 0 else 0
        else:
            current_si = -1
        now = wi >= 0 and t < tl.ends[wi] + HOLD_S
        focus = self._scroll(t)
        offset = int(round(focus - self.view_h * 0.40))
        fade_p = 1.0
        if current_si >= 0 and wi >= 0:
            fade_p = min(1.0, (t - tl.sentences[current_si].start_s) / FADE_S)
        fade_bucket = int(fade_p * 10)
        key = (offset, wi, now, current_si, fade_bucket)
        if key == self._caption_key:
            return
        self._caption_key = key
        region = self.cap_bg.copy()
        top_limit, bottom_limit = offset - self.head_line_h * 3, offset + self.view_h
        lo = max(0, bisect.bisect_right(self.sent_top, top_limit) - 1)
        si = lo
        while si < len(tl.sentences) and self.sent_top[si] < bottom_limit:
            if si == current_si and wi >= 0:
                upto = wi if tl.sentence_of[wi] == si else -1
                image = self._faded(self._sentence_image(si, upto, now), DIM_ALPHA + (1 - DIM_ALPHA) * fade_p)
            elif si == current_si:
                image = self._sentence_image(si, -1, False)
            elif si == current_si - 1 and fade_p < 1.0:
                done = self._sentence_image(si, tl.sentences[si].last, False)
                image = self._faded(done, 1.0 - (1 - DIM_ALPHA) * fade_p)
            else:
                image = self._faded(self._sentence_image(si, -1, False), DIM_ALPHA)
            y = int(round(self.sent_top[si] - offset))
            if y < 0:
                if -y < image.height:
                    region.alpha_composite(image, dest=(0, 0), source=(0, -y))
            else:
                region.alpha_composite(image, dest=(0, y))
            si += 1
        region.alpha_composite(self.cap_fade)
        y0, y1, x0, x1 = self.cap_box
        self.frame[y0:y1, x0:x1] = np.asarray(region.convert("RGB"))

    def _draw_label(self, t: float) -> None:
        from PIL import Image, ImageDraw

        chapter = bisect.bisect_right(self.tl.chapter_starts, t) - 1
        if chapter == self._chapter_shown:
            return
        self._chapter_shown = chapter
        y0, y1, x0, x1 = self.label_box
        image = Image.fromarray(self.base[y0:y1, x0:x1].copy())
        title = self.tl.chapter_titles[chapter] if chapter >= 0 else ""
        if title:
            text = " ".join(title.upper())
            draw = ImageDraw.Draw(image)
            font = self.label_font
            while draw.textlength(text, font=font) > (x1 - x0) and font.size > 16:
                font = _font(self.theme.label_font, font.size - 2, "SemiBold")
            draw.text((0, (y1 - y0) / 2), text, font=font, fill=self.accent, anchor="lm")
        self.frame[y0:y1, x0:x1] = np.asarray(image)

    def _subscribe_layer(self):
        from PIL import Image, ImageDraw

        u = self.u
        text = f"SUBSCRIBE TO {self.channel.upper()}"
        font = _font(self.theme.label_font, int(30 * u), "Bold")
        probe = ImageDraw.Draw(Image.new("L", (4, 4)))
        w = int(probe.textlength(text, font=font) + 70 * u)
        h = int(62 * u)
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=(*self.accent, 255))
        draw.text((w / 2, h / 2), text, font=font, fill=(*self.theme.ink, 255), anchor="mm")
        x = self.cx - w // 2
        y = int(self.H * 0.935) - h // 2
        return layer, (y, y + h, x, x + w)

    def _draw_subscribe(self, t: float) -> None:
        remaining = self.tl.duration_s - t
        alpha = 0.0 if remaining > SUBSCRIBE_S else min(1.0, (SUBSCRIBE_S - remaining) / 0.6)
        if abs(alpha - self._sub_alpha) < 0.02:
            return
        self._sub_alpha = alpha
        if not hasattr(self, "_sub"):
            self._sub = self._subscribe_layer()
        layer, (y0, y1, x0, x1) = self._sub
        bg = self.base[y0:y1, x0:x1].astype(np.float32)
        rgba = np.asarray(layer, np.float32)
        a = rgba[..., 3:4] / 255 * alpha
        self.frame[y0:y1, x0:x1] = np.clip(bg * (1 - a) + rgba[..., :3] * a, 0, 255).astype(np.uint8)

    # One frame ----------------------------------------------------------------

    def draw(self, f: int) -> np.ndarray:
        t = f / self.look.fps
        a = float(self.amp[min(f, len(self.amp) - 1)]) if len(self.amp) else 0.0
        breathe = 0.05 + 0.03 * math.sin(t * 2 * math.pi / 3.6)
        a = max(a, breathe)
        y0, y1, x0, x1 = self.ring_box
        if self.levels:
            n = len(self.levels)
            self.frame[y0:y1, x0:x1] = self.levels[min(n - 1, int(round(a * (n - 1))))]
        else:
            self.frame[y0:y1, x0:x1] = self._drawn_ring(f, t, a)
        self._draw_label(t)
        self._draw_captions(t)
        self._draw_subscribe(t)
        filled = int(self.W * min(1.0, t / max(self.tl.duration_s, 1e-6)))
        self.frame[self.H - self.bar_h:self.H] = self.bar_bg
        self.frame[self.H - self.bar_h:self.H, :filled] = self.accent
        return self.frame


# --- Segments, resume and the final file -----------------------------------------


def _ffprobe_duration(ffprobe_path: str, path: Path) -> float:
    process = subprocess.run(
        [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return float((process.stdout or b"").decode().strip() or 0)
    except ValueError:
        return 0.0


def _encoder_args(look: Look) -> list[str]:
    gop = str(look.fps * 4)
    if look.encoder == "qsv":
        return ["-vf", "format=nv12", "-c:v", "h264_qsv", "-preset", "medium",
                "-global_quality", "23", "-g", gop]
    return ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-tune", "animation", "-g", gop]


def _render_segment(renderer: Renderer, ffmpeg_path: str, first: int, last: int, target: Path) -> None:
    look = renderer.look
    partial = target.with_name(target.stem + ".part.mp4")
    partial.unlink(missing_ok=True)
    process = subprocess.Popen(
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{renderer.W}x{renderer.H}",
         "-r", str(look.fps), "-i", "-", *_encoder_args(look), "-an", str(partial)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdin is not None
    try:
        for f in range(first, last):
            process.stdin.write(renderer.draw(f).data)
        process.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    stderr = process.stderr.read() if process.stderr else b""
    process.wait()
    if process.returncode:
        partial.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", "replace").strip()[-400:]
        raise VisualizerError(f"FFmpeg could not encode {target.name}: {detail}")
    target.unlink(missing_ok=True)
    partial.replace(target)


def _speed_file() -> Path:
    return get_settings().data_root / SPEED_FILE


def measured_speed(look: Look) -> float | None:
    """Frames a second last measured for this style and size, if any."""
    try:
        data = json.loads(_speed_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get(f"{look.style}|{look.height}|{look.encoder}")
    return float(value) if value else None


def _save_speed(look: Look, fps_rendered: float) -> None:
    path = _speed_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[f"{look.style}|{look.height}|{look.encoder}"] = round(fps_rendered, 1)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def estimate_seconds(duration_s: float, look: Look) -> float:
    """Wall time to draw and encode ``duration_s`` of video in this look."""
    speed = measured_speed(look) or (20.0 if look.height >= 1080 else 38.0)
    if look.style == "still" and not measured_speed(look):
        speed *= 3.0
    elif look.style != "balloon" and not measured_speed(look):
        speed *= 0.6
    return duration_s * look.fps / speed


def render_video(
    tl: Timeline,
    look: Look,
    mp3: Path,
    destination: Path,
    work: Path,
    ffmpeg_path: str,
    ffprobe_path: str,
    *,
    art: Path | None,
    title: str,
    author: str,
    channel: str,
    badge: str,
    log_line: Callable[[str], None] | None = None,
    progress: Callable[[int, int, float | None], None] | None = None,
) -> dict[str, Any]:
    """Draw the book as 60-second segments, skipping any already made, then join.

    ``progress(segments_done, segments_total, seconds_left)`` is called after each segment.
    """
    say = log_line or (lambda message: None)
    report = progress or (lambda done, count, left: None)
    work.mkdir(parents=True, exist_ok=True)
    segments_dir = work / "segments"
    stamp = f"{look.fingerprint()}|{tl.digest()}|{mp3.stat().st_size}|{title}|{channel}|{art}"
    stamp_file = work / "render.json"
    try:
        previous = json.loads(stamp_file.read_text(encoding="utf-8")).get("stamp")
    except (OSError, json.JSONDecodeError):
        previous = None
    if previous != stamp and segments_dir.exists():
        say("The look or the timing changed since the last attempt; drawing the video afresh.")
        for old in segments_dir.glob("*.mp4"):
            old.unlink(missing_ok=True)
    segments_dir.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(json.dumps({"stamp": stamp}), encoding="utf-8")

    say("Measuring the narration's loudness for the ring.")
    amp, bands = audio_features(ffmpeg_path, mp3, look.fps, work / f"features-{look.fps}.npz")
    renderer = Renderer(tl, look, amp, bands, art=art, title=title, author=author,
                        channel=channel, badge=badge)
    per = SEGMENT_S * look.fps
    total = renderer.frame_count
    count = int(math.ceil(total / per))
    names: list[Path] = []
    drawn_frames, drawn_time = 0, 0.0
    # A segment drawn while the laptop was swapping can take ten times longer;
    # the time left follows the last few segments so one bad one does not
    # inflate the estimate for hours.
    recent: list[tuple[int, float]] = []
    for k in range(count):
        target = segments_dir / f"seg_{k:05d}.mp4"
        names.append(target)
        first, last = k * per, min(total, (k + 1) * per)
        expected = (last - first) / look.fps
        if target.is_file() and abs(_ffprobe_duration(ffprobe_path, target) - expected) < 0.6:
            report(k + 1, count, None)
            continue
        started = time.monotonic()
        _render_segment(renderer, ffmpeg_path, first, last, target)
        length = _ffprobe_duration(ffprobe_path, target)
        if abs(length - expected) >= 0.6:
            target.unlink(missing_ok=True)
            raise VisualizerError(f"{target.name} came out {length:.1f}s long, not {expected:.1f}s.")
        spent = time.monotonic() - started
        drawn_frames += last - first
        drawn_time += spent
        recent = [*recent[-4:], (last - first, spent)]
        recent_time = sum(t for _, t in recent)
        speed = sum(f for f, _ in recent) / recent_time if recent_time else 0.0
        left = (total - last) / speed if speed else 0
        say(f"Video segment {k + 1} of {count} drawn at {speed:.0f} fps; about {left / 60:.0f} min left.")
        if len(recent) >= 3:
            _save_speed(look, speed)
        report(k + 1, count, left)
    if drawn_time > 5:
        _save_speed(look, drawn_frames / drawn_time)

    listing = work / "segments.txt"
    listing.write_text(
        "\n".join("file '{}'".format(p.resolve().as_posix().replace("'", r"'\''")) for p in names) + "\n",
        encoding="utf-8",
    )
    partial = destination.with_name(destination.stem + ".part.mp4")
    partial.unlink(missing_ok=True)
    say("Joining the video segments with the narration.")
    process = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(mp3),
         "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest", "-movflags", "+faststart",
         str(partial)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode:
        partial.unlink(missing_ok=True)
        detail = (process.stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise VisualizerError(f"The segments could not be joined: {detail}")
    length = _ffprobe_duration(ffprobe_path, partial)
    if abs(length - tl.duration_s) > 2.0:
        partial.unlink(missing_ok=True)
        raise VisualizerError(f"The joined video is {length:.1f}s long, but the narration is {tl.duration_s:.1f}s.")
    destination.unlink(missing_ok=True)
    partial.replace(destination)
    return {
        "frames": total, "segments": count, "duration_s": round(length, 1),
        "render_fps": round(drawn_frames / drawn_time, 1) if drawn_time else None,
        "style": look.style, "height": look.height, "fps": look.fps, "encoder": look.encoder,
    }
