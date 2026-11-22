"""
Mutating CLI subcommands (`init`, `ensure-git-exclude`, `upsert`, `ack`, `migrate-renames`, `restore`, `zoom`,
`shard`, `pool`, `scaffold`, `apply-sync`, `install-git-hooks`, `doctor`).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import apply_sync as _apply_sync
from . import audit as _audit
from . import cli_read as _cli_read
from . import doctor as _doctor
from . import drift as _drift
from . import git_ops as _git_ops
from . import hubs as _hubs
from . import lock as _lock
from . import pool as _pool
from . import shards as _shards
from . import write_ops as _write_ops
from .batches import _build_pending_items, resolve_stable_batches
from .constants import (
    AOCI_DIR_NAME,
    BASELINE_FILE,
    CODE_FILE,
    DEFAULT_MAX_BYTES,
    HUBS_FILE,
    IGNORE_FILE,
    MANIFEST_FILE,
    META_FILE,
    SHARDS_DIR,
    SHARDS_INDEX,
    _resolve_cli_file_path,
    normalize_relpath,
    write_text_lf,
)
from .templates import (
    render_ignore_template,
    render_manifest_template,
    render_meta_template,
)

_CLI_RUNNER: Optional[Callable[[Sequence[str]], int]] = None


def _set_cli_runner(runner: Callable[[Sequence[str]], int]) -> None:
    global _CLI_RUNNER
    _CLI_RUNNER = runner


def _invoke_cli_runner(argv: Sequence[str]) -> int:
    if _CLI_RUNNER is not None:
        return _CLI_RUNNER(argv)
    from .cli_parser import run_cli  # imported lazily: `cli_parser` dispatches back into this module

    return run_cli(argv)


def cmd_init(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    git_info = _git_ops.detect_git_info(root)

    # Step 1: Enforce Git exclusion BEFORE writing index files
    git_status = _git_ops.ensure_git_exclusion(root, git_info, check_status=True)
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
        _shards.write_code_volume(code_path, {})

    baseline_path = aoci_dir / BASELINE_FILE
    if not baseline_path.exists() or args.force:
        _drift.save_baseline(aoci_dir, {"schema_version": 1, "root_name": project_name, "files": {}})

    ignore_path = aoci_dir / IGNORE_FILE
    if not ignore_path.exists():
        write_text_lf(ignore_path, render_ignore_template())

    final_git = _git_ops.check_git_exclusion(root, git_info, check_status=True)
    drift = _drift.compute_drift(root, git_info, max_bytes=args.max_bytes, save_scan_cache=True)

    if args.json:
        print(json.dumps({"git": final_git, "drift": _drift.public_drift(drift)}, indent=2, ensure_ascii=False))
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
    root = _git_ops.resolve_root(args.root)
    git_info = _git_ops.detect_git_info(root)
    res = _git_ops.ensure_git_exclusion(root, git_info, check_status=True)
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


def cmd_upsert(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
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

    git_info = _git_ops.detect_git_info(root)
    if args.dry_run:
        git_status = _git_ops.check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        git_status = _git_ops.ensure_git_exclusion(root, git_info, check_status=False)
        if git_status["is_git"] and not git_status["excluded"]:
            return _fail("Failed to exclude `.aoci/` from Git. Aborting upsert.", 2)

    raw_lines: List[str] = []
    line_sources: List[Optional[str]] = []
    pending_files_to_clean: List[Path] = []
    if getattr(args, "pending_dir", None):
        pdir = _resolve_cli_file_path(root, args.pending_dir, must_exist=True)
        if not pdir.is_dir():
            return _fail(f"`--pending-dir` `{pdir}` is not a directory.", 2)
        txt_files = sorted(f for f in pdir.glob("*.txt") if f.is_file())
        if not txt_files and not args.entries_file and not args.entry and not args.stdin and not args.prune_only:
            return _fail(f"No `*.txt` batch files found in `--pending-dir` `{pdir}`.", 2)
        for tf in txt_files:
            try:
                tf_lines = tf.read_text(encoding="utf-8-sig").splitlines()
            except OSError as exc:
                return _fail(f"Cannot read batch file `{tf}`: {exc}", 2)
            for lno, ltxt in enumerate(tf_lines, start=1):
                raw_lines.append(ltxt)
                line_sources.append(f"{tf.name}:L{lno}")
            pending_files_to_clean.append(tf)
    if args.entries_file:
        ep = _resolve_cli_file_path(root, args.entries_file, must_exist=True)
        try:
            ef_lines = ep.read_text(encoding="utf-8-sig").splitlines()
            raw_lines.extend(ef_lines)
            line_sources.extend([None] * len(ef_lines))
        except OSError as exc:
            return _fail(f"Cannot read `--entries-file` `{ep}`: {exc}", 2)
    if args.entry:
        raw_lines.extend(args.entry)
        line_sources.extend([None] * len(args.entry))
    if args.stdin:
        st_lines = sys.stdin.read().splitlines()
        raw_lines.extend(st_lines)
        line_sources.extend([None] * len(st_lines))

    if not raw_lines and not args.prune_only:
        return _fail(
            "Provide entries via `--entries-file`, `--pending-dir`, `--entry`, `--stdin`, or use `--prune-only`.",
            2,
        )

    lock_ctx: Any = contextlib.nullcontext() if args.dry_run else _lock.IndexLock(aoci_dir)
    try:
        with lock_ctx:
            rc = _write_ops._upsert_locked(
                args,
                root,
                aoci_dir,
                code_path,
                git_info,
                raw_lines,
                line_sources=line_sources,
            )
            if rc == 0 and not args.dry_run and getattr(args, "clean_pending", False) and pending_files_to_clean:
                for pf in pending_files_to_clean:
                    try:
                        pf.unlink()
                    except OSError:
                        pass
            return rc
    except TimeoutError as exc:
        return _fail(str(exc), 3)


def cmd_migrate_renames(args: argparse.Namespace) -> int:
    """Automatically migrate exact-content renamed files in `aoci.code.txt` and rewrite R:/A: path refs."""
    root = _git_ops.resolve_root(args.root)
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

    git_info = _git_ops.detect_git_info(root)
    if args.dry_run:
        _git_ops.check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        _git_ops.ensure_git_exclusion(root, git_info, check_status=False)

    lock_ctx: Any = contextlib.nullcontext() if args.dry_run else _lock.IndexLock(aoci_dir)
    try:
        with lock_ctx:
            return _write_ops._migrate_renames_locked(args, root, aoci_dir, code_path, git_info)
    except TimeoutError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 3


def cmd_ack(args: argparse.Namespace) -> int:
    """
    Re-baseline files whose content changed but whose existing FRAS entry is still accurate
    (refactors, typo fixes, body-only edits). Refreshes the Size tier automatically.
    """
    root = _git_ops.resolve_root(args.root)
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

    git_info = _git_ops.detect_git_info(root)
    if args.dry_run:
        _git_ops.check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        git_status = _git_ops.ensure_git_exclusion(root, git_info, check_status=False)
        if git_status["is_git"] and not git_status["excluded"]:
            return _fail("Failed to exclude `.aoci/` from Git. Aborting ack.", 2)

    lock_ctx: Any = contextlib.nullcontext() if args.dry_run else _lock.IndexLock(aoci_dir)
    try:
        with lock_ctx:
            return _write_ops._ack_locked(args, root, aoci_dir, code_path, git_info)
    except TimeoutError as exc:
        return _fail(str(exc), 3)


def cmd_zoom(args: argparse.Namespace) -> int:
    """Read, scaffold, or upsert symbol-level H-tier sections in `.aoci/aoci.hubs.txt`."""
    root = _git_ops.resolve_root(args.root)
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
        git_info = _git_ops.detect_git_info(root)
        if args.dry_run:
            _git_ops.check_git_exclusion(root, git_info, check_status=False, write_cache=False)
        else:
            git_status = _git_ops.ensure_git_exclusion(root, git_info, check_status=False)
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
                lock_cls=_lock.IndexLock,
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

        if not args.dry_run and res.get("upserted"):
            hubs_file = aoci_dir / HUBS_FILE
            if hubs_file.is_file():
                h_recs, _ = _hubs.parse_hubs_volume(hubs_file)
                up_keys = set(res["upserted"])
                changed_recs = [r for r in h_recs if f"{r['relpath']}#{r['symbol']}" in up_keys]
                hub_rows = _hubs.collect_hub_pool_rows(root, changed_recs)
                if hub_rows:
                    _pool.record_hub_pool_entries(root, git_info, hub_rows)

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
        rel, _, err = _cli_read._validate_zoom_target_file(root, str(args.path), args.max_bytes)
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

    zoom_res = _cli_read.compute_zoom_read(root, str(args.path), max_bytes=args.max_bytes)
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


def cmd_shard(args: argparse.Namespace) -> int:
    """Build or remove per-directory `.aoci/shards/<dir>.code.txt` slices for very large repositories."""
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return _cli_read._emit_cli_error(
            bool(getattr(args, "json", False)),
            f"`{code_path}` not found. Run `init` first.",
            2,
        )

    shards_dir = aoci_dir / SHARDS_DIR
    try:
        with _lock.IndexLock(aoci_dir):
            if args.clean:
                removed = shards_dir.exists()
                if removed:
                    shutil.rmtree(shards_dir)
                if args.json:
                    print(json.dumps({"cleaned": removed, "shards_dir": str(shards_dir)}, indent=2))
                else:
                    print(f"[AOCI Shard] {'Removed' if removed else 'No'} `.aoci/{SHARDS_DIR}/` directory.")
                return 0

            entries, _ = _shards.parse_code_volume(code_path)
            manifest = _shards.write_shards(aoci_dir, entries, depth=args.depth)
    except TimeoutError as exc:
        return _cli_read._emit_cli_error(bool(getattr(args, "json", False)), str(exc), 3)

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
    root = _git_ops.resolve_root(args.root)
    git_info = _git_ops.detect_git_info(root)
    if args.dry_run:
        _git_ops.check_git_exclusion(root, git_info, check_status=False, write_cache=False)
    else:
        _git_ops.ensure_git_exclusion(root, git_info, check_status=False)

    allow_sha = bool(getattr(args, "allow_sha_fallback", False) and not getattr(args, "exact_path_only", False))
    try:
        res = _write_ops.restore_from_semantic_pool(
            root,
            git_info,
            dry_run=args.dry_run,
            allow_sha_fallback=allow_sha,
            prune_deleted=not args.no_prune,
            max_bytes=args.max_bytes,
        )
    except TimeoutError as exc:
        return _cli_read._emit_cli_error(bool(getattr(args, "json", False)), str(exc), 3)

    if not res.get("ok"):
        rc_err = 2 if "not found" in str(res.get("error")) else 1
        return _cli_read._emit_cli_error(bool(getattr(args, "json", False)), str(res.get("error")), rc_err, extra=res)

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


def cmd_scaffold(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    git_info = _git_ops.detect_git_info(root)
    targets: List[str] = [normalize_relpath(p) for p in (args.paths or [])]
    precomputed_fps: Optional[Dict[str, Dict[str, Any]]] = None
    batch_plan_meta: Optional[Dict[str, Any]] = None

    if args.unindexed or args.batch:
        drift = _drift.compute_drift(
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
                return _cli_read._emit_cli_error(
                    bool(getattr(args, "json", False)),
                    f"Batch {args.batch} out of range (1..{len(batches)}).",
                    2,
                )
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
        return _cli_read._emit_cli_error(
            bool(getattr(args, "json", False)),
            "Specify file paths, `--unindexed`, or `--batch N`.",
            2,
        )

    drafts = _audit.scaffold_entries(root, targets, max_bytes=args.max_bytes, precomputed_fps=precomputed_fps)
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


def _capture_internal_cli(argv: Sequence[str]) -> Tuple[int, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            rc = _invoke_cli_runner(list(argv))
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
    root = _git_ops.resolve_root(args.root)
    res = _apply_sync.run_apply_sync(
        root,
        _capture_internal_cli,
        restore=bool(getattr(args, "restore", False)),
        allow_sha_fallback=bool(getattr(args, "allow_sha_fallback", False)),
        migrate_renames=bool(getattr(args, "migrate_renames", False)),
        exact_only=bool(getattr(args, "exact_only", False)),
        no_rewrite_refs=bool(getattr(args, "no_rewrite_refs", False)),
        ack_paths=getattr(args, "ack_path", None) or [],
        ack_impl_only=bool(getattr(args, "ack_impl_only", False)),
        reviewed=bool(getattr(args, "reviewed", False)),
        entries=getattr(args, "entry", None) or [],
        entries_file=getattr(args, "entries_file", None),
        pending_dir=getattr(args, "pending_dir", None),
        clean_pending=bool(getattr(args, "clean_pending", False)),
        prune_only=bool(getattr(args, "prune_only", False)),
        no_prune=bool(getattr(args, "no_prune", False)),
        strict_size=bool(getattr(args, "strict_size", False)),
        drop_malformed=bool(getattr(args, "drop_malformed", False)),
        verify=not bool(getattr(args, "no_verify", False)),
        full_verify=bool(getattr(args, "full_verify", False)),
        max_bytes=getattr(args, "max_bytes", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        lock_cls=_lock.IndexLock,
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


def cmd_pool(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    git_info = _git_ops.detect_git_info(root)
    action = args.pool_action
    as_json = bool(getattr(args, "json", False))

    if action == "stats":
        res = _write_ops.pool_stats(root, git_info)
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(
                f"[AOCI Pool] Exists={res['exists']} | Path={res['pool_path']} | "
                f"Bytes={res['bytes']} | ByPathSha={res['by_path_sha_count']} | "
                f"BySha={res['by_sha_count']} | ByHubs={res.get('by_hub_symbol_sha_count', 0)}"
            )
        return 0

    if action == "export":
        out_path = _resolve_cli_file_path(root, args.output, must_exist=False)
        res = _write_ops.export_semantic_pool(root, git_info, out_path, include_current=not args.pool_only)
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(
                f"[AOCI Pool Export] OK | ByPathSha={res['exported_by_path_sha']} | "
                f"BySha={res['exported_by_sha']} | Hubs={res.get('exported_hubs', 0)} | "
                f"Bytes={res['bytes']} -> {res['output_path']}"
            )
        return 0

    if action == "import":
        in_path = _resolve_cli_file_path(root, args.input, must_exist=True)
        res = _write_ops.import_semantic_pool(root, git_info, in_path, dry_run=bool(args.dry_run))
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
                f"BySha={res['imported_by_sha']} | Hubs={res.get('imported_hubs', 0)} | Rejected={res['rejected']}"
                + (f" | Meta={','.join(sorted(res['meta_applied']))}" if res.get("meta_applied") else "")
            )
        return 0

    return 2


def cmd_install_git_hooks(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    git_info = _git_ops.detect_git_info(root)
    if getattr(args, "uninstall", False):
        res = _git_ops.uninstall_git_hooks(root, git_info, dry_run=bool(args.dry_run))
    else:
        res = _git_ops.install_git_hooks(
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Deterministically repair mechanical `.aoci/` drift (duplicates, sizes, dangling refs, static R:, hubs)."""
    root = _git_ops.resolve_root(args.root)
    as_json = bool(getattr(args, "json", False))
    try:
        res = _doctor.run_index_doctor(
            root,
            sync_static_r=bool(getattr(args, "sync_static_r", False)),
            drop_malformed=bool(getattr(args, "drop_malformed", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            max_bytes=int(getattr(args, "max_bytes", DEFAULT_MAX_BYTES)),
        )
    except TimeoutError as exc:
        return _cli_read._emit_cli_error(as_json, str(exc), 3)

    if not res.get("ok"):
        code = 1 if res.get("malformed") else 2
        return _cli_read._emit_cli_error(as_json, str(res.get("error", "Doctor failed.")), code, extra=res)

    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        mode = " DRY-RUN" if res["dry_run"] else ""
        print(
            f"[AOCI Doctor{mode}] OK | Indexed={res['indexed_files']} | "
            f"TotalFixes={res['total_fixes']} | Dedup={len(res['deduplicated'])} | "
            f"SizeRefreshed={len(res['size_refreshed'])} | "
            f"DanglingPruned={len(res['dangling_refs_pruned'])} | "
            f"StaticRAdded={len(res['static_deps_added'])}"
        )
    return 0
