"""Thread border routers on the hub's LAN, for the MAX'Is Matter wizard.

Home Assistant's zeroconf already browses ``_meshcop._udp`` (the Thread
integration declares it), so the routers are read from that cache. Each one is
probed on the OpenThread REST port: a router that answers there (GL.iNet
GL-S20 and other OpenThread-based routers) can be added as an Open Thread
Border Router integration with ``http://<host>:8081``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

DATA_KEY = "rexlite_thread_border_routers"
OTBR_REST_PORT = 8081
PROBE_TIMEOUT = 3
# A cold zeroconf cache fills within seconds of listening for _meshcop._udp.
FRESH_SCAN_SECONDS = 4


def rest_host(addresses: list[str]) -> str:
    """The address to probe: a private IPv4 first, else a ULA IPv6.

    Public, loopback, link-local and multicast addresses are never probed.
    """
    ipv4: list[str] = []
    ipv6: list[str] = []
    for value in addresses:
        try:
            address = ip_address(value)
        except ValueError:
            continue
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            continue
        if address.version == 4:
            ipv4.append(str(address))
        else:
            ipv6.append(f"[{address}]")
    return (ipv4 or ipv6 or [""])[0]


def configured_otbr(entries) -> tuple[set[str], set[str]]:
    """Border agent ids and REST hosts of the hub's OTBR config entries."""
    agent_ids: set[str] = set()
    hosts: set[str] = set()
    for entry in entries:
        if entry.unique_id:
            agent_ids.add(str(entry.unique_id).lower())
        host = urlsplit(str(entry.data.get("url", ""))).hostname
        if host:
            hosts.add(f"[{host}]" if ":" in host else host)
    return agent_ids, hosts


def router_row(data, agent_ids: set[str], hosts: set[str]) -> dict:
    """One discovered router as the platform shows and adds it."""
    host = rest_host(list(data.addresses or []))
    agent_id = (data.border_agent_id or "").lower()
    return {
        "id": data.extended_address,
        "name": data.model_name or data.vendor_name or data.instance_name,
        "vendor": data.vendor_name,
        "model": data.model_name,
        "brand": data.brand,
        "network": data.network_name,
        "threadVersion": data.thread_version,
        "borderAgentId": data.border_agent_id,
        "host": host,
        "configured": bool(agent_id and agent_id in agent_ids)
        or bool(host and host in hosts),
    }


async def probe_rest(session, host: str) -> str:
    """``ok`` when the OpenThread REST API answers on the router, else ``closed``."""
    if not host:
        return "unreachable"
    import aiohttp

    url = f"http://{host}:{OTBR_REST_PORT}/node/ba-id"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
        ) as response:
            return "ok" if response.status == 200 else "closed"
    except (aiohttp.ClientError, TimeoutError):
        return "closed"


async def discover_routers(hass) -> list:
    """Routers in HA's zeroconf cache; listen briefly when the cache is empty."""
    from homeassistant.components import zeroconf
    from homeassistant.components.thread.discovery import (
        ThreadRouterDiscovery,
        async_read_zeroconf_cache,
    )

    aiozc = await zeroconf.async_get_async_instance(hass)
    found = {row.extended_address: row for row in async_read_zeroconf_cache(aiozc)}
    if found:
        return list(found.values())

    def discovered(key, data) -> None:
        found[key] = data

    discovery = ThreadRouterDiscovery(hass, discovered, lambda key: None)
    await discovery.async_start()
    try:
        await asyncio.sleep(FRESH_SCAN_SECONDS)
    finally:
        await discovery.async_stop()
    return list(found.values())


async def border_routers(hass) -> dict:
    """Discovered routers with their OTBR REST state and setup status."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    routers = await discover_routers(hass)
    agent_ids, hosts = configured_otbr(hass.config_entries.async_entries("otbr"))
    rows = [router_row(router, agent_ids, hosts) for router in routers]
    session = async_get_clientsession(hass)
    states = await asyncio.gather(*(probe_rest(session, row["host"]) for row in rows))
    for row, state in zip(rows, states, strict=True):
        row["rest"] = state
        row["restUrl"] = (
            f"http://{row['host']}:{OTBR_REST_PORT}" if state == "ok" else ""
        )
    rows.sort(key=lambda row: (row["rest"] != "ok", str(row["name"] or "")))
    return {"routers": rows, "scannedAt": datetime.now(UTC).isoformat()}


def register_thread_border_routers(hass) -> None:
    import voluptuous as vol
    from homeassistant.components import websocket_api

    if DATA_KEY in hass.data:
        return
    hass.data[DATA_KEY] = True

    @websocket_api.websocket_command(
        {vol.Required("type"): "rexlite/thread/border_routers"}
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def list_border_routers(hass, connection, msg):
        try:
            result = await border_routers(hass)
        except Exception:  # noqa: BLE001 - discovery must not interrupt the tunnel
            connection.send_error(
                msg["id"],
                "thread_discovery_failed",
                "Thread 邊界路由器搜尋失敗，請確認主機探索服務",
            )
            return
        connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, list_border_routers)
