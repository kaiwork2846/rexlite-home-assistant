"""Add IP cameras to the Frigate NVR that runs next to Home Assistant.

REXLiTE hubs run Frigate in Docker beside HA (host network), so its internal
API answers on 127.0.0.1. A camera gets a go2rtc restream for its main and
sub stream (like Frigate's own camera wizard), is written with
``PUT /api/config/set`` and Frigate is restarted. A job only succeeds once
Frigate reports real frames (``camera_fps`` > 0); otherwise the camera is
taken out again so a half-added camera never lingers.

Frigate 0.16–0.17 cannot add or remove cameras live reliably:

* ``CameraConfigUpdateSubscriber`` narrows its topic to the only camera when
  Frigate started with exactly one (a fresh hub's sample camera, or a hub's
  first real one), so the object processor and the recording maintainer never
  see a live ``add``; the processor thread then dies on a ``KeyError`` and no
  recording is saved for any camera.
* The live ``remove`` stops whichever camera was loaded last
  (``frigate/camera/maintainer.py``).

So both are applied with a restart. Before a removal the camera's recording
and capture are switched off live, so Frigate drops its cached segments: a
cached segment of a camera that is no longer configured makes the recording
maintainer fail every cycle (``frigate/record/maintainer.py``), for every
other camera too.

A camera's detect stream is decoded in software (``hwaccel_args: []``).
Frigate's ``auto`` picks VAAPI on Intel hubs, and on 2026-10-23 VAAPI failed
on a VIGI C540V sub stream every 30–40 s (``Failed to sync surface``); after
about 300 ffmpeg restarts Frigate ran out of file descriptors, its watchdog
thread died and the camera stayed without frames until Frigate restarted. A
small detect stream at 5 fps costs little CPU. ``[]`` and not ``""``: an
empty string makes ``/api/config/set`` delete the key, back to ``auto``.

Camera settings (recording, detection, retention, tracked objects) change
live through Frigate's update topics, except switching recording on: Frigate
only gives an ffmpeg process its recording output when recording was enabled
at start (``frigate/config/camera/camera.py``), so that one restarts Frigate.

Camera credentials are written into the Frigate configuration on this hub
(that is where Frigate needs them) and nowhere else: not into HA storage, job
records, results or error messages.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .camera_control import (
    DEFAULT_NTP,
    DIRECTIONS,
    IR_CUT_MODES,
    CameraControl,
    camera_access,
    stream_sources,
)
from .camera_probe import (
    TPLINK,
    CameraError,
    camera_host,
    camera_port,
    discover_onvif,
    fallback_streams,
    onvif_streams,
    rtsp_check,
    split_credentials,
    stream_uri,
    vendor_of,
    with_credentials,
    with_raw_credentials,
)
from .vigi_setup import vigi_activate, vigi_state

DATA_KEY = "rexlite_frigate"
STORE_KEY = "rexlite.frigate"
BRIDGE_API_VERSION = 1
FRIGATE_CANDIDATES = ("http://127.0.0.1:5050", "http://127.0.0.1:5000")
GO2RTC_URL = "http://127.0.0.1:1984"
RESTREAM_URL = "rtsp://127.0.0.1:8554/{name}"
# Frigate writes this sample camera when it starts without a configuration.
PLACEHOLDER_CAMERA = "name_of_your_camera"
PLACEHOLDER_PATH = "rtsp://10.0.10.10:554/rtsp"
SUPPORTED_CODECS = ("H264", "H265")
MAX_SNAPSHOT_BYTES = 600_000
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CAMERA = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Labels of Frigate's default COCO model an installer can choose to track.
TRACKABLE_OBJECTS = ("person", "car", "motorcycle", "bicycle", "dog", "cat")
MAX_RETAIN_DAYS = 365


class FrigateError(Exception):
    """User-facing failure; ``code`` is stable for the operation platform."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timeout(seconds: float) -> Any:
    try:
        import aiohttp
    except ImportError:  # unit tests use a duck-typed session
        return seconds
    factory = getattr(aiohttp, "ClientTimeout", None)
    return factory(total=seconds) if factory else seconds


