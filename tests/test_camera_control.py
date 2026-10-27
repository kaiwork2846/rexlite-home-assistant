"""Camera-side ONVIF control against a scripted camera and Frigate."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_camera_control_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
for name in ("camera_probe", "vigi_setup", "camera_control", "frigate_bridge"):
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", PACKAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
control_module = sys.modules[f"{package.__name__}.camera_control"]
bridge_module = sys.modules[f"{package.__name__}.frigate_bridge"]
CameraError = sys.modules[f"{package.__name__}.camera_probe"].CameraError

CAMERA = "m6d459e1198784b8fa0eec25491d50018"
PASSWORD = "s3cret!Pass#1"
RAW = f"""
go2rtc:
  streams:
    {CAMERA}:
      - rtsp://admin:s3cret%21Pass%231@192.168.2.170:554/stream1#backchannel=0
    {CAMERA}_sub:
      - rtsp://admin:s3cret%21Pass%231@192.168.2.170:554/stream2#backchannel=0
cameras:
  {CAMERA}:
    friendly_name: VIGI C540V
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/{CAMERA}
          roles: [record]
        - path: rtsp://127.0.0.1:8554/{CAMERA}_sub
          roles: [detect]
    onvif:
      host: 192.168.2.170
      port: 2020
      user: admin
      password: "{PASSWORD}"
