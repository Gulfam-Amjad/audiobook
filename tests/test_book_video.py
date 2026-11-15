"""The Book Video page: templates, voices, the queue, progress and the routes.

Speech and the long render are not run here (test_visualizer.py renders
real segments); what is checked is everything the page decides on its own.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import book_queue, book_studio, book_text, book_video, video_templates, visualizer, voice_catalog
from tests.conftest import write_image

SCRIPT = (
    "# The First Idea\n\nEvery great change begins with one small step. Nobody notices it at first. "
    "Then the habit grows, quietly, until it shapes the whole day.\n\n"
    "# The Second Idea\n\nSystems beat goals. A goal is a wish; a system is what you do every morning.\n"
)


# --- Templates ---------------------------------------------------------------------


def test_twelve_templates_with_distinct_looks() -> None:
    ids = [t.id for t in video_templates.TEMPLATES]
    assert len(ids) == 12 and len(set(ids)) == 12
    assert video_templates.DEFAULT_ID in ids
    keys = {t.theme.key() for t in video_templates.TEMPLATES}
    assert len(keys) == 12
    for t in video_templates.TEMPLATES:
        assert t.ring in visualizer.STYLES
        assert t.theme.cover_shape in visualizer.COVER_SHAPES
        assert t.theme.background in visualizer.BACKGROUNDS
        assert t.accent_hex.startswith("#") and len(t.swatch) == 3
        known = set(video_templates.FONT_SOURCES) | {"Lora.ttf", "Inter.ttf", "Oswald.ttf"}
        assert t.fonts() <= known, t.id


def test_mindset_is_the_original_look() -> None:
    mindset = video_templates.get("mindset")
    assert mindset.theme == visualizer.Theme()
    assert video_templates.get("nope").id == "mindset"


def test_look_for_uses_template_defaults_and_overrides() -> None:
    noir = video_templates.get("noir")
    look = video_templates.look_for(noir, height=720)
    assert look.style == noir.ring and look.accent == noir.accent and look.theme is noir.theme
    tuned = video_templates.look_for(noir, style="wave", accent=(1, 2, 3))
    assert tuned.style == "wave" and tuned.accent == (1, 2, 3)
    assert look.fingerprint() != video_templates.look_for(video_templates.get("calm"), height=720).fingerprint()


def test_preview_is_drawn_once_and_cached(data_root: Path) -> None:
    template = video_templates.get("chronicle")
    first = video_templates.preview(template)
    assert first.is_file() and first.stat().st_size > 5000
    stamp = first.stat().st_mtime_ns
    assert video_templates.preview(template) == first
    assert first.stat().st_mtime_ns == stamp
    art = write_image(data_root / "art.jpg", (900, 600), "navy")
    with_art = video_templates.preview(template, art)
    assert with_art != first and with_art.is_file()


@pytest.mark.parametrize("template_id", ["midnight", "boardroom", "growth"])
def test_cover_shapes_draw_with_and_without_art(data_root: Path, template_id: str) -> None:
    art = write_image(data_root / "wide.jpg", (1200, 500), "green")
    look = video_templates.look_for(video_templates.get(template_id), height=360, fps=8)
    tl = video_templates._sample_timeline()
    frames = int(tl.duration_s * look.fps) + 2
    amp = np.full(frames, 0.5, np.float32)
    bands = np.full((frames, visualizer.BANDS), 0.5, np.float32)
    for picture in (art, None):
        renderer = visualizer.Renderer(tl, look, amp, bands, art=picture, title="T", author="A",
                                       channel="C", badge="Book Summary", levels=4)
        frame = renderer.draw(40)
        assert frame.shape == (360, look.width, 3)


def test_still_style_keeps_the_ring_fixed_and_drops_the_frame_rate() -> None:
    request = book_video.BookVideoRequest(template="chronicle", style="still")
    look = book_video.look_from(request)
    assert look.style == "still" and look.fps == visualizer.STILL_FPS
    look = video_templates.look_for(video_templates.get("chronicle"), style="still", height=360, fps=8)
    tl = video_templates._sample_timeline()
    frames = int(tl.duration_s * look.fps) + 2
    quiet, loud = np.full(frames, 0.0, np.float32), np.full(frames, 1.0, np.float32)
    bands = np.full((frames, visualizer.BANDS), 0.5, np.float32)
    rings = []
    for amp in (quiet, loud):
        renderer = visualizer.Renderer(tl, look, amp, bands, art=None, title="T", author="A",
                                       channel="C", badge="Book Summary")
        renderer.draw(40)
        y0, y1, x0, x1 = renderer.ring_box
        rings.append(renderer.frame[y0:y1, x0:x1].copy())
    assert np.array_equal(rings[0], rings[1])
    assert visualizer.estimate_seconds(3600, look) < visualizer.estimate_seconds(
        3600, video_templates.look_for(video_templates.get("chronicle"), style="bars", height=360, fps=8))


# --- Voices ------------------------------------------------------------------------


@pytest.fixture
def fake_voices(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.providers.speech import kokoro
    from app.services import piper

    models = data_root / "models" / "kokoro"
    models.mkdir(parents=True)
    vector = np.zeros((2, 2), np.float32)
    with (models / kokoro.VOICES_FILE).open("wb") as handle:
        np.savez(handle, af_heart=vector, bm_george=vector, jf_alpha=vector, hm_omega=vector)
    monkeypatch.setattr(kokoro, "models_dir", lambda: models)
    voices = data_root / "piper-voices"
    (voices / "en_US-joe-medium").mkdir(parents=True)
    (voices / "en_US-joe-medium" / "en_US-joe-medium.onnx").write_bytes(b"x")
    (voices / "de_DE-thorsten-medium").mkdir(parents=True)
    (voices / "de_DE-thorsten-medium" / "de_DE-thorsten-medium.onnx").write_bytes(b"x")
    monkeypatch.setattr(piper, "voices_dir", lambda: voices)
    return data_root


def test_catalog_lists_kokoro_piper_and_edge(fake_voices: Path) -> None:
    specs = {v.spec for v in voice_catalog.catalog()}
    assert {"kokoro:af_heart", "kokoro:bm_george", "kokoro:hm_omega", "piper:en_US-joe-medium"} <= specs
    assert "kokoro:jf_alpha" not in specs  # Japanese is not narrated here
    assert "piper:de_DE-thorsten-medium" not in specs
    assert "edge:ur-PK-AsadNeural" in specs
    george = voice_catalog.find("kokoro:bm_george")
    assert george.accent == "British" and george.gender == "male" and george.recommended
    assert voice_catalog.rows("en")[0]["spec"] == "piper:en_US-joe-medium"
    assert voice_catalog.find("kokoro:hm_omega").language == "hi"
    assert voice_catalog.find("edge:en-US-AndrewMultilingualNeural").label == "Andrew (multilingual)"


def test_rows_show_demo_once_made(fake_voices: Path) -> None:
    urdu = voice_catalog.rows("ur")
    assert urdu and all(r["language"] == "ur" for r in urdu)
    assert not any(r["offline"] for r in urdu)
    voice = voice_catalog.find("kokoro:af_heart")
    assert next(r for r in voice_catalog.rows("en") if r["spec"] == voice.spec)["demo"] == ""
    voice.demo_path().parent.mkdir(parents=True, exist_ok=True)
    voice.demo_path().write_bytes(b"mp3")
    assert next(r for r in voice_catalog.rows("en") if r["spec"] == voice.spec)["demo"] == "kokoro-af_heart.mp3"


@pytest.mark.asyncio
async def test_make_missing_skips_made_demos_and_survives_a_failure(fake_voices: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []

    async def fake_demo(voice, ffmpeg_path):
        if voice.name == "bm_george":
            raise RuntimeError("no voice")
        made.append(voice.spec)
        voice.demo_path().parent.mkdir(parents=True, exist_ok=True)
        voice.demo_path().write_bytes(b"mp3")
        return voice.demo_path()

    async def fake_detect():
        from app.services.ffmpeg import FFmpegStatus
        return FFmpegStatus(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe")

    monkeypatch.setattr(voice_catalog, "make_demo", fake_demo)
    monkeypatch.setattr("app.services.ffmpeg.detect", fake_detect)
    first = await voice_catalog.make_missing(engines=("kokoro",))
    assert set(made) == {"kokoro:af_heart", "kokoro:hm_omega"}
    assert len(first["errors"]) == 1 and not first["running"]
    made.clear()
    await voice_catalog.make_missing(engines=("kokoro",))
    assert made == []


# --- The queue -----------------------------------------------------------------------


def test_a_dead_running_job_is_parked_for_the_next_chapter(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(book_queue, "_alive", lambda pid: pid == 42)
    job = book_queue.add(["--title", "Frankenstein"], book_folder=data_root / "audiobooks" / "Frankenstein")
    path = book_queue.queue_dir() / f"{job}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(status="running", pid=99999, attempts=2)
    path.write_text(json.dumps(data), encoding="utf-8")

    assert book_queue.park_orphans() == [job]
    parked = json.loads(path.read_text(encoding="utf-8"))
    assert parked["status"] == "queued" and parked["attempts"] == 0 and parked["pid"] is None
    assert "next chapter" in parked["last_error"]

    parked.update(status="running", pid=42)
    path.write_text(json.dumps(parked), encoding="utf-8")
    assert book_queue.park_orphans() == []
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "running"


def test_waiting_books_get_work_estimates_and_ready_times(data_root: Path) -> None:
    script = data_root / "book.md"
    script.write_text("# One\n\n" + "A sentence of the book. " * 4000, encoding="utf-8")
    argv = ["--text", str(script), "--title", "Long", "--voice", "piper:en_US-joe-medium",
            "--height", "720", "--video", "animated", "--template", "midnight"]
    book_queue.add(argv, book_folder=data_root / "audiobooks" / "Long")
    book_queue.add(argv[:-6] + ["--video", "cards"], book_folder=data_root / "audiobooks" / "Cards")

    rows = book_studio.job_rows()
    assert all(row["state"] == "waiting" and row["left_s"] and row["work"] for row in rows)
    animated, cards = rows
    assert animated["left_s"] > cards["left_s"]

    now = time.mktime((2026, 11, 15, 10, 0, 0, 0, 0, -1))
    summary = book_studio.queue_summary(rows, now=now)
    assert summary["waiting"] == 2 and summary["total_left"] and summary["estimate_complete"]
    assert animated["ready_at"].startswith(("today", "tomorrow")) and cards["ready_at"]


def test_ready_time_labels() -> None:
    now = time.mktime((2026, 11, 15, 10, 0, 0, 0, 0, -1))
    assert book_studio.when_label(now + 2 * 3600, now) == "today 12:00 PM"
    assert book_studio.when_label(now + 24 * 3600, now) == "tomorrow 10:00 AM"
    assert book_studio.when_label(now + 3 * 86400, now).endswith("10:00 AM")


def test_a_book_with_its_chapters_spoken_needs_less_work(data_root: Path) -> None:
    script = data_root / "book.md"
    script.write_text("# One\n\n" + "Words to speak. " * 3000, encoding="utf-8")
    job = {"argv": ["--text", str(script), "--voice", "piper:en_US-joe-medium", "--video", "cards"]}
    assert book_studio.work_left(job, done=10, total=10) < book_studio.work_left(job)


def test_add_numbers_jobs_and_puts_previews_first(data_root: Path) -> None:
    a = book_queue.add(["--text", "a.md", "--title", "Alpha"])
    b = book_queue.add(["--text", "b.md", "--title", "Beta"], book_folder=data_root / "x")
    p = book_queue.add(["--text", "c.md", "--title", "Gamma"], label="Gamma (preview)", preview=True)
    assert a.startswith("0001-") and b.startswith("0002-") and p.startswith("0000-")
    assert [path.stem for path in book_queue.waiting()][0] == p
    rows = book_queue.list_jobs()
    assert [r["id"] for r in rows] == [p, a, b]
    assert rows[2]["folder"] == str(data_root / "x")
    saved = json.loads((book_queue.queue_dir() / f"{a}.json").read_text(encoding="utf-8"))
    assert Path(saved["argv"][1]).is_absolute()


def test_cancel_resume_and_remove(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(book_queue, "_kill_tree", lambda pid: killed.append(pid))
    job = book_queue.add(["--title", "Book"])
    path = book_queue.queue_dir() / f"{job}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "status": "running", "pid": 4242, "attempts": 2}), encoding="utf-8")
    book_queue.cancel(job)
    assert book_queue.find(job)[0] == "cancelled" and killed == [4242]
    with pytest.raises(book_queue.QueueError):
        book_queue.cancel(job)
    book_queue.resume(job)
    state, path = book_queue.find(job)
    assert state == "waiting" and json.loads(path.read_text(encoding="utf-8"))["attempts"] == 0
    with pytest.raises(book_queue.QueueError):
        book_queue.remove(job)
    book_queue.cancel(job)
    book_queue.remove(job)
    assert book_queue.find(job) is None
    assert book_queue.find("../secrets") is None


def _child(code: str):
    return lambda argv: [sys.executable, "-c", code]


def test_run_job_success_retry_and_give_up(data_root: Path) -> None:
    good = book_queue.add(["--title", "Good"])
    assert book_queue.run_job(book_queue.queue_dir() / f"{good}.json", _child("print('ok')"))
    assert book_queue.find(good)[0] == "done"

    bad = book_queue.add(["--title", "Bad"])
    path = book_queue.queue_dir() / f"{bad}.json"
    for attempt in range(1, book_queue.MAX_ATTEMPTS + 1):
        assert not book_queue.run_job(path, _child("import sys; print('boom'); sys.exit(3)"))
        if attempt < book_queue.MAX_ATTEMPTS:
            assert book_queue.find(bad)[0] == "waiting"
    state, failed = book_queue.find(bad)
    data = json.loads(failed.read_text(encoding="utf-8"))
    assert state == "failed" and data["last_error"] == "boom" and data["attempts"] == book_queue.MAX_ATTEMPTS
    assert "boom" in book_queue.log_tail(bad)


def test_cancel_kills_a_running_job_and_it_is_not_retried(data_root: Path) -> None:
    job = book_queue.add(["--title", "Long"])
    path = book_queue.queue_dir() / f"{job}.json"
    result: list[bool] = []
    import threading

    worker = threading.Thread(target=lambda: result.append(
        book_queue.run_job(path, _child("import time; time.sleep(30)"))))
    worker.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if json.loads(path.read_text(encoding="utf-8")).get("pid"):
            break
        time.sleep(0.1)
    started = time.monotonic()
    book_queue.cancel(job)
    worker.join(20)
    assert result == [True]
    assert time.monotonic() - started < 15
    assert book_queue.find(job)[0] == "cancelled"


# --- Service: progress, preview text, plan ----------------------------------------------


def test_progress_is_weighted_by_estimated_work(data_root: Path) -> None:
    folder = data_root / "book"
    est = {"audio_s": 600, "speak_s": 800, "timing_s": 60, "draw_s": 400, "pack_s": 20}
    progress = book_video.Progress(folder, est)
    seen = []
    for stage, fraction in (("text", 1), ("speaking", 0.5), ("speaking", 1), ("timing", 1),
                            ("drawing", 0.5), ("packaging", 0.5), ("done", 1)):
        progress.update(stage, fraction)
        seen.append(book_video.read_progress(folder)["percent"])
    assert seen == sorted(seen) and seen[-1] == 100
    assert 30 < seen[1] < 40  # half the speaking is about a third of the work
    assert book_video.read_progress(folder)["seconds_left"] is None
    progress.fail("broke")
    data = book_video.read_progress(folder)
    assert data["stage"] == "failed" and data["detail"] == "broke"


def test_preview_script_takes_the_first_sentences() -> None:
    book = book_text.parse_script(SCRIPT, title="Habits", author="", language="en", kind="summary")
    text = book_video.preview_script(book, limit=80)
    assert text.startswith("# The First Idea")
    assert "Second Idea" not in text and len(text) < 200
    assert "Every great change" in text


def test_folders_for_requests(data_root: Path) -> None:
    plain = book_video.folder_for(book_video.BookVideoRequest(name="Atomic Habits - en"))
    preview = book_video.folder_for(book_video.BookVideoRequest(name="Atomic Habits - en", preview=True))
    assert plain.name == "Atomic Habits - en" and preview.name == "Atomic Habits - en - preview"
    assert plain.parent == preview.parent == data_root / "audiobooks"


@pytest.mark.asyncio
async def test_plan_only_writes_plan_run_file_and_progress(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_detect():
        from app.services.ffmpeg import FFmpegStatus
        return FFmpegStatus(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe")

    monkeypatch.setattr("app.services.ffmpeg.detect", fake_detect)
    script = data_root / "s.md"
    script.write_text(SCRIPT, encoding="utf-8")
    model = data_root / "voices" / "en_US-joe-medium" / "en_US-joe-medium.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x")
    request = book_video.BookVideoRequest(text_path=script, title="Habits", kind="summary", template="noir",
                                          name="habits", plan_only=True, argv=["--text", str(script)])
    lines: list[str] = []
    assert await book_video.make(request, lines.append) == 0
    folder = data_root / "audiobooks" / "habits"
    assert "noir template" in (folder / "plan.txt").read_text(encoding="utf-8")
    run = json.loads((folder / "make_book.json").read_text(encoding="utf-8"))
    assert run["argv"] == ["--text", str(script)] and run["request"]["template"] == "noir"
    assert book_video.read_progress(folder)["stage"] == "done"
    assert any("The First Idea" in line for line in lines)


def test_package_only_reuses_a_finished_audiobook(data_root: Path) -> None:
    from app.services import audiobook

    folder = data_root / "audiobooks" / "old"
    folder.mkdir(parents=True)
    voice = {"engine": "piper", "model": "en_US-joe-medium"}
    (folder / "script.txt").write_text("# Introduction\n\nHello there.\n", encoding="utf-8")
    request = book_video.BookVideoRequest(out=folder, title="Old Book", author="Someone", gutenberg=132,
                                          package_only=True)
    with pytest.raises(book_video.BookVideoError):
        book_video.recorded_book(request)
    (folder / "book.json").write_text(json.dumps({"status": audiobook.DONE, "voice": voice}), encoding="utf-8")
    (folder / "audiobook.mp3").write_bytes(b"x")
    book, full, found = book_video.recorded_book(request)
    assert full.startswith("# Introduction") and found == voice
    assert book.title == "Old Book" and book.kind == "public-domain" and book.source == "gutenberg:132"


def test_upload_details_is_one_file_with_the_listing(data_root: Path) -> None:
    from app.services import book_package as pack

    text = pack.upload_details_text(
        title="Meditations by Marcus Aurelius | Book Summary",
        description="A complete summary.\n\nCHAPTERS\n0:00 Introduction",
        tags=["Meditations", "book summary"], duration="24 minutes", category="Education",
        language="en", made_for_kids=False, altered="Yes (AI narration voice)",
        video="audiobook.mp4", thumbnail="thumbnail.jpg", subtitles="subtitles.srt",
        book_title="Meditations", author="Marcus Aurelius", kind="summary", channel="BookInAFlash",
        voice="Narrated with an AI voice (Kokoro: bm_george).", template="Stoic",
        cover="Louvre bust, public domain", source="Original summary",
        thumbnail_text="MEDITATIONS / MARCUS AURELIUS / BOOK SUMMARY • 24 MINUTES",
    )
    folder = data_root / "audiobooks" / "meditations"
    folder.mkdir(parents=True)
    path = pack.write_upload_details(folder, text)
    saved = path.read_text(encoding="utf-8")
    assert path.name == "youtube-upload-details.txt"
    assert saved.startswith("YOUTUBE UPLOAD DETAILS")
    for heading in ("TITLE", "DESCRIPTION", "TAGS", "LISTING", "ABOUT THIS VIDEO"):
        assert heading in saved
    assert "0:00 Introduction" in saved and "Meditations, book summary" in saved
    assert "not the full book" in saved and "under 100 characters" in saved


def test_clean_work_files_keeps_the_results(data_root: Path) -> None:
    folder = data_root / "audiobooks" / "b"
    (folder / "chapters").mkdir(parents=True)
    (folder / "video-work" / "segments").mkdir(parents=True)
    (folder / "chapters" / "001.wav").write_bytes(b"0" * 1000)
    (folder / "chapters" / "001.words.json").write_text("{}", encoding="utf-8")
    (folder / "video-work" / "segments" / "seg_00000.mp4").write_bytes(b"0" * 500)
    (folder / "audiobook.mp4").write_bytes(b"v")
    assert book_video.work_bytes(folder) == 1500
    assert book_video.clean_work_files(folder) == 1500
    assert (folder / "audiobook.mp4").is_file() and (folder / "chapters" / "001.words.json").is_file()
    assert not (folder / "video-work").exists() and book_video.work_bytes(folder) == 0


def test_make_book_cli_builds_the_same_request(data_root: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import make_book

    script = data_root / "s.md"
    script.write_text(SCRIPT, encoding="utf-8")
    argv = ["--text", str(script), "--title", "Habits", "--template", "rose", "--preview", "--no-whisper"]
    args = make_book.parser().parse_args(argv)
    request = make_book.request_from(args, argv)
    assert request.template == "rose" and request.preview and not request.use_whisper
    assert request.style == "" and request.accent == ""
    look = book_video.look_from(request)
    assert look.style == video_templates.get("rose").ring and look.theme.id == "rose"


# --- The page's logic -------------------------------------------------------------------------


def test_check_script_reports_length_chapters_and_problems(data_root: Path) -> None:
    result = book_studio.check_script(SCRIPT, title="Habits", voice="kokoro:bm_george", template="growth")
    assert result["ok"] and result["words"] > 40
    assert result["chapters"] == ["Introduction", "The First Idea", "The Second Idea", "Closing"]
    assert any("very short" in w for w in result["warnings"])
    untitled = book_studio.check_script(SCRIPT, title="")
    assert not untitled["ok"] and untitled["errors"]
    urdu = book_studio.check_script(SCRIPT, title="X", language="ur", voice="kokoro:bm_george")
    assert not urdu["animated"] and any("English only" in w for w in urdu["warnings"])
    assert book_studio.check_script("  ", title="X")["empty"]


def test_covers_are_checked_and_kept_as_jpeg(data_root: Path) -> None:
    import io

    from PIL import Image

    with pytest.raises(book_studio.StudioError):
        book_studio.save_cover(b"not a picture", "x.png")
    small = io.BytesIO()
    Image.new("RGB", (100, 100)).save(small, "PNG")
    with pytest.raises(book_studio.StudioError):
        book_studio.save_cover(small.getvalue(), "tiny.png")
    big = io.BytesIO()
    Image.new("RGB", (3000, 1500), "red").save(big, "PNG")
    path = book_studio.save_cover(big.getvalue(), "My Cover.png")
    assert path.suffix == ".jpg" and max(Image.open(path).size) == book_studio.COVER_MAX_SIDE
    assert book_studio.save_cover(big.getvalue(), "My Cover.png") == path
    rel = book_studio.cover_rel(path)
    assert book_studio.cover_from(rel) == path
    with pytest.raises(book_studio.StudioError):
        book_studio.cover_from("../outside.jpg")


@pytest.mark.asyncio
async def test_commons_fetch_refuses_other_hosts(data_root: Path) -> None:
    with pytest.raises(book_studio.StudioError):
        await book_studio.commons_fetch("https://example.com/a.jpg", "")


def test_queue_book_saves_script_job_and_settings(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(book_queue, "ensure_runner", lambda: True)
    cover = book_studio.save_cover(write_image(data_root / "c.jpg", (800, 800)).read_bytes(), "c.jpg")
    order = book_studio.Order(script=SCRIPT, title="Atomic Habits", author="James Clear", template="boardroom",
                              voice="edge:en-US-AndrewMultilingualNeural", cover=book_studio.cover_rel(cover),
                              accent="#123456", preview=True)
    result = book_studio.queue_book(order)
    state, path = book_queue.find(result["job"])
    data = json.loads(path.read_text(encoding="utf-8"))
    argv = data["argv"]
    assert state == "waiting" and data["preview"] and result["job"].startswith("0000-")
    assert argv[argv.index("--template") + 1] == "boardroom" and "--preview" in argv
    assert argv[argv.index("--art") + 1] == str(cover) and argv[argv.index("--accent") + 1] == "#123456"
    assert Path(argv[argv.index("--text") + 1]).read_text(encoding="utf-8") == SCRIPT
    assert data["folder"].endswith("Atomic Habits - en - preview")
    saved = book_studio.load_settings()
    assert saved["template"] == "boardroom" and saved["voice"] == "edge:en-US-AndrewMultilingualNeural"
    for bad in (dict(title=""), dict(template="nope"), dict(accent="red"), dict(voice="robot:x"),
                dict(script=" ")):
        with pytest.raises(book_studio.StudioError):
            book_studio.queue_book(book_studio.Order(**{"script": SCRIPT, "title": "T", **bad}))


def _finished_book(folder: Path) -> None:
    (folder / "chapters").mkdir(parents=True, exist_ok=True)
    (folder / "chapters" / "001.wav").write_bytes(b"0" * 2_000_000)
    (folder / "audiobook.mp4").write_bytes(b"v" * 1000)
    (folder / "audiobook.mp3").write_bytes(b"a")
    (folder / "thumbnail.jpg").write_bytes(b"j")
    (folder / "title.txt").write_text("Atomic Habits | Book Summary", encoding="utf-8")
    (folder / "description.txt").write_text("A complete summary.", encoding="utf-8")
    (folder / "tags.txt").write_text("habits, summary", encoding="utf-8")
    (folder / "qa.json").write_text(json.dumps({"duration_s": 300, "loudness_ok": True, "loudness_note": "ok",
                                                "long_silences": []}), encoding="utf-8")


def test_job_rows_show_progress_and_the_upload_kit(data_root: Path) -> None:
    done_folder = data_root / "audiobooks" / "Done - en"
    _finished_book(done_folder)
    done = book_queue.add(["--title", "Done"], book_folder=done_folder)
    book_queue.run_job(book_queue.queue_dir() / f"{done}.json", _child("pass"))
    live_folder = data_root / "audiobooks" / "Live - en"
    book_video.Progress(live_folder, None).update("drawing", 0.5, "Segment 2 of 4")
    book_queue.add(["--title", "Live"], book_folder=live_folder)
    rows = {r["title"]: r for r in book_studio.job_rows()}
    assert rows["Live"]["state"] == "waiting" and rows["Live"]["progress"]["detail"] == "Segment 2 of 4"
    kit = rows["Done"]["kit"]
    assert kit["title"] == "Atomic Habits | Book Summary" and kit["tags"] == "habits, summary"
    assert kit["video"] == "audiobooks/Done - en/audiobook.mp4" and kit["duration"] == "5 minutes"
    assert rows["Done"]["work_mb"] == 2
    assert book_studio.disk_status()["work_gb"] == pytest.approx(0.0, abs=0.01)
    with pytest.raises(book_studio.StudioError):
        book_studio.clean(next(r["id"] for r in book_studio.job_rows() if r["title"] == "Live"))
    assert book_studio.clean(done) == 2_000_000
    assert (done_folder / "audiobook.mp4").is_file()


# --- Routes ---------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    monkeypatch.setattr(book_queue, "ensure_runner", lambda: False)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def test_menu_groups_cover_every_page_once_with_cloud_folded() -> None:
    from app.routes.common import NAV_GROUPS, NAV_ITEMS

    hrefs = [item["href"] for item in NAV_ITEMS]
    assert len(hrefs) == len(set(hrefs)) == 18
    assert NAV_GROUPS[0]["label"].startswith("Books")
    folded = [group for group in NAV_GROUPS if group["collapsed"]]
    assert len(folded) == 1
    assert {"/remote", "/providers", "/accounts"} <= {item["href"] for item in folded[0]["items"]}


@pytest.mark.asyncio
async def test_menu_opens_the_folded_group_only_for_its_own_pages(client: AsyncClient) -> None:
    body = (await client.get("/book-video")).text
    assert body.count('<details class="nav-group" open>') == 3
    assert "Cloud &amp; Remote GPU" in body


@pytest.mark.asyncio
async def test_page_is_first_in_the_menu_and_renders(client: AsyncClient) -> None:
    from app.routes.common import NAV_ITEMS

    assert NAV_ITEMS[0]["href"] == "/book-video"
    response = await client.get("/book-video")
    assert response.status_code == 200
    body = response.text
    assert "Make Video" in body and "Quick preview" in body and "boardroom" in body
    assert (await client.get("/partials/book-video/jobs")).status_code == 200
    assert (await client.get("/partials/book-video/voice-status")).status_code == 200
    assert (await client.get("/partials/book-video/library")).status_code == 200
    assert 'hx-get="/partials/queue-strip"' in body


@pytest.mark.asyncio
async def test_every_page_shows_the_queue_strip_while_a_book_waits(client: AsyncClient, data_root: Path) -> None:
    empty = await client.get("/partials/queue-strip")
    assert empty.status_code == 200 and "Open Book Video" not in empty.text
    script = data_root / "book.md"
    script.write_text(SCRIPT, encoding="utf-8")
    book_queue.add(["--text", str(script), "--title", "Habits", "--video", "cards"],
                   book_folder=data_root / "audiobooks" / "Habits")
    strip = (await client.get("/partials/queue-strip")).text
    assert "1 waiting" in strip and "all ready around" in strip and "Open Book Video" in strip


@pytest.mark.asyncio
async def test_check_and_create_through_the_page(client: AsyncClient) -> None:
    form = {"script": SCRIPT, "title": "Habits", "kind": "summary", "language": "en",
            "voice": "kokoro:bm_george", "template": "stoic", "height": "720", "video": "animated"}
    check = await client.post("/book-video/check", data=form)
    assert check.status_code == 200 and "The Second Idea" in check.text
    created = await client.post("/book-video/create", data={**form, "preview": "0"})
    assert created.status_code == 200 and "Habits" in created.text
    assert "in the queue" in created.headers["X-AVM-Flash"]
    assert "Copy path" in created.text and "Open folder" in created.text
    job = book_queue.list_jobs()[0]["id"]
    cancelled = await client.post(f"/book-video/jobs/{job}/cancel")
    assert cancelled.status_code == 200 and book_queue.find(job)[0] == "cancelled"
    resumed = await client.post(f"/book-video/jobs/{job}/resume")
    assert resumed.status_code == 200 and book_queue.find(job)[0] == "waiting"
    bad = await client.post("/book-video/create", data={**form, "title": ""})
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_a_script_over_one_megabyte_is_accepted(client: AsyncClient) -> None:
    """Starlette's default form limit is 1024 KB. A full novel exceeds it once encoded."""
    script = "# Chapter One\n\n" + ("word " * 230_000)
    assert len(script.encode("utf-8")) > 1024 * 1024
    check = await client.post("/book-video/check", data={"script": script, "title": "Long Book"})
    assert check.status_code == 200
    assert "Field exceeded" not in check.text
    assert "230," in check.text


