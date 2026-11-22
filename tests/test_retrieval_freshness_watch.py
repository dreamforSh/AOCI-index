#!/usr/bin/env python3
"""Eval harness, index freshness, incremental MCP cache, hybrid (RRF) retrieval, and MCP resource watcher tests."""

from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from typing import Any, Dict, List

sys.dont_write_bytecode = True

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from aoci_testkit import HAS_GIT, RepoTestCase, SKILL_DIR, cli, cli_json  # noqa: E402
import aoci_mcp  # noqa: E402
from aoci_core import evalbench, freshness, graph, hybrid  # noqa: E402

_ENTRIES = {
    "src/mcp/server.py": "src/mcp/server.py[EM9T]: F:serves stdio JSON-RPC with resource subscriptions and push notifications | R:src/core/index.py | A:serve<-src/cli.py | S:never write logs to stdout",
    "src/core/lock.py": "src/core/lock.py[CK7T]: F:cross-process writer mutex with dead-PID stale reaping | R:- | A:IndexLock<-src/core/index.py | S:verify owner token before unlink",
    "src/core/index.py": "src/core/index.py[KK8T]: F:merges FRAS entries into the code volume atomically | R:src/core/lock.py | A:upsert<-src/mcp/server.py,src/cli.py | S:re-read volume inside the lock",
    "src/cli.py": "src/cli.py[EK6T]: F:command line entrypoint dispatching subcommands | R:src/core/index.py,src/mcp/server.py | A:main | S:-",
    "src/ignore.py": "src/ignore.py[KK5T]: F:evaluates gitignore negation patterns for discovery | R:- | A:IgnoreMatcher<-src/cli.py | S:-",
}


class _IndexedRepo(RepoTestCase):
    def build_index(self) -> None:
        for rel in _ENTRIES:
            self.write(rel, f"# {rel}\n" + "".join(f"def f{i}():\n    return {i}\n" for i in range(4)))
        self.init()
        code, out = self.upsert(*_ENTRIES.values())
        self.assertEqual(code, 0, out)


