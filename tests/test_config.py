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
        {"cameras": [{"url": "rtsp://x", "schedule": "nope"}]},
        {"cameras": [{"name": "a", "url": "rtsp://x"}, {"name": "a", "url": "rtsp://y"}]},
        {"cameras": [{"name": "a"}]},
        {"cameras": [{"name": "a", "url": "file:///etc/passwd"}]},
        {"timezone": "Mars/Base"},
        {"schedules": {"n": [{"days": [], "start": "01:00", "end": "02:00"}]}},
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


def test_incomplete_config_is_allowed_with_warnings():
    cfg = parse_config({})
    assert cfg.telegram is None and cfg.cameras == []
    assert len(cfg.warnings) == 2


def test_credentials_are_inserted_into_url():
    cfg = parse_config({"cameras": [{"url": "rtsp://10.0.0.5:554/s", "username": "ad min", "password": "p@ss"}]})
    assert cfg.cameras[0].url == "rtsp://ad%20min:p%40ss@10.0.0.5:554/s"


def test_store_roundtrip_assigns_ids(tmp_path):
    from motionwatch.config import ConfigStore

    store = ConfigStore(str(tmp_path / "config.yaml"))
    assert store.load_raw()["cameras"] == []  # initial config when the file is missing
    raw = store.load_raw()
    raw["cameras"].append({"name": "Gate", "url": "rtsp://1.2.3.4/s"})
    store.save_raw(raw)
    saved = store.load_raw()
    assert saved["cameras"][0]["id"].startswith("cam-")
    with pytest.raises(ConfigError):
        store.save_raw({"cameras": [{"name": "x"}]})
    assert store.load_raw()["cameras"][0]["name"] == "Gate"  # bad save left the file alone
