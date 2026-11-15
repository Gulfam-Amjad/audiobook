"""Finished files, gathered in one place so nobody has to remember a path.

Two sources: audiobooks, and the newest render of each film project. A
render folder holds hundreds of working files (clips, pieces, cards, the
mix), so only the finished ones are listed: the film, its short, the
thumbnail and the subtitles.

Every file is addressed by its path relative to the data root, and
:func:`resolve` refuses anything outside it, so the page cannot be used to
read arbitrary files off the disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services import audiobook
from app.services.paths import is_within

KINDS: dict[str, str] = {
    ".mp3": "audio",
    ".m4a": "audio",
    ".wav": "audio",
    ".mp4": "video",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".srt": "text",
}
MEDIA_TYPES: dict[str, str] = {".srt": "text/plain; charset=utf-8"}
#: Working files the renderer leaves beside the finished film.
_INTERMEDIATE = re.compile(r"^(clip|piece|card|picture|speech|mix)[-.]", re.IGNORECASE)
_KEEP_NAMES = frozenset({"thumbnail.jpg", "thumbnail-titled.jpg", "subtitles.srt"})
_PROJECT_PREFIX = re.compile(r"^\d+-")


class FilesError(Exception):
    """A requested file is not one this page serves."""


@dataclass
class FileEntry:
    name: str
    rel: str
    kind: str
    size_bytes: int
    modified: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rel": self.rel,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "modified": self.modified.strftime("%Y-%m-%d %H:%M"),
        }


def data_root() -> Path:
    return get_settings().data_root.resolve()


def relative(path: Path) -> str:
    return path.resolve().relative_to(data_root()).as_posix()


def resolve(rel: str) -> Path:
    """Turn a relative path from the page back into a file, or refuse."""
    text = (rel or "").strip().replace("\\", "/")
    if not text or "\x00" in text:
        raise FilesError("No file was named.")
    root = data_root()
    candidate = (root / text).resolve()
    if not is_within(candidate, root):
        raise FilesError("That file is outside the app's data folder.")
    if candidate.suffix.lower() not in KINDS:
        raise FilesError("That kind of file is not served here.")
    if not candidate.is_file():
        raise FilesError("That file does not exist any more.")
    return candidate


def media_type(path: Path) -> str | None:
    return MEDIA_TYPES.get(path.suffix.lower())


def _entry(path: Path) -> FileEntry:
    stat = path.stat()
    return FileEntry(
        name=path.name,
        rel=relative(path),
        kind=KINDS.get(path.suffix.lower(), "other"),
        size_bytes=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def audiobook_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for book in audiobook.list_books():
        folder = audiobook.audiobooks_dir() / book["id"]
        files = [
            _entry(path).as_dict()
            for path in (folder / "audiobook.mp3", folder / "audiobook.mp4")
            if path.is_file()
        ]
        if not files:
            continue
        rows.append(
            {
                "title": book.get("title") or book["id"],
                "detail": " - ".join(
                    part for part in (book.get("voice_label"), book.get("language_label")) if part
                ),
                "duration_s": book.get("duration_s"),
                "folder_rel": relative(folder),
                "folder_abs": str(folder),
                "files": files,
            }
        )
    return rows


def _final_files(render: Path) -> list[Path]:
    keep: list[Path] = []
    for path in sorted(render.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if path.suffix.lower() == ".mp4" and not _INTERMEDIATE.match(name) and ".part" not in name:
            keep.append(path)
        elif name.lower() in _KEEP_NAMES:
            keep.append(path)
    # Films first, then pictures and subtitles.
    keep.sort(key=lambda p: (p.suffix.lower() != ".mp4", p.name.lower()))
    return keep


def project_rows() -> list[dict[str, Any]]:
    """The newest render of each project that has a finished film."""
    output = get_settings().output_dir
    if not output.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for project in sorted(output.iterdir(), key=lambda p: p.name, reverse=True):
        if not project.is_dir():
            continue
        renders = sorted(
            (p for p in project.iterdir() if p.is_dir() and p.name.startswith("render-")),
            key=lambda p: p.name,
            reverse=True,
        )
        for render in renders:
            files = _final_files(render)
            if any(path.suffix.lower() == ".mp4" for path in files):
                rows.append(
                    {
                        "title": _PROJECT_PREFIX.sub("", project.name),
                        "detail": render.name,
                        "folder_rel": relative(render),
                        "folder_abs": str(render),
                        "files": [_entry(path).as_dict() for path in files],
                    }
                )
                break
    return rows


def folder_for(rel: str) -> Path:
    """A folder under the data root, for the Open folder button."""
    text = (rel or "").strip().replace("\\", "/")
    root = data_root()
    candidate = (root / text).resolve() if text else root
    if not is_within(candidate, root):
        raise FilesError("That folder is outside the app's data folder.")
    if candidate.is_file():
        candidate = candidate.parent
    if not candidate.is_dir():
        raise FilesError("That folder does not exist any more.")
    return candidate
