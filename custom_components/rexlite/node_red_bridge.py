"""Deploy REXLiTE RS-485 bridges into the site's Node-RED.

The operation platform generates a bridge flow (one Node-RED tab per
gateway). This module only accepts that narrow shape, merges it into the
existing flows without touching anything else, installs missing palette
modules, and confirms the running flow through an MQTT heartbeat. Gateway
discovery talks to the RS-485 gateways directly from the HA host.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .node_red_client import (
    LOCAL_CANDIDATES,
    NodeRedClient,
    NodeRedConflict,
    NodeRedError,
    SupervisorClient,
    connect_url,
    find_node_red_addon,
    supervisor_from_env,
)
from .rs485_protocol import (
    SOMFY_TEMPLATE,
    TEMPLATES,
    GatewayError,
    gateway_host,
    gateway_port,
    modbus_scan,
    motor_id,
    somfy_scan,
    somfy_send,
)

DATA_KEY = "rexlite_node_red"
STORE_KEY = "rexlite.node_red"
BRIDGE_API_VERSION = 1
BROKER_ID = "rexlite_mqtt_broker"
STATUS_TOPIC = "rexlite/nodered/{bridge}/status"
TOPIC_PREFIXES = ("ac/", "curtain/", "rexlite/nodered/")
MAX_FLOW_BYTES = 512 * 1024
MAX_NODES = 200
MAX_DEVICES = 32
ALLOWED_TYPES = {
    "tab",
    "inject",
    "function",
    "delay",
    "tcp request",
    "mqtt in",
    "mqtt out",
    "debug",
    "catch",
    "comment",
    "modbus-flex-getter",
    "modbus-flex-write",
    "modbus-client",
    "mqtt-broker",
}
CONFIG_TYPES = {"modbus-client", "mqtt-broker"}
MODULES = {
    "modbus-flex-getter": "node-red-contrib-modbus",
    "modbus-flex-write": "node-red-contrib-modbus",
    "modbus-client": "node-red-contrib-modbus",
}
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ROOM = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_GATEWAY_FRESH_MS = 30_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fail(message: str, code: str = "node_red_invalid_flow") -> NodeRedError:
    return NodeRedError(code, message)


def validate_bridge(bridge: Any) -> dict:
    """Normalize the declarative bridge definition sent with a flow."""

    if not isinstance(bridge, dict):
        raise _fail("橋接設定格式不正確", "node_red_invalid_device")
    template = bridge.get("template")
    if template not in TEMPLATES:
        raise _fail("不支援的設備範本", "node_red_invalid_device")
    try:
        host = gateway_host(bridge.get("gatewayHost"))
        port = gateway_port(bridge.get("gatewayPort"))
    except GatewayError as err:
        raise NodeRedError(err.code, err.message) from None
    devices = bridge.get("devices")
    if not isinstance(devices, list) or not 1 <= len(devices) <= MAX_DEVICES:
        raise _fail(f"每個閘道可部署 1–{MAX_DEVICES} 台設備", "node_red_invalid_device")
    normalized: list[dict] = []
    keys: set[str] = set()
    addresses: set[str] = set()
    for device in devices:
        if not isinstance(device, dict):
            raise _fail("設備資料格式不正確", "node_red_invalid_device")
        name = str(device.get("name", "")).strip()
        key = str(device.get("key", "")).strip()
        raw = device.get("address", "")
        if not name or len(name) > 128 or not _ROOM.match(key):
            raise _fail("設備名稱或房間代號不正確", "node_red_invalid_device")
        if template == SOMFY_TEMPLATE:
            try:
                address = motor_id(raw)
            except GatewayError as err:
                raise NodeRedError(err.code, err.message) from None
        else:
            text = str(raw).strip()
            if not text.isdigit() or not 1 <= int(text) <= 247:
                raise _fail("Modbus 站號需為 1–247", "node_red_invalid_device")
            address = str(int(text))
        if key in keys or address in addresses:
            raise _fail("房間代號或設備位址重複", "node_red_invalid_device")
        keys.add(key)
        addresses.add(address)
        normalized.append({"name": name, "key": key, "address": address})
    return {
        "template": template,
        "gatewayHost": host,
        "gatewayPort": port,
        "devices": normalized,
    }


def validate_flow(flow: Any, bridge: dict) -> dict:
    """Accept only a REXLiTE bridge tab for the declared gateway.

    Returns the tab id, the gateway-specific config node ids and the node
    types the flow needs.
    """

    if not isinstance(flow, list) or not 1 < len(flow) <= MAX_NODES:
        raise _fail("Node-RED 流程格式不正確")
    if len(json.dumps(flow, ensure_ascii=False).encode()) > MAX_FLOW_BYTES:
        raise _fail("Node-RED 流程內容過大")
    tabs = [n for n in flow if isinstance(n, dict) and n.get("type") == "tab"]
    if len(tabs) != 1:
        raise _fail("每次只能部署一個閘道分頁")
    tab = tabs[0]
    tab_id = str(tab.get("id", ""))
    if not _HEX16.match(tab_id) or not str(tab.get("label", "")).startswith("RexLite"):
        raise _fail("分頁識別不正確")
    ids: set[str] = set()
    config_ids: list[str] = []
    types: set[str] = set()
    host, port = bridge["gatewayHost"], str(bridge["gatewayPort"])
    modbus_clients = {
        n.get("id")
        for n in flow
        if isinstance(n, dict) and n.get("type") == "modbus-client"
    }
    for node in flow:
        if not isinstance(node, dict):
            raise _fail("Node-RED 節點格式不正確")
        node_id, node_type = str(node.get("id", "")), str(node.get("type", ""))
        if node_type not in ALLOWED_TYPES:
            raise _fail(f"不允許的節點類型 {node_type[:40]}")
        if not node_id or node_id in ids or "credentials" in node:
            raise _fail("節點識別重複或包含憑證")
        ids.add(node_id)
        types.add(node_type)
        if node_type == "tab":
            continue
        if node_type in CONFIG_TYPES:
            if "z" in node:
                raise _fail("設定節點不可屬於分頁")
            if node_type == "mqtt-broker":
                if node_id != BROKER_ID:
                    raise _fail("MQTT Broker 設定識別不正確")
            elif not _HEX16.match(node_id):
                raise _fail("閘道設定識別不正確")
            else:
                if (
                    node.get("clienttype") != "tcp"
                    or node.get("tcpHost") != host
                    or str(node.get("tcpPort")) != port
                ):
                    raise _fail("Modbus 閘道與設定不符")
                config_ids.append(node_id)
            continue
        if node.get("z") != tab_id:
            raise _fail("節點不屬於橋接分頁")
        if node_type == "function" and len(str(node.get("func", ""))) > 32_768:
            raise _fail("函式節點內容過大")
        if node_type in ("mqtt in", "mqtt out"):
            topic = str(node.get("topic", ""))
            if node.get("broker") != BROKER_ID or (
                topic and not topic.startswith(TOPIC_PREFIXES)
            ):
                raise _fail("MQTT 節點設定不正確")
        if node_type == "tcp request" and (
            node.get("server") != host or str(node.get("port")) != port
        ):
            raise _fail("RS-485 閘道與設定不符")
        if node_type.startswith("modbus-flex") and node.get("server") not in (
            modbus_clients
        ):
            raise _fail("Modbus 節點缺少閘道設定")
    if BROKER_ID not in ids:
        raise _fail("缺少 MQTT Broker 設定")
    return {"tabId": tab_id, "configIds": sorted(config_ids), "types": types}


def merge_flows(
    current: list[dict], incoming: list[dict], tab_id: str, stale_ids: set[str]
) -> list[dict]:
    """Replace only this bridge's tab and config nodes; keep everything else."""

    replaced = {n["id"] for n in incoming} | stale_ids | {tab_id}
    kept = [
        n
        for n in current
        if isinstance(n, dict) and n.get("id") not in replaced and n.get("z") != tab_id
    ]
    return kept + incoming


