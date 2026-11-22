import json
import urllib.error
import urllib.request

import pytest

from motionwatch.auth import Auth, check_password, hash_password
from motionwatch.config import ConfigStore
from motionwatch.monitor import Monitor
from motionwatch.web import SECRET, WebApp


@pytest.fixture
def server(tmp_path):
    store = ConfigStore(str(tmp_path / "config.yaml"))
    monitor = Monitor(store, start_cameras=False)
    monitor.reload()
    auth = Auth(str(tmp_path))
    app = WebApp(monitor, store, auth, "127.0.0.1:0", str(tmp_path))
    app.start()
    yield app
    app.stop()
    monitor.stop()


class Client:
    def __init__(self, app):
        self.base = f"http://127.0.0.1:{app.port}"
        self.cookie = None

    def call(self, method, path, body=None, header=True):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if header:
            req.add_header("X-MotionWatch", "1")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            status = resp.status
        except urllib.error.HTTPError as exc:
            resp, status = exc, exc.code
        cookie = resp.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        raw = resp.read()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw


def setup(app, client, password="correct horse"):
    code = app.auth.setup_code()
    status, _ = client.call("POST", "/api/setup", {"code": code, "password": password})
    assert status == 200


def test_password_hash():
    h = hash_password("hunter22")
    assert check_password("hunter22", h) and not check_password("hunter23", h)


def test_setup_requires_code(server):
    c = Client(server)
    assert c.call("GET", "/api/session")[1]["setup_required"] is True
    assert c.call("POST", "/api/setup", {"code": "WRONG", "password": "longenough"})[0] == 403
    setup(server, c)
    assert c.call("GET", "/api/session")[1]["authenticated"] is True
    # second setup is refused
    assert c.call("POST", "/api/setup", {"code": "x", "password": "longenough"})[0] == 409


def test_api_requires_login_and_header(server):
    c = Client(server)
    setup(server, c)
    anon = Client(server)
    assert anon.call("GET", "/api/config")[0] == 401
    assert c.call("PUT", "/api/config", {}, header=False)[0] == 403
    assert anon.call("POST", "/api/login", {"password": "nope"})[0] == 401
    assert anon.call("POST", "/api/login", {"password": "correct horse"})[0] == 200
    assert anon.call("GET", "/api/config")[0] == 200


def test_login_is_throttled(server):
    c = Client(server)
    setup(server, c)
    anon = Client(server)
    for _ in range(5):
        anon.call("POST", "/api/login", {"password": "bad"})
    assert anon.call("POST", "/api/login", {"password": "correct horse"})[0] == 429


def test_secrets_are_masked_and_kept(server):
    c = Client(server)
    setup(server, c)
    _, cfg = c.call("GET", "/api/config")
    cfg["telegram"]["bot_token"] = "123:SECRETTOKEN"
    cfg["telegram"]["chat_ids"] = ["42"]
    cfg["cameras"] = [{"id": "cam-1", "name": "Gate", "url": "rtsp://10.0.0.2/s", "username": "u", "password": "pw1"}]
    status, res = c.call("PUT", "/api/config", cfg)
    assert status == 200, res
    assert res["config"]["telegram"]["bot_token"] == SECRET
    assert res["config"]["cameras"][0]["password"] == SECRET
    # save again with masked values: real secrets must survive
    status, _ = c.call("PUT", "/api/config", res["config"])
    raw = server.store.load_raw()
    assert raw["telegram"]["bot_token"] == "123:SECRETTOKEN"
    assert raw["cameras"][0]["password"] == "pw1"
    assert b"SECRETTOKEN" not in json.dumps(c.call("GET", "/api/config")[1]).encode()


def test_invalid_config_rejected(server):
    c = Client(server)
    setup(server, c)
    status, res = c.call("PUT", "/api/config", {"cameras": [{"name": "x", "url": "ftp://nope"}]})
    assert status == 400 and "stream URL" in res["error"]


def test_status_and_pages(server):
    c = Client(server)
    setup(server, c)
    status, st = c.call("GET", "/api/status")
    assert status == 200 and st["telegram_ready"] is False
    assert c.call("GET", "/")[0] == 200
    assert c.call("GET", "/static/app.js")[0] == 200
    assert c.call("GET", "/static/../web.py")[0] == 404


def test_notifier_queue_delivers(monkeypatch):
    from motionwatch.config import TelegramConfig
    from motionwatch.notifier import Alert, TelegramNotifier

    sent = []
    n = TelegramNotifier(TelegramConfig(bot_token="1:x", chat_ids=["1"]))
    monkeypatch.setattr(n, "send_now", lambda a, retries=3: sent.append(a.caption) or True)
    n.start()
    n.submit(Alert(caption="hello"))
    n.stop()
    assert sent == ["hello"]
