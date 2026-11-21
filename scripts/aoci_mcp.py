#!/usr/bin/env python3
"""
Zero-Dependency Stdio MCP Server for AOCI Codebase Index (`aoci_mcp.py`).

Speaks MCP JSON-RPC 2.0 over stdio (auto-detecting newline-delimited JSON or
`Content-Length:` framing) using only the Python 3.8+ standard library.

Keeps parsed `.aoci/aoci.code.txt` entries and the directed FRAS dependency
graph warm in memory (keyed on file `(mtime_ns, size)`), delivering sub-millisecond
`aoci_overview`, `aoci_query` (BM25 + graph), and `aoci_context` lookups without
spawning shell commands or triggering terminal permission prompts.
"""

from __future__ import annotations

from collections import OrderedDict
import contextvars
import fnmatch
import io
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import aoci_index  # noqa: E402

SERVER_NAME = "aoci-index"
SERVER_VERSION = aoci_index.AOCI_VERSION
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


class _MemoryIndexCache:
    """LRU-bounded, mtime-keyed in-memory cache of parsed FRAS volumes, outlines, hubs, and BM25."""

    def __init__(self, max_slots: int = 16) -> None:
        self._max_slots = max(1, max_slots)
        self._lock = threading.RLock()
        self._slots: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._outline_slots: "OrderedDict[str, Tuple[Any, Dict[str, Any]]]" = OrderedDict()
        self._hub_slots: "OrderedDict[str, Tuple[Any, List[Dict[str, Any]]]]" = OrderedDict()

    @staticmethod
    def _canonical_root(root: Path) -> Tuple[Path, str]:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        return resolved, str(resolved)

    def invalidate(self, root: Path) -> None:
        _, root_key = self._canonical_root(root)
        raw_key = str(root)
        with self._lock:
            for key in (root_key, raw_key):
                self._slots.pop(key, None)
                self._outline_slots.pop(key, None)
                self._hub_slots.pop(key, None)

    def get(self, root: Path) -> Optional[Dict[str, Any]]:
        resolved, root_key = self._canonical_root(root)
        aoci_dir = resolved / aoci_index.AOCI_DIR_NAME
        code_path = aoci_dir / aoci_index.CODE_FILE
        with self._lock:
            if not code_path.is_file():
                self.invalidate(root)
                return None
            key = (
                aoci_index._stat_key(code_path),
                aoci_index._stat_key(aoci_dir / aoci_index.MANIFEST_FILE),
                aoci_index._stat_key(aoci_dir / aoci_index.META_FILE),
                aoci_index._stat_key(aoci_dir / aoci_index.GLOSSARY_FILE),
            )
            cached = self._slots.get(root_key)
            if cached is not None and cached.get("key") == key:
                self._slots.move_to_end(root_key)
                return cached

            duplicates: List[str] = []
            entries, malformed = aoci_index.parse_code_volume(code_path, duplicates=duplicates)
            parsed = aoci_index.parse_entries_map(entries)
            deps, dependents = aoci_index.build_fras_graph(entries, parsed_entries=parsed)
            mention_index = aoci_index.PathMentionIndex(sorted(entries.keys()))
            estimated_tokens = max(1, sum(aoci_index.estimate_tokens(ln) for ln in entries.values()) + 20)
            slot: Dict[str, Any] = {
                "key": key,
                "entries": entries,
                "parsed": parsed,
                "malformed": malformed,
                "duplicates": duplicates,
                "deps": deps,
                "dependents": dependents,
                "mention_index": mention_index,
                "bm25": None,
                "glossary": aoci_index.parse_glossary(aoci_dir),
                "project": aoci_index.parse_manifest_field(aoci_dir, "#Project:") or resolved.name,
                "invariants": aoci_index.parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-",
                "git_hdr": aoci_index.parse_manifest_field(aoci_dir, "#Git-Excluded:") or "unknown",
                "domains": aoci_index.parse_declared_domains(aoci_dir),
                "estimated_tokens": estimated_tokens,
            }
            self._slots[root_key] = slot
            self._slots.move_to_end(root_key)
            while len(self._slots) > self._max_slots:
                self._slots.popitem(last=False)
            return slot

    def get_bm25(self, slot: Dict[str, Any]) -> aoci_index.BM25Index:
        with self._lock:
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
        with self._lock:
            if not cache_path.is_file():
                self._outline_slots.pop(root_key, None)
                return None
            key = aoci_index._stat_key(cache_path)
            cached = self._outline_slots.get(root_key)
            if cached is not None and cached[0] == key:
                self._outline_slots.move_to_end(root_key)
                return cached[1]
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
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
            self._outline_slots[root_key] = (key, files_obj)
            self._outline_slots.move_to_end(root_key)
            while len(self._outline_slots) > self._max_slots:
                self._outline_slots.popitem(last=False)
            return files_obj

    def get_hub_records(self, root: Path) -> Optional[List[Dict[str, Any]]]:
        resolved, root_key = self._canonical_root(root)
        hubs_path = resolved / aoci_index.AOCI_DIR_NAME / aoci_index.HUBS_FILE
        with self._lock:
            if not hubs_path.is_file():
                self._hub_slots.pop(root_key, None)
                return None
            key = aoci_index._stat_key(hubs_path)
            cached = self._hub_slots.get(root_key)
            if cached is not None and cached[0] == key:
                self._hub_slots.move_to_end(root_key)
                return cached[1]
            records, _ = aoci_index._hubs.parse_hubs_volume(hubs_path)
            self._hub_slots[root_key] = (key, records)
            self._hub_slots.move_to_end(root_key)
            while len(self._hub_slots) > self._max_slots:
                self._hub_slots.popitem(last=False)
            return records


