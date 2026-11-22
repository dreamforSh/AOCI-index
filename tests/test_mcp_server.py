#!/usr/bin/env python3
"""MCP stdio server: framing, isolation, caches, and resource templates."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    INSTALL,
    RepoTestCase,
    aoci_index,
    cli,
)
import aoci_mcp  # noqa: E402


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase3FeatureTests(RepoTestCase):

    def _git(self, *args: str, cwd: Path = None) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd or self.root,
            check=True,
        )

    def test_mcp_stdio_server_and_plugin_bundle_install(self):
        import aoci_mcp

        self.write("src/app.py", "def serve():\n    return 200\n")
        self.init()
        self.upsert("src/app.py[EA7T]: F:serves primary HTTP application | R:- | A:serve | S:bind localhost only")

        # 1) Line-delimited JSON-RPC 2.0 session
        reqs = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "aoci_query", "arguments": {"root": str(self.root), "search": "HTTP serve"}},
                }
            ),
        ]
        in_buf = io.BytesIO(("\n".join(reqs) + "\n").encode("utf-8"))
        out_buf = io.BytesIO()
        aoci_mcp.serve_stdio(in_buf, out_buf)
        resps = [json.loads(ln.decode("utf-8")) for ln in out_buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual([r["id"] for r in resps], [1, 2, 3])
        self.assertGreaterEqual(len(resps[1]["result"]["tools"]), 9)
        q_payload = json.loads(resps[2]["result"]["content"][0]["text"])
        self.assertEqual(q_payload["scores"][0]["path"], "src/app.py")

        # 2) Content-Length framed JSON-RPC call + in-memory cache hit (zero parse_code_volume calls!)
        cl_body = json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "aoci_overview", "arguments": {"root": str(self.root)}}}
        ).encode("utf-8")
        cl_in = io.BytesIO(f"Content-Length: {len(cl_body)}\r\n\r\n".encode("ascii") + cl_body)
        cl_out = io.BytesIO()
        with mock.patch.object(aoci_index, "parse_code_volume", side_effect=AssertionError("must hit _CACHE")):
            aoci_mcp.serve_stdio(cl_in, cl_out)
        raw_reply = cl_out.getvalue()
        self.assertTrue(raw_reply.startswith(b"Content-Length:"))
        ov_resp = json.loads(raw_reply.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
        self.assertFalse(ov_resp["result"]["isError"])

        # 3) Native Antigravity `--as-plugin` bundle + `--with-mcp` install & clean uninstall
        home = Path(self._tmp.name) / "plugin_home"
        r_ins = subprocess.run(
            [
                sys.executable, str(INSTALL),
                "--host", "antigravity", "--scope", "global", "--home-dir", str(home),
                "--as-plugin", "--with-hooks", "--with-rules", "--with-mcp",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_ins.returncode, 0, r_ins.stdout + r_ins.stderr)
        pdir = home / ".gemini" / "config" / "plugins" / "aoci-index"
        self.assertTrue((pdir / "plugin.json").is_file())
        self.assertTrue((pdir / "assets" / "logo.svg").is_file())
        self.assertTrue((pdir / "hooks.json").is_file())
        self.assertTrue((pdir / "mcp_config.json").is_file())
        self.assertTrue((pdir / "rules" / "AGENTS.md").is_file())
        self.assertTrue((pdir / "skills" / "aoci-index" / "SKILL.md").is_file())

        r_un = subprocess.run(
            [
                sys.executable, str(INSTALL),
                "--host", "antigravity", "--scope", "global", "--home-dir", str(home),
                "--as-plugin", "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
        self.assertFalse(pdir.exists())


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase1FixesTests(RepoTestCase):

    def _git_at(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd,
            check=True,
        )

    def test_mcp_system_exit_isolation_dash_inputs_headers_and_14_tools(self):
        import aoci_mcp

        # 1) initialize with file:// rootUri + verify all 14 tools mark `root` required
        init_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "rootUri": self.root.resolve().as_uri()},
            }
        )
        self.assertEqual(init_resp["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("resources", init_resp["result"]["capabilities"])
        self.assertIn("prompts", init_resp["result"]["capabilities"])

        list_resp = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in list_resp["result"]["tools"]}
        self.assertEqual(
            set(tools.keys()),
            {
                "aoci_init",
                "aoci_overview",
                "aoci_query",
                "aoci_context",
                "aoci_diff",
                "aoci_scan",
                "aoci_outline",
                "aoci_scaffold",
                "aoci_ack",
                "aoci_upsert",
                "aoci_migrate_renames",
                "aoci_restore",
                "aoci_shard",
                "aoci_ensure_git_exclude",
                "aoci_lint",
                "aoci_verify",
                "aoci_audit",
                "aoci_export",
                "aoci_pool",
                "aoci_pack",
                "aoci_zoom",
                "aoci_prepare_sync",
                "aoci_apply_sync",
                "aoci_symbol",
                "aoci_install_git_hooks",
                "aoci_batch_pack",
                "aoci_ci_check",
                "aoci_refs",
                "aoci_doctor",
            },
        )
        for t_name, t_spec in tools.items():
            self.assertIn("root", t_spec["inputSchema"].get("required", []), f"{t_name} must require root")
            self.assertIn("annotations", t_spec, f"{t_name} must define annotations")
            self.assertIn("readOnlyHint", t_spec["annotations"])
        self.assertTrue(tools["aoci_query"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["aoci_upsert"]["annotations"]["readOnlyHint"])

        res_list = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 21, "method": "resources/list"})
        res_uris = {r["uri"] for r in res_list["result"]["resources"]}
        self.assertEqual(res_uris, {"aoci://manifest", "aoci://meta", "aoci://code", "aoci://hubs", "aoci://shards/INDEX"})

        prm_list = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 22, "method": "prompts/list"})
        prm_names = {p["name"] for p in prm_list["result"]["prompts"]}
        self.assertEqual(prm_names, {"aoci-bootstrap", "aoci-review-diff", "aoci-sync"})
        prm_get = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 23, "method": "prompts/get", "params": {"name": "aoci-sync", "arguments": {"root": str(self.root)}}}
        )
        self.assertIn("aoci_scan", prm_get["result"]["messages"][0]["content"]["text"])

        # 2) `aoci_init` via MCP using rootUri fallback when `root` omitted in direct RPC call
        self.write("-dash.py", "def handle():\n    return 1\n\n\nVAL = 2\n")
        r_init = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "aoci_init", "arguments": {}}}
        )
        self.assertFalse(r_init["result"]["isError"])

        res_read = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 24, "method": "resources/read", "params": {"uri": f"aoci://manifest?root={self.root}"}}
        )
        self.assertIn("#AOCI-ROOT-MANIFEST:", res_read["result"]["contents"][0]["text"])

        # 3) `--bad-flag` entry MUST NOT crash MCP server with SystemExit(2); returns isError=True cleanly
        r_bad = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "aoci_upsert", "arguments": {"root": str(self.root), "entries": ["--bad-flag"]}},
            }
        )
        self.assertTrue(r_bad["result"]["isError"])
        self.assertIn("Syntax mismatch", r_bad["result"]["content"][0]["text"])

        # 4) Dash-prefixed filename works across aoci_outline, aoci_upsert, aoci_ack, aoci_lint, aoci_migrate_renames
        r_out = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "aoci_outline", "arguments": {"root": str(self.root), "paths": ["-dash.py"]}},
            }
        )
        self.assertFalse(r_out["result"]["isError"], r_out["result"]["content"][0]["text"])
        r_up = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "aoci_upsert",
                    "arguments": {
                        "root": str(self.root),
                        "entries": ["-dash.py[AX3T]: F:handles dash prefixed module | R:- | A:handle | S:-"],
                    },
                },
            }
        )
        self.assertFalse(r_up["result"]["isError"], r_up["result"]["content"][0]["text"])
        self.write("-dash.py", "def handle():\n    return 2\n\n\nVAL = 3\n")
        r_ack = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "aoci_ack", "arguments": {"root": str(self.root), "paths": ["-dash.py"]}},
            }
        )
        self.assertFalse(r_ack["result"]["isError"], r_ack["result"]["content"][0]["text"])
        (self.root / "-dash.py").rename(self.root / "renamed_dash.py")
        r_mig = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "aoci_migrate_renames", "arguments": {"root": str(self.root)}},
            }
        )
        self.assertFalse(r_mig["result"]["isError"], r_mig["result"]["content"][0]["text"])
        r_lint = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "aoci_lint", "arguments": {"root": str(self.root), "strict": True}},
            }
        )
        self.assertFalse(r_lint["result"]["isError"], r_lint["result"]["content"][0]["text"])

        # 5) Content-Type BEFORE Content-Length header framing in serve_stdio
        body = json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"}).encode("utf-8")
        framed = (
            b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        out_buf = io.BytesIO()
        aoci_mcp.serve_stdio(io.BytesIO(framed), out_buf)
        self.assertIn(b'"id":10', out_buf.getvalue())


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditV2RegressionTests(RepoTestCase):

    def test_perf_mcp_bm25_cache_diff_cache_and_write_json_flags(self):
        import aoci_mcp

        self.write("src/db.py", "def query():\n    return []\n")
        self.write("src/api.py", "from src import db\ndef endpoint():\n    return db.query()\n")
        subprocess.run(["git", "add", "src/db.py", "src/api.py"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"],
            cwd=self.root, check=True,
        )
        self.init()

        up_code, up_out = cli(
            "upsert", "--root", str(self.root), "--json",
            "--entry", "src/db.py[PX8T]: F:executes parameterized SQL database queries | R:- | A:query<-src/api.py | S:parameterized SQL only",
            "--entry", "src/api.py[EX7T]: F:exposes JSON HTTP endpoint | R:src/db.py | A:endpoint | S:-",
        )
        self.assertEqual(up_code, 0, up_out)
        self.assertTrue(json.loads(up_out)["ok"])

        self.write("src/api.py", "from src import db\ndef endpoint():\n    return db.query() or []\n")
        ack_code, ack_out = cli("ack", "--root", str(self.root), "--json", "src/api.py")
        self.assertEqual(ack_code, 0, ack_out)
        self.assertEqual(json.loads(ack_out)["acked"], ["src/api.py"])

        # 1st MCP query builds BM25Index; 2nd query reuses cached BM25Index without re-tokenizing
        is_err1, txt1 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "parameterized SQL"})
        self.assertFalse(is_err1, txt1)
        self.assertEqual(json.loads(txt1)["scores"][0]["path"], "src/db.py")

        with mock.patch.object(aoci_index, "BM25Index", side_effect=AssertionError("must reuse cached BM25Index")):
            is_err2, txt2 = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "HTTP endpoint"})
        self.assertFalse(is_err2, txt2)
        self.assertEqual(json.loads(txt2)["scores"][0]["path"], "src/api.py")

        # MCP `aoci_diff` hits _CACHE and includes 1-hop `dependency_constraints` from R:src/db.py
        with mock.patch.object(aoci_index, "parse_code_volume", side_effect=AssertionError("must hit _CACHE")):
            is_err3, txt3 = aoci_mcp.call_tool("aoci_diff", {"root": str(self.root)})
        self.assertFalse(is_err3, txt3)
        diff_json = json.loads(txt3)
        self.assertEqual([c["path"] for c in diff_json["dependency_constraints"]], ["src/db.py"])


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_mcp_resource_templates_list_and_thread_safe_cli_capture(self):
        import concurrent.futures
        import aoci_mcp

        self.write("app.py", "def run():\n    return 1\n")
        self.init()
        self.upsert("app.py[CX2T]: F:runs application entrypoint | R:- | A:run | S:-")

        in_buf = io.BytesIO((json.dumps({"jsonrpc": "2.0", "id": 11, "method": "resources/templates/list"}) + "\n").encode("utf-8"))
        out_buf = io.BytesIO()
        aoci_mcp.serve_stdio(in_buf, out_buf)
        resp = json.loads(out_buf.getvalue().decode("utf-8"))
        self.assertEqual(resp["id"], 11)
        templates = resp["result"]["resourceTemplates"]
        uris = {t["uriTemplate"] for t in templates}
        self.assertIn("aoci://shards/{shard}", uris)
        self.assertIn("aoci://{volume}?root={root}", uris)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(aoci_mcp._run_cli_capture, ["verify", "--root", str(self.root), "--json"]) for _ in range(4)]
            for fut in futs:
                rc, out = fut.result()
                self.assertEqual(rc, 0, out)
                self.assertTrue(json.loads(out)["ok"])


class McpGuardsProfilesAndRegistryTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("src/a.py", "def a():\n    return 1\n")
        self.init()
        code, out = self.upsert("src/a.py[CX3T]: F:returns the constant one | R:- | A:a | S:-")
        self.assertEqual(code, 0, out)

    def _call(self, tool: str, **args: object):
        return aoci_mcp.call_tool(tool, {"root": str(self.root), **args})

    def test_write_targets_stay_inside_root_and_out_of_dot_git(self) -> None:
        outside = self.root.parent / "escape.mmd"
        is_err, text = self._call("aoci_export", format="mermaid", output=str(outside))
        self.assertTrue(is_err)
        self.assertIn("must stay inside the repository root", text)
        self.assertFalse(outside.exists())
        is_err, text = self._call("aoci_export", format="mermaid", output=".git/hooks/pre-commit")
        self.assertTrue(is_err)
        self.assertIn("must not point inside `.git/`", text)
        is_err, text = self._call("aoci_export", format="mermaid", output="docs/graph.mmd")
        self.assertFalse(is_err, text)
        self.assertTrue((self.root / "docs" / "graph.mmd").is_file())
        with mock.patch.dict(os.environ, {"AOCI_MCP_ALLOW_OUTSIDE_ROOT": "1"}):
            is_err, text = self._call("aoci_export", format="mermaid", output=str(outside))
            self.assertFalse(is_err, text)
            is_err, text = self._call("aoci_export", format="mermaid", output=".git/x.mmd")
            self.assertTrue(is_err, "the `.git/` ban has no override")
        # Reads and non-deleting pending dirs are not write targets
        is_err, text = self._call("aoci_pool", action="import", file=str(self.root.parent / "missing.json"))
        self.assertNotIn("must stay inside", text)
        is_err, text = self._call("aoci_upsert", pending_dir=str(self.root.parent), clean_pending=True)
        self.assertTrue(is_err)
        self.assertIn("pending_dir", text)

    def test_profiles_trim_tools_list_and_block_disabled_calls(self) -> None:
        try:
            aoci_mcp.set_tool_profile("core")
            listed = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            names = [t["name"] for t in listed["result"]["tools"]]
            self.assertEqual(sorted(names), sorted(aoci_mcp.TOOL_PROFILES["core"]))
            blocked = aoci_mcp.handle_rpc_request(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "aoci_upsert", "arguments": {}}}
            )
            self.assertTrue(blocked["result"]["isError"])
            self.assertIn("not enabled in the `core` profile", blocked["result"]["content"][0]["text"])
            self.assertEqual(aoci_mcp.set_tool_profile("bogus"), "full")
        finally:
            aoci_mcp.set_tool_profile("full")
        with mock.patch.object(aoci_mcp, "serve_stdio", return_value=0) as serve:
            try:
                self.assertEqual(aoci_mcp.main(["--profile", "sync"]), 0)
                self.assertEqual(aoci_mcp._TOOL_PROFILE, "sync")
            finally:
                aoci_mcp.set_tool_profile("full")
            serve.assert_called_once()

    def test_new_client_roots_replace_a_stale_default_root(self) -> None:
        other = self.root.parent / "other-workspace"
        other.mkdir()
        try:
            for root in (self.root, other):  # `other` has no index yet: it must still become the default
                aoci_mcp.handle_rpc_request(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"rootUri": root.resolve().as_uri()}}
                )
                self.assertEqual(Path(aoci_mcp._CLIENT_ROOT_HINT).resolve(), root.resolve())
            aoci_mcp.update_client_roots([{"uri": self.root.resolve().as_uri()}])
            self.assertEqual(Path(aoci_mcp._CLIENT_ROOT_HINT).resolve(), self.root.resolve())
        finally:
            with aoci_mcp._STATE_LOCK:
                aoci_mcp._CLIENT_ROOT_HINTS.clear()
                aoci_mcp._CLIENT_ROOT_HINT = None

    def test_registry_covers_every_advertised_tool(self) -> None:
        spec_names = {t["name"] for t in aoci_mcp.TOOLS_SPEC}
        self.assertEqual(set(aoci_mcp.TOOL_HANDLERS), spec_names)
        for profile, allowed in aoci_mcp.TOOL_PROFILES.items():
            if allowed is not None:
                self.assertLessEqual(set(allowed), spec_names, profile)
        is_err, text = aoci_mcp.call_tool("aoci_nope", {"root": str(self.root)})
        self.assertTrue(is_err)
        self.assertIn("Unknown tool", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
