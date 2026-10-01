"""KNX/IP gateway scan and its admin websocket command, without a KNX network."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_gateway_scan_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package


def load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", PACKAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = load("knx_gateway_scan")
deployment = load("knx_project_deployment")


class Address:
    def __init__(self, text: str) -> None:
        area, line, device = (int(part) for part in text.split("."))
        self.text, self.raw = text, (area << 12) | (line << 8) | device

    def __str__(self) -> str:
        return self.text


def descriptor(ip: str, address: str | None, **flags) -> types.SimpleNamespace:
    values = {
        "name": "",
        "port": 3671,
        "supports_tunnelling": False,
        "supports_tunnelling_tcp": False,
        "supports_routing": False,
        "tunnelling_requires_secure": None,
        "routing_requires_secure": None,
    }
    values.update(flags)
    return types.SimpleNamespace(
        ip_addr=ip,
        individual_address=Address(address) if address else None,
        **values,
    )


class FakeNetwork:
    """Stands in for xknx; records how the scanner was built."""

    def __init__(self, gateways=(), failure: Exception | None = None) -> None:
        self.gateways, self.failure, self.options = list(gateways), failure, None
        network = self

        class XKNX:
            pass

        class GatewayScanner:
            def __init__(self, xknx, **options):
                assert isinstance(xknx, XKNX)
                network.options = options

            async def scan(self):
                if network.failure:
                    raise network.failure
                return list(network.gateways)

        root = types.ModuleType("xknx")
        root.XKNX = XKNX
        scanner = types.ModuleType("xknx.io.gateway_scanner")
        scanner.GatewayScanner = GatewayScanner
        self.modules = {"xknx": root, "xknx.io.gateway_scanner": scanner}


def fake_hass(root: str) -> types.SimpleNamespace:
    async def executor(function, *args):
        return function(*args)

    return types.SimpleNamespace(
        config=types.SimpleNamespace(config_dir=root),
        data={},
        async_add_executor_job=executor,
        bus=types.SimpleNamespace(async_listen_once=Mock()),
    )


class GatewayScanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.hass = fake_hass(temp.name)

    async def test_reports_every_gateway_in_home_assistant_order(self):
        network = FakeNetwork(
            [
                descriptor(
                    "192.0.2.30",
                    "1.1.240",
                    name="Secure interface",
                    port=3672,
                    supports_tunnelling=True,
                    supports_tunnelling_tcp=True,
                    tunnelling_requires_secure=True,
                    routing_requires_secure=False,
                ),
                descriptor(
                    "192.0.2.20",
                    "1.1.0",
                    name="Line router",
                    supports_routing=True,
                    routing_requires_secure=True,
                ),
                # Core v1 devices report neither an address nor secure support.
                descriptor("192.0.2.10", None, supports_tunnelling=True),
            ]
        )

        with patch.dict(sys.modules, network.modules):
            result = await scan.scan_gateways(self.hass)

        self.assertEqual(network.options, {"stop_on_found": 0, "timeout_in_seconds": 3})
        self.assertEqual(
            result,
            [
                {
                    "name": "",
                    "ip": "192.0.2.10",
                    "port": 3671,
                    "individualAddress": "",
                    "supportsTunnelling": True,
                    "supportsTunnellingTcp": False,
                    "supportsRouting": False,
                    "tunnellingRequiresSecure": False,
                    "routingRequiresSecure": False,
                },
                {
                    "name": "Line router",
                    "ip": "192.0.2.20",
                    "port": 3671,
                    "individualAddress": "1.1.0",
                    "supportsTunnelling": False,
                    "supportsTunnellingTcp": False,
                    "supportsRouting": True,
                    "tunnellingRequiresSecure": False,
                    "routingRequiresSecure": True,
                },
                {
                    "name": "Secure interface",
                    "ip": "192.0.2.30",
                    "port": 3672,
                    "individualAddress": "1.1.240",
                    "supportsTunnelling": True,
                    "supportsTunnellingTcp": True,
                    "supportsRouting": False,
                    "tunnellingRequiresSecure": True,
                    "routingRequiresSecure": False,
                },
            ],
        )

    async def test_missing_xknx_is_reported_as_unavailable(self):
        with (
            patch.dict(sys.modules, {"xknx": None}),
            self.assertRaises(scan.GatewayScanUnavailable),
        ):
            await scan.scan_gateways(self.hass)

    async def test_websocket_command_returns_results_and_bounded_errors(self):
        handlers = {}
        websocket = types.SimpleNamespace(
            websocket_command=lambda schema: lambda fn: fn,
            require_admin=lambda fn: fn,
            async_response=lambda fn: fn,
            async_register_command=lambda hass, fn: handlers.setdefault(
                fn.__name__, fn
            ),
        )
        components = types.ModuleType("homeassistant.components")
        components.websocket_api = websocket
        with patch.dict(sys.modules, {"homeassistant.components": components}):
            deployment.register_websocket_commands(self.hass)
        self.addCleanup(self.hass.data["rexlite_knx_uploads"].close)
        command = handlers["gateway_scan"]
        router = descriptor("192.0.2.20", "1.1.0", supports_routing=True)

        cases = (
            (FakeNetwork([router]).modules, "result", None),
            (
                FakeNetwork(failure=OSError("private socket detail")).modules,
                "error",
                ("gateway_scan_failed", "gateway_scan_failed"),
            ),
            (
                {"xknx": None},
                "error",
                ("gateway_scan_unavailable", "knx_library_unavailable"),
            ),
        )
        for modules, outcome, error in cases:
            connection = Mock()
            with self.subTest(outcome=outcome, error=error):
                with patch.dict(sys.modules, modules):
                    await command(self.hass, connection, {"id": 5})
                if outcome == "result":
                    connection.send_error.assert_not_called()
                    (message_id, result), _ = connection.send_result.call_args
                    self.assertEqual(message_id, 5)
                    self.assertEqual(
                        [(item["ip"], item["supportsRouting"]) for item in result],
                        [("192.0.2.20", True)],
                    )
                else:
                    connection.send_result.assert_not_called()
                    connection.send_error.assert_called_once_with(5, *error)


if __name__ == "__main__":
    unittest.main()
