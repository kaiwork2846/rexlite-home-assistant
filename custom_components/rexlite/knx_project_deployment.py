"""Transactional, host-local ETS to KNX YAML deployment.

Only this integration's package is replaced. Existing YAML, includes, entity IDs and
KNX connection settings remain owned by their original configuration. No telegrams
that write actuator values are sent by this deployment operation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .knx_project_mapper import MAPPER_REVISION, _address, plan_project

DATA_KEY = "rexlite_knx_project_deployer"
PACKAGE_KEY = "rexlite_knx_auto"
MANAGED_DIR = ".rexlite_knx"
GENERATED = f"{MANAGED_DIR}/entities.yaml"
MANIFEST = f"{MANAGED_DIR}/deployment.json"
JOURNAL = f"{MANAGED_DIR}/transaction.json"
LAST_ATTEMPT = f"{MANAGED_DIR}/last_attempt.json"
IMPORT_ASSOCIATION = f"{MANAGED_DIR}/import.json"
MAX_PROJECT_BYTES = 100 * 1024 * 1024
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
MAX_FILE_BYTES = 8 * 1024 * 1024
MINIMUM_KNX_CORE_VERSION = "2026.1.0"


class DeploymentError(Exception):
    """An actionable deployment error safe to return over the API."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_digest(project: dict) -> str:
    return digest(
        json.dumps(
            project, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )


def _mapping(node: Any) -> dict[str, Any]:
    if not isinstance(node, yaml.MappingNode) or (node.flow_style and node.value):
        raise DeploymentError("configuration_structure_unsupported")
    result = {}
    for key, value in node.value:
        if (
            not isinstance(key, yaml.ScalarNode)
            or key.value in result
            or key.value == "<<"
        ):
            raise DeploymentError("configuration_duplicate_or_merged_keys")
        result[key.value] = value
    return result


def _insert_child(text: str, node: Any, key: str, value: str, indent: int) -> str:
    """Insert one mapping item without serializing any existing YAML."""
    if isinstance(node, yaml.MappingNode) and node.value:
        children = _mapping(node)
        if key in children:
            raise DeploymentError("managed_package_name_already_in_use")
        if node.value:
            position = node.value[0][0].start_mark.index
            position = text.rfind("\n", 0, position) + 1
            indent = node.value[0][0].start_mark.column
            return (
                text[:position] + " " * indent + f"{key}: {value}\n" + text[position:]
            )
    elif (
        isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:null"
    ) or (isinstance(node, yaml.MappingNode) and not node.value):
        # Empty/null homeassistant or packages mapping; keep its inline comment.
        start, end = node.start_mark.index, node.end_mark.index
        text = text[:start] + text[end:]
        position = text.find("\n", start)
        if position == -1:
            text += "\n"
            position = len(text) - 1
        position += 1
        return text[:position] + " " * indent + f"{key}: {value}\n" + text[position:]
    raise DeploymentError("configuration_structure_unsupported")


class SafeFiles:
    """Fixed-root files with symlink/hardlink rejection and atomic replacement."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=True)

    def path(self, relative: str) -> Path:
        part = Path(relative)
        if part.is_absolute() or ".." in part.parts or not part.parts:
            raise DeploymentError("configuration_path_unsafe")
        path = self.root
        for component in part.parts:
            path /= component
            if path.is_symlink():
                raise DeploymentError("configuration_symlink_not_supported")
            if path.exists() and not path.is_dir():
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise DeploymentError("configuration_file_unsafe")
        return path

    def read(self, relative: str) -> bytes | None:
        path = self.path(relative)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DeploymentError("configuration_file_unsafe")
            data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise DeploymentError("configuration_file_too_large")
            return data

    def write(self, relative: str, data: bytes | None) -> None:
        if data is not None and len(data) > MAX_FILE_BYTES:
            raise DeploymentError("configuration_file_too_large")
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path(relative)
        if data is None:
            path.unlink(missing_ok=True)
            return
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        fd, temporary = tempfile.mkstemp(prefix=".rexlite-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self.path(relative)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read_json(self, relative: str) -> dict | None:
        data = self.read(relative)
        if data is None:
            return None
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as err:
            raise DeploymentError("managed_metadata_invalid") from err
        if not isinstance(result, dict):
            raise DeploymentError("managed_metadata_invalid")
        return result

    def write_json(self, relative: str, data: dict) -> None:
        if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_FILE_BYTES // 2:
            raise DeploymentError("managed_metadata_too_large")
        self.write(
            relative,
            json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2).encode()
            + b"\n",
        )

    def activation_changes(self) -> dict[str, bytes]:
        """Link the fixed managed package to HA's actual package configuration."""
        contents = self.read("configuration.yaml")
        if contents is None:
            raise DeploymentError("configuration_yaml_missing")
        text = contents.decode("utf-8")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        top = _mapping(root) if root else {}
        include = f"!include {GENERATED}"
        if "homeassistant" not in top:
            text += ("" if text.endswith("\n") else "\n") + (
                f"\nhomeassistant:\n  packages:\n    {PACKAGE_KEY}: {include}\n"
            )
            return {"configuration.yaml": text.encode()}
        home = top["homeassistant"]
        if isinstance(home, yaml.ScalarNode) and home.tag == "tag:yaml.org,2002:null":
            return {
                "configuration.yaml": _insert_child(
                    text, home, "packages", f"\n    {PACKAGE_KEY}: {include}", 2
                ).encode()
            }
        home_children = _mapping(home)
        home_indent = home.value[0][0].start_mark.column if home.value else 2
        if "packages" not in home_children:
            return {
                "configuration.yaml": _insert_child(
                    text,
                    home,
                    "packages",
                    f"\n{' ' * (home_indent + 2)}{PACKAGE_KEY}: {include}",
                    home_indent,
                ).encode()
            }
        packages = home_children["packages"]
        if isinstance(packages, yaml.ScalarNode) and packages.tag in (
            "!include_dir_named",
            "!include_dir_merge_named",
        ):
            directory = packages.value
            self.path(directory)
            relative = str(Path(directory) / f"{PACKAGE_KEY}.yaml")
            include_path = os.path.relpath(self.path(GENERATED), self.path(directory))
            directive = f"!include {json.dumps(include_path)}\n"
            content = (
                f"{PACKAGE_KEY}: {directive}"
                if packages.tag == "!include_dir_merge_named"
                else directive
            )
            data = content.encode()
            existing = self.read(relative)
            if existing is not None and existing != data:
                raise DeploymentError("managed_package_name_already_in_use")
            return {relative: data}
        if isinstance(packages, yaml.MappingNode):
            items = _mapping(packages)
            if PACKAGE_KEY in items:
                value = items[PACKAGE_KEY]
                if value.tag != "!include" or value.value != GENERATED:
                    raise DeploymentError("managed_package_name_already_in_use")
                return {}
        return {
            "configuration.yaml": _insert_child(
                text, packages, PACKAGE_KEY, include, 4
            ).encode()
        }


def _addresses(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, list):
        for item in value:
            result |= _addresses(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "address" or key.endswith("_address"):
                for address in item if isinstance(item, list) else [item]:
                    if canonical := _address(str(address)):
                        result.add(canonical)
            if isinstance(item, (dict, list)):
                result |= _addresses(item)
    return result


def core_compatibility(version: str) -> dict:
    """Select the official YAML identity behavior supported by the installed Core."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.dev\d+|[ab]\d+)?", version)
    if not match or tuple(map(int, match.groups())) < (2026, 1, 0):
        raise DeploymentError("home_assistant_version_unsupported")
    # Pre-release builds may predate the KNX identity/schema change in a release.
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise DeploymentError("home_assistant_version_unsupported")
    return {
        "homeAssistantVersion": version,
        "requiredHomeAssistantVersion": MINIMUM_KNX_CORE_VERSION,
        "identityMode": (
            "custom"
            if tuple(map(int, match.groups())) >= (2026, 9, 0)
            else "native"
            if tuple(map(int, match.groups())) >= (2026, 8, 0)
            else "legacy"
        ),
    }


def format_native_identity(identity: str, address_format: str | None) -> str:
    """Older KNX platforms render native IDs in the active project's GA format."""
    if address_format in (None, "LONG"):
        return identity
    if address_format not in ("SHORT", "FREE"):
        raise DeploymentError("knx_address_format_unsupported")
    parts = []
    for part in identity.split("_"):
        if re.fullmatch(r"\d+/\d+/\d+", part) and (canonical := _address(part)):
            main, middle, sub = map(int, canonical.split("/"))
            parts.append(
                f"{main}/{middle * 256 + sub}"
                if address_format == "SHORT"
                else str(main * 2048 + middle * 256 + sub)
            )
        else:
            parts.append(part)
    return "_".join(parts)


def manifest_identity(entity: dict, manifest: dict, address_format: str | None) -> str:
    if manifest.get("identityMode", "custom") == "custom":
        return entity["uniqueId"]
    canonical = entity.get("canonicalUniqueId", entity["uniqueId"])
    return format_native_identity(canonical, address_format)


def row_identity(
    platform: str, row: dict, address_format: str | None = None
) -> str | None:
    """Mirror the official KNX 2026.8 stable YAML IDs for supported ETS platforms.

    Group-address lists use their primary address. Unsupported manual platforms
    remain manual; a generated entity without a known identity is rejected.
    """
    if uid := row.get("unique_id"):
        return str(uid)

    def primary(key: str) -> str | None:
        value = row.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        canonical = _address(str(value)) if value is not None else None
        return format_native_identity(canonical, address_format) if canonical else None

    if platform in ("light", "switch", "button"):
        return primary("address")
    if platform in ("sensor", "binary_sensor"):
        return primary("state_address")
    if platform == "cover":
        parts = (primary("move_long_address"), primary("position_address"))
    elif platform == "climate":
        parts = tuple(
            primary(key)
            for key in (
                "temperature_address",
                "target_temperature_state_address",
                "target_temperature_address",
                "setpoint_shift_address",
            )
        )
    elif platform == "scene" and isinstance(row.get("scene_number"), int):
        address = primary("address")
        return f"{address}_{row['scene_number']}" if address else None
    else:
        return None
    return (
        "_".join(str(part) for part in parts)
        if any(part is not None for part in parts)
        else None
    )


def raw_platform_lists(config: dict | None) -> dict:
    """Match HA's ensure_list containers without converting any entity fields."""
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise DeploymentError("invalid_knx_yaml_configuration")
    result = deepcopy(config)
    for platform, rows in result.items():
        if isinstance(rows, dict):
            result[platform] = [rows]
        elif rows is None:
            result[platform] = []
    return result


def indexed_rows(config: dict, address_format: str | None = None) -> dict:
    """Reject identity collisions instead of silently losing one configured row."""
    result = {}
    for platform, rows in raw_platform_lists(config).items():
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not (
                uid := row_identity(platform, row, address_format)
            ):
                continue
            key = (platform, uid)
            if key in result:
                raise DeploymentError("duplicate_knx_entity_identity")
            result[key] = row
    return result


def adapt_plan_identity(
    plan: dict, mode: str, address_format: str | None = None
) -> dict:
    result = deepcopy(plan)
    if mode in ("native", "legacy"):
        metadata = {
            (entity["platform"], entity["uniqueId"]): entity
            for entity in result["entities"]
        }
        for platform, rows in result["config"].items():
            for row in rows:
                entity = metadata[(platform, row.pop("unique_id"))]
                if not (uid := row_identity(platform, row, address_format)):
                    raise DeploymentError("knx_native_identity_unsupported")
                entity["uniqueId"] = uid
                entity["canonicalUniqueId"] = row_identity(platform, row)
    elif mode != "custom":
        raise DeploymentError("knx_identity_mode_unsupported")
    indexed_rows(result["config"], address_format)
    result["identityMode"] = mode
    if mode == "legacy":
        result["addressFormat"] = address_format
    return result


def filter_existing(
    plan: dict,
    existing: dict,
    previous: dict | None,
    ui_addresses: set[str],
    address_format: str | None = None,
) -> tuple[dict, dict]:
    """Keep manual YAML and UI entities authoritative across all platforms."""
    existing = raw_platform_lists(existing)
    indexed_rows(existing, address_format)
    owned = {
        (entity["platform"], manifest_identity(entity, previous, address_format))
        for entity in (previous or {}).get("entities", [])
    }
    manual_addresses = {
        canonical for address in ui_addresses if (canonical := _address(str(address)))
    }
    for platform, entries in existing.items():
        for entry in entries if isinstance(entries, list) else []:
            if (
                isinstance(entry, dict)
                and (platform, row_identity(platform, entry, address_format))
                not in owned
            ):
                manual_addresses |= _addresses(entry)
    result = deepcopy(plan)
    result["config"], result["entities"] = {}, []
    result["skipped"] = list(plan.get("skipped", []))
    wanted = {(e["platform"], e["uniqueId"]): e for e in plan["entities"]}
    for platform, entries in plan["config"].items():
        for entry in entries:
            collisions = _addresses(entry) & manual_addresses
            if collisions:
                result["skipped"].extend(
                    {
                        "address": address,
                        "reason": "existing_manual_or_ui_entity_preserved",
                    }
                    for address in sorted(collisions)
                )
                continue
            result["config"].setdefault(platform, []).append(entry)
            result["entities"].append(
                wanted[(platform, row_identity(platform, entry, address_format))]
            )
    result["entityCount"] = len(result["entities"])
    combined = deepcopy(existing)
    for platform, entries in list(combined.items()):
        if isinstance(entries, list):
            combined[platform] = [
                entry
                for entry in entries
                if not (
                    isinstance(entry, dict)
                    and (platform, row_identity(platform, entry, address_format))
                    in owned
                )
            ]
    for platform, entries in result["config"].items():
        combined.setdefault(platform, []).extend(entries)
    return result, combined


def manual_yaml_plan(source: str) -> dict:
    """Accept bounded KNX entity data only, never HA tags, includes or paths."""
    if not isinstance(source, str) or not 0 < len(source.encode()) <= 131072:
        raise DeploymentError("manual_yaml_size_invalid")
    try:
        # Reject anchors before construction so expansion cannot consume memory.
        depth = 0
        for token in yaml.scan(source):
            if isinstance(
                token,
                (
                    yaml.tokens.FlowMappingStartToken,
                    yaml.tokens.FlowSequenceStartToken,
                    yaml.tokens.BlockMappingStartToken,
                    yaml.tokens.BlockSequenceStartToken,
                ),
            ):
                depth += 1
                if depth > 12:
                    raise DeploymentError("manual_yaml_too_deep")
            elif isinstance(
                token,
                (
                    yaml.tokens.FlowMappingEndToken,
                    yaml.tokens.FlowSequenceEndToken,
                    yaml.tokens.BlockEndToken,
                ),
            ):
                depth -= 1
            if isinstance(
                token,
                (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken),
            ):
                raise DeploymentError("manual_yaml_tags_or_aliases_not_allowed")
        node = yaml.compose(source)

        def check(value, depth=0):
            if depth > 12:
                raise DeploymentError("manual_yaml_too_deep")
            if isinstance(value, yaml.MappingNode):
                names = set()
                for key, child in value.value:
                    if (
                        not isinstance(key, yaml.ScalarNode)
                        or key.value in names
                        or key.value == "<<"
                    ):
                        raise DeploymentError("manual_yaml_duplicate_key")
                    names.add(key.value)
                    check(child, depth + 1)
            elif isinstance(value, yaml.SequenceNode):
                for child in value.value:
                    check(child, depth + 1)

        check(node)
        document = yaml.safe_load(source)
    except yaml.YAMLError as err:
        raise DeploymentError("manual_yaml_invalid") from err
    if not isinstance(document, dict) or set(document) != {"knx"}:
        raise DeploymentError("manual_yaml_requires_knx_root")
    config = document["knx"]
    platforms = {
        "light",
        "switch",
        "button",
        "scene",
        "sensor",
        "binary_sensor",
        "cover",
        "climate",
    }
    if not isinstance(config, dict) or not config or not set(config) <= platforms:
        raise DeploymentError("manual_yaml_platform_unsupported")
    entities = []
    identities = set()
    for platform, rows in config.items():
        if not isinstance(rows, list) or not rows:
            raise DeploymentError("manual_yaml_entities_must_be_list")
        for row in rows:
            if not isinstance(row, dict) or "unique_id" in row:
                raise DeploymentError("manual_yaml_entity_invalid")
            if (
                not isinstance(row.get("name"), str)
                or not 0 < len(row["name"].strip()) <= 120
            ):
                raise DeploymentError("manual_yaml_name_required")
            uid = row_identity(platform, row)
            if uid is None or (platform, uid) in identities:
                raise DeploymentError("manual_yaml_identity_invalid_or_duplicate")
            if platform == "scene" and (
                type(row.get("scene_number")) is not int
                or not 1 <= row["scene_number"] <= 64
            ):
                raise DeploymentError("manual_yaml_scene_number_required")
            identities.add((platform, uid))
            unique_id = "rexlite_manual_" + digest(f"{platform}:{uid}".encode())[:32]
            row["unique_id"] = unique_id
            entities.append(
                {
                    "platform": platform,
                    "uniqueId": unique_id,
                    "addresses": sorted(_addresses(row)),
                }
            )
    if not 0 < len(entities) <= 100:
        raise DeploymentError("manual_yaml_entity_limit")
    return {
        "config": config,
        "entities": entities,
        "entityCount": len(entities),
        "skipped": [],
    }


def append_manual_plan(base: dict, addition: dict) -> dict:
    """Add new identities without modifying active entities."""
    result = deepcopy(base)
    keys = {(e["platform"], e["uniqueId"]) for e in result["entities"]}
    addresses = {a for e in result["entities"] for a in e["addresses"]}
    for entity in addition["entities"]:
        if (entity["platform"], entity["uniqueId"]) in keys:
            raise DeploymentError("manual_yaml_entity_already_exists")
        # Sharing a scene address across distinct explicit numbers is legitimate.
        if entity["platform"] != "scene" and set(entity["addresses"]) & addresses:
            raise DeploymentError("manual_yaml_address_already_exists")
    for platform, rows in addition["config"].items():
        result["config"].setdefault(platform, []).extend(deepcopy(rows))
    result["entities"].extend(deepcopy(addition["entities"]))
    result["entityCount"] = len(result["entities"])
    used = {a for e in addition["entities"] for a in e["addresses"]}
    result["skipped"] = [
        item for item in result["skipped"] if item.get("address") not in used
    ]
    return result


def preserve_manual_plan(
    plan: dict, manual: dict, address_format: str | None = None
) -> dict:
    """Keep previously approved manual mappings authoritative on mapper upgrades."""
    auto_rows = indexed_rows(plan["config"], address_format)
    manual_rows = indexed_rows(manual["config"], address_format)
    rejected = set()
    for entity in plan["entities"]:
        identity = entity["platform"], entity["uniqueId"]
        for approved in manual["entities"]:
            approved_identity = approved["platform"], approved["uniqueId"]
            overlap = set(entity["addresses"]) & set(approved["addresses"])
            if identity == approved_identity:
                rejected.add(identity)
                break
            if not overlap:
                continue
            if entity["platform"] == approved["platform"] == "scene":
                number = auto_rows[identity].get("scene_number")
                approved_number = manual_rows[approved_identity].get("scene_number")
                if (
                    type(number) is int
                    and type(approved_number) is int
                    and 1 <= number <= 64
                    and 1 <= approved_number <= 64
                    and number != approved_number
                ):
                    continue
            rejected.add(identity)
            break
    retained = deepcopy(plan)
    retained["entities"] = [
        entity
        for entity in retained["entities"]
        if (entity["platform"], entity["uniqueId"]) not in rejected
    ]
    retained["config"] = {
        platform: [
            row
            for row in rows
            if (platform, row_identity(platform, row, address_format)) not in rejected
        ]
        for platform, rows in retained["config"].items()
    }
    retained["config"] = {key: rows for key, rows in retained["config"].items() if rows}
    skipped_addresses = {item.get("address") for item in retained["skipped"]}
    for entity in plan["entities"]:
        if (entity["platform"], entity["uniqueId"]) in rejected:
            for address in entity["addresses"]:
                if address not in skipped_addresses:
                    retained["skipped"].append(
                        {
                            "address": address,
                            "reason": "existing_manual_or_ui_entity_preserved",
                        }
                    )
                    skipped_addresses.add(address)
    result = append_manual_plan(retained, manual)
    mapped = {
        address for entity in result["entities"] for address in entity["addresses"]
    }
    result["skipped"] = [
        item for item in result["skipped"] if item.get("address") not in mapped
    ]
    result["mappedAddressCount"] = len(mapped)
    return result


class ProjectDeployer:
    """Serialize writes, durably journal them, reload and verify actual entities."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.files = SafeFiles(hass.config.config_dir)
        self.lock = asyncio.Lock()
        self.current: dict | None = None

    async def _io(self, function: Any, *args: Any) -> Any:
        return await self.hass.async_add_executor_job(function, *args)

    def _module(self, require_project: bool = True) -> Any:
        from homeassistant.components.knx.const import KNX_MODULE_KEY

        module = self.hass.data.get(KNX_MODULE_KEY)
        if module is None or (
            require_project and not getattr(module.project, "loaded", False)
        ):
            raise DeploymentError("knx_project_not_loaded")
        return module

    @staticmethod
    def _parse_uploaded_project(
        hass: Any, file_id: str, password: str, fingerprint: str
    ) -> dict:
        from homeassistant.components.file_upload import process_uploaded_file
        from xknxproject import XKNXProj

        from .knx_project_metadata import enrich_project

        def file_fingerprint(path: Path) -> str:
            checksum = hashlib.sha256()
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                size = 0
                while block := handle.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_PROJECT_BYTES:
                        raise DeploymentError("project_file_too_large")
                    checksum.update(block)
            if not size:
                raise DeploymentError("project_file_empty")
            return checksum.hexdigest()

        from .knx_project_upload import DATA_KEY as UPLOAD_KEY

        uploaded_file = (
            hass.data[UPLOAD_KEY].consume(file_id)
            if file_id.startswith("rexlite-") and UPLOAD_KEY in hass.data
            else process_uploaded_file(hass, file_id)
        )
        with uploaded_file as path:
            if file_fingerprint(path) != fingerprint:
                raise DeploymentError("project_file_fingerprint_mismatch")
            project = XKNXProj(
                path, password=password, language=hass.config.language
            ).parse()
            project = enrich_project(project, path, password=password)
            if file_fingerprint(path) != fingerprint:
                raise DeploymentError("project_file_changed_during_parse")
            return project

    def _validate_import_identity(
        self, previous: dict | None, project: dict, mode: str
    ) -> None:
        """Reject unsafe identity changes before saving the parsed KNX project."""
        if not previous:
            return
        previous_mode = previous.get("identityMode", "custom")
        if previous_mode == "custom":
            if mode != "custom":
                raise DeploymentError("managed_custom_identity_requires_newer_core")
            return
        parsed_format = {
            "ThreeLevel": "LONG",
            "TwoLevel": "SHORT",
            "Free": "FREE",
        }.get(project.get("info", {}).get("group_address_style"))
        if parsed_format is None:
            raise DeploymentError("knx_project_address_style_unsupported")
        if previous_mode == "native" and mode == "legacy" and parsed_format != "LONG":
            raise DeploymentError("legacy_knx_identity_format_changed")
        if previous_mode != "legacy" or parsed_format == previous.get("addressFormat"):
            return
        if mode == "legacy":
            raise DeploymentError("legacy_knx_identity_format_changed")
        # Core 2026.8+ may already have migrated legacy registry IDs on startup.
        # A new project format is safe only after that migration is confirmed.
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        for entity in previous.get("entities", []):
            canonical = entity.get("canonicalUniqueId")
            if canonical == entity["uniqueId"]:
                continue
            entity_id = registry.async_get_entity_id(
                entity["platform"], "knx", canonical
            )
            entry = registry.async_get(entity_id) if entity_id else None
            if entry is None or entry.config_entry_id != previous["configEntryId"]:
                raise DeploymentError("legacy_knx_identity_format_changed")

    async def process_project(
        self, file_id: str, password: str, fingerprint: str
    ) -> dict:
        """Bind the parsed HA project to the actual uploaded bytes on this hub."""
        if not FINGERPRINT.fullmatch(fingerprint):
            raise DeploymentError("invalid_project_fingerprint")
        async with self.lock:
            compatibility = self._compatibility()
            previous = await self._read_previous()
            if (
                previous
                and previous.get("identityMode", "custom") == "custom"
                and compatibility["identityMode"] != "custom"
            ):
                raise DeploymentError("managed_custom_identity_requires_newer_core")
            module = self._module(require_project=False)
            project = await self._io(
                self._parse_uploaded_project, self.hass, file_id, password, fingerprint
            )
            self._validate_import_identity(
                previous, project, compatibility["identityMode"]
            )
            from homeassistant.components.knx.project import (
                STORAGE_KEY,
                STORAGE_VERSION,
            )
            from homeassistant.helpers.storage import Store

            store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
            await store.async_save(project)
            await module.project.load_project(module.xknx, data=project)
            association = {
                "projectFingerprint": fingerprint,
                "parsedProjectDigest": project_digest(project),
            }
            stored = await module.project.get_knxproject()
            if (
                not stored
                or project_digest(stored) != association["parsedProjectDigest"]
            ):
                raise DeploymentError("project_changed_during_import")
            await self._io(self.files.write_json, IMPORT_ASSOCIATION, association)
            return association

    @staticmethod
    def _schema(config: dict) -> dict:
        from homeassistant.components.knx import CONFIG_SCHEMA

        return CONFIG_SCHEMA({"knx": deepcopy(config)})["knx"]

    @staticmethod
    def _compatibility() -> dict:
        from homeassistant.const import __version__

        return core_compatibility(__version__)

    @staticmethod
    def _address_format() -> str:
        from xknx.telegram.address import GroupAddress

        value = GroupAddress.address_format.name
        if value not in ("LONG", "SHORT", "FREE"):
            raise DeploymentError("knx_address_format_unsupported")
        return value

    def _identity_format(self) -> str | None:
        return (
            self._address_format()
            if self._compatibility()["identityMode"] == "legacy"
            else None
        )

    def _schema_preflight(self, mode: str) -> None:
        probe = {"light": [{"name": "REXLiTE validation", "address": "0/0/1"}]}
        if mode == "custom":
            probe["light"][0]["unique_id"] = "rexlite_schema_validation"
        try:
            self._schema(probe)
        except Exception as err:
            raise DeploymentError("knx_schema_unsupported") from err

    async def capabilities(self) -> dict:
        # First-install capabilities must not import KNX's optional dependencies.
        # Once KNX is loaded we also check its actual schema before any upload.
        from homeassistant.const import __version__

        from .const import INTEGRATION_VERSION

        info = {
            "homeAssistantVersion": __version__,
            "requiredHomeAssistantVersion": MINIMUM_KNX_CORE_VERSION,
            "integrationVersion": INTEGRATION_VERSION,
            "chunkedUpload": True,
            "maxProjectBytes": MAX_PROJECT_BYTES,
        }
        try:
            info.update(self._compatibility())
            component = sys.modules.get("homeassistant.components.knx")
            if component is not None and hasattr(component, "CONFIG_SCHEMA"):
                self._schema_preflight(info["identityMode"])
            await self._io(self.files.activation_changes)
            supported, reason, message = True, None, None
        except Exception as err:
            supported = False
            reason = (
                str(err)
                if isinstance(err, DeploymentError)
                else "configuration_preflight_failed"
            )
            message = {
                "home_assistant_version_unsupported": (
                    "自動匯入 KNX 需要 Home Assistant 2026.1.0 或更新的正式版本。"
                ),
                "knx_schema_unsupported": (
                    "此主機的 KNX 設定格式與自動匯入不相容，請更新 REXLiTE 整合後重試。"
                ),
            }.get(
                reason, "目前主機的設定結構無法安全加入自動 KNX 設定，請檢查主機設定。"
            )
        return {
            "version": 1,
            "apiVersion": 1,
            "yamlDeployment": supported,
            "supported": supported,
            "reason": reason,
            "message": message,
            "schemaValidation": "before_deployment",
            **info,
        }

    def _counts(self, manifest: dict) -> dict:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        loaded, available = 0, 0
        for entity in manifest.get("entities", []):
            entity_id = registry.async_get_entity_id(
                entity["platform"],
                "knx",
                manifest_identity(entity, manifest, self._identity_format()),
            )
            if (
                entity_id
                and (entry := registry.async_get(entity_id))
                and (
                    entry.config_entry_id == manifest.get("configEntryId")
                    and not entry.disabled_by
                )
                and (state := self.hass.states.get(entity_id)) is not None
                and not getattr(state, "attributes", {}).get("restored")
            ):
                loaded += 1
                if state.state not in ("unknown", "unavailable"):
                    available += 1
        return {"loadedCount": loaded, "availableCount": available}

    def _registry_entry(self, record: dict) -> Any:
        """Resolve only the exact registry identity recorded by this writer."""
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(self.hass).async_get(record["entityId"])
        if (
            entry is not None
            and entry.domain == record["platform"]
            and entry.platform == "knx"
            and entry.unique_id == record["uniqueId"]
            and entry.config_entry_id == record["configEntryId"]
        ):
            return entry
        return None

    def _registry_change(self, record: dict, *, reverse: bool = False) -> bool:
        """Compare-and-swap only our disabler, preserving user registry choices."""
        from homeassistant.helpers import entity_registry as er

        if (entry := self._registry_entry(record)) is None:
            return False
        before, after = record["beforeDisabled"], record["afterDisabled"]
        if reverse:
            before, after = after, before
        if entry.disabled_by not in (before, after):
            return False
        if not reverse and after == "integration" and before is None:
            state = self.hass.states.get(record["entityId"])
            module = self._module()
            if (
                (
                    state is not None
                    and not getattr(state, "attributes", {}).get("restored")
                )
                or (
                    (record["platform"], record["uniqueId"])
                    in indexed_rows(module.config_yaml, self._identity_format())
                )
                or set(record["addresses"]).intersection(self._ui_addresses(module))
            ):
                return False
        if entry.disabled_by != after:
            er.async_get(self.hass).async_update_entity(
                record["entityId"],
                disabled_by=er.RegistryEntryDisabler(after)
                if after is not None
                else None,
            )
        if after is not None:
            # Unloaded YAML entities have no live listener for disable events.
            # HA's global restore listener only cleans up removal/rename events.
            state = self.hass.states.get(record["entityId"])
            if state and getattr(state, "attributes", {}).get("restored"):
                self.hass.states.async_remove(record["entityId"])
        return True

    def _registry_restorations(self, previous: dict | None, plan: dict) -> list[dict]:
        """Re-enable only identities previously retired by this writer."""
        wanted = {(item["platform"], item["uniqueId"]) for item in plan["entities"]}
        return [
            {**item, "beforeDisabled": "integration", "afterDisabled": None}
            for item in (previous or {}).get("retiredEntities", [])
            if (item["platform"], item["uniqueId"]) in wanted
            and (entry := self._registry_entry(item)) is not None
            and entry.disabled_by == "integration"
        ]

    def _registry_retirements(
        self, previous: dict | None, manifest: dict
    ) -> list[dict]:
        """Retire replaced auto entities only after their replacements are loaded."""
        from homeassistant.helpers import entity_registry as er

        if not previous or previous.get("configEntryId") != manifest["configEntryId"]:
            return []
        address_format = self._identity_format()
        module = self._module()
        active = indexed_rows(module.config_yaml, address_format)
        ui_addresses = self._ui_addresses(module)
        manual = {
            (item["platform"], item["uniqueId"])
            for item in previous.get("manualPlan", {}).get("entities", [])
        }
        registry = er.async_get(self.hass)
        result = []
        for item in previous.get("entities", []):
            uid = manifest_identity(item, previous, address_format)
            identity = item["platform"], uid
            addresses = set(item.get("addresses", []))
            replacements = [
                {"platform": new["platform"], "uniqueId": new["uniqueId"]}
                for new in manifest["entities"]
                if addresses.intersection(new.get("addresses", []))
            ]
            if (
                identity in active
                or identity in manual
                or not replacements
                or addresses.intersection(ui_addresses)
            ):
                continue
            entity_id = registry.async_get_entity_id(item["platform"], "knx", uid)
            if entity_id is None:
                continue
            record = {
                "entityId": entity_id,
                "platform": item["platform"],
                "uniqueId": uid,
                "configEntryId": manifest["configEntryId"],
                "addresses": sorted(addresses),
                "replacements": replacements,
                "beforeDisabled": None,
                "afterDisabled": "integration",
            }
            if (entry := self._registry_entry(record)) is None or entry.disabled_by:
                continue
            state = self.hass.states.get(entity_id)
            if state is not None and not getattr(state, "attributes", {}).get(
                "restored"
            ):
                continue
            result.append(record)
        return result

    async def _read_previous(self) -> dict | None:
        previous = await self._io(self.files.read_json, MANIFEST)
        generated = await self._io(self.files.read, GENERATED)
        if (
            previous
            and previous.get("managedDigest")
            and (generated is None or digest(generated) != previous["managedDigest"])
        ):
            raise DeploymentError("managed_yaml_modified_externally")
        if generated is not None and not previous:
            raise DeploymentError("unowned_managed_yaml_exists")
        return previous

    async def status(self, fingerprint: str | None = None) -> dict | None:
        if self.lock.locked():
            return None
        manifest = await self._io(self.files.read_json, LAST_ATTEMPT)
        if not manifest or (
            fingerprint and manifest.get("projectFingerprint") != fingerprint
        ):
            manifest = await self._io(self.files.read_json, MANIFEST)
        if not manifest or (
            fingerprint and manifest.get("projectFingerprint") != fingerprint
        ):
            return None
        public = self._public(manifest)
        if manifest.get("status") in ("completed", "partial"):
            try:
                await self._read_previous()
                project = await self._module().project.get_knxproject()
                if (
                    not project
                    or project_digest(project) != manifest["parsedProjectDigest"]
                ):
                    raise DeploymentError("loaded_project_changed")
                await self._verify_configuration(manifest)
                public.update(self._counts(manifest))
                if public["loadedCount"] != public["entityCount"]:
                    raise DeploymentError("managed_entities_not_loaded")
            except Exception as err:
                public.update(
                    status="failed",
                    stage="verification",
                    error=str(err)
                    if isinstance(err, DeploymentError)
                    else "verification_failed",
                )
        return public

    async def _verify_configuration(self, manifest: dict) -> None:
        from homeassistant.config import async_hass_config_yaml

        configuration = await async_hass_config_yaml(self.hass)
        actual = self._schema(configuration.get("knx", {}))
        generated = await self._io(self.files.read, GENERATED)
        if generated is None or digest(generated) != manifest["managedDigest"]:
            raise DeploymentError("managed_yaml_modified_externally")
        expected = self._schema(yaml.safe_load(generated)["knx"])
        address_format = self._identity_format()
        wanted = {
            (e["platform"], manifest_identity(e, manifest, address_format))
            for e in manifest["entities"]
        }
        expected_rows = indexed_rows(expected, address_format)
        if set(expected_rows) != wanted:
            raise DeploymentError("managed_entity_identity_mismatch")
        for source in (actual, self._module().config_yaml):
            found = indexed_rows(source, address_format)
            if any(found.get(key) != value for key, value in expected_rows.items()):
                raise DeploymentError("managed_package_not_active")

    @staticmethod
    def _ui_addresses(module: Any) -> set[str]:
        # Include disabled entries: they are absent from the runtime address map.
        # Core 2026.1-4 has tuple runtime identifiers without a UI marker; its
        # authoritative config store below covers both enabled and disabled UI rows.
        data = getattr(getattr(module, "config_store", None), "data", None)
        if not isinstance(data, dict) or not isinstance(data.get("entities"), dict):
            raise DeploymentError("knx_ui_store_unsupported")
        result = {
            str(address)
            for address, identifiers in getattr(
                module, "group_address_entities", {}
            ).items()
            if any(getattr(identifier, "ui", False) for identifier in identifiers)
        }

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("write", "state", "passive"):
                        for address in item if isinstance(item, list) else [item]:
                            if canonical := _address(str(address)):
                                result.add(canonical)
                    elif isinstance(item, (dict, list)):
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(data["entities"])
        return result

    async def recover(self) -> None:
        """Recover an interrupted transaction after HA has started."""
        async with self.lock:
            if journal := await self._io(self.files.read_json, JOURNAL):
                await self._rollback(journal)

    @staticmethod
    def _public(manifest: dict) -> dict:
        keys = (
            "status",
            "stage",
            "projectFingerprint",
            "entityCount",
            "loadedCount",
            "availableCount",
            "skipped",
            "error",
            "manualDigest",
            "manualCount",
        )
        return {key: manifest[key] for key in keys if key in manifest}

    async def _rollback(self, journal: dict) -> None:
        def restore() -> None:
            # Compare-and-swap: do not overwrite external edits made during reload.
            for relative, item in reversed(list(journal["files"].items())):
                current = self.files.read(relative)
                encoded = item["before"]
                before = base64.b64decode(encoded) if encoded is not None else None
                after = base64.b64decode(item["after"])
                if current == before:
                    continue
                if current != after:
                    raise DeploymentError("rollback_blocked_by_external_edit")
                self.files.write(relative, before)
            self.files.write_json(
                MANIFEST,
                journal.get("previous")
                or {
                    "status": "failed",
                    "stage": "rolled_back",
                    "entityCount": 0,
                    "loadedCount": 0,
                    "availableCount": 0,
                    "skipped": [],
                },
            )

        await self._io(restore)
        for record in reversed(journal.get("registryChanges", [])):
            self._registry_change(record, reverse=True)
        async with asyncio.timeout(60):
            if not await self.hass.config_entries.async_reload(
                journal["configEntryId"]
            ):
                raise DeploymentError("rollback_reload_failed")
        await self._io(self.files.write, JOURNAL, None)

    async def deploy(
        self,
        fingerprint: str,
        *,
        manual_yaml: str | None = None,
        check_only: bool = False,
        baseline: str = "",
    ) -> dict:
        if not FINGERPRINT.fullmatch(fingerprint):
            raise DeploymentError("invalid_project_fingerprint")
        async with self.lock:
            self.current = {
                "status": "partial",
                "stage": "preparing",
                "projectFingerprint": fingerprint,
                "entityCount": 0,
                "loadedCount": 0,
                "availableCount": 0,
                "skipped": [],
            }
            journal = None
            manifest = dict(self.current)
            try:
                compatibility = self._compatibility()
                if pending := await self._io(self.files.read_json, JOURNAL):
                    if check_only:
                        raise DeploymentError("deployment_recovery_required")
                    await self._rollback(pending)
                previous = await self._read_previous()
                mode = compatibility["identityMode"]
                address_format = self._identity_format()
                if previous:
                    previous_mode = previous.get("identityMode", "custom")
                    if previous_mode in ("native", "legacy"):
                        mode = "legacy" if mode == "legacy" else "native"
                        if (
                            previous_mode == "legacy"
                            and mode == "legacy"
                            and previous.get("addressFormat") != address_format
                        ):
                            raise DeploymentError("legacy_knx_identity_format_changed")
                    elif mode != "custom":
                        raise DeploymentError(
                            "managed_custom_identity_requires_newer_core"
                        )
                module = self._module()
                self._schema_preflight(mode)
                project = await module.project.get_knxproject()
                if not project:
                    raise DeploymentError("knx_project_not_loaded")
                parsed_hash = project_digest(project)
                association = await self._io(self.files.read_json, IMPORT_ASSOCIATION)
                if (
                    not association
                    or association.get("projectFingerprint") != fingerprint
                ):
                    raise DeploymentError("project_import_not_bound_to_uploaded_file")
                if association.get("parsedProjectDigest") != parsed_hash:
                    raise DeploymentError("project_changed_after_import")
                if (
                    manual_yaml is None
                    and previous
                    and previous.get("projectFingerprint") == fingerprint
                    and previous.get("mapperRevision") == MAPPER_REVISION
                    and (previous.get("parsedProjectDigest") == parsed_hash)
                    and previous.get("status") in ("completed", "partial")
                ):
                    try:
                        await self._verify_configuration(previous)
                    except DeploymentError:
                        pass
                    else:
                        counts = self._counts(previous)
                        if counts["loadedCount"] == previous["entityCount"]:
                            verified = {**previous, **counts}
                            # A successful retry must replace any persisted failed
                            # attempt, including failures before a YAML transaction.
                            await self._io(
                                self.files.write_json, LAST_ATTEMPT, verified
                            )
                            return self._public(verified)
                plan = adapt_plan_identity(
                    await self._io(plan_project, project), mode, address_format
                )
                from homeassistant.config import async_hass_config_yaml

                configured = await async_hass_config_yaml(self.hass)
                # CONFIG_SCHEMA is not idempotent (it returns KNX enum objects).
                # Preserve raw rows for combined validation; normalize only copies
                # used in ownership comparisons below.
                existing = raw_platform_lists(configured.get("knx"))
                if previous:
                    # A manual row must not become owned merely by copying an ID.
                    before_config = yaml.safe_load(
                        await self._io(self.files.read, GENERATED)
                    )["knx"]
                    owned_rows = indexed_rows(
                        self._schema(before_config), address_format
                    )
                    existing_rows = indexed_rows(self._schema(existing), address_format)
                    if any(
                        key in existing_rows and existing_rows[key] != row
                        for key, row in owned_rows.items()
                    ):
                        raise DeploymentError("managed_entity_configuration_conflict")
                ui_addresses = self._ui_addresses(module)
                if manual_yaml is not None:
                    addition = adapt_plan_identity(
                        await self._io(manual_yaml_plan, manual_yaml),
                        mode,
                        address_format,
                    )
                    try:
                        self._schema(addition["config"])
                    except Exception as err:
                        # Return field locations only, never values or file contents.
                        path = ".".join(str(part) for part in getattr(err, "path", []))
                        safe_path = re.sub(r"[^a-zA-Z0-9_.]", "", path)[:200]
                        raise DeploymentError(
                            "manual_yaml_schema_invalid:" + safe_path
                        ) from err
                    base = {
                        "config": before_config if previous else {},
                        "entities": deepcopy(previous.get("entities", []))
                        if previous
                        else [],
                        "skipped": deepcopy(previous.get("skipped", plan["skipped"]))
                        if previous
                        else plan["skipped"],
                        "entityCount": previous.get("entityCount", 0)
                        if previous
                        else 0,
                        "projectId": plan.get("projectId"),
                        "identityMode": mode,
                        "addressFormat": address_format,
                    }
                    if previous and previous.get("projectFingerprint") != fingerprint:
                        raise DeploymentError("manual_yaml_project_changed")
                    plan = append_manual_plan(base, addition)
                    manual_config = (
                        deepcopy(
                            previous.get(
                                "manualPlan",
                                {
                                    "config": {},
                                    "entities": [],
                                    "skipped": [],
                                    "entityCount": 0,
                                },
                            )
                        )
                        if previous
                        else {
                            "config": {},
                            "entities": [],
                            "skipped": [],
                            "entityCount": 0,
                        }
                    )
                    plan["manualPlan"] = append_manual_plan(manual_config, addition)
                    plan["manualDigest"] = digest(manual_yaml.encode())
                    plan["manualCount"] = addition["entityCount"]
                    checked_fingerprint = project_digest(
                        {
                            "source": plan["manualDigest"],
                            "project": fingerprint,
                            "parsed": parsed_hash,
                            "configEntryId": module.entry.entry_id,
                            "existing": existing,
                            "ui": sorted(ui_addresses),
                            "managed": previous.get("managedDigest")
                            if previous
                            else None,
                            "root": digest(
                                (await self._io(self.files.read, "configuration.yaml"))
                                or b""
                            ),
                        }
                    )
                    if not check_only and (
                        not baseline or baseline != checked_fingerprint
                    ):
                        raise DeploymentError("manual_yaml_preflight_stale")
                elif previous and previous.get("manualPlan"):
                    if previous.get("projectFingerprint") != fingerprint:
                        raise DeploymentError("manual_yaml_project_changed")
                    plan = preserve_manual_plan(
                        plan, previous["manualPlan"], address_format
                    )
                    plan["manualPlan"] = previous["manualPlan"]
                expected_count = plan["entityCount"]
                plan, combined = filter_existing(
                    plan, existing, previous, ui_addresses, address_format
                )
                if manual_yaml is not None and plan["entityCount"] != expected_count:
                    raise DeploymentError(
                        "manual_yaml_conflicts_with_existing_configuration"
                    )
                manifest.update(
                    plan,
                    parsedProjectDigest=parsed_hash,
                    configEntryId=module.entry.entry_id,
                    mapperRevision=(previous or {}).get("mapperRevision", 0)
                    if manual_yaml is not None
                    else MAPPER_REVISION,
                )
                if not plan["entityCount"]:
                    manifest.update(
                        status="needs_review",
                        stage="mapping",
                        error="no_safe_entity_mappings",
                    )
                    # Preserve active deployment provenance on no-op uploads.
                    await self._io(self.files.write_json, LAST_ATTEMPT, manifest)
                    return self._public(manifest)
                self._schema(plan["config"])
                self._schema(combined)
                if check_only:
                    await self._io(self.files.activation_changes)
                    return {
                        "status": "ready",
                        "fingerprint": checked_fingerprint,
                        "projectFingerprint": fingerprint,
                        "manualDigest": plan["manualDigest"],
                        "manualCount": plan["manualCount"],
                    }

                def prepare_changes() -> tuple[dict, dict]:
                    original_root = self.files.read("configuration.yaml")
                    activation = self.files.activation_changes()
                    before = {path: self.files.read(path) for path in activation}
                    if self.files.read("configuration.yaml") != original_root:
                        raise DeploymentError("configuration_changed_during_deployment")
                    if "configuration.yaml" in before:
                        before["configuration.yaml"] = original_root
                    before[GENERATED] = self.files.read(GENERATED)
                    return activation, before

                changes, originals = await self._io(prepare_changes)
                generated = (
                    "# Generated by REXLiTE; manual KNX files are preserved.\n"
                    + yaml.safe_dump(
                        {"knx": plan["config"]}, allow_unicode=True, sort_keys=False
                    )
                ).encode()
                changes = {GENERATED: generated, **changes}
                manifest["managedDigest"] = digest(generated)
                journal = {
                    "configEntryId": module.entry.entry_id,
                    "previous": previous,
                    "files": {},
                    "registryChanges": self._registry_restorations(previous, plan),
                }
                for relative, after in changes.items():
                    before = originals[relative]
                    journal["files"][relative] = {
                        "before": base64.b64encode(before).decode()
                        if before is not None
                        else None,
                        "after": base64.b64encode(after).decode(),
                    }
                # Persist recovery metadata before the first change.
                await self._io(self.files.write_json, JOURNAL, journal)
                manifest.update(stage="writing")
                self.current = self._public(manifest)
                await self._io(self.files.write_json, MANIFEST, manifest)
                for relative, after in changes.items():
                    before = journal["files"][relative]["before"]
                    current = await self._io(self.files.read, relative)
                    if current != (
                        base64.b64decode(before) if before is not None else None
                    ):
                        raise DeploymentError("configuration_changed_during_deployment")
                    await self._io(self.files.write, relative, after)
                # Resolve HA includes/packages and validate the resulting schema.
                resolved = await async_hass_config_yaml(self.hass)
                actual = self._schema(resolved.get("knx", {}))
                expected = {(e["platform"], e["uniqueId"]) for e in plan["entities"]}
                found = set(indexed_rows(actual, address_format))
                if not expected <= found:
                    raise DeploymentError(
                        "generated_package_not_loaded_by_configuration"
                    )
                for record in journal["registryChanges"]:
                    self._registry_change(record)
                manifest.update(stage="reloading")
                self.current = self._public(manifest)
                async with asyncio.timeout(60):
                    if not await self.hass.config_entries.async_reload(
                        module.entry.entry_id
                    ):
                        raise DeploymentError("knx_reload_failed")
                manifest.update(stage="verifying")
                self.current = self._public(manifest)
                async with asyncio.timeout(20):
                    while True:
                        counts = self._counts(manifest)
                        if counts["loadedCount"] == plan["entityCount"]:
                            break
                        await asyncio.sleep(0.25)
                await self._verify_configuration(manifest)
                current_project = await self._module().project.get_knxproject()
                if (
                    not current_project
                    or project_digest(current_project) != parsed_hash
                ):
                    raise DeploymentError("loaded_project_changed_during_deployment")
                retirements = self._registry_retirements(previous, manifest)
                if retirements:
                    journal["registryChanges"].extend(retirements)
                    # Registry changes join the same durable recovery transaction.
                    await self._io(self.files.write_json, JOURNAL, journal)
                restored = {
                    (item["platform"], item["uniqueId"])
                    for item in manifest["entities"]
                }
                manifest["retiredEntities"] = [
                    item
                    for item in (previous or {}).get("retiredEntities", [])
                    if (item["platform"], item["uniqueId"]) not in restored
                ]
                manifest["retiredEntities"].extend(
                    record for record in retirements if self._registry_change(record)
                )
                manifest.update(
                    counts,
                    status="partial" if plan["skipped"] else "completed",
                    stage="completed",
                )
                manifest.pop("config", None)
                await self._io(self.files.write_json, MANIFEST, manifest)
                await self._io(self.files.write_json, LAST_ATTEMPT, manifest)
                await self._io(self.files.write, JOURNAL, None)
                return self._public(manifest)
            except Exception as err:
                error = (
                    str(err)
                    if isinstance(err, DeploymentError)
                    else "deployment_failed"
                )
                if journal:
                    try:
                        await self._rollback(journal)
                    except Exception:
                        error += ":rollback_failed"
                manifest.update(
                    status="failed",
                    stage="rolled_back" if journal else "preflight",
                    loadedCount=0,
                    availableCount=0,
                    error=error,
                )
                if not check_only:
                    await self._io(self.files.write_json, LAST_ATTEMPT, manifest)
                return self._public(manifest)
            finally:
                self.current = None


def register_websocket_commands(hass: Any) -> ProjectDeployer:
    """Register one admin-only command set for this HA instance."""
    import voluptuous as vol
    from homeassistant.components import websocket_api

    if DATA_KEY in hass.data:
        return hass.data[DATA_KEY]
    deployer = ProjectDeployer(hass)
    hass.data[DATA_KEY] = deployer

    from .knx_project_upload import DATA_KEY as UPLOAD_KEY
    from .knx_project_upload import ProjectUploads

    uploads = hass.data[UPLOAD_KEY] = ProjectUploads()

    async def cleanup_uploads(_event):
        await hass.async_add_executor_job(uploads.close)

    hass.bus.async_listen_once("homeassistant_stop", cleanup_uploads)

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "rexlite/knx/project_upload",
            vol.Required("action"): vol.In(["start", "chunk", "seal", "discard"]),
            vol.Required("uploadId"): vol.All(str, vol.Match(r"^[a-f0-9]{32}$")),
            vol.Required("owner"): vol.All(str, vol.Length(min=1, max=256)),
            vol.Optional("size"): int,
            vol.Optional("fileName"): str,
            vol.Optional("projectFingerprint"): str,
            vol.Optional("offset"): int,
            vol.Optional("data"): vol.All(str, vol.Length(max=699052)),
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def upload(hass, connection, msg):
        try:
            result = await hass.async_add_executor_job(uploads.request, msg)
        except ValueError as err:
            connection.send_error(msg["id"], "project_upload_failed", str(err))
        except OSError:
            connection.send_error(
                msg["id"], "project_upload_failed", "upload_storage_unavailable"
            )
        else:
            connection.send_result(msg["id"], result)

    @websocket_api.websocket_command(
        {vol.Required("type"): "rexlite/knx/project_capabilities"}
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def capabilities(hass: Any, connection: Any, msg: dict) -> None:
        connection.send_result(msg["id"], await deployer.capabilities())

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "rexlite/knx/process_project",
            vol.Required("file_id"): str,
            vol.Optional("password", default=""): str,
            vol.Required("projectFingerprint"): vol.All(str, vol.Match(FINGERPRINT)),
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def process(hass: Any, connection: Any, msg: dict) -> None:
        task = hass.async_create_task(
            deployer.process_project(
                msg["file_id"], msg.get("password", ""), msg["projectFingerprint"]
            ),
            "REXLiTE KNX project import",
        )
        try:
            result = await asyncio.shield(task)
        except DeploymentError as err:
            connection.send_error(msg["id"], "project_import_failed", str(err))
        except Exception:
            connection.send_error(
                msg["id"], "project_import_failed", "project_parse_failed"
            )
        else:
            connection.send_result(msg["id"], result)

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "rexlite/knx/deploy_project",
            vol.Required("projectFingerprint"): vol.All(str, vol.Match(FINGERPRINT)),
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def deploy(hass: Any, connection: Any, msg: dict) -> None:
        # A client disconnect must not cancel a write or leave HA half-reloaded.
        task = hass.async_create_task(
            deployer.deploy(msg["projectFingerprint"]), "REXLiTE KNX project deployment"
        )
        connection.send_result(msg["id"], await asyncio.shield(task))

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "rexlite/knx/project_deployment_status",
            vol.Optional("projectFingerprint"): vol.All(str, vol.Match(FINGERPRINT)),
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def status(hass: Any, connection: Any, msg: dict) -> None:
        try:
            result = await deployer.status(msg.get("projectFingerprint"))
        except DeploymentError as err:
            connection.send_error(msg["id"], "deployment_status_failed", str(err))
        else:
            connection.send_result(msg["id"], result)

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "rexlite/knx/manual_yaml",
            vol.Required("projectFingerprint"): vol.All(str, vol.Match(FINGERPRINT)),
            vol.Required("yaml"): vol.All(str, vol.Length(min=1, max=131072)),
            vol.Required("action"): vol.In(("check", "deploy")),
            vol.Optional("fingerprint", default=""): str,
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def manual(hass: Any, connection: Any, msg: dict) -> None:
        task = hass.async_create_task(
            deployer.deploy(
                msg["projectFingerprint"],
                manual_yaml=msg["yaml"],
                check_only=msg["action"] == "check",
                baseline=msg["fingerprint"],
            ),
            "REXLiTE manual KNX YAML",
        )
        connection.send_result(msg["id"], await asyncio.shield(task))

    for command in (capabilities, process, deploy, status, manual, upload):
        websocket_api.async_register_command(hass, command)

    return deployer
