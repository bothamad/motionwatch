import base64
import hashlib
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from motionwatch import discovery
from motionwatch.discovery import OnvifError, onvif_stream_urls, parse_probe_match, recommended

PROBE_MATCH = b"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsdd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <SOAP-ENV:Body><wsdd:ProbeMatches><wsdd:ProbeMatch>
  <wsdd:Types>dn:NetworkVideoTransmitter</wsdd:Types>
  <wsdd:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/HIKVISION
   onvif://www.onvif.org/hardware/DS-2CD2143G2-I onvif://www.onvif.org/location/city/hangzhou</wsdd:Scopes>
  <wsdd:XAddrs>http://192.168.1.64/onvif/device_service http://[fe80::1]/onvif/device_service</wsdd:XAddrs>
 </wsdd:ProbeMatch></wsdd:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>"""


def test_parse_probe_match():
    cam = parse_probe_match(PROBE_MATCH, "192.168.1.64")
    assert cam["ip"] == "192.168.1.64"
    assert cam["manufacturer"] == "HIKVISION"
    assert cam["model"] == "DS-2CD2143G2-I"
    assert cam["onvif_url"] == "http://192.168.1.64/onvif/device_service"


def test_parse_rejects_garbage_and_entities():
    assert parse_probe_match(b"not xml", "1.2.3.4") is None
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    assert parse_probe_match(bomb, "1.2.3.4") is None


def test_recommended_prefers_substream():
    streams = [{"url": "main", "width": 2560}, {"url": "sub", "width": 640}, {"url": "tiny", "width": 176}]
    assert recommended(streams)["url"] == "sub"


def test_port_check():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    assert discovery._open("127.0.0.1", port, 1)
    srv.close()
    assert not discovery._open("127.0.0.1", port, 1)


def test_template_urls():
    t = discovery.template_urls("10.0.0.9", 8554)
    assert t[0]["sub"].startswith("rtsp://10.0.0.9:8554/")


# ---- fake ONVIF camera ---------------------------------------------------
USER, PASSWORD = "admin", "s3cret!"


def _valid_digest(body: str) -> bool:
    try:
        nonce = base64.b64decode(re.search(r"<Nonce[^>]*>([^<]+)</Nonce>", body).group(1))
        created = re.search(r"<Created[^>]*>([^<]+)</Created>", body).group(1)
        digest = re.search(r"<Password[^>]*>([^<]+)</Password>", body).group(1)
        user = re.search(r"<Username>([^<]+)</Username>", body).group(1)
    except AttributeError:
        return False
    expect = base64.b64encode(hashlib.sha1(nonce + created.encode() + PASSWORD.encode()).digest()).decode()
    return user == USER and digest == expect


class FakeCamera(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, status, inner):
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
                f"<s:Body>{inner}</s:Body></s:Envelope>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        port = self.server.server_address[1]
        if "GetSystemDateAndTime" in body:
            return self.reply(200, "<tds:GetSystemDateAndTimeResponse xmlns:tds='x'><tds:SystemDateAndTime>"
                                   "<tt:UTCDateTime><tt:Time><tt:Hour>1</tt:Hour><tt:Minute>2</tt:Minute>"
                                   "<tt:Second>3</tt:Second></tt:Time><tt:Date><tt:Year>2026</tt:Year>"
                                   "<tt:Month>11</tt:Month><tt:Day>22</tt:Day></tt:Date></tt:UTCDateTime>"
                                   "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
        if not _valid_digest(body):
            return self.reply(400, "<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>"
                                   "<s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason></s:Fault>")
        if "GetCapabilities" in body:
            # Real cameras often report an internal address; MotionWatch must keep the one it reached.
            return self.reply(200, f"<tt:Media><tt:XAddr>http://10.9.9.9:{port}/onvif/media</tt:XAddr></tt:Media>")
        if "GetProfiles" in body:
            return self.reply(200, "<trt:Profiles token='main'><tt:Name>mainStream</tt:Name><tt:VideoEncoderConfiguration>"
                                   "<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>2560</tt:Width>"
                                   "<tt:Height>1440</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>"
                                   "<trt:Profiles token='sub'><tt:Name>subStream</tt:Name><tt:VideoEncoderConfiguration>"
                                   "<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>640</tt:Width>"
                                   "<tt:Height>360</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>")
        if "GetStreamUri" in body:
            token = re.search(r"<trt:ProfileToken>(\w+)</trt:ProfileToken>", body).group(1)
            ch = "101" if token == "main" else "102"
            return self.reply(200, f"<tt:Uri>rtsp://admin:leak@10.9.9.9:554/Streaming/Channels/{ch}</tt:Uri>")
        self.reply(500, "<s:Fault><s:Reason><s:Text>unknown</s:Text></s:Reason></s:Fault>")


@pytest.fixture
def camera():
    srv = HTTPServer(("127.0.0.1", 0), FakeCamera)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/onvif/device_service"
    srv.shutdown()


def test_onvif_stream_lookup(camera):
    r = onvif_stream_urls(camera, USER, PASSWORD)
    assert [s["width"] for s in r["streams"]] == [2560, 640]
    # recommended = sub-stream, host rewritten to the address we reached, embedded credentials removed
    assert r["recommended"] == "rtsp://127.0.0.1:554/Streaming/Channels/102"


def test_onvif_wrong_password(camera):
    with pytest.raises(OnvifError, match="username or password"):
        onvif_stream_urls(camera, USER, "wrong")
