"""Scene metadata enrichment is proven from archive parameters, not names."""

from __future__ import annotations

import copy
import importlib.util
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

SPEC = importlib.util.spec_from_file_location(
    "knx_metadata_test",
    Path(__file__).parents[1] / "custom_components/rexlite/knx_project_metadata.py",
)
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)
MAPPER_SPEC = importlib.util.spec_from_file_location(
    "knx_metadata_test_mapper",
    Path(__file__).parents[1] / "custom_components/rexlite/knx_project_mapper.py",
)
mapper = importlib.util.module_from_spec(MAPPER_SPEC)
MAPPER_SPEC.loader.exec_module(mapper)

APP = "M-02F0_A-0098-10-B2ED-O0085"
MODULE = "MD-5_M-21_MI-1"
OBJECT = f"{MODULE}_O-2-2_R-116"
REFERENCE = f"{APP}_{MODULE}_UP-192_R-258"
NAMESPACE = 'xmlns="http://knx.org/xml/project/20"'


def project():
    return {
        "info": {"project_id": "P-0001"},
        "devices": {"1.1.1": {"application": APP}},
        "group_addresses": {
            "1/0/1": {
                "identifier": "GA-1",
                "name": "Unrelated Scene 63 label",
                "communication_object_ids": [f"1.1.1/{OBJECT}"],
            }
        },
        "communication_objects": {
            f"1.1.1/{OBJECT}": {
                "device_address": "1.1.1",
                "group_address_links": ["1/0/1"],
                "flags": {"communication": True, "transmit": True},
                "dpts": [{"main": 18, "sub": 1}],
            }
        },
    }


def device_xml(value="5", links="GA-1", extra=""):
    parameter = (
        f'<ParameterInstanceRef RefId="{REFERENCE}" Value="{value}" />'
        if value is not None
        else ""
    )
    return f'''<KNX {NAMESPACE}><Area Address="1"><Line Address="1">
      <DeviceInstance Address="1"><ParameterInstanceRefs>{parameter}{extra}
      </ParameterInstanceRefs><ComObjectInstanceRefs>
      <ComObjectInstanceRef RefId="{OBJECT}" Links="{links}" />
      </ComObjectInstanceRefs></DeviceInstance></Line></Area></KNX>'''


def app_xml(meaning="Scene number [1..64]", maximum="64", ref_value=""):
    return f'''<KNX {NAMESPACE}>
      <Parameter Id="{APP}_MD-5_UP-192" Name="{meaning}"
        ParameterType="{APP}_PT-Range" Value="1" />
      <ParameterRef Id="{APP}_MD-5_UP-192_R-258"
        RefId="{APP}_MD-5_UP-192" {ref_value} />
      <ParameterType Id="{APP}_PT-Range"><TypeNumber Type="unsignedInt"
        SizeInBit="8" minInclusive="1" maxInclusive="{maximum}" />
      </ParameterType></KNX>'''


class SceneMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sample.knxproj"

    def archive(self, device=None, application=None):
        with ZipFile(self.path, "w") as archive:
            archive.writestr("P-0001/0.xml", device or device_xml())
            archive.writestr(f"{APP[:6]}/{APP}.xml", application or app_xml())

    def annotated(self, parsed=None):
        result = metadata.enrich_project(parsed or project(), self.path)
        return result["group_addresses"]["1/0/1"]

    def test_explicit_instance_number_ignores_name_and_preserves_input(self):
        self.archive()
        parsed = project()
        before = copy.deepcopy(parsed)
        result = self.annotated(parsed)
        self.assertEqual(result["scene_number"], 5)
        self.assertEqual(result["scene_number_source"], "ets-sender-parameter")
        self.assertEqual(result["scene_sender_ids"], [f"1.1.1/{OBJECT}"])
        self.assertEqual(parsed, before)
        self.assertEqual(self.annotated(parsed), result)

    def test_application_default_and_reference_override(self):
        self.archive(device_xml(None))
        self.assertEqual(self.annotated()["scene_number"], 1)
        self.archive(device_xml(None), app_xml(ref_value='Value="7"'))
        self.assertEqual(self.annotated()["scene_number"], 7)

    def test_rejects_wrong_meaning_or_range(self):
        for application in [app_xml("Scene page index"), app_xml(maximum="255")]:
            with self.subTest(application=application):
                self.archive(application=application)
                self.assertNotIn("scene_number", self.annotated())

    def test_rejects_out_of_range_or_invalid_values(self):
        for value in ["0", "65", "-1", "1.0", "", "true"]:
            with self.subTest(value=value):
                self.archive(device_xml(value))
                self.assertNotIn("scene_number", self.annotated())

    def test_rejects_multiple_or_mismatched_raw_links(self):
        for links in ["GA-1 GA-2", "GA-2", ""]:
            self.archive(device_xml(links=links))
            self.assertNotIn("scene_number", self.annotated())

    def test_rejects_duplicate_parameter_ids(self):
        extra = f'<ParameterInstanceRef RefId="{REFERENCE}" Value="6" />'
        self.archive(device_xml(extra=extra))
        self.assertNotIn("scene_number", self.annotated())

    def test_unknown_revision_or_object_layout_does_not_guess(self):
        self.archive()
        parsed = project()
        parsed["devices"]["1.1.1"]["application"] += "-UNKNOWN"
        self.assertNotIn("scene_number", self.annotated(parsed))
        parsed = project()
        object_id = f"1.1.1/{OBJECT}"
        replacement = object_id.replace("_R-116", "_R-117")
        parsed["communication_objects"][replacement] = parsed[
            "communication_objects"
        ].pop(object_id)
        parsed["group_addresses"]["1/0/1"]["communication_object_ids"] = [replacement]
        self.assertNotIn("scene_number", self.annotated(parsed))

    def test_non_scene_or_non_transmitting_object_is_not_proof(self):
        self.archive()
        for flags, dpts in [
            ({"communication": True, "write": True}, [{"main": 18, "sub": 1}]),
            ({"communication": True, "transmit": True}, [{"main": 5, "sub": 1}]),
        ]:
            parsed = project()
            parsed["communication_objects"][f"1.1.1/{OBJECT}"].update(
                flags=flags, dpts=dpts
            )
            self.assertNotIn("scene_number", self.annotated(parsed))

    def test_unknown_second_transmitter_blocks_annotation(self):
        self.archive()
        parsed = project()
        unknown = "1.1.2/O-1_R-1"
        parsed["communication_objects"][unknown] = {
            **copy.deepcopy(parsed["communication_objects"][f"1.1.1/{OBJECT}"]),
            "device_address": "1.1.2",
        }
        parsed["group_addresses"]["1/0/1"]["communication_object_ids"].append(unknown)
        self.assertNotIn("scene_number", self.annotated(parsed))

    def test_disagreement_between_verified_senders_blocks_annotation(self):
        second_object = OBJECT.replace("MI-1", "MI-2")
        second_reference = REFERENCE.replace("MI-1", "MI-2")
        extra = f'<ParameterInstanceRef RefId="{second_reference}" Value="6" />'
        device = device_xml(extra=extra).replace(
            "</ComObjectInstanceRefs>",
            f'<ComObjectInstanceRef RefId="{second_object}" Links="GA-1" />'
            "</ComObjectInstanceRefs>",
        )
        self.archive(device)
        parsed = project()
        parsed["communication_objects"][f"1.1.1/{second_object}"] = copy.deepcopy(
            parsed["communication_objects"][f"1.1.1/{OBJECT}"]
        )
        parsed["group_addresses"]["1/0/1"]["communication_object_ids"].append(
            f"1.1.1/{second_object}"
        )
        self.assertNotIn("scene_number", self.annotated(parsed))

    def test_xml_entities_and_duplicate_zip_members_keep_baseline(self):
        self.archive(device='<!DOCTYPE KNX [<!ENTITY e "5">]>' + device_xml("&e;"))
        self.assertNotIn("scene_number", self.annotated())
        self.archive()
        with warnings.catch_warnings(), ZipFile(self.path, "a") as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("P-0001/0.xml", device_xml())
        self.assertNotIn("scene_number", self.annotated())

    def test_missing_or_protected_project_retains_baseline(self):
        self.assertNotIn("scene_number", self.annotated())
        with ZipFile(self.path, "w") as archive:
            archive.writestr("P-0001.zip", "opaque encrypted project")
        self.assertNotIn("scene_number", self.annotated())

    def test_bounded_archive_and_xml_sizes(self):
        self.archive()
        for limit in [
            "MAX_ARCHIVE_BYTES",
            "MAX_XML_BYTES",
            "MAX_TOTAL_XML_BYTES",
            "MAX_APPLICATION_XML_BYTES",
            "MAX_TOTAL_APPLICATION_XML_BYTES",
            "MAX_MEMBERS",
        ]:
            with self.subTest(limit=limit), patch.object(metadata, limit, 1):
                self.assertNotIn("scene_number", self.annotated())


