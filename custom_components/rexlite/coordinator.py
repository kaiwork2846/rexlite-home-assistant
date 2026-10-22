"""State coordinator for the REXLiTE integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .runtime import REXLiTETunnelClient, RuntimeState, TunnelConfig

_LOGGER = logging.getLogger(__name__)

# Lets the reload acknowledgement leave before the reload closes the tunnel.
_SELF_RELOAD_DELAY = 1.0


class REXLiTECoordinator(DataUpdateCoordinator[RuntimeState]):
    """Bridge push-based tunnel state into Home Assistant entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: ClientSession,
        config: TunnelConfig,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="REXLiTE",
            update_interval=None,
        )
        self.config = config
        self._entry = entry
        self.client = REXLiTETunnelClient(
            session,
            config,
            self._handle_state,
            lambda coroutine, name: entry.async_create_background_task(
                hass, coroutine, name=name
            ),
            self._handle_auth_failure,
            detached_task_factory=lambda coroutine, name: (
                hass.async_create_background_task(coroutine, name)
            ),
            self_reload_callback=self._schedule_self_reload,
        )
        self.async_set_updated_data(self.client.state)

    async def async_start(self) -> None:
        """Start the long-running tunnel client."""

        await self.client.async_start()

    async def async_shutdown(self) -> None:
        """Stop all runtime tasks."""

        await self.client.async_stop()
        await super().async_shutdown()

    async def async_set_remote_admin(self, enabled: bool) -> None:
        """Update the control gate while preserving the health connection."""

        self.config = replace(self.config, remote_admin_enabled=enabled)
        await self.client.async_set_remote_admin(enabled)

    def _handle_state(self, state: RuntimeState) -> None:
        self.async_set_updated_data(state)

    def _handle_auth_failure(self) -> None:
        """Request one Home Assistant reauthentication flow."""

        self._entry.async_start_reauth(self.hass)

    def _schedule_self_reload(self) -> None:
        """Reload this entry once the tunnel has delivered its reply."""

        self.hass.async_create_background_task(
            self._async_self_reload(), f"REXLiTE reload {self._entry.entry_id}"
        )

    async def _async_self_reload(self) -> None:
        await asyncio.sleep(_SELF_RELOAD_DELAY)
        entry_id = self._entry.entry_id
        if self.hass.config_entries.async_get_entry(entry_id) is not None:
            self.hass.config_entries.async_schedule_reload(entry_id)