# ---------------------------------------------------------------------------------------------- eval harness
class EvalHarnessTests(_IndexedRepo):
    def _cases(self, rows: List[Dict[str, Any]], name: str = "cases.jsonl") -> Path:
        p = self.root / name
        p.write_text("# comment line\n" + "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return p

    def test_eval_metrics_modes_thresholds_and_unknown_paths(self) -> None:
        self.build_index()
        cases = self._cases(
            [
                {"id": "lock", "task": "writer mutex stale reaping", "expected": ["src/core/lock.py"]},
                {"id": "ignore", "task": "gitignore negation patterns", "expected": ["src/ignore.py"]},
                {"id": "ghost", "task": "something unindexed", "expected": ["nope/missing.py"]},
            ]
        )
        res = cli_json("eval", "--root", str(self.root), "--cases", str(cases), "--modes", "bm25,hybrid", "--k", "3")
        self.assertTrue(res["ok"])
        self.assertEqual([r["mode"] for r in res["reports"]], ["bm25", "hybrid"])
        bm = res["reports"][0]
        self.assertEqual(bm["cases"], 3)
        self.assertEqual(bm["scored_cases"], 2, "cases whose expected paths are all unindexed are not scored")
        self.assertEqual(bm["metrics"]["recall_at_k"], 1.0)
        self.assertEqual(bm["metrics"]["mrr"], 1.0)
        self.assertEqual(bm["metrics"]["pack_recall"], 1.0)
        ghost = next(r for r in bm["results"] if r["id"] == "ghost")
        self.assertEqual(ghost["unknown_expected"], ["nope/missing.py"])
        self.assertEqual(res["reports"][1]["channels"], ["bm25", "trigram"])
        self.assertEqual(res["gate_failures"], [])

        code, out = cli("eval", "--root", str(self.root), "--cases", str(cases), "--min-recall", "1.01")
        self.assertEqual(code, 1, out)
        self.assertIn("GATE FAILED", out)
        code, out = cli("eval", "--root", str(self.root), "--cases", str(cases))
        self.assertEqual(code, 0, out)
        self.assertIn("# AOCI Eval [bm25]", out)

    def test_case_file_validation(self) -> None:
        self.build_index()
        bad_json = self.root / "bad.jsonl"
        bad_json.write_text('{"task": "x", "expected": ["a"]}\n{not json}\n', encoding="utf-8")
        with self.assertRaises(evalbench.EvalCaseError):
            evalbench.load_eval_cases(bad_json)
        no_expected = self.root / "noexp.json"
        no_expected.write_text(json.dumps({"cases": [{"task": "x", "expected": []}]}), encoding="utf-8")
        with self.assertRaises(evalbench.EvalCaseError):
            evalbench.load_eval_cases(no_expected)
        code, out = cli("eval", "--root", str(self.root), "--cases", str(bad_json))
        self.assertEqual(code, 2, out)
        code, out = cli("eval", "--root", str(self.root), "--cases", str(no_expected), "--modes", "vector")
        self.assertEqual(code, 2, out)


@unittest.skipUnless(HAS_GIT, "git is required")
class SelfEvalRegressionTests(unittest.TestCase):
    """Guards retrieval quality on AOCI-index itself (index bootstrapped from `examples/self-pool.json.gz`)."""

    def test_self_eval_recall_floor(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("AOCI_TEST_TMP")) as td:
            clone = Path(td) / "self"
            shutil.copytree(
                SKILL_DIR,
                clone,
                ignore=shutil.ignore_patterns(".git", ".aoci", "__pycache__", "build", "dist", ".venv", ".idea", "*.egg-info"),
            )
            cli_json("init", "--root", str(clone))
            meta = clone / ".aoci" / "aoci.meta.txt"
            meta.write_text(
                meta.read_text(encoding="utf-8") + "\n#Domains: G=governance, K=kernel, M=mcp, H=hooks-and-hosts, P=parsers\n",
                encoding="utf-8",
            )
            cli_json("pool", "import", "--root", str(clone), "--input", str(SKILL_DIR / "examples" / "self-pool.json.gz"))
            cli_json("restore", "--root", str(clone))
            cases = evalbench.load_eval_cases(SKILL_DIR / "examples" / "eval" / "self-eval.jsonl")
            res = evalbench.run_eval(clone, cases, modes=("bm25", "hybrid"), k=8, budget=1000)
            bm, hy = (r["metrics"] for r in res["reports"])
            # Absolute regression floors per mode (not tuning targets; the shipped index scores ~0.88 recall@8).
            # Hybrid vs BM25 swings by a case or two on this small set, so no relative assertion between them.
            for metrics in (bm, hy):
                self.assertGreaterEqual(metrics["recall_at_k"], 0.8, (bm, hy))
                self.assertGreaterEqual(metrics["pack_recall"], 0.8, (bm, hy))
            self.assertEqual(res["reports"][0]["scored_cases"], len(cases), "self-pool must restore every expected file")


# ---------------------------------------------------------------------------------------------- freshness
class FreshnessTests(_IndexedRepo):
    def test_reasons_and_touch_only_is_fresh(self) -> None:
        self.build_index()
        base = freshness.load_baseline_files(self.root)
        self.assertEqual(freshness.check_freshness(self.root, list(_ENTRIES), base), {})

        lock_p = self.root / "src/core/lock.py"
        os.utime(lock_p, (time.time() - 5, time.time() - 5))  # touch only: stat differs, content identical
        self.write("src/ignore.py", "def changed():\n    return 1\n")
        (self.root / "src/cli.py").unlink()
        stale = freshness.check_freshness(self.root, [*_ENTRIES, "never/indexed.py"], base)
        self.assertNotIn("src/core/lock.py", stale)
        self.assertEqual(stale["src/ignore.py"], "modified")
        self.assertEqual(stale["src/cli.py"], "missing")
        self.assertEqual(stale["never/indexed.py"], "missing")
        self.write("never/indexed.py", "x = 1\n")
        self.assertEqual(freshness.check_freshness(self.root, ["never/indexed.py"], base), {"never/indexed.py": "unbaselined"})

    def test_cli_and_mcp_read_paths_flag_stale_entries(self) -> None:
        self.build_index()
        self.write("src/core/lock.py", "def rewritten():\n    return 2\n")
        q = cli_json("query", "--root", str(self.root), "--search", "stale reaping mutex")
        self.assertIn({"path": "src/core/lock.py", "reason": "modified"}, [{k: s[k] for k in ("path", "reason")} for s in q["stale_entries"]])
        p = cli_json("pack", "--root", str(self.root), "--task", "writer mutex stale reaping", "--no-diff")
        self.assertEqual([s["path"] for s in p["stale_entries"]], ["src/core/lock.py"])
        self.assertEqual(next(s for s in p["seeds"] if s["path"] == "src/core/lock.py")["stale"], "modified")
        c = cli_json("context", "src/core/index.py", "--root", str(self.root))
        self.assertEqual([s["path"] for s in c["stale_entries"]], ["src/core/lock.py"])
        code, out = cli("pack", "--root", str(self.root), "--task", "writer mutex stale reaping", "--no-diff")
        self.assertIn("## Stale Entries (1)", out)
        self.assertIn("STALE:modified", out)

        for tool, args in (
            ("aoci_query", {"search": "stale reaping mutex"}),
            ("aoci_pack", {"task": "writer mutex stale reaping", "include_diff": False}),
            ("aoci_context", {"paths": ["src/core/index.py"]}),
        ):
            is_err, text = aoci_mcp.call_tool(tool, {"root": str(self.root), **args})
            self.assertFalse(is_err, text)
            self.assertIn("src/core/lock.py", [s["path"] for s in json.loads(text)["stale_entries"]], tool)


# ---------------------------------------------------------------------------------------------- incremental cache
def _random_entries(n: int, seed: int) -> Dict[str, str]:
    rnd = random.Random(seed)
    words = "auth token cache order payment session router config parser index search graph lock queue retry".split()
    paths = [f"pkg{i % 7}/m{i % 5}/f_{i}.py" for i in range(n)]
    out = {}
    for i, p in enumerate(paths):
        deps = ",".join(rnd.sample(paths, 2))
        out[p] = f"{p}[AX{rnd.randint(1, 9)}T]: F:{' '.join(rnd.sample(words, 5))} | R:{deps} | A:fn{i}<-{rnd.choice(paths)} | S:{' '.join(rnd.sample(words, 3))}"
    return out


class IncrementalIndexTests(unittest.TestCase):
    def test_bm25_and_graph_updates_match_full_rebuild_and_are_copy_on_write(self) -> None:
        rnd = random.Random(11)
        entries = _random_entries(300, seed=5)
        parsed = graph.parse_entries_map(entries)
        state = graph.build_fras_graph_state(entries, parsed)
        bm = graph.BM25Index(entries, parsed_entries=parsed)
        self.assertEqual((state.deps, state.dependents), graph.build_fras_graph(entries, parsed_entries=parsed))
        before = bm.search_detailed("payment retry lock", top_k=10)
        paths = sorted(entries)
        for rnd_no in range(12):
            changed: Dict[str, str] = {}
            for _ in range(rnd.randint(1, 4)):
                rel = rnd.choice(paths)
                if rel in entries:
                    changed[rel] = entries[rel].split(" | S:")[0] + f" | S:edited round {rnd_no}"
            new_p = f"new/n{rnd_no}.py"
            changed[new_p] = f"{new_p}[AX5T]: F:new payment worker | R:{rnd.choice(paths)} | A:run<-{rnd.choice(paths)} | S:-"
            removed = [p for p in rnd.sample(paths, 2) if p in entries and p not in changed]
            for r in removed:
                entries.pop(r)
            entries.update(changed)
            paths = sorted(entries)
            parsed = graph.parse_entries_map(entries)
            new_state = graph.update_fras_graph_state(state, entries, parsed, changed, removed)
            new_bm = bm.updated(changed, removed, parsed)
            full_bm = graph.BM25Index(entries, parsed_entries=parsed)
            self.assertIsNotNone(new_state)
            self.assertEqual((new_state.deps, new_state.dependents), graph.build_fras_graph(entries, parsed_entries=parsed))
            for q in ("payment retry lock", f"edited round {rnd_no}", "new worker"):
                self.assertEqual(new_bm.search_detailed(q, top_k=10), full_bm.search_detailed(q, top_k=10), q)
            self.assertEqual((new_bm.n_docs, new_bm.avgdl), (full_bm.n_docs, full_bm.avgdl))
            state, bm = new_state, new_bm
        # The very first index object was never mutated by later updates
        self.assertEqual(graph.BM25Index(_random_entries(300, seed=5)).search_detailed("payment retry lock", top_k=10), before)

    def test_graph_update_bails_out_on_huge_path_additions(self) -> None:
        entries = _random_entries(20, seed=1)
        parsed = graph.parse_entries_map(entries)
        state = graph.build_fras_graph_state(entries, parsed)
        added = {f"bulk/b{i}.py": f"bulk/b{i}.py[AX5T]: F:bulk | R:- | A:- | S:-" for i in range(70)}
        entries.update(added)
        self.assertIsNone(graph.update_fras_graph_state(state, entries, graph.parse_entries_map(entries), added, []))


class IncrementalMcpCacheTests(_IndexedRepo):
    def test_mutation_triggers_incremental_refresh_with_correct_results(self) -> None:
        self.build_index()
        root_key = str(self.root.resolve())
        is_err, text = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "mutex"})
        self.assertFalse(is_err, text)
        self.assertEqual(aoci_mcp._CACHE.last_refresh[root_key]["mode"], "full")

        self.write("src/heartbeat.py", "def beat():\n    return 1\n")
        is_err, text = aoci_mcp.call_tool(
            "aoci_upsert",
            {"root": str(self.root), "entries": ["src/heartbeat.py[CK5T]: F:emits lock heartbeat pulses | R:src/core/lock.py | A:beat | S:-"]},
        )
        self.assertFalse(is_err, text)
        is_err, text = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "heartbeat pulses"})
        self.assertFalse(is_err, text)
        self.assertEqual(json.loads(text)["scores"][0]["path"], "src/heartbeat.py")
        refresh = aoci_mcp._CACHE.last_refresh[root_key]
        self.assertEqual((refresh["mode"], refresh["changed"], refresh["removed"]), ("incremental", 1, 0))
        is_err, text = aoci_mcp.call_tool("aoci_context", {"root": str(self.root), "paths": ["src/core/lock.py"]})
        self.assertIn("src/heartbeat.py", json.dumps(json.loads(text)["callers"]))


