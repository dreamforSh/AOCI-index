#!/usr/bin/env python3
"""Tests for task-driven context packer (`aoci pack` / `build_task_pack` / MCP `aoci_pack`)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from aoci_testkit import SKILL_DIR, RepoTestCase, aoci_index, cli, cli_json  # noqa: E402


class TaskPackTests(RepoTestCase):
    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

    def test_bm25_bilingual_glossary_seeds_graph_expansion_and_constraints(self):
        ex_aoci = self.root / ".aoci"
        shutil.copytree(SKILL_DIR / "examples", ex_aoci)

        # Create matching files on disk and commit so working tree has no unrelated diff noise
        for rel in (
            "pyproject.toml",
            "src/main.py",
            "src/auth/jwt.py",
            "src/config/env.py",
            "src/order/repo.py",
            "src/order/service.py",
            "src/routes/orders.py",
        ):
            self.write(rel, f"# {rel}\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "baseline")

        data = cli_json("pack", "--root", str(self.root), "--task", "密钥轮换")
        self.assertTrue(data["ok"])
        self.assertEqual(data["task"], "密钥轮换")
        self.assertGreaterEqual(len(data["seeds"]), 1)
        top_seed = data["seeds"][0]
        self.assertEqual(top_seed["path"], "src/auth/jwt.py")
        self.assertIn("bm25", top_seed["reasons"])
        self.assertGreater(top_seed["score"], 0.0)
        self.assertTrue(any("密钥轮换 -> key rotation" in g for g in top_seed.get("matched_glossary", [])))
        self.assertTrue(data.get("glossary_expansions"))

        # 1-hop R: dep (src/config/env.py) and 1-hop A: caller (src/routes/orders.py)
        deps_txt = "\n".join(data["dependencies"])
        callers_txt = "\n".join(data["callers"])
        self.assertIn("src/config/env.py[", deps_txt)
        self.assertIn("src/routes/orders.py[", callers_txt)

        # critical_constraints collects S: from packed seeds and 1-hop R: dependencies
        cc_paths = {c["path"]: c for c in data["critical_constraints"]}
        self.assertIn("src/auth/jwt.py", cc_paths)
        self.assertIn("src/config/env.py", cc_paths)
        self.assertEqual(cc_paths["src/config/env.py"]["role"], "dependency")
        # Caller S: must not be mixed into critical_constraints unless it is also a seed/1-hop R: dep
        if "src/routes/orders.py" not in {s["path"] for s in data["seeds"]}:
            self.assertNotIn("src/routes/orders.py", cc_paths)

        # open_next recommends seeds and high-importance 1-hop deps
        open_paths = [item["path"] for item in data["open_next"]]
        self.assertEqual(open_paths[0], "src/auth/jwt.py")
        self.assertIn("src/config/env.py", open_paths)

    def test_path_mention_and_basename_seeds_with_boundary_safety(self):
        for rel in (
            "src/auth/token_guard.py",
            "src/other/token_guard.py",
            "src/routes/login.py",
            "src/utils/date_fmt.py",
            "src/nested/fmt.py",
        ):
            self.write(rel, "def fn():\n    return 1\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/auth/token_guard.py[MA7T]: F:validates session cookie headers | R:- | A:check | S:constant time",
                "src/other/token_guard.py[CX3T]: F:parses legacy xml archive | R:- | A:parse | S:-",
                "src/routes/login.py[EA6T]: F:handles browser sign in form | R:src/auth/token_guard.py | A:login | S:-",
                "src/utils/date_fmt.py[CX4T]: F:formats ISO timestamps | R:- | A:fmt_iso | S:-",
                "src/nested/fmt.py[CX2T]: F:formats plain integer labels | R:- | A:fmt_int | S:-",
            )[0],
            0,
        )

        # Full POSIX path inside CJK text must NOT trigger basename false match on src/other/token_guard.py
        p1 = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--no-diff",
            "--task",
            "修改src/auth/token_guard.py的校验逻辑",
        )
        by_p1 = {s["path"]: s for s in p1["seeds"]}
        self.assertEqual(p1["seeds"][0]["path"], "src/auth/token_guard.py")
        self.assertIn("path-mention", by_p1["src/auth/token_guard.py"]["reasons"])
        self.assertNotIn("path-mention", by_p1.get("src/other/token_guard.py", {}).get("reasons", []))

        # Windows backslash path also resolves cleanly
        p2 = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--no-diff",
            "--task",
            r"Inspect src\auth\token_guard.py carefully",
        )
        self.assertEqual(p2["seeds"][0]["path"], "src/auth/token_guard.py")
        self.assertIn("path-mention", p2["seeds"][0]["reasons"])

        # Bare basename mention matches exact basename without prefix-boundary collision (`date_fmt.py` vs `fmt.py`)
        p3 = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--no-diff",
            "--task",
            "Update date_fmt.py and login.py.",
        )
        by_p3 = {s["path"]: s for s in p3["seeds"]}
        self.assertIn("path-mention", by_p3["src/utils/date_fmt.py"]["reasons"])
        self.assertIn("path-mention", by_p3["src/routes/login.py"]["reasons"])
        self.assertNotIn("path-mention", by_p3.get("src/nested/fmt.py", {}).get("reasons", []))

    def test_diff_and_no_diff_plus_non_git_graceful_fallback(self):
        self.write("src/env.py", "SECRET = 'x'\n")
        self.write("src/jwt.py", "from src import env\n")
        self.write("src/routes.py", "from src import jwt\n")
        self.write("src/unrelated.py", "VAL = 1\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/env.py[CG7T]: F:loads startup secrets | R:- | A:SECRET<-src/jwt.py | S:fail fast on missing secret",
                "src/jwt.py[MA8T]: F:verifies bearer tokens | R:src/env.py | A:verify<-src/routes.py | S:alg allowlist",
                "src/routes.py[EA6T]: F:mounts HTTP routes | R:src/jwt.py | A:router | S:-",
                "src/unrelated.py[CX2T]: F:stores unrelated constant value | R:- | A:- | S:-",
            )[0],
            0,
        )

        # Modify indexed file & add new unindexed file
        self.write("src/jwt.py", "from src import env\nSKEW = 30\n")
        self.write("src/new_middleware.py", "def mw():\n    return True\n")

        with_diff = cli_json("pack", "--root", str(self.root), "--task", "check startup secrets")
        by_p = {s["path"]: s for s in with_diff["seeds"]}
        self.assertIn("src/jwt.py", by_p)
        self.assertIn("diff", by_p["src/jwt.py"]["reasons"])
        self.assertEqual(with_diff["unindexed_changed"], ["src/new_middleware.py"])
        open_map = {item["path"]: item for item in with_diff["open_next"]}
        self.assertEqual(open_map["src/new_middleware.py"]["kind"], "unindexed_changed")

        without_diff = cli_json("pack", "--root", str(self.root), "--no-diff", "--task", "check startup secrets")
        self.assertEqual(without_diff["unindexed_changed"], [])
        for s in without_diff["seeds"]:
            self.assertNotIn("diff", s["reasons"])

        # Non-Git workspace gracefully sets diff_brief=None without failing even without --no-diff
        with tempfile.TemporaryDirectory() as td:
            ng_root = Path(td)
            (ng_root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            self.assertEqual(cli("init", "--root", str(ng_root))[0], 0)
            self.assertEqual(
                cli(
                    "upsert",
                    "--root",
                    str(ng_root),
                    "--entry",
                    "app.py[EX6T]: F:runs standalone application logic | R:- | A:run | S:-",
                )[0],
                0,
            )
            ng_pack = cli_json("pack", "--root", str(ng_root), "--task", "standalone application")
            self.assertTrue(ng_pack["ok"])
            self.assertEqual(ng_pack["seeds"][0]["path"], "app.py")
            self.assertEqual(ng_pack["unindexed_changed"], [])

    def test_budget_truncation_and_priority_ranking(self):
        for rel in ("target.py", "dep_hi.py", "dep_lo.py", "caller_hi.py", "caller_lo.py", "hop2_deep.py"):
            self.write(rel, "x = 1\n")
        self.init()
        entries = [
            "target.py[AX7T]: F:coordinates core target workflow logic | R:dep_hi.py,dep_lo.py | A:run | S:-",
            "dep_hi.py[PX9T]: F:executes critical persistence ledger updates | R:hop2_deep.py | A:commit<-target.py | S:serializable lock required",
            "dep_lo.py[CX2T]: F:formats minor helper display label strings | R:- | A:fmt<-target.py | S:-",
            "caller_hi.py[EX8T]: F:exposes authenticated HTTP gateway API | R:target.py | A:api | S:rate limit before auth",
            "caller_lo.py[CX1T]: F:prints local developer debug helper trace | R:target.py | A:dbg | S:-",
            "hop2_deep.py[DX9T]: F:defines deep second hop core schema model | R:- | A:Schema<-dep_hi.py | S:never mutate schema id",
        ]
        self.assertEqual(self.upsert(*entries)[0], 0)

        full = cli_json("pack", "--root", str(self.root), "--no-diff", "--task", "workflow", "--depth", "2", "--budget", "0")
        self.assertFalse(full["truncated"])
        self.assertEqual([s["path"] for s in full["seeds"]], ["target.py"])
        self.assertIn("hop2_deep.py[", "\n".join(full["dependencies"]))

        # Budget sized for invariants + top seed + 1-hop high-imp items (dep_hi + caller_hi), omitting hop2 & low-imp
        tight_budget = (
            aoci_index.estimate_tokens("-")
            + aoci_index.estimate_tokens(entries[0])
            + aoci_index.estimate_tokens(entries[1])
            + aoci_index.estimate_tokens(entries[3])
            + 2
        )
        budgeted = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--no-diff",
            "--task",
            "workflow",
            "--depth",
            "2",
            "--budget",
            str(tight_budget),
        )
        self.assertTrue(budgeted["truncated"])
        self.assertLessEqual(budgeted["estimated_tokens"], tight_budget)
        kept = "\n".join([s["entry"] for s in budgeted["seeds"]] + budgeted["dependencies"] + budgeted["callers"])
        self.assertIn("target.py[", kept)
        self.assertIn("dep_hi.py[", kept)
        self.assertIn("caller_hi.py[", kept)
        self.assertNotIn("hop2_deep.py[", kept)
        self.assertIn("hop2_deep.py", budgeted["omitted_paths"])
        self.assertIn("dep_lo.py", budgeted["omitted_paths"])

        # Multi-seed budget prioritizes seeds before 1-hop deps and higher-importance seeds before lower-importance seeds
        self.write("seed_hi.py", "z = 1\n")
        self.write("seed_lo.py", "w = 2\n")
        extra_hi = "seed_hi.py[AX9T]: F:orchestrates high priority workflow scheduler | R:- | A:sched | S:lock order"
        extra_lo = "seed_lo.py[CX2T]: F:logs workflow debug trace metrics | R:- | A:trace | S:-"
        self.assertEqual(self.upsert(extra_hi, extra_lo)[0], 0)
        seed_budget = (
            aoci_index.estimate_tokens("-")
            + aoci_index.estimate_tokens(entries[0])
            + aoci_index.estimate_tokens(extra_hi)
            + 2
        )
        seed_packed = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--no-diff",
            "--task",
            "target.py workflow",
            "--depth",
            "1",
            "--budget",
            str(seed_budget),
        )
        self.assertTrue(seed_packed["truncated"])
        self.assertLessEqual(seed_packed["estimated_tokens"], seed_budget)
        kept_seed_paths = {s["path"] for s in seed_packed["seeds"]}
        self.assertEqual(kept_seed_paths, {"target.py", "seed_hi.py"})
        self.assertIn("seed_lo.py", seed_packed["omitted_paths"])

    def test_open_next_outline_cache_anchors_and_cli_text_output(self):
        worker_src = "".join(f"def step_{i}(x):\n    return x + {i}\n\n" for i in range(1, 8))
        self.write("src/worker.py", worker_src)
        self.write("src/core_dep.py", "class Engine:\n    def execute(self):\n        return True\n\ndef init_engine():\n    return Engine()\n")
        self.write("src/low_dep.py", "def helper():\n    return 0\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/worker.py[AX7T]: F:executes seven step background worker pipeline | R:src/core_dep.py,src/low_dep.py | A:step_1 | S:idempotent retry",
                "src/core_dep.py[DX8T]: F:provides core execution engine dependency | R:- | A:Engine<-src/worker.py | S:single active engine",
                "src/low_dep.py[CX2T]: F:returns minor numeric helper constant | R:- | A:helper<-src/worker.py | S:-",
            )[0],
            0,
        )

        # Without outline_cache.json, pack stays read-only and open_next has empty symbols
        no_cache_pack = cli_json("pack", "--root", str(self.root), "--no-diff", "--task", "background pipeline")
        cache_path = self.root / ".aoci" / ".cache" / "outline_cache.json"
        self.assertFalse(cache_path.exists())
        self.assertEqual(no_cache_pack["open_next"][0]["symbols"], [])

        # Populate outline_cache.json via `audit` (read-only static graph scan writes cache)
        cli_json("audit", "--root", str(self.root))
        self.assertTrue(cache_path.is_file())

        cached_pack = cli_json("pack", "--root", str(self.root), "--no-diff", "--task", "background pipeline")
        open_by_path = {item["path"]: item for item in cached_pack["open_next"]}
        self.assertIn("src/worker.py", open_by_path)
        self.assertIn("src/core_dep.py", open_by_path)
        self.assertNotIn("src/low_dep.py", open_by_path)

        worker_syms = open_by_path["src/worker.py"]["symbols"]
        self.assertEqual(len(worker_syms), 5)
        self.assertTrue(all("[L" in sym for sym in worker_syms), worker_syms)
        dep_syms = open_by_path["src/core_dep.py"]["symbols"]
        self.assertGreaterEqual(len(dep_syms), 2)
        self.assertTrue(any("Engine" in sym and "[L" in sym for sym in dep_syms))

        # Verify clean CLI text output
        rc, txt = cli("pack", "--root", str(self.root), "--no-diff", "--task", "background pipeline")
        self.assertEqual(rc, 0, txt)
        self.assertIn("# AOCI Task Pack", txt)
        self.assertIn("## Critical S: Constraints", txt)
        self.assertIn("## Seeds", txt)
        self.assertIn("## Open Next", txt)
        self.assertIn("[L", txt)

        # Malformed outline_cache.json degrades gracefully to empty symbol anchors
        cache_path.write_text("{corrupt json", encoding="utf-8")
        corrupt_pack = cli_json("pack", "--root", str(self.root), "--no-diff", "--task", "background pipeline")
        self.assertTrue(corrupt_pack["ok"])
        self.assertEqual(corrupt_pack["open_next"][0]["symbols"], [])

    def test_mcp_aoci_pack_reuses_warm_cache_and_handles_include_diff(self):
        import aoci_mcp

        self.write("src/db.py", "def query():\n    return []\n")
        self.write("src/api.py", "from src import db\ndef endpoint():\n    return db.query()\n")
        self._git("add", "src/db.py", "src/api.py")
        self._git("commit", "-q", "-m", "init")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/db.py[PX8T]: F:executes parameterized SQL database queries | R:- | A:query<-src/api.py | S:parameterized SQL only",
                "src/api.py[EX7T]: F:exposes JSON HTTP endpoint | R:src/db.py | A:endpoint | S:-",
            )[0],
            0,
        )

        # Verify TOOLS_SPEC metadata for aoci_pack
        list_resp = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 50, "method": "tools/list"})
        tools = {t["name"]: t for t in list_resp["result"]["tools"]}
        self.assertIn("aoci_pack", tools)
        pack_spec = tools["aoci_pack"]
        self.assertTrue(pack_spec["annotations"]["readOnlyHint"])
        self.assertEqual(pack_spec["inputSchema"]["required"], ["root", "task"])
        for prop in ("include_diff", "depth", "budget", "top_seeds"):
            self.assertIn(prop, pack_spec["inputSchema"]["properties"])

        # Modify tracked file & add unindexed file after baseline
        self.write("src/api.py", "from src import db\ndef endpoint():\n    return db.query() or []\n")
        self.write("src/added.py", "FLAG = True\n")

        # 1st MCP call warms _CACHE & BM25Index
        is_err1, txt1 = aoci_mcp.call_tool(
            "aoci_pack",
            {"root": str(self.root), "task": "parameterized SQL", "include_diff": True},
        )
        self.assertFalse(is_err1, txt1)
        p1 = json.loads(txt1)
        self.assertEqual(p1["seeds"][0]["path"], "src/db.py")
        self.assertEqual(p1["unindexed_changed"], ["src/added.py"])
        by_path1 = {s["path"]: s for s in p1["seeds"]}
        self.assertIn("diff", by_path1["src/api.py"]["reasons"])

        # 2nd & 3rd calls MUST reuse _CACHE (0 parse_code_volume, 0 BM25Index instantiations)
        with mock.patch.object(aoci_index, "parse_code_volume", side_effect=AssertionError("must hit _CACHE")):
            with mock.patch.object(aoci_index, "BM25Index", side_effect=AssertionError("must reuse cached BM25Index")):
                is_err2, txt2 = aoci_mcp.call_tool(
                    "aoci_pack",
                    {"root": str(self.root), "task": "HTTP endpoint", "include_diff": False, "budget": 0},
                )
                self.assertFalse(is_err2, txt2)
                p2 = json.loads(txt2)
                self.assertEqual(p2["seeds"][0]["path"], "src/api.py")
                self.assertEqual(p2["unindexed_changed"], [])
                self.assertNotIn("diff", p2["seeds"][0]["reasons"])
                self.assertEqual(p2["budget"], 0)

                is_err3, txt3 = aoci_mcp.call_tool(
                    "aoci_pack",
                    {"root": str(self.root), "task": "HTTP endpoint", "include_diff": True},
                )
                self.assertFalse(is_err3, txt3)
                p3 = json.loads(txt3)
                self.assertEqual(p3["unindexed_changed"], ["src/added.py"])
                self.assertIn("diff", p3["seeds"][0]["reasons"])

        # Error checks: empty task / uninitialized root
        err_empty, msg_empty = aoci_mcp.call_tool("aoci_pack", {"root": str(self.root), "task": "   "})
        self.assertTrue(err_empty)
        self.assertIn("task", msg_empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
