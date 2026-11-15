"""Audiobooks and the Files page.

Synthesis itself is not run here; the chapter split, the one-book rule and
the path guard are what can go wrong without a real voice.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import VoiceProfile
from app.services import audiobook, files
from app.services.audiobook import AudiobookError
from app.services.files import FilesError


def _profile(model: Path) -> VoiceProfile:
    return VoiceProfile(
        id=1,
        name="en_US-test-medium",
        engine="piper",
        model_path=str(model),
        config_path="",
        language="en_US",
        speaker_id=None,
        length_scale=1.0,
        noise_scale=0.667,
        noise_w=0.8,
        sentence_silence_s=0.2,
        sample_rate=22050,
        commercial_use="allowed",
        is_default=True,
        installed=True,
    )


# --- Chapters -------------------------------------------------------------


def test_headings_start_chapters_and_are_spoken() -> None:
    text = "# One\n\nFirst sentence here. Second one.\n\n# Two\n\nThird sentence here."
    chapters = audiobook.chapter_sentences(text)
    assert len(chapters) == 2
    assert chapters[0][0] == "One"
    assert chapters[1] == ["Two", "Third sentence here."]


def test_long_text_without_headings_is_cut_into_chapters() -> None:
    sentence = "This sentence is written to be about sixty characters long. "
    text = sentence * 600
    chapters = audiobook.chapter_sentences(text)
    assert len(chapters) >= 3
    for chapter in chapters:
        assert sum(len(s) for s in chapter) <= audiobook.MAX_CHAPTER_CHARS


def test_speed_changes_the_hash_so_a_chapter_is_recorded_again(tmp_path: Path) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"x")
    slow = audiobook.settings_from(audiobook.voice_snapshot(_profile(model), 0.85))
    fast = audiobook.settings_from(audiobook.voice_snapshot(_profile(model), 1.15))
    sentences = ["A line."]
    assert audiobook.chapter_hash(sentences, slow) != audiobook.chapter_hash(sentences, fast)


# --- One book at a time ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_book_is_refused_while_one_is_running(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def slow_run(folder, cancel=None, log_line=None):
        await release.wait()
        return {}

    monkeypatch.setattr(audiobook, "run_book", slow_run)
    monkeypatch.setattr(audiobook, "_JOB", None)
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"x")

    first = await audiobook.create_book("First", "One sentence here.", _profile(model), 1.0)
    assert audiobook.current_book_id() == first
    with pytest.raises(AudiobookError, match="One audiobook runs at a time"):
        await audiobook.create_book("Second", "Another sentence.", _profile(model), 1.0)
    with pytest.raises(AudiobookError, match="Stop this audiobook"):
        audiobook.delete_book(first)

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert audiobook.current_book_id() is None


@pytest.mark.asyncio
async def test_an_interrupted_book_is_listed_as_stopped(data_root: Path, tmp_path: Path) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"x")
    folder = audiobook.audiobooks_dir() / "Left running"
    audiobook.prepare_folder(
        folder, "Left running", "One sentence.", audiobook.voice_snapshot(_profile(model)), 1.0
    )
    audiobook._update(folder, status=audiobook.RUNNING)  # noqa: SLF001
    books = audiobook.list_books()
    assert books[0]["status"] == audiobook.STOPPED


def test_a_book_the_queue_is_speaking_stays_running(data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"x")
    folder = audiobook.audiobooks_dir() / "Live"
    audiobook.prepare_folder(folder, "Live", "One sentence.", audiobook.voice_snapshot(_profile(model)), 1.0)
    audiobook._update(folder, status=audiobook.RUNNING)  # noqa: SLF001
    monkeypatch.setattr("app.services.book_queue.is_speaking", lambda path: True)
    books = audiobook.list_books()
    assert books[0]["status"] == audiobook.RUNNING


def test_voices_for_the_page_put_piper_first_and_say_how_fast(tmp_path: Path) -> None:
    model = tmp_path / "joe.onnx"
    model.write_bytes(b"x")
    piper = VoiceProfile(id=1, name="en_US-joe-medium", engine="piper", language="en_US",
                         model_path=str(model), commercial_use="allowed")
    kokoro = VoiceProfile(id=2, name="am_michael", engine="kokoro", language="en_US",
                          commercial_use="allowed", is_default=True)
    rows = audiobook.voices_for_page([kokoro, piper])
    assert [row["engine"] for row in rows] == ["piper", "kokoro"]
    assert "hour" in rows[0]["speed_note"]
    assert "4 hours" in rows[1]["speed_note"]


def test_a_missing_piper_voice_file_is_refused(data_root: Path, tmp_path: Path) -> None:
    with pytest.raises(AudiobookError, match="no voice file"):
        asyncio.run(
            audiobook.create_book("Book", "A sentence.", _profile(tmp_path / "gone.onnx"), 1.0)
        )


# --- Path guard -----------------------------------------------------------


def test_resolve_serves_a_file_inside_the_data_root(data_root: Path) -> None:
    mp3 = get_settings().data_root / "audiobooks" / "Book" / "audiobook.mp3"
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"ID3")
    assert files.resolve("audiobooks/Book/audiobook.mp3") == mp3.resolve()


@pytest.mark.parametrize(
    "rel",
    [
        "../outside.mp3",
        "audiobooks/../../outside.mp3",
        "C:/Windows/win.ini",
        "app.db",
        "",
    ],
)
def test_resolve_refuses_anything_else(data_root: Path, rel: str) -> None:
    outside = data_root.parent / "outside.mp3"
    outside.write_bytes(b"ID3")
    with pytest.raises(FilesError):
        files.resolve(rel)


def test_project_rows_list_only_finished_files(data_root: Path) -> None:
    render = get_settings().output_dir / "0003-My film" / "render-0002"
    render.mkdir(parents=True)
    for name in ("My film.mp4", "My film - short.mp4", "clip-000.mp4", "piece-0001.mp4",
                 "picture.mp4", "thumbnail.jpg", "subtitles.srt", "mix.wav"):
        (render / name).write_bytes(b"x")
    rows = files.project_rows()
    assert rows[0]["title"] == "My film"
    names = [entry["name"] for entry in rows[0]["files"]]
    assert names == ["My film - short.mp4", "My film.mp4", "subtitles.srt", "thumbnail.jpg"]


# --- Pages ----------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.asyncio
async def test_pages_render(client: AsyncClient) -> None:
    for path in ("/audiobooks", "/files", "/partials/audiobooks", "/partials/files"):
        response = await client.get(path)
        assert response.status_code == 200, path


@pytest.mark.asyncio
async def test_media_endpoint_refuses_the_database(client: AsyncClient) -> None:
    response = await client.get("/files/media", params={"path": "app.db"})
    assert response.status_code == 404
