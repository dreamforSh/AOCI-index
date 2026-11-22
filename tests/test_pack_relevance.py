#!/usr/bin/env python3
"""`pack` seed relevance: test-layer demotion, relative score cutoff, and hub-section fallback."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aoci_testkit  # noqa: E402,F401  (puts scripts/ on sys.path)
from aoci_core import graph, pack  # noqa: E402

_ENTRIES = {
    "src/lock.py": "src/lock.py[CK7T]: F:writer mutex with stale lock reaping | R:- | A:IndexLock | S:verify owner token",
    "tests/test_lock.py": "tests/test_lock.py[TK5T]: F:tests stale lock reaping, stale lock reaping races, and writer mutex reaping | R:src/lock.py | A:LockTests | S:-",
    "src/net.py": "src/net.py[IK6T]: F:network helpers for the stdio server | R:- | A:read_frame | S:-",
    "src/misc.py": "src/misc.py[CK3T]: F:assorted helpers with a writer utility | R:- | A:util | S:-",
}


def _pack(task: str, **kw: object) -> dict:
    parsed = graph.parse_entries_map(_ENTRIES)
    deps, dependents = graph.build_fras_graph(_ENTRIES, parsed_entries=parsed)
    return pack.build_task_pack(
        Path("."),
        _ENTRIES,
        deps,
        dependents,
        "-",
        {},
        [],
        task=task,
        budget=0,
        depth=0,
        parsed_entries=parsed,
        check_stale=False,
        hub_records=kw.pop("hub_records", []),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


class PackRelevanceTests(unittest.TestCase):
    def test_non_test_tasks_demote_test_files(self) -> None:
        plain = _pack("fix stale lock reaping", test_demote=1.0, min_rel_score=0.0)
        self.assertEqual(plain["seeds"][0]["path"], "tests/test_lock.py", "BM25 alone ranks the noisy test first")
        tuned = _pack("fix stale lock reaping")
        tuned_paths = [s["path"] for s in tuned["seeds"]]
        self.assertEqual(tuned_paths[0], "src/lock.py", "demoted (not dropped) tests rank behind the implementation")
        demoted = next(s for s in tuned["seeds"] if s["path"] == "tests/test_lock.py")
        original = next(s for s in plain["seeds"] if s["path"] == "tests/test_lock.py")
        self.assertAlmostEqual(demoted["score"], round(original["score"] * pack.TEST_LAYER_DEMOTE, 3), places=2)

    def test_tasks_mentioning_tests_keep_test_files(self) -> None:
        self.assertTrue(pack.task_mentions_tests("the stale lock test is flaky"))
        self.assertTrue(pack.task_mentions_tests("修复锁回收的单测"))
        self.assertFalse(pack.task_mentions_tests("attest the latest release"))
        seeds = [s["path"] for s in _pack("the stale lock reaping test is flaky")["seeds"]]
        self.assertIn("tests/test_lock.py", seeds)

    def test_relative_cutoff_drops_weak_seeds(self) -> None:
        loose = [s["path"] for s in _pack("writer mutex", min_rel_score=0.0)["seeds"]]
        self.assertIn("src/misc.py", loose)
        strict = [s["path"] for s in _pack("writer mutex", min_rel_score=0.6)["seeds"]]
        self.assertNotIn("src/misc.py", strict)
        self.assertIn("src/lock.py", strict)

    def test_hub_sections_seed_files_whose_entry_misses_the_vocabulary(self) -> None:
        hubs = [
            {
                "relpath": "src/net.py",
                "symbol": "read_frame",
                "start": 10,
                "end": 40,
                "f": "decodes Content-Length framed JSON-RPC messages",
                "s": "reject oversized Content-Length headers",
                "line": "src/net.py#read_frame[L10-L40]: F:decodes Content-Length framed JSON-RPC messages"
                " | S:reject oversized Content-Length headers",
            }
        ]
        without = _pack("Content-Length framing breaks", hub_records=[])
        self.assertNotIn("src/net.py", [s["path"] for s in without["seeds"]])
        with_hubs = _pack("Content-Length framing breaks", hub_records=hubs)
        seed = next(s for s in with_hubs["seeds"] if s["path"] == "src/net.py")
        self.assertIn("hub", seed["reasons"])
        no_fallback = _pack("Content-Length framing breaks", hub_records=hubs, hub_fallback=False)
        self.assertNotIn("src/net.py", [s["path"] for s in no_fallback["seeds"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
