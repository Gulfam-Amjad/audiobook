"""Cards and labels laid over a shot: chapter titles, countdown numbers,
place-and-date labels, the film's title, an end card.

Each graphic is drawn once with Pillow as a transparent PNG, then laid
over its one shot by FFmpeg with an eased entrance and a fade out. Doing
it per shot keeps one decoder and one PNG in memory at a time.

Fonts are SIL Open Font Licence 1.1 (Bebas Neue, Oswald, Inter), kept in
``D:\\AIVideoManager\\fonts`` with each licence file beside it. OFL allows
use in a video, commercial or not; it only forbids selling the font file.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from app.config import get_settings

DISPLAY_FONT = "BebasNeue-Regular.ttf"
HEADING_FONT = "Oswald.ttf"
BODY_FONT = "Inter.ttf"
ACCENT = (232, 176, 58, 255)
WHITE = (255, 255, 255, 255)

#: How long each kind of graphic stays up, in seconds.
COUNTDOWN_S = 2.6
CHAPTER_S = 2.4
CHAPTER_KICKER_S = 3.8
LOWER_THIRD_IN = 0.6
LOWER_THIRD_S = 4.2
TITLE_S = 3.0
END_CARD_S = 3.5
MAP_S = 4.0
#: A card cut shorter than this by the transitions is left out.
MIN_CARD_S = 0.8


def fonts_dir() -> Path:
    return get_settings().data_root / "fonts"


def _font(name: str, size: int):
    from PIL import ImageFont

    path = fonts_dir() / name
    if path.exists():
        font = ImageFont.truetype(str(path), size)
        if name == HEADING_FONT:
            try:
                font.set_variation_by_name("Bold")
            except (OSError, ValueError):
                pass
        return font
    # The bundled Windows font keeps the render working if the OFL fonts
    # have not been downloaded; it is used for nothing but this fallback.
    return ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", size)


def _canvas(width: int, height: int):
    from PIL import Image

    return Image.new("RGBA", (width, height), (0, 0, 0, 0))


def _blur_shadow(image, radius: int = 10):
    """A soft drop shadow under everything drawn on the layer."""
    from PIL import Image, ImageFilter

    alpha = image.split()[-1]
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda a: int(a * 0.75)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
    base = Image.new("RGBA", image.size, (0, 0, 0, 0))
    base.alpha_composite(shadow, (radius // 3, radius // 3))
    base.alpha_composite(image)
    return base


def countdown_png(number: int, label: str, width: int, height: int, destination: Path) -> Path:
    from PIL import ImageDraw

    image = _canvas(width, height)
    draw = ImageDraw.Draw(image)
    unit = height / 1080
    draw.text((width / 2, height * 0.30), "NUMBER", font=_font(HEADING_FONT, int(64 * unit)),
              fill=ACCENT, anchor="mm")
    draw.text((width / 2, height * 0.52), str(number), font=_font(DISPLAY_FONT, int(420 * unit)),
              fill=WHITE, anchor="mm")
    if label:
        draw.text((width / 2, height * 0.76), label.upper(), font=_font(HEADING_FONT, int(72 * unit)),
                  fill=WHITE, anchor="mm")
    _blur_shadow(image).save(destination)
    return destination


def chapter_png(title: str, width: int, height: int, destination: Path, *, kicker: str = "") -> Path:
    from PIL import ImageDraw

    image = _canvas(width, height)
    draw = ImageDraw.Draw(image)
    unit = height / 1080
    size = 170
    font = _font(DISPLAY_FONT, int(size * unit))
    while draw.textlength(title.upper(), font=font) > width * 0.9 and size > 80:
        size -= 10
        font = _font(DISPLAY_FONT, int(size * unit))
    draw.text((width / 2, height * 0.5), title.upper(), font=font, fill=WHITE, anchor="mm")
    box = draw.textbbox((width / 2, height * 0.5), title.upper(), font=font, anchor="mm")
    line_w = min(width * 0.5, (box[2] - box[0]) * 0.6)
    y = box[3] + 30 * unit
    draw.rectangle([width / 2 - line_w / 2, y, width / 2 + line_w / 2, y + 6 * unit], fill=ACCENT)
    if kicker:
        kicker_size = 52
        kicker_font = _font(HEADING_FONT, int(kicker_size * unit))
        while draw.textlength(kicker.upper(), font=kicker_font) > width * 0.9 and kicker_size > 28:
            kicker_size -= 4
            kicker_font = _font(HEADING_FONT, int(kicker_size * unit))
        draw.text((width / 2, box[1] - 50 * unit), kicker.upper(), font=kicker_font,
                  fill=ACCENT, anchor="mm")
    _blur_shadow(image).save(destination)
    return destination


def lower_third_png(text: str, width: int, height: int, destination: Path) -> Path:
    """A place and date label. Split on the first comma into two lines."""
    from PIL import ImageDraw

    unit = height / 1080
    place, _, when = text.partition(",")
    big = _font(HEADING_FONT, int(58 * unit))
    small = _font(BODY_FONT, int(36 * unit))
    probe = ImageDraw.Draw(_canvas(1, 1))
    w1 = probe.textlength(place.strip().upper(), font=big)
    w2 = probe.textlength(when.strip(), font=small) if when else 0
    pad = int(28 * unit)
    bar = int(10 * unit)
    panel_w = int(max(w1, w2) + pad * 2 + bar)
    panel_h = int((58 + (46 if when else 0)) * unit + pad * 1.6)
    image = _canvas(panel_w + 20, panel_h + 20)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, panel_w, panel_h], fill=(10, 10, 12, 185))
    draw.rectangle([0, 0, bar, panel_h], fill=ACCENT)
    draw.text((bar + pad, pad * 0.7), place.strip().upper(), font=big, fill=WHITE, anchor="la")
    if when:
        draw.text((bar + pad, pad * 0.7 + 64 * unit), when.strip(), font=small,
                  fill=(225, 225, 225, 255), anchor="la")
    image.save(destination)
    return destination


def title_png(title: str, width: int, height: int, destination: Path) -> Path:
    from PIL import ImageDraw

    image = _canvas(width, height)
    draw = ImageDraw.Draw(image)
    unit = height / 1080
    size = 190 if len(title) <= 18 else 140 if len(title) <= 28 else 100
    draw.text((width / 2, height * 0.5), title.upper(), font=_font(DISPLAY_FONT, int(size * unit)),
              fill=WHITE, anchor="mm")
    _blur_shadow(image, 14).save(destination)
    return destination


def end_card_png(text: str, width: int, height: int, destination: Path) -> Path:
    from PIL import ImageDraw

    image = _canvas(width, height)
    draw = ImageDraw.Draw(image)
    unit = height / 1080
    draw.text((width / 2, height * 0.5), text, font=_font(HEADING_FONT, int(80 * unit)),
              fill=WHITE, anchor="mm")
    _blur_shadow(image).save(destination)
    return destination


# ---------------------------------------------------------------------------
# Laying them over a shot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Overlay:
    png: Path
    start_s: float
    end_s: float
    #: "pop" scales in from the centre, "slide" enters from the left,
    #: "fade" only fades.
    entrance: str = "fade"
    #: Dim and blur the picture under a full-frame card while it is up.
    dim_background: bool = False
    #: For a slide: the resting position as fractions of the frame.
    x: float = 0.0
    y: float = 0.0
    #: For a pop or a fade: the centre of the graphic, as fractions.
    cx: float = 0.5
    cy: float = 0.5


def overlay_graph(overlays: list[Overlay], *, width: int, height: int) -> str:
    """The filtergraph laying every overlay over input 0, in order."""
    chains: list[str] = []
    current = "[0:v]"
    dims = [o for o in overlays if o.dim_background]
    if dims:
        # A blurred, darkened copy fades in under each card and back out.
        # One copy per card: an alpha fade-out holds at zero afterwards, so
        # a second card on the same copy would never dim.
        labels = "".join(f"[soft{n}]" for n in range(len(dims)))
        chains.append(f"{current}split={len(dims) + 1}[base]{labels}")
        current = "[base]"
        for n, o in enumerate(dims):
            # Dark enough that white type reads over a sunlit archive photo.
            chains.append(
                f"[soft{n}]boxblur=18:2,eq=brightness=-0.32:contrast=0.85:saturation=0.5,"
                f"format=yuva420p,fade=t=in:st={o.start_s:.3f}:d=0.25:alpha=1,"
                f"fade=t=out:st={max(o.start_s, o.end_s - 0.45):.3f}:d=0.45:alpha=1[softened{n}]"
            )
            chains.append(
                f"{current}[softened{n}]overlay=shortest=1:"
                f"enable='between(t,{o.start_s:.3f},{o.end_s:.3f})'[dimmed{n}]"
            )
            current = f"[dimmed{n}]"

    for index, o in enumerate(overlays, start=1):
        fade_out = max(o.start_s, o.end_s - 0.4)
        layer = (
            f"[{index}:v]format=rgba,"
            f"fade=t=in:st={o.start_s:.3f}:d=0.3:alpha=1,"
            f"fade=t=out:st={fade_out:.3f}:d=0.4:alpha=1"
        )
        if o.entrance == "pop":
            grow = f"max(0,1-(t-{o.start_s:.3f})/0.45)"
            layer += f",scale=w='trunc(iw*(1+0.22*pow({grow},2))/2)*2':h=-2:eval=frame"
            x, y = f"W*{o.cx:.4f}-w/2", f"H*{o.cy:.4f}-h/2"
        elif o.entrance == "slide":
            rest_x = o.x * width
            progress = f"min(max((t-{o.start_s:.3f})/0.45,0),1)"
            x = f"{rest_x:.1f}-(w+{rest_x:.1f})*pow(1-{progress},3)"
            y = f"{o.y * height:.1f}"
        else:
            x, y = f"W*{o.cx:.4f}-w/2", f"H*{o.cy:.4f}-h/2"
        label = f"[g{index}]"
        chains.append(layer + label)
        out = f"[v{index}]"
        chains.append(
            f"{current}{label}overlay=x='{x}':y='{y}':eval=frame:shortest=1:"
            f"enable='between(t,{o.start_s:.3f},{o.end_s:.3f})'{out}"
        )
        current = out
    chains.append(f"{current}format=yuv420p[v]")
    return ";".join(chains)


def overlay_command(
    ffmpeg_path: str,
    source: Path,
    destination: Path,
    overlays: list[Overlay],
    *,
    width: int,
    height: int,
) -> list[str]:
    command = [ffmpeg_path, "-hide_banner", "-y", "-i", str(source)]
    for o in overlays:
        command += ["-loop", "1", "-i", str(o.png)]
    command += [
        "-filter_complex", overlay_graph(overlays, width=width, height=height),
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2",
        "-an", str(destination),
    ]
    return command


def plan_overlays(
    direction: dict,
    *,
    clip_s: float,
    index: int,
    last: bool,
    film_title: str,
    end_card: str,
    work: Path,
    width: int,
    height: int,
    overlap_in: float = 0.0,
    overlap_out: float = 0.0,
) -> list[Overlay]:
    """Which graphics this shot carries, drawn to disk, with their times."""
    overlays: list[Overlay] = []
    stem = f"gfx-{index:03d}"
    number = direction.get("countdown_number")
    chapter = str(direction.get("chapter_title") or "")
    lower = str(direction.get("lower_third") or "")

    if direction.get("beat") == "countdown" and number:
        label = lower or ("" if chapter.lower().startswith("number") else chapter)
        png = countdown_png(int(number), label, width, height, work / f"{stem}-count.png")
        overlays.append(Overlay(png, 0.0, min(COUNTDOWN_S, clip_s - 0.1), "pop", True))
        lower = ""
    elif chapter and index > 0:
        # "Kicker|Title" puts a small line above the title and holds the
        # card long enough to read both.
        kicker, _, title = chapter.rpartition("|")
        hold = CHAPTER_KICKER_S if kicker.strip() else CHAPTER_S
        png = chapter_png(title.strip(), width, height, work / f"{stem}-chapter.png",
                          kicker=kicker.strip())
        overlays.append(Overlay(png, 0.0, min(hold, clip_s - 0.1), "pop", True))

    if lower:
        start = (overlays[0].end_s if overlays else 0.0) + LOWER_THIRD_IN
        end = min(start + LOWER_THIRD_S, clip_s - 0.2)
        if end - start > 1.2:
            png = lower_third_png(lower, width, height, work / f"{stem}-lower.png")
            overlays.append(Overlay(png, start, end, "slide", False, x=0.05, y=0.07))

    if direction.get("show_map", True) and (lower or direction.get("map")):
        from app.services import maps

        spot = maps.locate({**direction, "lower_third": lower or direction.get("lower_third")})
        start = (CHAPTER_S if overlays and overlays[0].dim_background else 0.0) + 0.9
        end = min(start + MAP_S, clip_s - 0.3)
        if spot and end - start > 1.5:
            png = maps.map_png(spot, width, height, work / f"{stem}-map.png")
            overlays.append(Overlay(png, start, end, "pop", False, cx=0.80, cy=0.25))

    if index == 0 and film_title and clip_s > TITLE_S + 1.0:
        png = title_png(film_title, width, height, work / f"{stem}-title.png")
        overlays.append(Overlay(png, clip_s - TITLE_S, clip_s - 0.05, "pop", True))

    if last and end_card and clip_s > END_CARD_S + 0.5:
        png = end_card_png(end_card, width, height, work / f"{stem}-end.png")
        overlays.append(Overlay(png, clip_s - END_CARD_S, clip_s - 0.05, "fade", True))

    # Cards stay off the transitions, so two shots' cards never share the
    # screen while one picture hands over to the next.
    kept: list[Overlay] = []
    for overlay in overlays:
        start = max(overlay.start_s, overlap_in)
        end = min(overlay.end_s, clip_s - overlap_out - 0.05)
        if end - start >= MIN_CARD_S:
            kept.append(replace(overlay, start_s=start, end_s=end))
    return kept
