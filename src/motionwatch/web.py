"""Built-in web interface: a small JSON API plus a static single-page app.

Standard library only, so the Debian package needs no extra dependencies.
Security model: one admin password (scrypt), random session cookie
(HttpOnly, SameSite=Strict), state-changing requests must carry the
``X-MotionWatch`` header (cannot be sent cross-site without CORS, which we never
allow), strict CSP, login throttling, and secrets are never sent to the browser.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from . import __version__
from .auth import Auth
from .config import (TelegramConfig, ConfigError, ConfigStore, _ENV_RE, _expand_env, build_url,
                     valid_stream_url)
from .monitor import Monitor
from .notifier import Alert, TelegramError, TelegramNotifier, bot_api, find_chats

log = logging.getLogger(__name__)

SECRET = "********"
COOKIE = "mw_session"
MAX_BODY = 1024 * 1024
STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html"}
CSP = ("default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def parse_listen(value: str) -> Tuple[str, int]:
    host, _, port = value.rpartition(":")
    host = host.strip("[]") or "0.0.0.0"
    return host, int(port)


def _is_env_ref(value) -> bool:
    return isinstance(value, str) and bool(_ENV_RE.fullmatch(value.strip()))


def mask_config(raw: dict) -> dict:
    r = copy.deepcopy(raw)
    tg = r.setdefault("telegram", {}) or {}
    r["telegram"] = tg
    chats = tg.pop("chat_id", None)
    if "chat_ids" not in tg:
        tg["chat_ids"] = [] if chats is None else ([chats] if isinstance(chats, (str, int)) else chats)
    tg["chat_ids"] = [str(c) for c in tg.get("chat_ids") or []]
    if tg.get("bot_token") and not _is_env_ref(tg["bot_token"]):
        tg["bot_token"] = SECRET
    for cam in r.get("cameras") or []:
        if isinstance(cam, dict) and cam.get("password") and not _is_env_ref(cam["password"]):
            cam["password"] = SECRET
    return r


def unmask_config(new: dict, old: dict) -> dict:
    new = copy.deepcopy(new)
    tg = new.get("telegram") or {}
    if tg.get("bot_token") == SECRET:
        tg["bot_token"] = (old.get("telegram") or {}).get("bot_token", "")
    old_cams = {c.get("id"): c for c in old.get("cameras") or [] if isinstance(c, dict)}
    for cam in new.get("cameras") or []:
        if isinstance(cam, dict) and cam.get("password") == SECRET:
            cam["password"] = (old_cams.get(cam.get("id")) or {}).get("password", "")
    return new


class WebApp:
    def __init__(self, monitor: Monitor, store: ConfigStore, auth: Auth, listen: str, data_dir: str = ""):
        self.data_dir = data_dir
        self.monitor = monitor
        self.store = store
        self.auth = auth
        self.host, self.port = parse_listen(listen)
        self._server: Optional[ThreadingHTTPServer] = None
        self._reload_lock = threading.Lock()

    def start(self) -> None:
        handler = type("Handler", (_Handler,), {"app": self})
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="web", daemon=True).start()
        shown = "<this-machine>" if self.host in ("0.0.0.0", "::") else self.host
        log.info("Web interface on http://%s:%d", shown, self.port)
        if not self.auth.has_password():
            log.warning("First-time setup: open the web interface and enter setup code %s "
                        "(also in %s)", self.auth.setup_code(), self.auth.code_path)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def reload_async(self) -> None:
        def run():
            with self._reload_lock:
                self.monitor.reload()

        threading.Thread(target=run, name="reload", daemon=True).start()


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _Handler(BaseHTTPRequestHandler):
    app: WebApp
    server_version = "MotionWatch"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    ROUTES = [
        ("GET", r"/", "page_index", False),
        ("GET", r"/static/([a-z]+\.(?:js|css|svg))", "page_static", False),
        ("GET", r"/api/session", "api_session", False),
        ("POST", r"/api/setup", "api_setup", False),
        ("POST", r"/api/login", "api_login", False),
        ("POST", r"/api/logout", "api_logout", True),
        ("GET", r"/api/status", "api_status", True),
        ("GET", r"/api/config", "api_get_config", True),
        ("PUT", r"/api/config", "api_put_config", True),
        ("GET", r"/api/cameras/([A-Za-z0-9_-]+)/(frame|alert)\.jpg", "api_camera_image", True),
        ("POST", r"/api/cameras/probe", "api_probe", True),
        ("POST", r"/api/telegram/chats", "api_tg_chats", True),
        ("POST", r"/api/telegram/test", "api_tg_test", True),
        ("POST", r"/api/password", "api_password", True),
        ("GET", r"/api/timezones", "api_timezones", True),
    ]

    # ---- plumbing ----------------------------------------------------
    def log_message(self, fmt, *args):  # route access logs to debug
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        try:
            path_known = False
            for m, pattern, name, needs_auth in self.ROUTES:
                match = re.fullmatch(pattern, path)
                if not match:
                    continue
                path_known = True
                if m != method:
                    continue
                if method != "GET":
                    self._check_csrf()
                if needs_auth and not self.app.auth.check_session(self._token()):
                    raise HttpError(HTTPStatus.UNAUTHORIZED, "Please log in")
                getattr(self, name)(*match.groups())
                return
            if path_known:
                raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            raise HttpError(HTTPStatus.NOT_FOUND, "Not found")
        except HttpError as exc:
            self._json({"error": exc.message}, exc.status)
        except Exception:  # noqa: BLE001
            log.exception("web request failed: %s %s", method, path)
            self._json({"error": "Internal error - see the service log"}, 500)

    def _check_csrf(self) -> None:
        if self.headers.get("X-MotionWatch") != "1":
            raise HttpError(HTTPStatus.FORBIDDEN, "Missing request header")
        origin = self.headers.get("Origin")
        if origin and re.sub(r"^https?://", "", origin) != self.headers.get("Host", ""):
            raise HttpError(HTTPStatus.FORBIDDEN, "Cross-origin request refused")

    def _token(self) -> Optional[str]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[COOKIE].value if COOKIE in cookie else None

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Invalid JSON") from None
        if not isinstance(data, dict):
            raise HttpError(HTTPStatus.BAD_REQUEST, "Expected a JSON object")
        return data

    def _headers(self, status: int, ctype: str, length: int, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, status: int = 200, extra: Optional[dict] = None) -> None:
        data = json.dumps(obj).encode()
        self._headers(status, "application/json", len(data), extra)
        self.wfile.write(data)

    def _bytes(self, data: bytes, ctype: str) -> None:
        self._headers(200, ctype, len(data))
        self.wfile.write(data)

    def _set_cookie(self, token: str, max_age: int) -> dict:
        return {"Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"}

    # ---- pages -------------------------------------------------------
    def page_index(self):
        with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
            self._bytes(fh.read(), "text/html; charset=utf-8")

    def page_static(self, name: str):
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            raise HttpError(404, "Not found")
        with open(path, "rb") as fh:
            self._bytes(fh.read(), STATIC_TYPES[os.path.splitext(name)[1]] + "; charset=utf-8")

    # ---- auth --------------------------------------------------------
    def api_session(self):
        auth = self.app.auth
        self._json({
            "authenticated": auth.check_session(self._token()),
            "setup_required": not auth.has_password(),
            "version": __version__,
        })

    def _throttle(self) -> str:
        ip = self.client_address[0]
        wait = self.app.auth.wait_seconds(ip)
        if wait:
            raise HttpError(HTTPStatus.TOO_MANY_REQUESTS, f"Too many attempts - try again in {wait} s")
        return ip

    def api_setup(self):
        auth = self.app.auth
        if auth.has_password():
            raise HttpError(HTTPStatus.CONFLICT, "Already set up - please log in")
        ip = self._throttle()
        body = self._body()
        if not auth.check_setup_code(str(body.get("code", ""))):
            auth.record_failure(ip)
            raise HttpError(HTTPStatus.FORBIDDEN, "Wrong setup code")
        try:
            auth.set_password(str(body.get("password", "")))
        except ValueError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        auth.record_success(ip)
        log.info("Admin password set from %s", ip)
        self._json({"ok": True}, extra=self._set_cookie(auth.new_session(), 12 * 3600))

    def api_login(self):
        auth = self.app.auth
        ip = self._throttle()
        if not auth.verify(str(self._body().get("password", ""))):
            auth.record_failure(ip)
            log.warning("Failed web login from %s", ip)
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Wrong password")
        auth.record_success(ip)
        self._json({"ok": True}, extra=self._set_cookie(auth.new_session(), 12 * 3600))

    def api_logout(self):
        self.app.auth.end_session(self._token())
        self._json({"ok": True}, extra=self._set_cookie("", 0))

    def api_password(self):
        body = self._body()
        ip = self._throttle()
        if not self.app.auth.verify(str(body.get("current", ""))):
            self.app.auth.record_failure(ip)
            raise HttpError(HTTPStatus.FORBIDDEN, "Current password is wrong")
        try:
            self.app.auth.set_password(str(body.get("new", "")))
        except ValueError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        self._json({"ok": True}, extra=self._set_cookie(self.app.auth.new_session(), 12 * 3600))

    # ---- config ------------------------------------------------------
    def _raw(self) -> dict:
        try:
            return self.app.store.load_raw()
        except ConfigError as exc:
            raise HttpError(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from None

    def api_status(self):
        status = self.app.monitor.status()
        status["data_dir"] = self.app.data_dir
        self._json(status)

    def api_get_config(self):
        self._json(mask_config(self._raw()))

    def api_put_config(self):
        new = unmask_config(self._body(), self._raw())
        try:
            cfg = self.app.store.save_raw(new)
        except ConfigError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        log.info("Configuration updated from the web interface")
        self.app.reload_async()
        self._json({"ok": True, "warnings": cfg.warnings, "config": mask_config(self._raw())})

    def api_timezones(self):
        try:
            from zoneinfo import available_timezones

            zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))
        except Exception:  # noqa: BLE001
            zones = []
        self._json({"timezones": ["UTC"] + zones})

    # ---- cameras -----------------------------------------------------
    def api_camera_image(self, cam_id: str, kind: str):
        w = self.app.monitor.worker(cam_id)
        data = None
        if w:
            data = w.last_frame if kind == "frame" else w.last_alert_jpeg
        if not data:
            raise HttpError(404, "No image yet")
        self._bytes(data, "image/jpeg")

    def api_probe(self):
        from .camera import grab_snapshot

        body = self._body()
        password = body.get("password") or ""
        if password == SECRET:
            old = {c.get("id"): c for c in self._raw().get("cameras") or [] if isinstance(c, dict)}
            password = (old.get(body.get("id")) or {}).get("password", "")
        try:
            url = _expand_env(str(body.get("url") or "").strip())
            url = build_url(url, _expand_env(body.get("username") or ""), _expand_env(password))
        except ConfigError as exc:
            raise HttpError(400, str(exc)) from None
        if not url:
            raise HttpError(400, "Enter the stream URL first")
        if not valid_stream_url(url):
            raise HttpError(400, "The stream URL must start with rtsp://, rtsps://, http:// or https:// "
                                 "(or be a webcam number like 0)")
        jpeg = grab_snapshot(url, timeout=12)
        if not jpeg:
            raise HttpError(HTTPStatus.BAD_GATEWAY,
                            "Couldn't get a picture from that camera. Check the URL, username and password, "
                            "and that this machine can reach the camera.")
        self._bytes(jpeg, "image/jpeg")

    # ---- telegram ----------------------------------------------------
    def _token_from(self, body: dict) -> str:
        token = str(body.get("bot_token") or "").strip()
        if token == SECRET or not token:
            token = str((self._raw().get("telegram") or {}).get("bot_token") or "")
        try:
            return _expand_env(token)
        except ConfigError as exc:
            raise HttpError(400, str(exc)) from None

    def api_tg_chats(self):
        try:
            self._json(find_chats(self._token_from(self._body())))
        except TelegramError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from None

    def api_tg_test(self):
        body = self._body()
        token = self._token_from(body)
        chats = [str(c).strip() for c in body.get("chat_ids") or [] if str(c).strip()]
        if not chats:
            raise HttpError(400, "Add at least one chat id first")
        try:
            bot_api(token, "getMe")
        except TelegramError as exc:
            raise HttpError(400, str(exc)) from None
        notifier = TelegramNotifier(TelegramConfig(bot_token=token, chat_ids=chats))
        if not notifier.send_now(Alert(caption="✅ MotionWatch test message - alerts will arrive here."),
                                 retries=1):
            raise HttpError(400, "Telegram refused the message. Check the chat id, and make sure you have "
                                 "sent /start to the bot (or added it to the group).")
        self._json({"ok": True})
