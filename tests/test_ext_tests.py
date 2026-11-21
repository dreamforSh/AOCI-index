#!/usr/bin/env python3
"""Tests for AOCI test impact derivation (`--with-tests`, `--tests-for`, and MCP tools)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_aoci import RepoTestCase, aoci_index, cli, cli_json  # noqa: E402


class TestImpactTests(RepoTestCase):
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

    def test_fras_direct_and_transitive_bfs_with_dedup_and_sorting(self):
        for rel in (
            "src/core.py",
            "src/mid.py",
            "src/top.py",
            "src/l4.py",
            "tests/test_direct_low.py",
            "tests/test_direct_high.py",
            "tests/test_mid.py",
            "tests/test_top.py",
            "tests/test_too_far.py",
        ):
            self.write(rel, f"# {rel}\n")
        self.init()
        self.upsert(
            "src/core.py[CA9T]: F:provides core token helper | R:- | A:core_fn<-tests/test_direct_high.py | S:constant time",
            "src/mid.py[CA7T]: F:wraps core helper for sessions | R:src/core.py | A:mid_fn | S:-",
            "src/top.py[EA6T]: F:mounts session API handler | R:src/mid.py | A:top_fn | S:-",
            "src/l4.py[EA5T]: F:exposes fourth-hop adapter | R:src/top.py | A:l4_fn | S:-",
            "tests/test_direct_low.py[TA2T]: F:verifies core token smoke path | R:src/core.py | A:- | S:-",
            "tests/test_direct_high.py[TA9T]: F:verifies core and mid token contract | R:src/mid.py | A:- | S:-",
            "tests/test_mid.py[TA6T]: F:verifies session wrapper behavior | R:src/mid.py | A:- | S:-",
            "tests/test_top.py[TA8T]: F:verifies top session API route | R:src/top.py | A:- | S:-",
            "tests/test_too_far.py[TA9T]: F:verifies fourth-hop adapter flow | R:src/l4.py | A:- | S:-",
        )

        entries, _ = aoci_index.parse_code_volume(self.root / ".aoci" / "aoci.code.txt")
        _, dependents = aoci_index.build_fras_graph(entries)

        impacted = aoci_index.find_impacted_tests(
            self.root, entries, dependents, ["src/core.py"], max_depth=3
        )
        self.assertEqual(
            [item["path"] for item in impacted],
            [
                "tests/test_direct_high.py",
                "tests/test_direct_low.py",
                "tests/test_mid.py",
                "tests/test_top.py",
            ],
        )
        self.assertEqual(
            [
                (item["reason"], item["hop"], item["importance"], item["via"])
                for item in impacted
            ],
            [
                ("direct-fras", 1, 9, "src/core.py"),
                ("direct-fras", 1, 2, "src/core.py"),
                ("transitive-fras", 2, 6, "src/mid.py"),
                ("transitive-fras", 3, 8, "src/top.py"),
            ],
        )
        for item in impacted:
            self.assertEqual(set(item.keys()), {"path", "reason", "hop", "importance", "via", "entry"})
            self.assertEqual(item["entry"], entries[item["path"]])

        impacted_d4 = aoci_index.find_impacted_tests(
            self.root, entries, dependents, ["src/core.py"], max_depth=4
        )
        self.assertEqual(impacted_d4[-1]["path"], "tests/test_too_far.py")
        self.assertEqual(impacted_d4[-1]["reason"], "transitive-fras")
        self.assertEqual(impacted_d4[-1]["hop"], 4)

    def test_warm_outline_cache_static_import_fallback(self):
        self.write(
            "tsconfig.json",
            '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}\n',
        )
        self.write("go.mod", "module example.com/app\n\ngo 1.21\n")
        self.write("src/auth/jwt.py", "def sign():\n    return 'tok'\n")
        self.write("src/other/jwt.py", "def other():\n    return 'no'\n")
        self.write("tests/test_token_flow.py", "from src.auth import jwt\n\ndef test_ok():\n    assert jwt.sign()\n")
        self.write("tests/test_member.py", "from src.auth.jwt import sign\n\ndef test_m():\n    assert sign()\n")
        self.write("tests/test_unrelated.py", "from src.other import jwt\n\ndef test_u():\n    assert jwt.other()\n")
        self.write("src/ui/Button.tsx", "export function Button() { return null; }\n")
        self.write("tests/button_e2e.ts", "import { Button } from '@/ui/Button';\nexport const ok = !!Button;\n")
        self.write("pkg/store/repo.go", "package store\n\nfunc Save() {}\n")
        self.write(
            "integration/store_test.go",
            'package integration\n\nimport "example.com/app/pkg/store"\n\nfunc TestStore() { store.Save() }\n',
        )
        self.write("src/crypto/hash.rs", "pub fn digest() -> u32 { 1 }\n")
        self.write("tests/hash_integ.rs", "use my_crate::crypto::hash;\n\n#[test]\nfn t() { let _ = hash::digest(); }\n")

        self.init()
        # Warm `.aoci/.cache/outline_cache.json` via `scaffold`
        scaf = cli_json("scaffold", "--root", str(self.root), "--unindexed")
        self.assertTrue(scaf["entries"])
        cache_file = self.root / ".aoci" / ".cache" / "outline_cache.json"
        self.assertTrue(cache_file.is_file())

        self.upsert(
            "tsconfig.json[RG2T]: F:configures TypeScript path aliases | R:- | A:- | S:-",
            "go.mod[RG2T]: F:declares Go module path | R:- | A:- | S:-",
            "src/auth/jwt.py[CA7T]: F:signs authentication JWT tokens | R:- | A:sign | S:-",
            "src/other/jwt.py[CX2T]: F:provides unrelated legacy token stub | R:- | A:other | S:-",
            "tests/test_token_flow.py[TA6T]: F:verifies auth token flow via package import | R:- | A:- | S:-",
            "tests/test_member.py[TA5T]: F:verifies auth token sign helper | R:- | A:- | S:-",
            "tests/test_unrelated.py[TX3T]: F:verifies unrelated jwt stub | R:- | A:- | S:-",
            "src/ui/Button.tsx[UA5T]: F:renders primary UI button | R:- | A:Button | S:-",
            "tests/button_e2e.ts[TU5T]: F:verifies UI button alias import | R:- | A:- | S:-",
            "pkg/store/repo.go[PA6T]: F:persists store records | R:- | A:Save | S:-",
            "integration/store_test.go[TP5T]: F:verifies store package integration | R:- | A:- | S:-",
            "src/crypto/hash.rs[CA6T]: F:computes crypto hash digest | R:- | A:digest | S:-",
            "tests/hash_integ.rs[TC5T]: F:verifies Rust hash integration | R:- | A:- | S:-",
        )

        entries, _ = aoci_index.parse_code_volume(self.root / ".aoci" / "aoci.code.txt")
        _, dependents = aoci_index.build_fras_graph(entries)

        # Must read strictly from warm outline_cache.json without invoking extract_file_outline
        with mock.patch.object(aoci_index, "extract_file_outline", side_effect=AssertionError("must not rescan disk")):
            py_hits = aoci_index.find_impacted_tests(self.root, entries, dependents, ["src/auth/jwt.py"])
            self.assertEqual(
                [(h["path"], h["reason"], h["hop"]) for h in py_hits],
                [
                    ("tests/test_token_flow.py", "static-import", 1),
                    ("tests/test_member.py", "static-import", 1),
                ],
            )
            ts_hits = aoci_index.find_impacted_tests(self.root, entries, dependents, ["src/ui/Button.tsx"])
            self.assertEqual([(h["path"], h["reason"]) for h in ts_hits], [("tests/button_e2e.ts", "static-import")])
            go_hits = aoci_index.find_impacted_tests(self.root, entries, dependents, ["pkg/store/repo.go"])
            self.assertEqual(
                [(h["path"], h["reason"]) for h in go_hits],
                [("integration/store_test.go", "static-import")],
            )
            rs_hits = aoci_index.find_impacted_tests(self.root, entries, dependents, ["src/crypto/hash.rs"])
            self.assertEqual([(h["path"], h["reason"]) for h in rs_hits], [("tests/hash_integ.rs", "static-import")])

        # Without warm outline_cache.json, non-naming-convention static fallback returns empty
        cache_file.unlink()
        self.assertEqual(aoci_index.find_impacted_tests(self.root, entries, dependents, ["src/auth/jwt.py"]), [])

    def test_multilanguage_naming_conventions_and_dedup_precedence(self):
        for rel in (
            "src/billing.py",
            "frontend/billing.ts",
            "tests/test_billing.py",
            "tests/billing_test.py",
            "src/cart.ts",
            "tests/cart.test.ts",
            "tests/cart.spec.tsx",
            "pkg/api/handler.go",
            "pkg/api/routing_test.go",
            "pkg/db/db_test.go",
            "src/main/java/com/acme/OrderService.java",
            "src/test/java/com/acme/OrderServiceTest.java",
            "src/test/java/com/acme/OrderServiceSpec.java",
        ):
            self.write(rel, "// stub\n")
        self.init()
        self.upsert(
            "src/billing.py[CA7T]: F:calculates billing invoices | R:- | A:- | S:-",
            "frontend/billing.ts[UA4T]: F:renders frontend billing widget | R:- | A:- | S:-",
            "tests/test_billing.py[TA8T]: F:verifies billing invoice totals | R:- | A:- | S:-",
            "tests/billing_test.py[TA4T]: F:verifies billing edge cases | R:- | A:- | S:-",
            "src/cart.ts[CA6T]: F:manages shopping cart state | R:- | A:- | S:-",
            "tests/cart.test.ts[TA7T]: F:verifies cart item additions | R:- | A:- | S:-",
            "tests/cart.spec.tsx[TA5T]: F:verifies cart spec interactions | R:- | A:- | S:-",
            "pkg/api/handler.go[EA6T]: F:serves API HTTP handlers | R:- | A:- | S:-",
            "pkg/api/routing_test.go[TA6T]: F:verifies API package routing | R:- | A:- | S:-",
            "pkg/db/db_test.go[TP5T]: F:verifies DB package queries | R:- | A:- | S:-",
            "src/main/java/com/acme/OrderService.java[CA7T]: F:processes Java order workflow | R:- | A:- | S:-",
            "src/test/java/com/acme/OrderServiceTest.java[TA7T]: F:verifies Java OrderService | R:- | A:- | S:-",
            "src/test/java/com/acme/OrderServiceSpec.java[TA5T]: F:verifies OrderService spec | R:- | A:- | S:-",
        )

        entries, _ = aoci_index.parse_code_volume(self.root / ".aoci" / "aoci.code.txt")
        _, dependents = aoci_index.build_fras_graph(entries)

        py_hits = aoci_index.find_impacted_tests(self.root, entries, dependents, ["src/billing.py"])
        self.assertEqual(
            [(h["path"], h["reason"], h["importance"]) for h in py_hits],
            [
                ("tests/test_billing.py", "naming-convention", 8),
                ("tests/billing_test.py", "naming-convention", 4),
            ],
        )

        # Cross-language guard: frontend/billing.ts must NOT falsely match Python test_billing.py
        self.assertEqual(aoci_index.find_impacted_tests(self.root, entries, dependents, ["frontend/billing.ts"]), [])

        ts_hits = aoci_index.find_impacted_tests(self.root, entries, dependents, ["src/cart.ts"])
        self.assertEqual(
            [(h["path"], h["reason"]) for h in ts_hits],
            [("tests/cart.test.ts", "naming-convention"), ("tests/cart.spec.tsx", "naming-convention")],
        )

        go_hits = aoci_index.find_impacted_tests(self.root, entries, dependents, ["pkg/api/handler.go"])
        self.assertEqual([(h["path"], h["reason"]) for h in go_hits], [("pkg/api/routing_test.go", "naming-convention")])
        # Go test file itself should not trigger sibling *_test.go via naming convention
        self.assertEqual(aoci_index.find_impacted_tests(self.root, entries, dependents, ["pkg/api/routing_test.go"]), [])

        java_hits = aoci_index.find_impacted_tests(
            self.root, entries, dependents, ["src/main/java/com/acme/OrderService.java"]
        )
        self.assertEqual(
            [(h["path"], h["reason"]) for h in java_hits],
            [
                ("src/test/java/com/acme/OrderServiceTest.java", "naming-convention"),
                ("src/test/java/com/acme/OrderServiceSpec.java", "naming-convention"),
            ],
        )

        # Deduplication precedence: naming-convention < static-import < transitive-fras < direct-fras
        cache_dir = self.root / ".aoci" / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "outline_cache.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "files": {
                        "tests/test_billing.py": {
                            "mtime_ns": 1,
                            "bytes": 10,
                            "outline": {"imports": ["src.billing"], "symbols": []},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        hits_static = aoci_index.find_impacted_tests(self.root, entries, dependents, ["src/billing.py"])
        by_path = {h["path"]: h for h in hits_static}
        self.assertEqual(by_path["tests/test_billing.py"]["reason"], "static-import")
        self.assertEqual(by_path["tests/billing_test.py"]["reason"], "naming-convention")

        # Transitive FRAS overrides static-import & naming-convention
        dependents_trans = {k: set(v) for k, v in dependents.items()}
        dependents_trans.setdefault("src/billing.py", set()).add("src/cart.ts")
        dependents_trans.setdefault("src/cart.ts", set()).add("tests/test_billing.py")
        hits_trans = aoci_index.find_impacted_tests(self.root, entries, dependents_trans, ["src/billing.py"])
        self.assertEqual(
            next(h for h in hits_trans if h["path"] == "tests/test_billing.py")["reason"],
            "transitive-fras",
        )

        # Direct FRAS overrides transitive-fras, static-import, and naming-convention
        dependents_direct = {k: set(v) for k, v in dependents_trans.items()}
        dependents_direct["src/billing.py"].add("tests/test_billing.py")
        hits_direct = aoci_index.find_impacted_tests(self.root, entries, dependents_direct, ["src/billing.py"])
        self.assertEqual(
            next(h for h in hits_direct if h["path"] == "tests/test_billing.py")["reason"],
            "direct-fras",
        )

    def test_cli_context_diff_and_query_flags(self):
        self.write("src/env.py", "SECRET = 'k'\n")
        self.write("src/jwt.py", "from src import env\n")
        self.write("src/legacy.py", "OLD = 1\n")
        self.write("tests/test_jwt.py", "def test_jwt():\n    pass\n")
        self.write("tests/test_env_integ.py", "def test_env_integ():\n    pass\n")
        self.write("tests/test_legacy.py", "def test_legacy():\n    pass\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "initial")

        self.init()
        self.upsert(
            "src/env.py[CG7T]: F:loads signing secret config | R:- | A:SECRET<-src/jwt.py | S:fail fast",
            "src/jwt.py[MA8T]: F:issues signed JWT tokens | R:src/env.py | A:sign | S:alg allowlist",
            "src/legacy.py[CX3T]: F:holds deprecated legacy flag | R:- | A:- | S:-",
            "tests/test_jwt.py[TA8T]: F:verifies JWT signing behavior | R:- | A:- | S:-",
            "tests/test_env_integ.py[TA6T]: F:verifies JWT env integration | R:src/jwt.py | A:- | S:-",
            "tests/test_legacy.py[TX4T]: F:verifies deprecated legacy flag | R:src/legacy.py | A:- | S:-",
        )

        # Default calls without flags must NOT emit `impacted_tests`
        ctx_plain = cli_json("context", "--root", str(self.root), "src/env.py")
        self.assertNotIn("impacted_tests", ctx_plain)
        diff_plain = cli_json("diff", "--root", str(self.root))
        self.assertNotIn("impacted_tests", diff_plain)
        q_plain = cli_json("query", "--root", str(self.root), "--impact", "src/env.py")
        self.assertNotIn("impacted_tests", q_plain)

        # `context --with-tests` (JSON + text)
        ctx_tests = cli_json("context", "--root", str(self.root), "src/env.py", "--with-tests")
        self.assertEqual(
            [(t["path"], t["reason"], t["hop"]) for t in ctx_tests["impacted_tests"]],
            [("tests/test_env_integ.py", "transitive-fras", 2)],
        )
        rc, ctx_txt = cli("context", "--root", str(self.root), "src/env.py", "--with-tests")
        self.assertEqual(rc, 0, ctx_txt)
        self.assertIn("## Impacted Tests (1)", ctx_txt)
        self.assertIn("tests/test_env_integ.py", ctx_txt)

        # `query --tests-for` (JSON + text + filter)
        q_tests = cli_json("query", "--root", str(self.root), "--tests-for", "src/jwt.py")
        self.assertEqual(q_tests["count"], 2)
        self.assertEqual(
            [(t["path"], t["reason"]) for t in q_tests["impacted_tests"]],
            [
                ("tests/test_env_integ.py", "direct-fras"),
                ("tests/test_jwt.py", "naming-convention"),
            ],
        )
        self.assertEqual(
            [ln.split("[", 1)[0] for ln in q_tests["entries"]],
            ["tests/test_env_integ.py", "tests/test_jwt.py"],
        )
        q_top = cli_json("query", "--root", str(self.root), "--tests-for", "src/jwt.py", "--min-importance", "8")
        self.assertEqual(q_top["count"], 1)
        self.assertEqual([t["path"] for t in q_top["impacted_tests"]], ["tests/test_jwt.py"])

        rc, q_txt = cli("query", "--root", str(self.root), "--tests-for", "src/jwt.py")
        self.assertEqual(rc, 0, q_txt)
        self.assertIn("# Query matches: 2 / 6", q_txt)
        self.assertIn("test-impact: reason=direct-fras", q_txt)

        # `diff --with-tests` across touched + deleted indexed paths (JSON + text)
        self.write("src/jwt.py", "from src import env\nSKEW = 10\n")
        (self.root / "src" / "legacy.py").unlink()
        diff_tests = cli_json("diff", "--root", str(self.root), "--with-tests")
        self.assertEqual(
            [(t["path"], t["reason"]) for t in diff_tests["impacted_tests"]],
            [
                ("tests/test_env_integ.py", "direct-fras"),
                ("tests/test_legacy.py", "direct-fras"),
                ("tests/test_jwt.py", "naming-convention"),
            ],
        )
        rc, diff_txt = cli("diff", "--root", str(self.root), "--with-tests")
        self.assertEqual(rc, 0, diff_txt)
        self.assertIn("## Impacted Tests (3)", diff_txt)
        self.assertIn("tests/test_legacy.py", diff_txt)

        # `diff <rev> --with-tests` on committed history and `query --tests-for --search`
        self._git("add", "src/jwt.py")
        self._git("commit", "-q", "-m", "modify jwt")
        rev_diff = cli_json("diff", "HEAD~1..HEAD", "--root", str(self.root), "--with-tests")
        self.assertEqual(
            [(t["path"], t["reason"]) for t in rev_diff["impacted_tests"]],
            [
                ("tests/test_env_integ.py", "direct-fras"),
                ("tests/test_jwt.py", "naming-convention"),
            ],
        )

        q_search = cli_json(
            "query", "--root", str(self.root), "--tests-for", "src/jwt.py", "--search", "signing behavior"
        )
        self.assertEqual(q_search["count"], 1)
        self.assertEqual([t["path"] for t in q_search["impacted_tests"]], ["tests/test_jwt.py"])
        self.assertEqual(q_search["scores"][0]["path"], "tests/test_jwt.py")

    def test_mcp_with_tests_and_tests_for_and_cache_hit(self):
        import aoci_mcp

        self.write("src/env.py", "SECRET = 'k'\n")
        self.write("src/jwt.py", "from src import env\n")
        self.write("tests/test_jwt.py", "def test_jwt():\n    pass\n")
        self.write("tests/test_env_integ.py", "def test_env_integ():\n    pass\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "initial")

        self.init()
        self.upsert(
            "src/env.py[CG7T]: F:loads signing secret config | R:- | A:SECRET<-src/jwt.py | S:fail fast",
            "src/jwt.py[MA8T]: F:issues signed JWT tokens | R:src/env.py | A:sign | S:alg allowlist",
            "tests/test_jwt.py[TA8T]: F:verifies JWT signing behavior | R:- | A:- | S:-",
            "tests/test_env_integ.py[TA6T]: F:verifies JWT env integration | R:src/jwt.py | A:- | S:-",
        )

        # Schema declares new optional parameters
        list_resp = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in list_resp["result"]["tools"]}
        self.assertIn("tests_for", tools["aoci_query"]["inputSchema"]["properties"])
        self.assertIn("with_tests", tools["aoci_context"]["inputSchema"]["properties"])
        self.assertIn("with_tests", tools["aoci_diff"]["inputSchema"]["properties"])

        # Warm `_CACHE` once
        ov = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "aoci_overview", "arguments": {"root": str(self.root)}}}
        )
        self.assertFalse(ov["result"]["isError"])

        self.write("src/jwt.py", "from src import env\nSKEW = 30\n")

        with mock.patch.object(aoci_index, "parse_code_volume", side_effect=AssertionError("must hit _CACHE")):
            r_ctx_plain = aoci_mcp.handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "aoci_context", "arguments": {"root": str(self.root), "paths": ["src/env.py"]}},
                }
            )
            self.assertNotIn("impacted_tests", json.loads(r_ctx_plain["result"]["content"][0]["text"]))

            r_ctx = aoci_mcp.handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "aoci_context",
                        "arguments": {"root": str(self.root), "paths": ["src/env.py"], "with_tests": True},
                    },
                }
            )
            ctx_payload = json.loads(r_ctx["result"]["content"][0]["text"])
            self.assertEqual(
                [(t["path"], t["reason"]) for t in ctx_payload["impacted_tests"]],
                [("tests/test_env_integ.py", "transitive-fras")],
            )

            r_q = aoci_mcp.handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "aoci_query",
                        "arguments": {"root": str(self.root), "tests_for": "src/jwt.py"},
                    },
                }
            )
            q_payload = json.loads(r_q["result"]["content"][0]["text"])
            self.assertEqual(q_payload["count"], 2)
            self.assertEqual(
                [(t["path"], t["reason"]) for t in q_payload["impacted_tests"]],
                [("tests/test_env_integ.py", "direct-fras"), ("tests/test_jwt.py", "naming-convention")],
            )

            r_qs = aoci_mcp.handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "aoci_query",
                        "arguments": {
                            "root": str(self.root),
                            "tests_for": "src/jwt.py",
                            "search": "signing behavior",
                        },
                    },
                }
            )
            qs_payload = json.loads(r_qs["result"]["content"][0]["text"])
            self.assertEqual(qs_payload["scores"][0]["path"], "tests/test_jwt.py")
            self.assertEqual(qs_payload["count"], 1)

            r_diff_plain = aoci_mcp.handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "aoci_diff", "arguments": {"root": str(self.root)}},
                }
            )
            self.assertNotIn("impacted_tests", json.loads(r_diff_plain["result"]["content"][0]["text"]))

            r_diff = aoci_mcp.handle_rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "aoci_diff", "arguments": {"root": str(self.root), "with_tests": True}},
                }
            )
            diff_payload = json.loads(r_diff["result"]["content"][0]["text"])
            self.assertEqual(
                [(t["path"], t["reason"]) for t in diff_payload["impacted_tests"]],
                [("tests/test_env_integ.py", "direct-fras"), ("tests/test_jwt.py", "naming-convention")],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
