#!/usr/bin/env python3
"""Tests for AOCI v1.5.0 improvements (rich sig_hash, hub lifecycle, prepare-sync, co-change, HTML export, parsers)."""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_aoci import (  # noqa: E402
    HAS_GIT,
    HOOK,
    INSTALL,
    SCRIPT,
    RepoTestCase,
    aoci_index,
    cli,
    cli_json,
)
from aoci_core import hubs, pack, parsers  # noqa: E402
from aoci_core.constants import AOCI_DIR_NAME, HUBS_FILE, OUTLINE_CACHE  # noqa: E402


class V15ImprovementsTests(RepoTestCase):
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

    def test_multilang_rich_sig_hash_impl_vs_contract(self):
        cases = [
            (
                "app.ts",
                "export function greet(name: string): string {\n  return 'hi ' + name;\n}\n",
                "export function greet(name: string): string {\n  const prefix = 'hello ';\n  return prefix + name;\n}\n",
                "export function greet(name: string, polite: boolean): string {\n  return 'hi ' + name;\n}\n",
            ),
            (
                "svc.go",
                "package svc\nfunc Handle(id int) error {\n\treturn nil\n}\n",
                "package svc\nfunc Handle(id int) error {\n\t_ = id + 1\n\treturn nil\n}\n",
                "package svc\nfunc Handle(id string) error {\n\treturn nil\n}\n",
            ),
            (
                "lib.rs",
                "pub fn compute(x: u32) -> u32 {\n    x + 1\n}\n",
                "pub fn compute(x: u32) -> u32 {\n    let y = x * 2;\n    y\n}\n",
                "pub fn compute(x: u64) -> u64 {\n    x + 1\n}\n",
            ),
            (
                "Auth.java",
                "public class Auth {\n  public int verify(String tok) {\n    return 1;\n  }\n}\n",
                "public class Auth {\n  public int verify(String tok) {\n    int code = 2;\n    return code;\n  }\n}\n",
                "public class Auth {\n  public boolean verify(String tok, boolean strict) {\n    return true;\n  }\n}\n",
            ),
            (
                "engine.cpp",
                "class Engine {\npublic:\n  int step(double dt) {\n    return 0;\n  }\n};\n",
                "class Engine {\npublic:\n  int step(double dt) {\n    int v = static_cast<int>(dt);\n    return v;\n  }\n};\n",
                "class Engine {\npublic:\n  double step(double dt, bool clamp) {\n    return dt;\n  }\n};\n",
            ),
            (
                "api.proto",
                'syntax = "proto3";\nmessage User {\n  string id = 1;\n}\n',
                'syntax = "proto3";\n// comment changed\nmessage User {\n  string id = 1;\n}\n',
                'syntax = "proto3";\nmessage User {\n  string id = 1;\n  string email = 2;\n}\n',
            ),
            (
                "schema.sql",
                "CREATE TABLE orders (\n  id INT PRIMARY KEY\n);\n",
                "-- harmless SQL comment\nCREATE TABLE orders (\n  id INT PRIMARY KEY\n);\n",
                "CREATE TABLE orders (\n  id INT PRIMARY KEY,\n  total NUMERIC\n);\n",
            ),
        ]
        for rel, base_src, impl_src, contract_src in cases:
            ext = Path(rel).suffix
            h_base = parsers.compute_signature_hash_from_bytes(base_src.encode("utf-8"), ext, rel)
            h_impl = parsers.compute_signature_hash_from_bytes(impl_src.encode("utf-8"), ext, rel)
            h_contract = parsers.compute_signature_hash_from_bytes(contract_src.encode("utf-8"), ext, rel)
            self.assertIsNotNone(h_base, f"expected sig_hash for {rel}")
            self.assertEqual(h_base, h_impl, f"expected impl-only match for {rel}")
            self.assertNotEqual(h_base, h_contract, f"expected contract change detection for {rel}")

    def test_hubs_full_lifecycle_manifest_upsert_rename_verify_lint_context_diff_pack(self):
        self.write(
            "src/core.py",
            "class Worker:\n    def run(self):\n        return 1\n\ndef finalize():\n    return 0\n",
        )
        self.write("src/caller.py", "from src.core import Worker\n\ndef main():\n    return Worker().run()\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/core.py[DA9T]: F:core worker task runner and finalizer | R:- | A:Worker/finalize<-src/caller.py | S:stateless",
                "src/caller.py[EA5T]: F:invokes Worker loop on startup | R:src/core.py | A:main | S:-",
            )[0],
            0,
        )
        self._git("add", "src/core.py", "src/caller.py")
        self._git("commit", "-m", "initial")

        # 1) zoom upsert updates aoci.txt #Volumes: to include `hubs`
        z_up = cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "src/core.py#Worker[L1-L3]: F:worker class executing core task | S:stateless",
            "--upsert",
            "src/core.py#finalize[L5-L6]: F:returns terminal status zero | S:idempotent",
        )
        self.assertTrue(z_up["ok"])
        manifest_txt = (self.root / AOCI_DIR_NAME / "aoci.txt").read_text(encoding="utf-8")
        self.assertIn("hubs", manifest_txt)

        # 2) Edit src/core.py to shift lines + check verify/lint catch stale span, then upsert refreshes span
        self.write(
            "src/core.py",
            "\n\nclass Worker:\n    def run(self):\n        return 2\n\ndef finalize():\n    return 0\n",
        )
        v_rc, v_out = cli("verify", "--root", str(self.root), "--json")
        self.assertNotEqual(v_rc, 0)
        v_data = json.loads(v_out)
        self.assertTrue(any("outdated" in iss or "span" in iss or "modified" in iss for iss in v_data["issues"]))

        l_data = cli_json("lint", "--root", str(self.root))
        self.assertTrue(any(f["code"] == "hub-span-outdated" for f in l_data["findings"]))

        up_data = cli_json(
            "upsert",
            "--root",
            str(self.root),
            "--entry",
            "src/core.py[DA9T]: F:core worker task runner and finalizer | R:- | A:Worker/finalize<-src/caller.py | S:stateless",
        )
        self.assertTrue(up_data["ok"])
        self.assertTrue(len(up_data.get("hubs_refreshed", [])) >= 2)
        recs, _ = hubs.parse_hubs_volume(self.root / AOCI_DIR_NAME / HUBS_FILE)
        self.assertEqual([(r["symbol"], r["start"], r["end"]) for r in recs], [("Worker", 3, 5), ("finalize", 7, 8)])

        # 3) context, diff (with modification), and pack surface hub_sections
        ctx_data = cli_json("context", "--root", str(self.root), "src/caller.py")
        self.assertTrue(len(ctx_data.get("hub_sections", [])) >= 2)
        pack_data = cli_json("pack", "--root", str(self.root), "--task", "worker finalize", "--no-diff")
        self.assertTrue(len(pack_data.get("hub_sections", [])) >= 2)
        diff_data = cli_json("diff", "--root", str(self.root))
        self.assertTrue(len(diff_data.get("hub_sections", [])) >= 2)

        # 4) migrate-renames moves hub entries cleanly
        shutil.move(str(self.root / "src/core.py"), str(self.root / "src/engine.py"))
        age_p = self.root / "src/engine.py"
        self.write("src/engine.py", age_p.read_text(encoding="utf-8"))
        mig = cli_json("migrate-renames", "--root", str(self.root))
        self.assertTrue(mig["ok"])
        self.assertTrue(len(mig.get("migrated_hubs", [])) >= 2)
        recs_after_mig, _ = hubs.parse_hubs_volume(self.root / AOCI_DIR_NAME / HUBS_FILE)
        self.assertEqual({r["relpath"] for r in recs_after_mig}, {"src/engine.py"})

        # 5) deleting file and upsert --prune-only prunes hub entries automatically
        (self.root / "src/engine.py").unlink()
        prune_data = cli_json("upsert", "--root", str(self.root), "--prune-only")
        self.assertTrue(prune_data["ok"])
        self.assertTrue(len(prune_data.get("hubs_pruned", [])) >= 2)
        recs_final, _ = hubs.parse_hubs_volume(self.root / AOCI_DIR_NAME / HUBS_FILE)
        self.assertEqual(recs_final, [])

    def test_pack_with_tests_live_anchors_and_path_mention_index(self):
        self.write("src/service.py", "def handle_order():\n    return 1\n")
        self.write("pages/posts/[id].tsx", "export default function PostPage() { return null; }\n")
        self.write("tests/test_service.py", "from src.service import handle_order\n\ndef test_ok():\n    assert handle_order() == 1\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/service.py[CA7T]: F:order request handler returning integer status | R:- | A:handle_order<-tests/test_service.py | S:idempotent",
                "pages/posts/[id].tsx[UA5T]: F:renders dynamic blog post page route | R:- | A:PostPage | S:-",
                "tests/test_service.py[TG2T]: F:unit test for order handle_order | R:src/service.py | A:- | S:-",
            )[0],
            0,
        )

        # PathMentionIndex handles wrapper delimiters and Next.js brackets
        known = sorted(["pages/posts/[id].tsx", "src/service.py", "tests/test_service.py"])
        m_idx = pack.PathMentionIndex(known)
        found = pack._extract_task_path_mentions("fix (src/service.py) and pages/posts/[id].tsx.", known, mention_index=m_idx)
        self.assertEqual(found, ["pages/posts/[id].tsx", "src/service.py"])

        # Default pack leaves cold cache absent and symbols empty
        cold_cache = self.root / AOCI_DIR_NAME / ".cache" / OUTLINE_CACHE
        p_default = cli_json("pack", "--root", str(self.root), "--task", "handle order in src/service.py")
        self.assertFalse(cold_cache.exists())
        self.assertEqual(p_default["open_next"][0]["symbols"], [])
        self.assertNotIn("impacted_tests", p_default)

        # --with-tests and --live-anchors work without writing outline_cache.json
        p_live = cli_json(
            "pack",
            "--root",
            str(self.root),
            "--task",
            "handle order in src/service.py",
            "--with-tests",
            "--live-anchors",
        )
        self.assertFalse(cold_cache.exists())
        self.assertTrue(p_live["open_next"][0]["symbols"])
        self.assertEqual([t["path"] for t in p_live["impacted_tests"]], ["tests/test_service.py"])

    def test_prepare_sync_cli_and_mcp(self):
        import aoci_mcp

        self.write("src/clean.py", "def stay():\n    return 1\n")
        self.write("src/impl.py", "def calc(x):\n    return x + 1\n")
        self.write("src/api.py", "def endpoint(uid):\n    return uid\n")
        self.write("src/old_mod.py", "def moved_fn():\n    line_a = 1\n    line_b = 2\n    return line_a + line_b\n")
        self.write("src/gone.py", "x = 9\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/clean.py[CG4T]: F:unchanged baseline helper function | R:- | A:stay<-src/api.py | S:-",
                "src/impl.py[CG5T]: F:calculates integer offset helper | R:- | A:calc<-src/api.py | S:-",
                "src/api.py[EA8T]: F:handles uid request endpoint | R:src/clean.py,src/impl.py | A:endpoint | S:public API",
                "src/old_mod.py[CG4T]: F:module scheduled to be renamed cleanly | R:- | A:moved_fn | S:-",
                "src/gone.py[CG3T]: F:deprecated constant pending removal | R:- | A:- | S:-",
            )[0],
            0,
        )
        self._git("add", "src/clean.py", "src/impl.py", "src/api.py", "src/old_mod.py", "src/gone.py")
        self._git("commit", "-m", "baseline")

        self.write("src/impl.py", "def calc(x):\n    y = x * 2\n    return y\n")
        self.write("src/api.py", "def endpoint(uid, strict=False):\n    return (uid, strict)\n")
        self.write("src/new_add.py", "from src.clean import stay\n\ndef fresh():\n    return stay()\n")
        shutil.move(str(self.root / "src/old_mod.py"), str(self.root / "src/new_mod.py"))
        self.write("src/new_mod.py", (self.root / "src/new_mod.py").read_text(encoding="utf-8"))
        (self.root / "src/gone.py").unlink()

        prep = cli_json("prepare-sync", "--root", str(self.root), "--max-diff-lines", "20")
        self.assertTrue(prep["ok"])
        self.assertTrue(prep["has_drift"])
        self.assertEqual([x["path"] for x in prep["impl_only_modified"]], ["src/impl.py"])
        contract_mods = {x["path"]: x for x in prep["contract_or_unknown_modified"]}
        self.assertIn("src/api.py", contract_mods)
        self.assertTrue(contract_mods["src/api.py"]["previous_entry"])
        self.assertTrue(contract_mods["src/api.py"]["current_outline"]["symbols"])
        self.assertIn("strict", contract_mods["src/api.py"]["diff_preview"])
        self.assertEqual([x["path"] for x in prep["added_scaffolds"]], ["src/new_add.py"])
        self.assertIn("src/new_add.py[", prep["added_scaffolds"][0]["scaffold"])
        self.assertEqual(prep["deleted"], ["src/gone.py"])
        self.assertEqual(len(prep["renames"]), 1)
        self.assertTrue(prep["recommended_actions"])

        mcp_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 91,
                "method": "tools/call",
                "params": {"name": "aoci_prepare_sync", "arguments": {"root": str(self.root)}},
            }
        )
        self.assertFalse(mcp_resp["result"]["isError"])
        mcp_data = json.loads(mcp_resp["result"]["content"][0]["text"])
        self.assertEqual(mcp_data["counts"], prep["counts"])

    def test_audit_cochange_unlinked_cli_and_mcp(self):
        import aoci_mcp

        self.write("src/a.py", "def a():\n    return 0\n")
        self.write("src/b.py", "def b():\n    return 0\n")
        self.write("src/c.py", "def c():\n    return 0\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "src/a.py[CA6T]: F:component a importing component c cleanly | R:src/c.py | A:a | S:-",
                "src/b.py[CA6T]: F:component b coupled silently with a | R:- | A:b | S:-",
                "src/c.py[CA6T]: F:shared component c depended on by a | R:- | A:c<-src/a.py | S:-",
            )[0],
            0,
        )
        for step in range(1, 5):
            pad = "x" * step
            self.write("src/a.py", f"def a():\n    # {pad}\n    return {step}\n", aged=False)
            self.write("src/b.py", f"def b():\n    # {pad}\n    return {step}\n", aged=False)
            self.write("src/c.py", f"def c():\n    # {pad}\n    return {step}\n", aged=False)
            self._git("add", "src/a.py", "src/b.py", "src/c.py")
            self._git("commit", "-m", f"rev {step}")

        aud = cli_json("audit", "--root", str(self.root), "--no-static", "--co-change", "--min-co-changes", "3")
        pairs = {(r["file_a"], r["file_b"]): r for r in aud["co_change_unlinked"]}
        self.assertIn(("src/a.py", "src/b.py"), pairs)
        self.assertEqual(pairs[("src/a.py", "src/b.py")]["co_changes"], 4)
        # src/a.py -> src/c.py is already linked via R:, so excluded
        self.assertNotIn(("src/a.py", "src/c.py"), pairs)

        mcp_aud = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 92,
                "method": "tools/call",
                "params": {
                    "name": "aoci_audit",
                    "arguments": {
                        "root": str(self.root),
                        "check_static": False,
                        "check_co_change": True,
                        "min_co_changes": 3,
                    },
                },
            }
        )
        self.assertFalse(mcp_aud["result"]["isError"])
        mcp_data = json.loads(mcp_aud["result"]["content"][0]["text"])
        self.assertGreaterEqual(mcp_data["summary"]["co_change_unlinked"], 1)

    def test_idl_cpp_outlines_monorepo_and_nested_gitignore(self):
        self.write("proto/common.proto", 'syntax = "proto3";\nmessage Meta { string ver = 1; }\n')
        self.write("proto/service.proto", 'syntax = "proto3";\nimport "proto/common.proto";\nservice OrderSvc {\n  rpc GetOrder (Meta) returns (Meta);\n}\n')
        self.write("graphql/schema.graphql", "type User { id: ID! }\ninput CreateUser { name: String! }\n")
        self.write("prisma/schema.prisma", "model Account {\n  id Int @id\n}\nenum Role {\n  USER\n  ADMIN\n}\n")
        self.write("db/init.sql", "CREATE TABLE users (id INT PRIMARY KEY);\nCREATE VIEW active_users AS SELECT id FROM users;\n")
        self.write("cpp/util.hpp", "#pragma once\nint add(int a, int b);\n")
        self.write("cpp/main.cpp", '#include "util.hpp"\nnamespace core {\nint run_app() { return add(1, 2); }\n}\n')

        o_proto = parsers.extract_file_outline(self.root / "proto/service.proto", "proto/service.proto")
        self.assertIn("proto/common.proto", o_proto["imports"])
        self.assertTrue(any("service OrderSvc" in s for s in o_proto["symbols"]))

        o_gql = parsers.extract_file_outline(self.root / "graphql/schema.graphql", "graphql/schema.graphql")
        self.assertTrue(any("type User" in s for s in o_gql["symbols"]))

        o_pri = parsers.extract_file_outline(self.root / "prisma/schema.prisma", "prisma/schema.prisma")
        self.assertTrue(any("model Account" in s for s in o_pri["symbols"]))

        o_sql = parsers.extract_file_outline(self.root / "db/init.sql", "db/init.sql")
        self.assertTrue(any("table users" in s for s in o_sql["symbols"]))

        o_cpp = parsers.extract_file_outline(self.root / "cpp/main.cpp", "cpp/main.cpp")
        self.assertIn("util.hpp", o_cpp["imports"])

        # Monorepo independent nested tsconfig.json + go.mod resolution
        self.write(
            "packages/web/tsconfig.json",
            '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}',
        )
        self.write("packages/web/src/ui.ts", "export const v = 1;\n")
        self.write("packages/web/src/app.ts", "import { v } from '@/ui';\nexport const x = v;\n")
        self.write("services/auth/go.mod", "module example.com/auth\n\ngo 1.21\n")
        self.write("services/auth/pkg/tok/tok.go", "package tok\nfunc Sign() int { return 1 }\n")
        self.write("services/auth/cmd/srv.go", 'package main\nimport "example.com/auth/pkg/tok"\nfunc main() { _ = tok.Sign() }\n')

        discovered = [
            "cpp/util.hpp",
            "cpp/main.cpp",
            "proto/common.proto",
            "proto/service.proto",
            "packages/web/src/ui.ts",
            "packages/web/src/app.ts",
            "services/auth/pkg/tok/tok.go",
            "services/auth/cmd/srv.go",
        ]
        s_deps, _, _ = parsers.build_static_import_graph(self.root, discovered)
        self.assertIn("cpp/util.hpp", s_deps["cpp/main.cpp"])
        self.assertIn("proto/common.proto", s_deps["proto/service.proto"])
        self.assertIn("packages/web/src/ui.ts", s_deps["packages/web/src/app.ts"])
        self.assertIn("services/auth/pkg/tok/tok.go", s_deps["services/auth/cmd/srv.go"])

        # Non-Git nested .gitignore support
        with tempfile.TemporaryDirectory() as non_git_tmp:
            ng_root = Path(non_git_tmp) / "plain"
            (ng_root / "sub").mkdir(parents=True)
            (ng_root / "sub" / ".gitignore").write_text("secret.py\nbuild_out/\n", encoding="utf-8")
            (ng_root / "sub" / "secret.py").write_text("k = 1\n", encoding="utf-8")
            (ng_root / "sub" / "kept.py").write_text("k = 2\n", encoding="utf-8")
            (ng_root / "sub" / "build_out").mkdir()
            (ng_root / "sub" / "build_out" / "gen.py").write_text("g = 3\n", encoding="utf-8")
            cands, _ = aoci_index.discover_candidates(
                ng_root, {"is_git": False}, aoci_index.load_ignore_matcher(ng_root, {"is_git": False})
            )
            rel_paths = [p for p, _ in cands]
            self.assertIn("sub/kept.py", rel_paths)
            self.assertNotIn("sub/secret.py", rel_paths)
            self.assertNotIn("sub/build_out/gen.py", rel_paths)

    def test_html_export_cycles_and_script_escape(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "import a\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "a.py[DA7T]: F:cyclic node </script><script>alert(1)</script> alpha | R:b.py | A:- | S:guard cycle",
                "b.py[DA7T]: F:cyclic node beta referencing a | R:a.py | A:- | S:-",
            )[0],
            0,
        )
        exp = cli_json("export", "--root", str(self.root), "--format", "html")
        self.assertTrue(exp["ok"])
        self.assertEqual(exp["format"], "html")
        html_out = exp["content"]
        self.assertTrue(html_out.startswith("<!doctype html>"))
        self.assertNotIn("</script><script>", html_out)
        self.assertIn("<\\/script>", html_out)
        self.assertIn('"inCycle": true', html_out)

    def test_concurrent_lockfree_mcp_cli_capture_and_inprocess_tools(self):
        import aoci_mcp

        repos = []
        with tempfile.TemporaryDirectory() as tmpd:
            for i in range(8):
                r = Path(tmpd) / f"repo_{i}"
                r.mkdir()
                (r / f"mod_{i}.py").write_text(f"def fn_{i}():\n    return {i}\n", encoding="utf-8")
                cli("init", "--root", str(r))
                cli(
                    "upsert",
                    "--root",
                    str(r),
                    "--entry",
                    f"mod_{i}.py[CA5T]: F:unique worker token repo_{i}_tag | R:- | A:fn_{i} | S:-",
                )
                repos.append((i, r))

            def _run_one(item):
                idx, repo_path = item
                rc, out = aoci_mcp._run_cli_capture(["verify", "--root", str(repo_path), "--json"])
                return idx, rc, json.loads(out)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(_run_one, repos))
            for idx, rc, payload in results:
                self.assertEqual(rc, 0)
                self.assertTrue(payload["ok"])
                self.assertEqual(Path(payload["root"]).name, f"repo_{idx}")

        # In-process aoci_outline, aoci_lint, aoci_zoom(read), and cache invalidation on aoci_upsert
        self.write("m.py", "def ok():\n    return 1\n")
        self.init()
        self.assertEqual(self.upsert("m.py[CA5T]: F:provides ok helper function | R:- | A:ok | S:-")[0], 0)
        ov1 = json.loads(
            aoci_mcp.handle_rpc_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "aoci_overview", "arguments": {"root": str(self.root), "min_importance": 1}}}
            )["result"]["content"][0]["text"]
        )
        self.assertEqual(ov1["indexed_files"], 1)

        out_r = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "aoci_outline", "arguments": {"root": str(self.root), "paths": ["m.py"]}}}
        )
        self.assertFalse(out_r["result"]["isError"])
        lint_r = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "aoci_lint", "arguments": {"root": str(self.root)}}}
        )
        self.assertFalse(lint_r["result"]["isError"])
        zoom_r = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "aoci_zoom", "arguments": {"root": str(self.root), "mode": "read", "path": "m.py"}}}
        )
        self.assertFalse(zoom_r["result"]["isError"])

    def test_install_project_mcp_plugin_name_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmpd:
            proj = Path(tmpd) / "proj"
            home = Path(tmpd) / "home"
            proj.mkdir()
            home.mkdir()
            r_in = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "antigravity",
                    "--scope",
                    "project",
                    "--project-root",
                    str(proj),
                    "--home-dir",
                    str(home),
                    "--with-mcp",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_in.returncode, 0, r_in.stdout + r_in.stderr)
            pjson = proj / ".agents" / "plugins" / "aoci-index" / "plugin.json"
            self.assertTrue(pjson.is_file())
            self.assertEqual(json.loads(pjson.read_text(encoding="utf-8"))["name"], "aoci-index-project-mcp")

            r_un = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "antigravity",
                    "--scope",
                    "project",
                    "--project-root",
                    str(proj),
                    "--home-dir",
                    str(home),
                    "--uninstall",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
            self.assertFalse((proj / ".agents" / "plugins" / "aoci-index").exists())

    def test_real_subprocess_cli_and_hook_smoke(self):
        r_ver = subprocess.run(
            [sys.executable, str(SCRIPT), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r_ver.returncode, 0, r_ver.stderr)
        self.assertIn(aoci_index.AOCI_VERSION, r_ver.stdout)

        r_hook = subprocess.run(
            [sys.executable, str(HOOK), "--host", "claude-code", "--event", "session-start"],
            input=json.dumps({"cwd": str(self.root), "session_id": "smoke-subproc"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r_hook.returncode, 0, r_hook.stderr)
        self.assertIsInstance(json.loads(r_hook.stdout), dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
