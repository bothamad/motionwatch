"""Telegram Bot API notifier with a background send queue."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from .config import TelegramConfig

log = logging.getLogger(__name__)


@dataclass
class Alert:
    caption: str
    photo: Optional[bytes] = None


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig, max_queue: int = 50, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout
        self._q: "queue.Queue[Optional[Alert]]" = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._session = requests.Session()

    def _url(self, method: str) -> str:
        return f"{self.cfg.api_base}/bot{self.cfg.bot_token}/{method}"

    def send_now(self, alert: Alert, retries: int = 3) -> bool:
        ok_all = True
        for chat_id in self.cfg.chat_ids:
            ok_all &= self._send_one(chat_id, alert, retries)
        return ok_all

    def _send_one(self, chat_id: str, alert: Alert, retries: int) -> bool:
        for attempt in range(1, retries + 1):
            try:
                if alert.photo:
                    resp = self._session.post(
                        self._url("sendPhoto"),
                        data={"chat_id": chat_id, "caption": alert.caption[:1024]},
                        files={"photo": ("motion.jpg", alert.photo, "image/jpeg")},
                        timeout=self.timeout,
                    )
                else:
                    resp = self._session.post(
                        self._url("sendMessage"),
                        data={"chat_id": chat_id, "text": alert.caption[:4096]},
                        timeout=self.timeout,
                    )
                if resp.status_code == 200:
                    return True
                if resp.status_code == 429:
                    wait = 5
                    try:
                        wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
                    except ValueError:
                        pass
                    log.warning("Telegram rate limited, retrying in %ss", wait)
                    time.sleep(wait)
                    continue
                # Never log the token: only status and description
                desc = ""
                try:
                    desc = resp.json().get("description", "")
                except ValueError:
                    pass
                log.error("Telegram error %s: %s", resp.status_code, desc)
                if 400 <= resp.status_code < 500:
                    return False
            except requests.RequestException as exc:
                log.warning("Telegram request failed (attempt %d/%d): %s",
                            attempt, retries, type(exc).__name__)
            time.sleep(min(30, 2 ** attempt))
        return False

    # ---- async queue -------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()

    def submit(self, alert: Alert) -> None:
        try:
            self._q.put_nowait(alert)
        except queue.Full:
            log.warning("Notification queue full, dropping alert: %s", alert.caption)

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread:
            self._q.put(None)
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            alert = self._q.get()
            if alert is None:
                return
            self.send_now(alert)
