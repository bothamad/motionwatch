import os

import pytest

from motionwatch.config import ConfigError, load_config, parse_config, redact_url

ROOT = os.path.dirname(os.path.dirname(__file__))


def base(**over):
    d = {
        "telegram": {"bot_token": "t", "chat_id": 123},
        "cameras": [{"name": "a", "url": "rtsp://x"}],
    }
    d.update(over)
    return d


def test_minimal_defaults():
    cfg = parse_config(base())
    cam = cfg.cameras[0]
    assert cfg.telegram.chat_ids == ["123"]
    assert cam.schedule.name == "always"
    assert cam.min_area == 1500


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    cfg = parse_config(base(telegram={"bot_token": "${TOK}", "chat_id": "${CHAT:-99}"}))
    assert cfg.telegram.bot_token == "secret"
    assert cfg.telegram.chat_ids == ["99"]


def test_missing_env_errors(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError):
        parse_config(base(telegram={"bot_token": "${NOPE}", "chat_id": 1}))


@pytest.mark.parametrize(
    "data",
    [
        {"telegram": {"chat_id": 1}, "cameras": [{"url": "x"}]},
        {"telegram": {"bot_token": "t", "chat_id": 1}, "cameras": []},
        {"telegram": {"bot_token": "t", "chat_id": 1}, "cameras": [{"url": "x", "schedule": "nope"}]},
        {"telegram": {"bot_token": "t", "chat_id": 1},
         "cameras": [{"name": "a", "url": "x"}, {"name": "a", "url": "y"}]},
    ],
)
def test_invalid(data):
    with pytest.raises(ConfigError):
        parse_config(data)


def test_example_config_is_valid(monkeypatch):
    for k in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CAM_USER", "CAM_PASS"]:
        monkeypatch.setenv(k, "x")
    cfg = load_config(os.path.join(ROOT, "config.example.yaml"))
    assert len(cfg.cameras) == 2
    assert cfg.cameras[1].ignore_regions == [(0.0, 0.0, 1.0, 0.2)]


def test_redact_url():
    assert redact_url("rtsp://admin:pw@1.2.3.4/s") == "rtsp://***@1.2.3.4/s"
    assert redact_url("rtsp://1.2.3.4/s") == "rtsp://1.2.3.4/s"