# ---------------------------------------------------------------------------------------------- hybrid retrieval
class _StubEmbedder:
    name = "stub:crc16"

    def __init__(self) -> None:
        self.encoded = 0

    def encode(self, texts: List[str]) -> List[List[float]]:
        self.encoded += len(texts)
        out = []
        for t in texts:
            vec = [0.0] * 16
            for w in t.lower().split():
                vec[zlib.crc32(w.encode()) % 16] += 1.0
            out.append(vec)
        return out


class HybridRetrievalTests(_IndexedRepo):
    def test_trigram_channel_recovers_morphological_variants(self) -> None:
        entries = dict(_ENTRIES)
        parsed = graph.parse_entries_map(entries)
        bm_hits, _ = graph.BM25Index(entries, parsed_entries=parsed).search_detailed("subscribe notify", top_k=5)
        self.assertEqual(bm_hits, [], "BM25 has no whole-token match for `subscribe notify`")
        idx = hybrid.HybridIndex(entries, parsed_entries=parsed)
        hits, _ = idx.search_detailed("subscribe notify", top_k=5)
        self.assertEqual(hits[0]["path"], "src/mcp/server.py")
        self.assertEqual(set(hits[0]["channels"]), {"trigram"})
        self.assertEqual(hits[0]["matched_terms"], [])
        restricted, _ = idx.search_detailed("subscribe notify", candidate_paths={"src/cli.py"}, top_k=5)
        self.assertTrue(all(h["path"] == "src/cli.py" for h in restricted))
        self.assertEqual(idx.describe()["channels"], ["bm25", "trigram"])

    def test_dense_channel_caches_vectors_and_degrades_gracefully(self) -> None:
        self.build_index()
        entries = dict(_ENTRIES)
        stub = _StubEmbedder()
        idx = hybrid.HybridIndex(entries, root=self.root, embedder=stub)
        self.assertEqual(stub.encoded, len(entries))
        self.assertTrue(idx.channel_info()[-1].startswith("dense(stub:crc16"))
        hits, _ = idx.search_detailed("cross-process writer mutex", top_k=3)
        self.assertIn("dense", {c for h in hits for c in h["channels"]})

        stub2 = _StubEmbedder()
        hybrid.HybridIndex(entries, root=self.root, embedder=stub2)
        self.assertEqual(stub2.encoded, 0, "unchanged entries must come from the on-disk vector cache")
        entries["src/cli.py"] = entries["src/cli.py"].replace("entrypoint", "launcher")
        stub3 = _StubEmbedder()
        hybrid.HybridIndex(entries, root=self.root, embedder=stub3)
        self.assertEqual(stub3.encoded, 1)

        unknown = hybrid.HybridIndex(entries, dense_backend="faiss")
        self.assertIsNone(unknown.dense)
        self.assertIn("unknown dense backend", unknown.describe()["warnings"][0])

    def test_cli_and_mcp_hybrid_flag(self) -> None:
        self.build_index()
        q = cli_json("query", "--root", str(self.root), "--search", "subscribe notify", "--hybrid")
        self.assertEqual(q["retrieval"]["mode"], "hybrid")
        self.assertEqual(q["scores"][0]["path"], "src/mcp/server.py")
        p = cli_json("pack", "--root", str(self.root), "--task", "subscribe notify", "--hybrid", "--no-diff")
        self.assertEqual(p["seeds"][0]["path"], "src/mcp/server.py")
        is_err, text = aoci_mcp.call_tool("aoci_query", {"root": str(self.root), "search": "subscribe notify", "hybrid": True})
        self.assertFalse(is_err, text)
        self.assertEqual(json.loads(text)["scores"][0]["path"], "src/mcp/server.py")
        spec = {t["name"]: t for t in aoci_mcp.TOOLS_SPEC}
        for tool in ("aoci_query", "aoci_pack"):
            self.assertIn("hybrid", spec[tool]["inputSchema"]["properties"])