_CACHE = _MemoryIndexCache()
_CLIENT_ROOT_HINT: Optional[str] = None


def _file_uri_to_path(uri: str) -> Optional[str]:
    if not uri:
        return None
    if not uri.startswith("file://"):
        return uri if Path(uri).is_dir() else None
    try:
        parsed = urllib.parse.urlparse(uri)
        raw_path = urllib.request.url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw_path = f"//{parsed.netloc}{raw_path}"
        return raw_path if raw_path else None
    except Exception:
        return None


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
                "direction": {"type": "string", "enum": ["LR", "TD"], "default": "LR", "description": "Layout direction"},
                "layer": {"type": "string", "description": "Optional Layer code(s) filter"},
                "domain": {"type": "string", "description": "Optional Domain code(s) filter"},
                "min_importance": {"type": "integer", "description": "Optional minimum Importance (1-9)"},
                "path": {"type": "string", "description": "Optional path glob filter"},
                "focus": {"type": "string", "description": "Optional file path to extract neighborhood subgraph"},
                "depth": {"type": "integer", "default": 1, "description": "Hops around `focus`"},
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
                "ack_paths": {"type": "array", "items": {"type": "string"}, "description": "Modified paths to acknowledge (requires `reviewed: true` unless `dry_run`)"},
                "ack_impl_only": {"type": "boolean", "default": False, "description": "Acknowledge `impl_only` modified files (requires `reviewed: true` unless `dry_run`)"},
                "reviewed": {"type": "boolean", "default": False, "description": "Confirm acknowledged files were read and verified"},
                "entries": {"type": "array", "items": {"type": "string"}, "description": "object-fras-v2 entry lines for `upsert`"},
                "entries_file": {"type": "string", "description": "Optional batch file of object-fras-v2 lines for `upsert`"},
                "prune_only": {"type": "boolean", "default": False, "description": "Only prune deleted files in `upsert`"},
                "no_prune": {"type": "boolean", "default": False, "description": "Skip pruning deleted files in `upsert`"},
                "strict_size": {"type": "boolean", "default": False, "description": "Fail instead of auto-fixing Size tier in `upsert`"},
                "drop_malformed": {"type": "boolean", "default": False, "description": "Allow dropping pre-existing malformed lines"},
                "verify": {"type": "boolean", "default": True, "description": "Run trailing `verify` check (default: true)"},
                "full_verify": {"type": "boolean", "default": False, "description": "Run full SHA-256 `verify` instead of `--fast`"},
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
]


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

    def _target(self) -> Any:
        buf = self._var.get()
        return buf if buf is not None else self._fallback

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
        fn = getattr(self._fallback, "reconfigure", None)
        if callable(fn):
            fn(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fallback, name)


