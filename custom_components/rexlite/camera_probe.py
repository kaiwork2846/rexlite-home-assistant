"""Find and check IP cameras from the Home Assistant host.

The hub shares the camera LAN, so it can do what the cloud cannot: send
WS-Discovery probes, ask a camera's ONVIF media service for its RTSP streams
and DESCRIBE a stream with the installer's credentials before Frigate is
touched. Credentials are only used for the request at hand and never appear
in a result or an error message.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import os
import re
import socket
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

try:
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover - Home Assistant ships defusedxml
    from xml.etree import ElementTree as ET  # noqa: N817

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
# Common ONVIF service ports: Tapo and VIGI Wi-Fi models (2020), most PoE
# cameras (80), Hikvision/Uniview (8000) and Xiongmai "NVT" boards (8899).
ONVIF_PORTS = (2020, 80, 8000, 8899)
MAX_XML_BYTES = 256 * 1024
MAX_PROFILES = 6
DISCOVERY_ADDRESS = ("239.255.255.250", 3702)

TPLINK = "tplink"
# RTSP paths tried when a camera has no usable ONVIF media service, per vendor;
# the first two entries are the main and the sub stream.
FALLBACK_PATHS: dict[str, tuple[str, ...]] = {
    TPLINK: ("/stream1", "/stream2"),
    "hikvision": ("/Streaming/Channels/101", "/Streaming/Channels/102"),
    "dahua": (
        "/cam/realmonitor?channel=1&subtype=0",
        "/cam/realmonitor?channel=1&subtype=1",
    ),
    "reolink": ("/h264Preview_01_main", "/h264Preview_01_sub"),
}
_VENDOR_MARKERS = (
    # TP-Link cameras share one ONVIF endpoint UUID prefix, including VIGI
    # InSight models whose ONVIF name carries no brand.
    (
        TPLINK,
        (
            "tp-link",
            "tplink",
            "tp-ipc",
            "tapo",
            "vigi",
            "insight",
            "3fa1fe68-b915-4053-a3e1",
        ),
    ),
    ("hikvision", ("hikvision", "hikvison")),
    ("dahua", ("dahua", "amcrest")),
    ("reolink", ("reolink",)),
)
_NS = (
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema"'
)
_WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
_WSU = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_NONCE_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
_AUTH_FAULTS = ("notauthorized", "failedauthentication", "not authorized")


class CameraError(Exception):
    """User-facing failure; ``code`` is stable for the operation platform."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _invalid(message: str) -> CameraError:
    return CameraError("camera_invalid_request", message)


# Addresses and URLs --------------------------------------------------------


def camera_host(value: Any) -> str:
    """A camera is addressed by its RFC1918 IPv4 address."""

    text = str(value or "").strip()
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        raise _invalid("請輸入攝影機的 IPv4 位址") from None
    if not any(address in network for network in _PRIVATE_NETWORKS):
        raise _invalid("攝影機需位於區域網路（私有 IP）")
    return str(address)


def camera_port(value: Any, default: int = 80) -> int:
    if value in (None, ""):
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise _invalid("連接埠需為 1–65535") from None
    if not 1 <= port <= 65535:
        raise _invalid("連接埠需為 1–65535")
    return port


def split_credentials(value: Any) -> tuple[str, str, str]:
    """Split ``rtsp://user:pass@host/path`` into the bare URI and credentials."""

    text = str(value or "").strip()
    parts = urlsplit(text)
    if not parts.username and not parts.password:
        return text, "", ""
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise _invalid("串流網址格式不正確") from None
    netloc = host if port is None else f"{host}:{port}"
    bare = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return bare, unquote(parts.username or ""), unquote(parts.password or "")


