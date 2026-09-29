"""Safe ETS-to-YAML mapping behavior without a Home Assistant runtime."""

from __future__ import annotations

import copy
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components/rexlite/knx_project_mapper.py"
)
SPEC = importlib.util.spec_from_file_location("rexlite_knx_mapper_test", MODULE_PATH)
assert SPEC and SPEC.loader
mapper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mapper
SPEC.loader.exec_module(mapper)


def ga(address, name, main, sub):
    return {
        "address": address,
        "name": name,
        "dpt": {"main": main, "sub": sub},
        "communication_object_ids": [],
    }


def project(*groups, functions=None, objects=None):
    return {
        "info": {"guid": "test-project-guid"},
        "group_addresses": {g["address"]: g for g in groups},
        "functions": functions or {},
        "communication_objects": objects or {},
    }


def function(kind, **roles):
    return {
        "name": "Defined function",
        "function_type": kind,
        "group_addresses": {r: {"address": a, "role": r} for r, a in roles.items()},
    }


def sample_conventions():
    # Synthetic independent names using the supplied project's naming convention.
    # No user project, location, device addresses or real installation data stored.
    return project(
        ga("1/0/1", "測試主燈 開關", 1, 1),
        ga("1/0/2", "測試主燈 狀態", 1, 11),
        ga("1/0/3", "測試主燈 相對調光", 3, 7),
        ga("1/0/4", "測試主燈 亮度", 5, 1),
        ga("1/0/5", "測試主燈 亮度狀態", 5, 1),
        ga("1/1/1", "測試次燈 開關", 1, 1),
        ga("1/1/2", "測試次燈 狀態", 1, 11),
        ga("2/0/1", "測試窗簾 上下", 1, 8),
        ga("2/0/2", "測試窗簾 停止/微調", 1, 7),
        ga("2/0/3", "測試窗簾 位置", 5, 1),
        ga("2/0/4", "測試窗簾 位置狀態", 5, 1),
        ga("3/0/1", "測試 溫度", 9, 1),
        ga("3/0/2", "測試 濕度", 9, 7),
        ga("4/0/1", "測試 情境", 18, 1),
    )


def abbreviated_light(base="Example-Dali-Light", roles=None):
    """Independent, synthetic model of a six-role actuator plus a display."""
    roles = roles or [
        ("SW", 1, 1),
        ("SW-FB", 1, 11),
        ("VAL", 5, 1),
        ("VAL-FB", 5, 1),
        ("CT", 7, 600),
        ("CT-FB", 7, 600),
    ]
    groups, objects = [], {}
    for number, (role, main, sub) in enumerate(roles, start=1):
        address = f"8/0/{number}"
        groups.append(ga(address, f"{base}-{role}", main, sub))
        objects[f"2.1.5/MD-1_M-1_MI-1_O-{number}_R-{number}"] = {
            "device_address": "2.1.5",
            "channel": "CH-3",
            "text": "Output group A",
            "dpts": [{"main": main, "sub": sub}],
            "flags": {
                "communication": True,
                "read": role.endswith("-FB"),
                "write": not role.endswith("-FB"),
                "transmit": role.endswith("-FB"),
            },
            "group_address_links": [address],
        }
    return project(*groups, objects=objects)


def independent_units(base, unit_roles, count=3):
    """`count` independently-proven actuator channels sharing one literal name
    prefix -- models several real outputs (DALI circuits, air-conditioners)
    wired under the same room/unit label, each with its own device identity so
    `_channel_key` can tell them apart. Returns (project, table) where table
    maps (unit, role) -> the group address created for that role."""
    groups, objects, table = [], {}, {}
    for unit in range(1, count + 1):
        device = f"2.9.{unit}"
        for idx, (role, main, sub) in enumerate(unit_roles, start=1):
            address = f"{unit}/0/{idx}"
            groups.append(ga(address, f"{base}-{role}", main, sub))
            objects[f"{device}/O-{role}"] = {
                "device_address": device,
                "channel": f"CH-{unit}",
                "text": "Output",
                "dpts": [{"main": main, "sub": sub}],
                "flags": {
                    "communication": True,
                    "read": role.endswith("-FB"),
                    "write": not role.endswith("-FB"),
                    "transmit": role.endswith("-FB"),
                },
                "group_address_links": [address],
            }
            table[(unit, role)] = address
    return project(*groups, objects=objects), table


# Room temperature plus setpoint command/feedback, as used on real exports.
CLIMATE_TEMPERATURES = [
    ("Climate-VAL", 9, 1),
    ("Climate-VAL-FB", 9, 1),
    ("Climate-VAL-REAL-FB", 9, 1),
]


# REXLiTE KNX Contract v1 loops: (function type, function name, members).
# Each member is (name suffix, DPT, standard ETS role or "", is feedback).
CONTRACT_V1_LOOPS = [
    (
        "FT-1",
        "1F-玄關-崁燈1",
        [("開關", (1, 1), "SwitchOnOff", False), ("狀態", (1, 1), "InfoOnOff", True)],
    ),
    (
        "FT-6",
        "1F-客廳-吊燈1",
        [
            ("開關", (1, 1), "SwitchOnOff", False),
            ("狀態", (1, 1), "InfoOnOff", True),
            ("亮度", (5, 1), "DimmingValue", False),
            ("亮度狀態", (5, 1), "InfoDimmingValue", True),
            ("相對調光", (3, 7), "DimmingControl", False),
        ],
    ),
    (
        "FT-6",
        "1F-客廳-線燈1",
        [
            ("開關", (1, 1), "SwitchOnOff", False),
            ("狀態", (1, 1), "InfoOnOff", True),
            ("亮度", (5, 1), "DimmingValue", False),
            ("亮度狀態", (5, 1), "InfoDimmingValue", True),
            ("色溫", (7, 600), "", False),
            ("色溫狀態", (7, 600), "", True),
        ],
    ),
    (
        "FT-6",
        "2F-主臥-崁燈1",
        [
            ("開關", (1, 1), "SwitchOnOff", False),
            ("亮度", (5, 1), "DimmingValue", False),
            ("色溫", (5, 1), "", False),
            ("色溫狀態", (5, 1), "", True),
        ],
    ),
    (
        "FT-10",
        "2F-主臥-插座1",
        [("開關", (1, 1), "SwitchOnOff", False), ("狀態", (1, 1), "InfoOnOff", True)],
    ),
    (
        "FT-7",
        "1F-客廳-窗簾1",
        [
            ("上下", (1, 8), "MoveUpDown", False),
            ("停止/微調", (1, 7), "StopStepUpDown", False),
            ("位置", (5, 1), "", False),
            ("位置狀態", (5, 1), "CurrentAbsolutePositionBlindsPercentage", True),
        ],
    ),
    (
        "FT-7",
        "2F-主臥-百葉1",
        [
            ("上下", (1, 8), "MoveUpDown", False),
            ("停止/微調", (1, 7), "StopStepUpDown", False),
            ("位置", (5, 1), "", False),
            ("位置狀態", (5, 1), "CurrentAbsolutePositionBlindsPercentage", True),
            ("葉片角度", (5, 1), "", False),
            ("葉片角度狀態", (5, 1), "CurrentAbsolutePositionSlatPercentage", True),
        ],
    ),
    (
        "FT-7",
        "2F-主臥-窗簾1",
        [
            ("開脈衝", (1, 1), "", False),
            ("關脈衝", (1, 1), "", False),
            ("停脈衝", (1, 1), "", False),
        ],
    ),
    (
        "FT-0",
        "1F-客廳-冷氣1",
        [
            ("開關", (1, 1), "", False),
            ("狀態", (1, 1), "", True),
            ("模式", (20, 105), "", False),
            ("模式狀態", (20, 105), "", True),
            ("風速", (5, 1), "", False),
            ("風速狀態", (5, 1), "", True),
            ("設定溫度", (9, 1), "", False),
            ("設定溫度狀態", (9, 1), "", True),
            ("室溫", (9, 1), "", True),
        ],
    ),
    (
        "FT-9",
        "2F-浴室-地暖1",
        [
            ("室溫", (9, 1), "TempRoom", True),
            ("設定溫度", (9, 1), "", False),
            ("設定溫度狀態", (9, 1), "", True),
            ("運轉模式", (20, 102), "HVACMode", False),
            ("運轉模式狀態", (20, 102), "", True),
        ],
    ),
]


