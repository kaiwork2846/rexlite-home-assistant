"""One-tap VIGI setup through the camera's own web page API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import unittest
from datetime import datetime
from urllib.parse import unquote, urlsplit

from test_vigi_openapi import (
    CAMERA,
    HOST,
    PASSWORD,
    Camera,
    Response,
    Store,
    bridge_module,
    package,
    run,
)
from test_vigi_setup import SPKI, decrypt

web_module = sys.modules[f"{package.__name__}.vigi_web"]
CameraError = sys.modules[f"{package.__name__}.camera_probe"].CameraError
ZONE = web_module.zone_of("Asia/Taipei")
NONCE = "CgDgvECK"
STOK = "uc5!PvP0aA!Cw*v(W"
STUCK = "已開啟但連不上（20443 埠拒絕連線），紀錄與開閘改走攝影機網頁"


def stamp(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


class WebCamera(Camera):
    """Frigate and OpenAPI from ``Camera``, plus the camera's web page API on 443
    (requests and replies as in TP-Link's InSight LPR345Z web emulator)."""

    def __init__(
        self, *, model: str = "InSight%20LPR345Z", openapi_starts: bool = True, **kwargs
    ) -> None:
        super().__init__(openapi=False, **kwargs)
        self.model = model
        # The switch on the camera's page; OpenAPI itself may still not answer.
        self.openapi_switch = False
        self.openapi_starts = openapi_starts
        self.web_gate = 0
        self.alarm_in = "off"
        self.web_logins = 0
        self.stale_once = False
        self.ds: list[dict] = []
        self.lpr_para = {
            "same_tri_interval": "300",
            "reg_no_plate": "on",
            "reg_video": "off",
            "lpr_country": "Unknown",
        }
        self.lpss_rule = {
            "enabled": "off",
            "target_filter_enabled": "off",
            "min_target_width": 2000,
        }
        self.servers: dict[str, dict] = {}
        self.linkage = {
            "device_linkage_info_1": {
                "lpr_trigger_type": 0,
                "lp_verify_type": 1,
                "alarm_out_enabled": "off",
                "audio_out_enabled": "off",
            },
            "device_linkage_info_2": {
                "lpr_trigger_type": 0,
                "lp_verify_type": 2,
                "alarm_out_enabled": "on",
                "audio_out_enabled": "off",
            },
            "device_linkage_info_3": {
                "lpr_trigger_type": 0,
                "lp_verify_type": 3,
                "alarm_out_enabled": "off",
                "audio_out_enabled": "off",
            },
            "device_linkage_info_5": {
                "lpr_trigger_type": 1,
                "lp_verify_type": 0,
                "alarm_out_enabled": "on",
                "audio_out_enabled": "off",
            },
        }
        # The installer's own entries: one with a dash, one the platform lacks.
        self.plates = {
            "ABC-1234": {
                "enabled": "on",
                "start_time": 946656000,
                "end_time": 2145887999,
                "list_type": 2,
                "notes": "",
            },
            "OLD9999": {
                "enabled": "off",
                "start_time": stamp("2026-01-01T00:00:00+08:00"),
                "end_time": stamp("2026-12-31T23:59:59+08:00"),
                "list_type": 1,
                "notes": "%E5%B0%81%E9%8E%96",
            },
        }

    def request(self, method: str, url, **kwargs):
        parts = urlsplit(str(url))
        if parts.scheme != "https" or parts.port is not None:
            return super().request(method, url, **kwargs)
        if parts.hostname != HOST:
            raise OSError("connection refused")
        assert kwargs["ssl"] is False
        body = json.loads(kwargs["data"])
        if parts.path == "/":
            return Response(200, self.anonymous(body))
        assert parts.path == f"/stok={STOK}/ds", parts.path
        if self.stale_once:
            self.stale_once = False
            return Response(200, {"error_code": -40401})
        self.ds.append(body)
        return Response(200, self.handle(body))

    def anonymous(self, body: dict) -> dict:
        if "user_management" in body:
            return {
                "data": {
                    "code": -40407,
                    "encrypt_type": ["1", "2"],
                    "key": SPKI,
                    "key_2": SPKI,
                    "nonce": NONCE,
                    "passwdType": "md5",
                },
                "error_code": -40401,
            }
        login = body["login"]
        assert (
            login["encrypt_type"],
            login["passwdType"],
            login["username"],
            login["keyType"],
        ) == ("2", "md5", "admin", "1")
        secret = hashlib.md5(f"TPCQ75NF2Y:{self.password}".encode()).hexdigest().upper()
        if decrypt(login["password"]) != f"{secret}:{NONCE}":
            return {
                "data": {"code": -40401, "time": 1, "max_time": 5},
                "error_code": -40401,
            }
        self.web_logins += 1
        return {"user_group": "root", "stok": STOK, "error_code": 0}

    def handle(self, body: dict) -> dict:
        method = body["method"]
        if "device_info" in body:
            return {
                "device_info": {
                    "basic_info": {
                        "device_model": self.model,
                        "sw_version": "1.0.1%20Build%20251118",
                        "mac": "6C-4C-BC-AA-C9-5E",
                    }
                },
                "error_code": 0,
            }
        if "alarm_output" in body:
            assert body["alarm_output"] == {"lpr_manual_alarm": {"output_dev": 1}}
            self.web_gate += 1
            return {"error_code": 0}
        if "system" in body:
            assert body == {"method": "do", "system": {"get_user_id": None}}
            return {"user_id": 12, "error_code": 0}
        if "alarm_input" in body:
            self.alarm_in = body["alarm_input"]["notify_list_AI1"]["lpss_enabled"]
            return {"error_code": 0}
        if "openapi" in body:
            if method == "set":
                assert body["openapi"] == {
                    "server": {"enabled": "on", "openapi_port": "20443"}
                }
                self.openapi_switch = True
                self.openapi = self.openapi_starts
            return {
                "openapi": {
                    "server": {
                        "enabled": "on" if self.openapi_switch else "off",
                        "openapi_port": "20443",
                    }
                },
                "error_code": 0,
            }
        if "license_plate_recognition" in body:
            section = body["license_plate_recognition"]
            if "search_license_plate" in section:
                assert section["search_license_plate"]["user_id"] == 12
                row = {
                    "snapshot_timestamp": 150,
                    "license_plate_number": "RFS1515",
                    "list_type": 2,
                    "thumbnail_file_id": "0001000150028500",
                }
                return {
                    "total": 1,
                    "page": 1,
                    "license_plate_result": [{"license_plate_results_0": row}],
                    "error_code": 0,
                }
            if method == "set":
                self.lpr_para.update(section.get("lpr_para") or {})
                self.lpss_rule.update(section.get("lpss_rule") or {})
            return {
                "license_plate_recognition": {
                    "lpr_para": self.lpr_para,
                    "lpss_rule": self.lpss_rule,
                },
                "error_code": 0,
            }
        if "report_event" in body:
            section = body["report_event"]
            if method == "add":
                assert section["table"] == "report_server_list"
                self.servers[section["name"]] = section["para"]
            elif method == "set":
                self.servers.update(section)
            rows = [
                {key: {**row, "url": row["url"].replace("/", "%2f")}}
                for key, row in self.servers.items()
            ]
            return {"report_event": {"report_server_list": rows}, "error_code": 0}
        if "lpr" in body and method == "get":
            return {
                "lpr": {
                    "device_linkage_info": [
                        {key: row} for key, row in self.linkage.items()
                    ]
                },
                "error_code": 0,
            }
        if "lpr" in body and method == "set":
            assert set(body["lpr"]) == set(self.linkage)
            self.linkage.update(body["lpr"])
            return {"error_code": 0}
        return self.list_request(body["lpr"])

    def list_request(self, section: dict) -> dict:
        if "get_whitelist" in section:
            query = section["get_whitelist"]
            assert query["whitelist_filter"] == [1, 2]
            size, page = int(query["item_per_page"]), int(query["page_num"])
            rows = [
                {f"whitelist_{n}": {**row, "license_plate": plate}}
                for n, (plate, row) in enumerate(self.plates.items())
            ]
            return {
                "whitelist": rows[(page - 1) * size : page * size],
                "page": page,
                "total": len(rows),
                "error_code": 0,
            }
        if "del_whitelist" in section:
            for plate in section["del_whitelist"]["license_plate"]:
                del self.plates[plate]
            return {"error_code": 0}
        name = "add_whitelist" if "add_whitelist" in section else "mod_whitelist"
        row = dict(section[name])
        plate = unquote(row.pop("license_plate"))
        if name == "mod_whitelist":
            del self.plates[unquote(row.pop("old_license_plate"))]
        elif plate in self.plates:
            return {"error_code": -71112}
        self.plates[plate] = {
            **row,
            "start_time": int(row["start_time"]),
            "end_time": int(row["end_time"]),
        }
        return {"error_code": 0}


def bridge(camera: WebCamera, events=None):
    async def interfaces():
        return ["10.0.0.5", "192.168.2.10"]

    async def no_wait(_seconds):
        return None

    return bridge_module.FrigateBridge(
        store=Store(),
        session=camera,
        create_task=lambda coro, name: asyncio.ensure_future(coro),
        interfaces=interfaces,
        candidates=("http://127.0.0.1:5050",),
        plate_store=Store(),
        fire_event=(events.append if events is not None else None),
        http_port=lambda: 8123,
        time_zone=lambda: "Asia/Taipei",
        sleep=no_wait,
    )


class WebApiTests(unittest.TestCase):
    def test_login_sends_the_salted_password_and_renews_a_stale_session(self) -> None:
        camera = WebCamera()
        web = web_module.VigiWeb(camera, HOST, PASSWORD)

        async def scenario():
            first = await web.device()
            camera.stale_once = True
            return first, await web.openapi()

        device, state = run(scenario())
        self.assertEqual(device["model"], "InSight LPR345Z")
        self.assertEqual(state, {"enabled": False, "port": 20443})
        self.assertEqual(camera.web_logins, 2)

    def test_wrong_password_names_the_chances_left(self) -> None:
        web = web_module.VigiWeb(WebCamera(), HOST, "Wrong!Pass#99")
        with self.assertRaises(CameraError) as caught:
            run(web.login())
        self.assertEqual(caught.exception.code, "camera_auth_failed")
        self.assertIn("剩 1 次", caught.exception.message)

    def test_gate_rule(self) -> None:
        camera = WebCamera()
        web = web_module.VigiWeb(camera, HOST, PASSWORD)
        run(web.set_gate(True))
        self.assertEqual(
            {key: row["alarm_out_enabled"] for key, row in camera.linkage.items()},
            {
                "device_linkage_info_1": "on",
                "device_linkage_info_2": "off",
                "device_linkage_info_3": "off",
                "device_linkage_info_5": "off",
            },
        )
        camera.linkage["device_linkage_info_2"]["alarm_out_enabled"] = "on"
        run(web.set_gate(False))
        # Off only touches the allowed row.
        self.assertEqual(
            camera.linkage["device_linkage_info_1"]["alarm_out_enabled"], "off"
        )
        self.assertEqual(
            camera.linkage["device_linkage_info_2"]["alarm_out_enabled"], "on"
        )

    def test_alarm_server_is_reused_updated_or_added(self) -> None:
        camera = WebCamera()
        web = web_module.VigiWeb(camera, HOST, PASSWORD)
        self.assertEqual(
            run(web.point_alarm_server("192.168.2.10", 8123, "/api/webhook/a")), "added"
        )
        self.assertEqual(
            run(web.point_alarm_server("192.168.2.10", 8123, "/api/webhook/a")), "kept"
        )
        self.assertEqual(
            run(web.point_alarm_server("192.168.2.10", 8123, "/api/webhook/b")),
            "updated",
        )
        self.assertEqual(
            camera.servers["report_server_list_1"]["url"], "/api/webhook/b"
        )
        self.assertEqual(
            camera.servers["report_server_list_1"]["push_picture_enabled"], "off"
        )
        camera.servers = {
            f"report_server_list_{n}": {
                "ip_or_domain": f"10.0.0.{n}",
                "port": "80",
                "url": "/x",
                "protocol": "HTTP",
            }
            for n in (1, 2, 3)
        }
        with self.assertRaises(CameraError) as caught:
            run(web.point_alarm_server("192.168.2.10", 8123, "/api/webhook/a"))
        self.assertEqual(caught.exception.code, "camera_alarm_server_full")


class PlanTests(unittest.TestCase):
    def test_platform_entries_as_camera_rows(self) -> None:
        row = web_module.wanted_row(
            {
                "plate": "ABC1234",
                "kind": "allow",
                "name": "住戶 A 的第二台車輛很長的名字",
                "from": "2026-11-01",
                "until": None,
                "enabled": True,
            },
            ZONE,
        )
        self.assertEqual(row["start"], stamp("2026-11-01T00:00:00+08:00"))
        self.assertEqual(row["end"], stamp("2037-12-31T23:59:59+08:00"))
        self.assertEqual(len(row["notes"]), 16)

    def test_sync_plan(self) -> None:
        entries = [
            {
                "plate": "ABC1234",
                "kind": "allow",
                "name": None,
                "from": None,
                "until": None,
                "enabled": True,
            },
            {
                "plate": "NEW0001",
                "kind": "block",
                "name": None,
                "from": None,
                "until": None,
                "enabled": True,
            },
        ]
        rows = [
            {
                "raw": "ABC-1234",
                "plate": "ABC1234",
                "kind": "allow",
                "enabled": True,
                "start": web_module.day_start(None, ZONE),
                "end": web_module.day_end(None, ZONE),
                "notes": "",
            },
            {
                "raw": "GONE111",
                "plate": "GONE111",
                "kind": "allow",
                "enabled": True,
                "start": 0,
                "end": 0,
                "notes": "",
            },
        ]
        adds, updates, removals = web_module.plan_sync(entries, rows, ZONE)
        self.assertEqual([row["plate"] for row in adds], ["NEW0001"])
        # Same rule, but the camera's copy is renamed to the platform's spelling.
        self.assertEqual([old for old, _row in updates], ["ABC-1234"])
        self.assertEqual(removals, ["GONE111"])

    def test_merge_keeps_the_installers_entries(self) -> None:
        rows = [
            {
                "raw": "OLD9999",
                "plate": "OLD9999",
                "kind": "block",
                "enabled": False,
                "start": stamp("2026-01-01T00:00:00+08:00"),
                "end": stamp("2026-12-31T23:59:59+08:00"),
                "notes": "封鎖",
            },
            {
                "raw": "ABC-1234",
                "plate": "ABC1234",
                "kind": "allow",
                "enabled": True,
                "start": 946656000,
                "end": 2145887999,
                "notes": "",
            },
        ]
        merged = web_module.merge_rows([], rows, ZONE)
        self.assertEqual(
            merged[0],
            {
                "plate": "OLD9999",
                "kind": "block",
                "name": "封鎖",
                "from": "2026-01-01",
                "until": "2026-12-31",
                "enabled": False,
            },
        )
        self.assertEqual((merged[1]["from"], merged[1]["until"]), (None, None))


class SetupTests(unittest.TestCase):
    def test_one_tap_setup_of_a_plate_camera(self) -> None:
        camera = WebCamera()
        hub = bridge(camera)

        async def scenario():
            await hub.lpr_list_save(
                {"entries": [{"plate": "WSP7072", "kind": "allow", "name": "住戶 A"}]}
            )
            result = await hub.vigi_setup({"camera": CAMERA})
            await hub.plate_syncs[CAMERA]
            return (
                result,
                await hub.lpr_list({}),
                await hub.vigi_info({"camera": CAMERA}),
            )

        result, plates, info = run(scenario())
        self.assertEqual(
            [(step["id"], step["state"]) for step in result["steps"]],
            [
                ("login", "ok"),
                ("openapi", "ok"),
                ("recognition", "ok"),
                ("alarm", "ok"),
                ("gate", "ok"),
                ("list", "running"),
            ],
        )
        self.assertTrue(result["lpr"])
        self.assertEqual(result["merged"], 2)
        self.assertEqual(info["openapi"]["state"], "ok")
        self.assertEqual(
            (camera.lpr_para["reg_video"], camera.lpss_rule["enabled"]), ("on", "on")
        )
        server = camera.servers["report_server_list_1"]
        self.assertEqual(
            (server["ip_or_domain"], server["port"]), ("192.168.2.10", "8123")
        )
        self.assertEqual(server["url"], info["plates"]["alarmServer"]["path"])
        self.assertEqual(
            camera.linkage["device_linkage_info_1"]["alarm_out_enabled"], "on"
        )
        self.assertEqual(
            camera.linkage["device_linkage_info_2"]["alarm_out_enabled"], "off"
        )
        # The installer's entries joined the platform list; the camera now
        # holds the platform list under its spelling.
        self.assertEqual(
            {entry["plate"] for entry in plates["entries"]},
            {"WSP7072", "ABC1234", "OLD9999"},
        )
        self.assertEqual(set(camera.plates), {"WSP7072", "ABC1234", "OLD9999"})
        self.assertEqual(unquote(camera.plates["WSP7072"]["notes"]), "住戶 A")
        settings = info["plates"]
        self.assertTrue(
            settings["autoGate"] and settings["cameraGate"] and settings["cameraList"]
        )
        self.assertEqual(settings["listSync"]["state"], "ok")
        self.assertEqual(settings["listSync"]["added"], 1)
        self.assertNotIn(PASSWORD, json.dumps([result, plates, info]))

    def test_saved_list_reaches_the_camera_and_it_opens_the_gate(self) -> None:
        camera, events = WebCamera(), []
        hub = bridge(camera, events)

        async def scenario():
            await hub.vigi_setup({"camera": CAMERA})
            first = hub.plate_syncs.get(CAMERA)
            if first is not None:
                await first
            saved = await hub.lpr_list_save(
                {"entries": [{"plate": "WSP7072", "kind": "allow"}]}
            )
            await hub.plate_syncs[CAMERA]
            decision = await hub.plate_report(HOST, {"plate": "WSP7072"})
            off = await hub.vigi_control(
                {"camera": CAMERA, "op": "auto_gate", "enabled": False}
            )
            return saved, decision, off

        saved, decision, off = run(scenario())
        self.assertEqual(saved["syncing"], [CAMERA])
        self.assertEqual(set(camera.plates), {"WSP7072"})
        self.assertEqual(decision["action"], "camera")
        self.assertNotIn(("openBarrierGate", None), camera.calls)
        self.assertFalse(off["plates"]["autoGate"])
        self.assertEqual(
            camera.linkage["device_linkage_info_1"]["alarm_out_enabled"], "off"
        )

    def test_other_vigi_cameras_only_get_openapi(self) -> None:
        camera = WebCamera(model="VIGI%20C540V")
        result = run(bridge(camera).vigi_setup({"camera": CAMERA}))
        self.assertEqual([step["id"] for step in result["steps"]], ["login", "openapi"])
        self.assertFalse(result["lpr"])
        self.assertTrue(camera.openapi)

    def test_a_failed_login_stops_before_any_change(self) -> None:
        camera = WebCamera(password="Another!Pass#77")
        result = run(bridge(camera).vigi_setup({"camera": CAMERA}))
        self.assertEqual(
            [(step["id"], step["state"]) for step in result["steps"]],
            [("login", "failed")],
        )
        self.assertIn("密碼", result["steps"][0]["message"])
        self.assertEqual(camera.ds, [])


class FallbackTests(unittest.TestCase):
    """OpenAPI switched on but not answering: the web page API stands in."""

    def test_gate_trigger_and_records_without_openapi(self) -> None:
        camera = WebCamera(openapi_starts=False)
        hub = bridge(camera)

        async def scenario():
            setup = await hub.vigi_setup({"camera": CAMERA})
            pending = hub.plate_syncs.get(CAMERA)
            if pending is not None:
                await pending
            info = await hub.vigi_info({"camera": CAMERA})
            await hub.vigi_control({"camera": CAMERA, "op": "gate"})
            await hub.vigi_control(
                {"camera": CAMERA, "op": "lpr_trigger", "video": True, "alarm": True}
            )
            found = await hub.lpr_records({"camera": CAMERA, "start": 100, "end": 200})
            return setup, info, found

        setup, info, found = run(scenario())
        step = next(item for item in setup["steps"] if item["id"] == "openapi")
        self.assertEqual(step["state"], "failed")
        self.assertEqual(step["message"], STUCK)
        self.assertEqual(
            [item["state"] for item in setup["steps"] if item["id"] != "openapi"],
            ["ok", "ok", "ok", "ok", "running"],
        )
        self.assertEqual(info["openapi"]["state"], "off")
        self.assertTrue(info["web"])
        self.assertEqual(camera.web_gate, 1)
        self.assertEqual((camera.lpr_para["reg_video"], camera.alarm_in), ("on", "on"))
        self.assertEqual((found["total"], found["records"][0]["plate"]), (1, "RFS1515"))
        with self.assertRaises(CameraError) as caught:
            run(hub.lpr_image({"camera": CAMERA, "time": 150, "thumbId": "x"}))
        self.assertIn("需要攝影機 OpenAPI", caught.exception.message)

    def test_a_camera_never_set_up_still_needs_openapi(self) -> None:
        camera = WebCamera()
        with self.assertRaises(CameraError) as caught:
            run(bridge(camera).vigi_control({"camera": CAMERA, "op": "gate"}))
        self.assertEqual(caught.exception.code, "camera_openapi_off")
        self.assertEqual(camera.web_gate, 0)

    def test_a_switch_the_camera_ignores_is_named(self) -> None:
        camera = WebCamera(openapi_starts=False)
        original = camera.handle

        def ignore_switch(body):
            reply = original(body)
            if "openapi" in body:
                camera.openapi_switch = False
            return reply

        camera.handle = ignore_switch
        result = run(bridge(camera).vigi_setup({"camera": CAMERA}))
        step = next(item for item in result["steps"] if item["id"] == "openapi")
        self.assertEqual(step["message"], "攝影機沒有套用 OpenAPI 開關")


if __name__ == "__main__":
    unittest.main()