def _url(base: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """A URL whose query is already percent-encoded (camera URLs carry ``%``)."""

    url = base + path
    if params:
        url += "?" + urlencode(params, doseq=True, quote_via=quote, safe="")
    try:
        from yarl import URL
    except ImportError:  # unit tests compare plain strings
        return url
    return URL(url, encoded=True)


async def _send(
    session: Any,
    method: str,
    url: Any,
    *,
    json_body: Any = None,
    seconds: float = 10,
    binary: bool = False,
    service: str = "Frigate",
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    kwargs: dict[str, Any] = {"timeout": _timeout(seconds)}
    if headers:
        kwargs["headers"] = headers
    if json_body is not None:
        kwargs["json"] = json_body
    try:
        async with session.request(method, url, **kwargs) as response:
            status = response.status
            if binary:
                return status, await response.read()
            text = await response.text()
    except Exception as err:  # noqa: BLE001 - network errors become one code
        raise FrigateError(
            "frigate_unreachable", f"無法連線到 {service}，請確認服務已啟動"
        ) from err
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = text
    return status, payload


class FrigateApi:
    """Frigate's internal (unauthenticated) API on this host."""

    def __init__(self, session: Any, base_url: str) -> None:
        self.session = session
        self.base_url = base_url

    async def get(
        self, path: str, *, seconds: float = 10, headers: dict | None = None
    ) -> Any:
        status, payload = await _send(
            self.session,
            "GET",
            _url(self.base_url, "/api" + path),
            seconds=seconds,
            headers=headers,
        )
        if status != 200:
            raise FrigateError("frigate_failed", f"Frigate 回應錯誤（HTTP {status}）")
        return payload

    async def version(self) -> str:
        return str(await self.get("/version", seconds=4) or "").strip()[:40]

    async def config(self, *, fresh: bool = False) -> dict:
        # Frigate's nginx caches /api/config for 5 s; a read right after a
        # write must skip that cache.
        headers = {"X-Cache-Bypass": "1"} if fresh else None
        payload = await self.get("/config", seconds=15, headers=headers)
        if not isinstance(payload, dict):
            raise FrigateError("frigate_failed", "Frigate 設定格式不正確")
        return payload

    async def raw_config(self) -> str:
        """Frigate's configuration file as written, credentials included.

        Only this hub reads it, for a camera's ONVIF access; it never leaves.
        """

        payload = await self.get("/config/raw", seconds=15)
        if not isinstance(payload, str) or not payload.strip():
            raise FrigateError("frigate_failed", "無法讀取 Frigate 設定")
        return payload

    async def stats(self) -> dict:
        payload = await self.get("/stats")
        return payload if isinstance(payload, dict) else {}

    async def config_set(
        self, config_data: dict, update_topic: str | None = None
    ) -> None:
        """Write config; without an update topic it applies at the next restart."""

        body: dict[str, Any] = {
            "requires_restart": 0 if update_topic else 1,
            "config_data": config_data,
        }
        if update_topic:
            body["update_topic"] = update_topic
        status, payload = await _send(
            self.session,
            "PUT",
            _url(self.base_url, "/api/config/set"),
            json_body=body,
            seconds=60,
        )
        if status == 200 and not (
            isinstance(payload, dict) and payload.get("success") is False
        ):
            return
        if status in (400, 422):
            raise FrigateError(
                "frigate_config_rejected", "Frigate 未接受這組設定，原設定已保留"
            )
        if status in (404, 405):
            raise FrigateError(
                "frigate_unsupported",
                "此 Frigate 版本不支援遠端設定，請更新至 0.14 以上",
            )
        raise FrigateError("frigate_failed", f"Frigate 設定失敗（HTTP {status}）")

    async def restart(self) -> None:
        status, _payload = await _send(
            self.session, "POST", _url(self.base_url, "/api/restart"), seconds=15
        )
        if status != 200:
            raise FrigateError(
                "frigate_failed", f"Frigate 無法重新啟動（HTTP {status}）"
            )

    async def latest_jpeg(self, camera: str, height: int) -> bytes:
        status, data = await _send(
            self.session,
            "GET",
            _url(self.base_url, f"/api/{camera}/latest.jpg", {"h": height}),
            seconds=15,
            binary=True,
            headers={"X-Cache-Bypass": "1"},
        )
        if status != 200 or not isinstance(data, bytes | bytearray) or not data:
            raise FrigateError("frigate_snapshot_failed", "目前無法取得攝影機畫面")
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise FrigateError("frigate_snapshot_failed", "攝影機畫面過大")
        return bytes(data)

    async def ffprobe(self, url: str) -> dict:
        """Width, height and codec of a stream as Frigate's ffprobe sees it."""

        status, payload = await _send(
            self.session,
            "GET",
            _url(self.base_url, "/api/ffprobe", {"paths": url}),
            seconds=15,
        )
        if status != 200 or not isinstance(payload, list) or not payload:
            return {}
        first = payload[0] if isinstance(payload[0], dict) else {}
        stdout = first.get("stdout")
        streams = stdout.get("streams") if isinstance(stdout, dict) else None
        for stream in streams or []:
            if not isinstance(stream, dict) or not stream.get("width"):
                continue
            name = str(stream.get("codec_name") or stream.get("codec_long_name") or "")
            return {
                "width": _int(stream.get("width")),
                "height": _int(stream.get("height")),
                "codec": _codec_name(name),
            }
        return {}


class Go2rtcApi:
    """go2rtc inside the Frigate container, for restreams without a restart."""

    def __init__(self, session: Any, base_url: str = GO2RTC_URL) -> None:
        self.session = session
        self.base_url = base_url

    async def streams(self) -> dict:
        status, payload = await _send(
            self.session,
            "GET",
            _url(self.base_url, "/api/streams"),
            seconds=5,
            service="go2rtc",
        )
        if status != 200 or not isinstance(payload, dict):
            raise FrigateError("frigate_unreachable", "無法連線到 go2rtc")
        return payload

    async def add(self, name: str, source: str) -> None:
        # go2rtc creates the stream before it persists its own config file, so
        # a failed write (400) still leaves a live stream; check the list.
        await _send(
            self.session,
            "PUT",
            _url(self.base_url, "/api/streams", {"name": name, "src": source}),
            seconds=10,
            service="go2rtc",
        )
        if name not in await self.streams():
            raise FrigateError("frigate_restream_failed", "go2rtc 無法建立串流")

    async def webrtc(self, name: str, offer: str) -> str:
        """go2rtc's SDP answer for a browser offer (non-trickle, candidates inline)."""

        status, payload = await _send(
            self.session,
            "POST",
            _url(self.base_url, "/api/webrtc", {"src": name}),
            json_body={"type": "offer", "sdp": offer},
            seconds=20,
            service="go2rtc",
        )
        answer = payload.get("sdp") if isinstance(payload, dict) else None
        if status != 200 or not isinstance(answer, str) or not answer.startswith("v=0"):
            raise FrigateError("frigate_live_failed", "攝影機即時畫面連線失敗")
        return answer

    async def remove(self, name: str) -> None:
        with contextlib.suppress(FrigateError):
            await _send(
                self.session,
                "DELETE",
                _url(self.base_url, "/api/streams", {"src": name}),
                seconds=10,
                service="go2rtc",
            )


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _codec_name(value: str) -> str:
    text = value.upper()
    if "265" in text or "HEVC" in text:
        return "H265"
    if "264" in text or "AVC" in text:
        return "H264"
    if "JPEG" in text:
        return "MJPEG"
    return text[:16]


def _host_of(value: Any) -> str:
    """The camera host behind a stream source, ignoring local restreams."""

    for item in value if isinstance(value, list) else [value]:
        with contextlib.suppress(ValueError):
            host = urlsplit(str(item or "")).hostname or ""
            if host and host not in ("127.0.0.1", "localhost"):
                return host
    return ""


def camera_hosts(name: str, camera: dict, streams: dict) -> set[str]:
    """Every camera address a Frigate camera reads from."""

    hosts: set[str] = set()
    onvif = camera.get("onvif") if isinstance(camera.get("onvif"), dict) else {}
    if onvif.get("host"):
        hosts.add(str(onvif["host"]))
    ffmpeg = camera.get("ffmpeg") if isinstance(camera.get("ffmpeg"), dict) else {}
    for item in ffmpeg.get("inputs") or []:
        path = str(item.get("path") or "") if isinstance(item, dict) else ""
        direct = _host_of(path)
        if direct:
            hosts.add(direct)
            continue
        with contextlib.suppress(ValueError):
            restream = urlsplit(path).path.strip("/")
            if restream in streams:
                hosts.add(_host_of(streams[restream]))
    live = camera.get("live") if isinstance(camera.get("live"), dict) else {}
    for restream in [name, *(live.get("streams") or {}).values()]:
        if restream in streams:
            hosts.add(_host_of(streams[restream]))
    hosts.discard("")
    return hosts


def is_placeholder(name: str, camera: dict) -> bool:
    inputs = (camera.get("ffmpeg") or {}).get("inputs") or []
    paths = {str(i.get("path")) for i in inputs if isinstance(i, dict)}
    return name == PLACEHOLDER_CAMERA and paths == {PLACEHOLDER_PATH}


def camera_key(label: str, host: str, taken: Iterable[str]) -> str:
    """A Frigate camera name (``[A-Za-z0-9_-]``) for a display label."""

    used = set(taken)
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]
    base = slug if len(slug) >= 3 else f"camera_{host.rsplit('.', 1)[-1]}"
    key, index = base, 2
    while key in used or f"{key}_sub" in used:
        key = f"{base}_{index}"
        index += 1
    return key


def camera_config(
    *,
    label: str,
    key: str,
    main: dict,
    sub: dict | None,
    restream: bool,
    record: bool,
    detect: bool,
    onvif: dict | None,
) -> dict:
    """The ``cameras.<key>`` block, shaped like Frigate's own camera wizard."""

    detect_stream = sub or main
    if restream:
        inputs = [
            {
                "path": RESTREAM_URL.format(name=key),
                "input_args": "preset-rtsp-restream",
                "roles": ["record"] if sub else ["record", "detect"],
            }
        ]
        if sub:
            inputs.append(
                {
                    "path": RESTREAM_URL.format(name=f"{key}_sub"),
                    "input_args": "preset-rtsp-restream",
                    "roles": ["detect"],
                }
            )
    else:
        inputs = [
            {
                "path": main["direct"],
                "roles": ["record"] if sub else ["record", "detect"],
            }
        ]
        if sub:
            inputs.append({"path": sub["direct"], "roles": ["detect"]})
    config: dict[str, Any] = {
        "enabled": True,
        "friendly_name": label,
        # Software decode for detect: see the module docstring.
        "ffmpeg": {"inputs": inputs, "hwaccel_args": []},
        "detect": {"enabled": detect, "fps": 5},
        "record": {"enabled": record},
    }
    if detect_stream.get("width") and detect_stream.get("height"):
        config["detect"]["width"] = detect_stream["width"]
        config["detect"]["height"] = detect_stream["height"]
    if restream:
        streams = {_stream_label(main, "主串流"): key}
        if sub:
            streams[_stream_label(sub, "子串流")] = f"{key}_sub"
        config["live"] = {"streams": streams}
    if onvif:
        config["onvif"] = onvif
    return config


def _stream_label(stream: dict, fallback: str) -> str:
    height = stream.get("height")
    return f"{height}P" if height else fallback


def _public_job(job: dict) -> dict:
    return {
        key: job.get(key)
        for key in (
            "jobId",
            "kind",
            "state",
            "stage",
            "message",
            "camera",
            "label",
            "host",
            "startedAt",
            "finishedAt",
            "result",
            "error",
        )
    }


class FrigateBridge:
    """``rexlite/frigate/*`` logic; HA is injected so it stays testable."""

    def __init__(
        self,
        *,
        store: Any,
        session: Any,
        create_task: Callable,
        interfaces: Callable[[], Any] | None = None,
        integration_version: str = "",
        time_zone: Callable[[], str | None] | None = None,
        candidates: Iterable[str] = FRIGATE_CANDIDATES,
        go2rtc_url: str = GO2RTC_URL,
        sleep: Callable[[float], Any] = asyncio.sleep,
        verify_seconds: float = 90,
        restart_seconds: float = 180,
        drain_seconds: float = 15,
    ) -> None:
        self.store = store
        self.session = session
        self.create_task = create_task
        self.interfaces = interfaces
        self.integration_version = integration_version
        self.time_zone = time_zone or (lambda: None)
        self.candidates = tuple(candidates)
        self.go2rtc = Go2rtcApi(session, go2rtc_url)
        self.sleep = sleep
        self.verify_seconds = verify_seconds
        self.restart_seconds = restart_seconds
        self.drain_seconds = drain_seconds
        self.data: dict[str, Any] = {}
        self.loaded = False
        self.base_url: str | None = None
        self.version = ""
        self.jobs: dict[str, dict] = {}
        self.active: asyncio.Task | None = None
        # One ONVIF operation per camera at a time (a move ends with a stop).
        self.camera_locks: dict[str, asyncio.Lock] = {}

    # Storage ---------------------------------------------------------------

    async def load(self) -> None:
        if self.loaded:
            return
        stored = await self.store.async_load() or {}
        self.data = {
            "managed": dict(stored.get("managed") or {}),
            "jobs": list(stored.get("jobs") or [])[-10:],
        }
        for job in self.data["jobs"]:
            if job.get("state") == "running":
                # HA restarted mid-job; Frigate may hold a partial camera.
                job.update(
                    state="interrupted",
                    finishedAt=job.get("finishedAt") or _now(),
                    error={
                        "code": "frigate_interrupted",
                        "message": "中樞主機重新啟動，作業已中斷，請重新檢查攝影機",
                    },
                )
        self.loaded = True

    async def _save(self) -> None:
        await self.store.async_save(self.data)

    # Frigate access --------------------------------------------------------

    async def api(self) -> FrigateApi:
        """The first Frigate URL that answers; remembered until it fails."""

        order = [self.base_url] if self.base_url else []
        order += [c for c in self.candidates if c not in order]
        for base_url in order:
            api = FrigateApi(self.session, base_url)
            with contextlib.suppress(FrigateError):
                version = await api.version()
                if version:
                    self.base_url, self.version = base_url, version
                    return api
        self.base_url = None
        raise FrigateError(
            "frigate_unreachable",
            "找不到主機上的 Frigate（:5050），請確認 Frigate 已啟動",
        )

    async def status(self) -> dict:
        await self.load()
        job = self.data["jobs"][-1] if self.data["jobs"] else None
        result: dict[str, Any] = {
            "bridgeVersion": BRIDGE_API_VERSION,
            "integrationVersion": self.integration_version,
            "frigate": {"reachable": False},
            "cameras": [],
            "job": _public_job(job) if job else None,
        }
        try:
            api = await self.api()
            config = await api.config()
            stats = await api.stats()
        except FrigateError as err:
            result["frigate"] = {
                "reachable": False,
                "error": {"code": err.code, "message": err.message},
            }
            return result
        restream = True
        try:
            await self.go2rtc.streams()
        except FrigateError:
            restream = False
        service = stats.get("service") if isinstance(stats.get("service"), dict) else {}
        result["frigate"] = {
            "reachable": True,
            "version": self.version,
            "uptime": _int(service.get("uptime")),
            "restream": restream,
        }
        result["cameras"] = self._cameras(config, stats)
        return result

    def _cameras(self, config: dict, stats: dict) -> list[dict]:
        cameras = (
            config.get("cameras") if isinstance(config.get("cameras"), dict) else {}
        )
        go2rtc = config.get("go2rtc") if isinstance(config.get("go2rtc"), dict) else {}
        streams = (
            go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
        )
        live = stats.get("cameras") if isinstance(stats.get("cameras"), dict) else {}
        managed = self.data.get("managed", {})
        listed = []
        for name, camera in cameras.items():
            if not isinstance(camera, dict):
                continue
            current = live.get(name) if isinstance(live.get(name), dict) else {}
            fps = float(current.get("camera_fps") or 0)
            hosts = sorted(camera_hosts(name, camera, streams))
            listed.append(
                {
                    **camera_settings(camera),
                    "camera": name,
                    "label": str(camera.get("friendly_name") or name)[:120],
                    "host": hosts[0] if hosts else "",
                    "enabled": camera.get("enabled") is not False,
                    "ptz": bool((camera.get("onvif") or {}).get("host")),
                    "fps": round(fps, 1),
                    "detectFps": round(float(current.get("detection_fps") or 0), 1),
                    "online": fps > 0,
                    "managed": name in managed,
                    "placeholder": is_placeholder(name, camera),
                }
            )
        return sorted(listed, key=lambda c: (c["placeholder"], c["label"].lower()))

    # Discovery and probing ---------------------------------------------------

    async def discover(self, msg: dict) -> dict:
        await self.load()
        started = time.monotonic()
        interfaces = await self._interfaces()
        try:
            cameras = await discover_onvif(
                interfaces, seconds=float(msg.get("seconds", 3))
            )
        except CameraError as err:
            raise FrigateError(err.code, err.message) from None
        configured = await self._configured_hosts()
        for camera in cameras:
            camera["configured"] = configured.get(camera["host"])
        # A factory-fresh VIGI answers discovery but no login until activated.
        fresh = [
            c for c in cameras if c.get("vendor") == TPLINK and not c["configured"]
        ]
        states = await asyncio.gather(
            *(vigi_state(self.session, c["host"], 2) for c in fresh)
        )
        for camera in cameras:
            camera["uninitialized"] = False
        for camera, state in zip(fresh, states, strict=True):
            camera["uninitialized"] = state == "uninitialized"
        return {
            "cameras": cameras,
            "durationMs": int((time.monotonic() - started) * 1000),
            "scannedAt": _now(),
        }

    async def _interfaces(self) -> list[str | None]:
        if self.interfaces is None:
            return [None]
        with contextlib.suppress(Exception):
            found = self.interfaces()
            if asyncio.iscoroutine(found):
                found = await found
            addresses = [str(a) for a in found or [] if a]
            if addresses:
                return addresses
        return [None]

    async def _configured_hosts(self) -> dict[str, dict]:
        """Camera host → the Frigate camera already reading from it."""

        try:
            config = await (await self.api()).config()
        except FrigateError:
            return {}
        cameras = (
            config.get("cameras") if isinstance(config.get("cameras"), dict) else {}
        )
        go2rtc = config.get("go2rtc") if isinstance(config.get("go2rtc"), dict) else {}
        streams = (
            go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
        )
        hosts: dict[str, dict] = {}
        for name, camera in cameras.items():
            if isinstance(camera, dict):
                for host in camera_hosts(name, camera, streams):
                    hosts.setdefault(
                        host,
                        {
                            "camera": name,
                            "label": str(camera.get("friendly_name") or name),
                        },
                    )
        return hosts

    async def probe(self, msg: dict) -> dict:
        """Find the camera's streams and prove they answer to these credentials."""

        await self.load()
        try:
            host = camera_host(msg.get("host"))
            port = camera_port(msg.get("port"), 80)
            username, password = (
                str(msg.get("username") or ""),
                str(msg.get("password") or ""),
            )
            manual = str(msg.get("rtspUrl") or "").strip()
            if manual:
                bare, url_user, url_password = split_credentials(manual)
                if url_user and not username:
                    username, password = url_user, url_password
                manual = stream_uri(bare, host)
        except CameraError as err:
            raise FrigateError(err.code, err.message) from None
        vendor = vendor_of(msg.get("hint"))
        device: dict[str, str] = {}
        onvif: dict[str, Any] = {"ok": False}
        candidates: list[dict] = []
        try:
            if manual:
                candidates = [{"uri": manual, "name": "手動輸入"}]
            else:
                try:
                    found = await onvif_streams(
                        self.session, host, port, username, password
                    )
                except CameraError as err:
                    if err.code == "camera_auth_failed":
                        await self._raise_if_uninitialized(host)
                        raise
                    onvif["error"] = {"code": err.code, "message": err.message}
                else:
                    device = found["device"]
                    vendor = (
                        vendor_of(device.get("manufacturer"), device.get("model"))
                        or vendor
                    )
                    onvif = {"ok": True, "port": found["port"], "ptz": found["ptz"]}
                    for profile in found["profiles"]:
                        with contextlib.suppress(CameraError):
                            candidates.append(
                                {**profile, "uri": stream_uri(profile["uri"], host)}
                            )
            streams = await self._check(candidates, username, password)
            if not any(s["ok"] for s in streams) and not manual:
                streams = (
                    await self._fallback(host, vendor, username, password) or streams
                )
        except CameraError as err:
            raise FrigateError(err.code, err.message) from None
        await self._fill_dimensions(streams, username, password)
        main, sub = pick_streams(streams)
        configured = await self._configured_hosts()
        return {
            "host": host,
            "vendor": vendor,
            "device": device,
            "onvif": onvif,
            "streams": streams,
            "main": main["uri"] if main else None,
            "sub": sub["uri"] if sub else None,
            "existing": configured.get(host),
            "suggestedName": (device.get("model") or msg.get("hint") or "")[:60],
            "hints": _hints(vendor, onvif, streams),
        }

    async def _raise_if_uninitialized(self, host: str) -> None:
        if await vigi_state(self.session, host, 3) == "uninitialized":
            raise CameraError("camera_uninitialized", "攝影機尚未開通，可一鍵開通")

    async def activate(self, msg: dict) -> dict:
        """Activate a factory-fresh VIGI camera with the installer's password."""

        try:
            return await vigi_activate(
                self.session,
                msg.get("host"),
                str(msg.get("password") or ""),
                onvif_port=camera_port(msg.get("port"), 80),
                time_zone=self.time_zone(),
            )
        except CameraError as err:
            raise FrigateError(err.code, err.message) from None

    async def _check(
        self, candidates: list[dict], username: str, password: str
    ) -> list[dict]:
        streams: list[dict] = []
        seen: set[str] = set()
        for candidate in candidates:
            uri = candidate["uri"]
            if uri in seen:
                continue
            seen.add(uri)
            stream = {
                "uri": uri,
                "name": str(candidate.get("name") or "")[:64],
                "codec": candidate.get("codec") or "",
                "width": candidate.get("width"),
                "height": candidate.get("height"),
                "fps": candidate.get("fps"),
                "ok": False,
                "error": None,
            }
            try:
                checked = await rtsp_check(uri, username, password)
            except CameraError as err:
                if err.code == "camera_auth_failed":
                    raise
                stream["error"] = {"code": err.code, "message": err.message}
            else:
                stream["ok"] = True
                stream["codec"] = stream["codec"] or (checked["codecs"] or [""])[0]
            streams.append(stream)
        return streams

    async def _fallback(
        self, host: str, vendor: str | None, username: str, password: str
    ) -> list[dict]:
        """Well-known RTSP paths, vendor first; stops at the first working pair."""

        paths = fallback_streams(host, vendor)
        for index in range(0, len(paths), 2):
            pair = paths[index : index + 2]
            try:
                await rtsp_check(pair[0], username, password)
            except CameraError as err:
                if err.code in ("camera_auth_failed", "camera_unreachable"):
                    raise
                continue
            names = ("主串流", "子串流")
            return await self._check(
                [{"uri": uri, "name": names[i]} for i, uri in enumerate(pair)],
                username,
                password,
            )
        return []

    async def _fill_dimensions(
        self, streams: list[dict], username: str, password: str
    ) -> None:
        """Ask Frigate's ffprobe for the size of streams ONVIF did not describe."""

        missing = [
            s for s in streams if s["ok"] and not (s.get("width") and s.get("height"))
        ]
        if not missing:
            return
        try:
            api = await self.api()
        except FrigateError:
            return
        for stream in missing:
            with contextlib.suppress(FrigateError):
                info = await api.ffprobe(
                    with_raw_credentials(stream["uri"], username, password)
                )
                stream["width"] = stream.get("width") or info.get("width")
                stream["height"] = stream.get("height") or info.get("height")
                stream["codec"] = stream.get("codec") or info.get("codec") or ""

    # Jobs -----------------------------------------------------------------------

    def job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id) or next(
            (j for j in self.data.get("jobs", []) if j.get("jobId") == job_id), None
        )
        if job is None:
            raise FrigateError(
                "frigate_job_not_found", "找不到這個攝影機作業，請重新整理"
            )
        return _public_job(job)

    async def start_add(self, msg: dict) -> dict:
        await self.load()
        request = self._add_request(msg)
        return self._start(
            "add",
            msg["operationId"],
            {"label": request["label"], "host": request["host"]},
            lambda job: self._add(job, request),
        )

    async def start_remove(self, msg: dict) -> dict:
        await self.load()
        camera = str(msg.get("camera") or "")
        if not _CAMERA.match(camera):
            raise FrigateError("frigate_invalid_request", "攝影機名稱不正確")
        return self._start(
            "remove",
            msg["operationId"],
            {"camera": camera},
            lambda job: self._remove(job, camera),
        )

    async def start_configure(self, msg: dict) -> dict:
        await self.load()
        camera = str(msg.get("camera") or "")
        if not _CAMERA.match(camera):
            raise FrigateError("frigate_invalid_request", "攝影機名稱不正確")
        changes = settings_request(msg)
        return self._start(
            "configure",
            msg["operationId"],
            {"camera": camera},
            lambda job: self._configure(job, camera, changes),
        )

    def _add_request(self, msg: dict) -> dict:
        try:
            host = camera_host(msg.get("host"))
            username = str(msg.get("username") or "")
            password = str(msg.get("password") or "")
            main = stream_uri(msg.get("main"), host)
            sub = stream_uri(msg["sub"], host) if msg.get("sub") else None
            onvif_port = (
                camera_port(msg.get("onvifPort"), 80) if msg.get("ptz") else None
            )
        except CameraError as err:
            raise FrigateError(err.code, err.message) from None
        if sub == main:
            sub = None
        label = re.sub(r"\s+", " ", str(msg.get("name") or "")).strip()
        if not label or len(label) > 60 or any(ord(c) < 32 for c in label):
            raise FrigateError("frigate_invalid_request", "請輸入 1–60 字的攝影機名稱")
        return {
            "label": label,
            "host": host,
            "username": username,
            "password": password,
            "main": {
                "uri": main,
                "width": _int(msg.get("mainWidth")),
                "height": _int(msg.get("mainHeight")),
            },
            "sub": (
                {
                    "uri": sub,
                    "width": _int(msg.get("subWidth")),
                    "height": _int(msg.get("subHeight")),
                }
                if sub
                else None
            ),
            "record": msg.get("record") is not False,
            "detect": msg.get("detect") is not False,
            "onvifPort": onvif_port,
        }

    def _start(self, kind: str, operation_id: str, fields: dict, run: Callable) -> dict:
        if not _UUID.match(str(operation_id)):
            raise FrigateError("frigate_invalid_request", "作業識別碼不正確")
        existing = self.jobs.get(operation_id) or next(
            (j for j in self.data["jobs"] if j.get("jobId") == operation_id), None
        )
        if existing is not None:
            # A retried request after a lost response returns the same job.
            return _public_job(existing)
        if self.active is not None and not self.active.done():
            raise FrigateError("frigate_busy", "Frigate 正在處理其他攝影機，請稍候再試")
        job = {
            "jobId": operation_id,
            "kind": kind,
            "state": "running",
            "stage": "prepare",
            "message": "",
            "camera": fields.get("camera"),
            "label": fields.get("label"),
            "host": fields.get("host"),
            "startedAt": _now(),
            "finishedAt": None,
            "result": None,
            "error": None,
        }
        self.jobs[operation_id] = job
        self.data["jobs"] = [*self.data["jobs"], job][-10:]
        self.active = self.create_task(self._run(job, run), f"REXLiTE Frigate {kind}")
        return _public_job(job)

    async def _run(self, job: dict, run: Callable) -> None:
        with contextlib.suppress(Exception):
            await self._save()
        try:
            job["result"] = await run(job)
            job["state"] = "completed"
        except (FrigateError, CameraError) as err:
            job.update(state="failed", error={"code": err.code, "message": err.message})
        except Exception:  # noqa: BLE001 - the job record must always finish
            job.update(
                state="failed",
                error={
                    "code": "frigate_failed",
                    "message": "攝影機作業失敗，請重新整理狀態",
                },
            )
        finally:
            job["finishedAt"] = _now()
            job["message"] = ""
            with contextlib.suppress(Exception):
                await self._save()

    async def _add(self, job: dict, request: dict) -> dict:
        api = await self.api()
        config = await api.config()
        cameras = (
            config.get("cameras") if isinstance(config.get("cameras"), dict) else {}
        )
        go2rtc = config.get("go2rtc") if isinstance(config.get("go2rtc"), dict) else {}
        streams = (
            go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
        )
        host = request["host"]
        for name, camera in cameras.items():
            if isinstance(camera, dict) and host in camera_hosts(name, camera, streams):
                label = camera.get("friendly_name") or name
                raise FrigateError(
                    "frigate_camera_exists", f"這台攝影機已在 Frigate（{label}）"
                )
        key = camera_key(request["label"], host, [*cameras, *streams])
        job["camera"] = key

        job.update(stage="streams", message="確認攝影機串流")
        username, password = request["username"], request["password"]
        main, sub = dict(request["main"]), request["sub"] and dict(request["sub"])
        for stream in (main, sub):
            if stream:
                checked = await rtsp_check(stream["uri"], username, password)
                codec = (checked["codecs"] or [""])[0]
                if codec and codec not in SUPPORTED_CODECS and stream is main:
                    raise FrigateError(
                        "frigate_codec_unsupported",
                        f"主串流為 {codec}，Frigate 錄影需要 H.264 或 H.265",
                    )
                # go2rtc wants the URL percent-encoded; Frigate's ffmpeg encodes itself.
                stream["source"] = with_credentials(stream["uri"], username, password)
                stream["direct"] = with_raw_credentials(
                    stream["uri"], username, password
                )

        job.update(stage="configure", message="寫入 Frigate 設定")
        restream = True
        try:
            await self.go2rtc.streams()
        except FrigateError:
            restream = False  # Frigate reads the camera directly instead.
        added_streams: list[str] = []
        try:
            if restream:
                # Live restreams let Frigate size the detect stream while it
                # validates the new configuration.
                await self.go2rtc.add(key, main["source"] + "#backchannel=0")
                added_streams.append(key)
                if sub:
                    await self.go2rtc.add(
                        f"{key}_sub", sub["source"] + "#backchannel=0"
                    )
                    added_streams.append(f"{key}_sub")

            placeholder = cameras.get(PLACEHOLDER_CAMERA)
            if isinstance(placeholder, dict) and is_placeholder(
                PLACEHOLDER_CAMERA, placeholder
            ):
                # Frigate's sample camera never streams or records.
                with contextlib.suppress(FrigateError):
                    await api.config_set({"cameras": {PLACEHOLDER_CAMERA: ""}})
            onvif = (
                {
                    "host": host,
                    "port": request["onvifPort"],
                    "user": username,
                    "password": password,
                }
                # Frigate expands {NAME} in these fields as environment
                # variables; stream URLs are percent-encoded instead.
                if request["onvifPort"]
                and username
                and not any(c in username + password for c in "{}")
                else None
            )
            block = camera_config(
                label=request["label"],
                key=key,
                main=main,
                sub=sub,
                restream=restream,
                record=request["record"],
                detect=request["detect"],
                onvif=onvif,
            )
            data: dict[str, Any] = {"cameras": {key: block}}
            if restream:
                data["go2rtc"] = {
                    "streams": {
                        name: [
                            (main if name == key else sub)["source"] + "#backchannel=0"
                        ]
                        for name in added_streams
                    }
                }
            await api.config_set(data)
        except BaseException:
            for name in added_streams:
                await self.go2rtc.remove(name)
            raise

        try:
            job.update(stage="restart", message="重新啟動 Frigate（約 1 分鐘）")
            await api.restart()
            await self._wait_back(api)
            job.update(stage="verify", message="等待攝影機畫面")
            fps = await self._wait_fps(api, key)
            if fps <= 0:
                raise FrigateError(
                    "frigate_stream_not_running",
                    "Frigate 未取得攝影機畫面，已還原設定；請確認串流編碼與網路",
                )
        except BaseException:
            job.update(stage="rollback", message="還原設定")
            with contextlib.suppress(Exception):
                await self._take_out(job, api, key, added_streams)
            raise
        snapshot = None
        with contextlib.suppress(FrigateError):
            image = await api.latest_jpeg(key, 240)
            snapshot = base64.b64encode(image).decode()
        self.data["managed"][key] = {
            "host": host,
            "label": request["label"],
            "addedAt": _now(),
            "operationId": job["jobId"],
        }
        await self._save()
        return {
            "camera": key,
            "label": request["label"],
            "fps": fps,
            "restream": restream,
            "restarted": True,
            "snapshot": snapshot,
        }

    async def _wait_fps(self, api: FrigateApi, key: str) -> float:
        """Frames per second once the camera streams, or 0 after the timeout."""

        deadline = time.monotonic() + self.verify_seconds
        while True:
            with contextlib.suppress(FrigateError):
                cameras = (await api.stats()).get("cameras") or {}
                current = cameras.get(key) if isinstance(cameras, dict) else None
                fps = float((current or {}).get("camera_fps") or 0)
                if fps > 0:
                    return round(fps, 1)
            if time.monotonic() >= deadline:
                return 0.0
            await self.sleep(2)

    async def _wait_back(self, api: FrigateApi) -> None:
        deadline = time.monotonic() + self.restart_seconds
        await self.sleep(5)
        while time.monotonic() < deadline:
            with contextlib.suppress(FrigateError):
                if await api.version():
                    return
            await self.sleep(3)
        raise FrigateError(
            "frigate_unreachable", "Frigate 重新啟動後尚未恢復，請稍後檢查"
        )

    async def _take_out(
        self, job: dict, api: FrigateApi, key: str, restreams: list[str]
    ) -> bool:
        """Drop a camera and its restreams, then restart Frigate to stop it.

        Returns whether Frigate was restarted.
        """

        config = await api.config()
        if key in (config.get("cameras") or {}):
            await self._drain(job, api, key)
            config = await api.config()
        go2rtc = config.get("go2rtc") if isinstance(config.get("go2rtc"), dict) else {}
        streams = (
            go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
        )
        data: dict[str, Any] = {}
        if key in (config.get("cameras") or {}):
            data["cameras"] = {key: ""}
        names = [name for name in restreams if name in streams]
        if names:
            data["go2rtc"] = {"streams": {name: "" for name in names}}
        if data:
            await api.config_set(data)
        for name in restreams:
            await self.go2rtc.remove(name)
        if "cameras" not in data:
            return False
        job.update(stage="restart", message="重新啟動 Frigate（約 1 分鐘）")
        await api.restart()
        await self._wait_back(api)
        return True

    async def _drain(self, job: dict, api: FrigateApi, key: str) -> None:
        """Stop a camera live so Frigate drops its cached recording segments."""

        job.update(stage="configure", message="停止攝影機錄影")
        for field, value in (("record", {"enabled": False}), ("enabled", False)):
            with contextlib.suppress(FrigateError):
                await api.config_set(
                    {"cameras": {key: {field: value}}},
                    f"config/cameras/{key}/{field}",
                )
        # Frigate's recording maintainer runs every 5 s.
        await self.sleep(self.drain_seconds)

    async def _remove(self, job: dict, camera: str) -> dict:
        api = await self.api()
        config = await api.config()
        cameras = (
            config.get("cameras") if isinstance(config.get("cameras"), dict) else {}
        )
        if camera not in cameras:
            raise FrigateError("frigate_camera_not_found", "Frigate 已沒有這台攝影機")
        current = cameras[camera] if isinstance(cameras[camera], dict) else {}
        job["label"] = str(current.get("friendly_name") or camera)
        go2rtc = config.get("go2rtc") if isinstance(config.get("go2rtc"), dict) else {}
        streams = (
            go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
        )
        live = current.get("live") if isinstance(current.get("live"), dict) else {}
        # Only restreams this camera owns; another camera may share a stream.
        others = {
            str(urlsplit(str(i.get("path") or "")).path.strip("/"))
            for name, other in cameras.items()
            if name != camera and isinstance(other, dict)
            for i in ((other.get("ffmpeg") or {}).get("inputs") or [])
            if isinstance(i, dict)
        }
        owned = [
            name
            for name in dict.fromkeys(
                [camera, f"{camera}_sub", *(live.get("streams") or {}).values()]
            )
            if name in streams and name not in others
        ]
        job.update(stage="configure", message="更新 Frigate 設定")
        restarted = await self._take_out(job, api, camera, owned)
        self.data["managed"].pop(camera, None)
        await self._save()
        return {"camera": camera, "removed": True, "restarted": restarted}

    async def _configure(self, job: dict, camera: str, changes: dict) -> dict:
        api = await self.api()
        config = await api.config()
        cameras = (
            config.get("cameras") if isinstance(config.get("cameras"), dict) else {}
        )
        current = cameras.get(camera)
        if not isinstance(current, dict):
            raise FrigateError("frigate_camera_not_found", "Frigate 已沒有這台攝影機")
        job["label"] = str(current.get("friendly_name") or camera)
        sections = settings_patch(current, changes)
        before = camera_settings(current)
        # See the module docstring: recording only starts after a restart.
        restart = changes.get("record") is True and not before["record"]
        job.update(stage="configure", message="寫入 Frigate 設定")
        fps = None
        if restart:
            await api.config_set({"cameras": {camera: sections}})
            job.update(stage="restart", message="重新啟動 Frigate（約 1 分鐘）")
            await api.restart()
            await self._wait_back(api)
            job.update(stage="verify", message="等待攝影機畫面")
            fps = await self._wait_fps(api, camera)
        else:
            for section, value in sections.items():
                await api.config_set(
                    {"cameras": {camera: {section: value}}},
                    f"config/cameras/{camera}/{section}",
                )
        after = (await api.config(fresh=True)).get("cameras", {}).get(camera)
        return {
            "camera": camera,
            "restarted": restart,
            "fps": fps,
            "settings": camera_settings(after if isinstance(after, dict) else {}),
        }

    # Camera-side settings (ONVIF) --------------------------------------------

    async def _control(self, camera: str) -> tuple[FrigateApi, CameraControl]:
        if not _CAMERA.match(camera):
            raise FrigateError("frigate_invalid_request", "攝影機名稱不正確")
        api = await self.api()
        access = camera_access(await api.raw_config(), camera)
        control = CameraControl(self.session, access)
        await control.connect()
        return api, control

    async def camera_info(self, msg: dict) -> dict:
        camera = str(msg.get("camera") or "")
        async with self.camera_locks.setdefault(camera, asyncio.Lock()):
            _api, control = await self._control(camera)
            return {"camera": camera, **await control.info()}

    async def camera_control(self, msg: dict) -> dict:
        camera, op = str(msg.get("camera") or ""), str(msg.get("op") or "")
        async with self.camera_locks.setdefault(camera, asyncio.Lock()):
            api, control = await self._control(camera)
            result: dict[str, Any] = {"camera": camera, "op": op}
            if op == "move":
                await control.move(
                    msg["direction"],
                    float(msg.get("speed", 0.5)),
                    float(msg.get("seconds", 0.5)),
                )
                if msg.get("snapshot", True):
                    result["snapshot"] = await self._settled_frame(api, camera, 0.6)
            elif op == "stop":
                await control.stop()
            elif op == "goto":
                await control.goto(msg["token"])
                # GotoPreset returns at once; the camera turns for a moment.
                if msg.get("snapshot", True):
                    result["snapshot"] = await self._settled_frame(api, camera, 2.5)
            elif op == "save_preset":
                name = re.sub(r"\s+", " ", str(msg.get("name") or "")).strip()
                if not name or any(ord(c) < 32 for c in name):
                    raise FrigateError("frigate_invalid_request", "請輸入預置點名稱")
                result["token"] = await control.save_preset(name, msg.get("token"))
                result["presets"] = await control.presets()
            elif op == "remove_preset":
                await control.remove_preset(msg["token"])
                result["presets"] = await control.presets()
            elif op == "image":
                changes = dict(msg.get("image") or {})
                if not changes:
                    raise FrigateError("frigate_invalid_request", "沒有要變更的設定")
                await control.set_image(changes)
                result["image"] = await control.image()
            elif op == "quality":
                changes = dict(msg.get("quality") or {})
                if not msg.get("profile") or not changes:
                    raise FrigateError("frigate_invalid_request", "沒有要變更的設定")
                await control.set_quality(str(msg["profile"]), changes)
                result["quality"] = await control.quality()
            elif op == "clock":
                await control.set_clock(
                    bool(msg.get("ntp")),
                    str(msg.get("server") or DEFAULT_NTP),
                    self.time_zone(),
                )
                await self.sleep(1)
                result["clock"] = await control.clock()
            else:
                raise FrigateError("frigate_invalid_request", "不支援的攝影機操作")
            return result

    async def webrtc(self, msg: dict) -> dict:
        """Answer a browser's WebRTC offer with the hub's go2rtc.

        Only this exchange crosses the cloud; video and voice then flow
        browser ↔ hub directly (or through TURN when no direct path exists).
        Talking needs the camera's ONVIF audio backchannel, which the
        Frigate restreams switch off (``#backchannel=0``), so it uses a
        separate ``<camera>_talk`` restream that go2rtc opens on demand.
        """

        camera = str(msg.get("camera") or "")
        if not _CAMERA.match(camera):
            raise FrigateError("frigate_invalid_request", "攝影機名稱不正確")
        offer = str(msg.get("offer") or "")
        if not offer.startswith("v=0"):
            raise FrigateError("frigate_invalid_request", "連線參數不正確")
        quality = "main" if msg.get("quality") == "main" else "sub"
        talk = bool(msg.get("talk"))
        api = await self.api()
        sources = stream_sources(await api.raw_config(), camera)
        if not sources:
            raise FrigateError(
                "frigate_live_unavailable", "這台攝影機沒有可即時觀看的串流"
            )
        if quality not in sources:
            quality = next(iter(sources))
        name = camera if quality == "main" else f"{camera}_sub"
        if talk:
            name = f"{camera}_talk"
            # Credentials stay on this hub: go2rtc gets them from here only.
            await self.go2rtc.add(name, sources[quality])
        answer = await self.go2rtc.webrtc(name, offer)
        return {
            "camera": camera,
            "quality": quality,
            "qualities": sorted(sources, key=("main", "sub").index),
            "talk": talk and accepts_audio(answer),
            "answer": answer,
        }

    async def _settled_frame(
        self, api: FrigateApi, camera: str, seconds: float
    ) -> str | None:
        """A frame after the camera stopped moving; Frigate decodes at 5 fps."""

        await self.sleep(seconds)
        with contextlib.suppress(FrigateError):
            return base64.b64encode(await api.latest_jpeg(camera, 360)).decode()
        return None

    async def snapshot(self, msg: dict) -> dict:
        camera = str(msg.get("camera") or "")
        if not _CAMERA.match(camera):
            raise FrigateError("frigate_invalid_request", "攝影機名稱不正確")
        height = max(120, min(int(msg.get("height") or 360), 720))
        image = await (await self.api()).latest_jpeg(camera, height)
        return {
            "camera": camera,
            "contentType": "image/jpeg",
            "data": base64.b64encode(image).decode(),
            "capturedAt": _now(),
        }


