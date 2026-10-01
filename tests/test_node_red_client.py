"""Node-RED Admin API and Supervisor client behaviour with a scripted session."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_node_red_client_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.node_red_client", PACKAGE / "node_red_client.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class Response:
    def __init__(self, status: int, payload=None) -> None:
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


class Session:
    """Answers (method, url) pairs and records every request."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.routes.get((method, url))
        if answer is None:
            raise OSError("connection refused")
        if callable(answer):
            answer = answer(kwargs)
        return Response(*answer)


BASE = "http://192.168.1.20:1880"


class UrlTests(unittest.TestCase):
    def test_only_local_urls_without_credentials(self):
        self.assertEqual(m.validate_node_red_url(f"{BASE}/"), BASE)
        self.assertEqual(
            m.validate_node_red_url("http://nodered:1880"), "http://nodered:1880"
        )
        self.assertEqual(
            m.validate_node_red_url("https://ha.local:1880/admin"),
            "https://ha.local:1880/admin",
        )
        for bad in (
            "ftp://192.168.1.20",
            "http://8.8.8.8:1880",
            "http://user:pw@192.168.1.20:1880",
            "http://192.168.1.20:1880/?x=1",
            "http://example.com:1880",
            "http://169.254.1.10:1880",
        ):
            with self.assertRaises(m.NodeRedError, msg=bad):
                m.validate_node_red_url(bad)


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_admin_auth(self):
        session = Session(
            {
                ("GET", f"{BASE}/auth/login"): (200, {}),
                ("GET", f"{BASE}/settings"): (200, {"version": "4.0.9"}),
            }
        )
        client = await m.connect_url(session, BASE)
        self.assertEqual(client.endpoint.headers, {})
        self.assertEqual((await client.settings())["version"], "4.0.9")

    async def test_admin_auth_token(self):
        def token(kwargs):
            form = kwargs["data"]
            ok = form["username"] == "admin" and form["password"] == "pw"
            return (200, {"access_token": "abc"}) if ok else (403, {})

        def settings(kwargs):
            ok = kwargs["headers"].get("Authorization") == "Bearer abc"
            return (200, {"version": "4.1.0"}) if ok else (401, {})

        routes = {
            ("GET", f"{BASE}/auth/login"): (200, {"type": "credentials"}),
            ("POST", f"{BASE}/auth/token"): token,
            ("GET", f"{BASE}/settings"): settings,
        }
        client = await m.connect_url(
            Session(routes), BASE, username="admin", password="pw"
        )
        self.assertEqual(client.endpoint.headers["Authorization"], "Bearer abc")
        with self.assertRaises(m.NodeRedError) as caught:
            await m.connect_url(Session(routes), BASE, username="admin", password="x")
        self.assertEqual(caught.exception.code, "node_red_auth_failed")
        with self.assertRaises(m.NodeRedError) as caught:
            await m.connect_url(Session(routes), BASE)
        self.assertEqual(caught.exception.code, "node_red_auth_required")

    async def test_add_on_direct_port_uses_basic_auth(self):
        def guarded(kwargs):
            auth = kwargs["headers"].get("Authorization", "")
            return (
                (200, {"version": "4.0.9"}) if auth.startswith("Basic ") else (401, "")
            )

        routes = {
            ("GET", f"{BASE}/auth/login"): (401, ""),
            ("GET", f"{BASE}/settings"): guarded,
        }
        client = await m.connect_url(
            Session(routes), BASE, username="ha", password="pw"
        )
        self.assertTrue(client.endpoint.headers["Authorization"].startswith("Basic "))

    async def test_unreachable_and_foreign_services(self):
        with self.assertRaises(m.NodeRedError) as caught:
            await m.connect_url(Session({}), BASE)
        self.assertEqual(caught.exception.code, "node_red_unreachable")
        routes = {("GET", f"{BASE}/auth/login"): (404, "Not Found")}
        with self.assertRaises(m.NodeRedError) as caught:
            await m.connect_url(Session(routes), BASE)
        self.assertEqual(caught.exception.code, "node_red_unreachable")


