"""Clients for the Node-RED Admin API and the Supervisor add-on API.

The official Node-RED add-on runs Node-RED without adminAuth behind nginx:
its ingress server only accepts the Supervisor, and the direct port asks for
Home Assistant credentials. Home Assistant Core already holds a Supervisor
token, so an ingress session reaches the Admin API without asking the
installer for any password. Plain Node-RED installs are reached by URL.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

NODE_RED_ADDON_SLUG = "a0d7b954_nodered"
LOCAL_CANDIDATES = ("http://127.0.0.1:1880", "http://172.17.0.1:1880")


class NodeRedError(Exception):
    """User-facing failure; ``code`` is stable for the operation platform."""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class NodeRedConflict(NodeRedError):
    """The flows changed between read and deploy (Admin API v2 rev check)."""


def _timeout(seconds: float):
    try:
        import aiohttp
    except ImportError:  # unit tests use a duck-typed session
        return seconds
    factory = getattr(aiohttp, "ClientTimeout", None)
    return factory(total=seconds) if factory else seconds


async def _send(
    session: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
    seconds: float = 20,
    verify_ssl: bool = True,
) -> tuple[int, Any]:
    kwargs: dict[str, Any] = {"headers": headers or {}, "timeout": _timeout(seconds)}
    if json_body is not None:
        kwargs["json"] = json_body
    if form is not None:
        kwargs["data"] = form
    if not verify_ssl:
        kwargs["ssl"] = False
    try:
        async with session.request(method, url, **kwargs) as response:
            text = await response.text()
            status = response.status
    except NodeRedError:
        raise
    except Exception as err:  # noqa: BLE001 - network errors become one code
        raise NodeRedError(
            "node_red_unreachable", "無法連線到 Node-RED，請確認服務已啟動"
        ) from err
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = text
    return status, payload


def validate_node_red_url(value: object) -> str:
    """Only LAN/loopback Node-RED URLs; no credentials, query or fragment."""

    url = urlsplit(str(value).strip())
    host = url.hostname or ""
    if (
        url.scheme not in {"http", "https"}
        or not host
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise NodeRedError("node_red_invalid_url", "Node-RED 網址格式不正確")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Docker service names and mDNS hosts stay on the local network.
        if "." in host and not host.endswith(".local"):
            raise NodeRedError(
                "node_red_invalid_url", "Node-RED 網址需為區網 IP 或本機名稱"
            ) from None
    else:
        if not (address.is_private or address.is_loopback) or address.is_link_local:
            raise NodeRedError(
                "node_red_invalid_url", "Node-RED 網址需為區網 IP 或本機名稱"
            )
    try:
        port = url.port
    except ValueError:
        raise NodeRedError("node_red_invalid_url", "Node-RED 網址埠號不正確") from None
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((url.scheme, netloc, url.path.rstrip("/"), "", ""))


@dataclass(slots=True)
class NodeRedEndpoint:
    """Where and how to reach one Node-RED Admin API."""

    kind: str  # addon | url | local
    base_url: str
    label: str
    headers: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = True
    addon: dict[str, Any] | None = None


class NodeRedClient:
    def __init__(self, session: Any, endpoint: NodeRedEndpoint) -> None:
        self.session = session
        self.endpoint = endpoint

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        seconds: float = 20,
    ) -> tuple[int, Any]:
        return await _send(
            self.session,
            method,
            self.endpoint.base_url + path,
            headers={**self.endpoint.headers, **(headers or {})},
            json_body=json_body,
            seconds=seconds,
            verify_ssl=self.endpoint.verify_ssl,
        )

    async def settings(self) -> dict:
        status, payload = await self.request("GET", "/settings")
        if status in (401, 403):
            raise NodeRedError(
                "node_red_auth_required", "Node-RED 需要管理帳號密碼", status=status
            )
        if status != 200 or not isinstance(payload, dict):
            raise NodeRedError(
                "node_red_unreachable", "此網址不是可用的 Node-RED", status=status
            )
        return payload

    async def get_flows(self) -> tuple[str, list[dict]]:
        status, payload = await self.request(
            "GET", "/flows", headers={"Node-RED-API-Version": "v2"}
        )
        if (
            status != 200
            or not isinstance(payload, dict)
            or not isinstance(payload.get("flows"), list)
        ):
            raise NodeRedError(
                "node_red_unreachable", "無法讀取 Node-RED 流程", status=status
            )
        return str(payload.get("rev", "")), payload["flows"]

    async def set_flows(
        self, flows: list[dict], rev: str, deployment_type: str = "flows"
    ) -> str:
        status, payload = await self.request(
            "POST",
            "/flows",
            json_body={"flows": flows, "rev": rev},
            headers={
                "Node-RED-API-Version": "v2",
                "Node-RED-Deployment-Type": deployment_type,
            },
            seconds=60,
        )
        if status == 409:
            raise NodeRedConflict(
                "node_red_conflict", "Node-RED 流程剛被修改，請重試", status=status
            )
        if status not in (200, 204):
            detail = payload.get("message") if isinstance(payload, dict) else ""
            raise NodeRedError(
                "node_red_deploy_rejected",
                f"Node-RED 拒絕部署{f'：{detail}' if detail else ''}",
                status=status,
            )
        return str(payload.get("rev", "")) if isinstance(payload, dict) else ""

    async def node_types(self) -> dict[str, str]:
        """Installed node type -> providing module name."""

        status, payload = await self.request(
            "GET", "/nodes", headers={"Accept": "application/json"}
        )
        if status != 200 or not isinstance(payload, list):
            raise NodeRedError(
                "node_red_unreachable", "無法讀取 Node-RED 節點清單", status=status
            )
        types: dict[str, str] = {}
        for node_set in payload:
            if not isinstance(node_set, dict) or node_set.get("enabled") is False:
                continue
            for node_type in node_set.get("types") or []:
                types[str(node_type)] = str(node_set.get("module", ""))
        return types

    async def install_module(self, module: str) -> None:
        status, payload = await self.request(
            "POST", "/nodes", json_body={"module": module}, seconds=600
        )
        if status != 200:
            detail = payload.get("message") if isinstance(payload, dict) else ""
            raise NodeRedError(
                "node_red_install_failed",
                f"Node-RED 無法安裝 {module}"
                f"{f'：{detail}' if detail else ''}，請確認案場可連網",
                status=status,
            )


async def connect_url(
    session: Any,
    url: str,
    *,
    username: str = "",
    password: str = "",
    verify_ssl: bool = True,
    kind: str = "url",
) -> NodeRedClient:
    """Resolve Node-RED auth for a URL: none, adminAuth token or HTTP Basic."""

    base = validate_node_red_url(url)
    endpoint = NodeRedEndpoint(
        kind=kind, base_url=base, label=base, verify_ssl=verify_ssl
    )
    status, payload = await _send(
        session, "GET", f"{base}/auth/login", seconds=8, verify_ssl=verify_ssl
    )
    if status == 200 and isinstance(payload, dict):
        if payload.get("type") == "credentials":
            if not username:
                raise NodeRedError(
                    "node_red_auth_required", "Node-RED 需要管理帳號密碼"
                )
            status, token = await _send(
                session,
                "POST",
                f"{base}/auth/token",
                form={
                    "client_id": "node-red-admin",
                    "grant_type": "password",
                    "scope": "*",
                    "username": username,
                    "password": password,
                },
                seconds=15,
                verify_ssl=verify_ssl,
            )
            if (
                status != 200
                or not isinstance(token, dict)
                or not token.get("access_token")
            ):
                raise NodeRedError("node_red_auth_failed", "Node-RED 帳號或密碼錯誤")
            endpoint.headers["Authorization"] = f"Bearer {token['access_token']}"
        elif payload.get("type"):
            raise NodeRedError(
                "node_red_auth_unsupported", "此 Node-RED 使用不支援的登入方式"
            )
    elif status == 401:
        # The add-on's direct port asks nginx for HA credentials (HTTP Basic).
        if not username:
            raise NodeRedError("node_red_auth_required", "Node-RED 需要登入帳號密碼")
        basic = base64.b64encode(f"{username}:{password}".encode()).decode()
        endpoint.headers["Authorization"] = f"Basic {basic}"
    else:
        raise NodeRedError(
            "node_red_unreachable", "此網址不是可用的 Node-RED", status=status
        )
    client = NodeRedClient(session, endpoint)
    try:
        await client.settings()
    except NodeRedError as err:
        if err.code == "node_red_auth_required" and username:
            raise NodeRedError(
                "node_red_auth_failed", "Node-RED 帳號或密碼錯誤"
            ) from None
        raise
    return client


def supervisor_from_env() -> tuple[str, str] | None:
    host = os.environ.get("SUPERVISOR", "").strip()
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    return (host, token) if host and token else None


class SupervisorClient:
    def __init__(self, session: Any, host: str, token: str) -> None:
        self.session = session
        self.host = host
        self.token = token

    async def call(
        self, method: str, path: str, *, json_body: Any = None, seconds: float = 20
    ) -> Any:
        status, payload = await _send(
            self.session,
            method,
            f"http://{self.host}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            json_body=json_body,
            seconds=seconds,
        )
        if (
            status != 200
            or not isinstance(payload, dict)
            or payload.get("result") != "ok"
        ):
            raise NodeRedError(
                "node_red_supervisor_failed",
                "Home Assistant Supervisor 無法完成要求",
                status=status,
            )
        return payload.get("data")

    async def addons(self) -> list[dict]:
        data = await self.call("GET", "/addons")
        addons = data.get("addons") if isinstance(data, dict) else None
        return [a for a in addons or [] if isinstance(a, dict)]

    async def addon_info(self, slug: str) -> dict:
        data = await self.call("GET", f"/addons/{slug}/info")
        return data if isinstance(data, dict) else {}

    async def start_addon(self, slug: str) -> None:
        await self.call("POST", f"/addons/{slug}/start", seconds=60)

    async def ingress_endpoint(self, info: dict) -> NodeRedEndpoint:
        entry = str(info.get("ingress_entry") or info.get("ingress_url") or "")
        token = entry.rstrip("/").rsplit("/", 1)[-1]
        if not info.get("ingress") or not token:
            raise NodeRedError(
                "node_red_unreachable", "Node-RED add-on 未開放 ingress 連線"
            )
        data = await self.call("POST", "/ingress/session")
        session = data.get("session") if isinstance(data, dict) else None
        if not session:
            raise NodeRedError(
                "node_red_supervisor_failed", "無法建立 Node-RED 管理連線"
            )
        return NodeRedEndpoint(
            kind="addon",
            base_url=f"http://{self.host}/ingress/{token}",
            label=f"Node-RED add-on {info.get('version', '')}".strip(),
            headers={"Cookie": f"ingress_session={session}"},
            addon={
                "slug": info.get("slug"),
                "version": info.get("version"),
                "hostNetwork": bool(info.get("host_network")),
            },
        )


def find_node_red_addon(addons: list[dict]) -> dict | None:
    """Prefer the official add-on, then any installed Node-RED add-on."""

    def matches(addon: dict) -> bool:
        slug = str(addon.get("slug", "")).lower()
        name = str(addon.get("name", "")).lower()
        return (
            slug == NODE_RED_ADDON_SLUG
            or slug.endswith("_nodered")
            or ("node-red" in name or "nodered" in slug)
        )

    candidates = [a for a in addons if matches(a)]
    candidates.sort(
        key=lambda a: (
            a.get("slug") != NODE_RED_ADDON_SLUG,
            a.get("state") != "started",
        )
    )
    return candidates[0] if candidates else None
