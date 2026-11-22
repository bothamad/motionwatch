# Changelog

## 0.3.0 - 2026-11-22

- **Find cameras**: one button on the Cameras page searches the local network.
  - ONVIF WS-Discovery finds IP cameras and NVRs with their maker and model.
  - A quick scan for open RTSP ports catches cameras with ONVIF switched off.
  - Shows which devices are already added.
- **Automatic stream address**: enter a discovered camera's login and MotionWatch asks the camera
  for its stream URLs over ONVIF, picks the low-resolution sub-stream and tests it.
- For cameras without ONVIF, pick the brand to fill in the usual RTSP address.

## 0.2.1 - 2026-11-22

- Fix: the .deb left out the web interface's page files, so the web page showed an internal error.
- Release check now installs the .deb and loads the web page before publishing.

## 0.2.0 - 2026-11-22

- **Web interface** (port 8080): manage cameras, schedules and Telegram from the browser.
  - Test a camera and see a live picture; draw ignore zones on it.
  - Schedule editor with a weekly preview.
  - Telegram "Find chats" (no more getUpdates by hand) and test message.
  - Dashboard with live view, camera state, last motion and last alert picture.
  - Admin password with first-run setup code, login throttling, secrets never sent to the browser.
- Changes apply instantly - no service restart.
- Settings now live in `/var/lib/motionwatch/config.yaml` (moved automatically from `/etc` on upgrade).
- Cameras can have separate username / password fields.
- A schedule window with the same start and end (e.g. 00:00-00:00) means the whole day.
- New `set-password` command; the service starts with an empty config so setup can happen in the browser.

## 0.1.0 - 2026-11-22

- First release: RTSP/HTTP/USB motion detection, weekly schedules, ignore zones,
  Telegram photo alerts, `check-config` / `test-telegram` / `snapshot` commands,
  Debian package with systemd service, Docker image.