CHPB = "M-0085_A-0088-20-98F9"
CHPB_OBJECT = "O-149_R-1160"
CHPB_REFERENCES = sorted(set(metadata._CHPB_SCENE_OBJECTS.values()))


def chpb_project(object_id=CHPB_OBJECT, application=CHPB):
    parsed = project()
    parsed["devices"]["1.1.1"]["application"] = application
    parsed["group_addresses"]["1/0/1"]["communication_object_ids"] = [
        f"1.1.1/{object_id}"
    ]
    parsed["communication_objects"] = {
        f"1.1.1/{object_id}": project()["communication_objects"][f"1.1.1/{OBJECT}"]
    }
    return parsed


def chpb_device_xml(values, object_id=CHPB_OBJECT, application=CHPB):
    parameters = "".join(
        f'<ParameterInstanceRef RefId="{application}_{reference}" Value="{value}" />'
        for reference, value in values.items()
    )
    return f'''<KNX {NAMESPACE}><Area Address="1"><Line Address="1">
      <DeviceInstance Address="1"><ParameterInstanceRefs>{parameters}
      </ParameterInstanceRefs><ComObjectInstanceRefs>
      <ComObjectInstanceRef RefId="{object_id}" Links="GA-1" />
      </ComObjectInstanceRefs></DeviceInstance></Line></Area></KNX>'''


def chpb_app_xml(references=CHPB_REFERENCES, application=CHPB):
    """Every button's scene number, enumerated 1..64 like the product data."""
    enumeration = "".join(
        f'<Enumeration Text="Scene No.{n}" Value="{n}" '
        f'Id="{application}_PT-S_EN-{n}" />'
        for n in range(1, 65)
    )
    # One parameter may have several references (the 3-gang's buttons 5/6).
    parameters = "".join(
        f'''<Parameter Id="{application}_{parameter}" Value="1"
          Name="    8 bit scene number" ParameterType="{application}_PT-S" />'''
        for parameter in sorted({ref.split("_R-")[0] for ref in references})
    ) + "".join(
        f'''<ParameterRef Id="{application}_{reference}"
          RefId="{application}_{reference.split("_R-")[0]}" />'''
        for reference in references
    )
    return f'''<KNX {NAMESPACE}>{parameters}
      <ParameterType Id="{application}_PT-S"><TypeRestriction Base="Value"
        SizeInBit="8">{enumeration}</TypeRestriction></ParameterType></KNX>'''