@pytest.mark.asyncio
async def test_cover_upload_and_template_preview(client: AsyncClient, data_root: Path) -> None:
    picture = write_image(data_root / "p.jpg", (700, 700), "purple")
    uploaded = await client.post("/book-video/cover/upload",
                                 files={"cover_file": ("p.jpg", picture.read_bytes(), "image/jpeg")})
    assert uploaded.status_code == 200
    rel = json.loads(uploaded.headers["HX-Trigger"])["cover-changed"]["rel"]
    assert rel.startswith("book-video/covers/") and rel in uploaded.text
    preview = await client.get("/book-video/template-preview/rose", params={"cover": rel})
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    assert (await client.get("/book-video/template-preview/nope")).status_code == 404
    cleared = await client.post("/book-video/cover/clear")
    assert json.loads(cleared.headers["HX-Trigger"])["cover-changed"]["rel"] == ""
    refused = await client.post("/book-video/cover/upload", files={"cover_file": ("x.txt", b"hello", "text/plain")})
    assert refused.status_code == 400


@pytest.mark.asyncio
async def test_voice_demo_route_refuses_other_files(client: AsyncClient, data_root: Path) -> None:
    folder = voice_catalog.demo_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "kokoro-x.mp3").write_bytes(b"mp3")
    assert (await client.get("/book-video/voice-demo/kokoro-x.mp3")).status_code == 200
    assert (await client.get("/book-video/voice-demo/..%2Fapp.db")).status_code == 404
    assert (await client.get("/book-video/voice-demo/missing.mp3")).status_code == 404
