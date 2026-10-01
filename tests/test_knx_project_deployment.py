"""Real file/YAML transactions with a controlled HA lifecycle boundary.

These tests do not claim compatibility testing against an installed HA Core.
Runtime CONFIG_SCHEMA is exercised through its adapter, not copied into tests.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_writer_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.knx_project_deployment", PACKAGE / "knx_project_deployment.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

PLAN = {
    "projectId": "test-project",
    "entityCount": 2,
    "skipped": [],
    "config": {
        "light": [{"name": "Light", "address": "1/0/1", "unique_id": "auto-light"}],
        "sensor": [
            {
                "name": "Temperature",
                "state_address": "1/0/2",
                "type": "temperature",
                "unique_id": "auto-sensor",
            }
        ],
    },
    "entities": [
        {"platform": "light", "uniqueId": "auto-light", "addresses": ["1/0/1"]},
        {"platform": "sensor", "uniqueId": "auto-sensor", "addresses": ["1/0/2"]},
    ],
}
FINGERPRINT = "a" * 64


class InvalidPasswordException(Exception):
    """Stand-in for xknxproject's missing or wrong password error."""


parser_errors = types.ModuleType("xknxproject.exceptions")
parser_errors.InvalidPasswordException = InvalidPasswordException


def load_file(path: Path) -> dict:
    class Loader(yaml.SafeLoader):
        pass

    def include(loader, node):
        return load_file(path.parent / loader.construct_scalar(node))

    def directory(loader, node):
        folder = path.parent / loader.construct_scalar(node)
        output = {}
        for child in sorted(folder.glob("*.yaml")):
            if node.tag == "!include_dir_named":
                output[child.stem] = load_file(child)
            else:
                output.update(load_file(child))
        return output

    Loader.add_constructor("!include", include)
    Loader.add_constructor("!include_dir_named", directory)
    Loader.add_constructor("!include_dir_merge_named", directory)
    return yaml.load(path.read_text(), Loader=Loader) or {}


def load_config(root: Path) -> dict:
    result = load_file(root / "configuration.yaml")
    for package in result.get("homeassistant", {}).get("packages", {}).values():
        for domain, platforms in package.items():
            target = m.raw_platform_lists(result.setdefault(domain, {}))
            result[domain] = target
            for platform, rows in m.raw_platform_lists(platforms).items():
                target.setdefault(platform, []).extend(rows)
    return result