def stream_uri(value: Any, host: str) -> str:
    """Normalize an RTSP URI: no credentials or fragment, same camera host."""

    text = str(value or "").strip()
    if not text or len(text) > 1024 or any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise _invalid("串流網址格式不正確")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("rtsp", "rtsps") or not parts.hostname:
        raise _invalid("串流網址需以 rtsp:// 開頭")
    if parts.username or parts.password:
        raise _invalid("串流網址不可包含帳號密碼")
    if parts.hostname.lower() != host.lower():
        raise _invalid("串流網址需指向同一台攝影機")
    try:
        port = parts.port
    except ValueError:
        raise _invalid("串流網址埠號不正確") from None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit(
        (parts.scheme.lower(), netloc, parts.path or "/", parts.query, "")
    )


def with_credentials(uri: str, username: str, password: str) -> str:
    """Put percent-encoded credentials into a bare stream URI."""

    if not username:
        return uri
    parts = urlsplit(uri)
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    netloc = f"{auth}@{parts.hostname}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def with_raw_credentials(uri: str, username: str, password: str) -> str:
    """Credentials as typed, for Frigate's own ffmpeg/ffprobe inputs.

    Frigate percent-encodes the password of those paths itself
    (``escape_special_characters``), so encoding it here would encode twice.
    go2rtc parses URLs strictly and needs :func:`with_credentials` instead.
    """

    if not username:
        return uri
    parts = urlsplit(uri)
    auth = f"{username}:{password}" if password else username
    netloc = f"{auth}@{parts.hostname}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redact(uri: str) -> str:
    """Drop any credentials from a URI before it leaves this module."""

    with contextlib.suppress(ValueError, CameraError):
        bare, _user, _password = split_credentials(uri)
        return bare
    return ""


def vendor_of(*hints: Any) -> str | None:
    text = " ".join(str(hint or "") for hint in hints).lower()
    for vendor, markers in _VENDOR_MARKERS:
        if any(marker in text for marker in markers):
            return vendor
    return None


# XML -------------------------------------------------------------------------


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_xml(data: bytes | str) -> Any:
    raw = data.encode() if isinstance(data, str) else data
    if len(raw) > MAX_XML_BYTES:
        raise ValueError("XML response too large")
    return ET.fromstring(raw)


def _children(element: Any, name: str) -> list[Any]:
    return [child for child in list(element) if _local(child.tag) == name]


def _first(element: Any, *path: str) -> Any:
    """First descendant following ``path`` by local names (any depth for [0])."""

    if element is None or not path:
        return element
    head, rest = path[0], path[1:]
    for candidate in element.iter():
        if _local(candidate.tag) != head:
            continue
        found = _first_child_path(candidate, rest)
        if found is not None:
            return found
    return None


def _first_child_path(element: Any, path: tuple[str, ...]) -> Any:
    for name in path:
        matches = _children(element, name)
        if not matches:
            return None
        element = matches[0]
    return element


def _text(element: Any, *path: str) -> str:
    found = _first(element, *path) if path else element
    return (found.text or "").strip() if found is not None else ""


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# WS-Discovery ------------------------------------------------------------------


def probe_message(types: str) -> bytes:
    namespace = (
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"'
        if types.startswith("dn:")
        else 'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        f'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" {namespace}>'
        f"<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>"
        '<w:To e:mustUnderstand="true">'
        "urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        '<w:Action e:mustUnderstand="true">'
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>"
        f"<e:Body><d:Probe><d:Types>{types}</d:Types></d:Probe></e:Body></e:Envelope>"
    ).encode()


def _scope(scopes: list[str], key: str) -> str:
    prefix = f"onvif://www.onvif.org/{key}/"
    for scope in scopes:
        if scope.lower().startswith(prefix.lower()):
            return unquote(scope[len(prefix) :]).replace("_", " ").strip()
    return ""


