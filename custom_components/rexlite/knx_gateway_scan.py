"""Read-only KNXnet/IP gateway discovery.

This sends the same search requests as Home Assistant's KNX config flow but never
starts that flow, so it works whether or not KNX is configured and also reports
routing-only routers. Nothing on the KNX bus or in HA's configuration changes.
"""

from __future__ import annotations

from typing import Any

SCAN_SECONDS = 3


class GatewayScanUnavailable(Exception):
    """xknx is not installed on this host."""


def _scanner_types() -> tuple[Any, Any]:
    from xknx import XKNX
    from xknx.io.gateway_scanner import GatewayScanner

    return XKNX, GatewayScanner


def gateway_summary(gateway: Any) -> dict:
    address = gateway.individual_address
    return {
        "name": str(gateway.name or ""),
        "ip": str(gateway.ip_addr),
        "port": int(gateway.port),
        "individualAddress": str(address) if address is not None else "",
        "supportsTunnelling": bool(gateway.supports_tunnelling),
        "supportsTunnellingTcp": bool(gateway.supports_tunnelling_tcp),
        "supportsRouting": bool(gateway.supports_routing),
        # None means a Core v1 device that cannot tell; HA treats it as plain.
        "tunnellingRequiresSecure": bool(gateway.tunnelling_requires_secure),
        "routingRequiresSecure": bool(gateway.routing_requires_secure),
    }


async def scan_gateways(hass: Any) -> list[dict]:
    """Return every KNX/IP interface and router that answers within the scan."""
    try:
        # KNX may not be loaded yet, so the first import can hit the disk.
        xknx_type, scanner_type = await hass.async_add_executor_job(_scanner_types)
    except ImportError as err:
        raise GatewayScanUnavailable from err
    scanner = scanner_type(
        xknx_type(), stop_on_found=0, timeout_in_seconds=SCAN_SECONDS
    )
    gateways = await scanner.scan()
    # Same order as HA's tunnel list.
    gateways.sort(
        key=lambda gateway: (
            gateway.individual_address.raw if gateway.individual_address else 0,
            gateway.ip_addr,
            gateway.port,
        )
    )
    return [gateway_summary(gateway) for gateway in gateways]
