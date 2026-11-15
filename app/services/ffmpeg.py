"""FFmpeg and FFprobe discovery and media probing.

FFmpeg is an optional dependency: the app runs, queues, submits and downloads
without it. What is lost without it is *measurement* - clip durations are the
basis of every capacity figure, so an unprobed clip is counted as unknown
rather than guessed at.

Installation is never attempted automatically. The UI shows the exact command
for the package managers this machine already has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 30

#: Shown in the UI when FFmpeg is missing. Both managers were confirmed
#: present on this machine, so neither is a large manual download.
INSTALL_COMMANDS: tuple[tuple[str, str], ...] = (
    ("winget", "winget install --id Gyan.FFmpeg -e"),
    ("Chocolatey (needs an admin terminal)", "choco install ffmpeg -y"),
)
MANUAL_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/"
OFFICIAL_DOWNLOAD_URL = "https://ffmpeg.org/download.html"


@dataclass
class FFmpegStatus:
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    ffmpeg_version: str | None = None
    ffprobe_version: str | None = None
    source: str = "not found"  # "settings" | "PATH" | "not found"
    message: str = ""

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg_path and self.ffprobe_path)

    @property
    def probe_available(self) -> bool:
        return bool(self.ffprobe_path)


def _resolve_executable(configured: str, fallback_name: str) -> tuple[str | None, str]:
    """Prefer an explicitly configured path, then PATH."""
    text = (configured or "").strip().strip('"')
    if text:
        candidate = Path(text)
        # Accept either the executable itself or the folder holding it.
        if candidate.is_dir():
            candidate = candidate / f"{fallback_name}.exe"
        if candidate.exists() and candidate.is_file():
            return str(candidate), "settings"
        return None, "settings-invalid"

    found = shutil.which(fallback_name)
    if found:
        return found, "PATH"
    return None, "not found"


async def _run(executable: str, *args: str, timeout: int = PROBE_TIMEOUT_S) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", f"Timed out after {timeout}s."
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def detect(
    ffmpeg_override: str = "", ffprobe_override: str = ""
) -> FFmpegStatus:
    """Locate FFmpeg and FFprobe and confirm they actually execute."""
    settings = get_settings()
    ffmpeg_configured = ffmpeg_override or settings.ffmpeg_path
    ffprobe_configured = ffprobe_override or settings.ffprobe_path

    ffmpeg_path, ffmpeg_source = _resolve_executable(ffmpeg_configured, "ffmpeg")
    ffprobe_path, ffprobe_source = _resolve_executable(ffprobe_configured, "ffprobe")

    # A configured ffmpeg usually sits beside ffprobe; try that before failing.
    if ffmpeg_path and not ffprobe_path:
        sibling = Path(ffmpeg_path).with_name("ffprobe.exe")
        if sibling.exists():
            ffprobe_path, ffprobe_source = str(sibling), "settings"

    status = FFmpegStatus(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        source=ffmpeg_source if ffmpeg_path else ffprobe_source,
    )

    problems: list[str] = []
    if ffmpeg_path:
        code, out, err = await _run(ffmpeg_path, "-version", timeout=15)
        if code == 0:
            status.ffmpeg_version = (out or err).splitlines()[0].strip() if (out or err) else None
        else:
            problems.append(f"ffmpeg was found at {ffmpeg_path} but would not run.")
            status.ffmpeg_path = None
    else:
        problems.append(
            "ffmpeg was not found. Video assembly is unavailable until it is installed."
        )

    if ffprobe_path:
        code, out, err = await _run(ffprobe_path, "-version", timeout=15)
        if code == 0:
            status.ffprobe_version = (out or err).splitlines()[0].strip() if (out or err) else None
        else:
            problems.append(f"ffprobe was found at {ffprobe_path} but would not run.")
            status.ffprobe_path = None
    else:
        problems.append(
            "ffprobe was not found. Clip durations cannot be measured, so generated-video "
            "totals will be reported as unknown rather than estimated."
        )

    if ffmpeg_configured and ffmpeg_source == "settings-invalid":
        problems.append(f"The configured ffmpeg path does not exist: {ffmpeg_configured}")
    if ffprobe_configured and ffprobe_source == "settings-invalid":
        problems.append(f"The configured ffprobe path does not exist: {ffprobe_configured}")

    status.message = " ".join(problems) if problems else "FFmpeg and FFprobe are available."
    return status


@dataclass
class MediaInfo:
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    container: str | None = None
    has_audio: bool | None = None
    size_bytes: int | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.duration_s is not None


def _parse_fraction(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if "/" in value:
            numerator, _, denominator = value.partition("/")
            den = float(denominator)
            return float(numerator) / den if den else None
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


async def probe(
    path: Path, ffprobe_path: str | None = None, *, timeout: int = PROBE_TIMEOUT_S
) -> MediaInfo:
    """Read real duration and dimensions from a media file."""
    if ffprobe_path is None:
        status = await detect()
        ffprobe_path = status.ffprobe_path
    if not ffprobe_path:
        return MediaInfo(
            error="FFprobe is not installed, so this file's duration is unknown."
        )
    if not path.exists():
        return MediaInfo(error=f"File not found: {path}")

    code, out, err = await _run(
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
        timeout=timeout,
    )
    if code != 0:
        return MediaInfo(error=f"FFprobe failed: {err.strip()[:300] or 'unknown error'}")

    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return MediaInfo(error=f"FFprobe returned unreadable output: {exc.msg}")

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = fmt.get("duration") or (video or {}).get("duration")
    try:
        duration_s = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_s = None

    info = MediaInfo(
        duration_s=duration_s,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        fps=_parse_fraction((video or {}).get("avg_frame_rate")),
        video_codec=(video or {}).get("codec_name"),
        container=(fmt.get("format_name") or "").split(",")[0] or None,
        has_audio=audio is not None,
        size_bytes=int(fmt["size"]) if fmt.get("size") else None,
        # The full stream list is kept because comparing the video and
        # audio stream lengths against each other is what reveals drift,
        # and that cannot be done from the container duration alone.
        raw={"format": fmt, "video_stream": video or {}, "streams": streams},
    )
    if info.duration_s is None:
        info.error = "FFprobe could not determine a duration for this file."
    return info


async def is_playable_video(path: Path, ffprobe_path: str | None = None) -> tuple[bool, str]:
    """Confirm a downloaded file is genuinely decodable video."""
    info = await probe(path, ffprobe_path)
    if info.error:
        return False, info.error
    if not info.width or not info.height:
        return False, "The file contains no video stream."
    if not info.duration_s or info.duration_s <= 0:
        return False, "The file reports a zero-length duration."
    return True, (
        f"{info.width}x{info.height}, {info.duration_s:.2f}s, {info.video_codec or 'unknown codec'}"
    )


def install_instructions() -> dict[str, object]:
    return {
        "commands": INSTALL_COMMANDS,
        "manual_url": MANUAL_DOWNLOAD_URL,
        "official_url": OFFICIAL_DOWNLOAD_URL,
        "note": (
            "Install with one of the commands below, then restart this application so the "
            "new PATH entry is picked up. If you prefer a manual install, unzip the build "
            "and enter the full path to ffmpeg.exe and ffprobe.exe in Settings."
        ),
    }
