"""VIGI activation against a scripted factory-fresh camera."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_vigi_setup_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
for name in ("camera_probe", "vigi_setup"):
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", PACKAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
m = sys.modules[f"{package.__name__}.vigi_setup"]

HOST = "192.168.68.66"
PASSWORD = "Rex-Cam7Kp9x"
# A throwaway 1024-bit test key, so the fake camera can read what it gets.
N = int(
    "c6577fc5986bdc9609ef0015c8166b4ee7909f3ddc63eed6de87fe7b459a8d6c"
    "75ecadcb13f92fce7cde1f2f41a731fc9db3ad04f1d90ae3b20ae8ceca5d2176"
    "ec3beb99727eb37ede3da7cdf1eb1e76156c3b8527c5721852351d46eb7ba11c"
    "e1df3cd3119a7389d263b1e1534975e9a91e9ca1636b8e43c632ba0fa62a7db5",
    16,
)
D = int(
    "a40e3be174f1c2c357f4185debe29f5fe4a9fb0d6ffe38d001c0fafd7bd51aa5"
    "5be528db90e9cc613f677ae3dbaa2fee8382dad9fc314a908bf3fa1d11c576a7"
    "30aa5e461284c4044a62a9bdf59f7d3ceb97f1d44d072499fbf56b133ff8ca3a"
    "92102a90040b58a1aa0aa00690aa14dd29aafb19ae0955f98cdbe6516e654801",
    16,
)
SOAP = (
    '<?xml version="1.0"?><s:Envelope '
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>{}</s:Body>'
    "</s:Envelope>"
)


def _der(tag: int, body: bytes) -> bytes:
    if len(body) < 128:
        return bytes([tag, len(body)]) + body
    size = (len(body).bit_length() + 7) // 8
    return bytes([tag, 0x80 | size]) + len(body).to_bytes(size, "big") + body


def _integer(value: int) -> bytes:
    data = value.to_bytes((value.bit_length() + 8) // 8, "big")
    return _der(0x02, data)


# The test key as OpenAPI hands it out: base64 SubjectPublicKeyInfo, URL-encoded.
SPKI = quote(
    base64.b64encode(
        _der(
            0x30,
            _der(0x30, bytes.fromhex("06092a864886f70d0101010500"))
            + _der(0x03, b"\x00" + _der(0x30, _integer(N) + _integer(65537))),
        )
    ).decode(),
    safe="",
)


def decrypt(value: str) -> str:
    block = pow(int.from_bytes(base64.b64decode(unquote(value)), "big"), D, N)
    data = block.to_bytes(128, "big")
    assert data[:2] == b"\x00\x02" and 0 not in data[2:10]
    return data[data.index(0, 2) + 1 :].decode()


class Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body
        self.headers: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        return self.body


class Camera:
    """The camera's anonymous web API (HTTPS) and ONVIF device service."""

    def __init__(
        self, initialized: bool = False, vigi: bool = True, openapi: bool = False
    ):
        self.initialized = initialized
        self.vigi = vigi
        # InSight-style: the documented OpenAPI initialisation on /openapi/initialize.
        self.openapi = openapi
        self.method = ""
        self.password = ""
        self.received: dict = {}
        self.clock: str = ""
        self.drops = 0

    def request_(self, method, url, **kwargs):
        parts = urlsplit(url)
        if parts.scheme == "https":
            assert kwargs["ssl"] is False
            if self.drops:
                self.drops -= 1
                raise OSError("connection reset")
            if parts.path == "/openapi/initialize":
                if not self.openapi:
                    return Response(404, b"<html>404</html>")
                return Response(
                    200, json.dumps(self.init(json.loads(kwargs["data"]))).encode()
                )
            if not self.vigi:
                return Response(200, b"<html></html>")
            return Response(
                200, json.dumps(self.web(json.loads(kwargs["data"]))).encode()
            )
        body = kwargs["data"].decode()
        if "GetSystemDateAndTime" in body:
            return Response(
                200, SOAP.format("<GetSystemDateAndTimeResponse/>").encode()
            )
        if "SetSystemDateAndTime" in body:
            if not self.password or "UsernameToken" not in body:
                return Response(401, b"")
            self.clock = body
            return Response(
                200, SOAP.format("<SetSystemDateAndTimeResponse/>").encode()
            )
        return Response(404, b"")

    request = request_

    def init(self, body: dict) -> dict:
        if self.initialized:
            return {"method": body["method"], "error_code": -10002}
        if body["method"] == "getEncrptInfo":
            return {
                "method": "getEncryptInfo",
                "result": {"encryptInfo": {"rsakey": SPKI}},
                "error_code": 0,
            }
        if body["method"] == "setPassword":
            self.password = decrypt(body["params"]["encrypted_password"])
            self.initialized, self.method = True, "openapi"
            return {"method": "setPassword", "error_code": 0}
        return {"method": body["method"], "error_code": -10003}

    def web(self, body: dict) -> dict:
        if "user_management" in body:
            if self.initialized:
                return {"data": {"code": -40401, "nonce": "x"}, "error_code": -40401}
            return {"data": {"code": -40405}, "error_code": -40401}
        if "get_language_capability" in body:
            return {
                "language_capability": ["English", "Traditional%20Chinese"],
                "error_code": 0,
            }
        if "set_password" in body:
            self.received = body["set_password"]
            self.password = decrypt(self.received["ciphertext"])
            self.initialized = True
            return {"error_code": 0}
        return {"error_code": -40210}


class VigiSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.modulus = m._MODULUS
        m._MODULUS = N

    def tearDown(self):
        m._MODULUS = self.modulus

    async def test_state(self):
        self.assertEqual(await m.vigi_state(Camera(), HOST), "uninitialized")
        self.assertEqual(await m.vigi_state(Camera(initialized=True), HOST), "ready")
        self.assertIsNone(await m.vigi_state(Camera(vigi=False), HOST))
        flaky = Camera()
        flaky.drops = 1
        self.assertEqual(await m.vigi_state(flaky, HOST), "uninitialized")

    async def test_activation_sets_password_and_clock(self):
        camera = Camera()
        result = await m.vigi_activate(camera, HOST, PASSWORD, time_zone="Asia/Taipei")
        self.assertEqual(
            result,
            {
                "host": HOST,
                "username": "admin",
                "activated": True,
                "clock": True,
                "method": "web",
            },
        )
        self.assertEqual(camera.password, PASSWORD)
        self.assertEqual(camera.received["username"], "admin")
        self.assertEqual(decrypt(camera.received["reset_pwd_email"]), "")
        self.assertEqual(camera.received["language"], "Traditional%20Chinese")
        self.assertEqual(
            [camera.received[f"sec_question_{i}"] for i in (1, 2, 3)],
            ["father_name", "mother_name", "senior_teacher_name"],
        )
        self.assertNotIn(PASSWORD, json.dumps(camera.received))
        self.assertIn("<tt:TZ>UTC-8</tt:TZ>", camera.clock)
        self.assertNotIn(PASSWORD, json.dumps(result))

    async def test_openapi_initialisation_with_the_camera_key(self):
        # An InSight camera: its web API says nothing either way, OpenAPI does.
        camera = Camera(vigi=False, openapi=True)
        self.assertEqual(await m.vigi_state(camera, HOST), "uninitialized")
        result = await m.vigi_activate(camera, HOST, PASSWORD, time_zone="Asia/Taipei")
        self.assertEqual((result["method"], result["clock"]), ("openapi", True))
        self.assertEqual((camera.password, camera.method), (PASSWORD, "openapi"))
        self.assertIn("<tt:TZ>UTC-8</tt:TZ>", camera.clock)
        # Once it has a password the initialisation is closed.
        self.assertIsNone(await m.vigi_state(camera, HOST))

    async def test_web_activation_when_openapi_refuses(self):
        camera = Camera(openapi=True)
        camera.init = lambda body: (
            {
                "method": body["method"],
                "error_code": 0,
                "result": {"encryptInfo": {"rsakey": SPKI}},
            }
            if body["method"] == "getEncrptInfo"
            else {"method": "setPassword", "error_code": -10010}
        )
        result = await m.vigi_activate(camera, HOST, PASSWORD)
        self.assertEqual(result["method"], "web")
        self.assertEqual(camera.password, PASSWORD)

    def test_public_key(self):
        self.assertEqual(m.public_key(SPKI), (N, 65537))
        self.assertEqual(
            m.public_key(unquote(SPKI)[:40] + "\n" + unquote(SPKI)[40:]), (N, 65537)
        )
        for bad in ("", "bm90IGEga2V5", "%%%"):
            with self.subTest(bad), self.assertRaises(m.CameraError):
                m.public_key(bad)

    async def test_active_and_unknown_cameras_are_refused(self):
        with self.assertRaises(m.CameraError) as caught:
            await m.vigi_activate(Camera(initialized=True), HOST, PASSWORD)
        self.assertEqual(caught.exception.code, "camera_already_active")
        with self.assertRaises(m.CameraError) as caught:
            await m.vigi_activate(Camera(vigi=False), HOST, PASSWORD)
        self.assertEqual(caught.exception.code, "camera_not_vigi")

    async def test_weak_passwords_never_reach_the_camera(self):
        camera = Camera()
        for weak in (
            "Aa123456789",
            "short1!A",
            "Ab1!cD2#e",
            "Xaab!9y8z7Q1",
            "Xabc!9y8z7Q",
            "Xaaa!9y8z7Q",
            "Q!x9y789zW",
            "admin!X9y8z",
        ):
            with self.subTest(weak), self.assertRaises(m.CameraError) as caught:
                await m.vigi_activate(camera, HOST, weak)
            self.assertEqual(caught.exception.code, "camera_weak_password")
        self.assertFalse(camera.initialized)
        self.assertEqual(m.password_error(PASSWORD), "")

    def test_posix_time_zone(self):
        now = datetime(2026, 1, 15, tzinfo=UTC)
        self.assertEqual(m.posix_tz("Asia/Taipei", now), "UTC-8")
        self.assertEqual(m.posix_tz("Asia/Kolkata", now), "UTC-5:30")
        self.assertEqual(m.posix_tz("America/New_York", now), "UTC+5")
        self.assertEqual(m.posix_tz("Nowhere/City", now), "UTC-8")


if __name__ == "__main__":
    unittest.main()