def parse_probe_match(data: bytes, source: str) -> dict | None:
    """One ONVIF camera from a ProbeMatches reply, or None for anything else.

    Windows and NAS hosts answer WS-Discovery too (port 5357); only matches
    that declare an ONVIF type or ONVIF scopes are cameras.
    """

    try:
        root = _parse_xml(data)
    except (ET.ParseError, ValueError):
        return None
    match = _first(root, "ProbeMatch")
    if match is None:
        return None
    types = _text(match, "Types").split()
    scopes = _text(match, "Scopes").split()
    onvif = any(
        t.endswith(":NetworkVideoTransmitter") or t == "NetworkVideoTransmitter"
        for t in types
    ) or any(s.lower().startswith("onvif://www.onvif.org/") for s in scopes)
    if not onvif:
        return None
    endpoints: list[tuple[str, int, str]] = []
    for xaddr in _text(match, "XAddrs").split():
        parts = urlsplit(xaddr)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        try:
            ipaddress.IPv4Address(parts.hostname)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            continue
        endpoints.append((parts.hostname, port, xaddr))
    endpoints.sort(key=lambda item: item[0] != source)
    host, port, xaddr = endpoints[0] if endpoints else (source, 80, "")
    try:
        host = camera_host(host)
    except CameraError:
        return None
    name = _scope(scopes, "name")
    hardware = _scope(scopes, "hardware")
    endpoint = _text(match, "EndpointReference", "Address")[:120]
    return {
        "host": host,
        "port": port,
        "name": name or hardware or "ONVIF 攝影機",
        "hardware": hardware,
        "vendor": vendor_of(name, hardware, _scope(scopes, "manufacturer"), endpoint),
        "mac": _scope(scopes, "MAC").replace(" ", "").upper()[:17],
        "endpoint": endpoint,
        "xaddr": xaddr[:200],
    }


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, found: dict[str, dict]) -> None:
        self.found = found

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        camera = parse_probe_match(data, addr[0])
        if camera is not None:
            self.found.setdefault(camera["host"], camera)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover - ICMP noise
        return None


def _multicast_socket(interface: str | None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setblocking(False)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    if interface:
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)
        )
    sock.bind((interface or "0.0.0.0", 0))
    return sock


async def discover_onvif(
    interfaces: Iterable[str | None] = (None,),
    *,
    seconds: float = 3.0,
    target: tuple[str, int] = DISCOVERY_ADDRESS,
) -> list[dict]:
    """Multicast ONVIF probes on every LAN interface and collect the answers."""

    loop = asyncio.get_running_loop()
    found: dict[str, dict] = {}
    transports = []
    for interface in dict.fromkeys(interfaces or (None,)):
        with contextlib.suppress(OSError):
            transport, _protocol = await loop.create_datagram_endpoint(
                lambda: _DiscoveryProtocol(found), sock=_multicast_socket(interface)
            )
            transports.append(transport)
    if not transports:
        raise CameraError("camera_discovery_failed", "主機無法發送搜尋封包")
    messages = (
        probe_message("dn:NetworkVideoTransmitter"),
        probe_message("tds:Device"),
    )
    pause = min(0.4, seconds / 4)
    try:
        # UDP may drop a probe; a second round a moment later catches slow NICs.
        for _round in range(2):
            for transport in transports:
                for message in messages:
                    with contextlib.suppress(OSError):
                        transport.sendto(message, target)
            await asyncio.sleep(pause)
        await asyncio.sleep(max(0.0, seconds - 2 * pause))
    finally:
        for transport in transports:
            transport.close()
    return sorted(found.values(), key=lambda c: ipaddress.IPv4Address(c["host"]))


# HTTP and RTSP digest authentication ------------------------------------------


