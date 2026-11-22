"""Runs the camera workers for the current config and swaps them on reload."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from . import __version__
from .config import Config, ConfigError, ConfigStore, parse_config, redact_url
from .notifier import Alert, TelegramNotifier

log = logging.getLogger(__name__)


def make_clock(tz_name: Optional[str]) -> Callable[[], datetime]:
    if not tz_name:
        return datetime.now
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    return lambda: datetime.now(tz)


class Monitor:
    def __init__(self, store: ConfigStore, start_cameras: bool = True):
        self.store = store
        self.start_cameras = start_cameras
        self._lock = threading.RLock()
        self.config: Optional[Config] = None
        self.error: Optional[str] = None
        self.workers: Dict[str, "CameraWorker"] = {}  # noqa: F821
        self.notifier: Optional[TelegramNotifier] = None
        self.clock: Callable[[], datetime] = datetime.now
        self.started_at = time.time()
        self._announced = False

    # ---- lifecycle ---------------------------------------------------
    def reload(self) -> None:
        with self._lock:
            try:
                cfg = parse_config(self.store.load_raw())
                err = None
            except ConfigError as exc:
                cfg, err = None, str(exc)
            self._stop_all()
            self.config, self.error = cfg, err
            if cfg is None:
                log.error("Configuration error: %s - cameras are stopped until it is fixed", err)
                return
            logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))
            for w in cfg.warnings:
                log.warning("%s", w)
            self.clock = make_clock(cfg.timezone)
            if cfg.telegram:
                self.notifier = TelegramNotifier(cfg.telegram)
                self.notifier.start()
            if self.start_cameras:
                from .camera import CameraWorker

                for cam in cfg.cameras:
                    if not cam.enabled:
                        continue
                    w = CameraWorker(cam, self._alert, self.clock, cfg.snapshot_dir)
                    self.workers[cam.id] = w
                    w.start()
            log.info("Monitoring %d camera(s)", len(self.workers))
            if self.notifier and cfg.telegram.send_startup_message and not self._announced:
                self._announced = True
                names = ", ".join(c.name for c in cfg.cameras if c.enabled) or "none yet"
                self.notifier.submit(Alert(caption=f"✅ MotionWatch {__version__} started. Cameras: {names}"))

    def stop(self) -> None:
        with self._lock:
            self._stop_all()

    def _stop_all(self) -> None:
        workers = list(self.workers.values())
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=5)
        self.workers = {}
        if self.notifier:
            self.notifier.stop(timeout=5)
            self.notifier = None

    def _alert(self, alert: Alert) -> None:
        n = self.notifier
        if n:
            n.submit(alert)
        else:
            log.warning("Telegram not configured - alert not sent: %s", alert.caption.splitlines()[0])

    def dead_workers(self) -> List[str]:
        with self._lock:
            return [w.name for w in self.workers.values() if not w.is_alive()]

    # ---- for the web UI ----------------------------------------------
    def worker(self, cam_id: str):
        return self.workers.get(cam_id)

    def status(self) -> dict:
        cfg = self.config
        now = self.clock()
        cams = []
        if cfg:
            for c in cfg.cameras:
                w = self.workers.get(c.id)
                entry = {
                    "id": c.id,
                    "name": c.name,
                    "url": redact_url(c.url),
                    "enabled": c.enabled,
                    "schedule": c.schedule.name,
                    "armed": c.enabled and c.schedule.is_active(now),
                }
                if w:
                    entry.update(w.snapshot_state())
                else:
                    entry["state"] = "disabled" if not c.enabled else "stopped"
                cams.append(entry)
        return {
            "version": __version__,
            "error": self.error,
            "warnings": cfg.warnings if cfg else [],
            "telegram_ready": bool(cfg and cfg.telegram),
            "now": now.strftime("%a %d %b %H:%M"),
            "uptime": int(time.time() - self.started_at),
            "cameras": cams,
        }
