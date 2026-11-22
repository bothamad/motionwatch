#!/bin/sh
set -e

if ! getent passwd motionwatch >/dev/null; then
    adduser --system --group --home /var/lib/motionwatch --no-create-home motionwatch >/dev/null
fi
adduser motionwatch video >/dev/null 2>&1 || true

mkdir -p /var/lib/motionwatch
chown motionwatch:motionwatch /var/lib/motionwatch

chown root:motionwatch /etc/motionwatch /etc/motionwatch/config.yaml /etc/motionwatch/motionwatch.env
chmod 0750 /etc/motionwatch
chmod 0640 /etc/motionwatch/config.yaml /etc/motionwatch/motionwatch.env

if [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null || true
    if systemctl is-active --quiet motionwatch; then
        systemctl restart motionwatch || true
    else
        echo "MotionWatch installed. Edit /etc/motionwatch/motionwatch.env and config.yaml, then:"
        echo "  sudo motionwatch check-config && sudo systemctl enable --now motionwatch"
    fi
fi
exit 0
