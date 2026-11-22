#!/usr/bin/env python3
"""Regression tests for Option 3 feature extensions:
1. Structured API `contract_delta` (`prepare-sync` & `diff`),
2. Git diff hunk x `aoci.hubs.txt` intersection (`touched_hub_sections` in `diff` & `pack`),
3. FRAS-graph-pruned cross-file symbol reference finder (`refs` & `aoci_refs`),
4. Zero-token mechanical index self-healer (`doctor` / `lint --fix` & `aoci_doctor`),
5. `Stop` hook opt-in safe zero-token auto-heal (`AOCI_HOOK_AUTO_HEAL=safe` / `--auto-heal`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

sys.dont_write_bytecode = True

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from aoci_testkit import HAS_GIT, RepoTestCase, cli_json, hook  # noqa: E402
import aoci_mcp  # noqa: E402


class Option3ExtensionsTests(RepoTestCase):
    def _git(self, *args: str, cwd: Optional[Path] = None) -> str:
        res = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return res.stdout

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_1_contract_delta_in_prepare_sync_and_diff_across_languages(self) -> None:
        self._git("init", "-q")
        self.write(
            "src/client.py",
            (
                "import os\n"
                "\n"
                "class Client:\n"
                "    def connect(self) -> bool:\n"
                "        return True\n"
                "    def keep_alive(self) -> None:\n"
                "        pass\n"
                "\n"
                "def legacy_helper(x: int) -> int:\n"
                "    return x\n"
            ),
        )
        self.write(
            "web/api.ts",
            (
                "import { Base } from './base';\n"
                "export function fetchItems(limit: number): string[] {\n"
                "  return [];\n"
                "}\n"
            ),
        )
        self._git("add", ".")
        self._git("commit", "-qm", "baseline")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/client.py[IK7T]: F:manages network client connection lifecycle | R:- | A:Client,legacy_helper | S:-",
                "web/api.ts[UI6T]: F:fetches frontend item list | R:- | A:fetchItems | S:-",
            )[0],
            0,
        )

        # Mutate Python & TS public contracts
        self.write(
            "src/client.py",
            (
                "import os\n"
                "import ssl\n"
                "\n"
                "class Client:\n"
                "    def connect(self) -> bool:\n"
                "        return True\n"
                "    def keep_alive(self) -> None:\n"
                "        pass\n"
                "    def fetch(self, timeout: float = 5.0) -> bytes:\n"
                "        return b''\n"
            ),
        )
        self.write(
            "web/api.ts",
            (
                "import { Base } from './base';\n"
                "import { AuthToken } from './auth';\n"
                "export function fetchItems(limit: number, cursor?: string): string[] {\n"
                "  return [];\n"
                "}\n"
            ),
        )

        prep = cli_json("prepare-sync", "--root", str(self.root))
        by_path = {item["path"]: item for item in prep["contract_or_unknown_modified"]}
        self.assertIn("src/client.py", by_path)
        py_delta = by_path["src/client.py"].get("contract_delta")
        self.assertIsNotNone(py_delta)
        # Granular per-method delta: unchanged `connect` and `keep_alive` must NOT be repeated!
        self.assertTrue(any("Client::def fetch" in s for s in py_delta.get("added_signatures", ())), py_delta)
        self.assertFalse(any("keep_alive" in s for s in py_delta.get("added_signatures", ())), py_delta)
        self.assertTrue(any("legacy_helper" in s for s in py_delta.get("removed_signatures", ())), py_delta)
        self.assertIn("import ssl", py_delta.get("added_imports", ()))

        ts_delta = by_path["web/api.ts"].get("contract_delta")
        self.assertIsNotNone(ts_delta)
        self.assertIn("./auth", ts_delta.get("added_imports", ()))
        self.assertTrue(any("cursor" in s for s in ts_delta.get("added_signatures", ())), ts_delta)

        # Working-tree `diff` also includes `contract_deltas`
        wt_diff = cli_json("diff", "--root", str(self.root))
        diff_deltas = {d["path"]: d for d in wt_diff.get("contract_deltas", ())}
        self.assertIn("src/client.py", diff_deltas)
        self.assertIn("web/api.ts", diff_deltas)

        # Staged & revision-range `diff` (`HEAD~1..HEAD`) also compute `contract_deltas`
        self._git("add", "src/client.py", "web/api.ts")
        staged_diff = cli_json("diff", "--staged", "--root", str(self.root))
        self.assertEqual({d["path"] for d in staged_diff.get("contract_deltas", ())}, {"src/client.py", "web/api.ts"})

        self._git("commit", "-qm", "evolve contracts")
        rev_diff = cli_json("diff", "HEAD~1..HEAD", "--root", str(self.root))
        self.assertEqual({d["path"] for d in rev_diff.get("contract_deltas", ())}, {"src/client.py", "web/api.ts"})

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_2_diff_hunk_hub_intersection_and_pack_budget_filtering(self) -> None:
        self._git("init", "-q")
        engine_src = (
            "def step_alpha(x: int) -> int:\n"
            "    a = x + 1\n"
            "    return a\n"
            "\n"
            "def step_beta(x: int) -> int:\n"
            "    b = x + 2\n"
            "    return b\n"
            "\n"
            "def step_gamma(x: int) -> int:\n"
            "    g = x + 3\n"
            "    return g\n"
            "\n"
            "def step_delta(x: int) -> int:\n"
            "    d = x + 4\n"
            "    return d\n"
        )
        self.write("core/engine.py", engine_src)
        self._git("add", ".")
        self._git("commit", "-qm", "init engine")

        self.init()
        self.assertEqual(
            self.upsert(
                "core/engine.py[KX9T]: F:executes multi-stage pipeline steps | R:- | A:step_alpha,step_beta,step_gamma,step_delta | S:preserve stage order"
            )[0],
            0,
        )
        cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "core/engine.py#step_alpha[L1-L3]: F:computes alpha stage increment | S:idempotent",
            "--upsert",
            "core/engine.py#step_beta[L5-L7]: F:computes beta stage increment | S:idempotent",
            "--upsert",
            "core/engine.py#step_gamma[L9-L11]: F:computes gamma stage increment | S:non-negative input",
            "--upsert",
            "core/engine.py#step_delta[L13-L15]: F:computes delta stage increment | S:idempotent",
        )

        # Modify ONLY `step_gamma` on line 10!
        self.write("core/engine.py", engine_src.replace("g = x + 3", "g = x + 30"), aged=False)

        brief = cli_json("diff", "--root", str(self.root))
        self.assertEqual(len(brief["hub_sections"]), 4)
        touched = brief.get("touched_hub_sections", [])
        self.assertEqual([s["symbol"] for s in touched], ["step_gamma"])
        self.assertTrue(brief["hub_sections"][0]["touched"])
        self.assertEqual(brief["hub_sections"][0]["symbol"], "step_gamma")

        # Under a positive budget, `pack` keeps ONLY the touched `step_gamma` hub section (omitting the 3 untouched siblings)
        pack_res = cli_json("pack", "--root", str(self.root), "--task", "review current pipeline change", "--budget", "800")
        packed_hub_syms = [s["symbol"] for s in pack_res.get("hub_sections", ())]
        self.assertEqual(packed_hub_syms, ["step_gamma"])

    def test_3_graph_pruned_symbol_refs_cli_and_mcp_comment_safety(self) -> None:
        self.write(
            "src/auth.py",
            (
                "def verify_token(raw: str) -> bool:\n"
                "    return raw.startswith('ok:')\n"
            ),
        )
        self.write(
            "src/orders.py",
            (
                "from src.auth import verify_token\n"
                "\n"
                "def create_order(tok: str) -> str:\n"
                "    # verify_token should never match on this comment line!\n"
                "    if not verify_token(tok):\n"
                "        return 'denied'\n"
                "    fn_ref = verify_token\n"
                "    return 'created'\n"
            ),
        )
        self.write(
            "src/unrelated_noise.py",
            (
                "# Mentions verify_token in comment and string only\n"
                "def ping() -> str:\n"
                "    return 'pong'\n"
            ),
        )
        for idx in range(5):
            self.write(f"src/extra_{idx}.py", f"def extra_{idx}() -> int:\n    return {idx}\n")
        self.write(
            "tests/test_auth.py",
            (
                "from src.auth import verify_token\n"
                "def test_ok():\n"
                "    assert verify_token('ok:1')\n"
            ),
        )

        self.init()
        extra_entries = [
            f"src/extra_{idx}.py[CX3T]: F:returns constant {idx} | R:- | A:extra_{idx} | S:-" for idx in range(5)
        ]
        self.assertEqual(
            self.upsert(
                "src/auth.py[MK8T]: F:validates bearer token strings | R:- | A:verify_token<-src/orders.py | S:constant-time prefix check",
                "src/orders.py[EX7T]: F:creates orders after token check | R:src/auth.py | A:create_order | S:-",
                "src/unrelated_noise.py[CX3T]: F:responds to healthcheck ping | R:- | A:ping | S:-",
                "tests/test_auth.py[TX2T]: F:tests verify_token behavior | R:src/auth.py | A:test_ok | S:-",
                *extra_entries,
            )[0],
            0,
        )

        refs_res = cli_json("refs", "verify_token", "--root", str(self.root))
        self.assertTrue(refs_res["ok"], refs_res)
        self.assertEqual(refs_res["mode"], "fras-graph")
        # Pruned candidate set skips the 5 unrelated `extra_*` files AND `unrelated_noise.py`!
        self.assertLess(refs_res["candidate_files_searched"], refs_res["total_indexed_files"])
        hits = {(r["path"], r["line"]): r["kind"] for r in refs_res["references"]}
        self.assertEqual(hits.get(("src/orders.py", 1)), "import")
        self.assertEqual(hits.get(("src/orders.py", 5)), "call")
        self.assertEqual(hits.get(("src/orders.py", 7)), "reference")
        self.assertNotIn(("src/orders.py", 4), hits)  # comment line excluded!
        self.assertEqual(hits.get(("tests/test_auth.py", 3)), "call")
        self.assertNotIn(("src/auth.py", 1), hits)  # definition excluded unless --include-defs

        # MCP `aoci_refs` with `include_defs=True` and `path#symbol` syntax
        aoci_mcp._CACHE.invalidate(self.root)
        mcp_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {
                    "name": "aoci_refs",
                    "arguments": {
                        "root": str(self.root),
                        "symbol": "src/auth.py#verify_token",
                        "include_defs": True,
                    },
                },
            }
        )
        sc = mcp_resp["result"]["structuredContent"]
        self.assertTrue(sc["ok"])
        mcp_hits = {(r["path"], r["line"]): r["kind"] for r in sc["references"]}
        self.assertEqual(mcp_hits.get(("src/auth.py", 1)), "definition")

    def test_4_doctor_and_lint_fix_repair_mechanics_without_masking_unreviewed_edits(self) -> None:
        # 60-line file (`S` tier), plus `util.py`, `main.py`, and `unreviewed.py`
        big_lines = "\n".join([f"X_{i} = {i}" for i in range(58)]) + "\ndef compute_all():\n    return X_0\n"
        self.write("src/big.py", big_lines)
        self.write("src/util.py", "def helper():\n    return 1\n")
        self.write("src/main.py", "from src.util import helper\ndef run():\n    return helper()\n")
        self.write("src/unreviewed.py", "def orig():\n    return 10\n")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/big.py[CX6S]: F:defines constants and compute_all | R:- | A:compute_all | S:-",
                "src/util.py[CX5T]: F:provides helper utility | R:- | A:helper | S:-",
                # Intentionally omit R:src/util.py on src/main.py so `--sync-static-r` can heal it!
                "src/main.py[EX6T]: F:runs application entrypoint | R:- | A:run | S:-",
                "src/unreviewed.py[CX5T]: F:holds original routine | R:- | A:orig | S:-",
            )[0],
            0,
        )
        cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "src/big.py#compute_all[L59-L60]: F:returns first constant | S:-",
        )

        # Inject mechanical corruption into `.aoci/`:
        # 1) Stale Size tier `T` (should be `S`), dangling `src/ghost.py` in R: and A:, and duplicate row in `aoci.code.txt`
        code_p = self.root / ".aoci" / "aoci.code.txt"
        raw_code = code_p.read_text(encoding="utf-8")
        raw_code = raw_code.replace("src/big.py[CX6S]:", "src/big.py[CX6T]:")
        raw_code = raw_code.replace(
            "src/util.py[CX5T]: F:provides helper utility | R:- | A:helper | S:-",
            "src/util.py[CX5T]: F:provides helper utility | R:src/ghost.py | A:helper<-src/ghost.py,src/big.py; old_fn<-src/ghost.py | S:-",
        )
        raw_code += "src/main.py[EX6T]: F:runs application entrypoint | R:- | A:run | S:-\n"
        code_p.write_text(raw_code, encoding="utf-8")

        # 2) Corrupt hub span `[L1-L2]` (actual is `[L59-L60]`) and strip `#Volume: id=hubs` from `aoci.txt`
        hubs_p = self.root / ".aoci" / "aoci.hubs.txt"
        hubs_p.write_text(hubs_p.read_text(encoding="utf-8").replace("[L59-L60]", "[L1-L2]"), encoding="utf-8")
        man_p = self.root / ".aoci" / "aoci.txt"
        man_p.write_text(
            "\n".join(ln for ln in man_p.read_text(encoding="utf-8").splitlines() if "id=hubs" not in ln) + "\n",
            encoding="utf-8",
        )

        # 3) Modify `src/unreviewed.py` on disk WITHOUT acknowledging it!
        self.write("src/unreviewed.py", "def orig():\n    return 999\n" + "\n" * 60)

        # Dry-run previews fixes without mutating disk
        dry_doc = cli_json("doctor", "--root", str(self.root), "--sync-static-r", "--dry-run")
        self.assertTrue(dry_doc["ok"])
        self.assertGreaterEqual(dry_doc["total_fixes"], 5)
        self.assertIn("src/big.py[CX6T]:", code_p.read_text(encoding="utf-8"))

        # Apply via `lint --fix --sync-static-r`
        fixed_lint = cli_json("lint", "--root", str(self.root), "--fix", "--sync-static-r")
        doc_part = fixed_lint["doctor"]
        self.assertTrue(doc_part["ok"])
        self.assertIn("src/main.py", doc_part["deduplicated"])
        self.assertIn("src/big.py", doc_part["size_refreshed"])
        self.assertIn("src/util.py", doc_part["dangling_refs_pruned"])
        self.assertEqual(doc_part["static_deps_added"].get("src/main.py"), ["src/util.py"])
        self.assertTrue(any("src/big.py#compute_all" in h for h in doc_part["hubs_refreshed"]), doc_part["hubs_refreshed"])
        self.assertTrue(doc_part["manifest_repaired"])

        healed_code = code_p.read_text(encoding="utf-8")
        self.assertIn("src/big.py[CX6S]:", healed_code)
        self.assertIn("R:- | A:helper<-src/big.py; old_fn | S:-", healed_code)
        self.assertIn("src/main.py[EX6T]: F:runs application entrypoint | R:src/util.py | A:run | S:-", healed_code)

        # CRITICAL: `src/unreviewed.py` MUST still be reported as `modified` in `scan` (never silently acked!)
        scan_after = cli_json("scan", "--root", str(self.root))
        mod_paths = [m["path"] for m in scan_after["drift"]["modified"]]
        self.assertEqual(mod_paths, ["src/unreviewed.py"])

        # MCP `aoci_doctor` works idempotently too
        is_err, raw_mcp = aoci_mcp.call_tool("aoci_doctor", {"root": str(self.root), "sync_static_r": True})
        self.assertFalse(is_err, raw_mcp)

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_5_stop_hook_safe_auto_heal_and_semantic_refusal_guards(self) -> None:
        self._git("init", "-q")
        body_move = "\n".join([f"CONST_{i} = {i}" for i in range(10)]) + "\ndef moved_fn() -> int:\n    return CONST_0\n"
        self.write("src/orig_move.py", body_move)
        self.write("src/caller.py", "from src.orig_move import moved_fn\ndef use_it():\n    return moved_fn()\n")
        self.write("src/leaf_unused.py", "LEAF = 1\n")
        self._git("add", ".")
        self._git("commit", "-qm", "init")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/orig_move.py[CX6T]: F:defines moved_fn and constants | R:- | A:moved_fn<-src/caller.py | S:-",
                "src/caller.py[AX6T]: F:calls moved_fn from orig_move | R:src/orig_move.py | A:use_it | S:-",
                "src/leaf_unused.py[CX2T]: F:defines standalone leaf constant | R:- | A:LEAF | S:-",
            )[0],
            0,
        )

        sid = "sess-auto-heal"
        hook(
            "session-start",
            {"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(self.root)},
            host="claude-code",
        )

        # 1) Exact rename (`orig_move.py -> renamed_move.py`) + clean unreferenced delete (`leaf_unused.py`)
        (self.root / "src" / "orig_move.py").unlink()
        self.write("src/renamed_move.py", body_move)
        (self.root / "src" / "leaf_unused.py").unlink()

        with mock.patch.dict(os.environ, {"AOCI_HOOK_AUTO_HEAL": "safe"}, clear=False):
            stop_ok = hook(
                "stop",
                {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.root)},
                host="claude-code",
            )
        # Auto-healed silently -> empty `{}` (zero wasted LLM continuation turns!)
        self.assertEqual(stop_ok, {})
        ver = cli_json("verify", "--root", str(self.root))
        self.assertTrue(ver["ok"], ver)
        code_txt = (self.root / ".aoci" / "aoci.code.txt").read_text(encoding="utf-8")
        self.assertIn("src/renamed_move.py[", code_txt)
        self.assertIn("R:src/renamed_move.py", code_txt)
        self.assertNotIn("src/leaf_unused.py", code_txt)

        # 2) Safety guard A: deleting `src/renamed_move.py` which IS referenced by `src/caller.py` MUST still block!
        (self.root / "src" / "renamed_move.py").unlink()
        with mock.patch.dict(os.environ, {"AOCI_HOOK_AUTO_HEAL": "safe"}, clear=False):
            stop_dangling = hook(
                "stop",
                {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.root)},
                host="claude-code",
            )
        self.assertEqual(stop_dangling.get("decision"), "block")

        # Restore `src/renamed_move.py` on disk, then test Safety guard B: unpooled brand-new file MUST block!
        self.write("src/renamed_move.py", body_move)
        self.write("src/brand_new_semantic.py", "def needs_human_fras():\n    return 42\n")
        with mock.patch.dict(os.environ, {"AOCI_HOOK_AUTO_HEAL": "safe"}, clear=False):
            stop_new = hook(
                "stop",
                {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.root)},
                host="claude-code",
            )
        self.assertEqual(stop_new.get("decision"), "block")
        self.assertIn("src/brand_new_semantic.py", stop_new.get("reason", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
