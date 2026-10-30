"""Recover explicit scene numbers omitted by the standard ETS parser.

Only versioned, verified sender application layouts are supported. A number is
bound to a single-address scene object in the same module instance - or, for a
sender without modules, to the parameter of the object's own ETS branch - never
to a group-address label. Unknown/encrypted layouts retain the original parser
data.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 32 * 1024 * 1024
# Application programs are streamed, never held whole: GVS CHPB's are ~8.5 MB
# each and a home has several. These bound the bytes read, not memory.
MAX_APPLICATION_XML_BYTES = 32 * 1024 * 1024
MAX_TOTAL_APPLICATION_XML_BYTES = 128 * 1024 * 1024
_STREAM_CHUNK = 64 * 1024
# A ParameterType's own children, kept until the type itself is read.
_TYPE_PARTS = {"TypeNumber", "TypeRestriction", "Enumeration"}

# Application revision: module, scene object, parameter reference, meaning.
# Extending this table requires an ETS fixture proving the binding and encoding.
# GVS CHPBD push buttons with display: one module per button, "Scene control".
SCENE_LAYOUTS = {
    "M-0085_A-00BD-11-006E": ("MD-4", "O-2-0_R-56", "UP-59_R-62", "8 bit scene number"),
    "M-0085_A-00BB-12-301A": ("MD-4", "O-2-0_R-56", "UP-59_R-62", "8 bit scene number"),
    "M-0085_A-00BC-10-3A06": ("MD-4", "O-2-0_R-56", "UP-59_R-62", "8 bit scene number"),
    "M-0085_A-00BD-10-3994": ("MD-4", "O-2-0_R-56", "UP-59_R-62", "8 bit scene number"),
    "M-0085_A-00BC-12-78B4": ("MD-4", "O-2-0_R-56", "UP-59_R-62", "8 bit scene number"),
    "M-0085_A-00BD-12-0DBA": ("MD-4", "O-2-0_R-56", "UP-59_R-62", "8 bit scene number"),
    "M-02F0_A-0098-10-B2ED-O0085": (
        "MD-5",
        "O-2-2_R-116",
        "UP-192_R-258",
        "Scene number [1..64]",
    ),
    "M-02F0_A-0092-10-CF6C-O0085": (
        "MD-9",
        "O-2-0_R-1",
        "P-28_R-28",
        "Scene number [1..64]",
    ),
}


# GVS CHPB push buttons keep each button's "Scene control" at device level, so
# a button's scene object reference names the scene-number parameter reference
# of the same ETS branch (read from the application programs' Dynamic section;
# the 1-gang and the 3-gang's buttons 5/6 use other references of the same
# objects). Application revision: scene object → parameter reference, meaning.
_CHPB_SCENE_OBJECTS = {
    "O-142_R-78": "UP-207_R-152",
    "O-149_R-1160": "UP-3665_R-3692",
    "O-156_R-1274": "UP-3822_R-3857",
    "O-163_R-1388": "UP-3979_R-4023",
    "O-170_R-1502": "UP-4138_R-4188",
    "O-177_R-1616": "UP-4295_R-4355",
    "O-184_R-1730": "UP-4454_R-4516",
    "O-184_R-2522": "UP-4454_R-6556",
    "O-191_R-1844": "UP-4611_R-4682",
    "O-191_R-2764": "UP-4611_R-6736",
}
BUTTON_SCENE_LAYOUTS = {
    "M-0085_A-00C4-20-2793": (
        {
            **_CHPB_SCENE_OBJECTS,
            "O-156_R-2150": "UP-3822_R-6331",
            "O-163_R-2266": "UP-3979_R-6872",
        },
        "8 bit scene number",
    ),
    "M-0085_A-0088-20-98F9": (_CHPB_SCENE_OBJECTS, "8 bit scene number"),
    "M-0085_A-0089-20-4EA3": (_CHPB_SCENE_OBJECTS, "8 bit scene number"),
    "M-0085_A-008A-20-FCD0": (_CHPB_SCENE_OBJECTS, "8 bit scene number"),
    "M-0085_A-0069-10-9A10": (_CHPB_SCENE_OBJECTS, "8 bit scene number"),
}


# GVS push buttons, whose scene object only sends (its Write flag is off). On a
# wall key's own address the scene is the homeowner's trigger in Home Assistant
# rather than a KNX scene a device recalls, so the mapper needs no receiver.
WALL_KEY_APPLICATIONS = frozenset(BUTTON_SCENE_LAYOUTS) | {
    "M-0085_A-00BD-11-006E",
    "M-0085_A-00BB-12-301A",
    "M-0085_A-00BC-10-3A06",
    "M-0085_A-00BD-10-3994",
    "M-0085_A-00BC-12-78B4",
    "M-0085_A-00BD-12-0DBA",
}


def _references(application: str) -> tuple[str, list[str], str]:
    """The module ('' for none), parameter references and meaning of a layout."""
    if application in SCENE_LAYOUTS:
        module, _, reference, meaning = SCENE_LAYOUTS[application]
        return module, [reference], meaning
    objects, meaning = BUTTON_SCENE_LAYOUTS[application]
    return "", sorted(set(objects.values())), meaning


class _Archive:
    def __init__(self, archive: ZipFile) -> None:
        self.archive = archive
        members = archive.infolist()
        self.members = {entry.filename: entry for entry in members}
        if len(members) > MAX_MEMBERS or len(self.members) != len(members):
            raise ValueError("Unsupported or ambiguous archive")
        self.total = 0
        self.application_total = 0

    def elements(self, name: str) -> Iterator[ET.Element]:
        """Stream an application program's elements as each one ends.

        The same checks as xml() - no DTD or entity declaration, UTF-8 only -
        on a bounded stream; the caller may clear an element once read.
        """
        entry = self.members[name]
        if entry.flag_bits & 1 or entry.file_size > MAX_APPLICATION_XML_BYTES:
            raise ValueError("Protected or oversized XML")
        self.application_total += entry.file_size
        if self.application_total > MAX_TOTAL_APPLICATION_XML_BYTES:
            raise ValueError("XML budget exceeded")
        decoder = codecs.getincrementaldecoder("utf-8-sig")()
        parser = ET.XMLPullParser(events=("end",))
        size, tail = 0, ""
        with self.archive.open(entry) as stream:
            while True:
                chunk = stream.read(_STREAM_CHUNK)
                size += len(chunk)
                if size > entry.file_size:
                    raise ValueError("Invalid XML size")
                text = decoder.decode(chunk, final=not chunk)
                window = (tail + text).upper()
                if "\x00" in text or "<!DOCTYPE" in window or "<!ENTITY" in window:
                    raise ValueError("Unsupported XML declaration")
                tail = (tail + text)[-16:]
                parser.feed(text)
                if not chunk:
                    parser.close()
                for _, element in parser.read_events():
                    yield element
                if not chunk:
                    break
        if size != entry.file_size:
            raise ValueError("Invalid XML size")

    def xml(self, name: str) -> ET.Element:
        entry = self.members[name]
        if entry.flag_bits & 1 or entry.file_size > MAX_XML_BYTES:
            raise ValueError("Protected or oversized XML")
        self.total += entry.file_size
        if self.total > MAX_TOTAL_XML_BYTES:
            raise ValueError("XML budget exceeded")
        with self.archive.open(entry) as stream:
            data = stream.read(MAX_XML_BYTES + 1)
        if len(data) != entry.file_size or len(data) > MAX_XML_BYTES:
            raise ValueError("Invalid XML size")
        # ETS UTF-8 XML needs neither DTDs nor entity declarations. Reject both
        # before parsing, including UTF-16 input rather than guessing encodings.
        text = data.decode("utf-8-sig")
        if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("Unsupported XML declaration")
        return ET.fromstring(text)


def _unique_index(elements: list[ET.Element], attribute: str) -> dict:
    result = {}
    for element in elements:
        key = element.get(attribute)
        if not key or key in result:
            raise ValueError("Ambiguous ETS identifier")
        result[key] = element
    return result


def _devices(root: ET.Element) -> dict[str, ET.Element]:
    result = {}
    for area in root.findall(".//{*}Area"):
        for line in area.findall(".//{*}Line"):
            for device in line.findall(".//{*}DeviceInstance"):
                address = ".".join(
                    str(item.get("Address")) for item in (area, line, device)
                )
                if address in result:
                    raise ValueError("Ambiguous device address")
                result[address] = device
    return result


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _scene_number_type(parameter_type: ET.Element) -> bool:
    """Whether a ParameterType is a scene number 1..64, as a range or a list."""
    number = parameter_type.find("./{*}TypeNumber")
    restriction = parameter_type.find("./{*}TypeRestriction")
    enumeration = parameter_type.findall("./{*}TypeRestriction/{*}Enumeration")
    numeric_range = number is not None and all(
        number.get(key) == value
        for key, value in {
            "Type": "unsignedInt",
            "SizeInBit": "8",
            "minInclusive": "1",
            "maxInclusive": "64",
        }.items()
    )
    enum_range = (
        restriction is not None
        and restriction.get("Base") == "Value"
        and restriction.get("SizeInBit") == "8"
        and len(enumeration) == 64
        and {el.get("Value") for el in enumeration} == {str(n) for n in range(1, 65)}
    )
    return numeric_range or enum_range


def _parameters(bounded: _Archive, application: str) -> dict[str, str]:
    """Each layout reference → its application default, proven from the program.

    The program is streamed; only what proves a layout's references is kept:
    every Id must be unique, the reference must be a ParameterRef to the
    Parameter it names, of the layout's meaning and a 1..64 scene number type.
    """
    module, references, meaning = _references(application)
    wanted = {
        "_".join(part for part in (application, module, reference) if part): reference
        for reference in references
    }
    seen: set[str] = set()
    refs: dict[str, dict] = {}
    parameters: dict[str, tuple[str, str, str]] = {}
    scene_types: set[str] = set()
    for element in bounded.elements(f"{application[:6]}/{application}.xml"):
        tag = _tag(element)
        identifier = element.get("Id")
        if identifier:
            if identifier in seen:
                raise ValueError("Ambiguous ETS identifier")
            seen.add(identifier)
        if tag == "ParameterRef" and identifier in wanted:
            refs[identifier] = dict(element.attrib)
        elif tag == "Parameter":
            parameters[identifier or ""] = (
                element.get("Name", ""),
                element.get("ParameterType", ""),
                element.get("Value", ""),
            )
        elif tag == "ParameterType" and _scene_number_type(element):
            scene_types.add(identifier or "")
        if tag not in _TYPE_PARTS:
            element.clear()
    result = {}
    for ref_id, reference in wanted.items():
        ref = refs[ref_id]
        name, parameter_type, default = parameters[ref["RefId"]]
        if ref["RefId"] != ref_id.rsplit("_R-", 1)[0] or name.strip() != meaning:
            raise ValueError("Unknown scene parameter meaning")
        if parameter_type not in scene_types:
            raise ValueError("Unknown scene-number encoding")
        result[reference] = ref.get("Value", default)
    return result


def _sender_number(
    object_id: str,
    obj: dict,
    ga: dict,
    project: dict,
    devices: dict,
    parameters: dict,
) -> int:
    device_address = obj["device_address"]
    application = project["devices"][device_address]["application"]
    device_part, local_id = object_id.split("/", 1)
    if application in SCENE_LAYOUTS:
        module, object_suffix, reference, _ = SCENE_LAYOUTS[application]
        match = re.fullmatch(
            rf"({module}_M-[0-9]+_MI-[0-9]+)_{re.escape(object_suffix)}", local_id
        )
        if not match:
            raise ValueError("Unknown scene object layout")
        instance_id = f"{application}_{match.group(1)}_{reference}"
    else:
        reference = BUTTON_SCENE_LAYOUTS[application][0].get(local_id, "")
        if not reference:
            raise ValueError("Unknown scene object layout")
        instance_id = f"{application}_{reference}"
    if device_part != device_address:
        raise ValueError("Unknown scene object layout")
    raw_device = devices[device_address]
    objects = _unique_index(raw_device.findall(".//{*}ComObjectInstanceRef"), "RefId")
    raw_object = objects[local_id]
    # Corroborate the parsed object against the same raw project's exact link.
    if raw_object.get("Links", "").split() != [ga["identifier"]]:
        raise ValueError("Scene sender has ambiguous links")
    refs = _unique_index(raw_device.findall(".//{*}ParameterInstanceRef"), "RefId")
    default = parameters[application][reference]
    instance = refs.get(instance_id)
    value = instance.get("Value", "") if instance is not None else default
    if not re.fullmatch(r"[0-9]+", value) or not 1 <= int(value) <= 64:
        raise ValueError("Invalid scene number")
    return int(value)


def enrich_project(
    project: dict, path: str | Path, password: str | None = None
) -> dict:
    """Return a shallow copy with proven scene annotations; never alter input.

    This optional enrichment intentionally skips protected project archives.
    The upstream parser still handles their password and imports them normally.
    """
    del password  # No password is needed or retained by this unencrypted reader.
    try:
        project_id = project["info"]["project_id"]
        if not re.fullmatch(r"P-[A-Fa-f0-9]+", project_id):
            return project
        if Path(path).stat().st_size > MAX_ARCHIVE_BYTES:
            return project
        with ZipFile(path) as archive:
            bounded = _Archive(archive)
            devices = _devices(bounded.xml(f"{project_id}/0.xml"))
            applications = {
                value.get("application") for value in project["devices"].values()
            } & (SCENE_LAYOUTS.keys() | BUTTON_SCENE_LAYOUTS.keys())
            parameters = {}
            for application in sorted(applications):
                # A program that cannot be proven leaves only its own senders
                # unannotated: a home's 3TS keys do not depend on its buttons.
                try:
                    parameters[application] = _parameters(bounded, application)
                except (KeyError, ValueError, ET.ParseError):
                    continue
        groups = dict(project["group_addresses"])
        objects = project["communication_objects"]
        for address, ga in sorted(groups.items()):
            senders, numbers = [], set()
            try:
                for object_id in ga.get("communication_object_ids", []):
                    obj = objects[object_id]
                    flags = obj.get("flags", {})
                    if not (
                        flags.get("communication") is True
                        and flags.get("transmit") is True
                    ):
                        continue
                    if (
                        obj.get("group_address_links") != [address]
                        or not obj.get("dpts")
                        or any(
                            d not in ({"main": 17, "sub": 1}, {"main": 18, "sub": 1})
                            for d in obj["dpts"]
                        )
                    ):
                        raise ValueError("Sender is not an unambiguous scene object")
                    numbers.add(
                        _sender_number(object_id, obj, ga, project, devices, parameters)
                    )
                    senders.append(object_id)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if senders and len(numbers) == 1:
                groups[address] = {
                    **ga,
                    "scene_number": next(iter(numbers)),
                    "scene_number_source": "ets-sender-parameter",
                    "scene_sender_ids": sorted(senders),
                }
                if all(
                    project["devices"][objects[sender]["device_address"]]["application"]
                    in WALL_KEY_APPLICATIONS
                    for sender in senders
                ):
                    groups[address]["scene_wall_key"] = True
        return {**project, "group_addresses": groups}
    except (
        OSError,
        BadZipFile,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        RuntimeError,
        NotImplementedError,
        ET.ParseError,
    ):
        return project
