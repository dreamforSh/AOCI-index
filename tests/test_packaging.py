#!/usr/bin/env python3
"""Packaging guards: MANIFEST.in covers the wheel skill bundle, and pyproject metadata stays PEP 639 compliant."""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aoci_testkit import SKILL_DIR  # noqa: E402


def _setup_tuple(name: str) -> tuple:
    tree = ast.parse((SKILL_DIR / "setup.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"`{name}` not found in setup.py")


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = (SKILL_DIR / "MANIFEST.in").read_text(encoding="utf-8")
        cls.pyproject = (SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8")

    def _manifest_includes(self, rel: str) -> bool:
        top = rel.split("/", 1)[0]
        for line in self.manifest.splitlines():
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            if parts[0] == "include" and rel in parts[1:]:
                return True
            if parts[0] in ("recursive-include", "graft") and len(parts) >= 2 and parts[1] == top:
                return True
        return False

    def test_manifest_ships_everything_the_wheel_bundle_copies(self) -> None:
        # `python -m build` builds the wheel from the sdist: anything setup.py bundles must be in the sdist
        for rel in (*_setup_tuple("_SKILL_TOP_FILES"), *_setup_tuple("_SKILL_TOP_DIRS"), *_setup_tuple("_REQUIRED_BUNDLE_FILES")):
            self.assertTrue(self._manifest_includes(rel), f"MANIFEST.in does not ship `{rel}`")
            self.assertTrue((SKILL_DIR / rel).exists(), f"bundled path `{rel}` does not exist")
        self.assertNotIn("tests", _setup_tuple("_SKILL_TOP_DIRS"), "tests must not ship inside the wheel bundle")

    def test_pyproject_uses_pep639_license_and_complete_metadata(self) -> None:
        text = self.pyproject
        self.assertRegex(text, r'(?m)^license = "MIT"$')
        self.assertRegex(text, r'(?m)^license-files = \["LICENSE"\]$')
        self.assertNotIn("License ::", text, "PEP 639 forbids license classifiers next to a license expression")
        req = re.search(r'(?m)^requires = \["setuptools>=(\d+)', text)
        self.assertIsNotNone(req)
        self.assertGreaterEqual(int(req.group(1)), 77, "license expressions need setuptools>=77")
        for key in ("Homepage", "Repository", "Issues", "Changelog"):
            self.assertRegex(text, rf"(?m)^{key} = \"https://github.com/dreamforSh/AOCI-index")
        self.assertRegex(text, r'(?m)^requires-python = ">=3\.8"$')
        self.assertIn('"Programming Language :: Python :: 3.8"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