# ---------------------------------------------------------------------------------------------- resource watcher
class _Collector:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self.lock:
            self.buf += data
        return len(data)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        with self.lock:
            return self.buf.decode("utf-8")


class ResourceWatcherTests(_IndexedRepo):
    def setUp(self) -> None:
        super().setUp()
        self._orig_watcher = aoci_mcp._WATCHER
        with aoci_mcp._STATE_LOCK:
            self._orig_subs = set(aoci_mcp._SUBSCRIBED_URIS)

    def tearDown(self) -> None:
        aoci_mcp._WATCHER.stop()
        aoci_mcp._WATCHER = self._orig_watcher
        with aoci_mcp._STATE_LOCK:
            aoci_mcp._SUBSCRIBED_URIS.clear()
            aoci_mcp._SUBSCRIBED_URIS.update(self._orig_subs)
        super().tearDown()

    def test_poll_once_baselines_detects_changes_and_resyncs_after_own_mutations(self) -> None:
        self.build_index()
        watcher = aoci_mcp._ResourceWatcher(0.0)
        aoci_mcp._WATCHER = watcher
        uri = f"aoci://code?root={self.root}"
        with aoci_mcp._STATE_LOCK:
            aoci_mcp._SUBSCRIBED_URIS.add(uri)
        watcher.observe(uri)
        self.assertEqual(watcher.poll_once(), [])
        code = self.root / ".aoci" / "aoci.code.txt"
        code.write_text(code.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        msgs = watcher.poll_once()
        self.assertEqual([m["params"]["uri"] for m in msgs], [uri])
        self.assertEqual(watcher.poll_once(), [], "a change is pushed once")

        code.write_text(code.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        aoci_mcp._on_root_mutated(self.root)  # this server already notified -> watcher must not repeat it
        aoci_mcp.drain_outbound_messages()
        self.assertEqual(watcher.poll_once(), [])
        with aoci_mcp._STATE_LOCK:
            aoci_mcp._SUBSCRIBED_URIS.discard(uri)
        code.write_text(code.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertEqual(watcher.poll_once(), [], "unsubscribed URIs are not watched")

    def test_stdio_server_pushes_external_changes(self) -> None:
        self.build_index()
        aoci_mcp._WATCHER = aoci_mcp._ResourceWatcher(0.05)
        r_fd, w_fd = os.pipe()
        rin, win = os.fdopen(r_fd, "rb"), os.fdopen(w_fd, "wb")
        out = _Collector()
        server = threading.Thread(target=aoci_mcp.serve_stdio, args=(rin, out), daemon=True)
        server.start()
        try:
            uri = f"aoci://code?root={self.root}"
            for req in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "resources/subscribe", "params": {"uri": uri}},
            ):
                win.write((json.dumps(req) + "\n").encode("utf-8"))
                win.flush()
            deadline = time.time() + 5
            while '"id":2' not in out.text() and time.time() < deadline:
                time.sleep(0.02)
            code = self.root / ".aoci" / "aoci.code.txt"
            code.write_text(code.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            deadline = time.time() + 5
            while "notifications/resources/updated" not in out.text() and time.time() < deadline:
                time.sleep(0.02)
            self.assertIn("notifications/resources/updated", out.text())
        finally:
            win.close()
            server.join(timeout=5)
            rin.close()
        self.assertFalse(server.is_alive())

    def test_interval_zero_disables_the_thread(self) -> None:
        w = aoci_mcp._ResourceWatcher(0.0)
        w.start(lambda msgs: None)
        self.assertIsNone(w._thread)


if __name__ == "__main__":
    unittest.main(verbosity=2)