def parse_challenges(values: Iterable[str]) -> dict[str, dict[str, str]]:
    """``WWW-Authenticate`` headers by scheme (lower case)."""

    challenges: dict[str, dict[str, str]] = {}
    for value in values:
        scheme, _sep, rest = str(value).strip().partition(" ")
        params = {
            key.lower(): quoted if quoted or not bare else bare
            for key, quoted, bare in re.findall(
                r'([A-Za-z0-9_-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))', rest
            )
        }
        challenges.setdefault(scheme.lower(), params)
    return challenges


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def authorization(
    challenges: dict[str, dict[str, str]],
    method: str,
    uri: str,
    username: str,
    password: str,
) -> str | None:
    """An ``Authorization`` value for the strongest offered scheme."""

    digest = challenges.get("digest")
    if digest is not None:
        algorithm = digest.get("algorithm", "MD5")
        hash_name = "sha256" if algorithm.upper().startswith("SHA-256") else "md5"

        def h(value: str) -> str:
            return hashlib.new(hash_name, value.encode()).hexdigest()

        realm, nonce = digest.get("realm", ""), digest.get("nonce", "")
        cnonce = os.urandom(8).hex()
        ha1 = h(f"{username}:{realm}:{password}")
        if algorithm.upper().endswith("-SESS"):
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")
        qops = [q.strip() for q in digest.get("qop", "").split(",") if q.strip()]
        fields = [
            f'username="{_quoted(username)}"',
            f'realm="{_quoted(realm)}"',
            f'nonce="{_quoted(nonce)}"',
            f'uri="{_quoted(uri)}"',
        ]
        if "auth" in qops:
            response = h(f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}")
            fields += ["qop=auth", "nc=00000001", f'cnonce="{cnonce}"']
        else:
            response = h(f"{ha1}:{nonce}:{ha2}")
        fields.append(f'response="{response}"')
        if "opaque" in digest:
            fields.append(f'opaque="{_quoted(digest["opaque"])}"')
        if "algorithm" in digest:
            fields.append(f"algorithm={algorithm}")
        return "Digest " + ", ".join(fields)
    if "basic" in challenges:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {token}"
    return None


def _timeout(seconds: float) -> Any:
    try:
        import aiohttp
    except ImportError:  # unit tests use a duck-typed session
        return seconds
    factory = getattr(aiohttp, "ClientTimeout", None)
    return factory(total=seconds) if factory else seconds


def _header_values(headers: Any, name: str) -> list[str]:
    getall = getattr(headers, "getall", None)
    if callable(getall):
        with contextlib.suppress(KeyError):
            return [str(v) for v in getall(name)]
        return []
    value = (headers or {}).get(name) or (headers or {}).get(name.lower())
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


async def _http(
    session: Any,
    url: str,
    body: bytes,
    headers: dict[str, str],
    seconds: float,
) -> tuple[int, Any, bytes]:
    try:
        async with session.request(
            "POST", url, data=body, headers=headers, timeout=_timeout(seconds)
        ) as response:
            data = await response.read()
            return response.status, response.headers, data[: MAX_XML_BYTES + 1]
    except CameraError:
        raise
    except Exception as err:  # noqa: BLE001 - every network failure is one code
        host = urlsplit(url).netloc
        raise CameraError("camera_unreachable", f"無法連線到 {host}") from err


# ONVIF -------------------------------------------------------------------------


