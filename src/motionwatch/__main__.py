"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import datetime
from typing import Callable, Optional

from . import __version__
from .config import Config, ConfigError, load_config, redact_url

DEFAULT_CONFIG = os.environ.get("MOTIONWATCH_CONFIG", "/etc/motionwatch/config.yaml")
log = logging.getLogger("motionwatch")


def make_clock(tz_name: Optional[str]) -> Callable[[], datetime]:
    if not tz_name:
        return datetime.now
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    return lambda: datetime.now(tz)


def cmd_run(cfg: Config) -> int:
    from .camera import CameraWorker
    from .notifier import Alert, TelegramNotifier

    stop = threading.Event()

    def handle_signal(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    clock = make_clock(cfg.timezone)
    notifier = TelegramNotifier(cfg.telegram)
    notifier.start()

    cams = [c for c in cfg.cameras if c.enabled]
    workers = [CameraWorker(c, notifier.submit, stop, clock, cfg.snapshot_dir) for c in cams]
    for w in workers:
        w.start()
    log.info("MotionWatch %s started with %d camera(s)", __version__, len(workers))
    if cfg.telegram.send_startup_message:
        names = ", ".join(c.name for c in cams) or "none"
        notifier.submit(Alert(caption=f"✅ MotionWatch {__version__} started. Cameras: {names}"))

    return_code = 0
    while not stop.is_set():
        stop.wait(5)
        dead = [w for w in workers if not w.is_alive()]
        if dead and not stop.is_set():
            log.error("camera thread %s died; exiting so systemd restarts us", dead[0].name)
            return_code = 1
            stop.set()

    for w in workers:
        w.join(timeout=5)
    notifier.stop()
    log.info("stopped")
    return return_code


def cmd_check(cfg: Config) -> int:
    now = make_clock(cfg.timezone)()
    print(f"Config OK. {len(cfg.cameras)} camera(s), now={now:%a %H:%M}")
    for c in cfg.cameras:
        state = "ARMED" if c.schedule.is_active(now) else "disarmed"
        flag = "" if c.enabled else " (disabled)"
        print(f"  - {c.name}{flag}: {redact_url(c.url)} schedule={c.schedule.name} -> {state}")
    return 0


def cmd_test_telegram(cfg: Config) -> int:
    from .notifier import Alert, TelegramNotifier

    ok = TelegramNotifier(cfg.telegram).send_now(Alert(caption="MotionWatch test message ✅"))
    print("Sent." if ok else "Failed - check bot token / chat id and logs.")
    return 0 if ok else 1


def cmd_snapshot(cfg: Config, camera: Optional[str]) -> int:
    from .camera import grab_snapshot
    from .notifier import Alert, TelegramNotifier

    targets = [c for c in cfg.cameras if camera in (None, c.name)]
    if not targets:
        print(f"No camera named {camera!r}", file=sys.stderr)
        return 1
    notifier = TelegramNotifier(cfg.telegram)
    rc = 0
    for c in targets:
        jpeg = grab_snapshot(c.url)
        if jpeg is None:
            print(f"{c.name}: could not read a frame")
            rc = 1
            continue
        ok = notifier.send_now(Alert(caption=f"\U0001F4F7 Snapshot: {c.name}", photo=jpeg))
        print(f"{c.name}: {'sent' if ok else 'send failed'}")
        rc |= 0 if ok else 1
    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="motionwatch", description="CCTV motion alerts over Telegram")
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the monitor (default)")
    sub.add_parser("check-config", help="validate config and show schedule state")
    sub.add_parser("test-telegram", help="send a test message")
    sp = sub.add_parser("snapshot", help="send a live snapshot from camera(s)")
    sp.add_argument("camera", nargs="?", help="camera name (default: all)")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    level = logging.DEBUG if args.verbose else getattr(logging, cfg.log_level, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(threadName)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cmd = args.cmd or "run"
    if cmd == "check-config":
        return cmd_check(cfg)
    if cmd == "test-telegram":
        return cmd_test_telegram(cfg)
    if cmd == "snapshot":
        return cmd_snapshot(cfg, args.camera)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())
