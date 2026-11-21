#!/usr/bin/env python3
"""Tests for AOCI optimizations, bug fixes, and feature extensions (`apply-sync`, `symbol`, etc.)."""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_aoci import HAS_GIT, RepoTestCase, aoci_index, cli_json  # noqa: E402
import aoci_mcp  # noqa: E402
from aoci_core import graph, hubs, parsers, sync_prep, test_impact  # noqa: E402


class V16OptimizationsTests(RepoTestCase):
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

    def test_python_dunder_decorator_and_nested_import_sig_hash(self):
        base_src = (
            "try:\n"
            "    import ujson as json_mod\n"
            "except ImportError:\n"
            "    import json as json_mod\n"
            "\n"
            "@service_tag('v1')\n"
            "class Client:\n"
            "    def __init__(self, host: str):\n"
            "        self.host = host\n"
            "    def __call__(self, x: int) -> int:\n"
            "        return x\n"
            "    def _private_helper(self):\n"
            "        return 1\n"
            "\n"
            "@app.get('/v1/items')\n"
            "def list_items():\n"
            "    return []\n"
        )
        body_only_src = base_src.replace("self.host = host", "self.host = host.strip()").replace("return 1", "return 2")
        init_sig_changed = base_src.replace("def __init__(self, host: str):", "def __init__(self, host: str, port: int = 80):")
        call_sig_changed = base_src.replace("def __call__(self, x: int) -> int:", "def __call__(self, x: str) -> int:")
        dec_changed = base_src.replace("@app.get('/v1/items')", "@app.get('/v2/items')")
        cls_dec_changed = base_src.replace("@service_tag('v1')", "@service_tag('v2')")
        nested_imp_changed = base_src.replace("import ujson as json_mod", "import orjson as json_mod")

        h_base = parsers.compute_signature_hash_from_bytes(base_src.encode("utf-8"), ".py")
        h_body = parsers.compute_signature_hash_from_bytes(body_only_src.encode("utf-8"), ".py")
        self.assertIsNotNone(h_base)
        self.assertEqual(h_base, h_body)

        for label, mutated in (
            ("__init__", init_sig_changed),
            ("__call__", call_sig_changed),
            ("fn_decorator", dec_changed),
            ("cls_decorator", cls_dec_changed),
            ("nested_import", nested_imp_changed),
        ):
            h_mut = parsers.compute_signature_hash_from_bytes(mutated.encode("utf-8"), ".py")
            self.assertNotEqual(h_base, h_mut, f"{label} change must alter sig_hash")

    def test_mcp_cache_thread_safety_lint_limit_cache_reuse_and_invalidation(self):
        self.write("src/a.py", "def fa():\n    return 1\n")
        self.write("src/b.py", "def fb():\n    return 2\n")
        self.write("src/c.py", "def fc():\n    return 3\n")
        self.write("src/d.py", "def fd():\n    return 4\n")
        if HAS_GIT:
            self._git("add", ".")
            self._git("commit", "-q", "-m", "init")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/a.py[CX5T]: F:one | R:- | A:fa | S:-",
                "src/b.py[CX5T]: F:two | R:- | A:fb | S:-",
                "src/c.py[CX5T]: F:three | R:- | A:fc | S:-",
                "src/d.py[CX5T]: F:four | R:- | A:fd | S:-",
            )[0],
            0,
        )

        # Thread-safety stress test across _CACHE accessors + call_tool
        def worker(i: int) -> None:
            if i % 4 == 0:
                aoci_mcp._CACHE.invalidate(self.root)
            slot = aoci_mcp._CACHE.get(self.root)
            self.assertIsNotNone(slot)
            bm25 = aoci_mcp._CACHE.get_bm25(slot)
            self.assertGreater(bm25.n_docs, 0)
            aoci_mcp._CACHE.get_outline_files(self.root)
            err, txt = aoci_mcp.call_tool("aoci_lint", {"root": str(self.root), "limit": 2})
            self.assertFalse(err, txt)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(worker, i) for i in range(24)]
            for fut in futs:
                fut.result()

        # Lint limit & strict mode evaluated BEFORE slicing
        err_lim, out_lim = aoci_mcp.call_tool("aoci_lint", {"root": str(self.root), "limit": 2, "strict": True})
        self.assertTrue(err_lim)
        lim_obj = json.loads(out_lim)
        self.assertEqual(len(lim_obj["findings"]), 2)
        self.assertTrue(lim_obj["truncated"])
        self.assertGreaterEqual(lim_obj["total_findings"], 4)

        # Warm outline cache reuse in aoci_diff(with_tests=True)
        if HAS_GIT:
            cli_json("audit", "--root", str(self.root))
            with mock.patch.object(test_impact, "_load_outline_cache", side_effect=AssertionError("must reuse cache")):
                err_d, out_d = aoci_mcp.call_tool("aoci_diff", {"root": str(self.root), "with_tests": True})
            self.assertFalse(err_d, out_d)

        # aoci_prepare_sync reuses warm _CACHE slot entries/parsed/dependents inside prepare_sync_briefing
        if HAS_GIT:
            aoci_mcp._CACHE.get(self.root)
            with mock.patch.object(
                sync_prep, "build_fras_graph", side_effect=AssertionError("must reuse slot")
            ):
                err_ps, _ = aoci_mcp.call_tool("aoci_prepare_sync", {"root": str(self.root)})
            self.assertFalse(err_ps)

        # aoci_ensure_git_exclude invalidates _CACHE for root (even if warmed via non-canonical path)
        root_key = str(self.root.resolve())
        aoci_mcp._CACHE.get(self.root / "src" / "..")
        self.assertIn(root_key, aoci_mcp._CACHE._slots)
        aoci_mcp.call_tool("aoci_ensure_git_exclude", {"root": str(self.root)})
        self.assertNotIn(root_key, aoci_mcp._CACHE._slots)

    def test_jvm_and_csharp_static_import_graph_and_audit(self):
        self.write("src/com/app/model/User.java", "package com.app.model;\npublic class User {}\n")
        self.write("src/com/app/model/Role.java", "package com.app.model;\npublic class Role {}\n")
        self.write(
            "src/com/app/service/AuthService.java",
            "package com.app.service;\nimport com.app.model.User;\npublic class AuthService {}\n",
        )
        self.write(
            "src/com/app/service/Helper.java",
            "package com.app.service;\npublic class Helper {}\n",
        )
        self.write(
            "src/com/app/api/Controller.kt",
            "package com.app.api\nimport com.app.service.*\nclass Controller {}\n",
        )
        self.write(
            "src/App/Core/Token.cs",
            "namespace App.Core {\n  public class Token {}\n}\n",
        )
        self.write(
            "src/App/Web/LoginController.cs",
            "using App.Core;\nnamespace App.Web {\n  public class LoginController {}\n}\n",
        )
        self.init()
        self.assertEqual(
            self.upsert(
                "src/com/app/model/User.java[DX7T]: F:represents user entity model | R:- | A:User | S:-",
                "src/com/app/model/Role.java[DX6T]: F:represents role entity model | R:- | A:Role | S:-",
                # Helper.java is same-package: must NOT trigger phantom-fras-dep; missing User.java must trigger missing-static-dep
                "src/com/app/service/AuthService.java[AX8T]: F:authenticates users via model | R:src/com/app/service/Helper.java | A:AuthService | S:-",
                "src/com/app/service/Helper.java[CX5T]: F:provides auth service helper utilities | R:- | A:Helper | S:-",
                # Wildcard com.app.service.* without FRAS R: triggers missing-static-dep
                "src/com/app/api/Controller.kt[EA7T]: F:handles kotlin http controller endpoints | R:- | A:Controller | S:-",
                "src/App/Core/Token.cs[DX7T]: F:models core csharp bearer token | R:- | A:Token | S:-",
                # Using App.Core without FRAS R: triggers missing-static-dep
                "src/App/Web/LoginController.cs[EA7T]: F:handles csharp web login requests | R:- | A:LoginController | S:-",
            )[0],
            0,
        )

        aud = cli_json("audit", "--root", str(self.root), "--no-layers", "--no-cycles")
        missing_pairs = {(m["from"], m["to"]) for m in aud["missing_static_deps"]}
        self.assertIn(("src/com/app/service/AuthService.java", "src/com/app/model/User.java"), missing_pairs)
        self.assertIn(("src/App/Web/LoginController.cs", "src/App/Core/Token.cs"), missing_pairs)
        self.assertTrue(
            any(
                m["from"] == "src/com/app/api/Controller.kt"
                and m["to"] in {"src/com/app/service/AuthService.java", "src/com/app/service/Helper.java"}
                for m in aud["missing_static_deps"]
            ),
            missing_pairs,
        )
        phantom_pairs = {(p["from"], p["to"]) for p in aud["phantom_fras_deps"]}
        self.assertNotIn(("src/com/app/service/AuthService.java", "src/com/app/service/Helper.java"), phantom_pairs)

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_prepare_sync_batched_git_diff_calls(self):
        entries = []
        for idx in range(5):
            rel = f"src/m{idx}.py"
            self.write(rel, f"def fn_{idx}():\n    return {idx}\n")
            entries.append(f"{rel}[AX6T]: F:module number {idx} handler | R:- | A:fn_{idx} | S:-")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "baseline")
        self.init()
        self.assertEqual(self.upsert(*entries)[0], 0)

        for idx in range(5):
            self.write(f"src/m{idx}.py", f"def fn_{idx}(arg: int):\n    return {idx} + arg\n")

        diff_calls = 0
        orig_run_git = aoci_index.run_git

        def counting_git(args, cwd):
            nonlocal diff_calls
            if "diff" in args:
                diff_calls += 1
            return orig_run_git(args, cwd)

        git_info = aoci_index.detect_git_info(self.root)
        drift = aoci_index.compute_drift(self.root, git_info, fast_mtime=False, allow_heal=False)
        brief = aoci_index._sync_prep.prepare_sync_briefing(
            self.root,
            git_info,
            drift,
            scaffold_fn=aoci_index.scaffold_entries,
            run_git_fn=counting_git,
            max_diff_lines=30,
        )
        self.assertEqual(diff_calls, 1)
        mods = brief["contract_or_unknown_modified"]
        self.assertEqual(len(mods), 5)
        for item in mods:
            self.assertIn("arg: int", item.get("diff_preview", ""))

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_diff_briefing_dirty_check_skips_full_fingerprinting(self):
        self.write("src/app.py", "def run():\n    return 1\n")
        self.write("assets/raw.bin", "binary\x00data")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")
        self.init()
        self.assertEqual(
            self.upsert("src/app.py[AX7T]: F:runs main application loop | R:- | A:run | S:-")[0],
            0,
        )

        self.write("src/app.py", "def run():\n    return 222\n", aged=False)
        (self.root / "assets" / "dirty.bin").write_bytes(b"\x00\x01\x02hello")

        with mock.patch.object(
            aoci_index,
            "inspect_and_fingerprint_one",
            side_effect=AssertionError("compute_diff_briefing must use lightweight stat/head check"),
        ):
            brief = aoci_index.compute_diff_briefing(self.root)
        self.assertTrue(brief["ok"])
        self.assertEqual(len(brief["changed_entries"]), 1)
        self.assertNotIn("assets/dirty.bin", brief["unindexed_changed"])

    def test_verify_skips_live_symbol_parsing_while_enforcing_line_bounds(self):
        self.write("src/engine.py", "def step_one():\n    return 1\n\ndef step_two():\n    return 2\n")
        self.init()
        self.assertEqual(
            self.upsert("src/engine.py[KX8T]: F:executes engine step routines | R:- | A:step_one/step_two | S:pure")[0],
            0,
        )
        cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "src/engine.py#step_one[L1-L2]: F:returns constant one step | S:-",
        )

        with mock.patch.object(
            hubs,
            "_extract_live_symbol_span_map",
            side_effect=AssertionError("verify should skip live AST/outline symbol extraction"),
        ):
            rc_in, out_in = aoci_index._capture_internal_cli(["verify", "--root", str(self.root), "--json"])
            self.assertEqual(rc_in, 0, out_in)
        ver_ok = cli_json("verify", "--root", str(self.root))
        self.assertTrue(ver_ok["ok"], ver_ok)

        # Corrupt hub end line beyond file lines directly -> verify must still fail!
        hubs_file = self.root / ".aoci" / "aoci.hubs.txt"
        hubs_file.write_text(
            hubs_file.read_text(encoding="utf-8").replace("[L1-L2]", "[L1-L99]"),
            encoding="utf-8",
        )
        rc, out = aoci_index._capture_internal_cli(["verify", "--root", str(self.root), "--json"])
        self.assertNotEqual(rc, 0)
        self.assertIn("out of bounds", out)

    def test_regex_comment_strippers_edge_cases_and_speed(self):
        js_src = (
            'const url = "http://example.com/*not_comment*/"; // line cmt\n'
            "const tmpl = `hello // ${x} /* keep */`;\n"
            "const rx = /foo\\/bar\\/\\/baz[/*]not_cmt/gi; /* real block\n"
            "comment */\n"
            "function ratio(a, b, c) { return a / b / c; }\n"
            "function mk() { return /https?:\\/\\//; }\n"
        )
        js_clean = parsers._strip_js_comments(js_src)
        self.assertEqual(len(js_clean.splitlines()), len(js_src.splitlines()))
        self.assertIn("http://example.com/*not_comment*/", js_clean)
        self.assertIn("hello // ${x} /* keep */", js_clean)
        self.assertIn("/foo\\/bar\\/\\/baz[/*]not_cmt/gi;", js_clean)
        self.assertIn("return a / b / c;", js_clean)
        self.assertIn("return /https?:\\/\\//;", js_clean)
        self.assertNotIn("line cmt", js_clean)
        self.assertNotIn("real block", js_clean)

        rs_src = "fn borrow<'a>(s: &'a str) -> &'a str { // strip me\n    /* and me */ s\n}\n"
        rs_clean = parsers._strip_c_comments(rs_src, single_quote_char_only=True)
        self.assertIn("fn borrow<'a>(s: &'a str)", rs_clean)
        self.assertNotIn("strip me", rs_clean)
        self.assertNotIn("and me", rs_clean)

        go_src = 'const raw = `// keep // inside backtick` // drop me\n'
        go_clean = parsers._strip_c_comments(go_src, allow_backtick=True)
        self.assertIn("`// keep // inside backtick`", go_clean)
        self.assertNotIn("drop me", go_clean)

        # 100KB stress input must finish fast (linear time, zero ReDoS)
        huge_js = ("const x = 'abcdef\\\\ghijkl'; // cmt\n" + "const a = a / b / c;\n") * 2500
        huge_c = ('const char* s = "hello/*world*/"; // c\n' + "int x = 1; /* block */\n") * 2500
        t0 = time.perf_counter()
        parsers._strip_js_comments(huge_js)
        parsers._strip_c_comments(huge_c)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.75)

    def test_ack_impl_only_single_pass_no_duplicate_sig_hash(self):
        self.write("src/body.py", "def calc(x: int) -> int:\n    return x + 1\n")
        self.write("src/api.py", "def endpoint(x: int) -> int:\n    return x\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/body.py[KX6T]: F:computes integer offset value | R:- | A:calc | S:-",
                "src/api.py[EA7T]: F:handles api endpoint request | R:- | A:endpoint | S:-",
            )[0],
            0,
        )
        self.write("src/body.py", "def calc(x: int) -> int:\n    return x + 2\n")
        self.write("src/api.py", "def endpoint(x: int, y: int) -> int:\n    return x + y\n")

        calls = 0
        orig_sig_bytes = aoci_index.compute_signature_hash_from_bytes
        orig_sig = aoci_index.compute_signature_hash

        def spy_sig_bytes(*a, **kw):
            nonlocal calls
            calls += 1
            return orig_sig_bytes(*a, **kw)

        def spy_sig(*a, **kw):
            nonlocal calls
            calls += 1
            return orig_sig(*a, **kw)

        with mock.patch.object(aoci_index, "compute_signature_hash_from_bytes", side_effect=spy_sig_bytes):
            with mock.patch.object(aoci_index, "compute_signature_hash", side_effect=spy_sig):
                rc_dry, out_dry = aoci_index._capture_internal_cli(
                    ["ack", "--root", str(self.root), "--impl-only", "--dry-run", "--json"]
                )
                self.assertEqual(rc_dry, 0, out_dry)
        dry_obj = json.loads(out_dry)
        self.assertEqual(dry_obj["acked"], ["src/body.py"])
        self.assertEqual(calls, 2)  # exactly once per modified file, not twice

        res = cli_json("ack", "--root", str(self.root), "--impl-only", "--reviewed")
        self.assertTrue(res["ok"])
        self.assertEqual(res["acked"], ["src/body.py"])
        self.assertEqual(len(res["skipped_contract_changed"]), 1)

    def test_pack_budget_caps_hub_sections_and_impacted_tests(self):
        self.write("src/seed.py", "def alpha():\n    return 1\n\ndef beta():\n    return 2\n")
        self.write("src/dep.py", "def helper():\n    return 10\n")
        self.write("tests/test_seed.py", "def test_alpha():\n    assert True\n")
        self.init()
        seed_line = "src/seed.py[AX8T]: F:executes alpha beta settlement orchestrator | R:src/dep.py | A:alpha/beta | S:idempotent"
        dep_line = "src/dep.py[CX8T]: F:provides shared arithmetic accumulator utilities | R:- | A:helper<-src/seed.py | S:pure"
        test_line = "tests/test_seed.py[TA4T]: F:verifies integration suite cases | R:src/seed.py | A:- | S:-"
        self.assertEqual(self.upsert(seed_line, dep_line, test_line)[0], 0)

        # Put alphabetical path `src/dep.py` first vs `src/seed.py` -> pack must STILL prioritize seed section!
        h1 = "src/seed.py#alpha[L1-L2]: F:alpha core orchestrator entry step | S:none"
        h2 = "src/dep.py#helper[L1-L2]: F:helper shared dependency primitive | S:none"
        cli_json("zoom", "--root", str(self.root), "--upsert", h2, "--upsert", h1)

        # Room for invariants + seed + dep + first hub section (h1), too small for h2 and test_line
        tight_budget = (
            aoci_index.estimate_tokens("-")
            + aoci_index.estimate_tokens(seed_line)
            + aoci_index.estimate_tokens(dep_line)
            + aoci_index.estimate_tokens(h1)
            + 2
        )
        pkt = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--no-diff",
            "--task",
            "alpha settlement",
            "--with-tests",
            "--budget",
            str(tight_budget),
        )
        self.assertTrue(pkt["truncated"])
        self.assertLessEqual(pkt["estimated_tokens"], tight_budget)
        self.assertEqual(len(pkt["hub_sections"]), 1)
        self.assertEqual(pkt["hub_sections"][0]["path"], "src/seed.py")
        self.assertEqual(pkt["hub_sections_omitted"], 1)
        self.assertEqual(pkt["impacted_tests"], [])
        self.assertEqual(pkt["impacted_tests_omitted"], 1)

    def test_test_impact_plural_multilang_and_live_imports(self):
        # 1) Multi-language plural/new conventions
        files = {
            "src/Auth.java": "public class Auth {}\n",
            "tests/AuthTests.java": "public class AuthTests {}\n",
            "src/Order.cs": "public class Order {}\n",
            "tests/OrderSpecs.cs": "public class OrderSpecs {}\n",
            "src/token.rs": "pub fn sign() {}\n",
            "tests/token.rs": "fn test_sign() {}\n",
            "src/parser.cpp": "int parse() { return 0; }\n",
            "tests/parser_unittest.cc": "int main() { return 0; }\n",
            "lib/auth.rb": "class Auth; end\n",
            "spec/auth_spec.rb": "describe Auth do end\n",
            "src/Mailer.php": "<?php class Mailer {}\n",
            "tests/MailerTest.php": "<?php class MailerTest {}\n",
        }
        entries = []
        for rel, body in files.items():
            self.write(rel, body)
            layer = "T" if ("test" in rel.lower() or "spec" in rel.lower()) else "A"
            entries.append(f"{rel}[{layer}X5T]: F:implements {rel} module component | R:- | A:- | S:-")
        self.init()
        self.assertEqual(self.upsert(*entries)[0], 0)

        deps = {}
        parsed = graph.parse_entries_map({rel: line for rel, line in zip(files.keys(), entries)})
        for target, expected_test in (
            ("src/Auth.java", "tests/AuthTests.java"),
            ("src/Order.cs", "tests/OrderSpecs.cs"),
            ("src/token.rs", "tests/token.rs"),
            ("src/parser.cpp", "tests/parser_unittest.cc"),
            ("lib/auth.rb", "spec/auth_spec.rb"),
            ("src/Mailer.php", "tests/MailerTest.php"),
        ):
            found = {
                it["path"]
                for it in test_impact.find_impacted_tests(self.root, dict(zip(files.keys(), entries)), deps, [target], parsed_entries=parsed)
            }
            self.assertIn(expected_test, found, f"{target} should match {expected_test}")

        # 2) Opt-in --live-test-imports on cold outline_cache.json
        self.write("src/core_logic.py", "def compute():\n    return 42\n")
        self.write("tests/verify_behavior.py", "from src.core_logic import compute\n\ndef test_compute():\n    assert compute() == 42\n")
        self.assertEqual(
            self.upsert(
                "src/core_logic.py[AX7T]: F:computes core workflow metric value | R:- | A:compute | S:-",
                "tests/verify_behavior.py[TA4T]: F:validates end to end compute output | R:- | A:- | S:-",
            )[0],
            0,
        )
        cache_path = self.root / ".aoci" / ".cache" / "outline_cache.json"
        self.assertFalse(cache_path.exists())

        cold_q = cli_json("query", "--root", str(self.root), "--tests-for", "src/core_logic.py")
        self.assertEqual(cold_q["impacted_tests"], [])
        self.assertFalse(cache_path.exists())

        live_q = cli_json("query", "--root", str(self.root), "--tests-for", "src/core_logic.py", "--live-test-imports")
        self.assertEqual([t["path"] for t in live_q["impacted_tests"]], ["tests/verify_behavior.py"])
        self.assertEqual(live_q["impacted_tests"][0]["reason"], "static-import")
        self.assertFalse(cache_path.exists())

    def test_html_export_layout_direction_pan_zoom_focus_and_escape(self):
        entries = {
            "src/a.py": "src/a.py[EA8T]: F:entry </script> edge handler | R:src/b.py | A:main<-src/b.py | S:lock",
            "src/b.py": "src/b.py[DA7T]: F:domain logic module | R:src/a.py | A:work<-src/a.py | S:-",
        }
        parsed = graph.parse_entries_map(entries)
        deps, dependents = graph.build_fras_graph(entries, parsed_entries=parsed)
        out = graph.export_dependency_graph(
            entries,
            deps,
            dependents,
            {"A": "Auth"},
            fmt="html",
            direction="TD",
            parsed_entries=parsed,
        )
        self.assertEqual(out["direction"], "TD")
        html_txt = out["content"]
        self.assertIn('"direction": "TD"', html_txt)
        self.assertIn('<select id="layoutMode">', html_txt)
        self.assertIn('<button id="zoomIn"', html_txt)
        self.assertIn('viewportG.setAttribute("id", "viewport")', html_txt)
        self.assertIn(".node-box.dim", html_txt)
        self.assertIn("Dependencies (outgoing)", html_txt)
        self.assertIn("Callers (incoming)", html_txt)
        self.assertNotIn("</script> edge", html_txt)
        self.assertIn("<\\/script>", html_txt)

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_cochange_exclude_tests_directional_confidence_and_two_hop(self):
        for rel in ("src/a.py", "src/b.py", "src/bridge.py", "src/c.py", "tests/test_a.py"):
            self.write(rel, f"# {rel} v0\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")

        for i in range(1, 4):
            for rel in ("src/a.py", "src/b.py", "src/c.py", "tests/test_a.py"):
                self.write(rel, f"# {rel} v{i} {'x' * i}\n", aged=False)
            self._git("add", ".")
            self._git("commit", "-q", "-m", f"cochange {i}")

        for i in range(4, 6):
            self.write("src/c.py", f"# src/c.py v{i} {'x' * i}\n", aged=False)
            self._git("add", "src/c.py")
            self._git("commit", "-q", "-m", f"c only {i}")

        self.init()
        self.assertEqual(
            self.upsert(
                "src/a.py[AX6T]: F:implements alpha service logic | R:- | A:fa | S:-",
                "src/b.py[AX6T]: F:implements beta service logic | R:- | A:fb | S:-",
                # bridge.py links to BOTH a.py and b.py -> making (a.py, b.py) a 2-hop pair
                "src/bridge.py[EX7T]: F:connects alpha and beta modules | R:src/a.py,src/b.py | A:run | S:-",
                "src/c.py[CX5T]: F:implements unrelated c helper | R:- | A:fc | S:-",
                "tests/test_a.py[TX3T]: F:tests alpha integration cases | R:- | A:- | S:-",
            )[0],
            0,
        )

        aud_all = cli_json("audit", "--root", str(self.root), "--co-change", "--no-static", "--no-cycles", "--no-layers")
        pairs_all = {(r["file_a"], r["file_b"]): r for r in aud_all["co_change_unlinked"]}
        self.assertIn(("src/a.py", "tests/test_a.py"), pairs_all)
        self.assertIn(("src/a.py", "src/b.py"), pairs_all)
        self.assertIn(("src/a.py", "src/c.py"), pairs_all)

        ab = pairs_all[("src/a.py", "src/b.py")]
        self.assertTrue(ab["is_two_hop"])
        self.assertEqual(ab["confidence_a_to_b"], 1.0)
        self.assertEqual(ab["confidence_b_to_a"], 1.0)

        ac = pairs_all[("src/a.py", "src/c.py")]
        self.assertFalse(ac["is_two_hop"])
        self.assertEqual(ac["confidence_a_to_b"], 1.0)
        self.assertEqual(ac["confidence_b_to_a"], 0.667)

        aud_no_t = cli_json(
            "audit", "--root", str(self.root), "--co-change", "--exclude-tests", "--no-static", "--no-cycles", "--no-layers"
        )
        for row in aud_no_t["co_change_unlinked"]:
            self.assertNotEqual(row["file_a"], "tests/test_a.py")
            self.assertNotEqual(row["file_b"], "tests/test_a.py")

    @unittest.skipUnless(HAS_GIT, "git required")
    def test_apply_sync_cli_and_mcp(self):
        self.write("src/old_mod.py", "\n".join(f"VAR_{i} = {i}" for i in range(10)) + "\n")
        self.write("src/clean.py", "def compute(x: int) -> int:\n    return x + 1\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/old_mod.py[CX5T]: F:holds legacy numeric constants module | R:- | A:VAR_0 | S:-",
                "src/clean.py[KX6T]: F:computes incremented integer value | R:- | A:compute | S:-",
            )[0],
            0,
        )

        # Rename old_mod -> new_mod, body-edit clean.py, add new_added.py
        (self.root / "src" / "new_mod.py").write_text((self.root / "src" / "old_mod.py").read_text(encoding="utf-8"), encoding="utf-8")
        (self.root / "src" / "old_mod.py").unlink()
        self.write("src/clean.py", "def compute(x: int) -> int:\n    res = x + 1\n    return res\n")
        self.write("src/new_added.py", "def added_fn():\n    return True\n")

        # 1) Reject unreviewed ack unless --dry-run
        rc_norev, out_norev = aoci_index._capture_internal_cli(
            ["apply-sync", "--root", str(self.root), "--ack-impl-only", "--json"]
        )
        self.assertNotEqual(rc_norev, 0)
        norev_obj = json.loads(out_norev)
        self.assertEqual(norev_obj["failed_step"], "ack")

        # 2) Dry-run leaves disk intact
        dry_obj = cli_json(
            "apply-sync",
            "--root",
            str(self.root),
            "--migrate-renames",
            "--ack-impl-only",
            "--entry",
            "src/new_added.py[CX4T]: F:returns true flag constant helper | R:- | A:added_fn | S:-",
            "--dry-run",
        )
        self.assertTrue(dry_obj["ok"], dry_obj)
        self.assertTrue(dry_obj["dry_run"])
        self.assertEqual([s["step"] for s in dry_obj["steps"]], ["migrate_renames", "ack", "upsert"])

        # 3) Warm cache first, then apply-sync via MCP to confirm cache invalidation + PASS verify
        root_key = str(self.root.resolve())
        aoci_mcp._CACHE.get(self.root)
        self.assertIn(root_key, aoci_mcp._CACHE._slots)
        err_as, out_as = aoci_mcp.call_tool(
            "aoci_apply_sync",
            {
                "root": str(self.root),
                "migrate_renames": True,
                "ack_impl_only": True,
                "reviewed": True,
                "entries": ["src/new_added.py[CX4T]: F:returns true flag constant helper | R:- | A:added_fn | S:-"],
            },
        )
        self.assertFalse(err_as, out_as)
        as_obj = json.loads(out_as)
        self.assertTrue(as_obj["ok"])
        self.assertEqual([s["step"] for s in as_obj["steps"]], ["migrate_renames", "ack", "upsert", "verify"])
        self.assertTrue(as_obj["verify"]["ok"])
        self.assertNotIn(root_key, aoci_mcp._CACHE._slots)

        # 4) Stop on failure halts before verify when upsert fails
        err_fail, out_fail = aoci_mcp.call_tool(
            "aoci_apply_sync",
            {
                "root": str(self.root),
                "entries": ["bad entry syntax"],
            },
        )
        self.assertTrue(err_fail)
        fail_obj = json.loads(out_fail)
        self.assertEqual(fail_obj["failed_step"], "upsert")
        self.assertIsNone(fail_obj["verify"])

    def test_symbol_lookup_cli_and_mcp(self):
        self.write(
            "src/auth.py",
            (
                "class AuthService:\n"
                "    def verify_token(self, tok: str) -> bool:\n"
                "        return bool(tok)\n\n"
                "def verify(tok: str) -> bool:\n"
                "    return bool(tok)\n"
            ),
        )
        self.write("src/util.py", "def verify_helper():\n    return True\n")
        self.write("src/api.py", "def handler():\n    return True\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/auth.py[MA8T]: F:verifies incoming bearer tokens cleanly | R:- | A:AuthService/verify<-src/api.py | S:reject none alg",
                "src/util.py[CX4T]: F:provides internal verification helper utility | R:- | A:verify_helper | S:-",
                "src/api.py[EA6T]: F:dispatches authenticated http request routes | R:src/auth.py | A:handler | S:-",
            )[0],
            0,
        )
        cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "src/auth.py#AuthService.verify_token[L2-L3]: F:validates signature and expiry on AuthService | S:constant time",
        )

        # Cold outline cache + --live finds uncached symbols without creating outline_cache.json
        cache_path = self.root / ".aoci" / ".cache" / "outline_cache.json"
        self.assertFalse(cache_path.exists())
        live_res = cli_json("symbol", "verify_helper", "--root", str(self.root), "--live", "--exact")
        self.assertTrue(live_res["ok"])
        self.assertFalse(cache_path.exists())
        self.assertEqual([m["symbol"] for m in live_res["matches"]], ["verify_helper"])
        self.assertEqual(live_res["matches"][0]["match_tier"], 0)
        self.assertIn("outline", live_res["matches"][0]["sources"])

        # Populate outline cache and test MCP aoci_symbol ranking, hub fusion, exact vs substring, callers
        cli_json("audit", "--root", str(self.root))
        err_s, out_s = aoci_mcp.call_tool("aoci_symbol", {"root": str(self.root), "name": "verify", "depth": 1})
        self.assertFalse(err_s, out_s)
        sym_obj = json.loads(out_s)
        self.assertTrue(sym_obj["ok"])
        syms = [m["symbol"] for m in sym_obj["matches"]]
        self.assertEqual(syms[0], "verify")  # exact match first
        self.assertIn("AuthService.verify_token", syms)
        self.assertIn("verify_helper", syms)

        by_sym = {m["symbol"]: m for m in sym_obj["matches"]}
        self.assertEqual(by_sym["verify"]["callers"], ["src/api.py"])
        self.assertEqual(by_sym["AuthService.verify_token"]["line_span"], [2, 3])
        self.assertEqual(
            by_sym["AuthService.verify_token"]["hub_f"],
            "validates signature and expiry on AuthService",
        )
        self.assertEqual(sorted(by_sym["AuthService.verify_token"]["sources"]), ["hub", "outline"])

        # --exact excludes verify_helper for query "verify"
        exact_res = cli_json("symbol", "verify", "--root", str(self.root), "--exact")
        exact_syms = [m["symbol"] for m in exact_res["matches"]]
        self.assertIn("verify", exact_syms)
        self.assertNotIn("verify_helper", exact_syms)


if __name__ == "__main__":
    unittest.main()
