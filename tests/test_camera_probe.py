"""Camera discovery, ONVIF and RTSP checks against scripted cameras."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import re
import socket
import sys
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_camera_probe_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.camera_probe", PACKAGE / "camera_probe.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

HOST = "192.168.1.72"
USER = "admin"
PASSWORD = "p@ss:w/rd#1"

TAPO_MATCH = b"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:wsdd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:tdn="http://www.onvif.org/ver10/network/wsdl">
<SOAP-ENV:Body><wsdd:ProbeMatches><wsdd:ProbeMatch>
<wsa:EndpointReference><wsa:Address>uuid:3fa1fe68-b915-4053-a3e1-c006c3afec0e</wsa:Address></wsa:EndpointReference>
<wsdd:Types>tdn:NetworkVideoTransmitter</wsdd:Types>
<wsdd:Scopes>onvif://www.onvif.org/name/TP-IPC
 onvif://www.onvif.org/hardware/C540V onvif://www.onvif.org/Profile/Streaming
 onvif://www.onvif.org/location/ShenZhen</wsdd:Scopes>
<wsdd:XAddrs>http://192.168.1.72:2020/onvif/device_service</wsdd:XAddrs>
<wsdd:MetadataVersion>1</wsdd:MetadataVersion>
</wsdd:ProbeMatch></wsdd:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>"""

# What a Windows PC or a NAS running wsdd answers (port 5357, no ONVIF).
WSD_COMPUTER_MATCH = b"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof"
 xmlns:pub="http://schemas.microsoft.com/windows/pub/2005/07">
