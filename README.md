# MotionWatch

Watches your CCTV cameras for movement and sends you a Telegram photo with the
moving area boxed in red — only during the hours you choose.

- Works with RTSP / HTTP (MJPEG) IP cameras, NVR channel streams and USB webcams
- Weekly arming schedules per camera, including overnight windows
- Ignore zones (roads, trees), cooldowns and sensitivity per camera
- Auto-reconnects when a camera drops; runs as a hardened systemd service
- Lightweight: runs on a Raspberry Pi or any Debian 11+/Ubuntu 22.04+ box

## Install (Debian / Ubuntu / Raspberry Pi OS)

Download the latest `.deb` from the [Releases](https://github.com/bothamad/motionwatch/releases) page, then:

```sh
sudo apt install ./motionwatch_*_all.deb
```

## Set up

1. **Create a bot**: message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.
2. **Find your chat id**: send your bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
   For a group, add the bot to the group and use the (negative) group id.
3. **Add secrets** to `/etc/motionwatch/motionwatch.env`:
   ```sh
   sudo nano /etc/motionwatch/motionwatch.env
   ```
4. **Add cameras and schedules** in `/etc/motionwatch/config.yaml` (fully commented).
5. **Check and start**:
   ```sh
   sudo -u motionwatch sh -c 'set -a; . /etc/motionwatch/motionwatch.env; motionwatch check-config'
   sudo -u motionwatch sh -c 'set -a; . /etc/motionwatch/motionwatch.env; motionwatch test-telegram'
   sudo systemctl enable --now motionwatch
   journalctl -u motionwatch -f
   ```

Send a live picture from every camera at any time:
`motionwatch snapshot` (or `motionwatch snapshot "Front door"`).

## Schedules

```yaml
schedules:
  night:
    - days: [weekdays]      # mon..sun, weekdays, weekends, daily
      start: "18:00"
      end: "07:00"          # earlier than start => runs past midnight
    - days: [weekends]
      start: "00:00"
      end: "24:00"
cameras:
  - name: Front door
    url: rtsp://${CAM_USER}:${CAM_PASS}@192.168.1.10:554/stream2
    schedule: night         # or "always"
```

An overnight window belongs to the day it starts: `mon 18:00 → 07:00` covers
Monday evening and Tuesday early morning. Set `timezone:` if the server's clock
isn't in your local zone.

## Tuning

| Setting | Default | Meaning |
|---|---|---|
| `threshold` | 25 | Brightness change (0–255) that counts as movement. Raise for noisy night video. |
| `min_area` | 1500 | Smallest moving blob (px² on a 640 px-wide frame). Raise to ignore cats and leaves. |
| `min_motion_frames` | 3 | Consecutive frames with movement before alerting. |
| `cooldown_seconds` | 60 | Minimum gap between alerts per camera. |
| `frame_interval` | 0.25 | Seconds between analysed frames (CPU vs. reaction time). |
| `ignore_regions` | – | List of `[x, y, w, h]` fractions of the frame to ignore. |

Tip: use your camera's **sub-stream** (low resolution) — detection is just as
good and CPU use drops a lot.

## Docker

```sh
docker run -d --restart unless-stopped --env-file motionwatch.env \
  -v $PWD/config.yaml:/config/config.yaml:ro ghcr.io/bothamad/motionwatch:latest
```

See `docker-compose.yml`.

## Development

```sh
pip install -e .[dev]
pytest
python -m motionwatch -c config.yaml check-config
```

### Releasing

1. Bump `__version__` in `src/motionwatch/__init__.py` and update `CHANGELOG.md`.
2. `git tag v0.1.0 && git push --tags`

GitHub Actions runs the tests, builds the `.deb`, wheel and sdist, smoke-tests
the `.deb` on Debian, publishes a GitHub Release with checksums and pushes a
multi-arch (amd64/arm64) Docker image to GHCR.

## Privacy & security

Images go only to the Telegram chats you configure. Camera passwords and the bot
token live in `/etc/motionwatch/motionwatch.env` (mode 0640, root:motionwatch)
and are redacted from logs. See [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
