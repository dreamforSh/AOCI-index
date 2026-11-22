#!/usr/bin/env python3
"""Semantic pool: branch/worktree restore, SQLite migration, and bundle export/import."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    aoci_index,
    cli,
    cli_json,
    constants,
    hook,
)


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


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase1FixesTests(RepoTestCase):

    def _git_at(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd,
            check=True,
        )

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
        with mock.patch.object(constants, "MAX_SEMANTIC_POOL_ENTRIES", 5):
            def _writer(idx: int) -> None:
                fp_fake = {"sha256": f"{idx:064x}", "lines": 10}
                ent = f"f{idx}.py[CX3T]: F:concurrent entry number {idx} | R:- | A:- | S:-"
                aoci_index.record_semantic_pool_entries(self.root, git_info, [(f"f{idx}.py", fp_fake, ent)])

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(_writer, range(10)))
            loaded = aoci_index.load_semantic_pool(self.root, git_info)
            self.assertLessEqual(len(loaded["by_path_sha"]), 5)
            self.assertLessEqual(len(loaded["by_sha"]), 5)


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


class PoolBundleMetaTests(RepoTestCase):
    """Pool bundles carry index metadata; imports only fill gaps and stay idempotent."""

    def _source_bundle(self) -> Path:
        self.write("src/a.py", "def a():\n    return 1\n")
        self.init()
        aoci = self.root / ".aoci"
        manifest = aoci / "aoci.txt"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "#Global-Invariants: -", "#Global-Invariants: money is int64 cents; never log tokens"
            ),
            encoding="utf-8",
        )
        meta = aoci / "aoci.meta.txt"
        meta.write_text(
            meta.read_text(encoding="utf-8") + "#Domains: Q=queue\n#Glossary: 队列 = queue\n#Layer-Rules: D!->E\n",
            encoding="utf-8",
        )
        code, out = self.upsert("src/a.py[CQ3T]: F:returns the constant one | R:- | A:a | S:-")
        self.assertEqual(code, 0, out)
        bundle = self.root / "bundle.json"
        res = cli_json("pool", "export", "--root", str(self.root), "--output", str(bundle))
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["exported_meta"]), ["domains", "global_invariants", "glossary", "layer_rules", "project"])
        return bundle

    def test_import_fills_gaps_once_and_never_overrides(self) -> None:
        bundle = self._source_bundle()
        data = json.loads(bundle.read_text(encoding="utf-8"))
        self.assertEqual(data["meta"]["domains"]["Q"], "queue")

        target = self.root / "target"
        target.mkdir()
        cli_json("init", "--root", str(target))
        res = cli_json("pool", "import", "--root", str(target), "--input", str(bundle))
        applied = res["meta_applied"]
        self.assertIn("Q", applied["domains"])
        self.assertTrue(applied["global_invariants"])
        self.assertTrue(applied["layer_rules"])
        self.assertGreaterEqual(applied["glossary_groups"], 1)
        t_aoci = target / ".aoci"
        self.assertIn("money is int64 cents", (t_aoci / "aoci.txt").read_text(encoding="utf-8"))
        self.assertIn("#Glossary: 队列 = queue", (t_aoci / "aoci.meta.txt").read_text(encoding="utf-8"))

        again = cli_json("pool", "import", "--root", str(target), "--input", str(bundle))
        self.assertEqual(again["meta_applied"], {}, "a second import must not append duplicates")

        manifest = t_aoci / "aoci.txt"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace("money is int64 cents; never log tokens", "local rule wins"),
            encoding="utf-8",
        )
        cli_json("pool", "import", "--root", str(target), "--input", str(bundle))
        self.assertIn("local rule wins", manifest.read_text(encoding="utf-8"))

    def test_untrusted_meta_is_sanitized(self) -> None:
        self.init()
        hostile = {
            "global_invariants": "line one\n#Layer-Rules: E!->D\nline two",
            "domains": {"Z": "bad label\n#Inject", "zz": "ignored", "1": "ok"},
            "glossary": [["a;b", "c=d"], "not-a-list", ["solo"]],
        }
        bundle = self.root / "hostile.json"
        bundle.write_text(json.dumps({"format": "aoci-semantic-pool-v1", "by_path_sha": [], "meta": hostile}), encoding="utf-8")
        res = cli_json("pool", "import", "--root", str(self.root), "--input", str(bundle))
        self.assertTrue(res["ok"], res)
        meta_text = (self.root / ".aoci" / "aoci.meta.txt").read_text(encoding="utf-8")
        manifest_text = (self.root / ".aoci" / "aoci.txt").read_text(encoding="utf-8")
        self.assertNotIn("#Inject", meta_text)
        self.assertIn("Z=bad-label-Inject", meta_text)
        self.assertNotIn("zz=", meta_text)
        self.assertIn("#Global-Invariants: line one #Layer-Rules: E!->D line two", manifest_text)
        self.assertEqual([ln for ln in manifest_text.splitlines() if ln.startswith("#Layer-Rules:")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