def _days(value: Any) -> int | None:
    days = value.get("days") if isinstance(value, dict) else None
    return _int(days) if days is not None else None


def camera_settings(camera: dict) -> dict:
    """The settings an installer can change, as Frigate resolved them.

    ``recordDays`` is continuous recording (``record.continuous`` from Frigate
    0.16, ``record.retain`` before); ``eventDays`` keeps alerts and detections.
    """

    record = camera.get("record") if isinstance(camera.get("record"), dict) else {}
    detect = camera.get("detect") if isinstance(camera.get("detect"), dict) else {}
    objects = camera.get("objects") if isinstance(camera.get("objects"), dict) else {}
    alerts = record.get("alerts") if isinstance(record.get("alerts"), dict) else {}
    track = objects.get("track")
    return {
        "record": bool(record.get("enabled")),
        "detect": bool(detect.get("enabled")),
        "recordDays": _days(record.get("continuous", record.get("retain"))),
        "eventDays": _days(alerts.get("retain")),
        "objects": [str(o) for o in track] if isinstance(track, list) else None,
    }


def settings_request(msg: dict) -> dict:
    """The settings a ``configure`` request changes; at least one."""

    changes: dict[str, Any] = {}
    for key in ("record", "detect"):
        if isinstance(msg.get(key), bool):
            changes[key] = msg[key]
    for key in ("recordDays", "eventDays"):
        if msg.get(key) is not None:
            days = _int(msg[key])
            if days is None or not 0 <= days <= MAX_RETAIN_DAYS:
                raise FrigateError(
                    "frigate_invalid_request", f"保存天數需為 0–{MAX_RETAIN_DAYS} 天"
                )
            changes[key] = days
    if msg.get("objects") is not None:
        objects = list(dict.fromkeys(str(o) for o in msg["objects"]))
        if not objects or any(o not in TRACKABLE_OBJECTS for o in objects):
            raise FrigateError("frigate_invalid_request", "請至少選擇一種偵測物件")
        changes["objects"] = objects
    if not changes:
        raise FrigateError("frigate_invalid_request", "沒有要變更的設定")
    return changes


