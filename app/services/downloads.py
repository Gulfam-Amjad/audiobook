"""Downloading and filing generated videos.

Correctness rules, in order of importance:

1. **Never overwrite.** A colliding filename gets a ``(2)`` suffix.
2. **Never leave a half file in place.** Bytes land in ``.part`` inside the
   data root's temp directory and are only moved into the library once the
   transfer is complete and verified.
3. **Never trust the body.** Providers occasionally return an HTML error page
   with a 200 status; a file that FFprobe cannot decode as video is rejected.
4. **Record what is actually there.** Duration, dimensions and codec come
   from FFprobe, not from what we asked the provider for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import ErrorClass, OutputSource
from app.models import Batch, Image, Job, Output
from app.providers.http import DOWNLOAD_TIMEOUT, build_client, sanitize_provider_message
from app.services import ffmpeg, settings_store
from app.services.paths import safe_filename, unique_path
from app.services.timeutil import utcnow

log = logging.getLogger(__name__)

#: Anything smaller than this is an error page, not a video.
MIN_PLAUSIBLE_VIDEO_BYTES = 10 * 1024
CHUNK_BYTES = 256 * 1024


@dataclass
class DownloadResult:
    ok: bool
    path: Path | None = None
    size_bytes: int = 0
    sha256: str | None = None
    message: str = ""
    error_class: ErrorClass | None = None
    media: ffmpeg.MediaInfo | None = None


async def resolve_output_dir(
    session: AsyncSession, job: Job, batch: Batch | None
) -> Path:
    """Build the destination folder from the user's organisation settings."""
    app_settings = await settings_store.get_all(session)
    override = str(app_settings.get("output_dir_override") or "").strip()
    base = Path(override) if override else get_settings().output_dir

    parts: list[str] = []
    if app_settings.get("organize_by_provider", True):
        parts.append(safe_filename(job.provider_key or "unassigned", fallback="unassigned"))
    if app_settings.get("organize_by_date", True):
        parts.append(utcnow().strftime("%Y-%m-%d"))
    if batch is not None:
        parts.append(safe_filename(f"batch-{batch.id}-{batch.name}", fallback=f"batch-{batch.id}"))

    target = base.joinpath(*parts) if parts else base
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_filename(job: Job, image: Image | None, extension: str = ".mp4") -> str:
    stem = Path(image.filename).stem if image else f"job-{job.id}"
    pieces = [f"{job.id:06d}", safe_filename(stem, fallback="clip")[:60]]
    if job.manual_token:
        pieces.append(job.manual_token)
    return safe_filename("__".join(pieces) + extension, fallback=f"job-{job.id}{extension}")


def _extension_from(url: str, content_type: str | None) -> str:
    mapping = {
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
    }
    if content_type:
        guess = mapping.get(content_type.split(";")[0].strip().lower())
        if guess:
            return guess
    suffix = Path(httpx.URL(url).path).suffix.lower()
    if suffix in {".mp4", ".webm", ".mov", ".mkv", ".m4v"}:
        return suffix
    return ".mp4"


