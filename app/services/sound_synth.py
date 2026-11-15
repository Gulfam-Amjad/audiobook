"""Sound effects and ambience made on this machine, from noise and sine
waves, with numpy.

Nothing here is a recording of anyone's work, so there is no licence to
meet and nothing for Content ID to match. It covers the cues a
documentary needs most: the whoosh on a fast cut, a riser into a reveal,
a low boom on a chapter card, a clock tick, a drone under the cold open,
and beds of sea, wind, rain, radio static, a quiet room, night, fire and
distant battle.

Spectral shaping is done block by block (a short FFT, a moving band, then
overlap-add), which gives sweeps and slowly changing weather without a
per-sample Python loop.
"""

from __future__ import annotations

import wave
from pathlib import Path

RATE = 48_000
BLOCK = 2048
HOP = BLOCK // 4


def _np():
    import numpy as np

    return np


def _shaped_noise(seconds: float, band, rng, *, stereo_spread: float = 0.0):
    """Noise whose spectrum follows ``band(t) -> (centre_hz, width_octaves, gain)``."""
    np = _np()
    total = int(seconds * RATE)
    frames = max(1, (total - BLOCK) // HOP + 2)
    out = np.zeros((total + BLOCK, 2))
    window = np.hanning(BLOCK)
    freqs = np.fft.rfftfreq(BLOCK, 1 / RATE)
    log_f = np.log2(np.maximum(freqs, 1.0))
    for index in range(frames):
        t = index * HOP / RATE
        centre, width, gain = band(t)
        shape = gain * np.exp(-0.5 * ((log_f - np.log2(max(centre, 20.0))) / max(width, 0.05)) ** 2)
        for channel in range(2):
            spectrum = np.fft.rfft(rng.standard_normal(BLOCK) * window)
            pan = 1.0 + (stereo_spread * (1 if channel else -1) * np.sin(2 * np.pi * t * 0.07))
            block = np.fft.irfft(spectrum * shape * pan, BLOCK) * window
            start = index * HOP
            out[start:start + BLOCK, channel] += block
    return out[:total]


def _env(seconds: float, attack: float, release: float, curve: float = 2.0):
    np = _np()
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    env = np.ones(n)
    if attack > 0:
        env *= np.clip(t / attack, 0, 1) ** curve
    if release > 0:
        env *= np.clip((seconds - t) / release, 0, 1) ** curve
    return env


def _normalise(signal, peak_db: float):
    np = _np()
    peak = float(np.max(np.abs(signal))) or 1.0
    return signal / peak * (10 ** (peak_db / 20))


def write_wav(signal, destination: Path) -> Path:
    np = _np()
    if signal.ndim == 1:
        signal = np.stack([signal, signal], axis=1)
    data = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(data.tobytes())
    return destination


# ---------------------------------------------------------------------------
# Cues
# ---------------------------------------------------------------------------


def whoosh(seed: int = 0, seconds: float = 0.9):
    np = _np()
    rng = np.random.default_rng(seed)
    peak = 0.45 + 0.1 * rng.random()

    def band(t):
        p = t / seconds
        centre = 350 + 2600 * np.exp(-((p - peak) / 0.22) ** 2)
        return centre, 0.9, 1.0

    noise = _shaped_noise(seconds, band, rng)
    t = np.arange(noise.shape[0]) / RATE
    env = np.exp(-((t / seconds - peak) / 0.2) ** 2)
    pan = np.clip(t / seconds, 0, 1)
    noise[:, 0] *= env * (1.2 - pan)
    noise[:, 1] *= env * (0.2 + pan)
    return _normalise(noise, -3)


def riser(seed: int = 0, seconds: float = 2.6):
    np = _np()
    rng = np.random.default_rng(seed)

    def band(t):
        p = min(t / seconds, 1.0)
        return 200 * (30 ** p), 0.6 + 0.4 * p, 1.0

    noise = _shaped_noise(seconds, band, rng, stereo_spread=0.3)
    n = noise.shape[0]
    t = np.arange(n) / RATE
    p = t / seconds
    tone = np.sin(2 * np.pi * np.cumsum(90 * (8 ** p)) / RATE) * 0.35
    signal = noise + tone[:, None]
    env = (p ** 2.2) * np.clip((seconds - t) / 0.04, 0, 1)
    return _normalise(signal * env[:, None], -3)


def boom(seed: int = 0, seconds: float = 2.4):
    np = _np()
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    freq = 32 + 40 * np.exp(-t * 6)
    body = np.sin(2 * np.pi * np.cumsum(freq) / RATE) * np.exp(-t * 2.2)

    def band(tt):
        return 180, 1.2, float(np.exp(-tt * 9))

    thump = _shaped_noise(seconds, band, rng)[:, 0] * 0.6
    signal = body + thump
    return _normalise(np.stack([signal, signal * 0.97], axis=1), -2)


def impact(seed: int = 0, seconds: float = 1.6):
    np = _np()
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    freq = 48 + 90 * np.exp(-t * 14)
    body = np.sin(2 * np.pi * np.cumsum(freq) / RATE) * np.exp(-t * 3.5)

    def band(tt):
        return 2200 * np.exp(-tt * 3) + 300, 1.6, float(np.exp(-tt * 14))

    crack = _shaped_noise(seconds, band, rng)
    signal = body[:, None] * 0.9 + crack * 0.7
    return _normalise(signal, -1.5)


def tick(seed: int = 0, seconds: float = 1.0):
    """A clock: one tick a second, the second one slightly lower."""
    np = _np()
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    click = np.zeros(n)
    for start, pitch in ((0.0, 3200), (0.5, 2600)):
        local = t - start
        mask = local >= 0
        click[mask] += np.sin(2 * np.pi * pitch * local[mask]) * np.exp(-local[mask] * 160)
    return _normalise(click, -6)


def drone(seed: int = 0, seconds: float = 20.0):
    np = _np()
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    root = 55.0 * (2 ** (rng.integers(-2, 3) / 12))
    signal = np.zeros(n)
    for ratio, level in ((1.0, 1.0), (1.5, 0.45), (2.0, 0.35), (3.01, 0.12)):
        wobble = 0.4 * np.sin(2 * np.pi * t * (0.05 + 0.03 * ratio))
        signal += level * np.sin(2 * np.pi * np.cumsum(root * ratio + wobble) / RATE)
    signal *= 0.75 + 0.25 * np.sin(2 * np.pi * t * 0.11)

    def band(tt):
        return 140, 0.8, 0.5

    air = _shaped_noise(seconds, band, rng, stereo_spread=0.4) * 0.5
    stereo = np.stack([signal, np.roll(signal, 240)], axis=1) + air
    return _normalise(stereo * _env(seconds, 2.0, 2.0, 1.0)[:, None], -6)


# ---------------------------------------------------------------------------
# Ambience beds (loopable: they fade in and out over a second)
# ---------------------------------------------------------------------------


def _bed(seconds, band, rng, spread=0.5):
    np = _np()
    signal = _shaped_noise(seconds, band, rng, stereo_spread=spread)
    return signal * _env(seconds, 1.0, 1.0, 1.0)[:, None]


def ambience(tag: str, seed: int = 0, seconds: float = 45.0):
    np = _np()
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE

    if tag == "sea":
        waves = 0.55 + 0.45 * np.sin(2 * np.pi * t / 7.5) ** 2

        def band(tt):
            return 500 + 400 * np.sin(2 * np.pi * tt / 7.5) ** 2, 1.6, 1.0

        signal = _bed(seconds, band, rng) * waves[:, None]
    elif tag == "wind":
        gust = 0.5 + 0.5 * np.sin(2 * np.pi * t / 11) * np.sin(2 * np.pi * t / 4.3)

        def band(tt):
            return 380 + 260 * np.sin(2 * np.pi * tt / 9.0), 0.5, 1.0

        signal = _bed(seconds, band, rng) * (0.4 + 0.6 * np.abs(gust))[:, None]
    elif tag == "rain":
        def band(tt):
            return 4200, 1.4, 1.0

        signal = _bed(seconds, band, rng)
        drops = (rng.random((n, 2)) > 0.9993) * rng.standard_normal((n, 2)) * 3.0
        signal += np.convolve(drops[:, 0], np.exp(-np.arange(200) / 30), "same")[:, None] * 0.3
    elif tag == "radio":
        def band(tt):
            return 1400, 0.9, 1.0

        signal = _bed(seconds, band, rng, spread=0.0) * 0.6
        # The two-tone beep heard on NASA mission audio, now and then.
        for start in np.arange(3.0, seconds - 1.0, 9.0 + rng.random() * 4):
            for offset, pitch in ((0.0, 2525.0), (2.2, 2475.0)):
                s = int((start + offset) * RATE)
                length = int(0.25 * RATE)
                if s + length < n:
                    tone = np.sin(2 * np.pi * pitch * np.arange(length) / RATE) * 0.5
                    signal[s:s + length] += tone[:, None]
    elif tag == "room":
        def band(tt):
            return 120, 1.0, 1.0

        signal = _bed(seconds, band, rng) * 0.5
    elif tag == "night":
        def band(tt):
            return 220, 1.0, 1.0

        signal = _bed(seconds, band, rng) * 0.4
        chirp = np.sin(2 * np.pi * 4600 * t) * (np.sin(2 * np.pi * 28 * t) > 0.6)
        chirp *= (np.sin(2 * np.pi * t / 2.3) > 0.2) * 0.08
        signal += np.stack([chirp, np.roll(chirp, 9000)], axis=1)
    elif tag == "fire":
        def band(tt):
            return 260, 1.2, 1.0

        signal = _bed(seconds, band, rng) * 0.6
        pops = (rng.random((n, 2)) > 0.9985) * rng.standard_normal((n, 2))
        signal += np.apply_along_axis(lambda c: np.convolve(c, np.exp(-np.arange(120) / 15), "same"), 0, pops) * 0.6
    elif tag == "battle":
        def band(tt):
            return 160, 1.4, 1.0

        signal = _bed(seconds, band, rng) * 0.5
        for start in np.sort(rng.random(int(seconds / 2.5)) * (seconds - 3)):
            piece = boom(int(start * 1000), 2.4) * (0.25 + 0.5 * rng.random())
            s = int(start * RATE)
            signal[s:s + piece.shape[0]] += piece[: n - s]
    elif tag in ("crowd", "city"):
        # A murmur of many voices cannot be synthesised honestly from
        # noise; a low band of traffic-like rumble is all this can be.
        def band(tt):
            return 300 if tag == "city" else 700, 1.3, 1.0

        signal = _bed(seconds, band, rng) * 0.5
    else:
        raise ValueError(f"No ambience called {tag}")
    return _normalise(signal, -10)


CUES = {"whoosh": whoosh, "riser": riser, "boom": boom, "impact": impact, "tick": tick, "drone": drone}
AMBIENCE_TAGS = ("sea", "wind", "rain", "radio", "room", "night", "fire", "battle", "city")
