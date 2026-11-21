#!/usr/bin/env python3
"""
Regression tests for the aoci-index skill (stdlib `unittest` only, no third-party deps).

Run:  python tests/test_aoci.py           (set AOCI_TEST_TMP to choose the temp root)
Each test builds a throwaway Git repository and drives the real CLI via subprocess.
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


class UpsertValidationTests(RepoTestCase):
    def test_too_large_file_is_rejected_instead_of_looping(self):
        self.write("a.py", "x = 1\n")
        self.write("big.sql", "-- row\n" * 120000)  # > 512 KB
        self.init()
        code, out = self.upsert(
            "a.py[CG1T]: F:sets module constant x | R:- | A:- | S:-",
            "big.sql[PG3H]: F:database dump fixture | R:- | A:- | S:-",
        )
        self.assertEqual(code, 1)
        self.assertIn("excluded from indexing", out)
        self.assertIn("too-large", out)
        code, out = self.upsert("a.py[CG1T]: F:sets module constant x | R:- | A:- | S:-")
        self.assertEqual(code, 0, out)
        code, out = cli("verify", "--root", str(self.root))
        self.assertEqual(code, 0, out)

    def test_wrong_case_path_is_corrected_or_rejected_never_silently_dropped(self):
        self.write("src/Auth.py", "token = 1\n")
        self.init()
        code, out = self.upsert("src/auth.py[CG1T]: F:defines auth token constant | R:- | A:- | S:-")
        case_insensitive_fs = (self.root / "src/auth.py").exists()
        if case_insensitive_fs:
            self.assertEqual(code, 0, out)
            self.assertIn("Corrected path case", out)
            self.assertIn("src/Auth.py[", self.code_text())
            self.assertIn("TotalIndexed=1", out)
        else:
            self.assertEqual(code, 1, out)
            self.assertIn("does not exist", out)

    def test_gitignored_and_traversal_paths_rejected(self):
        self.write(".gitignore", "secret.txt\n")
        self.write("secret.txt", "s\n")
        self.write("ok.py", "v = 2\n")
        self.init()
        code, out = self.upsert("secret.txt[CG1T]: F:holds a local secret value | R:- | A:- | S:-")
        self.assertEqual(code, 1)
        self.assertIn("not discovered", out)
        code, out = self.upsert("../outside.py[CG1T]: F:escapes the repository root | R:- | A:- | S:-")
        self.assertEqual(code, 1)
        self.assertIn("no `..`", out)

    def test_malformed_lines_are_protected(self):
        self.write("a.py", "x = 1\n")
        self.write("b.py", "y = 2\n")
        self.init()
        self.assertEqual(self.upsert("a.py[CG1T]: F:sets module constant x | R:- | A:- | S:-")[0], 0)
        with open(self.root / ".aoci" / "aoci.code.txt", "a", encoding="utf-8") as fh:
            fh.write("this line is broken\n")
        code, out = self.upsert("b.py[CG1T]: F:sets module constant y | R:- | A:- | S:-")
        self.assertEqual(code, 1)
        self.assertIn("malformed", out)
        code, out = self.upsert(
            "b.py[CG1T]: F:sets module constant y | R:- | A:- | S:-", extra=["--drop-malformed"]
        )
        self.assertEqual(code, 0, out)

    def test_baseline_is_compact_schema_v2(self):
        self.write("a.py", "x = 1\n")
        self.init()
        self.upsert("a.py[CG1T]: F:sets module constant x | R:- | A:- | S:-")
        raw = (self.root / ".aoci" / "baseline.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["schema_version"], 2)
        self.assertNotIn("size_tier", data["files"]["a.py"])
        self.assertNotIn("\n  ", raw)


class ConcurrencyTests(RepoTestCase):
    def test_parallel_upserts_do_not_lose_entries(self):
        n = 16
        for i in range(n):
            self.write(f"m/f{i}.py", f"value = {i}\n")
        self.init()
        procs = [
            subprocess.Popen(
                [
                    sys.executable, str(SCRIPT), "upsert", "--root", str(self.root), "--no-prune",
                    "--entry", f"m/f{i}.py[CG1T]: F:defines numeric value {i} | R:- | A:- | S:-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in range(n)
        ]
        codes = [p.wait() for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(codes, [0] * n)
        persisted = sum(1 for ln in self.code_text().splitlines() if ln.startswith("m/"))
        self.assertEqual(persisted, n)
        self.assertFalse((self.root / ".aoci" / ".lock").exists())

    def test_dry_run_is_lockless_and_side_effect_free(self):
        body = "def handler():\n    return 1\n\n\nVALUE = 2\n"
        self.write("a.py", body)
        self.init()
        self.upsert("a.py[AX5T]: F:handles incoming requests cleanly | R:- | A:handler | S:-")
        # Simulate an active live writer holding `.aoci/.lock` and remove `.git/info/exclude`
        lock_path = self.root / ".aoci" / ".lock"
        lock_path.write_text(f"{os.getpid()} 2026-11-19T00:00:00Z\n", encoding="ascii")
        exclude_path = self.root / ".git" / "info" / "exclude"
        exclude_path.write_text("# clean\n", encoding="utf-8")
        cache_file = self.root / ".aoci" / ".cache" / "git_exclusion.json"
        if cache_file.exists():
            cache_file.unlink()

        # `--dry-run` on upsert, ack, and migrate-renames must not wait on `.lock` or touch exclude/cache
        self.write("b.py", "y = 2\n")
        code, out = self.upsert(
            "b.py[CX2T]: F:defines secondary constant y | R:- | A:- | S:-",
            extra=["--dry-run"],
        )
        self.assertEqual(code, 0, out)
        self.assertIn("DRY-RUN", out)

        self.write("a.py", body + "EXTRA = 3\n")
        code, out = cli("ack", "--root", str(self.root), "--all-modified", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("WouldAck=1", out)

        code, out = cli("migrate-renames", "--root", str(self.root), "--dry-run")
        self.assertEqual(code, 0, out)

        self.assertEqual(exclude_path.read_text(encoding="utf-8"), "# clean\n")
        self.assertFalse(cache_file.exists())
        self.assertTrue(lock_path.exists())
        lock_path.unlink()

    def test_dead_pid_lock_is_recovered_immediately(self):
        self.write("a.py", "x = 1\n")
        self.init()
        lock_path = self.root / ".aoci" / ".lock"
        lock_path.write_text("99999999 2026-11-19T00:00:00Z\n", encoding="ascii")
        age_file(lock_path, seconds=2)
        t0 = time.monotonic()
        code, out = self.upsert("a.py[CG1T]: F:sets module constant x | R:- | A:- | S:-")
        elapsed = time.monotonic() - t0
        self.assertEqual(code, 0, out)
        self.assertLess(elapsed, 10.0)
        self.assertFalse(lock_path.exists())


class DriftTests(RepoTestCase):
    def test_trivial_identical_files_are_not_treated_as_renames(self):
        self.write("pkg_a/__init__.py", "")
        self.write("pkg_a/core.py", "a = 1\n")
        self.init()
        self.upsert(
            "pkg_a/__init__.py[CX1T]: F:marks pkg_a as a package | R:- | A:- | S:-",
            "pkg_a/core.py[DX5T]: F:defines core constant a | R:- | A:- | S:-",
        )
        (self.root / "pkg_a/__init__.py").unlink()
        self.write("pkg_b/__init__.py", "")
        code, out = cli("migrate-renames", "--root", str(self.root), "--dry-run")
        self.assertIn("No exact-content renames", out)

    def test_unique_nontrivial_rename_is_migrated_with_refs(self):
        body = "def handler():\n    return 1\n\n\nVALUE = 2\n"
        self.write("src/old_name.py", body)
        self.write("src/user.py", "from src import old_name\n\n\nold_name.handler()\n")
        self.init()
        self.upsert(
            "src/old_name.py[AX5T]: F:request handler returning one | R:- | A:handler<-src/user.py | S:-",
            "src/user.py[EX3T]: F:entrypoint invoking the request handler | R:src/old_name.py | A:- | S:-",
        )
        (self.root / "src/old_name.py").rename(self.root / "src/new_name.py")
        code, out = cli("migrate-renames", "--root", str(self.root))
        self.assertEqual(code, 0, out)
        text = self.code_text()
        self.assertIn("src/new_name.py[AX5T]", text)
        self.assertIn("R:src/new_name.py", text)
        # Renaming a file referenced through the `<-` caller arrow must rewrite that reference too
        (self.root / "src/user.py").rename(self.root / "src/main.py")
        code, out = cli("migrate-renames", "--root", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn("A:handler<-src/main.py", self.code_text())

    def test_racy_same_size_same_mtime_edit_is_detected(self):
        p = self.write("pkg/mod.py", "def f():\n    return 0\n", aged=False)
        self.init()
        self.assertEqual(self.upsert("pkg/mod.py[CX3T]: F:returns the constant zero | R:- | A:f | S:-")[0], 0)
        st = os.stat(p)
        p.write_bytes(p.read_bytes().replace(b"return 0", b"return 9"))
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        code, out = cli("scan", "--root", str(self.root), "--fast")
        self.assertIn("Modified=1", out)

    def test_fast_scan_clean_on_aged_files(self):
        for i in range(5):
            self.write(f"d/f{i}.py", f"n = {i}\n")
        self.init()
        self.upsert(*[f"d/f{i}.py[CX2T]: F:defines constant number {i} | R:- | A:- | S:-" for i in range(5)])
        code, out = cli("scan", "--root", str(self.root), "--fast")
        self.assertIn("Status: CLEAN", out)

    def test_scan_reports_affected_neighbors(self):
        self.write("src/env.py", "A = 1\n")
        self.write("src/jwt.py", "B = 2\n")
        self.init()
        self.upsert(
            "src/env.py[CG7T]: F:loads environment settings | R:- | A:get_settings<-src/jwt.py | S:-",
            "src/jwt.py[MA8T]: F:issues signed access tokens | R:src/env.py | A:issue_token | S:alg allowlist",
        )
        self.write("src/env.py", "A = 3\n")
        code, out = cli("scan", "--root", str(self.root))
        self.assertIn("AFFECTED NEIGHBORS", out)
        self.assertIn("src/env.py <- src/jwt.py", out)

    def test_racy_flag_self_heals_after_window_passes(self):
        self.init()
        p = self.write("fresh.py", "value = 42\n", aged=False)
        t_recent = time.time() - 1.6  # inside 3s RACY_WINDOW_NS with subprocess headroom
        os.utime(p, (t_recent, t_recent))
        self.assertEqual(self.upsert("fresh.py[CX2T]: F:defines fresh value forty two | R:- | A:- | S:-")[0], 0)
        base1 = json.loads((self.root / ".aoci" / "baseline.json").read_text(encoding="utf-8"))
        self.assertTrue(base1["files"]["fresh.py"].get("racy"))

        time.sleep(max(0.35, 3.1 - (time.time() - t_recent)))  # now >3s past p's mtime without touching p
        code, out = cli("scan", "--root", str(self.root), "--fast")
        self.assertEqual(code, 0, out)
        base2 = json.loads((self.root / ".aoci" / "baseline.json").read_text(encoding="utf-8"))
        self.assertNotIn("racy", base2["files"]["fresh.py"])

    def test_similar_rename_moved_and_edited_migrates_to_modified(self):
        orig_lines = [f"def step_{i}(ctx):\n    return ctx + {i}\n" for i in range(10)]
        self.write("src/old_flow.py", "".join(orig_lines))
        self.write("src/caller.py", "from src import old_flow\n")
        self.init()
        self.upsert(
            "src/old_flow.py[AX8T]: F:executes ten-step payment pipeline | R:- | A:step_0<-src/caller.py | S:idempotent",
            "src/caller.py[EX3T]: F:invokes payment pipeline steps | R:src/old_flow.py | A:- | S:-",
        )
        # Move file AND edit 1 step so SHA-256 changes (~90% line similarity)
        (self.root / "src/old_flow.py").unlink()
        edited = list(orig_lines)
        edited[-1] = "def step_9(ctx):\n    return ctx + 999\n"
        self.write("src/pay/new_flow.py", "".join(edited))

        scan_data = cli_json("scan", "--root", str(self.root))
        self.assertEqual(len(scan_data["drift"]["similar_renames"]), 1)
        self.assertEqual(scan_data["drift"]["similar_renames"][0]["from"], "src/old_flow.py")
        self.assertEqual(scan_data["drift"]["similar_renames"][0]["to"], "src/pay/new_flow.py")

        code, out = cli("migrate-renames", "--root", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn("MigratedSimilar=1", out)
        text = self.code_text()
        self.assertIn("src/pay/new_flow.py[AX8T]", text)
        self.assertIn("R:src/pay/new_flow.py", text)
        # Moved+edited file MUST remain MODIFIED until reviewed via ack/upsert
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 1)
        self.assertEqual(cli("ack", "--root", str(self.root), "src/pay/new_flow.py")[0], 0)
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 0)

    def test_git_fast_incremental_scan_and_shards(self):
        self.write("root.py", "r = 0\n")
        self.write("pkg/a.py", "a = 1\n")
        self.write("pkg/b.py", "b = 2\n")
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "add", "root.py", "pkg/a.py", "pkg/b.py"],
            cwd=self.root,
            check=True,
        )
        self.init()
        self.upsert(
            "root.py[CX2T]: F:defines root level constant r | R:- | A:- | S:-",
            "pkg/a.py[DX5T]: F:defines package constant a | R:- | A:- | S:-",
            "pkg/b.py[DX8T]: F:defines package hub constant b | R:pkg/a.py | A:- | S:must stay positive",
        )
        # First --git-fast populates `.aoci/.cache/git_discovery.json`; second uses `git status` fast path
        self.assertEqual(cli_json("scan", "--root", str(self.root), "--git-fast")["drift"]["has_drift"], False)
        self.assertTrue((self.root / ".aoci" / ".cache" / "git_discovery.json").exists())
        self.assertEqual(cli_json("scan", "--root", str(self.root), "--git-fast")["drift"]["has_drift"], False)

        # Edit tracked file + add untracked file -> --git-fast detects both accurately
        self.write("pkg/a.py", "a = 99\n")
        self.write("pkg/c.py", "c = 3\n")
        d = cli_json("scan", "--root", str(self.root), "--git-fast")["drift"]
        self.assertEqual([x["path"] for x in d["modified"]], ["pkg/a.py"])
        self.assertEqual([x["path"] for x in d["added"]], ["pkg/c.py"])

        # Enable directory sharding and confirm auto-sync on upsert + cleanup
        code, out = cli("shard", "--root", str(self.root), "--depth", "1")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / ".aoci" / "shards" / "pkg.code.txt").exists())
        self.assertTrue((self.root / ".aoci" / "shards" / "_root.code.txt").exists())
        self.upsert(
            "pkg/a.py[DX5T]: F:defines updated package constant a | R:- | A:- | S:-",
            "pkg/c.py[DX3T]: F:defines package constant c | R:- | A:- | S:-",
        )
        shard_txt = (self.root / ".aoci" / "shards" / "pkg.code.txt").read_text(encoding="utf-8")
        self.assertIn("pkg/c.py[DX3T]", shard_txt)
        self.assertEqual(cli("shard", "--root", str(self.root), "--clean")[0], 0)
        self.assertFalse((self.root / ".aoci" / "shards").exists())


class AckTests(RepoTestCase):
    def test_ack_rebaselines_and_refreshes_size_tier(self):
        self.write("svc.py", "def run():\n    pass\n")
        self.init()
        self.upsert("svc.py[AX5T]: F:runs the background service loop | R:- | A:run | S:-")
        self.write("svc.py", "def run():\n" + "    x = 1\n" * 60)
        code, out = cli("verify", "--root", str(self.root))
        self.assertEqual(code, 1)
        code, out = cli("ack", "--root", str(self.root), "svc.py")
        self.assertEqual(code, 0, out)
        self.assertIn("svc.py T->S", out)
        self.assertIn("svc.py[AX5S]", self.code_text())
        code, out = cli("verify", "--root", str(self.root))
        self.assertEqual(code, 0, out)

    def test_ack_all_modified_requires_reviewed_flag(self):
        self.write("svc.py", "x = 1\n")
        self.init()
        self.upsert("svc.py[AX5T]: F:runs the background service loop | R:- | A:- | S:-")
        self.write("svc.py", "x = 2\n")
        code, out = cli("ack", "--root", str(self.root), "--all-modified")
        self.assertEqual(code, 2)
        self.assertIn("--reviewed", out)
        code, out = cli("ack", "--root", str(self.root), "--all-modified", "--reviewed")
        self.assertEqual(code, 0, out)
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 0)

    def test_ack_refuses_unindexed_file(self):
        self.write("new.py", "z = 1\n")
        self.init()
        code, out = cli("ack", "--root", str(self.root), "new.py")
        self.assertEqual(code, 1)
        self.assertIn("no entry", out)


class GraphTests(RepoTestCase):
    def _setup(self):
        for rel in ("src/env.py", "src/jwt.py", "src/routes.py", "pyproject.toml"):
            self.write(rel, f"# {rel}\n")
        self.init()
        self.upsert(
            "src/env.py[CG7T]: F:loads environment settings | R:pyproject.toml | A:get_settings<-src/jwt.py | S:-",
            "src/jwt.py[MA8T]: F:issues signed access tokens | R:- | A:issue_token | S:alg allowlist",
            "src/routes.py[EA6T]: F:exposes HTTP token endpoints | R:src/jwt.py | A:router | S:-",
            "pyproject.toml[RG3T]: F:declares package metadata and deps | R:- | A:- | S:-",
        )

    def test_context_respects_A_caller_direction(self):
        self._setup()
        data = cli_json("context", "--root", str(self.root), "src/jwt.py")
        deps = " ".join(data["dependencies"])
        callers = " ".join(data["callers"])
        self.assertIn("src/env.py[", deps)          # jwt calls env.get_settings
        self.assertNotIn("src/env.py[", callers)
        self.assertIn("src/routes.py[", callers)    # routes reads jwt

    def test_root_level_files_resolved_and_depth(self):
        self._setup()
        data = cli_json("context", "--root", str(self.root), "src/jwt.py", "--depth", "2")
        deps = " ".join(data["dependencies"])
        self.assertIn("pyproject.toml[", deps)

    def test_query_impact_lists_dependents_only(self):
        self._setup()
        entries = " ".join(cli_json("query", "--root", str(self.root), "--impact", "src/jwt.py")["entries"])
        self.assertIn("src/routes.py[", entries)
        self.assertIn("src/jwt.py[", entries)
        self.assertNotIn("src/env.py[", entries)


class LintTests(RepoTestCase):
    def test_lint_flags_vague_hub_and_dangling(self):
        self.write("core.py", "x = 1\n")
        self.write("util.py", "y = 2\n")
        self.init()
        self.upsert(
            "core.py[KX9T]: F:core logic helpers | R:src/gone.py | A:- | S:-",
            "util.py[CX3T]: F:formats ISO dates for log lines | R:- | A:fmt | S:-",
        )
        codes = {(f["path"], f["code"]) for f in cli_json("lint", "--root", str(self.root))["findings"]}
        self.assertIn(("core.py", "F-vague"), codes)
        self.assertIn(("core.py", "S-empty-hub"), codes)
        self.assertIn(("core.py", "ref-dangling"), codes)
        self.assertNotIn(("util.py", "F-vague"), codes)
        code, _ = cli("lint", "--root", str(self.root), "--strict")
        self.assertEqual(code, 1)

    def test_lint_cjk_responsibility_and_vague_detection(self):
        for rel in ("jwt.py", "mix.py", "short.py", "vague.py"):
            self.write(rel, "x = 1\n")
        self.init()
        self.upsert(
            "jwt.py[MA8T]: F:签发并校验支持多密钥轮换的访问令牌 | R:- | A:sign | S:强制白名单算法并拒绝none",
            "mix.py[CX3T]: F:签发JWT令牌 | R:- | A:issue | S:-",
            "short.py[CX2T]: F:鉴权工具 | R:- | A:- | S:-",
            "vague.py[CX3T]: F:核心逻辑辅助函数 | R:- | A:- | S:-",
        )
        codes = {(f["path"], f["code"]) for f in cli_json("lint", "--root", str(self.root))["findings"]}
        self.assertNotIn(("jwt.py", "F-too-short"), codes)
        self.assertNotIn(("jwt.py", "F-vague"), codes)
        self.assertNotIn(("mix.py", "F-too-short"), codes)
        self.assertIn(("short.py", "F-too-short"), codes)
        self.assertIn(("vague.py", "F-vague"), codes)


class OutlineTests(RepoTestCase):
    def test_js_ts_multiline_imports_reexports_and_comments(self):
        ts_src = (
            "// import ghost from './ghost'\n"
            "/*\n"
            "import blockGhost from './block-ghost'\n"
            "export function commentedOut() {}\n"
            "*/\n"
            "import {\n"
            "  signToken,\n"
            "  type JwtPayload,\n"
            "} from '../auth/jwt'\n"
            "import defaultCfg, {\n"
            "  loadEnv,\n"
            "} from './config/env'\n"
            "export {\n"
            "  createOrder,\n"
            "  cancelOrder as revokeOrder,\n"
            "} from './order/service'\n"
            "const legacy = require('./legacy/cjs')\n"
            "export async function bootApp() {\n"
            "  return import('./lazy/panel')\n"
            "}\n"
        )
        self.write("src/index.ts", ts_src)
        data = cli_json("outline", "--root", str(self.root), "src/index.ts")
        item = data["outlines"][0]
        self.assertEqual(
            item["imports"],
            ["../auth/jwt", "./config/env", "./order/service", "./legacy/cjs", "./lazy/panel"],
        )
        syms = " | ".join(item["symbols"])
        self.assertIn("export createOrder [L13]", syms)
        self.assertIn("export revokeOrder [L13]", syms)
        self.assertIn("function bootApp [L18]", syms)
        self.assertNotIn("commentedOut", syms)

    def test_vue_and_svelte_sfc_outline_preserves_line_numbers(self):
        vue_src = (
            "<template>\n"
            "  <div>{{ count }}</div>\n"
            "</template>\n"
            "<script setup lang=\"ts\">\n"
            "import {\n"
            "  ref,\n"
            "} from 'vue'\n"
            "export function useCounter() {\n"
            "  return ref(0)\n"
            "}\n"
            "</script>\n"
        )
        self.write("src/Counter.vue", vue_src)
        item = cli_json("outline", "--root", str(self.root), "src/Counter.vue")["outlines"][0]
        self.assertEqual(item["imports"], ["vue"])
        self.assertIn("function useCounter [L8]", item["symbols"])


class HookSessionTests(RepoTestCase):
    def test_stop_ignores_preexisting_drift_but_flags_new_edits(self):
        self.write("a.py", "a = 1\n")
        self.write("b.py", "b = 1\n")
        self.init()
        self.upsert(
            "a.py[CX2T]: F:defines constant a value | R:- | A:- | S:-",
            "b.py[CX2T]: F:defines constant b value | R:- | A:- | S:-",
        )
        self.write("a.py", "a = 2\n")  # user's own pending edit before the session
        base = {"cwd": str(self.root), "session_id": "sess-1"}
        start = hook("session-start", dict(base, hook_event_name="SessionStart", source="startup"))
        self.assertIn("additionalContext", start["hookSpecificOutput"])
        self.assertTrue((self.root / ".aoci" / ".sessions" / "sess-1.json").exists())

        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(stop, {})

        self.write("b.py", "b = 2\n")  # agent edit during the session
        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(stop.get("decision"), "block")
        self.assertIn("b.py", stop["reason"])
        self.assertNotIn("modified(2)", stop["reason"])

        self.write("a.py", "a = 3\n")  # agent now also touched the pre-existing file
        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertIn("modified(2)", stop["reason"])

    def test_antigravity_contract_and_fully_idle(self):
        self.write("a.py", "a = 1\n")
        self.init()
        payload = {"cwd": str(self.root), "conversationId": "c1", "hookEventName": "Stop",
                   "terminationReason": "NO_TOOL_CALL", "continuationActive": False}
        out = hook("stop", payload, host="antigravity")
        self.assertEqual(out.get("decision"), "continue")
        self.assertEqual(hook("stop", dict(payload, fullyIdle=False), host="antigravity"), {})
        start = hook("session-start", {"cwd": str(self.root), "conversationId": "c2",
                                       "hookEventName": "SessionStart"}, host="antigravity")
        self.assertIn("systemMessage", start["injectSteps"][0])

    def test_stop_detects_replaced_malformed_line_and_retires_resolved_drift(self):
        self.write("a.py", "a = 1\n")
        self.init()
        self.upsert("a.py[CX2T]: F:defines constant a value | R:- | A:- | S:-")
        code_file = self.root / ".aoci" / "aoci.code.txt"
        code_file.write_text(code_file.read_text(encoding="utf-8") + "old broken line\n", encoding="utf-8")

        base = {"cwd": str(self.root), "session_id": "sess-mal"}
        hook("session-start", dict(base, hook_event_name="SessionStart"))
        self.assertEqual(hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False)), {})

        # Replacing the old malformed line with a DIFFERENT malformed line keeps count=1, but must be caught
        code_file.write_text(
            code_file.read_text(encoding="utf-8").replace("old broken line", "new broken line"),
            encoding="utf-8",
        )
        stop = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(stop.get("decision"), "block")
        self.assertIn("malformed_lines(1)", stop["reason"])

    def test_turn_start_exempts_mid_session_user_edits_without_hiding_agent_edits(self):
        self.write("user_edit.py", "u = 1\n")
        self.write("agent_edit.py", "g = 1\n")
        self.init()
        self.upsert(
            "user_edit.py[CX2T]: F:holds user edited constant | R:- | A:- | S:-",
            "agent_edit.py[CX2T]: F:holds agent edited constant | R:- | A:- | S:-",
        )
        base = {"cwd": str(self.root), "conversationId": "conv-turn"}
        hook("session-start", dict(base, hookEventName="SessionStart"), host="antigravity")
        self.assertEqual(
            hook("stop", dict(base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False), host="antigravity"),
            {},
        )

        # User edits `user_edit.py` in IDE between Turn 1 and Turn 2
        self.write("user_edit.py", "u = 2\n")
        # Turn 2 starts and is CANCELED mid-turn (no Stop runs!) — must not leave any stuck flag
        self.assertEqual(
            hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=1), host="antigravity"),
            {},
        )
        # User edits `user_edit.py` again before Turn 3; `invocationNum=1` still refreshes statelessly
        self.write("user_edit.py", "u = 3\n")
        self.assertEqual(
            hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=1), host="antigravity"),
            {},
        )
        self.assertEqual(
            hook("stop", dict(base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False), host="antigravity"),
            {},
        )

        # Turn 4: `invocationNum=1` starts turn, agent edits `agent_edit.py`, `invocationNum=2` must NOT absorb it
        hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=1), host="antigravity")
        self.write("agent_edit.py", "g = 2\n")
        hook("auto", dict(base, hookEventName="PreInvocation", invocationNum=2), host="antigravity")
        stop = hook(
            "stop",
            dict(base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False),
            host="antigravity",
        )
        self.assertEqual(stop.get("decision"), "continue")
        self.assertIn("agent_edit.py", stop["reason"])
        self.assertNotIn("modified(2)", stop["reason"])

        # Claude Code `UserPromptSubmit` refreshes unconditionally without any invocationNum
        cc_base = {"cwd": str(self.root), "session_id": "cc-1"}
        self.write("user_edit.py", "u = 4\n")
        self.assertEqual(hook("auto", dict(cc_base, hook_event_name="UserPromptSubmit"), host="claude-code"), {})
        self.upsert("agent_edit.py[CX2T]: F:holds updated agent edited constant | R:- | A:- | S:-")
        self.assertEqual(hook("stop", dict(cc_base, hook_event_name="Stop", stop_hook_active=False), host="claude-code"), {})

    def test_hook_skips_git_subprocess_when_no_aoci_dir(self):
        import aoci_hook

        with tempfile.TemporaryDirectory() as plain_dir:
            with mock.patch.object(aoci_index, "detect_git_info", side_effect=AssertionError("git must not be called")):
                self.assertEqual(aoci_hook.discover_indexed_roots({"cwd": plain_dir}), [])


class InstallAndNonGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AOCI_TEST_TMP"))
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_install(self, *args: str):
        r = subprocess.run(
            [sys.executable, str(INSTALL), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return r.returncode, r.stdout + r.stderr

    def test_install_manifest_guard_and_clean_uninstall(self):
        home = self.tmp / "home"
        proj = self.tmp / "proj"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo keep"}]}]}}),
            encoding="utf-8",
        )
        proj.mkdir()

        code, out = self.run_install(
            "--host", "both", "--scope", "global", "--home-dir", str(home), "--with-hooks", "--with-rules"
        )
        self.assertEqual(code, 0, out)
        claude_skill = home / ".claude" / "skills" / "aoci-index" / "SKILL.md"
        agy_hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))
        self.assertTrue(claude_skill.exists())
        self.assertIn("aoci-index-guard", agy_hooks)

        # Locally editing an installed file must block overwrite unless --force is passed
        claude_skill.write_text(claude_skill.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
        code, out = self.run_install("--host", "claude-code", "--scope", "global", "--home-dir", str(home))
        self.assertEqual(code, 1)
        self.assertIn("--force", out)
        code, out = self.run_install("--host", "claude-code", "--scope", "global", "--home-dir", str(home), "--force")
        self.assertEqual(code, 0, out)

        # Uninstall cleans skill, aoci hooks, and rule snippets while keeping unrelated user hooks
        code, out = self.run_install("--host", "both", "--scope", "global", "--home-dir", str(home), "--uninstall")
        self.assertEqual(code, 0, out)
        self.assertFalse(claude_skill.parent.exists())
        remaining = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(remaining["hooks"]["Stop"]), 1)
        self.assertIn("echo keep", remaining["hooks"]["Stop"][0]["hooks"][0]["command"])

        # Project-scoped Antigravity rule snippet round-trip
        code, out = self.run_install(
            "--host", "antigravity", "--scope", "project", "--project-root", str(proj), "--with-rules"
        )
        self.assertEqual(code, 0, out)
        self.assertIn("overview", (proj / "AGENTS.md").read_text(encoding="utf-8"))

    def test_install_skips_vcs_dirs_and_removes_readonly_files(self):
        home = self.tmp / "home"
        proj = self.tmp / "proj"
        home.mkdir()
        proj.mkdir()
        ro_paths = []

        def _make_ro(path: Path, data: str = "locked\n") -> Path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
            os.chmod(path, 0o444)
            ro_paths.append(path)
            return path

        try:
            # 1) Claude Code install must never copy `.git/` or cache/IDE/venv dirs
            code, out = self.run_install(
                "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home)
            )
            self.assertEqual(code, 0, out)
            cc_skill = proj / ".claude" / "skills" / "aoci-index"
            self.assertTrue((cc_skill / "SKILL.md").is_file())
            for skipped in (".git", ".hg", ".svn", ".aoci", ".venv", "venv", "__pycache__", "tests", ".idea"):
                self.assertFalse((cc_skill / skipped).exists(), f"{skipped} must not be copied to {cc_skill}")

            # `.bak` / `.tmp` / `.git/` files inside dest_dir are ignored by `_tree_hashes`
            _make_ro(cc_skill / "scratch.bak")
            _make_ro(cc_skill / "scratch.tmp")
            ro_cc_nested = _make_ro(cc_skill / ".git" / "objects" / "pack" / "pack-test.pack")

            # Read-only files (0o444) inside an existing Claude Code install must not fail --force reinstall or --uninstall
            ro_cc_top = _make_ro(cc_skill / "SKILL.md", "# locked\n")
            code, out = self.run_install(
                "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home), "--force"
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(ro_cc_nested.exists())
            self.assertFalse((cc_skill / ".git").exists())
            self.assertFalse((cc_skill / "scratch.bak").exists())
            self.assertTrue(ro_cc_top.is_file())

            _make_ro(cc_skill / "ro_uninstall.txt")
            code, out = self.run_install(
                "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home), "--uninstall"
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(cc_skill.exists())

            # 2) Antigravity `--as-plugin` install must also skip `.git/` and handle 0o444 files on --force and --uninstall
            code, out = self.run_install(
                "--host",
                "antigravity",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--as-plugin",
                "--with-hooks",
                "--with-rules",
                "--with-mcp",
            )
            self.assertEqual(code, 0, out)
            plugin_dir = proj / ".agents" / "plugins" / "aoci-index"
            plugin_skill = plugin_dir / "skills" / "aoci-index"
            self.assertTrue((plugin_skill / "SKILL.md").is_file())
            self.assertFalse((plugin_dir / ".git").exists())
            for skipped in (".git", ".hg", ".svn", ".aoci", ".venv", "venv", "__pycache__", "tests", ".idea"):
                self.assertFalse((plugin_skill / skipped).exists(), f"{skipped} must not be copied to {plugin_skill}")

            ro_plug_nested = _make_ro(plugin_skill / ".git" / "objects" / "aa" / "bbcc")
            _make_ro(plugin_skill / "SKILL.md", "# locked plugin\n")
            code, out = self.run_install(
                "--host",
                "antigravity",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--as-plugin",
                "--force",
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(ro_plug_nested.exists())
            self.assertFalse((plugin_skill / ".git").exists())
            self.assertTrue((plugin_skill / "SKILL.md").is_file())

            _make_ro(plugin_skill / "ro_plugin_uninstall.txt")
            code, out = self.run_install(
                "--host",
                "antigravity",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--as-plugin",
                "--uninstall",
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(plugin_dir.exists())
        finally:
            for target in ro_paths:
                if target.exists():
                    try:
                        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass

    def test_non_git_workspace_honors_root_gitignore(self):
        ws = self.tmp / "nongit"
        ws.mkdir()
        (ws / ".gitignore").write_text("secret.env\ngen_out/\n", encoding="utf-8")
        (ws / "secret.env").write_text("TOKEN=1\n", encoding="utf-8")
        (ws / "gen_out").mkdir()
        (ws / "gen_out" / "x.py").write_text("x = 1\n", encoding="utf-8")
        (ws / "app.py").write_text("y = 2\n", encoding="utf-8")

        code, out = cli("init", "--root", str(ws))
        self.assertEqual(code, 0, out)
        scan = cli_json("scan", "--root", str(ws))
        added_paths = [it["path"] for it in scan["drift"]["added"]]
        self.assertIn("app.py", added_paths)
        self.assertNotIn("secret.env", added_paths)
        self.assertNotIn("gen_out/x.py", added_paths)


class IgnoreMatcherTests(unittest.TestCase):
    def test_gitignore_semantics(self):
        m = aoci_index.IgnoreMatcher(["generated/", "*.pb.go", "!keep.pb.go", "/rootonly.txt", "docs/**/draft*"])
        self.assertTrue(m.is_ignored("a/generated/x.py"))
        self.assertTrue(m.is_ignored("api/foo.pb.go"))
        self.assertFalse(m.is_ignored("api/keep.pb.go"))
        self.assertTrue(m.is_ignored("rootonly.txt"))
        self.assertFalse(m.is_ignored("sub/rootonly.txt"))
        self.assertTrue(m.is_ignored("docs/a/b/draft1.md"))
        self.assertFalse(m.is_ignored("docs/a/final.md"))
        self.assertTrue(m.is_ignored("generated", is_dir=True))

    def test_cannot_reinclude_inside_ignored_dir(self):
        m = aoci_index.IgnoreMatcher(["vendor/", "!vendor/keep.go"])
        self.assertTrue(m.is_ignored("vendor/keep.go"))


class PortabilityAndPython38Tests(unittest.TestCase):
    def test_python38_ast_and_stdlib_compatibility(self):
        py_files = sorted((SKILL_DIR / "scripts").rglob("*.py")) + sorted((SKILL_DIR / "tests").rglob("*.py"))
        banned_39_methods = {
            "removeprefix",
            "removesuffix",
            "is_relative_to",
            "readlink",
            "with_stem",
            "hardlink_to",
            "bit_count",
        }
        banned_method_kwargs = {
            "write_text": {"newline"},  # Python 3.10+
            "glob": {"root_dir", "dir_fd", "case_sensitive"},  # Python 3.10+/3.12+
            "rglob": {"case_sensitive"},  # Python 3.12+
            "relative_to": {"walk_up"},  # Python 3.12+
            "TemporaryDirectory": {"ignore_cleanup_errors"},  # Python 3.10+
            "zip": {"strict"},  # Python 3.10+
        }
        for py_path in py_files:
            src = py_path.read_text(encoding="utf-8")
            # Enforces Python 3.8 grammar (rejects match/case, parenthesized with-items, etc.)
            tree = ast.parse(src, filename=str(py_path), feature_version=(3, 8))
            has_future_annotations = any(
                isinstance(n, ast.ImportFrom)
                and n.module == "__future__"
                and any(a.name == "annotations" for a in n.names)
                for n in tree.body[:5]
            )
            self.assertTrue(has_future_annotations, f"{py_path.name} must import __future__.annotations for Python 3.8")
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in banned_39_methods:
                    self.fail(f"{py_path.name}:{node.lineno} uses Python 3.9+ method .{node.attr}()")
                if isinstance(node, ast.Call):
                    fn_name = (
                        node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else (node.func.id if isinstance(node.func, ast.Name) else "")
                    )
                    bad_kw = banned_method_kwargs.get(fn_name)
                    if bad_kw:
                        used_kw = {k.arg for k in node.keywords if k.arg} & bad_kw
                        if used_kw:
                            self.fail(
                                f"{py_path.name}:{node.lineno} passes Python 3.10+ kwargs {sorted(used_kw)} to {fn_name}()"
                            )

    @unittest.skipUnless(HAS_GIT, "git is required")
    def test_python38_strict_path_write_text_signature_end_to_end(self):
        import contextlib
        import io
        import install as install_mod

        orig_write_text = Path.write_text

        def py38_write_text(self_path: Path, data: str, encoding=None, errors=None) -> int:
            return orig_write_text(self_path, data, encoding=encoding, errors=errors)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo = tmp / "repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            with (repo / "app.py").open("w", encoding="utf-8", newline="\n") as fh:
                fh.write("x = 1\n")

            with mock.patch.object(Path, "write_text", py38_write_text):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        install_mod.main(
                            [
                                "--host",
                                "both",
                                "--scope",
                                "global",
                                "--home-dir",
                                str(home),
                                "--with-hooks",
                                "--with-rules",
                                "--with-mcp",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(aoci_index.main(["init", "--root", str(repo)]), 0)
                    self.assertEqual(
                        aoci_index.main(
                            [
                                "upsert",
                                "--root",
                                str(repo),
                                "--entry",
                                "app.py[CX3T]: F:defines module constant x | R:- | A:- | S:-",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(aoci_index.main(["verify", "--root", str(repo)]), 0)

    @unittest.skipUnless(HAS_GIT, "git is required")
    def test_case_sensitive_fs_rejects_wrong_case_in_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "Auth.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            git_info = aoci_index.detect_git_info(root)
            live = aoci_index.LiveFileSet(root, git_info, {}, aoci_index.DEFAULT_MAX_BYTES)
            # Simulate Linux case-sensitive filesystem where `src/auth.py` does not exist on disk
            orig_exists = Path.exists

            def fake_exists(p: Path) -> bool:
                if p.name == "auth.py":
                    return False
                return orig_exists(p)

            with mock.patch.object(Path, "exists", fake_exists):
                self.assertIsNone(live.canonical("src/auth.py"))
                self.assertEqual(live.canonical("src/Auth.py"), "src/Auth.py")


class P0RegressionTests(unittest.TestCase):
    def test_cjk_domains_and_multiline_invariants(self):
        with tempfile.TemporaryDirectory() as td:
            aoci_dir = Path(td) / ".aoci"
            aoci_dir.mkdir()
            (aoci_dir / "aoci.meta.txt").write_text(
                "# D (Domain):\n#   G=全局  X=核心\n#   A=认证  O=订单-履约\n#   （示例：Z=示例模块）\n",
                encoding="utf-8",
            )
            domains = aoci_index.parse_declared_domains(aoci_dir)
            self.assertEqual(domains, {"G": "全局", "X": "核心", "A": "认证", "O": "订单-履约"})

            (aoci_dir / "aoci.txt").write_text(
                "#AOCI-ROOT-MANIFEST: 1\n"
                "#Global-Invariants:\n"
                "#  - 写接口必须经过 JWT 鉴权\n"
                "#  - 金额统一使用 int64 分\n"
                "#Volume: id=meta kind=meta path=aoci.meta.txt format=meta-v1 depends=- state=enabled\n",
                encoding="utf-8",
            )
            inv = aoci_index.parse_manifest_field(aoci_dir, "#Global-Invariants:")
            self.assertEqual(inv, "写接口必须经过 JWT 鉴权; 金额统一使用 int64 分")

    def test_dynamic_frontend_route_refs_and_root_brackets(self):
        entries = {
            "[slug].tsx": (
                "[slug].tsx[UG5T]: F:renders root catch-all slug page | "
                "R:app/(auth)/[id]/page.tsx,src/routes/+page.svelte | A:Page<-app/routes/orders.$id.tsx | S:-"
            ),
            "app/(auth)/[id]/page.tsx": (
                "app/(auth)/[id]/page.tsx[UA6T]: F:renders authenticated user detail page | "
                "R:app/@modal/default.tsx | A:UserPage | S:-"
            ),
            "src/routes/+page.svelte": (
                "src/routes/+page.svelte[UG4T]: F:renders SvelteKit landing page | R:- | A:load | S:-"
            ),
            "app/routes/orders.$id.tsx": (
                "app/routes/orders.$id.tsx[UO6T]: F:renders Remix order detail route | R:- | A:loader | S:-"
            ),
            "app/@modal/default.tsx": (
                "app/@modal/default.tsx[UG3T]: F:provides parallel route fallback modal | R:- | A:DefaultModal | S:-"
            ),
        }
        deps, dependents = aoci_index.build_fras_graph(entries)
        self.assertEqual(
            deps["[slug].tsx"],
            {"app/(auth)/[id]/page.tsx", "src/routes/+page.svelte"},
        )
        self.assertEqual(dependents["[slug].tsx"], {"app/routes/orders.$id.tsx"})
        self.assertEqual(deps["app/(auth)/[id]/page.tsx"], {"app/@modal/default.tsx"})

    def test_chain_and_swap_rename_rewriting_is_atomic(self):
        self.assertEqual(
            aoci_index.rewrite_path_refs("a.py,b.py", {"a.py": "b.py", "b.py": "c.py"}),
            "b.py,c.py",
        )
        self.assertEqual(
            aoci_index.rewrite_path_refs(
                "load<-app/(old)/[id]/page.tsx,src/b.ts",
                {"app/(old)/[id]/page.tsx": "app/(new)/[id]/page.tsx", "src/b.ts": "app/(old)/[id]/page.tsx"},
            ),
            "load<-app/(new)/[id]/page.tsx,app/(old)/[id]/page.tsx",
        )

    def test_dangling_refs_catches_root_files_and_ignores_protocols_and_examples_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "keep.py").write_text("x = 1\n", encoding="utf-8")
            entries = {
                "keep.py": (
                    "keep.py[CG5T]: F:speaks HTTP protocol with upstream | "
                    "R:HTTP/1.1,TLS/1.3,gone_root.py | "
                    "A:OrderService.create/OrderService.cancel<-missing_cli.py | S:-"
                ),
            }
            dangling = aoci_index.find_dangling_refs(root, entries)
            self.assertEqual(dangling, {"keep.py": ["gone_root.py", "missing_cli.py"]})

        # Bundled examples/ must be self-contained with 0 lint findings when backed by matching files
        with tempfile.TemporaryDirectory() as td:
            ex_root = Path(td)
            ex_aoci = ex_root / ".aoci"
            shutil.copytree(SKILL_DIR / "examples", ex_aoci)
            ex_entries, malformed = aoci_index.parse_code_volume(ex_aoci / "aoci.code.txt")
            self.assertEqual(malformed, [])
            for rel in ex_entries:
                fp = ex_root / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text("# stub\n", encoding="utf-8")
            findings = aoci_index.lint_entries(ex_root, ex_entries, aoci_index.parse_declared_domains(ex_aoci))
            self.assertEqual(findings, [])

    def test_outline_regex_literals_local_exports_py_relative_rust_and_kotlin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ts_file = root / "mod.ts"
            ts_file.write_text(
                "const PROTO_RE = /^https?:\\/\\//; // trailing comment\n"
                "const CHAR_CLASS_RE = /[/*]/;\n"
                "function internalOrder() { return 1 }\n"
                "export { internalOrder as publicOrder }\n",
                encoding="utf-8",
            )
            ts_out = aoci_index.extract_file_outline(ts_file, "mod.ts")
            self.assertIn("function internalOrder [L3]", ts_out["symbols"])
            self.assertIn("export publicOrder [L4]", ts_out["symbols"])

            py_file = root / "routes.py"
            py_file.write_text(
                "from . import auth, db\n"
                "@router.post('/orders')\n"
                "async def create_order(req):\n"
                "    return req\n",
                encoding="utf-8",
            )
            py_out = aoci_index.extract_file_outline(py_file, "routes.py")
            self.assertEqual(py_out["imports"], [".auth", ".db"])
            self.assertIn("async def create_order(req) [@post('/orders')] [L3]", py_out["symbols"])

            rs_file = root / "lib.rs"
            rs_file.write_text("use crate::auth::{\n    sign,\n    verify,\n};\npub fn run() {}\n", encoding="utf-8")
            rs_out = aoci_index.extract_file_outline(rs_file, "lib.rs")
            self.assertEqual(rs_out["imports"], ["crate::auth::{ sign, verify, }"])
            self.assertIn("fn run [L5]", rs_out["symbols"])

            kt_file = root / "App.kt"
            kt_file.write_text("import java.time.Instant\nsuspend fun bootServer() {}\n", encoding="utf-8")
            kt_out = aoci_index.extract_file_outline(kt_file, "App.kt")
            self.assertEqual(kt_out["imports"], ["import java.time.Instant"])
            self.assertIn("fun bootServer [L2]", kt_out["symbols"])

    @unittest.skipUnless(HAS_GIT, "git is required")
    def test_turn_hook_wiring_and_cross_platform_claude_project_runner(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            proj = tmp / "space in project"
            proj.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
            (proj / "app.py").write_text("x = 1\n", encoding="utf-8")
            age_file(proj / "app.py")
            cli("init", "--root", str(proj))
            cli("upsert", "--root", str(proj), "--entry", "app.py[CX2T]: F:defines app constant x | R:- | A:- | S:-")

            # Install project-scoped Claude Code hooks + Antigravity --with-turn-hook
            r = subprocess.run(
                [
                    sys.executable, str(INSTALL),
                    "--host", "both", "--scope", "project",
                    "--project-root", str(proj), "--home-dir", str(home),
                    "--python-cmd", f'"{sys.executable}"' if " " in sys.executable else sys.executable,
                    "--with-hooks", "--with-turn-hook",
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            cc_settings = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertIn("UserPromptSubmit", cc_settings["hooks"])
            agy_hooks = json.loads((proj / ".agents" / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("PreInvocation", agy_hooks["aoci-index-guard"])

            # Execute the generated Claude Code project SessionStart command through the native OS shell
            # from an unrelated working directory with CLAUDE_PROJECT_DIR containing spaces!
            cc_start_cmd = cc_settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj))
            shell_proc = subprocess.run(
                cc_start_cmd,
                shell=True,
                cwd=str(tmp),
                env=env,
                input=json.dumps({"cwd": str(proj), "session_id": "sp1", "hook_event_name": "SessionStart"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(shell_proc.returncode, 0, shell_proc.stderr)
            parsed = json.loads(shell_proc.stdout)
            ctx_msg = parsed["hookSpecificOutput"]["additionalContext"]
            self.assertIn("aoci_index.py", ctx_msg)
            self.assertIn(".claude/skills/aoci-index/scripts/aoci_index.py", ctx_msg)

            # `--batch-size` alias works on `scan`
            (proj / "b.py").write_text("b = 2\n", encoding="utf-8")
            scan_data = cli_json("scan", "--root", str(proj), "--batch-size", "5")
            self.assertEqual(len(scan_data["batches"]), 1)

            # Clean uninstall removes UserPromptSubmit as well
            r_un = subprocess.run(
                [
                    sys.executable, str(INSTALL),
                    "--host", "both", "--scope", "project",
                    "--project-root", str(proj), "--home-dir", str(home),
                    "--uninstall",
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
            cc_after = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertNotIn("hooks", cc_after)


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase2PerformanceTests(RepoTestCase):
    def test_detect_git_info_pure_python_standard_and_worktree_zero_subprocesses(self):
        sub = self.root / "nested" / "pkg"
        sub.mkdir(parents=True)
        with mock.patch.object(aoci_index, "run_git", side_effect=AssertionError("git must not be spawned")):
            info = aoci_index.detect_git_info(sub)
        self.assertTrue(info["is_git"])
        self.assertEqual(info["toplevel"], self.root.resolve())
        self.assertEqual(info["exclude_file"], (self.root / ".git" / "info" / "exclude").resolve())
        self.assertEqual(info["index_file"], (self.root / ".git" / "index").resolve())

        # Create a commit and a secondary Git worktree (`commondir` layout)
        self.write("seed.py", "s = 1\n")
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", "add", "seed.py"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"], cwd=self.root, check=True)
        wt_dir = Path(self._tmp.name) / "wt2"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt_dir)], cwd=self.root, check=True)
        with mock.patch.object(aoci_index, "run_git", side_effect=AssertionError("git must not be spawned")):
            wt_info = aoci_index.detect_git_info(wt_dir)
        self.assertTrue(wt_info["is_git"])
        self.assertEqual(wt_info["toplevel"], wt_dir.resolve())
        self.assertEqual(wt_info["exclude_file"], (self.root / ".git" / "info" / "exclude").resolve())
        self.assertIn("worktrees", wt_info["index_file"].as_posix())

    def test_bulk_similar_renames_above_old_2500_cap(self):
        # 60 moved+edited files => 60 * 60 = 3,600 candidate pairs (exceeds the old 2,500 hard cliff)
        count = 60
        entries = []
        for i in range(count):
            lines = "".join(f"def mod_{i}_step_{k}(v):\n    return v + {i * 100 + k}\n" for k in range(8))
            self.write(f"old_pkg/m_{i:02d}.py", lines)
            entries.append(f"old_pkg/m_{i:02d}.py[DX5T]: F:executes domain module {i} steps | R:- | A:mod_{i}_step_0 | S:-")
        self.init()
        self.assertEqual(self.upsert(*entries)[0], 0)

        for i in range(count):
            (self.root / f"old_pkg/m_{i:02d}.py").unlink()
            lines = "".join(f"def mod_{i}_step_{k}(v):\n    return v + {i * 100 + k}\n" for k in range(7))
            lines += f"def mod_{i}_step_7(v):\n    return v + 999999\n"
            self.write(f"new_pkg/m_{i:02d}.py", lines)

        scan_data = cli_json("scan", "--root", str(self.root))
        self.assertEqual(len(scan_data["drift"]["similar_renames"]), count)
        code, out = cli("migrate-renames", "--root", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn(f"MigratedSimilar={count}", out)

    def test_incremental_git_discovery_cache_and_zero_git_ack(self):
        for name in ("a.py", "b.py", "c.py"):
            self.write(name, f"# {name}\nval = 1\n")
        subprocess.run(["git", "-c", "core.autocrlf=false", "add", "a.py", "b.py", "c.py"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"],
            cwd=self.root,
            check=True,
        )
        self.init()
        self.upsert(
            "a.py[CX2T]: F:defines module constant a | R:- | A:- | S:-",
            "b.py[CX2T]: F:defines module constant b | R:- | A:- | S:-",
            "c.py[CX2T]: F:defines module constant c | R:- | A:- | S:-",
        )
        # Populate `git_discovery.json` via --git-fast
        self.assertFalse(cli_json("scan", "--root", str(self.root), "--git-fast")["drift"]["has_drift"])
        disc_path = self.root / ".aoci" / ".cache" / "git_discovery.json"
        disc_before = json.loads(disc_path.read_text(encoding="utf-8"))
        self.assertEqual(disc_before["tracked_clean"], ["a.py", "b.py", "c.py"])

        # Modify `a.py` and run `ack a.py` IN-PROCESS with `run_git` forbidden (0 git subprocesses!)
        import contextlib
        import io

        self.write("a.py", "# a.py\nval = 2\n")
        with mock.patch.object(aoci_index, "run_git", side_effect=AssertionError("ack must not spawn git on warm tracked file")):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = aoci_index.main(["ack", "--root", str(self.root), "a.py"])
        self.assertEqual(rc, 0)

        # `git_discovery.json` must NOT be deleted: `b.py` and `c.py` stay in `tracked_clean`, only `a.py` demoted
        self.assertTrue(disc_path.exists())
        disc_after = json.loads(disc_path.read_text(encoding="utf-8"))
        self.assertEqual(disc_after["tracked_clean"], ["b.py", "c.py"])
        self.assertEqual(disc_after["tracked_recheck"], ["a.py"])
        self.assertFalse(cli_json("scan", "--root", str(self.root), "--git-fast")["drift"]["has_drift"])

    def test_incremental_shard_sync_rewrites_only_dirty_shard(self):
        self.write("pkg_a/x.py", "x = 1\n")
        self.write("pkg_b/y.py", "y = 2\n")
        self.init()
        self.upsert(
            "pkg_a/x.py[DX4T]: F:defines pkg_a constant x | R:- | A:- | S:-",
            "pkg_b/y.py[DX4T]: F:defines pkg_b constant y | R:- | A:- | S:-",
        )
        self.assertEqual(cli("shard", "--root", str(self.root), "--depth", "1")[0], 0)
        shard_a = self.root / ".aoci" / "shards" / "pkg_a.code.txt"
        shard_b = self.root / ".aoci" / "shards" / "pkg_b.code.txt"
        age_file(shard_a, seconds=120)
        age_file(shard_b, seconds=120)
        mtime_b_before = os.stat(shard_b).st_mtime_ns

        self.write("pkg_a/x.py", "x = 99\n")
        self.assertEqual(
            self.upsert("pkg_a/x.py[DX4T]: F:defines updated pkg_a constant x | R:- | A:- | S:-")[0],
            0,
        )
        self.assertEqual(os.stat(shard_b).st_mtime_ns, mtime_b_before)
        self.assertIn("updated pkg_a", shard_a.read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase3FeatureTests(RepoTestCase):
    def _git(self, *args: str, cwd: Path = None) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd or self.root,
            check=True,
        )

    def test_branch_switch_and_worktree_semantic_pool_restore(self):
        self.write("core.py", "c = 1\n")
        self._git("checkout", "-q", "-b", "main")
        self._git("add", "core.py")
        self._git("commit", "-q", "-m", "main base")

        self._git("checkout", "-q", "-b", "feature-x")
        self.write("feat_a.py", "def run_a():\n    return 1\n\nA = 10\n")
        self.write("feat_b.py", "def run_b():\n    return 2\n\nB = 20\n")
        self._git("add", "feat_a.py", "feat_b.py")
        self._git("commit", "-q", "-m", "feature files")

        self.init()
        self.assertEqual(
            self.upsert(
                "core.py[CX4T]: F:defines shared core constant c | R:- | A:- | S:-",
                "feat_a.py[AX6T]: F:runs feature workflow alpha | R:core.py | A:run_a | S:-",
                "feat_b.py[AX6T]: F:runs feature workflow beta | R:feat_a.py | A:run_b | S:-",
            )[0],
            0,
        )

        # Switch back to `main` and prune feature-x entries
        self._git("checkout", "-q", "main")
        self.assertEqual(cli("upsert", "--root", str(self.root), "--prune-only")[0], 0)
        self.assertNotIn("feat_a.py", self.code_text())

        # Switch back to `feature-x` and zero-token restore all feature entries
        self._git("checkout", "-q", "feature-x")
        res = cli_json("restore", "--root", str(self.root))
        self.assertEqual(res["restored"], ["feat_a.py", "feat_b.py"])
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 0)

        # Secondary Git worktree shares `<git-common-dir>/aoci-cache` automatically
        wt = Path(self._tmp.name) / "wt_feat"
        self._git("worktree", "add", "-q", "-b", "wt-copy", str(wt), "feature-x")
        self.assertEqual(cli("init", "--root", str(wt))[0], 0)
        scan_wt = cli_json("scan", "--root", str(wt), "--auto-restore")
        self.assertEqual(sorted(scan_wt["auto_restored"]), ["core.py", "feat_a.py", "feat_b.py"])
        self.assertFalse(scan_wt["drift"]["has_drift"])

    def test_bm25_search_and_multivalue_layer_domain_filters(self):
        for rel in ("jwt.py", "orders.py", "misc.py", "cn_auth.py"):
            self.write(rel, f"# {rel}\n")
        self.init()
        meta = self.root / ".aoci" / "aoci.meta.txt"
        meta.write_text(
            meta.read_text(encoding="utf-8") + "\n# D (Domain):\n#   G=Global X=Core A=Auth O=Order\n",
            encoding="utf-8",
        )
        self.upsert(
            "jwt.py[MA9T]: F:Issues and verifies JWT access tokens with multi-KID key rotation | R:- | A:signToken | S:reject alg none",
            "orders.py[EO7T]: F:Exposes HTTP order creation endpoints | R:jwt.py | A:createOrder | S:-",
            "misc.py[CX2T]: F:Formats log rotation timestamps | R:- | A:fmt | S:-",
            "cn_auth.py[MA8T]: F:签发并校验支持多密钥轮换的访问令牌 | R:- | A:verify | S:拒绝空算法",
        )
        # Multi-term non-adjacent BM25 query ("jwt rotation" is not a contiguous substring)
        q_en = cli_json("query", "--root", str(self.root), "--search", "jwt rotation", "--top", "2")
        self.assertGreaterEqual(q_en["count"], 1)
        self.assertEqual(q_en["scores"][0]["path"], "jwt.py")

        # CJK multi-term BM25 query
        q_cn = cli_json("query", "--root", str(self.root), "--search", "令牌 轮换")
        self.assertEqual(q_cn["scores"][0]["path"], "cn_auth.py")

        # Comma-separated Layer and Domain filters
        q_multi = cli_json("query", "--root", str(self.root), "--layer", "E,M", "--domain", "A,O")
        self.assertEqual(q_multi["count"], 3)

    def test_diff_impact_briefing_highlights_constraints_and_callers(self):
        self.write("src/env.py", "SECRET = 'x'\n")
        self.write("src/jwt.py", "from src import env\n")
        self.write("src/routes.py", "from src import jwt\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")

        self.init()
        self.upsert(
            "src/env.py[CG7T]: F:loads startup secrets | R:- | A:SECRET<-src/jwt.py | S:fail fast on missing secret",
            "src/jwt.py[MA8T]: F:verifies access tokens | R:src/env.py | A:verify<-src/routes.py | S:alg allowlist",
            "src/routes.py[EA6T]: F:mounts HTTP routes | R:src/jwt.py | A:router | S:-",
        )
        self.write("src/jwt.py", "from src import env\nSKEW = 30\n")
        self.write("src/new_helper.py", "h = 1\n")

        brief = cli_json("diff", "--root", str(self.root))
        self.assertEqual([c["path"] for c in brief["high_risk_constraints"]], ["src/jwt.py"])
        self.assertEqual(brief["unindexed_changed"], ["src/new_helper.py"])
        self.assertTrue(any(ln.startswith("src/routes.py[") for ln in brief["affected_neighbors"]))

        self._git("add", "src/jwt.py")
        self._git("commit", "-q", "-m", "update jwt")
        rev_brief = cli_json("diff", "HEAD~1..HEAD", "--root", str(self.root))
        self.assertEqual(len(rev_brief["changed_entries"]), 1)
        self.assertIn("src/jwt.py[", rev_brief["changed_entries"][0])

    def test_scaffold_resolves_tsconfig_aliases_and_blocks_unfilled_todo_upsert(self):
        self.write(
            "tsconfig.json",
            '{\n  // JSONC comment\n  "compilerOptions": {\n    "baseUrl": ".",\n    "paths": {"@/*": ["src/*"],}\n  }\n}\n',
        )
        self.write("src/auth/jwt.ts", "export function signToken(id: string) {\n  return id\n}\n")
        self.write(
            "src/routes/login.ts",
            "import { signToken } from '@/auth/jwt'\nexport function loginHandler() {\n  return signToken('u1')\n}\n",
        )
        self.init()

        scaf = cli_json("scaffold", "--root", str(self.root), "--unindexed")
        by_prefix = {ln.split("[", 1)[0]: ln for ln in scaf["entries"]}
        self.assertIn("R:src/auth/jwt.ts", by_prefix["src/routes/login.ts"])
        self.assertIn("A:signToken<-src/routes/login.ts", by_prefix["src/auth/jwt.ts"])

        # Attempting to upsert raw `<TODO:...>` without reading & authoring must fail Rule #4 validation
        code, out = self.upsert(by_prefix["src/auth/jwt.ts"])
        self.assertEqual(code, 1)
        self.assertIn("TODO", out)

    def test_mcp_stdio_server_and_plugin_bundle_install(self):
        import io
        import aoci_mcp

        self.write("src/app.py", "def serve():\n    return 200\n")
        self.init()
        self.upsert("src/app.py[EA7T]: F:serves primary HTTP application | R:- | A:serve | S:bind localhost only")

        # 1) Line-delimited JSON-RPC 2.0 session
        reqs = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "aoci_query", "arguments": {"root": str(self.root), "search": "HTTP serve"}},
                }
            ),
        ]
        in_buf = io.BytesIO(("\n".join(reqs) + "\n").encode("utf-8"))
        out_buf = io.BytesIO()
        aoci_mcp.serve_stdio(in_buf, out_buf)
        resps = [json.loads(ln.decode("utf-8")) for ln in out_buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual([r["id"] for r in resps], [1, 2, 3])
        self.assertGreaterEqual(len(resps[1]["result"]["tools"]), 9)
        q_payload = json.loads(resps[2]["result"]["content"][0]["text"])
        self.assertEqual(q_payload["scores"][0]["path"], "src/app.py")

        # 2) Content-Length framed JSON-RPC call + in-memory cache hit (zero parse_code_volume calls!)
        cl_body = json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "aoci_overview", "arguments": {"root": str(self.root)}}}
        ).encode("utf-8")
        cl_in = io.BytesIO(f"Content-Length: {len(cl_body)}\r\n\r\n".encode("ascii") + cl_body)
        cl_out = io.BytesIO()
        with mock.patch.object(aoci_index, "parse_code_volume", side_effect=AssertionError("must hit _CACHE")):
            aoci_mcp.serve_stdio(cl_in, cl_out)
        raw_reply = cl_out.getvalue()
        self.assertTrue(raw_reply.startswith(b"Content-Length:"))
        ov_resp = json.loads(raw_reply.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
        self.assertFalse(ov_resp["result"]["isError"])

        # 3) Native Antigravity `--as-plugin` bundle + `--with-mcp` install & clean uninstall
        home = Path(self._tmp.name) / "plugin_home"
        r_ins = subprocess.run(
            [
                sys.executable, str(INSTALL),
                "--host", "antigravity", "--scope", "global", "--home-dir", str(home),
                "--as-plugin", "--with-hooks", "--with-rules", "--with-mcp",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_ins.returncode, 0, r_ins.stdout + r_ins.stderr)
        pdir = home / ".gemini" / "config" / "plugins" / "aoci-index"
        self.assertTrue((pdir / "plugin.json").is_file())
        self.assertTrue((pdir / "assets" / "logo.svg").is_file())
        self.assertTrue((pdir / "hooks.json").is_file())
        self.assertTrue((pdir / "mcp_config.json").is_file())
        self.assertTrue((pdir / "rules" / "AGENTS.md").is_file())
        self.assertTrue((pdir / "skills" / "aoci-index" / "SKILL.md").is_file())

        r_un = subprocess.run(
            [
                sys.executable, str(INSTALL),
                "--host", "antigravity", "--scope", "global", "--home-dir", str(home),
                "--as-plugin", "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
        self.assertFalse(pdir.exists())


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditFixesRegressionTests(RepoTestCase):
    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            check=True,
        )

    def test_git_fast_detects_uncommitted_indexed_file_reverted_to_committed_version(self):
        # 1) Commit settings.py (v1) + clean.py
        self.write("settings.py", "DEBUG = False\n")
        self.write("clean.py", "OK = 1\n")
        self._git("add", "settings.py", "clean.py")
        self._git("commit", "-q", "-m", "committed v1")

        # 2) Edit settings.py in working tree (v2, uncommitted) and index v2 into AOCI
        self.write("settings.py", "DEBUG = True\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "settings.py[CG6T]: F:defines runtime debug flag | R:- | A:DEBUG | S:-",
                "clean.py[CG2T]: F:defines clean status constant | R:- | A:OK | S:-",
            )[0],
            0,
        )

        # 3) Build git_discovery.json via --git-fast: uncommitted settings.py MUST stay in tracked_recheck
        first_fast = cli_json("scan", "--root", str(self.root), "--git-fast")
        self.assertFalse(first_fast["drift"]["has_drift"])
        disc = json.loads((self.root / ".aoci" / ".cache" / "git_discovery.json").read_text(encoding="utf-8"))
        self.assertIn("clean.py", disc["tracked_clean"])
        self.assertNotIn("settings.py", disc["tracked_clean"])
        self.assertIn("settings.py", disc["tracked_recheck"])

        # 4) Revert settings.py in editor back to Git committed version (v1); even if timestamp matches!
        self.write("settings.py", "DEBUG = False\n")
        fast_scan = cli_json("scan", "--root", str(self.root), "--git-fast")["drift"]
        full_scan = cli_json("scan", "--root", str(self.root))["drift"]
        self.assertTrue(fast_scan["has_drift"])
        self.assertTrue(full_scan["has_drift"])
        self.assertEqual([m["path"] for m in fast_scan["modified"]], ["settings.py"])
        self.assertEqual([m["path"] for m in full_scan["modified"]], ["settings.py"])
        self.assertEqual(cli("verify", "--root", str(self.root), "--git-fast")[0], 1)
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 1)

    def test_migrate_renames_protects_malformed_lines_and_reports_lineno(self):
        body = "def run_job():\n    return 42\n\n\nFLAG = True\n"
        self.write("keep.py", "KEEP = 1\n")
        self.write("src/old_job.py", body)
        self.init()
        self.assertEqual(
            self.upsert(
                "keep.py[CX3T]: F:holds keep constant value | R:- | A:KEEP | S:-",
                "src/old_job.py[AX5T]: F:executes scheduled job worker | R:- | A:run_job | S:-",
            )[0],
            0,
        )

        # Introduce a syntax error in keep.py's line in aoci.code.txt, then rename src/old_job.py
        code_path = self.root / ".aoci" / "aoci.code.txt"
        corrupted = code_path.read_text(encoding="utf-8").replace(
            "keep.py[CX3T]: F:holds keep constant value | R:- | A:KEEP | S:-",
            "keep.py[CX3T] BROKEN_COLON F:holds keep constant value",
        )
        with code_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(corrupted)

        (self.root / "src/old_job.py").rename(self.root / "src/new_job.py")
        code, out = cli("migrate-renames", "--root", str(self.root))
        self.assertEqual(code, 1, out)
        self.assertIn("malformed", out)
        self.assertRegex(out, r"L\d+:.*keep\.py\[CX3T\] BROKEN_COLON")
        # Original file content (including broken keep.py line) MUST remain untouched
        self.assertIn("keep.py[CX3T] BROKEN_COLON", code_path.read_text(encoding="utf-8"))

        # Repair keep.py's line and confirm migrate-renames now succeeds keeping both entries
        repaired = code_path.read_text(encoding="utf-8").replace(
            "keep.py[CX3T] BROKEN_COLON F:holds keep constant value",
            "keep.py[CX3T]: F:holds keep constant value | R:- | A:KEEP | S:-",
        )
        with code_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(repaired)
        code2, out2 = cli("migrate-renames", "--root", str(self.root))
        self.assertEqual(code2, 0, out2)
        final_txt = code_path.read_text(encoding="utf-8")
        self.assertIn("keep.py[CX3T]:", final_txt)
        self.assertIn("src/new_job.py[AX5T]:", final_txt)

    def test_reinstall_and_uninstall_preserve_sibling_hooks_order_and_matcher_in_same_claude_group(self):
        home = Path(self._tmp.name) / "claude_home"
        r1 = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "global", "--home-dir", str(home), "--with-hooks"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)

        settings_path = home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        stop_group = settings["hooks"]["Stop"][0]
        aoci_handler = stop_group["hooks"][0]
        # Place custom commands BEFORE and AFTER AOCI in the SAME Stop group with a custom matcher
        stop_group["matcher"] = "CustomStopMatcher"
        stop_group["hooks"] = [
            {"type": "command", "command": "echo before-aoci"},
            aoci_handler,
            {"type": "command", "command": "echo after-aoci"},
        ]
        with settings_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(settings, indent=2) + "\n")

        # Re-install with --with-hooks: must replace ONLY the AOCI command in-place
        r2 = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "--host",
                "claude-code",
                "--scope",
                "global",
                "--home-dir",
                str(home),
                "--with-hooks",
                "--python-cmd",
                "python-custom",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        after_reinstall = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(len(after_reinstall["hooks"]["Stop"]), 1)
        grp_re = after_reinstall["hooks"]["Stop"][0]
        self.assertEqual(grp_re["matcher"], "CustomStopMatcher")
        cmds_re = [h["command"] for h in grp_re["hooks"]]
        self.assertEqual(len(cmds_re), 3)
        self.assertEqual(cmds_re[0], "echo before-aoci")
        self.assertIn("python-custom", cmds_re[1])
        self.assertIn("aoci_hook.py", cmds_re[1])
        self.assertEqual(cmds_re[2], "echo after-aoci")

        # Uninstall: must strip ONLY AOCI command while preserving sibling commands, order, and matcher
        r3 = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "global", "--home-dir", str(home), "--uninstall"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        after_un = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertNotIn("SessionStart", after_un["hooks"])
        self.assertNotIn("UserPromptSubmit", after_un["hooks"])
        self.assertEqual(len(after_un["hooks"]["Stop"]), 1)
        grp_un = after_un["hooks"]["Stop"][0]
        self.assertEqual(grp_un["matcher"], "CustomStopMatcher")
        self.assertEqual([h["command"] for h in grp_un["hooks"]], ["echo before-aoci", "echo after-aoci"])

    def test_cjk_paths_forward_reverse_context_and_distinct_shards(self):
        self.write("main.py", "print('boot')\n")
        self.write("模块/a.py", "def run_a():\n    return 1\n")
        self.write("服务/b.py", "def serve_b():\n    return 2\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "main.py[EG6T]: F:主入口编排核心模块与后端服务 | R:模块/a.py,服务/b.py | A:main | S:-",
                "模块/a.py[DX5T]: F:实现领域计算模块逻辑 | R:- | A:run_a | S:-",
                "服务/b.py[AX5T]: F:提供业务编排服务接口 | R:- | A:serve_b<-main.py | S:-",
            )[0],
            0,
        )

        # Forward query on main.py -> must return BOTH 模块/a.py and 服务/b.py
        ctx_main = cli_json("context", "--root", str(self.root), "main.py")
        dep_paths = [ln.split("[", 1)[0] for ln in ctx_main["dependencies"]]
        self.assertEqual(dep_paths, ["服务/b.py", "模块/a.py"])

        # Reverse queries on 模块/a.py and 服务/b.py -> both must trace back to main.py
        ctx_a = cli_json("context", "--root", str(self.root), "模块/a.py")
        self.assertEqual([ln.split("[", 1)[0] for ln in ctx_a["callers"]], ["main.py"])
        imp_b = cli_json("query", "--root", str(self.root), "--impact", "服务/b.py")
        imp_b_paths = sorted(ln.split("[", 1)[0] for ln in imp_b["entries"])
        self.assertEqual(imp_b_paths, ["main.py", "服务/b.py"])

        # Shards: `./`, `模块/`, and `服务/` must map to 3 distinct shard files (never colliding on `__`)
        shard_manifest = cli_json("shard", "--root", str(self.root), "--depth", "1")
        self.assertEqual(len(shard_manifest["shards"]), 3)
        by_prefix = {s["prefix"]: s for s in shard_manifest["shards"]}
        self.assertEqual(set(by_prefix.keys()), {"./", "模块/", "服务/"})
        shard_files = {s["file"] for s in shard_manifest["shards"]}
        self.assertEqual(len(shard_files), 3)
        mod_txt = (self.root / ".aoci" / by_prefix["模块/"]["file"]).read_text(encoding="utf-8")
        svc_txt = (self.root / ".aoci" / by_prefix["服务/"]["file"]).read_text(encoding="utf-8")
        self.assertIn("模块/a.py[DX5T]", mod_txt)
        self.assertNotIn("服务/b.py", mod_txt)
        self.assertIn("服务/b.py[AX5T]", svc_txt)
        self.assertNotIn("模块/a.py", svc_txt)

    def test_verify_and_lint_reject_invalid_layer_and_mismatched_size_tier(self):
        self.write("tiny.py", "ONE = 1\n")
        self.init()
        self.assertEqual(self.upsert("tiny.py[CX3T]: F:defines single line constant | R:- | A:ONE | S:-")[0], 0)

        # Hand-edit tiny.py (1 line) to illegal Layer `Z` and 1000+ line Size `H`
        code_path = self.root / ".aoci" / "aoci.code.txt"
        tampered = code_path.read_text(encoding="utf-8").replace("tiny.py[CX3T]:", "tiny.py[ZX3H]:")
        with code_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(tampered)

        ver_code, ver_out = cli("verify", "--root", str(self.root))
        self.assertEqual(ver_code, 1, ver_out)
        self.assertIn("Invalid Layer `Z`", ver_out)
        self.assertIn("Size tier mismatch", ver_out)

        lint_data = cli_json("lint", "--root", str(self.root))
        finding_codes = {(f["path"], f["code"]) for f in lint_data["findings"]}
        self.assertIn(("tiny.py", "layer-invalid"), finding_codes)
        self.assertIn(("tiny.py", "size-mismatch"), finding_codes)
        self.assertEqual(cli("lint", "--root", str(self.root), "--strict")[0], 1)


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase1FixesTests(RepoTestCase):
    def _git_at(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd,
            check=True,
        )

    def test_mcp_system_exit_isolation_dash_inputs_headers_and_14_tools(self):
        import io
        import aoci_mcp

        # 1) initialize with file:// rootUri + verify all 14 tools mark `root` required
        init_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "rootUri": self.root.resolve().as_uri()},
            }
        )
        self.assertEqual(init_resp["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("resources", init_resp["result"]["capabilities"])
        self.assertIn("prompts", init_resp["result"]["capabilities"])

        list_resp = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in list_resp["result"]["tools"]}
        self.assertEqual(
            set(tools.keys()),
            {
                "aoci_init",
                "aoci_overview",
                "aoci_query",
                "aoci_context",
                "aoci_diff",
                "aoci_scan",
                "aoci_outline",
                "aoci_scaffold",
                "aoci_ack",
                "aoci_upsert",
                "aoci_migrate_renames",
                "aoci_restore",
                "aoci_shard",
                "aoci_ensure_git_exclude",
                "aoci_lint",
                "aoci_verify",
                "aoci_audit",
                "aoci_export",
                "aoci_pool",
                "aoci_pack",
                "aoci_zoom",
                "aoci_prepare_sync",
                "aoci_apply_sync",
                "aoci_symbol",
            },
        )
        for t_name, t_spec in tools.items():
            self.assertIn("root", t_spec["inputSchema"].get("required", []), f"{t_name} must require root")
            self.assertIn("annotations", t_spec, f"{t_name} must define annotations")
            self.assertIn("readOnlyHint", t_spec["annotations"])
        self.assertTrue(tools["aoci_query"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["aoci_upsert"]["annotations"]["readOnlyHint"])

        res_list = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 21, "method": "resources/list"})
        res_uris = {r["uri"] for r in res_list["result"]["resources"]}
        self.assertEqual(res_uris, {"aoci://manifest", "aoci://meta", "aoci://code", "aoci://hubs", "aoci://shards/INDEX"})

        prm_list = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 22, "method": "prompts/list"})
        prm_names = {p["name"] for p in prm_list["result"]["prompts"]}
        self.assertEqual(prm_names, {"aoci-bootstrap", "aoci-review-diff", "aoci-sync"})
        prm_get = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 23, "method": "prompts/get", "params": {"name": "aoci-sync", "arguments": {"root": str(self.root)}}}
        )
        self.assertIn("aoci_scan", prm_get["result"]["messages"][0]["content"]["text"])

        # 2) `aoci_init` via MCP using rootUri fallback when `root` omitted in direct RPC call
        self.write("-dash.py", "def handle():\n    return 1\n\n\nVAL = 2\n")
        r_init = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "aoci_init", "arguments": {}}}
        )
        self.assertFalse(r_init["result"]["isError"])

        res_read = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 24, "method": "resources/read", "params": {"uri": f"aoci://manifest?root={self.root}"}}
        )
        self.assertIn("#AOCI-ROOT-MANIFEST:", res_read["result"]["contents"][0]["text"])

        # 3) `--bad-flag` entry MUST NOT crash MCP server with SystemExit(2); returns isError=True cleanly
        r_bad = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "aoci_upsert", "arguments": {"root": str(self.root), "entries": ["--bad-flag"]}},
            }
        )
        self.assertTrue(r_bad["result"]["isError"])
        self.assertIn("Syntax mismatch", r_bad["result"]["content"][0]["text"])

        # 4) Dash-prefixed filename works across aoci_outline, aoci_upsert, aoci_ack, aoci_lint, aoci_migrate_renames
        r_out = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "aoci_outline", "arguments": {"root": str(self.root), "paths": ["-dash.py"]}},
            }
        )
        self.assertFalse(r_out["result"]["isError"], r_out["result"]["content"][0]["text"])
        r_up = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "aoci_upsert",
                    "arguments": {
                        "root": str(self.root),
                        "entries": ["-dash.py[AX3T]: F:handles dash prefixed module | R:- | A:handle | S:-"],
                    },
                },
            }
        )
        self.assertFalse(r_up["result"]["isError"], r_up["result"]["content"][0]["text"])
        self.write("-dash.py", "def handle():\n    return 2\n\n\nVAL = 3\n")
        r_ack = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "aoci_ack", "arguments": {"root": str(self.root), "paths": ["-dash.py"]}},
            }
        )
        self.assertFalse(r_ack["result"]["isError"], r_ack["result"]["content"][0]["text"])
        (self.root / "-dash.py").rename(self.root / "renamed_dash.py")
        r_mig = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "aoci_migrate_renames", "arguments": {"root": str(self.root)}},
            }
        )
        self.assertFalse(r_mig["result"]["isError"], r_mig["result"]["content"][0]["text"])
        r_lint = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "aoci_lint", "arguments": {"root": str(self.root), "strict": True}},
            }
        )
        self.assertFalse(r_lint["result"]["isError"], r_lint["result"]["content"][0]["text"])

        # 5) Content-Type BEFORE Content-Length header framing in serve_stdio
        body = json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"}).encode("utf-8")
        framed = (
            b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        out_buf = io.BytesIO()
        aoci_mcp.serve_stdio(io.BytesIO(framed), out_buf)
        self.assertIn(b'"id":10', out_buf.getvalue())

    def test_monorepo_subproject_diff_briefing_and_git_copy_not_deleted(self):
        sub = self.root / "packages" / "api"
        sub.mkdir(parents=True)
        for rel, body in (("app.py", "PORT = 8080\n"), ("caller.py", "import app\n")):
            p = sub / rel
            with p.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            age_file(p)
        self._git_at(self.root, "add", ".")
        self._git_at(self.root, "commit", "-q", "-m", "init monorepo")

        self.assertEqual(cli("init", "--root", str(sub))[0], 0)
        self.assertEqual(
            cli(
                "upsert",
                "--root",
                str(sub),
                "--entry",
                "app.py[CG8T]: F:defines API listen port | R:- | A:PORT<-caller.py | S:must stay unprivileged >=1024",
                "--entry",
                "caller.py[EA5T]: F:boots API listener using app config | R:app.py | A:boot | S:-",
            )[0],
            0,
        )

        # Working-tree edit + untracked file inside subproject `packages/api`
        with (sub / "app.py").open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("PORT = 9090\n")
        with (sub / "extra.py").open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("EXTRA = 1\n")

        wt_brief = cli_json("diff", "--root", str(sub))
        self.assertEqual([c["path"] for c in wt_brief["high_risk_constraints"]], ["app.py"])
        self.assertTrue(any(ln.startswith("app.py[") for ln in wt_brief["changed_entries"]))
        self.assertEqual(wt_brief["unindexed_changed"], ["extra.py"])
        self.assertTrue(any(ln.startswith("caller.py[") for ln in wt_brief["affected_neighbors"]))

        # Commit edit and check Git revision diff (`HEAD~1..HEAD`) inside subproject
        self._git_at(self.root, "add", "packages/api/app.py")
        self._git_at(self.root, "commit", "-q", "-m", "change port")
        rev_brief = cli_json("diff", "HEAD~1..HEAD", "--root", str(sub))
        self.assertEqual([c["path"] for c in rev_brief["high_risk_constraints"]], ["app.py"])
        self.assertTrue(any(ln.startswith("caller.py[") for ln in rev_brief["affected_neighbors"]))

        # Git Copy (`C100\0app.py\0app_copy.py\0`) must NOT report source `app.py` as deleted
        fake_cp = subprocess.CompletedProcess(["git"], 0, stdout="C100\0app.py\0app_copy.py\0", stderr="")
        with mock.patch.object(aoci_index, "run_git", return_value=fake_cp):
            cp_brief = aoci_index.compute_diff_briefing(sub, rev="HEAD~1..HEAD")
        self.assertEqual(cp_brief["deleted_entries"], [])
        self.assertEqual(cp_brief["unindexed_changed"], ["app_copy.py"])

    def test_project_mcp_portability_plugin_root_and_no_tests_bloat(self):
        proj = Path(self._tmp.name) / "portable_proj"
        home = Path(self._tmp.name) / "portable_home"
        proj.mkdir(parents=True)
        home.mkdir(parents=True)

        # Install project-scoped for BOTH hosts with --with-mcp (without --as-plugin)
        r = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "--host",
                "both",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--python-cmd",
                sys.executable,
                "--with-mcp",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # `tests/` must NOT be copied into installed skill directories
        self.assertFalse((proj / ".agents" / "skills" / "aoci-index" / "tests").exists())
        self.assertFalse((proj / ".claude" / "skills" / "aoci-index" / "tests").exists())

        # Antigravity project MCP config must be under `.agents/plugins/aoci-index/mcp_config.json`, NOT `.agents/mcp_config.json`
        self.assertFalse((proj / ".agents" / "mcp_config.json").exists())
        agy_mcp_path = proj / ".agents" / "plugins" / "aoci-index" / "mcp_config.json"
        cc_mcp_path = proj / ".mcp.json"
        self.assertTrue(agy_mcp_path.is_file())
        self.assertTrue(cc_mcp_path.is_file())

        # Neither project MCP config may contain machine-specific absolute project paths
        agy_raw = agy_mcp_path.read_text(encoding="utf-8")
        cc_raw = cc_mcp_path.read_text(encoding="utf-8")
        self.assertNotIn(proj.resolve().as_posix(), agy_raw)
        self.assertNotIn(str(proj.resolve()), agy_raw)
        self.assertNotIn(proj.resolve().as_posix(), cc_raw)
        self.assertNotIn(str(proj.resolve()), cc_raw)
        self.assertIn("PLUGIN_ROOT", agy_raw)
        self.assertIn("CLAUDE_PROJECT_DIR", cc_raw)

        # Actually spawn the generated Antigravity plugin MCP server using PLUGIN_ROOT from an unrelated CWD
        agy_srv = json.loads(agy_raw)["mcpServers"]["aoci-index"]
        ping_req = json.dumps({"jsonrpc": "2.0", "id": 77, "method": "ping"}) + "\n"
        proc = subprocess.run(
            [agy_srv["command"], *agy_srv["args"]],
            cwd=str(home),
            env=dict(os.environ, PLUGIN_ROOT=str(proj / ".agents" / "plugins" / "aoci-index")),
            input=ping_req,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.strip())["id"], 77)

        # Clean uninstall removes both the skill and the lightweight MCP plugin wrapper
        r_un = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "--host",
                "both",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--uninstall",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
        self.assertFalse((proj / ".agents" / "plugins" / "aoci-index").exists())
        self.assertFalse(json.loads(cc_mcp_path.read_text(encoding="utf-8")).get("mcpServers"))

    def test_restore_sha_fallback_rewrites_moved_path_references(self):
        body = "def calc_fee(x):\n    return x * 2\n\n\nRATE = 2\n"
        self.write("src/old_fee.py", body)
        self.write("src/checkout.py", "def pay():\n    return 1\n\n\nOK = True\n")
        self._git_at(self.root, "checkout", "-q", "-b", "main")
        self._git_at(self.root, "add", ".")
        self._git_at(self.root, "commit", "-q", "-m", "base")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/old_fee.py[DX6T]: F:calculates transaction fee | R:- | A:calc_fee<-src/checkout.py | S:-",
                "src/checkout.py[AX6T]: F:runs checkout payment flow | R:src/old_fee.py | A:pay | S:-",
            )[0],
            0,
        )

        # On a new branch, `src/old_fee.py` was moved to `src/billing/fee.py` (same content SHA) and pruned on `main`
        (self.root / "src/old_fee.py").unlink()
        self.assertEqual(cli("upsert", "--root", str(self.root), "--prune-only", "--drop-malformed")[0], 0)
        self.write("src/billing/fee.py", body)

        # Default restore is exact-(path, sha256)-only: cross-path match is surfaced as a review candidate
        res_default = cli_json("restore", "--root", str(self.root))
        self.assertEqual(res_default["restored"], [])
        self.assertEqual(res_default["restored_by_sha"], [])
        self.assertEqual(len(res_default["sha_candidates"]), 1)
        self.assertEqual(res_default["sha_candidates"][0]["path"], "src/billing/fee.py")
        self.assertEqual(res_default["sha_candidates"][0]["matched_from"], "src/old_fee.py")
        self.assertFalse(res_default["sha_candidates"][0]["orig_exists"])
        self.assertEqual(res_default["sha_candidates"][0]["reason"], "cross-path-sha-match")

        # Explicit --allow-sha-fallback restores the unambiguously moved file and rewrites refs
        res = cli_json("restore", "--root", str(self.root), "--allow-sha-fallback")
        self.assertEqual(res["restored_by_sha"], ["src/billing/fee.py"])
        self.assertIn("src/checkout.py", res["rewritten_refs"])
        self.assertIn("R:src/billing/fee.py", self.code_text())


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditV2RegressionTests(RepoTestCase):
    def test_p0_redos_long_identifiers_complete_instantly(self):
        self.write("a.py", "x = 1\n")
        self.init()
        long_sym_a = "process_long_identifier_without_slashes_or_dots_alpha"
        long_sym_b = "validate_super_long_camel_case_identifier_token_beta"
        entries = {
            f"mod_{i}/file_{i}.py": (
                f"mod_{i}/file_{i}.py[AX6T]: F:handles long symbol workflow {i} | "
                f"R:{long_sym_a},a.py | A:{long_sym_b}<-a.py | S:-"
            )
            for i in range(40)
        }
        entries["a.py"] = "a.py[CG5T]: F:root entry module | R:- | A:boot | S:-"
        t0 = time.perf_counter()
        deps, dependents = aoci_index.build_fras_graph(entries)
        dangling = aoci_index.find_dangling_refs(self.root, entries)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.25)
        self.assertEqual(deps["mod_0/file_0.py"], {"a.py"})
        self.assertIn("mod_0/file_0.py", dependents["a.py"])
        self.assertEqual(dangling, {})

    def test_p1_cli_relative_output_and_entries_file_resolve_against_root_from_unrelated_cwd(self):
        self.write("src/app.py", "def run():\n    return 1\n")
        self.init()
        unrelated_cwd = Path(self._tmp.name) / "outside_cwd"
        unrelated_cwd.mkdir()

        # `scaffold --output .aoci/pending/batch.txt` from an unrelated CWD writes inside `--root`
        r_scaf = subprocess.run(
            [
                sys.executable, str(SCRIPT), "scaffold", "--root", str(self.root),
                "--unindexed", "--output", ".aoci/pending/batch_01.txt", "--json",
            ],
            cwd=str(unrelated_cwd),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_scaf.returncode, 0, r_scaf.stdout + r_scaf.stderr)
        batch_file = self.root / ".aoci" / "pending" / "batch_01.txt"
        self.assertTrue(batch_file.is_file())
        self.assertFalse((unrelated_cwd / ".aoci").exists())

        # Replace <TODO> and run `upsert --entries-file .aoci/pending/batch_01.txt --json` from unrelated CWD
        txt = batch_file.read_text(encoding="utf-8").replace("<TODO:read-source>", "runs application entrypoint")
        with batch_file.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(txt)
        r_up = subprocess.run(
            [
                sys.executable, str(SCRIPT), "upsert", "--root", str(self.root),
                "--entries-file", ".aoci/pending/batch_01.txt", "--json",
            ],
            cwd=str(unrelated_cwd),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        up_json = json.loads(r_up.stdout)
        self.assertTrue(up_json["ok"])
        self.assertEqual(up_json["upserted"], ["src/app.py"])

        # Missing `--entries-file` returns clean exit code 2 (no unhandled Traceback)
        r_miss = subprocess.run(
            [
                sys.executable, str(SCRIPT), "upsert", "--root", str(self.root),
                "--entries-file", ".aoci/pending/does_not_exist.txt", "--json",
            ],
            cwd=str(unrelated_cwd),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_miss.returncode, 2)
        self.assertFalse(json.loads(r_miss.stdout)["ok"])

    def test_p1_security_diff_rev_injection_and_outline_hardening(self):
        self.write("-dash.py", "def ok():\n    return 1\n")
        self.write("huge.py", "x = 1\n" * 50)
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
        outside = Path(self._tmp.name) / "secret.py"
        outside.write_text("SECRET = 'leak'\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", "-dash.py", "huge.py"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"],
            cwd=self.root, check=True,
        )
        self.init()

        # `diff` rejects flag-like revision specs
        r_diff = cli("diff", "--root", str(self.root), "--", "--output=evil")
        self.assertEqual(r_diff[0], 2)
        self.assertIn("Invalid Git revision", r_diff[1])

        # `outline` allows `-dash.py` inside root, rejects `../secret.py`, binary, and oversized files
        r_out = subprocess.run(
            [
                sys.executable, str(SCRIPT), "outline", "--root", str(self.root),
                "--max-bytes", "64", "--json", "--", "-dash.py", "../secret.py", "blob.bin", "huge.py",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_out.returncode, 1)
        data = json.loads(r_out.stdout)
        self.assertEqual([o["path"] for o in data["outlines"]], ["-dash.py"])
        rej = {r["path"]: r["reason"] for r in data["rejected"]}
        self.assertEqual(rej["../secret.py"], "path-invalid")
        self.assertEqual(rej["blob.bin"], "binary-content")
        self.assertTrue(rej["huge.py"].startswith("too-large("))

    def test_p1_symlink_skipped_consistently_in_inspect_and_fingerprinting(self):
        self.write("real.py", "x = 1\n")
        link_st = mock.MagicMock()
        link_st.st_mode = 0o120777  # S_IFLNK
        link_st.st_size = 10
        with mock.patch.object(os, "lstat", return_value=link_st):
            rel, fp, reason = aoci_index.inspect_and_fingerprint_one(
                self.root, "link.py", None, None, False, aoci_index.DEFAULT_MAX_BYTES
            )
        self.assertEqual(rel, "link.py")
        self.assertIsNone(fp)
        self.assertEqual(reason, "symlink")

    def test_p1_sqlite_semantic_pool_legacy_migration_concurrency_lru_and_session_hint(self):
        import concurrent.futures

        self.write("a.py", "def alpha():\n    return 1\n\n\nA = 1\n")
        self.write("b.py", "def beta():\n    return 2\n\n\nB = 2\n")
        subprocess.run(["git", "add", "a.py", "b.py"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"],
            cwd=self.root, check=True,
        )
        self.init()
        git_info = aoci_index.detect_git_info(self.root)
        legacy_path = aoci_index._legacy_semantic_pool_json_path(self.root, git_info)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        fp_a = aoci_index.fingerprint_file(self.root / "a.py")
        legacy_payload = {
            "schema_version": 1,
            "by_path_sha": {
                f"a.py\t{fp_a['sha256']}": "a.py[AX5T]: F:migrated legacy alpha entry | R:- | A:alpha | S:-"
            },
            "by_sha": {
                fp_a["sha256"]: "a.py[AX5T]: F:migrated legacy alpha entry | R:- | A:alpha | S:-"
            },
        }
        with legacy_path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(legacy_payload, fh)

        # `SessionStart` hook surfaces restorable count from semantic pool
        start = hook("session-start", {"cwd": str(self.root), "session_id": "s-pool", "hook_event_name": "SessionStart"})
        self.assertIn("restorable with 0 tokens", start["hookSpecificOutput"]["additionalContext"])

        # `restore` migrates legacy `.pool.json` into `.pool.sqlite3` and restores a.py
        res = cli_json("restore", "--root", str(self.root))
        self.assertEqual(res["restored"], ["a.py"])
        self.assertTrue(aoci_index.semantic_pool_path(self.root, git_info).is_file())

        # Concurrent writes + LRU eviction on SQLite WAL pool
        with mock.patch.object(aoci_index, "MAX_SEMANTIC_POOL_ENTRIES", 5):
            def _writer(idx: int) -> None:
                fp_fake = {"sha256": f"{idx:064x}", "lines": 10}
                ent = f"f{idx}.py[CX3T]: F:concurrent entry number {idx} | R:- | A:- | S:-"
                aoci_index.record_semantic_pool_entries(self.root, git_info, [(f"f{idx}.py", fp_fake, ent)])

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(_writer, range(10)))
            loaded = aoci_index.load_semantic_pool(self.root, git_info)
            self.assertLessEqual(len(loaded["by_path_sha"]), 5)
            self.assertLessEqual(len(loaded["by_sha"]), 5)

    def test_p1_scaffold_batch_matches_scan_and_uses_outline_cache(self):
        orig = "".join(f"def step_{i}(v):\n    return v + {i}\n" for i in range(8))
        self.write("old/flow.py", orig)
        self.write("pending_a.py", "def pa():\n    return 1\n")
        self.write("pending_b.py", "def pb():\n    return 2\n")
        self.init()
        self.upsert("old/flow.py[AX6T]: F:executes eight step flow | R:- | A:step_0 | S:-")

        (self.root / "old/flow.py").unlink()
        self.write("new/flow.py", orig.replace("return v + 7", "return v + 777"))

        scan_data = cli_json("scan", "--root", str(self.root), "--batch-size", "1")
        batch1_scan = [it["path"] for it in scan_data["batches"][0]]
        scaf_data = cli_json("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "1")
        batch1_scaf = [ln.split("[", 1)[0] for ln in scaf_data["entries"]]
        self.assertEqual(batch1_scaf, batch1_scan)
        self.assertNotIn("new/flow.py", batch1_scaf)
        self.assertTrue((self.root / ".aoci" / ".cache" / "outline_cache.json").is_file())

        # Second scaffold run hits `outline_cache.json` cleanly without re-extracting unchanged files
        with mock.patch.object(aoci_index, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
            scaf_again = cli_json("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "1")
        self.assertEqual([ln.split("[", 1)[0] for ln in scaf_again["entries"]], batch1_scan)

    def test_p2_parsers_go_rust_js_and_tsconfig_extends(self):
        # Go func + receiver + commented import ignored
        self.write("go.mod", "module example.com/app\n")
        self.write(
            "server/srv.go",
            'package server\nimport (\n\t"fmt"\n\t// "os"\n)\ntype Server struct{}\nfunc NewServer() *Server { return nil }\nfunc (s *Server) Start() {}\n',
        )
        # Rust crate::{...} + super:: resolution
        self.write("src/lib.rs", "use crate::{auth, order::repo};\npub fn run() {}\n")
        self.write("src/auth.rs", "pub fn check() {}\n")
        self.write("src/order/mod.rs", "pub mod repo;\n")
        self.write("src/order/repo.rs", "use super::super::auth::check;\npub fn save() {}\n")
        # JS string containing "export" is not treated as export
        self.write("web/util.ts", 'const LABEL = "export button";\nexport function renderBtn() { return LABEL }\n')
        # tsconfig extends chain
        self.write("tsconfig.base.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@core/*": ["web/*"]}}}\n')
        self.write("tsconfig.json", '{"extends": "./tsconfig.base.json"}\n')
        self.write("web/main.ts", "import { renderBtn } from '@core/util'\nexport const ok = renderBtn()\n")
        self.init()

        go_out = aoci_index.extract_file_outline(self.root / "server/srv.go", "server/srv.go")
        self.assertEqual(go_out["imports"], ["fmt"])
        self.assertEqual(aoci_index._clean_symbol_names(go_out["symbols"]), ["Server", "NewServer", "Start"])

        ts_out = aoci_index.extract_file_outline(self.root / "web/util.ts", "web/util.ts")
        self.assertEqual(ts_out["symbols"], ["function renderBtn [L2]"])

        scaf = cli_json("scaffold", "--root", str(self.root), "--unindexed")
        by_path = {ln.split("[", 1)[0]: ln for ln in scaf["entries"]}
        self.assertIn("A:Server/NewServer/Start", by_path["server/srv.go"])
        self.assertIn("R:src/auth.rs,src/order/repo.rs", by_path["src/lib.rs"])
        self.assertIn("R:src/auth.rs", by_path["src/order/repo.rs"])
        self.assertIn("R:web/util.ts", by_path["web/main.ts"])

    def test_perf_mcp_bm25_cache_diff_cache_and_write_json_flags(self):
        import aoci_mcp

        self.write("src/db.py", "def query():\n    return []\n")
        self.write("src/api.py", "from src import db\ndef endpoint():\n    return db.query()\n")
        subprocess.run(["git", "add", "src/db.py", "src/api.py"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"],
            cwd=self.root, check=True,
        )
        self.init()

        up_code, up_out = cli(
            "upsert", "--root", str(self.root), "--json",
            "--entry", "src/db.py[PX8T]: F:executes parameterized SQL database queries | R:- | A:query<-src/api.py | S:parameterized SQL only",
            "--entry", "src/api.py[EX7T]: F:exposes JSON HTTP endpoint | R:src/db.py | A:endpoint | S:-",
        )
        self.assertEqual(up_code, 0, up_out)
        self.assertTrue(json.loads(up_out)["ok"])

        self.write("src/api.py", "from src import db\ndef endpoint():\n    return db.query() or []\n")
        ack_code, ack_out = cli("ack", "--root", str(self.root), "--json", "src/api.py")
        self.assertEqual(ack_code, 0, ack_out)
        self.assertEqual(json.loads(ack_out)["acked"], ["src/api.py"])

        # 1st MCP query builds BM25Index; 2nd query reuses cached BM25Index without re-tokenizing
        is_err1, txt1 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "parameterized SQL"})
        self.assertFalse(is_err1, txt1)
        self.assertEqual(json.loads(txt1)["scores"][0]["path"], "src/db.py")

        with mock.patch.object(aoci_index, "BM25Index", side_effect=AssertionError("must reuse cached BM25Index")):
            is_err2, txt2 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "HTTP endpoint"})
        self.assertFalse(is_err2, txt2)
        self.assertEqual(json.loads(txt2)["scores"][0]["path"], "src/api.py")

        # MCP `aoci_diff` hits _CACHE and includes 1-hop `dependency_constraints` from R:src/db.py
        with mock.patch.object(aoci_index, "parse_code_volume", side_effect=AssertionError("must hit _CACHE")):
            is_err3, txt3 = aoci_mcp.call_tool("aoci_diff", {"root": str(self.root)})
        self.assertFalse(is_err3, txt3)
        diff_json = json.loads(txt3)
        self.assertEqual([c["path"] for c in diff_json["dependency_constraints"]], ["src/db.py"])

    def test_p2_installer_split_python_cmd_and_antigravity_plugin_uninstall(self):
        home = Path(self._tmp.name) / "inst_home"
        proj = Path(self._tmp.name) / "inst_proj"
        home.mkdir()
        proj.mkdir()

        r1 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home),
                "--with-hooks", "--with-rules", "--with-mcp", "--as-plugin", "--python-cmd", "py -3",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        pdir = proj / ".agents" / "plugins" / "aoci-index"
        mcp_cfg = json.loads((pdir / "mcp_config.json").read_text(encoding="utf-8"))
        srv = mcp_cfg["mcpServers"]["aoci-index"]
        self.assertEqual(srv["command"], "py")
        self.assertEqual(srv["args"][0], "-3")
        self.assertIn(".agents/plugins/aoci-index", srv["args"][2])

        # Uninstall WITHOUT `--as-plugin` still cleanly removes the Antigravity plugin bundle
        r2 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home), "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertFalse(pdir.exists())


@unittest.skipUnless(HAS_GIT, "git is required")
class Milestone2FeatureTests(RepoTestCase):
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

    def test_m2_sig_hash_classifies_impl_only_vs_contract_changed_and_ack_impl_only(self):
        self.write(
            "src/auth.py",
            "import hashlib\n\ndef verify(token: str) -> bool:\n    # initial comment\n    return bool(token)\n",
        )
        self.write(
            "src/api.py",
            "from src import auth\n\ndef endpoint(req: str) -> int:\n    return 200 if auth.verify(req) else 401\n",
        )
        self.write(
            "web/app.ts",
            "import { helper } from './helper';\nexport function boot(port: number) { return helper(port); }\n",
        )
        self.write("web/helper.ts", "export function helper(p: number) { return p; }\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/auth.py[DX8T]: F:verifies bearer token signature | R:- | A:verify<-src/api.py | S:constant time compare",
                "src/api.py[EX6T]: F:exposes authentication HTTP endpoint | R:src/auth.py | A:endpoint | S:-",
                "web/app.ts[UX5T]: F:boots frontend web application | R:web/helper.ts | A:boot | S:-",
                "web/helper.ts[CX3T]: F:returns numeric helper value | R:- | A:helper<-web/app.ts | S:-",
            )[0],
            0,
        )

        baseline = json.loads((self.root / ".aoci" / "baseline.json").read_text(encoding="utf-8"))
        self.assertIn("sig_hash", baseline["files"]["src/auth.py"])
        self.assertIn("sig_hash", baseline["files"]["web/app.ts"])

        # Edit body/comment only in auth.py + web/helper.ts, AND change function signature + import in api.py & web/app.ts
        self.write(
            "src/auth.py",
            'import hashlib\n\ndef verify(token: str) -> bool:\n    """Updated docstring"""\n    if not token:\n        return False\n    return hashlib.sha256(token.encode()).hexdigest() != ""\n',
        )
        self.write(
            "web/helper.ts",
            "export function helper(p: number) { const x = p + 1; return x; }\n",
        )
        self.write(
            "src/api.py",
            "from src import auth\nimport json\n\ndef endpoint(req: str, strict: bool = True) -> dict:\n    return {'ok': auth.verify(req) and strict}\n",
        )
        self.write(
            "web/app.ts",
            "import { helper } from './helper';\nexport function boot(port: number) { return helper(port); }\nexport function shutdown() {}\n",
        )

        scan = cli_json("scan", "--root", str(self.root))["drift"]
        by_path = {it["path"]: it for it in scan["modified"]}
        self.assertEqual(by_path["src/auth.py"]["change_kind"], "impl_only")
        self.assertEqual(by_path["web/helper.ts"]["change_kind"], "impl_only")
        self.assertEqual(by_path["src/api.py"]["change_kind"], "contract_changed")
        self.assertEqual(by_path["web/app.ts"]["change_kind"], "contract_changed")
        self.assertEqual(scan["modified_summary"]["impl_only"], 2)
        self.assertEqual(scan["modified_summary"]["contract_changed"], 2)

        # `ack --impl-only` without `--reviewed` fails (unless --dry-run)
        self.assertEqual(cli("ack", "--root", str(self.root), "--impl-only")[0], 2)
        # Explicit path with contract_changed fails under `--impl-only`
        err_code, err_out = cli("ack", "--root", str(self.root), "--impl-only", "--reviewed", "src/api.py")
        self.assertEqual(err_code, 1)
        self.assertIn("contract_changed", err_out)

        ack_res = cli_json("ack", "--root", str(self.root), "--impl-only", "--reviewed")
        self.assertEqual(sorted(ack_res["acked"]), ["src/auth.py", "web/helper.ts"])
        skipped_paths = {s["path"] for s in ack_res["skipped_contract_changed"]}
        self.assertEqual(skipped_paths, {"src/api.py", "web/app.ts"})

        # Only contract_changed files remain in modified drift
        scan_after = cli_json("scan", "--root", str(self.root))["drift"]
        self.assertEqual([it["path"] for it in scan_after["modified"]], ["src/api.py", "web/app.ts"])

    def test_m2_context_budget_prioritizes_hop_and_importance(self):
        import aoci_mcp

        for rel in ("target.py", "dep_hi.py", "dep_lo.py", "caller_hi.py", "caller_lo.py", "hop2_deep.py"):
            self.write(rel, "x = 1\n")
        self.init()
        entries = [
            "target.py[AX7T]: F:coordinates core target workflow logic | R:dep_hi.py,dep_lo.py | A:run | S:-",
            "dep_hi.py[PX9T]: F:executes critical transactional persistence ledger updates | R:hop2_deep.py | A:commit<-target.py | S:serializable transaction lock required",
            "dep_lo.py[CX2T]: F:formats minor helper display label strings | R:- | A:fmt<-target.py | S:-",
            "caller_hi.py[EX8T]: F:exposes authenticated HTTP gateway API entrypoint | R:target.py | A:api | S:rate limit before auth",
            "caller_lo.py[CX1T]: F:prints local developer debug helper trace | R:target.py | A:dbg | S:-",
            "hop2_deep.py[DX9T]: F:defines deep second hop core schema model | R:- | A:Schema<-dep_hi.py | S:never mutate schema id",
        ]
        self.assertEqual(self.upsert(*entries)[0], 0)

        full = cli_json("context", "--root", str(self.root), "target.py", "--depth", "2")
        self.assertGreater(full["estimated_tokens"], 80)

        # Pick a budget fitting Global-Invariants + target + top 1-hop high-importance items, excluding 2-hop/low-imp
        target_plus_hubs = (
            aoci_index.estimate_tokens("-")
            + aoci_index.estimate_tokens(entries[0])
            + aoci_index.estimate_tokens(entries[1])
            + aoci_index.estimate_tokens(entries[3])
            + 2
        )
        budgeted = cli_json("context", "--root", str(self.root), "target.py", "--depth", "2", "--budget", str(target_plus_hubs))
        self.assertTrue(budgeted["truncated"])
        self.assertLessEqual(budgeted["estimated_tokens"], target_plus_hubs)
        kept = "\n".join(budgeted["targets"] + budgeted["dependencies"] + budgeted["callers"])
        self.assertIn("target.py[", kept)
        self.assertIn("dep_hi.py[", kept)
        self.assertIn("caller_hi.py[", kept)
        self.assertNotIn("hop2_deep.py[", kept)
        self.assertIn("hop2_deep.py", budgeted["omitted_paths"])

        # MCP `aoci_context` produces identical budgeted output
        is_err, mcp_txt = aoci_mcp.call_tool(
            "aoci_context",
            {"root": str(self.root), "paths": ["target.py"], "depth": 2, "budget": target_plus_hubs},
        )
        self.assertFalse(is_err, mcp_txt)
        mcp_obj = json.loads(mcp_txt)
        self.assertEqual(mcp_obj["dependencies"], budgeted["dependencies"])
        self.assertEqual(mcp_obj["callers"], budgeted["callers"])
        self.assertEqual(mcp_obj["omitted_paths"], budgeted["omitted_paths"])

    def test_m2_audit_detects_missing_static_phantom_cycles_and_layer_rules(self):
        import aoci_mcp

        self.write("pyproject.toml", "[project]\nname='demo'\n")
        self.write("src/entry.py", "from src import domain, repo\n")
        self.write("src/domain.py", "from src import entry, repo\n")
        self.write("src/repo.py", "from src import entry, domain\n")
        self.write("src/phantom.py", "def helper():\n    return 1\n")
        self.init()
        # Add custom rule in manifest (`A!->K`) on top of built-in rules (`D!->E,U`, `P!->E,U`)
        manifest_p = self.root / ".aoci" / "aoci.txt"
        manifest_p.write_text(
            manifest_p.read_text(encoding="utf-8") + "#Layer-Rules: A!->K\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self.upsert(
                "pyproject.toml[RG3T]: F:declares package metadata | R:- | A:- | S:-",
                # R: mentions repo.py and pyproject.toml, omits static import src/domain.py -> missing-static-dep
                "src/entry.py[EX7T]: F:handles entry routing | R:src/repo.py,pyproject.toml | A:main | S:-",
                # R: omits repo.py (static) and lists entry.py + phantom.py -> D!->E violation + phantom info
                "src/domain.py[DX7T]: F:implements core domain rules | R:src/entry.py,src/phantom.py | A:calc | S:-",
                # repo depends on entry and domain -> 3-node cycle (entry->repo->domain->entry) + P!->E violation
                "src/repo.py[PX7T]: F:persists repository state | R:src/entry.py,src/domain.py | A:save | S:-",
                "src/phantom.py[CX3T]: F:provides auxiliary helper routine | R:- | A:helper | S:-",
            )[0],
            0,
        )

        # Deep linear chain test for iterative Tarjan SCC (> 1200 nodes won't hit recursion limit)
        deep_adj = {f"n{i}": ({f"n{i+1}"} if i < 1499 else set()) for i in range(1500)}
        self.assertEqual(aoci_index.find_dependency_cycles(deep_adj), [])

        aud = cli_json("audit", "--root", str(self.root))
        self.assertFalse(aud["ok"])
        missing_pairs = {(x["from"], x["to"]) for x in aud["missing_static_deps"]}
        self.assertIn(("src/entry.py", "src/domain.py"), missing_pairs)
        self.assertIn(("src/domain.py", "src/repo.py"), missing_pairs)

        # Only same-ecosystem Python R: phantom (src/phantom.py), NOT pyproject.toml read-along!
        phantom_pairs = {(x["from"], x["to"]) for x in aud["phantom_fras_deps"]}
        self.assertIn(("src/domain.py", "src/phantom.py"), phantom_pairs)
        self.assertNotIn(("src/entry.py", "pyproject.toml"), phantom_pairs)

        cycle_sets = [set(c["nodes"]) for c in aud["cycles"]]
        self.assertTrue(any({"src/entry.py", "src/repo.py", "src/domain.py"}.issubset(cs) for cs in cycle_sets))

        rules_hit = {v["rule"] for v in aud["layer_violations"]}
        self.assertIn("D!->E", rules_hit)
        self.assertIn("P!->E", rules_hit)

        # `--strict` returns exit code 1 when issues exist, and custom `--forbid E!->P` catches E->P
        strict_code, _ = cli("audit", "--root", str(self.root), "--strict", "--forbid", "E!->P")
        self.assertEqual(strict_code, 1)
        is_err, mcp_txt = aoci_mcp.call_tool("aoci_audit", {"root": str(self.root), "forbid": "E!->P"})
        self.assertFalse(is_err, mcp_txt)
        mcp_aud = json.loads(mcp_txt)
        self.assertIn("E!->P", {v["rule"] for v in mcp_aud["layer_violations"]})

    def test_m2_export_mermaid_and_dot_across_levels_focus_and_escaping(self):
        import aoci_mcp

        self.write("模块/入口.py", "x = 1\n")
        self.write("pages/[id]/view.tsx", "export default function View() { return null; }\n")
        self.write("core/service.py", "y = 2\n")
        self.write("core/isolated.py", "z = 3\n")
        self.init()
        meta_p = self.root / ".aoci" / "aoci.meta.txt"
        meta_p.write_text(meta_p.read_text(encoding="utf-8") + "\n# A=Auth  U=UI\n", encoding="utf-8")
        self.assertEqual(
            self.upsert(
                '模块/入口.py[EA7T]: F:入口服务调用 "核心" 逻辑 | R:core/service.py,pages/[id]/view.tsx | A:start | S:-',
                "pages/[id]/view.tsx[UU6T]: F:渲染动态路由页面组件 | R:core/service.py | A:View | S:-",
                "core/service.py[DX8T]: F:核心领域编排服务 | R:- | A:run | S:idempotent mutations only",
                "core/isolated.py[CX2T]: F:孤立辅助脚本模块 | R:- | A:- | S:-",
            )[0],
            0,
        )

        mmd = cli_json("export", "--root", str(self.root), "--format", "mermaid", "--group-by", "domain")
        self.assertTrue(mmd["content"].startswith("flowchart LR\n"))
        self.assertEqual(mmd["nodes"], 4)
        self.assertEqual(mmd["edges"], 3)
        self.assertIn("模块/入口.py", mmd["content"])
        self.assertIn("pages/[id]/view.tsx", mmd["content"])
        self.assertIn("subgraph sg_", mmd["content"])

        dot = cli_json(
            "export", "--root", str(self.root), "--format", "dot",
            "--level", "dir", "--direction", "TD", "--output", ".aoci/arch.dot",
        )
        self.assertTrue(dot["content"].startswith("digraph aoci {\n"))
        self.assertIn("rankdir=TB;", dot["content"])
        self.assertTrue((self.root / ".aoci" / "arch.dot").is_file())
        self.assertEqual(dot["nodes"], 3)

        dom_mmd = cli_json(
            "export", "--root", str(self.root), "--format", "mermaid",
            "--level", "domain", "--focus", "模块/入口.py", "--depth", "1",
        )
        self.assertEqual(dom_mmd["nodes"], 3)
        self.assertNotIn("core/isolated.py", dom_mmd["content"])

        is_err, mcp_txt = aoci_mcp.call_tool(
            "aoci_export",
            {"root": str(self.root), "format": "mermaid", "level": "file", "focus": "core/service.py"},
        )
        self.assertFalse(is_err, mcp_txt)
        mcp_res = json.loads(mcp_txt)
        self.assertEqual(mcp_res["nodes"], 3)
        self.assertEqual(mcp_res["edges"], 3)
        self.assertNotIn("core/isolated.py", mcp_res["content"])

    def test_m2_pool_export_import_gz_poison_rejection_and_cross_repo_restore(self):
        import aoci_mcp

        body_a = "def billing_calc(amount):\n    return round(amount * 1.07, 2)\n\n\nTAX = 0.07\n"
        self.write("src/billing.py", body_a)
        self.init()
        self.assertEqual(
            self.upsert("src/billing.py[DX7T]: F:computes invoice totals with sales tax | R:- | A:billing_calc | S:round half even")[0],
            0,
        )

        st = cli_json("pool", "stats", "--root", str(self.root))
        self.assertTrue(st["exists"])
        self.assertGreaterEqual(st["by_path_sha_count"], 1)

        bundle_gz = self.root / ".aoci" / "team_pool.json.gz"
        exp = cli_json("pool", "export", "--root", str(self.root), "--output", ".aoci/team_pool.json.gz")
        self.assertTrue(exp["gzip"])
        self.assertEqual(exp["exported_by_path_sha"], 1)
        self.assertTrue(bundle_gz.is_file())

        # Create a poisoned copy containing valid + traversal + TODO + bad-sha + wrong-path rows
        raw_bundle = json.loads(gzip.decompress(bundle_gz.read_bytes()).decode("utf-8"))
        good_sha = raw_bundle["by_path_sha"][0]["sha256"]
        raw_bundle["by_path_sha"].extend(
            [
                {"path": "../evil.py", "sha256": good_sha, "entry": "../evil.py[CX3T]: F:bad traversal | R:- | A:- | S:-"},
                {"path": "src/billing.py", "sha256": good_sha, "entry": "src/billing.py[CX3T]: F:<TODO:unfilled> | R:- | A:- | S:-"},
                {"path": "src/billing.py", "sha256": "not-a-sha", "entry": "src/billing.py[CX3T]: F:invalid sha | R:- | A:- | S:-"},
                {"path": "src/other.py", "sha256": good_sha, "entry": "src/mismatch.py[CX3T]: F:path mismatch | R:- | A:- | S:-"},
            ]
        )
        poison_json = self.root / ".aoci" / "poison.json"
        poison_json.write_text(json.dumps(raw_bundle), encoding="utf-8")

        # Fresh second repo gets 0-token restore solely by importing team bundle!
        repo2 = Path(self._tmp.name) / "repo2"
        repo2.mkdir()
        (repo2 / "src").mkdir()
        (repo2 / "src" / "billing.py").write_text(body_a, encoding="utf-8")
        self.assertEqual(cli("init", "--root", str(repo2))[0], 0)

        imp = cli_json("pool", "import", "--root", str(repo2), "--input", str(poison_json))
        self.assertEqual(imp["imported_by_path_sha"], 1)
        self.assertEqual(imp["rejected"], 4)

        res = cli_json("restore", "--root", str(repo2))
        self.assertEqual(res["restored"], ["src/billing.py"])
        self.assertEqual(cli("verify", "--root", str(repo2))[0], 0)

        # Verify `restore` also populated `sig_hash` into repo2's baseline
        b2 = json.loads((repo2 / ".aoci" / "baseline.json").read_text(encoding="utf-8"))
        self.assertIn("sig_hash", b2["files"]["src/billing.py"])

        # MCP `aoci_pool` stats works cleanly
        is_err, mcp_txt = aoci_mcp.call_tool("aoci_pool", {"root": str(repo2), "action": "stats"})
        self.assertFalse(is_err, mcp_txt)
        self.assertGreaterEqual(json.loads(mcp_txt)["by_path_sha_count"], 1)

    def test_m2_install_git_hooks_preserves_existing_custom_hooks_and_core_hookspath(self):
        self.write("app.py", "def run():\n    return 1\n\n\nFLAG = True\n")
        self._git("add", "app.py")
        self._git("commit", "-q", "-m", "base")
        self.init()

        hooks_dir = self.root / ".git" / "hooks"
        existing_pc = hooks_dir / "post-checkout"
        existing_pc.write_text("#!/bin/sh\necho user-post-checkout\n", encoding="utf-8")

        ins = cli_json(
            "install-git-hooks", "--root", str(self.root),
            "--with-pre-commit", "--pre-commit-verify",
        )
        self.assertTrue(ins["ok"])
        self.assertEqual(ins["hooks"]["post-checkout"], "appended")
        self.assertEqual(ins["hooks"]["post-merge"], "created")
        self.assertEqual(ins["hooks"]["pre-commit"], "created")

        pc_txt = existing_pc.read_text(encoding="utf-8")
        self.assertIn("echo user-post-checkout", pc_txt)
        self.assertIn(aoci_index.GIT_HOOK_BEGIN, pc_txt)
        self.assertIn("restore --root", pc_txt)

        # Re-installing is strictly idempotent (does not duplicate managed block)
        ins2 = cli_json("install-git-hooks", "--root", str(self.root), "--with-pre-commit")
        self.assertEqual(ins2["hooks"]["post-checkout"], "updated")
        self.assertEqual(existing_pc.read_text(encoding="utf-8").count(aoci_index.GIT_HOOK_BEGIN), 1)

        # Custom `core.hooksPath` is honored for both install and uninstall
        custom_hooks = self.root / ".githooks"
        custom_hooks.mkdir()
        self._git("config", "core.hooksPath", ".githooks")
        ins3 = cli_json("install-git-hooks", "--root", str(self.root), "--with-pre-commit")
        self.assertEqual(Path(ins3["hooks_dir"]).resolve(), custom_hooks.resolve())
        self.assertTrue((custom_hooks / "post-checkout").is_file())
        self.assertTrue((custom_hooks / "pre-commit").is_file())

        un3 = cli_json("install-git-hooks", "--root", str(self.root), "--uninstall")
        self.assertEqual(un3["hooks"]["post-checkout"], "removed")
        self.assertFalse((custom_hooks / "post-checkout").exists())
        self.assertFalse((custom_hooks / "pre-commit").exists())

        # Unset `core.hooksPath` and uninstall from `.git/hooks` -> preserves pre-existing post-checkout
        self._git("config", "--unset", "core.hooksPath")
        un_def = cli_json("install-git-hooks", "--root", str(self.root), "--uninstall")
        self.assertEqual(un_def["hooks"]["post-checkout"], "cleaned")
        self.assertEqual(un_def["hooks"]["pre-commit"], "removed")
        self.assertTrue(existing_pc.is_file())
        self.assertIn("echo user-post-checkout", existing_pc.read_text(encoding="utf-8"))
        self.assertNotIn(aoci_index.GIT_HOOK_BEGIN, existing_pc.read_text(encoding="utf-8"))
        self.assertFalse((hooks_dir / "post-merge").exists())


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):
    def test_restore_identical_cross_path_files_never_auto_copied(self):
        identical_policy = (
            "from . import config\n\n"
            "def evaluate(val):\n"
            "    return val >= config.THRESHOLD\n"
        )
        for region, thr in (("east", 10), ("west", 20), ("north", 30)):
            self.write(f"{region}/config.py", f"THRESHOLD = {thr}\n")
            self.write(f"{region}/policy.py", identical_policy)
        self.init()
        self.assertEqual(
            self.upsert(
                "east/config.py[CG4T]: F:holds east threshold constant | R:- | A:THRESHOLD<-east/policy.py | S:-",
                "east/policy.py[DX5T]: F:checks east thresholds | R:east/config.py | A:evaluate | S:-",
                "west/config.py[CG4T]: F:holds west threshold constant | R:- | A:THRESHOLD<-west/policy.py | S:-",
                "west/policy.py[DX5T]: F:checks west thresholds | R:west/config.py | A:evaluate | S:-",
                "north/config.py[CG4T]: F:holds north threshold constant | R:- | A:- | S:-",
            )[0],
            0,
        )

        # Default restore and even --allow-sha-fallback must NEVER auto-copy west/policy.py onto north/policy.py
        for extra in ([], ["--allow-sha-fallback"]):
            res = cli_json("restore", "--root", str(self.root), *extra)
            self.assertEqual(res["restored"], [])
            self.assertEqual(res["restored_by_sha"], [])
            self.assertEqual([c["path"] for c in res["sha_candidates"]], ["north/policy.py"])
            self.assertTrue(res["sha_candidates"][0]["orig_exists"])
            self.assertEqual(res["sha_candidates"][0]["reason"], "origin-still-exists")
            self.assertNotIn("north/policy.py[", self.code_text())

        # SessionStart hook must also report 0 restorable entries (never promise cross-path copies)
        start = hook("session-start", {"cwd": str(self.root), "session_id": "s-identical", "hook_event_name": "SessionStart"})
        self.assertNotIn("restorable with 0 tokens", start["hookSpecificOutput"]["additionalContext"])

        # `verify` MUST catch that north/policy.py is unindexed instead of passing with 0 warnings
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 1)

    def test_scan_affected_neighbors_includes_explicit_a_mentions_and_upsert_warns(self):
        self.write("lib.py", "def foo():\n    return 1\n")
        self.write("app.py", "import lib\ndef main():\n    return lib.foo()\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "lib.py[CX5T]: F:provides shared foo helper | R:- | A:foo<-app.py | S:-",
                "app.py[EX6T]: F:invokes foo helper from main | R:lib.py | A:main | S:-",
            )[0],
            0,
        )

        # `app.py` stops calling `lib.py`, while `lib.py` still claims `A:foo<-app.py`
        self.write("app.py", "def main():\n    return 42\n")
        scan_data = cli_json("scan", "--root", str(self.root))
        self.assertIn("lib.py", scan_data["affected_neighbors"].get("app.py", []))

        # Upserting `app.py` without `R:lib.py` warns that `lib.py` still mentions `app.py` in `A:`
        up = cli_json(
            "upsert", "--root", str(self.root),
            "--entry", "app.py[EX6T]: F:returns constant 42 from main | R:- | A:main | S:-",
        )
        self.assertTrue(any("lib.py" in w and "app.py" in w for w in up["warnings"]), up["warnings"])

    def test_install_refuses_unmanifested_nonempty_dir_and_preserves_second_mcp_on_plugin_uninstall(self):
        proj = Path(self._tmp.name) / "safe_proj"
        home = Path(self._tmp.name) / "safe_home"
        proj.mkdir()
        home.mkdir()

        # Repro A: manual install without `.aoci-install.json` + user notes must NOT be wiped without --force
        manual_dir = proj / ".claude" / "skills" / "aoci-index"
        manual_dir.mkdir(parents=True)
        (manual_dir / "SKILL.md").write_text("# manual skill\n", encoding="utf-8")
        (manual_dir / "user_notes.md").write_text("# keep my notes\n", encoding="utf-8")

        r_abort = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_abort.returncode, 1)
        self.assertIn("ownership cannot be verified", r_abort.stderr)
        self.assertTrue((manual_dir / "user_notes.md").is_file())

        r_force = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home), "--force"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_force.returncode, 0, r_force.stdout + r_force.stderr)
        self.assertFalse((manual_dir / "user_notes.md").exists())

        # Repro B: Antigravity plugin with user-added 2nd MCP server, sibling hook, custom rule, and note
        r_plug = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home),
                "--with-hooks", "--with-rules", "--with-mcp", "--as-plugin",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_plug.returncode, 0, r_plug.stdout + r_plug.stderr)
        pdir = proj / ".agents" / "plugins" / "aoci-index"

        mcp_path = pdir / "mcp_config.json"
        mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
        mcp_data["mcpServers"]["second-user-mcp"] = {"command": "node", "args": ["srv.js"]}
        mcp_path.write_text(json.dumps(mcp_data, indent=2), encoding="utf-8")

        hooks_path = pdir / "hooks.json"
        hooks_data = json.loads(hooks_path.read_text(encoding="utf-8"))
        hooks_data["my-custom-hook"] = {"SessionStart": [{"type": "command", "command": "echo hi"}]}
        hooks_path.write_text(json.dumps(hooks_data, indent=2), encoding="utf-8")

        agents_md = pdir / "rules" / "AGENTS.md"
        agents_md.write_text("# My Custom Rule\n\n" + agents_md.read_text(encoding="utf-8"), encoding="utf-8")

        # Uninstall MUST strip only AOCI keys and preserve the 2nd MCP server, sibling hook, and custom rule
        r_un = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home), "--as-plugin", "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
        self.assertFalse((pdir / "skills" / "aoci-index").exists())
        self.assertTrue((pdir / "plugin.json").is_file())
        after_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index", after_mcp.get("mcpServers", {}))
        self.assertIn("second-user-mcp", after_mcp.get("mcpServers", {}))
        after_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index-guard", after_hooks)
        self.assertIn("my-custom-hook", after_hooks)
        self.assertIn("# My Custom Rule", agents_md.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index", agents_md.read_text(encoding="utf-8"))

        # Reinstall plugin cleanly in a 2nd project and verify uninstall WITHOUT `--as-plugin` also preserves 2nd MCP server
        proj2 = Path(self._tmp.name) / "safe_proj2"
        proj2.mkdir()
        r_plug2 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj2), "--home-dir", str(home),
                "--with-hooks", "--with-rules", "--with-mcp", "--as-plugin",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_plug2.returncode, 0, r_plug2.stdout + r_plug2.stderr)
        pdir2 = proj2 / ".agents" / "plugins" / "aoci-index"
        mcp_path2 = pdir2 / "mcp_config.json"
        mcp_data2 = json.loads(mcp_path2.read_text(encoding="utf-8"))
        mcp_data2["mcpServers"]["second-user-mcp"] = {"command": "node", "args": ["srv.js"]}
        mcp_path2.write_text(json.dumps(mcp_data2, indent=2), encoding="utf-8")
        r_un2 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj2), "--home-dir", str(home), "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_un2.returncode, 0, r_un2.stdout + r_un2.stderr)
        after_mcp2 = json.loads(mcp_path2.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index", after_mcp2.get("mcpServers", {}))
        self.assertIn("second-user-mcp", after_mcp2.get("mcpServers", {}))

    def test_cold_bootstrap_scan_fp_cache_zero_rehash_and_stable_batch_plan(self):
        import contextlib
        import io

        def _run_inproc(*argv: str) -> dict:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = aoci_index.main([*argv, "--json"])
            self.assertEqual(rc, 0, f"{argv}: {buf.getvalue()}")
            return json.loads(buf.getvalue())

        names = ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"]
        for name in names:
            # Create files cleanly with aged=False (testing <3s stabilization as well as aged files)
            self.write(name, f"def fn_{name[0]}():\n    return '{name}'\n", aged=False)
        time.sleep(0.03)
        self.init()

        # Verify `baseline.json` stays empty of unindexed files while `scan_fps.json` caches them
        base0 = json.loads((self.root / ".aoci" / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(base0["files"], {})
        scan_fp_path = self.root / ".aoci" / ".cache" / "scan_fps.json"
        plan_path = self.root / ".aoci" / ".cache" / "batch_plan.json"
        self.assertTrue(scan_fp_path.is_file())

        # `scan --check-only --batches 2` must NOT write `batch_plan.json`
        scan_ro = cli_json("scan", "--root", str(self.root), "--batches", "2", "--check-only", "--fast")
        self.assertEqual([[it["path"] for it in b] for b in scan_ro["batches"]], [["a.py", "b.py"], ["c.py", "d.py"], ["e.py", "f.py"]])
        self.assertFalse(plan_path.exists())

        # `verify` stays strictly read-only (does not create scan_fps.json or batch_plan.json when absent)
        scan_fp_path.unlink()
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 1)
        self.assertFalse(scan_fp_path.exists())
        self.assertFalse(plan_path.exists())

        # `scan --batches 2 --fast` repopulates `scan_fps.json` and persists `batch_plan.json`
        scan_plan = cli_json("scan", "--root", str(self.root), "--batches", "2", "--fast")
        self.assertTrue(scan_fp_path.is_file())
        self.assertTrue(plan_path.is_file())
        self.assertEqual(scan_plan["batch_plan"]["remaining_batches"], [1, 2, 3])

        # 1st `scaffold --batch 1` runs IN-PROCESS with ZERO file hashes, populating `outline_cache.json`
        with mock.patch.object(aoci_index, "_fingerprint_bytes_norm", side_effect=AssertionError("must reuse scan_fps.json")):
            sc1 = _run_inproc("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc1["entries"]], ["a.py", "b.py"])

        # 2nd `scaffold --batch 2` before upsert -> 0 hashes & 0 outline extractions
        with mock.patch.object(aoci_index, "_fingerprint_bytes_norm", side_effect=AssertionError("must not re-hash")):
            with mock.patch.object(aoci_index, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
                sc2_before = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2", "--batch-size", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc2_before["entries"]], ["c.py", "d.py"])

        # Upsert Batch 1 (a.py, b.py)
        self.assertEqual(
            self.upsert(
                "a.py[CX4T]: F:returns filename string a | R:- | A:fn_a | S:-",
                "b.py[CX4T]: F:returns filename string b | R:- | A:fn_b | S:-",
            )[0],
            0,
        )

        # After upserting Batch 1 (`a.py`, `b.py` are still <3s old so `baseline.json` guards them as racy):
        # requesting Batch 2 STILL returns [c.py, d.py] (no drift!) and incurs ZERO hashes on unindexed [c..f] & 0 outlines!
        orig_fp_norm = aoci_index._fingerprint_bytes_norm
        fp_headers: list = []

        def _track_fp(norm, st_arg):
            fp_headers.append(norm.decode("utf-8", errors="replace").splitlines()[0])
            return orig_fp_norm(norm, st_arg)

        with mock.patch.object(aoci_index, "_fingerprint_bytes_norm", side_effect=_track_fp):
            with mock.patch.object(aoci_index, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
                sc2_after = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc2_after["entries"]], ["c.py", "d.py"])
        for header in fp_headers:
            self.assertIn(header, ("def fn_a():", "def fn_b():"), f"unexpected re-hash of unindexed file: {header}")

        # Once Batch 1 baselined files are >3s old and acknowledged in baseline, total hashes across ALL 6 files drop to 0
        age_file(self.root / "a.py")
        age_file(self.root / "b.py")
        cli_json("ack", "--root", str(self.root), "a.py", "b.py")
        orig_extract = aoci_index.extract_file_outline
        extracted_paths: list = []

        def _track_outline(path_p, rel_p, *rest, **kw):
            extracted_paths.append(rel_p)
            return orig_extract(path_p, rel_p, *rest, **kw)

        with mock.patch.object(aoci_index, "_fingerprint_bytes_norm", side_effect=AssertionError("must not re-hash")):
            with mock.patch.object(aoci_index, "extract_file_outline", side_effect=_track_outline):
                sc2_aged = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc2_aged["entries"]], ["c.py", "d.py"])
        self.assertTrue(all(p in ("a.py", "b.py") for p in extracted_paths), extracted_paths)

        # Repeated Batch 2 now hits 0 hashes AND 0 outline extractions across all 6 files
        with mock.patch.object(aoci_index, "_fingerprint_bytes_norm", side_effect=AssertionError("must not re-hash")):
            with mock.patch.object(aoci_index, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
                sc2_warm = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc2_warm["entries"]], ["c.py", "d.py"])

        # Asking for completed Batch 1 reports completed without shifting Batch 2 into slot 1
        sc1_done = cli_json("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "2")
        self.assertEqual(sc1_done["count"], 0)
        self.assertEqual(sc1_done["status"], "completed")
        self.assertEqual(sc1_done["remaining_batches"], [2, 3])

        # Modify `d.py` to 60 lines: ONLY `d.py` is re-hashed (call_count == 1) and shows updated size tier `S`
        self.write("d.py", "def fn_d():\n" + "".join(f"    x{i} = {i}\n" for i in range(60)), aged=True)

        def _counting_fp(norm, st):
            return orig_fp_norm(norm, st)

        with mock.patch.object(aoci_index, "_fingerprint_bytes_norm", side_effect=_counting_fp) as fp_spy:
            sc2_mod = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2", "--batch-size", "2")
        self.assertEqual(fp_spy.call_count, 1)
        by_p = {ln.split("[", 1)[0]: ln for ln in sc2_mod["entries"]}
        self.assertEqual(sorted(by_p.keys()), ["c.py", "d.py"])
        self.assertTrue(by_p["d.py"].startswith("d.py[") and "S]:" in by_p["d.py"])

        # `--reset-batches` repacks remaining 4 files cleanly into 2 batches `[c, d], [e, f]`
        sc_reset = cli_json("scaffold", "--root", str(self.root), "--batch", "2", "--batch-size", "2", "--reset-batches")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc_reset["entries"]], ["e.py", "f.py"])

        # Upsert remaining files -> both `batch_plan.json` and `scan_fps.json` auto-clean
        self.assertEqual(
            self.upsert(
                "c.py[CX4T]: F:returns filename string c | R:- | A:fn_c | S:-",
                "d.py[CX4S]: F:computes sixty sample bindings | R:- | A:fn_d | S:-",
                "e.py[CX4T]: F:returns filename string e | R:- | A:fn_e | S:-",
                "f.py[CX4T]: F:returns filename string f | R:- | A:fn_f | S:-",
            )[0],
            0,
        )
        self.assertEqual(cli("verify", "--root", str(self.root))[0], 0)
        self.assertFalse(plan_path.exists())
        self.assertFalse(scan_fp_path.exists())

    def test_bilingual_glossary_bm25_search_on_builtin_examples_and_mcp(self):
        import aoci_mcp

        ex_aoci = self.root / ".aoci"
        shutil.copytree(SKILL_DIR / "examples", ex_aoci)

        # Chinese query "密钥轮换" hits `src/auth/jwt.py` #1 via bilingual glossary in `examples/aoci.meta.txt`
        q_rot = cli_json("query", "--root", str(self.root), "--search", "密钥轮换")
        self.assertGreaterEqual(q_rot["count"], 1)
        self.assertEqual(q_rot["scores"][0]["path"], "src/auth/jwt.py")
        self.assertIn("rotation", q_rot["scores"][0]["matched_terms"])
        self.assertTrue(any("密钥轮换 -> key rotation" in g for g in q_rot["scores"][0].get("matched_glossary", [])))
        self.assertTrue(q_rot.get("glossary_expansions"))

        # Chinese query "预留库存" hits `src/order/repo.py` via `reserve stock` / `inventory reservation`
        q_inv = cli_json("query", "--root", str(self.root), "--search", "预留库存")
        self.assertGreaterEqual(q_inv["count"], 1)
        self.assertEqual(q_inv["scores"][0]["path"], "src/order/repo.py")
        self.assertIn("reserve", q_inv["scores"][0]["matched_terms"])
        self.assertTrue(any("预留库存 ->" in g for g in q_inv["scores"][0].get("matched_glossary", [])))

        # MCP `aoci_query` uses cached BM25Index + glossary AND invalidates automatically when `.aoci/glossary.txt` changes
        is_err1, res1 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "密钥轮换"})
        self.assertFalse(is_err1, res1)
        d1 = json.loads(res1)
        self.assertEqual(d1["scores"][0]["path"], "src/auth/jwt.py")

        # Add a standalone `.aoci/glossary.txt` entry + a Chinese entry and test English -> Chinese reverse lookup
        (ex_aoci / "aoci.code.txt").write_text(
            (ex_aoci / "aoci.code.txt").read_text(encoding="utf-8")
            + "src/limit.py[MA6T]: F:实现分布式网关限流与令牌桶控制 | R:- | A:allowReq | S:原子Lua脚本\n",
            encoding="utf-8",
        )
        (ex_aoci / "glossary.txt").write_text("# Custom terms\nrate limit = 限流\n", encoding="utf-8")
        is_err2, res2 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "rate limit"})
        self.assertFalse(is_err2, res2)
        d2 = json.loads(res2)
        self.assertGreaterEqual(d2["count"], 1)
        self.assertEqual(d2["scores"][0]["path"], "src/limit.py")
        self.assertTrue(any("rate limit -> 限流" in g for g in d2["scores"][0].get("matched_glossary", [])))

    def test_go_package_level_audit_5_file_pkg_no_false_missing_or_phantom(self):
        self.write("go.mod", "module example.com/demo\n")
        for i in range(1, 6):
            self.write(
                f"pkg/f{i}.go",
                f"package pkg\n\nfunc Func{i}() int {{ return {i} }}\n",
            )
        self.write("pkg/f1_test.go", "package pkg\nimport \"testing\"\nfunc TestF1(t *testing.T) {}\n")
        self.write(
            "cmd/app/main.go",
            'package main\n\nimport (\n\t"fmt"\n\t"example.com/demo/pkg"\n)\n\nfunc main() { fmt.Println(pkg.Func4()) }\n',
        )
        self.write(
            "cmd/app/unlinked.go",
            'package main\n\nimport "example.com/demo/pkg"\n\nfunc helper() int { return pkg.Func1() }\n',
        )
        self.write("other/extra.go", "package other\n\nfunc Extra() int { return 9 }\n")
        self.init()
        entries = [
            "pkg/f1.go[DX3T]: F:implements package part one | R:pkg/f4.go | A:Func1 | S:-",
            "pkg/f2.go[DX3T]: F:implements package part two | R:- | A:Func2 | S:-",
            "pkg/f3.go[DX3T]: F:implements package part three | R:- | A:Func3 | S:-",
            "pkg/f4.go[DX3T]: F:implements package part four | R:- | A:Func4 | S:-",
            "pkg/f5.go[DX3T]: F:implements package part five | R:- | A:Func5 | S:-",
            "pkg/f1_test.go[CX2T]: F:tests package func one | R:pkg/f1.go | A:TestF1 | S:-",
            "cmd/app/main.go[EX4T]: F:runs application entrypoint | R:pkg/f4.go | A:main | S:-",
            "cmd/app/unlinked.go[EX3T]: F:runs secondary app helper | R:- | A:helper | S:-",
            "other/extra.go[CX2T]: F:provides unrelated helper | R:- | A:Extra | S:-",
        ]
        self.assertEqual(self.upsert(*entries)[0], 0)

        aud = cli_json("audit", "--root", str(self.root))
        missing_pairs = [(x["from"], x["to"]) for x in aud["missing_static_deps"]]
        phantom_pairs = [(x["from"], x["to"]) for x in aud["phantom_fras_deps"]]

        # `cmd/app/main.go` cites 4th file `pkg/f4.go` -> satisfies `pkg` import with zero missing/phantom
        self.assertFalse(any(src == "cmd/app/main.go" for src, _ in missing_pairs), missing_pairs)
        self.assertFalse(any(src == "cmd/app/main.go" for src, _ in phantom_pairs), phantom_pairs)
        # Same-package sibling `pkg/f1.go` -> `pkg/f4.go` is NOT flagged as phantom
        self.assertNotIn(("pkg/f1.go", "pkg/f4.go"), phantom_pairs)
        # `cmd/app/unlinked.go` omits all files of imported `pkg` -> exactly one missing-static-dep (`pkg/f1.go`)
        unlinked_missing = [x for x in aud["missing_static_deps"] if x["from"] == "cmd/app/unlinked.go"]
        self.assertEqual(unlinked_missing, [{"from": "cmd/app/unlinked.go", "to": "pkg/f1.go", "code": "missing-static-dep"}])

        # Citing a `.go` file from an unimported package (`other/extra.go`) still triggers `phantom-fras-dep`
        self.assertEqual(
            self.upsert("cmd/app/main.go[EX4T]: F:runs application entrypoint | R:pkg/f4.go,other/extra.go | A:main | S:-")[0],
            0,
        )
        aud2 = cli_json("audit", "--root", str(self.root))
        self.assertIn(("cmd/app/main.go", "other/extra.go"), [(x["from"], x["to"]) for x in aud2["phantom_fras_deps"]])

    def test_global_claude_hook_spaced_interpreter_and_spaced_home_cmd_c(self):
        self.write("app.py", "x = 1\n")
        self.init()
        self.upsert("app.py[CX2T]: F:defines app constant x | R:- | A:- | S:-")

        tmp_base = Path(self._tmp.name)
        home = tmp_base / "space home"
        home.mkdir()
        py_dir = tmp_base / "py dir"
        base_exe = getattr(sys, "_base_executable", "") or sys.executable
        if not Path(base_exe).is_file():
            base_exe = sys.executable
        real_py_dir = Path(base_exe).parent
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(py_dir), str(real_py_dir)], check=True, capture_output=True)
        else:
            os.symlink(str(real_py_dir), str(py_dir))
        try:
            spaced_py = py_dir / Path(base_exe).name
            quoted_py = f'"{spaced_py.as_posix()}"'
            r = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "claude-code",
                    "--scope",
                    "global",
                    "--home-dir",
                    str(home),
                    "--python-cmd",
                    quoted_py,
                    "--with-hooks",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            settings_path = home / ".claude" / "settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            start_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertTrue(start_cmd.startswith(quoted_py), start_cmd)
            self.assertIn('-c "import runpy;runpy.run_path(', start_cmd)
            self.assertIn("aoci_hook.py", start_cmd)

            shell_proc = subprocess.run(
                start_cmd,
                shell=True,
                cwd=str(self.root),
                input=json.dumps({"cwd": str(self.root), "session_id": "win-sp", "hook_event_name": "SessionStart"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(shell_proc.returncode, 0, shell_proc.stderr)
            parsed = json.loads(shell_proc.stdout)
            self.assertIn("aoci_index.py", parsed["hookSpecificOutput"]["additionalContext"])

            # Uninstall still matches and removes the `-c` hook cleanly
            r_un = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "claude-code",
                    "--scope",
                    "global",
                    "--home-dir",
                    str(home),
                    "--uninstall",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
            after_un = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertNotIn("hooks", after_un)

            # Unquoted `--python-cmd` keeps direct `"<script>"` invocation
            r_unq = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "claude-code",
                    "--scope",
                    "global",
                    "--home-dir",
                    str(home),
                    "--python-cmd",
                    "python3",
                    "--with-hooks",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_unq.returncode, 0, r_unq.stdout + r_unq.stderr)
            unq_cmd = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertTrue(unq_cmd.startswith('python3 "'), unq_cmd)
        finally:
            if os.name == "nt" and py_dir.exists():
                subprocess.run(["cmd", "/c", "rmdir", str(py_dir)], capture_output=True)
            elif py_dir.is_symlink():
                py_dir.unlink()

    def test_mcp_resource_templates_list_and_thread_safe_cli_capture(self):
        import concurrent.futures
        import io
        import aoci_mcp

        self.write("app.py", "def run():\n    return 1\n")
        self.init()
        self.upsert("app.py[CX2T]: F:runs application entrypoint | R:- | A:run | S:-")

        in_buf = io.BytesIO((json.dumps({"jsonrpc": "2.0", "id": 11, "method": "resources/templates/list"}) + "\n").encode("utf-8"))
        out_buf = io.BytesIO()
        aoci_mcp.serve_stdio(in_buf, out_buf)
        resp = json.loads(out_buf.getvalue().decode("utf-8"))
        self.assertEqual(resp["id"], 11)
        templates = resp["result"]["resourceTemplates"]
        uris = {t["uriTemplate"] for t in templates}
        self.assertIn("aoci://shards/{shard}", uris)
        self.assertIn("aoci://{volume}?root={root}", uris)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(aoci_mcp._run_cli_capture, ["verify", "--root", str(self.root), "--json"]) for _ in range(4)]
            for fut in futs:
                rc, out = fut.result()
                self.assertEqual(rc, 0, out)
                self.assertTrue(json.loads(out)["ok"])

    def test_python_type_checking_try_imports_and_class_annassign_sig_hash(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/types.py", "class Token:\n    pass\n")
        self.write("pkg/fast.py", "def parse():\n    return 1\n")
        self.write("pkg/slow.py", "def parse():\n    return 2\n")
        self.write(
            "pkg/models.py",
            "from __future__ import annotations\n"
            "from dataclasses import dataclass\n"
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg import types\n"
            "try:\n"
            "    from pkg import fast\n"
            "except ImportError:\n"
            "    from pkg import slow\n\n"
            "__all__ = ['Item']\n\n"
            "@dataclass\n"
            "class Item:\n"
            "    id: int\n"
            "    label: str\n"
            "    _cache: int = 0\n\n"
            "    def render(self) -> str:\n"
            "        return self.label\n",
        )
        self.init()

        outline = aoci_index.extract_file_outline(self.root / "pkg/models.py", "pkg/models.py")
        self.assertIn("pkg", outline["imports"])
        self.assertIn("pkg.types", outline["py_from_members"])
        self.assertIn("pkg.fast", outline["py_from_members"])
        self.assertIn("pkg.slow", outline["py_from_members"])

        self.assertEqual(
            self.upsert(
                "pkg/__init__.py[CX1T]: F:marks package root | R:- | A:- | S:-",
                "pkg/types.py[DX2T]: F:defines token type | R:- | A:Token | S:-",
                "pkg/fast.py[CX2T]: F:provides fast parser | R:- | A:parse | S:-",
                "pkg/slow.py[CX2T]: F:provides fallback parser | R:- | A:parse | S:-",
                "pkg/models.py[DX5T]: F:defines item dataclass model | R:pkg/types.py,pkg/fast.py,pkg/slow.py | A:Item | S:-",
            )[0],
            0,
        )

        # Private `_cache` type or method body edit stays `impl_only`
        base_src = (self.root / "pkg/models.py").read_text(encoding="utf-8")
        self.write("pkg/models.py", base_src.replace("_cache: int = 0", "_cache: str = ''").replace("return self.label", "return self.label.strip()"))
        scan1 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p1 = {it["path"]: it for it in scan1["modified"]}
        self.assertEqual(by_p1["pkg/models.py"]["change_kind"], "impl_only")

        # Public dataclass `AnnAssign` change triggers `contract_changed`
        self.write("pkg/models.py", base_src.replace("label: str", "label: int"))
        scan2 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p2 = {it["path"]: it for it in scan2["modified"]}
        self.assertEqual(by_p2["pkg/models.py"]["change_kind"], "contract_changed")

        # `__all__` export list change triggers `contract_changed`
        self.write("pkg/models.py", base_src.replace("__all__ = ['Item']", "__all__ = ['Item', 'extra']"))
        scan3 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p3 = {it["path"]: it for it in scan3["modified"]}
        self.assertEqual(by_p3["pkg/models.py"]["change_kind"], "contract_changed")

        # `if TYPE_CHECKING:` import change triggers `contract_changed`
        self.write("pkg/models.py", base_src.replace("from pkg import types", "from pkg import types, extra_types"))
        scan4 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p4 = {it["path"]: it for it in scan4["modified"]}
        self.assertEqual(by_p4["pkg/models.py"]["change_kind"], "contract_changed")

    def test_single_read_sig_hash_on_scan_and_transient_key_stripping(self):
        import contextlib
        import io

        self.write("src/mod.py", "def compute(x: int) -> int:\n    return x + 1\n")
        self.write("src/other.py", "def run() -> int:\n    return 1\n")
        self.write("src/unindexed.py", "def pending() -> int:\n    return 0\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/mod.py[CX2T]: F:computes incremented integer | R:- | A:compute | S:-",
                "src/other.py[CX2T]: F:runs secondary worker | R:- | A:run | S:-",
            )[0],
            0,
        )

        # Modify both tracked files (one impl_only, one contract_changed)
        self.write("src/mod.py", "def compute(x: int) -> int:\n    return x + 2\n")
        self.write("src/other.py", "def run(flag: bool = False) -> int:\n    return 2 if flag else 1\n")

        # `scan` must compute `sig_hash` inside `inspect_and_fingerprint_one` on the single read (0 `compute_signature_hash` calls)
        with mock.patch.object(aoci_index, "compute_signature_hash", side_effect=AssertionError("must not re-read modified files")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = aoci_index.main(["scan", "--root", str(self.root), "--fast", "--json"])
            self.assertEqual(rc, 0)
        scan_data = json.loads(buf.getvalue())
        by_p = {it["path"]: it for it in scan_data["drift"]["modified"]}
        self.assertEqual(by_p["src/mod.py"]["change_kind"], "impl_only")
        self.assertEqual(by_p["src/other.py"]["change_kind"], "contract_changed")

        # Transient `_cur_sig_hash` must never be persisted to baseline.json or scan_fps.json
        base_files = json.loads((self.root / ".aoci" / "baseline.json").read_text(encoding="utf-8"))["files"]
        for fp_rec in base_files.values():
            self.assertFalse(any(k.startswith("_") for k in fp_rec), fp_rec)
        scan_fp_path = self.root / ".aoci" / ".cache" / "scan_fps.json"
        if scan_fp_path.is_file():
            sfp_files = json.loads(scan_fp_path.read_text(encoding="utf-8"))["files"]
            for fp_rec in sfp_files.values():
                self.assertFalse(any(k.startswith("_") for k in fp_rec), fp_rec)

        # IgnoreMatcher combined-regex fast path matches non-negated rules identically
        fast_m = aoci_index.IgnoreMatcher(["generated/", "*.pb.go", "/rootonly.txt"])
        self.assertTrue(fast_m.is_ignored("a/generated/x.py"))
        self.assertTrue(fast_m.is_ignored("api/foo.pb.go"))
        self.assertTrue(fast_m.is_ignored("rootonly.txt"))
        self.assertFalse(fast_m.is_ignored("sub/rootonly.txt"))
        self.assertFalse(fast_m.is_ignored("src/mod.py"))

    def test_post_checkout_hook_skips_file_checkout_arg3_zero(self):
        self.write("app.py", "def run():\n    return 1\n\n\nFLAG = True\n")
        self.init()
        ins = cli_json("install-git-hooks", "--root", str(self.root))
        self.assertTrue(ins["ok"])
        pc_path = self.root / ".git" / "hooks" / "post-checkout"
        pm_path = self.root / ".git" / "hooks" / "post-merge"
        pc_txt = pc_path.read_text(encoding="utf-8")
        pm_txt = pm_path.read_text(encoding="utf-8")
        self.assertIn('${3:-1}" = "1"', pc_txt)
        self.assertNotIn('${3:-1}', pm_txt)

        sh_bin = shutil.which("sh")
        if sh_bin:
            marker = self.root / ".aoci" / "hook_ran.txt"
            spy_script = self.root / "spy_restore.py"
            spy_script.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )
            ginfo = aoci_index.detect_git_info(self.root)
            from aoci_core import githooks as _gh

            _gh.install_git_hooks(self.root, ginfo, python_cmd=sys.executable, script_path=spy_script)
            # `$3 == 0` (single-file `git checkout -- <file>`) MUST skip restore
            r0 = subprocess.run([sh_bin, str(pc_path), "abc", "def", "0"], cwd=str(self.root), capture_output=True, text=True)
            self.assertEqual(r0.returncode, 0, r0.stderr)
            self.assertFalse(marker.exists())
            # `$3 == 1` (branch switch) MUST invoke restore
            r1 = subprocess.run([sh_bin, str(pc_path), "abc", "def", "1"], cwd=str(self.root), capture_output=True, text=True)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertTrue(marker.is_file())

    def test_convergent_stop_hook_retry_on_progress_and_exit_on_stall_or_cap(self):
        self.init()
        base = {"cwd": str(self.root), "session_id": "conv-1"}
        hook("session-start", dict(base, hook_event_name="SessionStart"))
        for name in ("a.py", "b.py", "c.py"):
            self.write(name, f"{name[0]} = 1\n")

        # Stop #1 with 3 unsynced files -> blocks and persists stop_retries=1, last_blocked_count=3
        s1 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False))
        self.assertEqual(s1.get("decision"), "block")
        snap1 = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap1["stop_retries"], 1)
        self.assertEqual(snap1["last_blocked_count"], 3)

        # Agent syncs `a.py` (3 -> 2 unsynced): strict convergence allows ONE extra continuation block
        self.assertEqual(self.upsert("a.py[CX2T]: F:defines module constant a | R:- | A:- | S:-")[0], 0)
        s2 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=True))
        self.assertEqual(s2.get("decision"), "block")
        snap2 = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap2["stop_retries"], 2)
        self.assertEqual(snap2["last_blocked_count"], 2)

        # Even if agent makes progress again (2 -> 1), stop_retries >= 2 exits immediately
        self.assertEqual(self.upsert("b.py[CX2T]: F:defines module constant b | R:- | A:- | S:-")[0], 0)
        s3 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=True))
        self.assertEqual(s3, {})

        # New turn-start resets retry counters; finishing cleanly keeps counters at 0
        hook("turn-start", dict(base, hook_event_name="UserPromptSubmit"))
        snap_reset = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap_reset["stop_retries"], 0)
        self.assertEqual(snap_reset["last_blocked_count"], 0)

        # Missing session_id with continuationActive=True returns {} immediately
        self.assertEqual(
            hook("stop", {"cwd": str(self.root), "hookEventName": "Stop", "continuationActive": True}, host="antigravity"),
            {},
        )

        # Stalled continuation (`cur_unsynced >= last_blocked_count`) returns {} immediately
        stall_base = {"cwd": str(self.root), "conversationId": "conv-stall"}
        hook("session-start", dict(stall_base, hookEventName="SessionStart"), host="antigravity")
        self.write("d.py", "d = 1\n")
        st1 = hook(
            "stop",
            dict(stall_base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False),
            host="antigravity",
        )
        self.assertEqual(st1.get("decision"), "continue")
        st2 = hook(
            "stop",
            dict(stall_base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=True),
            host="antigravity",
        )
        self.assertEqual(st2, {})

        # Clean finish resets `stop_retries` and `last_blocked_count` to 0
        self.assertEqual(
            self.upsert(
                "c.py[CX2T]: F:defines module constant c | R:- | A:- | S:-",
                "d.py[CX2T]: F:defines module constant d | R:- | A:- | S:-",
            )[0],
            0,
        )
        hook("turn-start", dict(base, hook_event_name="UserPromptSubmit"))
        self.assertEqual(hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False)), {})
        snap_clean = json.loads((self.root / ".aoci" / ".sessions" / "conv-1.json").read_text(encoding="utf-8"))
        self.assertEqual(snap_clean["stop_retries"], 0)
        self.assertEqual(snap_clean["last_blocked_count"], 0)

        # Clean finish on `conv-stall` also resets `stop_retries` and `last_blocked_count` without `turn-start`
        self.assertEqual(
            hook(
                "stop",
                dict(stall_base, hookEventName="Stop", terminationReason="NO_TOOL_CALL", continuationActive=False),
                host="antigravity",
            ),
            {},
        )
        stall_clean = json.loads((self.root / ".aoci" / ".sessions" / "conv-stall.json").read_text(encoding="utf-8"))
        self.assertEqual(stall_clean["stop_retries"], 0)
        self.assertEqual(stall_clean["last_blocked_count"], 0)

    def test_version_alignment_across_manifests_cli_and_mcp(self):
        import contextlib
        import io
        import re

        import aoci_core
        import aoci_mcp

        ver = aoci_index.AOCI_VERSION
        self.assertEqual(ver, "1.5.0")
        self.assertEqual(aoci_index.__version__, ver)
        self.assertEqual(aoci_core.__version__, ver)
        self.assertEqual(aoci_mcp.SERVER_VERSION, ver)

        pyproject_text = (SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), ver)

        plugin_data = json.loads((SKILL_DIR / "resources" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_data.get("version"), ver)

        readme_text = (SKILL_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"version-{ver}-", readme_text)
        self.assertNotIn("1.4.0", readme_text)
        self.assertNotIn("1.3.0", readme_text)
        self.assertNotIn("1.2.0", readme_text)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                aoci_index.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(ver, buf.getvalue())


if __name__ == "__main__":
    from test_ext_pack import TaskPackTests  # noqa: E401, F401
    from test_ext_tests import TestImpactTests  # noqa: E401, F401
    from test_ext_zoom import HubZoomTests  # noqa: E402, F401

    unittest.main(verbosity=2)
