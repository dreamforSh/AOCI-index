#!/usr/bin/env python3
"""Regression tests for Option 1 quick-win fixes, MCP schema/cache parity, and tree-sitter expansion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.dont_write_bytecode = True

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_aoci import HAS_GIT, RepoTestCase, SKILL_DIR, aoci_index, cli, cli_json  # noqa: E402
import aoci_mcp  # noqa: E402
from aoci_core import audit, hubs, parsers, ts_accel  # noqa: E402


def _has_real_treesitter() -> bool:
    ts_accel.reset_treesitter_probe()
    try:
        with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False):
            return ts_accel.is_enabled() and ts_accel._get_parser("typescript") is not None
    except Exception:
        return False
    finally:
        ts_accel.reset_treesitter_probe()


class QuickWinsRegressionTests(RepoTestCase):
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

    def test_1_ci_check_markdown_output_and_action_yml_single_invocation(self) -> None:
        self.write("domain/order.py", "from routes.http import render\nclass Order:\n    pass\n")
        self.write("routes/http.py", "def render():\n    return 'ok'\n")

        sarif_out = self.root / "reports" / "aoci.sarif"
        md_out = self.root / "reports" / "summary.md"
        rc, out_gh = cli(
            "ci-check",
            "--root",
            str(self.root),
            "--format",
            "github",
            "--sarif-output",
            str(sarif_out),
            "--markdown-output",
            str(md_out),
            "--strict",
        )
        self.assertEqual(rc, 1)
        self.assertIn("::error file=domain/order.py", out_gh)
        self.assertTrue(sarif_out.is_file())
        self.assertTrue(md_out.is_file())
        md_txt = md_out.read_text(encoding="utf-8")
        self.assertIn("## AOCI Architecture Check", md_txt)
        self.assertIn("domain/order.py", md_txt)

        # JSON output records both sarif_output_path and markdown_output_path
        rep_json = cli_json(
            "ci-check",
            "--root",
            str(self.root),
            "--sarif-output",
            "reports/ci2.sarif",
            "--markdown-output",
            "reports/ci2.md",
        )
        self.assertTrue(Path(rep_json["sarif_output_path"]).is_file())
        self.assertTrue(Path(rep_json["markdown_output_path"]).is_file())

        # MCP `aoci_ci_check` also supports `markdown_output`
        mcp_md = self.root / "reports" / "mcp_summary.md"
        is_err, raw_mcp = aoci_mcp.call_tool(
            "aoci_ci_check",
            {
                "root": str(self.root),
                "format": "github",
                "markdown_output": str(mcp_md),
            },
        )
        self.assertFalse(is_err, raw_mcp)
        self.assertTrue(mcp_md.is_file())
        self.assertIn("## AOCI Architecture Check", mcp_md.read_text(encoding="utf-8"))

        # Verify `action.yml` wires `exclude-tests`, `--markdown-output`, and runs `aoci_index.py` only once
        action_txt = (SKILL_DIR / "action.yml").read_text(encoding="utf-8")
        self.assertIn("exclude-tests:", action_txt)
        self.assertIn("INPUT_EXCLUDE_TESTS:", action_txt)
        self.assertIn("--exclude-tests", action_txt)
        self.assertIn("--markdown-output", action_txt)
        self.assertEqual(action_txt.count("scripts/aoci_index.py"), 1)

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_2_mcp_working_diff_cache_invalidates_on_commit_and_git_worktree(self) -> None:
        self._git("init", "-q")
        self.write("src/a.py", "def fa():\n    return 1\n")
        self.write("src/b.py", "from src.a import fa\ndef fb():\n    return fa()\n")
        self._git("add", ".")
        self._git("commit", "-qm", "init")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/a.py[CX7T]: F:computes base value fa | R:- | A:fa<-src/b.py | S:keep non-zero",
                "src/b.py[AX6T]: F:calls fa in fb | R:src/a.py | A:fb | S:-",
            )[0],
            0,
        )

        # Modify `src/a.py` in working tree -> diff cache records 1 changed entry
        self.write("src/a.py", "def fa():\n    return 2\n", aged=False)
        slot = aoci_mcp._CACHE.get(self.root)
        self.assertIsNotNone(slot)
        d_dirty = aoci_mcp._CACHE.get_working_diff_briefing(self.root, slot, None)
        self.assertEqual(len(d_dirty["changed_entries"]), 1)

        # Immediately stage & commit within `_DIFF_TTL_SECONDS` -> head/index key changes, cache invalidates!
        self._git("add", "src/a.py")
        self._git("commit", "-qm", "update fa")
        d_clean = aoci_mcp._CACHE.get_working_diff_briefing(self.root, slot, None)
        self.assertEqual(d_clean["changed_entries"], [])

        # Linked Git worktree (`.git` is a regular file pointing to `worktrees/<name>`, not a directory!)
        with tempfile.TemporaryDirectory() as wt_parent:
            wt_dir = Path(wt_parent) / "wt"
            self._git("worktree", "add", "-q", "-b", "feat-wt", str(wt_dir))
            try:
                self.assertTrue((wt_dir / ".git").is_file())
                cli_json("init", "--root", str(wt_dir))
                rc_u, out_u = cli(
                    "upsert",
                    "--root",
                    str(wt_dir),
                    "--entry",
                    "src/a.py[CX7T]: F:computes base value fa | R:- | A:fa<-src/b.py | S:keep non-zero",
                    "--entry",
                    "src/b.py[AX6T]: F:calls fa in fb | R:src/a.py | A:fb | S:-",
                    "--json",
                )
                self.assertEqual(rc_u, 0, out_u)

                (wt_dir / "src" / "a.py").write_text("def fa():\n    return 3\n", encoding="utf-8")
                wt_slot = aoci_mcp._CACHE.get(wt_dir)
                self.assertIsNotNone(wt_slot)
                wt_dirty = aoci_mcp._CACHE.get_working_diff_briefing(wt_dir, wt_slot, None)
                self.assertEqual(len(wt_dirty["changed_entries"]), 1)

                self._git("add", "src/a.py", cwd=wt_dir)
                self._git("commit", "-qm", "wt commit", cwd=wt_dir)
                wt_after_commit = aoci_mcp._CACHE.get_working_diff_briefing(wt_dir, wt_slot, None)
                self.assertEqual(wt_after_commit["changed_entries"], [])
            finally:
                self._git("worktree", "remove", "--force", str(wt_dir))

    def test_3_apply_sync_pending_dir_and_clean_pending_cli_and_mcp(self) -> None:
        self.write("src/one.py", "def one():\n    return 1\n")
        self.write("src/two.py", "def two():\n    return 2\n")
        self.write("src/three.py", "def three():\n    return 3\n")
        self.init()

        pdir = self.root / ".aoci" / "pending"
        pdir.mkdir(parents=True, exist_ok=True)
        b1 = pdir / "batch_01.txt"
        b2 = pdir / "batch_02.txt"
        b1.write_text("src/one.py[CX5T]: F:returns integer one | R:- | A:one | S:-\n", encoding="utf-8")
        b2.write_text("src/two.py[CX5T]: F:returns integer two | R:- | A:two | S:-\n", encoding="utf-8")

        # Dry-run previews `upsert` from `--pending-dir` without deleting batch files
        dry = cli_json(
            "apply-sync",
            "--root",
            str(self.root),
            "--pending-dir",
            ".aoci/pending",
            "--clean-pending",
            "--dry-run",
        )
        self.assertTrue(dry["ok"], dry)
        self.assertTrue(b1.is_file())
        self.assertTrue(b2.is_file())

        # Add third batch so `verify` also passes 100% cleanly after merge
        b3 = pdir / "batch_03.txt"
        b3.write_text("src/three.py[CX5T]: F:returns integer three | R:- | A:three | S:-\n", encoding="utf-8")

        applied = cli_json(
            "apply-sync",
            "--root",
            str(self.root),
            "--pending-dir",
            ".aoci/pending",
            "--clean-pending",
        )
        self.assertTrue(applied["ok"], applied)
        self.assertEqual([s["step"] for s in applied["steps"]], ["upsert", "verify"])
        self.assertEqual(list(pdir.glob("*.txt")), [])

        # MCP `aoci_apply_sync` with `pending_dir` and `clean_pending`
        self.write("src/four.py", "def four():\n    return 4\n")
        b4 = pdir / "batch_04.txt"
        b4.write_text("src/four.py[CX5T]: F:returns integer four | R:- | A:four | S:-\n", encoding="utf-8")
        is_err, raw_mcp = aoci_mcp.call_tool(
            "aoci_apply_sync",
            {
                "root": str(self.root),
                "pending_dir": ".aoci/pending",
                "clean_pending": True,
            },
        )
        self.assertFalse(is_err, raw_mcp)
        mcp_obj = json.loads(raw_mcp)
        self.assertTrue(mcp_obj["ok"])
        self.assertFalse(b4.exists())

    def test_4_mcp_all_27_tools_declare_output_schema_and_reinit_clears_subscriptions(self) -> None:
        tools_resp = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = tools_resp["result"]["tools"]
        self.assertEqual(len(tools), 27)
        missing_schema = [t["name"] for t in tools if not isinstance(t.get("outputSchema"), dict)]
        self.assertEqual(missing_schema, [])
        for t in tools:
            self.assertEqual(t["outputSchema"].get("type"), "object", t["name"])

        self.write("app.py", "def run():\n    return 42\n")
        for tool_name, tool_args in (
            ("aoci_init", {"root": str(self.root)}),
            ("aoci_scan", {"root": str(self.root)}),
            ("aoci_outline", {"root": str(self.root), "paths": ["app.py"]}),
            ("aoci_upsert", {"root": str(self.root), "entries": ["app.py[EX5T]: F:runs entrypoint | R:- | A:run | S:-"]}),
            ("aoci_verify", {"root": str(self.root)}),
            ("aoci_pool", {"root": str(self.root), "action": "stats"}),
            ("aoci_ensure_git_exclude", {"root": str(self.root)}),
        ):
            with self.subTest(tool=tool_name):
                resp = aoci_mcp.handle_rpc_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": tool_args},
                    }
                )
                res = resp["result"]
                self.assertFalse(res["isError"], res)
                self.assertIsInstance(res.get("structuredContent"), dict, tool_name)

        # Subscribe, then re-run `initialize` -> stale subscriptions from prior session must be cleared
        sub_uri = f"aoci://code?root={self.root}"
        aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 20, "method": "resources/subscribe", "params": {"uri": sub_uri}}
        )
        self.assertIn(sub_uri, aoci_mcp._SUBSCRIBED_URIS)
        aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
        self.assertEqual(len(aoci_mcp._SUBSCRIBED_URIS), 0)

    def test_5_infer_layer_and_domain_token_stem_and_path_hint_matching(self) -> None:
        domains = {
            "G": "Global",
            "X": "Core",
            "A": "authentication",
            "H": "hooks-and-hosts",
            "U": "user_management",
            "M": "mcp-server",
            "B": "billing (packages/pay)",
        }
        self.assertEqual(audit._infer_layer_and_domain("src/auth/jwt.py", domains), ("C", "A"))
        self.assertEqual(audit._infer_layer_and_domain("scripts/hooks/runner.py", domains), ("C", "H"))
        self.assertEqual(audit._infer_layer_and_domain("app/users/profile.py", domains), ("C", "U"))
        self.assertEqual(audit._infer_layer_and_domain("scripts/aoci_mcp.py", domains), ("C", "M"))
        self.assertEqual(audit._infer_layer_and_domain("packages/pay/stripe.py", domains), ("C", "B"))
        self.assertEqual(audit._infer_layer_and_domain("README.md", domains), ("O", "G"))

    @unittest.skipUnless(_has_real_treesitter(), "tree-sitter backend not installed")
    def test_6_real_treesitter_kotlin_vue_sfc_and_hub_spans_parity(self) -> None:
        vue_src = (
            "<template>\n"
            "  <div class=\"card\">Hello</div>\n"
            "</template>\n"
            "\n"
            "<script setup lang=\"ts\">\n"
            "import { ref } from 'vue';\n"
            "import { fetchUser } from './api';\n"
            "\n"
            "export interface CardProps {\n"
            "  id: string;\n"
            "}\n"
            "\n"
            "export function formatTitle(name: string): string {\n"
            "  return name.trim();\n"
            "}\n"
            "</script>\n"
        )
        vue_p = self.write("web/Card.vue", vue_src)
        kt_src = (
            "package com.app\n"
            "import com.app.repo.UserRepo\n"
            "\n"
            "class AuthService(\n"
            "    private val repo: UserRepo\n"
            ") {\n"
            "    fun login(user: String): Boolean {\n"
            "        return user.isNotEmpty()\n"
            "    }\n"
            "}\n"
            "\n"
            "enum class LoginState {\n"
            "    OK,\n"
            "    DENIED\n"
            "}\n"
        )
        kt_p = self.write("src/AuthService.kt", kt_src)
        ts_arrow_src = (
            "export const computeSum = (\n"
            "  a: number,\n"
            "  b: number,\n"
            "): number => (\n"
            "  a + b\n"
            ");\n"
        )
        ts_p = self.write("web/math.ts", ts_arrow_src)

        with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "1"}, clear=False):
            vue_rx = parsers.extract_file_outline(vue_p, "web/Card.vue")
            kt_rx = parsers.extract_file_outline(kt_p, "src/AuthService.kt")
            vue_spans_rx = hubs.extract_symbol_spans(vue_p, "web/Card.vue")
            kt_spans_rx = hubs.extract_symbol_spans(kt_p, "src/AuthService.kt")

        with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False):
            vue_ts = parsers.extract_file_outline(vue_p, "web/Card.vue")
            kt_ts = parsers.extract_file_outline(kt_p, "src/AuthService.kt")
            vue_spans_ts = hubs.extract_symbol_spans(vue_p, "web/Card.vue")
            kt_spans_ts = hubs.extract_symbol_spans(kt_p, "src/AuthService.kt")
            arrow_spans_ts = hubs.extract_symbol_spans(ts_p, "web/math.ts")

        self.assertEqual(vue_ts["imports"], vue_rx["imports"])
        self.assertEqual(vue_ts["symbols"], vue_rx["symbols"])
        self.assertIn("interface CardProps [L9]", vue_ts["symbols"])
        self.assertIn("function formatTitle [L13]", vue_ts["symbols"])

        self.assertEqual(kt_ts["imports"], kt_rx["imports"])
        self.assertEqual([s for s in kt_rx["symbols"] if s not in kt_ts["symbols"]], [])
        self.assertIn("enum LoginState [L12]", kt_ts["symbols"])

        self.assertEqual(vue_spans_ts, vue_spans_rx)
        self.assertEqual(kt_spans_ts, kt_spans_rx)
        self.assertEqual(arrow_spans_ts, [{"path": "web/math.ts", "symbol": "computeSum", "kind": "const", "start": 1, "end": 6}])

    def test_7_mocked_treesitter_kotlin_vue_and_span_fallback(self) -> None:
        class FakeNode:
            def __init__(
                self,
                ntype: str,
                s_b: int,
                e_b: int,
                s_pt: tuple,
                e_pt: tuple,
                children: Optional[List["FakeNode"]] = None,
                fields: Optional[Dict[str, "FakeNode"]] = None,
                has_error: bool = False,
            ) -> None:
                self.type = ntype
                self.start_byte = s_b
                self.end_byte = e_b
                self.start_point = s_pt
                self.end_point = e_pt
                self.children = children or []
                self._fields = fields or {}
                self.has_error = has_error

            def child_by_field_name(self, name: str) -> Optional["FakeNode"]:
                return self._fields.get(name)

        kt_bytes = b"import foo.Bar\nclass Svc {\n  fun run() {}\n}\n"
        imp_hdr = FakeNode("import_header", 0, 14, (0, 0), (0, 14))
        imp_list = FakeNode("import_list", 0, 14, (0, 0), (0, 14), [imp_hdr])
        cls_kw = FakeNode("class", 15, 20, (1, 0), (1, 5))
        cls_id = FakeNode("type_identifier", 21, 24, (1, 6), (1, 9))
        fn_id = FakeNode("simple_identifier", 33, 36, (2, 6), (2, 9))
        fn_decl = FakeNode("function_declaration", 29, 41, (2, 2), (2, 14), [fn_id], {"name": fn_id})
        cls_body = FakeNode("class_body", 25, 43, (1, 10), (3, 1), [fn_decl])
        cls_decl = FakeNode("class_declaration", 15, 43, (1, 0), (3, 1), [cls_kw, cls_id, cls_body])
        kt_root = FakeNode("source_file", 0, len(kt_bytes), (0, 0), (4, 0), [imp_list, cls_decl])

        class FakeKtParser:
            def parse(self, _b: bytes) -> Any:
                return type("FakeTree", (), {"root_node": kt_root})()

        ts_accel.reset_treesitter_probe()
        try:
            with mock.patch.object(ts_accel, "_probe_backend", return_value=lambda _lang: FakeKtParser()):
                with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False):
                    imps, syms = ts_accel.extract_outline_with_treesitter(kt_bytes, ".kt", line_offset=5)  # type: ignore[misc]
                    self.assertEqual(imps, ["import foo.Bar"])
                    self.assertEqual(syms, ["class Svc [L7]", "fun run [L8]"])
                    raw_spans = ts_accel.extract_raw_symbol_spans_with_treesitter(kt_bytes, ".kt")
                    self.assertEqual(raw_spans, [("class Svc", 2, 4), ("fun run", 3, 3)])
        finally:
            ts_accel.reset_treesitter_probe()


if __name__ == "__main__":
    unittest.main(verbosity=2)