class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    def client(self, routes):
        session = Session(routes)
        endpoint = m.NodeRedEndpoint(
            kind="addon", base_url=BASE, label="x", headers={"Cookie": "a=b"}
        )
        return session, m.NodeRedClient(session, endpoint)

    async def test_flows_use_api_v2_and_flows_deployment(self):
        session, client = self.client(
            {
                ("GET", f"{BASE}/flows"): (200, {"rev": "r1", "flows": [{"id": "a"}]}),
                ("POST", f"{BASE}/flows"): (200, {"rev": "r2"}),
            }
        )
        self.assertEqual(await client.get_flows(), ("r1", [{"id": "a"}]))
        self.assertEqual(await client.set_flows([{"id": "a"}], "r1"), "r2")
        method, _url, kwargs = session.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["json"], {"flows": [{"id": "a"}], "rev": "r1"})
        self.assertEqual(kwargs["headers"]["Node-RED-API-Version"], "v2")
        self.assertEqual(kwargs["headers"]["Node-RED-Deployment-Type"], "flows")
        self.assertEqual(kwargs["headers"]["Cookie"], "a=b")

    async def test_conflict_and_rejection(self):
        _session, client = self.client(
            {("POST", f"{BASE}/flows"): (409, {"code": "version_mismatch"})}
        )
        with self.assertRaises(m.NodeRedConflict):
            await client.set_flows([], "old")
        _session, client = self.client(
            {("POST", f"{BASE}/flows"): (400, {"message": "bad"})}
        )
        with self.assertRaises(m.NodeRedError) as caught:
            await client.set_flows([], "old")
        self.assertEqual(caught.exception.code, "node_red_deploy_rejected")

    async def test_node_types_and_module_install(self):
        _session, client = self.client(
            {
                ("GET", f"{BASE}/nodes"): (
                    200,
                    [
                        {"module": "node-red", "types": ["inject", "function"]},
                        {
                            "module": "node-red-contrib-modbus",
                            "types": ["modbus-flex-getter"],
                            "enabled": False,
                        },
                    ],
                ),
                ("POST", f"{BASE}/nodes"): (400, {"message": "npm failed"}),
            }
        )
        self.assertEqual(
            await client.node_types(), {"inject": "node-red", "function": "node-red"}
        )
        with self.assertRaises(m.NodeRedError) as caught:
            await client.install_module("node-red-contrib-modbus")
        self.assertIn("npm failed", caught.exception.message)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_endpoint_uses_a_supervisor_session(self):
        session = Session(
            {
                ("POST", "http://supervisor/ingress/session"): (
                    200,
                    {"result": "ok", "data": {"session": "s3cr3t"}},
                ),
            }
        )
        supervisor = m.SupervisorClient(session, "supervisor", "token")
        endpoint = await supervisor.ingress_endpoint(
            {
                "slug": "a0d7b954_nodered",
                "version": "19.0.0",
                "ingress": True,
                "ingress_entry": "/api/hassio_ingress/abc123",
                "host_network": True,
            }
        )
        self.assertEqual(endpoint.base_url, "http://supervisor/ingress/abc123")
        self.assertEqual(endpoint.headers, {"Cookie": "ingress_session=s3cr3t"})
        self.assertTrue(endpoint.addon["hostNetwork"])
        self.assertEqual(
            session.calls[0][2]["headers"]["Authorization"], "Bearer token"
        )

    async def test_supervisor_errors_are_reported(self):
        session = Session(
            {("GET", "http://supervisor/addons"): (403, {"result": "error"})}
        )
        with self.assertRaises(m.NodeRedError) as caught:
            await m.SupervisorClient(session, "supervisor", "t").addons()
        self.assertEqual(caught.exception.code, "node_red_supervisor_failed")

    def test_prefers_official_started_add_on(self):
        addons = [
            {"slug": "local_nodered", "name": "Node-RED dev", "state": "started"},
            {"slug": "core_mosquitto", "name": "Mosquitto", "state": "started"},
            {"slug": "a0d7b954_nodered", "name": "Node-RED", "state": "stopped"},
        ]
        self.assertEqual(m.find_node_red_addon(addons)["slug"], "a0d7b954_nodered")
        self.assertIsNone(m.find_node_red_addon(addons[1:2]))


if __name__ == "__main__":
    unittest.main()
