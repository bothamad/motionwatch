"""Configuration loading, validation and storage.

The config file is a YAML document that the web UI reads and writes. It may be
incomplete (no Telegram settings, no cameras) - the service then runs the web UI
only, so a fresh install can be set up entirely from the browser.
"""

from __future__ import annotations

import copy
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

from .schedule import Schedule

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULTS: Dict[str, Any] = {
    "threshold": 25,  # pixel intensity delta (0-255)
    "min_area": 1500,  # px^2 on the resized frame
    "min_motion_frames": 3,  # consecutive frames with motion before alerting
    "cooldown_seconds": 60,  # min seconds between alerts per camera
    "frame_interval": 0.25,  # seconds between analysed frames
    "resize_width": 640,
    "background_alpha": 0.05,
    "warmup_frames": 25,  # frames used to learn the background after (re)connect
}

# Starting point for a fresh install: nothing configured yet, one example schedule.
INITIAL_CONFIG: Dict[str, Any] = {
    "telegram": {"bot_token": "", "chat_ids": [], "send_startup_message": True},
    "timezone": None,
    "snapshot_dir": None,
    "log_level": "INFO",
    "defaults": dict(DEFAULTS),
    "schedules": {
        "night": [
            {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "18:00", "end": "07:00"},
            {"days": ["sat", "sun"], "start": "00:00", "end": "00:00"},
        ]
    },
    "cameras": [],
}


class ConfigError(Exception):
    pass


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def repl(m: "re.Match[str]") -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set")

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class TelegramConfig:
    bot_token: str
    chat_ids: List[str]
    send_startup_message: bool = True
    api_base: str = "https://api.telegram.org"


@dataclass
class CameraConfig:
    id: str
    name: str
    url: str  # effective URL including credentials
    schedule: Schedule
    threshold: int
    min_area: int
    min_motion_frames: int
    cooldown_seconds: float
    frame_interval: float
    resize_width: int
    background_alpha: float
    warmup_frames: int
    # Regions (fractions 0..1: x, y, w, h) to ignore, e.g. trees or a road
    ignore_regions: List[Tuple[float, float, float, float]] = field(default_factory=list)
    enabled: bool = True


@dataclass
class Config:
    telegram: Optional[TelegramConfig]  # None until bot token and chat id are set
    cameras: List[CameraConfig]
    timezone: Optional[str] = None
    snapshot_dir: Optional[str] = None
    log_level: str = "INFO"
    warnings: List[str] = field(default_factory=list)


def valid_stream_url(url: str) -> bool:
    url = url.strip()
    if url.isdigit():
        return True
    if url.startswith("${"):
        return True  # expanded later
    return bool(re.match(r"(rtsps?|rtmp|https?)://[^/\s]+", url, re.I))


def build_url(url: str, username: Optional[str], password: Optional[str]) -> str:
    """Insert credentials into a stream URL (unless it already has some)."""
    if not username:
        return url
    p = urlsplit(url)
    if not p.scheme or not p.netloc or "@" in p.netloc:
        return url
    cred = quote(str(username), safe="")
    if password:
        cred += ":" + quote(str(password), safe="")
    return urlunsplit((p.scheme, f"{cred}@{p.netloc}", p.path, p.query, p.fragment))


def _num(raw: dict, key: str, cast, where: str):
    try:
        v = cast(raw[key])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: {key} must be a number") from exc
    if v < 0:
        raise ConfigError(f"{where}: {key} must be >= 0")
    return v


