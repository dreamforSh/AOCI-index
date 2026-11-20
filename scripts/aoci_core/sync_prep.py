"""Single-pass agent synchronization briefing (`aoci prepare-sync` / MCP `aoci_prepare_sync`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from .constants import DEFAULT_MAX_BYTES, _is_within_root, normalize_relpath
from .graph import build_explicit_mention_index, build_fras_graph, parse_entries_map
from .parsers import extract_file_outline
from .pool import count_restorable_from_pool


def _safe_outline(
    root: Path,
    rel: str,
    max_bytes: int,
    max_items: int = 25,
) -> Optional[Dict[str, Any]]:
    full = root / rel
    if not _is_within_root(root, full):
        return None
    try:
        if full.is_symlink() or not full.is_file():
            return None
        if full.stat().st_size > max_bytes:
            return None
        return extract_file_outline(full, rel, max_items=max_items)
    except OSError:
        return None


def _bounded_git_diff_preview(
    root: Path,
    rel: str,
    git_info: Mapping[str, Any],
    run_git_fn: Any,
    max_diff_lines: int,
) -> str:
    if not git_info.get("is_git") or max_diff_lines <= 0:
        return ""
    cp = run_git_fn(["-c", "core.quotePath=false", "diff", "-U1", "HEAD", "--", rel], root)
    if cp.returncode != 0:
        cp = run_git_fn(["-c", "core.quotePath=false", "diff", "-U1", "--", rel], root)
    if cp.returncode != 0 or not cp.stdout:
        return ""
    lines = cp.stdout.splitlines()
    if len(lines) <= max_diff_lines:
        return "\n".join(lines)
    kept = lines[:max_diff_lines]
    kept.append(f"... (+{len(lines) - max_diff_lines} truncated line(s))")
    return "\n".join(kept)


def prepare_sync_briefing(
    root: Path,
    git_info: Dict[str, Any],
    drift: Dict[str, Any],
    *,
    scaffold_fn: Any,
    run_git_fn: Any,
    max_diff_lines: int = 40,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Build an agent-ready single-pass sync briefing combining drift classification,
    semantic pool restorable count, auto-generated scaffolds for added files,
    previous entries + outlines + bounded git diffs for contract/unknown modifications,
    1-hop affected neighbors, and ordered recommended CLI actions.
    """
    code_entries: Dict[str, str] = drift.get("_code_entries") or {}
    fps: Dict[str, Dict[str, Any]] = drift.get("_fingerprints") or {}
    restorable = count_restorable_from_pool(root, git_info, drift)

    impl_only_modified: List[Dict[str, Any]] = []
    contract_or_unknown_modified: List[Dict[str, Any]] = []
    for item in drift.get("modified", ()):
        rel = normalize_relpath(str(item["path"]))
        if item.get("change_kind") == "impl_only":
            impl_only_modified.append(dict(item))
        else:
            enriched = dict(item)
            enriched["previous_entry"] = code_entries.get(rel)
            enriched["current_outline"] = _safe_outline(root, rel, max_bytes)
            enriched["diff_preview"] = _bounded_git_diff_preview(
                root, rel, git_info, run_git_fn, max_diff_lines
            )
            contract_or_unknown_modified.append(enriched)

    renames = [dict(r) for r in drift.get("renamed", ())]
    similar_renames = [dict(sr) for sr in drift.get("similar_renames", ())]
    sim_to = {normalize_relpath(str(sr["to"])) for sr in similar_renames}
    sim_from = {normalize_relpath(str(sr["from"])) for sr in similar_renames}

    added_non_sim = [
        dict(it)
        for it in drift.get("added", ())
        if normalize_relpath(str(it["path"])) not in sim_to
    ]
    deleted = [
        normalize_relpath(str(p))
        for p in drift.get("deleted", ())
        if normalize_relpath(str(p)) not in sim_from
    ]

    added_paths = [normalize_relpath(str(it["path"])) for it in added_non_sim]
    scaffold_by_path: Dict[str, str] = {}
    if added_paths:
        scaffold_lines = scaffold_fn(
            root,
            added_paths,
            max_bytes=max_bytes,
            precomputed_fps=fps,
        )
        for ln in scaffold_lines:
            p_raw = ln.split("[", 1)[0].strip()
            scaffold_by_path[normalize_relpath(p_raw)] = ln

    added_scaffolds: List[Dict[str, Any]] = []
    for it in added_non_sim:
        rel = normalize_relpath(str(it["path"]))
        added_scaffolds.append(
            {
                "path": rel,
                "lines": it["lines"],
                "size_tier": it["size_tier"],
                "bytes": it["bytes"],
                "scaffold": scaffold_by_path.get(rel, ""),
                "current_outline": _safe_outline(root, rel, max_bytes),
            }
        )

    missing_in_code = [dict(it) for it in drift.get("missing_in_code", ())]
    invalid_entries = [dict(it) for it in drift.get("invalid_entries", ())]
    malformed_lines = [dict(it) for it in drift.get("malformed_lines", ())]

    affected_neighbors: Dict[str, List[str]] = {}
    changed_paths: List[str] = (
        [normalize_relpath(str(it["path"])) for it in drift.get("modified", ())]
        + [normalize_relpath(str(r["from"])) for r in renames]
        + [normalize_relpath(str(sr["from"])) for sr in similar_renames]
        + deleted
    )
    if changed_paths and code_entries:
        parsed = parse_entries_map(code_entries)
        _, dependents = build_fras_graph(code_entries, parsed_entries=parsed)
        mentioned_by = build_explicit_mention_index(code_entries, parsed_entries=parsed)
        changed_set: Set[str] = set(changed_paths)
        for p in changed_paths:
            nbs = sorted((mentioned_by.get(p, set()) | dependents.get(p, set())) - changed_set)
            if nbs:
                affected_neighbors[p] = nbs

    recommended_actions: List[str] = []
    if restorable > 0:
        recommended_actions.append(
            f"restore: recover {restorable} entry(ies) from semantic cache pool at zero token cost"
        )
    if renames or similar_renames:
        recommended_actions.append(
            f"migrate-renames: carry {len(renames)} exact rename(s) and {len(similar_renames)} similar move(s) + rewrite R:/A: refs"
        )
    if impl_only_modified:
        recommended_actions.append(
            f"ack --impl-only: fast-acknowledge {len(impl_only_modified)} implementation-only modified file(s)"
        )
    needs_upsert_total = (
        len(contract_or_unknown_modified)
        + len(added_scaffolds)
        + len(missing_in_code)
        + len(invalid_entries)
    )
    if needs_upsert_total > 0 or deleted or malformed_lines:
        recommended_actions.append(
            "upsert: read source/diffs for contract-changed, added, invalid, or missing entries, author FRAS lines, and prune deleted files"
        )
    if drift.get("has_drift"):
        recommended_actions.append("verify --fast: confirm `.aoci/` index invariants and zero remaining drift")

    counts = {
        "total_eligible": int(drift.get("total_eligible", 0)),
        "indexed_clean": int(drift.get("indexed_clean", 0)),
        "restorable_from_pool": restorable,
        "renamed": len(renames),
        "similar_renames": len(similar_renames),
        "impl_only_modified": len(impl_only_modified),
        "contract_or_unknown_modified": len(contract_or_unknown_modified),
        "added": len(added_scaffolds),
        "missing_in_code": len(missing_in_code),
        "deleted": len(deleted),
        "invalid_entries": len(invalid_entries),
        "malformed_lines": len(malformed_lines),
    }

    return {
        "ok": True,
        "root": str(root),
        "has_drift": bool(drift.get("has_drift", False)),
        "elapsed_ms": drift.get("elapsed_ms", 0.0),
        "counts": counts,
        "restorable_from_pool": restorable,
        "renames": renames,
        "similar_renames": similar_renames,
        "impl_only_modified": impl_only_modified,
        "contract_or_unknown_modified": contract_or_unknown_modified,
        "added_scaffolds": added_scaffolds,
        "missing_in_code": missing_in_code,
        "deleted": deleted,
        "invalid_entries": invalid_entries,
        "malformed_lines": malformed_lines,
        "affected_neighbors": affected_neighbors,
        "recommended_actions": recommended_actions,
    }
