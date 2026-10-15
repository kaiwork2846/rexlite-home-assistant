"""Bind HACS on this hub with a GitHub token held by the operation platform.

HACS's own config flow only offers GitHub's device login, which needs a GitHub
account on site. Dealers rarely have one, so the MAX'Is operation platform
binds HACS with a token of its own GitHub machine account instead: the same
config entry HACS's flow creates (``data={"token": …}``, ``experimental`` on),
created here, or, when HACS already has one (a reauth or a new token),
unloaded through HA, updated and set up again. HACS reloads itself from an
update listener that bypasses HA's setup lock, so updating a loaded entry and
reloading it as well would race into a failed unload.

The token is checked against GitHub first. It is never logged and never
returned; it is stored only in HACS's config entry, where HACS needs it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

DATA_KEY = "rexlite_hacs_bind"
HACS_DOMAIN = "hacs"
GITHUB_RATE_LIMIT_URL = "https://api.github.com/rate_limit"
# Classic and OAuth tokens (ghp_, gho_, …) and fine-grained tokens.
_TOKEN = re.compile(
    r"^(?:gh[pousr]_[A-Za-z0-9]{20,251}|github_pat_[A-Za-z0-9_]{20,251})$"
)


class HacsBindError(Exception):
    """User-facing failure; ``code`` is stable for the operation platform."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HacsBinder:
    """Create or update HACS's config entry. HA specifics are injected."""

    def __init__(
        self,
        *,
        hacs_installed: Callable[[], Awaitable[bool]],
        check_token: Callable[[str], Awaitable[None]],
        entries: Callable[[], list[Any]],
        add_entry: Callable[[str], Awaitable[str]],
        rebind: Callable[[Any, str], Awaitable[str]],
        abort_reauth: Callable[[], int],
    ) -> None:
        self._hacs_installed = hacs_installed
        self._check_token = check_token
        self._entries = entries
        self._add_entry = add_entry
        self._rebind = rebind
        self._abort_reauth = abort_reauth

    async def bind(self, token: Any) -> dict[str, Any]:
        token = token.strip() if isinstance(token, str) else ""
        if not _TOKEN.match(token):
            raise HacsBindError("hacs_token_invalid", "GitHub 權杖格式不正確")
        if not await self._hacs_installed():
            raise HacsBindError("hacs_not_installed", "此中樞主機未安裝 HACS")
        await self._check_token(token)

        entries = self._entries()
        if not entries:
            state = await self._add_entry(token)
            return {"bound": True, "created": True, "state": state}

        entry = entries[0]
        if getattr(entry, "disabled_by", None):
            raise HacsBindError("hacs_disabled", "HACS 已在中樞主機停用")
        # A reauth flow waits for a GitHub login that is no longer needed.
        self._abort_reauth()
        state = _state(entry)
        if entry.data.get("token") != token or state != "loaded":
            state = await self._rebind(entry, token)
        return {"bound": True, "created": False, "state": state}


def _state(entry: Any) -> str:
    state = getattr(entry, "state", None)
    return str(getattr(state, "value", state) or "")


def github_token_error(status: int, remaining: Any) -> HacsBindError | None:
    """Map GitHub's ``/rate_limit`` answer for a token to a bind failure."""

    if status == 401:
        return HacsBindError("hacs_token_invalid", "GitHub 權杖無效或已撤銷")
    if status in (403, 429) or (
        status == 200 and isinstance(remaining, int) and remaining <= 0
    ):
        return HacsBindError("hacs_rate_limited", "GitHub 權杖已達使用上限，請稍後再試")
    if status != 200:
        return HacsBindError(
            "hacs_github_unavailable", f"GitHub 暫時無法使用（{status}），請稍後再試"
        )
    return None


def register_hacs_bind(hass: Any) -> HacsBinder:
    """Register the admin-only ``rexlite/hacs/bind`` command once per HA instance."""

    from types import MappingProxyType

    import aiohttp
    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.config_entries import (
        SOURCE_IGNORE,
        SOURCE_REAUTH,
        SOURCE_USER,
        ConfigEntry,
        OperationNotAllowed,
    )
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.loader import IntegrationNotFound, async_get_integration

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]

    async def hacs_installed() -> bool:
        try:
            await async_get_integration(hass, HACS_DOMAIN)
        except IntegrationNotFound:
            return False
        return True

    async def check_token(token: str) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "REXLiTE-AI",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with async_get_clientsession(hass).get(
                GITHUB_RATE_LIMIT_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                body = (
                    await response.json(content_type=None)
                    if response.status == 200
                    else {}
                )
                status = response.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HacsBindError(
                "hacs_github_unreachable", "中樞主機無法連線 GitHub"
            ) from err
        core = ((body or {}).get("resources") or {}).get("core") or {}
        remaining = core.get("remaining") if status == 200 else None
        if (error := github_token_error(status, remaining)) is not None:
            raise error

    def entries() -> list[Any]:
        return [
            entry
            for entry in hass.config_entries.async_entries(HACS_DOMAIN)
            if entry.source != SOURCE_IGNORE
        ]

    async def add_entry(token: str) -> str:
        # What HACS's own flow creates after the device login (config_flow.py).
        entry = ConfigEntry(
            data={"token": token},
            discovery_keys=MappingProxyType({}),
            domain=HACS_DOMAIN,
            minor_version=1,
            options={"experimental": True},
            source=SOURCE_USER,
            subentries_data=None,
            title="",
            unique_id=None,
            version=1,
        )
        await hass.config_entries.async_add(entry)
        return _state(entry)

    async def rebind(entry: Any, token: str) -> str:
        # Unloading through HA also drops HACS's update listener, so the token
        # update below cannot start HACS's own reload behind HA's back.
        try:
            unloaded = await hass.config_entries.async_unload(entry.entry_id)
        except OperationNotAllowed as err:
            raise HacsBindError(
                "hacs_restart_required", "HACS 狀態異常，請先重新啟動中樞主機"
            ) from err
        if not unloaded:
            raise HacsBindError("hacs_busy", "HACS 正在處理工作，請稍後再試")
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "token": token}
        )
        await hass.config_entries.async_setup(entry.entry_id)
        return _state(entry)

    def abort_reauth() -> int:
        flows = [
            flow["flow_id"]
            for flow in hass.config_entries.flow.async_progress_by_handler(HACS_DOMAIN)
            if flow.get("context", {}).get("source") == SOURCE_REAUTH
        ]
        for flow_id in flows:
            hass.config_entries.flow.async_abort(flow_id)
        return len(flows)

    binder = hass.data[DATA_KEY] = HacsBinder(
        hacs_installed=hacs_installed,
        check_token=check_token,
        entries=entries,
        add_entry=add_entry,
        rebind=rebind,
        abort_reauth=abort_reauth,
    )

    # Plain ``str``: a length or pattern rule would echo the token in HA's
    # invalid_format message; the binder validates it instead.
    @websocket_api.websocket_command(
        {vol.Required("type"): "rexlite/hacs/bind", vol.Required("token"): str}
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def bind(hass: Any, connection: Any, msg: dict) -> None:
        try:
            result = await binder.bind(msg["token"])
        except HacsBindError as err:
            connection.send_error(msg["id"], err.code, err.message)
        except Exception:  # noqa: BLE001 - never break the admin websocket
            connection.send_error(
                msg["id"], "hacs_bind_failed", "HACS 綁定失敗，請稍後再試"
            )
        else:
            connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, bind)
    return binder