def parse_config(data: dict) -> Config:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    data = _expand_env(data)
    warnings: List[str] = []

    tg = data.get("telegram") or {}
    token = str(tg.get("bot_token") or "").strip()
    chats = tg.get("chat_ids", tg.get("chat_id"))
    if isinstance(chats, (str, int)):
        chats = [chats]
    chats = [str(c).strip() for c in (chats or []) if str(c).strip()]
    telegram: Optional[TelegramConfig] = None
    if token and chats:
        telegram = TelegramConfig(
            bot_token=token,
            chat_ids=chats,
            send_startup_message=bool(tg.get("send_startup_message", True)),
            api_base=str(tg.get("api_base", "https://api.telegram.org")).rstrip("/"),
        )
    else:
        warnings.append("Telegram is not set up yet - alerts will not be sent")

    tz = data.get("timezone") or None
    if tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(str(tz))
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"unknown timezone {tz!r}") from exc

    schedules: Dict[str, Schedule] = {"always": Schedule.always()}
    for name, raw in (data.get("schedules") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,40}", str(name)) or name == "always":
            raise ConfigError(f"invalid schedule name {name!r}")
        try:
            schedules[name] = Schedule.from_config(name, raw)
        except (ValueError, KeyError, AttributeError, TypeError) as exc:
            raise ConfigError(f"schedule {name!r}: {exc}") from exc

    defaults = dict(DEFAULTS)
    defaults.update(data.get("defaults") or {})

    cameras: List[CameraConfig] = []
    seen_names, seen_ids = set(), set()
    for i, c in enumerate(data.get("cameras") or []):
        where = f"camera {i + 1}"
        if not isinstance(c, dict):
            raise ConfigError(f"{where}: must be a mapping")
        name = str(c.get("name") or f"Camera {i + 1}").strip()
        where = f"camera {name!r}"
        if not c.get("url"):
            raise ConfigError(f"{where}: stream URL is required")
        if not valid_stream_url(str(c["url"])):
            raise ConfigError(f"{where}: the stream URL must start with rtsp://, rtsps://, http:// or https:// "
                              "(or be a webcam number like 0)")
        if name in seen_names:
            raise ConfigError(f"{where}: duplicate camera name")
        seen_names.add(name)
        cam_id = str(c.get("id") or f"cam{i + 1}")
        if cam_id in seen_ids:
            raise ConfigError(f"{where}: duplicate camera id")
        seen_ids.add(cam_id)
        merged = dict(defaults)
        merged.update({k: v for k, v in c.items() if v is not None and v != ""})
        sched_name = str(merged.get("schedule", "always"))
        if sched_name not in schedules:
            raise ConfigError(f"{where}: unknown schedule {sched_name!r}")
        regions = []
        for r in merged.get("ignore_regions") or []:
            if not (isinstance(r, (list, tuple)) and len(r) == 4):
                raise ConfigError(f"{where}: ignore_regions entries must be [x, y, w, h]")
            try:
                regions.append(tuple(min(1.0, max(0.0, float(v))) for v in r))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{where}: ignore_regions must be numbers") from exc
        cameras.append(
            CameraConfig(
                id=cam_id,
                name=name,
                url=build_url(str(c["url"]).strip(), c.get("username"), c.get("password")),
                schedule=schedules[sched_name],
                threshold=_num(merged, "threshold", int, where),
                min_area=_num(merged, "min_area", int, where),
                min_motion_frames=max(1, _num(merged, "min_motion_frames", int, where)),
                cooldown_seconds=_num(merged, "cooldown_seconds", float, where),
                frame_interval=_num(merged, "frame_interval", float, where),
                resize_width=max(160, _num(merged, "resize_width", int, where)),
                background_alpha=_num(merged, "background_alpha", float, where),
                warmup_frames=_num(merged, "warmup_frames", int, where),
                ignore_regions=regions,
                enabled=bool(merged.get("enabled", True)),
            )
        )
    if not cameras:
        warnings.append("No cameras added yet")

    return Config(
        telegram=telegram,
        cameras=cameras,
        timezone=tz,
        snapshot_dir=data.get("snapshot_dir") or None,
        log_level=str(data.get("log_level") or "INFO").upper(),
        warnings=warnings,
    )


def load_config(path: str) -> Config:
    return parse_config(ConfigStore(path).load_raw())


def redact_url(url: str) -> str:
    """Hide credentials in camera URLs for logging."""
    return re.sub(r"//[^/@]+@", "//***@", url)


def new_camera_id() -> str:
    return "cam-" + secrets.token_hex(4)


class ConfigStore:
    """Reads and atomically writes the raw YAML config."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load_raw(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except FileNotFoundError:
            return copy.deepcopy(INITIAL_CONFIG)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {self.path}: {exc}") from exc
        if data is None:
            return copy.deepcopy(INITIAL_CONFIG)
        if not isinstance(data, dict):
            raise ConfigError("config root must be a mapping")
        return data

    def save_raw(self, raw: dict) -> Config:
        """Validate then write. Raises ConfigError and leaves the file untouched on error."""
        raw = copy.deepcopy(raw)
        for cam in raw.get("cameras") or []:
            if isinstance(cam, dict) and not cam.get("id"):
                cam["id"] = new_camera_id()
        cfg = parse_config(raw)
        text = "# Managed by the MotionWatch web interface. Manual edits are kept.\n" + yaml.safe_dump(
            raw, sort_keys=False, allow_unicode=True
        )
        with self._lock:
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".config-", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return cfg