def _ensure_stream_routers() -> None:
    if isinstance(sys.stdout, _StreamRouter) and isinstance(sys.stderr, _StreamRouter):
        return
    with _ROUTER_INIT_LOCK:
        if not isinstance(sys.stdout, _StreamRouter):
            sys.stdout = _StreamRouter(_STDOUT_BUF_VAR, sys.stdout)  # type: ignore[assignment]
        if not isinstance(sys.stderr, _StreamRouter):
            sys.stderr = _StreamRouter(_STDERR_BUF_VAR, sys.stderr)  # type: ignore[assignment]


def _run_cli_capture(argv: List[str]) -> Tuple[int, str]:
    _ensure_stream_routers()
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    tok_out = _STDOUT_BUF_VAR.set(out_buf)
    tok_err = _STDERR_BUF_VAR.set(err_buf)
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
        _STDOUT_BUF_VAR.reset(tok_out)
        _STDERR_BUF_VAR.reset(tok_err)
    combined = (out_buf.getvalue() + ("\n" + err_buf.getvalue() if err_buf.getvalue().strip() else "")).strip()
    return rc, combined


def call_tool(name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
    raw_root = args.get("root") or _CLIENT_ROOT_HINT
    root = aoci_index.resolve_root(str(raw_root) if raw_root else None)

    if name == "aoci_init":
        cli_args = ["init", "--root", str(root), "--json"]
        if args.get("project"):
            cli_args.append(f"--project={args['project']}")
        if args.get("locale"):
            cli_args.append(f"--locale={args['locale']}")
        if args.get("force"):
            cli_args.append("--force")
        rc, out = _run_cli_capture(cli_args)
        _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_overview":
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

    if name == "aoci_query":
        slot = _CACHE.get(root)
        if slot is None:
            return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
        parsed_map = slot["parsed"]
        layer_set = aoci_index._parse_code_filter_set(args.get("layer"))
        domain_set = aoci_index._parse_code_filter_set(args.get("domain"))
        min_imp = int(args["min_importance"]) if args.get("min_importance") is not None else 0
        path_glob = args.get("path")
        keyword = str(args["keyword"]).lower() if args.get("keyword") else None
        search_q = args.get("search")
        raw_top = args.get("top")
        top_k = (20 if search_q else 0) if raw_top is None else int(raw_top)
        impact_set: Optional[set] = None
        if args.get("impact"):
            imp_target = aoci_index.normalize_relpath(str(args["impact"]))
            depth = int(args["depth"]) if args.get("depth") is not None else 1
            impact_set = {imp_target} | set(aoci_index.bfs_neighbors(slot["dependents"], [imp_target], depth))

        impacted_tests: Optional[List[Dict[str, Any]]] = None
        tests_for_set: Optional[set] = None
        if args.get("tests_for"):
            tests_target = aoci_index.normalize_relpath(str(args["tests_for"]))
            tests_depth = int(args["depth"]) if args.get("depth") is not None else 3
            impacted_tests = aoci_index.find_impacted_tests(
                root,
                slot["entries"],
                slot["dependents"],
                [tests_target],
                max_depth=tests_depth,
                parsed_entries=parsed_map,
                preloaded_outline_files=_CACHE.get_outline_files(root),
                live_imports=bool(args.get("live_test_imports", False)),
            )
            tests_for_set = {item["path"] for item in impacted_tests}

        has_filters = any(
            (layer_set, domain_set, min_imp, path_glob, keyword, impact_set is not None, tests_for_set is not None)
        )
        cand_paths: Optional[set] = None
        matched_lines: List[str] = []
        if has_filters or not search_q:
            cand_paths = set()
            for rel, item in sorted(parsed_map.items()):
                if layer_set and item.layer not in layer_set:
                    continue
                if domain_set and item.domain not in domain_set:
                    continue
                if min_imp and item.imp < min_imp:
                    continue
                if path_glob and not fnmatch.fnmatchcase(rel, str(path_glob)):
                    continue
                if keyword and keyword not in item.raw.lower():
                    continue
                if impact_set is not None and rel not in impact_set:
                    continue
                if tests_for_set is not None and rel not in tests_for_set:
                    continue
                cand_paths.add(rel)
                matched_lines.append(item.raw)

        if impacted_tests is not None:
            impacted_tests = [item for item in impacted_tests if cand_paths is None or item["path"] in cand_paths]
            if not search_q:
                matched_lines = [item["entry"] for item in impacted_tests]

        if search_q:
            bm25 = _CACHE.get_bm25(slot)
            glossary = slot.get("glossary", ())
            if hasattr(bm25, "search_detailed"):
                hits, expansions = bm25.search_detailed(
                    str(search_q), candidate_paths=cand_paths, top_k=top_k, glossary=glossary
                )
            else:
                ranked = bm25.search(str(search_q), candidate_paths=cand_paths, top_k=top_k)
                hits = [
                    {"path": r, "score": round(sc, 3), "matched_terms": [], "entry": ln}
                    for sc, r, ln in ranked
                ]
                expansions = []
            res_obj: Dict[str, Any] = {
                "count": len(hits),
                "entries": [h["entry"] for h in hits],
                "scores": hits,
            }
            if expansions:
                res_obj["glossary_expansions"] = expansions
            if impacted_tests is not None:
                hit_paths = {h["path"] for h in hits}
                impacted_tests = [item for item in impacted_tests if item["path"] in hit_paths]
        else:
            if top_k > 0:
                matched_lines = matched_lines[:top_k]
                if impacted_tests is not None:
                    impacted_tests = impacted_tests[:top_k]
            res_obj = {"count": len(matched_lines), "entries": matched_lines}
        if impacted_tests is not None:
            res_obj["impacted_tests"] = impacted_tests
        return False, json.dumps(res_obj, indent=2, ensure_ascii=False)

    if name == "aoci_context":
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
        if hub_secs:
            payload["hub_sections"] = hub_secs
        if with_tests:
            payload["impacted_tests"] = aoci_index.find_impacted_tests(
                root,
                slot["entries"],
                slot["dependents"],
                [str(p) for p in raw_paths],
                max_depth=max(3, depth),
                parsed_entries=slot["parsed"],
                preloaded_outline_files=_CACHE.get_outline_files(root),
                live_imports=bool(args.get("live_test_imports", False)),
            )
        return False, json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "aoci_diff":
        slot = _CACHE.get(root)
        depth = int(args["depth"]) if args.get("depth") is not None else 1
        with_tests = bool(args.get("with_tests", False))
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
            preloaded_outline_files=_CACHE.get_outline_files(root) if with_tests else None,
            live_test_imports=bool(args.get("live_test_imports", False)),
        )
        return (not brief.get("ok")), json.dumps(brief, indent=2, ensure_ascii=False)

    if name == "aoci_scan":
        cli_args = ["scan", "--root", str(root), "--json"]
        if args.get("fast", True):
            cli_args.append("--fast")
        if args.get("git_fast"):
            cli_args.append("--git-fast")
        if args.get("auto_restore"):
            cli_args.append("--auto-restore")
        if args.get("check_only"):
            cli_args.append("--check-only")
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

    if name == "aoci_outline":
        paths = [str(p) for p in (args.get("paths") or [])]
        max_items = int(args.get("max_items", 30))
        res = aoci_index.compute_outlines(root, paths, max_items=max_items)
        is_err = bool(res["missing"] or res["rejected"])
        return is_err, json.dumps(res, indent=2, ensure_ascii=False)

    if name == "aoci_scaffold":
        cli_args = ["scaffold", "--root", str(root), "--json"]
        if args.get("unindexed"):
            cli_args.append("--unindexed")
        if args.get("batch"):
            cli_args.extend(["--batch", str(int(args["batch"]))])
        if args.get("batch_size"):
            cli_args.extend(["--batch-size", str(int(args["batch_size"]))])
        if args.get("reset_batches"):
            cli_args.append("--reset-batches")
        if args.get("output"):
            cli_args.append(f"--output={args['output']}")
        paths = [str(p) for p in (args.get("paths") or [])]
        if paths:
            cli_args.extend(["--", *paths])
        rc, out = _run_cli_capture(cli_args)
        return rc != 0, out

    if name == "aoci_ack":
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
            _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_upsert":
        cli_args = ["upsert", "--root", str(root), "--json"]
        if args.get("entries_file"):
            cli_args.append(f"--entries-file={args['entries_file']}")
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
            _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_migrate_renames":
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
            _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_restore":
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
            _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_shard":
        cli_args = ["shard", "--root", str(root), "--json"]
        if args.get("clean"):
            cli_args.append("--clean")
        elif args.get("depth") is not None:
            cli_args.extend(["--depth", str(int(args["depth"]))])
        rc, out = _run_cli_capture(cli_args)
        _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_ensure_git_exclude":
        rc, out = _run_cli_capture(["ensure-git-exclude", "--root", str(root), "--json"])
        _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_lint":
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

    if name == "aoci_verify":
        cli_args = ["verify", "--root", str(root), "--json"]
        if args.get("fast"):
            cli_args.append("--fast")
        rc, out = _run_cli_capture(cli_args)
        return rc != 0, out

    if name == "aoci_audit":
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
        return bool(res.get("error")), json.dumps(res, indent=2, ensure_ascii=False)

    if name == "aoci_export":
        slot = _CACHE.get(root)
        if slot is None:
            return True, f"ERROR: `.aoci/aoci.code.txt` not found at {root}. Run `init` first."
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
        )
        return False, json.dumps(res, indent=2, ensure_ascii=False)

    if name == "aoci_pool":
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

    if name == "aoci_pack":
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
            if cand_diff.get("ok"):
                diff_brief = cand_diff
        bm25 = _CACHE.get_bm25(slot)
        depth_val = int(args["depth"]) if args.get("depth") is not None else 1
        budget_val = int(args["budget"]) if args.get("budget") is not None else 2500
        top_seeds_val = int(args["top_seeds"]) if args.get("top_seeds") is not None else 8
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
            with_tests=bool(args.get("with_tests", False)),
            live_test_imports=bool(args.get("live_test_imports", False)),
        )
        return False, json.dumps(payload, indent=2, ensure_ascii=False)

    if name == "aoci_zoom":
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
            _CACHE.invalidate(root)
        return rc != 0, out

    if name == "aoci_prepare_sync":
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

    if name == "aoci_apply_sync":
        dry_run = bool(args.get("dry_run", False))
        res = aoci_index._apply_sync.run_apply_sync(
            root,
            _run_cli_capture,
            restore=bool(args.get("restore", False)),
            allow_sha_fallback=bool(args.get("allow_sha_fallback", False)),
            migrate_renames=bool(args.get("migrate_renames", False)),
            exact_only=bool(args.get("exact_only", False)),
            ack_paths=[str(p) for p in (args.get("ack_paths") or [])],
            ack_impl_only=bool(args.get("ack_impl_only", False)),
            reviewed=bool(args.get("reviewed", False)),
            entries=[str(e) for e in (args.get("entries") or [])],
            entries_file=args.get("entries_file"),
            prune_only=bool(args.get("prune_only", False)),
            no_prune=bool(args.get("no_prune", False)),
            strict_size=bool(args.get("strict_size", False)),
            drop_malformed=bool(args.get("drop_malformed", False)),
            verify=bool(args.get("verify", True)),
            full_verify=bool(args.get("full_verify", False)),
            dry_run=dry_run,
        )
        if not dry_run:
            _CACHE.invalidate(root)
        return (not res.get("ok")), json.dumps(res, indent=2, ensure_ascii=False)

    if name == "aoci_symbol":
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

    return True, f"Unknown tool: `{name}`"


