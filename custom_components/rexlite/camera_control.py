"""Camera-side settings over ONVIF: PTZ, presets, image and clock.

The hub already holds every Frigate camera's credentials in Frigate's own
configuration file, so it reads them from Frigate's local ``/api/config/raw``
for the request at hand instead of asking the installer again or keeping a
second copy. They never appear in a result, a log or an error message.

Movement is a short ``ContinuousMove`` followed by ``Stop`` on the hub: a
nudge from the cloud then never keeps the camera turning when the tunnel
drops between "move" and "stop".
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from xml.etree.ElementTree import tostring
from xml.sax.saxutils import escape

from .camera_probe import (
    ONVIF_PORTS,
    CameraError,
    OnvifClient,
    _first,
    _first_child_path,
    _int,
    _local,
    _text,
    split_credentials,
)
from .vigi_setup import posix_tz

MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
IMAGING_NS = "http://www.onvif.org/ver20/imaging/wsdl"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
# Taiwan's national time service; cameras elsewhere can still use any server.
DEFAULT_NTP = "time.stdtime.gov.tw"
MAX_PRESETS = 64
IMAGE_FIELDS = (
    # (result key, ONVIF element); ImagingSettings20 puts IrCutFilter between
    # Contrast and Sharpness.
    ("brightness", "Brightness"),
    ("saturation", "ColorSaturation"),
    ("contrast", "Contrast"),
    ("sharpness", "Sharpness"),
)
IR_CUT_MODES = ("AUTO", "ON", "OFF")
DIRECTIONS = {
    "up": (0.0, 1.0, 0.0),
    "down": (0.0, -1.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "zoom_in": (0.0, 0.0, 1.0),
    "zoom_out": (0.0, 0.0, -1.0),
}


def _yaml(text: str) -> dict:
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _url_credentials(value: Any, host: str) -> tuple[str, str] | None:
    for item in value if isinstance(value, list) else [value]:
        text = str(item or "").split("#", 1)[0].strip()
        if "{" in text:
            continue  # Frigate environment variables are not ours to expand.
        with contextlib.suppress(ValueError, CameraError):
            parts = urlsplit(text)
            if parts.hostname == host and parts.username:
                _bare, user, password = split_credentials(text)
                return user, password
    return None


def camera_access(raw: str, camera: str) -> dict:
    """Host, ONVIF port and credentials of a Frigate camera, from its config.

    Order: the camera's ``onvif`` block, its go2rtc restreams (percent-encoded
    URLs), then its own ffmpeg inputs (credentials as typed).
    """

    config = _yaml(raw)
    cameras = config.get("cameras") if isinstance(config.get("cameras"), dict) else {}
    current = cameras.get(camera)
    if not isinstance(current, dict):
        raise CameraError("frigate_camera_not_found", "Frigate 已沒有這台攝影機")
    onvif = current.get("onvif") if isinstance(current.get("onvif"), dict) else {}
    go2rtc = config.get("go2rtc") if isinstance(config.get("go2rtc"), dict) else {}
    streams = go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
    ffmpeg = current.get("ffmpeg") if isinstance(current.get("ffmpeg"), dict) else {}
    inputs = [
        str(i.get("path") or "")
        for i in ffmpeg.get("inputs") or []
        if isinstance(i, dict)
    ]

    host = str(onvif.get("host") or "")
    for path in inputs if not host else []:
        with contextlib.suppress(ValueError):
            parsed = urlsplit(path)
            sources = streams.get(parsed.path.strip("/"), [])
            for item in [path, *(sources if isinstance(sources, list) else [sources])]:
                candidate = urlsplit(str(item or "")).hostname or ""
                if candidate and candidate not in ("127.0.0.1", "localhost"):
                    host = candidate
                    break
        if host:
            break
    if not host:
        raise CameraError("camera_unreachable", "找不到這台攝影機的 IP")

    user, password = str(onvif.get("user") or ""), str(onvif.get("password") or "")
    if not user or "{" in user + password:
        user = password = ""
        restreams = [urlsplit(p).path.strip("/") for p in inputs]
        sources = [
            streams[n] for n in [camera, f"{camera}_sub", *restreams] if n in streams
        ]
        for value in [*sources, *inputs]:
            found = _url_credentials(value, host)
            if found:
                user, password = found
                break
    if not user:
        raise CameraError(
            "camera_credentials_missing",
            "找不到攝影機帳密，請移除後重新加入這台攝影機",
        )
    port = _int(str(onvif.get("port") or ""))
    return {"host": host, "port": port, "username": user, "password": password}


def stream_sources(raw: str, camera: str) -> dict[str, str]:
    """go2rtc restreams of a camera → their camera URL (credentials included).

    ``main`` is the camera's own restream, ``sub`` the ``_sub`` one; the
    fragment (``#backchannel=0`` and the like) is dropped.
    """

    config = _yaml(raw)
    go2rtc = config.get("go2rtc") if isinstance(config.get("go2rtc"), dict) else {}
    streams = go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
    found: dict[str, str] = {}
    for key, name in (("main", camera), ("sub", f"{camera}_sub")):
        value = streams.get(name)
        first = value[0] if isinstance(value, list) and value else value
        if isinstance(first, str) and first.startswith(("rtsp://", "rtsps://")):
            found[key] = first.split("#", 1)[0]
    return found


class CameraControl:
    """One camera's ONVIF services, found once per request."""

    def __init__(self, session: Any, access: dict, *, seconds: float = 6) -> None:
        self.session = session
        self.access = access
        self.seconds = seconds
        self.client: OnvifClient | None = None
        self.urls: dict[str, str] = {}
        self.profile = ""
        self.source = ""

    async def connect(self) -> None:
        host, username, password = (
            self.access["host"],
            self.access["username"],
            self.access["password"],
        )
        ports = [self.access["port"]] if self.access.get("port") else []
        failure: CameraError | None = None
        for port in dict.fromkeys([*ports, *ONVIF_PORTS]):
            client = OnvifClient(
                self.session, host, port, username, password, seconds=self.seconds
            )
            try:
                await client.sync_clock()
                root = await client.call(
                    client.device_url,
                    f"{DEVICE_NS}/GetCapabilities",
                    "<tds:GetCapabilities><tds:Category>All</tds:Category>"
                    "</tds:GetCapabilities>",
                )
            except CameraError as err:
                if err.code == "camera_auth_failed":
                    raise CameraError(
                        "camera_auth_failed",
                        "攝影機帳密已變更，請移除後重新加入這台攝影機",
                    ) from None
                failure = err
                continue
            self.client = client
            for service in ("Media", "PTZ", "Imaging"):
                url = client._local_url(_text(root, service, "XAddr"))
                if url:
                    self.urls[service] = url
            break
        if self.client is None:
            raise failure or CameraError(
                "camera_unreachable", f"無法連線到攝影機 {host}"
            )
        media = self.urls.get("Media", self.client.device_url)
        profiles = await self.client.profiles(media)
        if profiles:
            self.profile, self.source = profiles[0]["token"], profiles[0]["source"]

    async def _call(self, service: str, namespace: str, name: str, body: str) -> Any:
        assert self.client is not None
        url = self.urls.get(service)
        if not url:
            raise CameraError("camera_unsupported", "攝影機不支援此功能")
        prefix = {
            PTZ_NS: "tptz",
            IMAGING_NS: "timg",
            DEVICE_NS: "tds",
            MEDIA_NS: "trt",
        }[namespace]
        return await self.client.call(
            url,
            f"{namespace}/{name}",
            f'<{prefix}:{name} xmlns:{prefix}="{namespace}">{body}</{prefix}:{name}>',
        )

    # PTZ -------------------------------------------------------------------

    async def presets(self) -> list[dict]:
        root = await self._call("PTZ", PTZ_NS, "GetPresets", self._profile_xml())
        found = []
        for element in root.iter():
            if _local(element.tag) != "Preset" or not element.get("token"):
                continue
            token = element.get("token")[:64]
            name = _text(_first_child_path(element, ("Name",)))[:40] or token
            found.append({"token": token, "name": name})
            if len(found) >= MAX_PRESETS:
                break
        return found

    def _profile_xml(self) -> str:
        if not self.profile:
            raise CameraError("camera_unsupported", "攝影機沒有可用的媒體設定檔")
        return f"<tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>"

    async def move(self, direction: str, speed: float, seconds: float) -> None:
        pan, tilt, zoom = DIRECTIONS[direction]
        velocity = (
            f'<tt:Zoom xmlns:tt="{SCHEMA_NS}" x="{zoom * speed:.2f}"/>'
            if zoom
            else f'<tt:PanTilt xmlns:tt="{SCHEMA_NS}" x="{pan * speed:.2f}" '
            f'y="{tilt * speed:.2f}"/>'
        )
        await self._call(
            "PTZ",
            PTZ_NS,
            "ContinuousMove",
            f"{self._profile_xml()}<tptz:Velocity>{velocity}</tptz:Velocity>",
        )
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.stop()

    async def stop(self) -> None:
        await self._call(
            "PTZ",
            PTZ_NS,
            "Stop",
            f"{self._profile_xml()}<tptz:PanTilt>true</tptz:PanTilt>"
            "<tptz:Zoom>true</tptz:Zoom>",
        )

    async def goto(self, token: str) -> None:
        await self._call(
            "PTZ",
            PTZ_NS,
            "GotoPreset",
            f"{self._profile_xml()}"
            f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken>",
        )

    async def save_preset(self, name: str, token: str | None) -> str:
        replace = (
            f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken>" if token else ""
        )
        root = await self._call(
            "PTZ",
            PTZ_NS,
            "SetPreset",
            f"{self._profile_xml()}<tptz:PresetName>{escape(name)}</tptz:PresetName>"
            f"{replace}",
        )
        return _text(root, "PresetToken")[:64] or (token or "")

    async def remove_preset(self, token: str) -> None:
        await self._call(
            "PTZ",
            PTZ_NS,
            "RemovePreset",
            f"{self._profile_xml()}"
            f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken>",
        )

    # Image -----------------------------------------------------------------

    def _source_xml(self) -> str:
        if not self.source:
            raise CameraError("camera_unsupported", "攝影機沒有影像來源設定")
        return f"<timg:VideoSourceToken>{escape(self.source)}</timg:VideoSourceToken>"

    async def image(self) -> dict:
        settings = await self._call(
            "Imaging", IMAGING_NS, "GetImagingSettings", self._source_xml()
        )
        options = None
        with contextlib.suppress(CameraError):
            options = await self._call(
                "Imaging", IMAGING_NS, "GetOptions", self._source_xml()
            )
        result: dict[str, Any] = {}
        for key, element in IMAGE_FIELDS:
            value = _number(_text(settings, "ImagingSettings", element))
            if value is None:
                continue
            low = (
                _number(_text(options, element, "Min")) if options is not None else None
            )
            high = (
                _number(_text(options, element, "Max")) if options is not None else None
            )
            result[key] = {
                "value": value,
                "min": 0.0 if low is None else low,
                "max": 100.0 if high is None else high,
            }
        mode = _text(settings, "ImagingSettings", "IrCutFilter").upper()
        if mode in IR_CUT_MODES:
            modes = (
                [
                    (e.text or "").strip().upper()
                    for e in options.iter()
                    if _local(e.tag) == "IrCutFilterModes"
                ]
                if options is not None
                else []
            )
            result["irCut"] = {
                "value": mode,
                "options": [m for m in IR_CUT_MODES if m in modes]
                or list(IR_CUT_MODES),
            }
        return result

    async def set_image(self, changes: dict) -> None:
        parts = []
        for key, element in IMAGE_FIELDS[:3]:
            if key in changes:
                parts.append(f"<tt:{element}>{changes[key]:g}</tt:{element}>")
        if "irCut" in changes:
            parts.append(f"<tt:IrCutFilter>{changes['irCut']}</tt:IrCutFilter>")
        if "sharpness" in changes:
            parts.append(f"<tt:Sharpness>{changes['sharpness']:g}</tt:Sharpness>")
        await self._call(
            "Imaging",
            IMAGING_NS,
            "SetImagingSettings",
            f'{self._source_xml()}<timg:ImagingSettings xmlns:tt="{SCHEMA_NS}">'
            f"{''.join(parts)}</timg:ImagingSettings>"
            "<timg:ForcePersistence>true</timg:ForcePersistence>",
        )

    # Quality (Media v1 video encoders) ----------------------------------------

    async def _encoders(self) -> list[tuple[str, Any]]:
        """(profile token, VideoEncoderConfiguration element) of the first two profiles.

        The camera lists its main stream first, then the sub stream.
        """

        root = await self._call("Media", MEDIA_NS, "GetProfiles", "")
        found = []
        for element in root.iter():
            if _local(element.tag) != "Profiles" or not element.get("token"):
                continue
            encoder = _first_child_path(element, ("VideoEncoderConfiguration",))
            if encoder is not None and encoder.get("token"):
                found.append((element.get("token"), encoder))
            if len(found) == 2:
                break
        return found

    async def quality(self) -> list[dict]:
        streams = []
        for index, (profile, encoder) in enumerate(await self._encoders()):
            codec = _text(encoder, "Encoding").upper()
            stream = {
                "profile": profile[:64],
                "name": ("主串流", "子串流")[index],
                "codec": {"H264": "H.264", "H265": "H.265"}.get(codec, codec)[:16],
                "width": _int(_text(encoder, "Resolution", "Width")),
                "height": _int(_text(encoder, "Resolution", "Height")),
                "fps": _int(_text(encoder, "RateControl", "FrameRateLimit")),
                "bitrate": _int(_text(encoder, "RateControl", "BitrateLimit")),
                "resolutions": [],
                "fpsRange": None,
                "bitrateRange": None,
            }
            with contextlib.suppress(CameraError):
                options = await self._call(
                    "Media",
                    MEDIA_NS,
                    "GetVideoEncoderConfigurationOptions",
                    f"<trt:ConfigurationToken>{escape(encoder.get('token'))}"
                    f"</trt:ConfigurationToken><trt:ProfileToken>{escape(profile)}"
                    "</trt:ProfileToken>",
                )
                # Media v1 only describes H.264 (and JPEG/MPEG4); an H.265
                # stream keeps its own size and the H.264 ranges.
                h264 = _first(options, "Options", "H264")
                if h264 is not None:
                    sizes = [
                        (_int(_text(r, "Width")), _int(_text(r, "Height")))
                        for r in h264
                        if _local(r.tag) == "ResolutionsAvailable"
                    ]
                    stream["resolutions"] = [
                        [w, h] for w, h in dict.fromkeys(sizes) if w and h
                    ][:16]
                    low = _int(_text(h264, "FrameRateRange", "Min"))
                    high = _int(_text(h264, "FrameRateRange", "Max"))
                    if low and high:
                        stream["fpsRange"] = [low, high]
                bitrate = _first(options, "Extension", "H264", "BitrateRange")
                low, high = (
                    _int(_text(bitrate, "Min")),
                    _int(_text(bitrate, "Max")),
                )
                if low is not None and high:
                    stream["bitrateRange"] = [low, high]
            current = [stream["width"], stream["height"]]
            if stream["width"] and current not in stream["resolutions"]:
                stream["resolutions"].insert(0, current)
            streams.append(stream)
        return streams

    async def set_quality(self, profile: str, changes: dict) -> None:
        """Rewrite one encoder; ONVIF wants the whole configuration back."""

        encoder = next((e for p, e in await self._encoders() if p == profile), None)
        if encoder is None:
            raise CameraError("camera_unsupported", "找不到這個串流")
        for path, key in (
            (("Resolution", "Width"), "width"),
            (("Resolution", "Height"), "height"),
            (("RateControl", "FrameRateLimit"), "fps"),
            (("RateControl", "BitrateLimit"), "bitrate"),
        ):
            if key in changes:
                field = _first_child_path(encoder, path)
                if field is None:
                    raise CameraError("camera_unsupported", "攝影機不支援調整此項目")
                field.text = str(int(changes[key]))
        children = "".join(tostring(child, encoding="unicode") for child in encoder)
        await self._call(
            "Media",
            MEDIA_NS,
            "SetVideoEncoderConfiguration",
            f'<trt:Configuration token="{escape(encoder.get("token"))}">{children}'
            "</trt:Configuration><trt:ForcePersistence>true</trt:ForcePersistence>",
        )

    # Clock -----------------------------------------------------------------

    async def clock(self) -> dict:
        assert self.client is not None
        root = await self.client.call(
            self.client.device_url,
            f"{DEVICE_NS}/GetSystemDateAndTime",
            "<tds:GetSystemDateAndTime/>",
        )
        utc = _first(root, "UTCDateTime")
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
        camera_time = None
        drift = None
        if all(value is not None for value in values):
            with contextlib.suppress(ValueError, TypeError):
                moment = datetime(*values, tzinfo=UTC)  # type: ignore[arg-type,misc]
                camera_time = moment.isoformat()
                drift = int((moment - datetime.now(UTC)).total_seconds())
        server = None
        with contextlib.suppress(CameraError):
            ntp = await self.client.call(
                self.client.device_url, f"{DEVICE_NS}/GetNTP", "<tds:GetNTP/>"
            )
            server = (
                _text(ntp, "NTPManual", "DNSname")
                or _text(ntp, "NTPManual", "IPv4Address")
                or _text(ntp, "NTPFromDHCP", "DNSname")
                or _text(ntp, "NTPFromDHCP", "IPv4Address")
                or None
            )
        return {
            "ntp": _text(root, "DateTimeType").upper() == "NTP",
            "server": server,
            "timeZone": _text(root, "TimeZone", "TZ")[:40] or None,
            "cameraTime": camera_time,
            "driftSeconds": drift,
        }

    async def set_clock(self, ntp: bool, server: str, time_zone: str | None) -> None:
        assert self.client is not None
        now = datetime.now(UTC)
        zone = f"<tds:TimeZone><tt:TZ>{posix_tz(time_zone, now)}</tt:TZ></tds:TimeZone>"
        if ntp:
            await self.client.call(
                self.client.device_url,
                f"{DEVICE_NS}/SetNTP",
                "<tds:SetNTP><tds:FromDHCP>false</tds:FromDHCP><tds:NTPManual>"
                f"<tt:Type>DNS</tt:Type><tt:DNSname>{escape(server)}</tt:DNSname>"
                "</tds:NTPManual></tds:SetNTP>",
            )
            body = (
                "<tds:SetSystemDateAndTime><tds:DateTimeType>NTP</tds:DateTimeType>"
                f"<tds:DaylightSavings>false</tds:DaylightSavings>{zone}"
                "</tds:SetSystemDateAndTime>"
            )
        else:
            # Manual: the hub's own clock, as on activation.
            body = (
                "<tds:SetSystemDateAndTime><tds:DateTimeType>Manual</tds:DateTimeType>"
                f"<tds:DaylightSavings>false</tds:DaylightSavings>{zone}"
                "<tds:UTCDateTime>"
                f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
                f"<tt:Day>{now.day}</tt:Day></tt:Date>"
                f"<tt:Time><tt:Hour>{now.hour}</tt:Hour>"
                f"<tt:Minute>{now.minute}</tt:Minute>"
                f"<tt:Second>{now.second}</tt:Second></tt:Time>"
                "</tds:UTCDateTime></tds:SetSystemDateAndTime>"
            )
        await self.client.call(
            self.client.device_url, f"{DEVICE_NS}/SetSystemDateAndTime", body
        )

    # Everything at once ----------------------------------------------------

    async def info(self) -> dict:
        """Supported features and current values; a failing section is null."""

        assert self.client is not None
        result: dict[str, Any] = {
            "host": self.access["host"],
            "port": self.client.port,
            "quality": None,
            "ptz": None,
            "image": None,
            "clock": None,
        }
        if "Media" in self.urls:
            with contextlib.suppress(CameraError):
                result["quality"] = await self.quality() or None
        if "PTZ" in self.urls and self.profile:
            with contextlib.suppress(CameraError):
                result["ptz"] = {"presets": await self.presets()}
        if "Imaging" in self.urls and self.source:
            with contextlib.suppress(CameraError):
                result["image"] = await self.image() or None
        with contextlib.suppress(CameraError):
            result["clock"] = await self.clock()
        return result


def _number(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2)
