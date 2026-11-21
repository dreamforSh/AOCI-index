"""CLI `ArgumentParser` construction (`build_parser`) and subcommand dispatch (`run_cli`)."""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from . import cli_write as _cli_write
from .cli_check import (
    cmd_audit,
    cmd_batch_pack,
    cmd_ci_check,
    cmd_diff,
    cmd_lint,
    cmd_prepare_sync,
    cmd_scan,
    cmd_verify,
)
from .cli_read import (
    cmd_context,
    cmd_export,
    cmd_outline,
    cmd_overview,
    cmd_pack,
    cmd_query,
    cmd_symbol,
)
from .cli_write import (
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
from .constants import AOCI_VERSION, DEFAULT_MAX_BATCH_LINES, DEFAULT_MAX_BYTES, configure_utf8_streams


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
    p_up.add_argument(
        "--pending-dir",
        help="Directory containing `*.txt` subagent batch entry files to validate and merge atomically",
    )
    p_up.add_argument(
        "--clean-pending",
        action="store_true",
        help="Remove consumed `*.txt` files in `--pending-dir` after a successful non-dry-run merge",
    )
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
    p_exp.add_argument("--diff", action="store_true", help="Highlight working-tree changed nodes and affected 1-hop callers")
    p_exp.add_argument("--diff-rev", help="Highlight changed nodes and affected callers against Git revision (e.g. `origin/main`)")
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
    p_as.add_argument("--no-rewrite-refs", action="store_true", help="Pass `--no-rewrite-refs` to `migrate-renames`")
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
    p_as.add_argument("--max-bytes", type=int, default=None, help="Override max source file size in bytes across stages")
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

    p_bp = sub.add_parser(
        "batch-pack",
        help="Assemble a self-contained subagent work-pack (scaffolds + outlines + optional source) for batch N",
    )
    p_bp.add_argument("--batch", type=int, required=True, help="1-indexed batch number to pack")
    p_bp.add_argument("--root", help="Target codebase root")
    p_bp.add_argument("--batch-size", type=int, default=None, help="Files per batch (inherits active batch_plan.json if omitted)")
    p_bp.add_argument("--max-batch-lines", type=int, default=None, help="Line budget per batch")
    p_bp.add_argument("--reset-batches", action="store_true", help="Rebuild `.aoci/.cache/batch_plan.json` before packing")
    p_bp.add_argument("--with-source", action="store_true", help="Embed bounded source excerpts per file")
    p_bp.add_argument("--max-source-lines", type=int, default=200, help="Maximum source lines per file with `--with-source` (default: 200)")
    p_bp.add_argument("--output", help="Optional output file path")
    p_bp.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_bp.add_argument("--json", action="store_true", help="Output JSON")
    p_bp.set_defaults(func=cmd_batch_pack)

    p_ci = sub.add_parser(
        "ci-check",
        help="CI-native architecture guard (layer rules, cycles, PR diff impact, GitHub annotations & SARIF 2.1.0)",
    )
    p_ci.add_argument("--root", help="Target codebase root")
    p_ci.add_argument(
        "--format",
        choices=["text", "json", "github", "sarif", "markdown"],
        default="text",
        help="Report output format (default: text)",
    )
    p_ci.add_argument("--base-rev", help="Base Git revision (e.g. `origin/main`) for PR diff impact & test derivation")
    p_ci.add_argument("--pool-bundle", help="Optional exported semantic pool bundle (`.json` or `.json.gz`) for zero-index CI")
    p_ci.add_argument("--forbid", help="Extra layer redlines (e.g. `D!->E,U; P!->E`)")
    p_ci.add_argument("--no-default-rules", action="store_true", help="Disable built-in layer redline defaults")
    p_ci.add_argument("--no-static", action="store_true", help="Skip static import vs FRAS cross-check")
    p_ci.add_argument("--no-cycles", action="store_true", help="Skip dependency cycle detection")
    p_ci.add_argument("--no-layers", action="store_true", help="Skip layer redline validation")
    p_ci.add_argument("--check-drift", action="store_true", help="Also report unsynced `.aoci/` drift when `.aoci/` exists")
    p_ci.add_argument("--exclude-tests", action="store_true", help="Exclude T-layer test files from layer/cycle checks")
    p_ci.add_argument("--sarif-output", help="Optional file path to write SARIF 2.1.0 output alongside stdout")
    p_ci.add_argument("--output", help="Optional file path to write formatted report")
    p_ci.add_argument("--strict", action="store_true", help="Exit with code 1 when any architectural finding exists")
    p_ci.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_ci.add_argument("--json", action="store_true", help="Shortcut for `--format json`")
    p_ci.set_defaults(func=cmd_ci_check)

    return p


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    configure_utf8_streams(include_stdin=(argv is None))
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


_cli_write._set_cli_runner(run_cli)
