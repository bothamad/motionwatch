<div align="center">

<img src="docs/images/icon.svg" alt="" width="96">

# MotionWatch

**Turn the CCTV cameras you already own into a smart alarm - with photo alerts on your phone via Telegram.**

Runs quietly on any **Debian / Ubuntu / Raspberry Pi** box and only watches during the hours **you** choose.

[![Latest release](https://img.shields.io/github/v/release/bothamad/motionwatch?label=download&style=for-the-badge)](https://github.com/bothamad/motionwatch/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/bothamad/motionwatch/total?style=for-the-badge)](https://github.com/bothamad/motionwatch/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/bothamad/motionwatch/ci.yml?branch=main&label=tests&style=for-the-badge)](https://github.com/bothamad/motionwatch/actions/workflows/ci.yml)
![Debian](https://img.shields.io/badge/Debian-11%20%7C%2012%20%7C%2013-A81D33?style=for-the-badge&logo=debian&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=for-the-badge&logo=python&logoColor=white)
[![Support on Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20this%20project-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white)](https://ko-fi.com/drquashi)

</div>

---

## Why this exists

Most home and small-business CCTV systems record everything and tell you nothing. Their own "motion alerts"
are either missing, locked behind a cloud subscription, or so noisy you turn them off after a day. Full NVR
suites like Frigate, ZoneMinder or Shinobi are powerful but heavy to set up when all you want is
*"send me a picture when someone's in the driveway at night"*.

MotionWatch does exactly that and nothing more: it **connects to your cameras' existing streams**, watches for
movement **only inside your schedule**, and sends an **annotated snapshot to Telegram** - the moving area boxed in
red, with the camera name and time. No cloud account, no recording server, no subscription.

## How it works

```mermaid
flowchart LR
    A[IP camera / NVR<br/>RTSP · HTTP · USB] -->|live stream| B{Inside<br/>schedule?}
    B -- no --> Z[Disconnect &<br/>sleep]
    B -- yes --> C[Motion detector<br/>background diff]
    C -->|movement for N frames| D[Snapshot with<br/>red boxes]
    D --> E[Telegram bot]
    E --> F[📱 Your phone / group]
```

## Common problems and what to do

| Problem | Cause | Fix |
|---|---|---|
| **Alerts every few minutes** from trees, shadows or rain | Sensitivity too high for that scene | Raise `min_area` (e.g. `3000`) and `min_motion_frames` (e.g. `5`) for that camera |
| **A busy road** in the corner sets it off | Road is inside the frame | Add an `ignore_regions` box, e.g. `[0.0, 0.0, 1.0, 0.2]` = top 20 % of the image |
| Alerts when **IR night mode switches on** at dusk | The whole picture changes brightness at once | Raise `threshold` to `35-40`; the detector re-learns the background within a few seconds |
| **High CPU** on a Raspberry Pi | Analysing the 4K main stream | Use the camera's **sub-stream** (e.g. Hikvision `/Streaming/Channels/102`, Dahua `subtype=1`) and/or raise `frame_interval` |
| `connect failed` in the log | Wrong URL, password, or RTSP disabled on the camera | Test the URL with `ffplay "rtsp://..."` or VLC from the same machine first |
| Grey / smeared frames | UDP packet loss on Wi-Fi | Already handled - MotionWatch forces RTSP over **TCP** |
| `test-telegram` fails with **chat not found** | Bot has never been messaged, or wrong id | Send your bot `/start`, then re-read the id from `getUpdates`. Group ids are negative |
| Schedule is **an hour off** | Server clock in UTC | Set `timezone: Europe/London` (any IANA name) in `config.yaml` |
| Nothing happens and **no log output** | Service not started | `sudo systemctl enable --now motionwatch`, then `journalctl -u motionwatch -f` |

## Quick start

1. **Download** the `.deb` from the [latest release](https://github.com/bothamad/motionwatch/releases/latest) and install it:
   ```sh
   wget https://github.com/bothamad/motionwatch/releases/latest/download/motionwatch_0.1.0_all.deb
   sudo apt install ./motionwatch_0.1.0_all.deb
   ```
2. **Create a Telegram bot** - message [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.
   Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `"chat":{"id": ...}`.
3. **Add your secrets** (bot token, chat id, camera login):
   ```sh
   sudo nano /etc/motionwatch/motionwatch.env
   ```
4. **Add your cameras and schedules** - the file is fully commented:
   ```sh
   sudo nano /etc/motionwatch/config.yaml
   ```
5. **Check, test, start:**
   ```sh
   sudo systemctl start motionwatch      # runs check-config first and refuses to start on errors
   journalctl -u motionwatch -f          # you'll get a "✅ MotionWatch started" message on Telegram
   ```

Want a live picture right now? This sends one from every camera:

```sh
sudo -u motionwatch sh -c 'set -a; . /etc/motionwatch/motionwatch.env; motionwatch snapshot'
```

## Schedules

```yaml
schedules:
  night:
    - days: [weekdays]      # mon..sun, weekdays, weekends, daily
      start: "18:00"
      end: "07:00"          # earlier than start = runs past midnight
    - days: [weekends]
      start: "00:00"
      end: "24:00"

cameras:
  - name: Front door
    url: rtsp://${CAM_USER}:${CAM_PASS}@192.168.1.10:554/Streaming/Channels/102
    schedule: night         # or "always"

  - name: Driveway
    url: rtsp://${CAM_USER}:${CAM_PASS}@192.168.1.11:554/stream2
    schedule: always
    min_area: 3000
    ignore_regions:
      - [0.0, 0.0, 1.0, 0.2]   # x, y, width, height as fractions of the frame
```

An overnight window belongs to the day it **starts**: `mon 18:00 → 07:00` covers Monday evening and Tuesday
early morning. Outside its schedule a camera is fully disconnected - no bandwidth, no CPU.

## Features

<table>
<tr><td width="50%">

**📷 Any camera with a stream**
RTSP and HTTP/MJPEG IP cameras, NVR/DVR channel streams (Hikvision, Dahua, Reolink, TP-Link Tapo, Uniview…) and
USB webcams. Auto-reconnect with back-off when a camera drops.

**🗓️ Weekly schedules**
Named schedules shared between cameras, overnight windows, per-camera assignment and an optional timezone.
`check-config` shows which cameras are armed right now.

**🎯 Fewer false alarms**
Adaptive background, minimum blob size, N-consecutive-frames rule, per-camera cooldown and ignore zones -
all tunable per camera.

</td><td width="50%">

**📲 Telegram photo alerts**
Annotated JPEG with red boxes, camera name and timestamp. Send to one person, several, or a group.
Handles Telegram rate limits and retries on network errors.

**🛡️ Runs as a proper service**
Debian package with a systemd unit, its own unprivileged user, sandboxing and automatic restart.
Logs go to the journal.

**🐳 Docker too**
Multi-arch (amd64 / arm64) image on GHCR for NAS boxes and home servers.
Optional on-disk copy of every alert image.

</td></tr>
</table>

## Tuning

| Setting | Default | What it does |
|---|---|---|
| `threshold` | `25` | Brightness change (0-255) that counts as movement. Raise for noisy night video. |
| `min_area` | `1500` | Smallest moving blob in px² (on a 640 px-wide copy of the frame). Raise to ignore cats and leaves. |
| `min_motion_frames` | `3` | Consecutive analysed frames with movement before alerting. |
| `cooldown_seconds` | `60` | Minimum gap between two alerts from the same camera. |
| `frame_interval` | `0.25` | Seconds between analysed frames - lower reacts faster, higher saves CPU. |
| `ignore_regions` | – | List of `[x, y, w, h]` boxes (fractions 0-1) to ignore. |
| `enabled` | `true` | Set `false` to keep a camera in the file but switched off. |

Put any of these under `defaults:` to apply them to every camera, or under a single camera to override.

## Commands

| Command | What it does |
|---|---|
| `motionwatch run` | Start monitoring (what the service runs) |
| `motionwatch check-config` | Validate the config and show each camera's schedule state |
| `motionwatch test-telegram` | Send a test message to every configured chat |
| `motionwatch snapshot [camera]` | Send a live picture from one or all cameras |

Use `-c /path/to/config.yaml` for a different config file and `-v` for debug logging.

## Docker

```sh
docker run -d --name motionwatch --restart unless-stopped \
  --env-file motionwatch.env \
  -v $PWD/config.yaml:/config/config.yaml:ro \
  ghcr.io/bothamad/motionwatch:latest
```

Or use the included [docker-compose.yml](docker-compose.yml).

## Safe by design

- **Your images stay yours.** Pictures go only to the Telegram chats you configure - no MotionWatch server,
  no cloud account, no telemetry.
- **Secrets kept out of the config.** The bot token and camera passwords live in
  `/etc/motionwatch/motionwatch.env` (`0640`, `root:motionwatch`) and are **redacted from every log line**.
- **Least privilege.** The service runs as its own `motionwatch` user with systemd hardening
  (`ProtectSystem=strict`, `NoNewPrivileges`, no home access) and can only write to `/var/lib/motionwatch`.
- **Read-only on your cameras.** MotionWatch only reads the video stream - it never changes camera settings.
- **Verified releases.** Every release ships with `SHA256SUMS`, built by GitHub Actions from the tagged source.

> MotionWatch is a convenience tool, not a certified alarm system. Don't rely on it as your only protection.

## Updating

Download the new `.deb` from [releases](https://github.com/bothamad/motionwatch/releases) and install it over the
old one - your `config.yaml` and `motionwatch.env` are kept and the service restarts itself.
What changed is in the release notes and [CHANGELOG.md](CHANGELOG.md).

## Where things are kept

| Path | Contents |
|---|---|
| `/etc/motionwatch/config.yaml` | Cameras, schedules and tuning |
| `/etc/motionwatch/motionwatch.env` | Bot token, chat id, camera passwords |
| `/var/lib/motionwatch/` | Alert images, if `snapshot_dir` is set |
| `/usr/lib/motionwatch/` | The program |
| `journalctl -u motionwatch` | Logs |

Remove with `sudo apt remove motionwatch`, or `sudo apt purge motionwatch` to delete the config and images too.

## Build from source

Requires Python 3.9+.

```sh
git clone https://github.com/bothamad/motionwatch && cd motionwatch
pip install -e .[dev]
pytest
python -m motionwatch -c config.example.yaml check-config
```

Build the `.deb` yourself with [nfpm](https://nfpm.goreleaser.com/):

```sh
VERSION=0.1.0 nfpm package -f nfpm.yaml -p deb -t dist/
```

**Releasing:** bump `__version__` in `src/motionwatch/__init__.py`, update `CHANGELOG.md`, then
`git tag vX.Y.Z && git push --tags`. GitHub Actions tests, builds the `.deb`, wheel and sdist, test-installs the
`.deb` on Debian, publishes the release and pushes the Docker image.

## Support the project

MotionWatch is free. If it caught something you'd have missed and you'd like to say thanks,
you can buy me a coffee on Ko-fi:

<a href="https://ko-fi.com/drquashi"><img src="https://img.shields.io/badge/Ko--fi-Buy%20me%20a%20coffee-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white" alt="Support on Ko-fi"></a>

Bug reports, camera URL tips and pull requests are welcome. Security issues: see [SECURITY.md](SECURITY.md).

## Credits

- [OpenCV](https://opencv.org/) (Apache 2.0) - video capture and motion detection
- [FFmpeg](https://ffmpeg.org/) (LGPL) - RTSP/HTTP stream decoding
- [Telegram Bot API](https://core.telegram.org/bots/api) - alert delivery
- [PyYAML](https://pyyaml.org/) (MIT) and [Requests](https://requests.readthedocs.io/) (Apache 2.0)
- [nfpm](https://github.com/goreleaser/nfpm) (MIT) - Debian packaging

---

<sub>MIT licensed. Not affiliated with or endorsed by Telegram or any camera manufacturer. Camera brand names are
trademarks of their respective owners. Only monitor cameras and places you're allowed to - recording laws differ by
country.</sub>
