#!/usr/bin/env python3
"""
AOCI Fast Codebase Index Manager (aoci_index.py)

Zero-dependency Python 3 CLI for initializing, scanning, incrementally
updating, querying, expanding context, migrating renames, outlining symbols,
summarizing, and verifying a local `.aoci/` AI-Oriented Code Indexing
repository, with strict Git local exclusion (`.git/info/exclude`).

Compatible with both Claude Code and Antigravity workflows.

Subcommands:
  init                Initialize `.aoci/` and enforce `.git/info/exclude`.
  scan | status       Detect drift (added/modified/renamed/deleted/missing/malformed)
                      and generate line-balanced indexing batches.
  upsert              Validate and atomically merge object-fras-v2 entries into
                      `aoci.code.txt` and `baseline.json` (auto-pruning deletions).
  migrate-renames     Migrate exact-content renamed files in `aoci.code.txt`, update
                      R:/A: cross-references, and sync `baseline.json`.
  verify              Full health gate: Git exclusion, format & domain dictionary
                      checks, and zero SHA-256 drift.
  query               Filter `aoci.code.txt` by layer/domain/importance/path/keyword
                      or trace `--impact` neighbors.
  context             Expand architectural subgraph for target file(s): global
                      invariants + target + R: dependencies + A:/reverse callers.
  overview            Print a compact global digest (manifest, meta dictionary,
                      directory rollup, and high-importance core entries).
  outline             Fast static extractor (imports + top-level symbols/signatures)
                      to assist R:/A: authoring alongside reading source files.
  ack                 Re-baseline modified files whose existing FRAS entries remain
                      accurate (auto-refreshes Size tier) without rewriting entries.
  lint                Quality gate for FRAS entries: vague F:, token budget vs.
                      Importance, empty S: on hubs, dangling R:/A: refs, duplicates.
  shard               Split `aoci.code.txt` into per-directory `.aoci/shards/` slices.
  restore             Zero-token restore of FRAS entries from the Git-common-dir cache
                      pool after branch switches, rebases, or new worktrees.
  diff                Generate an architectural impact briefing (FRAS + S: traps +
                      upstream neighbors) for working-tree or Git revision diffs.
  pack                Build a task-driven, token-budgeted context packet (BM25 +
                      glossary + path mentions + diff + R:/A: + S: + open_next).
  scaffold            Pre-fill [LDIS], resolved internal R: imports, and repo-wide A:
                      callers from static analysis (<TODO> F:/S: requires reading code).
  ensure-git-exclude  Idempotently enforce `.aoci/` exclusion in `.git/info/exclude`
                      and synchronize `#Git-Excluded` in `aoci.txt`.
  audit               Cross-check FRAS graph vs static imports, Tarjan SCC
                      cycles, and forbidden `#Layer-Rules:` edges.
  export              Export FRAS dependency graph in Mermaid, Graphviz DOT, or HTML.
  pool                Inspect, export, or import `.pool.sqlite3` semantic cache
                      entries and portable `aoci-semantic-pool-v1` bundles.
  install-git-hooks   Install or remove idempotent local Git hooks
                      (`post-checkout`/`post-merge`/`post-rewrite`/`pre-commit`).
  prepare-sync        Single-pass agent drift briefing (scaffolds, outlines, diffs).
  apply-sync          Execute ordered sync workflow (restore -> migrate-renames ->
                      ack -> upsert -> verify) in one call.
  zoom                Read, scaffold, or upsert symbol-level H-tier sections
                      in `.aoci/aoci.hubs.txt`.
  symbol              Search symbols across hubs, outlines, and FRAS A: exports.
  refs                Locate cross-file symbol references & call sites via FRAS graph pruning.
  doctor              Zero-token mechanical self-healer (duplicates, sizes, dangling refs, static R:).
  batch-pack          Assemble a self-contained subagent work-pack for batch N.
  ci-check            CI-native architecture guard (SARIF 2.1.0, GitHub Actions,
                      PR Markdown, layer redlines, cycles, and PR diff impact).
  eval                Score retrieval + `pack` against task cases (recall@k, MRR,
                      seed/pack recall); compare `bm25` vs opt-in `hybrid` retrieval.

All index writers serialize through `.aoci/.lock` so parallel agents cannot lose
updates.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _configure_bytecode_cache() -> None:
    """
    Cache bytecode (saves recompiling ~25k lines on every CLI/hook process) WITHOUT writing `__pycache__/`
    beside installed skill sources: `.pyc` files go to a per-user `sys.pycache_prefix` directory instead.

    Process-wide, so only entry points call it: this script as `__main__`, and `aoci_hook.py` / `aoci_mcp.py`
    when run as scripts. Plain library imports keep the historical `sys.dont_write_bytecode = True`.
    Opt-outs: `python -B`, `PYTHONDONTWRITEBYTECODE=1`, or `AOCI_NO_PYCACHE=1`. A user-chosen
    `PYTHONPYCACHEPREFIX` is kept as-is, and wheel installs (`site-packages`) keep pip's `__pycache__/`.
    Unwritable or over-long cache paths (Windows without long-path support) silently fall back to no cache.
    """
    if sys.flags.dont_write_bytecode:
        return
    if os.environ.get("AOCI_NO_PYCACHE", "").strip() not in ("", "0"):
        sys.dont_write_bytecode = True
        return
    if not getattr(sys, "pycache_prefix", None):
        here = os.path.normcase(os.path.realpath(__file__))
        if "site-packages" in here or "dist-packages" in here:
            return
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        elif sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Caches")
        else:
            base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        sys.pycache_prefix = os.path.join(base, "aoci-index", "pycache")
    sys.dont_write_bytecode = False


if __name__ == "__main__":
    _configure_bytecode_cache()
else:
    sys.dont_write_bytecode = True

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# `constants` is imported by every subcommand anyway, so its names stay eagerly bound.
from aoci_core.constants import (  # noqa: E402
    ALWAYS_SKIP_DIRS,
    AOCI_DIR_NAME,
    AOCI_VERSION,
    BASELINE_FILE,
    BASELINE_SCHEMA_VERSION,
    BATCH_PLAN_CACHE,
    CACHE_DIR,
    CJK_CHAR_RE,
    CODE_FILE,
    DEFAULT_FORBIDDEN_LAYER_EDGES,
    DEFAULT_MAX_BATCH_LINES,
    DEFAULT_MAX_BYTES,
    DEFAULT_SKIP_DIRS,
    DEFAULT_SKIP_EXTENSIONS,
    DEFAULT_SKIP_FILENAMES,
    DEFAULT_SKIP_SUFFIXES,
    DOMAIN_TOKEN_RE,
    ENTRY_RE,
    EXCLUDE_BEGIN,
    EXCLUDE_END,
    GIT_DISCOVERY_CACHE,
    GIT_EXCLUSION_CACHE,
    GIT_FAST_AUTO_THRESHOLD,
    GIT_HOOK_BEGIN,
    GIT_HOOK_END,
    GLOSSARY_FILE,
    HUBS_FILE,
    IGNORE_FILE,
    IMPORTANCE_TOKEN_BUDGET,
    INSTALL_MANIFEST_NAME,
    LINE_SKETCH_K,
    LOCK_FILE,
    LOCK_STALE_S,
    LOCK_TIMEOUT_S,
    MANAGED_GIT_HOOKS,
    MANIFEST_FILE,
    MAX_SEMANTIC_POOL_ENTRIES,
    META_FILE,
    MIN_RENAME_LINES,
    OUTLINE_CACHE,
    PARALLEL_HASH_THRESHOLD,
    RACY_WINDOW_NS,
    RELPATH_TOKEN_RE,
    ROOT_FILE_TOKEN_RE,
    SCAN_FP_CACHE,
    SESSIONS_DIR,
    SHARDS_DIR,
    SHARDS_INDEX,
    SHARDS_MANIFEST,
    SIG_HASH_EXTS,
    SIMILAR_RENAME_THRESHOLD,
    SKIP_RULES_REV,
    VALID_LAYERS,
    VALID_SIZES,
    VAGUE_F_RE,
    _LIKELY_FILE_EXTS,
    _PATH_SEG_CHAR,
    _PATH_SEG_PATTERN,
    _REF_BOUNDARY_LEFT,
    _REF_BOUNDARY_RIGHT,
    _is_within_root,
    _parse_code_filter_set,
    _resolve_cli_file_path,
    _stat_key,
    atomic_write_text,
    compute_size_tier,
    configure_utf8_streams,
    normalize_relpath,
    utc_now_iso,
    write_text_lf,
)

# Everything else is re-exported lazily (PEP 562): `aoci_index.<name>` imports only the defining
# `aoci_core` module on first use, so `--version`, hooks, and narrow subcommands skip the full
# import graph. Lookups resolve live against the defining module (never a stale snapshot).
_LAZY_MODULES: Dict[str, str] = {
    "_apply_sync": "apply_sync",
    "_audit": "audit",
    "_batch_pack": "batch_pack",
    "_ci_check": "ci_check",
    "_cli_check": "cli_check",
    "_cli_parser": "cli_parser",
    "_cli_read": "cli_read",
    "_cli_write": "cli_write",
    "_cochange": "cochange",
    "_diff_brief": "diff_brief",
    "_discovery": "discovery",
    "_doctor": "doctor",
    "_evalbench": "evalbench",
    "_freshness": "freshness",
    "_drift": "drift",
    "_git_ops": "git_ops",
    "_githooks": "githooks",
    "_graph": "graph",
    "_hubs": "hubs",
    "_hybrid": "hybrid",
    "_ignore": "ignore",
    "_lock": "lock",
    "_pack": "pack",
    "_parsers": "parsers",
    "_pool": "pool",
    "_refs": "refs",
    "_shards": "shards",
    "_symbols": "symbols",
    "_sync_prep": "sync_prep",
    "_test_impact": "test_impact",
    "_write_ops": "write_ops",
}

_LAZY_EXPORTS: Dict[str, Tuple[str, ...]] = {
    "audit": (
        "_infer_layer_and_domain", "audit_architecture", "build_static_import_graph", "compute_lint",
        "compute_outlines", "run_ci_architecture_check", "scaffold_entries",
    ),
    "batches": (
        "_build_pending_items", "_clear_batch_plan", "_load_batch_plan", "_save_batch_plan", "make_batches",
        "resolve_stable_batches",
    ),
    "cli_check": (
        "cmd_audit", "cmd_batch_pack", "cmd_ci_check", "cmd_diff", "cmd_lint", "cmd_prepare_sync", "cmd_scan",
        "cmd_verify",
    ),
    "cli_parser": (
        "build_parser", "run_cli",
    ),
    "cli_read": (
        "_emit_cli_error", "_print_impacted_tests", "_validate_zoom_target_file", "cmd_context", "cmd_export",
        "cmd_outline", "cmd_overview", "cmd_pack", "cmd_query", "cmd_refs", "cmd_symbol", "compute_zoom_read",
        "search_entries_bm25", "search_entries_bm25_detailed",
    ),
    "cli_write": (
        "_capture_internal_cli", "cmd_ack", "cmd_apply_sync", "cmd_doctor", "cmd_ensure_git_exclude", "cmd_init",
        "cmd_install_git_hooks", "cmd_migrate_renames", "cmd_pool", "cmd_restore", "cmd_scaffold", "cmd_shard",
        "cmd_upsert", "cmd_zoom",
    ),
    "diff_brief": (
        "compute_diff_briefing",
    ),
    "discovery": (
        "_attach_dir_stats", "_fingerprint_bytes", "_fingerprint_bytes_norm", "_is_scan_cache_fresh",
        "_line_sketch", "_load_scan_fp_cache", "_matches_nested_gitignore", "_prune_scan_fp_cache",
        "_save_scan_fp_cache", "_scandir_walk", "_sketch_similarity", "_try_git_incremental_scan",
        "attach_line_sketches", "discover_and_fingerprint", "discover_candidates", "fingerprint_file",
        "inspect_and_fingerprint_one",
    ),
    "evalbench": ("load_eval_cases", "run_eval"),
    "freshness": ("check_freshness", "load_baseline_files"),
    "doctor": (
        "run_index_doctor",
    ),
    "drift": (
        "_classify_modified_entry", "_try_heal_racy_baseline", "compute_drift", "drift_signature", "load_baseline",
        "public_drift", "save_baseline",
    ),
    "git_ops": (
        "_GIT_BIN_CACHE", "_demote_git_discovery_paths", "_detect_git_info_fast", "_git_discovery_cache_key",
        "_git_exclusion_cache_key", "_git_head_key", "_has_git_binary", "_parse_porcelain_z_paths",
        "_save_git_discovery_cache", "check_git_exclusion", "detect_git_info", "ensure_git_exclusion",
        "git_exclude_pattern", "install_git_hooks", "read_git_blobs_batch", "resolve_git_hooks_dir", "resolve_root",
        "run_git", "sync_manifest_git_header", "uninstall_git_hooks",
    ),
    "githooks": (
        "_format_hook_python_cmd", "_strip_git_hook_marker", "_upsert_git_hook_block",
    ),
    "graph": (
        "_CAMEL_PART_RE", "_CJK_RUN_RE", "BM25Index", "ParsedEntry", "_load_layer_rules_from_aoci",
        "_sanitize_graph_label", "_tokenize_search", "bfs_neighbors", "bfs_neighbors_with_distance",
        "build_context_subgraph", "build_explicit_mention_index", "build_fras_graph", "check_layer_violations",
        "code_dependency_view", "compile_path_ref_rewriter", "estimate_tokens", "expand_query_with_glossary",
        "export_dependency_graph", "extract_path_refs", "find_dangling_refs", "find_dependency_cycles",
        "lint_entries", "parse_entries_map", "parse_entry", "parse_entry_fields", "parse_glossary",
        "parse_layer_rules", "path_ref_pattern", "rewrite_path_refs", "semantic_word_count",
        "validate_parsed_entry_tags", "FrasGraphState", "build_fras_graph_state", "update_fras_graph_state",
    ),
    "hybrid": ("HybridIndex", "TrigramIndex"),
    "ignore": (
        "IgnoreMatcher", "_glob_to_regex", "_read_ignore_lines", "is_probably_binary_bytes", "load_aociignore",
        "install_manifest_dirs", "load_ignore_matcher", "matches_ignore", "path_skip_reason",
        "under_install_manifest_dir",
    ),
    "lock": (
        "IndexLock", "_pid_is_alive",
    ),
    "pack": (
        "PathMentionIndex", "build_task_pack",
    ),
    "parsers": (
        "_CLEAN_SYM_RE", "_GO_SYM_RE", "_JS_CALL_IMPORT_RE", "_JS_CJS_EXPORT_RE", "_JS_LEADING_EXPORT_RE",
        "_JS_LOCAL_EXPORT_RE", "_JS_REEXPORT_RE", "_JS_REGEX_PREV_CHARS", "_JS_REGEX_PREV_KW", "_JS_RESOLVE_EXTS",
        "_JS_STATIC_IMPORT_RE", "_JS_SYM_RE", "_JVM_CS_SYM_RE", "_RS_SYM_RE", "_SFC_SCRIPT_RE", "_ast_type_str",
        "_clean_symbol_names", "_expand_rust_use_tree", "_extract_js_ts_outline", "_extract_outline_from_bytes",
        "_load_go_module_prefix", "_load_tsconfig_aliases", "_load_tsconfig_file", "_norm_join_posix",
        "_parse_js_export_brace", "_probe_js_path", "_py_fn_sig", "_py_route_decorator", "_static_ecosystem",
        "_strip_c_comments", "_strip_js_comments", "compute_contract_delta", "compute_signature_hash",
        "compute_signature_hash_from_bytes", "extract_file_outline", "extract_signature_payload_from_bytes",
        "resolve_go_package_imports",
    ),
    "pool": (
        "_SHA256_HEX_RE", "_legacy_semantic_pool_json_path", "_load_legacy_semantic_pool_json",
        "_lookup_pool_entries", "_open_semantic_pool", "_validate_pool_entry_row", "count_restorable_from_pool",
        "load_semantic_pool", "semantic_pool_path",
    ),
    "refs": (
        "find_symbol_references",
    ),
    "shards": (
        "_WIN_RESERVED_SHARD_NAMES", "allocate_shard_ids", "parse_code_volume", "render_code_volume",
        "shard_key_for_path", "shard_prefix_for_path", "sync_shards_if_enabled", "write_code_volume",
        "write_shards",
    ),
    "templates": (
        "parse_declared_domains", "parse_manifest_field", "render_ignore_template", "render_manifest_template",
        "render_meta_template",
    ),
    "test_impact": (
        "find_impacted_tests",
    ),
    "write_ops": (
        "LiveFileSet", "_ack_locked", "_migrate_renames_locked", "_populate_missing_sig_hashes",
        "_resolve_disk_case_single", "_upsert_locked", "export_semantic_pool", "import_semantic_pool", "pool_stats",
        "record_semantic_pool_entries", "report_malformed_block", "restore_from_semantic_pool",
        "validate_and_normalize_entry",
    ),
}

_LAZY_ATTRS: Dict[str, str] = {name: mod for mod, names in _LAZY_EXPORTS.items() for name in names}


def _core_module(mod_name: str) -> Any:
    full = f"aoci_core.{mod_name}"
    mod = sys.modules.get(full)
    return mod if mod is not None else importlib.import_module(full)


def __getattr__(name: str) -> Any:
    mod_name = _LAZY_MODULES.get(name)
    if mod_name is not None:
        return _core_module(mod_name)
    mod_name = _LAZY_ATTRS.get(name)
    if mod_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_core_module(mod_name), name)


def __dir__() -> List[str]:
    return sorted(set(globals()) | set(_LAZY_MODULES) | set(_LAZY_ATTRS))


__version__ = AOCI_VERSION


def main(argv: Optional[Sequence[str]] = None) -> int:
    from aoci_core.cli_parser import run_cli

    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
