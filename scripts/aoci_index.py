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

All index writers serialize through `.aoci/.lock` so parallel agents cannot lose
updates.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fnmatch
import hashlib
import heapq
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.dont_write_bytecode = True

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from aoci_core import apply_sync as _apply_sync  # noqa: E402
from aoci_core import cochange as _cochange  # noqa: E402
from aoci_core import githooks as _githooks  # noqa: E402
from aoci_core import graph as _graph  # noqa: E402
from aoci_core import hubs as _hubs  # noqa: E402
from aoci_core import ignore as _ignore  # noqa: E402
from aoci_core import pack as _pack  # noqa: E402
from aoci_core import parsers as _parsers  # noqa: E402
from aoci_core import pool as _pool  # noqa: E402
from aoci_core import shards as _shards  # noqa: E402
from aoci_core import symbols as _symbols  # noqa: E402
from aoci_core import sync_prep as _sync_prep  # noqa: E402
from aoci_core import test_impact as _test_impact  # noqa: E402
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
    load_ignore_matcher,
    matches_ignore,
    path_skip_reason,
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

__version__ = AOCI_VERSION


# Cross-process lock (`_pid_is_alive`, `IndexLock`) is imported from `aoci_core.lock`.


# `IgnoreMatcher` & `.aociignore` helpers are imported from `aoci_core.ignore`.


def run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


_GIT_BIN_CACHE: Tuple[Optional[str], Optional[bool]] = (None, None)


def _has_git_binary() -> bool:
    global _GIT_BIN_CACHE
    cur_path = os.environ.get("PATH", "")
    if _GIT_BIN_CACHE[0] == cur_path and _GIT_BIN_CACHE[1] is not None:
        return _GIT_BIN_CACHE[1]
    found = shutil.which("git") is not None
    _GIT_BIN_CACHE = (cur_path, found)
    return found


def _detect_git_info_fast(start_dir: Path) -> Optional[Dict[str, Any]]:
    """
    Pure-Python `.git` topology resolver for standard repos, worktrees (`commondir`), and submodules.
    Eliminates a ~55ms `git rev-parse` subprocess on every CLI/hook call; returns `None` on exotic
    configs (`GIT_*` env overrides, bare repos, or paths inside `.git/`) to fall back to `git`.
    """
    if any(k in os.environ for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")):
        return None
    if not _has_git_binary():
        return {"is_git": False, "git_available": False}
    try:
        cur = start_dir.resolve()
    except OSError:
        return None
    if ".git" in cur.parts:
        return None

    for cand in (cur, *cur.parents):
        dot_git = cand / ".git"
        try:
            if dot_git.is_dir():
                git_dir = dot_git.resolve()
                common_dir = git_dir
            elif dot_git.is_file():
                first = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
                if not first or not first[0].lower().startswith("gitdir:"):
                    return None
                raw_gd = first[0].split(":", 1)[1].strip()
                gd_path = Path(raw_gd)
                git_dir = (gd_path if gd_path.is_absolute() else (cand / gd_path)).resolve()
                common_dir = git_dir
                common_file = git_dir / "commondir"
                if common_file.is_file():
                    raw_cd = common_file.read_text(encoding="utf-8", errors="replace").strip()
                    if raw_cd:
                        cd_path = Path(raw_cd)
                        common_dir = (cd_path if cd_path.is_absolute() else (git_dir / cd_path)).resolve()
            else:
                continue
        except OSError:
            return None

        if not (git_dir / "HEAD").is_file():
            return None
        cfg_file = common_dir / "config"
        if cfg_file.is_file():
            try:
                cfg_txt = cfg_file.read_text(encoding="utf-8", errors="replace")
                if re.search(r"(?im)^\s*bare\s*=\s*true\b", cfg_txt):
                    return None
            except OSError:
                return None

        return {
            "is_git": True,
            "git_available": True,
            "toplevel": cand,
            "common_dir": common_dir,
            "exclude_file": common_dir / "info" / "exclude",
            "index_file": git_dir / "index",
        }
    return {"is_git": False, "git_available": True}


def detect_git_info(start_dir: Path) -> Dict[str, Any]:
    """Detect Git work tree, toplevel, `info/exclude`, and index paths (pure-Python fast path + `git` fallback)."""
    fast = _detect_git_info_fast(start_dir)
    if fast is not None:
        return fast
    try:
        res = run_git(
            [
                "rev-parse",
                "--is-inside-work-tree",
                "--show-toplevel",
                "--git-path",
                "info/exclude",
                "--git-path",
                "index",
                "--git-common-dir",
            ],
            start_dir,
        )
    except FileNotFoundError:
        return {"is_git": False, "git_available": False}

    if res.returncode != 0:
        return {"is_git": False, "git_available": True}

    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if len(lines) < 3 or lines[0].lower() != "true":
        return {"is_git": False, "git_available": True}

    toplevel = Path(lines[1]).resolve()

    def _abs(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (start_dir / p).resolve()

    info: Dict[str, Any] = {
        "is_git": True,
        "git_available": True,
        "toplevel": toplevel,
        "exclude_file": _abs(lines[2]),
    }
    if len(lines) >= 4:
        info["index_file"] = _abs(lines[3])
    if len(lines) >= 5:
        info["common_dir"] = _abs(lines[4])
    return info


def resolve_root(root_arg: Optional[str]) -> Path:
    if root_arg:
        return Path(root_arg).resolve()
    cwd = Path.cwd().resolve()
    git_info = detect_git_info(cwd)
    if git_info.get("is_git"):
        return git_info["toplevel"]
    return cwd


def git_exclude_pattern(root: Path, git_toplevel: Path) -> str:
    """Return a repo-root-anchored ignore pattern for `.aoci/` (e.g. `/.aoci/` or `/sub/.aoci/`)."""
    try:
        rel = root.resolve().relative_to(git_toplevel.resolve()).as_posix()
    except ValueError:
        rel = ""
    if not rel or rel == ".":
        return f"/{AOCI_DIR_NAME}/"
    return f"/{rel.strip('/')}/{AOCI_DIR_NAME}/"


def _git_exclusion_cache_key(root: Path, git_info: Dict[str, Any]) -> Dict[str, Any]:
    toplevel: Path = git_info["toplevel"]
    return {
        "root": str(root),
        "exclude": _stat_key(git_info.get("exclude_file")),
        "index": _stat_key(git_info.get("index_file")),
        "gitignore": _stat_key(toplevel / ".gitignore"),
        "sub_gitignore": _stat_key(root / ".gitignore") if root != toplevel else None,
    }


def check_git_exclusion(
    root: Path,
    git_info: Dict[str, Any],
    check_status: bool = False,
    use_cache: bool = True,
    write_cache: bool = True,
) -> Dict[str, Any]:
    """
    Verify `.aoci/` is ignored by Git and contains zero tracked files (and optionally zero `git status` leaks).
    Positive results are cached in `.aoci/.cache/` keyed on exclude/index/.gitignore stat fingerprints,
    so hot paths (hooks, `scan --fast`) skip two `git` subprocesses when nothing Git-related changed.
    """
    if not git_info.get("is_git"):
        return {
            "is_git": False,
            "excluded": True,
            "tracked_files": [],
            "status_leaks": [],
            "pattern": None,
            "exclude_file": None,
            "ok": True,
        }

    toplevel: Path = git_info["toplevel"]
    pattern = git_exclude_pattern(root, toplevel)
    aoci_rel = pattern.lstrip("/").rstrip("/")

    cache_path = root / AOCI_DIR_NAME / CACHE_DIR / GIT_EXCLUSION_CACHE
    cache_ok = use_cache and not check_status and "index_file" in git_info
    cache_key: Optional[Dict[str, Any]] = None
    if cache_ok:
        cache_key = _git_exclusion_cache_key(root, git_info)
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("key") == cache_key:
                return {
                    "is_git": True,
                    "excluded": True,
                    "tracked_files": [],
                    "status_leaks": [],
                    "pattern": pattern,
                    "exclude_file": str(git_info["exclude_file"]),
                    "ok": True,
                    "cached": True,
                }
        except (OSError, ValueError):
            pass

    # Probe a concrete child path so directory rules match reliably across Git versions
    probe_path = f"{aoci_rel}/{MANIFEST_FILE}"
    ign_res = run_git(["check-ignore", "-q", probe_path], toplevel)
    excluded = ign_res.returncode == 0

    ls_res = run_git(["ls-files", "-z", "--", aoci_rel], toplevel)
    tracked = [item.strip() for item in ls_res.stdout.split("\0") if item.strip()]

    status_leaks: List[str] = []
    if check_status:
        st_res = run_git(["status", "--porcelain=v1", "-z", "--", aoci_rel], toplevel)
        status_leaks = [item.rstrip() for item in st_res.stdout.split("\0") if item.strip()]

    ok = excluded and len(tracked) == 0 and len(status_leaks) == 0
    if ok and write_cache and (root / AOCI_DIR_NAME).is_dir():
        try:
            key = cache_key or _git_exclusion_cache_key(root, git_info)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cache_path, json.dumps({"key": key}))
        except OSError:
            pass

    return {
        "is_git": True,
        "excluded": excluded,
        "tracked_files": tracked,
        "status_leaks": status_leaks,
        "pattern": pattern,
        "exclude_file": str(git_info["exclude_file"]),
        "ok": ok,
    }


def sync_manifest_git_header(root: Path, git_excluded: bool) -> None:
    """Keep `#Git-Excluded:` in `.aoci/aoci.txt` synchronized if Git was initialized later."""
    manifest_path = root / AOCI_DIR_NAME / MANIFEST_FILE
    if not manifest_path.exists():
        return
    desired = (
        "#Git-Excluded: true (.git/info/exclude)"
        if git_excluded
        else "#Git-Excluded: n/a (non-git workspace)"
    )
    text = manifest_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    updated = False
    for idx, line in enumerate(lines):
        if line.startswith("#Git-Excluded:"):
            if line.strip() != desired:
                lines[idx] = desired
                updated = True
            break
    if updated:
        atomic_write_text(manifest_path, "\n".join(lines) + "\n")


def ensure_git_exclusion(
    root: Path,
    git_info: Dict[str, Any],
    check_status: bool = False,
) -> Dict[str, Any]:
    """Idempotently add `.aoci/` to `.git/info/exclude` and verify with Git."""
    status = check_git_exclusion(root, git_info, check_status=check_status)
    if not status["is_git"]:
        status["changed"] = False
        return status

    exclude_file: Path = git_info["exclude_file"]
    pattern: str = status["pattern"]
    changed = False

    if not status["excluded"]:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text(encoding="utf-8", errors="replace") if exclude_file.exists() else ""
        lines = existing.splitlines()
        bare_patterns = {pattern, pattern.lstrip("/"), pattern.rstrip("/"), pattern.strip("/")}
        if not any(line.strip() in bare_patterns for line in lines):
            block = f"{EXCLUDE_BEGIN}\n{pattern}\n{EXCLUDE_END}\n"
            if existing and not existing.endswith("\n"):
                existing += "\n"
            if existing and not existing.endswith("\n\n"):
                existing += "\n"
            write_text_lf(exclude_file, existing + block)
            changed = True

    post = check_git_exclusion(root, git_info, check_status=check_status) if changed else status
    post["changed"] = changed
    if post["excluded"]:
        sync_manifest_git_header(root, True)
    return post


# Manifest/meta parsing helpers are imported from `aoci_core.templates`.


def _matches_nested_gitignore(
    rel: str,
    is_dir: bool,
    nested_ignores: Sequence[Tuple[str, IgnoreMatcher]],
) -> bool:
    for scope_prefix, matcher in nested_ignores:
        sub_rel = rel[len(scope_prefix) + 1 :] if scope_prefix else rel
        if sub_rel and matcher.is_ignored(sub_rel, is_dir=is_dir):
            return True
    return False


def _scandir_walk(
    base_dir: Path,
    rel_prefix: str,
    extra_ignores: Any,
    out: List[Tuple[str, Optional[os.stat_result]]],
    *,
    honor_nested_gitignore: bool = False,
    _nested_ignores: Tuple[Tuple[str, IgnoreMatcher], ...] = (),
) -> None:
    """Fast `os.scandir` recursive walker reusing OS-cached `DirEntry.stat()` on Windows."""
    if honor_nested_gitignore and rel_prefix:
        gi_path = base_dir / ".gitignore"
        if gi_path.is_file():
            gi_lines = _read_ignore_lines(gi_path)
            if gi_lines:
                _nested_ignores = (*_nested_ignores, (rel_prefix.rstrip("/"), IgnoreMatcher(gi_lines)))

    try:
        with os.scandir(base_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return

    for entry in entries:
        name = entry.name
        rel = f"{rel_prefix}{name}"
        try:
            if entry.is_dir(follow_symlinks=False):
                if name in DEFAULT_SKIP_DIRS:
                    continue
                if extra_ignores and matches_ignore(rel + "/", extra_ignores):
                    continue
                if _nested_ignores and _matches_nested_gitignore(rel, True, _nested_ignores):
                    continue
                _scandir_walk(
                    Path(entry.path),
                    f"{rel}/",
                    extra_ignores,
                    out,
                    honor_nested_gitignore=honor_nested_gitignore,
                    _nested_ignores=_nested_ignores,
                )
            elif entry.is_file(follow_symlinks=False):
                if _nested_ignores and _matches_nested_gitignore(rel, False, _nested_ignores):
                    continue
                st = entry.stat(follow_symlinks=False)
                out.append((rel, st))
        except OSError:
            continue


def _attach_dir_stats(root: Path, candidates: List[Tuple[str, Optional[os.stat_result]]]) -> List[Tuple[str, Optional[os.stat_result]]]:
    """
    Windows fast path for Git mode: one `os.scandir` per directory returns stat data straight from
    the directory enumeration (FindNextFileW), replacing one `os.stat` syscall per file.
    Symlinks and any names not found keep `None` and fall back to `os.stat`.
    """
    by_dir: Dict[str, Set[str]] = {}
    for rel, _ in candidates:
        parent, _, name = rel.rpartition("/")
        by_dir.setdefault(parent, set()).add(name)

    stats: Dict[str, os.stat_result] = {}
    for parent, names in by_dir.items():
        base = root / parent if parent else root
        if len(names) <= 2:
            # Sparse directory fast path: avoid enumerating thousands of ignored files via scandir
            for name in names:
                try:
                    lst = os.lstat(base / name)
                    if stat.S_ISREG(lst.st_mode):
                        stats[f"{parent}/{name}" if parent else name] = lst
                except OSError:
                    continue
            continue
        try:
            with os.scandir(base) as it:
                for entry in it:
                    if entry.name in names:
                        try:
                            if entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                                stats[f"{parent}/{entry.name}" if parent else entry.name] = entry.stat(
                                    follow_symlinks=False
                                )
                        except OSError:
                            continue
        except OSError:
            continue
    return [(rel, stats.get(rel)) for rel, _ in candidates]


def discover_candidates(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    tracked_out: Optional[Set[str]] = None,
) -> Tuple[List[Tuple[str, Optional[os.stat_result]]], Dict[str, int]]:
    """
    Discover candidate files as `(rel_posix, optional_stat_result)` after fast path-level filtering.
    When `tracked_out` is passed in Git mode, fills it with tracked paths from the same single `git ls-files -t` call.
    """
    raw_candidates: List[Tuple[str, Optional[os.stat_result]]] = []
    skipped: Dict[str, int] = {}
    used_git = False

    if git_info.get("is_git"):
        res = run_git(["ls-files", "-t", "--cached", "--others", "--exclude-standard", "-z", "--", "."], root)
        if res.returncode == 0:
            used_git = True
            seen: Set[str] = set()
            for item in res.stdout.split("\0"):
                if not item:
                    continue
                is_untracked = item.startswith("? ")
                raw_rel = item[2:] if len(item) > 2 and item[1] == " " else item
                norm = normalize_relpath(raw_rel)
                if norm:
                    if tracked_out is not None and not is_untracked:
                        tracked_out.add(norm)
                    if norm not in seen:
                        seen.add(norm)
                        raw_candidates.append((norm, None))
            raw_candidates.sort(key=lambda pair: pair[0])
        else:
            git_info = {"is_git": False}

    if not git_info.get("is_git"):
        _scandir_walk(root, "", extra_ignores, raw_candidates, honor_nested_gitignore=True)
        raw_candidates.sort(key=lambda pair: pair[0])

    filtered: List[Tuple[str, Optional[os.stat_result]]] = []
    for rel, st in raw_candidates:
        reason = path_skip_reason(rel, extra_ignores)
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        filtered.append((rel, st))

    if used_git and os.name == "nt" and filtered:
        filtered = _attach_dir_stats(root, filtered)

    return filtered, skipped


def _line_sketch(normalized: bytes) -> Optional[List[int]]:
    """
    Bottom-k CRC32 MinHash sketch over distinct non-trivial lines (>=4 stripped bytes).
    Enables fast, zero-source-storage similarity matching for moved+edited files.
    """
    hashes: Set[int] = set()
    for raw_ln in normalized.split(b"\n"):
        stripped = raw_ln.strip()
        if len(stripped) >= 4:
            hashes.add(zlib.crc32(stripped) & 0xFFFFFFFF)
    if len(hashes) < MIN_RENAME_LINES:
        return None
    return heapq.nsmallest(LINE_SKETCH_K, hashes)


def _sketch_similarity(sk_a: Optional[Sequence[int]], sk_b: Optional[Sequence[int]]) -> float:
    if not sk_a or not sk_b:
        return 0.0
    set_a = set(sk_a)
    set_b = set(sk_b)
    union_top = set(heapq.nsmallest(LINE_SKETCH_K, set_a | set_b))
    if not union_top:
        return 0.0
    shared = union_top & set_a & set_b
    if len(shared) < MIN_RENAME_LINES:
        return 0.0
    return len(shared) / len(union_top)


def _fingerprint_bytes_norm(normalized: bytes, st: os.stat_result) -> Dict[str, Any]:
    lines = 0 if not normalized else normalized.count(b"\n") + (0 if normalized.endswith(b"\n") else 1)
    fp: Dict[str, Any] = {
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "lines": lines,
        "bytes": st.st_size,
        "size_tier": compute_size_tier(lines),
        "mtime_ns": st.st_mtime_ns,
    }
    if lines >= MIN_RENAME_LINES:
        sketch = _line_sketch(normalized)
        if sketch is not None:
            fp["sketch"] = sketch
    # Racy-clean guard: a write landing in the same timestamp tick after hashing would keep
    # (mtime, size) identical, so never trust the stat shortcut for freshly modified files.
    if time.time_ns() - st.st_mtime_ns < RACY_WINDOW_NS:
        fp["racy"] = True
    return fp


def _fingerprint_bytes(raw: bytes, st: os.stat_result) -> Dict[str, Any]:
    normalized = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    return _fingerprint_bytes_norm(normalized, st)


def inspect_and_fingerprint_one(
    root: Path,
    rel: str,
    pre_stat: Optional[os.stat_result],
    cached: Optional[Dict[str, Any]],
    fast_mtime: bool,
    max_bytes: int,
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """
    Single-stat, single-read file classifier + fingerprinter.
    On fast_mtime cache hit: ZERO file opens and ZERO SHA-256 work.
    Returns `(rel, fingerprint_or_None, skip_reason_or_None)`.
    """
    full = root / rel
    st = pre_stat
    if st is None:
        try:
            st = os.lstat(full)
        except OSError:
            return rel, None, "missing"

    if stat.S_ISLNK(st.st_mode):
        return rel, None, "symlink"
    if not stat.S_ISREG(st.st_mode):
        return rel, None, "not-regular-file"
    if st.st_size > max_bytes:
        return rel, None, f"too-large(>{max_bytes}B)"

    # Fast cache hit: already validated text file with unchanged mtime_ns and byte size (never racy)
    if (
        fast_mtime
        and cached is not None
        and not cached.get("racy")
        and cached.get("mtime_ns") == st.st_mtime_ns
        and cached.get("bytes") == st.st_size
        and "sha256" in cached
        and "lines" in cached
    ):
        fp = dict(cached)
        fp["size_tier"] = compute_size_tier(int(fp["lines"]))
        return rel, fp, None

    try:
        raw = full.read_bytes()
    except OSError:
        return rel, None, "unreadable"

    if is_probably_binary_bytes(raw[:8192]):
        return rel, None, "binary-content"

    normalized = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    fp = _fingerprint_bytes_norm(normalized, st)
    if (
        cached is not None
        and fp["sha256"] != cached.get("sha256")
        and isinstance(cached.get("sig_hash"), str)
    ):
        fp["_cur_sig_hash"] = compute_signature_hash_from_bytes(normalized, full.suffix.lower(), rel)
    return rel, fp, None


def _git_head_key(git_info: Dict[str, Any]) -> Optional[str]:
    index_file = git_info.get("index_file")
    if not isinstance(index_file, Path):
        return None
    git_dir = index_file.parent
    head_file = git_dir / "HEAD"
    try:
        head_raw = head_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if head_raw.startswith("ref:"):
        ref_rel = head_raw.split(":", 1)[1].strip()
        common_dir = git_info.get("common_dir")
        for base in (git_dir, common_dir if isinstance(common_dir, Path) else None):
            if base is None:
                continue
            ref_path = base / ref_rel
            if ref_path.is_file():
                try:
                    return f"{head_raw}:{ref_path.read_text(encoding='utf-8', errors='replace').strip()}"
                except OSError:
                    break
        packed = (common_dir if isinstance(common_dir, Path) else git_dir) / "packed-refs"
        return f"{head_raw}:packed={_stat_key(packed)}"
    return head_raw


def _git_discovery_cache_key(root: Path, git_info: Dict[str, Any], max_bytes: int) -> Optional[Dict[str, Any]]:
    if not git_info.get("is_git") or git_info.get("toplevel") != root or "index_file" not in git_info:
        return None
    return {
        "head": _git_head_key(git_info),
        "index": _stat_key(git_info.get("index_file")),
        "exclude": _stat_key(git_info.get("exclude_file")),
        "gitignore": _stat_key(root / ".gitignore"),
        "aociignore": _stat_key(root / AOCI_DIR_NAME / IGNORE_FILE),
        "max_bytes": max_bytes,
    }


def _parse_porcelain_z_paths(stdout: str) -> Set[str]:
    """Extract all repository-relative paths reported by `git status --porcelain=v1 -z` (incl. R/C sources)."""
    tokens = stdout.split("\0")
    paths: Set[str] = set()
    idx = 0
    while idx < len(tokens):
        rec = tokens[idx]
        idx += 1
        if len(rec) < 4:
            continue
        xy = rec[:2]
        p1 = normalize_relpath(rec[3:])
        if p1:
            paths.add(p1)
        if ("R" in xy or "C" in xy) and idx < len(tokens):
            p2 = normalize_relpath(tokens[idx])
            idx += 1
            if p2:
                paths.add(p2)
    return paths


def _save_git_discovery_cache(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    base_files: Dict[str, Any],
    fingerprints: Dict[str, Dict[str, Any]],
    max_bytes: int,
    tracked_paths: Optional[Set[str]] = None,
) -> None:
    key = _git_discovery_cache_key(root, git_info, max_bytes)
    aoci_dir = root / AOCI_DIR_NAME
    if key is None or not aoci_dir.is_dir():
        return
    if tracked_paths is None:
        res = run_git(["ls-files", "--cached", "-z", "--", "."], root)
        if res.returncode != 0:
            return
        raw_tracked = [normalize_relpath(item) for item in res.stdout.split("\0") if item]
    else:
        raw_tracked = sorted(tracked_paths)

    # Only files that are BOTH clean in Git status AND match the AOCI baseline may skip future reads.
    # Otherwise, an uncommitted file indexed in AOCI and later reverted to Git's committed version
    # would look clean to `git status` while diverging from `baseline.json`.
    st_res = run_git(
        ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=no", "--", "."],
        root,
    )
    if st_res.returncode != 0:
        return
    git_dirty = _parse_porcelain_z_paths(st_res.stdout)

    tracked_clean: List[str] = []
    tracked_recheck: List[str] = []
    skipped_tracked: Dict[str, int] = {}
    seen: Set[str] = set()
    for rel in raw_tracked:
        if not rel or rel in seen:
            continue
        seen.add(rel)
        reason = path_skip_reason(rel, extra_ignores)
        if reason is not None:
            skipped_tracked[reason] = skipped_tracked.get(reason, 0) + 1
            continue
        fp = fingerprints.get(rel)
        cached = base_files.get(rel)
        if (
            rel not in git_dirty
            and fp is not None
            and cached is not None
            and not fp.get("racy")
            and not cached.get("racy")
            and fp["sha256"] == cached.get("sha256")
        ):
            tracked_clean.append(rel)
        else:
            tracked_recheck.append(rel)

    payload = {
        "key": key,
        "tracked_clean": sorted(tracked_clean),
        "tracked_recheck": sorted(tracked_recheck),
        "skipped_tracked": skipped_tracked,
    }
    cache_path = aoci_dir / CACHE_DIR / GIT_DISCOVERY_CACHE
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache_path, json.dumps(payload, separators=(",", ":")))
    except OSError:
        pass


def _demote_git_discovery_paths(aoci_dir: Path, touched_paths: Sequence[str]) -> None:
    """
    Incrementally move edited/upserted/acked paths from `tracked_clean` into `tracked_recheck`
    so single-file writes keep the 99%+ untouched clean table warm instead of deleting the cache.
    """
    cache_path = aoci_dir / CACHE_DIR / GIT_DISCOVERY_CACHE
    if not touched_paths or not cache_path.is_file():
        return
    if len(touched_paths) >= 256:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            return
        touched_set = set(touched_paths)
        clean_list = cached.get("tracked_clean", [])
        recheck_set = set(cached.get("tracked_recheck", []))
        moved = False
        new_clean: List[str] = []
        for rel in clean_list:
            if rel in touched_set:
                recheck_set.add(rel)
                moved = True
            else:
                new_clean.append(rel)
        if moved:
            cached["tracked_clean"] = new_clean
            cached["tracked_recheck"] = sorted(recheck_set)
            atomic_write_text(cache_path, json.dumps(cached, separators=(",", ":")))
    except (OSError, ValueError):
        pass


def _try_git_incremental_scan(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    base_files: Dict[str, Any],
    max_bytes: int,
) -> Optional[Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]]:
    """
    10k-file Git fast path: when `.git/index` and ignore files match cached stats, run a single
    `git status --porcelain=v1 -z -uall` (which uses Git's native multi-threaded C stat cache)
    instead of `git ls-files` + statting thousands of unchanged tracked files in Python.
    """
    key = _git_discovery_cache_key(root, git_info, max_bytes)
    if key is None:
        return None
    cache_path = root / AOCI_DIR_NAME / CACHE_DIR / GIT_DISCOVERY_CACHE
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict) or cached.get("key") != key:
        return None

    res = run_git(
        ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
        root,
    )
    if res.returncode != 0:
        return None

    status_paths = _parse_porcelain_z_paths(res.stdout)

    # Nested `.gitignore` edited -> fall back to full discovery so new ignore rules apply
    if any(p == ".gitignore" or p.endswith("/.gitignore") for p in status_paths):
        return None

    skipped: Dict[str, int] = dict(cached.get("skipped_tracked", {}))
    fingerprints: Dict[str, Dict[str, Any]] = {}
    recheck_orig: Set[str] = set(cached.get("tracked_recheck", []))
    to_probe: Set[str] = set(recheck_orig)

    for rel in cached.get("tracked_clean", []):
        c = base_files.get(rel)
        if rel not in status_paths and c is not None and not c.get("racy") and "sha256" in c and "lines" in c:
            fp = dict(c)
            fp["size_tier"] = compute_size_tier(int(fp["lines"]))
            fingerprints[rel] = fp
        else:
            to_probe.add(rel)

    for rel in status_paths:
        reason = path_skip_reason(rel, extra_ignores)
        if reason is not None:
            if rel not in to_probe:
                skipped[reason] = skipped.get(reason, 0) + 1
            continue
        to_probe.add(rel)

    sorted_probe = sorted(to_probe)
    probe_results: List[Tuple[str, Optional[Dict[str, Any]], Optional[str]]]
    if len(sorted_probe) >= PARALLEL_HASH_THRESHOLD:
        workers = min(8, max(2, (os.cpu_count() or 4) * 2))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    inspect_and_fingerprint_one,
                    root,
                    rel,
                    None,
                    base_files.get(rel),
                    True,
                    max_bytes,
                )
                for rel in sorted_probe
            ]
            probe_results = [fut.result() for fut in futures]
    else:
        probe_results = [
            inspect_and_fingerprint_one(root, rel, None, base_files.get(rel), True, max_bytes)
            for rel in sorted_probe
        ]

    promoted: List[str] = []
    for rel, fp, reason in probe_results:
        c_base = base_files.get(rel)
        if reason is not None:
            if reason != "missing":
                skipped[reason] = skipped.get(reason, 0) + 1
        elif fp is not None:
            fingerprints[rel] = fp
            if (
                rel in recheck_orig
                and rel not in status_paths
                and c_base is not None
                and not fp.get("racy")
                and not c_base.get("racy")
                and fp["sha256"] == c_base.get("sha256")
            ):
                promoted.append(rel)

    if promoted:
        try:
            prom_set = set(promoted)
            cached["tracked_clean"] = sorted(set(cached.get("tracked_clean", [])) | prom_set)
            cached["tracked_recheck"] = sorted(recheck_orig - prom_set)
            atomic_write_text(cache_path, json.dumps(cached, separators=(",", ":")))
        except OSError:
            pass

    return fingerprints, skipped


