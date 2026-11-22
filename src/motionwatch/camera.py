"""Per-camera worker thread: capture, schedule gating, detection, alerting."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import cv2

from .config import CameraConfig, redact_url
from .detector import MotionDetector, annotate, encode_jpeg
from .notifier import Alert

log = logging.getLogger(__name__)

# Prefer TCP for RTSP: far fewer corrupted frames on Wi-Fi cameras; 10 s socket timeout.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;10000000")

PREVIEW_EVERY = 2.0  # seconds between preview frames kept for the web UI
PREVIEW_WIDTH = 640


def open_capture(url: str) -> cv2.VideoCapture:
    src = int(url) if url.isdigit() else url  # "0" => /dev/video0
    cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG) if isinstance(src, str) else cv2.VideoCapture(src)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # noqa: BLE001 - not all backends support it
        pass
    return cap


def _preview(frame) -> bytes:
    h, w = frame.shape[:2]
    if w > PREVIEW_WIDTH:
        frame = cv2.resize(frame, (PREVIEW_WIDTH, int(h * PREVIEW_WIDTH / w)), interpolation=cv2.INTER_AREA)
    return encode_jpeg(frame, 75)


class CameraWorker(threading.Thread):
    def __init__(
        self,
        cfg: CameraConfig,
        on_alert: Callable[[Alert], None],
        now: Callable[[], datetime],
        snapshot_dir: Optional[str] = None,
    ):
        super().__init__(name=f"cam:{cfg.name}", daemon=True)
        self.cfg = cfg
        self.on_alert = on_alert
        self.stop_event = threading.Event()
        self.now = now
        self.snapshot_dir = snapshot_dir
        self.detector = MotionDetector(
            threshold=cfg.threshold,
            min_area=cfg.min_area,
            resize_width=cfg.resize_width,
            alpha=cfg.background_alpha,
            warmup_frames=cfg.warmup_frames,
            ignore_regions=cfg.ignore_regions,
        )
        self._last_alert = 0.0
        self._streak = 0
        # State read by the web UI
        self.state = "starting"  # starting | disarmed | connecting | online | offline
        self.error: Optional[str] = None
        self.last_motion: Optional[float] = None  # epoch seconds
        self.last_frame: Optional[bytes] = None
        self.last_frame_at: Optional[float] = None
        self.last_alert_jpeg: Optional[bytes] = None
        self.alerts_sent = 0

    def stop(self) -> None:
        self.stop_event.set()

    def snapshot_state(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "last_motion": self.last_motion,
            "last_frame_at": self.last_frame_at,
            "has_alert_image": self.last_alert_jpeg is not None,
            "alerts": self.alerts_sent,
        }

    def run(self) -> None:
        backoff = 2.0
        cap: Optional[cv2.VideoCapture] = None
        armed = None
        while not self.stop_event.is_set():
            active = self.cfg.schedule.is_active(self.now())
            if active != armed:
                log.info("[%s] %s (schedule: %s)", self.cfg.name,
                         "ARMED" if active else "disarmed", self.cfg.schedule.name)
                armed = active
            if not active:
                if cap is not None:
                    cap.release()
                    cap = None
                self.state = "disarmed"
                self.error = None
                self.detector.reset()
                self.stop_event.wait(15)
                continue

            if cap is None or not cap.isOpened():
                self.state = "connecting"
                log.info("[%s] connecting to %s", self.cfg.name, redact_url(self.cfg.url))
                cap = open_capture(self.cfg.url)
                if self.stop_event.is_set():
                    break
                if not cap.isOpened():
                    self.state = "offline"
                    self.error = "Could not open the stream - check the URL, username and password"
                    log.warning("[%s] connect failed, retry in %.0fs", self.cfg.name, backoff)
                    cap.release()
                    cap = None
                    self.stop_event.wait(backoff)
                    backoff = min(backoff * 2, 120)
                    continue
                self.detector.reset()
                self._streak = 0

            ok = self._loop(cap)
            if not ok:
                self.state = "offline"
                self.error = "Stream stopped sending frames"
                log.warning("[%s] stream lost, reconnecting", self.cfg.name)
                cap.release()
                cap = None
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 120)
            else:
                backoff = 2.0
        if cap is not None:
            cap.release()

    def _loop(self, cap: cv2.VideoCapture) -> bool:
        """Read frames until disarmed/stopped (True) or stream fails (False)."""
        last_proc = 0.0
        last_sched_check = time.monotonic()
        last_preview = 0.0
        failures = 0
        while not self.stop_event.is_set():
            # grab() continuously so we always analyse the newest frame
            if not cap.grab():
                failures += 1
                if failures > 20:
                    return False
                time.sleep(0.1)
                continue
            failures = 0
            t = time.monotonic()
            if t - last_sched_check > 10:
                last_sched_check = t
                if not self.cfg.schedule.is_active(self.now()):
                    return True
            if t - last_proc < self.cfg.frame_interval:
                continue
            last_proc = t
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            self.state = "online"
            self.error = None
            if t - last_preview >= PREVIEW_EVERY:
                last_preview = t
                self.last_frame = _preview(frame)
                self.last_frame_at = time.time()
            self._handle(frame)
        return True

    def _handle(self, frame) -> None:
        boxes = self.detector.process(frame)
        if not boxes:
            self._streak = 0
            return
        self._streak += 1
        if self._streak < self.cfg.min_motion_frames:
            return
        self.last_motion = time.time()
        t = time.monotonic()
        if self._last_alert and t - self._last_alert < self.cfg.cooldown_seconds:
            return
        self._last_alert = t
        self._streak = 0
        ts = self.now().strftime("%Y-%m-%d %H:%M:%S")
        label = f"{self.cfg.name}  {ts}"
        img = annotate(frame, boxes, label)
        jpeg = encode_jpeg(img)
        self.last_alert_jpeg = jpeg
        self.alerts_sent += 1
        log.info("[%s] motion detected (%d regions)", self.cfg.name, len(boxes))
        self._save(jpeg)
        if not self.stop_event.is_set():
            self.on_alert(Alert(caption=f"\U0001F6A8 Motion detected: {self.cfg.name}\n{ts}", photo=jpeg))

    def _save(self, jpeg: bytes) -> None:
        if not self.snapshot_dir:
            return
        try:
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.cfg.name)
            folder = os.path.join(self.snapshot_dir, safe)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, self.now().strftime("%Y%m%d-%H%M%S") + ".jpg")
            with open(path, "wb") as fh:
                fh.write(jpeg)
        except OSError as exc:
            log.warning("[%s] could not save snapshot: %s", self.cfg.name, exc)


def grab_snapshot(url: str, timeout: float = 15.0) -> Optional[bytes]:
    cap = open_capture(url)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                return encode_jpeg(frame)
        return None
    finally:
        cap.release()
