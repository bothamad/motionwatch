#!/bin/sh
set -e
if [ "$1" = "purge" ]; then
    rm -rf /var/lib/motionwatch /etc/motionwatch
    deluser --system motionwatch >/dev/null 2>&1 || true
fi
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
exit 0
