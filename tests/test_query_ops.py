#!/usr/bin/env python3
"""Shared `query` engine: CLI and MCP payload parity, and filter-independent ranking scores."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aoci_testkit import RepoTestCase, cli_json  # noqa: E402
import aoci_mcp  # noqa: E402

_ENTRIES = {
    "src/auth/jwt.py": "src/auth/jwt.py[MA8T]: F:issues and verifies JWT access tokens | R:src/config/env.py | A:issue_token<-src/routes/orders.py | S:reject alg none",
    "src/config/env.py": "src/config/env.py[CG7T]: F:loads environment settings and token secrets | R:- | A:get_settings<-src/auth/jwt.py | S:-",
    "src/routes/orders.py": "src/routes/orders.py[EO6T]: F:HTTP order routes requiring JWT tokens | R:src/auth/jwt.py | A:create_order | S:-",
    "src/order/service.py": "src/order/service.py[AO7T]: F:creates orders and reserves stock tokens | R:- | A:create<-src/routes/orders.py | S:idempotency key",
}


class QueryParityTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        for rel in _ENTRIES:
            self.write(rel, f"# {rel}\nvalue = 1\n")
        self.init()
        code, out = self.upsert(*_ENTRIES.values())
        self.assertEqual(code, 0, out)

    def _mcp(self, **args: object) -> dict:
        is_err, text = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), **args})
        self.assertFalse(is_err, text)
        return json.loads(text)

    def test_cli_and_mcp_return_identical_payloads(self) -> None:
        combos = [
            (["--search", "jwt tokens"], {"search": "jwt tokens"}),
            (["--search", "tokens", "--layer", "A,M"], {"search": "tokens", "layer": "A,M"}),
            (["--keyword", "order"], {"keyword": "order"}),
            (["--impact", "src/config/env.py", "--depth", "2"], {"impact": "src/config/env.py", "depth": 2}),
            (["--domain", "O", "--min-importance", "7"], {"domain": "O", "min_importance": 7}),
            (["--search", "tokens", "--top", "2"], {"search": "tokens", "top": 2}),
        ]
        for cli_args, mcp_args in combos:
            self.assertEqual(cli_json("query", "--root", str(self.root), *cli_args), self._mcp(**mcp_args), cli_args)

    def test_filtered_search_scores_match_unfiltered_ranking(self) -> None:
        full = {h["path"]: h["score"] for h in cli_json("query", "--root", str(self.root), "--search", "tokens")["scores"]}
        narrowed = cli_json("query", "--root", str(self.root), "--search", "tokens", "--path", "src/order/*")["scores"]
        self.assertEqual([h["path"] for h in narrowed], ["src/order/service.py"])
        self.assertEqual(narrowed[0]["score"], full["src/order/service.py"], "a filter must not change IDF")


if __name__ == "__main__":
    unittest.main(verbosity=2)