def settings_patch(camera: dict, changes: dict) -> dict:
    """``cameras.<name>`` sections for ``/api/config/set``, keyed by update topic."""

    record = camera.get("record") if isinstance(camera.get("record"), dict) else {}
    sections: dict[str, dict] = {}
    patch: dict[str, Any] = {}
    if "record" in changes:
        patch["enabled"] = changes["record"]
    if "recordDays" in changes:
        # Frigate 0.16 renamed record.retain to record.continuous.
        legacy = "retain" in record and "continuous" not in record
        key = "retain" if legacy else "continuous"
        patch[key] = {"days": changes["recordDays"]}
    if "eventDays" in changes:
        for review in ("alerts", "detections"):
            patch[review] = {"retain": {"days": changes["eventDays"]}}
    if patch:
        sections["record"] = patch
    if "detect" in changes:
        sections["detect"] = {"enabled": changes["detect"]}
    if "objects" in changes:
        sections["objects"] = {"track": changes["objects"]}
    return sections


def accepts_audio(answer: str) -> bool:
    """Whether go2rtc will take the browser's microphone (a camera backchannel)."""

    sections = answer.split("\nm=")
    for section in sections[1:]:
        if section.startswith("audio") and re.search(
            r"^a=(recvonly|sendrecv)\r?$", section, re.MULTILINE
        ):
            return True
    return False


