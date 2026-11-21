#!/usr/bin/env python3
"""Setuptools build hook embedding the complete AOCI skill bundle inside built wheels."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_REPO_ROOT = Path(__file__).resolve().parent
_SKILL_TOP_FILES = ("SKILL.md", "README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml")
_SKILL_TOP_DIRS = ("assets", "examples", "references", "resources", "scripts")
_SKIP_PATTERNS = (
    "__pycache__",
    "_skill_bundle",
    "site-packages",
    "*.pyc",
    "*.pyo",
    "*.bak",
    "*.orig.bak",
    "*.tmp",
    "*.egg-info",
    ".DS_Store",
    ".aoci-install.json",
)


class BuildPyWithSkillBundle(_build_py):
    """Copy verified skill files into `aoci_core/_skill_bundle` inside wheel/build outputs."""

    def run(self) -> None:
        super().run()
        if self.editable_mode:
            return
        bundle_dest = Path(self.build_lib) / "aoci_core" / "_skill_bundle"
        if bundle_dest.exists():
            shutil.rmtree(bundle_dest)
        bundle_dest.mkdir(parents=True, exist_ok=True)

        for fname in _SKILL_TOP_FILES:
            src_f = _REPO_ROOT / fname
            if src_f.is_file():
                shutil.copy2(src_f, bundle_dest / fname)

        ignore_fn = shutil.ignore_patterns(*_SKIP_PATTERNS)
        for dname in _SKILL_TOP_DIRS:
            src_d = _REPO_ROOT / dname
            if src_d.is_dir():
                shutil.copytree(src_d, bundle_dest / dname, ignore=ignore_fn)


setup(cmdclass={"build_py": BuildPyWithSkillBundle})
