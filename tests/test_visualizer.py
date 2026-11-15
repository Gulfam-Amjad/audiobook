import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.services import visualizer as vz
from app.services import word_timing as wt


def _chunk(lines: list[str], start: float = 0.2, rate: float = 0.3):
    sentences, t = [], start
    for line in lines:
        tokens = line.split()
        times = wt.spread(tokens, t, t + rate * len(tokens))
        sentences.append([[w, a, b] for w, (a, b) in zip(tokens, times)])
        t += rate * len(tokens) + 0.4
    return {"sentences": sentences}, t


def _timeline() -> vz.Timeline:
    first, end1 = _chunk(["Introduction", "Every habit begins with a small cue.", "Then comes the routine."])
    title = "Chapter Four. The Red-Headed League"
    second, end2 = _chunk(["Chapter Four.", "The Red-Headed League", "Change the routine and keep the cue."])
    return vz.build_timeline([("Introduction", 0.0, first), (title, end1, second)], end1 + end2 + 0.5)


def test_timeline_offsets_chapters_and_headings():
    tl = _timeline()
    assert tl.chapter_titles == ["Introduction", "Chapter Four. The Red-Headed League"]
    assert tl.chapter_starts[1] > 0
    assert tl.starts == sorted(tl.starts)
    headings = [s.heading for s in tl.sentences]
    # The second title is spoken as two sentences, both part of the heading.
    assert headings == [True, False, False, True, True, False]
    second_chapter = [s for s in tl.sentences if s.chapter == 1]
    assert second_chapter[0].start_s >= tl.chapter_starts[1]


def _renderer(style: str) -> vz.Renderer:
    tl = _timeline()
    look = vz.Look(style=style, height=360, fps=12)
    frames = int(tl.duration_s * look.fps) + 1
    amp = np.linspace(0, 1, frames, dtype=np.float32)
    bands = np.full((frames, vz.BANDS), 0.5, np.float32)
    return vz.Renderer(tl, look, amp, bands, art=None, title="Test", author="A. Writer",
                       channel="Channel", badge="Book Summary")


@pytest.mark.parametrize("style", vz.STYLES)
def test_a_frame_depends_only_on_its_time(style: str, data_root: Path):
    """Segments are drawn independently on resume, so history must not leak into a frame."""
    straight = _renderer(style)
    for f in range(40):
        straight.draw(f)
    expected = straight.draw(40).copy()
    jumped = _renderer(style)
    jumped.draw(5)
    assert np.array_equal(jumped.draw(40), expected)
    assert expected.shape == (360, 640, 3)


def test_the_ring_grows_with_loudness(data_root: Path):
    r = _renderer("balloon")
    quiet, loud = r.levels[0], r.levels[-1]
    accent = np.array(r.accent, np.float32)

    def warmth(image: np.ndarray) -> float:
        return float(-np.abs(image.astype(np.float32) - accent).sum(axis=2).mean())

    assert warmth(loud) > warmth(quiet)


def test_estimate_uses_a_default_before_anything_is_measured(data_root: Path):
    look = vz.Look()
    assert vz.measured_speed(look) is None
    assert vz.estimate_seconds(3600, look) > 0


def test_parse_accent():
    assert vz.parse_accent("#ffb347") == (255, 179, 71)
    with pytest.raises(ValueError):
        vz.parse_accent("orange")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs FFmpeg")
def test_render_resumes_from_the_missing_segment(data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    tl = _timeline()
    mp3 = tmp_path / "a.mp3"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={tl.duration_s:.2f}", "-c:a", "libmp3lame", str(mp3)],
                   check=True)
    monkeypatch.setattr(vz, "SEGMENT_S", 2)
    look = vz.Look(style="bars", height=360, fps=8)
    out, work = tmp_path / "out.mp4", tmp_path / "work"
    kwargs = dict(art=None, title="T", author="", channel="C", badge="Book Summary")
    vz.render_video(tl, look, mp3, out, work, ffmpeg, ffprobe, **kwargs)
    segments = sorted((work / "segments").glob("seg_*.mp4"))
    assert len(segments) >= 3 and out.is_file()
    kept = {p.name: p.stat().st_mtime_ns for p in segments}
    segments[1].unlink()
    lines: list[str] = []
    vz.render_video(tl, look, mp3, out, work, ffmpeg, ffprobe, log_line=lines.append, **kwargs)
    drawn = [line for line in lines if line.startswith("Video segment")]
    assert len(drawn) == 1 and "segment 2 of" in drawn[0]
    for path in sorted((work / "segments").glob("seg_*.mp4")):
        if path.name != segments[1].name:
            assert path.stat().st_mtime_ns == kept[path.name]
    assert abs(vz._ffprobe_duration(ffprobe, out) - tl.duration_s) < 1.0
