"""Fetch real, reusable images and clips for the Mighty Chapters shorts.

Sources: NASA Image and Video Library (public domain) and Wikimedia Commons
(through the Wikipedia search API). Only licences that allow reuse with
attribution are kept: public domain, CC0, CC BY, CC BY-SA. Anything with
NC, ND, or fair-use tags is skipped.

    python scripts/fetch_shorts_assets.py

Writes into D:\\AIVideoManager\\films\\shorts-v2\\assets\\<story>\\ and a
CREDITS.csv beside each story's files.
"""

from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\AIVideoManager\films\shorts-v2\assets")
UA = "MightyChaptersShorts/2.0 (local video project; contact: channel owner)"
ALLOWED = ("public domain", "cc0", "cc by", "cc-by")
BLOCKED = ("nc", "nd", "fair use", "non-commercial", "no derivatives")

STORIES = {
    "apollo13": {
        "nasa_images": [
            "Apollo 13 mailbox carbon dioxide filter",
            "Apollo 13 damaged service module",
            "Apollo 13 splashdown recovery",
            "Apollo 13 Odyssey command module",
            "Apollo 13 lunar module Aquarius",
            "Apollo 13 Swigert Haise Lovell",
        ],
        "nasa_videos": ["Apollo 13 mission", "Apollo 13 Lovell"],
        "wiki": ["Apollo 13 command module", "Apollo 13 oxygen tank explosion"],
    },
    "parmitano": {
        "nasa_images": [
            "Parmitano spacewalk helmet water",
            "Luca Parmitano EVA 23",
            "Parmitano airlock spacewalk",
            "Parmitano International Space Station",
        ],
        "nasa_videos": ["Parmitano spacewalk", "EVA 23 spacewalk water"],
        "wiki": ["Luca Parmitano"],
    },
    "gemini4": {
        "nasa_images": [
            "Ed White Gemini 4 spacewalk",
            "Gemini 4 extravehicular activity",
            "Edward White Gemini 4",
        ],
        "nasa_videos": ["Gemini 4 spacewalk", "Ed White spacewalk"],
        "wiki": ["Gemini 4", "Ed White astronaut"],
    },
    "semmelweis": {
        "nasa_images": [],
        "nasa_videos": [],
        "wiki": [
            "Ignaz Semmelweis portrait",
            "Ignaz Semmelweis",
            "Vienna General Hospital",
            "Semmelweis hand washing",
        ],
    },
    "gage": {
        "nasa_images": [],
        "nasa_videos": [],
        "wiki": [
            "Phineas Gage Daguerreotype",
            "Phineas Gage skull",
            "Phineas Gage Cased Daguerreotype",
            "Phineas Gage railroad",
        ],
    },
}


TIMEOUT_S = 25


def get_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read())


def download(url: str, destination: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        data = response.read()
    destination.write_bytes(data)
    return len(data)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:90]


def licence_ok(text: str) -> bool:
    lowered = (text or "").lower()
    if any(word in lowered for word in BLOCKED):
        return False
    return any(word in lowered for word in ALLOWED)


def nasa_images(query: str, limit: int) -> list[dict]:
    url = "https://images-api.nasa.gov/search?" + urllib.parse.urlencode(
        {"q": query, "media_type": "image"}
    )
    found = []
    for item in get_json(url)["collection"]["items"]:
        data = item["data"][0]
        nid = data.get("nasa_id")
        if not nid:
            continue
        found.append({"id": nid, "title": data.get("title", ""), "kind": "image"})
        if len(found) >= limit:
            break
    return found


def nasa_video_file(nid: str) -> str | None:
    urls = get_json(f"https://images-assets.nasa.gov/video/{nid}/collection.json")
    urls = [u for u in urls if isinstance(u, str)]
    for suffix in ("~medium.mp4", "~small.mp4", "~mobile.mp4", "~orig.mp4"):
        for u in urls:
            if u.endswith(suffix):
                return u
    return None


def nasa_image_file(nid: str) -> str:
    return f"https://images-assets.nasa.gov/image/{nid}/{nid}~large.jpg"


