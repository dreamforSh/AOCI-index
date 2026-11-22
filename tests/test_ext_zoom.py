#!/usr/bin/env python3
"""Regression tests for H-tier symbol sub-volume (`aoci.hubs.txt`, `aoci zoom`, and MCP `aoci_zoom`)."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
for _p in (str(TESTS_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aoci_core import hubs, lock  # noqa: E402
from aoci_core.constants import AOCI_DIR_NAME, HUBS_FILE  # noqa: E402
from aoci_testkit import RepoTestCase, cli, cli_json  # noqa: E402


class HubZoomTests(RepoTestCase):
    def test_python_span_extraction_and_py38_fallback(self):
        src = (
            "import os\n"
            "\n"
            "\n"
            "class AuthManager:\n"
            "    def __init__(self, secret: str) -> None:\n"
            "        self.secret = secret\n"
            "\n"
            "    async def verify(self, token: str) -> bool:\n"
            "        if not token:\n"
            "            return False\n"
            "        return True\n"
            "\n"
            "\n"
            "def compute_digest(payload: str) -> str:\n"
            "    return payload.strip()\n"
            "\n"
            "\n"
            "async def flush_cache() -> None:\n"
            "    return None\n"
        )
        p = self.write("src/auth.py", src)

        spans = hubs.extract_symbol_spans(p, "src/auth.py")
        by_sym = {sp["symbol"]: (sp["start"], sp["end"], sp["kind"]) for sp in spans}
        self.assertEqual(
            by_sym,
            {
                "AuthManager": (4, 11, "class"),
                "AuthManager.__init__": (5, 6, "method"),
                "AuthManager.verify": (8, 11, "method"),
                "compute_digest": (14, 15, "function"),
                "flush_cache": (18, 19, "async_function"),
            },
        )

        # Simulate Python 3.8 where `end_lineno` may be absent on AST nodes
        orig_parse = ast.parse

        def parse_without_end_lineno(source, *args, **kwargs):
            tree = orig_parse(source, *args, **kwargs)
            for node in ast.walk(tree):
                if hasattr(node, "end_lineno"):
                    delattr(node, "end_lineno")
            return tree

        with mock.patch.object(hubs.ast, "parse", side_effect=parse_without_end_lineno):
            spans_py38 = hubs.extract_symbol_spans(p, "src/auth.py")
        self.assertEqual(spans_py38, spans)

    def test_ts_and_go_span_extraction(self):
        ts_src = (
            "import { Request } from './http';\n"
            "\n"
            "export interface Session {\n"
            "  id: string;\n"
            "}\n"
            "\n"
            "export function verifySession(s: Session): boolean {\n"
            "  return Boolean(s.id);\n"
            "}\n"
            "\n"
            "export class SessionStore {\n"
            "  private items: Record<string, Session> = {};\n"
            "}\n"
            "\n"
            "const helper = 1;\n"
            "export { helper, Session as UserSession };\n"
        )
        ts_path = self.write("web/session.ts", ts_src)
        ts_spans = hubs.extract_symbol_spans(ts_path, "web/session.ts")
        ts_map = {sp["symbol"]: (sp["start"], sp["end"]) for sp in ts_spans}
        self.assertEqual(ts_map["Session"], (3, 5))
        self.assertEqual(ts_map["verifySession"], (7, 9))
        self.assertEqual(ts_map["SessionStore"], (11, 13))
        # Same-line exports share [L16-L16] cleanly without inverted bounds
        self.assertEqual(ts_map["helper"], (16, 16))
        self.assertEqual(ts_map["UserSession"], (16, 16))

        go_src = (
            "package server\n"
            "\n"
            'import "net/http"\n'
            "\n"
            "type Server struct {\n"
            "    addr string\n"
            "}\n"
            "\n"
            "func NewServer(addr string) *Server {\n"
            "    return &Server{addr: addr}\n"
            "}\n"
            "\n"
            "func (s *Server) Handle(w http.ResponseWriter) {\n"
            "    _ = w\n"
            "}\n"
            "\n"
            "func (c *Client) Handle(w http.ResponseWriter) {\n"
            "    _ = w\n"
            "}\n"
        )
        go_path = self.write("pkg/server.go", go_src)
        go_spans = hubs.extract_symbol_spans(go_path, "pkg/server.go")
        go_map = {sp["symbol"]: (sp["start"], sp["end"]) for sp in go_spans}
        self.assertEqual(go_map["Server"], (5, 7))
        self.assertEqual(go_map["NewServer"], (9, 11))
        self.assertEqual(go_map["Server.Handle"], (13, 15))
        self.assertEqual(go_map["Client.Handle"], (17, 19))

    def test_zoom_scaffold_cli_and_output_file(self):
        self.write(
            "core/engine.py",
            (
                "class Engine:\n"
                "    def run(self) -> int:\n"
                "        return 1\n"
                "\n"
                "def warmup() -> None:\n"
                "    pass\n"
            ),
        )
        self.init()

        scaf = cli_json("zoom", "--root", str(self.root), "core/engine.py", "--scaffold", "--output", ".aoci/pending/zoom.txt")
        self.assertTrue(scaf["ok"])
        self.assertEqual(scaf["mode"], "scaffold")
        self.assertEqual(
            scaf["entries"],
            [
                "core/engine.py#Engine[L1-L3]: F:<TODO:read-section> | S:-",
                "core/engine.py#Engine.run[L2-L3]: F:<TODO:read-section> | S:-",
                "core/engine.py#warmup[L5-L6]: F:<TODO:read-section> | S:-",
            ],
        )
        draft_file = self.root / ".aoci" / "pending" / "zoom.txt"
        self.assertTrue(draft_file.is_file())
        raw_bytes = draft_file.read_bytes()
        self.assertNotIn(b"\r\n", raw_bytes)
        self.assertIn("core/engine.py#Engine.run[L2-L3]: F:<TODO:read-section> | S:-", raw_bytes.decode("utf-8"))

    def test_zoom_upsert_validation_prune_missing_and_dry_run(self):
        self.write(
            "mod/a.py",
            (
                "def alpha():\n"
                "    return 1\n"
                "\n"
                "def beta():\n"
                "    return 2\n"
            ),
        )
        self.write("mod/b.py", "def gamma():\n    return 3\n")
        self.init()
        aoci_dir = self.root / AOCI_DIR_NAME
        hubs_path = aoci_dir / HUBS_FILE

        # 1) Reject <TODO>, empty F:, out-of-bounds spans, and path traversal
        bad_cases = [
            "mod/a.py#alpha[L1-L2]: F:<TODO:read-section> | S:-",
            "mod/a.py#alpha[L1-L2]: F:- | S:-",
            "mod/a.py#alpha[L1-L2]: F: | S:-",
            "mod/a.py#alpha[L0-L2]: F:bad zero start | S:-",
            "mod/a.py#alpha[L4-L2]: F:inverted bounds | S:-",
            "mod/a.py#alpha[L1-L99]: F:exceeds EOF | S:-",
            "../escape.py#alpha[L1-L2]: F:path traversal | S:-",
            "mod/missing.py#alpha[L1-L2]: F:missing file | S:-",
        ]
        for bad_line in bad_cases:
            rc, out = cli("zoom", "--root", str(self.root), "--upsert", bad_line, "--json")
            self.assertEqual(rc, 1, f"Expected failure for {bad_line}, got: {out}")
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertFalse(hubs_path.exists())

        # 2) `--dry-run` validates locklessly and does not create `aoci.hubs.txt`
        with mock.patch.object(lock, "IndexLock", side_effect=AssertionError("dry-run must be lockless")):
            dry_res = hubs.upsert_hub_entries(
                self.root,
                aoci_dir,
                ["mod/a.py#alpha[L1-L2]: F:returns constant 1 | S:pure function"],
                dry_run=True,
                lock_cls=lock.IndexLock,
            )
            dry_cli = cli_json(
                "zoom",
                "--root",
                str(self.root),
                "--upsert",
                "mod/a.py#alpha[L1-L2]: F:returns constant 1 | S:pure function",
                "--dry-run",
            )
        self.assertTrue(dry_res["ok"])
        self.assertTrue(dry_res["dry_run"])
        self.assertEqual(dry_res["upserted"], ["mod/a.py#alpha"])
        self.assertFalse(hubs_path.exists())
        self.assertTrue(dry_cli["ok"])
        self.assertTrue(dry_cli["dry_run"])
        self.assertFalse(hubs_path.exists())

        # 3) Repeatable `--upsert` + `--upsert-file` write deterministically sorted by (relpath, start, symbol)
        batch_file = self.root / ".aoci" / "pending" / "hubs_batch.txt"
        batch_file.parent.mkdir(parents=True, exist_ok=True)
        with batch_file.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "# comment ignored\n"
                "mod/b.py#gamma[L1-L2]: F:returns constant 3 | S:-\n"
                "mod/a.py#beta[L4-L5]: F:returns constant 2 | S:-\n"
            )
        up_res = cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "mod/a.py#alpha[L1-L2]: F:returns constant 1 | S:pure function",
            "--upsert-file",
            ".aoci/pending/hubs_batch.txt",
        )
        self.assertTrue(up_res["ok"])
        self.assertEqual(up_res["total"], 3)
        self.assertTrue(hubs_path.is_file())

        records, malformed = hubs.parse_hubs_volume(hubs_path)
        self.assertEqual(malformed, [])
        self.assertEqual(
            [(r["relpath"], r["start"], r["symbol"]) for r in records],
            [("mod/a.py", 1, "alpha"), ("mod/a.py", 4, "beta"), ("mod/b.py", 1, "gamma")],
        )

        # 4) `--prune-missing` drops deleted files AND deleted symbols while keeping live entries
        (self.root / "mod" / "b.py").unlink()
        self.write("mod/a.py", "def alpha():\n    return 1\n")
        prune_res = cli_json("zoom", "--root", str(self.root), "--prune-missing")
        self.assertTrue(prune_res["ok"])
        self.assertEqual(sorted(prune_res["pruned"]), ["mod/a.py#beta", "mod/b.py#gamma"])
        self.assertEqual(prune_res["total"], 1)
        remaining, _ = hubs.parse_hubs_volume(hubs_path)
        self.assertEqual([f"{r['relpath']}#{r['symbol']}" for r in remaining], ["mod/a.py#alpha"])

    def test_zoom_read_mode_and_ack_span_refresh(self):
        self.write(
            "big.py",
            (
                "class Worker:\n"
                "    def run(self) -> int:\n"
                "        return 10\n"
                "\n"
                "def finalize() -> str:\n"
                "    return 'ok'\n"
            ),
        )
        self.init()
        self.assertEqual(
            self.upsert("big.py[KX8T]: F:executes primary worker pipeline and finalizer | R:- | A:Worker,finalize | S:must run finalize last")[0],
            0,
        )
        up_hub = cli_json(
            "zoom",
            "--root",
            str(self.root),
            "--upsert",
            "big.py#Worker[L1-L3]: F:worker class holding run loop | S:stateless",
            "--upsert",
            "big.py#Worker.run[L2-L3]: F:executes unit work returning 10 | S:-",
            "--upsert",
            "big.py#finalize[L5-L6]: F:returns ok sentinel | S:idempotent",
        )
        self.assertTrue(up_hub["ok"])

        # Read mode returns code_entry + hub_entries + live symbol_spans
        rd = cli_json("zoom", "--root", str(self.root), "big.py")
        self.assertTrue(rd["ok"])
        self.assertEqual(rd["mode"], "read")
        self.assertIn("big.py[KX8T]:", rd["code_entry"])
        self.assertEqual(len(rd["hub_entries"]), 3)
        self.assertEqual(
            [(s["symbol"], s["start"], s["end"]) for s in rd["symbol_spans"]],
            [("Worker", 1, 3), ("Worker.run", 2, 3), ("finalize", 5, 6)],
        )
        rc_txt, txt_out = cli("zoom", "--root", str(self.root), "big.py")
        self.assertEqual(rc_txt, 0, txt_out)
        self.assertIn("=== big.py ===", txt_out)
        self.assertIn("big.py#Worker.run[L2-L3]", txt_out)

        # Insert 5 header lines at top of `big.py` so every symbol shifts by +5
        self.write(
            "big.py",
            (
                "# header 1\n"
                "# header 2\n"
                "# header 3\n"
                "# header 4\n"
                "# header 5\n"
                "class Worker:\n"
                "    def run(self) -> int:\n"
                "        return 10\n"
                "\n"
                "def finalize() -> str:\n"
                "    return 'ok'\n"
            ),
        )

        # `ack --dry-run` MUST NOT refresh `aoci.hubs.txt`
        ack_dry = cli_json("ack", "--root", str(self.root), "big.py", "--dry-run")
        self.assertTrue(ack_dry["ok"])
        before_recs, _ = hubs.parse_hubs_volume(self.root / AOCI_DIR_NAME / HUBS_FILE)
        self.assertEqual([(r["symbol"], r["start"], r["end"]) for r in before_recs], [("Worker", 1, 3), ("Worker.run", 2, 3), ("finalize", 5, 6)])

        # Real `ack` refreshes `aoci.hubs.txt` line spans automatically while preserving F:/S:
        ack_real = cli_json("ack", "--root", str(self.root), "big.py")
        self.assertTrue(ack_real["ok"])
        self.assertEqual(len(ack_real.get("hubs_refreshed", [])), 3)
        after_recs, _ = hubs.parse_hubs_volume(self.root / AOCI_DIR_NAME / HUBS_FILE)
        self.assertEqual(
            [(r["symbol"], r["start"], r["end"], r["f"], r["s"]) for r in after_recs],
            [
                ("Worker", 6, 8, "worker class holding run loop", "stateless"),
                ("Worker.run", 7, 8, "executes unit work returning 10", "-"),
                ("finalize", 10, 11, "returns ok sentinel", "idempotent"),
            ],
        )

    def test_mcp_aoci_zoom_and_hubs_resource(self):
        import aoci_mcp

        self.write("-dash.py", "def handle():\n    return 1\n")
        self.init()

        # Verify `aoci_zoom` in tools/list and `aoci://hubs` in resources/list
        tools_resp = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools_by_name = {t["name"]: t for t in tools_resp["result"]["tools"]}
        self.assertIn("aoci_zoom", tools_by_name)
        self.assertFalse(tools_by_name["aoci_zoom"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools_by_name["aoci_zoom"]["annotations"]["idempotentHint"])
        self.assertIn("root", tools_by_name["aoci_zoom"]["inputSchema"]["required"])

        res_resp = aoci_mcp.handle_rpc_request({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        self.assertIn("aoci://hubs", {r["uri"] for r in res_resp["result"]["resources"]})

        # Reading `aoci://hubs` before creation returns -32002 cleanly
        missing_hubs = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": f"aoci://hubs?root={self.root}"}}
        )
        self.assertEqual(missing_hubs["error"]["code"], -32002)

        # Scaffold via MCP (including dash-prefixed path)
        scaf_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "aoci_zoom", "arguments": {"root": str(self.root), "mode": "scaffold", "path": "-dash.py"}},
            }
        )
        self.assertFalse(scaf_resp["result"]["isError"], scaf_resp["result"]["content"][0]["text"])
        scaf_data = json.loads(scaf_resp["result"]["content"][0]["text"])
        self.assertEqual(scaf_data["entries"], ["-dash.py#handle[L1-L2]: F:<TODO:read-section> | S:-"])

        # Upsert via MCP + read via MCP + read via `aoci://hubs` resource
        up_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "aoci_zoom",
                    "arguments": {
                        "root": str(self.root),
                        "mode": "upsert",
                        "entries": ["-dash.py#handle[L1-L2]: F:handles dash test request | S:returns 1"],
                    },
                },
            }
        )
        self.assertFalse(up_resp["result"]["isError"], up_resp["result"]["content"][0]["text"])
        up_data = json.loads(up_resp["result"]["content"][0]["text"])
        self.assertEqual(up_data["upserted"], ["-dash.py#handle"])

        rd_resp = aoci_mcp.handle_rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "aoci_zoom", "arguments": {"root": str(self.root), "mode": "read", "path": "-dash.py"}},
            }
        )
        self.assertFalse(rd_resp["result"]["isError"], rd_resp["result"]["content"][0]["text"])
        rd_data = json.loads(rd_resp["result"]["content"][0]["text"])
        self.assertEqual(rd_data["hub_entries"], ["-dash.py#handle[L1-L2]: F:handles dash test request | S:returns 1"])

        hubs_res = aoci_mcp.handle_rpc_request(
            {"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": f"aoci://hubs?root={self.root}"}}
        )
        self.assertIn("-dash.py#handle[L1-L2]: F:handles dash test request | S:returns 1", hubs_res["result"]["contents"][0]["text"])
