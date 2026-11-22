#!/bin/sh
set -e

if ! getent passwd motionwatch >/dev/null; then
    adduser --system --group --home /var/lib/motionwatch --no-create-home motionwatch >/dev/null
fi
adduser motionwatch video >/dev/null 2>&1 || true

mkdir -p /var/lib/motionwatch
# Upgrade from 0.1.x: the config moved from /etc to the web-managed data directory.
if [ ! -f /var/lib/motionwatch/config.yaml ] && [ -f /etc/motionwatch/config.yaml ]; then
    cp /etc/motionwatch/config.yaml /var/lib/motionwatch/config.yaml
    mv /etc/motionwatch/config.yaml /etc/motionwatch/config.yaml.migrated
    echo "MotionWatch: moved /etc/motionwatch/config.yaml to /var/lib/motionwatch/config.yaml"
fi
chown -R motionwatch:motionwatch /var/lib/motionwatch
chmod 0750 /var/lib/motionwatch

chown root:motionwatch /etc/motionwatch /etc/motionwatch/motionwatch.env
chmod 0750 /etc/motionwatch
chmod 0640 /etc/motionwatch/motionwatch.env

if [ -d /run/systemd/system ]; then
    systemctl daemon-reload >/dev/null || true
    systemctl enable motionwatch >/dev/null 2>&1 || true
    systemctl restart motionwatch || true
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo ""
    echo "MotionWatch is running. Open http://${IP:-<this-machine>}:8080 in your browser."
    if [ ! -f /var/lib/motionwatch/auth.json ]; then
        sleep 2
        if [ -f /var/lib/motionwatch/setup-code ]; then
            echo "Setup code: $(cat /var/lib/motionwatch/setup-code)"
        else
            echo "Get the setup code with: sudo cat /var/lib/motionwatch/setup-code"
        fi
    fi
    echo ""
fi
exit 0
