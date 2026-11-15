"""Assemble finished clips with FFmpeg.

Each source is normalised to the same size, frame rate and pixel format,
then concatenated. Original files are only ever read. A fade, when asked
for, overlaps the clips, so the finished file is shorter than the sum of
its parts — that shorter length is stored separately and is not reported
as extra generated video.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import AssemblyStatus
from app.db import session_scope
from app.models import Assembly, Output
from app.services import ffmpeg
from app.services.library import LibraryError, assert_servable, media_roots
from app.services.paths import UnsafePathError, normalize, safe_filename, unique_path
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

_CANCELLED: set[int] = set()
_RUNNING: dict[int, asyncio.subprocess.Process] = {}


class AssemblyError(Exception):
    """The user asked for an assembly that cannot be started."""


def fade_filter(durations: list[float], fade_s: float) -> tuple[str, float]:
    """An xfade chain, and the duration it will produce.

    The offset of each join is the running length so far minus the fades
    already consumed. A fade longer than a clip is refused by the caller.
    """
    if len(durations) < 2:
        raise AssemblyError("A fade needs at least two clips.")
    if fade_s <= 0:
        raise AssemblyError("Fade length must be greater than zero.")
    parts: list[str] = []
    elapsed = durations[0]
    previous = "[0:v]"
    for index, duration in enumerate(durations[1:], start=1):
        offset = elapsed - fade_s
        if offset <= 0:
            raise AssemblyError(
                f"Clip {index} is too short for a {fade_s:.1f}s fade. "
                "Use a shorter fade or no transition."
            )
        label = "[vout]" if index == len(durations) - 1 else f"[v{index}]"
        parts.append(
            f"{previous}[{index}:v]xfade=transition=fade:duration={fade_s:.3f}:offset={offset:.3f}{label}"
        )
        previous = label
        elapsed = offset + duration
    return ";".join(parts), elapsed


def normalize_args(
    ffmpeg_path: str, source: Path, dest: Path, *, width: int, height: int, fps: int
) -> list[str]:
    """One clip, forced to a common shape. Audio is dropped here and added later."""
    return [
        ffmpeg_path,
        "-y",
        "-i",
        str(source),
        "-vf",
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p"
        ),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-an",
        str(dest),
    ]


def join_args(
    ffmpeg_path: str,
    clips: list[Path],
    destination: Path,
    *,
    durations: list[float],
    fade_s: float,
    audio_path: str,
    listing: Path,
) -> list[str]:
    """The final FFmpeg invocation. Always a list, never a shell string."""
    if fade_s:
        graph, _expected = fade_filter(durations, fade_s)
        command = [ffmpeg_path, "-y"]
        for path in clips:
            command.extend(["-i", str(path)])
        if audio_path:
            command.extend(["-i", audio_path])
        command.extend(["-filter_complex", graph, "-map", "[vout]"])
        if audio_path:
            command.extend(["-map", f"{len(clips)}:a", "-c:a", "aac", "-shortest"])
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", str(destination)])
        return command

    listing.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in clips),
        encoding="utf-8",
    )
    command = [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
    if audio_path:
        command.extend(
            ["-i", audio_path, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest"]
        )
    else:
        command.extend(["-c", "copy"])
    command.append(str(destination))
    return command


async def create_assembly(
    session: AsyncSession,
    *,
    name: str,
    output_ids: list[int],
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    fade_s: float = 0.0,
    audio_path: str = "",
) -> Assembly:
    cleaned = (name or "").strip() or "Assembly"
    if not output_ids:
        raise AssemblyError("Pick at least two clips.")
    if len(output_ids) < 2:
        raise AssemblyError("An assembly needs at least two clips.")
    if not 2 <= fps <= 60:
        raise AssemblyError("Frame rate must be between 2 and 60.")
    if width < 16 or height < 16 or width > 3840 or height > 2160:
        raise AssemblyError("Size must be between 16px and 3840×2160.")
    if fade_s < 0 or fade_s > 5:
        raise AssemblyError("Fade must be between 0 and 5 seconds.")

    roots = await media_roots(session)
    clips: list[Output] = []
    for output_id in output_ids:
        output = await session.get(Output, output_id)
        if output is None:
            raise AssemblyError(f"Clip #{output_id} is not in the library.")
        try:
            assert_servable(Path(output.file_path), roots)
        except LibraryError as exc:
            raise AssemblyError(str(exc)) from exc
        if not output.duration_s or output.duration_s <= 0:
            raise AssemblyError(
                f"{Path(output.file_path).name} has no measured duration. "
                "Re-probe it on the Video Library screen before assembling."
            )
        clips.append(output)

    if fade_s:
        fade_filter([clip.duration_s or 0.0 for clip in clips], fade_s)

    audio = ""
    if audio_path.strip():
        try:
            audio_file = normalize(audio_path)
        except UnsafePathError as exc:
            raise AssemblyError(str(exc)) from exc
        if not audio_file.is_file():
            raise AssemblyError(f"Audio file not found: {audio_file}")
        audio = str(audio_file)

    row = Assembly(
        name=cleaned[:200],
        status=AssemblyStatus.PENDING,
        clip_order=[clip.id for clip in clips],
        settings={
            "width": width,
            "height": height,
            "fps": fps,
            "fade_s": fade_s,
            "audio_path": audio,
        },
        source_duration_s=sum(clip.duration_s or 0.0 for clip in clips),
        stage_message="Waiting to start.",
    )
    session.add(row)
    await session.flush()
    return row


def request_cancel(assembly_id: int) -> None:
    _CANCELLED.add(assembly_id)
    process = _RUNNING.get(assembly_id)
    if process is not None and process.returncode is None:
        process.kill()


async def run_assembly(assembly_id: int) -> None:
    """Normalise, concatenate, probe. Safe to run as a background task."""
    try:
        await _run(assembly_id)
    except Exception as exc:  # noqa: BLE001 - recorded on the row, not lost
        log.exception("Assembly %s failed", assembly_id)
        async with session_scope() as session:
            row = await session.get(Assembly, assembly_id)
            if row is not None and row.status not in (
                AssemblyStatus.COMPLETED,
                AssemblyStatus.CANCELLED,
            ):
                row.status = AssemblyStatus.FAILED
                row.error = str(exc)[:500]
                row.stage_message = "Failed."


async def _run(assembly_id: int) -> None:
    status = await ffmpeg.detect()
    async with session_scope() as session:
        row = await session.get(Assembly, assembly_id)
        if row is None:
            return
        if not status.available or not status.ffmpeg_path:
            row.status = AssemblyStatus.FAILED
            row.error = status.message or "FFmpeg is not installed."
            row.stage_message = "FFmpeg is missing."
            return
        order = list(row.clip_order or [])
        settings = dict(row.settings or {})
        outputs = []
        for output_id in order:
            output = await session.get(Output, output_id)
            if output is None:
                row.status = AssemblyStatus.FAILED
                row.error = f"Clip #{output_id} disappeared before rendering."
                return
            outputs.append(output)
        row.status = AssemblyStatus.NORMALIZING
        row.stage_message = "Normalising clips."
        ffmpeg_path = status.ffmpeg_path
        ffprobe_path = status.ffprobe_path

    work = get_settings().tmp_dir / f"assembly-{assembly_id}"
    work.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    total = len(outputs)
    for index, output in enumerate(outputs, start=1):
        if assembly_id in _CANCELLED:
            await _mark(assembly_id, AssemblyStatus.CANCELLED, "Cancelled.")
            return
        dest = work / f"{index:03d}.mp4"
        code = await _spawn(
            assembly_id,
            normalize_args(
                ffmpeg_path,
                Path(output.file_path),
                dest,
                width=int(settings.get("width") or 1280),
                height=int(settings.get("height") or 720),
                fps=int(settings.get("fps") or 24),
            ),
        )
        if assembly_id in _CANCELLED:
            await _mark(assembly_id, AssemblyStatus.CANCELLED, "Cancelled.")
            return
        if code != 0 or not dest.exists():
            await _mark(assembly_id, AssemblyStatus.FAILED, f"Could not normalise clip {index}.")
            return
        normalized.append(dest)
        await _mark(
            assembly_id,
            AssemblyStatus.NORMALIZING,
            f"Normalised {index} of {total}.",
            progress=40.0 * index / total,
        )

    out_dir = get_settings().assemblies_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    async with session_scope() as session:
        row = await session.get(Assembly, assembly_id)
        stem = safe_filename(row.name if row else "assembly", fallback="assembly")
    final = unique_path(out_dir / f"{stem}.mp4")

    await _mark(assembly_id, AssemblyStatus.CONCATENATING, "Joining clips.", progress=50)
    fade_s = float(settings.get("fade_s") or 0)
    audio = str(settings.get("audio_path") or "")
    durations = [float(output.duration_s or 0) for output in outputs]
    listing = work / "concat.txt"
    command = join_args(
        ffmpeg_path,
        normalized,
        final,
        durations=durations,
        fade_s=fade_s,
        audio_path=audio,
        listing=listing,
    )

    if assembly_id in _CANCELLED:
        await _mark(assembly_id, AssemblyStatus.CANCELLED, "Cancelled.")
        return
    code = await _spawn(assembly_id, command)
    if assembly_id in _CANCELLED:
        await _mark(assembly_id, AssemblyStatus.CANCELLED, "Cancelled.")
        return
    if code != 0 or not final.exists():
        await _mark(assembly_id, AssemblyStatus.FAILED, "FFmpeg could not join the clips.")
        return

    info = await ffmpeg.probe(final, ffprobe_path)
    async with session_scope() as session:
        row = await session.get(Assembly, assembly_id)
        if row is None:
            return
        row.status = AssemblyStatus.COMPLETED
        row.output_path = str(final)
        row.output_duration_s = info.duration_s
        row.progress = 100
        row.completed_at = utcnow()
        row.error = None
        row.stage_message = (
            f"Saved {final.name}. Source clips total "
            f"{(row.source_duration_s or 0):.1f}s of generated video; "
            f"the file itself is "
            f"{info.duration_s:.1f}s."
            if info.duration_s
            else f"Saved {final.name}. Its duration could not be measured."
        )
    _CANCELLED.discard(assembly_id)


async def _mark(
    assembly_id: int, status: str, message: str, *, progress: float | None = None
) -> None:
    async with session_scope() as session:
        row = await session.get(Assembly, assembly_id)
        if row is None:
            return
        row.status = status
        row.stage_message = message
        if status == AssemblyStatus.FAILED:
            row.error = message
        if progress is not None:
            row.progress = progress


async def _spawn(assembly_id: int, command: list[str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _RUNNING[assembly_id] = process
    try:
        return await process.wait()
    finally:
        _RUNNING.pop(assembly_id, None)


async def list_assemblies(session: AsyncSession) -> list[Assembly]:
    return list(
        (
            await session.execute(select(Assembly).order_by(Assembly.id.desc()))
        )
        .scalars()
        .all()
    )
