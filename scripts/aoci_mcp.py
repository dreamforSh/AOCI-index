#!/usr/bin/env python3
"""
Zero-Dependency Stdio MCP Server for AOCI Codebase Index (`aoci_mcp.py`).

Speaks MCP JSON-RPC 2.0 over stdio (protocol `2024-11-05`, `2025-03-26`, and
`2025-06-18`; auto-detecting newline-delimited JSON or `Content-Length:` framing)
using only the Python 3.8+ standard library.

Surface:
  - 29 annotated tools (one `_tool_<name>` handler per `TOOLS_SPEC` entry, wired
    through `TOOL_HANDLERS`) with `readOnlyHint`, `outputSchema`, and
    `structuredContent`. `--profile core|sync|full` (or `AOCI_MCP_PROFILE`) trims
    `tools/list` to 8 read-only tools, core + 14 sync/maintenance tools, or all;
    calls outside the profile return `isError` with a hint.
  - Resources `aoci://manifest|meta|code|hubs|shards/INDEX`, templates
    `aoci://shards/{shard}` and `aoci://{volume}?root={root}`, prompts
    `aoci-bootstrap` / `aoci-review-diff` / `aoci-sync`, and `roots/list`
    multi-root discovery (the default root follows newly announced roots).
  - `resources/subscribe`: subscribed files are re-announced after this server's
    own mutations and by a background `stat` poller for outside changes
    (`AOCI_MCP_WATCH_INTERVAL`, default 2 s, `0` disables).

Keeps parsed `.aoci/aoci.code.txt` entries, the directed FRAS graph, BM25,
outlines, hubs, and glossary warm in memory per workspace (keyed on file
`(mtime_ns, size)`). After small edits the cache re-applies only changed entries
(copy-on-write `FrasGraphState` + `BM25Index.updated()`, identical to a full
rebuild), so read tools answer without spawning shell commands or triggering
terminal permission prompts. CLI-backed tools run in-process with per-call
`contextvars` stream capture; stdout carries only JSON-RPC frames.

Tools that write files accept targets only inside the repository root and never
under `.git/` (`AOCI_MCP_ALLOW_OUTSIDE_ROOT=1` lifts the root rule only).
"""

from __future__ import annotations

from collections import OrderedDict
import contextvars
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

# Never write `.pyc` beside the skill sources (bytecode caching is re-enabled below for script runs).
sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import aoci_index  # noqa: E402

if __name__ == "__main__":
    aoci_index._configure_bytecode_cache()  # per-user `sys.pycache_prefix` for the lazily imported core

SERVER_NAME = "aoci-index"
SERVER_VERSION = aoci_index.AOCI_VERSION
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