def _is_scan_cache_fresh(fp: Dict[str, Any], now_ns: Optional[int] = None) -> Dict[str, Any]:
    """
    Normalize an unindexed fingerprint for `.aoci/.cache/scan_fps.json`.
    Clears `racy` once sub-second mtime timestamps have stabilized (>5ms since write) so rapid
    multi-batch cold bootstrap skips redundant hashing while keeping 1s coarse FS protections intact.
    """
    cleaned = {k: v for k, v in fp.items() if k != "size_tier" and not k.startswith("_")}
    if cleaned.get("racy"):
        mtime_ns = int(cleaned.get("mtime_ns", 0) or 0)
        ts_now = time.time_ns() if now_ns is None else now_ns
        if (mtime_ns % 1_000_000_000 != 0 and ts_now - mtime_ns >= 5_000_000) or (
            ts_now - mtime_ns >= RACY_WINDOW_NS
        ):
            cleaned.pop("racy", None)
    return cleaned


def _load_scan_fp_cache(aoci_dir: Path, max_bytes: int) -> Dict[str, Dict[str, Any]]:
    cache_path = aoci_dir / CACHE_DIR / SCAN_FP_CACHE
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("max_bytes") != max_bytes:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_scan_fp_cache(
    aoci_dir: Path,
    fingerprints: Dict[str, Dict[str, Any]],
    base_files: Dict[str, Any],
    max_bytes: int,
    prev_files: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    if not aoci_dir.is_dir():
        return
    cache_path = aoci_dir / CACHE_DIR / SCAN_FP_CACHE
    now_ns = time.time_ns()
    unindexed = {
        rel: _is_scan_cache_fresh(fp, now_ns=now_ns)
        for rel, fp in fingerprints.items()
        if rel not in base_files
    }
    if not unindexed:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return
    if prev_files is not None and unindexed == prev_files:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            cache_path,
            json.dumps(
                {"schema_version": 1, "max_bytes": max_bytes, "files": unindexed},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    except OSError:
        pass


def _prune_scan_fp_cache(aoci_dir: Path, touched_paths: Sequence[str]) -> bool:
    """Drop newly indexed/pruned paths from `scan_fps.json`; returns True when cache is now empty/absent."""
    cache_path = aoci_dir / CACHE_DIR / SCAN_FP_CACHE
    if not cache_path.is_file():
        return True
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return False
    removed = False
    for p in touched_paths:
        if p in files:
            del files[p]
            removed = True
    if not files:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return True
    if removed:
        try:
            atomic_write_text(
                cache_path,
                json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        except OSError:
            pass
    return False


def discover_and_fingerprint(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    base_files: Dict[str, Any],
    fast_mtime: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    skip_paths: Optional[Dict[str, str]] = None,
    git_incremental: bool = False,
    read_scan_cache: bool = True,
    save_scan_cache: bool = False,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """
    Discover eligible source files and compute fingerprints in one pass (multi-threaded on large cold scans).
    When `fast_mtime=True` and `read_scan_cache=True`, unindexed files also reuse `.aoci/.cache/scan_fps.json`
    on matching non-racy `(mtime_ns, bytes)`, keeping `baseline.json` strictly human-verified.
    """
    use_git_inc = skip_paths is None and (git_incremental or (fast_mtime and len(base_files) >= GIT_FAST_AUTO_THRESHOLD))
    if use_git_inc:
        inc = _try_git_incremental_scan(root, git_info, extra_ignores, base_files, max_bytes)
        if inc is not None:
            return inc

    want_cache = skip_paths is None and git_info.get("is_git")
    tracked_set: Optional[Set[str]] = set() if want_cache else None
    candidates, skipped = discover_candidates(root, git_info, extra_ignores, tracked_out=tracked_set)

    aoci_dir = root / AOCI_DIR_NAME
    scan_fps: Dict[str, Dict[str, Any]] = {}
    if read_scan_cache and skip_paths is None and aoci_dir.is_dir():
        scan_fps = _load_scan_fp_cache(aoci_dir, max_bytes)

    # Decide whether parallel hashing helps (count how many files likely need disk reads)
    need_io = 0
    if not fast_mtime:
        need_io = len(candidates)
    else:
        for rel, st in candidates:
            c = base_files.get(rel) if rel in base_files else scan_fps.get(rel)
            if (
                c is None
                or c.get("racy")
                or (st is not None and (c.get("mtime_ns") != st.st_mtime_ns or c.get("bytes") != st.st_size))
            ):
                need_io += 1

    fingerprints: Dict[str, Dict[str, Any]] = {}

    def _collect(rel: str, fp: Optional[Dict[str, Any]], reason: Optional[str]) -> None:
        if reason is not None:
            if reason != "missing":
                skipped[reason] = skipped.get(reason, 0) + 1
            if skip_paths is not None:
                skip_paths[rel] = reason
        elif fp is not None:
            fingerprints[rel] = fp

    def _cached_fp_for(rel_path: str) -> Optional[Dict[str, Any]]:
        return base_files.get(rel_path) if rel_path in base_files else scan_fps.get(rel_path)

    if need_io >= PARALLEL_HASH_THRESHOLD:
        workers = min(8, max(2, (os.cpu_count() or 4) * 2))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    inspect_and_fingerprint_one,
                    root,
                    rel,
                    st,
                    _cached_fp_for(rel),
                    fast_mtime,
                    max_bytes,
                )
                for rel, st in candidates
            ]
            for fut in futures:
                _collect(*fut.result())
    else:
        for rel, st in candidates:
            _collect(*inspect_and_fingerprint_one(root, rel, st, _cached_fp_for(rel), fast_mtime, max_bytes))

    if save_scan_cache and skip_paths is None and aoci_dir.is_dir():
        _save_scan_fp_cache(
            aoci_dir,
            fingerprints,
            base_files,
            max_bytes,
            prev_files=scan_fps if fast_mtime else None,
        )

    if skip_paths is None and (git_incremental or len(fingerprints) >= GIT_FAST_AUTO_THRESHOLD):
        _save_git_discovery_cache(
            root, git_info, extra_ignores, base_files, fingerprints, max_bytes, tracked_paths=tracked_set
        )

    return fingerprints, skipped


def fingerprint_file(full_path: Path) -> Dict[str, Any]:
    """Single-file fingerprinter used during targeted `upsert` / `ack` writes (stat before read)."""
    st = os.stat(full_path)
    raw = full_path.read_bytes()
    norm = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    fp = _fingerprint_bytes_norm(norm, st)
    sig_hash = compute_signature_hash_from_bytes(norm, full_path.suffix.lower(), full_path.name)
    if sig_hash is not None:
        fp["sig_hash"] = sig_hash
    return fp


def load_baseline(aoci_dir: Path) -> Dict[str, Any]:
    bp = aoci_dir / BASELINE_FILE
    if not bp.exists():
        return {"schema_version": 1, "root_name": aoci_dir.parent.name, "updated_at": None, "files": {}}
    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            return {"schema_version": 1, "root_name": aoci_dir.parent.name, "updated_at": None, "files": {}}
        return data
    except json.JSONDecodeError:
        return {"schema_version": 1, "root_name": aoci_dir.parent.name, "updated_at": None, "files": {}}


def save_baseline(
    aoci_dir: Path,
    baseline: Dict[str, Any],
    touched_paths: Optional[Sequence[str]] = None,
    keep_discovery_cache: bool = False,
) -> None:
    """Persist compact JSON (derived `size_tier` is dropped; it is recomputed from `lines` on load)."""
    baseline["schema_version"] = BASELINE_SCHEMA_VERSION
    baseline["updated_at"] = utc_now_iso()
    files = baseline.get("files", {})
    baseline["files"] = {
        rel: {k: v for k, v in fp.items() if k != "size_tier" and not k.startswith("_")}
        for rel, fp in files.items()
    }
    atomic_write_text(
        aoci_dir / BASELINE_FILE,
        json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    )
    if keep_discovery_cache:
        return
    if touched_paths is not None:
        _prune_scan_fp_cache(aoci_dir, touched_paths)
        _demote_git_discovery_paths(aoci_dir, touched_paths)
        return
    try:
        (aoci_dir / CACHE_DIR / GIT_DISCOVERY_CACHE).unlink()
    except OSError:
        pass


def _try_heal_racy_baseline(aoci_dir: Path, healed: Dict[str, Dict[str, Any]]) -> None:
    """
    Opportunistically clear `racy` flags in `baseline.json` once files have aged past `RACY_WINDOW_NS`
    and a full hash confirmed their content still matches. Uses a non-blocking lock so readers never stall.
    """
    if not healed or not (aoci_dir / BASELINE_FILE).exists():
        return
    try:
        with IndexLock(aoci_dir, timeout=0.05):
            baseline = load_baseline(aoci_dir)
            files = baseline.get("files", {})
            changed = False
            for rel, fp in healed.items():
                cur = files.get(rel)
                if (
                    isinstance(cur, dict)
                    and cur.get("racy")
                    and cur.get("sha256") == fp.get("sha256")
                    and cur.get("mtime_ns") == fp.get("mtime_ns")
                    and cur.get("bytes") == fp.get("bytes")
                ):
                    cur.pop("racy", None)
                    if "sketch" in fp and "sketch" not in cur:
                        cur["sketch"] = fp["sketch"]
                    changed = True
            if changed:
                save_baseline(aoci_dir, baseline, keep_discovery_cache=True)
    except (TimeoutError, OSError):
        pass


def record_semantic_pool_entries(
    root: Path,
    git_info: Dict[str, Any],
    records: Sequence[Tuple[str, Dict[str, Any], str]],
) -> None:
    """Best-effort persist `(relpath, fingerprint, normalized_entry)` into the shared SQLite WAL semantic pool."""
    _pool.record_semantic_pool_entries(root, git_info, records, max_entries=MAX_SEMANTIC_POOL_ENTRIES)


def restore_from_semantic_pool(
    root: Path,
    git_info: Dict[str, Any],
    dry_run: bool = False,
    allow_sha_fallback: bool = False,
    prune_deleted: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Zero-token recovery of previously authored FRAS entries matching `(relpath, sha256)` from the
    shared semantic pool. Cross-path content-hash matches are surfaced as review-only `sha_candidates`
    by default and are only applied when `allow_sha_fallback=True` AND the original path no longer
    exists on disk with a globally unique live SHA.
    """
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    pool_file = semantic_pool_path(root, git_info)
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    lock_ctx: Any = contextlib.nullcontext() if dry_run else IndexLock(aoci_dir)
    with lock_ctx:
        entries, malformed = parse_code_volume(code_path)
        if malformed and not dry_run:
            return {
                "ok": False,
                "error": f"`aoci.code.txt` has {len(malformed)} malformed line(s); fix them before running restore.",
            }
        baseline = load_baseline(aoci_dir)
        base_files: Dict[str, Any] = baseline.get("files", {})
        ignores = load_ignore_matcher(root, git_info)
        fps, _ = discover_and_fingerprint(
            root, git_info, ignores, base_files=base_files, fast_mtime=True, max_bytes=max_bytes
        )

        # Archive any current entries before pruning so branch switches are always reversible
        if not dry_run:
            archive_batch = [
                (old_rel, base_files[old_rel], entries[old_rel])
                for old_rel in entries
                if old_rel in base_files and old_rel not in fps
            ]
            if archive_batch:
                record_semantic_pool_entries(root, git_info, archive_batch)

        live_sha_counts: Dict[str, int] = {}
        for fp_all in fps.values():
            sha_all = fp_all.get("sha256")
            if sha_all:
                live_sha_counts[sha_all] = live_sha_counts.get(sha_all, 0) + 1

        # Count SHA multiplicity among drifted files so content-only fallback is unambiguous
        unindexed_sha_counts: Dict[str, int] = {}
        drifted_rels: List[str] = []
        for rel in sorted(fps.keys()):
            fp = fps[rel]
            if rel in entries and base_files.get(rel, {}).get("sha256") == fp["sha256"]:
                continue
            drifted_rels.append(rel)
            unindexed_sha_counts[fp["sha256"]] = unindexed_sha_counts.get(fp["sha256"], 0) + 1

        ps_queries = [(rel, fps[rel]["sha256"]) for rel in drifted_rels]
        ps_hits, _ = _lookup_pool_entries(
            root, git_info, ps_queries, [], touch_lru=not dry_run
        )

        cand_sha_queries = sorted(
            {
                fps[rel]["sha256"]
                for rel in drifted_rels
                if (rel, fps[rel]["sha256"]) not in ps_hits and int(fps[rel]["lines"]) >= MIN_RENAME_LINES
            }
        )
        s_hits: Dict[str, str] = {}
        if cand_sha_queries:
            _, s_hits = _lookup_pool_entries(
                root, git_info, [], cand_sha_queries, touch_lru=False
            )

        restored: List[str] = []
        restored_by_sha: List[str] = []
        sha_candidates: List[Dict[str, Any]] = []
        applied_shas: List[str] = []
        sha_rename_map: Dict[str, str] = {}
        for rel in drifted_rels:
            fp = fps[rel]
            sha = fp["sha256"]
            cand = ps_hits.get((rel, sha))
            via_sha = False
            if cand is None and int(fp["lines"]) >= MIN_RENAME_LINES:
                s_cand = s_hits.get(sha)
                if s_cand is not None:
                    ms = ENTRY_RE.match(s_cand)
                    if ms and ms.group("layer") in VALID_LAYERS and ms.group("size") in VALID_SIZES:
                        orig_rel = normalize_relpath(ms.group("path"))
                        same_path = orig_rel == rel
                        orig_exists = bool(orig_rel and orig_rel != rel and orig_rel in fps)
                        ambiguous_drifted = unindexed_sha_counts.get(sha, 0) > 1
                        duplicate_live = live_sha_counts.get(sha, 0) > 1
                        if same_path or (
                            allow_sha_fallback
                            and not orig_exists
                            and not ambiguous_drifted
                            and not duplicate_live
                        ):
                            cand = s_cand
                            via_sha = not same_path
                            applied_shas.append(sha)
                        else:
                            reason = (
                                "origin-still-exists"
                                if orig_exists
                                else "ambiguous-sha"
                                if ambiguous_drifted
                                else "duplicate-live-sha"
                                if duplicate_live
                                else "cross-path-sha-match"
                            )
                            sha_candidates.append(
                                {
                                    "path": rel,
                                    "matched_from": orig_rel,
                                    "orig_exists": orig_exists,
                                    "reason": reason,
                                    "candidate_entry": s_cand,
                                }
                            )
            if cand is None:
                continue
            m = ENTRY_RE.match(cand)
            if not m or m.group("layer") not in VALID_LAYERS or m.group("size") not in VALID_SIZES:
                continue
            entries[rel] = (
                f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{fp['size_tier']}]: "
                f"F:{m.group('f').strip()} | R:{m.group('r').strip()} | "
                f"A:{m.group('a').strip()} | S:{m.group('s').strip()}"
            )
            fp_restored = dict(fp)
            if "sig_hash" not in fp_restored:
                sig_h = compute_signature_hash(root / rel, rel)
                if sig_h is not None:
                    fp_restored["sig_hash"] = sig_h
            base_files[rel] = fp_restored
            restored.append(rel)
            if via_sha:
                restored_by_sha.append(rel)
                orig_rel = normalize_relpath(m.group("path"))
                if orig_rel and orig_rel != rel and orig_rel not in fps:
                    sha_rename_map[orig_rel] = rel

        if applied_shas and not dry_run:
            _lookup_pool_entries(root, git_info, [], applied_shas, touch_lru=True)

        pruned: List[str] = []
        if prune_deleted and not malformed:
            for old_rel in list(entries.keys()):
                if old_rel not in fps:
                    del entries[old_rel]
                    pruned.append(old_rel)
            for old_base in list(base_files.keys()):
                if old_base not in fps or old_base not in entries:
                    del base_files[old_base]

        rewritten_refs: List[str] = []
        if sha_rename_map:
            rewriter = compile_path_ref_rewriter(sha_rename_map)
            for rel_e, line_e in list(entries.items()):
                me = ENTRY_RE.match(line_e)
                if not me:
                    continue
                r_old = me.group("r").strip()
                a_old = me.group("a").strip()
                r_new = rewriter(r_old)
                a_new = rewriter(a_old)
                if r_new != r_old or a_new != a_old:
                    entries[rel_e] = (
                        f"{rel_e}[{me.group('layer')}{me.group('domain')}{me.group('imp')}{me.group('size')}]: "
                        f"F:{me.group('f').strip()} | R:{r_new} | A:{a_new} | S:{me.group('s').strip()}"
                    )
                    rewritten_refs.append(rel_e)

        if not dry_run and (restored or pruned or rewritten_refs):
            write_code_volume(code_path, entries)
            sync_shards_if_enabled(aoci_dir, entries)
            baseline["files"] = base_files
            save_baseline(aoci_dir, baseline, touched_paths=restored + pruned)

        return {
            "ok": True,
            "dry_run": dry_run,
            "pool_path": str(pool_file),
            "restored": restored,
            "restored_by_sha": restored_by_sha,
            "sha_candidates": sha_candidates,
            "rewritten_refs": rewritten_refs,
            "pruned": pruned,
            "total_indexed": len(entries),
        }


def pool_stats(root: Path, git_info: Dict[str, Any]) -> Dict[str, Any]:
    return _pool.pool_stats(root, git_info, max_entries=MAX_SEMANTIC_POOL_ENTRIES)


def export_semantic_pool(
    root: Path,
    git_info: Dict[str, Any],
    out_path: Path,
    *,
    include_current: bool = True,
) -> Dict[str, Any]:
    return _pool.export_semantic_pool(
        root,
        git_info,
        out_path,
        include_current=include_current,
        parse_code_volume_fn=parse_code_volume,
        load_baseline_fn=load_baseline,
    )


def import_semantic_pool(
    root: Path,
    git_info: Dict[str, Any],
    in_path: Path,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    return _pool.import_semantic_pool(
        root,
        git_info,
        in_path,
        dry_run=dry_run,
        max_entries=MAX_SEMANTIC_POOL_ENTRIES,
    )


def parse_code_volume(
    code_path: Path,
    duplicates: Optional[List[str]] = None,
    raw_text_out: Optional[List[str]] = None,
) -> Tuple[Dict[str, str], List[Tuple[int, str]]]:
    """
    Parse `aoci.code.txt` into `{relpath: raw_line}` and list of `(lineno, malformed_line)`.
    Repeated paths keep the last occurrence; pass `duplicates` to collect them.
    """
    entries: Dict[str, str] = {}
    malformed: List[Tuple[int, str]] = []
    if not code_path.exists():
        return entries, malformed

    text = code_path.read_text(encoding="utf-8", errors="replace")
    if raw_text_out is not None:
        raw_text_out.append(text)
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or (line.startswith("===") and line.endswith("===")):
            continue
        m = ENTRY_RE.match(line)
        if not m:
            malformed.append((lineno, line))
            continue
        rel = normalize_relpath(m.group("path"))
        if rel in entries and duplicates is not None:
            duplicates.append(rel)
        entries[rel] = line
    return entries, malformed


# Code volume & directory shard helpers are imported from `aoci_core.shards`.


def _resolve_disk_case_single(
    root: Path,
    raw_rel: str,
    dir_cache: Dict[Path, List[Tuple[str, bool, bool, Path]]],
) -> Optional[str]:
    """
    Resolve a repo-relative path to its exact on-disk casing segment by segment without scanning the
    entire repository. Rejects symlinks and ambiguous case-insensitive collisions.
    """
    parts = [seg for seg in raw_rel.split("/") if seg]
    if not parts or any(seg in ("..", ".") for seg in parts):
        return None
    cur = root
    resolved_parts: List[str] = []
    for idx_p, seg in enumerate(parts):
        is_last = idx_p == len(parts) - 1
        listing = dir_cache.get(cur)
        if listing is None:
            try:
                with os.scandir(cur) as it:
                    listing = [
                        (
                            e.name,
                            e.is_dir(follow_symlinks=False),
                            e.is_file(follow_symlinks=False),
                            Path(e.path),
                        )
                        if not e.is_symlink()
                        else (e.name, False, False, Path(e.path))
                        for e in it
                    ]
            except OSError:
                return None
            dir_cache[cur] = listing

        exact = [row for row in listing if row[0] == seg]
        if exact:
            chosen = exact[0]
        else:
            low = seg.lower()
            ci = [row for row in listing if row[0].lower() == low]
            if len(ci) != 1:
                return None
            chosen = ci[0]
            probe = cur / seg
            try:
                if not probe.exists() or not os.path.samefile(probe, chosen[3]):
                    return None
            except OSError:
                return None

        name, is_dir, is_file, full_entry = chosen
        if is_last:
            if not is_file:
                return None
        else:
            if not is_dir:
                return None
        resolved_parts.append(name)
        cur = full_entry

    return "/".join(resolved_parts)


class LiveFileSet:
    """
    The authoritative set of indexable files, produced by the SAME discovery + content filters that
    `scan`/`verify` use. Writers validate against it so the index can never contain entries that
    drift detection would immediately report as deleted (too-large, binary, ignored, wrong case...).
    Pass `only_paths` when pruning is disabled (`ack <paths>`, `upsert --no-prune`) to validate just
    the requested paths in O(len(only_paths)) instead of scanning the entire repository.
    """

    def __init__(
        self,
        root: Path,
        git_info: Dict[str, Any],
        base_files: Dict[str, Any],
        max_bytes: int,
        only_paths: Optional[Sequence[str]] = None,
    ) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.ignores = load_ignore_matcher(root, git_info)
        self.skip_paths: Dict[str, str] = {}
        self._fresh: Dict[str, Dict[str, Any]] = {}

        if only_paths is not None:
            self.fps, self.lower_map = self._init_targeted(only_paths, git_info, base_files=base_files)
        else:
            self.fps, _ = discover_and_fingerprint(
                root,
                git_info,
                self.ignores,
                base_files=base_files,
                fast_mtime=True,
                max_bytes=max_bytes,
                skip_paths=None,
            )
            lower_map: Dict[str, Optional[str]] = {}
            for rel, fp in self.fps.items():
                low = rel.lower()
                lower_map[low] = None if low in lower_map else rel
                cached_b = base_files.get(rel)
                if cached_b is not None and (cached_b.get("racy") or fp.get("sha256") != cached_b.get("sha256")):
                    fresh_fp = dict(fp)
                    if "_cur_sig_hash" in fresh_fp:
                        cur_sig = fresh_fp.pop("_cur_sig_hash")
                        if isinstance(cur_sig, str) and cur_sig:
                            fresh_fp["sig_hash"] = cur_sig
                        else:
                            fresh_fp["_sig_checked"] = True
                    self._fresh[rel] = fresh_fp
            self.lower_map = lower_map

    def _init_targeted(
        self,
        only_paths: Sequence[str],
        git_info: Dict[str, Any],
        base_files: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Optional[str]]]:
        dir_cache: Dict[Path, List[Tuple[str, bool, bool, Path]]] = {}
        candidates: Dict[str, Dict[str, Any]] = {}
        is_git = bool(git_info.get("is_git"))

        for raw in only_paths:
            norm = normalize_relpath(raw)
            if not norm or any(seg in ("..", ".") for seg in norm.split("/")) or re.match(r"^[A-Za-z]:", norm):
                continue
            canon = _resolve_disk_case_single(self.root, norm, dir_cache)
            if canon is None or canon in candidates:
                continue
            if not is_git and any(seg in DEFAULT_SKIP_DIRS for seg in canon.split("/")[:-1]):
                continue
            reason = path_skip_reason(canon, self.ignores)
            if reason is not None:
                self.skip_paths[canon] = reason
                continue
            cached_b = base_files.get(canon) if base_files is not None else None
            _, fp, skip_r = inspect_and_fingerprint_one(self.root, canon, None, cached_b, False, self.max_bytes)
            if skip_r is not None:
                self.skip_paths[canon] = skip_r
                continue
            if fp is not None:
                candidates[canon] = fp
                fresh_fp = dict(fp)
                if "_cur_sig_hash" in fresh_fp:
                    cur_sig = fresh_fp.pop("_cur_sig_hash")
                    if isinstance(cur_sig, str) and cur_sig:
                        fresh_fp["sig_hash"] = cur_sig
                    else:
                        fresh_fp["_sig_checked"] = True
                elif (
                    cached_b is not None
                    and fp.get("sha256") == cached_b.get("sha256")
                    and isinstance(cached_b.get("sig_hash"), str)
                ):
                    fresh_fp["sig_hash"] = cached_b["sig_hash"]
                self._fresh[canon] = fresh_fp

        if is_git and candidates:
            known_tracked: Set[str] = set()
            key = _git_discovery_cache_key(self.root, git_info, self.max_bytes)
            if key is not None:
                cache_path = self.root / AOCI_DIR_NAME / CACHE_DIR / GIT_DISCOVERY_CACHE
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(cached, dict) and cached.get("key") == key:
                        known_tracked.update(cached.get("tracked_clean", ()))
                        known_tracked.update(cached.get("tracked_recheck", ()))
                except (OSError, ValueError):
                    pass
            unchecked = [c for c in candidates if c not in known_tracked]
            if unchecked:
                res = run_git(
                    ["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *unchecked],
                    self.root,
                )
                if res.returncode == 0:
                    allowed = {normalize_relpath(it) for it in res.stdout.split("\0") if it}
                    for c in unchecked:
                        if c not in allowed:
                            candidates.pop(c, None)
                            self._fresh.pop(c, None)

        lower_map: Dict[str, Optional[str]] = {}
        for rel in candidates:
            low = rel.lower()
            lower_map[low] = None if low in lower_map else rel
        return candidates, lower_map

    def __contains__(self, rel: str) -> bool:
        return rel in self.fps

    def canonical(self, rel: str) -> Optional[str]:
        if rel in self.fps:
            return rel
        cand = self.lower_map.get(rel.lower())
        if cand is None:
            return None
        # Only auto-correct case when the filesystem itself resolves both spellings to the same file
        probe = self.root / rel
        target = self.root / cand
        try:
            if probe.exists() and os.path.samefile(probe, target):
                return cand
        except OSError:
            return None
        return None

    def fresh_fingerprint(self, rel: str) -> Dict[str, Any]:
        """Hash once per process for files being written (never trusts the stat shortcut)."""
        fp = self._fresh.get(rel)
        if fp is None:
            fp = fingerprint_file(self.root / rel)
            self._fresh[rel] = fp
        elif "sig_hash" not in fp and not fp.get("_sig_checked"):
            sig_h = compute_signature_hash(self.root / rel, rel)
            if sig_h is not None:
                fp["sig_hash"] = sig_h
            else:
                fp["_sig_checked"] = True
        return fp

    def explain_missing(self, rel: str) -> str:
        full = self.root / rel
        reason = self.skip_paths.get(rel) or path_skip_reason(rel, self.ignores)
        try:
            lst = os.lstat(full)
        except OSError:
            return f"Target file does not exist on disk: `{rel}`"
        if stat.S_ISLNK(lst.st_mode):
            return (
                f"`{rel}` is excluded from indexing (symlink); only regular files listed by `scan` can be indexed."
            )
        if not stat.S_ISREG(lst.st_mode):
            return f"Target is not a regular file: `{rel}`"
        if reason is None:
            _, _, reason = inspect_and_fingerprint_one(self.root, rel, lst, None, False, self.max_bytes)
        if reason:
            return (
                f"`{rel}` is excluded from indexing ({reason}); it would be reported as deleted forever. "
                "Remove the entry, or adjust `.aoci/.aociignore` / `--max-bytes`."
            )
        return (
            f"`{rel}` is not discovered by the indexer (gitignored, outside --root, or a symlink); "
            "only files listed by `scan` can be indexed."
        )


def validate_and_normalize_entry(
    raw_line: str,
    root: Path,
    declared_domains: Optional[Dict[str, str]] = None,
    auto_fix_size: bool = True,
    live: Optional[LiveFileSet] = None,
) -> Tuple[Optional[str], Optional[str], List[str], Optional[str]]:
    """
    Validate a single object-fras-v2 line.
    Returns `(relpath, normalized_line, warnings, hard_error)`.
    When `live` is given, the path must be part of the discovered file set (case is auto-corrected).
    """
    line = raw_line.strip()
    if not line or line.startswith("#") or (line.startswith("===") and line.endswith("===")):
        return None, None, [], None

    if "\n" in line or "\r" in line:
        return None, None, [], "Entry must be strictly single-line."

    m = ENTRY_RE.match(line)
    if not m:
        return (
            None,
            None,
            [],
            f"Syntax mismatch (expected `path[LDIS]: F:... | R:... | A:... | S:...`): {line[:120]}",
        )

    rel = normalize_relpath(m.group("path").strip())
    layer = m.group("layer")
    domain = m.group("domain")
    imp = m.group("imp")
    size = m.group("size")
    f_val = m.group("f").strip()
    r_val = m.group("r").strip()
    a_val = m.group("a").strip()
    s_val = m.group("s").strip()
    fields = {"layer": layer, "domain": domain, "imp": imp, "size": size, "f": f_val, "r": r_val, "a": a_val, "s": s_val}

    tag_errs = validate_parsed_entry_tags(rel, fields, line, actual_lines=None)
    if tag_errs:
        return None, None, [], tag_errs[0][1]

    warnings: List[str] = []
    if live is not None:
        canon = live.canonical(rel)
        if canon is None:
            return None, None, [], live.explain_missing(rel)
        if canon != rel:
            warnings.append(f"Corrected path case `{rel}` -> `{canon}` to match the file on disk.")
            rel = canon
        fp = live.fresh_fingerprint(rel)
    else:
        full = root / rel
        if not full.exists() or not full.is_file():
            return None, None, [], f"Target file does not exist on disk: `{rel}`"
        fp = fingerprint_file(full)

    if declared_domains and domain not in declared_domains:
        warnings.append(
            f"Undeclared Domain `{domain}` in `{rel}` (declare `{domain}=<Name>` in `.aoci/aoci.meta.txt`)."
        )

    size_errs = validate_parsed_entry_tags(rel, fields, line, actual_lines=int(fp["lines"]))
    if size_errs:
        actual_size = fp["size_tier"]
        if auto_fix_size and size_errs[0][0] == "size-mismatch":
            warnings.append(
                f"Adjusted Size tier for `{rel}` from `{size}` to `{actual_size}` ({fp['lines']} lines)."
            )
            size = actual_size
        else:
            return None, None, [], size_errs[0][1]

    normalized = f"{rel}[{layer}{domain}{imp}{size}]: F:{f_val} | R:{r_val} | A:{a_val} | S:{s_val}"
    return rel, normalized, warnings, None


# Template renderers are imported from `aoci_core.templates`.


def _classify_modified_entry(
    root: Path,
    rel: str,
    cached: Optional[Dict[str, Any]],
    cur_fp: Optional[Dict[str, Any]] = None,
) -> str:
    """Classify a modified file against its baseline `sig_hash`: `impl_only`, `contract_changed`, or `unknown`."""
    old_sig = cached.get("sig_hash") if isinstance(cached, dict) else None
    if not isinstance(old_sig, str) or not old_sig:
        return "unknown"
    if isinstance(cur_fp, dict) and "_cur_sig_hash" in cur_fp:
        cur_sig = cur_fp.get("_cur_sig_hash")
    else:
        cur_sig = compute_signature_hash(root / rel, rel)
    if cur_sig is None:
        return "unknown"
    return "impl_only" if cur_sig == old_sig else "contract_changed"


def compute_drift(
    root: Path,
    git_info: Dict[str, Any],
    fast_mtime: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    git_incremental: bool = False,
    allow_heal: bool = True,
    save_scan_cache: bool = False,
) -> Dict[str, Any]:
    """Compare current codebase files against `.aoci/baseline.json` and `.aoci/aoci.code.txt`."""
    t0 = time.perf_counter()
    aoci_dir = root / AOCI_DIR_NAME
    extra_ignores = load_ignore_matcher(root, git_info)

    baseline = load_baseline(aoci_dir)
    base_files: Dict[str, Any] = baseline.get("files", {})
    duplicate_entries: List[str] = []
    raw_code_texts: List[str] = []
    code_entries, malformed = parse_code_volume(
        aoci_dir / CODE_FILE, duplicates=duplicate_entries, raw_text_out=raw_code_texts
    )

    fingerprints, skipped = discover_and_fingerprint(
        root,
        git_info,
        extra_ignores,
        base_files=base_files,
        fast_mtime=fast_mtime,
        max_bytes=max_bytes,
        git_incremental=git_incremental,
        save_scan_cache=save_scan_cache,
    )

    current_set = set(fingerprints.keys())
    base_set = set(base_files.keys())
    code_set = set(code_entries.keys())

    raw_added: List[Dict[str, Any]] = []
    modified: List[Dict[str, Any]] = []
    missing_in_code: List[Dict[str, Any]] = []
    healed_racy: Dict[str, Dict[str, Any]] = {}
    up_to_date = 0

    for rel in sorted(fingerprints.keys()):
        fp = fingerprints[rel]
        cached = base_files.get(rel)
        item = {
            "path": rel,
            "lines": fp["lines"],
            "size_tier": fp["size_tier"],
            "bytes": fp["bytes"],
            "sha256": fp["sha256"],
        }

        if rel not in base_set:
            if rel in code_set:
                mod_item = {k: v for k, v in item.items() if k != "sha256"}
                mod_item["change_kind"] = "unknown"
                modified.append(mod_item)
            else:
                raw_added.append(item)
        elif fp["sha256"] != cached.get("sha256"):
            mod_item = {k: v for k, v in item.items() if k != "sha256"}
            mod_item["change_kind"] = _classify_modified_entry(root, rel, cached, cur_fp=fp)
            modified.append(mod_item)
        elif rel not in code_set:
            missing_in_code.append({k: v for k, v in item.items() if k != "sha256"})
        else:
            up_to_date += 1
            if (
                cached.get("racy")
                and not fp.get("racy")
                and fp.get("mtime_ns") == cached.get("mtime_ns")
                and fp.get("bytes") == cached.get("bytes")
            ):
                healed_racy[rel] = fp

    if allow_heal and healed_racy:
        _try_heal_racy_baseline(aoci_dir, healed_racy)

    raw_deleted = sorted((base_set | code_set) - current_set)

    # Detect exact-content renames/moves by matching SHA-256 between deleted baseline entries and added
    # files. Only unambiguous pairings are accepted: trivial files (empty `__init__.py`, one-liners) never
    # match, and a hash shared by several candidates is resolved only through a unique basename match.
    deleted_by_sha: Dict[str, List[str]] = {}
    for old_rel in raw_deleted:
        old_fp = base_files.get(old_rel, {})
        old_sha = old_fp.get("sha256")
        if old_sha and int(old_fp.get("lines", 0)) >= MIN_RENAME_LINES:
            deleted_by_sha.setdefault(old_sha, []).append(old_rel)

    added_by_sha: Dict[str, List[Dict[str, Any]]] = {}
    for item in raw_added:
        if int(item["lines"]) >= MIN_RENAME_LINES:
            added_by_sha.setdefault(item["sha256"], []).append(item)

    pairs: Dict[str, str] = {}  # new_rel -> old_rel
    for sha, add_items in added_by_sha.items():
        dels = deleted_by_sha.get(sha, [])
        if not dels:
            continue
        if len(add_items) == 1 and len(dels) == 1:
            pairs[add_items[0]["path"]] = dels[0]
            continue
        base_of = lambda p: p.rsplit("/", 1)[-1]  # noqa: E731
        del_by_base: Dict[str, List[str]] = {}
        for d in dels:
            del_by_base.setdefault(base_of(d), []).append(d)
        add_by_base: Dict[str, List[str]] = {}
        for it in add_items:
            add_by_base.setdefault(base_of(it["path"]), []).append(it["path"])
        for bname, new_list in add_by_base.items():
            old_list = del_by_base.get(bname, [])
            if len(new_list) == 1 and len(old_list) == 1:
                pairs[new_list[0]] = old_list[0]

    renamed: List[Dict[str, Any]] = []
    matched_deleted: Set[str] = set(pairs.values())
    added: List[Dict[str, Any]] = []

    for item in raw_added:
        old_rel = pairs.get(item["path"])
        if old_rel is not None:
            renamed.append(
                {
                    "from": old_rel,
                    "to": item["path"],
                    "lines": item["lines"],
                    "size_tier": item["size_tier"],
                    "bytes": item["bytes"],
                    "previous_entry": code_entries.get(old_rel),
                    "fingerprint": fingerprints[item["path"]],
                }
            )
        else:
            added.append({k: v for k, v in item.items() if k != "sha256"})

    deleted = [p for p in raw_deleted if p not in matched_deleted]
    orphan_in_code = sorted(code_set - current_set)

    # Detect moved+edited files ("similar renames") via an inverted MinHash posting list + O(S) bucket
    # tie resolution (scales linearly to bulk directory moves of hundreds/thousands of files).
    similar_renames: List[Dict[str, Any]] = []
    del_sketches = [
        (old_rel, base_files[old_rel])
        for old_rel in deleted
        if old_rel in base_files and base_files[old_rel].get("sketch")
    ]
    add_sketches = [
        (it, fingerprints[it["path"]])
        for it in added
        if fingerprints.get(it["path"], {}).get("sketch")
    ]
    if del_sketches and add_sketches:
        postings: Dict[int, List[int]] = {}
        for old_idx, (_, old_fp) in enumerate(del_sketches):
            for h in old_fp["sketch"]:
                postings.setdefault(int(h), []).append(old_idx)

        max_df = max(256, len(del_sketches) // 2) if len(del_sketches) > 512 else len(del_sketches) + 1
        scored: List[Tuple[float, int, str, Dict[str, Any], Dict[str, Any]]] = []
        for add_item, new_fp in add_sketches:
            hit_counts: Dict[int, int] = {}
            for h in new_fp["sketch"]:
                plist = postings.get(int(h))
                if plist and len(plist) <= max_df:
                    for old_idx in plist:
                        hit_counts[old_idx] = hit_counts.get(old_idx, 0) + 1
            if not hit_counts:
                continue
            new_b = int(new_fp.get("bytes", 0))
            new_base = add_item["path"].rsplit("/", 1)[-1]
            for old_idx, shared_cnt in hit_counts.items():
                if shared_cnt < MIN_RENAME_LINES:
                    continue
                old_rel, old_fp = del_sketches[old_idx]
                old_b = max(1, int(old_fp.get("bytes", 1)))
                ratio = new_b / old_b
                if ratio < 0.25 or ratio > 4.0:
                    continue
                sim = _sketch_similarity(old_fp.get("sketch"), new_fp.get("sketch"))
                if sim >= SIMILAR_RENAME_THRESHOLD:
                    same_base = 1 if new_base == old_rel.rsplit("/", 1)[-1] else 0
                    scored.append((sim, same_base, old_rel, add_item, old_fp))

        scored.sort(key=lambda row: (-round(row[0], 3), -row[1], row[2], row[3]["path"]))
        used_old: Set[str] = set()
        used_new: Set[str] = set()
        pos = 0
        total_scored = len(scored)
        while pos < total_scored:
            r_sim = round(scored[pos][0], 3)
            r_base = scored[pos][1]
            end = pos + 1
            while end < total_scored and round(scored[end][0], 3) == r_sim and scored[end][1] == r_base:
                end += 1
            active = [
                row
                for row in scored[pos:end]
                if row[2] not in used_old and row[3]["path"] not in used_new
            ]
            old_deg: Dict[str, int] = {}
            new_deg: Dict[str, int] = {}
            for row in active:
                old_deg[row[2]] = old_deg.get(row[2], 0) + 1
                new_p = row[3]["path"]
                new_deg[new_p] = new_deg.get(new_p, 0) + 1
            for sim, _, old_rel, add_item, old_fp in active:
                new_rel = add_item["path"]
                if old_deg[old_rel] > 1 or new_deg[new_rel] > 1:
                    continue
                used_old.add(old_rel)
                used_new.add(new_rel)
                similar_renames.append(
                    {
                        "from": old_rel,
                        "to": new_rel,
                        "similarity": round(sim, 2),
                        "lines": add_item["lines"],
                        "size_tier": add_item["size_tier"],
                        "bytes": add_item["bytes"],
                        "previous_entry": code_entries.get(old_rel),
                        "old_fingerprint": old_fp,
                    }
                )
            pos = end

    declared_domains = parse_declared_domains(aoci_dir)
    undeclared_domains: Dict[str, List[str]] = {}
    invalid_entries: List[Dict[str, str]] = []
    invalid_clean_paths: Set[str] = set()
    for rel, line in sorted(code_entries.items()):
        f = parse_entry_fields(line)
        if not f:
            continue
        if declared_domains and f["domain"] not in declared_domains:
            undeclared_domains.setdefault(f["domain"], []).append(rel)
        fp_cur = fingerprints.get(rel)
        actual_l = int(fp_cur["lines"]) if fp_cur is not None else None
        tag_errs = validate_parsed_entry_tags(rel, f, line, actual_lines=actual_l)
        if tag_errs:
            for code, msg in tag_errs:
                invalid_entries.append({"path": rel, "code": code, "message": msg})
            if fp_cur is not None and rel in base_set and fp_cur["sha256"] == base_files[rel].get("sha256"):
                invalid_clean_paths.add(rel)

    if invalid_clean_paths:
        up_to_date = max(0, up_to_date - len(invalid_clean_paths))

    has_drift = bool(added or modified or renamed or deleted or missing_in_code or malformed or invalid_entries)

    dir_counts: Dict[str, int] = {}
    for rel in fingerprints.keys():
        top = rel.split("/", 1)[0] if "/" in rel else "."
        dir_counts[top] = dir_counts.get(top, 0) + 1

    mod_summary: Dict[str, int] = {"impl_only": 0, "contract_changed": 0, "unknown": 0}
    for it in modified:
        ck = str(it.get("change_kind", "unknown"))
        mod_summary[ck] = mod_summary.get(ck, 0) + 1

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    return {
        "root": str(root),
        "aoci_exists": (aoci_dir / CODE_FILE).exists(),
        "total_eligible": len(fingerprints),
        "indexed_clean": up_to_date,
        "added": added,
        "modified": modified,
        "modified_summary": mod_summary,
        "renamed": [{k: v for k, v in r.items() if k != "fingerprint"} for r in renamed],
        "_renamed_full": renamed,
        "similar_renames": [{k: v for k, v in r.items() if k != "old_fingerprint"} for r in similar_renames],
        "_similar_renames_full": similar_renames,
        "deleted": deleted,
        "missing_in_code": missing_in_code,
        "orphan_in_code": orphan_in_code,
        "malformed_lines": [{"line": ln, "text": txt} for ln, txt in malformed],
        "invalid_entries": invalid_entries,
        "duplicate_entries": sorted(set(duplicate_entries)),
        "undeclared_domains": {k: sorted(v) for k, v in sorted(undeclared_domains.items())},
        "has_drift": has_drift,
        "skipped_summary": skipped,
        "top_dirs": dict(sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "elapsed_ms": elapsed_ms,
        "_code_entries": code_entries,
        "_code_text": raw_code_texts[0] if raw_code_texts else "",
        "_fingerprints": fingerprints,
    }


def drift_signature(drift: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Map every drifted path to its current content hash (None = gone). Used by hooks to tell drift
    that already existed at session start apart from drift the agent introduced afterwards.
    """
    fps: Dict[str, Dict[str, Any]] = drift.get("_fingerprints", {})
    sig: Dict[str, Optional[str]] = {}
    for key in ("added", "modified", "missing_in_code"):
        for it in drift.get(key, []):
            sig[it["path"]] = fps.get(it["path"], {}).get("sha256")
    for r in drift.get("renamed", []):
        sig[r["to"]] = fps.get(r["to"], {}).get("sha256")
        sig[r["from"]] = None
    for p in drift.get("deleted", []):
        sig[p] = None
    return sig


def public_drift(drift: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in drift.items() if not k.startswith("_")}


# Batch planning helpers are imported from `aoci_core.batches`.


# ---------------------------------------------------------------------------
# FRAS dependency graph (R:/A: semantics) and lint helpers
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    git_info = detect_git_info(root)

    # Step 1: Enforce Git exclusion BEFORE writing index files
    git_status = ensure_git_exclusion(root, git_info, check_status=True)
    if git_status["is_git"] and not git_status["excluded"]:
        print(
            f"ERROR: Git detected at {git_info['toplevel']}, but `{git_status['pattern']}` "
            f"is still NOT ignored after updating `{git_status['exclude_file']}`.\n"
            "Check for overriding negation rules (e.g. `!.aoci/`) in `.gitignore`.",
            file=sys.stderr,
        )
        return 2

    aoci_dir.mkdir(parents=True, exist_ok=True)
    # Scratch area for batch entry files (inside `.aoci/`, so never indexed and never committed)
    (aoci_dir / "pending").mkdir(exist_ok=True)
    project_name = args.project or root.name

    manifest_path = aoci_dir / MANIFEST_FILE
    if not manifest_path.exists() or args.force:
        write_text_lf(
            manifest_path,
            render_manifest_template(project_name, args.locale, git_status["is_git"]),
        )

    meta_path = aoci_dir / META_FILE
    if not meta_path.exists() or args.force:
        write_text_lf(meta_path, render_meta_template())

    code_path = aoci_dir / CODE_FILE
    if not code_path.exists() or args.force:
        write_code_volume(code_path, {})

    baseline_path = aoci_dir / BASELINE_FILE
    if not baseline_path.exists() or args.force:
        save_baseline(aoci_dir, {"schema_version": 1, "root_name": project_name, "files": {}})

    ignore_path = aoci_dir / IGNORE_FILE
    if not ignore_path.exists():
        write_text_lf(ignore_path, render_ignore_template())

    final_git = check_git_exclusion(root, git_info, check_status=True)
    drift = compute_drift(root, git_info, max_bytes=args.max_bytes, save_scan_cache=True)

    if args.json:
        print(json.dumps({"git": final_git, "drift": public_drift(drift)}, indent=2, ensure_ascii=False))
    else:
        print(f"[AOCI Init] Root: {root}")
        print(f"[AOCI Init] Index Dir: {aoci_dir}")
        if final_git["is_git"]:
            action = "added rule to" if git_status.get("changed") else "already excluded in"
            print(f"[Git Exclude] OK ({action} {final_git['exclude_file']} -> `{final_git['pattern']}`)")
            if final_git["tracked_files"]:
                print(
                    f"[Git Exclude] CRITICAL WARNING: {len(final_git['tracked_files'])} file(s) inside "
                    f"`.aoci/` are tracked by Git! Run: `git rm -r --cached {AOCI_DIR_NAME}`",
                    file=sys.stderr,
                )
        else:
            print("[Git Exclude] No Git repository detected (local non-git workspace).")
        print(
            f"[Scan] Eligible source files: {drift['total_eligible']} | Pending indexing: {len(drift['added'])} "
            f"({drift['elapsed_ms']}ms)"
        )
        if drift["top_dirs"]:
            top_preview = ", ".join(f"{k}({v})" for k, v in list(drift["top_dirs"].items())[:12])
            print(f"[Structure] Top dirs: {top_preview}")
        if drift["skipped_summary"]:
            sk = ", ".join(f"{k}:{v}" for k, v in sorted(drift["skipped_summary"].items()))
            print(f"[Skipped] {sk}")
        print(
            "[Next] 1) Customize domain codes in `.aoci/aoci.meta.txt` and global invariants in `.aoci/aoci.txt`; "
            "2) Run `scan --batches 20`; 3) Read source files and `upsert` FRAS entries; 4) Run `verify`."
        )

    return 0 if final_git["ok"] else 2


def cmd_ensure_git_exclude(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    git_info = detect_git_info(root)
    res = ensure_git_exclusion(root, git_info, check_status=True)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        if not res["is_git"]:
            print(f"[Git Exclude] No Git repository detected at {root}.")
        elif res["ok"]:
            state = "updated" if res.get("changed") else "verified"
            print(f"[Git Exclude] OK ({state}): `{res['pattern']}` -> {res['exclude_file']}")
        else:
            if not res["excluded"]:
                print(f"[Git Exclude] FAIL: `{res['pattern']}` is NOT ignored by Git!", file=sys.stderr)
            if res["tracked_files"]:
                print(
                    f"[Git Exclude] FAIL: Tracked files in `.aoci/`: {', '.join(res['tracked_files'][:10])}\n"
                    f"Fix with: git rm -r --cached {AOCI_DIR_NAME}",
                    file=sys.stderr,
                )
            if res["status_leaks"]:
                print(
                    f"[Git Exclude] FAIL: `git status` still reports `.aoci/`: {', '.join(res['status_leaks'][:5])}",
                    file=sys.stderr,
                )
    return 0 if res["ok"] else 2


def cmd_scan(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    git_info = detect_git_info(root)

    if args.check_only:
        git_status = check_git_exclusion(root, git_info, check_status=False)
    else:
        git_status = ensure_git_exclusion(root, git_info, check_status=False)

    restored_info: Optional[Dict[str, Any]] = None
    if getattr(args, "auto_restore", False) and not args.check_only and (root / AOCI_DIR_NAME / CODE_FILE).exists():
        restored_info = restore_from_semantic_pool(
            root, git_info, allow_sha_fallback=False, prune_deleted=False, max_bytes=args.max_bytes
        )

    drift = compute_drift(
        root,
        git_info,
        fast_mtime=args.fast or getattr(args, "git_fast", False),
        max_bytes=args.max_bytes,
        git_incremental=getattr(args, "git_fast", False),
        allow_heal=not args.check_only,
        save_scan_cache=not args.check_only,
    )

    # Exact/similar renames should be migrated via `migrate-renames` first rather than re-authored in batches
    pending, added_non_sim, deleted_non_sim = _build_pending_items(drift)
    batches: List[List[Dict[str, Any]]] = []
    batch_plan_meta: Optional[Dict[str, Any]] = None
    if args.batches:
        batches, batch_plan_meta = resolve_stable_batches(
            root / AOCI_DIR_NAME,
            pending,
            args.batches,
            args.max_batch_lines,
            persist=not args.check_only,
            reset=bool(getattr(args, "reset_batches", False)),
        )
    elif not pending and not args.check_only:
        _clear_batch_plan(root / AOCI_DIR_NAME)

    # Entries whose R:/A: explicitly mention (or depend on) a modified/renamed/deleted file need review
    affected: Dict[str, List[str]] = {}
    changed_paths = [it["path"] for it in drift["modified"]] + [r["from"] for r in drift["renamed"]] + list(
        drift["deleted"]
    )
    if changed_paths and drift["_code_entries"]:
        code_map = drift["_code_entries"]
        parsed_map = parse_entries_map(code_map)
        _, dependents = build_fras_graph(code_map, parsed_entries=parsed_map)
        mentioned_by = build_explicit_mention_index(code_map, parsed_entries=parsed_map)
        changed_set = set(changed_paths)
        for p in changed_paths:
            nbs = sorted((mentioned_by.get(p, set()) | dependents.get(p, set())) - changed_set)
            if nbs:
                affected[p] = nbs

    payload: Dict[str, Any] = {
        "git": git_status,
        "drift": public_drift(drift),
        "affected_neighbors": affected,
        "batches": batches,
    }
    if batch_plan_meta is not None:
        payload["batch_plan"] = batch_plan_meta
    if restored_info is not None:
        payload["auto_restored"] = restored_info.get("restored", [])
        if restored_info.get("sha_candidates"):
            payload["auto_restore_sha_candidates"] = restored_info["sha_candidates"]

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        status_label = "CLEAN" if not drift["has_drift"] else "DRIFT"
        print(f"[AOCI Scan] Status: {status_label} | Root: {root} | {drift['elapsed_ms']}ms")
        if restored_info and restored_info.get("restored"):
            print(f"[Auto-Restore] Restored {len(restored_info['restored'])} entry(ies) from semantic cache pool.")
        if git_status["is_git"]:
            git_lbl = "OK" if git_status["ok"] else "FAIL"
            extra = " (auto-added to .git/info/exclude)" if git_status.get("changed") else ""
            print(f"[Git Exclude] {git_lbl}{extra}")
            if git_status["tracked_files"]:
                print(f"  CRITICAL: tracked `.aoci` files found -> run `git rm -r --cached {AOCI_DIR_NAME}`")
        print(
            f"[Counts] Total={drift['total_eligible']} | Clean={drift['indexed_clean']} | "
            f"Added={len(drift['added'])} | Modified={len(drift['modified'])} | "
            f"Renamed={len(drift['renamed'])} | MissingInCode={len(drift['missing_in_code'])} | "
            f"Deleted={len(drift['deleted'])} | Malformed={len(drift['malformed_lines'])}"
        )

        if drift["renamed"]:
            print("\nRENAMED (exact content match - run `migrate-renames` or migrate manually):")
            for r in drift["renamed"][:30]:
                print(f"  > {r['from']} -> {r['to']}  [size={r['size_tier']}, {r['lines']}L]")
                if r.get("previous_entry"):
                    print(f"    prev: {r['previous_entry']}")

        if drift.get("similar_renames"):
            print(
                "\nSIMILAR RENAMES (moved + edited; `migrate-renames` carries entry & refs, "
                "leaving file MODIFIED for `ack`/`upsert` review):"
            )
            for sr in drift["similar_renames"][:30]:
                pct = int(round(sr["similarity"] * 100))
                print(f"  ~> {sr['from']} -> {sr['to']}  [{pct}% similar, size={sr['size_tier']}, {sr['lines']}L]")
                if sr.get("previous_entry"):
                    print(f"     prev: {sr['previous_entry']}")

        if added_non_sim:
            print("\nADDED (need new FRAS entries):")
            for it in added_non_sim[:50]:
                print(f"  + {it['path']}  [size={it['size_tier']}, {it['lines']}L]")
            if len(added_non_sim) > 50:
                print(f"  ... (+{len(added_non_sim) - 50} more; use --batches or --json)")

        if drift["modified"]:
            print("\nMODIFIED (review entry; if F/R/A/S still accurate run `ack <path>`, else `upsert`):")
            for it in drift["modified"][:50]:
                ck = str(it.get("change_kind", "unknown"))
                ck_tag = f", {ck.replace('_', '-')}" if ck != "unknown" else ""
                print(f"  ~ {it['path']}  [size={it['size_tier']}, {it['lines']}L{ck_tag}]")
            if len(drift["modified"]) > 50:
                print(f"  ... (+{len(drift['modified']) - 50} more)")

        if drift["missing_in_code"]:
            print("\nMISSING IN aoci.code.txt:")
            for it in drift["missing_in_code"][:50]:
                print(f"  ? {it['path']}  [size={it['size_tier']}, {it['lines']}L]")

        if deleted_non_sim:
            print("\nDELETED (will be pruned by `upsert`):")
            for p in deleted_non_sim[:50]:
                print(f"  - {p}")
            if len(deleted_non_sim) > 50:
                print(f"  ... (+{len(deleted_non_sim) - 50} more)")

        if drift["malformed_lines"]:
            print("\nMALFORMED LINES IN aoci.code.txt:")
            for ml in drift["malformed_lines"][:20]:
                print(f"  ! L{ml['line']}: {ml['text'][:100]}")

        if drift["invalid_entries"]:
            print("\nINVALID ENTRY TAGS / FIELDS IN aoci.code.txt:")
            for inv in drift["invalid_entries"][:20]:
                print(f"  ! [{inv['code']}] {inv['message']}")

        if drift["duplicate_entries"]:
            print("\nDUPLICATE ENTRIES IN aoci.code.txt (last occurrence wins; next upsert dedups):")
            for p in drift["duplicate_entries"][:20]:
                print(f"  ! {p}")

        if affected:
            print("\nAFFECTED NEIGHBORS (entries whose R:/A: mention or depend on changed files; re-check their R:/A:/S:):")
            for p, nbs in list(affected.items())[:30]:
                more = f" (+{len(nbs) - 8} more)" if len(nbs) > 8 else ""
                print(f"  * {p} <- {', '.join(nbs[:8])}{more}")

        if drift["undeclared_domains"]:
            print("\nUNDECLARED DOMAINS (add to `.aoci/aoci.meta.txt`):")
            for dom, paths in drift["undeclared_domains"].items():
                print(f"  ! Domain `{dom}` used by {len(paths)} file(s) (e.g. {paths[0]})")

        if batches:
            eff_lines = (batch_plan_meta or {}).get("max_lines", args.max_batch_lines)
            print(
                f"\nBATCH PLAN ({len(batches)} batch(es), max_files={args.batches}, "
                f"max_lines={eff_lines}):"
            )
            for idx, batch in enumerate(batches, start=1):
                if not batch:
                    print(f"  Batch {idx}/{len(batches)} (DONE)")
                else:
                    tot_l = sum(int(b.get("lines", 0)) for b in batch)
                    paths = ", ".join(f"{b['path']}[{b['size_tier']}]" for b in batch)
                    print(f"  Batch {idx}/{len(batches)} ({len(batch)} files, {tot_l}L): {paths}")

    if args.fail_on_drift and (drift["has_drift"] or not git_status["ok"]):
        return 1
    return 0


def cmd_upsert(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    as_json = bool(getattr(args, "json", False))

    def _fail(msg: str, rc: int = 2) -> int:
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return rc

    if not aoci_dir.exists() or not code_path.exists():
        return _fail(f"`.aoci/` is not initialized at {root}. Run `init` first.", 2)

    git_info = detect_git_info(root)
    if args.dry_run:
        git_status = check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        git_status = ensure_git_exclusion(root, git_info, check_status=False)
        if git_status["is_git"] and not git_status["excluded"]:
            return _fail("Failed to exclude `.aoci/` from Git. Aborting upsert.", 2)

    raw_lines: List[str] = []
    if args.entries_file:
        ep = _resolve_cli_file_path(root, args.entries_file, must_exist=True)
        try:
            raw_lines.extend(ep.read_text(encoding="utf-8-sig").splitlines())
        except OSError as exc:
            return _fail(f"Cannot read `--entries-file` `{ep}`: {exc}", 2)
    if args.entry:
        raw_lines.extend(args.entry)
    if args.stdin:
        raw_lines.extend(sys.stdin.read().splitlines())

    if not raw_lines and not args.prune_only:
        return _fail("Provide entries via `--entries-file`, `--entry`, `--stdin`, or use `--prune-only`.", 2)

    lock_ctx: Any = contextlib.nullcontext() if args.dry_run else IndexLock(aoci_dir)
    try:
        with lock_ctx:
            return _upsert_locked(args, root, aoci_dir, code_path, git_info, raw_lines)
    except TimeoutError as exc:
        return _fail(str(exc), 3)


def report_malformed_block(malformed: Sequence[Tuple[int, str]], target_name: str = "aoci.code.txt") -> None:
    print(
        f"ERROR: `{target_name}` contains {len(malformed)} malformed line(s) that a rewrite would delete. "
        "Fix them (or re-upsert those files), or pass `--drop-malformed`:",
        file=sys.stderr,
    )
    for ln, txt in malformed[:10]:
        print(f"  L{ln}: {txt[:120]}", file=sys.stderr)


def _upsert_locked(
    args: argparse.Namespace,
    root: Path,
    aoci_dir: Path,
    code_path: Path,
    git_info: Dict[str, Any],
    raw_lines: List[str],
) -> int:
    as_json = bool(getattr(args, "json", False))
    # Re-read everything under the lock so concurrent writers never lose each other's updates
    declared_domains = parse_declared_domains(aoci_dir)
    entries, malformed = parse_code_volume(code_path)
    baseline = load_baseline(aoci_dir)
    base_files: Dict[str, Any] = baseline.get("files", {})
    targeted_paths: Optional[List[str]] = None
    if args.no_prune and raw_lines:
        targeted_paths = [
            normalize_relpath(m.group("path").strip())
            for raw in raw_lines
            for m in (ENTRY_RE.match(raw.strip()),)
            if m
        ]
    live = LiveFileSet(root, git_info, base_files, args.max_bytes, only_paths=targeted_paths)

    known_before = set(entries.keys())
    prev_r_deps: Dict[str, Set[str]] = {}
    errors: List[str] = []
    warnings: List[str] = []
    upserted_paths: List[str] = []
    seen_in_input: Set[str] = set()

    for raw in raw_lines:
        rel, norm, warns, err = validate_and_normalize_entry(
            raw,
            root,
            declared_domains=declared_domains,
            auto_fix_size=not args.strict_size,
            live=live,
        )
        if err is not None:
            errors.append(err)
            continue
        if rel is None or norm is None:
            continue
        warnings.extend(warns)
        if rel not in prev_r_deps and rel in entries:
            old_f = parse_entry_fields(entries[rel])
            prev_r_deps[rel] = set(extract_path_refs(old_f["r"], known_before)) if old_f else set()
        if rel in seen_in_input:
            warnings.append(f"Duplicate entry for `{rel}` in input; the last one wins.")
        else:
            upserted_paths.append(rel)
        seen_in_input.add(rel)
        entries[rel] = norm

    if errors:
        if as_json:
            print(json.dumps({"ok": False, "errors": errors, "warnings": warnings}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: Validation failed for {len(errors)} entry(ies); no files were modified:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
        return 1

    if malformed and not args.drop_malformed:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"`aoci.code.txt` contains {len(malformed)} malformed line(s) (pass `--drop-malformed` to drop).",
                        "malformed": [{"line": ln, "text": txt[:120]} for ln, txt in malformed[:10]],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            report_malformed_block(malformed)
        return 1

    pruned: List[str] = []
    archived_before_prune: List[Tuple[str, Dict[str, Any], str]] = []
    if not args.no_prune:
        for existing_rel in list(entries.keys()):
            if existing_rel not in live:
                if existing_rel in base_files:
                    archived_before_prune.append((existing_rel, base_files[existing_rel], entries[existing_rel]))
                del entries[existing_rel]
                pruned.append(existing_rel)
        for existing_base in list(base_files.keys()):
            if existing_base not in live or existing_base not in entries:
                del base_files[existing_base]

    if malformed:
        warnings.append(f"Dropped {len(malformed)} previously malformed line(s) during rewrite (--drop-malformed).")

    # Surface R:/A: references that now point at pruned files, or stale A: callers after R: edge removal
    known_after = set(entries.keys())
    for rel in upserted_paths:
        old_deps = prev_r_deps.get(rel)
        if not old_deps or rel not in entries:
            continue
        new_f = parse_entry_fields(entries[rel])
        new_deps = set(extract_path_refs(new_f["r"], known_after)) if new_f else set()
        dropped = sorted((old_deps - new_deps) & known_after)
        for dep in dropped:
            dep_f = parse_entry_fields(entries[dep])
            if dep_f and rel in extract_path_refs(dep_f["a"], known_after):
                warnings.append(
                    f"Entry `{rel}` no longer lists `{dep}` in `R:`, but `{dep}` still mentions `{rel}` in `A:` — review `{dep}`."
                )

    dangling_warnings: List[str] = []
    if pruned:
        pruned_set = set(pruned)
        for rel, line in sorted(entries.items()):
            f = parse_entry_fields(line)
            if not f:
                continue
            stale = sorted(set(extract_path_refs(f["r"], pruned_set) + extract_path_refs(f["a"], pruned_set)))
            if stale:
                dangling_warnings.append(
                    f"Dangling ref: `{rel}` still references pruned {', '.join(stale)}; update its entry."
                )

    if not as_json:
        for w in warnings:
            print(f"[Warn] {w}")
        for dw in dangling_warnings:
            print(f"[Warn] {dw}")

    if args.dry_run:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "upserted": upserted_paths,
                        "pruned": pruned,
                        "total_indexed": len(entries),
                        "warnings": warnings,
                        "dangling_warnings": dangling_warnings,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(
                f"[AOCI Upsert DRY-RUN] Validation OK | WouldUpsert={len(upserted_paths)} | "
                f"WouldPrune={len(pruned)} | WouldTotal={len(entries)}"
            )
        return 0

    for rel in upserted_paths:
        base_files[rel] = live.fresh_fingerprint(rel)

    write_code_volume(code_path, entries)
    sync_shards_if_enabled(aoci_dir, entries)
    baseline["files"] = base_files
    save_baseline(aoci_dir, baseline, touched_paths=upserted_paths + pruned)
    saved_plan = _load_batch_plan(aoci_dir)
    if saved_plan is not None:
        if targeted_paths is None:
            any_unindexed = any((p not in entries or p not in base_files) for p in live.fps)
            if not any_unindexed:
                _clear_batch_plan(aoci_dir)
        elif not (aoci_dir / CACHE_DIR / SCAN_FP_CACHE).exists():
            scheduled = {p for b in saved_plan.get("batches", []) if isinstance(b, list) for p in b}
            if scheduled and all(p in entries and p in base_files for p in scheduled):
                _clear_batch_plan(aoci_dir)
    pool_records = list(archived_before_prune) + [
        (rel, base_files[rel], entries[rel]) for rel in upserted_paths if rel in entries
    ]
    record_semantic_pool_entries(root, git_info, pool_records)
    hub_sync = _hubs.sync_hubs_on_upsert(root, aoci_dir, upserted_paths, pruned)

    if as_json:
        upsert_payload: Dict[str, Any] = {
            "ok": True,
            "dry_run": False,
            "upserted": upserted_paths,
            "pruned": pruned,
            "total_indexed": len(entries),
            "warnings": warnings,
            "dangling_warnings": dangling_warnings,
        }
        if hub_sync.get("refreshed"):
            upsert_payload["hubs_refreshed"] = hub_sync["refreshed"]
        if hub_sync.get("pruned"):
            upsert_payload["hubs_pruned"] = hub_sync["pruned"]
        print(json.dumps(upsert_payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"[AOCI Upsert] OK | Upserted={len(upserted_paths)} | Pruned={len(pruned)} | "
            f"TotalIndexed={len(entries)}"
        )
        if hub_sync.get("refreshed") or hub_sync.get("pruned"):
            print(
                f"  [hubs] Refreshed={len(hub_sync.get('refreshed', []))} | "
                f"Pruned={len(hub_sync.get('pruned', []))}"
            )
    return 0


def cmd_migrate_renames(args: argparse.Namespace) -> int:
    """Automatically migrate exact-content renamed files in `aoci.code.txt` and rewrite R:/A: path refs."""
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    as_json = bool(getattr(args, "json", False))
    if not code_path.exists():
        msg = f"`{code_path}` not found. Run `init` first."
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    git_info = detect_git_info(root)
    if args.dry_run:
        check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        ensure_git_exclusion(root, git_info, check_status=False)

    lock_ctx: Any = contextlib.nullcontext() if args.dry_run else IndexLock(aoci_dir)
    try:
        with lock_ctx:
            return _migrate_renames_locked(args, root, aoci_dir, code_path, git_info)
    except TimeoutError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 3


def _migrate_renames_locked(
    args: argparse.Namespace,
    root: Path,
    aoci_dir: Path,
    code_path: Path,
    git_info: Dict[str, Any],
) -> int:
    as_json = bool(getattr(args, "json", False))
    entries, malformed = parse_code_volume(code_path)
    drop_malformed = getattr(args, "drop_malformed", False)
    if malformed and not drop_malformed:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"`aoci.code.txt` contains {len(malformed)} malformed line(s) (pass `--drop-malformed` to drop).",
                        "malformed": [{"line": ln, "text": txt[:120]} for ln, txt in malformed[:10]],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            report_malformed_block(malformed)
        return 1

    drift = compute_drift(root, git_info, fast_mtime=False, max_bytes=args.max_bytes, allow_heal=not args.dry_run)
    renamed_full: List[Dict[str, Any]] = drift["_renamed_full"]
    similar_full: List[Dict[str, Any]] = (
        [] if getattr(args, "exact_only", False) else drift.get("_similar_renames_full", [])
    )
    if not renamed_full and not similar_full:
        if as_json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": bool(args.dry_run),
                        "migrated": [],
                        "migrated_similar": [],
                        "rewritten_refs": [],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("[AOCI Migrate-Renames] No exact-content renames (or similar moves) detected.")
        return 0

    baseline = load_baseline(aoci_dir)
    base_files: Dict[str, Any] = baseline.get("files", {})

    rename_map: Dict[str, str] = {}
    migrated: List[Tuple[str, str]] = []
    migrated_similar: List[Tuple[str, str, int]] = []
    for r in renamed_full:
        old_rel = r["from"]
        new_rel = r["to"]
        old_line = entries.get(old_rel)
        if not old_line:
            continue
        m = ENTRY_RE.match(old_line)
        if not m:
            continue
        rename_map[old_rel] = new_rel
        new_line = (
            f"{new_rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{r['size_tier']}]: "
            f"F:{m.group('f').strip()} | R:{m.group('r').strip()} | "
            f"A:{m.group('a').strip()} | S:{m.group('s').strip()}"
        )
        del entries[old_rel]
        entries[new_rel] = new_line
        base_files.pop(old_rel, None)
        fp_new = dict(r["fingerprint"])
        if "sig_hash" not in fp_new:
            sig_h = compute_signature_hash(root / new_rel, new_rel)
            if sig_h is not None:
                fp_new["sig_hash"] = sig_h
        base_files[new_rel] = fp_new
        migrated.append((old_rel, new_rel))

    for sr in similar_full:
        old_rel = sr["from"]
        new_rel = sr["to"]
        old_line = entries.get(old_rel)
        if not old_line or old_rel in rename_map or new_rel in entries:
            continue
        m = ENTRY_RE.match(old_line)
        if not m:
            continue
        rename_map[old_rel] = new_rel
        new_line = (
            f"{new_rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{sr['size_tier']}]: "
            f"F:{m.group('f').strip()} | R:{m.group('r').strip()} | "
            f"A:{m.group('a').strip()} | S:{m.group('s').strip()}"
        )
        del entries[old_rel]
        entries[new_rel] = new_line
        base_files.pop(old_rel, None)
        # Keep the OLD baseline hash so the moved+edited file stays MODIFIED until reviewed via ack/upsert
        if isinstance(sr.get("old_fingerprint"), dict):
            base_files[new_rel] = dict(sr["old_fingerprint"])
        migrated_similar.append((old_rel, new_rel, int(round(sr["similarity"] * 100))))

    # Rewrite exact relative path mentions inside R: and A: across all entries (single simultaneous pass)
    updated_refs: List[str] = []
    if rename_map and not args.no_rewrite_refs:
        rewriter = compile_path_ref_rewriter(rename_map)
        for rel, line in list(entries.items()):
            m = ENTRY_RE.match(line)
            if not m:
                continue
            r_val = m.group("r").strip()
            a_val = m.group("a").strip()
            new_r = rewriter(r_val)
            new_a = rewriter(a_val)
            if new_r != r_val or new_a != a_val:
                entries[rel] = (
                    f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{m.group('size')}]: "
                    f"F:{m.group('f').strip()} | R:{new_r} | A:{new_a} | S:{m.group('s').strip()}"
                )
                updated_refs.append(rel)

    migrated_hubs = _hubs.migrate_hubs_renames(
        root, aoci_dir, rename_map, dry_run=bool(args.dry_run)
    )

    if args.dry_run:
        if as_json:
            dry_mig_payload: Dict[str, Any] = {
                "ok": True,
                "dry_run": True,
                "migrated": [{"from": o, "to": n} for o, n in migrated],
                "migrated_similar": [
                    {"from": o, "to": n, "similarity_pct": pct} for o, n, pct in migrated_similar
                ],
                "rewritten_refs": updated_refs,
            }
            if migrated_hubs:
                dry_mig_payload["migrated_hubs"] = migrated_hubs
            print(json.dumps(dry_mig_payload, indent=2, ensure_ascii=False))
        else:
            print(
                f"[AOCI Migrate-Renames DRY-RUN] WouldMigrate={len(migrated)} | "
                f"WouldMigrateSimilar={len(migrated_similar)} | WouldRewriteRefs={len(updated_refs)}"
            )
            for old_p, new_p in migrated:
                print(f"  > {old_p} -> {new_p}")
            for old_p, new_p, pct in migrated_similar:
                print(f"  ~> {old_p} -> {new_p} ({pct}% similar; remains MODIFIED for review)")
            for ref_p in updated_refs:
                print(f"  ~ ref updated: {ref_p}")
            for hm in migrated_hubs:
                print(f"  ~ [hubs] would migrate: {hm}")
        return 0

    if malformed and not as_json:
        print(f"[Warn] Dropped {len(malformed)} previously malformed line(s) during rewrite (--drop-malformed).")

    write_code_volume(code_path, entries)
    sync_shards_if_enabled(aoci_dir, entries)
    baseline["files"] = base_files
    save_baseline(aoci_dir, baseline, touched_paths=list(rename_map.keys()) + list(rename_map.values()))
    record_semantic_pool_entries(
        root,
        git_info,
        [(new_p, base_files[new_p], entries[new_p]) for _, new_p in migrated if new_p in entries and new_p in base_files],
    )

    if as_json:
        mig_payload: Dict[str, Any] = {
            "ok": True,
            "dry_run": False,
            "migrated": [{"from": o, "to": n} for o, n in migrated],
            "migrated_similar": [
                {"from": o, "to": n, "similarity_pct": pct} for o, n, pct in migrated_similar
            ],
            "rewritten_refs": updated_refs,
            "dropped_malformed": len(malformed),
        }
        if migrated_hubs:
            mig_payload["migrated_hubs"] = migrated_hubs
        print(json.dumps(mig_payload, indent=2, ensure_ascii=False))
    else:
        sim_note = f" | MigratedSimilar={len(migrated_similar)} (review & ack/upsert)" if migrated_similar else ""
        print(f"[AOCI Migrate-Renames] OK | Migrated={len(migrated)}{sim_note} | RewrittenRefs={len(updated_refs)}")
        for old_p, new_p in migrated:
            print(f"  > {old_p} -> {new_p}")
        for old_p, new_p, pct in migrated_similar:
            print(f"  ~> {old_p} -> {new_p} ({pct}% similar; still MODIFIED until `ack` or `upsert`)")
        for ref_p in updated_refs:
            print(f"  ~ ref updated: {ref_p}")
        for hm in migrated_hubs:
            print(f"  ~ [hubs] migrated: {hm}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    git_info = detect_git_info(root)
    git_status = check_git_exclusion(root, git_info, check_status=True)

    issues: List[str] = []
    warnings: List[str] = []

    if git_status["is_git"]:
        if not git_status["excluded"]:
            issues.append(
                f"Git exclusion missing: `{git_status['pattern']}` is NOT ignored. "
                "Run `aoci_index.py ensure-git-exclude`."
            )
        if git_status["tracked_files"]:
            issues.append(
                f"Git index tracks {len(git_status['tracked_files'])} `.aoci/` file(s)! "
                f"Run `git rm -r --cached {AOCI_DIR_NAME}`."
            )
        if git_status["status_leaks"] and git_status["excluded"] and not git_status["tracked_files"]:
            issues.append(
                f"`git status` still reports `.aoci/`: {', '.join(git_status['status_leaks'][:5])}"
            )

    for fname in (MANIFEST_FILE, META_FILE, CODE_FILE, BASELINE_FILE):
        if not (aoci_dir / fname).exists():
            issues.append(f"Missing required index file: `.aoci/{fname}`")

    if issues and not (aoci_dir / CODE_FILE).exists():
        if args.json:
            print(json.dumps({"ok": False, "issues": issues, "git": git_status}, indent=2, ensure_ascii=False))
        else:
            print("[AOCI Verify] FAIL")
            for i in issues:
                print(f"  - {i}")
        return 1

    drift = compute_drift(
        root,
        git_info,
        fast_mtime=getattr(args, "fast", False) or getattr(args, "git_fast", False),
        max_bytes=args.max_bytes,
        git_incremental=getattr(args, "git_fast", False),
        allow_heal=False,
    )
    if drift["renamed"]:
        sample = drift["renamed"][0]
        issues.append(
            f"{len(drift['renamed'])} renamed file(s) need migration "
            f"(e.g. {sample['from']} -> {sample['to']}; run `migrate-renames`)"
        )
    if drift["added"]:
        issues.append(f"{len(drift['added'])} unindexed new file(s) (e.g. {drift['added'][0]['path']})")
    if drift["modified"]:
        issues.append(
            f"{len(drift['modified'])} modified/stale file(s) (e.g. {drift['modified'][0]['path']}; "
            "`upsert` a revised entry, or `ack` if the entry is still accurate)"
        )
    if drift["missing_in_code"]:
        issues.append(f"{len(drift['missing_in_code'])} file(s) missing from `aoci.code.txt`")
    if drift["deleted"]:
        issues.append(f"{len(drift['deleted'])} deleted/orphan file(s) still in index (run `upsert --prune-only`)")
    if drift["malformed_lines"]:
        issues.append(f"{len(drift['malformed_lines'])} malformed line(s) in `aoci.code.txt`")
    for inv in drift["invalid_entries"]:
        issues.append(inv["message"])
    hub_verify_issues, _ = _hubs.validate_hubs_volume(
        root, aoci_dir, drift["_code_entries"], fingerprints=drift.get("_fingerprints"), check_live_symbols=False
    )
    issues.extend(hub_verify_issues)

    for dom, paths in drift["undeclared_domains"].items():
        warnings.append(f"Undeclared Domain `{dom}` in `.aoci/aoci.meta.txt` (used by {len(paths)} file(s))")
    if drift["duplicate_entries"]:
        warnings.append(
            f"{len(drift['duplicate_entries'])} duplicate path(s) in `aoci.code.txt` "
            f"(e.g. {drift['duplicate_entries'][0]}); the next upsert keeps the last occurrence"
        )
    dangling = find_dangling_refs(root, drift["_code_entries"])
    if dangling:
        sample_rel, sample_refs = next(iter(sorted(dangling.items())))
        warnings.append(
            f"{len(dangling)} entr(ies) reference missing paths in R:/A: "
            f"(e.g. {sample_rel} -> {sample_refs[0]}; run `lint`)"
        )

    code_text = drift.get("_code_text") or (aoci_dir / CODE_FILE).read_text(encoding="utf-8", errors="replace")
    entries: Dict[str, str] = drift["_code_entries"]

    manifest_p = aoci_dir / MANIFEST_FILE
    if manifest_p.is_file():
        if "#AOCI-ROOT-MANIFEST:" not in manifest_p.read_text(encoding="utf-8", errors="replace"):
            warnings.append("`.aoci/aoci.txt` is missing the `#AOCI-ROOT-MANIFEST:` header.")
    meta_p = aoci_dir / META_FILE
    if meta_p.is_file():
        if "#AOCI-META-VOLUME:" not in meta_p.read_text(encoding="utf-8", errors="replace"):
            warnings.append("`.aoci/aoci.meta.txt` is missing the `#AOCI-META-VOLUME:` header.")
    shards_manifest_p = aoci_dir / SHARDS_DIR / SHARDS_MANIFEST
    if shards_manifest_p.is_file():
        try:
            sm = json.loads(shards_manifest_p.read_text(encoding="utf-8"))
            if isinstance(sm, dict) and int(sm.get("total_files", -1)) != len(entries):
                warnings.append(
                    f"`.aoci/{SHARDS_DIR}/{SHARDS_MANIFEST}` is out of sync "
                    f"(total_files={sm.get('total_files')}, code entries={len(entries)}); run `shard`."
                )
        except (OSError, ValueError, TypeError):
            warnings.append(f"`.aoci/{SHARDS_DIR}/{SHARDS_MANIFEST}` is unreadable or invalid JSON; run `shard`.")
    est_tokens = estimate_tokens(code_text)
    layer_dist: Dict[str, int] = {}
    domain_dist: Dict[str, int] = {}
    imp_high = 0
    for pe in parse_entries_map(entries).values():
        layer_dist[pe.layer] = layer_dist.get(pe.layer, 0) + 1
        domain_dist[pe.domain] = domain_dist.get(pe.domain, 0) + 1
        if pe.imp >= 8:
            imp_high += 1

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    ok = len(issues) == 0
    report = {
        "ok": ok,
        "root": str(root),
        "elapsed_ms": elapsed_ms,
        "git": git_status,
        "indexed_files": len(entries),
        "high_importance_files": imp_high,
        "code_volume_bytes": len(code_text.encode("utf-8")),
        "estimated_tokens": est_tokens,
        "layers": dict(sorted(layer_dist.items())),
        "domains": dict(sorted(domain_dist.items())),
        "issues": issues,
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"[AOCI Verify] {'PASS' if ok else 'FAIL'} | Root: {root} | {elapsed_ms}ms")
        print(
            f"[Stats] Indexed={len(entries)} | HighImp(8-9)={imp_high} | "
            f"Size={report['code_volume_bytes']}B (~{est_tokens} tokens)"
        )
        if layer_dist:
            ld = ", ".join(f"{k}:{v}" for k, v in sorted(layer_dist.items()))
            print(f"[Layers] {ld}")
        if domain_dist:
            dd = ", ".join(f"{k}:{v}" for k, v in sorted(domain_dist.items()))
            print(f"[Domains] {dd}")
        if git_status["is_git"]:
            print(f"[Git Exclude] {'OK' if git_status['ok'] else 'FAIL'} ({git_status['pattern']})")
        if warnings:
            print("Warnings:")
            for w in warnings:
                print(f"  - {w}")
        if issues:
            print("Issues:")
            for iss in issues:
                print(f"  - {iss}")

    return 0 if ok else 1


def search_entries_bm25(
    entries: Dict[str, str],
    query_text: str,
    candidate_paths: Optional[Set[str]] = None,
    top_k: int = 20,
    glossary: Optional[Sequence[Sequence[str]]] = None,
    parsed_entries: Optional[Any] = None,
) -> List[Tuple[float, str, str]]:
    """Rank FRAS entries with field-weighted Okapi BM25 + Importance centrality prior."""
    return _graph.search_entries_bm25(
        entries,
        query_text,
        candidate_paths=candidate_paths,
        top_k=top_k,
        index_cls=BM25Index,
        glossary=glossary,
        parsed_entries=parsed_entries,
    )


def search_entries_bm25_detailed(
    entries: Dict[str, str],
    query_text: str,
    candidate_paths: Optional[Set[str]] = None,
    top_k: int = 20,
    glossary: Optional[Sequence[Sequence[str]]] = None,
    parsed_entries: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Rank FRAS entries with BM25 + project glossary expansion and return per-hit term attribution."""
    return _graph.search_entries_bm25_detailed(
        entries,
        query_text,
        candidate_paths=candidate_paths,
        top_k=top_k,
        index_cls=BM25Index,
        glossary=glossary,
        parsed_entries=parsed_entries,
    )


def _print_impacted_tests(impacted_tests: Sequence[Dict[str, Any]]) -> None:
    print(f"\n## Impacted Tests ({len(impacted_tests)})")
    for item in impacted_tests:
        print(f"  [{item['reason']}, hop={item['hop']}, via={item['via']}] {item['entry']}")


def cmd_query(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    code_path = root / AOCI_DIR_NAME / CODE_FILE
    if not code_path.exists():
        print(f"ERROR: `{code_path}` not found.", file=sys.stderr)
        return 2

    entries, _ = parse_code_volume(code_path)
    parsed_map = parse_entries_map(entries)
    impact_target = normalize_relpath(args.impact) if args.impact else None
    raw_tests_for = getattr(args, "tests_for", None)
    tests_for_target = normalize_relpath(raw_tests_for) if raw_tests_for else None
    keyword = args.keyword.lower() if args.keyword else None
    layer_set = _parse_code_filter_set(args.layer)
    domain_set = _parse_code_filter_set(args.domain)
    search_q = getattr(args, "search", None)
    raw_top = getattr(args, "top", None)
    top_k = (20 if search_q else 0) if raw_top is None else int(raw_top)
    raw_depth = getattr(args, "depth", None)
    impact_depth = int(raw_depth) if raw_depth is not None else 1
    tests_depth = int(raw_depth) if raw_depth is not None else 3

    dependents: Optional[Dict[str, Set[str]]] = None
    impact_set: Optional[Set[str]] = None
    if impact_target or tests_for_target:
        _, dependents = build_fras_graph(entries, parsed_entries=parsed_map)
    if impact_target and dependents is not None:
        # Impact = the target plus every entry that (transitively, up to --depth) depends on it
        impact_set = {impact_target} | set(bfs_neighbors(dependents, [impact_target], impact_depth))

    impacted_tests: Optional[List[Dict[str, Any]]] = None
    impacted_test_paths: Optional[Set[str]] = None
    if tests_for_target and dependents is not None:
        impacted_tests = _test_impact.find_impacted_tests(
            root,
            entries,
            dependents,
            [tests_for_target],
            max_depth=tests_depth,
            parsed_entries=parsed_map,
            live_imports=bool(getattr(args, "live_test_imports", False)),
        )
        impacted_test_paths = {item["path"] for item in impacted_tests}

    filtered_paths: Set[str] = set()
    results: List[str] = []
    for rel, line in sorted(entries.items()):
        pe = parsed_map.get(rel)
        if pe is None:
            continue
        if layer_set and pe.layer not in layer_set:
            continue
        if domain_set and pe.domain not in domain_set:
            continue
        if args.min_importance and pe.imp < args.min_importance:
            continue
        if args.path and not fnmatch.fnmatchcase(rel, args.path):
            continue
        if keyword and keyword not in line.lower():
            continue
        if impact_set is not None and rel not in impact_set:
            continue
        if impacted_test_paths is not None and rel not in impacted_test_paths:
            continue
        filtered_paths.add(rel)
        results.append(line)

    if impacted_tests is not None:
        impacted_tests = [item for item in impacted_tests if item["path"] in filtered_paths]
        if not search_q:
            results = [item["entry"] for item in impacted_tests]

    scores_out: Optional[List[Dict[str, Any]]] = None
    glossary_expansions: List[Dict[str, Any]] = []
    if search_q:
        glossary = parse_glossary(root / AOCI_DIR_NAME)
        hits, glossary_expansions = search_entries_bm25_detailed(
            entries,
            search_q,
            candidate_paths=filtered_paths,
            top_k=top_k,
            glossary=glossary,
            parsed_entries=parsed_map,
        )
        results = [h["entry"] for h in hits]
        scores_out = hits
        if impacted_tests is not None:
            hit_paths = {h["path"] for h in hits}
            impacted_tests = [item for item in impacted_tests if item["path"] in hit_paths]
    elif top_k > 0:
        results = results[:top_k]
        if impacted_tests is not None:
            impacted_tests = impacted_tests[:top_k]

    if args.json:
        payload: Dict[str, Any] = {"count": len(results), "entries": results}
        if scores_out is not None:
            payload["scores"] = scores_out
        if glossary_expansions:
            payload["glossary_expansions"] = glossary_expansions
        if impacted_tests is not None:
            payload["impacted_tests"] = impacted_tests
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"# Query matches: {len(results)} / {len(entries)}")
        if glossary_expansions:
            exp_txt = "; ".join(f"{e['term']} -> {', '.join(e['aliases'])}" for e in glossary_expansions)
            print(f"# Glossary: {exp_txt}")
        if scores_out is not None:
            for item in scores_out:
                print(item["entry"])
                g_note = (
                    f", glossary=[{'; '.join(item['matched_glossary'])}]"
                    if item.get("matched_glossary")
                    else ""
                )
                print(f"#   match: score={item['score']}, terms=[{', '.join(item.get('matched_terms', []))}]{g_note}")
        elif impacted_tests is not None:
            for item in impacted_tests:
                print(item["entry"])
                print(f"#   test-impact: reason={item['reason']}, hop={item['hop']}, via={item['via']}")
        else:
            for line in results:
                print(line)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """
    Expand the architectural neighborhood for one or more target files:
      Global Invariants + Target entries + dependencies (what targets read/call) + dependents (who
      reads/calls targets). Edges come from R: (X depends on listed paths) and A: (`sym<-C` means
      caller C depends on X), so direction is preserved.
    """
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        print(f"ERROR: `{code_path}` not found. Run `init` first.", file=sys.stderr)
        return 2

    entries, _ = parse_code_volume(code_path)
    parsed_map = parse_entries_map(entries)
    deps_adj, dependents_adj = build_fras_graph(entries, parsed_entries=parsed_map)
    invariants = parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-"
    budget = int(getattr(args, "budget", 0) or 0)
    with_tests = bool(getattr(args, "with_tests", False))
    payload = build_context_subgraph(
        root,
        entries,
        deps_adj,
        dependents_adj,
        invariants,
        args.paths,
        depth=args.depth,
        budget=budget,
        parsed_entries=parsed_map,
    )
    ctx_hub_paths = [
        normalize_relpath(ln.split("[", 1)[0].strip())
        for ln in (payload["targets"] + payload["dependencies"])
        if ln
    ]
    hub_secs = _hubs.lookup_hub_sections(aoci_dir, ctx_hub_paths)
    if hub_secs:
        payload["hub_sections"] = hub_secs
    if with_tests:
        payload["impacted_tests"] = _test_impact.find_impacted_tests(
            root,
            entries,
            dependents_adj,
            args.paths,
            max_depth=max(3, int(args.depth)),
            parsed_entries=parsed_map,
            live_imports=bool(getattr(args, "live_test_imports", False)),
        )

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"# AOCI Context Subgraph (depth={args.depth})")
    print(f"# Global-Invariants: {invariants}")
    if budget > 0:
        trunc_note = f" | Omitted={payload['omitted_count']}" if payload.get("truncated") else ""
        print(f"# Token-Budget: ~{payload['estimated_tokens']}/{budget} tokens{trunc_note}")
    if payload["missing_targets"]:
        print(f"# Missing from index: {', '.join(payload['missing_targets'])}")
    print(f"\n## Targets ({len(payload['targets'])})")
    for ln in payload["targets"]:
        print(ln)
    if hub_secs:
        print(f"\n## Hub Sections ({len(hub_secs)})")
        for sec in hub_secs:
            print(sec["entry"])
    print(f"\n## Dependencies / Read-Along ({len(payload['dependencies'])})")
    for ln in payload["dependencies"]:
        print(ln)
    print(f"\n## Dependents / Upstream Callers ({len(payload['callers'])})")
    for ln in payload["callers"]:
        print(ln)
    if with_tests:
        _print_impacted_tests(payload.get("impacted_tests", []))
    if payload.get("truncated"):
        print(f"\n# Omitted under --budget ({payload['omitted_count']}): {', '.join(payload['omitted_paths'])}")
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    """Print a compact architectural digest for fast single-pass orientation (especially on >600-file repos)."""
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        print(f"ERROR: `{code_path}` not found. Run `init` first.", file=sys.stderr)
        return 2

    raw_code_texts: List[str] = []
    entries, _ = parse_code_volume(code_path, raw_text_out=raw_code_texts)
    project = parse_manifest_field(aoci_dir, "#Project:") or root.name
    invariants = parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-"
    git_hdr = parse_manifest_field(aoci_dir, "#Git-Excluded:") or "unknown"
    domains = parse_declared_domains(aoci_dir)

    dir_counts: Dict[str, int] = {}
    layer_counts: Dict[str, int] = {}
    core_entries: List[Tuple[int, str, str]] = []

    for rel, line in sorted(entries.items()):
        top = rel.split("/", 1)[0] if "/" in rel else "."
        dir_counts[top] = dir_counts.get(top, 0) + 1
        m = ENTRY_RE.match(line)
        if m:
            layer_counts[m.group("layer")] = layer_counts.get(m.group("layer"), 0) + 1
            imp = int(m.group("imp"))
            if imp >= args.min_importance:
                core_entries.append((imp, rel, line))

    core_entries.sort(key=lambda item: (-item[0], item[1]))
    core_lines = [line for _, _, line in core_entries[: args.max_core]]

    code_text = raw_code_texts[0] if raw_code_texts else ""
    est_tokens = estimate_tokens(code_text)

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "project": project,
                    "git_excluded": git_hdr,
                    "global_invariants": invariants,
                    "indexed_files": len(entries),
                    "estimated_full_tokens": est_tokens,
                    "domains": domains,
                    "layers": dict(sorted(layer_counts.items())),
                    "top_dirs": dict(sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
                    "core_entries": core_lines,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(f"# AOCI Overview: {project} ({len(entries)} files, ~{est_tokens} tokens full)")
    print(f"# Root: {root} | Git-Excluded: {git_hdr}")
    print(f"# Global-Invariants: {invariants}")
    if domains:
        dom_str = ", ".join(f"{k}={v}" for k, v in sorted(domains.items()))
        print(f"# Domains: {dom_str}")
    if layer_counts:
        lay_str = ", ".join(f"{k}:{v}" for k, v in sorted(layer_counts.items()))
        print(f"# Layers: {lay_str}")
    if dir_counts:
        top_str = ", ".join(
            f"{k}({v})" for k, v in sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
        )
        print(f"# Top-Dirs: {top_str}")
    print(f"\n# Core Entries (Importance >= {args.min_importance}, showing {len(core_lines)}/{len(core_entries)}):")
    for line in core_lines:
        print(line)
    return 0


def compute_outlines(
    root: Path,
    raw_paths: Sequence[str],
    max_items: int = 30,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """Validate target paths safely under `root` and extract lightweight static outlines."""
    outlines: List[Dict[str, Any]] = []
    missing: List[str] = []
    rejected: List[Dict[str, str]] = []

    for raw_p in raw_paths:
        raw_str = str(raw_p).strip()
        rel = normalize_relpath(raw_str)
        if (
            not rel
            or raw_str.startswith(("/", "\\"))
            or Path(raw_str).is_absolute()
            or re.match(r"^[A-Za-z]:", raw_str)
            or any(seg in ("..", ".") for seg in rel.split("/"))
        ):
            rejected.append({"path": raw_str, "reason": "path-invalid"})
            continue
        full = root / rel
        if not _is_within_root(root, full):
            rejected.append({"path": rel, "reason": "outside-root"})
            continue
        try:
            st = os.lstat(full)
        except OSError:
            missing.append(rel)
            continue
        if stat.S_ISLNK(st.st_mode):
            rejected.append({"path": rel, "reason": "symlink"})
            continue
        if not stat.S_ISREG(st.st_mode):
            rejected.append({"path": rel, "reason": "not-regular-file"})
            continue
        if st.st_size > max_bytes:
            rejected.append({"path": rel, "reason": f"too-large(>{max_bytes}B)"})
            continue
        try:
            raw_bytes = full.read_bytes()
        except OSError:
            rejected.append({"path": rel, "reason": "unreadable"})
            continue
        if is_probably_binary_bytes(raw_bytes[:8192]):
            rejected.append({"path": rel, "reason": "binary-content"})
            continue
        if extract_file_outline is _parsers.extract_file_outline:
            norm_bytes = raw_bytes.replace(b"\r\n", b"\n") if b"\r\n" in raw_bytes else raw_bytes
            outlines.append(_parsers._extract_outline_from_bytes(norm_bytes, full.suffix.lower(), rel, max_items=max_items))
        else:
            outlines.append(extract_file_outline(full, rel, max_items=max_items))

    return {"outlines": outlines, "missing": missing, "rejected": rejected}


def cmd_outline(args: argparse.Namespace) -> int:
    """Extract lightweight static imports & top-level symbols to speed up R:/A: authoring."""
    root = resolve_root(args.root)
    max_bytes = getattr(args, "max_bytes", DEFAULT_MAX_BYTES)
    res = compute_outlines(root, args.paths, max_items=args.max_items, max_bytes=max_bytes)
    outlines = res["outlines"]
    missing = res["missing"]
    rejected = res["rejected"]

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if (not missing and not rejected) else 1

    for o in outlines:
        print(f"=== {o['path']} [{o['size_tier']}, {o['lines']}L] ===")
        print(f"  imports: {', '.join(o['imports']) if o['imports'] else '-'}")
        print(f"  symbols: {', '.join(o['symbols']) if o['symbols'] else '-'}")
    if missing:
        print(f"Missing files: {', '.join(missing)}", file=sys.stderr)
    if rejected:
        for rej in rejected:
            print(f"Rejected `{rej['path']}` ({rej['reason']})", file=sys.stderr)
    return 0 if (not missing and not rejected) else 1


def cmd_ack(args: argparse.Namespace) -> int:
    """
    Re-baseline files whose content changed but whose existing FRAS entry is still accurate
    (refactors, typo fixes, body-only edits). Refreshes the Size tier automatically.
    """
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    as_json = bool(getattr(args, "json", False))
    impl_only = bool(getattr(args, "impl_only", False))

    def _fail(msg: str, rc: int = 2) -> int:
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return rc

    if not code_path.exists():
        return _fail(f"`{code_path}` not found. Run `init` first.", 2)
    if not args.paths and not args.all_modified and not impl_only:
        return _fail("Pass one or more paths, or `--all-modified --reviewed`, or `--impl-only --reviewed`.", 2)
    if (args.all_modified or impl_only) and not args.dry_run and not getattr(args, "reviewed", False):
        return _fail(
            "`--all-modified` / `--impl-only` requires `--reviewed` to confirm every acknowledged file was read and its "
            "FRAS entry verified accurate (Rule #4: Evidence-Only Semantics). Prefer explicit `ack <paths...>` "
            "or preview first with `--dry-run`.",
            2,
        )

    git_info = detect_git_info(root)
    if args.dry_run:
        check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        git_status = ensure_git_exclusion(root, git_info, check_status=False)
        if git_status["is_git"] and not git_status["excluded"]:
            return _fail("Failed to exclude `.aoci/` from Git. Aborting ack.", 2)

    hubs_refreshed: List[str] = []
    lock_ctx: Any = contextlib.nullcontext() if args.dry_run else IndexLock(aoci_dir)
    try:
        with lock_ctx:
            entries, malformed = parse_code_volume(code_path)
            baseline = load_baseline(aoci_dir)
            base_files: Dict[str, Any] = baseline.get("files", {})
            targets = [normalize_relpath(p) for p in args.paths]
            auto_select = bool(args.all_modified or (impl_only and not targets))
            explicit_impl_check = bool(impl_only and targets and not args.all_modified)
            live = LiveFileSet(
                root,
                git_info,
                base_files,
                args.max_bytes,
                only_paths=None if auto_select else targets,
            )

            skipped_contract_changed: List[Dict[str, str]] = []
            if auto_select:
                for cand_rel in sorted(live.fps.keys()):
                    if cand_rel not in entries:
                        continue
                    cached = base_files.get(cand_rel)
                    if cached is None or live.fps[cand_rel]["sha256"] != cached.get("sha256"):
                        if impl_only:
                            ck = _classify_modified_entry(root, cand_rel, cached, cur_fp=live.fps.get(cand_rel))
                            if ck == "impl_only":
                                targets.append(cand_rel)
                            else:
                                skipped_contract_changed.append({"path": cand_rel, "change_kind": ck})
                        else:
                            targets.append(cand_rel)
            targets = list(dict.fromkeys(targets))

            errors: List[str] = []
            acked: List[str] = []
            resized: List[str] = []
            for rel in targets:
                canon = live.canonical(rel)
                if canon is None:
                    errors.append(live.explain_missing(rel))
                    continue
                line = entries.get(canon)
                if line is None:
                    errors.append(f"`{canon}` has no entry in aoci.code.txt; author one with `upsert`.")
                    continue
                f = parse_entry_fields(line)
                if not f:
                    errors.append(f"Entry for `{canon}` is malformed; fix it with `upsert`.")
                    continue
                tag_errs = validate_parsed_entry_tags(canon, f, line, actual_lines=None)
                if tag_errs:
                    errors.append(f"{tag_errs[0][1]} Fix it with `upsert`.")
                    continue
                if explicit_impl_check:
                    ck = _classify_modified_entry(
                        root, canon, base_files.get(canon), cur_fp=live.fps.get(canon)
                    )
                    if ck != "impl_only":
                        errors.append(
                            f"`{canon}` has change_kind `{ck}` (requires review via `upsert` or omit `--impl-only`)."
                        )
                        continue
                fp = live.fresh_fingerprint(canon)
                if f["size"] != fp["size_tier"]:
                    entries[canon] = (
                        f"{canon}[{f['layer']}{f['domain']}{f['imp']}{fp['size_tier']}]: "
                        f"F:{f['f']} | R:{f['r']} | A:{f['a']} | S:{f['s']}"
                    )
                    resized.append(f"{canon} {f['size']}->{fp['size_tier']}")
                base_files[canon] = fp
                acked.append(canon)

            if errors:
                if as_json:
                    print(json.dumps({"ok": False, "errors": errors}, indent=2, ensure_ascii=False))
                else:
                    print(f"ERROR: {len(errors)} path(s) cannot be acknowledged; nothing was written:", file=sys.stderr)
                    for err in errors:
                        print(f"  - {err}", file=sys.stderr)
                return 1

            if args.dry_run:
                if as_json:
                    dry_payload: Dict[str, Any] = {
                        "ok": True,
                        "dry_run": True,
                        "acked": acked,
                        "resized": resized,
                    }
                    if impl_only:
                        dry_payload["impl_only"] = True
                        dry_payload["skipped_contract_changed"] = skipped_contract_changed
                    print(json.dumps(dry_payload, indent=2, ensure_ascii=False))
                else:
                    print(f"[AOCI Ack DRY-RUN] WouldAck={len(acked)} | WouldResize={len(resized)}")
                    for p in acked:
                        print(f"  = {p}")
                    if skipped_contract_changed:
                        print(f"[Skipped] {len(skipped_contract_changed)} non-impl-only file(s) require manual review:")
                        for sk in skipped_contract_changed[:30]:
                            print(f"  ! {sk['path']} ({sk['change_kind']})")
                return 0

            if resized:
                if malformed:
                    if as_json:
                        print(
                            json.dumps(
                                {
                                    "ok": False,
                                    "error": f"`aoci.code.txt` contains {len(malformed)} malformed line(s).",
                                    "malformed": [{"line": ln, "text": txt[:120]} for ln, txt in malformed[:10]],
                                },
                                indent=2,
                                ensure_ascii=False,
                            )
                        )
                    else:
                        report_malformed_block(malformed)
                    return 1
                write_code_volume(code_path, entries)
                sync_shards_if_enabled(aoci_dir, entries)
            if acked:
                baseline["files"] = base_files
                save_baseline(aoci_dir, baseline, touched_paths=acked)
                record_semantic_pool_entries(
                    root,
                    git_info,
                    [(c, base_files[c], entries[c]) for c in acked if c in entries and c in base_files],
                )
                if (aoci_dir / HUBS_FILE).is_file() and not args.dry_run:
                    hubs_refreshed = _hubs.refresh_hub_spans_for_paths(root, aoci_dir, acked)
    except TimeoutError as exc:
        return _fail(str(exc), 3)

    if as_json:
        ack_payload: Dict[str, Any] = {
            "ok": True,
            "dry_run": False,
            "acked": acked,
            "resized": resized,
            "all_modified": bool(args.all_modified),
        }
        if hubs_refreshed:
            ack_payload["hubs_refreshed"] = hubs_refreshed
        if impl_only:
            ack_payload["impl_only"] = True
            ack_payload["skipped_contract_changed"] = skipped_contract_changed
        print(json.dumps(ack_payload, indent=2, ensure_ascii=False))
    else:
        print(f"[AOCI Ack] OK | Acked={len(acked)} | SizeTierUpdated={len(resized)}")
        for p in acked:
            print(f"  = {p}")
        for r in resized:
            print(f"  [size] {r}")
        for hr in hubs_refreshed:
            print(f"  [hubs] {hr}")
        if skipped_contract_changed:
            print(f"[Skipped] {len(skipped_contract_changed)} non-impl-only file(s) require `upsert` review:")
            for sk in skipped_contract_changed[:30]:
                print(f"  ! {sk['path']} ({sk['change_kind']})")
        if (args.all_modified or impl_only) and acked:
            print("[Note] Automatic selection trusts that every listed entry was verified against its new source.")
    return 0


def _validate_zoom_target_file(
    root: Path,
    raw_path: str,
    max_bytes: int,
) -> Tuple[Optional[str], Optional[Path], Optional[str]]:
    rel, path_err = _hubs.validate_hub_relpath(str(raw_path))
    if path_err is not None or rel is None:
        return None, None, (path_err or f"Invalid relative path `{raw_path}`.")

    full = root / rel
    if not _is_within_root(root, full):
        return None, None, f"Path `{rel}` escapes repository root."
    try:
        st = os.lstat(full)
    except OSError:
        return None, None, f"Target file `{rel}` does not exist on disk."
    if stat.S_ISLNK(st.st_mode):
        return None, None, f"Refusing symlink target `{rel}`."
    if not stat.S_ISREG(st.st_mode):
        return None, None, f"Target `{rel}` is not a regular file."
    if st.st_size > max_bytes:
        return None, None, f"Target `{rel}` exceeds `--max-bytes` ({st.st_size} > {max_bytes})."
    try:
        with full.open("rb") as fh:
            head = fh.read(8192)
    except OSError as exc:
        return None, None, f"Cannot read `{rel}`: {exc}"
    if is_probably_binary_bytes(head):
        return None, None, f"Target `{rel}` appears to be binary."
    return rel, full, None


def compute_zoom_read(
    root: Path,
    raw_path: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    *,
    cached_entries: Optional[Dict[str, str]] = None,
    cached_hub_records: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Read L1 entry, L1.5 hub sections, and live symbol spans for `raw_path` without mutating disk."""
    rel, full, err = _validate_zoom_target_file(root, raw_path, max_bytes)
    if err is not None or rel is None or full is None:
        return {"ok": False, "error": err or f"Invalid path `{raw_path}`.", "exit_code": 2}

    aoci_dir = root / AOCI_DIR_NAME
    if not aoci_dir.is_dir():
        return {"ok": False, "error": f"`.aoci/` is not initialized at {root}. Run `init` first.", "exit_code": 2}

    if cached_entries is not None:
        code_entries = cached_entries
    else:
        code_path = aoci_dir / CODE_FILE
        code_entries, _ = parse_code_volume(code_path) if code_path.is_file() else ({}, [])
    code_entry = code_entries.get(rel)

    if cached_hub_records is not None:
        hub_records = cached_hub_records
    else:
        hub_records, _ = _hubs.parse_hubs_volume(aoci_dir / HUBS_FILE)
    matching_hubs = [r["line"] for r in hub_records if r["relpath"] == rel]
    symbol_spans = _hubs.extract_symbol_spans(full, rel)

    return {
        "ok": True,
        "mode": "read",
        "root": str(root),
        "path": rel,
        "code_entry": code_entry,
        "hub_entries": matching_hubs,
        "symbol_spans": symbol_spans,
    }


def cmd_zoom(args: argparse.Namespace) -> int:
    """Read, scaffold, or upsert symbol-level H-tier sections in `.aoci/aoci.hubs.txt`."""
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    as_json = bool(getattr(args, "json", False))

    def _fail(msg: str, rc: int = 2) -> int:
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return rc

    is_upsert = bool(args.upsert or args.upsert_file or args.prune_missing)
    if args.scaffold and is_upsert:
        return _fail("Cannot combine `--scaffold` with `--upsert`, `--upsert-file`, or `--prune-missing`.", 2)
    if is_upsert and args.path:
        return _fail("Do not pass positional `<path>` together with `--upsert`/`--upsert-file`/`--prune-missing`.", 2)
    if not is_upsert and not args.path:
        return _fail("Specify a target `<path>` (or pass `--upsert` / `--upsert-file` / `--prune-missing`).", 2)

    if is_upsert:
        if not aoci_dir.is_dir():
            return _fail(f"`.aoci/` is not initialized at {root}. Run `init` first.", 2)
        git_info = detect_git_info(root)
        if args.dry_run:
            check_git_exclusion(root, git_info, check_status=False, write_cache=False)
        else:
            git_status = ensure_git_exclusion(root, git_info, check_status=False)
            if git_status["is_git"] and not git_status["excluded"]:
                return _fail("Failed to exclude `.aoci/` from Git. Aborting zoom upsert.", 2)

        raw_lines: List[str] = []
        if args.upsert_file:
            ep = _resolve_cli_file_path(root, args.upsert_file, must_exist=True)
            try:
                raw_lines.extend(ep.read_text(encoding="utf-8-sig").splitlines())
            except OSError as exc:
                return _fail(f"Cannot read `--upsert-file` `{ep}`: {exc}", 2)
        if args.upsert:
            raw_lines.extend(args.upsert)

        try:
            res = _hubs.upsert_hub_entries(
                root,
                aoci_dir,
                raw_lines,
                prune_missing=bool(args.prune_missing),
                dry_run=bool(args.dry_run),
                lock_cls=IndexLock,
            )
        except TimeoutError as exc:
            return _fail(str(exc), 3)

        if not res.get("ok"):
            if as_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                errs = res.get("errors") or [res.get("error", "Zoom upsert failed.")]
                print(f"ERROR: Zoom upsert failed ({len(errs)} issue(s)); no files were modified:", file=sys.stderr)
                for err in errs:
                    print(f"  - {err}", file=sys.stderr)
            return 1

        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            mode_tag = " DRY-RUN" if res.get("dry_run") else ""
            print(
                f"[AOCI Zoom Upsert{mode_tag}] OK | Upserted={len(res['upserted'])} | "
                f"Pruned={len(res['pruned'])} | Total={res['total']}"
            )
            for w in res.get("warnings", ()):
                print(f"  WARN: {w}", file=sys.stderr)
        return 0

    if args.scaffold:
        rel, _, err = _validate_zoom_target_file(root, str(args.path), args.max_bytes)
        if err is not None or rel is None:
            return _fail(err or f"Invalid relative path `{args.path}`.", 2)
        drafts = _hubs.scaffold_hub_entries(root, rel)
        out_path_str: Optional[str] = None
        if args.output:
            out_path = _resolve_cli_file_path(root, args.output, must_exist=False)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            header = (
                "# AOCI Zoom Scaffold — Read each section and replace <TODO:read-section> before `zoom --upsert`!\n"
            )
            write_text_lf(out_path, header + "\n".join(drafts) + ("\n" if drafts else ""))
            out_path_str = str(out_path)
        if as_json:
            scaf_payload: Dict[str, Any] = {
                "ok": True,
                "mode": "scaffold",
                "root": str(root),
                "path": rel,
                "count": len(drafts),
                "output": args.output,
                "entries": drafts,
            }
            if out_path_str is not None:
                scaf_payload["output_path"] = out_path_str
            print(json.dumps(scaf_payload, indent=2, ensure_ascii=False))
        else:
            print(f"# Scaffolded {len(drafts)} hub section(s) for `{rel}` (replace <TODO:read-section> after reading source):")
            for ln in drafts:
                print(ln)
        return 0

    zoom_res = compute_zoom_read(root, str(args.path), max_bytes=args.max_bytes)
    if not zoom_res.get("ok"):
        return _fail(str(zoom_res.get("error", "Zoom read failed.")), int(zoom_res.get("exit_code", 2)))

    if as_json:
        print(json.dumps(zoom_res, indent=2, ensure_ascii=False))
    else:
        print(f"=== {zoom_res['path']} ===")
        print(f"  code_entry: {zoom_res['code_entry'] or '-'}")
        matching_hubs = zoom_res["hub_entries"]
        symbol_spans = zoom_res["symbol_spans"]
        print(f"  hub_entries ({len(matching_hubs)}):")
        for ln in matching_hubs:
            print(f"    {ln}")
        print(f"  symbol_spans ({len(symbol_spans)}):")
        for sp in symbol_spans:
            print(f"    {sp['symbol']} [L{sp['start']}-L{sp['end']}] ({sp['kind']})")
    return 0


def compute_lint(
    root: Path,
    *,
    code_filter: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cached_entries: Optional[Dict[str, str]] = None,
    cached_parsed: Optional[Dict[str, ParsedEntry]] = None,
    cached_malformed: Optional[Sequence[Tuple[int, str]]] = None,
    cached_duplicates: Optional[Sequence[str]] = None,
    cached_domains: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run FRAS L1 + L1.5 (`aoci.hubs.txt`) quality checks and return structured payload."""
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    duplicates: List[str] = []
    if cached_entries is not None and cached_malformed is not None:
        entries = cached_entries
        malformed = list(cached_malformed)
        if cached_duplicates is not None:
            duplicates = list(cached_duplicates)
    else:
        entries, malformed = parse_code_volume(code_path, duplicates=duplicates)

    git_info = detect_git_info(root)
    ignores = load_ignore_matcher(root, git_info)
    base_files = load_baseline(aoci_dir).get("files", {})
    fps, _ = discover_and_fingerprint(
        root,
        git_info,
        ignores,
        base_files=base_files,
        fast_mtime=True,
        max_bytes=max_bytes,
    )
    parsed_map = cached_parsed if cached_parsed is not None else parse_entries_map(entries)
    domains = cached_domains if cached_domains is not None else parse_declared_domains(aoci_dir)
    findings = lint_entries(
        root,
        entries,
        domains,
        fingerprints=fps,
        parsed_entries=parsed_map,
    )
    _, hub_lint_findings = _hubs.validate_hubs_volume(root, aoci_dir, entries, fingerprints=fps)
    findings.extend(hub_lint_findings)
    for rel in sorted(set(duplicates)):
        findings.append({"path": rel, "code": "duplicate", "message": "path appears more than once in aoci.code.txt"})
    for ln, txt in malformed:
        findings.append({"path": f"L{ln}", "code": "malformed", "message": txt[:120]})
    if code_filter:
        wanted = {c.strip() for c in code_filter.split(",") if c.strip()}
        findings = [f for f in findings if f["code"] in wanted]

    by_code: Dict[str, int] = {}
    for f in findings:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1

    return {"ok": not findings, "indexed": len(entries), "counts": by_code, "findings": findings}


def cmd_lint(args: argparse.Namespace) -> int:
    """FRAS quality gate: vague/short F:, token budget vs Importance, empty S: on hubs, dangling refs, duplicates."""
    root = resolve_root(args.root)
    res = compute_lint(
        root,
        code_filter=args.code,
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
    )
    if res.get("error"):
        print(f"ERROR: {res['error']}", file=sys.stderr)
        return 2

    findings = res["findings"]
    by_code = res["counts"]
    indexed = res["indexed"]
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        label = "CLEAN" if not findings else f"{len(findings)} finding(s)"
        print(f"[AOCI Lint] {label} | Indexed={indexed}")
        if by_code:
            print("[Counts] " + ", ".join(f"{k}:{v}" for k, v in sorted(by_code.items())))
        for f in findings[: args.limit]:
            print(f"  [{f['code']}] {f['path']}: {f['message']}")
        if len(findings) > args.limit:
            print(f"  ... (+{len(findings) - args.limit} more; use --json or --limit)")
    return 1 if (findings and args.strict) else 0


def cmd_shard(args: argparse.Namespace) -> int:
    """Build or remove per-directory `.aoci/shards/<dir>.code.txt` slices for very large repositories."""
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        print(f"ERROR: `{code_path}` not found. Run `init` first.", file=sys.stderr)
        return 2

    shards_dir = aoci_dir / SHARDS_DIR
    try:
        with IndexLock(aoci_dir):
            if args.clean:
                removed = shards_dir.exists()
                if removed:
                    shutil.rmtree(shards_dir)
                if args.json:
                    print(json.dumps({"cleaned": removed, "shards_dir": str(shards_dir)}, indent=2))
                else:
                    print(f"[AOCI Shard] {'Removed' if removed else 'No'} `.aoci/{SHARDS_DIR}/` directory.")
                return 0

            entries, _ = parse_code_volume(code_path)
            manifest = write_shards(aoci_dir, entries, depth=args.depth)
    except TimeoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
    else:
        print(
            f"[AOCI Shard] OK | Shards={len(manifest['shards'])} (depth={manifest['depth']}) | "
            f"Index=.aoci/{SHARDS_DIR}/{SHARDS_INDEX} (auto-synced on upsert/ack/migrate-renames)"
        )
        for s in manifest["shards"][:40]:
            print(f"  {s['prefix']} -> .aoci/{s['file']} [files={s['count']}, hubs={s['hubs']}, ~{s['estimated_tokens']} tokens]")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Zero-token recovery of cached `(path, sha256) -> FRAS` entries across branches and worktrees."""
    root = resolve_root(args.root)
    git_info = detect_git_info(root)
    if args.dry_run:
        check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        ensure_git_exclusion(root, git_info, check_status=False)

    allow_sha = bool(getattr(args, "allow_sha_fallback", False) and not getattr(args, "exact_path_only", False))
    try:
        res = restore_from_semantic_pool(
            root,
            git_info,
            dry_run=args.dry_run,
            allow_sha_fallback=allow_sha,
            prune_deleted=not args.no_prune,
            max_bytes=args.max_bytes,
        )
    except TimeoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    if not res.get("ok"):
        print(f"ERROR: {res.get('error')}", file=sys.stderr)
        return 2 if "not found" in str(res.get("error")) else 1

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    mode = " DRY-RUN" if args.dry_run else ""
    cands = res.get("sha_candidates", [])
    print(
        f"[AOCI Restore{mode}] OK | Restored={len(res['restored'])} "
        f"(sha_fallback={len(res['restored_by_sha'])}) | Candidates={len(cands)} | "
        f"Pruned={len(res['pruned'])} | TotalIndexed={res['total_indexed']}"
    )
    for p in res["restored"][:40]:
        print(f"  + restored: {p}")
    for c in cands[:20]:
        print(f"  ? candidate (review manually): {c['path']} <- {c['matched_from']} [{c['reason']}]")
    for p in res["pruned"][:40]:
        print(f"  - pruned: {p}")
    return 0


def compute_diff_briefing(
    root: Path,
    rev: Optional[str] = None,
    staged: bool = False,
    depth: int = 1,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cached_entries: Optional[Dict[str, str]] = None,
    cached_deps: Optional[Dict[str, Set[str]]] = None,
    cached_dependents: Optional[Dict[str, Set[str]]] = None,
    cached_invariants: Optional[str] = None,
    with_tests: bool = False,
    cached_parsed: Optional[Any] = None,
    cached_hub_records: Optional[Sequence[Dict[str, Any]]] = None,
    preloaded_outline_files: Optional[Dict[str, Any]] = None,
    live_test_imports: bool = False,
) -> Dict[str, Any]:
    """
    Build a focused architectural impact packet for Git working-tree or revision diffs:
    Global Invariants + changed FRAS entries + non-obvious S: traps + downstream R: deps + affected callers.
    """
    if rev is not None and str(rev).strip().startswith("-"):
        return {"ok": False, "error": f"Invalid Git revision `{rev}`: revision specs cannot start with `-`."}

    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    git_info = detect_git_info(root)
    if not git_info.get("is_git"):
        return {"ok": False, "error": f"`{root}` is not inside a Git repository."}

    ignores = load_ignore_matcher(root, git_info)
    if cached_entries is not None:
        entries = cached_entries
    else:
        entries, _ = parse_code_volume(code_path)
    invariants = (
        cached_invariants
        if cached_invariants is not None
        else (parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-")
    )

    toplevel: Path = git_info["toplevel"]
    try:
        rel_sub = root.resolve().relative_to(toplevel.resolve()).as_posix()
    except ValueError:
        rel_sub = ""
    sub_prefix = "" if rel_sub in ("", ".") else rel_sub.strip("/") + "/"

    def _to_root_rel(git_rel: str) -> Optional[str]:
        norm = normalize_relpath(git_rel)
        if not norm:
            return None
        if not sub_prefix:
            return norm
        if norm.startswith(sub_prefix):
            tail = norm[len(sub_prefix) :]
            return tail or None
        return None

    changed_active: Set[str] = set()
    deleted_paths: Set[str] = set()
    renamed_pairs: List[Dict[str, str]] = []

    if rev or staged:
        git_args = ["diff", "--name-status", "-M", "--relative", "-z"]
        if staged:
            git_args.append("--cached")
        if rev:
            git_args.append(rev)
        git_args.extend(["--", "."])
        res = run_git(git_args, root)
        if res.returncode != 0:
            return {"ok": False, "error": f"`git diff` failed: {res.stderr.strip()}"}
        toks = res.stdout.split("\0")
        idx = 0
        while idx < len(toks):
            st_tok = toks[idx]
            idx += 1
            if not st_tok or idx >= len(toks):
                continue
            p1 = normalize_relpath(toks[idx])
            idx += 1
            code_ch = st_tok[0].upper()
            if code_ch in ("R", "C") and idx < len(toks):
                p2 = normalize_relpath(toks[idx])
                idx += 1
                if code_ch == "R" and p1:
                    deleted_paths.add(p1)
                if p2:
                    changed_active.add(p2)
                    if code_ch == "R" and p1:
                        renamed_pairs.append({"from": p1, "to": p2})
            elif code_ch == "D":
                if p1:
                    deleted_paths.add(p1)
            else:
                if p1:
                    changed_active.add(p1)
    else:
        # Working tree + staged + untracked new files (`git status` always emits paths relative to `toplevel`)
        res = run_git(["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."], root)
        if res.returncode != 0:
            return {"ok": False, "error": f"`git status` failed: {res.stderr.strip()}"}
        toks = res.stdout.split("\0")
        idx = 0
        while idx < len(toks):
            rec = toks[idx]
            idx += 1
            if len(rec) < 4:
                continue
            xy = rec[:2]
            p1 = _to_root_rel(rec[3:])
            if ("R" in xy or "C" in xy) and idx < len(toks):
                old_p = _to_root_rel(toks[idx])
                idx += 1
                if "R" in xy and old_p and not (root / old_p).exists():
                    deleted_paths.add(old_p)
                if p1:
                    changed_active.add(p1)
                    if "R" in xy and old_p:
                        renamed_pairs.append({"from": old_p, "to": p1})
            elif "D" in xy and p1 and not (root / p1).exists():
                deleted_paths.add(p1)
            elif p1:
                changed_active.add(p1)

    eligible_changed: List[str] = []
    for p in sorted(changed_active):
        if path_skip_reason(p, ignores) is not None:
            continue
        full = root / p
        try:
            lst = os.lstat(full)
        except OSError:
            lst = None
        if lst is not None:
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
                continue
            if lst.st_size > max_bytes:
                continue
            try:
                with full.open("rb") as fh:
                    head = fh.read(8192)
            except OSError:
                continue
            if is_probably_binary_bytes(head):
                continue
        eligible_changed.append(p)

    parsed_map = cached_parsed if cached_parsed is not None else parse_entries_map(entries)
    indexed_rows: List[Tuple[int, str, str]] = []
    high_risk: List[Dict[str, Any]] = []
    unindexed_changed: List[str] = []
    for p in eligible_changed:
        line = entries.get(p)
        if line is None:
            unindexed_changed.append(p)
            continue
        pe = parsed_map.get(p)
        imp = pe.imp if pe is not None else 5
        indexed_rows.append((imp, p, line))
        if pe is not None and pe.s not in ("-", ""):
            high_risk.append({"path": p, "importance": imp, "s": pe.s})

    indexed_rows.sort(key=lambda row: (-row[0], row[1]))
    high_risk.sort(key=lambda item: (-int(item["importance"]), str(item["path"])))
    deleted_indexed = [entries[p] for p in sorted(deleted_paths) if p in entries]

    if cached_deps is not None and cached_dependents is not None:
        deps_adj, dependents = cached_deps, cached_dependents
    else:
        deps_adj, dependents = build_fras_graph(entries, parsed_entries=parsed_map)
    seed_paths = [p for _, p, _ in indexed_rows] + [p for p in sorted(deleted_paths) if p in entries]
    exclude_seeds = set(eligible_changed) | deleted_paths
    neighbor_paths = [nb for nb in bfs_neighbors(dependents, seed_paths, max(1, depth)) if nb not in exclude_seeds]
    neighbor_lines = [entries[nb] for nb in sorted(neighbor_paths) if nb in entries]

    dep_paths = [
        dp
        for dp in bfs_neighbors(deps_adj, [p for _, p, _ in indexed_rows], 1)
        if dp not in exclude_seeds and dp in entries
    ]
    dep_constraints: List[Dict[str, Any]] = []
    for dp in dep_paths:
        dpe = parsed_map.get(dp)
        if dpe is not None and dpe.s not in ("-", ""):
            dep_constraints.append({"path": dp, "importance": dpe.imp, "s": dpe.s})
    dep_constraints.sort(key=lambda item: (-int(item["importance"]), str(item["path"])))

    hub_secs = _hubs.lookup_hub_sections(
        aoci_dir,
        [p for _, p, _ in indexed_rows],
        preloaded_records=cached_hub_records,
    )

    brief: Dict[str, Any] = {
        "ok": True,
        "root": str(root),
        "scope": rev or ("--staged" if staged else "working-tree"),
        "global_invariants": invariants,
        "renames": renamed_pairs,
        "changed_entries": [ln for _, _, ln in indexed_rows],
        "high_risk_constraints": high_risk,
        "dependency_constraints": dep_constraints,
        "unindexed_changed": unindexed_changed,
        "deleted_entries": deleted_indexed,
        "affected_neighbors": neighbor_lines,
    }
    if hub_secs:
        brief["hub_sections"] = hub_secs
    if with_tests:
        brief["impacted_tests"] = _test_impact.find_impacted_tests(
            root,
            entries,
            dependents,
            seed_paths,
            max_depth=max(3, int(depth)),
            parsed_entries=parsed_map,
            preloaded_outline_files=preloaded_outline_files,
            live_imports=live_test_imports,
        )
    return brief


def cmd_diff(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    with_tests = bool(getattr(args, "with_tests", False))
    brief = compute_diff_briefing(
        root,
        rev=args.rev,
        staged=args.staged,
        depth=args.depth,
        max_bytes=args.max_bytes,
        with_tests=with_tests,
        live_test_imports=bool(getattr(args, "live_test_imports", False)),
    )
    if not brief.get("ok"):
        print(f"ERROR: {brief.get('error')}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(brief, indent=2, ensure_ascii=False))
        return 0

    print(f"# AOCI Diff Impact Briefing ({brief['scope']})")
    print(f"# Global-Invariants: {brief['global_invariants']}")
    if brief["high_risk_constraints"]:
        print(f"\n## High-Risk S: Constraints on Touched Files ({len(brief['high_risk_constraints'])})")
        for item in brief["high_risk_constraints"]:
            print(f"  ! {item['path']} [I={item['importance']}]: {item['s']}")
    if brief.get("dependency_constraints"):
        print(f"\n## Downstream R: Dependency S: Constraints ({len(brief['dependency_constraints'])})")
        for item in brief["dependency_constraints"]:
            print(f"  * {item['path']} [I={item['importance']}]: {item['s']}")
    for ln in brief["changed_entries"]:
        print(ln)
    if brief.get("hub_sections"):
        print(f"\n## Hub Sections ({len(brief['hub_sections'])})")
        for sec in brief["hub_sections"]:
            print(sec["entry"])
    if brief["unindexed_changed"]:
        print(f"\n## Unindexed Changed/New Files ({len(brief['unindexed_changed'])})")
        for p in brief["unindexed_changed"]:
            print(f"  + {p}")
    if brief["deleted_entries"]:
        print(f"\n## Deleted Indexed Entries ({len(brief['deleted_entries'])})")
        for ln in brief["deleted_entries"]:
            print(f"  - {ln}")
    print(f"\n## Affected Upstream Neighbors (depth={args.depth}, count={len(brief['affected_neighbors'])})")
    for ln in brief["affected_neighbors"]:
        print(ln)
    if with_tests:
        _print_impacted_tests(brief.get("impacted_tests", []))
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        msg = f"`{code_path}` not found. Run `init` first."
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    entries, _ = parse_code_volume(code_path)
    parsed_map = parse_entries_map(entries)
    deps_adj, dependents_adj = build_fras_graph(entries, parsed_entries=parsed_map)
    invariants = parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-"
    domains = parse_declared_domains(aoci_dir)
    glossary = parse_glossary(aoci_dir)

    diff_brief: Optional[Dict[str, Any]] = None
    if not args.no_diff:
        cand_diff = compute_diff_briefing(
            root,
            depth=1,
            max_bytes=args.max_bytes,
            cached_entries=entries,
            cached_deps=deps_adj,
            cached_dependents=dependents_adj,
            cached_invariants=invariants,
            cached_parsed=parsed_map,
        )
        if cand_diff.get("ok"):
            diff_brief = cand_diff

    if entries:
        bm25 = (
            BM25Index(entries, parsed_entries=parsed_map)
            if BM25Index is _graph.BM25Index
            else BM25Index(entries)
        )
    else:
        bm25 = None
    pack_obj = _pack.build_task_pack(
        root,
        entries,
        deps_adj,
        dependents_adj,
        invariants,
        domains,
        glossary,
        task=args.task,
        diff_brief=diff_brief,
        depth=args.depth,
        budget=args.budget,
        top_seeds=args.top_seeds,
        bm25_index=bm25,
        parsed_entries=parsed_map,
        live_anchors=bool(getattr(args, "live_anchors", False)),
        with_tests=bool(getattr(args, "with_tests", False)),
        live_test_imports=bool(getattr(args, "live_test_imports", False)),
    )

    if args.json:
        print(json.dumps(pack_obj, indent=2, ensure_ascii=False))
        return 0

    trunc_note = f" | Omitted={pack_obj['omitted_count']}" if pack_obj.get("truncated") else ""
    budget_str = (
        f"~{pack_obj['estimated_tokens']}/{pack_obj['budget']} tokens{trunc_note}"
        if pack_obj["budget"] > 0
        else f"~{pack_obj['estimated_tokens']} tokens"
    )
    print(f"# AOCI Task Pack (depth={args.depth}, {budget_str})")
    print(f"# Task: {pack_obj['task']}")
    print(f"# Global-Invariants: {pack_obj['global_invariants']}")
    if pack_obj.get("glossary_expansions"):
        exp_txt = "; ".join(f"{e['term']} -> {', '.join(e['aliases'])}" for e in pack_obj["glossary_expansions"])
        print(f"# Glossary: {exp_txt}")
    if pack_obj["critical_constraints"]:
        print(f"\n## Critical S: Constraints ({len(pack_obj['critical_constraints'])})")
        for item in pack_obj["critical_constraints"]:
            print(f"  ! {item['path']} [I={item['importance']}]: {item['s']}")
    print(f"\n## Seeds ({len(pack_obj['seeds'])})")
    for seed in pack_obj["seeds"]:
        reasons_str = ",".join(seed["reasons"])
        print(f"{seed['entry']}  # [{reasons_str}, score={seed['score']}]")
    if pack_obj.get("hub_sections"):
        print(f"\n## Hub Sections ({len(pack_obj['hub_sections'])})")
        for sec in pack_obj["hub_sections"]:
            print(sec["entry"])
    print(f"\n## Dependencies / Read-Along ({len(pack_obj['dependencies'])})")
    for ln in pack_obj["dependencies"]:
        print(ln)
    print(f"\n## Dependents / Upstream Callers ({len(pack_obj['callers'])})")
    for ln in pack_obj["callers"]:
        print(ln)
    if pack_obj["unindexed_changed"]:
        print(f"\n## Unindexed Changed/New Files ({len(pack_obj['unindexed_changed'])})")
        for p in pack_obj["unindexed_changed"]:
            print(f"  + {p}")
    if pack_obj["open_next"]:
        print(f"\n## Open Next ({len(pack_obj['open_next'])})")
        for item in pack_obj["open_next"]:
            syms = f" -> {', '.join(item['symbols'])}" if item.get("symbols") else ""
            print(f"  - {item['path']} ({item['kind']}: {', '.join(item['reasons'])}){syms}")
    if pack_obj.get("impacted_tests"):
        _print_impacted_tests(pack_obj["impacted_tests"])
    if pack_obj.get("truncated") and pack_obj.get("omitted_paths"):
        print(f"\n# Omitted under --budget ({pack_obj['omitted_count']}): {', '.join(pack_obj['omitted_paths'])}")
    return 0


def build_static_import_graph(
    root: Path,
    discovered_files: Sequence[str],
    fps: Optional[Dict[str, Dict[str, Any]]] = None,
    aoci_dir: Optional[Path] = None,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    return _parsers.build_static_import_graph(
        root,
        discovered_files,
        fps=fps,
        aoci_dir=aoci_dir,
        outline_fn=extract_file_outline,
    )


def _infer_layer_and_domain(rel: str, declared_domains: Dict[str, str]) -> Tuple[str, str]:
    low = rel.lower()
    parts = low.split("/")
    bname = parts[-1]

    layer = "C"
    if (
        "test" in parts
        or "tests" in parts
        or "__tests__" in parts
        or bname.startswith("test_")
        or any(k in bname for k in (".test.", ".spec.", "_test."))
    ):
        layer = "T"
    elif bname.endswith((".md", ".rst", ".mdx")) or "docs" in parts:
        layer = "O"
    elif bname in {
        "package.json", "pyproject.toml", "cargo.toml", "go.mod", "dockerfile", "makefile",
    } or ".github" in parts:
        layer = "R"
    elif bname.endswith((".vue", ".svelte", ".css", ".scss")) or any(
        p in parts for p in ("components", "pages", "views", "ui")
    ):
        layer = "U"
    elif any(p in parts for p in ("middleware", "middlewares", "guards", "interceptors")):
        layer = "M"
    elif any(p in parts for p in ("db", "dao", "repo", "repos", "repositories", "migrations", "models", "prisma")) or bname.endswith(
        (".sql", ".prisma")
    ):
        layer = "P"
    elif any(p in parts for p in ("clients", "adapters", "sdk", "providers", "integrations")):
        layer = "I"
    elif any(p in parts for p in ("routes", "controllers", "handlers", "cli", "cmd")) or bname.startswith(
        ("main.", "index.", "app.", "server.")
    ):
        layer = "E"
    elif any(p in parts for p in ("services", "usecases", "workflows", "orchestrators")):
        layer = "A"
    elif any(p in parts for p in ("domain", "entities", "aggregates")):
        layer = "D"
    elif any(p in parts for p in ("engine", "compiler", "parser", "indexer")):
        layer = "K"

    domain = "X" if "X" in declared_domains or not declared_domains else next(iter(sorted(declared_domains)))
    if layer in ("R", "O") and ("G" in declared_domains or not declared_domains):
        domain = "G"
    for code, label in declared_domains.items():
        if code in ("G", "X"):
            continue
        lbl_low = label.lower()
        if lbl_low and any(lbl_low in seg for seg in parts[:-1]):
            domain = code
            break
    return layer, domain


def scaffold_entries(
    root: Path,
    target_paths: Sequence[str],
    max_bytes: int = DEFAULT_MAX_BYTES,
    precomputed_fps: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """
    Generate FRAS entry skeletons with auto-computed `[LDIS]`, resolved internal `R:` dependencies,
    and repo-wide `A: sym<-caller` edges. Leaves `<TODO:read-source>` in `F:` so `upsert` enforces
    reading the actual source file before merge (Rule #4).
    """
    aoci_dir = root / AOCI_DIR_NAME
    git_info = detect_git_info(root)
    ignores = load_ignore_matcher(root, git_info)
    declared_domains = parse_declared_domains(aoci_dir)
    if precomputed_fps is not None:
        fps = precomputed_fps
    else:
        base_files = load_baseline(aoci_dir).get("files", {}) if (aoci_dir / BASELINE_FILE).is_file() else {}
        fps, _ = discover_and_fingerprint(
            root, git_info, ignores, base_files=base_files, fast_mtime=True, max_bytes=max_bytes, save_scan_cache=True
        )

    all_discovered = sorted(fps.keys())
    static_deps, static_callers, outlines = build_static_import_graph(
        root,
        all_discovered,
        fps=fps,
        aoci_dir=aoci_dir if aoci_dir.is_dir() else None,
    )

    lines_out: List[str] = []
    for raw_t in target_paths:
        rel = normalize_relpath(raw_t)
        if rel not in fps:
            continue
        fp = fps[rel]
        out = outlines.get(rel) or extract_file_outline(root / rel, rel)
        layer, domain = _infer_layer_and_domain(rel, declared_domains)
        in_deg = len(static_callers.get(rel, ()))
        if layer == "T":
            imp = 2
        elif layer in ("R", "O"):
            imp = 4
        else:
            imp = min(8, 4 + (1 if in_deg >= 1 else 0) + (1 if in_deg >= 3 else 0) + (2 if in_deg >= 6 else 0))

        r_deps = static_deps.get(rel, [])[:6]
        r_str = ",".join(r_deps) if r_deps else "-"

        syms = _clean_symbol_names(out.get("symbols", ()), limit=4)
        callers = static_callers.get(rel, [])[:5]
        if syms and callers:
            a_str = f"{'/'.join(syms)}<-{','.join(callers)}"
        elif syms:
            a_str = "/".join(syms)
        elif callers:
            a_str = f"module<-{','.join(callers)}"
        else:
            a_str = "-"

        s_str = "<TODO:document-hub-invariants>" if imp >= 8 else "-"
        lines_out.append(
            f"{rel}[{layer}{domain}{imp}{fp['size_tier']}]: F:<TODO:read-source> | R:{r_str} | A:{a_str} | S:{s_str}"
        )
    return lines_out


def cmd_scaffold(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    git_info = detect_git_info(root)
    targets: List[str] = [normalize_relpath(p) for p in (args.paths or [])]
    precomputed_fps: Optional[Dict[str, Dict[str, Any]]] = None
    batch_plan_meta: Optional[Dict[str, Any]] = None

    if args.unindexed or args.batch:
        drift = compute_drift(
            root, git_info, fast_mtime=True, max_bytes=args.max_bytes, allow_heal=False, save_scan_cache=True
        )
        precomputed_fps = drift.get("_fingerprints")
        pending, _, _ = _build_pending_items(drift)
        if args.batch:
            batches, batch_plan_meta = resolve_stable_batches(
                root / AOCI_DIR_NAME,
                pending,
                args.batch_size,
                args.max_batch_lines,
                persist=True,
                reset=bool(getattr(args, "reset_batches", False)),
            )
            if not batches:
                if args.json:
                    print(
                        json.dumps(
                            {
                                "count": 0,
                                "batch": args.batch,
                                "status": "all-completed",
                                "remaining_batches": [],
                                "batch_plan": batch_plan_meta,
                                "output": args.output,
                                "entries": [],
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                else:
                    print("# All pending files are already indexed; no remaining batches to scaffold.")
                return 0
            if args.batch < 1 or args.batch > len(batches):
                print(f"ERROR: Batch {args.batch} out of range (1..{len(batches)}).", file=sys.stderr)
                return 2
            selected = batches[args.batch - 1]
            if not selected:
                rem_list = (batch_plan_meta or {}).get("remaining_batches", [])
                if args.json:
                    print(
                        json.dumps(
                            {
                                "count": 0,
                                "batch": args.batch,
                                "status": "completed",
                                "remaining_batches": rem_list,
                                "batch_plan": batch_plan_meta,
                                "output": args.output,
                                "entries": [],
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                else:
                    rem_str = ", ".join(str(b) for b in rem_list) or "none"
                    print(f"# Batch {args.batch}/{len(batches)} is already completed (remaining batches: {rem_str}).")
                return 0
            targets.extend(it["path"] for it in selected)
        else:
            targets.extend(it["path"] for it in pending)

    targets = list(dict.fromkeys(targets))
    if not targets:
        print("ERROR: Specify file paths, `--unindexed`, or `--batch N`.", file=sys.stderr)
        return 2

    drafts = scaffold_entries(root, targets, max_bytes=args.max_bytes, precomputed_fps=precomputed_fps)
    out_path_str: Optional[str] = None
    if args.output:
        out_path = _resolve_cli_file_path(root, args.output, must_exist=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# AOCI Scaffold Draft — Read each source file and replace <TODO:...> before `upsert`!\n"
        )
        write_text_lf(out_path, header + "\n".join(drafts) + ("\n" if drafts else ""))
        out_path_str = str(out_path)

    if args.json:
        payload: Dict[str, Any] = {"count": len(drafts), "output": args.output, "entries": drafts}
        if args.batch:
            payload["batch"] = args.batch
        if batch_plan_meta is not None:
            payload["batch_plan"] = batch_plan_meta
        if out_path_str is not None:
            payload["output_path"] = out_path_str
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"# Scaffolded {len(drafts)} entry skeleton(s) (replace <TODO:...> after reading source files):")
        for ln in drafts:
            print(ln)
    return 0


def audit_architecture(
    root: Path,
    git_info: Optional[Dict[str, Any]] = None,
    *,
    check_static: bool = True,
    check_cycles: bool = True,
    check_layers: bool = True,
    check_co_change: bool = False,
    co_change_commits: int = 200,
    min_co_changes: int = 3,
    min_co_confidence: float = 0.6,
    exclude_tests: bool = False,
    forbid_spec: Optional[str] = None,
    use_default_rules: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cached_entries: Optional[Dict[str, str]] = None,
    cached_deps: Optional[Dict[str, Set[str]]] = None,
    cached_dependents: Optional[Dict[str, Set[str]]] = None,
    cached_parsed: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Cross-check `.aoci/aoci.code.txt` against static imports, strongly connected dependency cycles,
    architectural layer redlines, and optional Git co-change coupling analysis.
    """
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    if cached_entries is not None:
        entries = cached_entries
    else:
        entries, _ = parse_code_volume(code_path)

    parsed_map = cached_parsed if cached_parsed is not None else parse_entries_map(entries)
    if cached_deps is not None and (cached_dependents is not None or not check_co_change):
        fras_deps = cached_deps
        fras_dependents = cached_dependents if cached_dependents is not None else {}
    else:
        fras_deps, fras_dependents = build_fras_graph(entries, parsed_entries=parsed_map)

    ginfo: Optional[Dict[str, Any]] = git_info
    missing_static_deps: List[Dict[str, str]] = []
    phantom_fras_deps: List[Dict[str, str]] = []
    if check_static:
        if ginfo is None:
            ginfo = detect_git_info(root)
        ignores = load_ignore_matcher(root, ginfo)
        base_files = load_baseline(aoci_dir).get("files", {}) if (aoci_dir / BASELINE_FILE).is_file() else {}
        fps, _ = discover_and_fingerprint(
            root, ginfo, ignores, base_files=base_files, fast_mtime=True, max_bytes=max_bytes
        )
        discovered = sorted(fps.keys())
        static_deps, _, outlines = build_static_import_graph(root, discovered, fps=fps, aoci_dir=aoci_dir)
        go_imported_dirs_by_file, go_files_by_dir = resolve_go_package_imports(root, outlines, discovered)
        jvm_cs_resolved = _parsers.resolve_jvm_cs_package_imports(outlines, discovered)
        known = set(entries.keys())

        static_sets: Dict[str, Set[str]] = {k: set(v) for k, v in static_deps.items()}
        for rel in sorted(entries.keys()):
            s_deps = static_sets.get(rel, set())
            f_deps = fras_deps.get(rel, set())
            rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
            rel_eco = _static_ecosystem(rel)
            if rel.endswith(".go"):
                for imp_dir in sorted(go_imported_dirs_by_file.get(rel, ())):
                    if imp_dir == rel_dir:
                        continue
                    pkg_indexed = [f for f in go_files_by_dir.get(imp_dir, ()) if f in entries and f != rel]
                    if pkg_indexed and not (set(pkg_indexed) & f_deps):
                        missing_static_deps.append({"from": rel, "to": pkg_indexed[0], "code": "missing-static-dep"})
            elif rel_eco in ("jvm", "cs") and rel in jvm_cs_resolved:
                exact_hits, group_hits = jvm_cs_resolved[rel]
                reported_missing: Set[str] = set()
                for dep in sorted(set(exact_hits)):
                    if dep in entries and dep not in f_deps and dep not in reported_missing:
                        reported_missing.add(dep)
                        missing_static_deps.append({"from": rel, "to": dep, "code": "missing-static-dep"})
                for grp in group_hits:
                    grp_indexed = [f for f in grp if f in entries and f != rel]
                    if grp_indexed and not (set(grp_indexed) & f_deps):
                        cand_to = grp_indexed[0]
                        if cand_to not in reported_missing:
                            reported_missing.add(cand_to)
                            missing_static_deps.append({"from": rel, "to": cand_to, "code": "missing-static-dep"})
            else:
                for dep in sorted(s_deps):
                    if dep in entries and dep not in f_deps:
                        missing_static_deps.append({"from": rel, "to": dep, "code": "missing-static-dep"})
            if rel_eco is not None:
                pe = parsed_map.get(rel)
                if pe is not None:
                    for r_dep in extract_path_refs(pe.r, known):
                        if r_dep == rel or _static_ecosystem(r_dep) != rel_eco:
                            continue
                        if rel.endswith(".go") and r_dep.endswith(".go"):
                            r_dir = r_dep.rsplit("/", 1)[0] if "/" in r_dep else ""
                            if r_dir == rel_dir or r_dir in go_imported_dirs_by_file.get(rel, ()):
                                continue
                        elif rel_eco in ("jvm", "cs"):
                            r_dir = r_dep.rsplit("/", 1)[0] if "/" in r_dep else ""
                            if r_dir == rel_dir:
                                continue
                        if r_dep not in s_deps:
                            phantom_fras_deps.append(
                                {
                                    "from": rel,
                                    "to": r_dep,
                                    "code": "phantom-fras-dep",
                                    "severity": "info",
                                }
                            )

    cycles: List[Dict[str, Any]] = []
    if check_cycles:
        for comp in find_dependency_cycles(fras_deps):
            cycles.append({"nodes": comp, "size": len(comp)})

    forbidden: Dict[str, Set[str]] = {}
    layer_violations: List[Dict[str, str]] = []
    if check_layers:
        if use_default_rules:
            for src_l, dsts in DEFAULT_FORBIDDEN_LAYER_EDGES.items():
                forbidden.setdefault(src_l, set()).update(dsts)
        for src_l, dsts in _load_layer_rules_from_aoci(aoci_dir, manifest_spec=parse_manifest_field(aoci_dir, "#Layer-Rules:")).items():
            forbidden.setdefault(src_l, set()).update(dsts)
        if forbid_spec:
            for src_l, dsts in parse_layer_rules(forbid_spec).items():
                forbidden.setdefault(src_l, set()).update(dsts)
        layer_violations = check_layer_violations(entries, fras_deps, forbidden, parsed_entries=parsed_map)

    co_change_unlinked: Optional[List[Dict[str, Any]]] = None
    if check_co_change:
        if ginfo is None:
            ginfo = detect_git_info(root)
        co_change_unlinked = _cochange.find_cochange_unlinked_pairs(
            root,
            ginfo,
            entries,
            fras_deps,
            fras_dependents,
            run_git,
            max_commits=co_change_commits,
            min_co_changes=min_co_changes,
            min_confidence=min_co_confidence,
            parsed_entries=parsed_map,
            exclude_tests=exclude_tests,
        )

    ok = not (missing_static_deps or cycles or layer_violations)
    rules_used = {k: sorted(v) for k, v in sorted(forbidden.items()) if v}
    summary: Dict[str, Any] = {
        "indexed": len(entries),
        "missing_static_deps": len(missing_static_deps),
        "phantom_fras_deps": len(phantom_fras_deps),
        "cycles": len(cycles),
        "layer_violations": len(layer_violations),
    }
    res: Dict[str, Any] = {
        "ok": ok,
        "root": str(root),
        "summary": summary,
        "missing_static_deps": missing_static_deps,
        "phantom_fras_deps": phantom_fras_deps,
        "cycles": cycles,
        "layer_violations": layer_violations,
        "rules_used": rules_used,
    }
    if co_change_unlinked is not None:
        summary["co_change_unlinked"] = len(co_change_unlinked)
        res["co_change_unlinked"] = co_change_unlinked
    return res


def cmd_audit(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    check_co = bool(getattr(args, "co_change", False))
    res = audit_architecture(
        root,
        check_static=not getattr(args, "no_static", False),
        check_cycles=not getattr(args, "no_cycles", False),
        check_layers=not getattr(args, "no_layers", False),
        check_co_change=check_co,
        co_change_commits=int(getattr(args, "co_change_commits", 200)),
        min_co_changes=int(getattr(args, "min_co_changes", 3)),
        min_co_confidence=float(getattr(args, "min_co_confidence", 0.6)),
        exclude_tests=bool(getattr(args, "exclude_tests", False)),
        forbid_spec=getattr(args, "forbid", None),
        use_default_rules=not getattr(args, "no_default_rules", False),
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
    )
    if res.get("error"):
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {res['error']}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        sm = res["summary"]
        status = "CLEAN" if res["ok"] else "ISSUES"
        co_note = f" | CoChangeUnlinked={sm['co_change_unlinked']}" if "co_change_unlinked" in sm else ""
        print(
            f"[AOCI Audit] {status} | Indexed={sm['indexed']} | "
            f"MissingStaticDeps={sm['missing_static_deps']} | PhantomFrasInfo={sm['phantom_fras_deps']} | "
            f"Cycles={sm['cycles']} | LayerViolations={sm['layer_violations']}{co_note}"
        )
        if res["missing_static_deps"]:
            print(f"\nMISSING STATIC DEPS IN FRAS R:/A: ({len(res['missing_static_deps'])}):")
            for item in res["missing_static_deps"][:40]:
                print(f"  ! {item['from']} -> {item['to']}")
        if res["layer_violations"]:
            print(f"\nLAYER VIOLATIONS ({len(res['layer_violations'])}):")
            for item in res["layer_violations"][:40]:
                print(f"  ! [{item['rule']}] {item['from']} -> {item['to']}")
        if res["cycles"]:
            print(f"\nDEPENDENCY CYCLES ({len(res['cycles'])}):")
            for cyc in res["cycles"][:20]:
                print(f"  * ({cyc['size']}) {' <-> '.join(cyc['nodes'][:10])}")
        if res["phantom_fras_deps"]:
            print(f"\nPHANTOM FRAS R: DEPS [info] ({len(res['phantom_fras_deps'])}):")
            for item in res["phantom_fras_deps"][:30]:
                print(f"  ? {item['from']} -> {item['to']}")
        if res.get("co_change_unlinked"):
            print(f"\nCO-CHANGE UNLINKED PAIRS ({len(res['co_change_unlinked'])}):")
            for item in res["co_change_unlinked"][:30]:
                hop_tag = " [2-hop]" if item.get("is_two_hop") else ""
                print(
                    f"  ~ {item['file_a']} <-> {item['file_b']} "
                    f"(co={item['co_changes']}, conf={item['confidence']} "
                    f"[{item.get('confidence_a_to_b', item['confidence'])}/{item.get('confidence_b_to_a', item['confidence'])}]){hop_tag}"
                )

    return 1 if (getattr(args, "strict", False) and not res["ok"]) else 0


def cmd_prepare_sync(args: argparse.Namespace) -> int:
    """Collect single-pass drift briefing, scaffolds, outlines, diffs, and recommended sync actions."""
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    as_json = bool(getattr(args, "json", False))
    if not (aoci_dir / CODE_FILE).exists():
        msg = f"`{aoci_dir / CODE_FILE}` not found. Run `init` first."
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    git_info = detect_git_info(root)
    drift = compute_drift(
        root,
        git_info,
        fast_mtime=not bool(getattr(args, "no_fast", False)),
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
        allow_heal=False,
        save_scan_cache=False,
    )
    brief = _sync_prep.prepare_sync_briefing(
        root,
        git_info,
        drift,
        scaffold_fn=scaffold_entries,
        run_git_fn=run_git,
        max_diff_lines=int(getattr(args, "max_diff_lines", 40)),
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
    )

    if as_json:
        print(json.dumps(brief, indent=2, ensure_ascii=False))
    else:
        c = brief["counts"]
        status = "DRIFT" if brief["has_drift"] else "CLEAN"
        print(
            f"[AOCI Prepare-Sync] {status} | PoolRestorable={c['restorable_from_pool']} | "
            f"Renames={c['renamed']}+{c['similar_renames']} | "
            f"ImplOnly={c['impl_only_modified']} | ContractReview={c['contract_or_unknown_modified']} | "
            f"Added={c['added']} | Deleted={c['deleted']}"
        )
        for act in brief["recommended_actions"]:
            print(f"  -> {act}")
    return 0


def _capture_internal_cli(argv: Sequence[str]) -> Tuple[int, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            rc = main(list(argv))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                rc = exc.code
            elif exc.code is None:
                rc = 0
            else:
                rc = 1
                err_buf.write(f"\n{exc.code}")
    out_txt = out_buf.getvalue().strip()
    err_txt = err_buf.getvalue().strip()
    return rc, (out_txt if out_txt else err_txt)


def cmd_apply_sync(args: argparse.Namespace) -> int:
    """Execute ordered sync workflow (`restore -> migrate-renames -> ack -> upsert -> verify`) in one pass."""
    root = resolve_root(args.root)
    res = _apply_sync.run_apply_sync(
        root,
        _capture_internal_cli,
        restore=bool(getattr(args, "restore", False)),
        allow_sha_fallback=bool(getattr(args, "allow_sha_fallback", False)),
        migrate_renames=bool(getattr(args, "migrate_renames", False)),
        exact_only=bool(getattr(args, "exact_only", False)),
        ack_paths=getattr(args, "ack_path", None) or [],
        ack_impl_only=bool(getattr(args, "ack_impl_only", False)),
        reviewed=bool(getattr(args, "reviewed", False)),
        entries=getattr(args, "entry", None) or [],
        entries_file=getattr(args, "entries_file", None),
        prune_only=bool(getattr(args, "prune_only", False)),
        no_prune=bool(getattr(args, "no_prune", False)),
        strict_size=bool(getattr(args, "strict_size", False)),
        drop_malformed=bool(getattr(args, "drop_malformed", False)),
        verify=not bool(getattr(args, "no_verify", False)),
        full_verify=bool(getattr(args, "full_verify", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        status = "DRY-RUN OK" if res["dry_run"] and res["ok"] else ("OK" if res["ok"] else f"FAILED ({res['failed_step']})")
        print(f"[AOCI Apply-Sync] {status} | Steps={len(res['steps'])}")
        for st in res["steps"]:
            st_flag = "OK" if st["ok"] else f"ERR(rc={st['rc']})"
            print(f"  * {st['step']}: {st_flag}")
    return 0 if res["ok"] else 1


def cmd_symbol(args: argparse.Namespace) -> int:
    """Look up a symbol across `.aoci/aoci.hubs.txt`, outline cache (or `--live`), and FRAS `A:` exports."""
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    as_json = bool(getattr(args, "json", False))
    if not code_path.exists():
        msg = f"`{code_path}` not found. Run `init` first."
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    entries, _ = parse_code_volume(code_path)
    parsed_map = parse_entries_map(entries)
    deps_adj, dependents_adj = build_fras_graph(entries, parsed_entries=parsed_map)
    res = _symbols.lookup_symbol(
        root,
        entries,
        deps_adj,
        dependents_adj,
        args.name,
        exact=bool(getattr(args, "exact", False)),
        depth=int(getattr(args, "depth", 1)),
        limit=int(getattr(args, "limit", 20)),
        live=bool(getattr(args, "live", False)),
        parsed_entries=parsed_map,
    )
    if not res.get("ok"):
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {res.get('error', 'Symbol lookup failed.')}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(
            f"# AOCI Symbol Lookup: {res['query']} "
            f"(matches={res['total_matches']}, showing={len(res['matches'])})"
        )
        for m in res["matches"]:
            span_txt = f" [L{m['line_span'][0]}-L{m['line_span'][1]}]" if m.get("line_span") else ""
            print(f"\n* {m['symbol']} @ {m['path']}{span_txt} [src={','.join(m['sources'])}, imp={m['importance']}]")
            if m.get("signature"):
                print(f"  sig: {m['signature']}")
            if m.get("hub_f"):
                print(f"  hub-F: {m['hub_f']}")
            if m.get("hub_s"):
                print(f"  hub-S: {m['hub_s']}")
            if m.get("file_entry"):
                print(f"  fras: {m['file_entry']}")
            if m.get("dependencies"):
                print(f"  deps: {', '.join(m['dependencies'])}")
            if m.get("callers"):
                print(f"  callers: {', '.join(m['callers'])}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        msg = f"`{code_path}` not found. Run `init` first."
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    entries, _ = parse_code_volume(code_path)
    parsed_map = parse_entries_map(entries)
    deps_adj, dependents_adj = build_fras_graph(entries, parsed_entries=parsed_map)
    declared_domains = parse_declared_domains(aoci_dir)
    res = export_dependency_graph(
        entries,
        deps_adj,
        dependents_adj,
        declared_domains,
        fmt=args.format,
        level=args.level,
        group_by=args.group_by,
        direction=args.direction,
        layer=args.layer,
        domain=args.domain,
        min_importance=args.min_importance or 0,
        path_glob=args.path,
        focus=args.focus,
        depth=args.depth,
        parsed_entries=parsed_map,
    )

    if args.output:
        out_path = _resolve_cli_file_path(root, args.output, must_exist=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(out_path, res["content"])
        res["output_path"] = str(out_path)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.output:
        print(
            f"[AOCI Export] OK | Format={res['format']} | Level={res['level']} | "
            f"Nodes={res['nodes']} | Edges={res['edges']} -> {res['output_path']}"
        )
    else:
        print(res["content"], end="")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    git_info = detect_git_info(root)
    action = args.pool_action
    as_json = bool(getattr(args, "json", False))

    if action == "stats":
        res = pool_stats(root, git_info)
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(
                f"[AOCI Pool] Exists={res['exists']} | Path={res['pool_path']} | "
                f"Bytes={res['bytes']} | ByPathSha={res['by_path_sha_count']} | BySha={res['by_sha_count']}"
            )
        return 0

    if action == "export":
        out_path = _resolve_cli_file_path(root, args.output, must_exist=False)
        res = export_semantic_pool(root, git_info, out_path, include_current=not args.pool_only)
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(
                f"[AOCI Pool Export] OK | ByPathSha={res['exported_by_path_sha']} | "
                f"BySha={res['exported_by_sha']} | Bytes={res['bytes']} -> {res['output_path']}"
            )
        return 0

    if action == "import":
        in_path = _resolve_cli_file_path(root, args.input, must_exist=True)
        res = import_semantic_pool(root, git_info, in_path, dry_run=bool(args.dry_run))
        if not res.get("ok"):
            if as_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(f"ERROR: {res.get('error')}", file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            mode = " DRY-RUN" if res["dry_run"] else ""
            print(
                f"[AOCI Pool Import{mode}] OK | ByPathSha={res['imported_by_path_sha']} | "
                f"BySha={res['imported_by_sha']} | Rejected={res['rejected']}"
            )
        return 0

    return 2


def resolve_git_hooks_dir(root: Path, git_info: Dict[str, Any]) -> Optional[Path]:
    return _githooks.resolve_git_hooks_dir(root, git_info, run_git_fn=run_git)


def install_git_hooks(
    root: Path,
    git_info: Dict[str, Any],
    *,
    python_cmd: Optional[str] = None,
    with_pre_commit: bool = False,
    pre_commit_verify: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    return _githooks.install_git_hooks(
        root,
        git_info,
        python_cmd=python_cmd,
        with_pre_commit=with_pre_commit,
        pre_commit_verify=pre_commit_verify,
        dry_run=dry_run,
        script_path=Path(__file__).resolve(),
        run_git_fn=run_git,
    )


def uninstall_git_hooks(
    root: Path,
    git_info: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    return _githooks.uninstall_git_hooks(
        root,
        git_info,
        dry_run=dry_run,
        run_git_fn=run_git,
    )


def cmd_install_git_hooks(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    git_info = detect_git_info(root)
    if getattr(args, "uninstall", False):
        res = uninstall_git_hooks(root, git_info, dry_run=bool(args.dry_run))
    else:
        res = install_git_hooks(
            root,
            git_info,
            python_cmd=getattr(args, "python_cmd", None),
            with_pre_commit=bool(getattr(args, "with_pre_commit", False)),
            pre_commit_verify=bool(getattr(args, "pre_commit_verify", False)),
            dry_run=bool(args.dry_run),
        )
    if not res.get("ok"):
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {res.get('error')}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        mode = " DRY-RUN" if res["dry_run"] else ""
        verb = "Uninstall" if res["action"] == "uninstall" else "Install"
        print(f"[AOCI Git-Hooks {verb}{mode}] OK | Dir={res['hooks_dir']}")
        for hn, act in sorted(res["hooks"].items()):
            print(f"  * {hn}: {act}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AOCI Fast Codebase Index Manager")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {AOCI_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize `.aoci/` index repository and enforce `.git/info/exclude`")
    p_init.add_argument("--root", help="Target codebase root (defaults to Git toplevel or CWD)")
    p_init.add_argument("--project", help="Project name for `aoci.txt`")
    p_init.add_argument("--locale", default="en-US", help="Locale label (default: en-US)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing template files")
    p_init.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Max file size in bytes (default: 524288)")
    p_init.add_argument("--json", action="store_true", help="Output JSON")
    p_init.set_defaults(func=cmd_init)

    for alias in ("scan", "status"):
        p_scan = sub.add_parser(alias, help="Detect codebase drift and generate line-balanced batching plan")
        p_scan.add_argument("--root", help="Target codebase root")
        p_scan.add_argument(
            "--batches",
            "--batch-size",
            dest="batches",
            type=int,
            default=0,
            help="Split pending files into batches of up to N files per batch (`--batch-size N`)",
        )
        p_scan.add_argument(
            "--max-batch-lines",
            type=int,
            default=DEFAULT_MAX_BATCH_LINES,
            help=f"Maximum line budget per batch (default: {DEFAULT_MAX_BATCH_LINES})",
        )
        p_scan.add_argument(
            "--reset-batches",
            action="store_true",
            help="Rebuild `.aoci/.cache/batch_plan.json` from current pending files",
        )
        p_scan.add_argument("--fast", action="store_true", help="Skip disk reads when mtime & byte size match baseline")
        p_scan.add_argument(
            "--git-fast",
            action="store_true",
            help="Use cached Git index + `git status` incremental discovery (auto-enabled on >=2000-file repos with --fast)",
        )
        p_scan.add_argument(
            "--auto-restore",
            action="store_true",
            help="Automatically restore matching `(path, sha256)` entries from semantic cache pool before scanning",
        )
        p_scan.add_argument("--check-only", action="store_true", help="Do not auto-repair `.git/info/exclude`")
        p_scan.add_argument("--fail-on-drift", action="store_true", help="Exit with code 1 if any drift exists")
        p_scan.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        p_scan.add_argument("--json", action="store_true", help="Output JSON")
        p_scan.set_defaults(func=cmd_scan)

    p_up = sub.add_parser("upsert", help="Validate & merge FRAS entries into `aoci.code.txt` and `baseline.json`")
    p_up.add_argument("--root", help="Target codebase root")
    p_up.add_argument("--entries-file", help="Path to a text file containing object-fras-v2 lines")
    p_up.add_argument("--entry", action="append", help="Single object-fras-v2 line (repeatable)")
    p_up.add_argument("--stdin", action="store_true", help="Read object-fras-v2 lines from stdin")
    p_up.add_argument("--prune-only", action="store_true", help="Only prune deleted/ignored files from index")
    p_up.add_argument("--no-prune", action="store_true", help="Do not automatically prune deleted files")
    p_up.add_argument("--strict-size", action="store_true", help="Fail instead of auto-fixing stale Size tier")
    p_up.add_argument("--dry-run", action="store_true", help="Validate entries without writing to disk")
    p_up.add_argument(
        "--drop-malformed",
        action="store_true",
        help="Allow the rewrite to delete pre-existing malformed lines in aoci.code.txt",
    )
    p_up.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_up.add_argument("--json", action="store_true", help="Output JSON")
    p_up.set_defaults(func=cmd_upsert)

    p_ack = sub.add_parser(
        "ack",
        help="Re-baseline modified files whose existing FRAS entries are still accurate (auto Size tier refresh)",
    )
    p_ack.add_argument("paths", nargs="*", help="Relative file paths to acknowledge")
    p_ack.add_argument("--root", help="Target codebase root")
    p_ack.add_argument("--all-modified", action="store_true", help="Acknowledge every currently modified file (requires --reviewed)")
    p_ack.add_argument(
        "--impl-only",
        action="store_true",
        help="Acknowledge only modified files whose interface signature hash is unchanged (requires --reviewed unless --dry-run)",
    )
    p_ack.add_argument(
        "--reviewed",
        action="store_true",
        help="Confirm every file matched by --all-modified was read and its FRAS entry verified accurate",
    )
    p_ack.add_argument("--dry-run", action="store_true", help="Preview without writing")
    p_ack.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_ack.add_argument("--json", action="store_true", help="Output JSON")
    p_ack.set_defaults(func=cmd_ack)

    p_lint = sub.add_parser("lint", help="FRAS quality checks (vague F, token budget, hub S, dangling refs, dups)")
    p_lint.add_argument("--root", help="Target codebase root")
    p_lint.add_argument("--code", help="Comma-separated finding codes to keep (e.g. ref-dangling,F-vague)")
    p_lint.add_argument("--limit", type=int, default=100, help="Max findings to print (default: 100)")
    p_lint.add_argument("--strict", action="store_true", help="Exit 1 when any finding exists")
    p_lint.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_lint.add_argument("--json", action="store_true", help="Output JSON")
    p_lint.set_defaults(func=cmd_lint)

    p_mig = sub.add_parser(
        "migrate-renames",
        help="Migrate exact-content (and high-similarity moved+edited) renamed files and rewrite R:/A: refs",
    )
    p_mig.add_argument("--root", help="Target codebase root")
    p_mig.add_argument("--exact-only", action="store_true", help="Only migrate byte-identical renames, skip moved+edited matches")
    p_mig.add_argument("--no-rewrite-refs", action="store_true", help="Do not rewrite path mentions in other R:/A: fields")
    p_mig.add_argument(
        "--drop-malformed",
        action="store_true",
        help="Allow the rewrite to delete pre-existing malformed lines in aoci.code.txt",
    )
    p_mig.add_argument("--dry-run", action="store_true", help="Preview migrations without writing to disk")
    p_mig.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_mig.add_argument("--json", action="store_true", help="Output JSON")
    p_mig.set_defaults(func=cmd_migrate_renames)

    p_ver = sub.add_parser("verify", help="Verify Git exclusion, entry format, and zero SHA-256 drift")
    p_ver.add_argument("--root", help="Target codebase root")
    p_ver.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_ver.add_argument(
        "--fast",
        action="store_true",
        help="Trust (mtime,size) for unchanged non-racy files (quick pre-check; full hashing is the default)",
    )
    p_ver.add_argument("--git-fast", action="store_true", help="Use cached Git index + `git status` incremental scan")
    p_ver.add_argument("--json", action="store_true", help="Output JSON")
    p_ver.set_defaults(func=cmd_verify)

    p_q = sub.add_parser("query", help="Filter or BM25-search `.aoci/aoci.code.txt` entries or trace impact neighbors")
    p_q.add_argument("--root", help="Target codebase root")
    p_q.add_argument("--layer", help="Filter by Layer code(s), e.g. `M` or `E,A,M`")
    p_q.add_argument("--domain", help="Filter by Domain code(s), e.g. `O` or `A,O`")
    p_q.add_argument("--min-importance", type=int, help="Filter entries with Importance >= N")
    p_q.add_argument("--path", help="Glob pattern on relative path (e.g. `src/auth/*`)")
    p_q.add_argument("--keyword", help="Case-insensitive exact substring filter")
    p_q.add_argument("--search", help="Ranked multi-term BM25 + Importance search across path/symbols/F/R/A/S")
    p_q.add_argument("--top", type=int, default=None, help="Maximum entries to return (default: 20 with --search; 0 = unlimited)")
    p_q.add_argument("--impact", help="Target file plus every entry that depends on it (via R: or A: callers)")
    p_q.add_argument("--tests-for", help="Target file to find impacted T-layer test files (FRAS + outline + naming)")
    p_q.add_argument(
        "--live-test-imports",
        action="store_true",
        help="Read uncached T-layer test imports in memory when outline_cache.json is missing/stale",
    )
    p_q.add_argument("--depth", type=int, default=None, help="Transitive hops (default: 1 for --impact, 3 for --tests-for)")
    p_q.add_argument("--json", action="store_true", help="Output JSON")
    p_q.set_defaults(func=cmd_query)

    p_ctx = sub.add_parser("context", help="Expand architectural subgraph (targets + R: deps + upstream callers)")
    p_ctx.add_argument("paths", nargs="+", help="One or more relative file paths")
    p_ctx.add_argument("--root", help="Target codebase root")
    p_ctx.add_argument("--depth", type=int, default=1, help="Graph expansion hops (default: 1)")
    p_ctx.add_argument("--budget", type=int, default=0, help="Optional token budget ceiling (0 = unlimited)")
    p_ctx.add_argument("--with-tests", action="store_true", help="Attach impacted T-layer test files")
    p_ctx.add_argument(
        "--live-test-imports",
        action="store_true",
        help="Read uncached T-layer test imports in memory when outline_cache.json is missing/stale",
    )
    p_ctx.add_argument("--json", action="store_true", help="Output JSON")
    p_ctx.set_defaults(func=cmd_context)

    p_ov = sub.add_parser("overview", help="Print compact global digest for fast orientation")
    p_ov.add_argument("--root", help="Target codebase root")
    p_ov.add_argument("--min-importance", type=int, default=7, help="Minimum Importance for core entries (default: 7)")
    p_ov.add_argument("--max-core", type=int, default=50, help="Max core entries to display (default: 50)")
    p_ov.add_argument("--json", action="store_true", help="Output JSON")
    p_ov.set_defaults(func=cmd_overview)

    p_out = sub.add_parser("outline", help="Extract static imports and top-level symbols to assist R:/A: authoring")
    p_out.add_argument("paths", nargs="+", help="One or more relative file paths")
    p_out.add_argument("--root", help="Target codebase root")
    p_out.add_argument("--max-items", type=int, default=30, help="Max imports/symbols per file (default: 30)")
    p_out.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_out.add_argument("--json", action="store_true", help="Output JSON")
    p_out.set_defaults(func=cmd_outline)

    p_shard = sub.add_parser(
        "shard",
        help="Split `aoci.code.txt` into per-directory `.aoci/shards/<dir>.code.txt` slices (auto-synced once enabled)",
    )
    p_shard.add_argument("--root", help="Target codebase root")
    p_shard.add_argument("--depth", type=int, default=1, help="Directory prefix depth for shard grouping (default: 1)")
    p_shard.add_argument("--clean", action="store_true", help="Disable sharding and remove `.aoci/shards/`")
    p_shard.add_argument("--json", action="store_true", help="Output JSON")
    p_shard.set_defaults(func=cmd_shard)

    p_res = sub.add_parser(
        "restore",
        help="Zero-token restore of FRAS entries from `<git-common-dir>/aoci-cache` after branch switches or worktrees",
    )
    p_res.add_argument("--root", help="Target codebase root")
    p_res.add_argument(
        "--allow-sha-fallback",
        action="store_true",
        help="Opt in to content-hash fallback for unambiguous moved files whose original path no longer exists",
    )
    p_res.add_argument(
        "--exact-path-only",
        action="store_true",
        help="Legacy alias; exact `(path, sha256)` restore is the default",
    )
    p_res.add_argument("--no-prune", action="store_true", help="Do not prune entries missing on the current branch")
    p_res.add_argument("--dry-run", action="store_true", help="Preview restorable entries without writing")
    p_res.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_res.add_argument("--json", action="store_true", help="Output JSON")
    p_res.set_defaults(func=cmd_restore)

    p_diff = sub.add_parser(
        "diff",
        help="Generate architectural impact briefing (FRAS + S: traps + affected callers) for Git diffs",
    )
    p_diff.add_argument("rev", nargs="?", default=None, help="Optional Git revision spec (e.g. `main...HEAD`, `HEAD~1`)")
    p_diff.add_argument("--root", help="Target codebase root")
    p_diff.add_argument("--staged", action="store_true", help="Inspect staged (`--cached`) diff")
    p_diff.add_argument("--depth", type=int, default=1, help="Upstream caller hops (default: 1)")
    p_diff.add_argument("--with-tests", action="store_true", help="Attach impacted T-layer test files")
    p_diff.add_argument(
        "--live-test-imports",
        action="store_true",
        help="Read uncached T-layer test imports in memory when outline_cache.json is missing/stale",
    )
    p_diff.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_diff.add_argument("--json", action="store_true", help="Output JSON")
    p_diff.set_defaults(func=cmd_diff)

    p_scaf = sub.add_parser(
        "scaffold",
        help="Draft FRAS entry skeletons with auto-resolved R: imports and repo-wide A: callers (<TODO> F:/S:)",
    )
    p_scaf.add_argument("paths", nargs="*", help="Target relative paths to scaffold")
    p_scaf.add_argument("--root", help="Target codebase root")
    p_scaf.add_argument("--unindexed", action="store_true", help="Scaffold all currently pending/unindexed files")
    p_scaf.add_argument("--batch", type=int, default=0, help="Scaffold 1-indexed batch N from pending files")
    p_scaf.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Batch size when used with --batch (inherits active batch_plan.json if omitted, else 20)",
    )
    p_scaf.add_argument("--max-batch-lines", type=int, default=None, help="Maximum line budget per batch (inherits active plan if omitted)")
    p_scaf.add_argument("--reset-batches", action="store_true", help="Rebuild `.aoci/.cache/batch_plan.json` before selecting `--batch`")
    p_scaf.add_argument("--output", help="Optional path (e.g. `.aoci/pending/batch_01.txt`) to write skeleton lines")
    p_scaf.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_scaf.add_argument("--json", action="store_true", help="Output JSON")
    p_scaf.set_defaults(func=cmd_scaffold)

    p_git = sub.add_parser("ensure-git-exclude", help="Idempotently enforce `.aoci/` in `.git/info/exclude`")
    p_git.add_argument("--root", help="Target codebase root")
    p_git.add_argument("--json", action="store_true", help="Output JSON")
    p_git.set_defaults(func=cmd_ensure_git_exclude)

    p_aud = sub.add_parser(
        "audit",
        help="Audit FRAS graph vs static imports, dependency cycles (Tarjan SCC), and forbidden layer edges",
    )
    p_aud.add_argument("--root", help="Target codebase root")
    p_aud.add_argument("--forbid", help="Extra layer redlines (e.g. `D!->E,U; P!->E`)")
    p_aud.add_argument("--no-default-rules", action="store_true", help="Disable built-in layer redline defaults")
    p_aud.add_argument("--no-static", action="store_true", help="Skip static import vs FRAS cross-check")
    p_aud.add_argument("--no-cycles", action="store_true", help="Skip dependency cycle detection")
    p_aud.add_argument("--no-layers", action="store_true", help="Skip layer redline validation")
    p_aud.add_argument("--co-change", action="store_true", help="Mine recent Git commits for unlinked co-changing pairs")
    p_aud.add_argument("--co-change-commits", type=int, default=200, help="Max recent commits to scan for --co-change (default: 200)")
    p_aud.add_argument("--min-co-changes", type=int, default=3, help="Minimum co-change commit count (default: 3)")
    p_aud.add_argument("--min-co-confidence", type=float, default=0.6, help="Minimum co-change confidence in [0,1] (default: 0.6)")
    p_aud.add_argument("--exclude-tests", action="store_true", help="Exclude T-layer test files from --co-change pairs")
    p_aud.add_argument("--strict", action="store_true", help="Exit 1 when any missing static dep, cycle, or layer violation exists")
    p_aud.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_aud.add_argument("--json", action="store_true", help="Output JSON")
    p_aud.set_defaults(func=cmd_audit)

    p_exp = sub.add_parser("export", help="Export FRAS dependency graph in Mermaid, Graphviz DOT, or standalone HTML format")
    p_exp.add_argument("--root", help="Target codebase root")
    p_exp.add_argument("--format", default="mermaid", choices=["mermaid", "dot", "html"], help="Output format (default: mermaid)")
    p_exp.add_argument("--level", default="file", choices=["file", "dir", "domain"], help="Aggregation level (default: file)")
    p_exp.add_argument("--group-by", default="domain", choices=["domain", "dir", "layer", "none"], help="Subgraph grouping for file level")
    p_exp.add_argument("--direction", default="LR", choices=["LR", "TD", "TB"], help="Graph layout direction (default: LR)")
    p_exp.add_argument("--layer", help="Filter nodes by Layer code(s)")
    p_exp.add_argument("--domain", help="Filter nodes by Domain code(s)")
    p_exp.add_argument("--min-importance", type=int, default=0, help="Filter nodes with Importance >= N")
    p_exp.add_argument("--path", help="Glob filter on relative file paths")
    p_exp.add_argument("--focus", help="Center subgraph on a target file and its neighbors")
    p_exp.add_argument("--depth", type=int, default=1, help="Neighborhood hops around `--focus` (default: 1)")
    p_exp.add_argument("--output", help="Optional output file path relative to `--root`")
    p_exp.add_argument("--json", action="store_true", help="Output JSON")
    p_exp.set_defaults(func=cmd_export)

    p_pool = sub.add_parser("pool", help="Inspect, export, or import `.pool.sqlite3` semantic cache entries")
    pool_sub = p_pool.add_subparsers(dest="pool_action", required=True)

    p_pool_stats = pool_sub.add_parser("stats", help="Show SQLite WAL semantic pool counts and path")
    p_pool_stats.add_argument("--root", help="Target codebase root")
    p_pool_stats.add_argument("--json", action="store_true", help="Output JSON")
    p_pool_stats.set_defaults(func=cmd_pool)

    p_pool_exp = pool_sub.add_parser("export", help="Export validated semantic pool entries to a portable JSON(.gz) bundle")
    p_pool_exp.add_argument("--root", help="Target codebase root")
    p_pool_exp.add_argument("--output", required=True, help="Output bundle path (use `.json.gz` for gzip)")
    p_pool_exp.add_argument("--pool-only", action="store_true", help="Export only rows already in the SQLite pool (skip current code.txt)")
    p_pool_exp.add_argument("--json", action="store_true", help="Output JSON")
    p_pool_exp.set_defaults(func=cmd_pool)

    p_pool_imp = pool_sub.add_parser("import", help="Validate and import entries from an `aoci-semantic-pool-v1` JSON(.gz) bundle")
    p_pool_imp.add_argument("--root", help="Target codebase root")
    p_pool_imp.add_argument("--input", required=True, help="Input bundle file path relative to `--root` or absolute")
    p_pool_imp.add_argument("--dry-run", action="store_true", help="Validate bundle without modifying the pool DB")
    p_pool_imp.add_argument("--json", action="store_true", help="Output JSON")
    p_pool_imp.set_defaults(func=cmd_pool)

    p_gh = sub.add_parser("install-git-hooks", help="Install or remove idempotent local Git hooks (`post-checkout`/`post-merge`/`post-rewrite` + optional `pre-commit`)")
    p_gh.add_argument("--root", help="Target codebase root")
    p_gh.add_argument("--python-cmd", help="Python interpreter command written into hook scripts")
    p_gh.add_argument("--with-pre-commit", action="store_true", help="Also manage a `pre-commit` guard rejecting staged `.aoci/` files")
    p_gh.add_argument("--pre-commit-verify", action="store_true", help="Run `verify --fast` inside `pre-commit` when `--with-pre-commit` is enabled")
    p_gh.add_argument("--uninstall", action="store_true", help="Strip AOCI managed blocks from Git hooks cleanly")
    p_gh.add_argument("--dry-run", action="store_true", help="Preview hook actions without writing")
    p_gh.add_argument("--json", action="store_true", help="Output JSON")
    p_gh.set_defaults(func=cmd_install_git_hooks)

    p_pack = sub.add_parser(
        "pack",
        help="Build a task-driven, token-budgeted context pack (seeds + R:/A: neighborhood + S: traps + open_next)",
    )
    p_pack.add_argument("--task", required=True, help="Task description or query to pack context for")
    p_pack.add_argument("--root", help="Target codebase root")
    p_pack.add_argument("--no-diff", action="store_true", help="Exclude working-tree Git diff signals")
    p_pack.add_argument("--depth", type=int, default=1, help="Graph expansion hops around seeds (default: 1)")
    p_pack.add_argument("--budget", type=int, default=2500, help="Token budget ceiling (default: 2500; 0 = unlimited)")
    p_pack.add_argument("--top-seeds", type=int, default=8, help="Maximum BM25 seed hits (default: 8)")
    p_pack.add_argument("--with-tests", action="store_true", help="Attach impacted T-layer test files for packed seeds")
    p_pack.add_argument(
        "--live-test-imports",
        action="store_true",
        help="Read uncached T-layer test imports in memory when outline_cache.json is missing/stale",
    )
    p_pack.add_argument("--live-anchors", action="store_true", help="Extract live symbol anchors in memory when outline cache misses")
    p_pack.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_pack.add_argument("--json", action="store_true", help="Output JSON")
    p_pack.set_defaults(func=cmd_pack)

    p_ps = sub.add_parser(
        "prepare-sync",
        help="Collect single-pass agent sync briefing (scaffolds, outlines, bounded diffs, neighbors, actions)",
    )
    p_ps.add_argument("--root", help="Target codebase root")
    p_ps.add_argument("--no-fast", action="store_true", help="Force full SHA-256 verification instead of fast mtime mode")
    p_ps.add_argument("--max-diff-lines", type=int, default=40, help="Max git diff lines per modified file (default: 40)")
    p_ps.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_ps.add_argument("--json", action="store_true", help="Output JSON")
    p_ps.set_defaults(func=cmd_prepare_sync)

    p_zoom = sub.add_parser(
        "zoom",
        help="Read, scaffold, or upsert symbol-level H-tier sections in `.aoci/aoci.hubs.txt`",
    )
    p_zoom.add_argument("path", nargs="?", default=None, help="Target relative file path (for read or --scaffold)")
    p_zoom.add_argument("--root", help="Target codebase root")
    p_zoom.add_argument("--scaffold", action="store_true", help="Scaffold symbol section skeletons for <path>")
    p_zoom.add_argument("--output", help="Optional output file path for --scaffold skeletons")
    p_zoom.add_argument("--upsert", action="append", help="Hub entry line to merge into aoci.hubs.txt (repeatable)")
    p_zoom.add_argument("--upsert-file", help="Path to a text file containing hub entry lines")
    p_zoom.add_argument("--prune-missing", action="store_true", help="Prune hub entries whose file or symbol no longer exists")
    p_zoom.add_argument("--dry-run", action="store_true", help="Validate --upsert changes without writing to disk")
    p_zoom.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_zoom.add_argument("--json", action="store_true", help="Output JSON")
    p_zoom.set_defaults(func=cmd_zoom)

    p_as = sub.add_parser(
        "apply-sync",
        help="Run ordered sync workflow (restore -> migrate-renames -> ack -> upsert -> verify) in one pass",
    )
    p_as.add_argument("--root", help="Target codebase root")
    p_as.add_argument("--restore", action="store_true", help="Run semantic pool restore (`--no-prune`) first")
    p_as.add_argument("--allow-sha-fallback", action="store_true", help="Pass `--allow-sha-fallback` to `restore`")
    p_as.add_argument("--migrate-renames", action="store_true", help="Migrate exact and similar renames")
    p_as.add_argument("--exact-only", action="store_true", help="Pass `--exact-only` to `migrate-renames`")
    p_as.add_argument("--ack-path", action="append", help="Path to acknowledge via `ack` (repeatable; requires --reviewed)")
    p_as.add_argument("--ack-impl-only", action="store_true", help="Acknowledge `impl_only` modified files (requires --reviewed)")
    p_as.add_argument("--reviewed", action="store_true", help="Confirm acknowledged files were read and verified")
    p_as.add_argument("--entry", action="append", help="object-fras-v2 line to merge in `upsert` (repeatable)")
    p_as.add_argument("--entries-file", help="File containing object-fras-v2 lines to merge in `upsert`")
    p_as.add_argument("--prune-only", action="store_true", help="Pass `--prune-only` to `upsert`")
    p_as.add_argument("--no-prune", action="store_true", help="Pass `--no-prune` to `upsert`")
    p_as.add_argument("--strict-size", action="store_true", help="Pass `--strict-size` to `upsert`")
    p_as.add_argument("--drop-malformed", action="store_true", help="Allow dropping pre-existing malformed lines")
    p_as.add_argument("--no-verify", action="store_true", help="Skip trailing `verify` check")
    p_as.add_argument("--full-verify", action="store_true", help="Run full `verify` instead of `verify --fast`")
    p_as.add_argument("--dry-run", action="store_true", help="Preview mutating stages without writing to disk")
    p_as.add_argument("--json", action="store_true", help="Output JSON")
    p_as.set_defaults(func=cmd_apply_sync)

    p_sym = sub.add_parser(
        "symbol",
        help="Search symbols across `.aoci/aoci.hubs.txt`, outline cache, and FRAS `A:` exports",
    )
    p_sym.add_argument("name", help="Symbol name or substring to look up")
    p_sym.add_argument("--root", help="Target codebase root")
    p_sym.add_argument("--exact", action="store_true", help="Restrict to exact / qualified suffix matches")
    p_sym.add_argument("--depth", type=int, default=1, help="FRAS neighbor expansion hops per matched file (default: 1)")
    p_sym.add_argument("--limit", type=int, default=20, help="Maximum matched symbols to return (default: 20; 0 = unlimited)")
    p_sym.add_argument("--live", action="store_true", help="Read-only live outline fallback for files missing from outline cache")
    p_sym.add_argument("--json", action="store_true", help="Output JSON")
    p_sym.set_defaults(func=cmd_symbol)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
