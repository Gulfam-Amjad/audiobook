"""Where the face is in a still, so a push can end on it.

YuNet (OpenCV Zoo, MIT licence, 232 KB) runs on the processor in a
fraction of a second per photo. If its model file is missing, OpenCV's
bundled Haar cascade is used instead. Either way the answer is a point,
or None when nothing was found, and a None means a centred push.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
#: Photos are shrunk to this width before detection. Archive scans are
#: often 6000 pixels wide, and the detector does not need that.
DETECT_WIDTH = 960
MIN_SCORE = 0.7


def yunet_path() -> Path:
    return get_settings().data_root / "models" / "yunet" / YUNET_FILE


def find_focus(path: Path) -> tuple[float, float] | None:
    """The centre of the largest face, as fractions of width and height."""
    try:
        import cv2
    except ImportError:
        return None

    image = cv2.imread(str(path))
    if image is None:
        return None
    height, width = image.shape[:2]
    if width > DETECT_WIDTH:
        scale = DETECT_WIDTH / width
        image = cv2.resize(image, (DETECT_WIDTH, max(1, round(height * scale))))
        height, width = image.shape[:2]

    boxes: list[tuple[float, float, float, float]] = []
    model = yunet_path()
    if model.exists() and hasattr(cv2, "FaceDetectorYN"):
        try:
            detector = cv2.FaceDetectorYN.create(str(model), "", (width, height), MIN_SCORE)
            _, faces = detector.detect(image)
            if faces is not None:
                boxes = [(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in faces]
        except cv2.error as exc:
            log.warning("YuNet failed on %s: %s", path, exc)
    # OpenCV 5 no longer ships the Haar classifier in the main package.
    if not boxes and hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
        cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        classifier = cv2.CascadeClassifier(str(cascade))
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        found = classifier.detectMultiScale(grey, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40))
        boxes = [(float(x), float(y), float(w), float(h)) for x, y, w, h in found]
    if not boxes:
        return None

    x, y, w, h = max(boxes, key=lambda box: box[2] * box[3])
    return round((x + w / 2) / width, 4), round((y + h / 2) / height, 4)
