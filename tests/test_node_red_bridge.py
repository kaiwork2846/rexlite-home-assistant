"""Bridge flow validation, merge rules and background deploy jobs."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import time
import types
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_node_red_bridge_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
for name in ("rs485_protocol", "node_red_client", "node_red_bridge"):
    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.{name}", PACKAGE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
m = sys.modules[f"{package.__name__}.node_red_bridge"]
client_module = sys.modules[f"{package.__name__}.node_red_client"]

TAB = "0123456789abcdef"
CLIENT = "fedcba9876543210"
OPERATION = "3f2a3c8e-5d1b-4a8e-9c3d-2b1a0f9e8d7c"
CURTAINS = {
    "template": "somfy_curtain_rs485",
    "gatewayHost": "192.168.77.95",
    "gatewayPort": 8899,
    "devices": [
        {"name": "客廳布簾", "key": "livingroom1", "address": "203fab"},
        {"name": "客廳紗簾", "key": "livingroom2", "address": "203F95"},
    ],
}
ACS = {
    "template": "hitachi_ac_modbus",
    "gatewayHost": "192.168.77.99",
    "gatewayPort": "1621",
    "devices": [{"name": "客廳冷氣", "key": "livingroom", "address": "2"}],
}
LOCK = {
    "template": "yale_lock_ya071",
    "gatewayHost": "192.168.1.35",
    "gatewayPort": 26,
    "devices": [{"name": "前門", "key": "front_door", "address": "1"}],
}


def broker():
    return {"id": m.BROKER_ID, "type": "mqtt-broker", "broker": "x", "port": "1"}


def curtain_flow() -> list[dict]:
    return [
        {"id": TAB, "type": "tab", "label": "RexLite · Somfy 窗簾"},
        {
            "id": "a1",
            "type": "mqtt in",
            "z": TAB,
            "topic": "curtain/+/command",
            "broker": m.BROKER_ID,
        },
        {"id": "a2", "type": "function", "z": TAB, "func": "return msg;"},
        {
            "id": "a3",
            "type": "tcp request",
            "z": TAB,
            "server": "192.168.77.95",
            "port": "8899",
        },
        {"id": "a4", "type": "mqtt out", "z": TAB, "topic": "", "broker": m.BROKER_ID},
        broker(),
    ]


def ac_flow() -> list[dict]:
    return [
        {"id": TAB, "type": "tab", "label": "RexLite · 日立冷氣"},
        {"id": "b1", "type": "modbus-flex-getter", "z": TAB, "server": CLIENT},
        {"id": "b2", "type": "modbus-flex-write", "z": TAB, "server": CLIENT},
        {
            "id": "b3",
            "type": "mqtt out",
            "z": TAB,
            "topic": "rexlite/nodered/x/status",
            "broker": m.BROKER_ID,
        },
        {
            "id": CLIENT,
            "type": "modbus-client",
            "clienttype": "tcp",
            "tcpHost": "192.168.77.99",
            "tcpPort": "1621",
        },
        broker(),
    ]


class ValidationTests(unittest.TestCase):
    def test_bridge_definitions_are_normalized(self):
        bridge = m.validate_bridge(CURTAINS)
        self.assertEqual(bridge["devices"][0]["address"], "203FAB")
        self.assertEqual(m.validate_bridge(ACS)["gatewayPort"], 1621)
        for change in (
            {"template": "other"},
            {"gatewayHost": "8.8.8.8"},
            {"devices": []},
            {"devices": [{"name": "x", "key": "Bad Key", "address": "203F95"}]},
            {"devices": [CURTAINS["devices"][0], CURTAINS["devices"][0]]},
        ):
            with self.assertRaises(m.NodeRedError, msg=change):
                m.validate_bridge({**CURTAINS, **change})
        with self.assertRaises(m.NodeRedError):
            m.validate_bridge(
                {**ACS, "devices": [{**ACS["devices"][0], "address": "300"}]}
            )

    def test_yale_lock_is_one_device_per_serial_port(self):
        self.assertEqual(m.validate_bridge(LOCK)["devices"][0]["address"], "1")
        for change in (
            {"devices": [{**LOCK["devices"][0], "address": "2"}]},
            {
                "devices": [
                    LOCK["devices"][0],
                    {"name": "後門", "key": "back_door", "address": "1"},
                ]
            },
        ):
            with self.assertRaises(m.NodeRedError, msg=change):
                m.validate_bridge({**LOCK, **change})

    def test_yale_flow_may_use_lock_topics_only_on_its_gateway(self):
        bridge = m.validate_bridge(LOCK)
        flow = [
            {"id": TAB, "type": "tab", "label": "RexLite · 耶魯電子鎖"},
            {
                "id": "c1",
                "type": "mqtt in",
                "z": TAB,
                "topic": "lock/+/set",
                "broker": m.BROKER_ID,
            },
            {
                "id": "c2",
                "type": "tcp request",
                "z": TAB,
                "server": "192.168.1.35",
                "port": "26",
                "out": "sit",
            },
            {
                "id": "c3",
                "type": "mqtt out",
                "z": TAB,
                "topic": "",
                "broker": m.BROKER_ID,
            },
            broker(),
        ]
        self.assertEqual(m.validate_flow(flow, bridge)["tabId"], TAB)
        bad = copy.deepcopy(flow)
        bad[1]["topic"] = "homeassistant/lock/x/config"
        with self.assertRaises(m.NodeRedError):
            m.validate_flow(bad, bridge)

    def test_flow_shape_is_enforced(self):
        shape = m.validate_flow(curtain_flow(), m.validate_bridge(CURTAINS))
        self.assertEqual(shape["tabId"], TAB)
        self.assertEqual(shape["configIds"], [])
        ac = m.validate_flow(ac_flow(), m.validate_bridge(ACS))
        self.assertEqual(ac["configIds"], [CLIENT])
        self.assertIn("modbus-flex-getter", ac["types"])

        def broken(mutate):
            flow = curtain_flow()
            mutate(flow)
            with self.assertRaises(m.NodeRedError):
                m.validate_flow(flow, m.validate_bridge(CURTAINS))

        broken(lambda f: f.append({"id": "x", "type": "exec", "z": TAB}))
        broken(lambda f: f[3].update(server="10.0.0.9"))
        broken(lambda f: f[1].update(topic="homeassistant/#"))
        broken(lambda f: f[1].update(broker="other"))
        broken(lambda f: f[2].update(z="another"))
        broken(lambda f: f[5].update(credentials={"user": "x"}))
        broken(lambda f: f.append(copy.deepcopy(f[0]) | {"id": "1111111111111111"}))
        broken(lambda f: f[0].update(label="Kitchen"))
        broken(lambda f: f.pop())

    def test_merge_replaces_only_this_bridge(self):
        current = [
            {"id": "user-tab", "type": "tab"},
            {"id": "u1", "type": "inject", "z": "user-tab"},
            {"id": "user-config", "type": "mqtt-broker"},
            {"id": TAB, "type": "tab", "label": "old"},
            {"id": "old-node", "type": "function", "z": TAB},
            {"id": "stale-client", "type": "modbus-client"},
            {"id": m.BROKER_ID, "type": "mqtt-broker", "broker": "old"},
        ]
        merged = m.merge_flows(current, curtain_flow(), TAB, {"stale-client"})
        ids = [n["id"] for n in merged]
        self.assertEqual(ids[:3], ["user-tab", "u1", "user-config"])
        self.assertNotIn("old-node", ids)
        self.assertNotIn("stale-client", ids)
        self.assertEqual(ids.count(m.BROKER_ID), 1)
        self.assertEqual(len(ids), len(set(ids)))

    def test_broker_credentials_are_added_on_site(self):
        flow = m.apply_broker(
            curtain_flow(),
            {"host": "127.0.0.1", "port": 1883, "username": "ha", "password": "pw"},
        )
        node = next(n for n in flow if n["id"] == m.BROKER_ID)
        self.assertEqual((node["broker"], node["port"]), ("127.0.0.1", "1883"))
        self.assertEqual(node["credentials"], {"user": "ha", "password": "pw"})
        self.assertNotIn("credentials", curtain_flow()[5])

    def test_slow_polling_flows_report_their_period(self):
        now = int(time.time() * 1000)
        beat = {"deployment": OPERATION, "ts": now, "gateway": {"lastRxAgeMs": 55_000}}
        self.assertFalse(m.heartbeat_health(beat, OPERATION, now)["gateway"])
        beat["gateway"]["pollMs"] = 60_000
        self.assertTrue(m.heartbeat_health(beat, OPERATION, now)["gateway"])
        beat["gateway"].update(lastRxAgeMs=95_000)
        self.assertFalse(m.heartbeat_health(beat, OPERATION, now)["gateway"])
        # An absurd period cannot hide a dead gateway.
        beat["gateway"].update(pollMs=10**9, lastRxAgeMs=3_600_000)
        self.assertFalse(m.heartbeat_health(beat, OPERATION, now)["gateway"])

    def test_heartbeat_health(self):
        now = int(time.time() * 1000)
        good = {"deployment": OPERATION, "ts": now, "gateway": {"lastRxAgeMs": 1200}}
        self.assertTrue(m.heartbeat_health(good, OPERATION, now)["gateway"])
        stale_gateway = {**good, "gateway": {"lastRxAgeMs": None}}
        self.assertEqual(
            {
                k: v
                for k, v in m.heartbeat_health(stale_gateway, OPERATION, now).items()
                if k != "checkedAt"
            },
            {"mqtt": True, "gateway": False},
        )
        self.assertFalse(m.heartbeat_health(good, "other", now)["mqtt"])
        self.assertFalse(
            m.heartbeat_health({**good, "ts": now - 600_000}, None, now)["mqtt"]
        )


class Store:
    def __init__(self, data=None) -> None:
        self.data = data
        self.saved: list[dict] = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.saved.append(copy.deepcopy(data))


class Mqtt:
    def __init__(self, heartbeat=None, config=None) -> None:
        self.heartbeat = heartbeat
        self.settings = (
            config
            if config is not None
            else {
                "host": "core-mosquitto",
                "port": 1883,
                "username": "homeassistant",
                "password": "secret",
            }
        )
        self.published: list[tuple[str, str, bool]] = []
        self.subscriptions: list[str] = []

    def config(self):
        return self.settings

    async def available(self):
        return True

    async def subscribe(self, topic, handler):
        self.subscriptions.append(topic)
        if self.heartbeat is not None:
            payload = self.heartbeat() if callable(self.heartbeat) else self.heartbeat
            asyncio.get_running_loop().call_later(
                0.01, handler, topic.replace("+", TAB), json.dumps(payload)
            )
        return lambda: None

    async def publish(self, topic, payload, *, retain):
        self.published.append((topic, payload, retain))


class NodeRed:
    def __init__(self, flows, types=None) -> None:
        self.flows = flows
        self.rev = "r1"
        self.types = types or {"inject": "node-red", "function": "node-red"}
        self.installed: list[str] = []
        self.conflicts = 0
        self.deployed: list[list[dict]] = []

    async def settings(self):
        return {"version": "4.0.9"}

    async def node_types(self):
        return dict(self.types)

    async def install_module(self, module):
        self.installed.append(module)
        for name in ("modbus-flex-getter", "modbus-flex-write", "modbus-client"):
            self.types[name] = module

    async def get_flows(self):
        return self.rev, copy.deepcopy(self.flows)

    async def set_flows(self, flows, rev):
        if self.conflicts:
            self.conflicts -= 1
            self.rev = "r-changed"
            raise client_module.NodeRedConflict("node_red_conflict", "changed")
        if rev != self.rev:
            raise AssertionError("stale rev used")
        self.flows = flows
        self.deployed.append(flows)
        self.rev = "r-next"
        return self.rev


def manager(node_red, *, mqtt=None, store=None, info=None):
    bridges = m.NodeRedBridges(
        store=store or Store(),
        session=None,
        mqtt=mqtt,
        create_task=lambda coro, name: asyncio.get_running_loop().create_task(coro),
        integration_version="0.1.22",
    )

    async def locate():
        if node_red is None:
            return None, {
                "found": False,
                "state": "not_found",
                "message": "找不到 Node-RED",
            }
        return node_red, info or {
            "found": True,
            "kind": "addon",
            "state": "ready",
            "hostNetwork": True,
        }

    bridges._locate = locate
    bridges.verify_timeout = 0.2
    return bridges


async def finish(bridges, job):
    for _ in range(400):
        current = bridges.job(job["jobId"])
        if current["state"] != "running":
            return current
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def test_deploy_installs_modules_merges_and_verifies(self):
        node_red = NodeRed([{"id": "user-tab", "type": "tab"}])
        node_red.conflicts = 1
        heartbeat = lambda: {  # noqa: E731
            "deployment": OPERATION,
            "ts": int(time.time() * 1000),
            "gateway": {"lastRxAgeMs": 500},
        }
        store = Store()
        bridges = manager(node_red, mqtt=Mqtt(heartbeat), store=store)
        job = await bridges.start_deploy(
            {"operationId": OPERATION, "bridge": ACS, "flow": ac_flow(), "broker": {}}
        )
        self.assertEqual(job["state"], "running")
        again = await bridges.start_deploy(
            {"operationId": OPERATION, "bridge": ACS, "flow": ac_flow(), "broker": {}}
        )
        self.assertEqual(again["jobId"], OPERATION)
        done = await finish(bridges, job)
        self.assertEqual(done["state"], "completed", done)
        self.assertEqual(done["result"]["installed"], ["node-red-contrib-modbus"])
        self.assertEqual(done["result"]["health"]["gateway"], True)
        ids = [n["id"] for n in node_red.flows]
        self.assertEqual(ids[0], "user-tab")
        self.assertIn(TAB, ids)
        broker_node = next(n for n in node_red.flows if n["id"] == m.BROKER_ID)
        self.assertEqual(broker_node["credentials"]["user"], "homeassistant")
        saved = store.saved[-1]
        self.assertEqual(saved["bridges"][TAB]["devices"][0]["key"], "livingroom")
        self.assertNotIn("secret", json.dumps(saved))

    async def test_only_one_job_at_a_time(self):
        node_red = NodeRed([])
        gate = asyncio.Event()
        original = node_red.node_types

        async def slow_types():
            await gate.wait()
            return await original()

        node_red.node_types = slow_types
        bridges = manager(node_red, mqtt=Mqtt({"deployment": OPERATION}))
        job = await bridges.start_deploy(
            {"operationId": OPERATION, "bridge": CURTAINS, "flow": curtain_flow()}
        )
        with self.assertRaises(m.NodeRedError) as caught:
            await bridges.start_deploy(
                {
                    "operationId": "9f2a3c8e-5d1b-4a8e-9c3d-2b1a0f9e8d7c",
                    "bridge": CURTAINS,
                    "flow": curtain_flow(),
                }
            )
        self.assertEqual(caught.exception.code, "node_red_busy")
        gate.set()
        await finish(bridges, job)

    async def test_missing_node_red_fails_the_job_clearly(self):
        bridges = manager(None, mqtt=Mqtt())
        job = await bridges.start_deploy(
            {"operationId": OPERATION, "bridge": CURTAINS, "flow": curtain_flow()}
        )
        done = await finish(bridges, job)
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["error"]["code"], "node_red_not_ready")

    async def test_add_on_broker_hostname_maps_to_host_port(self):
        class Supervisor:
            async def addon_info(self, slug):
                assert slug == "core_mosquitto"
                return {"network": {"1883/tcp": 11883}}

        bridges = manager(NodeRed([]), mqtt=Mqtt())
        bridges.supervisor = Supervisor
        suggestion = await bridges._suggest_broker(
            {"kind": "addon", "hostNetwork": True}
        )
        self.assertEqual(
            suggestion, {"host": "127.0.0.1", "port": 11883, "source": "addon-port"}
        )
        custom = await bridges._broker_settings(
            {
                "mode": "custom",
                "host": "172.17.0.1",
                "port": 11883,
                "useHaCredentials": False,
            },
            {},
        )
        self.assertEqual(custom, {"host": "172.17.0.1", "port": 11883})
        with self.assertRaises(m.NodeRedError):
            await bridges._broker_settings(
                {"mode": "custom", "host": "a b", "port": 1}, {}
            )
        empty = manager(NodeRed([]), mqtt=Mqtt(config={}))
        with self.assertRaises(m.NodeRedError) as caught:
            await empty._broker_settings({}, {})
        self.assertEqual(caught.exception.code, "node_red_mqtt_missing")

    async def test_remove_drops_tab_and_gateway_config(self):
        node_red = NodeRed([])
        mqtt = Mqtt(
            {
                "deployment": OPERATION,
                "ts": int(time.time() * 1000),
                "gateway": {"lastRxAgeMs": 1},
            }
        )
        bridges = manager(node_red, mqtt=mqtt)
        await finish(
            bridges,
            await bridges.start_deploy(
                {"operationId": OPERATION, "bridge": ACS, "flow": ac_flow()}
            ),
        )
        node_red.flows.append({"id": "keep", "type": "tab"})
        remove_id = "8f2a3c8e-5d1b-4a8e-9c3d-2b1a0f9e8d7c"
        done = await finish(
            bridges,
            await bridges.start_remove({"operationId": remove_id, "bridgeId": TAB}),
        )
        self.assertEqual(done["state"], "completed")
        ids = {n["id"] for n in node_red.flows}
        self.assertEqual(ids, {m.BROKER_ID, "keep"})
        self.assertIn((f"rexlite/nodered/{TAB}/status", "", True), mqtt.published)
        with self.assertRaises(m.NodeRedError):
            await bridges.start_remove({"operationId": remove_id, "bridgeId": TAB})

    async def test_status_hides_secrets_and_marks_interrupted_jobs(self):
        store = Store(
            {
                "endpoint": {
                    "mode": "url",
                    "url": "http://192.168.1.2:1880",
                    "username": "a",
                    "password": "pw",
                },
                "bridges": {},
                "jobs": [{"jobId": OPERATION, "kind": "deploy", "state": "running"}],
            }
        )
        bridges = manager(NodeRed([]), mqtt=Mqtt(), store=store)
        status = await bridges.status()
        self.assertEqual(status["api"], 1)
        self.assertEqual(
            status["endpoint"],
            {
                "mode": "url",
                "url": "http://192.168.1.2:1880",
                "username": "a",
                "hasPassword": True,
                "verifySsl": True,
            },
        )
        self.assertEqual(status["job"]["state"], "interrupted")
        self.assertEqual(status["nodeRed"]["version"], "4.0.9")
        self.assertNotIn("secret", json.dumps(status))
        self.assertNotIn("pw", json.dumps(status))

    async def test_scan_marks_devices_already_deployed(self):
        bridges = manager(NodeRed([]), mqtt=Mqtt())
        bridges.data["bridges"][TAB] = {
            **m.validate_bridge(CURTAINS),
            "bridgeId": TAB,
        }

        async def fake_scan(host, port):
            return [{"address": "203F95"}, {"address": "203F00"}]

        original = m.somfy_scan
        m.somfy_scan = fake_scan
        try:
            result = await bridges.scan(
                {
                    "template": "somfy_curtain_rs485",
                    "host": "192.168.77.95",
                    "port": 8899,
                }
            )
        finally:
            m.somfy_scan = original
        self.assertEqual(
            result["devices"][0]["existing"], {"name": "客廳紗簾", "key": "livingroom2"}
        )
        self.assertIsNone(result["devices"][1]["existing"])


if __name__ == "__main__":
    unittest.main()
