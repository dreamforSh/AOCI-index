#!/usr/bin/env python3
"""FRAS graph, BM25 search, glossary, lint/verify validation, context budgets, and exports."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    SKILL_DIR,
    aoci_index,
    cli,
    cli_json,
)


class GraphTests(RepoTestCase):
    def _setup(self):
        for rel in ("src/env.py", "src/jwt.py", "src/routes.py", "pyproject.toml"):
            self.write(rel, f"# {rel}\n")
        self.init()
        self.upsert(
            "src/env.py[CG7T]: F:loads environment settings | R:pyproject.toml | A:get_settings<-src/jwt.py | S:-",
            "src/jwt.py[MA8T]: F:issues signed access tokens | R:- | A:issue_token | S:alg allowlist",
            "src/routes.py[EA6T]: F:exposes HTTP token endpoints | R:src/jwt.py | A:router | S:-",
            "pyproject.toml[RG3T]: F:declares package metadata and deps | R:- | A:- | S:-",
        )

    def test_context_respects_A_caller_direction(self):
        self._setup()
        data = cli_json("context", "--root", str(self.root), "src/jwt.py")
        deps = " ".join(data["dependencies"])
        callers = " ".join(data["callers"])
        self.assertIn("src/env.py[", deps)          # jwt calls env.get_settings
        self.assertNotIn("src/env.py[", callers)
        self.assertIn("src/routes.py[", callers)    # routes reads jwt

    def test_root_level_files_resolved_and_depth(self):
        self._setup()
        data = cli_json("context", "--root", str(self.root), "src/jwt.py", "--depth", "2")
        deps = " ".join(data["dependencies"])
        self.assertIn("pyproject.toml[", deps)

    def test_query_impact_lists_dependents_only(self):
        self._setup()
        entries = " ".join(cli_json("query", "--root", str(self.root), "--impact", "src/jwt.py")["entries"])
        self.assertIn("src/routes.py[", entries)
        self.assertIn("src/jwt.py[", entries)
        self.assertNotIn("src/env.py[", entries)


class LintTests(RepoTestCase):
    def test_lint_flags_vague_hub_and_dangling(self):
        self.write("core.py", "x = 1\n")
        self.write("util.py", "y = 2\n")
        self.init()
        self.upsert(
            "core.py[KX9T]: F:core logic helpers | R:src/gone.py | A:- | S:-",
            "util.py[CX3T]: F:formats ISO dates for log lines | R:- | A:fmt | S:-",
        )
        codes = {(f["path"], f["code"]) for f in cli_json("lint", "--root", str(self.root))["findings"]}
        self.assertIn(("core.py", "F-vague"), codes)
        self.assertIn(("core.py", "S-empty-hub"), codes)
        self.assertIn(("core.py", "ref-dangling"), codes)
        self.assertNotIn(("util.py", "F-vague"), codes)
        code, _ = cli("lint", "--root", str(self.root), "--strict")
        self.assertEqual(code, 1)

    def test_lint_cjk_responsibility_and_vague_detection(self):
        for rel in ("jwt.py", "mix.py", "short.py", "vague.py"):
            self.write(rel, "x = 1\n")
        self.init()
        self.upsert(
            "jwt.py[MA8T]: F:签发并校验支持多密钥轮换的访问令牌 | R:- | A:sign | S:强制白名单算法并拒绝none",
            "mix.py[CX3T]: F:签发JWT令牌 | R:- | A:issue | S:-",
            "short.py[CX2T]: F:鉴权工具 | R:- | A:- | S:-",
            "vague.py[CX3T]: F:核心逻辑辅助函数 | R:- | A:- | S:-",
        )
        codes = {(f["path"], f["code"]) for f in cli_json("lint", "--root", str(self.root))["findings"]}
        self.assertNotIn(("jwt.py", "F-too-short"), codes)
        self.assertNotIn(("jwt.py", "F-vague"), codes)
        self.assertNotIn(("mix.py", "F-too-short"), codes)
        self.assertIn(("short.py", "F-too-short"), codes)
        self.assertIn(("vague.py", "F-vague"), codes)


class P0RegressionTests(unittest.TestCase):

    def test_dynamic_frontend_route_refs_and_root_brackets(self):
        entries = {
            "[slug].tsx": (
                "[slug].tsx[UG5T]: F:renders root catch-all slug page | "
                "R:app/(auth)/[id]/page.tsx,src/routes/+page.svelte | A:Page<-app/routes/orders.$id.tsx | S:-"
            ),
            "app/(auth)/[id]/page.tsx": (
                "app/(auth)/[id]/page.tsx[UA6T]: F:renders authenticated user detail page | "
                "R:app/@modal/default.tsx | A:UserPage | S:-"
            ),
            "src/routes/+page.svelte": (
                "src/routes/+page.svelte[UG4T]: F:renders SvelteKit landing page | R:- | A:load | S:-"
            ),
            "app/routes/orders.$id.tsx": (
                "app/routes/orders.$id.tsx[UO6T]: F:renders Remix order detail route | R:- | A:loader | S:-"
            ),
            "app/@modal/default.tsx": (
                "app/@modal/default.tsx[UG3T]: F:provides parallel route fallback modal | R:- | A:DefaultModal | S:-"
            ),
        }
        deps, dependents = aoci_index.build_fras_graph(entries)
        self.assertEqual(
            deps["[slug].tsx"],
            {"app/(auth)/[id]/page.tsx", "src/routes/+page.svelte"},
        )
        self.assertEqual(dependents["[slug].tsx"], {"app/routes/orders.$id.tsx"})
        self.assertEqual(deps["app/(auth)/[id]/page.tsx"], {"app/@modal/default.tsx"})

    def test_dangling_refs_catches_root_files_and_ignores_protocols_and_examples_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "keep.py").write_text("x = 1\n", encoding="utf-8")
            entries = {
                "keep.py": (
                    "keep.py[CG5T]: F:speaks HTTP protocol with upstream | "
                    "R:HTTP/1.1,TLS/1.3,gone_root.py | "
                    "A:OrderService.create/OrderService.cancel<-missing_cli.py | S:-"
                ),
            }
            dangling = aoci_index.find_dangling_refs(root, entries)
            self.assertEqual(dangling, {"keep.py": ["gone_root.py", "missing_cli.py"]})

        # Bundled examples/ must be self-contained with 0 lint findings when backed by matching files
        with tempfile.TemporaryDirectory() as td:
            ex_root = Path(td)
            ex_aoci = ex_root / ".aoci"
            shutil.copytree(SKILL_DIR / "examples", ex_aoci)
            ex_entries, malformed = aoci_index.parse_code_volume(ex_aoci / "aoci.code.txt")
            self.assertEqual(malformed, [])
            for rel in ex_entries:
                fp = ex_root / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text("# stub\n", encoding="utf-8")
            findings = aoci_index.lint_entries(ex_root, ex_entries, aoci_index.parse_declared_domains(ex_aoci))
            self.assertEqual(findings, [])


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase3FeatureTests(RepoTestCase):

    def _git(self, *args: str, cwd: Path = None) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd or self.root,
            check=True,
        )

    def test_bm25_search_and_multivalue_layer_domain_filters(self):
        for rel in ("jwt.py", "orders.py", "misc.py", "cn_auth.py"):
            self.write(rel, f"# {rel}\n")
        self.init()
        meta = self.root / ".aoci" / "aoci.meta.txt"
        meta.write_text(
            meta.read_text(encoding="utf-8") + "\n# D (Domain):\n#   G=Global X=Core A=Auth O=Order\n",
            encoding="utf-8",
        )
        self.upsert(
            "jwt.py[MA9T]: F:Issues and verifies JWT access tokens with multi-KID key rotation | R:- | A:signToken | S:reject alg none",
            "orders.py[EO7T]: F:Exposes HTTP order creation endpoints | R:jwt.py | A:createOrder | S:-",
            "misc.py[CX2T]: F:Formats log rotation timestamps | R:- | A:fmt | S:-",
            "cn_auth.py[MA8T]: F:签发并校验支持多密钥轮换的访问令牌 | R:- | A:verify | S:拒绝空算法",
        )
        # Multi-term non-adjacent BM25 query ("jwt rotation" is not a contiguous substring)
        q_en = cli_json("query", "--root", str(self.root), "--search", "jwt rotation", "--top", "2")
        self.assertGreaterEqual(q_en["count"], 1)
        self.assertEqual(q_en["scores"][0]["path"], "jwt.py")

        # CJK multi-term BM25 query
        q_cn = cli_json("query", "--root", str(self.root), "--search", "令牌 轮换")
        self.assertEqual(q_cn["scores"][0]["path"], "cn_auth.py")

        # Comma-separated Layer and Domain filters
        q_multi = cli_json("query", "--root", str(self.root), "--layer", "E,M", "--domain", "A,O")
        self.assertEqual(q_multi["count"], 3)


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditFixesRegressionTests(RepoTestCase):

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            check=True,
        )

    def test_verify_and_lint_reject_invalid_layer_and_mismatched_size_tier(self):
        self.write("tiny.py", "ONE = 1\n")
        self.init()
        self.assertEqual(self.upsert("tiny.py[CX3T]: F:defines single line constant | R:- | A:ONE | S:-")[0], 0)

        # Hand-edit tiny.py (1 line) to illegal Layer `Z` and 1000+ line Size `H`
        code_path = self.root / ".aoci" / "aoci.code.txt"
        tampered = code_path.read_text(encoding="utf-8").replace("tiny.py[CX3T]:", "tiny.py[ZX3H]:")
        with code_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(tampered)

        ver_code, ver_out = cli("verify", "--root", str(self.root))
        self.assertEqual(ver_code, 1, ver_out)
        self.assertIn("Invalid Layer `Z`", ver_out)
        self.assertIn("Size tier mismatch", ver_out)

        lint_data = cli_json("lint", "--root", str(self.root))
        finding_codes = {(f["path"], f["code"]) for f in lint_data["findings"]}
        self.assertIn(("tiny.py", "layer-invalid"), finding_codes)
        self.assertIn(("tiny.py", "size-mismatch"), finding_codes)
        self.assertEqual(cli("lint", "--root", str(self.root), "--strict")[0], 1)


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

    def test_m2_context_budget_prioritizes_hop_and_importance(self):
        import aoci_mcp

        for rel in ("target.py", "dep_hi.py", "dep_lo.py", "caller_hi.py", "caller_lo.py", "hop2_deep.py"):
            self.write(rel, "x = 1\n")
        self.init()
        entries = [
            "target.py[AX7T]: F:coordinates core target workflow logic | R:dep_hi.py,dep_lo.py | A:run | S:-",
            "dep_hi.py[PX9T]: F:executes critical transactional persistence ledger updates | R:hop2_deep.py | A:commit<-target.py | S:serializable transaction lock required",
            "dep_lo.py[CX2T]: F:formats minor helper display label strings | R:- | A:fmt<-target.py | S:-",
            "caller_hi.py[EX8T]: F:exposes authenticated HTTP gateway API entrypoint | R:target.py | A:api | S:rate limit before auth",
            "caller_lo.py[CX1T]: F:prints local developer debug helper trace | R:target.py | A:dbg | S:-",
            "hop2_deep.py[DX9T]: F:defines deep second hop core schema model | R:- | A:Schema<-dep_hi.py | S:never mutate schema id",
        ]
        self.assertEqual(self.upsert(*entries)[0], 0)

        full = cli_json("context", "--root", str(self.root), "target.py", "--depth", "2")
        self.assertGreater(full["estimated_tokens"], 80)

        # Pick a budget fitting Global-Invariants + target + top 1-hop high-importance items, excluding 2-hop/low-imp
        target_plus_hubs = (
            aoci_index.estimate_tokens("-")
            + aoci_index.estimate_tokens(entries[0])
            + aoci_index.estimate_tokens(entries[1])
            + aoci_index.estimate_tokens(entries[3])
            + 2
        )
        budgeted = cli_json("context", "--root", str(self.root), "target.py", "--depth", "2", "--budget", str(target_plus_hubs))
        self.assertTrue(budgeted["truncated"])
        self.assertLessEqual(budgeted["estimated_tokens"], target_plus_hubs)
        kept = "\n".join(budgeted["targets"] + budgeted["dependencies"] + budgeted["callers"])
        self.assertIn("target.py[", kept)
        self.assertIn("dep_hi.py[", kept)
        self.assertIn("caller_hi.py[", kept)
        self.assertNotIn("hop2_deep.py[", kept)
        self.assertIn("hop2_deep.py", budgeted["omitted_paths"])

        # MCP `aoci_context` produces identical budgeted output
        is_err, mcp_txt = aoci_mcp.call_tool(
            "aoci_context",
            {"root": str(self.root), "paths": ["target.py"], "depth": 2, "budget": target_plus_hubs},
        )
        self.assertFalse(is_err, mcp_txt)
        mcp_obj = json.loads(mcp_txt)
        self.assertEqual(mcp_obj["dependencies"], budgeted["dependencies"])
        self.assertEqual(mcp_obj["callers"], budgeted["callers"])
        self.assertEqual(mcp_obj["omitted_paths"], budgeted["omitted_paths"])

    def test_m2_export_mermaid_and_dot_across_levels_focus_and_escaping(self):
        import aoci_mcp

        self.write("模块/入口.py", "x = 1\n")
        self.write("pages/[id]/view.tsx", "export default function View() { return null; }\n")
        self.write("core/service.py", "y = 2\n")
        self.write("core/isolated.py", "z = 3\n")
        self.init()
        meta_p = self.root / ".aoci" / "aoci.meta.txt"
        meta_p.write_text(meta_p.read_text(encoding="utf-8") + "\n# A=Auth  U=UI\n", encoding="utf-8")
        self.assertEqual(
            self.upsert(
                '模块/入口.py[EA7T]: F:入口服务调用 "核心" 逻辑 | R:core/service.py,pages/[id]/view.tsx | A:start | S:-',
                "pages/[id]/view.tsx[UU6T]: F:渲染动态路由页面组件 | R:core/service.py | A:View | S:-",
                "core/service.py[DX8T]: F:核心领域编排服务 | R:- | A:run | S:idempotent mutations only",
                "core/isolated.py[CX2T]: F:孤立辅助脚本模块 | R:- | A:- | S:-",
            )[0],
            0,
        )

        mmd = cli_json("export", "--root", str(self.root), "--format", "mermaid", "--group-by", "domain")
        self.assertTrue(mmd["content"].startswith("flowchart LR\n"))
        self.assertEqual(mmd["nodes"], 4)
        self.assertEqual(mmd["edges"], 3)
        self.assertIn("模块/入口.py", mmd["content"])
        self.assertIn("pages/[id]/view.tsx", mmd["content"])
        self.assertIn("subgraph sg_", mmd["content"])

        dot = cli_json(
            "export", "--root", str(self.root), "--format", "dot",
            "--level", "dir", "--direction", "TD", "--output", ".aoci/arch.dot",
        )
        self.assertTrue(dot["content"].startswith("digraph aoci {\n"))
        self.assertIn("rankdir=TB;", dot["content"])
        self.assertTrue((self.root / ".aoci" / "arch.dot").is_file())
        self.assertEqual(dot["nodes"], 3)

        dom_mmd = cli_json(
            "export", "--root", str(self.root), "--format", "mermaid",
            "--level", "domain", "--focus", "模块/入口.py", "--depth", "1",
        )
        self.assertEqual(dom_mmd["nodes"], 3)
        self.assertNotIn("core/isolated.py", dom_mmd["content"])

        is_err, mcp_txt = aoci_mcp.call_tool(
            "aoci_export",
            {"root": str(self.root), "format": "mermaid", "level": "file", "focus": "core/service.py"},
        )
        self.assertFalse(is_err, mcp_txt)
        mcp_res = json.loads(mcp_txt)
        self.assertEqual(mcp_res["nodes"], 3)
        self.assertEqual(mcp_res["edges"], 3)
        self.assertNotIn("core/isolated.py", mcp_res["content"])


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_bilingual_glossary_bm25_search_on_builtin_examples_and_mcp(self):
        import aoci_mcp

        ex_aoci = self.root / ".aoci"
        shutil.copytree(SKILL_DIR / "examples", ex_aoci)

        # Chinese query "密钥轮换" hits `src/auth/jwt.py` #1 via bilingual glossary in `examples/aoci.meta.txt`
        q_rot = cli_json("query", "--root", str(self.root), "--search", "密钥轮换")
        self.assertGreaterEqual(q_rot["count"], 1)
        self.assertEqual(q_rot["scores"][0]["path"], "src/auth/jwt.py")
        self.assertIn("rotation", q_rot["scores"][0]["matched_terms"])
        self.assertTrue(any("密钥轮换 -> key rotation" in g for g in q_rot["scores"][0].get("matched_glossary", [])))
        self.assertTrue(q_rot.get("glossary_expansions"))

        # Chinese query "预留库存" hits `src/order/repo.py` via `reserve stock` / `inventory reservation`
        q_inv = cli_json("query", "--root", str(self.root), "--search", "预留库存")
        self.assertGreaterEqual(q_inv["count"], 1)
        self.assertEqual(q_inv["scores"][0]["path"], "src/order/repo.py")
        self.assertIn("reserve", q_inv["scores"][0]["matched_terms"])
        self.assertTrue(any("预留库存 ->" in g for g in q_inv["scores"][0].get("matched_glossary", [])))

        # MCP `aoci_query` uses cached BM25Index + glossary AND invalidates automatically when `.aoci/glossary.txt` changes
        is_err1, res1 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "密钥轮换"})
        self.assertFalse(is_err1, res1)
        d1 = json.loads(res1)
        self.assertEqual(d1["scores"][0]["path"], "src/auth/jwt.py")

        # Add a standalone `.aoci/glossary.txt` entry + a Chinese entry and test English -> Chinese reverse lookup
        (ex_aoci / "aoci.code.txt").write_text(
            (ex_aoci / "aoci.code.txt").read_text(encoding="utf-8")
            + "src/limit.py[MA6T]: F:实现分布式网关限流与令牌桶控制 | R:- | A:allowReq | S:原子Lua脚本\n",
            encoding="utf-8",
        )
        (ex_aoci / "glossary.txt").write_text("# Custom terms\nrate limit = 限流\n", encoding="utf-8")
        is_err2, res2 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "rate limit"})
        self.assertFalse(is_err2, res2)
        d2 = json.loads(res2)
        self.assertGreaterEqual(d2["count"], 1)
        self.assertEqual(d2["scores"][0]["path"], "src/limit.py")
        self.assertTrue(any("rate limit -> 限流" in g for g in d2["scores"][0].get("matched_glossary", [])))


class GlossaryParsingTests(RepoTestCase):
    def test_template_docs_and_directives_are_not_glossary_groups(self) -> None:
        from aoci_core import graph

        self.init()
        aoci = self.root / ".aoci"
        self.assertEqual(graph.parse_glossary(aoci), [], "the template's `Syntax:` row is documentation")
        meta = aoci / "aoci.meta.txt"
        meta.write_text(meta.read_text(encoding="utf-8") + "#Domains: Q=queue\n#Layer-Rules: D!->E\n", encoding="utf-8")
        self.assertEqual(graph.parse_glossary(aoci), [], "directives after the glossary section are not aliases")
        meta.write_text(meta.read_text(encoding="utf-8") + "#Glossary: 队列 = queue\n", encoding="utf-8")
        self.assertEqual(graph.parse_glossary(aoci), [["队列", "queue"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
