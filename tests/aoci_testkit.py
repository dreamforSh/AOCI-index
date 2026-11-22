#!/usr/bin/env python3
"""
Shared test kit for the aoci-index suite (stdlib `unittest` only, no third-party deps).

Paths (`SKILL_DIR`, `SCRIPT`, `HOOK`, `INSTALL`), the in-process / subprocess CLI runners (`cli`, `cli_json`,
`hook`; set `AOCI_TEST_SUBPROCESS=1` for real subprocesses), `age_file`, and `RepoTestCase` (a throwaway
Git repository per test; set `AOCI_TEST_TMP` to choose the temp root).
Run the suite with:  python -B -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import ast
import atexit
import contextlib
import gzip
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from typing import Optional

sys.dont_write_bytecode = True
SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "scripts" / "aoci_index.py"
HOOK = SKILL_DIR / "scripts" / "aoci_hook.py"
INSTALL = SKILL_DIR / "scripts" / "install.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import aoci_hook  # noqa: E402
import aoci_index  # noqa: E402
from aoci_core import constants, discovery, git_ops, parsers  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def _use_subprocess_runner() -> bool:
    return os.environ.get("AOCI_TEST_SUBPROCESS") == "1"


def _cli_in_process(*args: str, stdin: Optional[str] = None):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(stdin if stdin is not None else "")
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                rc = int(aoci_index.main([str(a) for a in args]))
            except SystemExit as exc:
                if isinstance(exc.code, int):
                    rc = exc.code
                elif exc.code is None:
                    rc = 0
                else:
                    rc = 1
                    err_buf.write(f"\n{exc.code}")
    finally:
        sys.stdin = old_stdin
    return rc, out_buf.getvalue(), err_buf.getvalue()


def cli(*args: str, stdin: str = None):
    if _use_subprocess_runner():
        r = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=stdin,
        )
        return r.returncode, r.stdout + r.stderr
    rc, out, err = _cli_in_process(*args, stdin=stdin)
    return rc, out + err


def cli_json(*args: str) -> dict:
    """Run a `--json` command and parse stdout only (stderr may carry interpreter/launcher noise)."""
    if _use_subprocess_runner():
        r = subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(r.stdout)
    _, out, _ = _cli_in_process(*args, "--json")
    return json.loads(out)


def hook(event: str, payload: dict, host: str = "claude-code") -> dict:
    if _use_subprocess_runner():
        r = subprocess.run(
            [sys.executable, str(HOOK), "--host", host, "--event", event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(r.stdout or "{}")
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(json.dumps(payload))
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                aoci_hook.main(["--host", host, "--event", event])
            except SystemExit:
                pass
    finally:
        sys.stdin = old_stdin
    return json.loads(out_buf.getvalue() or "{}")


def age_file(path: Path, seconds: int = 60) -> None:
    """Push mtime into the past so fingerprints are not 'racy' (models a file edited a while ago)."""
    t = time.time() - seconds
    os.utime(path, (t, t))


_TEMPLATE_TMP: Optional[tempfile.TemporaryDirectory] = None
_TEMPLATE_GIT_DIR: Optional[Path] = None


def _cleanup_template_git_dir() -> None:
    global _TEMPLATE_TMP, _TEMPLATE_GIT_DIR
    if _TEMPLATE_TMP is not None:
        try:
            _TEMPLATE_TMP.cleanup()
        except OSError:
            pass
        _TEMPLATE_TMP = None
        _TEMPLATE_GIT_DIR = None


def _get_template_git_dir() -> Path:
    global _TEMPLATE_TMP, _TEMPLATE_GIT_DIR
    if _TEMPLATE_GIT_DIR is None or not _TEMPLATE_GIT_DIR.is_dir():
        _TEMPLATE_TMP = tempfile.TemporaryDirectory(dir=os.environ.get("AOCI_TEST_TMP"))
        atexit.register(_cleanup_template_git_dir)
        tpl = Path(_TEMPLATE_TMP.name) / "tpl"
        tpl.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=tpl, check=True)
        _TEMPLATE_GIT_DIR = tpl / ".git"
    return _TEMPLATE_GIT_DIR


@unittest.skipUnless(HAS_GIT, "git is required")
class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AOCI_TEST_TMP"))
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        shutil.copytree(_get_template_git_dir(), self.root / ".git")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str, aged: bool = True) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        if aged:
            age_file(p)
        return p

    def init(self) -> None:
        code, out = cli("init", "--root", str(self.root))
        self.assertEqual(code, 0, out)

    def upsert(self, *entries: str, extra=()):
        args = ["upsert", "--root", str(self.root), *extra]
        for e in entries:
            args += ["--entry", e]
        return cli(*args)

    def code_text(self) -> str:
        return (self.root / ".aoci" / "aoci.code.txt").read_text(encoding="utf-8")
