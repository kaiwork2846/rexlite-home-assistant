"""Frigate camera jobs against a scripted Frigate + go2rtc."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_frigate_bridge_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
for name in ("camera_probe", "vigi_setup", "frigate_bridge"):
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", PACKAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
m = sys.modules[f"{package.__name__}.frigate_bridge"]
probe = sys.modules[f"{package.__name__}.camera_probe"]

OPERATION = "3f2a3c8e-5d1b-4a8e-9c3d-2b1a0f9e8d7c"
OTHER_OPERATION = "6b1d2c3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
PASSWORD = "s3cret!Pass"
EXISTING = {
    "m6d459e1198784b8fa0eec25491d50018": {
        "friendly_name": "VIGI C540V",
        "enabled": True,
        "ffmpeg": {
            "inputs": [
                {
                    "path": "rtsp://127.0.0.1:8554/m6d459e1198784b8fa0eec25491d50018",
                    "roles": ["record"],
                },
                {
                    "path": "rtsp://127.0.0.1:8554/m6d459e1198784b8fa0eec25491d50018_sub",
                    "roles": ["detect"],
                },
            ]
        },
        "detect": {"enabled": True},
        "record": {"enabled": True},
        "onvif": {"host": "192.168.2.170", "port": 2020},
        "live": {
            "streams": {
                "720P": "m6d459e1198784b8fa0eec25491d50018",
                "360P": "m6d459e1198784b8fa0eec25491d50018_sub",
            }
        },
    },
    "name_of_your_camera": {
        "enabled": True,
        "ffmpeg": {
            "inputs": [
                {"path": "rtsp://10.0.10.10:554/rtsp", "roles": ["detect", "record"]}
            ]
        },
        "detect": {"enabled": False},
        "record": {"enabled": False},
    },
}
EXISTING_STREAMS = {
    "m6d459e1198784b8fa0eec25491d50018": [
        "rtsp://*:*@192.168.2.170:554/stream1#backchannel=0"
    ],
    "m6d459e1198784b8fa0eec25491d50018_sub": [
        "rtsp://*:*@192.168.2.170:554/stream2#backchannel=0"
    ],
}


class Response:
    def __init__(self, status: int, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self) -> str:
        if self.payload is None:
            return ""
        return (
            self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        )

    async def read(self) -> bytes:
        return self.payload if isinstance(self.payload, bytes) else b""


class Frigate:
    """Frigate's internal API and go2rtc, enough for the camera jobs.

    ``mode`` decides what a live ``add`` does: ``live`` streams at once,
    ``restart`` only after a Frigate restart, ``dark`` never sends frames.
    """

    def __init__(
        self,
        mode: str = "live",
        reject: bool = False,
        down: bool = False,
        go2rtc_down: bool = False,
    ):
        self.mode = mode
        self.reject = reject
        self.down = down
        self.go2rtc_down = go2rtc_down
        self.ffprobes: list[str] = []
        self.config = {
            "cameras": copy.deepcopy(EXISTING),
            "go2rtc": {"streams": copy.deepcopy(EXISTING_STREAMS)},
        }
        self.go2rtc = {name: {"producers": []} for name in EXISTING_STREAMS}
        self.running = {
            "m6d459e1198784b8fa0eec25491d50018": 5.0,
            "name_of_your_camera": 0.0,
        }
        self.calls: list[tuple[str, str, dict]] = []
        self.config_sets: list[dict] = []
        self.restarts = 0
        self.restarting = 0

    def request(self, method: str, url, **kwargs):
        parts = urlsplit(str(url))
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((method, str(url), kwargs))
        if (
            self.down
            or parts.port not in (5050, 1984)
            or (parts.port == 1984 and self.go2rtc_down)
        ):
            raise OSError("connection refused")
        if parts.port == 1984:
            return self.go2rtc_api(method, query)
        if self.restarting:
            self.restarting -= 1
            raise OSError("frigate restarting")
        path = parts.path
        if (method, path) == ("GET", "/api/version"):
            return Response(200, "0.17.1-416a9b7")
        if (method, path) == ("GET", "/api/config"):
            return Response(200, self.config)
        if (method, path) == ("GET", "/api/stats"):
            return Response(
                200,
                {
                    "cameras": {
                        name: {
                            "camera_fps": fps,
                            "detection_fps": fps,
                            "process_fps": fps,
                        }
                        for name, fps in self.running.items()
                    },
                    "service": {"uptime": 3707, "version": "0.17.1-416a9b7"},
                },
            )
        if (method, path) == ("PUT", "/api/config/set"):
            return self.config_set(kwargs["json"])
        if (method, path) == ("POST", "/api/restart"):
            self.restarts += 1
            self.restarting = 2
            # A camera that never sends frames is listed with 0 fps.
            self.running = {
                name: 0.0 if self.mode == "dark" and name not in EXISTING else 5.0
                for name in self.config["cameras"]
            }
            return Response(200, {"success": True})
        if method == "GET" and path.endswith("/latest.jpg"):
            return Response(200, b"\xff\xd8jpeg\xff\xd9")
        if (method, path) == ("GET", "/api/ffprobe"):
            self.ffprobes.append(query["paths"])
            return Response(
                200,
                [
                    {
                        "return_code": 0,
                        "stderr": [],
                        "stdout": {
                            "streams": [
                                {
                                    "codec_long_name": "H.264 / AVC",
                                    "width": 1280,
                                    "height": 720,
                                }
                            ]
                        },
                    }
                ],
            )
        return Response(404, {"detail": "Not Found"})

    def go2rtc_api(self, method: str, query: dict):
        if method == "GET":
            return Response(200, self.go2rtc)
        if method == "PUT":
            self.go2rtc[query["name"]] = {"src": query["src"]}
            return Response(200, "")
        if method == "DELETE":
            self.go2rtc.pop(query["src"], None)
            return Response(200, "")
        return Response(405, "")

    def config_set(self, body: dict):
        self.config_sets.append(copy.deepcopy(body))
        if self.reject:
            return Response(400, {"success": False, "message": "Error parsing config."})
        self.apply(self.config, body["config_data"])
        return Response(
            200, {"success": True, "message": "Config successfully updated"}
        )

    def apply(self, target: dict, data: dict):
        for key, value in data.items():
            if value == "":
                del target[key]  # Frigate raises (500) on a missing key
            elif isinstance(value, dict):
                self.apply(target.setdefault(key, {}), value)
            else:
                target[key] = value


class Store:
    def __init__(self, data=None):
        self.data = data
        self.saved: list[str] = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.saved.append(json.dumps(data, ensure_ascii=False))


def bridge(frigate: Frigate, store: Store | None = None):
    async def sleep(_seconds):
        await asyncio.sleep(0.005)

    return m.FrigateBridge(
        store=store or Store(),
        session=frigate,
        create_task=lambda coro, name: asyncio.get_running_loop().create_task(coro),
        integration_version="0.1.28",
        sleep=sleep,
        verify_seconds=0.4,
        restart_seconds=1,
        drain_seconds=0,
    )


ADD = {
    "operationId": OPERATION,
    "name": "VIGI C540V 大門",
    "host": "192.168.1.72",
    "main": "rtsp://192.168.1.72:554/stream1",
    "sub": "rtsp://192.168.1.72:554/stream2",
    "mainWidth": 2560,
    "mainHeight": 1440,
    "subWidth": 640,
    "subHeight": 360,
    "username": "admin",
    "password": PASSWORD,
    "record": True,
    "detect": True,
    "ptz": True,
    "onvifPort": 2020,
}


class HelperTests(unittest.TestCase):
    def test_camera_keys_are_frigate_safe_and_unique(self):
        self.assertEqual(m.camera_key("VIGI C540V", "192.168.1.72", []), "vigi_c540v")
        self.assertEqual(m.camera_key("大門入口", "192.168.1.72", []), "camera_72")
        self.assertEqual(
            m.camera_key(
                "VIGI C540V", "192.168.1.72", ["vigi_c540v", "vigi_c540v_2_sub"]
            ),
            "vigi_c540v_3",
        )

    def test_camera_hosts_follow_restreams(self):
        camera = EXISTING["m6d459e1198784b8fa0eec25491d50018"]
        self.assertEqual(
            m.camera_hosts(
                "m6d459e1198784b8fa0eec25491d50018", camera, EXISTING_STREAMS
            ),
            {"192.168.2.170"},
        )
        self.assertTrue(
            m.is_placeholder("name_of_your_camera", EXISTING["name_of_your_camera"])
        )
        self.assertFalse(m.is_placeholder("m6d459e1198784b8fa0eec25491d50018", camera))

    def test_streams_are_picked_by_size(self):
        streams = [
            {"uri": "a", "ok": True, "width": 640, "height": 360},
            {"uri": "b", "ok": True, "width": 2560, "height": 1440},
            {"uri": "c", "ok": False, "width": 3840, "height": 2160},
            {"uri": "d", "ok": True, "width": 1280, "height": 720},
        ]
        main, sub = m.pick_streams(streams)
        self.assertEqual((main["uri"], sub["uri"]), ("b", "a"))
        main, sub = m.pick_streams([{"uri": "x", "ok": True}, {"uri": "y", "ok": True}])
        self.assertEqual((main["uri"], sub["uri"]), ("x", "y"))
        same = {"ok": True, "width": 1280, "height": 720}
        main, sub = m.pick_streams([{**same, "uri": "s1"}, {**same, "uri": "s2"}])
        self.assertEqual((main["uri"], sub["uri"]), ("s1", "s2"))
        main, sub = m.pick_streams(
            [
                {
                    "uri": "jpeg",
                    "ok": True,
                    "width": 3840,
                    "height": 2160,
                    "codec": "MJPEG",
                },
                {
                    "uri": "hd",
                    "ok": True,
                    "width": 1920,
                    "height": 1080,
                    "codec": "H264",
                },
                {
                    "uri": "tiny",
                    "ok": True,
                    "width": 320,
                    "height": 240,
                    "codec": "H264",
                },
            ]
        )
        self.assertEqual((main["uri"], sub["uri"]), ("hd", "tiny"))
        self.assertEqual(m.pick_streams([{"uri": "z", "ok": False}]), (None, None))


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_lists_cameras_with_live_frames(self):
        status = await bridge(Frigate()).status()
        self.assertEqual(
            status["frigate"],
            {
                "reachable": True,
                "version": "0.17.1-416a9b7",
                "uptime": 3707,
                "restream": True,
            },
        )
        self.assertEqual(
            [
                (
                    c["camera"],
                    c["label"],
                    c["host"],
                    c["online"],
                    c["fps"],
                    c["ptz"],
                    c["placeholder"],
                )
                for c in status["cameras"]
            ],
            [
                (
                    "m6d459e1198784b8fa0eec25491d50018",
                    "VIGI C540V",
                    "192.168.2.170",
                    True,
                    5.0,
                    True,
                    False,
                ),
                (
                    "name_of_your_camera",
                    "name_of_your_camera",
                    "10.0.10.10",
                    False,
                    0.0,
                    False,
                    True,
                ),
            ],
        )

    async def test_frigate_down(self):
        status = await bridge(Frigate(down=True)).status()
        self.assertEqual(status["frigate"]["reachable"], False)
        self.assertEqual(status["frigate"]["error"]["code"], "frigate_unreachable")
        self.assertEqual(status["cameras"], [])


class AddTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.checks: list[tuple[str, str, str]] = []
        self.original = m.rtsp_check

        async def rtsp_check(uri, username, password, **kwargs):
            self.checks.append((uri, username, password))
            return {"status": 200, "codecs": ["H264"]}

        m.rtsp_check = rtsp_check

    def tearDown(self):
        m.rtsp_check = self.original

    async def run_job(self, frigate: Frigate, msg=ADD, store=None):
        b = bridge(frigate, store)
        job = await b.start_add(dict(msg))
        self.assertEqual(job["state"], "running")
        await b.active
        return b, b.job(msg["operationId"])

    async def test_camera_is_written_then_frigate_restarts(self):
        frigate = Frigate()
        store = Store()
        b, job = await self.run_job(frigate, store=store)
        self.assertEqual(job["state"], "completed", job["error"])
        self.assertEqual(job["camera"], "vigi_c540v")
        self.assertEqual(job["result"]["fps"], 5.0)
        self.assertTrue(job["result"]["restarted"])
        self.assertTrue(job["result"]["snapshot"])
        # Live adds break Frigate when it started with one camera: restart once.
        self.assertEqual(frigate.restarts, 1)
        source = "rtsp://admin:s3cret%21Pass@192.168.1.72:554/stream{}#backchannel=0"
        self.assertEqual(frigate.go2rtc["vigi_c540v"], {"src": source.format(1)})
        self.assertEqual(frigate.go2rtc["vigi_c540v_sub"], {"src": source.format(2)})
        placeholder, add = frigate.config_sets
        self.assertEqual(
            placeholder,
            {
                "requires_restart": 1,
                "config_data": {"cameras": {"name_of_your_camera": ""}},
            },
        )
        self.assertNotIn("update_topic", add)
        self.assertEqual(add["requires_restart"], 1)
        self.assertEqual(
            add["config_data"]["cameras"]["vigi_c540v"],
            {
                "enabled": True,
                "friendly_name": "VIGI C540V 大門",
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "rtsp://127.0.0.1:8554/vigi_c540v",
                            "input_args": "preset-rtsp-restream",
                            "roles": ["record"],
                        },
                        {
                            "path": "rtsp://127.0.0.1:8554/vigi_c540v_sub",
                            "input_args": "preset-rtsp-restream",
                            "roles": ["detect"],
                        },
                    ],
                    # Software decode: VAAPI crash-looped Frigate on a VIGI C540V.
                    "hwaccel_args": [],
                },
                "detect": {"enabled": True, "fps": 5, "width": 640, "height": 360},
                "record": {"enabled": True},
                "live": {"streams": {"1440P": "vigi_c540v", "360P": "vigi_c540v_sub"}},
                "onvif": {
                    "host": "192.168.1.72",
                    "port": 2020,
                    "user": "admin",
                    "password": PASSWORD,
                },
            },
        )
        self.assertEqual(
            add["config_data"]["go2rtc"],
            {
                "streams": {
                    "vigi_c540v": [source.format(1)],
                    "vigi_c540v_sub": [source.format(2)],
                }
            },
        )
        self.assertEqual([c[0] for c in self.checks], [ADD["main"], ADD["sub"]])
        # Credentials never reach jobs or results; HA storage keeps them only
        # in the remembered credentials, so the camera can be added again.
        public = json.dumps(b.job(OPERATION), ensure_ascii=False)
        self.assertNotIn(PASSWORD, public)
        self.assertTrue(store.saved)
        for saved in store.saved:
            outside = {k: v for k, v in json.loads(saved).items() if k != "credentials"}
            self.assertNotIn(PASSWORD, json.dumps(outside, ensure_ascii=False))
        remembered = next(iter(store.data["credentials"].values()))
        self.assertEqual(
            (remembered["password"], remembered["source"]), (PASSWORD, "add")
        )
        self.assertIn("vigi_c540v", store.data["managed"])
        status = await b.status()
        self.assertTrue(
            next(c for c in status["cameras"] if c["camera"] == "vigi_c540v")["managed"]
        )

    async def test_without_go2rtc_frigate_reads_the_camera_directly(self):
        frigate = Frigate(go2rtc_down=True)
        _b, job = await self.run_job(frigate)
        self.assertEqual(job["state"], "completed", job["error"])
        self.assertFalse(job["result"]["restream"])
        block = frigate.config_sets[-1]["config_data"]["cameras"]["vigi_c540v"]
        # Raw credentials: Frigate percent-encodes the password for ffmpeg.
        self.assertEqual(
            block["ffmpeg"]["inputs"],
            [
                {
                    "path": f"rtsp://admin:{PASSWORD}@192.168.1.72:554/stream1",
                    "roles": ["record"],
                },
                {
                    "path": f"rtsp://admin:{PASSWORD}@192.168.1.72:554/stream2",
                    "roles": ["detect"],
                },
            ],
        )
        self.assertEqual(block["ffmpeg"]["hwaccel_args"], [])
        self.assertNotIn("go2rtc", frigate.config_sets[-1]["config_data"])
        self.assertNotIn("live", block)

    async def test_a_camera_already_in_frigate_is_not_added_twice(self):
        frigate = Frigate()
        _b, job = await self.run_job(
            frigate,
            {
                **ADD,
                "host": "192.168.2.170",
                "main": "rtsp://192.168.2.170/stream1",
                "sub": None,
            },
        )
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "frigate_camera_exists")
        self.assertIn("VIGI C540V", job["error"]["message"])
        self.assertEqual(frigate.config_sets, [])

    async def test_a_camera_without_frames_is_taken_out_again(self):
        frigate = Frigate(mode="dark")
        _b, job = await self.run_job(frigate)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "frigate_stream_not_running")
        self.assertNotIn("vigi_c540v", frigate.config["cameras"])
        self.assertNotIn("vigi_c540v", frigate.config["go2rtc"]["streams"])
        self.assertNotIn("vigi_c540v", frigate.go2rtc)
        self.assertNotIn("vigi_c540v_sub", frigate.go2rtc)
        record, capture, delete = frigate.config_sets[-3:]
        self.assertEqual(record["update_topic"], "config/cameras/vigi_c540v/record")
        self.assertEqual(capture["update_topic"], "config/cameras/vigi_c540v/enabled")
        self.assertNotIn("update_topic", delete)  # Frigate's live remove is unsafe
        self.assertEqual(frigate.restarts, 2)

    async def test_rejected_config_leaves_no_restream_behind(self):
        frigate = Frigate(reject=True)
        _b, job = await self.run_job(frigate)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "frigate_config_rejected")
        self.assertEqual(set(frigate.go2rtc), set(EXISTING_STREAMS))
        self.assertEqual(frigate.restarts, 0)

    async def test_bad_stream_stops_before_frigate_is_touched(self):
        async def rtsp_check(uri, username, password, **kwargs):
            raise probe.CameraError("camera_auth_failed", "攝影機帳號或密碼錯誤")

        m.rtsp_check = rtsp_check
        frigate = Frigate()
        _b, job = await self.run_job(frigate)
        self.assertEqual(job["error"]["code"], "camera_auth_failed")
        self.assertEqual(frigate.config_sets, [])
        self.assertEqual(set(frigate.go2rtc), set(EXISTING_STREAMS))

    async def test_retries_return_the_same_job_and_others_wait(self):
        b = bridge(Frigate())
        first = await b.start_add(dict(ADD))
        again = await b.start_add(dict(ADD))
        self.assertEqual(first["jobId"], again["jobId"])
        with self.assertRaises(m.FrigateError) as caught:
            await b.start_add({**ADD, "operationId": OTHER_OPERATION})
        self.assertEqual(caught.exception.code, "frigate_busy")
        await b.active

    async def test_requests_are_validated(self):
        b = bridge(Frigate())
        for bad in (
            {**ADD, "operationId": "nope"},
            {**ADD, "host": "8.8.8.8"},
            {**ADD, "main": "rtsp://192.168.1.99/stream1"},
            {**ADD, "name": " "},
        ):
            with self.assertRaises(m.FrigateError):
                await b.start_add(bad)


class RemoveTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_drops_owned_restreams_and_restarts(self):
        frigate = Frigate()
        b = bridge(
            frigate,
            Store({"managed": {"m6d459e1198784b8fa0eec25491d50018": {}}, "jobs": []}),
        )
        await b.start_remove(
            {"operationId": OPERATION, "camera": "m6d459e1198784b8fa0eec25491d50018"}
        )
        await b.active
        job = b.job(OPERATION)
        self.assertEqual(job["state"], "completed", job["error"])
        self.assertEqual(job["label"], "VIGI C540V")
        self.assertEqual(
            job["result"],
            {
                "camera": "m6d459e1198784b8fa0eec25491d50018",
                "removed": True,
                "restarted": True,
            },
        )
        self.assertNotIn("m6d459e1198784b8fa0eec25491d50018", frigate.config["cameras"])
        self.assertEqual(frigate.config["go2rtc"]["streams"], {})
        self.assertEqual(frigate.go2rtc, {})
        # Recording and capture stop live first, so Frigate drops the cached
        # segments; then the camera is deleted and Frigate restarts.
        record, capture, delete = frigate.config_sets
        camera = "m6d459e1198784b8fa0eec25491d50018"
        self.assertEqual(record["update_topic"], f"config/cameras/{camera}/record")
        self.assertEqual(
            record["config_data"], {"cameras": {camera: {"record": {"enabled": False}}}}
        )
        self.assertEqual(capture["update_topic"], f"config/cameras/{camera}/enabled")
        self.assertEqual(
            capture["config_data"], {"cameras": {camera: {"enabled": False}}}
        )
        self.assertNotIn("update_topic", delete)
        self.assertEqual(frigate.restarts, 1)
        self.assertEqual(b.data["managed"], {})

    async def test_unknown_camera(self):
        b = bridge(Frigate())
        await b.start_remove({"operationId": OPERATION, "camera": "nope"})
        await b.active
        self.assertEqual(b.job(OPERATION)["error"]["code"], "frigate_camera_not_found")


CAMERA = "m6d459e1198784b8fa0eec25491d50018"


class ConfigureTests(unittest.IsolatedAsyncioTestCase):
    async def configure(self, frigate: Frigate, **changes):
        b = bridge(frigate)
        await b.start_configure({"operationId": OPERATION, "camera": CAMERA, **changes})
        await b.active
        return b.job(OPERATION)

    async def test_status_reports_settings(self):
        frigate = Frigate()
        frigate.config["cameras"][CAMERA]["record"].update(
            continuous={"days": 3}, alerts={"retain": {"days": 10}}
        )
        frigate.config["cameras"][CAMERA]["objects"] = {"track": ["person"]}
        camera = (await bridge(frigate).status())["cameras"][0]
        self.assertEqual(
            {k: camera[k] for k in ("record", "detect", "recordDays", "eventDays")},
            {"record": True, "detect": True, "recordDays": 3, "eventDays": 10},
        )
        self.assertEqual(camera["objects"], ["person"])
        # Frigate 0.15 and older keep continuous recording in record.retain.
        self.assertEqual(
            m.camera_settings({"record": {"retain": {"days": 7}}})["recordDays"], 7
        )

    async def test_live_changes_use_update_topics(self):
        frigate = Frigate()
        job = await self.configure(
            frigate, detect=False, recordDays=7, eventDays=14, objects=["person", "car"]
        )
        self.assertEqual(job["state"], "completed", job["error"])
        self.assertEqual(frigate.restarts, 0)
        self.assertFalse(job["result"]["restarted"])
        self.assertEqual(
            {c["update_topic"]: c["config_data"] for c in frigate.config_sets},
            {
                f"config/cameras/{CAMERA}/record": {
                    "cameras": {
                        CAMERA: {
                            "record": {
                                "continuous": {"days": 7},
                                "alerts": {"retain": {"days": 14}},
                                "detections": {"retain": {"days": 14}},
                            }
                        }
                    }
                },
                f"config/cameras/{CAMERA}/detect": {
                    "cameras": {CAMERA: {"detect": {"enabled": False}}}
                },
                f"config/cameras/{CAMERA}/objects": {
                    "cameras": {CAMERA: {"objects": {"track": ["person", "car"]}}}
                },
            },
        )
        settings = job["result"]["settings"]
        self.assertEqual(
            (settings["detect"], settings["recordDays"], settings["eventDays"]),
            (False, 7, 14),
        )
        self.assertEqual(settings["objects"], ["person", "car"])

    async def test_recording_off_stays_live(self):
        frigate = Frigate()
        job = await self.configure(frigate, record=False)
        self.assertEqual(job["state"], "completed", job["error"])
        self.assertEqual(frigate.restarts, 0)
        self.assertFalse(frigate.config["cameras"][CAMERA]["record"]["enabled"])

    async def test_recording_on_restarts_frigate(self):
        # ffmpeg only writes recording segments when recording was on at start.
        frigate = Frigate()
        frigate.config["cameras"][CAMERA]["record"]["enabled"] = False
        job = await self.configure(frigate, record=True, recordDays=3)
        self.assertEqual(job["state"], "completed", job["error"])
        self.assertEqual(frigate.restarts, 1)
        (written,) = frigate.config_sets
        self.assertNotIn("update_topic", written)
        self.assertEqual(
            written["config_data"],
            {
                "cameras": {
                    CAMERA: {"record": {"enabled": True, "continuous": {"days": 3}}}
                }
            },
        )
        self.assertEqual(
            (job["result"]["restarted"], job["result"]["fps"]), (True, 5.0)
        )

    async def test_legacy_retain_key(self):
        frigate = Frigate()
        frigate.config["cameras"][CAMERA]["record"]["retain"] = {"days": 0}
        await self.configure(frigate, recordDays=5)
        self.assertEqual(
            frigate.config["cameras"][CAMERA]["record"]["retain"], {"days": 5}
        )
        self.assertNotIn("continuous", frigate.config["cameras"][CAMERA]["record"])

    async def test_rejected_and_missing(self):
        job = await self.configure(Frigate(reject=True), detect=False)
        self.assertEqual(job["error"]["code"], "frigate_config_rejected")
        frigate = Frigate()
        b = bridge(frigate)
        await b.start_configure(
            {"operationId": OPERATION, "camera": "nope", "detect": False}
        )
        await b.active
        self.assertEqual(b.job(OPERATION)["error"]["code"], "frigate_camera_not_found")

    async def test_invalid_requests(self):
        b = bridge(Frigate())
        for changes in (
            {},
            {"recordDays": 400},
            {"objects": []},
            {"objects": ["unicorn"]},
        ):
            with self.assertRaises(m.FrigateError) as caught:
                await b.start_configure(
                    {"operationId": OPERATION, "camera": CAMERA, **changes}
                )
            self.assertEqual(caught.exception.code, "frigate_invalid_request")
        self.assertIsNone(b.active)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.originals = (m.onvif_streams, m.rtsp_check)

    def tearDown(self):
        m.onvif_streams, m.rtsp_check = self.originals

    async def test_onvif_streams_are_checked_and_ranked(self):
        async def onvif_streams(session, host, port, username, password):
            return {
                "port": 2020,
                "ptz": True,
                "device": {
                    "manufacturer": "TP-LINK",
                    "model": "VIGI C540V",
                    "firmware": "1",
                    "serial": "x",
                },
                "profiles": [
                    {
                        "token": "p1",
                        "name": "mainStream",
                        "uri": f"rtsp://{host}:554/stream1",
                        "codec": "H264",
                        "width": 2560,
                        "height": 1440,
                        "fps": 15,
                    },
                    {
                        "token": "p2",
                        "name": "minorStream",
                        "uri": f"rtsp://{host}:554/stream2",
                        "codec": "H264",
                        "width": 640,
                        "height": 360,
                        "fps": 15,
                    },
                ],
            }

        async def rtsp_check(uri, username, password, **kwargs):
            return {"status": 200, "codecs": ["H264"]}

        m.onvif_streams, m.rtsp_check = onvif_streams, rtsp_check
        result = await bridge(Frigate()).probe(
            {
                "host": "192.168.2.170",
                "port": 80,
                "username": "admin",
                "password": PASSWORD,
            }
        )
        self.assertEqual(result["onvif"], {"ok": True, "port": 2020, "ptz": True})
        self.assertEqual(result["main"], "rtsp://192.168.2.170:554/stream1")
        self.assertEqual(result["sub"], "rtsp://192.168.2.170:554/stream2")
        self.assertEqual(result["vendor"], "tplink")
        self.assertEqual(result["suggestedName"], "VIGI C540V")
        self.assertEqual(
            result["existing"],
            {"camera": "m6d459e1198784b8fa0eec25491d50018", "label": "VIGI C540V"},
        )
        self.assertNotIn(PASSWORD, json.dumps(result, ensure_ascii=False))

    async def test_tplink_paths_when_onvif_is_off(self):
        async def onvif_streams(*args):
            raise probe.CameraError("camera_onvif_failed", "攝影機未開啟 ONVIF 服務")

        tried: list[str] = []

        async def rtsp_check(uri, username, password, **kwargs):
            tried.append(uri)
            if uri.endswith(("/stream1", "/stream2")):
                return {"status": 200, "codecs": ["H264"]}
            raise probe.CameraError("camera_stream_not_found", "沒有這個串流路徑")

        m.onvif_streams, m.rtsp_check = onvif_streams, rtsp_check
        frigate = Frigate()
        result = await bridge(frigate).probe(
            {
                "host": "192.168.1.72",
                "username": "admin",
                "password": PASSWORD,
                "hint": "TP-IPC",
            }
        )
        self.assertEqual(result["onvif"]["error"]["code"], "camera_onvif_failed")
        self.assertEqual(tried[0], "rtsp://192.168.1.72:554/stream1")
        self.assertEqual(
            (result["main"], result["sub"]),
            ("rtsp://192.168.1.72:554/stream1", "rtsp://192.168.1.72:554/stream2"),
        )
        # Sizes come from Frigate's ffprobe when ONVIF cannot tell; it gets
        # the password as typed because it encodes it itself.
        self.assertEqual(result["streams"][0]["width"], 1280)
        self.assertEqual(
            frigate.ffprobes[0],
            f"rtsp://admin:{PASSWORD}@192.168.1.72:554/stream1",
        )
        self.assertTrue(any("Tapo" in hint for hint in result["hints"]))

    async def test_wrong_password_is_reported(self):
        async def onvif_streams(*args):
            raise probe.CameraError("camera_auth_failed", "攝影機帳號或密碼錯誤")

        m.onvif_streams = onvif_streams
        with self.assertRaises(m.FrigateError) as caught:
            await bridge(Frigate()).probe(
                {"host": "192.168.1.72", "username": "admin", "password": "x"}
            )
        self.assertEqual(caught.exception.code, "camera_auth_failed")

    async def test_a_fresh_vigi_is_reported_as_uninitialized(self):
        async def onvif_streams(*args):
            raise probe.CameraError("camera_auth_failed", "攝影機帳號或密碼錯誤")

        async def vigi_state(session, host, seconds=3):
            return "uninitialized"

        original = m.vigi_state
        m.onvif_streams, m.vigi_state = onvif_streams, vigi_state
        try:
            with self.assertRaises(m.FrigateError) as caught:
                await bridge(Frigate()).probe(
                    {"host": "192.168.68.66", "username": "admin", "password": "x"}
                )
        finally:
            m.vigi_state = original
        self.assertEqual(caught.exception.code, "camera_uninitialized")

    async def test_manual_url_credentials_are_used_and_dropped(self):
        seen: list[tuple[str, str, str]] = []

        async def rtsp_check(uri, username, password, **kwargs):
            seen.append((uri, username, password))
            return {"status": 200, "codecs": ["H265"]}

        m.rtsp_check = rtsp_check
        result = await bridge(Frigate()).probe(
            {
                "host": "192.168.1.10",
                "rtspUrl": "rtsp://admin:a%40b@192.168.1.10:554/user=admin_password=_channel=1_stream=0.sdp",
            }
        )
        self.assertEqual(
            seen,
            [
                (
                    "rtsp://192.168.1.10:554/user=admin_password=_channel=1_stream=0.sdp",
                    "admin",
                    "a@b",
                )
            ],
        )
        self.assertEqual(
            result["main"],
            "rtsp://192.168.1.10:554/user=admin_password=_channel=1_stream=0.sdp",
        )
        self.assertIsNone(result["sub"])


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_jobs_from_before_a_restart_are_interrupted(self):
        b = bridge(
            Frigate(),
            Store({"managed": {}, "jobs": [{"jobId": OPERATION, "state": "running"}]}),
        )
        await b.load()
        job = b.job(OPERATION)
        self.assertEqual(job["state"], "interrupted")
        self.assertEqual(job["error"]["code"], "frigate_interrupted")


if __name__ == "__main__":
    unittest.main()