def _read_resource_uri(uri: str) -> Tuple[Optional[str], Optional[Tuple[int, str]]]:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "aoci":
        return None, (-32602, f"Unsupported resource URI scheme: `{uri}`")
    qs = urllib.parse.parse_qs(parsed.query)
    raw_root = (qs.get("root") or [None])[0] or _CLIENT_ROOT_HINT
    root = aoci_index.resolve_root(str(raw_root) if raw_root else None)
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

    if not target_path.is_file():
        return None, (-32002, f"Resource file does not exist: `{target_path}`")
    return target_path.read_text(encoding="utf-8", errors="replace"), None


def _render_prompt(name: str, arguments: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    root_hint = str(arguments.get("root") or _CLIENT_ROOT_HINT or "<repo>")
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
    global _CLIENT_ROOT_HINT
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") if isinstance(req.get("params"), dict) else {}

    # Notifications carry no `id` and expect no response
    if req_id is None:
        return None

    def _ok(result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _err(code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        root_uri = params.get("rootUri")
        ws_folders = params.get("workspaceFolders")
        if isinstance(ws_folders, list) and len(ws_folders) == 1 and isinstance(ws_folders[0], dict):
            root_uri = ws_folders[0].get("uri") or root_uri
        if isinstance(root_uri, str) and root_uri.strip():
            hint = _file_uri_to_path(root_uri.strip())
            if hint:
                _CLIENT_ROOT_HINT = hint
        client_ver = str(params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION))
        chosen_ver = client_ver if client_ver in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL_VERSION
        return _ok(
            {
                "protocolVersion": chosen_ver,
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        )

    if method == "ping":
        return _ok({})

    if method == "tools/list":
        return _ok({"tools": TOOLS_SPEC})

    if method == "resources/list":
        return _ok({"resources": RESOURCES_SPEC})

    if method == "resources/templates/list":
        return _ok({"resourceTemplates": RESOURCE_TEMPLATES_SPEC})

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
        p_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        prompt_obj, err_msg = _render_prompt(p_name, p_args)
        if err_msg is not None:
            return _err(-32602, err_msg)
        return _ok(prompt_obj)

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            is_err, text = call_tool(tool_name, tool_args)
        except (Exception, SystemExit) as exc:  # Isolation boundary for RPC server stability
            is_err, text = True, f"Internal error in `{tool_name}`: {exc}"
        return _ok({"content": [{"type": "text", "text": text}], "isError": is_err})

    return _err(-32601, f"Method not found: {method}")


def serve_stdio(in_stream: Any = None, out_stream: Any = None) -> int:
    """Serve MCP JSON-RPC 2.0 over binary stdio (auto-detecting line-delimited vs Content-Length headers)."""
    raw_in = in_stream if in_stream is not None else sys.stdin.buffer
    raw_out = out_stream if out_stream is not None else sys.stdout.buffer

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
            while True:
                hdr = raw_in.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
                headers.append(hdr.strip())
            content_len = -1
            for h_line in headers:
                if h_line.lower().startswith(b"content-length:"):
                    try:
                        content_len = int(h_line.split(b":", 1)[1].strip())
                    except ValueError:
                        content_len = -1
                    break
            if content_len < 0:
                continue
            body = raw_in.read(content_len)
            if not body:
                break
        else:
            body = stripped

        try:
            req = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(req, dict):
            continue

        resp = handle_rpc_request(req)
        if resp is None:
            continue
        encoded = json.dumps(resp, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if use_headers:
            raw_out.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
        else:
            raw_out.write(encoded + b"\n")
        raw_out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_stdio())