class ButtonSceneMetadataTests(unittest.TestCase):
    """GVS CHPB: a button's scene number is the parameter of its own branch."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sample.knxproj"

    def archive(self, device, application=CHPB, app=None):
        with ZipFile(self.path, "w") as archive:
            archive.writestr("P-0001/0.xml", device)
            archive.writestr(
                f"{application[:6]}/{application}.xml",
                app or chpb_app_xml(application=application),
            )

    def annotated(self, parsed):
        return metadata.enrich_project(parsed, self.path)["group_addresses"]["1/0/1"]

    def test_button_reads_its_own_scene_number(self):
        # Button 2's number, never another button's.
        self.archive(chpb_device_xml({"UP-3665_R-3692": "9", "UP-207_R-152": "4"}))
        result = self.annotated(chpb_project())
        self.assertEqual(result["scene_number"], 9)
        self.assertEqual(result["scene_number_source"], "ets-sender-parameter")
        self.assertIs(result["scene_wall_key"], True)

    def test_application_default(self):
        self.archive(chpb_device_xml({}))
        self.assertEqual(self.annotated(chpb_project())["scene_number"], 1)

    def test_one_gang_uses_the_borrowed_references(self):
        one_gang = "M-0085_A-00C4-20-2793"
        references = sorted(set(metadata.BUTTON_SCENE_LAYOUTS[one_gang][0].values()))
        self.archive(
            chpb_device_xml({"UP-3822_R-6331": "3"}, "O-156_R-2150", one_gang),
            one_gang,
            chpb_app_xml(references, one_gang),
        )
        result = self.annotated(chpb_project("O-156_R-2150", one_gang))
        self.assertEqual(result["scene_number"], 3)

    def test_an_object_of_another_function_is_not_proof(self):
        # Button 2's switch object, not its scene object.
        self.archive(chpb_device_xml({}, "O-149_R-1105"))
        self.assertNotIn("scene_number", self.annotated(chpb_project("O-149_R-1105")))

    def test_a_missing_reference_keeps_baseline(self):
        self.archive(chpb_device_xml({}), app=chpb_app_xml(CHPB_REFERENCES[1:]))
        self.assertNotIn("scene_number", self.annotated(chpb_project()))

    def test_application_entities_are_refused_while_streaming(self):
        app = chpb_app_xml().replace(
            f"<KNX {NAMESPACE}>", f'<!DOCTYPE KNX [<!ENTITY e "1">]><KNX {NAMESPACE}>'
        )
        self.archive(chpb_device_xml({}), app=app)
        self.assertNotIn("scene_number", self.annotated(chpb_project()))

    def test_a_program_streamed_in_many_chunks(self):
        self.archive(chpb_device_xml({"UP-3665_R-3692": "9"}))
        with patch.object(metadata, "_STREAM_CHUNK", 7):
            self.assertEqual(self.annotated(chpb_project())["scene_number"], 9)

    def test_an_unproven_program_leaves_the_others_alone(self):
        # A 3TS key and a CHPB key in one home; the CHPB program is broken.
        parsed = chpb_project()
        parsed["devices"]["1.1.2"] = {"application": APP}
        parsed["group_addresses"]["1/0/2"] = {
            "identifier": "GA-2",
            "name": "3TS",
            "communication_object_ids": [f"1.1.2/{OBJECT}"],
        }
        parsed["communication_objects"][f"1.1.2/{OBJECT}"] = {
            **project()["communication_objects"][f"1.1.1/{OBJECT}"],
            "device_address": "1.1.2",
            "group_address_links": ["1/0/2"],
        }
        device = chpb_device_xml({}).replace(
            "</Line>",
            f'''<DeviceInstance Address="2"><ParameterInstanceRefs>
            <ParameterInstanceRef RefId="{REFERENCE}" Value="5" />
            </ParameterInstanceRefs><ComObjectInstanceRefs>
            <ComObjectInstanceRef RefId="{OBJECT}" Links="GA-2" />
            </ComObjectInstanceRefs></DeviceInstance></Line>''',
        )
        with ZipFile(self.path, "w") as archive:
            archive.writestr("P-0001/0.xml", device)
            archive.writestr(f"{CHPB[:6]}/{CHPB}.xml", "<KNX><broken")
            archive.writestr(f"{APP[:6]}/{APP}.xml", app_xml())
        result = metadata.enrich_project(parsed, self.path)["group_addresses"]
        self.assertNotIn("scene_number", result["1/0/1"])
        self.assertEqual(result["1/0/2"]["scene_number"], 5)
        # A 3TS scene object also receives: not a wall key's own scene.
        self.assertNotIn("scene_wall_key", result["1/0/2"])

    def test_every_chpb_layout_lists_known_objects(self):
        for application, (objects, meaning) in metadata.BUTTON_SCENE_LAYOUTS.items():
            with self.subTest(application=application):
                self.assertEqual(meaning, "8 bit scene number")
                self.assertTrue(
                    set(metadata._CHPB_SCENE_OBJECTS.items()) <= set(objects.items())
                )


CHPBD_V12 = ("M-0085_A-00BC-12-78B4", "M-0085_A-00BD-12-0DBA")
# Button 6 of a CHPBD-06 (ETS module instance M-9), as in the D3-7F project.
CHPBD_OBJECT = "MD-4_M-9_MI-1_O-2-0_R-56"
CHPBD_REFERENCE = "MD-4_M-9_MI-1_UP-59_R-62"


class DisplayButtonSceneMetadataTests(unittest.TestCase):
    """GVS CHPBD 1.2: a button's scene number sits in its own module instance."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sample.knxproj"

    def archive(self, application, values):
        with ZipFile(self.path, "w") as archive:
            archive.writestr(
                "P-0001/0.xml", chpb_device_xml(values, CHPBD_OBJECT, application)
            )
            archive.writestr(
                f"{application[:6]}/{application}.xml",
                chpb_app_xml(["MD-4_UP-59_R-62"], application),
            )

    def test_version_1_2_reads_the_module_scene_number(self):
        for application in CHPBD_V12:
            with self.subTest(application=application):
                self.archive(application, {CHPBD_REFERENCE: "3"})
                parsed = chpb_project(CHPBD_OBJECT, application)
                result = metadata.enrich_project(parsed, self.path)
                group = result["group_addresses"]["1/0/1"]
                self.assertEqual(group["scene_number"], 3)
                self.assertIs(group["scene_wall_key"], True)

    def test_version_1_2_scene_key_maps_to_a_scene(self):
        # A scene key left at the program default, linked only to its address.
        for application in CHPBD_V12:
            with self.subTest(application=application):
                self.archive(application, {})
                parsed = chpb_project(CHPBD_OBJECT, application)
                parsed["info"]["guid"] = "test-project-guid"
                parsed["functions"] = {}
                parsed["group_addresses"] = {
                    "0/7/6": {
                        **parsed["group_addresses"]["1/0/1"],
                        "address": "0/7/6",
                        "name": "7F-測試 面板1 按鈕-6",
                        "dpt": {"main": 18, "sub": 1},
                    }
                }
                sender = parsed["communication_objects"][f"1.1.1/{CHPBD_OBJECT}"]
                sender["group_address_links"] = ["0/7/6"]
                plan = mapper.plan_project(metadata.enrich_project(parsed, self.path))
                scene = plan["config"]["scene"][0]
                self.assertEqual(
                    (scene["address"], scene["scene_number"]), ("0/7/6", 1)
                )
                self.assertNotIn(
                    "missing_explicit_scene_number",
                    {skipped["reason"] for skipped in plan["skipped"]},
                )

    def test_version_1_2_buttons_are_wall_keys(self):
        self.assertLessEqual(set(CHPBD_V12), metadata.WALL_KEY_APPLICATIONS)


if __name__ == "__main__":
    unittest.main()
