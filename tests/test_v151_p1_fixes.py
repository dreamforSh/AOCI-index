#!/usr/bin/env python3
"""v1.5.1 P1 regressions: co-read cycles, MCP structuredContent, `#Domains:` directive, VS Code global MCP path."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from aoci_testkit import RepoTestCase, aoci_index  # noqa: E402
import aoci_mcp  # noqa: E402
from aoci_core import graph, installer, templates  # noqa: E402


def _tools_call(name: str, arguments: dict) -> dict:
    resp = aoci_mcp.handle_rpc_request(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    return resp["result"]


class CoReadCycleTests(RepoTestCase):
    def _index(self) -> None:
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.write("setup.py", "from setuptools import setup\nsetup()\n")
        self.write("README.md", "# x\n")
        self.write("SKILL.md", "# skill\n")
        self.write("cfg.toml", "a = 1\n")
        self.write("core/a.py", "X = 1\n")
        self.write("core/b.py", "Y = 2\n")
        self.write("core/c.py", "Z = 3\n")
        self.init()
        code, out = self.upsert(
            "pyproject.toml[RG3T]: F:declares package metadata | R:setup.py | A:- | S:-",
            "setup.py[RG3T]: F:builds wheel bundle | R:pyproject.toml | A:- | S:-",
            "README.md[OG3T]: F:documents usage | R:SKILL.md | A:- | S:-",
            "SKILL.md[OG3T]: F:defines agent workflow | R:README.md | A:- | S:-",
            "cfg.toml[RG3T]: F:runtime config read by c | R:core/c.py | A:- | S:-",
            "core/a.py[KG3T]: F:computes X | R:core/b.py | A:- | S:-",
            "core/b.py[KG3T]: F:computes Y | R:core/a.py | A:- | S:-",
            "core/c.py[KG3T]: F:computes Z from config | R:cfg.toml | A:- | S:-",
        )
        self.assertEqual(code, 0, out)

    def test_runtime_and_docs_co_read_pairs_are_not_cycles_but_code_cycles_are(self):
        self._index()
        res = aoci_index.audit_architecture(self.root, check_static=False, check_layers=False)
        cycle_nodes = sorted(c["nodes"] for c in res["cycles"])
        # R<->R and O<->O co-reads ignored; K<->K import cycle and mixed R<->K cycle still reported
        self.assertEqual(cycle_nodes, [["cfg.toml", "core/c.py"], ["core/a.py", "core/b.py"]])

    def test_code_dependency_view_only_drops_edges_between_two_non_code_files(self):
        entries = {
            "a.toml": "a.toml[RG3T]: F:config a | R:b.toml,k.py | A:- | S:-",
            "b.toml": "b.toml[RG3T]: F:config b | R:a.toml | A:- | S:-",
            "k.py": "k.py[KG3T]: F:kernel | R:a.toml | A:- | S:-",
        }
        parsed = graph.parse_entries_map(entries)
        deps, _ = graph.build_fras_graph(entries, parsed_entries=parsed)
        view = graph.code_dependency_view(deps, parsed)
        self.assertEqual(view["a.toml"], {"k.py"})
        self.assertEqual(view["b.toml"], set())
        self.assertEqual(view["k.py"], {"a.toml"})
        self.assertEqual(deps["a.toml"], {"b.toml", "k.py"})  # original graph untouched


class McpStructuredContentTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        aoci_mcp._CACHE.invalidate(self.root)

    def test_ci_check_non_json_formats_still_return_structured_report(self):
        self.write("routes/http.py", "from domain.order import Order\n")
        self.write("domain/order.py", "class Order:\n    pass\n")
        for fmt in ("markdown", "github", "sarif"):
            with self.subTest(fmt=fmt):
                res = _tools_call("aoci_ci_check", {"root": str(self.root), "format": fmt})
                self.assertFalse(res["isError"], res)
                sc = res.get("structuredContent")
                self.assertIsInstance(sc, dict, res)
                self.assertIn("summary", sc)
                self.assertIn("ok", sc)
        sarif = _tools_call("aoci_ci_check", {"root": str(self.root), "format": "sarif"})
        self.assertEqual(json.loads(sarif["content"][0]["text"])["version"], "2.1.0")  # text channel unchanged

    def test_batch_pack_markdown_returns_structured_pack(self):
        self.write("pkg/alpha.py", "def alpha():\n    return 1\n")
        self.init()
        res = _tools_call("aoci_batch_pack", {"root": str(self.root), "batch": 1, "format": "markdown"})
        self.assertFalse(res["isError"], res)
        self.assertEqual(res["structuredContent"]["batch"], 1)
        self.assertIn("files", res["structuredContent"])

    def test_every_successful_output_schema_tool_returns_structured_content(self):
        # Any outputSchema tool whose success text is not a JSON object still yields a schema-valid object
        with mock.patch.object(aoci_mcp, "call_tool", return_value=(False, "flowchart LR\n  a --> b\n")):
            res = _tools_call("aoci_export", {"root": str(self.root), "format": "mermaid"})
        self.assertFalse(res["isError"], res)
        self.assertEqual(res["structuredContent"], {"text": "flowchart LR\n  a --> b\n"})
        # Errors never carry structuredContent; tools without outputSchema are untouched
        with mock.patch.object(aoci_mcp, "call_tool", return_value=(True, "boom")):
            self.assertNotIn("structuredContent", _tools_call("aoci_export", {"root": str(self.root)}))
        with mock.patch.dict(aoci_mcp._TOOL_BY_NAME, {"aoci_custom_untyped": {"name": "aoci_custom_untyped"}}):
            with mock.patch.object(aoci_mcp, "call_tool", return_value=(False, "done")):
                self.assertNotIn("structuredContent", _tools_call("aoci_custom_untyped", {"root": str(self.root)}))

    def test_root_hints_and_subscriptions_are_lock_guarded_under_concurrency(self):
        aoci_mcp._SUBSCRIBED_URIS.clear()
        try:
            uris = [f"aoci://code?root={self.root}&n={i}" for i in range(200)]

            def sub(u: str) -> None:
                aoci_mcp.handle_rpc_request(
                    {"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe", "params": {"uri": u}}
                )

            threads = [threading.Thread(target=sub, args=(u,)) for u in uris]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(aoci_mcp._SUBSCRIBED_URIS), 200)
            self.assertTrue(isinstance(aoci_mcp._STATE_LOCK, type(threading.RLock())))
        finally:
            aoci_mcp._SUBSCRIBED_URIS.clear()


class DomainsDirectiveTests(RepoTestCase):
    def test_domains_directive_in_meta_and_manifest_is_honored(self):
        self.write("gov/policy.py", "P = 1\n")
        self.write("kern/core.py", "K = 1\n")
        self.init()
        aoci_dir = self.root / ".aoci"
        meta = aoci_dir / "aoci.meta.txt"
        meta.write_text(meta.read_text(encoding="utf-8") + "\n#Domains: G=governance, Q=quality\n", encoding="utf-8")
        manifest = aoci_dir / "aoci.txt"
        manifest.write_text(manifest.read_text(encoding="utf-8") + "#Domain: K=kernel\n", encoding="utf-8")

        domains = templates.parse_declared_domains(aoci_dir)
        self.assertEqual(domains.get("G"), "governance")  # directive overrides section `G=Global`
        self.assertEqual(domains.get("Q"), "quality")
        self.assertEqual(domains.get("K"), "kernel")
        self.assertEqual(domains.get("X"), "Core")  # section-declared codes still present
        self.assertNotIn("A", domains)  # `(Example: A=Auth ...)` stays ignored

        code, out = self.upsert(
            "gov/policy.py[CQ3T]: F:defines quality policy constant | R:- | A:P | S:-",
            "kern/core.py[KK3T]: F:defines kernel constant | R:- | A:K | S:-",
        )
        self.assertEqual(code, 0, out)


class VsCodeGlobalMcpPathTests(unittest.TestCase):
    def test_platform_specific_user_dirs(self):
        home = Path("/h")
        self.assertEqual(
            installer.vscode_user_config_dir(home, platform="win32", honor_env=False),
            home / "AppData" / "Roaming" / "Code" / "User",
        )
        self.assertEqual(
            installer.vscode_user_config_dir(home, platform="darwin", honor_env=False),
            home / "Library" / "Application Support" / "Code" / "User",
        )
        self.assertEqual(
            installer.vscode_user_config_dir(home, platform="linux", honor_env=False),
            home / ".config" / "Code" / "User",
        )
        with mock.patch.dict(os.environ, {"APPDATA": str(Path("/roam")), "XDG_CONFIG_HOME": str(Path("/xdg"))}):
            self.assertEqual(
                installer.vscode_user_config_dir(home, platform="win32", honor_env=True),
                Path("/roam") / "Code" / "User",
            )
            self.assertEqual(
                installer.vscode_user_config_dir(home, platform="linux", honor_env=True),
                Path("/xdg") / "Code" / "User",
            )

    def test_global_install_writes_user_dir_and_migrates_legacy_vscode_file(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            legacy = home / ".vscode" / "mcp.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                json.dumps({"servers": {"aoci-index": {"type": "stdio", "command": "py"}, "other": {"type": "stdio"}}}),
                encoding="utf-8",
            )
            out = io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", out):
                rc = installer.main(["--host", "vscode", "--scope", "global", "--home-dir", td, "--with-mcp"])
            self.assertEqual(rc, 0, out.getvalue())
            user_mcp = installer.vscode_user_config_dir(home) / "mcp.json"
            self.assertTrue(str(user_mcp).startswith(td))
            self.assertEqual(json.loads(user_mcp.read_text(encoding="utf-8"))["servers"]["aoci-index"]["type"], "stdio")
            legacy_servers = json.loads(legacy.read_text(encoding="utf-8"))["servers"]
            self.assertNotIn("aoci-index", legacy_servers)
            self.assertIn("other", legacy_servers)

            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", out):
                rc_u = installer.main(["--host", "vscode", "--scope", "global", "--home-dir", td, "--uninstall"])
            self.assertEqual(rc_u, 0, out.getvalue())
            self.assertFalse(user_mcp.exists())
            self.assertTrue(user_mcp.parent.is_dir())  # never prune the VS Code user settings directory


if __name__ == "__main__":
    unittest.main()
