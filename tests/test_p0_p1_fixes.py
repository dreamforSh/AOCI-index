#!/usr/bin/env python3
"""Regression tests for P0, P1, and packaging fixes across CLI, MCP, Hooks, Sync, Parsers, and Release."""

from __future__ import annotations

import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_aoci import HAS_GIT, INSTALL, SKILL_DIR, RepoTestCase, aoci_hook, aoci_index, cli, cli_json, hook  # noqa: E402
import aoci_mcp  # noqa: E402
from aoci_core import hubs, ignore, installer, lock, parsers, pool  # noqa: E402


class P0P1FixesTests(RepoTestCase):
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

    def test_packaging_no_toplevel_install_wheel_bundle_and_reproducible_release(self):
        pyproject_txt = (SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn('"install"', pyproject_txt)
        self.assertIn('aoci-install = "aoci_core.installer:main"', pyproject_txt)
        self.assertIn('aoci-hook = "aoci_hook:main"', pyproject_txt)

        # Fake `site-packages/aoci_core/installer.py` WITHOUT `_skill_bundle` must refuse to copy `Lib`
        with tempfile.TemporaryDirectory() as td:
            fake_lib = Path(td) / "Lib"
            fake_sp = fake_lib / "site-packages" / "aoci_core"
            fake_sp.mkdir(parents=True)
            (fake_lib / "stdlib_sentinel.py").write_text("x = 1\n", encoding="utf-8")
            fake_installer = fake_sp / "installer.py"
            fake_installer.write_text("# stub\n", encoding="utf-8")

            self.assertIsNone(installer.resolve_source_dir(fake_installer))
            dest = Path(td) / "dest_skill"
            with mock.patch.object(installer, "SOURCE_DIR", None), mock.patch.object(
                installer, "resolve_source_dir", return_value=None
            ):
                err_buf = io.StringIO()
                with mock.patch("sys.stderr", err_buf):
                    ok = installer.copy_skill_tree(dest, dry_run=False, force=True)
                self.assertFalse(ok)
                self.assertFalse(dest.exists())
                self.assertIn("could not be located", err_buf.getvalue())

            # Adding verified `_skill_bundle` resolves cleanly
            bundle_dir = fake_sp / "_skill_bundle"
            (bundle_dir / "scripts").mkdir(parents=True)
            (bundle_dir / "SKILL.md").write_text("---\nname: aoci-index\n---\n", encoding="utf-8")
            (bundle_dir / "scripts" / "aoci_index.py").write_text("# cli\n", encoding="utf-8")
            self.assertEqual(installer.resolve_source_dir(fake_installer), bundle_dir.resolve())

        # Deterministic `.tar.gz` + CRLF normalization + PEP 440 RC tag check in `build_release.py`
        rel_script = SKILL_DIR / ".github" / "scripts" / "build_release.py"
        spec = importlib.util.spec_from_file_location("build_release_mod", rel_script)
        assert spec is not None and spec.loader is not None
        build_rel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_rel)

        self.assertEqual(build_rel.verify_version_consistency(SKILL_DIR, "v1.6.0-rc.1"), "1.6.0")
        with tempfile.TemporaryDirectory() as out1, tempfile.TemporaryDirectory() as out2:
            t1, z1 = build_rel.build_skill_archives(SKILL_DIR, Path(out1), "v1.6.0")
            time.sleep(0.05)
            t2, z2 = build_rel.build_skill_archives(SKILL_DIR, Path(out2), "v1.6.0")
            self.assertEqual(t1.read_bytes(), t2.read_bytes(), ".tar.gz must be byte-for-byte reproducible")
            self.assertEqual(z1.read_bytes(), z2.read_bytes(), ".zip must be byte-for-byte reproducible")

    def test_mcp_windows_uri_stderr_isolation_worker_escape_and_batch_rpc(self):
        # 1) Percent-encoded Windows drive colon URI (`%3A`)
        win_decoded = aoci_mcp._file_uri_to_path("file:///f%3A/CancriRecoleta/AOCI-index", require_dir=False)
        self.assertIsNotNone(win_decoded)
        assert win_decoded is not None
        self.assertFalse(win_decoded.startswith(("\\", "/")), f"Unexpected leading slash: {win_decoded!r}")
        self.assertTrue(win_decoded.lower().startswith("f:"))

        # Real workspace folder URI with `%3A` (on Windows) + subdirectory root climb
        sub_pkg = self.root / "packages" / "auth"
        sub_pkg.mkdir(parents=True)
        self.write("packages/auth/token.py", "def sign():\n    return 'ok'\n")
        self.init()
        self.assertEqual(
            self.upsert("packages/auth/token.py[MA7T]: F:signs short-lived auth tokens | R:- | A:sign | S:-")[0],
            0,
        )

        raw_uri = self.root.resolve().as_uri().replace(":", "%3A", 1)
        # Re-encode only the drive colon after `file:///`
        if "file:///" in self.root.resolve().as_uri():
            prefix_fixed = self.root.resolve().as_uri().split("://", 1)[1]
            raw_uri = "file://" + prefix_fixed.replace(":", "%3A", 1)
        init_res = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"workspaceFolders": [{"uri": raw_uri, "name": "repo"}]},
            }
        )
        self.assertIn("result", init_res)

        # Calling `aoci_overview` with nested subdirectory `packages/auth` resolves parent `.aoci`
        is_err, ov_txt = aoci_mcp.call_tool("aoci_overview", {"root": str(sub_pkg)})
        self.assertFalse(is_err, ov_txt)
        ov_mcp = json.loads(ov_txt)
        ov_cli = cli_json("overview", "--root", str(self.root))
        self.assertEqual(ov_mcp["indexed_files"], 1)
        self.assertEqual(ov_mcp["estimated_full_tokens"], ov_cli["estimated_full_tokens"])

        # `layer="-"` filter returns 0 entries instead of silently returning all entries
        is_err_q, q_txt = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "layer": "-"})
        self.assertFalse(is_err_q, q_txt)
        self.assertEqual(json.loads(q_txt)["count"], 0)
        q_cli = cli_json("query", "--root", str(self.root), "--layer", "-")
        self.assertEqual(q_cli["count"], 0)

        # 2) `stderr` warnings during `_run_cli_capture` must NOT corrupt JSON stdout
        orig_main = aoci_index.main

        def noisy_main(argv=None):
            sys.stderr.write("warning: LF will be replaced by CRLF\n")
            return orig_main(argv)

        with mock.patch.object(aoci_index, "main", side_effect=noisy_main):
            is_err_sc, sc_txt = aoci_mcp.call_tool("aoci_scan", {"root": str(self.root)})
        self.assertFalse(is_err_sc, sc_txt)
        sc_json = json.loads(sc_txt)
        self.assertEqual(sc_json["drift"]["indexed_clean"], 1)

        # 3) Worker-thread `sys.stdout.write` escape + invalid JSON `-32700` + JSON-RPC batch array
        aoci_mcp._ensure_stream_routers()

        def noisy_overview(name, args):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(lambda: sys.stdout.write("STRAY_WORKER_STDOUT_NOISE\n")).result()
            return False, '{"ok":true}'

        batch_req = json.dumps(
            [
                {"jsonrpc": "2.0", "id": 10, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "aoci_overview", "arguments": {}}},
            ]
        )
        wire_in = io.BytesIO((batch_req + "\n{bad json\n42\n").encode("utf-8"))
        wire_out = io.BytesIO()
        err_sink = io.StringIO()
        assert isinstance(sys.stderr, aoci_mcp._StreamRouter)
        prev_err_fb = sys.stderr.set_fallback(err_sink)
        try:
            with mock.patch.object(aoci_mcp, "call_tool", side_effect=noisy_overview):
                # Test real fallback redirection by temporarily leaving `out_stream=None` with mocked buffer
                fake_stdout_holder = mock.Mock()
                fake_stdout_holder.buffer = wire_out
                assert isinstance(sys.stdout, aoci_mcp._StreamRouter)
                prev_out_fb = sys.stdout.set_fallback(fake_stdout_holder)
                try:
                    aoci_mcp.serve_stdio(wire_in, None)
                finally:
                    sys.stdout.set_fallback(prev_out_fb)
        finally:
            sys.stderr.set_fallback(prev_err_fb)

        wire_lines = [json.loads(ln.decode("utf-8")) for ln in wire_out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(wire_lines), 3)
        self.assertIsInstance(wire_lines[0], list)
        self.assertEqual([r["id"] for r in wire_lines[0]], [10, 11])
        self.assertEqual(wire_lines[1]["error"]["code"], -32700)
        self.assertEqual(wire_lines[2]["error"]["code"], -32600)
        self.assertIn("STRAY_WORKER_STDOUT_NOISE", err_sink.getvalue())

        # Export `output` parameter & `aoci_install_git_hooks` tool
        is_err_ex, ex_txt = aoci_mcp.call_tool(
            "aoci_export",
            {"root": str(self.root), "direction": "TB", "output": ".aoci/pending/graph.mmd"},
        )
        self.assertFalse(is_err_ex, ex_txt)
        ex_obj = json.loads(ex_txt)
        self.assertTrue(Path(ex_obj["output_path"]).is_file())

        if HAS_GIT:
            is_err_gh, gh_txt = aoci_mcp.call_tool(
                "aoci_install_git_hooks",
                {"root": str(self.root), "with_pre_commit": True, "dry_run": True},
            )
            self.assertFalse(is_err_gh, gh_txt)
            self.assertTrue(json.loads(gh_txt)["ok"])

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_restore_hubs_sync_and_apply_sync_combined_flags(self):
        self.write("src/keep.py", "def run_job(x: int = 1) -> int:\n    return x\n")
        self.write("src/branch_only.py", "def helper() -> int:\n    return 2\n")
        self.write("src/contract.py", "def calc(a: int) -> int:\n    return a\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        self._git("branch", "-M", "main")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/keep.py[KA8T]: F:executes primary job pipeline | R:- | A:run_job | S:idempotent",
                "src/branch_only.py[CA6T]: F:branch specific helper routine | R:- | A:helper | S:-",
                "src/contract.py[CA7T]: F:computes integer calculation | R:- | A:calc | S:-",
            )[0],
            0,
        )
        z_res = cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "src/keep.py#run_job[L1-L2]: F:runs job returning integer x | S:-",
            "--upsert",
            "src/branch_only.py#helper[L1-L2]: F:returns branch constant two | S:-",
        )
        self.assertTrue(z_res["ok"])

        # Switch to a new branch that deletes `src/branch_only.py`
        self._git("checkout", "-q", "-b", "slim")
        self._git("rm", "-q", "src/branch_only.py")
        self._git("commit", "-q", "-m", "remove branch_only")

        # `apply-sync --restore` (without `--entry`) must prune `src/branch_only.py` from code AND hubs and pass `verify`!
        as_restore = cli_json("apply-sync", "--root", str(self.root), "--restore")
        self.assertTrue(as_restore["ok"], as_restore)
        hub_recs, _ = hubs.parse_hubs_volume(self.root / ".aoci" / "aoci.hubs.txt")
        self.assertEqual([f"{r['relpath']}#{r['symbol']}" for r in hub_recs], ["src/keep.py#run_job"])
        self.assertTrue(cli_json("verify", "--root", str(self.root))["ok"])

        # Now make an `impl_only` edit on `src/keep.py` AND a `contract_changed` edit on `src/contract.py`
        self.write("src/keep.py", "\n\ndef run_job(x: int = 1) -> int:\n    return x + 0\n")
        self.write("src/contract.py", "def calc(a: int, scale: int = 2) -> int:\n    return a * scale\n")

        # Combining `--ack-impl-only` AND `--ack-path src/contract.py --reviewed` must acknowledge BOTH cleanly!
        as_both = cli_json(
            "apply-sync",
            "--root",
            str(self.root),
            "--ack-impl-only",
            "--ack-path",
            "src/contract.py",
            "--reviewed",
        )
        self.assertTrue(as_both["ok"], as_both)
        hub_recs_shifted, _ = hubs.parse_hubs_volume(self.root / ".aoci" / "aoci.hubs.txt")
        self.assertEqual(
            [(r["relpath"], r["symbol"], r["start"], r["end"]) for r in hub_recs_shifted],
            [("src/keep.py", "run_job", 3, 4)],
        )

        # Deleting a zoomed symbol inside `src/keep.py` must auto-prune the dead symbol on `ack`
        self.write("src/keep.py", "def replacement_job() -> int:\n    return 0\n")
        self.assertTrue(cli_json("ack", "--root", str(self.root), "src/keep.py")["ok"])
        hub_recs_after_del, _ = hubs.parse_hubs_volume(self.root / ".aoci" / "aoci.hubs.txt")
        self.assertEqual(hub_recs_after_del, [])

    def test_stale_outline_cache_validation_and_live_fallbacks(self):
        self.write("src/svc.py", "def old_api() -> int:\n    return 1\n")
        self.write("src/other.py", "def untouched() -> int:\n    return 2\n")
        self.write("tests/check_suite.py", "from src.other import untouched\n\ndef test_it():\n    assert untouched() == 2\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/svc.py[AA8T]: F:handles service operations | R:- | A:old_api | S:-",
                "src/other.py[CA5T]: F:secondary helper module | R:- | A:untouched | S:-",
                "tests/check_suite.py[TA3T]: F:regression suite for modules | R:- | A:- | S:-",
            )[0],
            0,
        )
        # Warm `outline_cache.json` via `audit`
        cli_json("audit", "--root", str(self.root))
        cache_file = self.root / ".aoci" / ".cache" / "outline_cache.json"
        self.assertTrue(cache_file.is_file())

        # Edit `src/svc.py` and `tests/check_suite.py` in IDE WITHOUT refreshing `outline_cache.json`
        time.sleep(0.02)
        self.write("src/svc.py", "def fresh_api(limit: int = 10) -> int:\n    return limit\n")
        self.write(
            "tests/check_suite.py",
            "from src.svc import fresh_api\n\ndef test_fresh():\n    assert fresh_api() == 10\n",
        )

        # Cold lookup must NOT serve stale `old_api` from `outline_cache.json`, while `--live*` must find `fresh_api`!
        sym_cold = cli_json("symbol", "--root", str(self.root), "fresh_api")
        self.assertEqual(sym_cold["total_matches"], 0)
        sym_live = cli_json("symbol", "--root", str(self.root), "fresh_api", "--live")
        self.assertEqual(sym_live["total_matches"], 1)
        self.assertEqual(sym_live["matches"][0]["path"], "src/svc.py")

        pack_live = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--task",
            "update src/svc.py",
            "--no-diff",
            "--live-anchors",
            "--with-tests",
            "--live-test-imports",
        )
        svc_open = next(x for x in pack_live["open_next"] if x["path"] == "src/svc.py")
        self.assertTrue(any("fresh_api" in s for s in svc_open["symbols"]), svc_open)
        test_paths = [t["path"] for t in pack_live.get("impacted_tests", [])]
        self.assertIn("tests/check_suite.py", test_paths)

    def test_utf8_bom_across_parsers_volumes_baseline_and_vue_spans(self):
        bom = b"\xef\xbb\xbf"
        py_path = self.root / "src" / "bom_mod.py"
        py_path.parent.mkdir(parents=True, exist_ok=True)
        py_path.write_bytes(bom + b"import os\n\ndef greet(name: str = 'world') -> str:\n    return name\n")

        cs_path = self.root / "src" / "OrderService.cs"
        cs_path.write_bytes(bom + b"using MyCorp.Domain;\n\npublic class OrderService {\n    public void Run() {}\n}\n")

        vue_path = self.root / "src" / "Card.vue"
        vue_path.write_bytes(
            bom
            + (
                "<script setup lang=\"ts\">\n"
                "export function formatTitle(s: string): string {\n"
                "  return s.trim();\n"
                "}\n"
                "</script>\n"
                "<template>\n"
                "  <div>{{ title }}</div>\n"
                "</template>\n"
                "<style scoped>\n"
                ".card { color: red; }\n"
                "</style>\n"
            ).encode("utf-8")
        )

        self.init()
        (self.root / ".aoci" / ".aociignore").write_bytes(bom + b"ignored_bom.txt\n")
        (self.root / "ignored_bom.txt").write_text("skip me\n", encoding="utf-8")

        out_py = parsers.extract_file_outline(py_path, "src/bom_mod.py")
        self.assertIn("os", out_py["imports"])
        self.assertTrue(any("greet" in s for s in out_py["symbols"]))
        self.assertIsNotNone(parsers.compute_signature_hash(py_path, "src/bom_mod.py"))

        out_cs = parsers.extract_file_outline(cs_path, "src/OrderService.cs")
        self.assertTrue(any("MyCorp.Domain" in i for i in out_cs["imports"]), out_cs)

        vue_spans = hubs.extract_symbol_spans(vue_path, "src/Card.vue")
        self.assertEqual([(s["symbol"], s["start"], s["end"]) for s in vue_spans], [("formatTitle", 2, 4)])

        self.assertEqual(
            self.upsert(
                "src/bom_mod.py[CA5T]: F:greets caller using default world string | R:- | A:greet | S:-",
                "src/OrderService.cs[AA6T]: F:executes C# order service workflow | R:- | A:OrderService | S:-",
                "src/Card.vue[UA5T]: F:renders card SFC with title formatter | R:- | A:formatTitle | S:-",
            )[0],
            0,
        )

        # Prefix BOM onto `aoci.code.txt` and `baseline.json` -> `verify` must still pass!
        code_file = self.root / ".aoci" / "aoci.code.txt"
        base_file = self.root / ".aoci" / "baseline.json"
        code_file.write_bytes(bom + code_file.read_bytes())
        base_file.write_bytes(bom + base_file.read_bytes())
        ver = cli_json("verify", "--root", str(self.root))
        self.assertTrue(ver["ok"], ver)

    def test_python_sig_hash_defaults_constants_and_over_40_imports_graph(self):
        src_v1 = (
            "from contextlib import suppress\n"
            "with suppress(ImportError):\n"
            "    import ssl\n"
            "\n"
            "API_VERSION = 'v1'\n"
            "DEFAULT_TIMEOUT: int = 30\n"
            "\n"
            "def connect(host: str, *, verify_ssl: bool = True) -> bool:\n"
            "    return verify_ssl\n"
        )
        h_base = parsers.compute_signature_hash_from_bytes(src_v1.encode("utf-8"), ".py")
        h_body = parsers.compute_signature_hash_from_bytes(
            src_v1.replace("return verify_ssl", "return bool(verify_ssl)").encode("utf-8"), ".py"
        )
        self.assertEqual(h_base, h_body)

        for label, mutated in (
            ("kw_default", src_v1.replace("verify_ssl: bool = True", "verify_ssl: bool = False")),
            ("const", src_v1.replace("API_VERSION = 'v1'", "API_VERSION = 'v2'")),
            ("ann_const", src_v1.replace("DEFAULT_TIMEOUT: int = 30", "DEFAULT_TIMEOUT: int = 60")),
            ("with_import", src_v1.replace("import ssl", "import hashlib")),
        ):
            h_mut = parsers.compute_signature_hash_from_bytes(mutated.encode("utf-8"), ".py")
            self.assertNotEqual(h_base, h_mut, f"{label} mutation must change sig_hash")

        out_kw = parsers._extract_outline_from_bytes(src_v1.encode("utf-8"), ".py", "conn.py")
        self.assertIn("def connect(host, *, verify_ssl) [L8]", out_kw["symbols"])

        # >40 imports in a single hub file must still resolve trailing internal imports in `build_static_import_graph`
        self.write("src/leaf.py", "def leaf_fn():\n    return 1\n")
        many_stdlib = "\n".join(f"import mod_ext_{i}" for i in range(50))
        self.write("src/hub_many.py", f"{many_stdlib}\nfrom src import leaf\n\ndef hub_fn():\n    return leaf.leaf_fn()\n")
        self.init()
        scaf = cli_json("scaffold", "--root", str(self.root), "src/hub_many.py")
        self.assertIn("R:src/leaf.py", scaf["entries"][0])

    def test_ignore_brackets_readonly_pool_hook_persistence_symmetric_uninstall_and_cli_json_errors(self):
        # 1) Bracket edge cases (`[]a]`, `[!]]`) & non-slash `a**b` in IgnoreMatcher
        matcher = ignore.IgnoreMatcher(["[]a].tmp", "[!]].log", "a**b.txt"])
        self.assertTrue(matcher.is_ignored("].tmp"))
        self.assertTrue(matcher.is_ignored("a.tmp"))
        self.assertTrue(matcher.is_ignored("x.log"))
        self.assertFalse(matcher.is_ignored("].log"))
        self.assertTrue(matcher.is_ignored("axxb.txt"))
        self.assertFalse(matcher.is_ignored("a/sub/b.txt"))

        # 2) Read-only SQLite pool access executes zero write pragmas / DDL
        self.write("src/p.py", "x = 1\n\ny = 2\n\nz = 3\n\nw = 4\n")
        self.init()
        self.assertEqual(self.upsert("src/p.py[CA5T]: F:defines four module constants | R:- | A:- | S:-")[0], 0)
        git_info = aoci_index.detect_git_info(self.root)
        st = pool.pool_stats(self.root, git_info)
        self.assertTrue(st["exists"])
        self.assertGreaterEqual(st["by_path_sha_count"], 1)

        # Verify read-only SQLite connection fails on `CREATE TABLE` (proving `mode=ro` is active)
        with contextlib.closing(pool._open_semantic_pool(Path(st["pool_path"]), readonly=True)) as ro_conn:
            with self.assertRaises(sqlite3.OperationalError):
                ro_conn.execute("CREATE TABLE should_fail (id INT)")

        # 3) Hook `turn-start` preserves prior `Stop`-blocked agent drift & `Stop` catches broken `aoci.hubs.txt`
        base = {"cwd": str(self.root), "session_id": "s-persist"}
        hook("session-start", dict(base, hook_event_name="SessionStart"), host="claude-code")
        self.write("src/p.py", "x = 99\n\ny = 2\n\nz = 3\n\nw = 4\n")
        stop1 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False), host="claude-code")
        self.assertEqual(stop1.get("decision"), "block")

        # Next turn starts BEFORE agent synced `src/p.py` -> `src/p.py` must STILL be blocked at next `Stop`!
        hook("turn-start", dict(base, hook_event_name="UserPromptSubmit"), host="claude-code")
        stop2 = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False), host="claude-code")
        self.assertEqual(stop2.get("decision"), "block")
        self.assertIn("src/p.py", stop2["reason"])

        # Sync `src/p.py`, then corrupt `aoci.hubs.txt` -> `Stop` must catch `hub_issues`!
        cli_json("ack", "--root", str(self.root), "src/p.py")
        self.assertEqual(hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False), host="claude-code"), {})
        (self.root / ".aoci" / "aoci.hubs.txt").write_text(
            "src/p.py#missing[L1-L999]: F:bad out of bounds span | S:-\n", encoding="utf-8"
        )
        stop_hub = hook("stop", dict(base, hook_event_name="Stop", stop_hook_active=False), host="claude-code")
        self.assertEqual(stop_hub.get("decision"), "block")
        self.assertIn("hub_issues", stop_hub["reason"])
        (self.root / ".aoci" / "aoci.hubs.txt").unlink()

        # 4) Symmetric project-scoped uninstall: uninstalling Antigravity preserves shared `.agents/skills` for Codex
        with tempfile.TemporaryDirectory() as td_proj, tempfile.TemporaryDirectory() as td_home:
            proj_p = Path(td_proj)
            r_all = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "all",
                    "--scope",
                    "project",
                    "--project-root",
                    str(proj_p),
                    "--home-dir",
                    td_home,
                    "--with-hooks",
                    "--with-rules",
                    "--with-mcp",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_all.returncode, 0, r_all.stdout + r_all.stderr)
            r_un_agy = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "antigravity",
                    "--scope",
                    "project",
                    "--project-root",
                    str(proj_p),
                    "--home-dir",
                    td_home,
                    "--uninstall",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_un_agy.returncode, 0, r_un_agy.stdout + r_un_agy.stderr)
            self.assertTrue((proj_p / ".agents" / "skills" / "aoci-index" / "SKILL.md").is_file())
            self.assertIn("aoci-index", (proj_p / "AGENTS.md").read_text(encoding="utf-8"))

        # 5) CLI `--json` error paths must return valid JSON objects + `context --budget` budgets hubs/tests
        with tempfile.TemporaryDirectory() as empty_repo:
            for subcmd_args in (
                ("query", "--root", empty_repo, "--json"),
                ("context", "--root", empty_repo, "a.py", "--json"),
                ("overview", "--root", empty_repo, "--json"),
                ("lint", "--root", empty_repo, "--json"),
                ("shard", "--root", empty_repo, "--json"),
                ("restore", "--root", empty_repo, "--json"),
            ):
                rc_e, out_e = cli(*subcmd_args)
                self.assertNotEqual(rc_e, 0)
                parsed_err = json.loads(out_e)
                self.assertFalse(parsed_err["ok"])
                self.assertIn("error", parsed_err)

        # `context --budget` budgets `hub_sections` accurately
        cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "src/p.py#const_block[L1-L4]: F:holds four module constants with long description for budget check | S:immutable",
        )
        ctx_tight = cli_json("context", "--root", str(self.root), "src/p.py", "--budget", "25")
        self.assertTrue(ctx_tight.get("truncated"))
        self.assertEqual(ctx_tight.get("hub_sections_omitted"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