<soap:Body><wsd:ProbeMatches><wsd:ProbeMatch>
<wsa:EndpointReference><wsa:Address>urn:uuid:94ae2a8b-0f7e-5b3e-a1c2-1b2c3d4e5f60</wsa:Address></wsa:EndpointReference>
<wsd:Types>wsdp:Device pub:Computer</wsd:Types>
<wsd:XAddrs>http://fishbone-cam:5357/94ae2a8b-0f7e-5b3e-a1c2-1b2c3d4e5f60/</wsd:XAddrs>
<wsd:MetadataVersion>2</wsd:MetadataVersion>
</wsd:ProbeMatch></wsd:ProbeMatches></soap:Body></soap:Envelope>"""


def envelope(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"<SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


AUTH_FAULT = envelope(
    "<SOAP-ENV:Fault><SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value>"
    "<SOAP-ENV:Subcode><SOAP-ENV:Value>ter:NotAuthorized</SOAP-ENV:Value>"
    "</SOAP-ENV:Subcode></SOAP-ENV:Code><SOAP-ENV:Reason>"
    '<SOAP-ENV:Text xml:lang="en">Sender not Authorized</SOAP-ENV:Text>'
    "</SOAP-ENV:Reason></SOAP-ENV:Fault>"
)
CAMERA_TIME = datetime(2026, 10, 12, 7, 5, 9, tzinfo=UTC)
ANSWERS = {
    "GetSystemDateAndTime": envelope(
        "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
        "<tt:DateTimeType>Manual</tt:DateTimeType><tt:UTCDateTime>"
        "<tt:Time><tt:Hour>7</tt:Hour><tt:Minute>5</tt:Minute><tt:Second>9</tt:Second></tt:Time>"
        "<tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>12</tt:Day></tt:Date>"
        "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
    ),
    "GetDeviceInformation": envelope(
        "<tds:GetDeviceInformationResponse><tds:Manufacturer>TP-LINK</tds:Manufacturer>"
        "<tds:Model>VIGI C540V</tds:Model>"
        "<tds:FirmwareVersion>1.1.3 Build 230707</tds:FirmwareVersion>"
        "<tds:SerialNumber>2a9b3c</tds:SerialNumber><tds:HardwareId>1.0</tds:HardwareId>"
        "</tds:GetDeviceInformationResponse>"
    ),
    # Old DHCP address in the XAddr: the client must keep talking to HOST.
    "GetCapabilities": envelope(
        "<tds:GetCapabilitiesResponse><tds:Capabilities>"
        "<tt:Device><tt:XAddr>http://192.168.0.60:2020/onvif/device_service</tt:XAddr></tt:Device>"
        "<tt:Media><tt:XAddr>http://192.168.0.60:2020/onvif/service</tt:XAddr></tt:Media>"
        "<tt:PTZ><tt:XAddr>http://192.168.0.60:2020/onvif/service</tt:XAddr></tt:PTZ>"
        "</tds:Capabilities></tds:GetCapabilitiesResponse>"
    ),
    "GetProfiles": envelope(
        '<trt:GetProfilesResponse><trt:Profiles token="profile_1" fixed="true">'
        "<tt:Name>mainStream</tt:Name>"
        '<tt:VideoSourceConfiguration token="vsconf">'
        "<tt:Name>VideoSourceConfig</tt:Name>"
        "<tt:SourceToken>vsrc</tt:SourceToken></tt:VideoSourceConfiguration>"
        '<tt:VideoEncoderConfiguration token="main"><tt:Name>VideoEncoder_1</tt:Name>'
        "<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>2560</tt:Width>"
        "<tt:Height>1440</tt:Height></tt:Resolution><tt:Quality>4</tt:Quality>"
        "<tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit>"
        "<tt:BitrateLimit>2048</tt:BitrateLimit></tt:RateControl></tt:VideoEncoderConfiguration>"
        '</trt:Profiles><trt:Profiles token="profile_2" fixed="true">'
        "<tt:Name>minorStream</tt:Name>"
        '<tt:VideoSourceConfiguration token="vsconf">'
        "<tt:SourceToken>vsrc</tt:SourceToken>"
        "</tt:VideoSourceConfiguration>"
        '<tt:VideoEncoderConfiguration token="minor"><tt:Encoding>H264</tt:Encoding>'
        "<tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>"
        "<tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit></tt:RateControl>"
        "</tt:VideoEncoderConfiguration></trt:Profiles></trt:GetProfilesResponse>"
    ),
}


def stream_answer(token: str) -> str:
    path = "stream1" if token == "profile_1" else "stream2"
    return envelope(
        "<trt:GetStreamUriResponse><trt:MediaUri>"
        f"<tt:Uri>rtsp://192.168.0.60:554/{path}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )


class Response:
    def __init__(self, status: int, body: str = "", headers: dict | None = None):
        self.status = status
        self.body = body.encode()
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        return self.body


class Camera:
    """A scripted ONVIF camera that checks WS-Security digests."""

    def __init__(self, ports=(2020,), password=PASSWORD, web_ports=(80,)):
        self.ports = ports
        self.password = password
        self.web_ports = web_ports
        self.calls: list[tuple[str, str, bytes]] = []

    def request(self, method: str, url, **kwargs):
        url = str(url)
        body = kwargs.get("data") or b""
        self.calls.append((method, url, body))
        port = int(re.search(r":(\d+)/", url).group(1))
        if port in self.web_ports:
            return Response(404, "<html>Not Found</html>")
        if port not in self.ports:
            raise OSError("connection refused")
        action = re.search(r"/wsdl/(\w+)", kwargs["headers"]["Content-Type"]).group(1)
        if action == "GetSystemDateAndTime":
            return Response(200, ANSWERS[action])
        if not self._authorized(body.decode()):
            return Response(400, AUTH_FAULT)
        if action == "GetStreamUri":
            token = re.search(r"<trt:ProfileToken>(.*?)</", body.decode()).group(1)
            return Response(200, stream_answer(token))
        return Response(200, ANSWERS[action])

    def _authorized(self, body: str) -> bool:
        user = re.search(r"<Username>(.*?)</Username>", body)
        digest = re.search(r'<Password Type="[^"]+">(.*?)</Password>', body)
        nonce = re.search(r"<Nonce [^>]+>(.*?)</Nonce>", body)
        created = re.search(r"<Created [^>]+>(.*?)</Created>", body)
        if not (user and digest and nonce and created) or user.group(1) != USER:
            return False
        stamp = datetime.strptime(created.group(1), "%Y-%m-%dT%H:%M:%S.000Z")
        # The camera rejects tokens that are not created on its own clock.
        if abs((stamp.replace(tzinfo=UTC) - CAMERA_TIME).total_seconds()) > 5:
            return False
        expected = base64.b64encode(
            hashlib.sha1(
                base64.b64decode(nonce.group(1))
                + created.group(1).encode()
                + self.password.encode()
            ).digest()
        ).decode()
        return digest.group(1) == expected


class UrlTests(unittest.TestCase):
    def test_camera_host_is_a_private_ipv4_address(self):
        self.assertEqual(m.camera_host(" 192.168.2.170 "), "192.168.2.170")
        for bad in ("fishbone-cam", "8.8.8.8", "127.0.0.1", "", "192.168.1.300"):
            with self.assertRaises(m.CameraError) as caught:
                m.camera_host(bad)
            self.assertEqual(caught.exception.code, "camera_invalid_request")

    def test_stream_uris_stay_on_the_camera_without_credentials(self):
        self.assertEqual(
            m.stream_uri("RTSP://192.168.1.72:554/stream1#x", HOST),
            "rtsp://192.168.1.72:554/stream1",
        )
        self.assertEqual(
            m.stream_uri(
                "rtsp://192.168.1.72/cam/realmonitor?channel=1&subtype=0", HOST
            ),
            "rtsp://192.168.1.72/cam/realmonitor?channel=1&subtype=0",
        )
        for bad in (
            "http://192.168.1.72/stream1",
            "rtsp://192.168.1.99:554/stream1",
            "rtsp://admin:pw@192.168.1.72/stream1",
            "rtsp://192.168.1.72:99999/x",
            "rtsp://192.168.1.72/a b",
        ):
            with self.assertRaises(m.CameraError):
                m.stream_uri(bad, HOST)

    def test_credentials_are_percent_encoded_once(self):
        uri = m.with_credentials("rtsp://192.168.1.72:554/stream1", USER, PASSWORD)
        self.assertEqual(
            uri, "rtsp://admin:p%40ss%3Aw%2Frd%231@192.168.1.72:554/stream1"
        )
        self.assertEqual(
            m.split_credentials(uri),
            ("rtsp://192.168.1.72:554/stream1", USER, PASSWORD),
        )
        self.assertEqual(m.redact(uri), "rtsp://192.168.1.72:554/stream1")
        # Frigate's own ffmpeg/ffprobe encodes the password itself.
        self.assertEqual(
            m.with_raw_credentials("rtsp://192.168.1.72:554/stream1", USER, PASSWORD),
            "rtsp://admin:p@ss:w/rd#1@192.168.1.72:554/stream1",
        )
        self.assertEqual(
            m.with_credentials("rtsp://192.168.1.72/s", "", "x"),
            "rtsp://192.168.1.72/s",
        )

    def test_vendor_and_fallback_paths(self):
        self.assertEqual(m.vendor_of("TP-LINK", "VIGI C540V"), m.TPLINK)
        self.assertEqual(m.vendor_of("TP-IPC"), m.TPLINK)
        # VIGI InSight (seen on the office LAN as "InSight-LPR345Z").
        self.assertEqual(m.vendor_of("InSight-LPR345Z"), m.TPLINK)
        self.assertEqual(
            m.vendor_of("IPC", "uuid:3fa1fe68-b915-4053-a3e1-6c4cbcaac95e"), m.TPLINK
        )
        self.assertIsNone(m.vendor_of("ACME"))
        paths = m.fallback_streams(HOST, m.TPLINK)
        self.assertEqual(
            paths[:2],
            ["rtsp://192.168.1.72:554/stream1", "rtsp://192.168.1.72:554/stream2"],
        )
        self.assertEqual(len(paths), 8)


class DiscoveryTests(unittest.TestCase):
    def test_onvif_camera_is_parsed(self):
        camera = m.parse_probe_match(TAPO_MATCH, "192.168.1.72")
        self.assertEqual(
            {k: camera[k] for k in ("host", "port", "name", "hardware", "vendor")},
            {
                "host": "192.168.1.72",
                "port": 2020,
                "name": "TP-IPC",
                "hardware": "C540V",
                "vendor": m.TPLINK,
            },
        )

    def test_windows_and_nas_wsd_hosts_are_not_cameras(self):
        self.assertIsNone(m.parse_probe_match(WSD_COMPUTER_MATCH, "192.168.1.30"))
        self.assertIsNone(m.parse_probe_match(b"<not xml", "192.168.1.30"))
        self.assertIsNone(
            m.parse_probe_match(
                b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
                "192.168.1.30",
            )
        )

    def test_probe_asks_for_video_transmitters(self):
        message = m.probe_message("dn:NetworkVideoTransmitter").decode()
        self.assertIn("<d:Types>dn:NetworkVideoTransmitter</d:Types>", message)
        self.assertIn('xmlns:dn="http://www.onvif.org/ver10/network/wsdl"', message)
        self.assertIn('xmlns:tds="', m.probe_message("tds:Device").decode())


class DiscoveryRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_answers_are_collected_and_deduplicated(self):
        loop = asyncio.get_running_loop()
        probes: list[bytes] = []

        class Responder(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                probes.append(data)
                self.transport.sendto(TAPO_MATCH, addr)
                self.transport.sendto(WSD_COMPUTER_MATCH, addr)

        transport, _ = await loop.create_datagram_endpoint(
            Responder, local_addr=("127.0.0.1", 0), family=socket.AF_INET
        )
        try:
            found = await m.discover_onvif(
                [None], seconds=0.6, target=transport.get_extra_info("sockname")
            )
        finally:
            transport.close()
        self.assertEqual([c["host"] for c in found], ["192.168.1.72"])
        self.assertEqual(len(probes), 4)  # two probe types, sent twice


class DigestTests(unittest.TestCase):
    def test_rfc2617_example(self):
        challenge = m.parse_challenges(
            [
                'Digest realm="testrealm@host.com", qop="auth,auth-int", '
                'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
                'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
            ]
        )
        header = m.authorization(
            challenge, "GET", "/dir/index.html", "Mufasa", "Circle Of Life"
        )
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
        ha1 = hashlib.md5(b"Mufasa:testrealm@host.com:Circle Of Life").hexdigest()
        ha2 = hashlib.md5(b"GET:/dir/index.html").hexdigest()
        expected = hashlib.md5(
            f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:00000001:{fields['cnonce']}:auth:{ha2}".encode()
        ).hexdigest()
        self.assertEqual(fields["response"], expected)
        self.assertEqual(fields["opaque"], "5ccc069c403ebaf9f0171e9517f40e41")
        self.assertTrue(header.startswith("Digest "))

    def test_basic_when_digest_is_not_offered(self):
        header = m.authorization(
            m.parse_challenges(['Basic realm="cam"']), "DESCRIBE", "rtsp://x/", "a", "b"
        )
        self.assertEqual(header, "Basic " + base64.b64encode(b"a:b").decode())
        self.assertIsNone(m.authorization({}, "DESCRIBE", "rtsp://x/", "a", "b"))

    def test_sdp_video_codecs(self):
        sdp = (
            "v=0\r\nm=audio 0 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"
            "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\n"
        )
        self.assertEqual(m.sdp_codecs(sdp), ["H265"])
        self.assertEqual(m.sdp_codecs("m=video 0 RTP/AVP 26\r\n"), ["MJPEG"])


class OnvifTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_from_a_camera_on_port_2020(self):
        camera = Camera()
        found = await m.onvif_streams(camera, HOST, 80, USER, PASSWORD)
        self.assertEqual(found["port"], 2020)
        self.assertTrue(found["ptz"])
        self.assertEqual(found["device"]["model"], "VIGI C540V")
        self.assertEqual(
            [
                (p["uri"], p["width"], p["height"], p["codec"], p["fps"])
                for p in found["profiles"]
            ],
            [
                ("rtsp://192.168.1.72:554/stream1", 2560, 1440, "H264", 15),
                ("rtsp://192.168.1.72:554/stream2", 640, 360, "H264", 15),
            ],
        )
        # Port 80 answered with a web page, so 2020 was tried next; every
        # media call went to the camera's current address.
        urls = [url for _method, url, _body in camera.calls]
        self.assertEqual(urls[0], "http://192.168.1.72:80/onvif/device_service")
        self.assertIn("http://192.168.1.72:2020/onvif/service", urls)
        self.assertFalse(any("192.168.0.60" in url for url in urls))
        self.assertFalse(
            any(PASSWORD.encode() in body for _m, _u, body in camera.calls)
        )

    async def test_wrong_password_is_an_auth_failure(self):
        with self.assertRaises(m.CameraError) as caught:
            await m.onvif_streams(Camera(), HOST, 2020, USER, "wrong")
        self.assertEqual(caught.exception.code, "camera_auth_failed")
        self.assertNotIn("wrong", caught.exception.message)

    async def test_no_onvif_anywhere(self):
        with self.assertRaises(m.CameraError) as caught:
            await m.onvif_streams(
                Camera(ports=(), web_ports=()), HOST, 80, USER, PASSWORD
            )
        self.assertEqual(caught.exception.code, "camera_unreachable")
        with self.assertRaises(m.CameraError) as caught:
            await m.onvif_streams(
                Camera(ports=(), web_ports=(80,)), HOST, 80, USER, PASSWORD
            )
        self.assertEqual(caught.exception.code, "camera_onvif_failed")


class RtspServer:
    """A digest-protected RTSP server that answers DESCRIBE.

    ``keep_alive`` mimics gortsplib/MediaMTX: one nonce per connection, valid
    only on it. Otherwise the server hangs up after every reply and accepts
    its nonce on any connection.
    """

    def __init__(self, password=PASSWORD, keep_alive=False):
        self.password = password
        self.keep_alive = keep_alive
        self.requests: list[str] = []
        self.connections = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        self.connections += 1
        nonce = f"n{self.connections}" if self.keep_alive else "0a4f113b"
        while True:
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
            except asyncio.IncompleteReadError:
                break
            self.requests.append(head)
            uri = head.split(" ", 2)[1]
            cseq = re.search(r"CSeq: (\d+)", head).group(1)
            if uri.endswith("/missing"):
                writer.write(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n".encode())
            elif "Authorization:" not in head or not self._valid(head, uri, nonce):
                writer.write(
                    f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n"
                    'WWW-Authenticate: Basic realm="TP-LINK IP-Camera"\r\n'
                    'WWW-Authenticate: Digest realm="TP-LINK IP-Camera", '
                    f'nonce="{nonce}"\r\n\r\n'.encode()
                )
            else:
                sdp = "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                writer.write(
                    f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
                    "Content-Type: application/sdp\r\n"
                    f"Content-Length: {len(sdp)}\r\n\r\n{sdp}".encode()
                )
            await writer.drain()
            if not self.keep_alive:
                break
        writer.close()

    def _valid(self, head: str, uri: str, nonce: str) -> bool:
        line = head.split("Authorization:", 1)[1].split("\r\n")[0]
        if not line.strip().startswith("Digest "):
            return False  # this camera insists on digest
        fields = dict(re.findall(r'(\w+)="([^"]*)"', line))
        ha1 = hashlib.md5(
            f"{USER}:TP-LINK IP-Camera:{self.password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
        return (
            fields.get("response")
            == hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        )


class RtspTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_bound_nonce_is_answered_on_the_same_connection(self):
        async with RtspServer(keep_alive=True) as server:
            result = await m.rtsp_check(
                f"rtsp://127.0.0.1:{server.port}/stream1", USER, PASSWORD
            )
        self.assertEqual(result, {"status": 200, "codecs": ["H264"]})
        self.assertEqual(server.connections, 1)

    async def test_digest_describe_reports_the_codec(self):
        async with RtspServer() as server:
            result = await m.rtsp_check(
                f"rtsp://127.0.0.1:{server.port}/stream1", USER, PASSWORD
            )
        self.assertEqual(result, {"status": 200, "codecs": ["H264"]})
        self.assertEqual(len(server.requests), 2)
        self.assertNotIn(PASSWORD, "".join(server.requests))

    async def test_failures_have_distinct_codes(self):
        async with RtspServer() as server:
            base = f"rtsp://127.0.0.1:{server.port}"
            for uri, password, code in (
                (f"{base}/stream1", "wrong", "camera_auth_failed"),
                (f"{base}/missing", PASSWORD, "camera_stream_not_found"),
            ):
                with self.assertRaises(m.CameraError) as caught:
                    await m.rtsp_check(uri, USER, password)
                self.assertEqual(caught.exception.code, code)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed = sock.getsockname()[1]
        with self.assertRaises(m.CameraError) as caught:
            await m.rtsp_check(
                f"rtsp://127.0.0.1:{closed}/stream1", USER, PASSWORD, seconds=2
            )
        self.assertEqual(caught.exception.code, "camera_unreachable")


if __name__ == "__main__":
    unittest.main()