"""

ENVELOPE = (
    '<?xml version="1.0"?><s:Envelope '
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
    'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
    "<s:Body>{}</s:Body></s:Envelope>"
)
BASE = "http://192.168.2.170:2020"


class Response:
    def __init__(self, status: int, payload=b"", headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return str(self.payload).encode()

    async def text(self) -> str:
        if isinstance(self.payload, bytes):
            return self.payload.decode()
        return (
            self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        )


class Site:
    """A VIGI-like ONVIF camera (port 2020) and the hub's Frigate (5050)."""

    def __init__(self, *, imaging: bool = True, password: str = PASSWORD):
        self.imaging = imaging
        self.password = password
        self.raw = RAW
        self.actions: list[tuple[str, str]] = []
        self.presets = {"1": "大門", "2": "停車場"}
        self.image = {
            "Brightness": 50,
            "ColorSaturation": 50,
            "Contrast": 50,
            "Sharpness": 50,
            "IrCutFilter": "AUTO",
        }
        self.clock_mode = "Manual"
        self.encoders = [
            {"w": 2560, "h": 1440, "fps": 15, "kbps": 2048},
            {"w": 640, "h": 360, "fps": 15, "kbps": 512},
        ]
        self.go2rtc: dict[str, str] = {}
        self.offers: list[tuple[str, str]] = []
        self.backchannel = True
        self.ntp = ""

    def request(self, method: str, url, **kwargs):
        parts = urlsplit(str(url))
        if parts.port == 5050:
            if parts.path == "/api/version":
                return Response(200, "0.17.1-416a9b7")
            if parts.path == "/api/config/raw":
                return Response(200, json.dumps(self.raw))
            if parts.path.endswith("/latest.jpg"):
                return Response(200, b"\xff\xd8jpeg\xff\xd9")
            return Response(404, "")
        if parts.port == 1984:
            query = dict(
                pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair
            )
            if method == "PUT":
                self.go2rtc[unquote(query["name"])] = unquote(query["src"])
                return Response(200, "")
            if method == "GET":
                return Response(200, json.dumps({n: {} for n in self.go2rtc}))
            if method == "POST" and parts.path == "/api/webrtc":
                name = unquote(query["src"])
                self.offers.append((name, kwargs["json"]["sdp"]))
                audio = (
                    "a=sendrecv"
                    if name.endswith("_talk") and self.backchannel
                    else "a=sendonly"
                )
                answer = (
                    "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=sendonly\r\n"
                    f"m=audio 9 UDP/TLS/RTP/SAVPF 0\r\n{audio}\r\n"
                )
                return Response(200, json.dumps({"type": "answer", "sdp": answer}))
            return Response(404, "")
        if parts.port != 2020:
            raise OSError("connection refused")
        body = kwargs["data"].decode()
        action = re.search(r'action="([^"]+)"', kwargs["headers"]["Content-Type"])
        name = action.group(1).rsplit("/", 1)[-1]
        self.actions.append((name, body))
        if name == "GetSystemDateAndTime":
            return self.reply(
                f"<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
                f"<tt:DateTimeType>{self.clock_mode}</tt:DateTimeType>"
                "<tt:TimeZone><tt:TZ>UTC-8</tt:TZ></tt:TimeZone><tt:UTCDateTime>"
                "<tt:Date><tt:Year>2025</tt:Year><tt:Month>9</tt:Month><tt:Day>4</tt:Day>"
                "</tt:Date><tt:Time><tt:Hour>1</tt:Hour><tt:Minute>2</tt:Minute>"
                "<tt:Second>3</tt:Second></tt:Time></tt:UTCDateTime>"
                "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
            )
        if self.password not in (PASSWORD,) or "Username>admin<" not in body:
            return Response(400, ENVELOPE.format("<s:Fault>NotAuthorized</s:Fault>"))
        handler = getattr(self, f"on_{name}", None)
        if handler is None:
            return Response(
                400, ENVELOPE.format("<s:Fault>ActionNotSupported</s:Fault>")
            )
        return handler(body)

    def reply(self, inner: str):
        return Response(200, ENVELOPE.format(inner))

    def on_GetCapabilities(self, _body):
        imaging = (
            f"<tt:Imaging><tt:XAddr>{BASE}/onvif/service</tt:XAddr></tt:Imaging>"
            if self.imaging
            else ""
        )
        return self.reply(
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"{imaging}"
            # The camera reports its old DHCP address; the hub uses the real one.
            "<tt:Media><tt:XAddr>http://192.168.0.60:2020/onvif/service</tt:XAddr></tt:Media>"
            f"<tt:PTZ><tt:XAddr>{BASE}/onvif/service</tt:XAddr></tt:PTZ>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def on_GetProfiles(self, _body):
        profiles = "".join(
            f'<trt:Profiles token="profile_{i}"><tt:Name>{name}</tt:Name>'
            "<tt:VideoSourceConfiguration><tt:SourceToken>vsrc</tt:SourceToken>"
            "</tt:VideoSourceConfiguration>"
            f'<tt:VideoEncoderConfiguration token="enc_{i}">'
            "<tt:Name>enc</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{e['w']}</tt:Width>"
            f"<tt:Height>{e['h']}</tt:Height></tt:Resolution><tt:Quality>4</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{e['fps']}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{e['kbps']}</tt:BitrateLimit></tt:RateControl>"
            "</tt:VideoEncoderConfiguration></trt:Profiles>"
            for i, (name, e) in enumerate(
                (("mainStream", self.encoders[0]), ("minorStream", self.encoders[1])), 1
            )
        )
        return self.reply(
            f"<trt:GetProfilesResponse>{profiles}</trt:GetProfilesResponse>"
        )

    def on_GetVideoEncoderConfigurationOptions(self, body):
        main = "enc_1" in body
        sizes = ((2560, 1440), (1920, 1080), (1280, 720)) if main else ((640, 360),)
        resolutions = "".join(
            f"<tt:ResolutionsAvailable><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height>"
            "</tt:ResolutionsAvailable>"
            for w, h in sizes
        )
        return self.reply(
            "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
            f"<tt:H264>{resolutions}<tt:FrameRateRange><tt:Min>1</tt:Min>"
            "<tt:Max>25</tt:Max></tt:FrameRateRange></tt:H264>"
            "<tt:Extension><tt:H264><tt:BitrateRange><tt:Min>256</tt:Min>"
            "<tt:Max>4096</tt:Max></tt:BitrateRange></tt:H264></tt:Extension>"
            "</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>"
        )

    def on_SetVideoEncoderConfiguration(self, body):
        index = 0 if 'token="enc_1"' in body else 1
        for key, tag in (
            ("w", "Width"),
            ("h", "Height"),
            ("fps", "FrameRateLimit"),
            ("kbps", "BitrateLimit"),
        ):
            found = re.search(rf":{tag}>(\d+)<", body)
            if found:
                self.encoders[index][key] = int(found.group(1))
        return self.reply("<trt:SetVideoEncoderConfigurationResponse/>")

    def on_GetPresets(self, _body):
        presets = "".join(
            f'<tptz:Preset token="{t}"><tt:Name>{n}</tt:Name></tptz:Preset>'
            for t, n in self.presets.items()
        )
        return self.reply(
            f"<tptz:GetPresetsResponse>{presets}</tptz:GetPresetsResponse>"
        )

    def on_ContinuousMove(self, _body):
        return self.reply("<tptz:ContinuousMoveResponse/>")

    def on_Stop(self, _body):
        return self.reply("<tptz:StopResponse/>")

    def on_GotoPreset(self, _body):
        return self.reply("<tptz:GotoPresetResponse/>")

    def on_SetPreset(self, body):
        name = re.search(r"PresetName>([^<]+)<", body).group(1)
        token = re.search(r"PresetToken>([^<]+)<", body)
        token = token.group(1) if token else str(len(self.presets) + 1)
        self.presets[token] = name
        return self.reply(
            f"<tptz:SetPresetResponse><tptz:PresetToken>{token}</tptz:PresetToken>"
            "</tptz:SetPresetResponse>"
        )

    def on_RemovePreset(self, body):
        self.presets.pop(re.search(r"PresetToken>([^<]+)<", body).group(1), None)
        return self.reply("<tptz:RemovePresetResponse/>")

    def on_GetImagingSettings(self, _body):
        fields = "".join(f"<tt:{k}>{v}</tt:{k}>" for k, v in self.image.items())
        return self.reply(
            "<timg:GetImagingSettingsResponse><timg:ImagingSettings>"
            f"{fields}</timg:ImagingSettings></timg:GetImagingSettingsResponse>"
        )

    def on_GetOptions(self, _body):
        ranges = "".join(
            f"<tt:{k}><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:{k}>"
            for k in ("Brightness", "ColorSaturation", "Contrast", "Sharpness")
        )
        return self.reply(
            "<timg:GetOptionsResponse><timg:ImagingOptions>"
            f"{ranges}<tt:IrCutFilterModes>ON</tt:IrCutFilterModes>"
            "<tt:IrCutFilterModes>OFF</tt:IrCutFilterModes>"
            "<tt:IrCutFilterModes>AUTO</tt:IrCutFilterModes>"
            "</timg:ImagingOptions></timg:GetOptionsResponse>"
        )

    def on_SetImagingSettings(self, body):
        for key in self.image:
            found = re.search(rf"<tt:{key}>([^<]+)</tt:{key}>", body)
            if found:
                value = found.group(1)
                self.image[key] = value if key == "IrCutFilter" else float(value)
        return self.reply("<timg:SetImagingSettingsResponse/>")

    def on_GetNTP(self, _body):
        manual = (
            f"<tt:NTPManual><tt:Type>DNS</tt:Type><tt:DNSname>{self.ntp}</tt:DNSname>"
            "</tt:NTPManual>"
            if self.ntp
            else ""
        )
        return self.reply(
            "<tds:GetNTPResponse><tds:NTPInformation><tt:FromDHCP>false</tt:FromDHCP>"
            f"{manual}</tds:NTPInformation></tds:GetNTPResponse>"
        )

    def on_SetNTP(self, body):
        self.ntp = re.search(r"DNSname>([^<]+)<", body).group(1)
        return self.reply("<tds:SetNTPResponse/>")

    def on_SetSystemDateAndTime(self, body):
        self.clock_mode = re.search(r"DateTimeType>([^<]+)<", body).group(1)
        return self.reply("<tds:SetSystemDateAndTimeResponse/>")


def bridge(site: Site):
    async def sleep(_seconds):
        await asyncio.sleep(0)

    return bridge_module.FrigateBridge(
        store=None,
        session=site,
        create_task=lambda coro, name: asyncio.get_running_loop().create_task(coro),
        time_zone=lambda: "Asia/Taipei",
        sleep=sleep,
    )


class AccessTests(unittest.TestCase):
    def test_onvif_block_first(self):
        self.assertEqual(
            control_module.camera_access(RAW, CAMERA),
            {
                "host": "192.168.2.170",
                "port": 2020,
                "username": "admin",
                "password": PASSWORD,
            },
        )

    def test_restream_credentials_are_decoded(self):
        raw = RAW.split("    onvif:")[0]
        access = control_module.camera_access(raw, CAMERA)
        self.assertEqual(
            (access["host"], access["username"], access["password"], access["port"]),
            ("192.168.2.170", "admin", PASSWORD, None),
        )

    def test_missing_camera_or_credentials(self):
        with self.assertRaises(CameraError) as caught:
            control_module.camera_access(RAW, "nope")
        self.assertEqual(caught.exception.code, "frigate_camera_not_found")
        raw = (
            "cameras:\n  gate:\n    ffmpeg:\n      inputs:\n"
            "        - path: rtsp://{FRIGATE_USER}:{FRIGATE_PASS}@192.168.1.9/live\n"
        )
        with self.assertRaises(CameraError) as caught:
            control_module.camera_access(raw, "gate")
        self.assertEqual(caught.exception.code, "camera_credentials_missing")


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_info_lists_ptz_image_and_clock(self):
        site = Site()
        info = await bridge(site).camera_info({"camera": CAMERA})
        self.assertEqual(info["camera"], CAMERA)
        self.assertEqual(info["port"], 2020)
        self.assertEqual(
            info["ptz"]["presets"],
            [{"token": "1", "name": "大門"}, {"token": "2", "name": "停車場"}],
        )
        self.assertEqual(
            info["image"]["brightness"], {"value": 50.0, "min": 0.0, "max": 100.0}
        )
        self.assertEqual(info["image"]["irCut"]["options"], ["AUTO", "ON", "OFF"])
        self.assertEqual(info["clock"]["ntp"], False)
        self.assertEqual(info["clock"]["cameraTime"], "2025-09-04T01:02:03+00:00")
        self.assertLess(info["clock"]["driftSeconds"], 0)
        # No credentials in the result.
        self.assertNotIn(PASSWORD, json.dumps(info, ensure_ascii=False))
        self.assertNotIn("s3cret", json.dumps(info))

    async def test_unsupported_imaging_is_null(self):
        info = await bridge(Site(imaging=False)).camera_info({"camera": CAMERA})
        self.assertIsNone(info["image"])
        self.assertIsNotNone(info["ptz"])

    async def test_move_stops_and_returns_a_frame(self):
        site = Site()
        result = await bridge(site).camera_control(
            {"camera": CAMERA, "op": "move", "direction": "left", "speed": 0.5}
        )
        names = [name for name, _body in site.actions]
        self.assertEqual(names[-2:], ["ContinuousMove", "Stop"])
        move = dict(site.actions)["ContinuousMove"]
        self.assertIn('x="-0.50" y="0.00"', move)
        self.assertIn("<tptz:ProfileToken>profile_1</tptz:ProfileToken>", move)
        self.assertTrue(result["snapshot"])
        zoom = await bridge(site).camera_control(
            {"camera": CAMERA, "op": "move", "direction": "zoom_in"}
        )
        self.assertTrue(zoom["snapshot"])
        self.assertIn("<tt:Zoom", site.actions[-2][1])
        quiet = await bridge(site).camera_control(
            {"camera": CAMERA, "op": "move", "direction": "up", "snapshot": False}
        )
        self.assertNotIn("snapshot", quiet)

    async def test_presets(self):
        site = Site()
        b = bridge(site)
        saved = await b.camera_control(
            {"camera": CAMERA, "op": "save_preset", "name": "  後門  "}
        )
        self.assertEqual(saved["token"], "3")
        self.assertIn({"token": "3", "name": "後門"}, saved["presets"])
        renamed = await b.camera_control(
            {"camera": CAMERA, "op": "save_preset", "name": "大門口", "token": "1"}
        )
        self.assertIn({"token": "1", "name": "大門口"}, renamed["presets"])
        removed = await b.camera_control(
            {"camera": CAMERA, "op": "remove_preset", "token": "2"}
        )
        self.assertNotIn("2", [p["token"] for p in removed["presets"]])
        went = await b.camera_control({"camera": CAMERA, "op": "goto", "token": "1"})
        self.assertTrue(went["snapshot"])
        with self.assertRaises(bridge_module.FrigateError):
            await b.camera_control(
                {"camera": CAMERA, "op": "save_preset", "name": "\x07"}
            )

    async def test_image_keeps_schema_order(self):
        site = Site()
        result = await bridge(site).camera_control(
            {
                "camera": CAMERA,
                "op": "image",
                "image": {"sharpness": 70, "brightness": 60, "irCut": "OFF"},
            }
        )
        body = dict(site.actions)["SetImagingSettings"]
        self.assertLess(body.index("Brightness"), body.index("IrCutFilter"))
        self.assertLess(body.index("IrCutFilter"), body.index("Sharpness"))
        self.assertIn("<timg:ForcePersistence>true", body)
        self.assertEqual(result["image"]["brightness"]["value"], 60.0)
        self.assertEqual(result["image"]["irCut"]["value"], "OFF")

    async def test_clock_ntp_and_manual(self):
        site = Site()
        b = bridge(site)
        result = await b.camera_control({"camera": CAMERA, "op": "clock", "ntp": True})
        self.assertEqual(site.ntp, control_module.DEFAULT_NTP)
        self.assertEqual(result["clock"]["ntp"], True)
        self.assertEqual(result["clock"]["server"], control_module.DEFAULT_NTP)
        self.assertIn(
            "<tt:TZ>UTC-8</tt:TZ>", dict(site.actions)["SetSystemDateAndTime"]
        )
        await b.camera_control({"camera": CAMERA, "op": "clock", "ntp": False})
        self.assertEqual(site.clock_mode, "Manual")
        self.assertIn("<tt:Year>", site.actions[-3][1])

    async def test_quality_lists_supported_sizes_and_rewrites_encoder(self):
        site = Site()
        info = await bridge(site).camera_info({"camera": CAMERA})
        main, sub = info["quality"]
        self.assertEqual(
            (main["name"], main["width"], main["height"]), ("主串流", 2560, 1440)
        )
        self.assertEqual(main["resolutions"], [[2560, 1440], [1920, 1080], [1280, 720]])
        self.assertEqual(
            (main["fpsRange"], main["bitrateRange"]), ([1, 25], [256, 4096])
        )
        self.assertEqual((sub["name"], sub["resolutions"]), ("子串流", [[640, 360]]))
        result = await bridge(site).camera_control(
            {
                "camera": CAMERA,
                "op": "quality",
                "profile": "profile_1",
                "quality": {"width": 1920, "height": 1080, "bitrate": 3072},
            }
        )
        body = dict(site.actions)["SetVideoEncoderConfiguration"]
        # The whole configuration goes back, with only the changed fields.
        self.assertIn('token="enc_1"', body)
        self.assertIn(">enc<", body)
        self.assertIn(">15<", body)
        self.assertEqual(
            site.encoders[0], {"w": 1920, "h": 1080, "fps": 15, "kbps": 3072}
        )
        self.assertEqual(result["quality"][0]["width"], 1920)

    async def test_webrtc_answers_from_go2rtc(self):
        site = Site()
        b = bridge(site)
        live = await b.webrtc({"camera": CAMERA, "offer": "v=0\r\nm=video 9"})
        self.assertEqual(
            (live["quality"], live["qualities"], live["talk"]),
            ("sub", ["main", "sub"], False),
        )
        self.assertEqual(site.offers[-1][0], f"{CAMERA}_sub")
        self.assertTrue(live["answer"].startswith("v=0"))
        main = await b.webrtc({"camera": CAMERA, "offer": "v=0", "quality": "main"})
        self.assertEqual((main["quality"], site.offers[-1][0]), ("main", CAMERA))
        # Talking opens a restream with the camera's backchannel left on.
        talk = await b.webrtc({"camera": CAMERA, "offer": "v=0", "talk": True})
        self.assertTrue(talk["talk"])
        self.assertEqual(site.offers[-1][0], f"{CAMERA}_talk")
        source = site.go2rtc[f"{CAMERA}_talk"]
        self.assertTrue(source.endswith("/stream2"))
        self.assertNotIn("#", source)
        self.assertNotIn(PASSWORD, json.dumps(talk))
        site.backchannel = False
        self.assertFalse(
            (await b.webrtc({"camera": CAMERA, "offer": "v=0", "talk": True}))["talk"]
        )
        with self.assertRaises(bridge_module.FrigateError):
            await b.webrtc({"camera": CAMERA, "offer": "nope"})

    async def test_changed_password(self):
        site = Site(password="changed")
        with self.assertRaises(CameraError) as caught:
            await bridge(site).camera_info({"camera": CAMERA})
        self.assertEqual(caught.exception.code, "camera_auth_failed")
        self.assertIn("重新加入", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
