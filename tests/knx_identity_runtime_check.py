"""Check writer identities against real, offline HA KNX entity constructors.

Only the platform setup context is supplied; no device connection is started and
no state or control telegram is sent. Requires official HA and KNX requirements.
"""

import asyncio
import importlib
import importlib.util
import sys
import tempfile
import types
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from homeassistant.components.knx import CONFIG_SCHEMA
from homeassistant.components.knx import entity as knx_entity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from xknx import XKNX
from xknx.telegram.address import GroupAddress, GroupAddressType

BASE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_real_identity")
package.__path__ = [str(BASE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + ".knx_project_deployment", BASE / "knx_project_deployment.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


async def check_registry_retirement(hass: HomeAssistant) -> None:
    """Exercise real registry enums, restored ghost removal and reversible updates."""
    registry = er.async_get(hass)
    config_entry = ConfigEntry(
        domain="knx",
        title="Offline retirement fixture",
        unique_id=None,
        version=1,
        minor_version=1,
        source="user",
        data={},
        options={},
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    with patch.object(
        hass,
        "config_entries",
        types.SimpleNamespace(async_get_entry=lambda entry_id: config_entry),
    ):
        entry = registry.async_get_or_create(
            "switch",
            "knx",
            "retirement-switch",
            suggested_object_id="retirement_switch",
            config_entry=config_entry,
        )
    registry.async_update_entity(entry.entity_id, name="User customized lamp")
    entry = registry.async_get(entry.entity_id)
    entry.write_unavailable_state(hass)
    assert hass.states.get(entry.entity_id).attributes.get("restored")
    writer = m.ProjectDeployer(hass)
    module = types.SimpleNamespace(
        config_yaml={},
        config_store=types.SimpleNamespace(data={"entities": {}}),
        group_address_entities={},
    )
    record = {
        "entityId": entry.entity_id,
        "platform": "switch",
        "uniqueId": entry.unique_id,
        "configEntryId": entry.config_entry_id,
        "addresses": ["1/0/1"],
        "beforeDisabled": None,
        "afterDisabled": "integration",
    }
    manifest = {
        "configEntryId": entry.config_entry_id,
        "identityMode": "custom",
        "entities": [{"platform": "switch", "uniqueId": entry.unique_id}],
    }
    with patch.object(writer, "_module", return_value=module):
        assert writer._counts(manifest)["loadedCount"] == 0
        assert writer._registry_change(record)
        retired = registry.async_get(entry.entity_id)
        assert retired.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert retired.name == "User customized lamp"
        assert hass.states.get(entry.entity_id) is None
        # Replaying after a process interruption is safe and preserves identity.
        assert writer._registry_change(record)
        assert writer._registry_change(record, reverse=True)
        assert registry.async_get(entry.entity_id).disabled_by is None
        registry.async_update_entity(
            entry.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
        assert not writer._registry_change(record)
        assert (
            registry.async_get(entry.entity_id).disabled_by
            is er.RegistryEntryDisabler.USER
        )
    print("Real HA", __version__, "reversible registry retirement: PASS")


async def main() -> None:
    samples = {
        "light": [{"name": "Light", "address": "1/0/1"}],
        "switch": [{"name": "Switch", "address": ["1/0/2", "1/0/3"]}],
        "sensor": [{"name": "Temperature", "state_address": "3/0/1", "type": "9.001"}],
        "binary_sensor": [{"name": "Contact", "state_address": ["3/0/2", "3/0/3"]}],
        "cover": [
            {
                "name": "Cover",
                "move_long_address": "2/0/1",
                "position_address": "2/0/3",
            },
            {"name": "Cover no position", "move_long_address": "2/0/4"},
        ],
        "scene": [{"name": "Scene", "address": "4/0/1", "scene_number": 13}],
        "button": [
            {"name": "Button", "address": "6/0/1"},
            {"name": "Button zero", "address": "6/0/2", "payload": 0},
            {
                "name": "Button value",
                "address": "6/0/3",
                "value": 50,
                "type": "percent",
            },
        ],
        "climate": [
            {
                "name": "Climate",
                "temperature_address": "5/0/1",
                "target_temperature_state_address": "5/0/2",
                "target_temperature_address": "5/0/3",
            }
        ],
    }
    validated = CONFIG_SCHEMA({"knx": samples})["knx"]
    with tempfile.TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        if hasattr(dr, "async_setup"):
            dr.async_setup(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        module = types.SimpleNamespace(hass=hass, xknx=XKNX(), connected=False)
        count = 0
        legacy = m.core_compatibility(__version__)["identityMode"] == "legacy"
        original_format = GroupAddress.address_format
        try:
            for address_format in (
                GroupAddressType.LONG,
                GroupAddressType.SHORT,
                GroupAddressType.FREE,
            ):
                GroupAddress.address_format = address_format
                for platform, configs in validated.items():
                    if not configs:
                        continue
                    component = importlib.import_module(
                        f"homeassistant.components.knx.{platform}"
                    )
                    class_name = "KnxYaml" + "".join(
                        word.title() for word in platform.split("_")
                    )
                    constructor = getattr(component, class_name)
                    with patch.object(
                        knx_entity,
                        "async_get_current_platform",
                        return_value=types.SimpleNamespace(domain=platform),
                        create=True,
                    ):
                        for config in configs:
                            migrated_entity_id = None
                            if (
                                not legacy
                                and platform == "light"
                                and address_format == GroupAddressType.FREE
                            ):
                                registry = er.async_get(hass)
                                migrated_entity_id = registry.async_get_or_create(
                                    "light",
                                    "knx",
                                    "2049",
                                    suggested_object_id="original_light",
                                ).entity_id
                            entity = constructor(module, config)
                            expected = m.row_identity(
                                platform,
                                config,
                                address_format.name if legacy else None,
                            )
                            if migrated_entity_id:
                                assert (
                                    registry.async_get_entity_id(
                                        "light", "knx", expected
                                    )
                                    == migrated_entity_id
                                )
                            assert entity.unique_id == expected, (
                                platform,
                                address_format,
                                entity.unique_id,
                                expected,
                            )
                            count += 1
            print(
                "Real HA KNX native entity identities: PASS",
                count,
                "constructors across 3 address formats",
            )
            await check_registry_retirement(hass)
        finally:
            GroupAddress.address_format = original_format
            await hass.async_stop(force=True)


if __name__ == "__main__":
    asyncio.run(main())
