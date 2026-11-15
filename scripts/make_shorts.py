"""Cut vertical Shorts from a finished film.

    python scripts/make_shorts.py

Three 45-second windows from the Panama Canal film. Each becomes a 1080x1920
Short with a blurred background, a hook line for the first seconds, and
burned-in captions taken from the film's own subtitles. Nothing is uploaded.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.services import ffmpeg, graphics  # noqa: E402

RENDER = Path(r"D:\AIVideoManager\output\0009-Panama Canal\render-0025")
SOURCE = RENDER / "Panama Canal.mp4"
SUBS = RENDER / "subtitles.srt"
OUT = RENDER / "shorts"

LENGTH_S = 45.0
W, H = 1080, 1920

# (file stem, start second in the film, hook line)
CLIPS = [
    ("1-between-two-oceans", 0.0, "A SHIP CLIMBS A MOUNTAIN"),
    ("2-the-mosquito", 100.0, "THE REAL ENEMY WAS A MOSQUITO"),
    ("3-one-slide", 228.0, "ONE SLIDE ERASED A MONTH OF WORK"),
]

HOOK_SECONDS = 4.0


def _secs(stamp: str) -> float:
    h, m, rest = stamp.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _stamp(seconds: float) -> str:
    total = int(round(seconds * 1000))
    h, rem = divmod(total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clip_subtitles(start: float, end: float, dest: Path) -> None:
    """Copy the cues that fall in the window, shifted to start at zero."""
    text = SUBS.read_text(encoding="utf-8-sig")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        a, b = (part.strip() for part in lines[1].split("-->"))
        cue_start, cue_end = _secs(a), _secs(b)
        if cue_end <= start or cue_start >= end:
            continue
        new_start = max(0.0, cue_start - start)
        new_end = min(end - start, cue_end - start)
        if new_end - new_start < 0.05:
            continue
        cues.append((new_start, new_end, "\n".join(lines[2:])))
    out = []
    for i, (s, e, body) in enumerate(cues, 1):
        out.append(f"{i}\n{_stamp(s)} --> {_stamp(e)}\n{body}\n")
    dest.write_text("\n".join(out), encoding="utf-8")


def hook_image(line: str, dest: Path) -> None:
    """A transparent frame with the hook line in the top third."""
    font_path = graphics.fonts_dir() / "BebasNeue-Regular.ttf"
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    words = line.split()
    size = 170
    font = ImageFont.truetype(str(font_path), size)
    lines = [line]
    while True:
        widest = max(draw.textlength(text, font=font) for text in lines)
        if widest <= W - 120 or size <= 80:
            break
        size -= 6
        font = ImageFont.truetype(str(font_path), size)
    if max(draw.textlength(text, font=font) for text in lines) > W - 120:
        mid = len(words) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]

    y = 260
    for text in lines:
        width = draw.textlength(text, font=font)
        x = (W - width) / 2
        draw.rectangle(
            [x - 24, y - 14, x + width + 24, y + size + 14],
            fill=(0, 0, 0, 170),
        )
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 255),
                  stroke_width=6, stroke_fill=(0, 0, 0, 255))
        y += size + 30
    canvas.save(dest)


def build(ffmpeg_path: str, stem: str, start: float, hook: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    end = start + LENGTH_S
    subs = OUT / f"{stem}.srt"
    clip_subtitles(start, end, subs)
    hook_png = OUT / f"{stem}-hook.png"
    hook_image(hook, hook_png)
    dest = OUT / f"{stem}.mp4"

    graph = (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        "boxblur=24:6,eq=brightness=-0.08[bg];"
        f"[fgsrc]scale={W}:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        "[base]subtitles=filename='" + subs.name + "':force_style='"
        "FontName=Arial,FontSize=50,Bold=1,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=4,Shadow=0,"
        "Alignment=2,MarginV=330'[sub];"
        f"[sub][1:v]overlay=0:0:enable='between(t,0,{HOOK_SECONDS})'[v];"
        "[0:a]afade=t=out:st=" + str(LENGTH_S - 1.5) + ":d=1.5[a]"
    )
    cmd = [
        ffmpeg_path, "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{LENGTH_S:.3f}", "-i", str(SOURCE),
        "-loop", "1", "-t", f"{LENGTH_S:.3f}", "-i", str(hook_png),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dest),
    ]
    # The subtitles filter reads its file relative to the working folder.
    subprocess.run(cmd, cwd=OUT, check=True)
    return dest


def main() -> None:
    status = asyncio.run(ffmpeg.detect())
    for stem, start, hook in CLIPS:
        dest = build(status.ffmpeg_path, stem, start, hook)
        print(f"{dest.name}: {dest.stat().st_size} bytes", flush=True)


if __name__ == "__main__":
    main()
