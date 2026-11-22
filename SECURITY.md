# Security Policy

Please report vulnerabilities privately through GitHub's
"Report a vulnerability" (Security Advisories) on this repository rather than
in a public issue. We aim to respond within 7 days.

Notes for operators:

- Keep the bot token secret; anyone with it can read what the bot sends. Revoke it with @BotFather if leaked.
- Put cameras on an isolated VLAN and don't expose RTSP to the internet.
- The service runs as the unprivileged `motionwatch` user with systemd sandboxing.
- MotionWatch is a convenience tool, not a certified alarm system.
