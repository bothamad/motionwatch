#!/bin/sh
# Launcher installed as /usr/bin/motionwatch by the Debian package.
PYTHONPATH="/usr/lib/motionwatch${PYTHONPATH:+:$PYTHONPATH}" exec /usr/bin/python3 -m motionwatch "$@"
