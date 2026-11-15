"""Cut a long film into the shots that are actually in it.

A 28-minute download is not one picture. PySceneDetect finds the cuts on
the processor, with no graphics card, and returns each shot's start and
end. Matching a sentence to the shot that shows its noun still has to be
looked at: a CLIP-style matcher needs a model this machine's 8 GB of
memory cannot hold next to the editor, so that step is not installed.
"""

from __future__ import annotations

from pathlib import Path


class ShotDetectError(Exception):
    """The film could not be cut into shots."""


def detect_shots(path: Path, *, threshold: float = 27.0) -> list[dict[str, float]]:
    """Return each shot as ``start_s`` and ``end_s``, in order.

    ``threshold`` is PySceneDetect's content-cut sensitivity. The default
    is the library's own. A higher number finds fewer cuts.
    """
    film = Path(path)
    if not film.is_file():
        raise ShotDetectError(f"There is no film at {film}.")
    try:
        from scenedetect import ContentDetector, detect
    except ImportError as exc:
        raise ShotDetectError(
            "PySceneDetect is not installed, so the film cannot be cut into shots."
        ) from exc

    try:
        found = detect(str(film), ContentDetector(threshold=threshold))
    except Exception as exc:
        raise ShotDetectError(f"The film could not be read: {exc}") from exc

    shots: list[dict[str, float]] = []
    for start, end in found:
        start_s = round(float(start.get_seconds()), 3)
        end_s = round(float(end.get_seconds()), 3)
        if end_s > start_s:
            shots.append({"start_s": start_s, "end_s": end_s})
    return shots