async def download_to_library(
    session: AsyncSession,
    job: Job,
    url: str,
    *,
    timeout_s: int | None = None,
) -> DownloadResult:
    """Fetch a finished video and file it under the output directory."""
    settings = get_settings()
    image = await session.get(Image, job.image_id)
    batch = await session.get(Batch, job.batch_id) if job.batch_id else None

    if timeout_s is None:
        timeout_s = int(await settings_store.get(session, "download_timeout_seconds"))
    timeout = httpx.Timeout(
        connect=15.0, read=float(timeout_s), write=60.0, pool=15.0
    )

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    part_path = settings.tmp_dir / f"job-{job.id}-{job.client_token[-8:]}.part"
    if part_path.exists():
        part_path.unlink()

    digest = hashlib.sha256()
    written = 0
    content_type: str | None = None

    try:
        async with build_client(timeout=timeout) as client:
            async with client.stream("GET", url) as response:
                if response.status_code == 404:
                    return DownloadResult(
                        ok=False,
                        message=(
                            "The provider's download link has expired or was removed "
                            "(HTTP 404). The generation itself may have succeeded."
                        ),
                        error_class=ErrorClass.DOWNLOAD,
                    )
                if response.status_code >= 400:
                    body = sanitize_provider_message(
                        (await response.aread()).decode("utf-8", "replace"), 200
                    )
                    return DownloadResult(
                        ok=False,
                        message=f"Download failed with HTTP {response.status_code}: {body}",
                        error_class=ErrorClass.DOWNLOAD,
                    )

                content_type = response.headers.get("content-type")
                expected = response.headers.get("content-length")
                expected_bytes = int(expected) if expected and expected.isdigit() else None

                with part_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes(CHUNK_BYTES):
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)

                if expected_bytes is not None and written != expected_bytes:
                    part_path.unlink(missing_ok=True)
                    return DownloadResult(
                        ok=False,
                        message=(
                            f"Download was truncated: received {written} bytes but the server "
                            f"declared {expected_bytes}. The partial file was discarded."
                        ),
                        error_class=ErrorClass.DOWNLOAD,
                    )
    except httpx.HTTPError as exc:
        part_path.unlink(missing_ok=True)
        return DownloadResult(
            ok=False,
            message=f"Download failed: {sanitize_provider_message(str(exc), 200)}",
            error_class=ErrorClass.DOWNLOAD,
        )

    if written < MIN_PLAUSIBLE_VIDEO_BYTES:
        preview = part_path.read_bytes()[:200].decode("utf-8", "replace")
        part_path.unlink(missing_ok=True)
        return DownloadResult(
            ok=False,
            message=(
                f"The provider returned only {written} bytes, which is not a video. "
                f"Body started with: {sanitize_provider_message(preview, 150)}"
            ),
            error_class=ErrorClass.DOWNLOAD,
        )

    # Confirm it decodes before it is allowed into the library.
    ffmpeg_status = await ffmpeg.detect()
    media = await ffmpeg.probe(part_path, ffmpeg_status.ffprobe_path)
    if ffmpeg_status.probe_available and media.error:
        part_path.unlink(missing_ok=True)
        return DownloadResult(
            ok=False,
            message=f"The downloaded file is not decodable video: {media.error}",
            error_class=ErrorClass.DOWNLOAD,
            media=media,
        )

    target_dir = await resolve_output_dir(session, job, batch)
    extension = _extension_from(url, content_type)
    final_path = unique_path(target_dir / build_filename(job, image, extension))

    try:
        part_path.replace(final_path)
    except OSError as exc:
        part_path.unlink(missing_ok=True)
        return DownloadResult(
            ok=False,
            message=f"Could not move the finished download into place: {exc}",
            error_class=ErrorClass.DOWNLOAD,
        )

    log.info(
        "Download complete",
        extra={
            "job_id": job.id,
            "provider": job.provider_key,
            "bytes": written,
            "duration_s": media.duration_s,
        },
    )
    return DownloadResult(
        ok=True,
        path=final_path,
        size_bytes=written,
        sha256=digest.hexdigest(),
        message="Downloaded and verified.",
        media=media,
    )


async def record_output(
    session: AsyncSession,
    job: Job,
    result: DownloadResult,
    *,
    source: OutputSource = OutputSource.API,
    source_url: str | None = None,
) -> Output:
    """Persist an output row and write a sidecar metadata file."""
    if result.path is None:
        raise ValueError("Cannot record an output without a file path.")

    media = result.media or ffmpeg.MediaInfo()
    existing = (
        await session.execute(select(Output).where(Output.job_id == job.id))
    ).scalar_one_or_none()
    output = existing or Output(job_id=job.id)

    output.file_path = str(result.path)
    output.file_size = result.size_bytes
    output.sha256 = result.sha256
    output.source = source
    output.provider_key = job.provider_key
    output.duration_s = media.duration_s
    output.width = media.width
    output.height = media.height
    output.fps = media.fps
    output.video_codec = media.video_codec
    output.container = media.container
    output.has_audio = media.has_audio
    output.probe_error = media.error
    output.downloaded_at = utcnow()
    output.extra = {
        "requested_settings": job.settings,
        "prompt": job.prompt,
        "negative_prompt": job.negative_prompt,
        "task_ref": job.task_ref,
        "client_token": job.client_token,
        "manual_token": job.manual_token,
    }
    if existing is None:
        session.add(output)
    await session.flush()

    await _write_sidecar(session, job, output, source_url=source_url)
    return output


