#!/usr/bin/env python3
"""Drift detection: fingerprints, racy-clean guards, renames, git-fast discovery, and `sig_hash`."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    age_file,
    aoci_index,
    cli,
    cli_json,
    discovery,
    git_ops,
    parsers,
)


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


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase2PerformanceTests(RepoTestCase):

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

        self.write("a.py", "# a.py\nval = 2\n")
        with mock.patch.object(git_ops, "run_git", side_effect=AssertionError("ack must not spawn git on warm tracked file")):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = aoci_index.main(["ack", "--root", str(self.root), "a.py"])
        self.assertEqual(rc, 0)

        # `git_discovery.json` must NOT be deleted: `b.py` and `c.py` stay in `tracked_clean`, only `a.py` demoted
        self.assertTrue(disc_path.exists())
        disc_after = json.loads(disc_path.read_text(encoding="utf-8"))
        self.assertEqual(disc_after["tracked_clean"], ["b.py", "c.py"])
        self.assertEqual(disc_after["tracked_recheck"], ["a.py"])
        self.assertFalse(cli_json("scan", "--root", str(self.root), "--git-fast")["drift"]["has_drift"])


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


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditV2RegressionTests(RepoTestCase):

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


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

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

    def test_cold_bootstrap_scan_fp_cache_zero_rehash_and_stable_batch_plan(self):

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
        with mock.patch.object(discovery, "_fingerprint_bytes_norm", side_effect=AssertionError("must reuse scan_fps.json")):
            sc1 = _run_inproc("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc1["entries"]], ["a.py", "b.py"])

        # 2nd `scaffold --batch 2` before upsert -> 0 hashes & 0 outline extractions
        with mock.patch.object(discovery, "_fingerprint_bytes_norm", side_effect=AssertionError("must not re-hash")):
            with mock.patch.object(parsers, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
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
        orig_fp_norm = discovery._fingerprint_bytes_norm
        fp_headers: list = []

        def _track_fp(norm, st_arg, *rest, **kw):
            fp_headers.append(norm.decode("utf-8", errors="replace").splitlines()[0])
            return orig_fp_norm(norm, st_arg, *rest, **kw)

        with mock.patch.object(discovery, "_fingerprint_bytes_norm", side_effect=_track_fp):
            with mock.patch.object(parsers, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
                sc2_after = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc2_after["entries"]], ["c.py", "d.py"])
        self.assertGreaterEqual(len(fp_headers), 1)
        for header in fp_headers:
            self.assertIn(header, ("def fn_a():", "def fn_b():"), f"unexpected re-hash of unindexed file: {header}")

        # Once Batch 1 baselined files are >3s old and acknowledged in baseline, total hashes across ALL 6 files drop to 0
        age_file(self.root / "a.py")
        age_file(self.root / "b.py")
        cli_json("ack", "--root", str(self.root), "a.py", "b.py")
        orig_extract = parsers.extract_file_outline
        extracted_paths: list = []

        def _track_outline(path_p, rel_p, *rest, **kw):
            extracted_paths.append(rel_p)
            return orig_extract(path_p, rel_p, *rest, **kw)

        with mock.patch.object(discovery, "_fingerprint_bytes_norm", side_effect=AssertionError("must not re-hash")):
            with mock.patch.object(parsers, "extract_file_outline", side_effect=_track_outline):
                sc2_aged = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc2_aged["entries"]], ["c.py", "d.py"])
        self.assertEqual(sorted(extracted_paths), ["a.py", "b.py"])

        # Repeated Batch 2 now hits 0 hashes AND 0 outline extractions across all 6 files
        with mock.patch.object(discovery, "_fingerprint_bytes_norm", side_effect=AssertionError("must not re-hash")):
            with mock.patch.object(parsers, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
                sc2_warm = _run_inproc("scaffold", "--root", str(self.root), "--batch", "2")
        self.assertEqual([ln.split("[", 1)[0] for ln in sc2_warm["entries"]], ["c.py", "d.py"])

        # Asking for completed Batch 1 reports completed without shifting Batch 2 into slot 1
        sc1_done = cli_json("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "2")
        self.assertEqual(sc1_done["count"], 0)
        self.assertEqual(sc1_done["status"], "completed")
        self.assertEqual(sc1_done["remaining_batches"], [2, 3])

        # Modify `d.py` to 60 lines: ONLY `d.py` is re-hashed (call_count == 1) and shows updated size tier `S`
        self.write("d.py", "def fn_d():\n" + "".join(f"    x{i} = {i}\n" for i in range(60)), aged=True)

        def _counting_fp(norm, st, *rest, **kw):
            return orig_fp_norm(norm, st, *rest, **kw)

        with mock.patch.object(discovery, "_fingerprint_bytes_norm", side_effect=_counting_fp) as fp_spy:
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

    def test_single_read_sig_hash_on_scan_and_transient_key_stripping(self):

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
        with mock.patch.object(parsers, "compute_signature_hash", side_effect=AssertionError("must not re-read modified files")):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
