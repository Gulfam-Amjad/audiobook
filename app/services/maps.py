"""A small locator map for the place a shot names.

Drawn from Natural Earth (public domain: "You may use the maps in any
manner ... for personal, educational, and commercial purposes"), read
with pyshp (MIT) and painted with Pillow. No tiles are fetched from a
map service, so there are no map-provider terms to meet.

A place is found from the shot's ``map`` field when the writer gave
coordinates, otherwise by matching the place in its lower-third label
against Natural Earth's country and city names. Nothing is guessed: no
match, no map.
"""

from __future__ import annotations

import functools
import math
from pathlib import Path
from typing import Any

from app.config import get_settings

#: Places that matter to documentaries but are not cities or countries.
#: Approximate centres, good to a few kilometres, which is finer than a
#: dot on an inset map can show.
LANDMARKS: dict[str, tuple[float, float]] = {
    "omaha beach": (49.369, -0.873),
    "utah beach": (49.415, -1.175),
    "normandy": (49.183, -0.370),
    "pearl harbor": (21.350, -157.950),
    "kennedy space center": (28.573, -80.649),
    "cape canaveral": (28.392, -80.605),
    "hiroshima": (34.385, 132.455),
    "nagasaki": (32.750, 129.877),
    "stalingrad": (48.708, 44.513),
    "dunkirk": (51.034, 2.377),
    "gallipoli": (40.360, 26.450),
    "verdun": (49.160, 5.384),
    "chernobyl": (51.389, 30.099),
    "pompeii": (40.749, 14.485),
    "waterloo": (50.680, 4.412),
    "gettysburg": (39.831, -77.231),
    "midway": (28.208, -177.376),
    "iwo jima": (24.780, 141.320),
    "the somme": (50.000, 2.700),
    "berlin wall": (52.535, 13.390),
}

OCEAN = (16, 24, 34, 235)
LAND = (52, 58, 64, 255)
BORDER = (92, 100, 110, 255)
HIGHLIGHT = (232, 176, 58, 110)
ACCENT = (232, 176, 58, 255)


def _root() -> Path:
    return get_settings().data_root / "maps"


@functools.lru_cache(maxsize=1)
def _countries() -> list[dict[str, Any]]:
    import shapefile

    path = _root() / "countries" / "ne_50m_admin_0_countries.shp"
    if not path.exists():
        return []
    reader = shapefile.Reader(str(path), encoding="utf-8")
    fields = [f[0] for f in reader.fields[1:]]
    result = []
    for record, shape in zip(reader.records(), reader.shapes()):
        row = dict(zip(fields, record))
        parts = list(shape.parts) + [len(shape.points)]
        rings = [shape.points[parts[i]:parts[i + 1]] for i in range(len(parts) - 1)]
        # France's bounding box includes French Guiana. The map is framed
        # on the largest piece of land instead.
        main = max(rings, key=len) if rings else []
        xs = [p[0] for p in main] or [0.0]
        ys = [p[1] for p in main] or [0.0]
        result.append(
            {
                "names": {str(row.get(k) or "").lower() for k in ("NAME", "NAME_LONG", "ADMIN", "FORMAL_EN") if row.get(k)},
                "label": (float(row.get("LABEL_Y") or 0), float(row.get("LABEL_X") or 0)),
                "bbox": shape.bbox,
                "main_bbox": (min(xs), min(ys), max(xs), max(ys)),
                "rings": rings,
            }
        )
    return result


@functools.lru_cache(maxsize=1)
def _places() -> dict[str, tuple[float, float, int]]:
    import shapefile

    path = _root() / "places" / "ne_10m_populated_places_simple.shp"
    if not path.exists():
        return {}
    reader = shapefile.Reader(str(path), encoding="utf-8")
    fields = [f[0] for f in reader.fields[1:]]
    places: dict[str, tuple[float, float, int]] = {}
    for record in reader.records():
        row = dict(zip(fields, record))
        name = str(row.get("name") or "").lower()
        pop = int(row.get("pop_max") or 0)
        if name and (name not in places or places[name][2] < pop):
            places[name] = (float(row["latitude"]), float(row["longitude"]), pop)
    return places