class IdentityTests(unittest.TestCase):
    def test_native_identity_formulas_match_official_2026_8_platforms(self):
        cases = [
            ("light", {"address": "2049"}, "1/0/1"),
            ("switch", {"address": ["1/2", "1/0/3"]}, "1/0/2"),
            ("sensor", {"state_address": "1/0/4"}, "1/0/4"),
            ("binary_sensor", {"state_address": ["1/0/5", "1/0/6"]}, "1/0/5"),
            (
                "cover",
                {"move_long_address": "2/0/1", "position_address": "2/0/3"},
                "2/0/1_2/0/3",
            ),
            ("cover", {"move_long_address": "2/0/1"}, "2/0/1_None"),
            (
                "climate",
                {
                    "temperature_address": "3/0/1",
                    "target_temperature_state_address": "3/0/2",
                    "target_temperature_address": "3/0/3",
                },
                "3/0/1_3/0/2_3/0/3_None",
            ),
            ("scene", {"address": "4/0/1", "scene_number": 13}, "4/0/1_13"),
        ]
        for platform, config, expected in cases:
            with self.subTest(platform=platform, config=config):
                self.assertEqual(m.row_identity(platform, config), expected)
        self.assertEqual(
            m.row_identity("light", {"address": "1/0/1", "unique_id": "custom"}),
            "custom",
        )

    def test_capability_versions_reject_unknown_or_pre_release_core(self):
        for version in ("2025.12.4", "2026.8.0b2", "unknown", "2026.9.0.dev20260901"):
            with self.subTest(version=version), self.assertRaises(m.DeploymentError):
                m.core_compatibility(version)
        self.assertEqual(m.core_compatibility("2026.8.0")["identityMode"], "native")
        self.assertEqual(m.core_compatibility("2026.9.1")["identityMode"], "custom")


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = m.SafeFiles(self.root)

    def activate(self, text: str):
        self.files.write("configuration.yaml", text.encode())
        self.files.write(
            m.GENERATED,
            b"knx:\n  switch:\n    - name: Generated\n      address: 1/0/1\n",
        )
        for name, data in self.files.activation_changes().items():
            self.files.write(name, data)
        return load_config(self.root)

    def test_preserves_manual_yaml_bytes_and_comments(self):
        manual = (
            b"# User's KNX configuration\nlight:\n"
            b"  - name: Manual\n    address: 2/0/1\n"
        )
        self.files.write("KNX/manual.yaml", manual)
        conf = self.activate("# keep\ndefault_config:\nknx: !include KNX/manual.yaml\n")
        self.assertEqual(self.files.read("KNX/manual.yaml"), manual)
        self.assertEqual(conf["knx"]["light"][0]["name"], "Manual")
        self.assertEqual(conf["knx"]["switch"][0]["name"], "Generated")
        self.assertTrue(
            self.files.read("configuration.yaml").startswith(
                b"# keep\ndefault_config:\nknx: !include KNX/manual.yaml"
            )
        )

    def test_mapping_indentation_and_null_nodes(self):
        for text in (
            "homeassistant:\n",
            "homeassistant: null # keep\n",
            "homeassistant: {}\n",
            "homeassistant:\n  packages: {}\n",
            "homeassistant:\n  packages:\n",
            "homeassistant:\n    name: My home\n",
            (
                "homeassistant:\n  packages:\n    manual:\n"
                "      knx:\n        sensor: []\n"
            ),
        ):
            with self.subTest(text=text):
                conf = self.activate(text)
                self.assertEqual(len(conf["knx"]["switch"]), 1)
                self.assertEqual(self.files.activation_changes(), {})

    def test_existing_directory_packages(self):
        for tag in ("!include_dir_named", "!include_dir_merge_named"):
            with self.subTest(tag=tag):
                (self.root / "packages").mkdir(exist_ok=True)
                # Reset the reserved bridge when testing the other directory mode.
                (self.root / "packages/rexlite_knx_auto.yaml").unlink(missing_ok=True)
                conf = self.activate(f"homeassistant:\n  packages: {tag} packages\n")
                self.assertEqual(len(conf["knx"]["switch"]), 1)

    def test_reject_unsupported_include_and_duplicate_package(self):
        for text in (
            "homeassistant:\n  packages: !include packages.yaml\n",
            "homeassistant: {name: Existing}\n",
            "homeassistant:\n  packages:\n    rexlite_knx_auto: {}\n",
            "knx: {}\nknx: {}\n",
        ):
            self.files.write("configuration.yaml", text.encode())
            with self.assertRaises(m.DeploymentError):
                self.files.activation_changes()

    def test_paths_symlinks_hardlinks_rejected(self):
        self.files.write("original", b"unchanged")
        (self.root / "linked").symlink_to(self.root / "original")
        (self.root / "folder").symlink_to(self.root)
        import os

        os.link(self.root / "original", self.root / "hard")
        for name in ("../escape", "/tmp/escape", "linked", "folder/escape", "hard"):
            with self.subTest(name=name), self.assertRaises(m.DeploymentError):
                self.files.write(name, b"bad")
        self.assertEqual((self.root / "original").read_bytes(), b"unchanged")

    def test_unowned_bridge_not_overwritten(self):
        self.files.write(
            "configuration.yaml",
            b"homeassistant:\n  packages: !include_dir_named packages\n",
        )
        self.files.write("packages/rexlite_knx_auto.yaml", b"manual: true\n")
        with self.assertRaises(m.DeploymentError):
            self.files.activation_changes()
        self.assertEqual(
            self.files.read("packages/rexlite_knx_auto.yaml"), b"manual: true\n"
        )

    def test_collisions_normalize_address_styles_and_preserve_manual(self):
        existing = {"switch": [{"name": "manual", "address": "2049"}]}
        plan, merged = m.filter_existing(PLAN, existing, None, set())
        self.assertEqual(plan["entityCount"], 1)
        self.assertEqual(merged["switch"], existing["switch"])
        self.assertEqual(plan["skipped"][0]["address"], "1/0/1")

    def test_singleton_manual_platforms_keep_same_and_cross_platform_ownership(self):
        for platform in ("light", "switch"):
            with self.subTest(platform=platform):
                manual = {"name": "Manual", "address": "1/0/1"}
                raw = {platform: manual, "binary_sensor": None}
                plan, combined = m.filter_existing(PLAN, raw, None, set())
                self.assertEqual(plan["entityCount"], 1)
                self.assertEqual(plan["skipped"][0]["address"], "1/0/1")
                self.assertEqual(combined[platform], [manual])
                self.assertEqual(combined["binary_sensor"], [])
                self.assertEqual(raw[platform], manual)
                self.assertEqual(m.indexed_rows(raw)[(platform, "1/0/1")], manual)
        plan, combined = m.filter_existing(PLAN, None, None, set())
        self.assertEqual(plan["entityCount"], 2)
        self.assertEqual(combined, PLAN["config"])

    def test_nested_manual_rgb_addresses_are_preserved(self):
        manual = {
            "name": "Manual RGB",
            "individual_colors": {
                "red": {"address": "1/0/1", "brightness_address": ["2/0/1", "2/0/2"]},
                "green": {"brightness_address": "2/0/3"},
                "blue": {"brightness_address": "2/0/4"},
            },
        }
        plan, combined = m.filter_existing(PLAN, {"light": [manual]}, None, set())
        self.assertEqual(plan["entityCount"], 1)
        self.assertEqual(plan["skipped"][0]["address"], "1/0/1")
        self.assertEqual(combined["light"], [manual])
        self.assertEqual(
            m._addresses(manual), {"1/0/1", "2/0/1", "2/0/2", "2/0/3", "2/0/4"}
        )

    def test_disabled_ui_entity_collision(self):
        module = types.SimpleNamespace(
            group_address_entities={},
            config_store=types.SimpleNamespace(
                data={
                    "entities": {
                        "light": {
                            "disabled": {
                                "knx": {"ga_switch": {"write": "1/1", "state": "1/0/9"}}
                            }
                        }
                    }
                }
            ),
        )
        addresses = m.ProjectDeployer._ui_addresses(module)
        self.assertIn("1/0/1", addresses)
        plan, _ = m.filter_existing(PLAN, {}, None, addresses)
        self.assertEqual(plan["entityCount"], 1)


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "configuration.yaml").write_text("default_config:\n")
        self.project = {"info": {"name": "test"}}
        self.registry_entries, self.states = {}, {}
        self.registry_updates = []
        self.reload_calls = 0
        self.fail_reload = False
        self.spoof_config = False
        self.schema_fail = False
        self.module = types.SimpleNamespace(
            project=types.SimpleNamespace(loaded=True, get_knxproject=self.get_project),
            entry=types.SimpleNamespace(entry_id="knx-entry"),
            config_yaml={},
            group_address_entities={},
            config_store=types.SimpleNamespace(data={"entities": {}}),
        )
        self.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(config_dir=str(self.root)),
            data={"knx": self.module},
            async_add_executor_job=self.executor,
            config_entries=types.SimpleNamespace(async_reload=self.reload),
            states=types.SimpleNamespace(
                get=self.states.get,
                async_remove=lambda entity_id: self.states.pop(entity_id, None),
            ),
        )
        self.writer = m.ProjectDeployer(self.hass)
        self.writer.files.write_json(
            m.IMPORT_ASSOCIATION,
            {
                "projectFingerprint": FINGERPRINT,
                "parsedProjectDigest": m.project_digest(self.project),
            },
        )
        self.writer._schema = self.schema
        registry = types.SimpleNamespace(
            async_get_entity_id=lambda platform, integration, uid: next(
                (
                    key
                    for key, value in self.registry_entries.items()
                    if value.unique_id == uid and value.domain == platform
                ),
                None,
            ),
            async_get=self.registry_entries.get,
            async_update_entity=self.update_registry,
        )
        config_module = types.ModuleType("homeassistant.config")
        config_module.async_hass_config_yaml = self.load
        const_module = types.ModuleType("homeassistant.components.knx.const")
        const_module.KNX_MODULE_KEY = "knx"
        core_const = types.ModuleType("homeassistant.const")
        core_const.__version__ = "2026.9.1"
        core_const.Platform = types.SimpleNamespace(
            SENSOR="sensor", BINARY_SENSOR="binary_sensor", SWITCH="switch"
        )
        helpers = types.ModuleType("homeassistant.helpers")
        helpers.entity_registry = types.SimpleNamespace(
            async_get=lambda hass: registry, RegistryEntryDisabler=str
        )
        self.patch_modules = patch.dict(
            sys.modules,
            {
                "homeassistant.config": config_module,
                "homeassistant.const": core_const,
                "homeassistant.components.knx.const": const_module,
                "homeassistant.helpers": helpers,
            },
        )
        self.patch_modules.start()
        self.addCleanup(self.patch_modules.stop)
        self.mapper = patch.object(m, "plan_project", return_value=deepcopy(PLAN))
        self.mapper.start()
        self.addCleanup(self.mapper.stop)

    async def test_manual_yaml_check_is_read_only_and_deploy_preserves_existing(self):
        await self.writer.deploy(FINGERPRINT)
        before = self.writer.files.read(m.GENERATED)
        source = 'knx:\n  switch:\n    - name: Manual\n      address: "3/0/8"\n'
        checked = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, check_only=True
        )
        self.assertEqual(checked["status"], "ready", checked)
        self.assertEqual(self.writer.files.read(m.GENERATED), before)
        result = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, baseline=checked["fingerprint"]
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["entityCount"], 3)
        self.assertEqual(result["manualCount"], 1)
        self.assertEqual(result["manualDigest"], m.digest(source.encode()))
        configured = load_config(self.root)["knx"]
        self.assertEqual(configured["light"], PLAN["config"]["light"])
        # Auto retries retain the manual addition and don't redeploy it.
        retry = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(retry["entityCount"], 3)
        self.assertEqual(load_config(self.root)["knx"], configured)

    async def test_manual_yaml_rejects_stale_check_and_existing_address(self):
        source = 'knx:\n  switch:\n    - name: Manual\n      address: "3/0/8"\n'
        checked = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, check_only=True
        )
        self.assertEqual(checked["status"], "ready", checked)
        self.writer.files.write("configuration.yaml", b"default_config:\n# changed\n")
        result = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, baseline=checked["fingerprint"]
        )
        self.assertEqual(result["error"], "manual_yaml_preflight_stale")
        self.assertIsNone(self.writer.files.read(m.GENERATED))
        await self.writer.deploy(FINGERPRINT)
        result = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source.replace("3/0/8", "1/0/1"), check_only=True
        )
        self.assertIn("already_exists", result["error"])

    async def test_mapper_upgrade_preserves_manual_mapping_and_adds_other_improvements(
        self,
    ):
        await self.writer.deploy(FINGERPRINT)
        source = (
            "knx:\n  light:\n    - name: User mapped lamp\n"
            '      address: "3/0/8"\n      state_address: "3/0/9"\n'
        )
        checked = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, check_only=True
        )
        await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, baseline=checked["fingerprint"]
        )
        original_manual = self.writer.files.read_json(m.MANIFEST)["manualPlan"]
        manual_uid = original_manual["entities"][0]["uniqueId"]
        manual_entry = self.registry_entries[f"light.{manual_uid}"]
        manual_entry.name = "User customized lamp"
        upgraded = deepcopy(PLAN)
        upgraded["config"]["switch"] = [
            {
                "name": "Auto rediscovered lamp",
                "address": "3/0/8",
                "unique_id": "auto-clash",
            }
        ]
        upgraded["config"]["light"].append(
            {"name": "Newly mapped lamp", "address": "3/0/10", "unique_id": "auto-new"}
        )
        upgraded["entities"].extend(
            [
                {
                    "platform": "switch",
                    "uniqueId": "auto-clash",
                    "addresses": ["3/0/8"],
                },
                {"platform": "light", "uniqueId": "auto-new", "addresses": ["3/0/10"]},
            ]
        )
        upgraded["entityCount"] = 4
        result = await self.deploy_plan(upgraded)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["entityCount"], 4)
        configured = load_config(self.root)["knx"]
        self.assertNotIn("switch", configured)
        self.assertIn(original_manual["config"]["light"][0], configured["light"])
        self.assertIn("light.auto-new", self.states)
        self.assertIs(self.registry_entries[f"light.{manual_uid}"], manual_entry)
        self.assertEqual(manual_entry.name, "User customized lamp")
        self.assertIsNone(manual_entry.disabled_by)
        self.assertEqual(
            self.writer.files.read_json(m.MANIFEST)["manualPlan"], original_manual
        )

    async def test_mapper_upgrade_preserves_manual_scene_and_distinct_scene_numbers(
        self,
    ):
        await self.writer.deploy(FINGERPRINT)
        source = (
            "knx:\n  scene:\n    - name: Confirmed scene\n"
            '      address: "4/0/1"\n      scene_number: 2\n'
        )
        checked = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, check_only=True
        )
        await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, baseline=checked["fingerprint"]
        )
        original_manual = self.writer.files.read_json(m.MANIFEST)["manualPlan"]
        upgraded = deepcopy(PLAN)
        upgraded["config"]["scene"] = [
            {
                "name": f"New automatic scene {number}",
                "address": "4/0/1",
                "scene_number": number,
                "unique_id": f"auto-scene-{number}",
            }
            for number in (2, 3)
        ]
        upgraded["entities"].extend(
            {
                "platform": "scene",
                "uniqueId": f"auto-scene-{number}",
                "addresses": ["4/0/1"],
            }
            for number in (2, 3)
        )
        upgraded["entityCount"] = 4
        result = await self.deploy_plan(upgraded)
        self.assertEqual(result["status"], "completed", result)
        scenes = load_config(self.root)["knx"]["scene"]
        self.assertEqual(sorted(row["scene_number"] for row in scenes), [2, 3])
        self.assertIn(original_manual["config"]["scene"][0], scenes)
        self.assertNotIn("scene.auto-scene-2", self.registry_entries)
        for mode in ("native", "legacy"):
            with self.subTest(mode=mode):
                plan = m.adapt_plan_identity(upgraded, mode, "FREE")
                manual = m.adapt_plan_identity(original_manual, mode, "FREE")
                merged = m.preserve_manual_plan(plan, manual, "FREE")
                self.assertEqual(
                    sorted(row["scene_number"] for row in merged["config"]["scene"]),
                    [2, 3],
                )

    def test_manual_feedback_collision_keeps_uncovered_command_visible_for_review(self):
        _, auto = self.pairing_plans()
        manual = m.manual_yaml_plan(
            "knx:\n  binary_sensor:\n    - name: Confirmed feedback\n"
            '      state_address: "1/0/2"\n'
        )
        merged = m.preserve_manual_plan(auto, manual)
        self.assertEqual(merged["config"], manual["config"])
        self.assertEqual(merged["entityCount"], 1)
        self.assertEqual(
            merged["skipped"],
            [{"address": "1/0/1", "reason": "existing_manual_or_ui_entity_preserved"}],
        )

    async def test_manual_yaml_reload_failure_rolls_back(self):
        source = 'knx:\n  switch:\n    - name: Manual\n      address: "3/0/8"\n'
        checked = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, check_only=True
        )
        before = self.writer.files.read("configuration.yaml")
        self.fail_reload = True
        result = await self.writer.deploy(
            FINGERPRINT, manual_yaml=source, baseline=checked["fingerprint"]
        )
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.writer.files.read("configuration.yaml"), before)
        self.assertIsNone(self.writer.files.read(m.GENERATED))

    async def get_project(self):
        return deepcopy(self.project)

    async def executor(self, fn, *args):
        return fn(*args)

    async def load(self, hass):
        return load_config(self.root)

    def schema(self, value):
        if self.schema_fail:
            raise ValueError("invalid schema")
        return deepcopy(value)

    def update_registry(self, entity_id, *, disabled_by):
        self.registry_updates.append((entity_id, disabled_by))
        self.registry_entries[entity_id].disabled_by = disabled_by
        return self.registry_entries[entity_id]

    async def reload(self, entry_id):
        self.reload_calls += 1
        if self.fail_reload and self.reload_calls == 1:
            return False
        self.module.config_yaml = load_config(self.root).get("knx", {})
        self.states.clear()
        for entity_id, entry in self.registry_entries.items():
            if not entry.disabled_by:
                self.states[entity_id] = types.SimpleNamespace(
                    state="unavailable", attributes={"restored": True}
                )
        for platform, rows in self.module.config_yaml.items():
            for row in rows:
                uid = m.row_identity(platform, row, self.writer._identity_format())
                if not uid:
                    continue
                entity_id = f"{platform}.{uid}"
                entry = self.registry_entries.setdefault(
                    entity_id,
                    types.SimpleNamespace(
                        unique_id=uid,
                        domain=platform,
                        platform="knx",
                        config_entry_id=entry_id,
                        disabled_by=None,
                    ),
                )
                if entry.disabled_by:
                    continue
                self.states[entity_id] = types.SimpleNamespace(
                    state="off" if platform == "light" else "unknown", attributes={}
                )
        if self.spoof_config:
            self.module.config_yaml["light"][0]["address"] = "3/0/9"
        return True

    async def test_success_loaded_separate_from_available_and_idempotent(self):
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(
            (result["entityCount"], result["loadedCount"], result["availableCount"]),
            (2, 2, 1),
        )
        self.assertEqual((self.root / m.GENERATED).stat().st_mode & 0o777, 0o600)
        self.assertEqual(await self.writer.deploy(FINGERPRINT), result)
        self.assertEqual(self.reload_calls, 1)
        fresh = m.ProjectDeployer(self.hass)
        fresh._schema = self.schema
        self.assertEqual(await fresh.status(FINGERPRINT), result)

    @staticmethod
    def pairing_plans():
        old = {
            "projectId": "test-project",
            "entityCount": 2,
            "skipped": [],
            "config": {
                "switch": [
                    {"name": "Lamp SW", "address": "1/0/1", "unique_id": "old-sw"}
                ],
                "binary_sensor": [
                    {
                        "name": "Lamp FB",
                        "state_address": "1/0/2",
                        "unique_id": "old-fb",
                    }
                ],
            },
            "entities": [
                {"platform": "switch", "uniqueId": "old-sw", "addresses": ["1/0/1"]},
                {
                    "platform": "binary_sensor",
                    "uniqueId": "old-fb",
                    "addresses": ["1/0/2"],
                },
            ],
        }
        new = {
            "projectId": "test-project",
            "entityCount": 1,
            "skipped": [],
            "config": {
                "light": [
                    {
                        "name": "Lamp",
                        "address": "1/0/1",
                        "state_address": "1/0/2",
                        "unique_id": "new-light",
                    }
                ]
            },
            "entities": [
                {
                    "platform": "light",
                    "uniqueId": "new-light",
                    "addresses": ["1/0/1", "1/0/2"],
                }
            ],
        }
        return old, new

    async def deploy_plan(self, plan):
        # Force replanning as if the mapper was upgraded or its plan changed.
        with (
            patch.object(m, "plan_project", return_value=deepcopy(plan)),
            patch.object(m, "MAPPER_REVISION", self.reload_calls + 100),
        ):
            return await self.writer.deploy(FINGERPRINT)

    async def test_pairing_upgrade_retires_ghosts_and_preserves_registry_settings(self):
        old, new = self.pairing_plans()
        await self.deploy_plan(old)
        entry = self.registry_entries["switch.old-sw"]
        entry.name, entry.area_id = "User lamp", "living-room"
        result = await self.deploy_plan(new)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["loadedCount"], 1)
        self.assertEqual(set(self.states), {"light.new-light"})
        self.assertEqual(entry.disabled_by, "integration")
        self.assertEqual((entry.name, entry.area_id), ("User lamp", "living-room"))
        self.assertEqual(
            self.registry_entries["binary_sensor.old-fb"].disabled_by, "integration"
        )
        manifest = self.writer.files.read_json(m.MANIFEST)
        self.assertEqual(len(manifest["retiredEntities"]), 2)
        self.assertTrue(
            all(item["replacements"] for item in manifest["retiredEntities"])
        )
        await self.deploy_plan(new)
        self.assertEqual(len(self.registry_updates), 2)
        self.assertEqual(
            len(self.writer.files.read_json(m.MANIFEST)["retiredEntities"]), 2
        )

    async def test_pairing_upgrade_reintroduction_restores_only_our_disabled_entries(
        self,
    ):
        old, new = self.pairing_plans()
        await self.deploy_plan(old)
        await self.deploy_plan(new)
        result = await self.deploy_plan(old)
        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(self.registry_entries["switch.old-sw"].disabled_by)
        self.assertIsNone(self.registry_entries["binary_sensor.old-fb"].disabled_by)
        self.assertEqual(set(self.states), {"switch.old-sw", "binary_sensor.old-fb"})
        retired = self.writer.files.read_json(m.MANIFEST)["retiredEntities"]
        self.assertEqual([item["entityId"] for item in retired], ["light.new-light"])
        self.registry_entries["light.new-light"].disabled_by = "user"
        self.assertEqual(
            self.writer._registry_restorations({"retiredEntities": retired}, new), []
        )

    async def test_restored_unavailable_registry_state_is_not_a_loaded_entity(self):
        await self.writer.deploy(FINGERPRINT)
        manifest = self.writer.files.read_json(m.MANIFEST)
        self.states["light.auto-light"] = types.SimpleNamespace(
            state="unavailable", attributes={"restored": True}
        )
        self.assertEqual(self.writer._counts(manifest)["loadedCount"], 1)
        self.states["light.auto-light"].attributes.clear()
        self.assertEqual(self.writer._counts(manifest)["loadedCount"], 2)

    async def test_same_platform_enrichment_keeps_original_identity(self):
        old, new = self.pairing_plans()
        new["config"]["switch"] = new["config"].pop("light")
        new["config"]["switch"][0]["unique_id"] = "old-sw"
        new["entities"][0].update(platform="switch", uniqueId="old-sw")
        await self.deploy_plan(old)
        entry = self.registry_entries["switch.old-sw"]
        entry.name = "User lamp"
        result = await self.deploy_plan(new)
        self.assertEqual(result["status"], "completed", result)
        self.assertIs(self.registry_entries["switch.old-sw"], entry)
        self.assertIsNone(entry.disabled_by)
        self.assertEqual(entry.name, "User lamp")
        self.assertEqual(set(self.states), {"switch.old-sw"})

    async def test_retirement_preserves_manual_ui_live_and_user_disabled_entities(self):
        old, new = self.pairing_plans()
        await self.deploy_plan(old)
        previous = self.writer.files.read_json(m.MANIFEST)
        manifest = {**new, "configEntryId": "knx-entry"}
        self.module.config_yaml = new["config"]
        for state in self.states.values():
            state.attributes["restored"] = True
        self.assertEqual(len(self.writer._registry_retirements(previous, manifest)), 2)
        self.registry_entries["switch.old-sw"].disabled_by = "user"
        previous["manualPlan"] = {"entities": [old["entities"][1]]}
        self.assertEqual(self.writer._registry_retirements(previous, manifest), [])
        previous.pop("manualPlan")
        self.module.config_store.data["entities"] = {"manual": {"state": "1/0/2"}}
        self.assertEqual(self.writer._registry_retirements(previous, manifest), [])
        self.module.config_store.data["entities"] = {}
        self.states["binary_sensor.old-fb"].attributes.clear()
        self.assertEqual(self.writer._registry_retirements(previous, manifest), [])
        self.states["binary_sensor.old-fb"].attributes["restored"] = True
        self.module.config_yaml["binary_sensor"] = old["config"]["binary_sensor"]
        self.assertEqual(self.writer._registry_retirements(previous, manifest), [])

    async def test_upgrade_verification_failure_never_retires_old_entities(self):
        old, new = self.pairing_plans()
        await self.deploy_plan(old)
        with patch.object(
            self.writer,
            "_verify_configuration",
            side_effect=m.DeploymentError("verification_failed"),
        ):
            result = await self.deploy_plan(new)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["stage"], "rolled_back", result)
        self.assertEqual(self.registry_updates, [])
        self.assertIsNone(self.registry_entries["switch.old-sw"].disabled_by)

    async def test_registry_mutation_failure_rolls_back_yaml_and_disablers(self):
        old, new = self.pairing_plans()
        await self.deploy_plan(old)
        before = self.writer.files.read(m.GENERATED)
        original = self.writer._registry_change
        calls = 0

        def fail_second(record, *, reverse=False):
            nonlocal calls
            if not reverse:
                calls += 1
                if calls == 2:
                    raise RuntimeError("injected registry failure")
            return original(record, reverse=reverse)

        with patch.object(self.writer, "_registry_change", side_effect=fail_second):
            result = await self.deploy_plan(new)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["stage"], "rolled_back", result)
        self.assertEqual(self.writer.files.read(m.GENERATED), before)
        self.assertIsNone(self.registry_entries["switch.old-sw"].disabled_by)
        self.assertIsNone(self.registry_entries["binary_sensor.old-fb"].disabled_by)
        self.assertFalse(self.states["switch.old-sw"].attributes.get("restored"))

    async def test_registry_crash_recovery_and_external_disable_are_preserved(self):
        old, new = self.pairing_plans()
        await self.deploy_plan(old)
        original = self.writer._registry_change

        class SimulatedCrash(BaseException):
            pass

        def crash_after_first(record, *, reverse=False):
            original(record, reverse=reverse)
            raise SimulatedCrash

        with (
            patch.object(
                self.writer, "_registry_change", side_effect=crash_after_first
            ),
            self.assertRaises(SimulatedCrash),
        ):
            await self.deploy_plan(new)
        self.assertIsNotNone(self.writer.files.read(m.JOURNAL))
        self.registry_entries["switch.old-sw"].disabled_by = "user"
        await self.writer.recover()
        self.assertEqual(self.registry_entries["switch.old-sw"].disabled_by, "user")
        self.assertIsNone(self.registry_entries["binary_sensor.old-fb"].disabled_by)
        self.assertEqual(load_config(self.root)["knx"], old["config"])
        self.assertIsNone(self.writer.files.read(m.JOURNAL))

    async def test_reload_failure_restores_originals_and_persists_outcome(self):
        original = (self.root / "configuration.yaml").read_bytes()
        self.fail_reload = True
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "knx_reload_failed")
        self.assertEqual((self.root / "configuration.yaml").read_bytes(), original)
        self.assertFalse((self.root / m.GENERATED).exists())
        self.assertEqual(self.reload_calls, 2)
        self.assertEqual(await self.writer.status(FINGERPRINT), result)
        self.assertFalse((self.root / m.JOURNAL).exists())

    async def test_schema_failure_changes_no_configuration(self):
        original = (self.root / "configuration.yaml").read_bytes()
        self.schema_fail = True
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["status"], "failed")
        self.assertEqual((self.root / "configuration.yaml").read_bytes(), original)
        self.assertFalse((self.root / m.GENERATED).exists())
        self.assertEqual(self.reload_calls, 0)

    async def test_removed_package_cannot_report_success_from_stale_states(self):
        await self.writer.deploy(FINGERPRINT)
        (self.root / "configuration.yaml").write_text("default_config:\n")
        status = await self.writer.status(FINGERPRINT)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "managed_package_not_active")
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.reload_calls, 2)

    async def test_same_ids_with_different_runtime_configuration_roll_back(self):
        self.spoof_config = True
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("managed_package_not_active", result["error"])

    async def test_modified_generated_file_is_not_overwritten(self):
        await self.writer.deploy(FINGERPRINT)
        (self.root / m.GENERATED).write_text("# manually edited\n")
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["error"], "managed_yaml_modified_externally")
        self.assertEqual((self.root / m.GENERATED).read_text(), "# manually edited\n")

    async def test_no_mappings_preserves_active_deployment_and_persists_review(self):
        await self.writer.deploy(FINGERPRINT)
        original = (self.root / m.GENERATED).read_bytes()
        with patch.object(
            m,
            "plan_project",
            return_value={
                "config": {},
                "entities": [],
                "entityCount": 0,
                "skipped": [{"address": "1/0/1", "reason": "ambiguous"}],
            },
        ):
            self.writer.files.write_json(
                m.IMPORT_ASSOCIATION,
                {
                    "projectFingerprint": "b" * 64,
                    "parsedProjectDigest": m.project_digest(self.project),
                },
            )
            result = await self.writer.deploy("b" * 64)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual((self.root / m.GENERATED).read_bytes(), original)
        self.assertEqual(await self.writer.status("b" * 64), result)

    async def test_project_hash_change_prevents_false_idempotency(self):
        await self.writer.deploy(FINGERPRINT)
        self.project["info"]["name"] = "changed"
        self.assertEqual((await self.writer.status(FINGERPRINT))["status"], "failed")
        self.assertEqual(
            (await self.writer.deploy(FINGERPRINT))["error"],
            "project_changed_after_import",
        )
        self.assertEqual(self.reload_calls, 1)

    async def test_concurrent_retry_serializes_and_reloads_once(self):
        result = await asyncio.gather(
            self.writer.deploy(FINGERPRINT), self.writer.deploy(FINGERPRINT)
        )
        self.assertEqual(
            [value["status"] for value in result], ["completed", "completed"]
        )
        self.assertEqual(self.reload_calls, 1)

    async def test_new_mapper_revision_replans_same_file_once_and_keeps_identity(self):
        await self.writer.deploy(FINGERPRINT)
        previous = self.writer.files.read_json(m.MANIFEST)
        previous.pop("mapperRevision")  # Deployment made before mapping revisions.
        self.writer.files.write_json(m.MANIFEST, previous)
        improved = deepcopy(PLAN)
        improved["config"]["light"][0]["state_address"] = "1/0/9"
        improved["entities"][0]["addresses"].append("1/0/9")
        with patch.object(m, "plan_project", return_value=improved):
            upgraded = await self.writer.deploy(FINGERPRINT)
            self.assertEqual(upgraded["status"], "completed", upgraded)
            self.assertEqual(self.reload_calls, 2)
            generated = load_config(self.root)["knx"]
            self.assertEqual(generated["light"][0]["state_address"], "1/0/9")
            self.assertEqual(generated["light"][0]["unique_id"], "auto-light")
            self.assertEqual(
                self.writer.files.read_json(m.MANIFEST)["mapperRevision"],
                m.MAPPER_REVISION,
            )
            await self.writer.deploy(FINGERPRINT)
            self.assertEqual(self.reload_calls, 2)

    async def test_failed_mapping_upgrade_restores_previous_working_revision(self):
        await self.writer.deploy(FINGERPRINT)
        previous = self.writer.files.read_json(m.MANIFEST)
        previous["mapperRevision"] = 1
        self.writer.files.write_json(m.MANIFEST, previous)
        before = self.writer.files.read(m.GENERATED)
        self.reload_calls = 0
        self.fail_reload = True
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.writer.files.read(m.GENERATED), before)
        self.assertEqual(self.writer.files.read_json(m.MANIFEST)["mapperRevision"], 1)

    async def test_crash_journal_recovers_original_config(self):
        before, after = b"default_config:\n", b"default_config:\n# partial write\n"
        self.writer.files.write("configuration.yaml", after)
        journal = {
            "configEntryId": "knx-entry",
            "previous": None,
            "files": {
                "configuration.yaml": {
                    "before": base64.b64encode(before).decode(),
                    "after": base64.b64encode(after).decode(),
                }
            },
        }
        self.writer.files.write_json(m.JOURNAL, journal)
        await self.writer.recover()
        self.assertEqual((self.root / "configuration.yaml").read_bytes(), before)
        self.assertEqual(self.reload_calls, 1)
        self.assertFalse((self.root / m.JOURNAL).exists())

    async def test_rollback_does_not_erase_external_edit(self):
        self.writer.files.write("configuration.yaml", b"# external edit\n")
        journal = {
            "configEntryId": "knx-entry",
            "previous": None,
            "files": {
                "configuration.yaml": {
                    "before": base64.b64encode(b"before").decode(),
                    "after": base64.b64encode(b"after").decode(),
                }
            },
        }
        with self.assertRaisesRegex(m.DeploymentError, "external_edit"):
            await self.writer._rollback(journal)
        self.assertEqual(
            (self.root / "configuration.yaml").read_bytes(), b"# external edit\n"
        )

    async def test_invalid_fingerprint_and_no_deployment_status(self):
        with self.assertRaises(m.DeploymentError):
            await self.writer.deploy("../escape")
        self.assertIsNone(await self.writer.status(FINGERPRINT))

    async def test_admin_is_required_before_every_command(self):
        handlers = []

        def require_admin(fn):
            def checked(hass, connection, msg):
                if connection.user is None or not connection.user.is_admin:
                    raise PermissionError("Admin required")
                return fn(hass, connection, msg)

            return checked

        websocket = types.SimpleNamespace(
            websocket_command=lambda schema: lambda fn: fn,
            require_admin=require_admin,
            async_response=lambda fn: fn,
            async_register_command=lambda hass, fn: handlers.append(fn),
        )
        components = types.ModuleType("homeassistant.components")
        components.websocket_api = websocket

        class Connection:
            user = types.SimpleNamespace(is_admin=False)

            def send_result(self, *args):
                raise AssertionError("No result for non-admin")

        with patch.dict(sys.modules, {"homeassistant.components": components}):
            self.hass.bus = types.SimpleNamespace(async_listen_once=Mock())
            m.register_websocket_commands(self.hass)
            m.register_websocket_commands(self.hass)
        self.addCleanup(self.hass.data["rexlite_knx_uploads"].close)
        self.assertEqual(len(handlers), 7)
        for handler in handlers:
            with self.assertRaises(PermissionError):
                await handler(self.hass, Connection(), {"id": 1})
        self.assertEqual(self.reload_calls, 0)

    async def test_idempotent_retry_clears_a_persisted_preflight_failure(self):
        self.assertEqual((await self.writer.deploy(FINGERPRINT))["status"], "completed")
        self.schema_fail = True
        self.assertEqual((await self.writer.deploy(FINGERPRINT))["status"], "failed")
        self.schema_fail = False
        retry = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(retry["status"], "completed", retry)
        self.assertEqual(await self.writer.status(FINGERPRINT), retry)
        self.assertEqual(self.reload_calls, 1)

    async def test_singleton_manual_platform_survives_deployment_and_retry(self):
        (self.root / "configuration.yaml").write_text(
            "knx:\n  light:\n    name: Manual\n    address: 2/0/1\n"
        )
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((await self.writer.deploy(FINGERPRINT))["loadedCount"], 2)
        self.assertEqual(self.reload_calls, 1)

    async def test_legacy_2026_1_upload_retry_and_core_upgrade_keep_entity_ids(self):
        for address_format, expected_ids in (
            ("LONG", ["1/0/1", "1/0/2"]),
            ("SHORT", ["1/1", "1/2"]),
            ("FREE", ["2049", "2050"]),
        ):
            with self.subTest(address_format=address_format):
                # Isolate each initial deployment from the preceding format.
                for relative in (m.MANIFEST, m.LAST_ATTEMPT, m.GENERATED):
                    self.writer.files.write(relative, None)
                (self.root / "configuration.yaml").write_text("default_config:\n")
                self.registry_entries.clear()
                self.states.clear()
                self.reload_calls = 0
                with (
                    patch.object(
                        self.writer,
                        "_compatibility",
                        return_value=m.core_compatibility("2026.1.0"),
                    ),
                    patch.object(
                        self.writer, "_address_format", return_value=address_format
                    ),
                ):
                    first = await self.writer.deploy(FINGERPRINT)
                    self.assertEqual(first["status"], "completed", first)
                    manifest = self.writer.files.read_json(m.MANIFEST)
                    self.assertEqual(
                        [e["uniqueId"] for e in manifest["entities"]], expected_ids
                    )
                    self.assertEqual(
                        [e["canonicalUniqueId"] for e in manifest["entities"]],
                        ["1/0/1", "1/0/2"],
                    )
                    self.assertEqual(
                        (await self.writer.deploy(FINGERPRINT))["loadedCount"], 2
                    )
                    self.assertEqual(self.reload_calls, 1)
                # Model HA2026.8's official migration: the same entity_id gets a
                # stable native unique_id; deployment must verify that actual ID.
                for entry in self.registry_entries.values():
                    entry.unique_id = "1/0/1" if entry.domain == "light" else "1/0/2"
                self.assertEqual(
                    (await self.writer.status(FINGERPRINT))["loadedCount"], 2
                )
                self.assertEqual(
                    (await self.writer.deploy(FINGERPRINT))["loadedCount"], 2
                )
                self.assertEqual(self.reload_calls, 1)

    async def test_legacy_tuple_identifiers_and_changed_format_guard(self):
        self.module.group_address_entities = {"1/0/2": {("sensor", "manual_ui")}}
        self.module.config_store.data["entities"] = {
            "sensor": {"manual_ui": {"data": {"knx": {"state": "1/0/2"}}}}
        }
        with (
            patch.object(
                self.writer,
                "_compatibility",
                return_value=m.core_compatibility("2026.1.0"),
            ),
            patch.object(self.writer, "_address_format", return_value="FREE"),
        ):
            result = await self.writer.deploy(FINGERPRINT)
            self.assertEqual(result["status"], "partial", result)
            self.assertEqual(result["entityCount"], 1)
        original = (self.root / m.GENERATED).read_bytes()
        with (
            patch.object(
                self.writer,
                "_compatibility",
                return_value=m.core_compatibility("2026.7.4"),
            ),
            patch.object(self.writer, "_address_format", return_value="LONG"),
        ):
            result = await self.writer.deploy(FINGERPRINT)
            self.assertEqual(
                result["error"], "legacy_knx_identity_format_changed", result
            )
        self.assertEqual((self.root / m.GENERATED).read_bytes(), original)
        self.assertEqual(self.reload_calls, 1)

    async def test_2026_8_uses_native_ids_and_retains_them_after_core_upgrade(self):
        with patch.object(
            self.writer, "_compatibility", return_value=m.core_compatibility("2026.8.0")
        ):
            result = await self.writer.deploy(FINGERPRINT)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["loadedCount"], 2)
            self.assertEqual(result["availableCount"], 1)
            generated = yaml.safe_load((self.root / m.GENERATED).read_bytes())["knx"]
            self.assertNotIn("unique_id", generated["light"][0])
            manifest = self.writer.files.read_json(m.MANIFEST)
            self.assertEqual(
                [entity["uniqueId"] for entity in manifest["entities"]],
                ["1/0/1", "1/0/2"],
            )
            self.assertEqual((await self.writer.deploy(FINGERPRINT))["loadedCount"], 2)
        # A later deployment after upgrading Core must preserve those registry IDs.
        self.writer.files.write_json(
            m.IMPORT_ASSOCIATION,
            {
                "projectFingerprint": "b" * 64,
                "parsedProjectDigest": m.project_digest(self.project),
            },
        )
        upgraded = await self.writer.deploy("b" * 64)
        self.assertEqual(upgraded["status"], "completed", upgraded)
        self.assertEqual(
            self.writer.files.read_json(m.MANIFEST)["identityMode"], "native"
        )
        self.assertEqual(self.reload_calls, 2)
        self.assertEqual((await self.writer.status("b" * 64))["loadedCount"], 2)

    async def test_native_ids_preserve_manual_entities_and_detect_changed_owned_row(
        self,
    ):
        (self.root / "configuration.yaml").write_text(
            "knx:\n  light:\n    - name: Manual\n      address: 2049\n"
        )
        with patch.object(
            self.writer, "_compatibility", return_value=m.core_compatibility("2026.8.0")
        ):
            result = await self.writer.deploy(FINGERPRINT)
            self.assertEqual(result["status"], "partial", result)
            self.assertEqual(result["entityCount"], 1)
            self.assertEqual(
                load_config(self.root)["knx"]["light"][0]["name"], "Manual"
            )
            # Removing the package and replacing an owned identity with another row
            # must not transfer ownership of the user's replacement to the writer.
            (self.root / "configuration.yaml").write_text(
                "knx:\n  sensor:\n    - name: Manual replacement\n"
                "      state_address: 1/0/2\n      type: temperature\n"
            )
            retry = await self.writer.deploy(FINGERPRINT)
            self.assertEqual(
                retry["error"], "managed_entity_configuration_conflict", retry
            )
            self.assertEqual(self.reload_calls, 1)

    async def test_preflight_rejects_old_core_and_loaded_schema_before_writes(self):
        with patch.object(
            sys.modules["homeassistant.const"], "__version__", "2025.12.4"
        ):
            unsupported = await self.writer.capabilities()
            self.assertFalse(unsupported["supported"])
            self.assertEqual(
                unsupported["reason"], "home_assistant_version_unsupported"
            )
            self.assertEqual(unsupported["requiredHomeAssistantVersion"], "2026.1.0")
            self.assertIn("2026.1.0", unsupported["message"])
            failure = await self.writer.deploy(FINGERPRINT)
            self.assertEqual(failure["error"], "home_assistant_version_unsupported")
        self.schema_fail = True
        component = types.ModuleType("homeassistant.components.knx")
        component.CONFIG_SCHEMA = lambda config: config
        with patch.dict(sys.modules, {"homeassistant.components.knx": component}):
            unsupported = await self.writer.capabilities()
            self.assertFalse(unsupported["supported"])
            self.assertEqual(unsupported["reason"], "knx_schema_unsupported")
        self.assertEqual(self.reload_calls, 0)
        self.assertFalse((self.root / m.GENERATED).exists())

    async def test_duplicate_native_id_rejected_before_write(self):
        (self.root / "configuration.yaml").write_text(
            "knx:\n  light:\n    - name: One\n      address: 1/0/3\n"
            "    - name: Two\n      address: 2051\n"
        )
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["error"], "duplicate_knx_entity_identity", result)
        self.assertFalse((self.root / m.GENERATED).exists())
        self.assertEqual(self.reload_calls, 0)

    async def test_capability_preflight_does_not_require_loaded_knx(self):
        self.hass.data.clear()
        self.schema_fail = True  # Optional KNX dependencies/schema are unavailable.
        result = await self.writer.capabilities()
        self.assertTrue(result["supported"])
        self.assertEqual(result["version"], 1)
        self.assertFalse((self.root / m.GENERATED).exists())
        self.assertEqual(self.reload_calls, 0)

    async def test_identical_uid_modified_effective_address_is_not_success(self):
        await self.writer.deploy(FINGERPRINT)
        # Disconnect the managed package, supplying the same UID with another GA.
        replacement = deepcopy(PLAN["config"])
        replacement["light"][0]["address"] = "2/0/7"
        (self.root / "configuration.yaml").write_text(
            yaml.safe_dump({"knx": replacement})
        )
        status = await self.writer.status(FINGERPRINT)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "managed_package_not_active")

    async def test_deployment_rejects_unbound_native_project_import(self):
        self.writer.files.write(m.IMPORT_ASSOCIATION, None)
        result = await self.writer.deploy(FINGERPRINT)
        self.assertEqual(result["error"], "project_import_not_bound_to_uploaded_file")
        self.assertFalse((self.root / m.GENERATED).exists())
        self.assertEqual(self.reload_calls, 0)

    async def test_legacy_format_change_is_rejected_before_project_store_and_load(self):
        with (
            patch.object(
                self.writer,
                "_compatibility",
                return_value=m.core_compatibility("2026.1.0"),
            ),
            patch.object(self.writer, "_address_format", return_value="FREE"),
        ):
            self.assertEqual(
                (await self.writer.deploy(FINGERPRINT))["status"], "completed"
            )
            old_project = deepcopy(self.project)
            old_association = self.writer.files.read(m.IMPORT_ASSOCIATION)
            old_yaml = self.writer.files.read(m.GENERATED)
            self.module.project.load_project = Mock(
                side_effect=AssertionError("must not load new project")
            )
            storage = types.ModuleType("homeassistant.helpers.storage")
            storage.Store = Mock(
                side_effect=AssertionError("must not save new project")
            )
            parsed = {"info": {"name": "new", "group_address_style": "ThreeLevel"}}
            with (
                patch.dict(sys.modules, {"homeassistant.helpers.storage": storage}),
                patch.object(
                    self.writer, "_parse_uploaded_project", return_value=parsed
                ),
                self.assertRaisesRegex(
                    m.DeploymentError, "legacy_knx_identity_format_changed"
                ),
            ):
                await self.writer.process_project("new-upload", "", "b" * 64)
            storage.Store.assert_not_called()
            self.module.project.load_project.assert_not_called()
            self.assertEqual(self.project, old_project)
            self.assertEqual(self.writer._address_format(), "FREE")
            self.assertEqual(
                self.writer.files.read(m.IMPORT_ASSOCIATION), old_association
            )
            self.assertEqual(self.writer.files.read(m.GENERATED), old_yaml)
            self.assertEqual(
                (await self.writer.status(FINGERPRINT))["status"], "completed"
            )

    async def test_core_downgrade_rejected_before_parsing_or_storing_new_project(self):
        await self.writer.deploy(FINGERPRINT)  # Custom identities on 2026.9.
        with (
            patch.object(
                self.writer,
                "_compatibility",
                return_value=m.core_compatibility("2026.1.0"),
            ),
            patch.object(self.writer, "_parse_uploaded_project") as parser,
            self.assertRaisesRegex(
                m.DeploymentError, "managed_custom_identity_requires_newer_core"
            ),
        ):
            await self.writer.process_project("new-upload", "", "b" * 64)
        parser.assert_not_called()

    async def test_upgraded_legacy_format_change_requires_completed_registry_migration(
        self,
    ):
        with (
            patch.object(
                self.writer,
                "_compatibility",
                return_value=m.core_compatibility("2026.1.0"),
            ),
            patch.object(self.writer, "_address_format", return_value="FREE"),
        ):
            await self.writer.deploy(FINGERPRINT)
        previous = self.writer.files.read_json(m.MANIFEST)
        parsed = {"info": {"group_address_style": "TwoLevel"}}
        with self.assertRaisesRegex(
            m.DeploymentError, "legacy_knx_identity_format_changed"
        ):
            self.writer._validate_import_identity(previous, parsed, "native")
        for entry in self.registry_entries.values():
            entry.unique_id = "1/0/1" if entry.domain == "light" else "1/0/2"
        self.writer._validate_import_identity(previous, parsed, "native")

    async def test_verified_import_stores_file_and_parsed_project_binding(self):
        project = {"info": {"name": "verified upload"}}
        self.module.project.loaded = False
        self.module.xknx = object()
        saved = []

        class Store:
            def __init__(self, hass, version, key):
                self.version, self.key = version, key

            async def async_save(self, data):
                saved.append(deepcopy(data))

        async def load_project(xknx, data):
            self.project = deepcopy(data)
            self.module.project.loaded = True

        self.module.project.load_project = load_project
        storage = types.ModuleType("homeassistant.helpers.storage")
        storage.Store = Store
        project_api = types.ModuleType("homeassistant.components.knx.project")
        project_api.STORAGE_KEY, project_api.STORAGE_VERSION = "knx/project.json", 1
        with (
            patch.dict(
                sys.modules,
                {
                    "homeassistant.helpers.storage": storage,
                    "homeassistant.components.knx.project": project_api,
                },
            ),
            patch.object(self.writer, "_parse_uploaded_project", return_value=project),
        ):
            result = await self.writer.process_project("file-id", "", FINGERPRINT)
        self.assertEqual(saved, [project])
        self.assertEqual(
            result,
            {
                "projectFingerprint": FINGERPRINT,
                "parsedProjectDigest": m.project_digest(project),
            },
        )
        self.assertEqual(self.writer.files.read_json(m.IMPORT_ASSOCIATION), result)

    async def test_uploaded_file_hash_mismatch_stops_parser(self):
        upload = self.root / "upload.knxproj"
        upload.write_bytes(b"actual uploaded bytes")

        @contextmanager
        def uploaded_file(hass, file_id):
            yield upload

        upload_api = types.ModuleType("homeassistant.components.file_upload")
        upload_api.process_uploaded_file = uploaded_file
        parser_api = types.ModuleType("xknxproject")
        parser_api.XKNXProj = Mock()
        with (
            patch.dict(
                sys.modules,
                {
                    "homeassistant.components.file_upload": upload_api,
                    "xknxproject": parser_api,
                    "xknxproject.exceptions": parser_errors,
                },
            ),
            self.assertRaisesRegex(m.DeploymentError, "fingerprint_mismatch"),
        ):
            self.writer._parse_uploaded_project(self.hass, "file-id", "", FINGERPRINT)
        parser_api.XKNXProj.assert_not_called()

    async def test_uploaded_file_change_during_parse_is_detected(self):
        upload = self.root / "upload.knxproj"
        raw = b"actual uploaded bytes"
        upload.write_bytes(raw)
        self.hass.config.language = "en"

        @contextmanager
        def uploaded_file(hass, file_id):
            yield upload

        def parse():
            upload.write_bytes(b"different file")
            return {"info": {"name": "parsed"}}

        upload_api = types.ModuleType("homeassistant.components.file_upload")
        upload_api.process_uploaded_file = uploaded_file
        parser_api = types.ModuleType("xknxproject")
        parser_api.XKNXProj = Mock(return_value=types.SimpleNamespace(parse=parse))
        with (
            patch.dict(
                sys.modules,
                {
                    "homeassistant.components.file_upload": upload_api,
                    "xknxproject": parser_api,
                    "xknxproject.exceptions": parser_errors,
                },
            ),
            self.assertRaisesRegex(m.DeploymentError, "changed_during_parse"),
        ):
            self.writer._parse_uploaded_project(self.hass, "file-id", "", m.digest(raw))

    async def test_project_password_failures_are_reported_apart(self):
        upload = self.root / "upload.knxproj"
        raw = b"protected uploaded bytes"
        upload.write_bytes(raw)
        self.hass.config.language = "en"

        @contextmanager
        def uploaded_file(hass, file_id):
            yield upload

        upload_api = types.ModuleType("homeassistant.components.file_upload")
        upload_api.process_uploaded_file = uploaded_file
        # xknxproject uses one exception type for both cases.
        for password, failure, expected in (
            ("", "Password required.", "project_password_required"),
            ("wrong", "Invalid password.", "project_password_invalid"),
        ):
            parse = Mock(side_effect=InvalidPasswordException(failure))
            parser_api = types.ModuleType("xknxproject")
            parser_api.XKNXProj = Mock(return_value=types.SimpleNamespace(parse=parse))
            with (
                self.subTest(expected),
                patch.dict(
                    sys.modules,
                    {
                        "homeassistant.components.file_upload": upload_api,
                        "xknxproject": parser_api,
                        "xknxproject.exceptions": parser_errors,
                    },
                ),
                self.assertRaisesRegex(m.DeploymentError, f"^{expected}$"),
            ):
                self.writer._parse_uploaded_project(
                    self.hass, "file-id", password, m.digest(raw)
                )
            parser_api.XKNXProj.assert_called_once_with(
                upload, password=password, language="en"
            )

    async def test_other_parser_failures_are_not_reported_as_password(self):
        upload = self.root / "upload.knxproj"
        raw = b"corrupt uploaded bytes"
        upload.write_bytes(raw)
        self.hass.config.language = "en"

        @contextmanager
        def uploaded_file(hass, file_id):
            yield upload

        upload_api = types.ModuleType("homeassistant.components.file_upload")
        upload_api.process_uploaded_file = uploaded_file
        parse = Mock(side_effect=ValueError("unexpected project content"))
        parser_api = types.ModuleType("xknxproject")
        parser_api.XKNXProj = Mock(return_value=types.SimpleNamespace(parse=parse))
        with (
            patch.dict(
                sys.modules,
                {
                    "homeassistant.components.file_upload": upload_api,
                    "xknxproject": parser_api,
                    "xknxproject.exceptions": parser_errors,
                },
            ),
            # The websocket handler maps this to the generic project_parse_failed.
            self.assertRaises(ValueError),
        ):
            self.writer._parse_uploaded_project(
                self.hass, "file-id", "secret", m.digest(raw)
            )


if __name__ == "__main__":
    unittest.main()


class ManualYamlTests(unittest.TestCase):
    def test_untrusted_yaml_is_rejected(self):
        for source in (
            "knx: !include secret.yaml",
            "knx: &a {light: *a}",
            "knx: {}\nknx: {}",
            "knx: {}\nautomation: []",
            "knx: {shell_command: []}",
            'knx: {scene: [{name: Scene, address: "4/0/1"}]}',
            'knx: {scene: [{name: Scene, address: "4/0/1", scene_number: 0}]}',
            'knx: {light: [{name: Lamp, address: "1/0/1", unique_id: stolen}]}',
        ):
            with self.subTest(source=source), self.assertRaises(m.DeploymentError):
                m.manual_yaml_plan(source)

    def test_equivalent_address_styles_have_same_identity(self):
        a = m.manual_yaml_plan('knx: {light: [{name: Lamp, address: "1/0/1"}]}')
        b = m.manual_yaml_plan('knx: {light: [{name: Lamp, address: "2049"}]}')
        self.assertEqual(a["entities"], b["entities"])