def apply_broker(flow: list[dict], broker: dict) -> list[dict]:
    result = copy.deepcopy(flow)
    for node in result:
        if node.get("id") == BROKER_ID:
            node["broker"] = broker["host"]
            node["port"] = str(broker["port"])
            if broker.get("username"):
                node["credentials"] = {
                    "user": broker["username"],
                    "password": broker.get("password", ""),
                }
    return result


def heartbeat_health(payload: Any, operation_id: str | None, now_ms: int) -> dict:
    """Interpret a bridge status message published by the flow."""

    if not isinstance(payload, dict):
        return {"mqtt": False, "gateway": False}
    if operation_id and payload.get("deployment") != operation_id:
        return {"mqtt": False, "gateway": False}
    ts = payload.get("ts")
    age = (
        payload.get("gateway", {}).get("lastRxAgeMs")
        if isinstance(payload.get("gateway"), dict)
        else None
    )
    fresh = isinstance(ts, (int, float)) and abs(now_ms - ts) < 120_000
    return {
        "mqtt": fresh,
        "gateway": fresh and isinstance(age, (int, float)) and age < _GATEWAY_FRESH_MS,
        "checkedAt": _now(),
    }


class NodeRedBridges:
    """Node-RED connection, gateway discovery and background deploy jobs."""

    def __init__(
        self,
        *,
        store: Any,
        session: Any,
        mqtt: Any,
        create_task: Callable[[Any, str], asyncio.Task],
        supervisor: Callable[[], SupervisorClient | None] | None = None,
        integration_version: str = "",
    ) -> None:
        self.store = store
        self.session = session
        self.mqtt = mqtt
        self.create_task = create_task
        self.supervisor = supervisor or (lambda: None)
        self.integration_version = integration_version
        self.data: dict = {"endpoint": {"mode": "auto"}, "bridges": {}, "jobs": []}
        self.jobs: dict[str, dict] = {}
        self.active: asyncio.Task | None = None
        # Heartbeats repeat every 10 s; allow the first gateway poll to land.
        self.verify_timeout = 35.0
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            self.data.update(stored)
        for job in self.data.get("jobs", []):
            if job.get("state") == "running":
                job.update(
                    state="interrupted",
                    error={
                        "code": "node_red_interrupted",
                        "message": "部署過程中主機重新啟動，請重新檢查狀態",
                    },
                )
        self._loaded = True

    async def _save(self) -> None:
        await self.store.async_save(self.data)

    # Node-RED connection -------------------------------------------------

    async def _locate(self) -> tuple[NodeRedClient | None, dict]:
        endpoint = self.data.get("endpoint") or {"mode": "auto"}
        if endpoint.get("mode") == "url":
            info = {"found": True, "kind": "url", "label": endpoint.get("url", "")}
            try:
                client = await connect_url(
                    self.session,
                    endpoint["url"],
                    username=endpoint.get("username", ""),
                    password=endpoint.get("password", ""),
                    verify_ssl=endpoint.get("verifySsl", True),
                )
            except NodeRedError as err:
                return None, {**info, "state": _state(err), "message": err.message}
            return client, {**info, "state": "ready"}
        supervisor = self.supervisor()
        if supervisor is not None:
            try:
                addon = find_node_red_addon(await supervisor.addons())
            except NodeRedError:
                addon = None
            if addon is not None:
                slug = str(addon.get("slug"))
                info = {
                    "found": True,
                    "kind": "addon",
                    "label": f"Node-RED add-on {addon.get('version', '')}".strip(),
                    "addonSlug": slug,
                }
                try:
                    details = await supervisor.addon_info(slug)
                    if details.get("state") != "started":
                        return None, {
                            **info,
                            "state": "stopped",
                            "canStart": True,
                            "message": "Node-RED add-on 尚未啟動",
                        }
                    endpoint_info = await supervisor.ingress_endpoint(details)
                    client = NodeRedClient(self.session, endpoint_info)
                    await client.settings()
                except NodeRedError as err:
                    return None, {**info, "state": _state(err), "message": err.message}
                return client, {
                    **info,
                    "state": "ready",
                    "hostNetwork": bool(details.get("host_network")),
                }
        for url in LOCAL_CANDIDATES:
            try:
                client = await connect_url(self.session, url, kind="local")
            except NodeRedError as err:
                if err.code == "node_red_auth_required":
                    return None, {
                        "found": True,
                        "kind": "local",
                        "label": url,
                        "state": "auth_required",
                        "message": err.message,
                    }
                continue
            return client, {
                "found": True,
                "kind": "local",
                "label": url,
                "state": "ready",
            }
        return None, {
            "found": False,
            "kind": "addon" if supervisor is not None else "local",
            "state": "not_installed" if supervisor is not None else "not_found",
            "message": "此主機尚未安裝 Node-RED add-on"
            if supervisor is not None
            else "找不到 Node-RED，請輸入 Node-RED 網址",
        }

    async def _ready_client(self) -> tuple[NodeRedClient, dict]:
        client, info = await self._locate()
        if client is None:
            raise NodeRedError(
                "node_red_not_ready", info.get("message") or "Node-RED 尚未就緒"
            )
        return client, info

    def _mqtt_config(self) -> dict | None:
        return self.mqtt.config() if self.mqtt is not None else None

    async def _suggest_broker(self, info: dict) -> dict | None:
        config = self._mqtt_config()
        if not config or not config.get("host"):
            return None
        host, port = str(config["host"]), int(config.get("port") or 1883)
        suggestion = {"host": host, "port": port, "source": "ha"}
        supervisor = self.supervisor()
        # An add-on on the host network cannot resolve hassio hostnames such
        # as core-mosquitto; use the broker add-on's published host port.
        if (
            supervisor is not None
            and info.get("kind") == "addon"
            and info.get("hostNetwork")
            and "." not in host
            and "-" in host
        ):
            with contextlib.suppress(NodeRedError):
                details = await supervisor.addon_info(host.replace("-", "_"))
                mapped = (details.get("network") or {}).get(f"{port}/tcp")
                if mapped:
                    suggestion = {
                        "host": "127.0.0.1",
                        "port": int(mapped),
                        "source": "addon-port",
                    }
        return suggestion

    async def _broker_settings(self, requested: Any, info: dict) -> dict:
        config = self._mqtt_config() or {}
        requested = requested if isinstance(requested, dict) else {}
        if requested.get("mode") == "custom":
            try:
                host = str(requested.get("host", "")).strip()
                port = int(requested.get("port"))
            except (TypeError, ValueError):
                raise _fail(
                    "MQTT Broker 設定不正確", "node_red_invalid_broker"
                ) from None
            if (
                not host
                or len(host) > 253
                or not 1 <= port <= 65535
                or any(c in host for c in "/@ ")
            ):
                raise _fail("MQTT Broker 設定不正確", "node_red_invalid_broker")
            broker = {"host": host, "port": port}
        else:
            suggestion = await self._suggest_broker(info)
            if suggestion is None:
                raise _fail(
                    "Home Assistant 尚未設定 MQTT，請先完成 MQTT Broker 設定",
                    "node_red_mqtt_missing",
                )
            broker = {"host": suggestion["host"], "port": suggestion["port"]}
        if requested.get("useHaCredentials", True) and config.get("username"):
            broker["username"] = config["username"]
            broker["password"] = config.get("password", "")
        return broker

    async def _heartbeats(self, wait: float = 1.0) -> dict[str, Any]:
        """Collect retained bridge status messages (best effort)."""

        if self.mqtt is None or not await self.mqtt.available():
            return {}
        received: dict[str, Any] = {}

        def handle(topic: str, payload: str) -> None:
            parts = topic.split("/")
            if len(parts) == 4 and parts[3] == "status":
                with contextlib.suppress(ValueError):
                    received[parts[2]] = json.loads(payload)

        unsubscribe = await self.mqtt.subscribe("rexlite/nodered/+/status", handle)
        try:
            await asyncio.sleep(wait)
        finally:
            unsubscribe()
        return received

    async def status(self) -> dict:
        await self.load()
        client, info = await self._locate()
        modules: dict[str, bool] = {}
        if client is not None:
            with contextlib.suppress(NodeRedError):
                settings = await client.settings()
                info["version"] = str(settings.get("version", ""))
            with contextlib.suppress(NodeRedError):
                types = await client.node_types()
                modules["node-red-contrib-modbus"] = "modbus-flex-getter" in types
        config = self._mqtt_config()
        heartbeats = await self._heartbeats()
        now_ms = int(time.time() * 1000)
        bridges = []
        for bridge in self.data.get("bridges", {}).values():
            health = (
                heartbeat_health(heartbeats.get(bridge["bridgeId"]), None, now_ms)
                if bridge["bridgeId"] in heartbeats
                else None
            )
            bridges.append({**bridge, "health": health})
        job = self.data["jobs"][-1] if self.data.get("jobs") else None
        if job and job.get("jobId") in self.jobs:
            job = self.jobs[job["jobId"]]
        return {
            "api": BRIDGE_API_VERSION,
            "integrationVersion": self.integration_version,
            "nodeRed": {**info, "modules": modules},
            "mqtt": {
                "configured": bool(config and config.get("host")),
                "host": config.get("host") if config else None,
                "port": config.get("port") if config else None,
                "hasCredentials": bool(config and config.get("username")),
                "suggestedBroker": await self._suggest_broker(info),
            },
            "endpoint": _public_endpoint(self.data.get("endpoint") or {}),
            "bridges": sorted(bridges, key=lambda b: (b["template"], b["gatewayHost"])),
            "job": _public_job(job) if job else None,
        }

    async def configure(self, msg: dict) -> dict:
        await self.load()
        if msg.get("mode") == "auto":
            self.data["endpoint"] = {"mode": "auto"}
        else:
            endpoint = {
                "mode": "url",
                "url": msg.get("url", ""),
                "username": msg.get("username", ""),
                "password": msg.get("password", ""),
                "verifySsl": bool(msg.get("verifySsl", True)),
            }
            # Keep the stored password when only the username/URL is resent.
            previous = self.data.get("endpoint") or {}
            if (
                not endpoint["password"]
                and previous.get("username") == endpoint["username"]
            ):
                endpoint["password"] = previous.get("password", "")
            client = await connect_url(
                self.session,
                endpoint["url"],
                username=endpoint["username"],
                password=endpoint["password"],
                verify_ssl=endpoint["verifySsl"],
            )
            endpoint["url"] = client.endpoint.base_url
            self.data["endpoint"] = endpoint
        await self._save()
        return await self.status()

    async def start_addon(self) -> dict:
        await self.load()
        supervisor = self.supervisor()
        if supervisor is None:
            raise NodeRedError(
                "node_red_not_supervised",
                "此主機不是 Home Assistant OS，請手動啟動 Node-RED",
            )
        addon = find_node_red_addon(await supervisor.addons())
        if addon is None:
            raise NodeRedError(
                "node_red_not_installed", "此主機尚未安裝 Node-RED add-on"
            )
        slug = str(addon["slug"])
        # Stay inside the 120 s Go core budget: start (≤60 s), then ~40 s of
        # polling while the container and Node-RED come up.
        await supervisor.start_addon(slug)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if (await supervisor.addon_info(slug)).get("state") == "started":
                client, _info = await self._locate()
                if client is not None:
                    break
            await asyncio.sleep(2)
        return await self.status()

    # Gateway discovery ---------------------------------------------------

    async def scan(self, msg: dict) -> dict:
        await self.load()
        template = msg["template"]
        try:
            host = gateway_host(msg["host"])
            port = gateway_port(msg["port"])
            started = time.monotonic()
            if template == SOMFY_TEMPLATE:
                devices = await somfy_scan(host, port)
            else:
                devices = await modbus_scan(
                    host, port, max_unit=int(msg.get("maxUnit", 16))
                )
        except GatewayError as err:
            raise NodeRedError(err.code, err.message) from None
        known = {
            d["address"]: {"name": d["name"], "key": d["key"]}
            for bridge in self.data.get("bridges", {}).values()
            if bridge["template"] == template
            and bridge["gatewayHost"] == host
            and bridge["gatewayPort"] == port
            for d in bridge["devices"]
        }
        return {
            "template": template,
            "gatewayHost": host,
            "gatewayPort": port,
            "devices": [{**d, "existing": known.get(d["address"])} for d in devices],
            "durationMs": int((time.monotonic() - started) * 1000),
            "scannedAt": _now(),
        }

    async def test(self, msg: dict) -> dict:
        if msg["template"] != SOMFY_TEMPLATE:
            raise NodeRedError("node_red_invalid_device", "此設備不支援測試動作")
        try:
            await somfy_send(
                gateway_host(msg["host"]),
                gateway_port(msg["port"]),
                motor_id(msg["address"]),
                msg["action"],
            )
        except GatewayError as err:
            raise NodeRedError(err.code, err.message) from None
        return {"ok": True, "sentAt": _now()}

    # Background jobs -----------------------------------------------------

    def job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id) or next(
            (j for j in self.data.get("jobs", []) if j.get("jobId") == job_id), None
        )
        if job is None:
            raise NodeRedError(
                "node_red_job_not_found", "找不到部署作業，請重新檢查狀態"
            )
        return _public_job(job)

    async def start_deploy(self, msg: dict) -> dict:
        await self.load()
        bridge = validate_bridge(msg.get("bridge"))
        shape = validate_flow(msg.get("flow"), bridge)
        flow = msg["flow"]
        return self._start(
            "deploy",
            msg["operationId"],
            lambda job: self._deploy(job, bridge, shape, flow, msg.get("broker")),
        )

    async def start_remove(self, msg: dict) -> dict:
        await self.load()
        bridge_id = msg["bridgeId"]
        if bridge_id not in self.data.get("bridges", {}):
            raise NodeRedError("node_red_bridge_not_found", "找不到這個閘道的部署")
        return self._start(
            "remove", msg["operationId"], lambda job: self._remove(job, bridge_id)
        )

    def _start(self, kind: str, operation_id: str, run: Callable) -> dict:
        if not _UUID.match(operation_id):
            raise _fail("作業識別碼不正確", "node_red_invalid_operation")
        existing = self.jobs.get(operation_id)
        if existing is not None:
            return _public_job(existing)
        if self.active is not None and not self.active.done():
            raise NodeRedError("node_red_busy", "Node-RED 正在處理其他部署，請稍後再試")
        job = {
            "jobId": operation_id,
            "kind": kind,
            "state": "running",
            "stage": "connect",
            "message": "",
            "startedAt": _now(),
            "finishedAt": None,
            "result": None,
            "error": None,
        }
        self.jobs[operation_id] = job
        # A retry after an HA restart replaces the interrupted record.
        history = [
            j for j in self.data.get("jobs", []) if j.get("jobId") != operation_id
        ]
        self.data["jobs"] = [*history, job][-10:]
        self.active = self.create_task(self._run(job, run), f"REXLiTE Node-RED {kind}")
        return _public_job(job)

    async def _run(self, job: dict, run: Callable) -> None:
        await self._save()
        try:
            job["result"] = await run(job)
            job["state"] = "completed"
        except NodeRedError as err:
            job.update(state="failed", error={"code": err.code, "message": err.message})
        except Exception:  # noqa: BLE001 - the job record must always finish
            job.update(
                state="failed",
                error={
                    "code": "node_red_failed",
                    "message": "Node-RED 部署失敗，請重新檢查狀態",
                },
            )
        finally:
            job["finishedAt"] = _now()
            with contextlib.suppress(Exception):
                await self._save()

    async def _deploy(
        self, job: dict, bridge: dict, shape: dict, flow: list[dict], broker: Any
    ) -> dict:
        started_ms = int(time.time() * 1000)
        client, info = await self._ready_client()
        job["stage"] = "modules"
        types = await client.node_types()
        missing = sorted(
            {MODULES[t] for t in shape["types"] if t in MODULES and t not in types}
        )
        for module in missing:
            job["message"] = f"正在安裝 {module}"
            await client.install_module(module)
        if missing:
            types = await client.node_types()
            if any(t in MODULES and t not in types for t in shape["types"]):
                raise NodeRedError(
                    "node_red_install_failed", "Node-RED 節點安裝後仍無法使用"
                )
        job.update(stage="deploy", message="")
        settings = await self._broker_settings(broker, info)
        incoming = apply_broker(flow, settings)
        tab_id = shape["tabId"]
        previous = self.data["bridges"].get(tab_id) or {}
        stale = set(previous.get("configIds", [])) - set(shape["configIds"])
        for attempt in range(2):
            rev, current = await client.get_flows()
            merged = merge_flows(current, incoming, tab_id, stale)
            try:
                rev = await client.set_flows(merged, rev)
                break
            except NodeRedConflict:
                if attempt:
                    raise
        self.data["bridges"][tab_id] = {
            **bridge,
            "bridgeId": tab_id,
            "configIds": shape["configIds"],
            "broker": {"host": settings["host"], "port": settings["port"]},
            "operationId": job["jobId"],
            "deployedAt": _now(),
        }
        await self._save()
        job["stage"] = "verify"
        health = await self._wait_health(tab_id, job["jobId"], started_ms)
        return {
            "bridgeId": tab_id,
            "rev": rev,
            "installed": missing,
            "broker": {"host": settings["host"], "port": settings["port"]},
            "health": health,
        }

    async def _wait_health(self, tab_id: str, operation_id: str, since_ms: int) -> dict:
        """Wait for the new flow's heartbeat, preferably with gateway traffic."""

        if self.mqtt is None or not await self.mqtt.available():
            return {"mqtt": None, "gateway": None, "checkedAt": _now()}
        latest: dict = {"mqtt": False, "gateway": False}
        done = asyncio.Event()

        def handle(_topic: str, payload: str) -> None:
            nonlocal latest
            with contextlib.suppress(ValueError):
                health = heartbeat_health(
                    json.loads(payload), operation_id, int(time.time() * 1000)
                )
                if health["mqtt"]:
                    latest = health
                if health["gateway"]:
                    done.set()

        unsubscribe = await self.mqtt.subscribe(
            STATUS_TOPIC.format(bridge=tab_id), handle
        )
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), self.verify_timeout)
        finally:
            unsubscribe()
        return {**latest, "checkedAt": _now()}

    async def _remove(self, job: dict, bridge_id: str) -> dict:
        client, _info = await self._ready_client()
        bridge = self.data["bridges"][bridge_id]
        job["stage"] = "deploy"
        drop = set(bridge.get("configIds", [])) | {bridge_id}
        for attempt in range(2):
            rev, current = await client.get_flows()
            kept = [
                n
                for n in current
                if isinstance(n, dict)
                and n.get("id") not in drop
                and n.get("z") != bridge_id
            ]
            try:
                await client.set_flows(kept, rev)
                break
            except NodeRedConflict:
                if attempt:
                    raise
        del self.data["bridges"][bridge_id]
        await self._save()
        if self.mqtt is not None and await self.mqtt.available():
            with contextlib.suppress(Exception):
                await self.mqtt.publish(
                    STATUS_TOPIC.format(bridge=bridge_id), "", retain=True
                )
        return {"bridgeId": bridge_id, "removed": True}


