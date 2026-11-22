"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
    name: str
    url: str
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
    telegram: TelegramConfig
    cameras: List[CameraConfig]
    timezone: Optional[str] = None
    snapshot_dir: Optional[str] = None
    log_level: str = "INFO"


def _num(raw: dict, key: str, cast, where: str):
    try:
        v = cast(raw[key])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: {key} must be a number") from exc
    if v < 0:
        raise ConfigError(f"{where}: {key} must be >= 0")
    return v


def parse_config(data: dict) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    data = _expand_env(data)

    tg = data.get("telegram") or {}
    token = str(tg.get("bot_token") or "").strip()
    chats = tg.get("chat_id", tg.get("chat_ids"))
    if isinstance(chats, (str, int)):
        chats = [chats]
    chats = [str(c).strip() for c in (chats or []) if str(c).strip()]
    if not token:
        raise ConfigError("telegram.bot_token is required")
    if not chats:
        raise ConfigError("telegram.chat_id is required")
    telegram = TelegramConfig(
        bot_token=token,
        chat_ids=chats,
        send_startup_message=bool(tg.get("send_startup_message", True)),
        api_base=str(tg.get("api_base", "https://api.telegram.org")).rstrip("/"),
    )

    schedules: Dict[str, Schedule] = {"always": Schedule.always()}
    for name, raw in (data.get("schedules") or {}).items():
        try:
            schedules[name] = Schedule.from_config(name, raw)
        except (ValueError, KeyError, AttributeError) as exc:
            raise ConfigError(f"schedules.{name}: {exc}") from exc

    defaults = dict(DEFAULTS)
    defaults.update(data.get("defaults") or {})

    cams_raw = data.get("cameras") or []
    if not cams_raw:
        raise ConfigError("at least one camera is required")
    cameras: List[CameraConfig] = []
    seen = set()
    for i, c in enumerate(cams_raw):
        where = f"cameras[{i}]"
        if not isinstance(c, dict) or not c.get("url"):
            raise ConfigError(f"{where}: url is required")
        name = str(c.get("name") or f"camera{i + 1}")
        if name in seen:
            raise ConfigError(f"{where}: duplicate camera name {name!r}")
        seen.add(name)
        merged = dict(defaults)
        merged.update(c)
        sched_name = str(merged.get("schedule", "always"))
        if sched_name not in schedules:
            raise ConfigError(f"{where}: unknown schedule {sched_name!r}")
        regions = []
        for r in merged.get("ignore_regions") or []:
            if not (isinstance(r, (list, tuple)) and len(r) == 4):
                raise ConfigError(f"{where}: ignore_regions entries must be [x, y, w, h]")
            regions.append(tuple(float(v) for v in r))
        cameras.append(
            CameraConfig(
                name=name,
                url=str(c["url"]),
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

    return Config(
        telegram=telegram,
        cameras=cameras,
        timezone=data.get("timezone"),
        snapshot_dir=data.get("snapshot_dir"),
        log_level=str(data.get("log_level", "INFO")).upper(),
    )


def load_config(path: str) -> Config:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return parse_config(data or {})


def redact_url(url: str) -> str:
    """Hide credentials in camera URLs for logging."""
    return re.sub(r"//[^/@]+@", "//***@", url)