def pick_streams(streams: list[dict]) -> tuple[dict | None, dict | None]:
    """Record the largest working stream; detect on a smaller one."""

    usable = [s for s in streams if s["ok"]]
    if not usable:
        return None, None

    def area(stream: dict) -> int:
        return (stream.get("width") or 0) * (stream.get("height") or 0)

    def recordable(stream: dict) -> bool:
        return not stream.get("codec") or stream["codec"] in SUPPORTED_CODECS

    # A stable sort keeps the camera's own order (main stream first) for
    # streams of equal or unknown size.
    ordered = sorted(usable, key=area, reverse=True)
    main = next((s for s in ordered if recordable(s)), ordered[0])
    rest = [s for s in ordered if s is not main]
    rest = [s for s in rest if recordable(s)] or rest
    if not rest:
        return main, None
    smaller = [s for s in rest if area(s) < area(main)]
    adequate = [s for s in smaller if (s.get("width") or 0) >= 640]
    if adequate:
        return main, adequate[-1]  # the smallest that still detects well
    if smaller:
        return main, smaller[0]  # the largest of the tiny ones
    return main, rest[0]


def _hints(vendor: str | None, onvif: dict, streams: list[dict]) -> list[str]:
    hints: list[str] = []
    if vendor == TPLINK:
        hints.append("TP-Link：Tapo 請使用 App「進階設定 → 攝影機帳號」建立的帳密")
    if not onvif.get("ok") and not any(s["ok"] for s in streams):
        hints.append("找不到可用串流，可改用完整 RTSP 網址")
    if any(
        s["ok"] and s.get("codec") and s["codec"] not in SUPPORTED_CODECS
        for s in streams
    ):
        hints.append("Frigate 錄影需要 H.264 或 H.265 串流")
    return hints


