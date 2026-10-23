"""Thread border router listing: which address is probed and how setup is detected."""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).parents[1] / "custom_components/rexlite/thread_border_routers.py"
spec = importlib.util.spec_from_file_location("thread_border_routers_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def router(**changes):
    return SimpleNamespace(
        **{
            "instance_name": "GL-S20-4F2A",
            "addresses": ["fe80::1", "192.168.68.70", "fd11:22::5"],
            "border_agent_id": "A1B2C3D4E5F60718293A4B5C6D7E8F90",
            "brand": "glinet",
            "extended_address": "aabbccddeeff0011",
            "model_name": "GL-S20",
            "network_name": "OpenThread-4f2a",
            "thread_version": "1.3.0",
            "vendor_name": "GL.iNET Inc.",
            **changes,
        }
    )


class AddressTests(unittest.TestCase):
    def test_private_ipv4_is_probed_before_ula_ipv6(self):
        self.assertEqual(
            m.rest_host(["fe80::1", "fd11:22::5", "192.168.68.70"]), "192.168.68.70"
        )
        self.assertEqual(m.rest_host(["fe80::1", "fd11:22::5"]), "[fd11:22::5]")

    def test_public_and_local_only_addresses_are_never_probed(self):
        for addresses in (
            ["8.8.8.8"],
            ["2001:4860:4860::8888"],
            ["127.0.0.1", "::1"],
            ["169.254.10.20", "fe80::1"],
            ["224.0.0.251", "0.0.0.0"],
            ["not-an-address"],
            [],
        ):
            self.assertEqual(m.rest_host(addresses), "", addresses)


class SetupStateTests(unittest.TestCase):
    def test_configured_by_border_agent_id_or_rest_host(self):
        entries = [
            SimpleNamespace(
                unique_id="a1b2c3d4e5f60718293a4b5c6d7e8f90",
                data={"url": "http://192.168.68.99:8081"},
            ),
            SimpleNamespace(unique_id=None, data={"url": "http://[fd11:22::9]:8081"}),
        ]
        agent_ids, hosts = m.configured_otbr(entries)
        self.assertEqual(hosts, {"192.168.68.99", "[fd11:22::9]"})
        # Same border agent id, any address.
        self.assertTrue(m.router_row(router(), agent_ids, hosts)["configured"])
        # A new router whose REST host is already an OTBR entry.
        moved = router(border_agent_id=None, addresses=["192.168.68.99"])
        self.assertTrue(m.router_row(moved, agent_ids, hosts)["configured"])
        self.assertFalse(
            m.router_row(router(border_agent_id="ff"), agent_ids, hosts)["configured"]
        )

    def test_row_names_the_model_and_keeps_identity(self):
        row = m.router_row(router(), set(), set())
        self.assertEqual(
            (row["name"], row["vendor"], row["host"], row["network"]),
            ("GL-S20", "GL.iNET Inc.", "192.168.68.70", "OpenThread-4f2a"),
        )
        self.assertEqual(
            m.router_row(router(model_name=None), set(), set())["name"], "GL.iNET Inc."
        )
        self.assertEqual(
            m.router_row(router(model_name=None, vendor_name=None), set(), set())[
                "name"
            ],
            "GL-S20-4F2A",
        )


class ClientError(Exception):
    pass


class ClientConnectionError(ClientError):
    pass


# The probe imports aiohttp lazily; HA ships it, the verify environment may not.
fake_aiohttp = SimpleNamespace(
    ClientError=ClientError,
    ClientConnectionError=ClientConnectionError,
    ClientTimeout=lambda total: SimpleNamespace(total=total),
)


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.urls = []

    def get(self, url, timeout):
        self.urls.append((url, timeout.total))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return FakeResponse(self.outcome)


@patch.dict(sys.modules, {"aiohttp": fake_aiohttp})
class ProbeTests(unittest.TestCase):
    def test_rest_api_state(self):
        ok = FakeSession(200)
        self.assertEqual(asyncio.run(m.probe_rest(ok, "192.168.68.70")), "ok")
        self.assertEqual(
            ok.urls, [("http://192.168.68.70:8081/node/ba-id", m.PROBE_TIMEOUT)]
        )
        self.assertEqual(
            asyncio.run(m.probe_rest(FakeSession(404), "192.168.68.70")), "closed"
        )
        self.assertEqual(
            asyncio.run(
                m.probe_rest(FakeSession(ClientConnectionError()), "192.168.68.70")
            ),
            "closed",
        )
        self.assertEqual(
            asyncio.run(m.probe_rest(FakeSession(TimeoutError()), "[fd11:22::5]")),
            "closed",
        )
        self.assertEqual(asyncio.run(m.probe_rest(FakeSession(200), "")), "unreachable")


if __name__ == "__main__":
    unittest.main()
