"""Command line entry point."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from typing import Optional

from . import __version__
from .config import ConfigError, ConfigStore, parse_config, redact_url
from .monitor import make_clock

DEFAULT_DATA = os.environ.get("MOTIONWATCH_DATA", "/var/lib/motionwatch")
DEFAULT_LISTEN = os.environ.get("MOTIONWATCH_WEB_LISTEN", "0.0.0.0:8080")
log = logging.getLogger("motionwatch")


def _load(store: ConfigStore):
    try:
        return parse_config(store.load_raw())
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return None


def cmd_run(store: ConfigStore, data_dir: str, listen: Optional[str]) -> int:
    from .auth import Auth
    from .monitor import Monitor

    stop = threading.Event()

    def handle_signal(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info("MotionWatch %s starting (config: %s)", __version__, store.path)
    monitor = Monitor(store)
    monitor.reload()

    web = None
    if listen:
        from .web import WebApp

        web = WebApp(monitor, store, Auth(data_dir), listen, data_dir)
        try:
            web.start()
        except OSError as exc:
            log.error("Could not start the web interface on %s: %s", listen, exc)
            return 1

    return_code = 0
    while not stop.is_set():
        stop.wait(5)
        dead = monitor.dead_workers()
        if dead and not stop.is_set():
            log.error("camera thread %s died; exiting so systemd restarts us", dead[0])
            return_code = 1
            stop.set()

    if web:
        web.stop()
    monitor.stop()
    log.info("stopped")
    return return_code


def cmd_check(store: ConfigStore) -> int:
    cfg = _load(store)
    if cfg is None:
        return 2
    now = make_clock(cfg.timezone)()
    print(f"Config OK ({store.path}). {len(cfg.cameras)} camera(s), now={now:%a %H:%M}")
    for w in cfg.warnings:
        print(f"  ! {w}")
    for c in cfg.cameras:
        state = "ARMED" if c.schedule.is_active(now) else "disarmed"
        flag = "" if c.enabled else " (disabled)"
        print(f"  - {c.name}{flag}: {redact_url(c.url)} schedule={c.schedule.name} -> {state}")
    return 0


def cmd_test_telegram(store: ConfigStore) -> int:
    from .notifier import Alert, TelegramNotifier

    cfg = _load(store)
    if cfg is None:
        return 2
    if not cfg.telegram:
        print("Telegram is not set up yet - do it in the web interface.", file=sys.stderr)
        return 1
    ok = TelegramNotifier(cfg.telegram).send_now(Alert(caption="MotionWatch test message ✅"))
    print("Sent." if ok else "Failed - check bot token / chat id and logs.")
    return 0 if ok else 1


def cmd_snapshot(store: ConfigStore, camera: Optional[str]) -> int:
    from .camera import grab_snapshot
    from .notifier import Alert, TelegramNotifier

    cfg = _load(store)
    if cfg is None:
        return 2
    if not cfg.telegram:
        print("Telegram is not set up yet.", file=sys.stderr)
        return 1
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


def cmd_set_password(data_dir: str) -> int:
    from .auth import Auth

    pw = getpass.getpass("New admin password: ")
    if pw != getpass.getpass("Repeat password: "):
        print("Passwords don't match.", file=sys.stderr)
        return 1
    try:
        Auth(data_dir).set_password(pw)
    except (ValueError, OSError) as exc:
        print(f"Could not set password: {exc}", file=sys.stderr)
        return 1
    print("Password set. Restart the service to log out existing sessions: sudo systemctl restart motionwatch")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="motionwatch", description="CCTV motion alerts over Telegram")
    p.add_argument("-d", "--data-dir", default=DEFAULT_DATA, help=f"state directory (default {DEFAULT_DATA})")
    p.add_argument("-c", "--config", default=os.environ.get("MOTIONWATCH_CONFIG"),
                   help="config file (default <data-dir>/config.yaml)")
    p.add_argument("--web", default=DEFAULT_LISTEN, metavar="HOST:PORT",
                   help=f"web interface address (default {DEFAULT_LISTEN})")
    p.add_argument("--no-web", action="store_true", help="don't start the web interface")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the monitor and web interface (default)")
    sub.add_parser("check-config", help="validate config and show schedule state")
    sub.add_parser("test-telegram", help="send a test message")
    sp = sub.add_parser("snapshot", help="send a live snapshot from camera(s)")
    sp.add_argument("camera", nargs="?", help="camera name (default: all)")
    sub.add_parser("set-password", help="set or reset the web interface admin password")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(threadName)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    store = ConfigStore(args.config or os.path.join(args.data_dir, "config.yaml"))
    cmd = args.cmd or "run"
    if cmd == "check-config":
        return cmd_check(store)
    if cmd == "test-telegram":
        return cmd_test_telegram(store)
    if cmd == "snapshot":
        return cmd_snapshot(store, args.camera)
    if cmd == "set-password":
        return cmd_set_password(args.data_dir)
    return cmd_run(store, args.data_dir, None if args.no_web else args.web)


if __name__ == "__main__":
    sys.exit(main())
