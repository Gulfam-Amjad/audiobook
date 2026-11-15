"""Turn a long script into an audiobook from the command line.

    python scripts/make_audiobook.py book.txt
    python scripts/make_audiobook.py book.txt --still cover.jpg
    python scripts/make_audiobook.py book.txt --voice en_US-norman-medium --speed 1.1

The same work as the Audiobook page, through :mod:`app.services.audiobook`:
one Piper process per chapter, chapters already recorded are skipped, and
the result is ``audiobook.mp3``. With ``--still``, the folder also gets
``audiobook.mp4``: a short loop of the one picture, copied out to the
length of the narration.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services import audiobook  # noqa: E402
from app.services import ffmpeg as ffmpeg_tool  # noqa: E402

DEFAULT_VOICE = "en_US-norman-medium"


def main() -> int:
    parser = argparse.ArgumentParser(description="Speak a book with one warm Piper process per chapter.")
    parser.add_argument("script", type=Path, help="The text file to read.")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Piper voice key or a path to an .onnx file.")
    parser.add_argument("--speed", type=float, default=1.0, help="1.0 is the voice's own pace.")
    parser.add_argument("--still", type=Path, default=None, help="One picture to loop under the audio.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output folder. Defaults to the audiobooks folder under the data root.",
    )
    args = parser.parse_args()
    if not args.script.is_file():
        print(f"The script was not found: {args.script}", file=sys.stderr)
        return 1
    folder = args.out or get_settings().data_root / "audiobooks" / args.script.stem

    try:
        text = args.script.read_text(encoding="utf-8-sig")
        voice = audiobook.piper_voice(args.voice, args.speed)
        audiobook.prepare_folder(folder, args.script.stem, text, voice, args.speed)
    except (OSError, audiobook.AudiobookError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    result = asyncio.run(audiobook.run_book(folder, log_line=lambda line: print(line, flush=True)))
    if result.get("status") != audiobook.DONE:
        print(result.get("error") or result.get("message") or "The audiobook was not finished.", file=sys.stderr)
        return 1

    if args.still is not None:
        tools = asyncio.run(ffmpeg_tool.detect())
        try:
            audiobook.attach_still(
                tools.ffmpeg_path or "ffmpeg",
                args.still,
                folder / "audiobook.mp3",
                folder / "audiobook.mp4",
                folder / "chapters",
            )
        except audiobook.AudiobookError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"video: {folder / 'audiobook.mp4'}", flush=True)
    print(f"audio: {folder / 'audiobook.mp3'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
