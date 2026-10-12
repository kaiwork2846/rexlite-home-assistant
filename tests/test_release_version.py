"""Prevent stale firmware labels after a package release."""

import ast
import json
import unittest
from pathlib import Path


class ReleaseVersionTests(unittest.TestCase):
    def test_device_firmware_matches_manifest(self) -> None:
        root = Path(__file__).parents[1] / "custom_components" / "rexlite"
        manifest = json.loads((root / "manifest.json").read_text())
        tree = ast.parse((root / "const.py").read_text())
        versions = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "INTEGRATION_VERSION"
        ]
        self.assertEqual(versions, [manifest["version"]])

    def test_readme_and_changelog_name_the_released_version(self) -> None:
        # One release, one version everywhere a person or HACS looks for it.
        repo = Path(__file__).parents[1]
        version = json.loads(
            (repo / "custom_components" / "rexlite" / "manifest.json").read_text()
        )["version"]
        readme = (repo / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"**目前版本：`{version}`**", readme)
        changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
        latest = next(
            line[3:].strip()
            for line in changelog.splitlines()
            if line.startswith("## ")
        )
        self.assertEqual(latest, version)