def contract_v1_project():
    """Synthetic xknxproject output shaped like a Contract v1 ETS6 export."""
    groups, objects, functions, table = [], {}, {}, {}
    for loop, (kind, name, members) in enumerate(CONTRACT_V1_LOOPS, start=1):
        device = f"1.1.{loop}"
        refs = {}
        for offset, (suffix, (main, sub), role, feedback) in enumerate(members):
            address = f"{loop}/1/{offset}"
            groups.append(ga(address, f"{name} {suffix}", main, sub))
            objects[f"{device}/O-{offset}"] = {
                "device_address": device,
                "channel": "CH-1",
                "text": suffix,
                "dpts": [{"main": main, "sub": sub}],
                "flags": {
                    "communication": True,
                    "read": feedback,
                    "write": not feedback,
                    "transmit": feedback,
                },
                "group_address_links": [address],
            }
            refs[address] = {"address": address, "name": "", "role": role}
            table[(name, suffix)] = address
        functions[f"F-{loop}"] = {
            "name": name,
            "function_type": kind,
            "group_addresses": refs,
        }
    for suffix, dpt in (("溫度", (9, 1)), ("照度", (9, 4)), ("人體感應", (1, 18))):
        address = f"6/1/{len(table)}"
        groups.append(ga(address, f"1F-客廳-感測器 {suffix}", *dpt))
        objects[f"1.1.99/O-{suffix}"] = {
            "device_address": "1.1.99",
            "channel": "CH-1",
            "text": suffix,
            "dpts": [{"main": dpt[0], "sub": dpt[1]}],
            "flags": {"communication": True, "read": True, "transmit": True},
            "group_address_links": [address],
        }
        table[("1F-客廳-感測器", suffix)] = address
    return project(*groups, functions=functions, objects=objects), table


def has_ha_distribution():
    try:
        importlib.metadata.version("homeassistant")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


