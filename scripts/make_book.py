"""One command from a book to a folder ready for YouTube.

    python scripts/make_book.py --gutenberg 1661 --voice kokoro:bm_george --art-query "Sidney Paget Holmes"
    python scripts/make_book.py --text summary.md --title "Atomic Habits" --author "James Clear" --kind summary
    python scripts/make_book.py --text summary.md --title "..." --template boardroom
    python scripts/make_book.py --text kahani.md --title "..." --lang ur --voice edge:ur-PK-AsadNeural
    python scripts/make_book.py --gutenberg 2680 --plan-only

The work is done by ``app.services.book_video`` (the Book Video page queues
this same command). Steps: fetch and clean the text (or read a script), add
the opening and closing lines, speak it chapter by chapter, join the MP3,
time every word, then draw the animated video in a template (a ring that
pulses with the voice and live word-by-word captions), the thumbnail, the
title, description with chapter timestamps, tags, and a check report.
``--video cards`` makes the old one-still-per-chapter video instead.
``--plan-only`` stops after the text so the chapter list can be read before
hours are spent speaking it. ``--preview`` speaks only the first few
sentences into "<name> - preview", to see the look in a minute.

Every stage keeps what it finished: chapters, word timings and 60-second
video segments. Run the same command again, or ``--resume <folder>``, and
it carries on from where it stopped.

Voices: ``piper:<voice key>``, ``kokoro:<voice>`` (am_michael, bm_george,
af_heart, hm_omega ...), ``edge:<voice>`` (en-US-AndrewMultilingualNeural,
ur-PK-AsadNeural, hi-IN-MadhurNeural ...).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services import book_text  # noqa: E402
from app.services import book_video  # noqa: E402
from app.services import video_templates  # noqa: E402
from app.services import visualizer  # noqa: E402

RUN_FILE = book_video.RUN_FILE


def _absolute_paths(argv: list[str]) -> list[str]:
    """Saved commands must still find their files when run from another folder."""
    out = list(argv)
    for i, item in enumerate(out[:-1]):
        if item in ("--text", "--art", "--about-file", "--out"):
            out[i + 1] = str(Path(out[i + 1]).resolve())
    return out


def resumed_argv(argv: list[str]) -> list[str]:
    """``--resume FOLDER`` repeats the command that made that folder; anything else passes through."""
    if "--resume" not in argv:
        return argv
    at = argv.index("--resume")
    if at + 1 >= len(argv):
        raise SystemExit("--resume needs the book's folder.")
    folder = Path(argv[at + 1])
    if not folder.is_dir():
        folder = get_settings().data_root / "audiobooks" / argv[at + 1]
    try:
        saved = json.loads((folder / RUN_FILE).read_text(encoding="utf-8"))["argv"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{folder} has no {RUN_FILE} to resume from: {exc}") from None
    return list(saved) + argv[:at] + argv[at + 2:]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Make an upload-ready audiobook.")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--gutenberg", type=int, help="Project Gutenberg book number.")
    source.add_argument("--text", type=Path, help="A markdown script with '# ' chapter headings.")
    p.add_argument("--title")
    p.add_argument("--author", default=None)
    p.add_argument("--kind", choices=book_text.KINDS, default="public-domain")
    p.add_argument("--lang", choices=book_text.LANGUAGES, default="en")
    p.add_argument("--voice", help="engine:voice, e.g. kokoro:bm_george.")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--channel", default=book_video.DEFAULT_CHANNEL)
    p.add_argument("--display-title", help="Title on the pictures, e.g. a romanised Urdu title.")
    p.add_argument("--display-author", default=None)
    p.add_argument("--art", type=Path, help="A picture for the cover and thumbnail.")
    p.add_argument("--art-credit", default="", help="Credit line for --art.")
    p.add_argument("--art-query", help="Search Wikimedia Commons for public-domain cover art.")
    p.add_argument("--about", help="A paragraph about the book for the description.")
    p.add_argument("--about-file", type=Path)
    p.add_argument("--bitrate", default="96k", help="MP3 bitrate. 96k mono is clear for speech.")
    p.add_argument("--video", choices=("animated", "cards"), default="animated",
                   help="animated: pulse ring and live captions. cards: one still per chapter, fast.")
    p.add_argument("--template", choices=list(video_templates.BY_ID), default=video_templates.DEFAULT_ID,
                   help="The look: colours, fonts, cover shape and ring.")
    p.add_argument("--style", choices=visualizer.STYLES, default="", help="Ring style; default the template's.")
    p.add_argument("--accent", default="", help="Accent colour such as #ffb347; default the template's.")
    p.add_argument("--height", type=int, choices=(720, 1080), default=1080)
    p.add_argument("--encoder", choices=visualizer.ENCODERS, default="x264")
    p.add_argument("--fps", type=int, default=0, help="Default 24 for animated, 1 for cards.")
    p.add_argument("--no-whisper", action="store_true", help="Time the words by syllables only.")
    p.add_argument("--name", help="Folder name under audiobooks/.")
    p.add_argument("--out", type=Path)
    p.add_argument("--max-chapters", type=int, default=0, help="Only the first N chapters (a test run).")
    p.add_argument("--preview", action="store_true", help="Only the first few sentences, about 15 seconds.")
    p.add_argument("--package-only", action="store_true",
                   help="--out holds a finished audiobook (Audiobook page): time, draw and package it only.")
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--keep-wavs", action="store_true")
    p.add_argument("--resume", metavar="FOLDER",
                   help="Carry on the book in FOLDER with the command that started it.")
    return p


def request_from(args: argparse.Namespace, argv: list[str]) -> book_video.BookVideoRequest:
    about = args.about_file.read_text(encoding="utf-8").strip() if args.about_file else (args.about or "")
    return book_video.BookVideoRequest(
        gutenberg=args.gutenberg, text_path=args.text, title=args.title or "", author=args.author,
        kind=args.kind, language=args.lang, voice=args.voice or "", speed=args.speed, channel=args.channel,
        display_title=args.display_title or "", display_author=args.display_author, art=args.art,
        art_credit=args.art_credit, art_query=args.art_query or "", about=about, bitrate=args.bitrate,
        video=args.video, template=args.template, style=args.style, accent=args.accent, height=args.height,
        encoder=args.encoder, fps=args.fps, use_whisper=not args.no_whisper, name=args.name or "",
        out=args.out, max_chapters=args.max_chapters, plan_only=args.plan_only, no_video=args.no_video,
        keep_wavs=args.keep_wavs, preview=args.preview, package_only=args.package_only,
        argv=_absolute_paths([a for a in argv if a != "--plan-only"]),
    )


def main(argv: list[str] | None = None) -> int:
    argv = resumed_argv(list(sys.argv[1:] if argv is None else argv))
    p = parser()
    args = p.parse_args(argv)
    if args.accent:
        try:
            visualizer.parse_accent(args.accent)
        except ValueError as exc:
            p.error(str(exc))
    if args.text and not args.text.is_file():
        p.error(f"{args.text} was not found.")
    if args.text and not args.title:
        p.error("--title is needed with --text.")
    if args.package_only and not args.out:
        p.error("--package-only needs --out, the finished audiobook's folder.")
    return asyncio.run(book_video.make(request_from(args, argv)))


if __name__ == "__main__":
    raise SystemExit(main())
