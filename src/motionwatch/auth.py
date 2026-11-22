"""Admin password, first-run setup code and login sessions for the web UI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

MIN_PASSWORD = 8
SESSION_SECONDS = 12 * 3600
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def _write_private(path: str, text: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    # When run as root (e.g. `sudo motionwatch set-password`) hand the file to the
    # owner of the data directory so the service can read it.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        st = os.stat(folder)
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)


class Auth:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "auth.json")
        self.code_path = os.path.join(data_dir, "setup-code")
        self._lock = threading.Lock()
        self._sessions: Dict[str, float] = {}
        self._fails: Dict[str, Tuple[int, float]] = {}

    # ---- password ----------------------------------------------------
    def _stored_hash(self) -> Optional[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh).get("password_hash")
        except (FileNotFoundError, ValueError):
            return None

    def has_password(self) -> bool:
        return bool(self._stored_hash())

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
        _write_private(self.path, json.dumps({"password_hash": hash_password(password)}))
        with self._lock:
            self._sessions.clear()  # log out everywhere
        try:
            os.unlink(self.code_path)
        except FileNotFoundError:
            pass

    def verify(self, password: str) -> bool:
        stored = self._stored_hash()
        return bool(stored) and check_password(password, stored)

    # ---- first run ---------------------------------------------------
    def setup_code(self) -> str:
        """One-time code needed to set the first password (stops a stranger on the LAN doing it)."""
        try:
            with open(self.code_path, "r", encoding="utf-8") as fh:
                code = fh.read().strip()
                if code:
                    return code
        except FileNotFoundError:
            pass
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        _write_private(self.code_path, code + "\n")
        return code

    def check_setup_code(self, code: str) -> bool:
        if self.has_password():
            return False
        return hmac.compare_digest(self.setup_code().replace("-", ""), code.strip().upper().replace("-", ""))

    # ---- sessions ----------------------------------------------------
    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self._sessions = {t: e for t, e in self._sessions.items() if e > now}
            self._sessions[token] = now + SESSION_SECONDS
        return token

    def check_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if not exp or exp < time.time():
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = time.time() + SESSION_SECONDS  # sliding expiry
            return True

    def end_session(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    # ---- brute-force protection ---------------------------------------
    def wait_seconds(self, ip: str) -> int:
        with self._lock:
            count, until = self._fails.get(ip, (0, 0.0))
            return max(0, int(until - time.time()))

    def record_failure(self, ip: str) -> None:
        with self._lock:
            count, _ = self._fails.get(ip, (0, 0.0))
            count += 1
            delay = 0 if count < 5 else min(300, 15 * 2 ** (count - 5))
            self._fails[ip] = (count, time.time() + delay)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