def _state(err: NodeRedError) -> str:
    return {
        "node_red_auth_required": "auth_required",
        "node_red_auth_failed": "auth_failed",
        "node_red_auth_unsupported": "auth_unsupported",
    }.get(err.code, "unreachable")


def _public_endpoint(endpoint: dict) -> dict:
    if endpoint.get("mode") != "url":
        return {"mode": "auto"}
    return {
        "mode": "url",
        "url": endpoint.get("url", ""),
        "username": endpoint.get("username", ""),
        "hasPassword": bool(endpoint.get("password")),
        "verifySsl": endpoint.get("verifySsl", True),
    }


def _public_job(job: dict) -> dict:
    return {
        key: job.get(key)
        for key in (
            "jobId",
            "kind",
            "state",
            "stage",
            "message",
            "startedAt",
            "finishedAt",
            "result",
            "error",
        )
    }


class _HomeAssistantMqtt:
    """Thin adapter so the bridge logic stays testable without HA."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    def config(self) -> dict | None:
        entries = [
            e
            for e in self.hass.config_entries.async_entries("mqtt")
            if not e.disabled_by
        ]
        if not entries:
            return None
        data = {**entries[0].data}
        return {
            "host": data.get("broker"),
            "port": int(data.get("port") or 1883),
            "username": data.get("username") or "",
            "password": data.get("password") or "",
        }

    async def available(self) -> bool:
        if "mqtt" not in self.hass.config.components:
            return False
        from homeassistant.components import mqtt

        with contextlib.suppress(Exception):
            return bool(
                await asyncio.wait_for(mqtt.async_wait_for_mqtt_client(self.hass), 5)
            )
        return False

    async def subscribe(self, topic: str, handler: Callable[[str, str], None]):
        from homeassistant.components import mqtt
        from homeassistant.core import callback

        @callback
        def message(msg: Any) -> None:
            handler(msg.topic, msg.payload if isinstance(msg.payload, str) else "")

        return await mqtt.async_subscribe(self.hass, topic, message)

    async def publish(self, topic: str, payload: str, *, retain: bool) -> None:
        from homeassistant.components import mqtt

        await mqtt.async_publish(self.hass, topic, payload, retain=retain)


def register_node_red_bridge(hass: Any) -> NodeRedBridges:
    """Register admin-only `rexlite/nodered/*` commands once per HA instance."""

    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store

    from .const import INTEGRATION_VERSION

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]

    def supervisor() -> SupervisorClient | None:
        env = supervisor_from_env()
        return SupervisorClient(async_get_clientsession(hass), *env) if env else None

    bridges = hass.data[DATA_KEY] = NodeRedBridges(
        store=Store(hass, 1, STORE_KEY, private=True),
        session=async_get_clientsession(hass),
        mqtt=_HomeAssistantMqtt(hass),
        create_task=lambda coro, name: hass.async_create_background_task(coro, name),
        supervisor=supervisor,
        integration_version=INTEGRATION_VERSION,
    )

    def handler(name: str, schema: dict, run: Callable):
        @websocket_api.websocket_command(
            {vol.Required("type"): f"rexlite/nodered/{name}", **schema}
        )
        @websocket_api.require_admin
        @websocket_api.async_response
        async def command(hass: Any, connection: Any, msg: dict) -> None:
            try:
                result = await run(msg)
            except NodeRedError as err:
                connection.send_error(msg["id"], err.code, err.message)
            except Exception:  # noqa: BLE001 - never break the admin websocket
                connection.send_error(
                    msg["id"], "node_red_failed", "Node-RED 操作失敗，請重新檢查狀態"
                )
            else:
                connection.send_result(msg["id"], result)

        return command

    host = vol.All(str, vol.Length(min=7, max=15))
    port = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
    operation = vol.All(str, vol.Length(min=36, max=36))
    commands = (
        handler("status", {}, lambda msg: bridges.status()),
        handler(
            "configure",
            {
                vol.Required("mode"): vol.In(("auto", "url")),
                vol.Optional("url", default=""): vol.All(str, vol.Length(max=300)),
                vol.Optional("username", default=""): vol.All(str, vol.Length(max=128)),
                vol.Optional("password", default=""): vol.All(str, vol.Length(max=256)),
                vol.Optional("verifySsl", default=True): bool,
            },
            bridges.configure,
        ),
        handler("start_addon", {}, lambda msg: bridges.start_addon()),
        handler(
            "scan",
            {
                vol.Required("template"): vol.In(TEMPLATES),
                vol.Required("host"): host,
                vol.Required("port"): port,
                vol.Optional("maxUnit", default=16): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=32)
                ),
            },
            bridges.scan,
        ),
        handler(
            "test",
            {
                vol.Required("template"): vol.In(TEMPLATES),
                vol.Required("host"): host,
                vol.Required("port"): port,
                vol.Required("address"): vol.All(str, vol.Length(min=1, max=8)),
                vol.Required("action"): vol.In(("open", "close", "stop")),
            },
            bridges.test,
        ),
        handler(
            "deploy",
            {
                vol.Required("operationId"): operation,
                vol.Required("bridge"): dict,
                vol.Required("flow"): list,
                vol.Optional("broker", default={}): dict,
            },
            bridges.start_deploy,
        ),
        handler(
            "job",
            {vol.Required("jobId"): operation},
            lambda msg: _async_value(bridges.job(msg["jobId"])),
        ),
        handler(
            "remove",
            {
                vol.Required("operationId"): operation,
                vol.Required("bridgeId"): vol.All(str, vol.Match(_HEX16)),
            },
            bridges.start_remove,
        ),
    )
    for command in commands:
        websocket_api.async_register_command(hass, command)
    return bridges


async def _async_value(value: Any) -> Any:
    return value
