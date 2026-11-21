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
  batch-pack          Assemble a self-contained subagent work-pack for batch N.
  ci-check            CI-native architecture guard (SARIF 2.1.0, GitHub Actions,
                      PR Markdown, layer redlines, cycles, and PR diff impact).

All index writers serialize through `.aoci/.lock` so parallel agents cannot lose
updates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

sys.dont_write_bytecode = True

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from aoci_core import apply_sync as _apply_sync  # noqa: E402
from aoci_core import audit as _audit  # noqa: E402
from aoci_core import batch_pack as _batch_pack  # noqa: E402
from aoci_core import ci_check as _ci_check  # noqa: E402
from aoci_core import cli_check as _cli_check  # noqa: E402
from aoci_core import cli_parser as _cli_parser  # noqa: E402
from aoci_core import cli_read as _cli_read  # noqa: E402
from aoci_core import cli_write as _cli_write  # noqa: E402
from aoci_core import cochange as _cochange  # noqa: E402
from aoci_core import diff_brief as _diff_brief  # noqa: E402
from aoci_core import discovery as _discovery  # noqa: E402
from aoci_core import drift as _drift  # noqa: E402
from aoci_core import git_ops as _git_ops  # noqa: E402
from aoci_core import githooks as _githooks  # noqa: E402
from aoci_core import graph as _graph  # noqa: E402
from aoci_core import hubs as _hubs  # noqa: E402
from aoci_core import ignore as _ignore  # noqa: E402
from aoci_core import lock as _lock  # noqa: E402
from aoci_core import pack as _pack  # noqa: E402
from aoci_core import parsers as _parsers  # noqa: E402
from aoci_core import pool as _pool  # noqa: E402
from aoci_core import shards as _shards  # noqa: E402
from aoci_core import symbols as _symbols  # noqa: E402
from aoci_core import sync_prep as _sync_prep  # noqa: E402
from aoci_core import test_impact as _test_impact  # noqa: E402
from aoci_core import write_ops as _write_ops  # noqa: E402
from aoci_core.audit import (  # noqa: E402
    _infer_layer_and_domain,
    audit_architecture,
    build_static_import_graph,
    compute_lint,
    compute_outlines,
    run_ci_architecture_check,
    scaffold_entries,
)
from aoci_core.diff_brief import compute_diff_briefing  # noqa: E402
from aoci_core.batches import (  # noqa: E402
    _build_pending_items,
    _clear_batch_plan,
    _load_batch_plan,
    _save_batch_plan,
    make_batches,
    resolve_stable_batches,
)
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
from aoci_core.discovery import (  # noqa: E402
    _attach_dir_stats,
    _fingerprint_bytes,
    _fingerprint_bytes_norm,
    _is_scan_cache_fresh,
    _line_sketch,
    _load_scan_fp_cache,
    _matches_nested_gitignore,
    _prune_scan_fp_cache,
    _save_scan_fp_cache,
    _scandir_walk,
    _sketch_similarity,
    _try_git_incremental_scan,
    discover_and_fingerprint,
    discover_candidates,
    fingerprint_file,
    inspect_and_fingerprint_one,
)
from aoci_core.drift import (  # noqa: E402
    _classify_modified_entry,
    _try_heal_racy_baseline,
    compute_drift,
    drift_signature,
    load_baseline,
    public_drift,
    save_baseline,
)
from aoci_core.git_ops import (  # noqa: E402
    _GIT_BIN_CACHE,
    _demote_git_discovery_paths,
    _detect_git_info_fast,
    _git_discovery_cache_key,
    _git_exclusion_cache_key,
    _git_head_key,
    _has_git_binary,
    _parse_porcelain_z_paths,
    _save_git_discovery_cache,
    check_git_exclusion,
    detect_git_info,
    ensure_git_exclusion,
    git_exclude_pattern,
    install_git_hooks,
    resolve_git_hooks_dir,
    resolve_root,
    run_git,
    sync_manifest_git_header,
    uninstall_git_hooks,
)
from aoci_core.githooks import (  # noqa: E402
    _format_hook_python_cmd,
    _strip_git_hook_marker,
    _upsert_git_hook_block,
)
from aoci_core.graph import (  # noqa: E402
    _CAMEL_PART_RE,
    _CJK_RUN_RE,
    BM25Index,
    ParsedEntry,
    _load_layer_rules_from_aoci,
    _sanitize_graph_label,
    _tokenize_search,
    bfs_neighbors,
    bfs_neighbors_with_distance,
    build_context_subgraph,
    build_explicit_mention_index,
    build_fras_graph,
    check_layer_violations,
    code_dependency_view,
    compile_path_ref_rewriter,
    estimate_tokens,
    expand_query_with_glossary,
    export_dependency_graph,
    extract_path_refs,
    find_dangling_refs,
    find_dependency_cycles,
    lint_entries,
    parse_entries_map,
    parse_entry,
    parse_entry_fields,
    parse_glossary,
    parse_layer_rules,
    path_ref_pattern,
    rewrite_path_refs,
    semantic_word_count,
    validate_parsed_entry_tags,
)
from aoci_core.ignore import (  # noqa: E402
    IgnoreMatcher,
    _glob_to_regex,
    _read_ignore_lines,
    is_probably_binary_bytes,
    load_aociignore,
    install_manifest_dirs,
    load_ignore_matcher,
    matches_ignore,
    path_skip_reason,
    under_install_manifest_dir,
)
from aoci_core.lock import IndexLock, _pid_is_alive  # noqa: E402
from aoci_core.pack import PathMentionIndex, build_task_pack  # noqa: E402
from aoci_core.parsers import (  # noqa: E402
    _CLEAN_SYM_RE,
    _GO_SYM_RE,
    _JS_CALL_IMPORT_RE,
    _JS_CJS_EXPORT_RE,
    _JS_LEADING_EXPORT_RE,
    _JS_LOCAL_EXPORT_RE,
    _JS_REEXPORT_RE,
    _JS_REGEX_PREV_CHARS,
    _JS_REGEX_PREV_KW,
    _JS_RESOLVE_EXTS,
    _JS_STATIC_IMPORT_RE,
    _JS_SYM_RE,
    _JVM_CS_SYM_RE,
    _RS_SYM_RE,
    _SFC_SCRIPT_RE,
    _ast_type_str,
    _clean_symbol_names,
    _expand_rust_use_tree,
    _extract_js_ts_outline,
    _extract_outline_from_bytes,
    _load_go_module_prefix,
    _load_tsconfig_aliases,
    _load_tsconfig_file,
    _norm_join_posix,
    _parse_js_export_brace,
    _probe_js_path,
    _py_fn_sig,
    _py_route_decorator,
    _static_ecosystem,
    _strip_c_comments,
    _strip_js_comments,
    compute_signature_hash,
    compute_signature_hash_from_bytes,
    extract_file_outline,
    resolve_go_package_imports,
)
from aoci_core.pool import (  # noqa: E402
    _SHA256_HEX_RE,
    _legacy_semantic_pool_json_path,
    _load_legacy_semantic_pool_json,
    _lookup_pool_entries,
    _open_semantic_pool,
    _validate_pool_entry_row,
    count_restorable_from_pool,
    load_semantic_pool,
    semantic_pool_path,
)
from aoci_core.shards import (  # noqa: E402
    _WIN_RESERVED_SHARD_NAMES,
    allocate_shard_ids,
    parse_code_volume,
    render_code_volume,
    shard_key_for_path,
    shard_prefix_for_path,
    sync_shards_if_enabled,
    write_code_volume,
    write_shards,
)
from aoci_core.templates import (  # noqa: E402
    parse_declared_domains,
    parse_manifest_field,
    render_ignore_template,
    render_manifest_template,
    render_meta_template,
)
from aoci_core.test_impact import find_impacted_tests  # noqa: E402
from aoci_core.write_ops import (  # noqa: E402
    LiveFileSet,
    _ack_locked,
    _migrate_renames_locked,
    _populate_missing_sig_hashes,
    _resolve_disk_case_single,
    _upsert_locked,
    export_semantic_pool,
    import_semantic_pool,
    pool_stats,
    record_semantic_pool_entries,
    report_malformed_block,
    restore_from_semantic_pool,
    validate_and_normalize_entry,
)

from aoci_core.cli_check import (  # noqa: E402
    cmd_audit,
    cmd_batch_pack,
    cmd_ci_check,
    cmd_diff,
    cmd_lint,
    cmd_prepare_sync,
    cmd_scan,
    cmd_verify,
)
from aoci_core.cli_parser import build_parser, run_cli  # noqa: E402
from aoci_core.cli_read import (  # noqa: E402
    _emit_cli_error,
    _print_impacted_tests,
    _validate_zoom_target_file,
    cmd_context,
    cmd_export,
    cmd_outline,
    cmd_overview,
    cmd_pack,
    cmd_query,
    cmd_symbol,
    compute_zoom_read,
    search_entries_bm25,
    search_entries_bm25_detailed,
)
from aoci_core.cli_write import (  # noqa: E402
    _capture_internal_cli,
    cmd_ack,
    cmd_apply_sync,
    cmd_ensure_git_exclude,
    cmd_init,
    cmd_install_git_hooks,
    cmd_migrate_renames,
    cmd_pool,
    cmd_restore,
    cmd_scaffold,
    cmd_shard,
    cmd_upsert,
    cmd_zoom,
)

__version__ = AOCI_VERSION


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_utf8_streams(include_stdin=(argv is None))
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
