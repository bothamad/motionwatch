"""Find cameras on the local network and look up their stream URLs.

Two methods, both standard library only:

* ONVIF WS-Discovery - a UDP multicast probe that most IP cameras and NVRs
  answer with their address, maker and model.
* A quick TCP scan of the local /24 for open RTSP ports, to catch cameras with
  ONVIF switched off.

With a username and password, ``onvif_stream_urls`` asks a camera for its exact
RTSP URLs (main and sub-stream) using WS-Security digest authentication.
Passwords are never guessed.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger(__name__)

WSD_ADDR = ("239.255.255.250", 3702)
RTSP_PORTS = (554, 8554)
MAX_XML = 256 * 1024

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{msg_id}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""

# Common RTSP paths by brand, used as suggestions when ONVIF can't be used.
URL_TEMPLATES: List[Tuple[str, str, str]] = [
    ("Hikvision / Annke / HiLook", "/Streaming/Channels/102", "/Streaming/Channels/101"),
    ("Dahua / Amcrest / Lorex", "/cam/realmonitor?channel=1&subtype=1", "/cam/realmonitor?channel=1&subtype=0"),
    ("Reolink", "/h264Preview_01_sub", "/h264Preview_01_main"),
    ("TP-Link Tapo / VIGI", "/stream2", "/stream1"),
    ("Uniview", "/unicast/c1/s1/live", "/unicast/c1/s0/live"),
    ("Axis", "/axis-media/media.amp?resolution=640x360", "/axis-media/media.amp"),
    ("Foscam", "/videoSub", "/videoMain"),
    ("Generic ONVIF", "/live/ch1", "/live/ch0"),
]


# ---------------------------------------------------------------- helpers
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse(xml: bytes) -> Optional[ET.Element]:
    if len(xml) > MAX_XML or b"<!DOCTYPE" in xml[:2048] or b"<!ENTITY" in xml:
        return None  # refuse anything odd: cameras never send DTDs
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        return None


def _find_all(root: ET.Element, name: str) -> List[ET.Element]:
    return [el for el in root.iter() if _local(el.tag) == name]


def _text(root: ET.Element, name: str) -> str:
    for el in root.iter():
        if _local(el.tag) == name and el.text:
            return el.text.strip()
    return ""


def local_ipv4s() -> List[str]:
    """Best-effort list of this machine's LAN IPv4 addresses (primary first)."""
    ips: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    seen, out = set(), []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if ip not in seen and addr.is_private and not addr.is_loopback and not addr.is_link_local:
            seen.add(ip)
            out.append(ip)
    return out


# ---------------------------------------------------------- WS-Discovery
def parse_probe_match(xml: bytes, sender_ip: str) -> Optional[dict]:
    root = _parse(xml)
    if root is None or not _find_all(root, "ProbeMatch"):
        return None
    xaddrs = _text(root, "XAddrs").split()
    scopes = _text(root, "Scopes").split()
    info: Dict[str, str] = {}
    for sc in scopes:
        m = re.match(r"onvif://www\.onvif\.org/(name|hardware|MfgName|mfgname|location/city)/(.+)", sc)
        if m:
            info[m.group(1).lower()] = unquote(m.group(2)).replace("_", " ").strip()
    onvif_url = ""
    for x in xaddrs:
        p = urlsplit(x)
        if p.scheme in ("http", "https") and p.hostname and ":" not in p.hostname:
            onvif_url = x
            if p.hostname == sender_ip:
                break
    host = urlsplit(onvif_url).hostname if onvif_url else sender_ip
    maker = info.get("mfgname") or ""
    name = info.get("name") or ""
    if not maker and name:
        maker = name.split()[0]
    return {
        "ip": host or sender_ip,
        "manufacturer": maker,
        "model": info.get("hardware", ""),
        "name": name,
        "onvif_url": onvif_url,
        "source": "onvif",
    }


