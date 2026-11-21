"""Baseline load/save, racy-clean healing, exact & MinHash rename pairing, and drift computation."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import discovery as _discovery
from . import git_ops as _git_ops
from . import graph as _graph
from . import lock as _lock
from . import parsers as _parsers
from . import shards as _shards
from .constants import (
    AOCI_DIR_NAME,
    BASELINE_FILE,
    BASELINE_SCHEMA_VERSION,
    CACHE_DIR,
    CODE_FILE,
    DEFAULT_MAX_BYTES,
    GIT_DISCOVERY_CACHE,
    MIN_RENAME_LINES,
    SIMILAR_RENAME_THRESHOLD,
    atomic_write_text,
    utc_now_iso,
)
from .ignore import load_ignore_matcher
from .templates import parse_declared_domains


def load_baseline(aoci_dir: Path) -> Dict[str, Any]:
    bp = aoci_dir / BASELINE_FILE
    empty = {"schema_version": 1, "root_name": aoci_dir.parent.name, "updated_at": None, "files": {}}
    if not bp.exists():
        return empty
    try:
        raw = bp.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"WARN: Cannot read `{bp}` ({exc}); treating baseline as empty.", file=sys.stderr)
        return empty
    if not raw.strip():
        return empty
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            print(f"WARN: Invalid schema in `{bp}`; treating baseline as empty.", file=sys.stderr)
            return empty
        return data
    except ValueError as exc:
        print(f"WARN: Corrupt JSON in `{bp}` ({exc}); treating baseline as empty.", file=sys.stderr)
        return empty


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
        _discovery._prune_scan_fp_cache(aoci_dir, touched_paths)
        _git_ops._demote_git_discovery_paths(aoci_dir, touched_paths)
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
        with _lock.IndexLock(aoci_dir, timeout=0.05):
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
        cur_sig = _parsers.compute_signature_hash(root / rel, rel)
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
    code_entries, malformed = _shards.parse_code_volume(
        aoci_dir / CODE_FILE, duplicates=duplicate_entries, raw_text_out=raw_code_texts
    )

    fingerprints, skipped = _discovery.discover_and_fingerprint(
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
                sim = _discovery._sketch_similarity(old_fp.get("sketch"), new_fp.get("sketch"))
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
    parsed_entries = _graph.parse_entries_map(code_entries)
    undeclared_domains: Dict[str, List[str]] = {}
    invalid_entries: List[Dict[str, str]] = []
    invalid_clean_paths: Set[str] = set()
    for rel in sorted(parsed_entries.keys()):
        pe = parsed_entries[rel]
        if declared_domains and pe.domain not in declared_domains:
            undeclared_domains.setdefault(pe.domain, []).append(rel)
        fp_cur = fingerprints.get(rel)
        actual_l = int(fp_cur["lines"]) if fp_cur is not None else None
        tag_errs = _graph._validate_tag_fields(rel, pe.layer, pe.size, pe.f, pe.raw, actual_lines=actual_l)
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
        "_parsed_entries": parsed_entries,
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
