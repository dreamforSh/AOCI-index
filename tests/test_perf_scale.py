#!/usr/bin/env python3
"""Performance & monorepo-scale regression tests for AOCI index engine, parsers, graph, pool, and MCP."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import threading
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
from aoci_core import graph, ignore, lock, parsers, pool, sync_prep, test_impact  # noqa: E402


class PerfScaleTests(RepoTestCase):
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

    def test_ignore_matcher_negation_fast_union_regex_and_scale(self):
        patterns = [
            "*.log",
            "!important.log",
            "logs/force_ignore.log",
            "build/",
            "dist/**",
            "*.tmp",
            "!keep/*.tmp",
            "keep/secret.tmp",
            "docs/generated/**",
            "!docs/generated/",
            "!docs/generated/index.md",
            "node_modules/",
            "__pycache__/",
            "*.pyc",
            "coverage/",
            ".venv/",
        ]
        fast_m = ignore.IgnoreMatcher(patterns)
        fast_m._ensure_fast_regexes()
        self.assertTrue(fast_m._fast_can_use)
        self.assertIsNotNone(fast_m._fast_file_rx)
        self.assertIsNotNone(fast_m._fast_neg_file_rx)

        # Build an oracle matcher forced onto sequential rule evaluation
        slow_m = ignore.IgnoreMatcher(patterns)
        slow_m._ensure_fast_regexes()
        slow_m._fast_can_use = False

        probe_cases = [
            ("src/app/service.py", False, False),
            ("debug.log", False, True),
            ("sub/important.log", False, False),
            ("logs/important.log", False, False),
            ("logs/force_ignore.log", False, True),
            ("keep/cache.tmp", False, False),
            ("keep/secret.tmp", False, True),
            ("other/cache.tmp", False, True),
            ("build/out.js", False, True),
            ("build", True, True),
            ("docs/generated/api.md", False, True),
            ("docs/generated/index.md", False, False),
        ]
        for rel, is_dir, expected in probe_cases:
            self.assertEqual(fast_m.is_ignored(rel, is_dir=is_dir), expected, rel)
            self.assertEqual(fast_m.is_ignored(rel, is_dir=is_dir), slow_m.is_ignored(rel, is_dir=is_dir), rel)

        # Evaluate 10,000 synthetic source paths in well under 0.5s even on CI runners
        synth_paths = [f"packages/pkg_{i % 40}/src/mod_{i % 25}/handler_{i}.py" for i in range(10000)]
        t0 = time.perf_counter()
        hits = sum(1 for p in synth_paths if fast_m.is_ignored(p, is_dir=False))
        elapsed = time.perf_counter() - t0
        self.assertEqual(hits, 0)
        self.assertLess(elapsed, 1.0)

    def test_python_suffix_index_monorepo_import_resolution_scale_and_parity(self):
        # 50 subpackages (`packages/svc_00/src/pkg_00/...`) + 2,000 Python files with external + internal imports
        discovered = []
        outlines = {}
        for p_idx in range(50):
            toml_rel = f"packages/svc_{p_idx:02d}/pyproject.toml"
            discovered.append(toml_rel)
            outlines[toml_rel] = {"imports": [], "symbols": []}
            pkg_root = f"packages/svc_{p_idx:02d}/src/pkg_{p_idx:02d}"
            init_rel = f"{pkg_root}/__init__.py"
            discovered.append(init_rel)
            outlines[init_rel] = {"imports": [], "py_from_members": [], "symbols": []}
            for m_idx in range(38):
                rel = f"{pkg_root}/mod_{m_idx:02d}.py"
                discovered.append(rel)
                target_pkg = (p_idx + 1) % 50
                target_mod = (m_idx + 1) % 38
                outlines[rel] = {
                    "imports": [
                        "os",
                        "sys",
                        "typing",
                        "pydantic.fields",
                        "fastapi.routing",
                        "sqlalchemy.orm.session",
                        f"pkg_{target_pkg:02d}.mod_{target_mod:02d}",
                        ".mod_00" if m_idx > 0 else "json",
                    ],
                    "py_from_members": [
                        f"pkg_{target_pkg:02d}.mod_{target_mod:02d}.run_handler",
                    ],
                    "symbols": [f"def fn_{m_idx}() [L1]"],
                }
        discovered.sort()
        self.assertEqual(len(discovered), 2000)
        synth_fps = {rel: {"mtime_ns": 1, "bytes": 100} for rel in discovered}

        t0 = time.perf_counter()
        deps, callers, _ = parsers.build_static_import_graph(
            self.root,
            discovered,
            fps=synth_fps,
            outline_fn=lambda _fp, rel, *_a, **_kw: outlines[rel],
        )
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 1.5, f"2,000-file Python monorepo static graph took {elapsed:.3f}s")
        self.assertIn(
            "packages/svc_01/src/pkg_01/mod_02.py",
            deps["packages/svc_00/src/pkg_00/mod_01.py"],
        )
        self.assertIn(
            "packages/svc_00/src/pkg_00/mod_00.py",
            deps["packages/svc_00/src/pkg_00/mod_01.py"],
        )
        self.assertIn(
            "packages/svc_49/src/pkg_49/mod_37.py",
            callers["packages/svc_00/src/pkg_00/mod_00.py"],
        )

    def test_single_pass_comment_strip_and_audit_zero_duplicate_resolvers(self):
        ts_src = (
            b"// header comment\n"
            b"import { helper } from './helper';\n"
            b"/* block comment */\n"
            b"export function computeTotal(a: number, b: number = 1): number {\n"
            b"  return helper(a) + b;\n"
            b"}\n"
        )
        with mock.patch.object(parsers, "_strip_js_comments", wraps=parsers._strip_js_comments) as spy_js:
            out, sig_h = parsers.extract_outline_and_sig_hash_from_bytes(ts_src, ".ts", "src/calc.ts")
            self.assertEqual(spy_js.call_count, 1)
        self.assertIsNotNone(sig_h)
        self.assertIn("./helper", out["imports"])
        self.assertTrue(any("computeTotal" in s for s in out["symbols"]))

        cpp_src = (
            b"/* header */\n"
            b"#include \"calc.h\"\n"
            b"int add_values(int x, int y) {\n"
            b"  return x + y;\n"
            b"}\n"
        )
        with mock.patch.object(parsers, "_strip_c_comments", wraps=parsers._strip_c_comments) as spy_c:
            out_cpp, sig_cpp = parsers.extract_outline_and_sig_hash_from_bytes(cpp_src, ".cpp", "src/calc.cpp")
            self.assertEqual(spy_c.call_count, 1)
        self.assertIsNotNone(sig_cpp)
        self.assertIn("calc.h", out_cpp["imports"])

        # `audit_architecture` reuses Go & JVM/C# resolution from `build_static_import_graph` without re-running
        self.write("go.mod", "module example.com/perf\n")
        self.write("pkg/a.go", "package pkg\nfunc A() int { return 1 }\n")
        self.write("cmd/main.go", 'package main\nimport "example.com/perf/pkg"\nfunc main() { _ = pkg.A() }\n')
        self.write("src/com/acme/Util.java", "package com.acme;\npublic class Util {}\n")
        self.write("src/com/acme/App.java", "package com.acme;\nimport com.acme.Util;\npublic class App {}\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "go.mod[RG5T]: F:declares Go module path | R:- | A:- | S:-",
                "pkg/a.go[DX4T]: F:provides A helper | R:- | A:A | S:-",
                "cmd/main.go[EX4T]: F:runs main binary | R:pkg/a.go | A:main | S:-",
                "src/com/acme/Util.java[CX3T]: F:provides Util class | R:- | A:Util | S:-",
                "src/com/acme/App.java[EX4T]: F:runs Java app | R:src/com/acme/Util.java | A:App | S:-",
            )[0],
            0,
        )
        with mock.patch.object(
            aoci_index, "resolve_go_package_imports", wraps=aoci_index.resolve_go_package_imports
        ) as spy_go, mock.patch.object(
            parsers, "_build_jvm_cs_indexes", wraps=parsers._build_jvm_cs_indexes
        ) as spy_jvm:
            res = aoci_index.audit_architecture(self.root, check_static=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(spy_go.call_count, 0)
            self.assertEqual(spy_jvm.call_count, 1)

    def test_dangling_refs_zero_stat_on_symbol_slashes_and_single_parse_verify(self):
        self.write("src/core.py", "def Alpha(): return 1\ndef Beta(): return 2\ndef Gamma(): return 3\n")
        self.write("src/app.py", "from src.core import Alpha\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/core.py[KX6T]: F:exports Alpha/Beta/Gamma helpers | R:- | A:Alpha/Beta/Gamma<-src/app.py | S:-",
                "src/app.py[EX5T]: F:dispatches HTTP/gRPC routes | R:src/core.py | A:handle/v1/users | S:-",
            )[0],
            0,
        )

        orig_exists = Path.exists
        exists_calls = []

        def tracked_exists(p: Path) -> bool:
            rel_s = p.as_posix()
            if any(tok in rel_s for tok in ("Alpha", "Beta", "HTTP", "handle/v1")):
                exists_calls.append(rel_s)
            return orig_exists(p)

        with mock.patch.object(Path, "exists", autospec=True, side_effect=tracked_exists), mock.patch.object(
            aoci_index, "parse_entries_map", wraps=aoci_index.parse_entries_map
        ) as spy_parse:
            ns = aoci_index.build_parser().parse_args(["verify", "--root", str(self.root), "--fast", "--json"])
            buf_rc = []
            import io, contextlib

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                buf_rc.append(aoci_index.cmd_verify(ns))
            self.assertEqual(buf_rc[0], 0)
            rep = json.loads(out.getvalue())
            self.assertTrue(rep["ok"])
            self.assertEqual(exists_calls, [])
            self.assertEqual(spy_parse.call_count, 1)

        # Genuine missing file reference is still caught and memoized to 1 `exists()` check
        entries_bad = {
            "src/a.py": "src/a.py[CX3T]: F:calls missing | R:src/missing_dep.py | A:- | S:-",
            "src/b.py": "src/b.py[CX3T]: F:also calls missing | R:src/missing_dep.py | A:- | S:-",
        }
        missing_checks = []

        def count_missing(p: Path) -> bool:
            if p.name == "missing_dep.py":
                missing_checks.append(p.as_posix())
            return orig_exists(p)

        with mock.patch.object(Path, "exists", autospec=True, side_effect=count_missing):
            dangling = graph.find_dangling_refs(
                self.root,
                entries_bad,
                existing_paths={"src/a.py", "src/b.py"},
            )
        self.assertEqual(dangling, {"src/a.py": ["src/missing_dep.py"], "src/b.py": ["src/missing_dep.py"]})
        self.assertEqual(len(missing_checks), 1)

    def test_pool_row_value_composite_lookup_and_fallback(self):
        self.write("src/a.py", "x = 1\n")
        self.write("src/b.py", "x = 1\n")  # identical SHA to `src/a.py`!
        self.write("src/c.py", "def compute_y() -> int:\n    val = 2\n    val += 1\n    return val\n")
        self.init()
        e_a = "src/a.py[CX3T]: F:defines x in module a | R:- | A:x | S:-"
        e_b = "src/b.py[CX3T]: F:defines x in module b | R:- | A:x | S:-"
        e_c = "src/c.py[CX3T]: F:defines y in module c | R:- | A:y | S:-"
        self.assertEqual(self.upsert(e_a, e_b, e_c)[0], 0)

        git_info = aoci_index.detect_git_info(self.root)
        base = aoci_index.load_baseline(self.root / ".aoci")["files"]
        sha_x = base["src/a.py"]["sha256"]
        sha_y = base["src/c.py"]["sha256"]
        self.assertEqual(sha_x, base["src/b.py"]["sha256"])

        ps_hits, s_hits = pool._lookup_pool_entries(
            self.root,
            git_info,
            [("src/a.py", sha_x)],
            [sha_y],
            touch_lru=True,
        )
        self.assertEqual(list(ps_hits.keys()), [("src/a.py", sha_x)])
        self.assertIn("module a", ps_hits[("src/a.py", sha_x)])
        self.assertIn(sha_y, s_hits)

    def test_reentrant_index_lock_holds_continuously_across_apply_sync(self):
        self.write("src/mod.py", "def calc(x: int) -> int:\n    return x + 1\n")
        self.init()
        self.assertEqual(
            self.upsert("src/mod.py[CX4T]: F:computes incremented integer | R:- | A:calc | S:-")[0],
            0,
        )
        # Body-only edit (`impl_only`) + new file -> `apply-sync` runs `ack` + `upsert` + `verify`
        self.write("src/mod.py", "def calc(x: int) -> int:\n    return x + 99\n")
        self.write("src/extra.py", "Y = 2\n")

        aoci_dir = self.root / ".aoci"
        lock_path = aoci_dir / ".lock"

        # Verify thread-local re-entrancy blocks OTHER threads while allowing same-thread nesting
        contended_err = []
        with lock.IndexLock(aoci_dir, timeout=1.0):
            self.assertTrue(lock_path.exists())
            with lock.IndexLock(aoci_dir, timeout=0.1) as nested:
                self.assertTrue(nested._reentrant)
                self.assertTrue(lock_path.exists())
            # Outer lock MUST still hold `.lock` after inner context exits!
            self.assertTrue(lock_path.exists())

            def other_thread_attempt() -> None:
                try:
                    with lock.IndexLock(aoci_dir, timeout=0.08):
                        pass
                except TimeoutError as exc:
                    contended_err.append(str(exc))

            t = threading.Thread(target=other_thread_attempt)
            t.start()
            t.join(timeout=2.0)
            self.assertEqual(len(contended_err), 1)
        self.assertFalse(lock_path.exists())

        # During `apply-sync`, `.lock` stays present during inner subcommands (`os.open` called only ONCE)
        open_calls = []
        real_os_open = lock.os.open

        def spy_open(p: str, flags: int, *rest):
            if p.endswith(".lock"):
                open_calls.append(p)
            return real_os_open(p, flags, *rest)

        with mock.patch.object(lock.os, "open", side_effect=spy_open):
            err_flag, out_txt = aoci_mcp.call_tool(
                "aoci_apply_sync",
                {
                    "root": str(self.root),
                    "ack_impl_only": True,
                    "reviewed": True,
                    "entries": ["src/extra.py[CX2T]: F:defines extra constant Y | R:- | A:Y | S:-"],
                },
            )
        self.assertFalse(err_flag, out_txt)
        self.assertEqual(len(open_calls), 1)
        self.assertFalse(lock_path.exists())

    def test_test_impact_index_bucketing_and_mcp_per_root_concurrency_and_diff_ttl(self):
        if not HAS_GIT:
            self.skipTest("git required")
        self._git("init", "-q")
        self.write("src/billing_invoice.py", "def create_invoice(): return 1\n")
        self.write("src/auth_token.py", "def check_token(): return True\n")
        self.write("tests/test_billing_invoice.py", "def test_inv(): assert True\n")
        self.write("tests/test_auth_token.py", "def test_tok(): assert True\n")
        for idx in range(60):
            self.write(f"tests/test_other_{idx:03d}.py", f"def test_{idx}(): assert True\n")
        self._git("add", ".")
        self._git("commit", "-qm", "init")

        self.init()
        entries = [
            "src/billing_invoice.py[AX6T]: F:creates customer invoices | R:- | A:create_invoice | S:-",
            "src/auth_token.py[MX7T]: F:verifies bearer tokens | R:- | A:check_token | S:-",
            "tests/test_billing_invoice.py[TX3T]: F:tests invoice creation | R:- | A:test_inv | S:-",
            "tests/test_auth_token.py[TX3T]: F:tests token verification | R:- | A:test_tok | S:-",
        ] + [
            f"tests/test_other_{idx:03d}.py[TX2T]: F:tests unrelated feature {idx} | R:- | A:test_{idx} | S:-"
            for idx in range(60)
        ]
        self.assertEqual(self.upsert(*entries)[0], 0)
        aoci_mcp._CACHE.invalidate(self.root)

        # Bucketed `TestImpactIndex` probes `_matches_naming_convention` ONLY on matching stem bucket (1 call, not 62!)
        with mock.patch.object(
            test_impact, "_matches_naming_convention", wraps=test_impact._matches_naming_convention
        ) as spy_match:
            err, out = aoci_mcp.call_tool(
                "aoci_query",
                {"root": str(self.root), "tests_for": "src/billing_invoice.py"},
            )
            self.assertFalse(err, out)
            q_res = json.loads(out)
            self.assertEqual([t["path"] for t in q_res["impacted_tests"]], ["tests/test_billing_invoice.py"])
            self.assertEqual(spy_match.call_count, 1)

        # Back-to-back `aoci_pack(include_diff=True)` hits short-TTL diff cache (`compute_diff_briefing` called once)
        with mock.patch.object(
            aoci_index, "compute_diff_briefing", wraps=aoci_index.compute_diff_briefing
        ) as spy_diff:
            e1, p1 = aoci_mcp.call_tool("aoci_pack", {"root": str(self.root), "task": "billing invoice"})
            e2, p2 = aoci_mcp.call_tool("aoci_pack", {"root": str(self.root), "task": "auth token"})
            self.assertFalse(e1, p1)
            self.assertFalse(e2, p2)
            self.assertEqual(spy_diff.call_count, 1)

        # `prepare_sync_briefing` reuses outlines written by `scaffold` without calling `extract_file_outline` again
        self.write("src/brand_new.py", "def brand_new_api() -> str:\n    return 'ok'\n")
        git_info = aoci_index.detect_git_info(self.root)
        drift = aoci_index.compute_drift(self.root, git_info, fast_mtime=True, allow_heal=False, save_scan_cache=False)
        with mock.patch.object(
            sync_prep, "extract_file_outline", side_effect=AssertionError("should hit fresh outline_cache.json")
        ):
            brief = sync_prep.prepare_sync_briefing(
                self.root,
                git_info,
                drift,
                scaffold_fn=aoci_index.scaffold_entries,
                run_git_fn=aoci_index.run_git,
            )
        self.assertEqual(len(brief["added_scaffolds"]), 1)
        self.assertEqual(brief["added_scaffolds"][0]["path"], "src/brand_new.py")
        self.assertTrue(any("brand_new_api" in s for s in brief["added_scaffolds"][0]["current_outline"]["symbols"]))

        # Per-root lock concurrency: slow cold parse on repo A does NOT block warm `aoci_overview` on `self.root`
        other_root = Path(self._tmp.name) / "other_repo"
        other_root.mkdir()
        (other_root / "m.py").write_text("Z = 1\n", encoding="utf-8")
        cli_json("init", "--root", str(other_root))
        cli_json(
            "upsert",
            "--root",
            str(other_root),
            "--entry",
            "m.py[CX2T]: F:defines Z constant | R:- | A:Z | S:-",
        )
        aoci_mcp._CACHE.invalidate(other_root)
        # Ensure `self.root` slot is warm
        self.assertIsNotNone(aoci_mcp._CACHE.get(self.root))

        entered_slow = threading.Event()
        release_slow = threading.Event()
        real_parse_cv = aoci_index.parse_code_volume

        def slow_other_parse(code_path: Path, *a, **kw):
            if other_root.resolve() in code_path.resolve().parents:
                entered_slow.set()
                release_slow.wait(timeout=3.0)
            return real_parse_cv(code_path, *a, **kw)

        with mock.patch.object(aoci_index, "parse_code_volume", side_effect=slow_other_parse):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool_ex:
                fut_slow = pool_ex.submit(aoci_mcp.call_tool, "aoci_overview", {"root": str(other_root)})
                self.assertTrue(entered_slow.wait(timeout=2.0))
                t_fast = time.perf_counter()
                err_warm, out_warm = aoci_mcp.call_tool("aoci_overview", {"root": str(self.root)})
                warm_ms = (time.perf_counter() - t_fast) * 1000.0
                release_slow.set()
                err_other, out_other = fut_slow.result(timeout=3.0)
        self.assertFalse(err_warm, out_warm)
        self.assertFalse(err_other, out_other)
        self.assertLess(warm_ms, 250.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
