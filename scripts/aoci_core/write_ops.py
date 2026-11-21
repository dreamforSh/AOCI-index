"""Write-path operations: `LiveFileSet`, entry validation, locked `upsert` / `ack` / `migrate-renames`, and semantic pool sync."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import constants as _constants
from . import discovery as _discovery
from . import drift as _drift
from . import git_ops as _git_ops
from . import hubs as _hubs
from . import ignore as _ignore
from . import lock as _lock
from . import parsers as _parsers
from . import pool as _pool
from . import shards as _shards
from .batches import _clear_batch_plan, _load_batch_plan
from .constants import (
    AOCI_DIR_NAME,
    CACHE_DIR,
    CODE_FILE,
    DEFAULT_MAX_BYTES,
    DEFAULT_SKIP_DIRS,
    ENTRY_RE,
    GIT_DISCOVERY_CACHE,
    HUBS_FILE,
    MIN_RENAME_LINES,
    SCAN_FP_CACHE,
    SIG_HASH_EXTS,
    VALID_LAYERS,
    VALID_SIZES,
    normalize_relpath,
)
from .graph import (
    compile_path_ref_rewriter,
    extract_path_refs,
    parse_entry_fields,
    validate_parsed_entry_tags,
)
from .shards import (
    parse_code_volume,
    render_code_volume,
    sync_shards_if_enabled,
    write_code_volume,
)
from .templates import parse_declared_domains


def _populate_missing_sig_hashes(root: Path, fp_targets: Dict[str, Dict[str, Any]]) -> None:
    """Compute missing `sig_hash` fields in parallel when bulk-restoring or migrating many files."""
    todo = [
        rel
        for rel, fp_obj in fp_targets.items()
        if "sig_hash" not in fp_obj and Path(rel).suffix.lower() in SIG_HASH_EXTS
    ]
    if not todo:
        return
    if len(todo) >= 32:
        workers = min(8, os.cpu_count() or 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for rel, sig_h in zip(todo, ex.map(lambda r: _parsers.compute_signature_hash(root / r, r), todo)):
                if sig_h is not None:
                    fp_targets[rel]["sig_hash"] = sig_h
    else:
        for rel in todo:
            sig_h = _parsers.compute_signature_hash(root / rel, rel)
            if sig_h is not None:
                fp_targets[rel]["sig_hash"] = sig_h


def record_semantic_pool_entries(
    root: Path,
    git_info: Dict[str, Any],
    records: Sequence[Tuple[str, Dict[str, Any], str]],
) -> None:
    """Best-effort persist `(relpath, fingerprint, normalized_entry)` into the shared SQLite WAL semantic pool."""
    _pool.record_semantic_pool_entries(root, git_info, records, max_entries=_constants.MAX_SEMANTIC_POOL_ENTRIES)


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
    pool_file = _pool.semantic_pool_path(root, git_info)
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    lock_ctx: Any = contextlib.nullcontext() if dry_run else _lock.IndexLock(aoci_dir)
    with lock_ctx:
        entries, malformed = _shards.parse_code_volume(code_path)
        if malformed and not dry_run:
            return {
                "ok": False,
                "error": f"`aoci.code.txt` has {len(malformed)} malformed line(s); fix them before running restore.",
            }
        baseline = _drift.load_baseline(aoci_dir)
        base_files: Dict[str, Any] = baseline.get("files", {})
        ignores = _ignore.load_ignore_matcher(root, git_info)
        fps, _ = _discovery.discover_and_fingerprint(
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
        ps_hits, _ = _pool._lookup_pool_entries(
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
            _, s_hits = _pool._lookup_pool_entries(
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
            validated_cand = _pool._validate_pool_entry_row(None if via_sha else rel, sha, cand)
            if validated_cand is None:
                continue
            m = ENTRY_RE.match(validated_cand)
            if not m or m.group("layer") not in VALID_LAYERS or m.group("size") not in VALID_SIZES:
                continue
            entries[rel] = (
                f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{fp['size_tier']}]: "
                f"F:{m.group('f').strip()} | R:{m.group('r').strip()} | "
                f"A:{m.group('a').strip()} | S:{m.group('s').strip()}"
            )
            fp_restored = dict(fp)
            base_files[rel] = fp_restored
            restored.append(rel)
            if via_sha:
                restored_by_sha.append(rel)
                orig_rel = normalize_relpath(m.group("path"))
                if orig_rel and orig_rel != rel and orig_rel not in fps:
                    sha_rename_map[orig_rel] = rel

        if restored:
            _populate_missing_sig_hashes(root, {r: base_files[r] for r in restored if r in base_files})

        if applied_shas and not dry_run:
            _pool._lookup_pool_entries(root, git_info, [], applied_shas, touch_lru=True)

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

        migrated_hubs: List[str] = []
        if sha_rename_map:
            migrated_hubs = _hubs.migrate_hubs_renames(root, aoci_dir, sha_rename_map, dry_run=dry_run)

        hub_sync: Dict[str, List[str]] = {"refreshed": [], "pruned": []}
        hubs_path = aoci_dir / HUBS_FILE
        if not dry_run:
            hub_orphan_paths: List[str] = list(pruned)
            if prune_deleted and hubs_path.is_file():
                hub_recs, hub_mal = _hubs.parse_hubs_volume(hubs_path)
                if not hub_mal:
                    for hr in hub_recs:
                        h_rel = hr["relpath"]
                        if h_rel not in fps and h_rel not in hub_orphan_paths:
                            hub_orphan_paths.append(h_rel)
            if restored or pruned or rewritten_refs:
                write_code_volume(code_path, entries)
                sync_shards_if_enabled(aoci_dir, entries)
                baseline["files"] = base_files
                _drift.save_baseline(aoci_dir, baseline, touched_paths=restored + pruned)
                _parsers.invalidate_outline_cache(
                    root, aoci_dir, touched_paths=restored, pruned_paths=pruned, rename_map=sha_rename_map
                )
            if hubs_path.is_file() and (restored or hub_orphan_paths):
                hub_sync = _hubs.sync_hubs_on_upsert(root, aoci_dir, restored, hub_orphan_paths)

        restored_hubs = _hubs.restore_hubs_from_pool(
            root,
            aoci_dir,
            sorted(entries.keys()),
            lambda: _pool.list_pooled_hub_relpaths(root, git_info),
            lambda q, touch: _pool.lookup_hub_pool_entries(root, git_info, q, touch_lru=touch),
            dry_run=dry_run,
        )

        res_payload: Dict[str, Any] = {
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
        if restored_hubs:
            res_payload["restored_hubs"] = restored_hubs
        if migrated_hubs:
            res_payload["migrated_hubs"] = migrated_hubs
        if hub_sync.get("refreshed"):
            res_payload["hubs_refreshed"] = hub_sync["refreshed"]
        if hub_sync.get("pruned"):
            res_payload["hubs_pruned"] = hub_sync["pruned"]
        return res_payload


def pool_stats(root: Path, git_info: Dict[str, Any]) -> Dict[str, Any]:
    return _pool.pool_stats(root, git_info, max_entries=_constants.MAX_SEMANTIC_POOL_ENTRIES)


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
        parse_code_volume_fn=_shards.parse_code_volume,
        load_baseline_fn=_drift.load_baseline,
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
        max_entries=_constants.MAX_SEMANTIC_POOL_ENTRIES,
    )


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
        self.ignores = _ignore.load_ignore_matcher(root, git_info)
        self.skip_paths: Dict[str, str] = {}
        self._fresh: Dict[str, Dict[str, Any]] = {}

        if only_paths is not None:
            self.fps, self.lower_map = self._init_targeted(only_paths, git_info, base_files=base_files)
        else:
            self.fps, _ = _discovery.discover_and_fingerprint(
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
            reason = _ignore.path_skip_reason(canon, self.ignores)
            if reason is not None:
                self.skip_paths[canon] = reason
                continue
            cached_b = base_files.get(canon) if base_files is not None else None
            _, fp, skip_r = _discovery.inspect_and_fingerprint_one(
                self.root, canon, None, cached_b, False, self.max_bytes
            )
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
            key = _git_ops._git_discovery_cache_key(self.root, git_info, self.max_bytes)
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
                allowed: Set[str] = set()
                git_ok = True
                chunk_sz = 200
                for idx in range(0, len(unchecked), chunk_sz):
                    chunk = unchecked[idx : idx + chunk_sz]
                    res = _git_ops.run_git(
                        ["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *chunk],
                        self.root,
                    )
                    if res.returncode != 0:
                        git_ok = False
                        break
                    allowed.update(normalize_relpath(it) for it in res.stdout.split("\0") if it)
                if git_ok:
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
            fp = _discovery.fingerprint_file(self.root / rel)
            self._fresh[rel] = fp
        elif "sig_hash" not in fp and not fp.get("_sig_checked"):
            sig_h = _parsers.compute_signature_hash(self.root / rel, rel)
            if sig_h is not None:
                fp["sig_hash"] = sig_h
            else:
                fp["_sig_checked"] = True
        return fp

    def explain_missing(self, rel: str) -> str:
        full = self.root / rel
        reason = self.skip_paths.get(rel) or _ignore.path_skip_reason(rel, self.ignores)
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
            _, _, reason = _discovery.inspect_and_fingerprint_one(self.root, rel, lst, None, False, self.max_bytes)
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
        fp = _discovery.fingerprint_file(full)

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
    line_sources: Optional[Sequence[Optional[str]]] = None,
) -> int:
    as_json = bool(getattr(args, "json", False))
    # Re-read everything under the lock so concurrent writers never lose each other's updates
    declared_domains = parse_declared_domains(aoci_dir)
    entries, malformed = _shards.parse_code_volume(code_path)
    baseline = _drift.load_baseline(aoci_dir)
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

    for idx_line, raw in enumerate(raw_lines):
        rel, norm, warns, err = validate_and_normalize_entry(
            raw,
            root,
            declared_domains=declared_domains,
            auto_fix_size=not args.strict_size,
            live=live,
        )
        if err is not None:
            src_tag = line_sources[idx_line] if (line_sources and idx_line < len(line_sources)) else None
            errors.append(f"[{src_tag}] {err}" if src_tag else err)
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
    _drift.save_baseline(aoci_dir, baseline, touched_paths=upserted_paths + pruned)
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
    _parsers.invalidate_outline_cache(root, aoci_dir, touched_paths=upserted_paths, pruned_paths=pruned)
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


def _migrate_renames_locked(
    args: argparse.Namespace,
    root: Path,
    aoci_dir: Path,
    code_path: Path,
    git_info: Dict[str, Any],
) -> int:
    as_json = bool(getattr(args, "json", False))
    entries, malformed = _shards.parse_code_volume(code_path)
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

    drift = _drift.compute_drift(root, git_info, fast_mtime=False, max_bytes=args.max_bytes, allow_heal=not args.dry_run)
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

    baseline = _drift.load_baseline(aoci_dir)
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
        base_files[new_rel] = fp_new
        migrated.append((old_rel, new_rel))

    if migrated:
        _populate_missing_sig_hashes(root, {new_r: base_files[new_r] for _, new_r in migrated if new_r in base_files})

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
    if not args.dry_run and rename_map:
        sim_dirty = [n for _, n, _ in migrated_similar]
        _parsers.invalidate_outline_cache(root, aoci_dir, touched_paths=sim_dirty, rename_map=rename_map)

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
    _drift.save_baseline(aoci_dir, baseline, touched_paths=list(rename_map.keys()) + list(rename_map.values()))
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


def _ack_locked(
    args: argparse.Namespace,
    root: Path,
    aoci_dir: Path,
    code_path: Path,
    git_info: Dict[str, Any],
) -> int:
    as_json = bool(getattr(args, "json", False))
    impl_only = bool(getattr(args, "impl_only", False))
    hubs_refreshed: List[str] = []

    entries, malformed = _shards.parse_code_volume(code_path)
    baseline = _drift.load_baseline(aoci_dir)
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
                    ck = _drift._classify_modified_entry(root, cand_rel, cached, cur_fp=live.fps.get(cand_rel))
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
            ck = _drift._classify_modified_entry(
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
        _drift.save_baseline(aoci_dir, baseline, touched_paths=acked)
        record_semantic_pool_entries(
            root,
            git_info,
            [(c, base_files[c], entries[c]) for c in acked if c in entries and c in base_files],
        )
        if (aoci_dir / HUBS_FILE).is_file() and not args.dry_run:
            hubs_refreshed = _hubs.refresh_hub_spans_for_paths(root, aoci_dir, acked)
        _parsers.invalidate_outline_cache(root, aoci_dir, touched_paths=acked)

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
