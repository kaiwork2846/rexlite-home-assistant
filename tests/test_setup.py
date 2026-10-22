"""Config-entry lifecycle tests without a Home Assistant installation."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "rexlite"
PACKAGE_NAME = "rexlite_setup_test"


class _ConfigEntry:
    @classmethod
    def __class_getitem__(cls, item: Any) -> type[_ConfigEntry]:
        return cls


class _ConfigEntryAuthFailed(Exception):
    """Stub Home Assistant permanent authentication error."""


class _ConfigEntryNotReady(Exception):
    """Stub Home Assistant temporary setup error."""


class _Platform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SWITCH = "switch"


class _DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, item: Any) -> type[_DataUpdateCoordinator]:
        return cls

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.hass = args[0]

    def async_set_updated_data(self, data: Any) -> None:
        self.data = data

    async def async_shutdown(self) -> None:
        return None


homeassistant = types.ModuleType("homeassistant")
homeassistant_config_entries = types.ModuleType("homeassistant.config_entries")
homeassistant_config_entries.ConfigEntry = _ConfigEntry
homeassistant_const = types.ModuleType("homeassistant.const")
homeassistant_const.Platform = _Platform
homeassistant_const.__version__ = "2026.8.0"
homeassistant_core = types.ModuleType("homeassistant.core")
homeassistant_core.HomeAssistant = object
homeassistant_exceptions = types.ModuleType("homeassistant.exceptions")
homeassistant_exceptions.ConfigEntryAuthFailed = _ConfigEntryAuthFailed
homeassistant_exceptions.ConfigEntryNotReady = _ConfigEntryNotReady
homeassistant_helpers = types.ModuleType("homeassistant.helpers")
homeassistant_config_validation = types.ModuleType(
    "homeassistant.helpers.config_validation"
)
homeassistant_config_validation.config_entry_only_config_schema = Mock()
homeassistant_helpers.config_validation = homeassistant_config_validation
homeassistant_aiohttp = types.ModuleType("homeassistant.helpers.aiohttp_client")
homeassistant_aiohttp.async_get_clientsession = lambda hass: object()
homeassistant_update_coordinator = types.ModuleType(
    "homeassistant.helpers.update_coordinator"
)
homeassistant_update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator

sys.modules["homeassistant"] = homeassistant
sys.modules["homeassistant.config_entries"] = homeassistant_config_entries
sys.modules["homeassistant.const"] = homeassistant_const
sys.modules["homeassistant.core"] = homeassistant_core
sys.modules["homeassistant.exceptions"] = homeassistant_exceptions
sys.modules["homeassistant.helpers"] = homeassistant_helpers
sys.modules["homeassistant.helpers.aiohttp_client"] = homeassistant_aiohttp
sys.modules["homeassistant.helpers.update_coordinator"] = (
    homeassistant_update_coordinator
)

aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientError = OSError
aiohttp.ClientSession = object
aiohttp.ClientWebSocketResponse = object
aiohttp.WSServerHandshakeError = OSError
sys.modules["aiohttp"] = aiohttp

spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    PACKAGE_PATH / "__init__.py",
    submodule_search_locations=[str(PACKAGE_PATH)],
)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load REXLiTE integration")
integration = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE_NAME] = integration
spec.loader.exec_module(integration)


class _Entry:
    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.data = {
            integration.CONF_AGENT_ID: "hub-1",
            integration.CONF_AGENT_AUTH_TOKEN: "secret-value",
            integration.CONF_GATEWAY_WS_URL: "wss://gateway.example/ws/agent",
            integration.CONF_HOME_ASSISTANT_URL: "http://127.0.0.1:8123",
        }
        self.options = {integration.CONF_REMOTE_ADMIN_ENABLED: True}
        self.runtime_data: Any = None
        self.unload_callbacks: list[Any] = []
        self.reauth_requests: list[Any] = []

    def async_on_unload(self, callback: Any) -> None:
        self.unload_callbacks.append(callback)

    def async_start_reauth(self, hass: Any) -> None:
        self.reauth_requests.append(hass)


class _ConfigEntriesManager:
    def __init__(self) -> None:
        self.forwarded: list[tuple[Any, Any]] = []
        self.unloaded: list[tuple[Any, Any]] = []

    async def async_forward_entry_setups(self, entry: Any, platforms: Any) -> None:
        self.forwarded.append((entry, platforms))

    async def async_unload_platforms(self, entry: Any, platforms: Any) -> bool:
        self.unloaded.append((entry, platforms))
        return True


class _Hass:
    def __init__(self) -> None:
        self.config_entries = _ConfigEntriesManager()


class _Coordinator:
    instances: list[_Coordinator] = []

    def __init__(self, hass: Any, entry: Any, session: Any, config: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.session = session
        self.config = config
        self.started = False
        self.shutdown_called = False
        self.__class__.instances.append(self)

    async def async_start(self) -> None:
        self.started = True

    async def async_shutdown(self) -> None:
        self.shutdown_called = True


class SetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _Coordinator.instances.clear()

    async def test_setup_validates_before_start_and_registers_cleanup(self) -> None:
        hass = _Hass()
        entry = _Entry()
        validations: list[dict[str, Any]] = []

        async def validate(session: Any, **kwargs: Any) -> None:
            validations.append({"session": session, **kwargs})

        session = object()
        with (
            patch.object(integration, "async_get_clientsession", return_value=session),
            patch.object(
                integration, "async_validate_gateway_credentials", side_effect=validate
            ),
            patch.object(integration, "REXLiTECoordinator", _Coordinator),
        ):
            result = await integration.async_setup_entry(hass, entry)

        self.assertTrue(result)
        self.assertEqual(len(validations), 1)
        self.assertEqual(validations[0]["agent_id"], "hub-1")
        self.assertEqual(validations[0]["token"], "secret-value")
        coordinator = _Coordinator.instances[0]
        self.assertTrue(coordinator.started)
        self.assertEqual(coordinator.config.entry_id, "entry-1")
        self.assertIs(entry.runtime_data, coordinator)
        self.assertEqual(entry.unload_callbacks, [coordinator.async_shutdown])
        self.assertEqual(hass.config_entries.forwarded[0][0], entry)

    async def test_rejected_credential_triggers_home_assistant_reauth(self) -> None:
        async def reject(*args: Any, **kwargs: Any) -> None:
            raise integration.InvalidAuthError

        with (
            patch.object(
                integration, "async_validate_gateway_credentials", side_effect=reject
            ),
            patch.object(integration, "REXLiTECoordinator", _Coordinator),
            self.assertRaises(_ConfigEntryAuthFailed),
        ):
            await integration.async_setup_entry(_Hass(), _Entry())

        self.assertEqual(_Coordinator.instances, [])

    async def test_temporary_failure_uses_home_assistant_setup_retry(self) -> None:
        async def unavailable(*args: Any, **kwargs: Any) -> None:
            raise integration.CannotConnectError

        with (
            patch.object(
                integration,
                "async_validate_gateway_credentials",
                side_effect=unavailable,
            ),
            patch.object(integration, "REXLiTECoordinator", _Coordinator),
            self.assertRaises(_ConfigEntryNotReady),
        ):
            await integration.async_setup_entry(_Hass(), _Entry())

        self.assertEqual(_Coordinator.instances, [])

    async def test_unload_delegates_to_platform_lifecycle(self) -> None:
        hass = _Hass()
        entry = _Entry()

        self.assertTrue(await integration.async_unload_entry(hass, entry))
        self.assertEqual(hass.config_entries.unloaded[0][0], entry)

    async def test_runtime_auth_failure_requests_supported_reauth_flow(self) -> None:
        hass = _Hass()
        entry = _Entry()
        config = integration.TunnelConfig(
            gateway_url="wss://gateway.example/ws/agent",
            agent_id="hub-1",
            auth_token="secret-value",
            home_assistant_url="http://127.0.0.1:8123",
            home_assistant_version="2026.1.0",
            remote_admin_enabled=True,
        )
        coordinator = integration.REXLiTECoordinator(hass, entry, object(), config)

        coordinator._handle_auth_failure()

        self.assertEqual(entry.reauth_requests, [hass])


if __name__ == "__main__":
    unittest.main()