def wiki_images(query: str, limit: int) -> list[dict]:
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "6",
            "gsrlimit": "12",
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": "1400",
            "format": "json",
        }
    )
    pages = (get_json(url).get("query") or {}).get("pages", {})
    found = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        mime = info.get("mime") or ""
        width = info.get("width") or 0
        if not mime.startswith("image/") or width < 500:
            continue
        meta = info.get("extmetadata") or {}
        licence = (meta.get("LicenseShortName") or {}).get("value", "")
        if not licence_ok(licence):
            continue
        artist = (meta.get("Artist") or {}).get("value", "")
        artist = re.sub(r"<[^>]+>", "", artist).strip()
        found.append(
            {
                "id": page["title"].replace("File:", ""),
                "title": page["title"],
                "url": info.get("thumburl") or info["url"].split("?")[0],
                "licence": licence,
                "artist": artist[:120],
                "width": width,
                "height": info.get("height"),
                "kind": "image",
                "source": "wikimedia",
            }
        )
        if len(found) >= limit:
            break
    return found


def main() -> None:
    manifest: dict[str, list[dict]] = {}
    for story, plan in STORIES.items():
        folder = ROOT / story
        folder.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []
        print(f"\n== {story}", flush=True)

        for query in plan["nasa_images"]:
            for item in nasa_images(query, 3):
                try:
                    url = nasa_image_file(item["id"])
                    path = folder / f"nasa-{safe_name(item['id'])}.jpg"
                    if not path.exists():
                        download(url, path)
                    rows.append({**item, "file": path.name, "licence": "Public domain (NASA)", "url": url, "source": "nasa", "artist": "NASA"})
                    print("  img", path.name, flush=True)
                except Exception as exc:  # noqa: BLE001
                    print("  skip", item["id"], type(exc).__name__, flush=True)

        for query in plan["nasa_videos"]:
            url = "https://images-api.nasa.gov/search?" + urllib.parse.urlencode(
                {"q": query, "media_type": "video"}
            )
            try:
                items = get_json(url)["collection"]["items"][:2]
            except Exception as exc:  # noqa: BLE001
                print("  video search failed", query, type(exc).__name__, flush=True)
                continue
            for entry in items:
                nid = entry["data"][0].get("nasa_id")
                try:
                    file_url = nasa_video_file(nid)
                    if not file_url:
                        continue
                    path = folder / f"clip-{safe_name(nid)}.mp4"
                    if not path.exists():
                        download(file_url, path)
                    rows.append({"id": nid, "title": entry["data"][0].get("title", ""), "file": path.name, "licence": "Public domain (NASA)", "url": file_url, "source": "nasa", "kind": "video", "artist": "NASA"})
                    print("  clip", path.name, flush=True)
                except Exception as exc:  # noqa: BLE001
                    print("  skip clip", nid, type(exc).__name__, flush=True)

        for query in plan["wiki"]:
            try:
                wiki_hits = wiki_images(query, 4)
            except Exception as exc:  # noqa: BLE001
                print("  wiki search failed", query, type(exc).__name__, flush=True)
                continue
            for item in wiki_hits:
                try:
                    path = folder / f"wiki-{safe_name(item['id'])}"
                    if not path.exists():
                        download(item["url"], path)
                    item["file"] = path.name
                    rows.append(item)
                    print("  wiki", path.name, item["width"], item["licence"], flush=True)
                except Exception as exc:  # noqa: BLE001
                    print("  skip wiki", item["id"], type(exc).__name__, flush=True)
                time.sleep(0.2)

        seen = set()
        unique = []
        for row in rows:
            key = row["file"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        manifest[story] = unique

        with (folder / "CREDITS.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["file", "kind", "licence", "artist", "source", "url"])
            for row in unique:
                writer.writerow([row["file"], row["kind"], row["licence"], row.get("artist", ""), row["source"], row.get("url", "")])

    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nDONE", {k: len(v) for k, v in manifest.items()}, flush=True)


if __name__ == "__main__":
    main()