def ws_discover(timeout: float = 3.0) -> List[dict]:
    msg = PROBE.format(msg_id=uuid.uuid4()).encode()
    found: Dict[str, dict] = {}
    sockets = []
    for ip in local_ipv4s() or ["0.0.0.0"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if ip != "0.0.0.0":
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.bind((ip, 0))
            s.settimeout(0.3)
            s.sendto(msg, WSD_ADDR)
            sockets.append(s)
        except OSError as exc:
            log.debug("WS-Discovery on %s failed: %s", ip, exc)
    deadline = time.monotonic() + timeout
    try:
        while sockets and time.monotonic() < deadline:
            for s in sockets:
                try:
                    data, (addr, _) = s.recvfrom(65535)
                except (socket.timeout, OSError):
                    continue
                cam = parse_probe_match(data, addr)
                if cam and cam["ip"] not in found:
                    found[cam["ip"]] = cam
    finally:
        for s in sockets:
            s.close()
    return list(found.values())


# --------------------------------------------------------------- port scan
def _open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def rtsp_scan(timeout: float = 0.6, ports=RTSP_PORTS) -> List[dict]:
    """Scan this machine's /24 network(s) for open RTSP ports."""
    targets = []
    for ip in local_ipv4s():
        net = ipaddress.ip_network(ip + "/24", strict=False)
        targets += [(str(h), p) for h in net.hosts() if str(h) != ip for p in ports]
    found: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=128) as pool:
        for (ip, port), ok in zip(targets, pool.map(lambda t: _open(t[0], t[1], timeout), targets)):
            if ok:
                cam = found.setdefault(ip, {"ip": ip, "manufacturer": "", "model": "", "name": "",
                                            "onvif_url": "", "source": "scan", "rtsp_ports": []})
                cam["rtsp_ports"].append(port)
    return list(found.values())


