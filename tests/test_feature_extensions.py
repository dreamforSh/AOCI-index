#!/usr/bin/env python3
"""End-to-end tests for all 7 AOCI high-value feature extensions."""

from __future__ import annotations

import gzip
import io
import json
import os
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

from aoci_testkit import HAS_GIT, SKILL_DIR, RepoTestCase, cli, cli_json, hook  # noqa: E402
import aoci_mcp  # noqa: E402
from aoci_core import installer, ts_accel  # noqa: E402


class FeatureExtensionsTests(RepoTestCase):
    def _git(self, *args: str) -> str:
        r = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return r.stdout

    def _run_installer(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = installer.main(list(args))
        return rc, out.getvalue() + err.getvalue()

    def test_1_multi_host_installer_cursor_windsurf_vscode_and_cursor_hook(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            proj = Path(td) / "proj"
            home.mkdir()
            proj.mkdir()

            # Cursor (project scope): `.cursor/skills`, `.cursor/rules/aoci-index.mdc`, `.cursor/hooks.json`, `.cursor/mcp.json`
            rc, log = self._run_installer(
                "--host", "cursor", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home),
                "--with-hooks", "--with-rules", "--with-mcp",
            )
            self.assertEqual(rc, 0, log)
            self.assertTrue((proj / ".cursor" / "skills" / "aoci-index" / "SKILL.md").is_file())
            mdc_txt = (proj / ".cursor" / "rules" / "aoci-index.mdc").read_text(encoding="utf-8")
            self.assertIn("alwaysApply: true", mdc_txt)
            cursor_hooks = json.loads((proj / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
            self.assertEqual(cursor_hooks["version"], 1)
            self.assertIn("sessionStart", cursor_hooks["hooks"])
            self.assertIn("beforeSubmitPrompt", cursor_hooks["hooks"])
            self.assertIn("stop", cursor_hooks["hooks"])
            cursor_mcp = json.loads((proj / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
            self.assertIn("aoci-index", cursor_mcp["mcpServers"])

            # Windsurf (global + project)
            rc_w, log_w = self._run_installer(
                "--host", "windsurf", "--scope", "global", "--home-dir", str(home),
                "--with-rules", "--with-mcp",
            )
            self.assertEqual(rc_w, 0, log_w)
            self.assertTrue((home / ".codeium" / "windsurf" / "skills" / "aoci-index" / "SKILL.md").is_file())
            self.assertTrue((home / ".codeium" / "windsurf" / "memories" / "global_rules.md").is_file())
            ws_mcp = json.loads((home / ".codeium" / "windsurf" / "mcp_config.json").read_text(encoding="utf-8"))
            self.assertIn("aoci-index", ws_mcp["mcpServers"])

            # VS Code / GitHub Copilot (`servers` + `type: stdio` in `.vscode/mcp.json`)
            rc_v, log_v = self._run_installer(
                "--host", "vscode", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home),
                "--with-rules", "--with-mcp",
            )
            self.assertEqual(rc_v, 0, log_v)
            self.assertTrue((proj / ".github" / "skills" / "aoci-index" / "SKILL.md").is_file())
            self.assertTrue((proj / ".github" / "copilot-instructions.md").is_file())
            vsc_mcp = json.loads((proj / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(vsc_mcp["servers"]["aoci-index"]["type"], "stdio")

            # Clean surgical uninstall of Cursor + VS Code leaves `proj` clean
            self.assertEqual(
                self._run_installer("--host", "cursor", "--scope", "project", "--project-root", str(proj), "--uninstall")[0],
                0,
            )
            self.assertEqual(
                self._run_installer("--host", "vscode", "--scope", "project", "--project-root", str(proj), "--uninstall")[0],
                0,
            )
            self.assertFalse((proj / ".cursor").exists())
            self.assertFalse((proj / ".vscode").exists())
            self.assertFalse((proj / ".github").exists())

        # Cursor lifecycle hook protocol (`additional_context` on sessionStart, `followup_message` on stop)
        self.write("app.py", "X = 1\n")
        self.init()
        self.assertEqual(self.upsert("app.py[CX3T]: F:defines X constant | R:- | A:X | S:-")[0], 0)
        start_out = hook("session-start", {"conversation_id": "cur-1", "workspace_roots": [str(self.root)]}, host="cursor")
        self.assertIn("additional_context", start_out)
        self.assertIn("AOCI Index Active", start_out["additional_context"])

        self.write("new_mod.py", "Y = 2\n")
        stop_out = hook("stop", {"conversation_id": "cur-1", "workspace_roots": [str(self.root)]}, host="cursor")
        self.assertIn("followup_message", stop_out)
        self.assertIn("new_mod.py", stop_out["followup_message"])

    def test_2_hub_aware_semantic_pool_line_shift_resilient_restore(self):
        hub_src = (
            "def helper_a(x: int) -> int:\n"
            "    val = x + 1\n"
            "    return val * 2\n\n"
            "def critical_engine(payload: dict) -> bool:\n"
            "    if not payload:\n"
            "        return False\n"
            "    return bool(payload.get('ok'))\n"
        )
        self.write("src/engine.py", hub_src)
        self.init()
        self.assertEqual(
            self.upsert("src/engine.py[KX8T]: F:runs core validation engine | R:- | A:critical_engine | S:must not mutate payload")[0],
            0,
        )
        zoom_res = cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "src/engine.py#critical_engine[L5-L8]: F:validates incoming payload dict | S:never mutate caller dict",
            "--json",
        )
        self.assertTrue(zoom_res["ok"])

        bundle_path = Path(self._tmp.name) / "hub_pool.json.gz"
        exp = cli_json("pool", "export", "--root", str(self.root), "--output", str(bundle_path), "--json")
        self.assertTrue(exp["ok"])
        self.assertGreaterEqual(exp["exported_hubs"], 1)

        # Create a brand-new workspace where 20 header lines were inserted ABOVE `critical_engine`!
        # Whole-file SHA changed completely, and `critical_engine` moved from [L5-L8] -> [L25-L28]!
        shifted_root = Path(self._tmp.name) / "shifted_repo"
        shifted_root.mkdir()
        prefix_lines = "".join(f"# banner line {i}\n" for i in range(20))
        (shifted_root / "src").mkdir()
        (shifted_root / "src" / "engine.py").write_text(prefix_lines + hub_src, encoding="utf-8")

        cli_json("init", "--root", str(shifted_root))
        cli_json(
            "upsert",
            "--root",
            str(shifted_root),
            "--entry",
            "src/engine.py[KX8S]: F:runs core validation engine | R:- | A:critical_engine | S:must not mutate payload",
        )
        imp = cli_json("pool", "import", "--root", str(shifted_root), "--input", str(bundle_path), "--json")
        self.assertTrue(imp["ok"])
        self.assertGreaterEqual(imp["imported_hubs"], 1)

        rest = cli_json("restore", "--root", str(shifted_root), "--json")
        self.assertTrue(rest["ok"])
        self.assertIn("src/engine.py#critical_engine", rest.get("restored_hubs", []))

        hubs_txt = (shifted_root / ".aoci" / "aoci.hubs.txt").read_text(encoding="utf-8")
        self.assertIn("src/engine.py#critical_engine[L25-L28]:", hubs_txt)
        self.assertIn("never mutate caller dict", hubs_txt)

    def test_3_batch_pack_and_upsert_pending_dir_atomic_merge(self):
        self.write("pkg/alpha.py", "def alpha(x: int) -> int:\n    return x + 1\n")
        self.write("pkg/beta.py", "from pkg.alpha import alpha\ndef beta() -> int:\n    return alpha(2)\n")
        self.write("svc/gamma.py", "def gamma() -> str:\n    return 'g'\n")
        self.init()

        # Pack Batch 1 (size 2) via MCP `aoci_batch_pack` with bounded source excerpts
        err, out = aoci_mcp.call_tool(
            "aoci_batch_pack",
            {"root": str(self.root), "batch": 1, "batch_size": 2, "with_source": True},
        )
        self.assertFalse(err, out)
        bp1 = json.loads(out)
        self.assertEqual(bp1["batch"], 1)
        self.assertEqual(bp1["total_batches"], 2)
        self.assertEqual(bp1["total_files"], 2)
        self.assertTrue(any("def alpha" in f.get("source_excerpt", "") for f in bp1["files"]))

        # Simulate two parallel subagents writing `.aoci/pending/batch_01.txt` and `batch_02.txt`
        pdir = self.root / ".aoci" / "pending"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "batch_01.txt").write_text(
            "pkg/alpha.py[CX4T]: F:increments integer input | R:- | A:alpha<-pkg/beta.py | S:-\n"
            "pkg/beta.py[AX4T]: F:invokes alpha helper | R:pkg/alpha.py | A:beta | S:-\n",
            encoding="utf-8",
        )
        (pdir / "batch_02.txt").write_text(
            "svc/gamma.py[AX3T]: F:returns gamma marker | R:- | A:<TODO:unfilled> | S:-\n",
            encoding="utf-8",
        )

        # Validation error reports exact batch filename & line provenance (`[batch_02.txt:L1]`), leaving files intact
        err_bad, out_bad = aoci_mcp.call_tool(
            "aoci_upsert",
            {"root": str(self.root), "pending_dir": ".aoci/pending", "clean_pending": True},
        )
        self.assertTrue(err_bad)
        bad_obj = json.loads(out_bad)
        self.assertTrue(any("batch_02.txt:L1" in e for e in bad_obj["errors"]), bad_obj)
        self.assertTrue((pdir / "batch_01.txt").is_file())

        # Fix `batch_02.txt` and merge all batches atomically with `clean_pending=True`
        (pdir / "batch_02.txt").write_text(
            "svc/gamma.py[AX3T]: F:returns gamma marker | R:- | A:gamma | S:-\n",
            encoding="utf-8",
        )
        err_ok, out_ok = aoci_mcp.call_tool(
            "aoci_upsert",
            {"root": str(self.root), "pending_dir": ".aoci/pending", "clean_pending": True},
        )
        self.assertFalse(err_ok, out_ok)
        ok_obj = json.loads(out_ok)
        self.assertEqual(ok_obj["total_indexed"], 3)
        self.assertEqual(list(pdir.glob("*.txt")), [])

    def test_4_ci_check_zero_index_sarif_github_annotations_and_pr_markdown(self):
        if not HAS_GIT:
            self.skipTest("git required")
        self._git("init", "-q")
        # Domain file (`domain/order.py` -> layer `D`) illegally importing Entrypoint (`routes/http.py` -> layer `E`) + cycle!
        self.write("routes/http.py", "from domain.order import Order\ndef serve(): return Order()\n")
        self.write("domain/order.py", "from routes.http import serve\nclass Order:\n    pass\n")
        self.write("tests/test_order.py", "from domain.order import Order\ndef test_o(): assert Order()\n")
        self._git("add", ".")
        self._git("commit", "-qm", "base")

        # Modify `domain/order.py` on feature worktree WITHOUT `.aoci/` initialized!
        self.write("domain/order.py", "from routes.http import serve\nclass Order:\n    total = 0\n")
        self.assertFalse((self.root / ".aoci").exists())

        sarif_file = self.root / "aoci.sarif"
        rc_strict, out_gh = cli(
            "ci-check",
            "--root",
            str(self.root),
            "--base-rev",
            "HEAD",
            "--format",
            "github",
            "--sarif-output",
            str(sarif_file),
            "--strict",
        )
        self.assertEqual(rc_strict, 1)
        self.assertIn("::error file=domain/order.py", out_gh)
        self.assertIn("::warning file=", out_gh)
        # Zero-index CI check must NOT create `.aoci/` on disk!
        self.assertFalse((self.root / ".aoci").exists())

        sarif_doc = json.loads(sarif_file.read_text(encoding="utf-8"))
        self.assertEqual(sarif_doc["version"], "2.1.0")
        rule_ids = {r["ruleId"] for r in sarif_doc["runs"][0]["results"]}
        self.assertIn("AOCI001", rule_ids)
        self.assertIn("AOCI002", rule_ids)

        err_md, out_md = aoci_mcp.call_tool(
            "aoci_ci_check",
            {"root": str(self.root), "base_rev": "HEAD", "format": "markdown"},
        )
        self.assertFalse(err_md, out_md)
        self.assertIn("## AOCI Architecture Check", out_md)
        self.assertIn("tests/test_order.py", out_md)
        self.assertTrue((SKILL_DIR / "action.yml").is_file())

    def test_5_mcp_2025_06_18_roots_list_structured_content_and_resource_subscriptions(self):
        self.write("core.py", "A = 1\n")
        self.init()
        self.assertEqual(self.upsert("core.py[CX4T]: F:defines core constant A | R:- | A:A | S:-")[0], 0)

        aoci_mcp.drain_outbound_messages()
        aoci_mcp._CLIENT_ROOT_HINTS.clear()
        aoci_mcp._CLIENT_ROOT_HINT = None
        aoci_mcp._SUBSCRIBED_URIS.clear()

        # 1) Initialize with `2025-06-18` and `capabilities.roots` (no `rootUri` yet)
        init_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"roots": {"listChanged": True}},
                },
            }
        )
        self.assertEqual(init_resp["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(init_resp["result"]["capabilities"]["resources"]["subscribe"])

        # 2) `notifications/initialized` triggers outbound `roots/list` request; client replies with `self.root` URI
        self.assertIsNone(aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        outbound_reqs = aoci_mcp.drain_outbound_messages()
        self.assertEqual(len(outbound_reqs), 1)
        self.assertEqual(outbound_reqs[0]["method"], "roots/list")

        self.assertIsNone(
            aoci_mcp.handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": outbound_reqs[0]["id"],
                    "result": {"roots": [{"uri": self.root.resolve().as_uri(), "name": "test-repo"}]},
                }
            )
        )
        self.assertEqual(Path(aoci_mcp._CLIENT_ROOT_HINT).resolve(), self.root.resolve())

        # 3) `tools/call` on structured tool returns `structuredContent` matching JSON payload
        ov_resp = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "aoci_overview", "arguments": {}}}
        )
        self.assertFalse(ov_resp["result"]["isError"])
        self.assertIn("structuredContent", ov_resp["result"])
        self.assertEqual(ov_resp["result"]["structuredContent"]["indexed_files"], 1)

        # 4) `resources/subscribe` -> mutating `aoci_upsert` emits `notifications/resources/updated`
        sub_uri = f"aoci://code?root={self.root}"
        sub_resp = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 3, "method": "resources/subscribe", "params": {"uri": sub_uri}}
        )
        self.assertEqual(sub_resp["result"], {})

        self.write("extra.py", "B = 2\n")
        up_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "aoci_upsert",
                    "arguments": {
                        "root": str(self.root),
                        "entries": ["extra.py[CX2T]: F:defines extra constant B | R:- | A:B | S:-"],
                    },
                },
            }
        )
        self.assertFalse(up_resp["result"]["isError"])
        notifs = aoci_mcp.drain_outbound_messages()
        self.assertTrue(
            any(n.get("method") == "notifications/resources/updated" and n["params"]["uri"] == sub_uri for n in notifs),
            notifs,
        )

    def test_6_optional_treesitter_accelerator_and_env_disable(self):
        pyproject_txt = (SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[project.optional-dependencies]", pyproject_txt)
        self.assertIn("tree-sitter-language-pack", pyproject_txt)

        # Mock a lightweight AST node tree to verify dynamic `tree-sitter` path + `AOCI_DISABLE_TREESITTER=1` fallback
        class FakeNode:
            def __init__(self, ntype, start_b, end_b, line0, children=None, fields=None):
                self.type = ntype
                self.start_byte = start_b
                self.end_byte = end_b
                self.start_point = (line0, 0)
                self.children = children or []
                self._fields = fields or {}

            def child_by_field_name(self, name):
                return self._fields.get(name)

        src = b"import { x } from './dep';\nexport function tsFastFn() { return x; }\n"
        str_node = FakeNode("string", 18, 25, 0)
        imp_node = FakeNode("import_statement", 0, 26, 0, [str_node], {"source": str_node})
        id_node = FakeNode("identifier", 43, 51, 1)
        fn_node = FakeNode("function_declaration", 34, 67, 1, [id_node], {"name": id_node})
        exp_node = FakeNode("export_statement", 27, 67, 1, [fn_node])
        root_node = FakeNode("program", 0, len(src), 0, [imp_node, exp_node])

        class FakeParser:
            def parse(self, _b):
                return type("FakeTree", (), {"root_node": root_node})()

        ts_accel.reset_treesitter_probe()
        try:
            with mock.patch.object(ts_accel, "_probe_backend", return_value=lambda _lang: FakeParser()):
                with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False):
                    res_ts = ts_accel.extract_outline_with_treesitter(src, ".ts")
                    self.assertEqual(res_ts, (["./dep"], ["function tsFastFn [L2]"]))
                with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "1"}, clear=False):
                    self.assertFalse(ts_accel.is_enabled())
                    self.assertIsNone(ts_accel.extract_outline_with_treesitter(src, ".ts"))
        finally:
            ts_accel.reset_treesitter_probe()

    def test_7_html_graph_diff_highlighting_and_dogfood_self_pool_bundle(self):
        if not HAS_GIT:
            self.skipTest("git required")
        self._git("init", "-q")
        self.write("src/db.py", "def query(): return []\n")
        self.write("src/api.py", "from src.db import query\ndef handle(): return query()\n")
        self._git("add", ".")
        self._git("commit", "-qm", "init")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/db.py[PX6T]: F:executes database queries | R:- | A:query<-src/api.py | S:-",
                "src/api.py[EX5T]: F:serves API endpoint | R:src/db.py | A:handle | S:-",
            )[0],
            0,
        )
        # Modify `src/db.py` -> `src/db.py` becomes `"changed"`, `src/api.py` becomes `"affected"`!
        self.write("src/db.py", "def query(): return [1]\n")
        html_out = self.root / "graph.html"
        err_exp, out_exp = aoci_mcp.call_tool(
            "aoci_export",
            {"root": str(self.root), "format": "html", "diff": True, "output": "graph.html"},
        )
        self.assertFalse(err_exp, out_exp)
        html_txt = html_out.read_text(encoding="utf-8")
        self.assertIn('id="diffOnly"', html_txt)
        self.assertIn('"diff": "changed"', html_txt)
        self.assertIn('"diff": "affected"', html_txt)

        # Verify `examples/self-pool.json.gz` dogfooding bundle imports with 0 rejections and restores cleanly
        self_bundle = SKILL_DIR / "examples" / "self-pool.json.gz"
        self.assertTrue(self_bundle.is_file(), f"Missing {self_bundle}")
        bundle_data = json.loads(gzip.decompress(self_bundle.read_bytes()).decode("utf-8"))
        self.assertGreaterEqual(len(bundle_data["by_path_sha"]), 60)
        self.assertGreaterEqual(len(bundle_data["hubs_by_symbol_sha"]), 6)

        with tempfile.TemporaryDirectory() as td:
            clone_dir = Path(td) / "aoci_clone"
            shutil.copytree(
                SKILL_DIR,
                clone_dir,
                ignore=shutil.ignore_patterns(
                    ".git", ".aoci", "__pycache__", "dist", "build", ".venv", "venv", ".idea", "*.egg-info"
                ),
            )
            cli_json("init", "--root", str(clone_dir))
            # Declare project domains matching `self-pool.json.gz` (`G`, `K`, `M`, `H`, `P`)
            meta_p = clone_dir / ".aoci" / "aoci.meta.txt"
            meta_txt = meta_p.read_text(encoding="utf-8")
            meta_p.write_text(
                meta_txt
                + "\n#Domains: G=governance, K=kernel, M=mcp, H=hooks-and-hosts, P=parsers\n",
                encoding="utf-8",
            )
            imp_self = cli_json("pool", "import", "--root", str(clone_dir), "--input", str(self_bundle), "--json")
            self.assertTrue(imp_self["ok"], imp_self)
            self.assertEqual(imp_self["rejected"], 0, imp_self)

            rest_self = cli_json("restore", "--root", str(clone_dir), "--json")
            self.assertTrue(rest_self["ok"], rest_self)
            self.assertGreaterEqual(
                len(rest_self["restored"]),
                60,
                "examples/self-pool.json.gz is out of date; run `python .github/scripts/refresh_self_pool.py`.",
            )
            self.assertGreaterEqual(
                len(rest_self.get("restored_hubs", [])),
                6,
                "examples/self-pool.json.gz hubs are out of date; run `python .github/scripts/refresh_self_pool.py`.",
            )

    def test_8_refresh_self_pool_script_check_and_deterministic_roundtrip(self):
        refresh_script = SKILL_DIR / ".github" / "scripts" / "refresh_self_pool.py"
        self.assertTrue(refresh_script.is_file(), f"Missing {refresh_script}")

        # 1) Live repository self-pool MUST report zero stale files or hubs
        live_chk = subprocess.run(
            [sys.executable, "-B", str(refresh_script), "--root", str(SKILL_DIR), "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            live_chk.returncode,
            0,
            f"examples/self-pool.json.gz is stale!\n{live_chk.stdout}{live_chk.stderr}\n"
            "Fix by running: python .github/scripts/refresh_self_pool.py",
        )
        self.assertIn("0 stale", live_chk.stdout)

        # 2) In an isolated sandbox, `--check` reports stale file + hub without modifying the bundle,
        #    and running refresh with `--entry-file` deterministically updates SHAs (`mtime=0`).
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td) / "sandbox"
            (sb / "scripts").mkdir(parents=True)
            (sb / "examples").mkdir(parents=True)
            mod_file = sb / "scripts" / "sample.py"
            mod_file.write_text("def alpha(x: int) -> int:\n    val = x + 1\n    return val\n", encoding="utf-8")

            from aoci_core import hubs as _hubs_mod

            norm_b = _hubs_mod._read_norm_bytes(mod_file)
            span_sha = _hubs_mod.compute_span_body_sha256(norm_b, 1, 3)
            init_bundle = {
                "schema_version": 1,
                "format": "aoci-semantic-pool-v1",
                "exported_at": "2026-01-01T00:00:00Z",
                "by_path_sha": [
                    {
                        "path": "scripts/sample.py",
                        "sha256": "0" * 64,
                        "entry": "scripts/sample.py[CK5T]: F:computes alpha offset | R:- | A:alpha | S:-",
                    }
                ],
                "by_sha": [
                    {
                        "sha256": "0" * 64,
                        "entry": "scripts/sample.py[CK5T]: F:computes alpha offset | R:- | A:alpha | S:-",
                    }
                ],
                "hubs_by_symbol_sha": [
                    {
                        "path": "scripts/sample.py",
                        "symbol": "alpha",
                        "span_sha256": "f" * 64,
                        "f": "increments input integer",
                        "s": "pure arithmetic",
                    }
                ],
            }
            bundle_p = sb / "examples" / "self-pool.json.gz"
            orig_gz = gzip.compress(
                (json.dumps(init_bundle, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                compresslevel=9,
                mtime=0,
            )
            bundle_p.write_bytes(orig_gz)

            # Create symlink/copy to repo `scripts/aoci_core` so `--root sb` can bootstrap `aoci_core`
            shutil.copytree(
                SKILL_DIR / "scripts" / "aoci_core",
                sb / "scripts" / "aoci_core",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            stale_chk = subprocess.run(
                [sys.executable, "-B", str(refresh_script), "--root", str(sb), "--check"],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(stale_chk.returncode, 1)
            self.assertIn("scripts/sample.py", stale_chk.stderr)
            self.assertIn("scripts/sample.py#alpha", stale_chk.stderr)
            self.assertIn("refresh_self_pool.py", stale_chk.stderr)
            # `--check` must NOT modify disk!
            self.assertEqual(bundle_p.read_bytes(), orig_gz)

            ov_file = sb / "overrides.txt"
            ov_file.write_text(
                "scripts/sample.py[CK6T]: F:computes deterministic alpha offset | R:- | A:alpha | S:-\n",
                encoding="utf-8",
            )
            ref_run = subprocess.run(
                [sys.executable, "-B", str(refresh_script), "--root", str(sb), "--entry-file", str(ov_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(ref_run.returncode, 0, ref_run.stdout + ref_run.stderr)

            raw_after = bundle_p.read_bytes()
            # Gzip header bytes 4..8 store 32-bit little-endian mtime; deterministic builds require mtime == 0
            self.assertEqual(int.from_bytes(raw_after[4:8], "little"), 0)
            after_obj = json.loads(gzip.decompress(raw_after).decode("utf-8"))
            self.assertEqual(after_obj["exported_at"], "2026-01-01T00:00:00Z")
            self.assertIn("deterministic alpha offset", after_obj["by_path_sha"][0]["entry"])
            self.assertEqual(after_obj["hubs_by_symbol_sha"][0]["span_sha256"], span_sha)

            ok_chk = subprocess.run(
                [sys.executable, "-B", str(refresh_script), "--root", str(sb), "--check"],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(ok_chk.returncode, 0, ok_chk.stdout + ok_chk.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