async def _async_value(value: Any) -> Any:
    return value


def register_frigate_bridge(hass: Any) -> FrigateBridge:
    """Register admin-only ``rexlite/frigate/*`` commands once per HA instance."""

    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store

    from .const import INTEGRATION_VERSION

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]

    async def interfaces() -> list[str]:
        """Every LAN IPv4 address, so cameras on a second NIC are found too.

        HA's own network settings may enable only the default adapter; camera
        VLANs are usually on another one, so Docker bridges are the only skip.
        """

        from homeassistant.components import network

        addresses: list[str] = []
        for adapter in await network.async_get_adapters(hass):
            name = str(adapter.get("name") or "")
            if name.startswith(("docker", "br-", "veth", "hassio", "lo")):
                continue
            for ipv4 in adapter.get("ipv4") or []:
                with contextlib.suppress(CameraError):
                    addresses.append(camera_host(ipv4.get("address")))
        return addresses

    bridge = hass.data[DATA_KEY] = FrigateBridge(
        store=Store(hass, 1, STORE_KEY, private=True),
        session=async_get_clientsession(hass),
        create_task=lambda coro, name: hass.async_create_background_task(coro, name),
        interfaces=interfaces,
        integration_version=INTEGRATION_VERSION,
        time_zone=lambda: hass.config.time_zone,
    )

    def handler(name: str, schema: dict, run: Callable):
        @websocket_api.websocket_command(
            {vol.Required("type"): f"rexlite/frigate/{name}", **schema}
        )
        @websocket_api.require_admin
        @websocket_api.async_response
        async def command(hass: Any, connection: Any, msg: dict) -> None:
            try:
                result = await run(msg)
            except (FrigateError, CameraError) as err:
                connection.send_error(msg["id"], err.code, err.message)
            except Exception:  # noqa: BLE001 - never break the admin websocket
                connection.send_error(
                    msg["id"], "frigate_failed", "攝影機操作失敗，請重新整理狀態"
                )
            else:
                connection.send_result(msg["id"], result)

        return command

    text = vol.All(str, vol.Length(max=512))
    host = vol.All(str, vol.Length(min=7, max=15))
    port = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
    size = vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=16, max=8192)))
    uri = vol.All(str, vol.Length(min=8, max=1024))
    operation = vol.All(str, vol.Length(min=36, max=36))
    camera = vol.All(str, vol.Match(_CAMERA))
    days = vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_RETAIN_DAYS))
    commands = (
        handler("status", {}, lambda msg: bridge.status()),
        handler(
            "discover",
            {
                vol.Optional("seconds", default=3): vol.All(
                    vol.Coerce(float), vol.Range(min=1, max=8)
                )
            },
            bridge.discover,
        ),
        handler(
            "probe",
            {
                vol.Required("host"): host,
                vol.Optional("port"): port,
                vol.Optional("username", default=""): text,
                vol.Optional("password", default=""): text,
                vol.Optional("rtspUrl", default=""): vol.All(str, vol.Length(max=1024)),
                vol.Optional("hint", default=""): vol.All(str, vol.Length(max=120)),
            },
            bridge.probe,
        ),
        handler(
            "activate",
            {
                vol.Required("host"): host,
                vol.Optional("port"): port,
                vol.Required("password"): vol.All(str, vol.Length(min=9, max=32)),
            },
            bridge.activate,
        ),
        handler(
            "add",
            {
                vol.Required("operationId"): operation,
                vol.Required("name"): vol.All(str, vol.Length(min=1, max=60)),
                vol.Required("host"): host,
                vol.Required("main"): uri,
                vol.Optional("sub"): vol.Any(None, uri),
                vol.Optional("username", default=""): text,
                vol.Optional("password", default=""): text,
                vol.Optional("mainWidth"): size,
                vol.Optional("mainHeight"): size,
                vol.Optional("subWidth"): size,
                vol.Optional("subHeight"): size,
                vol.Optional("record", default=True): bool,
                vol.Optional("detect", default=True): bool,
                vol.Optional("ptz", default=False): bool,
                vol.Optional("onvifPort"): port,
            },
            bridge.start_add,
        ),
        handler(
            "job",
            {vol.Required("jobId"): operation},
            lambda msg: _async_value(bridge.job(msg["jobId"])),
        ),
        handler(
            "remove",
            {vol.Required("operationId"): operation, vol.Required("camera"): camera},
            bridge.start_remove,
        ),
        handler(
            "configure",
            {
                vol.Required("operationId"): operation,
                vol.Required("camera"): camera,
                vol.Optional("record"): bool,
                vol.Optional("detect"): bool,
                vol.Optional("recordDays"): days,
                vol.Optional("eventDays"): days,
                vol.Optional("objects"): vol.All(
                    [vol.In(TRACKABLE_OBJECTS)],
                    vol.Length(min=1, max=len(TRACKABLE_OBJECTS)),
                ),
            },
            bridge.start_configure,
        ),
        handler("camera_info", {vol.Required("camera"): camera}, bridge.camera_info),
        handler(
            "webrtc",
            {
                vol.Required("camera"): camera,
                vol.Required("offer"): vol.All(str, vol.Length(min=10, max=20000)),
                vol.Optional("quality", default="sub"): vol.In(("main", "sub")),
                vol.Optional("talk", default=False): bool,
            },
            bridge.webrtc,
        ),
        handler(
            "camera_control",
            {
                vol.Required("camera"): camera,
                vol.Required("op"): vol.In(
                    (
                        "move",
                        "stop",
                        "goto",
                        "save_preset",
                        "remove_preset",
                        "image",
                        "clock",
                        "quality",
                    )
                ),
                vol.Optional("direction"): vol.In(tuple(DIRECTIONS)),
                vol.Optional("speed", default=0.5): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=1)
                ),
                vol.Optional("seconds", default=0.5): vol.All(
                    vol.Coerce(float), vol.Range(min=0.1, max=2)
                ),
                # A live view already shows the move: no frame through the cloud.
                vol.Optional("snapshot", default=True): bool,
                vol.Optional("token"): vol.All(str, vol.Length(min=1, max=64)),
                vol.Optional("name"): vol.All(str, vol.Length(min=1, max=40)),
                vol.Optional("image"): {
                    vol.Optional(key): vol.All(
                        vol.Coerce(float), vol.Range(min=-1000, max=1000)
                    )
                    for key in ("brightness", "saturation", "contrast", "sharpness")
                }
                | {vol.Optional("irCut"): vol.In(IR_CUT_MODES)},
                vol.Optional("profile"): vol.All(str, vol.Length(min=1, max=64)),
                vol.Optional("quality"): {
                    vol.Optional(key): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=100000)
                    )
                    for key in ("width", "height", "fps", "bitrate")
                },
                vol.Optional("ntp"): bool,
                vol.Optional("server"): vol.All(
                    str, vol.Length(min=1, max=253), vol.Match(r"^[A-Za-z0-9.-]+$")
                ),
            },
            bridge.camera_control,
        ),
        handler(
            "snapshot",
            {
                vol.Required("camera"): camera,
                vol.Optional("height", default=360): vol.All(
                    vol.Coerce(int), vol.Range(min=120, max=720)
                ),
            },
            bridge.snapshot,
        ),
    )
    for command in commands:
        websocket_api.async_register_command(hass, command)
    return bridge