class _MemoryIndexCache:
    """LRU-bounded, mtime-keyed in-memory cache of parsed FRAS volumes, outlines, hubs, and BM25."""

    _DIFF_TTL_SECONDS = 1.5

    def __init__(self, max_slots: int = 16) -> None:
        self._max_slots = max(1, max_slots)
        self._lock = threading.RLock()
        self._root_locks: "OrderedDict[str, threading.RLock]" = OrderedDict()
        self._slots: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._outline_slots: "OrderedDict[str, Tuple[Any, Dict[str, Any]]]" = OrderedDict()
        self._hub_slots: "OrderedDict[str, Tuple[Any, List[Dict[str, Any]]]]" = OrderedDict()
        self._diff_slots: "OrderedDict[str, Tuple[Any, float, Dict[str, Any]]]" = OrderedDict()
        self._baseline_slots: "OrderedDict[str, Tuple[Any, Dict[str, Any]]]" = OrderedDict()
        # Slots evicted by `invalidate()`: never served, only reused as the base for an incremental refresh
        self._stale_bases: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        # Last slot refresh per root: `{"mode": "hit"|"incremental"|"full", "ms", "changed", "removed"}`
        self.last_refresh: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _canonical_root(root: Path) -> Tuple[Path, str]:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        return resolved, str(resolved)

    def _get_root_lock(self, root_key: str) -> threading.RLock:
        with self._lock:
            rlk = self._root_locks.get(root_key)
            if rlk is None:
                rlk = threading.RLock()
                self._root_locks[root_key] = rlk
            else:
                self._root_locks.move_to_end(root_key)
            while len(self._root_locks) > self._max_slots * 4:
                oldest_k, _ = next(iter(self._root_locks.items()))
                if oldest_k == root_key:
                    break
                self._root_locks.pop(oldest_k, None)
            return rlk

    def invalidate(self, root: Path, drop: bool = False) -> None:
        """
        Evict the root's live slot so the next `get()` re-reads the volume. The evicted slot is parked in
        `_stale_bases` (never served) so that refresh can re-apply only the changed entries; `drop=True`
        discards it as well.
        """
        _, root_key = self._canonical_root(root)
        raw_key = str(root)
        with self._lock:
            for key in (root_key, raw_key):
                old_slot = self._slots.pop(key, None)
                if drop:
                    self._stale_bases.pop(key, None)
                elif old_slot is not None:
                    self._stale_bases[key] = old_slot
                    self._stale_bases.move_to_end(key)
                    while len(self._stale_bases) > self._max_slots:
                        self._stale_bases.popitem(last=False)
                self._outline_slots.pop(key, None)
                self._hub_slots.pop(key, None)
                self._diff_slots.pop(key, None)

    _INCREMENTAL_MAX_RATIO = 0.25
    _INCREMENTAL_MIN_DELTA = 256

    def get(self, root: Path) -> Optional[Dict[str, Any]]:
        resolved, root_key = self._canonical_root(root)
        aoci_dir = resolved / aoci_index.AOCI_DIR_NAME
        code_path = aoci_dir / aoci_index.CODE_FILE
        root_lock = self._get_root_lock(root_key)
        with root_lock:
            if not code_path.is_file():
                self.invalidate(root, drop=True)
                return None
            key = (
                aoci_index._stat_key(code_path),
                aoci_index._stat_key(aoci_dir / aoci_index.MANIFEST_FILE),
                aoci_index._stat_key(aoci_dir / aoci_index.META_FILE),
                aoci_index._stat_key(aoci_dir / aoci_index.GLOSSARY_FILE),
            )
            with self._lock:
                cached = self._slots.get(root_key)
                if cached is not None and cached.get("key") == key:
                    self._slots.move_to_end(root_key)
                    return cached
                if cached is None:
                    cached = self._stale_bases.pop(root_key, None)

            t0 = time.perf_counter()
            slot: Optional[Dict[str, Any]] = None
            if (
                cached is not None
                and cached.get("graph_state") is not None
                and tuple(cached.get("key") or ())[1:] == key[1:]
            ):
                slot = self._refresh_incremental(cached, key, code_path)
            mode = "incremental"
            if slot is None:
                slot = self._build_full(resolved, aoci_dir, code_path, key, root_lock)
                mode = "full"
            delta = slot.pop("_delta", (len(slot["entries"]), 0))
            self.last_refresh[root_key] = {
                "mode": mode,
                "ms": round((time.perf_counter() - t0) * 1000.0, 2),
                "changed": delta[0],
                "removed": delta[1],
            }
            with self._lock:
                self._slots[root_key] = slot
                self._slots.move_to_end(root_key)
                while len(self._slots) > self._max_slots:
                    self._slots.popitem(last=False)
            return slot

    @staticmethod
    def _estimate_volume_tokens(entries: Dict[str, str], raw_code_texts: List[str]) -> int:
        if raw_code_texts:
            return aoci_index.estimate_tokens(raw_code_texts[0])
        return max(1, sum(aoci_index.estimate_tokens(ln) for ln in entries.values()) + 20)

    def _build_full(
        self,
        resolved: Path,
        aoci_dir: Path,
        code_path: Path,
        key: Tuple[Any, ...],
        root_lock: threading.RLock,
    ) -> Dict[str, Any]:
        duplicates: List[str] = []
        raw_code_texts: List[str] = []
        entries, malformed = aoci_index.parse_code_volume(
            code_path, duplicates=duplicates, raw_text_out=raw_code_texts
        )
        parsed = aoci_index.parse_entries_map(entries)
        graph_state = aoci_index._graph.build_fras_graph_state(entries, parsed)
        return {
            "key": key,
            "_lock": root_lock,
            "entries": entries,
            "parsed": parsed,
            "malformed": malformed,
            "duplicates": duplicates,
            "deps": graph_state.deps,
            "dependents": graph_state.dependents,
            "graph_state": graph_state,
            "mention_index": aoci_index.PathMentionIndex(sorted(entries.keys())),
            "bm25": None,
            "hybrid": {},
            "test_index_entry": None,
            "glossary": aoci_index.parse_glossary(aoci_dir),
            "project": aoci_index.parse_manifest_field(aoci_dir, "#Project:") or resolved.name,
            "invariants": aoci_index.parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-",
            "git_hdr": aoci_index.parse_manifest_field(aoci_dir, "#Git-Excluded:") or "unknown",
            "domains": aoci_index.parse_declared_domains(aoci_dir),
            "estimated_tokens": self._estimate_volume_tokens(entries, raw_code_texts),
        }

    def _refresh_incremental(
        self,
        cached: Dict[str, Any],
        key: Tuple[Any, ...],
        code_path: Path,
    ) -> Optional[Dict[str, Any]]:
        """
        Re-read `aoci.code.txt` and re-apply only changed / removed entries to the parsed map, graph state,
        mention index, and (if built) BM25, all copy-on-write. `None` means "do a full rebuild" (large delta).
        """
        duplicates: List[str] = []
        raw_code_texts: List[str] = []
        entries, malformed = aoci_index.parse_code_volume(
            code_path, duplicates=duplicates, raw_text_out=raw_code_texts
        )
        old_entries: Dict[str, str] = cached["entries"]
        changed = {rel: line for rel, line in entries.items() if old_entries.get(rel) != line}
        removed = [rel for rel in old_entries if rel not in entries]
        if len(changed) + len(removed) > max(self._INCREMENTAL_MIN_DELTA, int(len(entries) * self._INCREMENTAL_MAX_RATIO)):
            return None
        parsed = dict(cached["parsed"])
        for rel in removed:
            parsed.pop(rel, None)
        for rel, line in changed.items():
            pe = aoci_index.parse_entry(rel, line)
            if pe is None:
                parsed.pop(rel, None)
            else:
                parsed[rel] = pe
        graph_state = aoci_index._graph.update_fras_graph_state(
            cached["graph_state"], entries, parsed, changed, removed
        )
        if graph_state is None:
            return None
        same_paths = not removed and all(rel in old_entries for rel in changed)
        bm25 = cached.get("bm25")
        if bm25 is not None and (changed or removed):
            bm25 = bm25.updated(changed, removed, parsed)
        slot = dict(cached)
        slot.update(
            {
                "key": key,
                "entries": entries,
                "parsed": parsed,
                "malformed": malformed,
                "duplicates": duplicates,
                "deps": graph_state.deps,
                "dependents": graph_state.dependents,
                "graph_state": graph_state,
                "mention_index": (
                    cached["mention_index"] if same_paths else aoci_index.PathMentionIndex(sorted(entries.keys()))
                ),
                "bm25": bm25,
                "hybrid": {} if (changed or removed) else cached.get("hybrid", {}),
                "test_index_entry": None if (changed or removed) else cached.get("test_index_entry"),
                "estimated_tokens": self._estimate_volume_tokens(entries, raw_code_texts),
                "_delta": (len(changed), len(removed)),
            }
        )
        return slot

    def get_baseline_files(self, root: Path) -> Dict[str, Any]:
        """`baseline.json` `files`, cached by file stat (freshness checks on read results)."""
        resolved, root_key = self._canonical_root(root)
        bp = resolved / aoci_index.AOCI_DIR_NAME / aoci_index.BASELINE_FILE
        st_key = aoci_index._stat_key(bp) if bp.is_file() else None
        with self._lock:
            cached = self._baseline_slots.get(root_key)
            if cached is not None and cached[0] == st_key:
                self._baseline_slots.move_to_end(root_key)
                return cached[1]
        files = aoci_index._freshness.load_baseline_files(resolved) if st_key is not None else {}
        with self._lock:
            self._baseline_slots[root_key] = (st_key, files)
            self._baseline_slots.move_to_end(root_key)
            while len(self._baseline_slots) > self._max_slots:
                self._baseline_slots.popitem(last=False)
        return files

    def get_hybrid(self, root: Path, slot: Dict[str, Any], opts: Mapping[str, Any]) -> Any:
        """Lazily build (and cache per slot + options) a `HybridIndex` sharing the slot's BM25 index."""
        opt_key = tuple(sorted((str(k), str(v)) for k, v in opts.items()))
        slot_lock = slot.get("_lock") or self._lock
        with slot_lock:
            per_slot = slot.setdefault("hybrid", {})
            idx = per_slot.get(opt_key)
            if idx is None:
                resolved, _ = self._canonical_root(root)
                idx = aoci_index._hybrid.HybridIndex(
                    slot["entries"],
                    parsed_entries=slot["parsed"],
                    root=resolved,
                    bm25_index=self.get_bm25(slot),
                    **dict(opts),
                )
                per_slot[opt_key] = idx
            return idx

    def get_bm25(self, slot: Dict[str, Any]) -> aoci_index.BM25Index:
        slot_lock = slot.get("_lock") or self._lock
        with slot_lock:
            idx = slot.get("bm25")
            if idx is None:
                if aoci_index.BM25Index is aoci_index._graph.BM25Index:
                    idx = aoci_index.BM25Index(slot["entries"], parsed_entries=slot["parsed"])
                else:
                    idx = aoci_index.BM25Index(slot["entries"])
                slot["bm25"] = idx
            return idx

    def get_outline_files(self, root: Path) -> Optional[Dict[str, Any]]:
        resolved, root_key = self._canonical_root(root)
        cache_path = resolved / aoci_index.AOCI_DIR_NAME / aoci_index.CACHE_DIR / aoci_index.OUTLINE_CACHE
        root_lock = self._get_root_lock(root_key)
        with root_lock:
            if not cache_path.is_file():
                with self._lock:
                    self._outline_slots.pop(root_key, None)
                return None
            key = aoci_index._stat_key(cache_path)
            with self._lock:
                cached = self._outline_slots.get(root_key)
                if cached is not None and cached[0] == key:
                    self._outline_slots.move_to_end(root_key)
                    return cached[1]
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError):
                return None
            if not isinstance(loaded, dict):
                return None
            sv = loaded.get("schema_version")
            if sv is not None:
                try:
                    if int(sv) != 2:
                        return None
                except (ValueError, TypeError):
                    return None
            files_obj = loaded.get("files") if isinstance(loaded.get("files"), dict) else loaded
            if not isinstance(files_obj, dict):
                return None
            with self._lock:
                self._outline_slots[root_key] = (key, files_obj)
                self._outline_slots.move_to_end(root_key)
                while len(self._outline_slots) > self._max_slots:
                    self._outline_slots.popitem(last=False)
            return files_obj

    def get_hub_records(self, root: Path) -> Optional[List[Dict[str, Any]]]:
        resolved, root_key = self._canonical_root(root)
        hubs_path = resolved / aoci_index.AOCI_DIR_NAME / aoci_index.HUBS_FILE
        root_lock = self._get_root_lock(root_key)
        with root_lock:
            if not hubs_path.is_file():
                with self._lock:
                    self._hub_slots.pop(root_key, None)
                return None
            key = aoci_index._stat_key(hubs_path)
            with self._lock:
                cached = self._hub_slots.get(root_key)
                if cached is not None and cached[0] == key:
                    self._hub_slots.move_to_end(root_key)
                    return cached[1]
            records, _ = aoci_index._hubs.parse_hubs_volume(hubs_path)
            with self._lock:
                self._hub_slots[root_key] = (key, records)
                self._hub_slots.move_to_end(root_key)
                while len(self._hub_slots) > self._max_slots:
                    self._hub_slots.popitem(last=False)
            return records

    def get_test_index(self, root: Path, slot: Dict[str, Any]) -> Any:
        resolved, _ = self._canonical_root(root)
        cache_path = resolved / aoci_index.AOCI_DIR_NAME / aoci_index.CACHE_DIR / aoci_index.OUTLINE_CACHE
        outline_key = aoci_index._stat_key(cache_path) if cache_path.is_file() else None
        slot_lock = slot.get("_lock") or self._lock
        with slot_lock:
            cached = slot.get("test_index_entry")
            if cached is not None and cached[0] == outline_key:
                return cached[1]
            outline_files = self.get_outline_files(resolved) if outline_key is not None else {}
            idx = aoci_index._test_impact.TestImpactIndex(
                resolved,
                slot["parsed"],
                preloaded_outline_files=outline_files or {},
                live_imports=False,
            )
            slot["test_index_entry"] = (outline_key, idx)
            return idx

    def get_working_diff_briefing(
        self,
        root: Path,
        slot: Dict[str, Any],
        hub_records: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        resolved, root_key = self._canonical_root(root)
        git_info = aoci_index.detect_git_info(resolved)
        git_idx_stat: Any = None
        git_head_stat: Any = None
        if git_info.get("is_git"):
            git_idx_stat = aoci_index._stat_key(git_info.get("index_file"))
            git_head_stat = aoci_index._git_head_key(git_info)
        hubs_path = resolved / aoci_index.AOCI_DIR_NAME / aoci_index.HUBS_FILE
        hubs_stat = aoci_index._stat_key(hubs_path) if hubs_path.is_file() else None
        diff_key = (slot.get("key"), hubs_stat, git_idx_stat, git_head_stat)
        now = time.monotonic()
        root_lock = self._get_root_lock(root_key)
        with root_lock:
            with self._lock:
                cached = self._diff_slots.get(root_key)
                if cached is not None and cached[0] == diff_key and (now - cached[1]) <= self._DIFF_TTL_SECONDS:
                    self._diff_slots.move_to_end(root_key)
                    return cached[2]
            cand_diff = aoci_index.compute_diff_briefing(
                root,
                depth=1,
                cached_entries=slot["entries"],
                cached_deps=slot["deps"],
                cached_dependents=slot["dependents"],
                cached_invariants=slot["invariants"],
                cached_parsed=slot["parsed"],
                cached_hub_records=hub_records,
            )
            with self._lock:
                self._diff_slots[root_key] = (diff_key, time.monotonic(), cand_diff)
                self._diff_slots.move_to_end(root_key)
                while len(self._diff_slots) > self._max_slots:
                    self._diff_slots.popitem(last=False)
            return cand_diff


_CACHE = _MemoryIndexCache()
_CLIENT_ROOT_HINT: Optional[str] = None
_CLIENT_ROOT_HINTS: List[str] = []
_NEGOTIATED_PROTOCOL: str = DEFAULT_PROTOCOL_VERSION
_CLIENT_SUPPORTS_ROOTS: bool = False
_SUBSCRIBED_URIS: Set[str] = set()
# Guards client root hints and resource subscriptions when `handle_rpc_request` is driven from several threads
_STATE_LOCK = threading.RLock()
# Per-`tools/call` holder letting a tool hand its underlying dict to `structuredContent` when the text channel
# is rendered as Markdown / SARIF / GitHub annotations (MCP 2025-06-18: `outputSchema` => `structuredContent`)
_STRUCTURED_SIDECAR: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar(
    "aoci_structured_sidecar", default=None
)


def _publish_structured(obj: Any) -> None:
    holder = _STRUCTURED_SIDECAR.get()
    if holder is not None and isinstance(obj, dict):
        holder["structured"] = obj


def _structured_content_for(text: str, holder: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Pick `structuredContent` for an `outputSchema` tool: the dict a tool explicitly published (keeps one stable
    report shape across `json`/`markdown`/`sarif`/`github` renderings), else JSON text, else a `{"text": ...}` wrapper.
    """
    published = holder.get("structured")
    if isinstance(published, dict):
        return published
    try:
        parsed_obj = json.loads(text)
        if isinstance(parsed_obj, dict):
            return parsed_obj
    except ValueError:
        pass
    return {"text": text}
_OUTBOUND_LOCK = threading.Lock()
_OUTBOUND_QUEUE: List[Dict[str, Any]] = []
_ROOTS_REQ_SEQ = 0
MAX_CONTENT_LENGTH = 32 * 1024 * 1024


def drain_outbound_messages() -> List[Dict[str, Any]]:
    """Drain queued server-initiated notifications and requests (used by `serve_stdio` and tests)."""
    with _OUTBOUND_LOCK:
        items = list(_OUTBOUND_QUEUE)
        _OUTBOUND_QUEUE.clear()
        return items


def _enqueue_outbound(msg: Dict[str, Any]) -> None:
    with _OUTBOUND_LOCK:
        _OUTBOUND_QUEUE.append(msg)


def _enqueue_roots_list_request() -> None:
    global _ROOTS_REQ_SEQ
    with _OUTBOUND_LOCK:
        _ROOTS_REQ_SEQ += 1
        req_id = f"aoci-roots-{_ROOTS_REQ_SEQ}"
        _OUTBOUND_QUEUE.append({"jsonrpc": "2.0", "id": req_id, "method": "roots/list", "params": {}})


def update_client_roots(roots_items: Any) -> List[str]:
    """Apply a client `roots` array (`[{"uri": "file:///...", "name": "..."}]`) to multi-root hints."""
    global _CLIENT_ROOT_HINT
    if not isinstance(roots_items, list):
        with _STATE_LOCK:
            return list(_CLIENT_ROOT_HINTS)
    collected: List[str] = []
    for item in roots_items:
        uri_str = item.get("uri") if isinstance(item, dict) else (item if isinstance(item, str) else None)
        if isinstance(uri_str, str) and uri_str.strip():
            resolved = _file_uri_to_path(uri_str.strip())
            if resolved and resolved not in collected:
                collected.append(resolved)
    with _STATE_LOCK:
        if collected:
            _CLIENT_ROOT_HINTS[:] = collected
            _CLIENT_ROOT_HINT = _first_indexed_hint(collected) or collected[0]
        hints_now = list(_CLIENT_ROOT_HINTS)
    watcher = globals().get("_WATCHER")
    if collected and watcher is not None:
        watcher.reset_targets()
    return hints_now


def _notify_resource_updates(mutated_root: Path) -> None:
    """Queue `notifications/resources/updated` for any subscribed `aoci://` URIs matching `mutated_root`."""
    with _STATE_LOCK:
        subscribed = sorted(_SUBSCRIBED_URIS)
    if not subscribed:
        return
    try:
        mut_resolved = mutated_root.resolve()
    except OSError:
        mut_resolved = mutated_root
    for sub_uri in subscribed:
        parsed = urllib.parse.urlparse(sub_uri)
        qs = urllib.parse.parse_qs(parsed.query)
        raw_r: Optional[str] = (qs.get("root") or [None])[0]  # type: ignore[list-item]
        if raw_r is not None:
            try:
                sub_root = _resolve_mcp_root(raw_r).resolve()
            except OSError:
                sub_root = _resolve_mcp_root(raw_r)
            if sub_root != mut_resolved:
                continue
        _enqueue_outbound(
            {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": sub_uri},
            }
        )


def _on_root_mutated(root: Path, ok: bool = True) -> None:
    _CACHE.invalidate(root)
    if ok:
        _notify_resource_updates(root)
    watcher = globals().get("_WATCHER")
    if watcher is not None:
        watcher.resync_root(root)


def _file_uri_to_path(uri: str, require_dir: bool = True) -> Optional[str]:
    if not uri:
        return None
    if not uri.startswith("file://"):
        if not require_dir:
            return uri
        return uri if Path(uri).is_dir() else None
    try:
        parsed = urllib.parse.urlparse(uri)
        unquoted_path = urllib.parse.unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:(?:/|$)", unquoted_path):
            raw_path = unquoted_path[1:]
            if os.name == "nt":
                raw_path = os.path.normpath(raw_path)
        else:
            raw_path = urllib.request.url2pathname(unquoted_path)
            if re.match(r"^[\\/][A-Za-z]:(?:[\\/]|$)", raw_path):
                raw_path = raw_path[1:]
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            if os.name == "nt" and not raw_path.startswith(("\\\\", "//")):
                sep_path = raw_path if raw_path.startswith(("\\", "/")) else f"\\{raw_path}"
                raw_path = f"\\\\{parsed.netloc}{sep_path}"
            elif not raw_path.startswith("//"):
                raw_path = f"//{parsed.netloc}{raw_path}"
        if not raw_path:
            return None
        if require_dir and not Path(raw_path).is_dir():
            return None
        return raw_path
    except Exception:
        return None


def _first_indexed_hint(hints: List[str]) -> Optional[str]:
    """First hint that already holds an index (used when a client announces new roots, never a stale default)."""
    for cand_str in hints:
        if (Path(cand_str) / aoci_index.AOCI_DIR_NAME / aoci_index.CODE_FILE).is_file():
            return cand_str
    return None


def _select_default_root_hint() -> Optional[str]:
    with _STATE_LOCK:
        hints = list(_CLIENT_ROOT_HINTS)
        fallback = _CLIENT_ROOT_HINT
    for cand_str in hints:
        cand = Path(cand_str)
        if (cand / aoci_index.AOCI_DIR_NAME / aoci_index.CODE_FILE).is_file():
            return cand_str
    return fallback


def _resolve_mcp_root(raw_root: Optional[Any], *, for_init: bool = False) -> Path:
    chosen = str(raw_root).strip() if (raw_root is not None and str(raw_root).strip()) else None
    if chosen is None:
        hint = _select_default_root_hint()
        return aoci_index.resolve_root(hint if hint else None)
    explicit = aoci_index.resolve_root(chosen)
    if for_init or (explicit / aoci_index.AOCI_DIR_NAME / aoci_index.CODE_FILE).is_file():
        return explicit
    # If caller passed a nested subdirectory inside an indexed repository, climb up to `.git` boundary
    if explicit.is_dir() and not (explicit / ".git").exists():
        for parent in explicit.parents:
            if (parent / aoci_index.AOCI_DIR_NAME / aoci_index.CODE_FILE).is_file():
                return parent
            if (parent / ".git").exists():
                break
    return explicit


_READ_ONLY_ANNOTATIONS: Dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_MUTATE_IDEMPOTENT_ANNOTATIONS: Dict[str, bool] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_STRUCTURED_OBJECT_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


_HYBRID_INPUT_PROPS: Dict[str, Any] = {
    "hybrid": {
        "type": "boolean",
        "default": False,
        "description": "Fuse BM25 with a char-trigram channel (+ optional dense embeddings) via RRF",
    },
    "embed_backend": {
        "type": "string",
        "enum": ["none", "model2vec"],
        "description": "Optional dense channel for `hybrid` (default: $AOCI_EMBED_BACKEND or none)",
    },
    "embed_model": {"type": "string", "description": "Dense model name for `embed_backend`"},
}


TOOLS_SPEC: List[Dict[str, Any]] = [
    {
        "name": "aoci_init",
        "description": "Initialize a `.aoci/` index repository and enforce local `.git/info/exclude`.",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "project": {"type": "string", "description": "Optional project name for `aoci.txt`"},
                "locale": {"type": "string", "default": "en-US", "description": "Locale tag (default: en-US)"},
                "force": {"type": "boolean", "default": False, "description": "Overwrite existing template files"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_overview",
        "description": (
            "Read a compact global architectural digest of a `.aoci/` indexed repository "
            "(project invariants, domain dictionary, directory distribution, and high-importance hubs)."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "min_importance": {"type": "integer", "default": 7, "description": "Minimum Importance (1-9) for core hubs"},
                "max_core": {"type": "integer", "default": 50, "description": "Maximum core entries to return"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_query",
        "description": (
            "Search or filter `.aoci/aoci.code.txt` entries in memory using cached BM25 + Importance ranking (`search`), "
            "substring `keyword`, `layer`, `domain`, `path` glob, or transitive `impact` dependents."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "search": {"type": "string", "description": "Multi-term BM25 + Importance ranked search query"},
                "keyword": {"type": "string", "description": "Exact case-insensitive substring filter"},
                "layer": {"type": "string", "description": "Layer code(s), e.g. 'M' or 'E,A,M'"},
                "domain": {"type": "string", "description": "Domain code(s), e.g. 'O' or 'A,O'"},
                "min_importance": {"type": "integer", "description": "Minimum Importance rating (1-9)"},
                "path": {"type": "string", "description": "Glob pattern on relative path (e.g. 'src/auth/*')"},
                "impact": {"type": "string", "description": "Target file to trace transitive upstream dependents"},
                "tests_for": {"type": "string", "description": "Target file to find impacted T-layer test files"},
                "live_test_imports": {
                    "type": "boolean",
                    "default": False,
                    "description": "Read uncached T-layer test imports in memory when outline cache misses",
                },
                "depth": {"type": "integer", "default": 1, "description": "Transitive hops for `impact` (default 1) or `tests_for` (default 3)"},
                "top": {"type": "integer", "default": 20, "description": "Maximum entries to return (0 = unlimited)"},
                **_HYBRID_INPUT_PROPS,
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_context",
        "description": (
            "Expand the directed architectural subgraph for one or more target files: "
            "Global Invariants + target FRAS entries + R: dependencies + upstream A: callers."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Target relative file paths"},
                "depth": {"type": "integer", "default": 1, "description": "Graph expansion hops"},
                "budget": {"type": "integer", "default": 0, "description": "Optional token budget ceiling (0 = unlimited)"},
                "with_tests": {"type": "boolean", "default": False, "description": "Attach impacted T-layer test files"},
                "live_test_imports": {
                    "type": "boolean",
                    "default": False,
                    "description": "Read uncached T-layer test imports in memory when outline cache misses",
                },
            },
            "required": ["root", "paths"],
        },
    },
    {
        "name": "aoci_diff",
        "description": (
            "Generate an architectural impact briefing (changed FRAS entries, non-obvious S: traps, "
            "downstream R: dependency constraints, unindexed files, and upstream callers) for working-tree or Git revision diffs."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "rev": {"type": "string", "description": "Optional Git revision spec (e.g. 'main...HEAD')"},
                "staged": {"type": "boolean", "default": False, "description": "Inspect staged (`--cached`) changes"},
                "depth": {"type": "integer", "default": 1, "description": "Upstream caller hops"},
                "with_tests": {"type": "boolean", "default": False, "description": "Attach impacted T-layer test files"},
                "live_test_imports": {
                    "type": "boolean",
                    "default": False,
                    "description": "Read uncached T-layer test imports in memory when outline cache misses",
                },
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_scan",
        "description": "Detect `.aoci/` index drift (added/modified/renamed/similar_renames/deleted) and affected neighbors.",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "fast": {"type": "boolean", "default": True, "description": "Use cached (mtime, size) fast scan"},
                "git_fast": {"type": "boolean", "default": False, "description": "Use cached Git index + `git status` incremental discovery"},
                "auto_restore": {"type": "boolean", "default": False, "description": "Auto-restore cached entries first"},
                "check_only": {"type": "boolean", "default": False, "description": "Read-only scan without auto-repairing `.git/info/exclude`"},
                "fail_on_drift": {"type": "boolean", "default": False, "description": "Return error status if any drift or Git exclusion issue exists"},
                "batch_size": {"type": "integer", "default": 0, "description": "Optional batch size for pending files"},
                "max_batch_lines": {"type": "integer", "description": "Maximum line budget per batch"},
                "reset_batches": {"type": "boolean", "default": False, "description": "Rebuild saved batch_plan.json from scratch"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_outline",
        "description": "Extract static imports and top-level symbols/routes to assist R:/A: authoring alongside reading code.",
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Relative file paths to outline"},
                "max_items": {"type": "integer", "default": 30, "description": "Max imports/symbols per file"},
            },
            "required": ["root", "paths"],
        },
    },
    {
        "name": "aoci_scaffold",
        "description": "Pre-fill `[LDIS]`, resolved internal `R:` imports, and repo-wide `A:` callers (`<TODO>` F:/S: require reading source).",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Specific relative paths to scaffold"},
                "unindexed": {"type": "boolean", "default": False, "description": "Scaffold all pending/unindexed files"},
                "batch": {"type": "integer", "default": 0, "description": "1-indexed pending batch number"},
                "batch_size": {"type": "integer", "description": "Batch size when `batch` > 0 (inherits active plan if omitted)"},
                "max_batch_lines": {"type": "integer", "description": "Maximum line budget per batch"},
                "reset_batches": {"type": "boolean", "default": False, "description": "Rebuild saved batch_plan.json before selecting batch"},
                "output": {"type": "string", "description": "Optional output file path relative to `root` (e.g. `.aoci/pending/batch_01.txt`)"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_ack",
        "description": "Re-baseline modified files whose existing FRAS entries remain accurate (auto-refreshes Size tier).",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Relative file paths to ack"},
                "all_modified": {"type": "boolean", "default": False, "description": "Acknowledge every currently modified file (requires `reviewed: true`)"},
                "impl_only": {"type": "boolean", "default": False, "description": "Acknowledge only modified files with unchanged interface `sig_hash` (requires `reviewed: true` unless `dry_run`)"},
                "reviewed": {"type": "boolean", "default": False, "description": "Confirm every modified file was read and verified accurate"},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without writing"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_upsert",
        "description": "Validate and atomically merge `object-fras-v2` lines into `.aoci/aoci.code.txt` and `baseline.json`.",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "entries": {"type": "array", "items": {"type": "string"}, "description": "object-fras-v2 entry lines"},
                "entries_file": {"type": "string", "description": "Optional path to a batch file containing object-fras-v2 lines"},
                "pending_dir": {
                    "type": "string",
                    "description": "Optional directory of `*.txt` subagent batch files to validate and merge atomically",
                },
                "clean_pending": {
                    "type": "boolean",
                    "default": False,
                    "description": "Remove consumed `*.txt` files in `pending_dir` after a successful merge",
                },
                "prune_only": {"type": "boolean", "default": False, "description": "Only prune deleted files"},
                "no_prune": {"type": "boolean", "default": False, "description": "Skip pruning deleted files"},
                "strict_size": {"type": "boolean", "default": False, "description": "Fail instead of auto-fixing stale Size tier"},
                "drop_malformed": {"type": "boolean", "default": False, "description": "Allow dropping pre-existing malformed lines"},
                "dry_run": {"type": "boolean", "default": False, "description": "Validate without writing to disk"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_migrate_renames",
        "description": "Migrate unambiguous exact (and moved+edited) renames in `aoci.code.txt` and rewrite R:/A: cross-references.",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "exact_only": {"type": "boolean", "default": False, "description": "Skip moved+edited similar renames"},
                "no_rewrite_refs": {"type": "boolean", "default": False, "description": "Skip rewriting R:/A: path references"},
                "drop_malformed": {"type": "boolean", "default": False, "description": "Allow dropping pre-existing malformed lines"},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without writing"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_restore",
        "description": "Zero-token restore of FRAS entries from `<git-common-dir>/aoci-cache` after branch switches (exact path+hash by default).",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "allow_sha_fallback": {
                    "type": "boolean",
                    "default": False,
                    "description": "Opt in to content-hash fallback for unambiguous moved paths whose origin no longer exists",
                },
                "exact_path_only": {"type": "boolean", "default": False, "description": "Legacy alias (exact path+hash is already default)"},
                "no_prune": {"type": "boolean", "default": False, "description": "Skip pruning deleted files"},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without writing"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_shard",
        "description": "Split `.aoci/aoci.code.txt` into per-directory `.aoci/shards/<dir>.code.txt` slices (or remove with `clean: true`).",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "depth": {"type": "integer", "default": 1, "description": "Directory prefix depth for shard grouping"},
                "clean": {"type": "boolean", "default": False, "description": "Disable sharding and remove `.aoci/shards/`"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_ensure_git_exclude",
        "description": "Idempotently repair `.git/info/exclude` to cover `.aoci/` and sync `#Git-Excluded` in `aoci.txt`.",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_lint",
        "description": "Run FRAS quality and [LDIS] tag/size checks (vague F, token budget, hub S:-, dangling refs, duplicates).",
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "code": {"type": "string", "description": "Optional comma-separated finding codes filter"},
                "strict": {"type": "boolean", "default": False, "description": "Return error status if any finding exists"},
                "limit": {"type": "integer", "default": 100, "description": "Maximum findings to report"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_verify",
        "description": "Run the full `.aoci/` verification gate (Git exclusion + format + tags/size + domain check + zero drift).",
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "fast": {"type": "boolean", "default": False, "description": "Use stat shortcut for non-racy files"},
                "git_fast": {"type": "boolean", "default": False, "description": "Use cached Git index + `git status` incremental discovery"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_audit",
        "description": (
            "Audit `.aoci/aoci.code.txt` against static imports (missing-static-dep / phantom-fras-dep), "
            "dependency cycles (Tarjan SCC), forbidden layer redlines, and optional Git co-change coupling."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "forbid": {"type": "string", "description": "Optional extra layer rules, e.g. 'D!->E,U; P!->E'"},
                "use_default_rules": {"type": "boolean", "default": True, "description": "Apply built-in layer redline defaults"},
                "check_static": {"type": "boolean", "default": True, "description": "Cross-check static imports vs FRAS"},
                "check_cycles": {"type": "boolean", "default": True, "description": "Detect dependency cycles"},
                "check_layers": {"type": "boolean", "default": True, "description": "Check forbidden layer transitions"},
                "check_co_change": {"type": "boolean", "default": False, "description": "Mine Git history for unlinked co-changing pairs"},
                "co_change_commits": {"type": "integer", "default": 200, "description": "Max commits scanned when check_co_change=true"},
                "min_co_changes": {"type": "integer", "default": 3, "description": "Minimum co-change commit threshold"},
                "min_co_confidence": {"type": "number", "default": 0.6, "description": "Minimum co-change confidence in [0,1]"},
                "exclude_tests": {"type": "boolean", "default": False, "description": "Exclude T-layer test files from co-change analysis"},
                "strict": {"type": "boolean", "default": False, "description": "Return error status if any audit finding exists"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_export",
        "description": "Export filtered or aggregated FRAS dependency graph in Mermaid, Graphviz DOT, or standalone HTML format.",
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "format": {"type": "string", "enum": ["mermaid", "dot", "html"], "default": "mermaid", "description": "Graph output format"},
                "level": {"type": "string", "enum": ["file", "dir", "domain"], "default": "file", "description": "Node aggregation level"},
                "group_by": {"type": "string", "enum": ["domain", "dir", "layer", "none"], "default": "domain", "description": "Subgraph grouping when level=file"},
                "direction": {"type": "string", "enum": ["LR", "TD", "TB"], "default": "LR", "description": "Layout direction"},
                "layer": {"type": "string", "description": "Optional Layer code(s) filter"},
                "domain": {"type": "string", "description": "Optional Domain code(s) filter"},
                "min_importance": {"type": "integer", "description": "Optional minimum Importance (1-9)"},
                "path": {"type": "string", "description": "Optional path glob filter"},
                "focus": {"type": "string", "description": "Optional file path to extract neighborhood subgraph"},
                "depth": {"type": "integer", "default": 1, "description": "Hops around `focus`"},
                "diff": {"type": "boolean", "default": False, "description": "Highlight working-tree changed nodes and affected 1-hop callers"},
                "diff_rev": {"type": "string", "description": "Optional Git revision (e.g. `origin/main`) to highlight changed/affected nodes against"},
                "output": {"type": "string", "description": "Optional output file path (relative to `root` or absolute) to write the exported graph"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_pool",
        "description": "Inspect SQLite WAL semantic cache pool stats, or export/import portable `aoci-semantic-pool-v1` JSON(.gz) bundles.",
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "action": {"type": "string", "enum": ["stats", "export", "import"], "default": "stats", "description": "Pool action"},
                "file": {"type": "string", "description": "Bundle file path for `export` or `import` (relative to `root` or absolute)"},
                "pool_only": {"type": "boolean", "default": False, "description": "For `export`: exclude live `aoci.code.txt` entries"},
                "dry_run": {"type": "boolean", "default": False, "description": "For `import`: validate without writing"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_pack",
        "description": (
            "Build a task-driven, token-budgeted context packet: BM25 + bilingual glossary + path-mention + diff seeds, "
            "R:/A: graph neighborhood, S: critical constraints, and prioritized open_next files with cached symbol anchors."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "task": {"type": "string", "description": "Task description or query to pack context for"},
                "include_diff": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include Git working-tree diff signals when inside a Git repository",
                },
                "depth": {"type": "integer", "default": 1, "description": "Graph expansion hops around seeds"},
                "budget": {
                    "type": "integer",
                    "default": 2500,
                    "description": "Token budget ceiling (0 = unlimited)",
                },
                "top_seeds": {"type": "integer", "default": 8, "description": "Maximum BM25 seed hits"},
                **_HYBRID_INPUT_PROPS,
                "with_tests": {
                    "type": "boolean",
                    "default": False,
                    "description": "Attach impacted T-layer test files for packed seeds",
                },
                "live_test_imports": {
                    "type": "boolean",
                    "default": False,
                    "description": "Read uncached T-layer test imports in memory when outline cache misses",
                },
                "live_anchors": {
                    "type": "boolean",
                    "default": False,
                    "description": "Extract live symbol anchors in memory when outline cache misses",
                },
            },
            "required": ["root", "task"],
        },
    },
    {
        "name": "aoci_zoom",
        "description": (
            "Read, scaffold, or upsert symbol-level H-tier sections in `.aoci/aoci.hubs.txt` "
            "(validates line spans on disk and rejects `<TODO>` placeholders)."
        ),
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "mode": {
                    "type": "string",
                    "enum": ["read", "scaffold", "upsert"],
                    "default": "read",
                    "description": "Zoom mode: read existing hub/code/spans, scaffold skeletons, or upsert hub entries",
                },
                "path": {"type": "string", "description": "Target relative file path (required for `read` and `scaffold`)"},
                "entries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hub entry lines (`<relpath>#<symbol>[Lstart-Lend]: F:... | S:...`) for `upsert`",
                },
                "entries_file": {"type": "string", "description": "Optional batch file containing hub entry lines for `upsert`"},
                "output": {"type": "string", "description": "Optional output file path relative to `root` for `scaffold`"},
                "prune_missing": {"type": "boolean", "default": False, "description": "Remove missing files/symbols during `upsert`"},
                "dry_run": {"type": "boolean", "default": False, "description": "Validate `upsert` without writing to disk"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_prepare_sync",
        "description": (
            "Collect a single-pass drift synchronization briefing: pool restorable count, auto-generated scaffolds "
            "for added files, previous entries + outlines + bounded git diffs for contract/unknown modifications, "
            "1-hop affected neighbors, and ordered recommended actions."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "fast": {
                    "type": "boolean",
                    "default": True,
                    "description": "Use mtime_ns/size cache for unchanged files",
                },
                "max_diff_lines": {
                    "type": "integer",
                    "default": 40,
                    "description": "Maximum git diff lines attached per contract/unknown modified file",
                },
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_apply_sync",
        "description": (
            "Execute ordered index synchronization in one call "
            "(restore -> migrate-renames -> ack -> upsert -> verify)."
        ),
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "restore": {"type": "boolean", "default": False, "description": "Run semantic pool restore (`--no-prune`) first"},
                "allow_sha_fallback": {"type": "boolean", "default": False, "description": "Allow content-hash fallback in `restore`"},
                "migrate_renames": {"type": "boolean", "default": False, "description": "Migrate exact and similar renames"},
                "exact_only": {"type": "boolean", "default": False, "description": "Only migrate exact byte-identical renames"},
                "no_rewrite_refs": {"type": "boolean", "default": False, "description": "Skip rewriting R:/A: path references in `migrate_renames`"},
                "ack_paths": {"type": "array", "items": {"type": "string"}, "description": "Modified paths to acknowledge (requires `reviewed: true` unless `dry_run`)"},
                "ack_impl_only": {"type": "boolean", "default": False, "description": "Acknowledge `impl_only` modified files (requires `reviewed: true` unless `dry_run`)"},
                "reviewed": {"type": "boolean", "default": False, "description": "Confirm acknowledged files were read and verified"},
                "entries": {"type": "array", "items": {"type": "string"}, "description": "object-fras-v2 entry lines for `upsert`"},
                "entries_file": {"type": "string", "description": "Optional batch file of object-fras-v2 lines for `upsert`"},
                "pending_dir": {
                    "type": "string",
                    "description": "Optional directory of `*.txt` subagent batch files to validate and merge in `upsert`",
                },
                "clean_pending": {
                    "type": "boolean",
                    "default": False,
                    "description": "Remove consumed `*.txt` files in `pending_dir` after a successful `upsert`",
                },
                "prune_only": {"type": "boolean", "default": False, "description": "Only prune deleted files in `upsert`"},
                "no_prune": {"type": "boolean", "default": False, "description": "Skip pruning deleted files in `upsert`"},
                "strict_size": {"type": "boolean", "default": False, "description": "Fail instead of auto-fixing Size tier in `upsert`"},
                "drop_malformed": {"type": "boolean", "default": False, "description": "Allow dropping pre-existing malformed lines"},
                "verify": {"type": "boolean", "default": True, "description": "Run trailing `verify` check (default: true)"},
                "full_verify": {"type": "boolean", "default": False, "description": "Run full SHA-256 `verify` instead of `--fast`"},
                "max_bytes": {"type": "integer", "description": "Override max source file size in bytes across sync stages"},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview mutating stages without writing to disk"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_symbol",
        "description": (
            "Search symbols across `.aoci/aoci.hubs.txt`, outline cache (or opt-in read-only `live` extraction), "
            "and FRAS `A:` exports, returning spans, signatures, and FRAS graph dependencies/callers."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "name": {"type": "string", "description": "Symbol name or substring to look up"},
                "exact": {"type": "boolean", "default": False, "description": "Restrict to exact / qualified suffix matches"},
                "depth": {"type": "integer", "default": 1, "description": "FRAS neighbor hops per matched file"},
                "limit": {"type": "integer", "default": 20, "description": "Maximum matches to return (0 = unlimited)"},
                "live": {"type": "boolean", "default": False, "description": "Read-only live outline fallback for files absent from outline cache"},
            },
            "required": ["root", "name"],
        },
    },
    {
        "name": "aoci_install_git_hooks",
        "description": (
            "Install or remove idempotent local Git hooks (`post-checkout`/`post-merge`/`post-rewrite` "
            "auto-restore + optional `pre-commit` local exclusion guard)."
        ),
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "python_cmd": {"type": "string", "description": "Optional Python executable for hook invocations"},
                "with_pre_commit": {"type": "boolean", "default": False, "description": "Also install a `pre-commit` guard rejecting staged `.aoci/` files"},
                "pre_commit_verify": {"type": "boolean", "default": False, "description": "With `with_pre_commit`: also run `verify --fast` in `pre-commit`"},
                "uninstall": {"type": "boolean", "default": False, "description": "Remove managed AOCI Git hook blocks"},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview hook changes without modifying files"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_batch_pack",
        "description": (
            "Assemble a self-contained subagent briefing work-pack for 1-indexed batch `batch` "
            "(FRAS scaffolds + outlines + invariants + domains + optional bounded source excerpts)."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "outputSchema": _STRUCTURED_OBJECT_OUTPUT_SCHEMA,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "batch": {"type": "integer", "description": "1-indexed pending batch number"},
                "batch_size": {"type": "integer", "description": "Files per batch (inherits active batch_plan.json if omitted)"},
                "max_batch_lines": {"type": "integer", "description": "Maximum line budget per batch"},
                "reset_batches": {"type": "boolean", "default": False, "description": "Rebuild saved batch_plan.json before packing"},
                "with_source": {"type": "boolean", "default": False, "description": "Embed bounded source code excerpts per file"},
                "max_source_lines": {"type": "integer", "default": 200, "description": "Max source lines per file when `with_source` is true"},
                "format": {"type": "string", "enum": ["json", "markdown"], "default": "json", "description": "Return structured JSON or Markdown prompt"},
                "output": {"type": "string", "description": "Optional output file path relative to `root`"},
            },
            "required": ["root", "batch"],
        },
    },
    {
        "name": "aoci_ci_check",
        "description": (
            "Run CI-native architectural guard (layer redlines, Tarjan dependency cycles, static vs FRAS check, "
            "PR diff impact & test derivation, and optional SARIF 2.1.0 / GitHub Actions / Markdown rendering)."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "outputSchema": _STRUCTURED_OBJECT_OUTPUT_SCHEMA,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "format": {
                    "type": "string",
                    "enum": ["json", "sarif", "github", "markdown", "text"],
                    "default": "json",
                    "description": "Output format (default: json)",
                },
                "base_rev": {"type": "string", "description": "Optional base Git revision (e.g. `origin/main`) for PR diff impact"},
                "pool_bundle": {"type": "string", "description": "Optional exported semantic pool bundle (`.json` or `.json.gz`)"},
                "forbid": {"type": "string", "description": "Optional layer redline rules (e.g. `D!->E,U; P!->E`)"},
                "use_default_rules": {"type": "boolean", "default": True, "description": "Apply built-in layer redlines when none declared"},
                "check_static": {"type": "boolean", "default": True, "description": "Cross-check static imports vs FRAS"},
                "check_cycles": {"type": "boolean", "default": True, "description": "Detect circular dependency cycles"},
                "check_layers": {"type": "boolean", "default": True, "description": "Validate layer dependency rules"},
                "check_drift": {"type": "boolean", "default": False, "description": "Also report unsynced `.aoci/` drift when initialized"},
                "exclude_tests": {"type": "boolean", "default": False, "description": "Exclude T-layer test files from layer/cycle checks"},
                "sarif_output": {"type": "string", "description": "Optional path to write SARIF 2.1.0 output"},
                "markdown_output": {"type": "string", "description": "Optional path to write PR Markdown summary"},
                "strict": {"type": "boolean", "default": False, "description": "Return `isError=True` when any finding exists"},
            },
            "required": ["root"],
        },
    },
    {
        "name": "aoci_refs",
        "description": (
            "Locate exact line-level cross-file symbol references and call sites by pruning the search space "
            "with the FRAS dependency graph, cached static imports, and T-layer test stems."
        ),
        "annotations": _READ_ONLY_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "symbol": {"type": "string", "description": "Symbol identifier (or `<path>#<symbol>` / `Class.method`) to search"},
                "path": {"type": "string", "description": "Optional definition file path filter"},
                "depth": {"type": "integer", "default": 2, "description": "FRAS caller expansion hops for candidate pruning"},
                "all_files": {"type": "boolean", "default": False, "description": "Scan all indexed files instead of FRAS-pruned candidates"},
                "include_defs": {"type": "boolean", "default": False, "description": "Include symbol definition lines in results"},
                "limit": {"type": "integer", "default": 50, "description": "Maximum references to return (0 = unlimited)"},
            },
            "required": ["root", "symbol"],
        },
    },
    {
        "name": "aoci_doctor",
        "description": (
            "Zero-token mechanical `.aoci/` self-healer: deduplicates entries, refreshes `[LDIS]` Size tiers, "
            "prunes dangling deleted-file refs in `R:`/`A:`, optionally syncs confirmed internal static imports "
            "into `R:`, and repairs `aoci.hubs.txt` spans/registration and shards without fabricating F:/S:."
        ),
        "annotations": _MUTATE_IDEMPOTENT_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Target repository root path"},
                "sync_static_r": {"type": "boolean", "default": False, "description": "Append confirmed internal static imports into `R:`"},
                "drop_malformed": {"type": "boolean", "default": False, "description": "Allow discarding pre-existing malformed lines"},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview mechanical repairs without modifying disk"},
            },
            "required": ["root"],
        },
    },
]

_TOOL_OUTPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "aoci_overview": {
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "project": {"type": "string"},
            "git_excluded": {"type": "string"},
            "global_invariants": {"type": "string"},
            "indexed_files": {"type": "integer"},
            "estimated_full_tokens": {"type": "integer"},
            "domains": {"type": "object", "additionalProperties": {"type": "string"}},
            "layers": {"type": "object", "additionalProperties": {"type": "integer"}},
            "top_dirs": {"type": "object", "additionalProperties": {"type": "integer"}},
            "core_entries": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    },
    "aoci_query": {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "entries": {"type": "array", "items": {"type": "string"}},
            "scores": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "impacted_tests": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
        "additionalProperties": True,
    },
    "aoci_context": {
        "type": "object",
        "properties": {
            "global_invariants": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "dependencies": {"type": "array", "items": {"type": "string"}},
            "dependents": {"type": "array", "items": {"type": "string"}},
            "unindexed_targets": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    },
    "aoci_diff": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "changed_entries": {"type": "array", "items": {"type": "string"}},
            "affected_neighbors": {"type": "array", "items": {"type": "string"}},
            "high_risk_constraints": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "unindexed_changed": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    },
    "aoci_pack": {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "budget": {"type": "integer"},
            "used_tokens": {"type": "integer"},
            "seeds": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "open_next": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
        "additionalProperties": True,
    },
    "aoci_scan": {
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "has_drift": {"type": "boolean"},
            "drift": {"type": "object", "additionalProperties": True},
            "git": {"type": "object", "additionalProperties": True},
        },
        "additionalProperties": True,
    },
    "aoci_verify": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "indexed_files": {"type": "integer"},
            "issues": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    },
    "aoci_lint": {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "total_findings": {"type": "integer"},
        },
        "additionalProperties": True,
    },
    "aoci_audit": {
        "type": "object",
        "properties": {
            "total_findings": {"type": "integer"},
            "missing_static_deps": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "phantom_fras_deps": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "cycles": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "layer_violations": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
        "additionalProperties": True,
    },
    "aoci_ci_check": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "mode": {"type": "string"},
            "indexed_files": {"type": "integer"},
            "total_findings": {"type": "integer"},
            "summary": {"type": "object", "additionalProperties": True},
        },
        "additionalProperties": True,
    },
    "aoci_apply_sync": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "root": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
        "additionalProperties": True,
    },
    "aoci_symbol": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "query": {"type": "string"},
            "count": {"type": "integer"},
            "matches": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
        "additionalProperties": True,
    },
    "aoci_refs": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "query": {"type": "string"},
            "symbol": {"type": "string"},
            "mode": {"type": "string"},
            "candidate_files_searched": {"type": "integer"},
            "total_indexed_files": {"type": "integer"},
            "total_references": {"type": "integer"},
            "definitions": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "references": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
        "additionalProperties": True,
    },
    "aoci_doctor": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "total_fixes": {"type": "integer"},
            "deduplicated": {"type": "array", "items": {"type": "string"}},
            "size_refreshed": {"type": "array", "items": {"type": "string"}},
            "dangling_refs_pruned": {"type": "object", "additionalProperties": True},
            "static_deps_added": {"type": "object", "additionalProperties": True},
        },
        "additionalProperties": True,
    },
}

_STRUCTURED_TOOL_NAMES = {t["name"] for t in TOOLS_SPEC}
for _t_entry in TOOLS_SPEC:
    if _t_entry["name"] in _TOOL_OUTPUT_SCHEMAS:
        _t_entry["outputSchema"] = _TOOL_OUTPUT_SCHEMAS[_t_entry["name"]]
    elif _t_entry["name"] in _STRUCTURED_TOOL_NAMES:
        _t_entry.setdefault("outputSchema", _STRUCTURED_OBJECT_OUTPUT_SCHEMA)
_TOOL_BY_NAME: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOLS_SPEC}


RESOURCES_SPEC: List[Dict[str, Any]] = [
    {
        "uri": "aoci://manifest",
        "name": "AOCI Root Manifest",
        "description": "Root `.aoci/aoci.txt` manifest (#Project, #Global-Invariants, volume layout).",
        "mimeType": "text/plain",
    },
    {
        "uri": "aoci://meta",
        "name": "AOCI Meta Volume",
        "description": "Tag dictionary [LDIS] and FRAS authoring rules (`.aoci/aoci.meta.txt`).",
        "mimeType": "text/plain",
    },
    {
        "uri": "aoci://code",
        "name": "AOCI Code Volume",
        "description": "Full object-fras-v2 code map (`.aoci/aoci.code.txt`).",
        "mimeType": "text/plain",
    },
    {
        "uri": "aoci://hubs",
        "name": "AOCI Hubs Volume",
        "description": "Symbol-level H-tier sub-volume (`.aoci/aoci.hubs.txt`).",
        "mimeType": "text/plain",
    },
    {
        "uri": "aoci://shards/INDEX",
        "name": "AOCI Shard Index",
        "description": "Per-directory shard index (`.aoci/shards/INDEX.txt`) for large repositories.",
        "mimeType": "text/plain",
    },
]

RESOURCE_TEMPLATES_SPEC: List[Dict[str, Any]] = [
    {
        "uriTemplate": "aoci://shards/{shard}",
        "name": "AOCI Directory Shard",
        "description": "Per-directory shard file (`.aoci/shards/<shard>.code.txt`).",
        "mimeType": "text/plain",
    },
    {
        "uriTemplate": "aoci://{volume}?root={root}",
        "name": "AOCI Volume for Specific Root",
        "description": "Read a specific AOCI volume (`manifest`, `meta`, `code`, `hubs`, `shards/INDEX`, `shards/<shard>`) for an explicit `?root=` path.",
        "mimeType": "text/plain",
    },
]


PROMPTS_SPEC: List[Dict[str, Any]] = [
    {
        "name": "aoci-bootstrap",
        "description": "Initialize and populate a complete `.aoci/` index repository for a codebase.",
        "arguments": [
            {"name": "root", "description": "Repository root path", "required": False},
        ],
    },
    {
        "name": "aoci-review-diff",
        "description": "Generate and review an AOCI architectural diff impact briefing (traps + downstream deps + callers).",
        "arguments": [
            {"name": "root", "description": "Repository root path", "required": False},
            {"name": "rev", "description": "Optional Git revision range (e.g. main...HEAD)", "required": False},
        ],
    },
    {
        "name": "aoci-sync",
        "description": "Detect `.aoci/` drift, migrate renames, update modified/added FRAS entries, and verify clean.",
        "arguments": [
            {"name": "root", "description": "Repository root path", "required": False},
        ],
    },
]


_STDOUT_BUF_VAR: "contextvars.ContextVar[Optional[io.StringIO]]" = contextvars.ContextVar(
    "aoci_mcp_stdout_buf", default=None
)
_STDERR_BUF_VAR: "contextvars.ContextVar[Optional[io.StringIO]]" = contextvars.ContextVar(
    "aoci_mcp_stderr_buf", default=None
)
_STDIN_BUF_VAR: "contextvars.ContextVar[Optional[io.StringIO]]" = contextvars.ContextVar(
    "aoci_mcp_stdin_buf", default=None
)
_ROUTER_INIT_LOCK = threading.Lock()


class _StreamRouter:
    """Thread-safe stream proxy routing writes to per-call ContextVar StringIO buffers when active."""

    def __init__(
        self,
        var: "contextvars.ContextVar[Optional[io.StringIO]]",
        fallback: Any,
    ) -> None:
        self._var = var
        self._fallback = fallback
        self._fb_lock = threading.Lock()

    def set_fallback(self, new_fallback: Any) -> Any:
        with self._fb_lock:
            prev = self._fallback
            self._fallback = new_fallback
            return prev

    def _target(self) -> Any:
        buf = self._var.get()
        if buf is not None:
            return buf
        with self._fb_lock:
            return self._fallback

    def write(self, s: str) -> int:
        return self._target().write(s)

    def writelines(self, lines: Any) -> None:
        self._target().writelines(lines)

    def flush(self) -> None:
        self._target().flush()

    def isatty(self) -> bool:
        return bool(getattr(self._target(), "isatty", lambda: False)())

    def reconfigure(self, *args: Any, **kwargs: Any) -> None:
        if self._var.get() is not None:
            return
        with self._fb_lock:
            fb = self._fallback
        fn = getattr(fb, "reconfigure", None)
        if callable(fn):
            fn(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        with self._fb_lock:
            fb = self._fallback
        return getattr(fb, name)


class _StdinRouter:
    """Thread-safe stdin proxy returning empty input during in-process CLI capture."""

    def __init__(
        self,
        var: "contextvars.ContextVar[Optional[io.StringIO]]",
        fallback: Any,
    ) -> None:
        self._var = var
        self._fallback = fallback

    def _target(self) -> Any:
        buf = self._var.get()
        return buf if buf is not None else self._fallback

    def read(self, *args: Any) -> str:
        return self._target().read(*args)

    def readline(self, *args: Any) -> str:
        return self._target().readline(*args)

    def readlines(self, *args: Any) -> List[str]:
        return self._target().readlines(*args)

    def __iter__(self) -> Any:
        return iter(self._target())

    def reconfigure(self, *args: Any, **kwargs: Any) -> None:
        if self._var.get() is not None:
            return
        fn = getattr(self._fallback, "reconfigure", None)
        if callable(fn):
            fn(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fallback, name)


def _ensure_stream_routers() -> None:
    if (
        isinstance(sys.stdout, _StreamRouter)
        and isinstance(sys.stderr, _StreamRouter)
        and isinstance(sys.stdin, _StdinRouter)
    ):
        return
    with _ROUTER_INIT_LOCK:
        if not isinstance(sys.stdout, _StreamRouter):
            sys.stdout = _StreamRouter(_STDOUT_BUF_VAR, sys.stdout)  # type: ignore[assignment]
        if not isinstance(sys.stderr, _StreamRouter):
            sys.stderr = _StreamRouter(_STDERR_BUF_VAR, sys.stderr)  # type: ignore[assignment]
        if not isinstance(sys.stdin, _StdinRouter):
            sys.stdin = _StdinRouter(_STDIN_BUF_VAR, sys.stdin)  # type: ignore[assignment]


def _run_cli_capture(argv: List[str]) -> Tuple[int, str]:
    _ensure_stream_routers()
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    in_buf = io.StringIO("")
    tok_out = _STDOUT_BUF_VAR.set(out_buf)
    tok_err = _STDERR_BUF_VAR.set(err_buf)
    tok_in = _STDIN_BUF_VAR.set(in_buf)
    try:
        try:
            rc = aoci_index.main(argv)
        except SystemExit as exc:
            if isinstance(exc.code, int):
                rc = exc.code
            elif exc.code is None:
                rc = 0
            else:
                rc = 1
                err_buf.write(f"\n{exc.code}")
    finally:
        _STDIN_BUF_VAR.reset(tok_in)
        _STDOUT_BUF_VAR.reset(tok_out)
        _STDERR_BUF_VAR.reset(tok_err)
    out_txt = out_buf.getvalue().strip()
    err_txt = err_buf.getvalue().strip()
    if out_txt:
        if rc == 0:
            return 0, out_txt
        if out_txt.startswith(("{", "[")):
            try:
                json.loads(out_txt)
                return rc, out_txt
            except ValueError:
                pass
        return rc, (f"{out_txt}\n{err_txt}".strip() if err_txt else out_txt)
    return rc, err_txt


_CORE_TOOLS: Tuple[str, ...] = (
    "aoci_overview",
    "aoci_query",
    "aoci_pack",
    "aoci_context",
    "aoci_symbol",
    "aoci_refs",
    "aoci_diff",
    "aoci_scan",
)
_SYNC_TOOLS: Tuple[str, ...] = _CORE_TOOLS + (
    "aoci_init",
    "aoci_prepare_sync",
    "aoci_apply_sync",
    "aoci_upsert",
    "aoci_ack",
    "aoci_migrate_renames",
    "aoci_restore",
    "aoci_scaffold",
    "aoci_batch_pack",
    "aoci_zoom",
    "aoci_outline",
    "aoci_verify",
    "aoci_lint",
    "aoci_doctor",
)
# `tools/list` profiles: trimming the advertised surface cuts the per-session tool-definition token cost
TOOL_PROFILES: Dict[str, Optional[Tuple[str, ...]]] = {"core": _CORE_TOOLS, "sync": _SYNC_TOOLS, "full": None}
DEFAULT_TOOL_PROFILE = "full"


def _normalize_profile(raw: Optional[str]) -> str:
    name = (raw or "").strip().lower()
    return name if name in TOOL_PROFILES else DEFAULT_TOOL_PROFILE


_TOOL_PROFILE = _normalize_profile(os.environ.get("AOCI_MCP_PROFILE"))


def set_tool_profile(name: str) -> str:
    """Select the advertised tool profile (`core`, `sync`, or `full`); unknown names fall back to `full`."""
    global _TOOL_PROFILE
    _TOOL_PROFILE = _normalize_profile(name)
    return _TOOL_PROFILE


def tool_enabled(name: str) -> bool:
    allowed = TOOL_PROFILES.get(_TOOL_PROFILE)
    return allowed is None or name in allowed


def profile_tools() -> List[Dict[str, Any]]:
    return [spec for spec in TOOLS_SPEC if tool_enabled(spec["name"])]


_ALLOW_OUTSIDE_ROOT_ENV = "AOCI_MCP_ALLOW_OUTSIDE_ROOT"
# Tool arguments that make the server write (or, for `pending_dir` + `clean_pending`, delete) files
_WRITE_TARGET_PARAMS: Dict[str, Tuple[str, ...]] = {
    "aoci_scaffold": ("output",),
    "aoci_export": ("output",),
    "aoci_zoom": ("output",),
    "aoci_batch_pack": ("output",),
    "aoci_ci_check": ("sarif_output", "markdown_output"),
    "aoci_pool": ("file",),
    "aoci_upsert": ("pending_dir",),
    "aoci_apply_sync": ("pending_dir",),
}


def _write_target_error(name: str, root: Path, args: Mapping[str, Any]) -> Optional[str]:
    """
    MCP-only guard: file targets a tool would write or delete must resolve inside `root` and never under
    `.git/` (hooks there are executable). The CLI stays unrestricted because a human runs it directly.
    `AOCI_MCP_ALLOW_OUTSIDE_ROOT=1` lifts the root restriction (never the `.git/` one).
    """
    params = _WRITE_TARGET_PARAMS.get(name, ())
    if name == "aoci_pool" and str(args.get("action", "stats")) != "export":
        params = ()
    if name in ("aoci_upsert", "aoci_apply_sync") and not args.get("clean_pending"):
        params = ()
    allow_outside = os.environ.get(_ALLOW_OUTSIDE_ROOT_ENV, "").strip() not in ("", "0")
    for param in params:
        raw = args.get(param)
        if raw is None or not str(raw).strip():
            continue
        target = aoci_index._resolve_cli_file_path(root, str(raw), must_exist=False)
        try:
            rel_parts = target.resolve().relative_to(root.resolve()).parts
        except (OSError, ValueError):
            rel_parts = None
        if rel_parts is not None and rel_parts[:1] == (".git",):
            return f"ERROR: `{param}` must not point inside `.git/` (got `{raw}`)."
        if rel_parts is None and not allow_outside:
            return (
                f"ERROR: `{param}` must stay inside the repository root `{root}` (got `{raw}`). "
                f"Set {_ALLOW_OUTSIDE_ROOT_ENV}=1 to allow MCP writes elsewhere."
            )
    return None


def _tool_aoci_init(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["init", "--root", str(root), "--json"]
    if args.get("project"):
        cli_args.append(f"--project={args['project']}")
    if args.get("locale"):
        cli_args.append(f"--locale={args['locale']}")
    if args.get("force"):
        cli_args.append("--force")
    rc, out = _run_cli_capture(cli_args)
    _on_root_mutated(root, ok=(rc == 0))
    return rc != 0, out


def _tool_aoci_overview(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    if slot is None:
        return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
    min_imp = int(args.get("min_importance", 7))
    max_core = int(args.get("max_core", 50))
    parsed_map: Dict[str, Any] = slot["parsed"]
    dir_counts: Dict[str, int] = {}
    layer_counts: Dict[str, int] = {}
    core: List[Tuple[int, str, str]] = []
    for rel, item in sorted(parsed_map.items()):
        top_d = rel.split("/", 1)[0] if "/" in rel else "."
        dir_counts[top_d] = dir_counts.get(top_d, 0) + 1
        layer_counts[item.layer] = layer_counts.get(item.layer, 0) + 1
        imp = item.imp
        if imp >= min_imp:
            core.append((imp, rel, item.raw))
    core.sort(key=lambda item: (-item[0], item[1]))
    payload = {
        "root": str(root),
        "project": slot["project"],
        "git_excluded": slot["git_hdr"],
        "global_invariants": slot["invariants"],
        "indexed_files": len(slot["entries"]),
        "estimated_full_tokens": slot["estimated_tokens"],
        "domains": slot["domains"],
        "layers": dict(sorted(layer_counts.items())),
        "top_dirs": dict(sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "core_entries": [ln for _, _, ln in core[:max_core]],
    }
    return False, json.dumps(payload, indent=2, ensure_ascii=False)


def _tool_aoci_query(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    if slot is None:
        return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
    live_ti = bool(args.get("live_test_imports", False))

    def _retriever(hybrid: bool) -> Any:
        if hybrid:
            return _CACHE.get_hybrid(root, slot, aoci_index._hybrid.options_from(args))
        return _CACHE.get_bm25(slot)

    payload = aoci_index._query_ops.run_query(
        root,
        slot["entries"],
        slot["parsed"],
        get_dependents=lambda: slot["dependents"],
        get_retriever=_retriever,
        glossary=slot.get("glossary", ()),
        layer=args.get("layer"),
        domain=args.get("domain"),
        min_importance=int(args["min_importance"]) if args.get("min_importance") is not None else None,
        path_glob=str(args["path"]) if args.get("path") else None,
        keyword=str(args["keyword"]) if args.get("keyword") else None,
        search=str(args["search"]) if args.get("search") else None,
        top=int(args["top"]) if args.get("top") is not None else None,
        impact=str(args["impact"]) if args.get("impact") else None,
        tests_for=str(args["tests_for"]) if args.get("tests_for") else None,
        depth=int(args["depth"]) if args.get("depth") is not None else None,
        live_test_imports=live_ti,
        hybrid=bool(args.get("hybrid")),
        test_index=None if (live_ti or not args.get("tests_for")) else _CACHE.get_test_index(root, slot),
        preloaded_outline_files=_CACHE.get_outline_files(root) if live_ti else None,
        baseline_files=_CACHE.get_baseline_files(root),
    )
    return False, json.dumps(payload, indent=2, ensure_ascii=False)


def _tool_aoci_context(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    if slot is None:
        return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
    raw_paths = args.get("paths") or []
    depth = int(args["depth"]) if args.get("depth") is not None else 1
    budget = int(args.get("budget", 0) or 0)
    with_tests = bool(args.get("with_tests", False))
    payload = aoci_index.build_context_subgraph(
        root,
        slot["entries"],
        slot["deps"],
        slot["dependents"],
        slot["invariants"],
        [str(p) for p in raw_paths],
        depth=depth,
        budget=budget,
        parsed_entries=slot["parsed"],
    )
    ctx_hub_paths = [
        aoci_index.normalize_relpath(ln.split("[", 1)[0].strip())
        for ln in (payload["targets"] + payload["dependencies"])
        if ln
    ]
    hub_secs = aoci_index._hubs.lookup_hub_sections(
        root / aoci_index.AOCI_DIR_NAME,
        ctx_hub_paths,
        preloaded_records=_CACHE.get_hub_records(root),
    )
    raw_tests: Optional[List[Dict[str, Any]]] = None
    if with_tests:
        live_ti = bool(args.get("live_test_imports", False))
        t_idx = None if live_ti else _CACHE.get_test_index(root, slot)
        raw_tests = aoci_index.find_impacted_tests(
            root,
            slot["entries"],
            slot["dependents"],
            [str(p) for p in raw_paths],
            max_depth=max(3, depth),
            parsed_entries=slot["parsed"],
            preloaded_outline_files=_CACHE.get_outline_files(root) if live_ti else None,
            live_imports=live_ti,
            test_index=t_idx,
        )
    aoci_index._graph.attach_budgeted_context_extras(
        payload,
        hub_secs,
        raw_tests,
        [str(p) for p in raw_paths],
        budget,
    )
    aoci_index._freshness.annotate_payload_freshness(
        root, payload, baseline_files=_CACHE.get_baseline_files(root)
    )
    return False, json.dumps(payload, indent=2, ensure_ascii=False)


def _tool_aoci_diff(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    depth = int(args["depth"]) if args.get("depth") is not None else 1
    with_tests = bool(args.get("with_tests", False))
    live_ti = bool(args.get("live_test_imports", False))
    t_idx = _CACHE.get_test_index(root, slot) if (with_tests and slot is not None and not live_ti) else None
    brief = aoci_index.compute_diff_briefing(
        root,
        rev=args.get("rev"),
        staged=bool(args.get("staged", False)),
        depth=depth,
        with_tests=with_tests,
        cached_entries=slot["entries"] if slot is not None else None,
        cached_deps=slot["deps"] if slot is not None else None,
        cached_dependents=slot["dependents"] if slot is not None else None,
        cached_invariants=slot["invariants"] if slot is not None else None,
        cached_parsed=slot["parsed"] if slot is not None else None,
        cached_hub_records=_CACHE.get_hub_records(root),
        preloaded_outline_files=_CACHE.get_outline_files(root) if (with_tests and t_idx is None) else None,
        live_test_imports=live_ti,
        test_index=t_idx,
    )
    return (not brief.get("ok")), json.dumps(brief, indent=2, ensure_ascii=False)


def _tool_aoci_scan(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["scan", "--root", str(root), "--json"]
    if args.get("fast", True):
        cli_args.append("--fast")
    if args.get("git_fast"):
        cli_args.append("--git-fast")
    if args.get("auto_restore"):
        cli_args.append("--auto-restore")
    if args.get("check_only"):
        cli_args.append("--check-only")
    if args.get("fail_on_drift"):
        cli_args.append("--fail-on-drift")
    if args.get("batch_size"):
        cli_args.extend(["--batch-size", str(int(args["batch_size"]))])
    if args.get("max_batch_lines"):
        cli_args.extend(["--max-batch-lines", str(int(args["max_batch_lines"]))])
    if args.get("reset_batches"):
        cli_args.append("--reset-batches")
    rc, out = _run_cli_capture(cli_args)
    if args.get("auto_restore") and not args.get("check_only"):
        _CACHE.invalidate(root)
    return rc != 0, out


def _tool_aoci_outline(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    paths = [str(p) for p in (args.get("paths") or [])]
    max_items = int(args.get("max_items", 30))
    res = aoci_index.compute_outlines(root, paths, max_items=max_items)
    is_err = bool(res["missing"] or res["rejected"])
    return is_err, json.dumps(res, indent=2, ensure_ascii=False)


def _tool_aoci_scaffold(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["scaffold", "--root", str(root), "--json"]
    if args.get("unindexed"):
        cli_args.append("--unindexed")
    if args.get("batch"):
        cli_args.extend(["--batch", str(int(args["batch"]))])
    if args.get("batch_size"):
        cli_args.extend(["--batch-size", str(int(args["batch_size"]))])
    if args.get("max_batch_lines"):
        cli_args.extend(["--max-batch-lines", str(int(args["max_batch_lines"]))])
    if args.get("reset_batches"):
        cli_args.append("--reset-batches")
    if args.get("output"):
        cli_args.append(f"--output={args['output']}")
    paths = [str(p) for p in (args.get("paths") or [])]
    if paths:
        cli_args.extend(["--", *paths])
    rc, out = _run_cli_capture(cli_args)
    return rc != 0, out


def _tool_aoci_ack(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    paths = [str(p) for p in (args.get("paths") or [])]
    cli_args = ["ack", "--root", str(root), "--json"]
    if args.get("all_modified"):
        cli_args.append("--all-modified")
    if args.get("impl_only"):
        cli_args.append("--impl-only")
    if args.get("reviewed"):
        cli_args.append("--reviewed")
    if args.get("dry_run"):
        cli_args.append("--dry-run")
    if paths:
        cli_args.extend(["--", *paths])
    rc, out = _run_cli_capture(cli_args)
    if not args.get("dry_run"):
        _on_root_mutated(root, ok=(rc == 0))
    return rc != 0, out


def _tool_aoci_upsert(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["upsert", "--root", str(root), "--json"]
    if args.get("entries_file"):
        cli_args.append(f"--entries-file={args['entries_file']}")
    if args.get("pending_dir"):
        cli_args.append(f"--pending-dir={args['pending_dir']}")
    if args.get("clean_pending"):
        cli_args.append("--clean-pending")
    if args.get("prune_only"):
        cli_args.append("--prune-only")
    if args.get("no_prune"):
        cli_args.append("--no-prune")
    if args.get("strict_size"):
        cli_args.append("--strict-size")
    if args.get("drop_malformed"):
        cli_args.append("--drop-malformed")
    if args.get("dry_run"):
        cli_args.append("--dry-run")
    for entry in args.get("entries") or []:
        cli_args.append(f"--entry={entry}")
    rc, out = _run_cli_capture(cli_args)
    if not args.get("dry_run"):
        _on_root_mutated(root, ok=(rc == 0))
    return rc != 0, out


def _tool_aoci_migrate_renames(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["migrate-renames", "--root", str(root), "--json"]
    if args.get("exact_only"):
        cli_args.append("--exact-only")
    if args.get("no_rewrite_refs"):
        cli_args.append("--no-rewrite-refs")
    if args.get("drop_malformed"):
        cli_args.append("--drop-malformed")
    if args.get("dry_run"):
        cli_args.append("--dry-run")
    rc, out = _run_cli_capture(cli_args)
    if not args.get("dry_run"):
        _on_root_mutated(root, ok=(rc == 0))
    return rc != 0, out


def _tool_aoci_restore(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["restore", "--root", str(root), "--json"]
    if args.get("allow_sha_fallback") and not args.get("exact_path_only"):
        cli_args.append("--allow-sha-fallback")
    if args.get("exact_path_only"):
        cli_args.append("--exact-path-only")
    if args.get("no_prune"):
        cli_args.append("--no-prune")
    if args.get("dry_run"):
        cli_args.append("--dry-run")
    rc, out = _run_cli_capture(cli_args)
    if not args.get("dry_run"):
        _on_root_mutated(root, ok=(rc == 0))
    return rc != 0, out


def _tool_aoci_shard(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["shard", "--root", str(root), "--json"]
    if args.get("clean"):
        cli_args.append("--clean")
    elif args.get("depth") is not None:
        cli_args.extend(["--depth", str(int(args["depth"]))])
    rc, out = _run_cli_capture(cli_args)
    _on_root_mutated(root, ok=(rc == 0))
    return rc != 0, out


def _tool_aoci_ensure_git_exclude(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    rc, out = _run_cli_capture(["ensure-git-exclude", "--root", str(root), "--json"])
    _CACHE.invalidate(root)
    return rc != 0, out


def _tool_aoci_lint(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    res = aoci_index.compute_lint(
        root,
        code_filter=args.get("code"),
        cached_entries=slot["entries"] if slot is not None else None,
        cached_parsed=slot["parsed"] if slot is not None else None,
        cached_malformed=slot["malformed"] if slot is not None else None,
        cached_duplicates=slot.get("duplicates") if slot is not None else None,
        cached_domains=slot["domains"] if slot is not None else None,
    )
    if res.get("error"):
        return True, f"ERROR: {res['error']}"
    all_findings = res["findings"]
    total_count = len(all_findings)
    is_err = bool(all_findings and args.get("strict"))
    raw_limit = args.get("limit", 100)
    limit = 100 if raw_limit is None else int(raw_limit)
    if limit > 0 and total_count > limit:
        res = dict(res)
        res["findings"] = all_findings[:limit]
        res["total_findings"] = total_count
        res["truncated"] = True
    return is_err, json.dumps(res, indent=2, ensure_ascii=False)


def _tool_aoci_verify(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["verify", "--root", str(root), "--json"]
    if args.get("fast"):
        cli_args.append("--fast")
    if args.get("git_fast"):
        cli_args.append("--git-fast")
    rc, out = _run_cli_capture(cli_args)
    return rc != 0, out


def _tool_aoci_audit(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    res = aoci_index.audit_architecture(
        root,
        check_static=bool(args.get("check_static", True)),
        check_cycles=bool(args.get("check_cycles", True)),
        check_layers=bool(args.get("check_layers", True)),
        check_co_change=bool(args.get("check_co_change", False)),
        co_change_commits=int(args.get("co_change_commits", 200)),
        min_co_changes=int(args.get("min_co_changes", 3)),
        min_co_confidence=float(args.get("min_co_confidence", 0.6)),
        exclude_tests=bool(args.get("exclude_tests", False)),
        forbid_spec=args.get("forbid"),
        use_default_rules=bool(args.get("use_default_rules", True)),
        cached_entries=slot["entries"] if slot is not None else None,
        cached_deps=slot["deps"] if slot is not None else None,
        cached_dependents=slot["dependents"] if slot is not None else None,
        cached_parsed=slot["parsed"] if slot is not None else None,
    )
    is_audit_err = bool(
        res.get("error") or (args.get("strict") and int(res.get("total_findings", 0) or 0) > 0)
    )
    return is_audit_err, json.dumps(res, indent=2, ensure_ascii=False)


def _tool_aoci_export(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    if slot is None:
        return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
    changed_exp: List[str] = []
    affected_exp: List[str] = []
    if args.get("diff") or args.get("diff_rev"):
        dbrief = aoci_index.compute_diff_briefing(
            root,
            rev=args.get("diff_rev"),
            depth=1,
            cached_entries=slot["entries"],
            cached_deps=slot["deps"],
            cached_dependents=slot["dependents"],
            cached_invariants=slot["invariants"],
            cached_parsed=slot["parsed"],
        )
        if dbrief.get("ok"):
            for ln in dbrief.get("changed_entries") or ():
                changed_exp.append(aoci_index.normalize_relpath(ln.split("[", 1)[0].strip()))
            changed_exp.extend(aoci_index.normalize_relpath(u) for u in (dbrief.get("unindexed_changed") or ()))
            for ln in dbrief.get("affected_neighbors") or ():
                affected_exp.append(aoci_index.normalize_relpath(ln.split("[", 1)[0].strip()))
    res = aoci_index.export_dependency_graph(
        slot["entries"],
        slot["deps"],
        slot["dependents"],
        slot["domains"],
        fmt=str(args.get("format", "mermaid")),
        level=str(args.get("level", "file")),
        group_by=str(args.get("group_by", "domain")),
        direction=str(args.get("direction", "LR")),
        layer=args.get("layer"),
        domain=args.get("domain"),
        min_importance=int(args["min_importance"]) if args.get("min_importance") is not None else 0,
        path_glob=args.get("path"),
        focus=args.get("focus"),
        depth=int(args.get("depth", 1)),
        parsed_entries=slot["parsed"],
        changed_paths=changed_exp,
        affected_paths=affected_exp,
    )
    if args.get("output"):
        out_path = aoci_index._resolve_cli_file_path(root, str(args["output"]), must_exist=False)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            aoci_index.write_text_lf(out_path, str(res.get("content") or res.get("graph", "")))
            res = dict(res)
            res["output_path"] = str(out_path)
        except OSError as exc:
            return True, f"ERROR: Cannot write exported graph to `{out_path}`: {exc}"
    return False, json.dumps(res, indent=2, ensure_ascii=False)


def _tool_aoci_pool(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    action = str(args.get("action", "stats"))
    cli_args = ["pool", action, "--root", str(root), "--json"]
    if action == "export":
        if not args.get("file"):
            return True, "ERROR: `file` parameter is required for `aoci_pool(action='export')`."
        cli_args.extend(["--output", str(args["file"])])
        if args.get("pool_only"):
            cli_args.append("--pool-only")
    elif action == "import":
        if not args.get("file"):
            return True, "ERROR: `file` parameter is required for `aoci_pool(action='import')`."
        cli_args.extend(["--input", str(args["file"])])
        if args.get("dry_run"):
            cli_args.append("--dry-run")
    rc, out = _run_cli_capture(cli_args)
    if action == "import" and not args.get("dry_run"):
        _CACHE.invalidate(root)
    return rc != 0, out


def _tool_aoci_pack(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    if slot is None:
        return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
    task_q = args.get("task")
    if task_q is None or not str(task_q).strip():
        return True, "ERROR: `task` parameter is required for `aoci_pack`."
    outline_files = _CACHE.get_outline_files(root)
    hub_records = _CACHE.get_hub_records(root)
    include_diff = bool(args.get("include_diff", True))
    diff_brief: Optional[Dict[str, Any]] = None
    if include_diff:
        cand_diff = _CACHE.get_working_diff_briefing(root, slot, hub_records)
        if cand_diff.get("ok"):
            diff_brief = cand_diff
    if args.get("hybrid"):
        bm25 = _CACHE.get_hybrid(root, slot, aoci_index._hybrid.options_from(args))
    else:
        bm25 = _CACHE.get_bm25(slot)
    depth_val = int(args["depth"]) if args.get("depth") is not None else 1
    budget_val = int(args["budget"]) if args.get("budget") is not None else 2500
    top_seeds_val = int(args["top_seeds"]) if args.get("top_seeds") is not None else 8
    with_tests_pack = bool(args.get("with_tests", False))
    live_ti_pack = bool(args.get("live_test_imports", False))
    t_idx_pack = _CACHE.get_test_index(root, slot) if (with_tests_pack and not live_ti_pack) else None
    payload = aoci_index.build_task_pack(
        root,
        slot["entries"],
        slot["deps"],
        slot["dependents"],
        slot["invariants"],
        slot["domains"],
        slot.get("glossary", ()),
        task=str(task_q),
        diff_brief=diff_brief,
        depth=depth_val,
        budget=budget_val,
        top_seeds=top_seeds_val,
        bm25_index=bm25,
        parsed_entries=slot["parsed"],
        mention_index=slot["mention_index"],
        preloaded_outline_files=outline_files,
        hub_records=hub_records,
        live_anchors=bool(args.get("live_anchors", False)),
        with_tests=with_tests_pack,
        live_test_imports=live_ti_pack,
        test_index=t_idx_pack,
        baseline_files=_CACHE.get_baseline_files(root),
    )
    return False, json.dumps(payload, indent=2, ensure_ascii=False)


def _tool_aoci_zoom(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    raw_mode = args.get("mode")
    if raw_mode is None:
        mode = (
            "upsert"
            if (args.get("entries") or args.get("entries_file") or args.get("prune_missing"))
            and not args.get("path")
            else "read"
        )
    else:
        mode = str(raw_mode).strip().lower()
    if mode not in ("read", "scaffold", "upsert"):
        return True, f"ERROR: Unsupported `mode` `{mode}` for `aoci_zoom` (expected `read`, `scaffold`, or `upsert`)."
    if mode == "read":
        if not args.get("path"):
            return True, "ERROR: `path` parameter is required for `aoci_zoom(mode='read')`."
        slot = _CACHE.get(root)
        res = aoci_index.compute_zoom_read(
            root,
            str(args["path"]),
            cached_entries=slot["entries"] if slot is not None else None,
            cached_hub_records=_CACHE.get_hub_records(root),
        )
        if not res.get("ok"):
            err_out = {"ok": False, "error": res.get("error", "Zoom read failed.")}
            return True, json.dumps(err_out, indent=2, ensure_ascii=False)
        return False, json.dumps(res, indent=2, ensure_ascii=False)
    cli_args = ["zoom", "--root", str(root), "--json"]
    if mode == "scaffold":
        if not args.get("path"):
            return True, "ERROR: `path` parameter is required for `aoci_zoom(mode='scaffold')`."
        cli_args.append("--scaffold")
        if args.get("output"):
            cli_args.append(f"--output={args['output']}")
        cli_args.extend(["--", str(args["path"])])
    else:
        if args.get("entries_file"):
            cli_args.append(f"--upsert-file={args['entries_file']}")
        if args.get("prune_missing"):
            cli_args.append("--prune-missing")
        if args.get("dry_run"):
            cli_args.append("--dry-run")
        for entry in args.get("entries") or []:
            cli_args.append(f"--upsert={entry}")
    rc, out = _run_cli_capture(cli_args)
    if mode == "upsert" and not args.get("dry_run"):
        _on_root_mutated(root, ok=(rc == 0))
    return rc != 0, out


def _tool_aoci_prepare_sync(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    aoci_dir = root / aoci_index.AOCI_DIR_NAME
    code_path = aoci_dir / aoci_index.CODE_FILE
    if not code_path.is_file():
        return True, json.dumps(
            {"ok": False, "error": f"`{code_path}` not found. Run `init` first."},
            indent=2,
            ensure_ascii=False,
        )
    slot = _CACHE.get(root)
    git_info = aoci_index.detect_git_info(root)
    drift = aoci_index.compute_drift(
        root,
        git_info,
        fast_mtime=bool(args.get("fast", True)),
        allow_heal=False,
        save_scan_cache=False,
    )
    brief = aoci_index._sync_prep.prepare_sync_briefing(
        root,
        git_info,
        drift,
        scaffold_fn=aoci_index.scaffold_entries,
        run_git_fn=aoci_index.run_git,
        max_diff_lines=int(args.get("max_diff_lines", 40)),
        cached_entries=slot["entries"] if slot is not None else None,
        cached_parsed=slot["parsed"] if slot is not None else None,
        cached_dependents=slot["dependents"] if slot is not None else None,
    )
    return False, json.dumps(brief, indent=2, ensure_ascii=False)


def _tool_aoci_apply_sync(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    dry_run = bool(args.get("dry_run", False))
    raw_mb = args.get("max_bytes")
    res = aoci_index._apply_sync.run_apply_sync(
        root,
        _run_cli_capture,
        restore=bool(args.get("restore", False)),
        allow_sha_fallback=bool(args.get("allow_sha_fallback", False)),
        migrate_renames=bool(args.get("migrate_renames", False)),
        exact_only=bool(args.get("exact_only", False)),
        no_rewrite_refs=bool(args.get("no_rewrite_refs", False)),
        ack_paths=[str(p) for p in (args.get("ack_paths") or [])],
        ack_impl_only=bool(args.get("ack_impl_only", False)),
        reviewed=bool(args.get("reviewed", False)),
        entries=[str(e) for e in (args.get("entries") or [])],
        entries_file=args.get("entries_file"),
        pending_dir=args.get("pending_dir"),
        clean_pending=bool(args.get("clean_pending", False)),
        prune_only=bool(args.get("prune_only", False)),
        no_prune=bool(args.get("no_prune", False)),
        strict_size=bool(args.get("strict_size", False)),
        drop_malformed=bool(args.get("drop_malformed", False)),
        verify=bool(args.get("verify", True)),
        full_verify=bool(args.get("full_verify", False)),
        max_bytes=int(raw_mb) if raw_mb is not None else None,
        dry_run=dry_run,
        lock_cls=aoci_index.IndexLock,
    )
    if not dry_run:
        _on_root_mutated(root, ok=bool(res.get("ok")))
    return (not res.get("ok")), json.dumps(res, indent=2, ensure_ascii=False)


def _tool_aoci_symbol(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    if slot is None:
        return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
    res = aoci_index._symbols.lookup_symbol(
        root,
        slot["entries"],
        slot["deps"],
        slot["dependents"],
        str(args.get("name", "")),
        exact=bool(args.get("exact", False)),
        depth=int(args["depth"]) if args.get("depth") is not None else 1,
        limit=int(args["limit"]) if args.get("limit") is not None else 20,
        live=bool(args.get("live", False)),
        parsed_entries=slot["parsed"],
        preloaded_outline_files=_CACHE.get_outline_files(root),
        hub_records=_CACHE.get_hub_records(root),
    )
    return (not res.get("ok")), json.dumps(res, indent=2, ensure_ascii=False)


def _tool_aoci_install_git_hooks(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    cli_args = ["install-git-hooks", "--root", str(root), "--json"]
    if args.get("python_cmd"):
        cli_args.extend(["--python-cmd", str(args["python_cmd"])])
    if args.get("with_pre_commit"):
        cli_args.append("--with-pre-commit")
    if args.get("pre_commit_verify"):
        cli_args.append("--pre-commit-verify")
    if args.get("uninstall"):
        cli_args.append("--uninstall")
    if args.get("dry_run"):
        cli_args.append("--dry-run")
    rc, out = _run_cli_capture(cli_args)
    return rc != 0, out


def _tool_aoci_batch_pack(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    aoci_dir = root / aoci_index.AOCI_DIR_NAME
    if not (aoci_dir / aoci_index.CODE_FILE).is_file():
        return True, json.dumps(
            {"ok": False, "error": f"`{aoci_dir / aoci_index.CODE_FILE}` not found. Run `init` first."},
            indent=2,
            ensure_ascii=False,
        )
    if args.get("batch") is None:
        return True, "ERROR: `batch` parameter is required for `aoci_batch_pack`."
    git_info = aoci_index.detect_git_info(root)
    drift = aoci_index.compute_drift(root, git_info, fast_mtime=True, allow_heal=False, save_scan_cache=False)
    pack_res = aoci_index._batch_pack.build_batch_pack(
        root,
        drift,
        int(args["batch"]),
        scaffold_fn=aoci_index.scaffold_entries,
        batch_size=int(args["batch_size"]) if args.get("batch_size") is not None else None,
        max_batch_lines=int(args["max_batch_lines"]) if args.get("max_batch_lines") is not None else None,
        reset_batches=bool(args.get("reset_batches", False)),
        with_source=bool(args.get("with_source", False)),
        max_source_lines=int(args.get("max_source_lines", 200)),
    )
    if not pack_res.get("ok"):
        return True, json.dumps(pack_res, indent=2, ensure_ascii=False)
    fmt_bp = str(args.get("format", "json")).lower()
    rendered_md = aoci_index._batch_pack.render_batch_pack_markdown(pack_res)
    if args.get("output"):
        out_path = aoci_index._resolve_cli_file_path(root, str(args["output"]), must_exist=False)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            aoci_index.write_text_lf(
                out_path,
                rendered_md if fmt_bp == "markdown" else (json.dumps(pack_res, indent=2, ensure_ascii=False) + "\n"),
            )
            pack_res = dict(pack_res)
            pack_res["output_path"] = str(out_path)
        except OSError as exc:
            return True, f"ERROR: Cannot write batch pack to `{out_path}`: {exc}"
    if fmt_bp == "markdown":
        _publish_structured(pack_res)
        return False, rendered_md
    return False, json.dumps(pack_res, indent=2, ensure_ascii=False)


def _tool_aoci_ci_check(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    fmt_ci = str(args.get("format", "json")).lower()
    try:
        report = aoci_index.run_ci_architecture_check(
            root,
            base_rev=args.get("base_rev"),
            pool_bundle=args.get("pool_bundle"),
            forbid_spec=args.get("forbid"),
            use_default_rules=bool(args.get("use_default_rules", True)),
            check_cycles=bool(args.get("check_cycles", True)),
            check_layers=bool(args.get("check_layers", True)),
            check_static=bool(args.get("check_static", True)),
            check_drift=bool(args.get("check_drift", False)),
            exclude_tests=bool(args.get("exclude_tests", False)),
        )
    except Exception as exc:
        return True, json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False)
    if report.get("error"):
        return True, json.dumps(report, indent=2, ensure_ascii=False)
    if args.get("sarif_output"):
        sarif_p = aoci_index._resolve_cli_file_path(root, str(args["sarif_output"]), must_exist=False)
        try:
            sarif_p.parent.mkdir(parents=True, exist_ok=True)
            aoci_index.write_text_lf(sarif_p, aoci_index._ci_check.format_ci_sarif(report))
            report["sarif_output_path"] = str(sarif_p)
        except OSError as exc:
            return True, f"ERROR: Cannot write SARIF output to `{sarif_p}`: {exc}"
    if args.get("markdown_output"):
        md_p = aoci_index._resolve_cli_file_path(root, str(args["markdown_output"]), must_exist=False)
        try:
            md_p.parent.mkdir(parents=True, exist_ok=True)
            aoci_index.write_text_lf(md_p, aoci_index._ci_check.format_ci_markdown(report))
            report["markdown_output_path"] = str(md_p)
        except OSError as exc:
            return True, f"ERROR: Cannot write Markdown output to `{md_p}`: {exc}"
    is_ci_err = bool(args.get("strict") and not report.get("ok"))
    if fmt_ci in ("sarif", "github", "markdown"):
        _publish_structured(report)
    if fmt_ci == "sarif":
        return is_ci_err, aoci_index._ci_check.format_ci_sarif(report)
    if fmt_ci == "github":
        return is_ci_err, aoci_index._ci_check.format_ci_github_annotations(report)
    if fmt_ci == "markdown":
        return is_ci_err, aoci_index._ci_check.format_ci_markdown(report)
    return is_ci_err, json.dumps(report, indent=2, ensure_ascii=False)


def _tool_aoci_refs(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    slot = _CACHE.get(root)
    if slot is None:
        return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
    res = aoci_index.find_symbol_references(
        root,
        slot["entries"],
        slot["deps"],
        slot["dependents"],
        str(args.get("symbol", "")),
        def_path=args.get("path"),
        depth=int(args["depth"]) if args.get("depth") is not None else 2,
        all_files=bool(args.get("all_files", False)),
        include_defs=bool(args.get("include_defs", False)),
        limit=int(args["limit"]) if args.get("limit") is not None else 50,
        parsed_entries=slot["parsed"],
        preloaded_outline_files=_CACHE.get_outline_files(root),
        hub_records=_CACHE.get_hub_records(root),
    )
    return (not res.get("ok")), json.dumps(res, indent=2, ensure_ascii=False)


def _tool_aoci_doctor(root: Path, args: Dict[str, Any]) -> Tuple[bool, str]:
    dry_run = bool(args.get("dry_run", False))
    res = aoci_index.run_index_doctor(
        root,
        sync_static_r=bool(args.get("sync_static_r", False)),
        drop_malformed=bool(args.get("drop_malformed", False)),
        dry_run=dry_run,
    )
    if not dry_run:
        _on_root_mutated(root, ok=bool(res.get("ok")))
    return (not res.get("ok")), json.dumps(res, indent=2, ensure_ascii=False)


ToolHandler = Callable[[Path, Dict[str, Any]], Tuple[bool, str]]
# Tool name -> handler; every `TOOLS_SPEC` entry must have exactly one (enforced by tests)
TOOL_HANDLERS: Dict[str, ToolHandler] = {
    "aoci_init": _tool_aoci_init,
    "aoci_overview": _tool_aoci_overview,
    "aoci_query": _tool_aoci_query,
    "aoci_context": _tool_aoci_context,
    "aoci_diff": _tool_aoci_diff,
    "aoci_scan": _tool_aoci_scan,
    "aoci_outline": _tool_aoci_outline,
    "aoci_scaffold": _tool_aoci_scaffold,
    "aoci_ack": _tool_aoci_ack,
    "aoci_upsert": _tool_aoci_upsert,
    "aoci_migrate_renames": _tool_aoci_migrate_renames,
    "aoci_restore": _tool_aoci_restore,
    "aoci_shard": _tool_aoci_shard,
    "aoci_ensure_git_exclude": _tool_aoci_ensure_git_exclude,
    "aoci_lint": _tool_aoci_lint,
    "aoci_verify": _tool_aoci_verify,
    "aoci_audit": _tool_aoci_audit,
    "aoci_export": _tool_aoci_export,
    "aoci_pool": _tool_aoci_pool,
    "aoci_pack": _tool_aoci_pack,
    "aoci_zoom": _tool_aoci_zoom,
    "aoci_prepare_sync": _tool_aoci_prepare_sync,
    "aoci_apply_sync": _tool_aoci_apply_sync,
    "aoci_symbol": _tool_aoci_symbol,
    "aoci_install_git_hooks": _tool_aoci_install_git_hooks,
    "aoci_batch_pack": _tool_aoci_batch_pack,
    "aoci_ci_check": _tool_aoci_ci_check,
    "aoci_refs": _tool_aoci_refs,
    "aoci_doctor": _tool_aoci_doctor,
}


def call_tool(name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
    """Resolve the target root, enforce MCP write-target guards, and dispatch to the registered handler."""
    root = _resolve_mcp_root(args.get("root"), for_init=(name == "aoci_init"))
    guard_err = _write_target_error(name, root, args)
    if guard_err is not None:
        return True, guard_err
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return True, f"Unknown tool: `{name}`"
    return handler(root, args)


def _resource_target_path(uri: str) -> Tuple[Optional[Path], Optional[Tuple[int, str]]]:
    """Resolve an `aoci://` resource URI to its backing file (which may not exist yet)."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "aoci":
        return None, (-32602, f"Unsupported resource URI scheme: `{uri}`")
    qs = urllib.parse.parse_qs(parsed.query)
    raw_root: Optional[str] = (qs.get("root") or [None])[0]  # type: ignore[list-item]
    root = _resolve_mcp_root(raw_root)
    aoci_dir = root / aoci_index.AOCI_DIR_NAME

    resource_key = f"{parsed.netloc}{parsed.path}".rstrip("/")
    target_path: Optional[Path] = None
    if resource_key == "manifest":
        target_path = aoci_dir / aoci_index.MANIFEST_FILE
    elif resource_key == "meta":
        target_path = aoci_dir / aoci_index.META_FILE
    elif resource_key == "code":
        target_path = aoci_dir / aoci_index.CODE_FILE
    elif resource_key == "hubs":
        target_path = aoci_dir / aoci_index.HUBS_FILE
    elif resource_key == "shards/INDEX":
        target_path = aoci_dir / aoci_index.SHARDS_DIR / aoci_index.SHARDS_INDEX
    elif resource_key.startswith("shards/"):
        shard_name = resource_key.split("/", 1)[1]
        if not shard_name or "/" in shard_name or "\\" in shard_name or ".." in shard_name:
            return None, (-32602, f"Invalid shard resource URI: `{uri}`")
        fname = shard_name if shard_name.endswith(".code.txt") else f"{shard_name}.code.txt"
        target_path = aoci_dir / aoci_index.SHARDS_DIR / fname
    else:
        return None, (-32602, f"Unknown AOCI resource URI: `{uri}`")
    return target_path, None


def _read_resource_uri(uri: str) -> Tuple[Optional[str], Optional[Tuple[int, str]]]:
    target_path, err = _resource_target_path(uri)
    if target_path is None:
        return None, err
    if not target_path.is_file():
        return None, (-32002, f"Resource file does not exist: `{target_path}`")
    return target_path.read_text(encoding="utf-8-sig", errors="replace"), None


_WATCH_MISSING: Any = object()


class _ResourceWatcher:
    """
    Background poller for subscribed `aoci://` resources. Pushes `notifications/resources/updated` when a
    backing file changes outside this server (CLI `upsert`, Git hooks after a checkout, editors), so clients
    re-read without waiting for their next tool call. Polls only `stat` of subscribed files, does nothing
    without subscriptions, and is re-synced after this server's own mutations to avoid duplicate pushes.
    Interval: `AOCI_MCP_WATCH_INTERVAL` seconds (default 2.0; `0` disables).
    """

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._seen: Dict[str, Any] = {}
        self._targets: Dict[str, Optional[Path]] = {}  # URI -> backing file, resolved once per subscription
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def reset_targets(self) -> None:
        """Drop cached URI resolutions (client roots changed, so root-less URIs may point elsewhere)."""
        with self._lock:
            self._targets.clear()

    def _stat_of(self, uri: str) -> Tuple[Optional[Path], Any]:
        with self._lock:
            cached = self._targets.get(uri, _WATCH_MISSING)
        if cached is _WATCH_MISSING:
            cached, _ = _resource_target_path(uri)
            with self._lock:
                self._targets[uri] = cached
        target = cached
        if target is None:
            return None, _WATCH_MISSING
        return target, (aoci_index._stat_key(target) if target.is_file() else None)

    def observe(self, uri: str) -> None:
        """Record the current state of `uri` as the baseline (called on `resources/subscribe`)."""
        with self._lock:
            self._targets.pop(uri, None)
        _, st_key = self._stat_of(uri)
        if st_key is not _WATCH_MISSING:
            with self._lock:
                self._seen[uri] = st_key

    def forget(self, uri: str) -> None:
        with self._lock:
            self._seen.pop(uri, None)
            self._targets.pop(uri, None)

    def resync_root(self, root: Path) -> None:
        """Re-baseline subscribed URIs under `root/.aoci` after a mutation this server already notified."""
        try:
            aoci_dir = (root / aoci_index.AOCI_DIR_NAME).resolve()
        except OSError:
            aoci_dir = root / aoci_index.AOCI_DIR_NAME
        with _STATE_LOCK:
            subscribed = sorted(_SUBSCRIBED_URIS)
        for uri in subscribed:
            target, st_key = self._stat_of(uri)
            if target is None or st_key is _WATCH_MISSING:
                continue
            try:
                under = os.path.commonpath([str(aoci_dir), str(target.resolve())]) == str(aoci_dir)
            except (OSError, ValueError):
                under = False
            if under:
                with self._lock:
                    self._seen[uri] = st_key

    def poll_once(self) -> List[Dict[str, Any]]:
        """Return `notifications/resources/updated` messages for subscribed URIs whose file changed."""
        with _STATE_LOCK:
            subscribed = sorted(_SUBSCRIBED_URIS)
        out: List[Dict[str, Any]] = []
        with self._lock:
            for gone in [u for u in self._seen if u not in subscribed]:
                self._seen.pop(gone, None)
                self._targets.pop(gone, None)
        for uri in subscribed:
            _, st_key = self._stat_of(uri)
            if st_key is _WATCH_MISSING:
                continue
            with self._lock:
                prev = self._seen.get(uri, _WATCH_MISSING)
                self._seen[uri] = st_key
            if prev is not _WATCH_MISSING and prev != st_key:
                out.append({"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": {"uri": uri}})
        return out

    def start(self, emit: Any) -> None:
        if self.interval <= 0 or self._thread is not None:
            return

        def _run() -> None:
            while not self._stop.wait(self.interval):
                try:
                    msgs = self.poll_once()
                    if msgs:
                        emit(msgs)
                except Exception:  # a watcher hiccup must never take the stdio server down
                    continue

        self._thread = threading.Thread(target=_run, name="aoci-mcp-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.interval * 2))
        self._thread = None


def _watch_interval_from_env() -> float:
    raw = os.environ.get("AOCI_MCP_WATCH_INTERVAL", "").strip()
    if not raw:
        return 2.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 2.0


_WATCHER = _ResourceWatcher(_watch_interval_from_env())


def _render_prompt(name: str, arguments: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    with _STATE_LOCK:
        cached_hint = _CLIENT_ROOT_HINT
    root_hint = str(arguments.get("root") or cached_hint or "<repo>")
    if name == "aoci-bootstrap":
        text = (
            f"Bootstrap a complete `.aoci/` index for `{root_hint}`:\n"
            "1. Call `aoci_init` with `root` and confirm `.aoci/` is Git-excluded.\n"
            "2. Read top-level manifests/README and customize Domain codes in `.aoci/aoci.meta.txt` and `#Global-Invariants:` in `.aoci/aoci.txt`.\n"
            "3. Call `aoci_scan(root, batch_size=20)` and `aoci_scaffold(root, batch=1, batch_size=20, output='.aoci/pending/batch_01.txt')`.\n"
            "4. Read each actual source file, replace `<TODO:...>` with evidence-based F:/R:/A:/S:, then call `aoci_upsert`.\n"
            "5. Finish by passing `aoci_verify` and `aoci_lint`."
        )
        return {
            "description": "Bootstrap a new AOCI codebase index",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }, None
    if name == "aoci-review-diff":
        rev_val = arguments.get("rev")
        rev_clause = f", rev='{rev_val}'" if rev_val else ""
        text = (
            f"Review architectural impact for changes in `{root_hint}`:\n"
            f"1. Call `aoci_diff(root='{root_hint}'{rev_clause}, depth=1)`.\n"
            "2. Inspect `high_risk_constraints`, `dependency_constraints` (downstream R: invariants), and `affected_neighbors`.\n"
            "3. Expand critical targets via `aoci_context` before judging correctness or proposing edits."
        )
        return {
            "description": "Architectural diff impact review",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }, None
    if name == "aoci-sync":
        text = (
            f"Synchronize `.aoci/` drift in `{root_hint}`:\n"
            "1. Call `aoci_scan(root)` and migrate renames with `aoci_migrate_renames(root)`.\n"
            "2. Read drifted files: call `aoci_upsert` if responsibilities/contracts/invariants changed, `aoci_ack` if unchanged, `aoci_upsert(prune_only=True)` for deletions.\n"
            "3. Review `affected_neighbors` and confirm `aoci_verify(root)` passes."
        )
        return {
            "description": "Incremental AOCI index drift sync",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }, None
    return None, f"Unknown prompt: `{name}`"


def handle_rpc_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    global _CLIENT_ROOT_HINT, _NEGOTIATED_PROTOCOL, _CLIENT_SUPPORTS_ROOTS
    # Client response to a server-initiated request (e.g. `roots/list`) carries `result` or `error`, not `method`
    if "method" not in req and ("result" in req or "error" in req):
        res_obj = req.get("result")
        if isinstance(res_obj, dict) and isinstance(res_obj.get("roots"), list):
            update_client_roots(res_obj["roots"])
        return None

    method = req.get("method", "")
    req_id = req.get("id")
    raw_params = req.get("params")
    params: Dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    # Notifications carry no `id` and expect no response
    if req_id is None:
        if method == "notifications/initialized":
            with _STATE_LOCK:
                need_roots = _CLIENT_SUPPORTS_ROOTS and not _CLIENT_ROOT_HINTS
            if need_roots:
                _enqueue_roots_list_request()
        elif method == "notifications/roots/list_changed":
            if isinstance(params.get("roots"), list):
                update_client_roots(params["roots"])
            with _STATE_LOCK:
                supports_roots_now = _CLIENT_SUPPORTS_ROOTS
            if supports_roots_now:
                _enqueue_roots_list_request()
        return None

    def _ok(result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _err(code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        client_caps = params.get("capabilities")
        supports_roots = bool(isinstance(client_caps, dict) and isinstance(client_caps.get("roots"), dict))
        ws_folders = params.get("workspaceFolders")
        collected_hints: List[str] = []
        if isinstance(ws_folders, list):
            for wf in ws_folders:
                if isinstance(wf, dict) and isinstance(wf.get("uri"), str) and wf["uri"].strip():
                    h_wf = _file_uri_to_path(wf["uri"].strip())
                    if h_wf and h_wf not in collected_hints:
                        collected_hints.append(h_wf)
        if isinstance(params.get("roots"), list):
            for rt in params["roots"]:
                if isinstance(rt, dict) and isinstance(rt.get("uri"), str) and rt["uri"].strip():
                    h_rt = _file_uri_to_path(rt["uri"].strip())
                    if h_rt and h_rt not in collected_hints:
                        collected_hints.append(h_rt)
        root_uri = params.get("rootUri")
        if isinstance(root_uri, str) and root_uri.strip():
            h_ru = _file_uri_to_path(root_uri.strip())
            if h_ru and h_ru not in collected_hints:
                collected_hints.insert(0, h_ru)
        client_ver = str(params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION))
        chosen_ver = client_ver if client_ver in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL_VERSION
        with _STATE_LOCK:
            _CLIENT_SUPPORTS_ROOTS = supports_roots
            _NEGOTIATED_PROTOCOL = chosen_ver
            _SUBSCRIBED_URIS.clear()
            if collected_hints:
                _CLIENT_ROOT_HINTS[:] = collected_hints
                _CLIENT_ROOT_HINT = _first_indexed_hint(collected_hints) or collected_hints[0]
        return _ok(
            {
                "protocolVersion": chosen_ver,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": True, "listChanged": True},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        )

    if method == "ping":
        return _ok({})

    if method == "tools/list":
        return _ok({"tools": profile_tools()})

    if method == "resources/list":
        return _ok({"resources": RESOURCES_SPEC})

    if method == "resources/templates/list":
        return _ok({"resourceTemplates": RESOURCE_TEMPLATES_SPEC})

    if method == "resources/subscribe":
        uri = str(params.get("uri", "")).strip()
        if not uri or not uri.startswith("aoci://"):
            return _err(-32602, f"Invalid or unsupported resource URI for subscribe: `{uri}`")
        with _STATE_LOCK:
            _SUBSCRIBED_URIS.add(uri)
        _WATCHER.observe(uri)
        return _ok({})

    if method == "resources/unsubscribe":
        uri = str(params.get("uri", "")).strip()
        with _STATE_LOCK:
            _SUBSCRIBED_URIS.discard(uri)
        _WATCHER.forget(uri)
        return _ok({})

    if method == "resources/read":
        uri = str(params.get("uri", ""))
        text, err_info = _read_resource_uri(uri)
        if err_info is not None:
            return _err(err_info[0], err_info[1])
        return _ok({"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]})

    if method == "prompts/list":
        return _ok({"prompts": PROMPTS_SPEC})

    if method == "prompts/get":
        p_name = str(params.get("name", ""))
        p_args: Dict[str, Any] = params["arguments"] if isinstance(params.get("arguments"), dict) else {}
        prompt_obj, err_msg = _render_prompt(p_name, p_args)
        if err_msg is not None:
            return _err(-32602, err_msg)
        return _ok(prompt_obj)

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args: Dict[str, Any] = params["arguments"] if isinstance(params.get("arguments"), dict) else {}
        if tool_name in _TOOL_BY_NAME and not tool_enabled(tool_name):
            msg = (
                f"Tool `{tool_name}` is not enabled in the `{_TOOL_PROFILE}` profile; restart the server with "
                "`--profile full` (or `AOCI_MCP_PROFILE=full`) to use it."
            )
            return _ok({"content": [{"type": "text", "text": msg}], "isError": True})
        sidecar: Dict[str, Any] = {}
        sidecar_token = _STRUCTURED_SIDECAR.set(sidecar)
        try:
            is_err, text = call_tool(tool_name, tool_args)
        except (Exception, SystemExit) as exc:  # Isolation boundary for RPC server stability
            is_err, text = True, f"Internal error in `{tool_name}`: {exc}"
        finally:
            _STRUCTURED_SIDECAR.reset(sidecar_token)
        call_result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_err}
        t_spec = _TOOL_BY_NAME.get(tool_name)
        if not is_err and t_spec is not None and "outputSchema" in t_spec:
            # MCP 2025-06-18: tools declaring `outputSchema` MUST return `structuredContent` on success,
            # even when the text channel is Markdown / SARIF / GitHub annotations / Mermaid
            call_result["structuredContent"] = _structured_content_for(text, sidecar)
        return _ok(call_result)

    return _err(-32601, f"Method not found: {method}")


_WRITE_LOCK = threading.Lock()
# Framing of the most recent client message, reused for watcher pushes (which have no request to mirror)
_LAST_FRAMING: Dict[str, bool] = {"use_headers": False}


def _write_rpc_payload(raw_out: Any, payload: Any, use_headers: bool) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    framed = (
        f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded if use_headers else encoded + b"\n"
    )
    with _WRITE_LOCK:  # the resource watcher writes from its own thread
        raw_out.write(framed)
        raw_out.flush()


def _flush_outbound_queue(raw_out: Any, use_headers: bool) -> None:
    for msg in drain_outbound_messages():
        _write_rpc_payload(raw_out, msg, use_headers)


def serve_stdio(in_stream: Any = None, out_stream: Any = None) -> int:
    """Serve MCP JSON-RPC 2.0 over binary stdio (auto-detecting line-delimited vs Content-Length headers)."""
    _ensure_stream_routers()
    raw_in: Any = in_stream if in_stream is not None else getattr(sys.stdin, "buffer", sys.stdin)
    prev_stdout_fb: Any = None
    redirected_stdout_fb = False
    if out_stream is not None:
        raw_out = out_stream
    else:
        raw_out = getattr(sys.stdout, "buffer", sys.stdout)
        if isinstance(sys.stdout, _StreamRouter):
            err_fb = sys.stderr._fallback if isinstance(sys.stderr, _StreamRouter) else sys.stderr
            prev_stdout_fb = sys.stdout.set_fallback(err_fb)
            redirected_stdout_fb = True

    def _emit_watch(msgs: List[Dict[str, Any]]) -> None:
        for msg in msgs:
            _write_rpc_payload(raw_out, msg, _LAST_FRAMING["use_headers"])

    _WATCHER.start(_emit_watch)
    try:
        while True:
            first_line = raw_in.readline()
            if not first_line:
                break
            stripped = first_line.strip()
            if not stripped:
                continue

            use_headers = False
            low_first = stripped.lower()
            if low_first.startswith((b"content-length:", b"content-type:")):
                use_headers = True
                headers: List[bytes] = [stripped]
                eof_in_headers = False
                while True:
                    hdr = raw_in.readline()
                    if not hdr:
                        eof_in_headers = True
                        break
                    if hdr in (b"\r\n", b"\n"):
                        break
                    headers.append(hdr.strip())
                if eof_in_headers:
                    break
                content_len = -1
                for h_line in headers:
                    if h_line.lower().startswith(b"content-length:"):
                        try:
                            content_len = int(h_line.split(b":", 1)[1].strip())
                        except ValueError:
                            content_len = -1
                        break
                if content_len < 0:
                    _write_rpc_payload(
                        raw_out,
                        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Content-Length"}},
                        use_headers,
                    )
                    continue
                if content_len > MAX_CONTENT_LENGTH:
                    _write_rpc_payload(
                        raw_out,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32600,
                                "message": f"Content-Length {content_len} exceeds maximum {MAX_CONTENT_LENGTH}",
                            },
                        },
                        use_headers,
                    )
                    break
                if content_len == 0:
                    body = b""
                else:
                    chunks: List[bytes] = []
                    remaining = content_len
                    while remaining > 0:
                        part = raw_in.read(remaining)
                        if not part:
                            break
                        chunks.append(part)
                        remaining -= len(part)
                    body = b"".join(chunks)
                    if len(body) < content_len:
                        break
            else:
                body = stripped

            _LAST_FRAMING["use_headers"] = use_headers
            try:
                req = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                _write_rpc_payload(
                    raw_out,
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                    use_headers,
                )
                continue

            if isinstance(req, list):
                if not req:
                    _write_rpc_payload(
                        raw_out,
                        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                        use_headers,
                    )
                    continue
                batch_resps: List[Dict[str, Any]] = []
                for item in req:
                    if isinstance(item, dict):
                        item_resp = handle_rpc_request(item)
                        if item_resp is not None:
                            batch_resps.append(item_resp)
                    else:
                        batch_resps.append(
                            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
                        )
                if batch_resps:
                    _write_rpc_payload(raw_out, batch_resps, use_headers)
                _flush_outbound_queue(raw_out, use_headers)
                continue

            if not isinstance(req, dict):
                _write_rpc_payload(
                    raw_out,
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                    use_headers,
                )
                continue

            resp = handle_rpc_request(req)
            if resp is not None:
                _write_rpc_payload(raw_out, resp, use_headers)
            _flush_outbound_queue(raw_out, use_headers)
    finally:
        _WATCHER.stop()
        if redirected_stdout_fb and isinstance(sys.stdout, _StreamRouter):
            sys.stdout.set_fallback(prev_stdout_fb)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Console entry point (`aoci-mcp` / `python aoci_mcp.py [--profile core|sync|full]`)."""
    import argparse

    parser = argparse.ArgumentParser(prog="aoci_mcp.py", description="AOCI stdio MCP server")
    parser.add_argument(
        "--profile",
        choices=sorted(TOOL_PROFILES),
        default=None,
        help="Advertised tool set: core (8 read-only tools), sync (core + 14 sync/maintenance tools), full (default)",
    )
    args = parser.parse_args(argv)
    if args.profile:
        set_tool_profile(args.profile)
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