class OnvifClient:
    """Just enough ONVIF (Device + Media v1) to read a camera's RTSP streams."""

    def __init__(
        self,
        session: Any,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        seconds: float = 6.0,
    ) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.seconds = seconds
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.offset = timedelta()

    def _security(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (datetime.now(UTC) + self.offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        ).decode()
        return (
            f'<s:Header><Security s:mustUnderstand="1" xmlns="{_WSSE}"><UsernameToken>'
            f"<Username>{escape(self.username)}</Username>"
            f'<Password Type="{_DIGEST_TYPE}">{digest}</Password>'
            f'<Nonce EncodingType="{_NONCE_TYPE}">'
            f"{base64.b64encode(nonce).decode()}</Nonce>"
            f'<Created xmlns="{_WSU}">{created}</Created>'
            "</UsernameToken></Security></s:Header>"
        )

    async def call(self, url: str, action: str, body: str, *, auth: bool = True) -> Any:
        envelope = (
            f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {_NS}>'
            f"{self._security() if auth else ''}<s:Body>{body}</s:Body></s:Envelope>"
        ).encode()
        headers = {
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'
        }
        status, response_headers, data = await _http(
            self.session, url, envelope, headers, self.seconds
        )
        if status == 401 and auth and self.username:
            # A few cameras guard ONVIF with HTTP digest instead of WS-Security.
            header = authorization(
                parse_challenges(_header_values(response_headers, "WWW-Authenticate")),
                "POST",
                urlsplit(url).path or "/",
                self.username,
                self.password,
            )
            if header:
                status, response_headers, data = await _http(
                    self.session,
                    url,
                    envelope,
                    {**headers, "Authorization": header},
                    self.seconds,
                )
        try:
            root = _parse_xml(data) if data else None
        except (ET.ParseError, ValueError):
            root = None
        fault = _first(root, "Fault") if root is not None else None
        if status in (401, 403) or (fault is not None and _is_auth_fault(fault)):
            raise CameraError("camera_auth_failed", "攝影機帳號或密碼錯誤")
        if fault is not None or root is None or status >= 400:
            raise CameraError(
                "camera_onvif_failed", f"攝影機的 ONVIF 服務拒絕了請求（HTTP {status}）"
            )
        return root

    async def sync_clock(self) -> None:
        """Confirm ONVIF answers on this port and adopt the camera's clock.

        WS-Security tokens are signed with the camera's time, so a camera
        without NTP still accepts them. A port that answers with something
        other than SOAP (a web UI on port 80, say) raises like a closed one.
        """

        envelope = (
            f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {_NS}>'
            "<s:Body><tds:GetSystemDateAndTime/></s:Body></s:Envelope>"
        ).encode()
        action = "http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime"
        status, _headers, data = await _http(
            self.session,
            self.device_url,
            envelope,
            {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
            min(self.seconds, 4.0),
        )
        if status in (401, 403):
            return  # ONVIF is here but even the clock needs credentials.
        try:
            root = _parse_xml(data)
        except (ET.ParseError, ValueError):
            root = None
        if root is None or _local(root.tag) != "Envelope":
            raise CameraError(
                "camera_onvif_failed", f"{self.host}:{self.port} 不是 ONVIF 服務"
            )
        utc = _first(root, "UTCDateTime")
        if utc is None:
            return
        values = [
            _int(_text(utc, *path))
            for path in (
                ("Date", "Year"),
                ("Date", "Month"),
                ("Date", "Day"),
                ("Time", "Hour"),
                ("Time", "Minute"),
                ("Time", "Second"),
            )
        ]
        if all(value is not None for value in values):
            with contextlib.suppress(ValueError, TypeError):
                camera = datetime(*values, tzinfo=UTC)  # type: ignore[arg-type,misc]
                self.offset = camera - datetime.now(UTC)

    async def device_information(self) -> dict:
        root = await self.call(
            self.device_url,
            "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
            "<tds:GetDeviceInformation/>",
        )
        return {
            "manufacturer": _text(root, "Manufacturer")[:80],
            "model": _text(root, "Model")[:80],
            "firmware": _text(root, "FirmwareVersion")[:80],
            "serial": _text(root, "SerialNumber")[:80],
        }

    async def services(self) -> tuple[str, bool]:
        """The media service URL and whether the camera reports PTZ."""

        root = await self.call(
            self.device_url,
            "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
        )
        media = _text(root, "Media", "XAddr")
        ptz = bool(_text(root, "PTZ", "XAddr"))
        return self._local_url(media) or self.device_url, ptz

    def _local_url(self, xaddr: str) -> str:
        parts = urlsplit(xaddr)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ""
        # Cameras behind a changed DHCP lease still report their old address.
        port = parts.port or self.port
        return urlunsplit(
            (parts.scheme, f"{self.host}:{port}", parts.path, parts.query, "")
        )

    async def profiles(self, media_url: str) -> list[dict]:
        root = await self.call(
            media_url,
            "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
            "<trt:GetProfiles/>",
        )
        profiles: list[dict] = []
        for element in root.iter():
            if _local(element.tag) != "Profiles" or not element.get("token"):
                continue
            encoder = _first_child_path(element, ("VideoEncoderConfiguration",))
            if encoder is None:
                continue
            source = _first_child_path(
                element, ("VideoSourceConfiguration", "SourceToken")
            )
            profiles.append(
                {
                    "token": element.get("token")[:64],
                    "name": _text(_first_child_path(element, ("Name",)))[:64],
                    "codec": _codec(_text(encoder, "Encoding")),
                    "width": _int(_text(encoder, "Resolution", "Width")),
                    "height": _int(_text(encoder, "Resolution", "Height")),
                    "fps": _int(_text(encoder, "RateControl", "FrameRateLimit")),
                    "source": _text(source)[:64],
                }
            )
            if len(profiles) >= MAX_PROFILES:
                break
        return profiles

    async def stream_uri(self, media_url: str, token: str) -> str:
        root = await self.call(
            media_url,
            "http://www.onvif.org/ver10/media/wsdl/GetStreamUri",
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            f"</trt:StreamSetup><trt:ProfileToken>{escape(token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>",
        )
        uri, _user, _password = split_credentials(_text(root, "MediaUri", "Uri"))
        parts = urlsplit(uri)
        if parts.scheme.lower() not in ("rtsp", "rtsps") or not parts.hostname:
            return ""
        port = parts.port
        netloc = self.host if port is None else f"{self.host}:{port}"
        return urlunsplit(
            (parts.scheme.lower(), netloc, parts.path or "/", parts.query, "")
        )


def _is_auth_fault(fault: Any) -> bool:
    detail = " ".join(
        (element.text or "") for element in fault.iter() if element.text
    ).lower()
    return any(marker in detail for marker in _AUTH_FAULTS)


def _codec(value: str) -> str:
    text = value.strip().upper()
    if text in ("H265", "HEVC", "H.265"):
        return "H265"
    if text in ("H264", "AVC", "H.264"):
        return "H264"
    if text in ("JPEG", "MJPEG", "MJPG"):
        return "MJPEG"
    return text[:16]


async def onvif_streams(
    session: Any,
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    seconds: float = 6.0,
    alternatives: Iterable[int] = ONVIF_PORTS,
) -> dict:
    """Device information and every media profile's RTSP stream.

    The installer's port is tried first, then the usual ONVIF ports, because
    scanners and installers often only know the camera's web port.
    """

    failures: list[CameraError] = []
    for candidate in dict.fromkeys((port, *alternatives)):
        client = OnvifClient(
            session, host, candidate, username, password, seconds=seconds
        )
        try:
            await client.sync_clock()
        except CameraError as err:
            failures.append(err)
            continue
        device = await client.device_information()
        media_url, ptz = await client.services()
        profiles = await client.profiles(media_url)
        for profile in profiles:
            with contextlib.suppress(CameraError):
                profile["uri"] = await client.stream_uri(media_url, profile["token"])
        return {
            "port": candidate,
            "device": device,
            "ptz": ptz,
            "profiles": [p for p in profiles if p.get("uri")],
        }
    # Something answered but not as ONVIF beats "nothing answered at all".
    for failure in failures:
        if failure.code != "camera_unreachable":
            raise CameraError("camera_onvif_failed", "攝影機未開啟 ONVIF 服務")
    raise CameraError("camera_unreachable", f"無法連線到攝影機 {host}")


# RTSP --------------------------------------------------------------------------


def sdp_codecs(sdp: str) -> list[str]:
    codecs: list[str] = []
    media = ""
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            fields = line[2:].split()
            media = fields[0] if fields else ""
            if media == "video" and "26" in fields[3:]:
                codecs.append("MJPEG")
        elif line.startswith("a=rtpmap:") and media == "video":
            _sep, _space, encoding = line.partition(" ")
            codecs.append(_codec(encoding.split("/", 1)[0]))
    return list(dict.fromkeys(c for c in codecs if c))


class _RtspConnection:
    """One RTSP control connection; DESCRIBE requests share it.

    Cameras such as gortsplib/MediaMTX servers tie a digest nonce to the
    connection that issued it, so the challenge is answered on that same
    connection, as RTSP clients normally do.
    """

    def __init__(self, host: str, port: int, seconds: float) -> None:
        self.host = host
        self.port = port
        self.seconds = seconds
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> _RtspConnection:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.seconds
            )
        except (OSError, TimeoutError) as err:
            raise CameraError(
                "camera_unreachable", f"無法連線到攝影機串流 {self.host}:{self.port}"
            ) from err
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

    async def describe(
        self, uri: str, cseq: int, auth: str | None
    ) -> tuple[int, dict[str, list[str]], str]:
        assert self.reader is not None and self.writer is not None
        request = (
            f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n"
            "Accept: application/sdp\r\nUser-Agent: REXLiTE-Camera-Probe\r\n"
        )
        if auth:
            request += f"Authorization: {auth}\r\n"
        try:
            self.writer.write((request + "\r\n").encode())
            await self.writer.drain()
            head = await asyncio.wait_for(
                self.reader.readuntil(b"\r\n\r\n"), self.seconds
            )
            lines = head.decode("latin-1").split("\r\n")
            match = re.match(r"RTSP/\d\.\d\s+(\d{3})", lines[0])
            if not match:
                raise CameraError(
                    "camera_rtsp_failed", "攝影機的串流服務回應格式不正確"
                )
            headers: dict[str, list[str]] = {}
            for line in lines[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers.setdefault(name.strip().lower(), []).append(value.strip())
            length = _int((headers.get("content-length") or ["0"])[0]) or 0
            body = b""
            if 0 < length <= 65536:
                body = await asyncio.wait_for(
                    self.reader.readexactly(length), self.seconds
                )
            return int(match.group(1)), headers, body.decode("utf-8", "replace")
        except CameraError:
            raise
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as err:
            raise _ConnectionDropped(
                f"攝影機串流 {self.host}:{self.port} 沒有回應"
            ) from err


class _ConnectionDropped(CameraError):
    def __init__(self, message: str) -> None:
        super().__init__("camera_rtsp_failed", message)


async def rtsp_check(
    uri: str, username: str, password: str, *, seconds: float = 6.0
) -> dict:
    """DESCRIBE a stream: reachable, accepted credentials, path exists, codec."""

    parts = urlsplit(uri)
    host = parts.hostname or ""
    port = parts.port or 554
    if parts.scheme == "rtsps":
        # TLS streams are left to Frigate's own probe.
        return {"status": None, "codecs": []}
    request_uri = urlunsplit(("rtsp", parts.netloc, parts.path or "/", parts.query, ""))
    header: str | None = None
    # Some cameras hang up after every reply; their digest nonce outlives the
    # connection, so the computed answer is sent again on a fresh one.
    for attempt in range(2):
        async with _RtspConnection(host, port, seconds) as connection:
            try:
                if header is None:
                    status, headers, body = await connection.describe(
                        request_uri, 1, None
                    )
                    if status == 401 and username:
                        header = authorization(
                            parse_challenges(headers.get("www-authenticate", [])),
                            "DESCRIBE",
                            request_uri,
                            username,
                            password,
                        )
                        if header:
                            status, headers, body = await connection.describe(
                                request_uri, 2, header
                            )
                else:
                    status, headers, body = await connection.describe(
                        request_uri, 1, header
                    )
            except _ConnectionDropped:
                if attempt or header is None:
                    raise
                continue
        break
    if status in (401, 403):
        raise CameraError("camera_auth_failed", "攝影機帳號或密碼錯誤")
    if status in (404, 454):
        raise CameraError("camera_stream_not_found", "攝影機沒有這個串流路徑")
    if status != 200:
        raise CameraError("camera_rtsp_failed", f"攝影機拒絕串流（RTSP {status}）")
    return {"status": status, "codecs": sdp_codecs(body)}


def fallback_streams(host: str, vendor: str | None) -> list[str]:
    """Likely RTSP paths when ONVIF cannot list them, the vendor's first."""

    order = [vendor] if vendor in FALLBACK_PATHS else []
    order += [v for v in FALLBACK_PATHS if v not in order]
    return [f"rtsp://{host}:554{path}" for v in order for path in FALLBACK_PATHS[v]]