class KNXProjectMappingTests(unittest.TestCase):
    def test_abbreviated_roles_merge_full_light_with_display_receivers(self):
        p = abbreviated_light()
        # Both group and receiver declarations may use Status while the
        # producing actuator calls its equivalent on/off feedback Switch.
        p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-2_R-2"]["dpts"] = [
            {"main": 1, "sub": 1}
        ]
        p["communication_objects"]["display"] = {
            "group_address_links": ["8/0/2"],
            "dpts": [{"main": 1, "sub": 1}],
            "flags": {"communication": True, "write": True, "transmit": True},
        }
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(result["mappedAddressCount"], 6)
        light = result["config"]["light"][0]
        self.assertEqual(light["state_address"], "8/0/2")
        self.assertEqual(light["brightness_address"], "8/0/3")
        self.assertEqual(light["brightness_state_address"], "8/0/4")
        self.assertEqual(light["color_temperature_address"], "8/0/5")
        self.assertEqual(light["color_temperature_state_address"], "8/0/6")
        self.assertEqual(light["color_temperature_mode"], "absolute")
        self.assertEqual(result["entities"][0]["source"], "exact-name-object-channel")

    def test_abbreviated_pair_requires_same_device_and_module_instance(self):
        for incompatible in ("3.1.5", "2.1.5/MD-1_M-1_MI-2_O-2_R-2"):
            with self.subTest(incompatible=incompatible):
                p = abbreviated_light()
                objects = p["communication_objects"]
                obj = objects.pop("2.1.5/MD-1_M-1_MI-1_O-2_R-2")
                if "/" in incompatible:
                    objects[incompatible] = obj
                else:
                    obj["device_address"] = incompatible
                    objects["2.1.5/MD-1_M-1_MI-1_O-2_R-2"] = obj
                result = mapper.plan_project(p)
                self.assertNotIn("state_address", result["config"]["light"][0])
                self.assertEqual(
                    result["config"]["binary_sensor"][0]["state_address"], "8/0/2"
                )

    def test_channel_text_fallback_distinguishes_non_modular_outputs(self):
        p = abbreviated_light()
        objects = p["communication_objects"]
        for number in (1, 2):
            obj = objects.pop(f"2.1.5/MD-1_M-1_MI-1_O-{number}_R-{number}")
            obj["channel"] = None
            obj["text"] = "Channel A"
            objects[f"2.1.5/O-{number}"] = obj
        first = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(first["state_address"], "8/0/2")
        objects["2.1.5/O-2"]["text"] = "Channel B"
        second = mapper.plan_project(p)["config"]["switch"][0]
        self.assertNotIn("state_address", second)

    def test_switch_status_conflict_not_relaxed_without_same_channel_proof(self):
        p = abbreviated_light()
        obj = p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-2_R-2"]
        obj["dpts"] = [{"main": 1, "sub": 1}]
        obj["device_address"] = "3.1.5"
        result = mapper.plan_project(p)
        self.assertNotIn("state_address", result["config"]["light"][0])
        self.assertIn(
            {"address": "8/0/2", "reason": "conflicting_datapoint_type"},
            result["skipped"],
        )

    def test_true_optional_dpt_conflict_does_not_erase_proven_light(self):
        for main, sub in ((5, 4), (9, 1)):
            with self.subTest(dpt=(main, sub)):
                p = abbreviated_light()
                p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-3_R-3"]["dpts"] = [
                    {"main": main, "sub": sub}
                ]
                result = mapper.plan_project(p)
                light = result["config"]["light"][0]
                self.assertEqual(light["state_address"], "8/0/2")
                self.assertNotIn("brightness_address", light)
                self.assertNotIn("brightness_state_address", light)
                self.assertEqual(light["color_temperature_address"], "8/0/5")
                self.assertIn(
                    {"address": "8/0/3", "reason": "conflicting_datapoint_type"},
                    result["skipped"],
                )

    def test_brightness_color_spelling_maps_the_same_as_val_ct(self):
        p = abbreviated_light(
            base="2F-01G",
            roles=[
                ("SW", 1, 1),
                ("SW-FB", 1, 11),
                ("Brightness", 5, 1),
                ("Brightness-FB", 5, 1),
                ("Color", 7, 600),
                ("Color-FB", 7, 600),
            ],
        )
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["brightness_address"], "8/0/3")
        self.assertEqual(light["brightness_state_address"], "8/0/4")
        self.assertEqual(light["color_temperature_address"], "8/0/5")

    def test_terse_name_with_a_proven_brightness_channel_is_a_light(self):
        p = abbreviated_light(
            base="Loop-A CC-1CT",
            roles=[("SW", 1, 1), ("SW-FB", 1, 11), ("Value", 5, 1), ("Value-FB", 5, 1)],
        )
        result = mapper.plan_project(p)
        self.assertNotIn("switch", result["config"])
        light = result["config"]["light"][0]
        self.assertEqual(light["brightness_address"], "8/0/3")

    def test_value_carrying_a_temperature_datapoint_never_becomes_brightness(self):
        p = abbreviated_light(
            base="AC-01",
            roles=[("SW", 1, 1), ("SW-FB", 1, 11), ("Value", 9, 1), ("Value-FB", 9, 1)],
        )
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])
        switch = result["config"]["switch"][0]
        self.assertEqual(switch["address"], "8/0/1")
        self.assertNotIn("brightness_address", switch)

    def test_relative_color_temperature_maps_with_relative_mode(self):
        p = abbreviated_light()
        for address in ("8/0/5", "8/0/6"):
            p["group_addresses"][address]["dpt"] = {"main": 5, "sub": 1}
            p["communication_objects"][
                f"2.1.5/MD-1_M-1_MI-1_O-{address[-1]}_R-{address[-1]}"
            ]["dpts"] = [{"main": 5, "sub": 1}]
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["color_temperature_address"], "8/0/5")
        self.assertEqual(light["color_temperature_state_address"], "8/0/6")
        self.assertEqual(light["color_temperature_mode"], "relative")

    def test_switch_feedback_tolerates_a_bool_listener_on_the_same_channel(self):
        p = abbreviated_light()
        # The State object stays; a logic-block Bool listener also subscribes.
        p["communication_objects"]["logic"] = {
            "device_address": "2.1.9",
            "group_address_links": ["8/0/2"],
            "dpts": [{"main": 1, "sub": 2}],
            "flags": {"communication": True, "write": True, "transmit": True},
        }
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["state_address"], "8/0/2")

    def test_incompatible_color_temperature_preserves_brightness_channel(self):
        p = abbreviated_light()
        p["group_addresses"]["8/0/5"]["dpt"] = {"main": 5, "sub": 1}
        result = mapper.plan_project(p)
        light = result["config"]["light"][0]
        self.assertEqual(light["brightness_state_address"], "8/0/4")
        self.assertNotIn("color_temperature_address", light)
        self.assertNotIn("color_temperature_state_address", light)
        self.assertNotIn("color_temperature_mode", light)

    def test_explicit_ga_selects_declared_alternative_but_never_incompatible_object(
        self,
    ):
        p = abbreviated_light()
        obj = p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-3_R-3"]
        obj["dpts"] = [{"main": 5, "sub": 1}, {"main": 5, "sub": 4}]
        light = mapper.plan_project(p)["config"]["light"][0]
        self.assertEqual(light["brightness_address"], "8/0/3")
        p["communication_objects"]["other"] = {
            "group_address_links": ["8/0/3"],
            "dpts": [{"main": 5, "sub": 4}],
        }
        self.assertNotIn(
            "brightness_address", mapper.plan_project(p)["config"]["light"][0]
        )

    def test_feedback_input_with_write_flag_is_observation_only(self):
        p = project(
            ga("8/1/1", "Example-ReedSensor-ACTIVE-FB", 1, 1),
            objects={
                "contact": {
                    "group_address_links": ["8/1/1"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {
                        "communication": True,
                        "write": True,
                        "transmit": True,
                        "read": False,
                    },
                }
            },
        )
        result = mapper.plan_project(p)
        self.assertNotIn("switch", result["config"])
        self.assertEqual(result["config"]["binary_sensor"][0]["sync_state"], False)
        p["communication_objects"]["contact"]["flags"]["transmit"] = False
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_duplicate_optional_role_is_not_selected_arbitrarily(self):
        p = abbreviated_light()
        p["group_addresses"]["8/0/7"] = ga("8/0/7", "Example-Dali-Light-VAL", 5, 1)
        result = mapper.plan_project(p)
        light = result["config"]["light"][0]
        self.assertEqual(light["state_address"], "8/0/2")
        self.assertNotIn("brightness_address", light)
        self.assertEqual({s["address"] for s in result["skipped"]}, {"8/0/3", "8/0/7"})

    def test_abbreviations_without_actuator_objects_never_join_by_name_alone(self):
        p = abbreviated_light()
        p["communication_objects"] = {}
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])

    def test_fanout_command_never_uses_one_actuators_feedback_as_aggregate(self):
        p = abbreviated_light()
        extra = copy.deepcopy(p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-1_R-1"])
        extra["device_address"] = "2.1.6"
        p["communication_objects"]["2.1.6/MD-1_M-1_MI-1_O-1_R-1"] = extra
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])
        self.assertNotIn("state_address", result["config"]["switch"][0])

    def test_multiple_feedback_producers_never_pick_first_matching_channel(self):
        p = abbreviated_light()
        extra = copy.deepcopy(p["communication_objects"]["2.1.5/MD-1_M-1_MI-1_O-2_R-2"])
        extra["device_address"] = "2.1.6"
        p["communication_objects"]["2.1.6/MD-1_M-1_MI-1_O-2_R-2"] = extra
        result = mapper.plan_project(p)
        self.assertNotIn("state_address", result["config"]["light"][0])

    def test_command_only_abbreviation_preserves_existing_switch_fallback(self):
        p = abbreviated_light()
        p["group_addresses"] = {"8/0/1": p["group_addresses"]["8/0/1"]}
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])
        self.assertEqual(result["config"]["switch"][0]["address"], "8/0/1")

    def test_multiple_circuits_share_one_room_name_split_by_proven_channel(self):
        # Three independent DALI outputs installed under one shared room
        # label ("2F-01L") must not be refused as one big ambiguous group;
        # each proven actuator channel gets its own light.
        p, table = independent_units("2F-01L", [("SW", 1, 1), ("VAL", 5, 1)], count=3)
        result = mapper.plan_project(p)
        self.assertNotIn("switch", result["config"])
        lights = result["config"]["light"]
        self.assertEqual(len(lights), 3)
        self.assertEqual(result["skipped"], [])
        for unit in (1, 2, 3):
            light = next(
                light for light in lights if light["address"] == table[(unit, "SW")]
            )
            self.assertEqual(light["brightness_address"], table[(unit, "VAL")])

    def test_multi_circuit_command_channel_collision_blocks_just_those_commands(self):
        # Two group addresses wired to the exact same producing object are
        # genuinely indistinguishable channels -- never guess between them,
        # even though the multi-circuit split above proves distinct ones.
        p, table = independent_units("2F-01L", [("SW", 1, 1)], count=1)
        dup = copy.deepcopy(p["group_addresses"][table[(1, "SW")]])
        dup["address"] = "9/0/9"
        p["group_addresses"]["9/0/9"] = dup
        obj_id = next(iter(p["communication_objects"]))
        p["communication_objects"][obj_id]["group_address_links"].append("9/0/9")
        result = mapper.plan_project(p)
        self.assertNotIn("light", result["config"])
        self.assertNotIn("switch", result["config"])
        self.assertEqual(
            {s["reason"] for s in result["skipped"]},
            {"ambiguous_duplicate_function_role"},
        )

    def test_multi_circuit_optional_role_binds_only_its_own_command(self):
        # Unit 2's brightness channel is unprovable; unit 1's command must
        # still get its own brightness pairing instead of both being blocked.
        p, table = independent_units("2F-01L", [("SW", 1, 1), ("VAL", 5, 1)], count=2)
        unproven = table[(2, "VAL")]
        del p["group_addresses"][unproven]
        for obj_id, obj in list(p["communication_objects"].items()):
            if unproven in obj.get("group_address_links", []):
                del p["communication_objects"][obj_id]
        result = mapper.plan_project(p)
        lights = result["config"]["light"]
        self.assertEqual(len(lights), 1)
        self.assertEqual(lights[0]["address"], table[(1, "SW")])
        self.assertEqual(lights[0]["brightness_address"], table[(1, "VAL")])
        switches = result["config"]["switch"]
        self.assertEqual(len(switches), 1)
        self.assertEqual(switches[0]["address"], table[(2, "SW")])

    def test_climate_dialect_maps_on_off_mode_fan_and_temperatures(self):
        p, table = independent_units(
            "13F-A12",
            [
                ("Climate-SW", 1, 1),
                ("Climate-SW-FB", 1, 11),
                ("Climate-MODE", 20, 105),
                ("Climate-MODE-FB", 20, 105),
                ("Climate-FAN", 5, 1),
                ("Climate-FAN-FB", 5, 1),
                *CLIMATE_TEMPERATURES,
            ],
            count=1,
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        climate = result["config"]["climate"][0]
        self.assertEqual(climate["on_off_address"], table[(1, "Climate-SW")])
        self.assertEqual(climate["on_off_state_address"], table[(1, "Climate-SW-FB")])
        self.assertEqual(climate["controller_mode_address"], table[(1, "Climate-MODE")])
        self.assertEqual(
            climate["controller_mode_state_address"], table[(1, "Climate-MODE-FB")]
        )
        self.assertEqual(climate["fan_speed_address"], table[(1, "Climate-FAN")])
        self.assertEqual(
            climate["fan_speed_state_address"], table[(1, "Climate-FAN-FB")]
        )
        self.assertEqual(
            climate["temperature_address"], table[(1, "Climate-VAL-REAL-FB")]
        )
        self.assertEqual(
            climate["target_temperature_address"], table[(1, "Climate-VAL")]
        )
        self.assertEqual(
            climate["target_temperature_state_address"],
            table[(1, "Climate-VAL-FB")],
        )

    def test_climate_dialect_without_temperatures_never_emits_invalid_climate(self):
        # HA's KNX schema requires temperature_address and
        # target_temperature_state_address; one invalid climate row would make
        # the whole deployment fail schema validation.
        p, table = independent_units(
            "13F-A12",
            [
                ("Climate-SW", 1, 1),
                ("Climate-MODE", 20, 105),
                ("Climate-FAN", 5, 1),
                ("Climate-VAL-FB", 9, 1),
            ],
            count=1,
        )
        result = mapper.plan_project(p)
        self.assertNotIn("climate", result["config"])
        self.assertEqual(
            result["config"]["switch"][0]["address"], table[(1, "Climate-SW")]
        )

    def test_climate_dialect_byte_count_value_is_not_a_setpoint(self):
        p, _ = independent_units(
            "13F-A12",
            [
                ("Climate-SW", 1, 1),
                ("Climate-MODE", 20, 105),
                ("Climate-VAL", 5, 10),
                ("Climate-VAL-FB", 5, 10),
                ("Climate-VAL-REAL-FB", 9, 1),
            ],
            count=1,
        )
        self.assertNotIn("climate", mapper.plan_project(p)["config"])

    def test_climate_dialect_without_mode_or_fan_falls_back_to_plain_switch(self):
        # A bare on/off pair is not distinctly a climate device; do not
        # manufacture a control-less climate entity from it.
        p, table = independent_units("13F-A12", [("Climate-SW", 1, 1)], count=1)
        result = mapper.plan_project(p)
        self.assertNotIn("climate", result["config"])
        self.assertEqual(
            result["config"]["switch"][0]["address"], table[(1, "Climate-SW")]
        )

    def test_climate_dialect_rejects_non_standard_fan_datapoint(self):
        # DPT 5.010 is a raw byte count, not the DPT 5.001 percentage HA's fan
        # speed expects; a mismatched fan datapoint must not be guessed into
        # the entity, even though the proven mode channel still forms one.
        p, table = independent_units(
            "13F-A12",
            [
                ("Climate-SW", 1, 1),
                ("Climate-MODE", 20, 105),
                ("Climate-FAN", 5, 10),
                *CLIMATE_TEMPERATURES,
            ],
            count=1,
        )
        result = mapper.plan_project(p)
        climate = result["config"]["climate"][0]
        self.assertNotIn("fan_speed_address", climate)
        self.assertEqual(climate["controller_mode_address"], table[(1, "Climate-MODE")])
        # The unmatched fan address is never silently folded into any entity;
        # fallback() is free to give it its own (unrelated-role) skip reason.
        fan_address = table[(1, "Climate-FAN")]
        self.assertIn(fan_address, {s["address"] for s in result["skipped"]})

    def test_multiple_climate_units_share_one_prefix_split_by_proven_channel(self):
        p, table = independent_units(
            "13F-A12",
            [("Climate-SW", 1, 1), ("Climate-MODE", 20, 105), *CLIMATE_TEMPERATURES],
            count=2,
        )
        result = mapper.plan_project(p)
        climates = result["config"]["climate"]
        self.assertEqual(len(climates), 2)
        self.assertEqual(
            {c["on_off_address"] for c in climates},
            {table[(1, "Climate-SW")], table[(2, "Climate-SW")]},
        )

    def test_explicit_sender_scene_parameter_allows_recall_compatible_types(self):
        group = ga("8/2/1", "Example arbitrary scene label", 18, 1)
        group.update(
            scene_number=6,
            scene_number_source="ets-sender-parameter",
            scene_sender_ids=["sender"],
        )
        p = project(
            group,
            objects={
                "sender": {
                    "group_address_links": ["8/2/1"],
                    "dpts": [{"main": 18, "sub": 1}],
                    "flags": {"communication": True, "transmit": True},
                },
                "receiver": {
                    "group_address_links": ["8/2/1"],
                    "dpts": [{"main": 17, "sub": 1}],
                    "flags": {"communication": True, "write": True},
                },
            },
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(result["config"]["scene"][0]["scene_number"], 6)
        self.assertEqual(result["entities"][0]["source"], "ets-sender-scene-parameter")
        for invalid in (None, 0, 65, True, "6"):
            group["scene_number"] = invalid
            self.assertEqual(mapper.plan_project(p)["entityCount"], 0)
        group["scene_number"] = 6
        for change in (
            {"scene_number_source": "name"},
            {"scene_sender_ids": []},
            {"scene_sender_ids": ["not-linked"]},
            {"scene_sender_ids": [None]},
        ):
            broken = copy.deepcopy(p)
            broken["group_addresses"]["8/2/1"].update(change)
            self.assertEqual(mapper.plan_project(broken)["entityCount"], 0)
        for change in (
            {"transmit": False},
            {"communication": False},
        ):
            broken = copy.deepcopy(p)
            broken["communication_objects"]["sender"]["flags"].update(change)
            self.assertEqual(mapper.plan_project(broken)["entityCount"], 0)
        p["communication_objects"]["receiver"]["flags"]["write"] = False
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)
        p["communication_objects"]["receiver"]["flags"]["write"] = True
        p["communication_objects"]["receiver"]["dpts"] = [{"main": 5, "sub": 1}]
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_exact_conventions_map_five_entities_and_leave_scene_unresolved(self):
        result = mapper.plan_project(sample_conventions())
        self.assertEqual(result["entityCount"], 5)
        self.assertEqual(
            {k: len(v) for k, v in result["config"].items()},
            {"light": 2, "cover": 1, "sensor": 2},
        )
        self.assertEqual(result["config"]["light"][0]["brightness_address"], "1/0/4")
        self.assertEqual(result["config"]["light"][0]["state_address"], "1/0/2")
        cover = result["config"]["cover"][0]
        self.assertEqual(cover["move_short_address"], "2/0/2")
        self.assertNotIn("stop_address", cover)  # DPT 1.007 is step/stop, not 1.010.
        self.assertEqual({s["address"] for s in result["skipped"]}, {"1/0/3", "4/0/1"})
        self.assertEqual(
            next(s for s in result["skipped"] if s["address"] == "4/0/1")["reason"],
            "missing_explicit_scene_number",
        )

    def test_shade_close_step_pos_labels_map_one_cover(self):
        result = mapper.plan_project(
            project(
                ga("2/1/0", "Example-MatterShade-Blind-Close", 1, 8),
                ga("2/1/1", "Example-MatterShade-Blind-Step", 1, 7),
                ga("2/1/2", "Example-MatterShade-Blind-POS", 5, 1),
            )
        )
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(result["skipped"], [])
        cover = result["config"]["cover"][0]
        self.assertEqual(cover["move_long_address"], "2/1/0")
        self.assertEqual(cover["move_short_address"], "2/1/1")
        self.assertEqual(cover["position_address"], "2/1/2")

    def test_shade_close_label_with_wrong_datapoint_type_is_not_a_cover(self):
        # An impulse-relay "Close" (DPT 1.001) is not the bi-directional up/down
        # datapoint; the role guard rejects it instead of inventing a cover.
        result = mapper.plan_project(
            project(
                ga("2/1/0", "Relay-Blind-Close", 1, 1),
                ga("2/1/1", "Relay-Blind-Open", 1, 1),
            )
        )
        self.assertNotIn("cover", result["config"])

    def test_command_status_labels_map_one_dimmable_light(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Living Room Light - Command", 1, 1),
                ga("1/0/2", "Living Room Light - Status", 1, 1),
                ga("1/1/1", "Living Room Light - Brightness Command", 5, 1),
                ga("1/1/2", "Living Room Light - Brightness Status", 5, 1),
            )
        )
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(result["skipped"], [])
        light = result["config"]["light"][0]
        self.assertEqual(
            {
                k: light[k]
                for k in (
                    "address",
                    "state_address",
                    "brightness_address",
                    "brightness_state_address",
                )
            },
            {
                "address": "1/0/1",
                "state_address": "1/0/2",
                "brightness_address": "1/1/1",
                "brightness_state_address": "1/1/2",
            },
        )
        self.assertEqual(result["entities"][0]["source"], "exact-name-role")

    def test_chinese_command_label_keeps_the_prefix_before_the_switch_word(self):
        result = mapper.plan_project(
            project(
                ga("2/0/1", "客廳主燈 開關指令", 1, 1),
                ga("2/0/2", "客廳主燈 狀態", 1, 1),
                ga("2/0/3", "客廳主燈 亮度指令", 5, 1),
                ga("2/0/4", "客廳主燈 亮度狀態", 5, 1),
            )
        )
        light = result["config"]["light"][0]
        self.assertEqual(light["name"], "客廳主燈")
        self.assertEqual(light["address"], "2/0/1")
        self.assertEqual(light["brightness_address"], "2/0/3")

    def test_command_label_with_wrong_datapoint_type_blocks_the_prefix(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Hall Light - Command", 3, 7),
                ga("1/0/2", "Hall Light - Status", 1, 1),
            )
        )
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(
            {s["reason"] for s in result["skipped"]}, {"role_datapoint_type_mismatch"}
        )

    def test_fb_named_binary_address_without_objects_is_a_read_only_sensor(self):
        result = mapper.plan_project(
            project(ga("1/1/0", "Zone-B-DoorSensor-B-Sensor-ACTIVE-FB", 1, 1))
        )
        sensor = result["config"]["binary_sensor"][0]
        self.assertEqual(sensor["state_address"], "1/1/0")
        self.assertIs(sensor["sync_state"], False)
        self.assertNotIn("switch", result["config"])

    def test_fb_name_alone_never_infers_a_sensor_when_objects_disagree(self):
        # A writable command object on the same address still wins over the name.
        p = project(
            ga("1/1/0", "Relay-FB", 1, 1),
            objects={
                "cmd": {
                    "group_address_links": ["1/1/0"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {"communication": True, "write": True, "transmit": False},
                }
            },
        )
        result = mapper.plan_project(p)
        self.assertNotIn("binary_sensor", result["config"])

    def test_no_dpt_one_switch_or_scene_number_guessed_from_address(self):
        result = mapper.plan_project(
            project(ga("1/0/1", "Unknown", 1, 1), ga("4/0/13", "Scene 13", 18, 1))
        )
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(len(result["skipped"]), 2)

    def test_conflicting_named_feedback_blocks_whole_channel(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Main light switch", 1, 1),
                ga("1/0/2", "Main light status", 9, 1),
            )
        )
        self.assertEqual(result["entityCount"], 0)
        self.assertTrue(
            all(
                s["reason"] == "role_datapoint_type_mismatch" for s in result["skipped"]
            )
        )

    def test_duplicate_role_does_not_pick_first_address(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Main light switch", 1, 1),
                ga("1/0/2", "Main light switch", 1, 1),
            )
        )
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(
            result["skipped"][0]["reason"], "ambiguous_duplicate_function_role"
        )

    def test_missing_name_never_infers_control_pair(self):
        result = mapper.plan_project(
            project(ga("1/0/1", "", 1, 1), ga("1/0/2", "", 1, 11))
        )
        self.assertNotIn("switch", result["config"])
        self.assertNotIn("light", result["config"])
        self.assertEqual(result["entityCount"], 1)  # Observation-only DPT Status.

    def test_similar_names_do_not_merge_channels(self):
        result = mapper.plan_project(
            project(
                ga("1/0/1", "Main light switch", 1, 1),
                ga("1/0/2", "Main light 2 status", 1, 11),
            )
        )
        self.assertNotIn("state_address", result["config"]["light"][0])
        self.assertEqual(result["config"]["binary_sensor"][0]["state_address"], "1/0/2")

    def test_contract_v1_home_maps_every_loop_without_unexpected_skips(self):
        p, table = contract_v1_project()
        result = mapper.plan_project(p)
        by_name = {e["name"]: e for e in result["entities"]}
        # The dry-contact curtain is three buttons, not one entity; checked below.
        loops = [loop for loop in CONTRACT_V1_LOOPS if loop[1] != "2F-主臥-窗簾1"]
        self.assertEqual(
            {name: by_name[name]["platform"] for _, name, _ in loops},
            {
                "1F-玄關-崁燈1": "light",
                "1F-客廳-吊燈1": "light",
                "1F-客廳-線燈1": "light",
                "2F-主臥-崁燈1": "light",
                "2F-主臥-插座1": "switch",
                "1F-客廳-窗簾1": "cover",
                "2F-主臥-百葉1": "cover",
                "1F-客廳-冷氣1": "climate",
                "2F-浴室-地暖1": "climate",
            },
        )
        self.assertTrue(
            all(by_name[name]["source"] == "ets-function-role" for _, name, _ in loops)
        )
        buttons = {row["name"]: row for row in result["config"]["button"]}
        self.assertEqual(
            buttons,
            {
                f"2F-主臥-窗簾1 {label}": {
                    "name": f"2F-主臥-窗簾1 {label}",
                    "unique_id": by_name[f"2F-主臥-窗簾1 {label}"]["uniqueId"],
                    "address": table[("2F-主臥-窗簾1", suffix)],
                }
                for label, suffix in (
                    ("開", "開脈衝"),
                    ("關", "關脈衝"),
                    ("停", "停脈衝"),
                )
            },
        )
        # Only the deliberately non-entity relative-dimming address is left.
        self.assertEqual(
            result["skipped"],
            [
                {
                    "address": table[("1F-客廳-吊燈1", "相對調光")],
                    "reason": "function_role_not_exposed",
                }
            ],
        )
        lights = {row["name"]: row for row in result["config"]["light"]}
        self.assertEqual(lights["1F-客廳-線燈1"]["color_temperature_mode"], "absolute")
        self.assertEqual(
            lights["1F-客廳-線燈1"]["color_temperature_state_address"],
            table[("1F-客廳-線燈1", "色溫狀態")],
        )
        self.assertEqual(lights["2F-主臥-崁燈1"]["color_temperature_mode"], "relative")
        cover = {row["name"]: row for row in result["config"]["cover"]}["2F-主臥-百葉1"]
        self.assertEqual(cover["position_address"], table[("2F-主臥-百葉1", "位置")])
        self.assertEqual(cover["angle_address"], table[("2F-主臥-百葉1", "葉片角度")])
        climates = {row["name"]: row for row in result["config"]["climate"]}
        aircon = climates["1F-客廳-冷氣1"]
        self.assertEqual(aircon["on_off_address"], table[("1F-客廳-冷氣1", "開關")])
        self.assertEqual(
            aircon["controller_mode_address"], table[("1F-客廳-冷氣1", "模式")]
        )
        self.assertEqual(
            aircon["temperature_address"], table[("1F-客廳-冷氣1", "室溫")]
        )
        heating = climates["2F-浴室-地暖1"]
        self.assertEqual(
            heating["operation_mode_address"], table[("2F-浴室-地暖1", "運轉模式")]
        )
        self.assertEqual(
            heating["target_temperature_state_address"],
            table[("2F-浴室-地暖1", "設定溫度狀態")],
        )
        sensors = {row["name"] for row in result["config"]["sensor"]}
        self.assertLessEqual({"1F-客廳-感測器 溫度", "1F-客廳-感測器 照度"}, sensors)

    def test_partial_impulse_curtain_is_rejected_not_guessed(self):
        p, table = contract_v1_project()
        stop = table[("2F-主臥-窗簾1", "停脈衝")]
        function = next(
            f for f in p["functions"].values() if f["name"] == "2F-主臥-窗簾1"
        )
        del function["group_addresses"][stop]
        del p["group_addresses"][stop]
        result = mapper.plan_project(p)
        self.assertNotIn("button", result["config"])
        self.assertIn(
            {
                "address": table[("2F-主臥-窗簾1", "開脈衝")],
                "reason": "incompatible_function_roles",
            },
            result["skipped"],
        )

    def test_function_member_name_must_share_the_function_prefix(self):
        p, table = contract_v1_project()
        position = table[("1F-客廳-窗簾1", "位置")]
        p["group_addresses"][position]["name"] = "1F-客廳-窗簾2 位置"
        result = mapper.plan_project(p)
        cover = {row["name"]: row for row in result["config"]["cover"]}["1F-客廳-窗簾1"]
        self.assertNotIn("position_address", cover)
        self.assertNotIn(
            position, {a for e in result["entities"] for a in e["addresses"]}
        )

    def test_function_member_without_role_or_convention_is_reported(self):
        p, table = contract_v1_project()
        address = table[("1F-客廳-冷氣1", "風速")]
        p["group_addresses"][address]["name"] = "Fan level"
        result = mapper.plan_project(p)
        self.assertIn(
            {"address": address, "reason": "unresolved_function_role"},
            result["skipped"],
        )
        self.assertNotIn(
            "fan_speed_address",
            {row["name"]: row for row in result["config"]["climate"]}["1F-客廳-冷氣1"],
        )

    def test_function_colour_temperature_feedback_must_match_command_encoding(self):
        p, table = contract_v1_project()
        state = table[("1F-客廳-線燈1", "色溫狀態")]
        p["group_addresses"][state]["dpt"] = {"main": 5, "sub": 1}
        p["communication_objects"]["1.1.3/O-5"]["dpts"] = [{"main": 5, "sub": 1}]
        result = mapper.plan_project(p)
        self.assertNotIn("1F-客廳-線燈1", {e["name"] for e in result["entities"]})
        self.assertIn(
            {"address": state, "reason": "role_datapoint_type_mismatch"},
            result["skipped"],
        )

    def test_colour_temperature_on_a_non_light_function_is_not_a_switch(self):
        p = project(
            ga("1/0/1", "Relay 開關", 1, 1),
            ga("1/0/2", "Relay 色溫", 7, 600),
            functions={
                "F1": {
                    "name": "Relay",
                    "function_type": "FT-10",
                    "group_addresses": {
                        "1/0/1": {"address": "1/0/1", "role": "SwitchOnOff"},
                        "1/0/2": {"address": "1/0/2", "role": ""},
                    },
                }
            },
        )
        self.assertNotIn("switch", mapper.plan_project(p)["config"])

    def test_formal_ets_function_roles_are_preferred_to_names(self):
        p = project(
            ga("1/0/1", "A", 1, 1),
            ga("1/0/2", "B", 1, 11),
            ga("1/0/3", "C", 5, 1),
            ga("1/0/4", "D", 5, 1),
            functions={
                "F1": function(
                    "DimmableLight",
                    SwitchOnOff="1/0/1",
                    InfoOnOff="1/0/2",
                    AbsoluteSetvalueControl="1/0/3",
                    ActualDimmingValue="1/0/4",
                )
            },
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        self.assertEqual(
            result["config"]["light"][0]["brightness_state_address"], "1/0/4"
        )
        self.assertEqual(result["entities"][0]["source"], "ets-function-role")

    def test_missing_subtype_never_defaults_to_switch(self):
        result = mapper.plan_project(project(ga("1/0/1", "Main light switch", 1, None)))
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "missing_exact_datapoint_type")

    def test_conflicting_object_dpt_cannot_override_group_metadata(self):
        p = project(
            ga("1/0/1", "Main light switch", 1, 1),
            objects={
                "CO1": {
                    "group_address_links": ["1/0/1"],
                    "dpts": [{"main": 9, "sub": 1}],
                    "flags": {"communication": True, "write": True},
                }
            },
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "conflicting_datapoint_type")

    def test_switch_flags_prove_write_and_unanimous_dpt_can_fill_missing_group_dpt(
        self,
    ):
        group = ga("1/0/1", "Actuator", 1, 1)
        group["dpt"] = None
        p = project(
            group,
            objects={
                "CO1": {
                    "group_address_links": ["1/0/1"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {"communication": True, "write": True},
                }
            },
        )
        self.assertEqual(
            mapper.plan_project(p)["config"]["switch"][0]["address"], "1/0/1"
        )

    def test_transmit_only_object_never_becomes_write_control(self):
        p = project(
            ga("1/0/1", "Main light switch", 1, 1),
            objects={
                "CO1": {
                    "group_address_links": ["1/0/1"],
                    "dpts": [{"main": 1, "sub": 1}],
                    "flags": {"communication": True, "transmit": True, "write": False},
                }
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_climate_requires_actual_and_target_feedback_plus_target_control(self):
        p = project(
            ga("1/0/1", "AC 室內溫度", 9, 1),
            ga("1/0/2", "AC 目標溫度", 9, 1),
            ga("1/0/3", "AC 目標溫度狀態", 9, 1),
            ga("1/0/4", "AC 模式", 20, 105),
            ga("1/0/5", "AC 模式狀態", 20, 105),
            ga("1/0/6", "AC 開關", 1, 1),
        )
        result = mapper.plan_project(p)
        self.assertEqual(result["entityCount"], 1)
        climate = result["config"]["climate"][0]
        self.assertEqual(climate["temperature_address"], "1/0/1")
        self.assertEqual(climate["target_temperature_state_address"], "1/0/3")
        self.assertEqual(climate["on_off_address"], "1/0/6")
        del p["group_addresses"]["1/0/3"]
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_scene_explicit_structured_number_is_required_and_bounded(self):
        p = project(
            ga("1/0/1", "Scene bus", 18, 1),
            functions={"F1": function("Scene", Scene="1/0/1")},
        )
        for invalid in (None, 0, 65, True, "13"):
            p["functions"]["F1"]["scene_number"] = invalid
            self.assertEqual(mapper.plan_project(p)["entityCount"], 0)
        p["functions"]["F1"]["scene_number"] = 13
        self.assertEqual(
            mapper.plan_project(p)["config"]["scene"][0]["scene_number"], 13
        )

    def test_equivalent_address_styles_have_same_identity(self):
        a = mapper.plan_project(project(ga("1/0/1", "A switch", 1, 1)))
        b = mapper.plan_project(project(ga("2049", "A switch", 1, 1)))
        self.assertEqual(a["entities"][0]["uniqueId"], b["entities"][0]["uniqueId"])
        self.assertEqual(b["config"]["switch"][0]["address"], "1/0/1")

    def test_output_is_json_compatible_stable_and_does_not_mutate_input(self):
        p = sample_conventions()
        original = copy.deepcopy(p)
        a = mapper.plan_project(p)
        p["group_addresses"] = dict(reversed(list(p["group_addresses"].items())))
        b = mapper.plan_project(p)
        self.assertEqual(a, b)
        self.assertEqual(json.loads(json.dumps(a)), a)
        self.assertEqual(p, original)
        for entity in a["entities"]:
            item = next(
                c
                for c in a["config"][entity["platform"]]
                if c["name"] == entity["name"]
            )
            self.assertEqual(item["unique_id"], entity["uniqueId"])

    def test_disagreeing_function_claims_reject_both(self):
        p = project(
            ga("1/0/1", "Command", 1, 1),
            ga("1/0/2", "Status", 1, 11),
            functions={
                "F1": function("SwitchableLight", SwitchOnOff="1/0/1"),
                "F2": function(
                    "SwitchableLight", SwitchOnOff="1/0/1", InfoOnOff="1/0/2"
                ),
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_invalid_address_and_bool_dpt_rejected(self):
        for address in ("32/0/1", "1/8/1", "1/0/256", "0/0/0", "1/-1/1", "__bad__"):
            self.assertEqual(
                mapper.plan_project(project(ga(address, "Main light switch", 1, 1)))[
                    "entityCount"
                ],
                0,
            )
        self.assertEqual(
            mapper.plan_project(project(ga("1/0/1", "Main light switch", True, 1)))[
                "entityCount"
            ],
            0,
        )

    def test_missing_feedback_dpt_blocks_the_whole_named_control(self):
        p = project(
            ga("1/0/1", "Main light switch", 1, 1),
            ga("1/0/2", "Main light status", 1, None),
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_missing_project_id_keeps_entities_stable_when_an_address_is_added(self):
        p = project(ga("1/0/1", "A switch", 1, 1))
        p.pop("info")
        first = mapper.plan_project(p)
        p["group_addresses"]["3/0/1"] = ga("3/0/1", "Temperature", 9, 1)
        second = mapper.plan_project(p)
        self.assertEqual(first["projectId"], second["projectId"])
        self.assertEqual(
            first["config"]["switch"][0]["unique_id"],
            second["config"]["switch"][0]["unique_id"],
        )

    def test_missing_formal_feedback_reference_cannot_create_partial_control(self):
        p = project(
            ga("1/0/1", "A", 1, 1),
            functions={
                "F1": function(
                    "SwitchableLight", SwitchOnOff="1/0/1", InfoOnOff="no-such-address"
                )
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    def test_invalid_second_function_blocks_previously_valid_command(self):
        p = project(
            ga("1/0/1", "A", 1, 1),
            ga("1/0/2", "B", 9, 1),
            functions={
                "F1": function("SwitchableLight", SwitchOnOff="1/0/1"),
                "F2": function(
                    "SwitchableLight", SwitchOnOff="1/0/1", InfoOnOff="1/0/2"
                ),
            },
        )
        self.assertEqual(mapper.plan_project(p)["entityCount"], 0)

    @unittest.skipUnless(has_ha_distribution(), "HA runtime not installed")
    def test_generated_platforms_pass_the_installed_official_ha_knx_schema(self):
        scene = function("Scene", Scene="4/0/1")
        scene["scene_number"] = 13
        samples = [
            sample_conventions(),
            abbreviated_light(),
            project(
                ga("2/1/0", "Example-MatterShade-Blind-Close", 1, 8),
                ga("2/1/1", "Example-MatterShade-Blind-Step", 1, 7),
                ga("2/1/2", "Example-MatterShade-Blind-POS", 5, 1),
            ),
            project(
                ga("5/0/1", "AC 室內溫度", 9, 1),
                ga("5/0/2", "AC 目標溫度", 9, 1),
                ga("5/0/3", "AC 目標溫度狀態", 9, 1),
            ),
            project(ga("4/0/1", "Scene bus", 18, 1), functions={"S1": scene}),
            contract_v1_project()[0],
            independent_units(
                "13F-A12",
                [
                    ("Climate-SW", 1, 1),
                    ("Climate-MODE", 20, 105),
                    ("Climate-FAN", 5, 1),
                    *CLIMATE_TEMPERATURES,
                ],
                count=2,
            )[0],
            project(ga("6/0/1", "Relay switch", 1, 1)),
            project(ga("6/0/2", "Status", 1, 11)),
        ]
        plans = [mapper.plan_project(sample) for sample in samples]
        self.assertTrue(all(plan["entityCount"] for plan in plans))
        # Existing isolated component tests stub HA in sys.modules. Run the real
        # validator in a fresh interpreter so those stubs cannot mask a failure.
        script = """
import importlib.util, json, sys, types
from pathlib import Path
from homeassistant.components.knx import CONFIG_SCHEMA
from homeassistant.const import __version__
base = Path(sys.argv[1])
pkg = types.ModuleType("real_writer_validation")
pkg.__path__ = [str(base)]
sys.modules[pkg.__name__] = pkg
spec = importlib.util.spec_from_file_location(
    pkg.__name__ + ".knx_project_deployment", base / "knx_project_deployment.py"
)
writer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = writer
spec.loader.exec_module(writer)
mode = writer.core_compatibility(__version__)["identityMode"]
validated = set()
for plan in json.load(sys.stdin):
    from xknx.telegram.address import GroupAddress
    address_format = GroupAddress.address_format.name if mode == "legacy" else None
    plan = writer.adapt_plan_identity(plan, mode, address_format)
    result = CONFIG_SCHEMA({"knx": plan["config"]})["knx"]
    for platform, rows in plan["config"].items():
        assert {writer.row_identity(platform, row, address_format) for row in rows} == {
            writer.row_identity(platform, row, address_format)
            for row in result[platform]
        }
        validated.add(platform)
print(json.dumps(sorted(validated)))
"""
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(Path(__file__).parents[1] / "custom_components/rexlite"),
            ],
            input=json.dumps(plans),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            set(json.loads(process.stdout)),
            {
                "light",
                "cover",
                "sensor",
                "climate",
                "scene",
                "switch",
                "binary_sensor",
                "button",
            },
        )

    def test_entity_limit_rejects_without_partial_silent_truncation(self):
        saved = mapper.MAX_ENTITIES
        try:
            mapper.MAX_ENTITIES = 1
            with self.assertRaisesRegex(ValueError, "entity limit"):
                mapper.plan_project(sample_conventions())
        finally:
            mapper.MAX_ENTITIES = saved


if __name__ == "__main__":
    unittest.main()