def locate(direction: dict[str, Any]) -> dict[str, Any] | None:
    """Where the shot's place is, or None when it cannot be found."""
    given = direction.get("map")
    if isinstance(given, dict) and "lat" in given and "lon" in given:
        try:
            return {
                "lat": float(given["lat"]),
                "lon": float(given["lon"]),
                "label": str(given.get("label") or ""),
                "country": None,
            }
        except (TypeError, ValueError):
            return None

    text = str(direction.get("lower_third") or "")
    place = text.partition(",")[0].strip()
    if not place:
        return None
    key = place.lower()
    if key in LANDMARKS:
        lat, lon = LANDMARKS[key]
        return {"lat": lat, "lon": lon, "label": place, "country": None}
    for country in _countries():
        if key in country["names"]:
            lat, lon = country["label"]
            return {"lat": lat, "lon": lon, "label": place, "country": country}
    found = _places().get(key)
    if found:
        return {"lat": found[0], "lon": found[1], "label": place, "country": None}
    return None


def map_png(spot: dict[str, Any], width: int, height: int, destination: Path) -> Path:
    """An inset map centred on the spot, with the place marked."""
    from PIL import Image, ImageDraw

    unit = height / 1080
    w, h = int(560 * unit), int(360 * unit)
    lat, lon = spot["lat"], spot["lon"]
    country = spot.get("country")
    if country:
        x0, y0, x1, y1 = country["main_bbox"]
        span = max(x1 - x0, (y1 - y0) * 1.6, 8.0) * 1.5
    else:
        span = 14.0
    span = min(span, 120.0)
    squash = max(math.cos(math.radians(lat)), 0.2)
    deg_per_px = span / w

    def project(px_lon: float, px_lat: float) -> tuple[float, float]:
        return (
            w / 2 + (px_lon - lon) / deg_per_px,
            h / 2 - (px_lat - lat) / (deg_per_px / squash),
        )

    image = Image.new("RGBA", (w, h), OCEAN)
    draw = ImageDraw.Draw(image, "RGBA")
    half_lon = span / 2 + 5
    half_lat = (h / 2) * deg_per_px / squash + 5
    for row in _countries():
        bx0, by0, bx1, by1 = row["bbox"]
        if bx1 < lon - half_lon or bx0 > lon + half_lon or by1 < lat - half_lat or by0 > lat + half_lat:
            continue
        fill = HIGHLIGHT if country is not None and row is country else LAND
        for ring in row["rings"]:
            if len(ring) < 3:
                continue
            points = [project(x, y) for x, y in ring]
            draw.polygon(points, fill=LAND, outline=BORDER)
            if fill is HIGHLIGHT:
                draw.polygon(points, fill=HIGHLIGHT)

    cx, cy = w / 2, h / 2
    for radius, alpha in ((26, 60), (16, 120)):
        r = radius * unit
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(232, 176, 58, alpha))
    r = 8 * unit
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT, outline=(255, 255, 255, 255), width=max(1, int(3 * unit)))

    from app.services.graphics import BODY_FONT, _font

    label = str(spot.get("label") or "")
    if label:
        size = int(30 * unit)
        font = _font(BODY_FONT, size)
        while draw.textlength(label, font=font) > w - 24 and size > 12:
            size -= 2
            font = _font(BODY_FONT, size)
        text_w = draw.textlength(label, font=font)
        tx, ty = cx + 22 * unit, cy - 18 * unit
        if tx + text_w > w - 12:
            tx = cx - 22 * unit - text_w
        if tx < 12:
            # Neither side has room: under the marker, centred on it.
            tx, ty = cx - text_w / 2, cy + 26 * unit
            if ty + size > h - 12:
                ty = cy - 26 * unit - size
        tx = min(max(tx, 12), w - 12 - text_w)
        ty = min(max(ty, 8), h - size - 12)
        box = draw.textbbox((tx, ty), label, font=font)
        draw.rectangle([box[0] - 8, box[1] - 4, box[2] + 8, box[3] + 4], fill=(0, 0, 0, 170))
        draw.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))

    framed = Image.new("RGBA", (w + 8, h + 8), (0, 0, 0, 0))
    border = ImageDraw.Draw(framed)
    border.rectangle([0, 0, w + 7, h + 7], fill=(255, 255, 255, 220))
    framed.alpha_composite(image, (4, 4))
    framed.save(destination)
    return destination
