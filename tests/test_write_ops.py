#!/usr/bin/env python3
"""Index writers: `upsert` validation, `ack`, rename migration, and concurrent writers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    SCRIPT,
    age_file,
    aoci_index,
    cli,
)


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
        outs = [p.communicate() for p in procs]
        codes = [p.returncode for p in procs]
        errs = {i: err.decode("utf-8", errors="replace") for i, (_, err) in enumerate(outs) if procs[i].returncode != 0}
        self.assertEqual(codes, [0] * n, f"Parallel upsert child stderr: {errs}")
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


class P0RegressionTests(unittest.TestCase):

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


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditFixesRegressionTests(RepoTestCase):

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            check=True,
        )

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
