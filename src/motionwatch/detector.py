"""Frame-differencing motion detector based on a running-average background."""

from __future__ import annotations

from typing import List, Sequence, Tuple

import cv2
import numpy as np

Box = Tuple[int, int, int, int]


class MotionDetector:
    def __init__(
        self,
        threshold: int = 25,
        min_area: int = 1500,
        resize_width: int = 640,
        alpha: float = 0.05,
        warmup_frames: int = 25,
        ignore_regions: Sequence[Tuple[float, float, float, float]] = (),
    ):
        self.threshold = threshold
        self.min_area = min_area
        self.resize_width = resize_width
        self.alpha = alpha
        self.warmup_frames = warmup_frames
        self.ignore_regions = list(ignore_regions)
        self.reset()

    def reset(self) -> None:
        self._avg = None
        self._mask = None
        self._seen = 0

    def _prepare(self, frame: np.ndarray) -> Tuple[np.ndarray, float]:
        h, w = frame.shape[:2]
        scale = self.resize_width / float(w) if w > self.resize_width else 1.0
        if scale != 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        return gray, scale

    def _build_mask(self, shape) -> np.ndarray:
        h, w = shape
        mask = np.full((h, w), 255, dtype=np.uint8)
        for x, y, rw, rh in self.ignore_regions:
            x0, y0 = int(x * w), int(y * h)
            x1, y1 = int((x + rw) * w), int((y + rh) * h)
            mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = 0
        return mask

    def process(self, frame: np.ndarray) -> List[Box]:
        """Return bounding boxes (in original frame coordinates) of motion."""
        gray, scale = self._prepare(frame)
        if self._avg is None or self._avg.shape != gray.shape:
            self._avg = gray.astype("float")
            self._mask = self._build_mask(gray.shape)
            self._seen = 1
            return []

        delta = cv2.absdiff(gray, cv2.convertScaleAbs(self._avg))
        cv2.accumulateWeighted(gray, self._avg, self.alpha)
        self._seen += 1
        if self._seen <= self.warmup_frames:
            return []

        _, thresh = cv2.threshold(delta, self.threshold, 255, cv2.THRESH_BINARY)
        thresh = cv2.bitwise_and(thresh, self._mask)
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        boxes: List[Box] = []
        inv = 1.0 / scale
        for c in contours:
            if cv2.contourArea(c) < self.min_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            boxes.append((int(x * inv), int(y * inv), int(w * inv), int(h * inv)))
        return boxes


def annotate(frame: np.ndarray, boxes: Sequence[Box], label: str) -> np.ndarray:
    out = frame.copy()
    for x, y, w, h in boxes:
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 0, 255), 2)
    cv2.rectangle(out, (0, 0), (out.shape[1], 28), (0, 0, 0), -1)
    cv2.putText(out, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def encode_jpeg(frame: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("failed to encode JPEG")
    return buf.tobytes()
