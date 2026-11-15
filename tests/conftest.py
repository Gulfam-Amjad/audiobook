"""Test fixtures.

Every test runs against its own temporary data root and SQLite file, so no
test can touch the real library on D: or leak state into another test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image as PILImage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config, db
from app.models import Base
from app.services.ffmpeg import MediaInfo


def pytest_configure(config: pytest.Config) -> None:
    """Keep test scratch files off a system drive that has no room.

    A full run writes a few hundred megabytes of temporary media, and the
    install paths refuse to write when the target drive is nearly full. On
    a machine whose C: is down to its last gigabyte that shows up as a
    free-space error in an unrelated test rather than as a real failure.
    Set ``AVM_TEST_TMP`` to choose the directory; otherwise the large-asset
    drive is used when it exists, and pytest's own default when it does not.
    """
    if config.option.basetemp:
        return

    chosen = os.environ.get("AVM_TEST_TMP") or ""
    if not chosen:
        fallback = Path("D:/AIVideoManager/tmp/pytest")
        if fallback.drive and Path(fallback.drive + "/").exists():
            chosen = str(fallback)
    if not chosen:
        return

    try:
        Path(chosen).mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    config.option.basetemp = chosen


@pytest.fixture(autouse=True)
def stub_ffprobe(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the suite independent of whether FFmpeg is installed here.

    Test videos are deliberately fake bytes. A real ffprobe rejects them,
    so without this a machine with FFmpeg and one without would disagree
    about whether a download succeeded. Tests that exercise probing itself
    opt out with ``@pytest.mark.real_ffprobe``.
    """
    if "real_ffprobe" in request.keywords:
        return

    async def fake_probe(path: Path, ffprobe_path: str | None = None) -> MediaInfo:
        return MediaInfo(
            duration_s=4.0,
            width=1280,
            height=720,
            fps=24.0,
            video_codec="h264",
            container="mov,mp4,m4a",
            has_audio=False,
            size_bytes=Path(path).stat().st_size if Path(path).exists() else None,
        )

    monkeypatch.setattr("app.services.ffmpeg.probe", fake_probe)
    monkeypatch.setattr("app.services.downloads.ffmpeg.probe", fake_probe)


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the application at a throwaway data directory."""
    root = tmp_path / "avm-data"
    root.mkdir()
    monkeypatch.setenv("AVM_DATA_ROOT", str(root))
    monkeypatch.setenv("AVM_PAID_ENABLED", "false")
    monkeypatch.setenv("AVM_MONTHLY_SPEND_CAP_USD", "0")
    monkeypatch.setenv("AVM_UI_TOKEN", "")
    # Credentials must be absent so adapters stay unconfigured in tests.
    for name in (
        "ALIBABA_API_KEY", "BYTEPLUS_API_KEY", "FAL_API_KEY", "REPLICATE_API_TOKEN",
        "MINIMAX_API_KEY", "PIXVERSE_API_KEY", "RUNWAY_API_KEY", "LUMA_API_KEY",
        "PIKA_API_KEY", "VIDU_API_KEY", "KLING_ACCESS_KEY", "KLING_SECRET_KEY",
        "KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_WEIGHTS_DATASET",
        "HF_TOKEN", "HF_SPACE_I2V", "HF_SPACE_LIPSYNC",
    ):
        monkeypatch.setenv(f"AVM_{name}", "")

    config.get_settings.cache_clear()
    settings = config.get_settings()
    settings.ensure_dirs()
    yield root
    config.get_settings.cache_clear()


@pytest_asyncio.fixture
async def session(data_root: Path) -> AsyncIterator[AsyncSession]:
    """An async session against a fresh schema built from the ORM metadata."""
    settings = config.get_settings()
    engine = create_async_engine(settings.db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Point the app's own session machinery at this engine so services that
    # open their own sessions join the same database.
    db._engine = engine  # noqa: SLF001
    db._session_factory = factory  # noqa: SLF001

    async with factory() as test_session:
        yield test_session

    await engine.dispose()
    db._engine = None  # noqa: SLF001
    db._session_factory = None  # noqa: SLF001


def write_image(path: Path, size: tuple[int, int] = (640, 480), colour: str = "red") -> Path:
    """Create a small real JPEG so Pillow and the hasher have genuine input."""
    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", size, colour).save(path, "JPEG", quality=70)
    return path


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    return tmp_path / "pictures"
