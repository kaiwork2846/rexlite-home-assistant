"""Deterministic ETS metadata to Home Assistant KNX YAML planning.

This module never sends telegrams or writes files. Addresses are joined only by
ETS Function roles or exact, shared conventional name prefixes. Device names,
address offsets and a DPT alone never establish a relationship between channels.
Scene numbers must be explicit structured metadata; DPT 17/18 does not contain
that information. Unsupported addresses remain visible in the plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

MAX_GROUP_ADDRESSES = 65535
MAX_ENTITIES = 2000
MAPPER_REVISION = 10

# role: (YAML field, allowed exact DPTs). Names here are semantic roles, not GA
# names. Standard ETS roles include AbsoluteSetvalueControl/ActualDimmingValue.
ROLE_FIELDS = {
    "switchonoff": ("address", {(1, 1)}),
    "infoonoff": ("state_address", {(1, 1), (1, 11)}),
    "statusonoff": ("state_address", {(1, 1), (1, 11)}),
    "absolutesetvaluecontrol": ("brightness_address", {(5, 1)}),
    "dimmingvalue": ("brightness_address", {(5, 1)}),
    "brightnessvalue": ("brightness_address", {(5, 1)}),
    "actualdimmingvalue": ("brightness_state_address", {(5, 1)}),
    "infodimmingvalue": ("brightness_state_address", {(5, 1)}),
    "moveupdown": ("move_long_address", {(1, 8)}),
    "stopstepupdown": ("move_short_address", {(1, 7)}),
    "stop": ("stop_address", {(1, 10)}),
    "position": ("position_address", {(5, 1)}),
    "positionvalue": ("position_address", {(5, 1)}),
    "currentabsolutepositionblindspercentage": ("position_state_address", {(5, 1)}),
    "positionstate": ("position_state_address", {(5, 1)}),
    "angle": ("angle_address", {(5, 1)}),
    "colortemperature": ("color_temperature_address", {(7, 600), (5, 1)}),
    "colortemperaturestate": ("color_temperature_state_address", {(7, 600), (5, 1)}),
    "currentabsolutepositionslatpercentage": ("angle_state_address", {(5, 1)}),
    "temproom": ("temperature_address", {(9, 1)}),
    "temperature": ("temperature_address", {(9, 1)}),
    "targettemperature": ("target_temperature_address", {(9, 1)}),
    "targettemperaturestate": ("target_temperature_state_address", {(9, 1)}),
    "controllermode": ("controller_mode_address", {(20, 105)}),
    "controllermodestate": ("controller_mode_state_address", {(20, 105)}),
    "operationmode": ("operation_mode_address", {(20, 102)}),
    "operationmodestate": ("operation_mode_state_address", {(20, 102)}),
    "hvacmode": ("operation_mode_address", {(20, 102)}),
    "fanspeed": ("fan_speed_address", {(5, 1)}),
    "fanspeedstate": ("fan_speed_state_address", {(5, 1)}),
    "scene": ("address", {(17, 1), (18, 1)}),
    "impulseopen": ("open_impulse_address", {(1, 1)}),
    # The close impulse may also be declared 1.008 (Down = 1): no 7TS logic is spent
    # on it, as the actuator's close channel listens to the wall buttons'
    # up/down address directly, and one object carries one datapoint type.
    "impulseclose": ("close_impulse_address", {(1, 1), (1, 8)}),
    "impulsestop": ("stop_impulse_address", {(1, 1)}),
}

# A dry-contact curtain on a relay actuator in "switch impulse" mode (REXLiTE
# KNX Contract, CurtainImpulse): open, close and stop are three separate relay
# channels that each only react to a 1. HA's KNX cover writes 0/1 on one
# up/down address and cannot drive that, so each becomes a KNX button sending 1.
# There is no position feedback to model, so no cover entity is invented.
IMPULSE_BUTTONS = {
    "open_impulse_address": "開",
    "close_impulse_address": "關",
    "stop_impulse_address": "停",
}

# The same dry-contact curtain may also carry the addresses its wall buttons
# send to (a GVS CHPB key in blind mode: long press up/down 1.008, short press
# stop/step 1.007; the 7TS logic turns them into the 1s above). They are real
# bus functions, but HA already drives the impulses directly, so they are
# reported as not exposed rather than blocking the curtain as mixed roles.
IMPULSE_WALL_BUTTON_FIELDS = {"move_long_address", "move_short_address"}

# Longest suffix wins. A separator and a non-empty exact prefix are required.
# This supports existing installer naming without combining vaguely similar
# labels or inferring a feedback address by incrementing a group address.
# The shade roles "close"/"step"/"pos" are single-address labels for the
# bi-directional up/down (DPT 1.008), step/stop (DPT 1.007) and absolute
# position (DPT 5.001) group addresses; the exact-DPT guard below still rejects
# a label that does not carry the datapoint type its role requires. "open" is
# deliberately absent: paired with "close" it only produces an ambiguous
# duplicate up/down role.
# "Command"/"Status" (and "Brightness Command"/"Brightness Status", plus the
# Chinese "指令"/"亮度指令") is the other common English convention alongside
# "開關"/"狀態"; a role datapoint-type mismatch still blocks the whole prefix.
NAME_ROLES = {
    "開脈衝": "impulseopen",
    "關脈衝": "impulseclose",
    "停脈衝": "impulsestop",
    "色溫狀態": "colortemperaturestate",
    "色溫": "colortemperature",
    "葉片角度狀態": "currentabsolutepositionslatpercentage",
    "葉片角度": "angle",
    "運轉模式狀態": "operationmodestate",
    "運轉模式": "operationmode",
    "亮度狀態": "actualdimmingvalue",
    "brightness status": "actualdimmingvalue",
    "亮度指令": "absolutesetvaluecontrol",
    "brightness command": "absolutesetvaluecontrol",
    "亮度": "absolutesetvaluecontrol",
    "brightness": "absolutesetvaluecontrol",
    "開關指令": "switchonoff",
    "on/off command": "switchonoff",
    "switch command": "switchonoff",
    "開關": "switchonoff",
    "on/off": "switchonoff",
    "switch": "switchonoff",
    "指令": "switchonoff",
    "command": "switchonoff",
    "狀態": "infoonoff",
    "status": "infoonoff",
    "上下": "moveupdown",
    "up/down": "moveupdown",
    "close": "moveupdown",
    "停止/微調": "stopstepupdown",
    "stop/step": "stopstepupdown",
    "step": "stopstepupdown",
    "停止": "stop",
    "stop": "stop",
    "位置狀態": "positionstate",
    "position status": "positionstate",
    "位置": "position",
    "position": "position",
    "pos": "position",
    "目標溫度狀態": "targettemperaturestate",
    "設定溫度狀態": "targettemperaturestate",
    "target temperature status": "targettemperaturestate",
    "目標溫度": "targettemperature",
    "設定溫度": "targettemperature",
    "target temperature": "targettemperature",
    "室內溫度": "temproom",
    "室溫": "temproom",
    "溫度": "temproom",
    "temperature": "temproom",
    "模式狀態": "controllermodestate",
    "模式": "controllermode",
    "風速狀態": "fanspeedstate",
    "風速": "fanspeed",
}

# Standard ETS DatapointRoles that are real bus functions but never a Home
# Assistant entity field (e.g. relative dimming for wall buttons). Unmapped
# members with these roles are reported distinctly from unresolved metadata.
NON_ENTITY_FUNCTION_ROLES = {
    "dimmingcontrol",
    "windalarm",
    "rainalarm",
    "valveposition",
    "valveswitch",
    "windowstatus",
}

# Measurement-only fallbacks never expose a write action. Other exact DPTs can
# still be represented when a communication object proves this is telemetry.
MEASUREMENT_DPTS = {
    (9, sub)
    for sub in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29)
}
TELEMETRY_DPTS = MEASUREMENT_DPTS | {
    (5, 1),
    (5, 3),
    (5, 4),
    (5, 5),
    (5, 6),
    (5, 10),
    (7, 1),
    (7, 2),
    (7, 3),
    (7, 4),
    (7, 5),
    (7, 6),
    (7, 600),
    (12, 1),
    (13, 1),
    (13, 10),
    (13, 13),
    (14, 56),
    (14, 76),
}
LIGHT_TYPES = {"switchablelight", "dimmablelight", "light", "ft1", "ft6"}
COVER_TYPES = {"sunprotection", "cover", "blinds", "ft7"}
CLIMATE_TYPES = {
    "heatingradiator",
    "heatingfloor",
    "climate",
    "hvac",
    "ft4",
    "ft5",
    "ft8",
    "ft9",
}

# Installer abbreviations require communication-object channel evidence below;
# they are deliberately not added to the name-only convention table.
ABBREVIATED_ROLES = {
    "SW": ("address", {(1, 1)}),
    "SW-FB": ("state_address", {(1, 1), (1, 11)}),
    "VAL": ("brightness_address", {(5, 1)}),
    "VAL-FB": ("brightness_state_address", {(5, 1)}),
    "CT": ("color_temperature_address", {(7, 600), (5, 1)}),
    "CT-FB": ("color_temperature_state_address", {(7, 600), (5, 1)}),
}

# The installer "<unit>-Climate-SW/-MODE/-FAN/-VAL(-FB)/-VAL-REAL-FB" convention
# observed across real air-conditioner exports. HA's KNX climate schema
# requires both temperature_address and target_temperature_state_address, so
# a climate entity is only emitted when the real room temperature
# (VAL-REAL-FB) and target-temperature feedback (VAL-FB) are proven on the same
# channel. "VAL" is only a setpoint when it carries DPT 9.001; exports that use
# a raw 5.010 "VAL" are left unmapped. Controller mode is DPT 20.105 (not the
# DPT 20.102 operation mode) and fan speed is DPT 5.001 only. "SW" itself is the
# anchor command and is intentionally absent here, mirroring ABBREVIATED_ROLES.
CLIMATE_ROLE_FIELDS = {
    "SW-FB": ("on_off_state_address", {(1, 1), (1, 11)}),
    "MODE": ("controller_mode_address", {(20, 105)}),
    "MODE-FB": ("controller_mode_state_address", {(20, 105)}),
    "FAN": ("fan_speed_address", {(5, 1)}),
    "FAN-FB": ("fan_speed_state_address", {(5, 1)}),
    "VAL": ("target_temperature_address", {(9, 1)}),
    "VAL-FB": ("target_temperature_state_address", {(9, 1)}),
    "VAL-REAL-FB": ("temperature_address", {(9, 1)}),
}

# Home Assistant's KNX climate YAML schema rejects an entity without these.
CLIMATE_REQUIRED_FIELDS = {"temperature_address", "target_temperature_state_address"}

# 1-bit encodings that a proven on/off feedback address may mix: Switch (0/1),
# State (0/1) and Bool (0/1) share the same wire format, so a status object
# driving the address alongside logic-block Bool listeners is not a real
# conflict once the same producing actuator channel has been proven.
FEEDBACK_BIT_DPTS = {(1, 1), (1, 2), (1, 11)}


def _semantic(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _address(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:/[0-9]+){0,2}", value):
        return None
    parts = [int(part) for part in value.split("/")]
    limits = {1: [65535], 2: [31, 2047], 3: [31, 7, 255]}[len(parts)]
    if any(part > limit for part, limit in zip(parts, limits, strict=True)):
        return None
    raw = parts[0] if len(parts) == 1 else parts[0] * 2048 + parts[1]
    if len(parts) == 3:
        raw = parts[0] * 2048 + parts[1] * 256 + parts[2]
    return f"{raw // 2048}/{(raw % 2048) // 256}/{raw % 256}"


def _dpt(value: Any) -> tuple[int, int | None] | None:
    if not isinstance(value, dict):
        return None
    main, sub = value.get("main"), value.get("sub")
    if type(main) is not int or main <= 0:
        return None
    if sub is not None and (type(sub) is not int or sub < 0):
        return None
    return main, sub


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _label(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split())[:120] or fallback


def _named_role(name: str) -> tuple[str, str] | None:
    for suffix in sorted(NAME_ROLES, key=len, reverse=True):
        match = re.fullmatch(r"(.+?)[\s_-]+" + re.escape(suffix), name, re.IGNORECASE)
        if match and (prefix := match.group(1).strip()):
            return prefix, NAME_ROLES[suffix]
    return None


class _Planner:
    def __init__(self, project: dict) -> None:
        self.project = project
        info = _dict(project.get("info"))
        self.project_id = _label(info.get("guid") or info.get("project_id"), "")
        if not self.project_id:
            # The writer owns one managed project per HA host. Without an ETS
            # project identifier, keep this host-local namespace stable when
            # addresses are added or renamed on the next import.
            self.project_id = "host-managed-project"
        self.groups: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.objects: dict[str, list[dict]] = defaultdict(list)
        self.dpts: dict[str, tuple[int, int]] = {}
        self.dpt_choices: dict[str, set[tuple[int, int | None] | None]] = {}
        self.issues: dict[str, str] = {}
        self.blocked: set[str] = set()
        self.used: set[str] = set()
        self.candidates: list[dict] = []
        self.non_entity_members: set[str] = set()
        # Function members already settled as not exposed (a dry-contact
        # curtain's wall-button addresses): later name- and DPT-based passes
        # must not re-read them as something else.
        self.reserved: set[str] = set()
        self.unresolved_members: set[str] = set()
        self._load()

    def _load(self) -> None:
        source = _dict(self.project.get("group_addresses"))
        if len(source) > MAX_GROUP_ADDRESSES:
            raise ValueError("ETS project exceeds the supported group-address limit")
        for key in sorted(source):
            ga = _dict(source[key])
            address = _address(ga.get("address") or str(key))
            if address is None or address == "0/0/0":
                self.issues[str(key)] = "invalid_group_address"
                continue
            if address in self.groups and self.groups[address] != ga:
                self.issues[address] = "duplicate_group_address_metadata"
                self.blocked.add(address)
                continue
            self.groups[address] = ga
            for alias in (str(key), address, ga.get("identifier")):
                if isinstance(alias, str) and alias:
                    self.aliases[alias] = address
        objects = _dict(self.project.get("communication_objects"))
        linked_ids: dict[str, set[str]] = defaultdict(set)
        for object_id, raw in objects.items():
            for link in _list(_dict(raw).get("group_address_links")):
                address = self.aliases.get(str(link), _address(link))
                if address in self.groups:
                    linked_ids[address].add(object_id)
        for address, ga in self.groups.items():
            object_ids = {
                item
                for item in _list(ga.get("communication_object_ids"))
                if isinstance(item, str)
            } | linked_ids[address]
            linked = [
                {**_dict(objects[item]), "_object_id": item}
                for item in sorted(object_ids)
                if item in objects
            ]
            self.objects[address] = linked
            candidates = []
            if ga.get("dpt") is not None:
                candidates.append(_dpt(ga["dpt"]))
            for obj in linked:
                candidates.extend(_dpt(dpt) for dpt in _list(obj.get("dpts")))
            exact = {dpt for dpt in candidates if dpt and dpt[1] is not None}
            mains = {dpt[0] for dpt in candidates if dpt}
            self.dpt_choices[address] = set(candidates)
            # An object's list declares supported alternatives, not simultaneous
            # wire encodings. An explicit GA subtype can select one only when
            # every declared object accepts that exact type (or its main type).
            declared = _dpt(ga.get("dpt"))
            if (
                declared
                and declared[1] is not None
                and all(
                    not (options := _list(obj.get("dpts")))
                    or (
                        all(_dpt(option) is not None for option in options)
                        and any(
                            _dpt(option) in {declared, (declared[0], None)}
                            for option in options
                        )
                    )
                    for obj in linked
                )
            ):
                exact, mains = {declared}, {declared[0]}
            if None in candidates or len(exact) != 1 or len(mains) != 1:
                self.issues[address] = (
                    "conflicting_datapoint_type"
                    if len(exact) > 1 or len(mains) > 1
                    else "missing_exact_datapoint_type"
                )
                self.blocked.add(address)
            else:
                self.dpts[address] = next(iter(exact))

    def has_flag(self, address: str, flag: str) -> bool:
        return any(
            _dict(obj.get("flags")).get("communication") is True
            and _dict(obj.get("flags")).get(flag) is True
            for obj in self.objects[address]
        )

    @staticmethod
    def _channel_key(obj: dict) -> tuple[str, ...] | None:
        """Identify a channel without confusing multi-instance panel modules."""
        device = obj.get("device_address")
        if not isinstance(device, str) or not device:
            return None
        object_id = str(obj.get("_object_id", ""))
        module = re.search(r"/(MD-[^/]+)_O-", object_id)
        channel = str(obj.get("channel") or "")
        if module:
            return device, "module", module.group(1), channel
        text = _label(obj.get("text"), "")
        if text:
            return device, "channel", channel, text
        return None

    def _actuator_keys(self, address: str, *, feedback: bool) -> set[tuple]:
        keys = set()
        for obj in self.objects[address]:
            flags = _dict(obj.get("flags"))
            if flags.get("communication") is not True:
                continue
            if feedback:
                # A receiver/display may also be writable on a feedback GA.
                # Use the producing actuator's flags, not the union of flags.
                proven = (
                    flags.get("write") is False
                    and flags.get("read") is True
                    and flags.get("transmit") is True
                )
            else:
                proven = flags.get("write") is True and flags.get("transmit") is False
            if proven and (key := self._channel_key(obj)):
                keys.add(key)
        return keys

    # Installer spellings for the same abbreviated actuator roles. The exact-DPT
    # guard in ABBREVIATED_ROLES still applies, so a "VALUE" that carries a
    # temperature datapoint (a climate setpoint) never becomes light brightness.
    _ROLE_ALIASES = {
        "BRIGHTNESS": "VAL",
        "VALUE": "VAL",
        "BRIGHTNESS-FB": "VAL-FB",
        "VALUE-FB": "VAL-FB",
        "COLOR": "CT",
        "COLOUR": "CT",
        "COLOR-FB": "CT-FB",
        "COLOUR-FB": "CT-FB",
    }

    def _bind_optional_role(
        self,
        address: str,
        role: str,
        expected: set[tuple[int, int | None]],
        anchor: set[tuple],
    ) -> bool:
        """Prove one optional-role address shares a command's actuator channel
        and carries the role's exact datapoint type. Shared by every dialect
        that pairs an anchor command with SW-FB/VAL/CT/MODE/FAN-style roles."""
        if anchor != self._actuator_keys(address, feedback=role.endswith("-FB")):
            self.issues.setdefault(address, "unproven_same_actuator_channel")
            return False
        # Switch/State/Bool share 0=off/1=on. Permit only a mix of those 1-bit
        # encodings after proving the same producing actuator, and only when
        # at least one Switch or State object is present.
        choices = {choice for choice in self.dpt_choices[address] if choice}
        if (
            role == "SW-FB"
            and self.issues.get(address) == "conflicting_datapoint_type"
            and choices <= FEEDBACK_BIT_DPTS
            and choices & {(1, 1), (1, 11)}
        ):
            self.dpts[address] = (1, 11)
            self.blocked.discard(address)
            self.issues.pop(address, None)
        if address in self.blocked or self.dpts.get(address) not in expected:
            self.issues.setdefault(address, "role_datapoint_type_mismatch")
            return False
        return True

    def _bind_multi_command_roles(
        self,
        commands: list[str],
        roles: dict[str, list[str]],
        role_fields: dict[str, tuple[str, set]],
    ) -> dict[str, dict[str, str]]:
        """Several distinct outputs share one name prefix (e.g. three DALI
        circuits or air-conditioners in the same room). Bind each command only
        to the optional-role addresses proven to share its own actuator
        channel, instead of refusing to disambiguate the whole shared-name
        group. A command or optional address without a single proven channel,
        or two commands/addresses that resolve to the same channel, is never
        guessed -- only distinct, individually-proven channels are split out.
        """
        anchors: dict[str, tuple] = {}
        for command in commands:
            if command in self.blocked or self.dpts.get(command) != (1, 1):
                continue
            anchor = self._actuator_keys(command, feedback=False)
            if len(anchor) == 1:
                anchors[command] = next(iter(anchor))
        by_anchor: dict[tuple, list[str]] = defaultdict(list)
        for command, anchor in anchors.items():
            by_anchor[anchor].append(command)
        for dupes in by_anchor.values():
            if len(dupes) > 1:
                # Two commands that resolve to the same actuator channel are
                # genuinely indistinguishable; block just those.
                self.blocked.update(dupes)
                for command in dupes:
                    self.issues[command] = "ambiguous_duplicate_function_role"
                    anchors.pop(command, None)
        fields_by_command: dict[str, dict[str, str]] = {c: {} for c in anchors}
        for role, addresses in sorted(roles.items()):
            if role == "SW" or role not in role_fields:
                continue
            field, expected = role_fields[role]
            claims: dict[str, list[str]] = defaultdict(list)
            for address in addresses:
                key = self._actuator_keys(address, feedback=role.endswith("-FB"))
                own = next(iter(key)) if len(key) == 1 else None
                matched = [
                    command
                    for command, a in anchors.items()
                    if own is not None and a == own
                ]
                if len(matched) != 1:
                    self.issues.setdefault(address, "unproven_same_actuator_channel")
                    continue
                claims[matched[0]].append(address)
            for command, claimants in claims.items():
                if len(claimants) != 1:
                    # Two addresses under the same role proved to share the
                    # same command's channel: never guess which one is real.
                    # Leave them unblocked (unlike a genuine command-channel
                    # collision above) so a still-useful standalone reading --
                    # e.g. an unrelated room-temperature sensor that merely
                    # can't be tied to one specific unit -- keeps its normal
                    # fallback classification instead of being suppressed.
                    for address in claimants:
                        self.issues.setdefault(
                            address, "ambiguous_duplicate_function_role"
                        )
                    continue
                address = claimants[0]
                if self._bind_optional_role(
                    address, role, expected, {anchors[command]}
                ):
                    fields_by_command[command][field] = address
        return fields_by_command

    def _finish_abbreviated(self, name: str, fields: dict[str, str]) -> None:
        # Brightness / CT features are independent: a malformed optional
        # feature must not erase a proven on/off channel. Orphan feedback
        # remains eligible for an observation-only fallback.
        for feature in ("brightness", "color_temperature"):
            if f"{feature}_address" not in fields:
                fields.pop(f"{feature}_state_address", None)
        # A proven brightness or colour-temperature channel is a dimmable
        # light even when the terse installer name carries no light word.
        named_light = bool(re.search(r"燈|照明|\blight\b", name, re.IGNORECASE))
        is_light = named_light or bool(
            {"brightness_address", "color_temperature_address"} & fields.keys()
        )
        if not is_light:
            # A plain on/off actuator keeps only the switch-compatible roles.
            fields = {
                role: address
                for role, address in fields.items()
                if role in {"address", "state_address"}
            }
        if "color_temperature_address" in fields:
            command_dpt = self.dpts[fields["color_temperature_address"]]
            feedback = fields.get("color_temperature_state_address")
            if feedback is not None and self.dpts[feedback] != command_dpt:
                # Feedback must use the command's encoding, not the other one.
                fields.pop("color_temperature_state_address")
            fields["color_temperature_mode"] = (
                "relative" if command_dpt == (5, 1) else "absolute"
            )
        if len(fields) == 1:
            # Retain the existing single-address fallback when pairing proves
            # no additional feature. Never fabricate state feedback.
            return
        self._add(
            "light" if is_light else "switch",
            name,
            fields,
            "exact-name-object-channel",
        )

    def abbreviated_groups(self) -> None:
        """Join proven actuator channels with exact SW/VAL/CT installer roles."""
        grouped: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        names = {}
        for address, ga in sorted(self.groups.items()):
            if address in self.used or address in self.reserved:
                continue
            match = re.fullmatch(
                r"(.+?)[\s_-]+"
                r"(SW-FB|VAL-FB|CT-FB|BRIGHTNESS-FB|VALUE-FB|COLOU?R-FB"
                r"|SW|VAL|CT|BRIGHTNESS|VALUE|COLOU?R)",
                _label(ga.get("name"), ""),
                re.IGNORECASE,
            )
            if match:
                name, role = match.groups()
                name = name.rstrip(" ,;:-_/") or name
                role = role.upper()
                role = self._ROLE_ALIASES.get(role, role)
                key = name.casefold()
                names[key] = name
                grouped[key][role].append(address)
        for key, roles in sorted(grouped.items()):
            commands = roles.get("SW", [])
            if not commands:
                continue
            if len(commands) > 1:
                # Several distinct outputs (e.g. multiple DALI circuits) share
                # one room/name prefix: split them by proven actuator channel
                # instead of refusing to disambiguate the whole group.
                for command, fields in self._bind_multi_command_roles(
                    commands, roles, ABBREVIATED_ROLES
                ).items():
                    self._finish_abbreviated(names[key], {"address": command, **fields})
                continue
            command = commands[0]
            if command in self.blocked or self.dpts.get(command) != (1, 1):
                continue
            anchor = self._actuator_keys(command, feedback=False)
            if len(anchor) != 1:
                # A fan-out command may control several independent outputs;
                # one output's feedback cannot prove the aggregate state.
                continue
            fields = {"address": command}
            for role, addresses in sorted(roles.items()):
                if role == "SW":
                    continue
                field, expected = ABBREVIATED_ROLES[role]
                if len(addresses) != 1:
                    # Never choose between duplicate optional roles for a
                    # single command; only distinct, separately-proven
                    # commands (handled above) may split addresses this way.
                    for address in addresses:
                        self.blocked.add(address)
                        self.issues[address] = "ambiguous_duplicate_function_role"
                    continue
                if self._bind_optional_role(addresses[0], role, expected, anchor):
                    fields[field] = addresses[0]
            self._finish_abbreviated(names[key], fields)

    def climate_groups(self) -> None:
        """Join the installer's "<unit>-Climate-SW/-MODE/-FAN(-FB)" convention
        into a climate entity. HA's climate schema requires the room temperature
        and target-temperature feedback, so those must be proven as well.
        Every role still needs the same proven actuator-channel evidence as
        the SW/VAL/CT light dialect, and the exact-DPT guard still applies.
        """
        grouped: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        names = {}
        for address, ga in sorted(self.groups.items()):
            if address in self.used or address in self.reserved:
                continue
            match = re.fullmatch(
                r"(.+?)[\s_-]+climate[\s_-]+"
                r"(sw-fb|sw|mode-fb|mode|fan-fb|fan|val-real-fb|val-fb|val)",
                _label(ga.get("name"), ""),
                re.IGNORECASE,
            )
            if match:
                name, role = match.groups()
                name = name.rstrip(" ,;:-_/") or name
                key = name.casefold()
                names[key] = name
                grouped[key][role.upper()].append(address)
        for key, roles in sorted(grouped.items()):
            commands = roles.get("SW", [])
            if not commands:
                continue
            if len(commands) > 1:
                for command, fields in self._bind_multi_command_roles(
                    commands, roles, CLIMATE_ROLE_FIELDS
                ).items():
                    fields["on_off_address"] = command
                    self._finish_climate(names[key], fields)
                continue
            command = commands[0]
            if command in self.blocked or self.dpts.get(command) != (1, 1):
                continue
            anchor = self._actuator_keys(command, feedback=False)
            if len(anchor) != 1:
                continue
            fields = {"on_off_address": command}
            for role, addresses in sorted(roles.items()):
                if role == "SW":
                    continue
                field, expected = CLIMATE_ROLE_FIELDS[role]
                if len(addresses) != 1:
                    for address in addresses:
                        self.blocked.add(address)
                        self.issues[address] = "ambiguous_duplicate_function_role"
                    continue
                if self._bind_optional_role(addresses[0], role, expected, anchor):
                    fields[field] = addresses[0]
            self._finish_climate(names[key], fields)

    def _finish_climate(self, name: str, fields: dict[str, str]) -> None:
        # HA rejects the whole KNX configuration when a climate row lacks its
        # required temperature addresses, so never emit a partial one. The
        # command addresses stay available to the plain fallbacks instead.
        if not fields.keys() >= CLIMATE_REQUIRED_FIELDS:
            return
        self._add("climate", name, fields, "exact-name-object-channel")

    def scene_metadata(self) -> None:
        """Use explicit sender parameters recovered from the same ETS archive."""
        for address, ga in sorted(self.groups.items()):
            if address in self.used or address in self.reserved:
                continue
            number = ga.get("scene_number")
            sender_ids = ga.get("scene_sender_ids")
            if (
                ga.get("scene_number_source") != "ets-sender-parameter"
                or type(number) is not int
                or not 1 <= number <= 64
                or not isinstance(sender_ids, list)
                or not sender_ids
                or any(not isinstance(item, str) or not item for item in sender_ids)
            ):
                continue
            linked = {obj["_object_id"]: obj for obj in self.objects[address]}
            if not all(
                sender in linked
                and _dict(linked[sender].get("flags")).get("communication") is True
                and _dict(linked[sender].get("flags")).get("transmit") is True
                for sender in sender_ids
            ) or not self.has_flag(address, "write"):
                continue
            choices = self.dpt_choices[address]
            if not choices or not choices <= {(17, 1), (18, 1)}:
                continue
            if address in self.blocked:
                if self.issues.get(address) != "conflicting_datapoint_type":
                    continue
                # Both scene encodings share the lower six recall bits. This
                # path only recalls an explicit 1..64 scene and never sets the
                # learn/store bit; it does not generalize DPT compatibility.
                self.blocked.discard(address)
                self.issues.pop(address, None)
            self._add(
                "scene",
                _label(ga.get("name"), f"KNX Scene {number}"),
                {"address": address, "scene_number": number},
                "ets-sender-scene-parameter",
            )

    def _add(self, platform: str, name: str, config: dict, source: str) -> None:
        addresses = sorted({v for k, v in config.items() if k.endswith("address")})
        primary = (
            config.get("address")
            or config.get("move_long_address")
            or config.get("position_address")
            or config.get("target_temperature_address")
            or config.get("on_off_address")
            or config.get("state_address")
            or config.get("temperature_address")
        )
        identity = f"{self.project_id}|{platform}|{primary}"
        if platform == "scene":
            identity += f"|{config['scene_number']}"
        unique_id = "rexlite_ets_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.candidates.append(
            {
                "platform": platform,
                "name": _label(name, f"KNX {primary}"),
                "uniqueId": unique_id,
                "addresses": addresses,
                "source": source,
                "primaryAddress": primary,
                "config": config,
            }
        )
        self.used.update(addresses)

    def _group(
        self,
        name: str,
        kind: str,
        refs: list[tuple[str, str]],
        source: str,
        metadata: dict | None = None,
    ) -> None:
        fields: dict[str, str] = {}
        mentioned = {address for _, address in refs if address in self.groups}
        error = None
        for role, address in refs:
            if role not in ROLE_FIELDS:
                continue
            field, expected = ROLE_FIELDS[role]
            if address not in self.groups or address in self.blocked:
                error = "invalid_or_missing_function_address"
                break
            if self.dpts[address] not in expected:
                error = "role_datapoint_type_mismatch"
                break
            if field in fields and fields[field] != address:
                error = "ambiguous_duplicate_function_role"
                break
            # KNX object's Write flag means the actuator accepts group writes.
            # Explicit metadata may work without objects, but must never
            # contradict present, complete object flags.
            if (
                field not in {"state_address", "temperature_address"}
                and not field.endswith("state_address")
                and self.objects[address]
                and not self.has_flag(address, "write")
            ):
                error = "command_address_not_writable"
                break
            fields[field] = address
        if error:
            self.blocked.update(mentioned)
            for address in mentioned:
                self.issues.setdefault(address, error)
            return
        if impulse := fields.keys() & IMPULSE_BUTTONS.keys():
            for field in fields.keys() & IMPULSE_WALL_BUTTON_FIELDS:
                address = fields.pop(field)
                self.non_entity_members.add(address)
                self.reserved.add(address)
            if fields.keys() != IMPULSE_BUTTONS.keys():
                # A partial set (or one mixed with other roles) cannot move a
                # curtain both ways and stop it; say so rather than guess.
                self.blocked.update(mentioned)
                for address in mentioned:
                    self.issues.setdefault(address, "incompatible_function_roles")
                return
            for field in sorted(impulse, key=list(IMPULSE_BUTTONS).index):
                self._add(
                    "button",
                    f"{name} {IMPULSE_BUTTONS[field]}",
                    {"address": fields[field]},
                    source,
                )
            return
        platform = None
        kind = _semantic(kind)
        if kind in CLIMATE_TYPES or "target_temperature_address" in fields:
            required = {
                "temperature_address",
                "target_temperature_address",
                "target_temperature_state_address",
            }
            if required <= fields.keys():
                platform = "climate"
                if "address" in fields:
                    fields["on_off_address"] = fields.pop("address")
                if "state_address" in fields:
                    fields["on_off_state_address"] = fields.pop("state_address")
            elif any(k in fields for k in required - {"temperature_address"}):
                for address in mentioned:
                    self.issues.setdefault(address, "incomplete_climate_addresses")
                self.blocked.update(mentioned)
                return
        elif kind in COVER_TYPES or "move_long_address" in fields:
            if "move_long_address" in fields or "position_address" in fields:
                platform = "cover"
        elif kind in {"scene", "scenes"}:
            scene_number = _dict(metadata).get("scene_number")
            if (
                "address" in fields
                and type(scene_number) is int
                and 1 <= scene_number <= 64
            ):
                platform = "scene"
                fields["scene_number"] = scene_number
            else:
                for address in mentioned:
                    self.issues.setdefault(address, "missing_explicit_scene_number")
                self.blocked.update(mentioned)
                return
        elif "address" in fields:
            platform = "light" if kind in LIGHT_TYPES else "switch"
            dimming = {"brightness_address", "color_temperature_address"}
            if dimming & fields.keys() and platform != "light":
                platform = None
            if "color_temperature_state_address" in fields:
                command = fields.get("color_temperature_address")
                state = fields["color_temperature_state_address"]
                if command is None or self.dpts[command] != self.dpts[state]:
                    for address in mentioned:
                        self.issues.setdefault(address, "role_datapoint_type_mismatch")
                    self.blocked.update(mentioned)
                    return
            if "color_temperature_address" in fields:
                relative = self.dpts[fields["color_temperature_address"]] == (5, 1)
                fields["color_temperature_mode"] = (
                    "relative" if relative else "absolute"
                )
        if platform is None:
            return
        allowed = {
            "light": {
                "address",
                "state_address",
                "brightness_address",
                "brightness_state_address",
                "color_temperature_address",
                "color_temperature_state_address",
                "color_temperature_mode",
            },
            "switch": {"address", "state_address"},
            "cover": {
                "move_long_address",
                "move_short_address",
                "stop_address",
                "position_address",
                "position_state_address",
                "angle_address",
                "angle_state_address",
            },
            "scene": {"address", "scene_number"},
            "climate": {
                "temperature_address",
                "target_temperature_address",
                "target_temperature_state_address",
                "on_off_address",
                "on_off_state_address",
                "controller_mode_address",
                "controller_mode_state_address",
                "operation_mode_address",
                "operation_mode_state_address",
                "fan_speed_address",
                "fan_speed_state_address",
            },
        }.get(platform)
        if allowed is not None and not fields.keys() <= allowed:
            self.blocked.update(mentioned)
            for address in mentioned:
                self.issues.setdefault(address, "incompatible_function_roles")
            return
        self._add(platform, name, fields, source)

    def _function_member_role(self, function_name: str, address: str) -> str | None:
        """Resolve a Function member without a standard ETS DatapointRole.

        ETS has no standard role for e.g. colour temperature, absolute blind
        position or air-conditioner mode/fan. Such a member may carry an exact
        name convention, but only when its prefix is exactly the Function's own
        name: the Function membership proves the grouping, the suffix names the
        role, and the exact-DPT guard in `_group()` still applies.
        """
        parsed = _named_role(_label(self.groups.get(address, {}).get("name"), ""))
        if parsed and parsed[0].casefold() == function_name.casefold():
            return parsed[1]
        return None

    def functions(self) -> None:
        for function_id, raw in sorted(_dict(self.project.get("functions")).items()):
            function = _dict(raw)
            function_name = _label(function.get("name"), function_id)
            refs = []
            for ref_key, raw_ref in _dict(function.get("group_addresses")).items():
                ref = _dict(raw_ref)
                raw_address = ref.get("address")
                address = self.aliases.get(str(raw_address), _address(raw_address))
                role = _semantic(ref.get("role") or ref_key)
                if address is None:
                    address = str(raw_address or ref_key)
                    self.issues[address] = "missing_function_address"
                elif role in NON_ENTITY_FUNCTION_ROLES:
                    self.non_entity_members.add(address)
                    continue
                elif role not in ROLE_FIELDS and role != "temproomsetpoint":
                    named = self._function_member_role(function_name, address)
                    if named is None:
                        self.unresolved_members.add(address)
                        continue
                    role = named
                # TempRoomSetpoint is not inherently a write or state role. Use
                # both only when the object flags explicitly prove both roles.
                if role == "temproomsetpoint":
                    if self.has_flag(address, "write") and self.has_flag(
                        address, "read"
                    ):
                        refs.extend(
                            [
                                ("targettemperature", address),
                                ("targettemperaturestate", address),
                            ]
                        )
                    continue
                refs.append((role, address))
            self._group(
                function_name,
                function.get("function_type", ""),
                refs,
                "ets-function-role",
                function,
            )

    def named_groups(self) -> None:
        grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
        names = {}
        for address, ga in sorted(self.groups.items()):
            if address in self.used or address in self.reserved:
                continue
            parsed = _named_role(_label(ga.get("name"), ""))
            if parsed:
                name, role = parsed
                key = name.casefold()
                names[key] = name
                grouped[key].append((role, address))
        for key, refs in sorted(grouped.items()):
            name = names[key]
            kind = (
                "light"
                if re.search(r"燈|照明|\blight\b", name, re.IGNORECASE)
                else "switch"
            )
            self._group(name, kind, refs, "exact-name-role")

    def fallback(self) -> None:
        for address, ga in sorted(self.groups.items()):
            if address in self.used | self.reserved | self.blocked:
                continue
            dpt = self.dpts[address]
            name = _label(ga.get("name"), f"KNX {address}")
            named_feedback = bool(re.search(r"[\s_-]FB$", name, re.IGNORECASE))
            telemetry = self.has_flag(address, "transmit") and (
                not self.has_flag(address, "write") or named_feedback
            )
            # An "…-FB" group address with an exact 1-bit status datapoint and no
            # communication object at all is a read-only status: nothing declares
            # it a command and there are no flags to contradict the name.
            orphan_feedback = (
                named_feedback
                and not self.objects[address]
                and dpt in {(1, 1), (1, 11)}
            )
            if dpt in {(17, 1), (18, 1)}:
                self.issues[address] = "missing_explicit_scene_number"
            elif (
                dpt == (1, 11)
                or orphan_feedback
                or (
                    dpt in {(1, 1), (1, 2), (1, 5), (1, 9), (1, 18), (1, 19), (1, 22)}
                    and telemetry
                )
            ):
                config = {"state_address": address}
                if not self.has_flag(address, "read"):
                    config["sync_state"] = False
                self._add("binary_sensor", name, config, "ets-dpt-telemetry")
            elif (
                dpt == (1, 1) and self.has_flag(address, "write") and not named_feedback
            ):
                self._add("switch", name, {"address": address}, "ets-dpt-object-flags")
            elif dpt in MEASUREMENT_DPTS or (dpt in TELEMETRY_DPTS and telemetry):
                config = {"state_address": address, "type": f"{dpt[0]}.{dpt[1]:03d}"}
                if self.objects[address] and not self.has_flag(address, "read"):
                    config["sync_state"] = False
                self._add("sensor", name, config, "ets-dpt-telemetry")
            else:
                self.issues[address] = "insufficient_supported_entity_metadata"

    def annotate_function_members(self) -> None:
        """Explain unmapped Function members instead of a generic reason."""
        generic = {None, "insufficient_supported_entity_metadata"}
        for members, reason in (
            (self.non_entity_members, "function_role_not_exposed"),
            (self.unresolved_members, "unresolved_function_role"),
        ):
            for address in members:
                if address not in self.used and self.issues.get(address) in generic:
                    self.issues[address] = reason

    def result(self) -> dict:
        # Any disagreement about the same command address is a conflict, not
        # an opportunity to pick whichever record happened to be first.
        claims: dict[str, list[dict]] = defaultdict(list)
        for candidate in self.candidates:
            if self.blocked.intersection(candidate["addresses"]):
                continue
            config = candidate["config"]
            key = candidate["primaryAddress"]
            if candidate["platform"] == "scene":
                key += f"|scene|{config['scene_number']}"
            claims[key].append(candidate)
        accepted = []
        for candidates in claims.values():
            fingerprints = {
                json.dumps((c["platform"], c["config"]), sort_keys=True)
                for c in candidates
            }
            if len(fingerprints) != 1:
                for candidate in candidates:
                    for address in candidate["addresses"]:
                        self.issues[address] = "conflicting_entity_address_claim"
                continue
            accepted.append(candidates[0])
        if len(accepted) > MAX_ENTITIES:
            raise ValueError("ETS project exceeds the supported entity limit")
        config: dict[str, list[dict]] = defaultdict(list)
        entities = []
        used_names: set[tuple[str, str]] = set()
        mapped = set()
        for candidate in sorted(
            accepted, key=lambda c: (c["platform"], c["primaryAddress"], c["uniqueId"])
        ):
            platform, name = candidate["platform"], candidate["name"]
            base = name
            suffix = candidate["primaryAddress"]
            if platform == "scene":
                suffix += f" · {candidate['config']['scene_number']}"
            serial = 1
            while (platform, name.casefold()) in used_names:
                serial_text = f" · {serial}" if serial > 1 else ""
                name = f"{base[:80]} · {suffix}{serial_text}"
                serial += 1
            used_names.add((platform, name.casefold()))
            config[platform].append(
                {
                    "name": name,
                    "unique_id": candidate["uniqueId"],
                    **candidate["config"],
                }
            )
            entities.append(
                {k: v for k, v in candidate.items() if k != "config"} | {"name": name}
            )
            mapped.update(candidate["addresses"])
        skipped = [
            {
                "address": address,
                "reason": self.issues.get(
                    address, "insufficient_supported_entity_metadata"
                ),
            }
            for address in sorted((self.groups.keys() | self.issues.keys()) - mapped)
        ]
        return {
            "projectId": self.project_id,
            "config": dict(config),
            "entityCount": len(entities),
            "entities": entities,
            "skipped": skipped,
            "groupAddressCount": len(self.groups),
            "mappedAddressCount": len(mapped),
        }


def plan_project(project: dict) -> dict:
    """Return a stable, side-effect-free KNX YAML plan and unresolved addresses."""
    if not isinstance(project, dict):
        raise ValueError("Parsed ETS project must be an object")
    planner = _Planner(project)
    planner.functions()
    planner.climate_groups()
    planner.abbreviated_groups()
    planner.scene_metadata()
    planner.named_groups()
    planner.fallback()
    planner.annotate_function_members()
    return planner.result()