async def _write_sidecar(
    session: AsyncSession, job: Job, output: Output, *, source_url: str | None
) -> None:
    """Write a JSON file beside the video recording how it was made.

    The provider's URL is stored, but never any credential. If the sidecar
    cannot be written the download still counts as successful - it is
    metadata, not the deliverable.
    """
    image = await session.get(Image, job.image_id)
    payload = {
        "job_id": job.id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": utcnow().isoformat(),
        "provider": job.provider_key,
        "account_id": job.account_id,
        "task_ref": job.task_ref,
        "client_token": job.client_token,
        "manual_token": job.manual_token,
        "source_image": {
            "path": image.path if image else None,
            "filename": image.filename if image else None,
            "sha256": image.sha256 if image else None,
            "width": image.width if image else None,
            "height": image.height if image else None,
        },
        "prompt": job.prompt,
        "negative_prompt": job.negative_prompt,
        "requested_settings": job.settings,
        "measured": {
            "duration_s": output.duration_s,
            "width": output.width,
            "height": output.height,
            "fps": output.fps,
            "video_codec": output.video_codec,
            "container": output.container,
            "has_audio": output.has_audio,
            "probe_error": output.probe_error,
        },
        "file": {
            "path": output.file_path,
            "size_bytes": output.file_size,
            "sha256": output.sha256,
        },
        "download_url": source_url,
        "cost_estimate_usd": job.cost_estimate_usd,
        "cost_estimate_units": job.cost_estimate_units,
        "cost_estimate_unit": job.cost_estimate_unit,
    }

    sidecar = Path(output.file_path).with_suffix(Path(output.file_path).suffix + ".json")
    try:
        sidecar.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning(
            "Could not write sidecar metadata",
            extra={"job_id": job.id, "path": str(sidecar), "error": str(exc)},
        )


async def import_local_file(
    session: AsyncSession,
    job: Job,
    source_path: Path,
    *,
    move: bool = True,
) -> DownloadResult:
    """Bring a manually downloaded file into the library.

    Used by manual-assist. The file is probed exactly like an API download,
    so a clip the user produced by hand is measured the same way and cannot
    inflate the generated-motion total with an unverified duration.
    """
    if not source_path.exists():
        return DownloadResult(
            ok=False,
            message=f"File not found: {source_path}",
            error_class=ErrorClass.DOWNLOAD,
        )

    size = source_path.stat().st_size
    if size < MIN_PLAUSIBLE_VIDEO_BYTES:
        return DownloadResult(
            ok=False,
            message=f"File is only {size} bytes, which is too small to be a video.",
            error_class=ErrorClass.DOWNLOAD,
        )

    ffmpeg_status = await ffmpeg.detect()
    media = await ffmpeg.probe(source_path, ffmpeg_status.ffprobe_path)
    if ffmpeg_status.probe_available and media.error:
        return DownloadResult(
            ok=False,
            message=f"This file is not decodable video: {media.error}",
            error_class=ErrorClass.DOWNLOAD,
            media=media,
        )

    image = await session.get(Image, job.image_id)
    batch = await session.get(Batch, job.batch_id) if job.batch_id else None
    target_dir = await resolve_output_dir(session, job, batch)
    final_path = unique_path(
        target_dir / build_filename(job, image, source_path.suffix.lower() or ".mp4")
    )

    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                digest.update(chunk)
        if move:
            # replace() fails when the inbox and the library are on different
            # drives. move() falls back to a copy in that case.
            shutil.move(str(source_path), final_path)
        else:
            final_path.write_bytes(source_path.read_bytes())
    except OSError as exc:
        return DownloadResult(
            ok=False,
            message=f"Could not file the video: {exc}",
            error_class=ErrorClass.DOWNLOAD,
        )

    return DownloadResult(
        ok=True,
        path=final_path,
        size_bytes=size,
        sha256=digest.hexdigest(),
        message="Imported from the manual-assist inbox.",
        media=media,
    )


async def reprobe_output(session: AsyncSession, output: Output) -> ffmpeg.MediaInfo:
    """Re-measure an output, for use once FFmpeg is finally installed."""
    status = await ffmpeg.detect()
    path = Path(output.file_path)
    media = await ffmpeg.probe(path, status.ffprobe_path)
    if media.ok:
        output.duration_s = media.duration_s
        output.width = media.width
        output.height = media.height
        output.fps = media.fps
        output.video_codec = media.video_codec
        output.container = media.container
        output.has_audio = media.has_audio
        output.probe_error = None
    else:
        output.probe_error = media.error
    await session.flush()
    return media


def expiry_warning(ttl_hours: float | None, submitted_at: datetime | None) -> str | None:
    """Warn when a provider's result URL is close to expiring."""
    if not ttl_hours or submitted_at is None:
        return None
    age_hours = (utcnow() - submitted_at).total_seconds() / 3600
    if age_hours >= ttl_hours:
        return (
            f"The provider keeps result links for about {ttl_hours:g}h and this task is "
            f"{age_hours:.1f}h old, so the link has probably expired."
        )
    if age_hours >= ttl_hours * 0.75:
        return (
            f"This task is {age_hours:.1f}h old and links last about {ttl_hours:g}h. "
            "Download it soon."
        )
    return None