def discover(timeout: float = 3.0) -> dict:
    """Run both methods in parallel and merge by IP."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_onvif = pool.submit(ws_discover, timeout)
        f_scan = pool.submit(rtsp_scan)
        onvif, scan = f_onvif.result(), f_scan.result()
    merged: Dict[str, dict] = {c["ip"]: dict(c, rtsp_ports=[]) for c in onvif}
    for c in scan:
        if c["ip"] in merged:
            merged[c["ip"]]["rtsp_ports"] = c["rtsp_ports"]
        else:
            merged[c["ip"]] = c
    cams = sorted(merged.values(), key=lambda c: tuple(int(x) for x in c["ip"].split(".")))
    return {"cameras": cams, "networks": [ip.rsplit(".", 1)[0] + ".0/24" for ip in local_ipv4s()]}


# ------------------------------------------------------- ONVIF stream URLs
class OnvifError(Exception):
    pass


def _security_header(username: str, password: str, offset: dt.timedelta) -> str:
    nonce = os.urandom(16)
    created = (dt.datetime.now(dt.timezone.utc) + offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()

    def esc(v: str) -> str:
        return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return (
        '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
        f"<Username>{esc(username)}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'
        f'#PasswordDigest">{digest}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0'
        f'#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created></UsernameToken></Security></s:Header>"
    )


class OnvifClient:
    def __init__(self, device_url: str, username: str = "", password: str = "", timeout: float = 6.0):
        p = urlsplit(device_url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise OnvifError("Not an ONVIF address")
        self.device_url = device_url
        self.host = p.hostname
        self.username, self.password = username, password
        self.timeout = timeout
        self.offset = dt.timedelta(0)

    def call(self, url: str, body: str, auth: bool = True) -> ET.Element:
        header = _security_header(self.username, self.password, self.offset) if auth and self.username else ""
        envelope = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope '
                    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
                    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
                    'xmlns:tt="http://www.onvif.org/ver10/schema">'
                    f"{header}<s:Body>{body}</s:Body></s:Envelope>")
        try:
            resp = requests.post(url, data=envelope.encode(), timeout=self.timeout, stream=True,
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
            raw = resp.raw.read(MAX_XML + 1, decode_content=True)
        except requests.RequestException as exc:
            raise OnvifError(f"Camera did not answer ({type(exc).__name__})") from exc
        root = _parse(raw)
        if root is None:
            raise OnvifError(f"Unexpected reply from camera (HTTP {resp.status_code})")
        if resp.status_code in (400, 401, 403, 500) and _find_all(root, "Fault"):
            reason = _text(root, "Text") or _text(root, "Value") or "error"
            if resp.status_code in (401, 403) or re.search(r"auth|password|NotAuthorized|sender", reason, re.I):
                raise OnvifError("The camera rejected the username or password")
            raise OnvifError(f"Camera error: {reason}")
        if resp.status_code >= 400:
            raise OnvifError(f"Camera returned HTTP {resp.status_code}")
        return root

    def sync_clock(self) -> None:
        """WS-Security digests fail if the camera's clock is far off - measure the difference."""
        try:
            root = self.call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
            utc = next((el for el in root.iter() if _local(el.tag) == "UTCDateTime"), None)
            if utc is None:
                return
            vals = {_local(e.tag): int(e.text) for e in utc.iter() if e.text and e.text.strip().isdigit()}
            cam = dt.datetime(vals["Year"], vals["Month"], vals["Day"], vals["Hour"], vals["Minute"],
                              vals["Second"], tzinfo=dt.timezone.utc)
            self.offset = cam - dt.datetime.now(dt.timezone.utc)
        except (OnvifError, KeyError, ValueError, StopIteration):
            pass

    def media_url(self) -> str:
        root = self.call(self.device_url,
                         '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>')
        for el in root.iter():
            if _local(el.tag) == "Media":
                x = _text(el, "XAddr")
                if x:
                    # Some cameras report an internal IP; keep their path but use the address we reached.
                    p = urlsplit(x)
                    return urlunsplit((p.scheme, f"{self.host}:{p.port}" if p.port else self.host,
                                       p.path, p.query, ""))
        return self.device_url

    def streams(self) -> List[dict]:
        self.sync_clock()
        media = self.media_url()
        root = self.call(media, "<trt:GetProfiles/>")
        profiles = []
        for prof in _find_all(root, "Profiles"):
            token = prof.get("token")
            if not token:
                continue
            enc = next((e for e in prof.iter() if _local(e.tag) == "VideoEncoderConfiguration"), None)
            w = h = 0
            codec = ""
            if enc is not None:
                try:
                    w, h = int(_text(enc, "Width") or 0), int(_text(enc, "Height") or 0)
                except ValueError:
                    pass
                codec = _text(enc, "Encoding")
            profiles.append({"token": token, "name": _text(prof, "Name") or token,
                             "width": w, "height": h, "codec": codec})
        out = []
        for prof in profiles[:6]:
            body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                    "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                    f"<trt:ProfileToken>{prof['token']}</trt:ProfileToken></trt:GetStreamUri>")
            try:
                uri = _text(self.call(media, body), "Uri")
            except OnvifError as exc:
                log.debug("GetStreamUri %s failed: %s", prof["token"], exc)
                continue
            if not uri.lower().startswith("rtsp"):
                continue
            p = urlsplit(uri)
            netloc = p.netloc.rsplit("@", 1)[-1]  # drop any credentials the camera embedded
            hostport = netloc.rsplit(":", 1)
            port = f":{hostport[1]}" if len(hostport) == 2 and hostport[1].isdigit() else ""
            uri = urlunsplit((p.scheme, f"{self.host}{port}", p.path, p.query, ""))
            out.append(dict(prof, url=uri))
        if not out:
            raise OnvifError("The camera didn't report any RTSP streams")
        return out


def recommended(streams: List[dict]) -> dict:
    """Prefer a sub-stream around 640 px wide: same detection quality, a fraction of the CPU."""
    def score(s):
        w = s.get("width") or 9999
        return (0 if w >= 320 else 1, abs(w - 640))

    return sorted(streams, key=score)[0]


def onvif_stream_urls(device_url: str, username: str, password: str) -> dict:
    streams = OnvifClient(device_url, username, password).streams()
    return {"streams": streams, "recommended": recommended(streams)["url"]}


def template_urls(ip: str, port: int = 554) -> List[dict]:
    hp = ip if port == 554 else f"{ip}:{port}"
    return [{"brand": b, "sub": f"rtsp://{hp}{sub}", "main": f"rtsp://{hp}{main}"} for b, sub, main in URL_TEMPLATES]
