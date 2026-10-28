"""VIGI OpenAPI, the platform's plate list and the plate commands."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_vigi_openapi_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
for name in (
    "camera_probe",
    "vigi_setup",
    "camera_control",
    "vigi_openapi",
    "lpr_gate",
    "frigate_bridge",
):
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", PACKAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
openapi_module = sys.modules[f"{package.__name__}.vigi_openapi"]
gate_module = sys.modules[f"{package.__name__}.lpr_gate"]
bridge_module = sys.modules[f"{package.__name__}.frigate_bridge"]
CameraError = sys.modules[f"{package.__name__}.camera_probe"].CameraError

CAMERA = "insight_lpr345z"
HOST = "192.168.2.180"
PASSWORD = "Gate!Keeper#42"
RAW = f"""
go2rtc:
  streams:
    {CAMERA}:
      - rtsp://admin:Gate%21Keeper%2342@{HOST}:554/stream1#backchannel=0
cameras:
  {CAMERA}:
    friendly_name: InSight-LPR345Z
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/{CAMERA}
          roles: [record]
    onvif:
      host: {HOST}
      port: 80
      user: admin
      password: "{PASSWORD}"
"""


class Response:
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        if isinstance(self.payload, str):
            return self.payload.encode()
        return json.dumps(self.payload).encode()

    async def text(self) -> str:
        return (await self.read()).decode()


class Camera:
    """The hub's Frigate (5050) and an InSight LPR345Z's OpenAPI (20443)."""

    def __init__(self, *, openapi: bool = True, password: str = PASSWORD) -> None:
        self.openapi = openapi
        self.password = password
        self.nonce = "c51594999c7dcfd020e97a2688d431d0"
        self.tokens: set[str] = set()
        self.logins = 0
        self.calls: list[tuple[str, dict | None]] = []
        self.unsupported: set[str] = set()
        self.day_night = "auto"
        self.codes = {"main": "H265", "sub": "H264"}
        self.records = [
            {
                "license_plate_results_0": {
                    "device_name": "InSight%20LPR345Z%201.0_E4C4",
                    "snapshot_timestamp": 1755846951,
                    "license_plate_number": "WSP7072",
                    "event_type": 52,
                    "thumbnail_size": 10201,
                    "thumbnail_file_id": "0001000000001000",
                    "background_size": 34617,
                    "background_file_id": "0001000000001001",
                    "list_type": 2,
                }
            },
            {
                "license_plate_results_1": {
                    "snapshot_timestamp": 1755846999,
                    "license_plate_number": "ABC1234",
                    "list_type": 0,
                }
            },
        ]

    def request(self, method: str, url, **kwargs):
        parts = urlsplit(str(url))
        if parts.port == 5050:
            if parts.path == "/api/version":
                return Response(200, "0.17.1-416a9b7")
            if parts.path == "/api/config/raw":
                return Response(200, json.dumps(RAW))
            return Response(404, "")
        if parts.port != 20443 or parts.hostname != HOST or not self.openapi:
            raise OSError("connection refused")
        assert kwargs["ssl"] is False
        assert kwargs["headers"]["Connection"] == "close"
        body = json.loads(kwargs["data"])
        if body["method"] == "doAuth":
            return Response(200, self.auth(body.get("params")))
        stok = parts.path.removeprefix("/stok=")
        if stok not in self.tokens:
            return Response(200, {"method": body["method"], "errCode": -10002})
        self.calls.append((body["method"], body.get("params")))
        if body["method"] in self.unsupported:
            return Response(200, {"method": body["method"], "errCode": -10003})
        handler = getattr(self, f"on_{body['method']}")
        return Response(
            200,
            {
                "method": body["method"],
                "errCode": 0,
                "result": handler(body.get("params")),
            },
        )

    def auth(self, params):
        if params is None:
            return {
                "method": "doAuth",
                "authenticate": {
                    "realm": "TP-LINK IP-Camera",
                    "nonce": self.nonce,
                    "algorithm": "SHA-256",
                    "uri": "doAuth",
                    "method": "POST",
                },
                "errCode": -10020,
            }

        def h(text: str) -> str:
            return hashlib.sha256(text.encode()).hexdigest()

        expected = h(
            f"{h(f'admin:TP-LINK IP-Camera:{self.password}')}:{self.nonce}:"
            f"{h('POST:doAuth')}"
        )
        if params != {"nonce": self.nonce, "response": expected}:
            return {"method": "doAuth", "errCode": -10020}
        self.logins += 1
        stok = f"tok{self.logins}*X"
        self.tokens.add(stok)
        return {"method": "doAuth", "stok": stok, "errCode": 0}

    def on_getDeviceInfo(self, _params):
        return {
            "model": "InSight LPR345Z",
            "alias": "InSight%20LPR345Z%201.0_E4C4",
            "mac": "6C-4C-BC-AA-C9-5E",
            "hwVer": "1.0",
            "swVer": "1.0.2 Build 260601 Rel.1n",
        }

    def on_getDayNightMode(self, _params):
        return {"mode": self.day_night}

    def on_setDayNightMode(self, params):
        self.day_night = params["mode"]
        return {}

    def on_getVideoCode(self, _params):
        return {
            "main_encode_type": self.codes["main"],
            "minor_encode_type": self.codes["sub"],
        }

    def on_setVideoCode(self, params):
        if "main_encode_type" in params:
            self.codes["main"] = params["main_encode_type"]
        return {}

    def on_getSdCardStatus(self, _params):
        return {"status": "normal", "total_space": "119.1GB", "free_space": "118.4GB"}

    def on_openBarrierGate(self, _params):
        return {}

    def on_lprTriggerMode(self, _params):
        return {}

    def on_getUserId(self, _params):
        return {"user_id": 7}

    def on_searchLprInfo(self, params):
        return {
            "total": len(self.records),
            "page": params["page_num"],
            "license_plate_result": self.records,
            "error_code": 0,
        }

    def on_downloadLprImage(self, _params):
        return {
            "license_plate_image_result": {
                "thumbnail_image": "dGh1bWI=",
                "background_image": "c2NlbmU=",
            }
        }


class Store:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.saves += 1
        self.data = json.loads(json.dumps(data))


def run(coro):
    return asyncio.run(coro)


class DigestTests(unittest.TestCase):
    def test_matches_the_documented_formula(self) -> None:
        def h(text: str) -> str:
            return hashlib.sha256(text.encode()).hexdigest()

        a1, a2 = h("admin:TP-LINK IP-Camera:pw"), h("POST:doAuth")
        self.assertEqual(
            openapi_module.digest("pw", "TP-LINK IP-Camera", "n1", "POST", "doAuth"),
            h(f"{a1}:n1:{a2}"),
        )

    def test_error_code_may_be_nested(self) -> None:
        self.assertEqual(openapi_module.error_code({"errCode": -10013}), -10013)
        self.assertEqual(
            openapi_module.error_code({"result": {"error_code": 0, "total": 1}}), 0
        )
        self.assertEqual(openapi_module.error_code({"error_code": "-10003"}), -10003)


class OpenApiTests(unittest.TestCase):
    def client(self, camera: Camera, tokens=None):
        return openapi_module.VigiOpenApi(camera, HOST, PASSWORD, tokens=tokens)

    def test_logs_in_once_and_reuses_the_token(self) -> None:
        camera, tokens = Camera(), {}

        async def scenario():
            await self.client(camera, tokens).device()
            return await self.client(camera, tokens).day_night()

        self.assertEqual(run(scenario()), "auto")
        self.assertEqual(camera.logins, 1)

    def test_stale_token_logs_in_again(self) -> None:
        camera, tokens = Camera(), {}
        run(self.client(camera, tokens).device())
        camera.tokens.clear()  # the camera restarted
        device = run(self.client(camera, tokens).device())
        self.assertEqual(device["model"], "InSight LPR345Z")
        self.assertEqual(device["alias"], "InSight LPR345Z 1.0_E4C4")
        self.assertEqual(camera.logins, 2)

    def test_wrong_password(self) -> None:
        with self.assertRaises(CameraError) as caught:
            run(self.client(Camera(password="Other#Pass1")).device())
        self.assertEqual(caught.exception.code, "camera_auth_failed")

    def test_refused_port_means_openapi_is_off(self) -> None:
        with self.assertRaises(CameraError) as caught:
            run(self.client(Camera(openapi=False)).device())
        self.assertEqual(caught.exception.code, "camera_openapi_off")
        self.assertIn("OpenAPI", caught.exception.message)

    def test_unsupported_method(self) -> None:
        camera = Camera()
        camera.unsupported.add("getVideoCode")
        with self.assertRaises(CameraError) as caught:
            run(self.client(camera).video_code())
        self.assertEqual(caught.exception.code, "camera_not_supported")

    def test_plate_records_and_pictures(self) -> None:
        camera = Camera()
        found = run(self.client(camera).plate_records(1, 2, 1, 20, "WSP7072"))
        self.assertEqual(found["total"], 2)
        first, second = found["records"]
        self.assertEqual(first["plate"], "WSP7072")
        self.assertEqual(first["cameraList"], "allow")
        self.assertEqual(first["thumbId"], "0001000000001000")
        self.assertEqual(first["device"], "InSight LPR345Z 1.0_E4C4")
        self.assertIsNone(second["cameraList"])
        search = dict(camera.calls)["searchLprInfo"]
        self.assertEqual(search["user_id"], 7)
        self.assertEqual(search["license_plate_number"], "WSP7072")
        images = run(
            self.client(camera).plate_images(1755846951, "0001000000001000", None)
        )
        self.assertEqual(images, {"thumbnail": "dGh1bWI=", "background": "c2NlbmU="})

    def test_settings_are_checked_before_sending(self) -> None:
        camera = Camera()
        with self.assertRaises(CameraError):
            run(self.client(camera).set_day_night("dusk"))
        with self.assertRaises(CameraError):
            run(self.client(camera).set_video_code("MJPEG", None))
        run(self.client(camera).set_video_code("H265+", None))
        self.assertEqual(camera.codes["main"], "H265+")
        run(self.client(camera).set_lpr_trigger(True, False))
        self.assertEqual(
            dict(camera.calls)["lprTriggerMode"],
            {"video_trigger": "on", "alarm_trigger": "off"},
        )


class PlateListTests(unittest.TestCase):
    def test_entries_are_cleaned(self) -> None:
        entries = gate_module.clean_entries(
            [
                {"plate": "abc-1234", "kind": "allow", "name": "  王 小明 "},
                {"plate": "XY 99", "kind": "block", "until": "2026-12-31"},
            ]
        )
        self.assertEqual(entries[0]["plate"], "ABC1234")
        self.assertEqual(entries[0]["name"], "王 小明")
        self.assertTrue(entries[0]["enabled"])
        self.assertEqual(entries[1]["until"], "2026-12-31")
        for bad in (
            [
                {"plate": "ABC1234", "kind": "allow"},
                {"plate": "abc 1234", "kind": "block"},
            ],
            [{"plate": "ABC1234", "kind": "maybe"}],
            [{"plate": "A", "kind": "allow"}],
            [{"plate": "ABC1234", "kind": "allow", "until": "2026/12/31"}],
            [
                {
                    "plate": "ABC1234",
                    "kind": "allow",
                    "from": "2026-12-31",
                    "until": "2026-01-01",
                }
            ],
            "nope",
        ):
            with self.assertRaises(CameraError):
                gate_module.clean_entries(bad)

    def test_validity_and_disabled_entries(self) -> None:
        entries = gate_module.clean_entries(
            [
                {"plate": "AAA1111", "kind": "allow", "from": "2026-11-01"},
                {"plate": "BBB2222", "kind": "allow", "until": "2026-10-27"},
                {"plate": "CCC3333", "kind": "allow", "enabled": False},
                {"plate": "DDD4444", "kind": "allow"},
            ]
        )
        today = date(2026, 10, 28)
        for plate in ("AAA1111", "BBB2222", "CCC3333"):
            self.assertIsNone(gate_module.find_entry(entries, plate, today))
        self.assertEqual(
            gate_module.find_entry(entries, "ddd-4444", today)["plate"], "DDD4444"
        )


class GateTests(unittest.TestCase):
    def gate(self, opened: list, *, fail: bool = False, clock=None):
        async def open_gate(camera):
            if fail:
                raise CameraError("camera_openapi_off", "off")
            opened.append(camera)

        events: list[dict] = []
        gate = gate_module.PlateGate(
            Store(),
            open_gate=open_gate,
            fire=events.append,
            today=lambda: date(2026, 10, 28),
            clock=clock or (lambda: 100.0),
        )
        return gate, events

    def test_decisions(self) -> None:
        opened: list = []
        gate, events = self.gate(opened)
        times = iter(range(0, 1000, 20))
        gate.clock = lambda: float(next(times))

        async def scenario():
            await gate.replace(
                [
                    {"plate": "ABC-1234", "kind": "allow", "name": "住戶 A"},
                    {"plate": "BAD0001", "kind": "block"},
                ]
            )
            off = await gate.report(CAMERA, "大門", {"plate": "ABC1234"})
            await gate.set_camera(CAMERA, autoGate=True)
            on = await gate.report(CAMERA, "大門", {"plate": "ABC1234"})
            blocked = await gate.report(CAMERA, "大門", {"plate": "BAD0001"})
            unknown = await gate.report(CAMERA, "大門", {"plate": "ZZZ9999"})
            elsewhere = await gate.report(None, "192.168.2.99", {"plate": "ABC1234"})
            return off, on, blocked, unknown, elsewhere

        off, on, blocked, unknown, elsewhere = run(scenario())
        self.assertEqual(off["action"], "auto_off")
        self.assertEqual(
            (on["action"], on["match"], on["name"]), ("opened", "allow", "住戶 A")
        )
        self.assertEqual(blocked["action"], "blocked")
        self.assertEqual((unknown["action"], unknown["match"]), ("none", None))
        # Only a known camera's gate is ever opened.
        self.assertEqual(elsewhere["action"], "auto_off")
        self.assertEqual(opened, [CAMERA])
        self.assertEqual(len(events), 5)
        log = run(gate.snapshot())["log"]
        self.assertEqual(log[0]["plate"], "ABC1234")  # newest first
        self.assertEqual(len(log), 5)

    def test_a_waiting_car_opens_the_gate_once(self) -> None:
        opened: list = []
        now = [0.0]
        gate, events = self.gate(opened, clock=lambda: now[0])

        async def scenario():
            await gate.replace([{"plate": "ABC1234", "kind": "allow"}])
            await gate.set_camera(CAMERA, autoGate=True)
            for second in (0, 2, 5, 12):
                now[0] = float(second)
                await gate.report(CAMERA, "大門", {"plate": "ABC1234"})

        run(scenario())
        self.assertEqual(opened, [CAMERA, CAMERA])
        self.assertEqual(len(events), 2)

    def test_gate_failure_is_logged(self) -> None:
        gate, _events = self.gate([], fail=True)

        async def scenario():
            await gate.replace([{"plate": "ABC1234", "kind": "allow"}])
            await gate.set_camera(CAMERA, autoGate=True)
            return await gate.report(CAMERA, "大門", {"plate": "ABC1234"})

        decision = run(scenario())
        self.assertEqual(
            (decision["action"], decision["message"]), ("gate_failed", "off")
        )

    def test_test_packet_only_records_contact(self) -> None:
        gate, events = self.gate([])
        self.assertIsNone(run(gate.report(CAMERA, "大門", {"plate": ""})))
        self.assertIsNotNone(run(gate.last_contact(CAMERA)))
        self.assertEqual(events, [])
        self.assertEqual(len(run(gate.webhook_id())), 32)


class BridgeTests(unittest.TestCase):
    def bridge(self, camera: Camera, events=None):
        async def interfaces():
            return ["10.0.0.5", "192.168.2.10"]

        return bridge_module.FrigateBridge(
            store=Store(),
            session=camera,
            create_task=lambda coro, name: asyncio.ensure_future(coro),
            interfaces=interfaces,
            candidates=("http://127.0.0.1:5050",),
            plate_store=Store(),
            fire_event=(events.append if events is not None else None),
            http_port=lambda: 8123,
        )

    def test_info_of_a_license_plate_camera(self) -> None:
        camera = Camera()
        camera.unsupported.add("getVideoCode")
        info = run(self.bridge(camera).vigi_info({"camera": CAMERA}))
        self.assertEqual(info["openapi"]["state"], "ok")
        self.assertTrue(info["vigi"])
        self.assertTrue(info["lpr"])
        self.assertEqual(info["dayNight"], "auto")
        self.assertIsNone(info["video"])
        self.assertEqual(info["sdCard"]["status"], "normal")
        server = info["plates"]["alarmServer"]
        self.assertEqual((server["host"], server["port"]), ("192.168.2.10", 8123))
        self.assertRegex(server["path"], r"^/api/webhook/[0-9a-f]{32}$")
        self.assertFalse(info["plates"]["autoGate"])
        self.assertNotIn(PASSWORD, json.dumps(info))

    def test_info_when_openapi_is_off(self) -> None:
        info = run(self.bridge(Camera(openapi=False)).vigi_info({"camera": CAMERA}))
        self.assertEqual(info["openapi"]["state"], "off")
        self.assertIn("OpenAPI", info["openapi"]["message"])
        self.assertIsNone(info["dayNight"])

    def test_controls(self) -> None:
        camera = Camera()
        bridge = self.bridge(camera)

        async def scenario():
            gate = await bridge.vigi_control({"camera": CAMERA, "op": "gate"})
            mode = await bridge.vigi_control(
                {"camera": CAMERA, "op": "day_night", "mode": "night"}
            )
            code = await bridge.vigi_control(
                {"camera": CAMERA, "op": "video_code", "main": "H264"}
            )
            trigger = await bridge.vigi_control(
                {"camera": CAMERA, "op": "lpr_trigger", "video": True, "alarm": True}
            )
            auto = await bridge.vigi_control(
                {"camera": CAMERA, "op": "auto_gate", "enabled": True}
            )
            return gate, mode, code, trigger, auto, await bridge.lpr_list({})

        gate, mode, code, trigger, auto, plates = run(scenario())
        self.assertEqual(gate["op"], "gate")
        self.assertEqual(mode["dayNight"], "night")
        self.assertEqual(code["video"], {"main": "H264", "sub": "H264"})
        self.assertEqual(trigger["plates"]["trigger"], {"video": True, "alarm": True})
        self.assertTrue(auto["plates"]["autoGate"])
        self.assertEqual(plates["log"][0]["action"], "manual")
        self.assertEqual(plates["log"][0]["label"], "InSight-LPR345Z")
        with self.assertRaises(bridge_module.FrigateError):
            run(
                bridge.vigi_control(
                    {"camera": CAMERA, "op": "lpr_trigger", "video": False}
                )
            )

    def test_alarm_server_report_opens_the_gate(self) -> None:
        camera, events = Camera(), []
        bridge = self.bridge(camera, events)

        async def scenario():
            await bridge.lpr_list_save(
                {"entries": [{"plate": "WSP7072", "kind": "allow"}]}
            )
            await bridge.vigi_control(
                {"camera": CAMERA, "op": "auto_gate", "enabled": True}
            )
            return await bridge.plate_report(
                HOST, {"camid": "InSight LPR345Z 1.0_E4C4", "plate": "WSP7072"}
            )

        decision = run(scenario())
        self.assertEqual(decision["camera"], CAMERA)
        self.assertEqual(decision["action"], "opened")
        self.assertIn(("openBarrierGate", None), camera.calls)
        self.assertEqual(events[0]["plate"], "WSP7072")
        self.assertIsNone(run(bridge.plate_report(HOST, ["not", "a", "packet"])))

    def test_records_and_pictures(self) -> None:
        bridge = self.bridge(Camera())
        found = run(
            bridge.lpr_records(
                {"camera": CAMERA, "start": 100, "end": 200, "plate": "wsp-7072"}
            )
        )
        self.assertEqual((found["start"], found["end"], found["total"]), (100, 200, 2))
        with self.assertRaises(bridge_module.FrigateError):
            run(bridge.lpr_records({"camera": CAMERA, "start": 0, "end": 40 * 86400}))
        image = run(
            bridge.lpr_image(
                {
                    "camera": CAMERA,
                    "time": 1755846951,
                    "thumbId": "0001000000001000",
                    "backgroundId": "0001000000001001",
                    "which": "background",
                }
            )
        )
        self.assertIsNone(image["thumbnail"])
        self.assertEqual(image["background"], "c2NlbmU=")

    def test_nearest_address(self) -> None:
        self.assertEqual(
            bridge_module.nearest_address(
                ["10.0.0.5", "192.168.2.10"], "192.168.2.180"
            ),
            "192.168.2.10",
        )
        self.assertEqual(
            bridge_module.nearest_address(
                ["10.0.0.5", "192.168.9.10"], "192.168.2.180"
            ),
            "192.168.9.10",
        )
        self.assertIsNone(bridge_module.nearest_address([], "192.168.2.180"))


if __name__ == "__main__":
    unittest.main()
