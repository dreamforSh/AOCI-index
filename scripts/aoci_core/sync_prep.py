"""Single-pass agent synchronization briefing (`aoci prepare-sync` / MCP `aoci_prepare_sync`)."""

from __future__ import annotations

import shlex
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


def _truncate_diff_lines(lines: List[str], max_diff_lines: int) -> str:
    if len(lines) <= max_diff_lines:
        return "\n".join(lines)
    kept = lines[:max_diff_lines]
    kept.append(f"... (+{len(lines) - max_diff_lines} truncated line(s))")
    return "\n".join(kept)


def _extract_diff_section_rel(sec_lines: Sequence[str], wanted: Set[str]) -> Optional[str]:
    for ln in sec_lines[1:8]:
        if ln.startswith("+++ "):
            rhs = ln[4:].strip()
            if rhs == "/dev/null":
                break
            if rhs.startswith('"') and rhs.endswith('"') and len(rhs) >= 2:
                rhs = rhs[1:-1]
            if rhs.startswith("b/"):
                cand = normalize_relpath(rhs[2:])
                if cand in wanted:
                    return cand
        elif ln.startswith("--- "):
            lhs = ln[4:].strip()
            if lhs == "/dev/null":
                continue
            if lhs.startswith('"') and lhs.endswith('"') and len(lhs) >= 2:
                lhs = lhs[1:-1]
            if lhs.startswith("a/"):
                cand = normalize_relpath(lhs[2:])
                if cand in wanted:
                    return cand
    if sec_lines and sec_lines[0].startswith("diff --git "):
        body = sec_lines[0][11:]
        mid = len(body) // 2
        if len(body) % 2 == 1 and body[mid : mid + 1] == " ":
            lhs, rhs = body[:mid], body[mid + 1 :]
            if lhs.startswith("a/") and rhs.startswith("b/") and lhs[2:] == rhs[2:]:
                cand = normalize_relpath(rhs[2:])
                if cand in wanted:
                    return cand
        try:
            toks = shlex.split(body, posix=True)
            if len(toks) >= 2 and toks[-1].startswith("b/"):
                cand = normalize_relpath(toks[-1][2:])
                if cand in wanted:
                    return cand
        except ValueError:
            pass
    return None


def _batch_bounded_git_diff_previews(
    root: Path,
    rels: Sequence[str],
    git_info: Mapping[str, Any],
    run_git_fn: Any,
    max_diff_lines: int,
) -> Dict[str, str]:
    """Fetch bounded unified diffs for many modified files in batched `git diff` calls."""
    if not git_info.get("is_git") or max_diff_lines <= 0 or not rels:
        return {}
    previews: Dict[str, str] = {}
    unique_rels = list(dict.fromkeys(normalize_relpath(r) for r in rels if str(r).strip()))
    chunk_size = 150
    for idx in range(0, len(unique_rels), chunk_size):
        chunk = unique_rels[idx : idx + chunk_size]
        wanted = set(chunk)
        cp = run_git_fn(
            ["-c", "core.quotePath=false", "diff", "-U1", "--relative", "HEAD", "--", *chunk],
            root,
        )
        if cp.returncode != 0:
            cp = run_git_fn(
                ["-c", "core.quotePath=false", "diff", "-U1", "--relative", "--", *chunk],
                root,
            )
        if cp.returncode != 0 or not cp.stdout:
            continue
        sections: List[List[str]] = []
        cur: List[str] = []
        for ln in cp.stdout.splitlines():
            if ln.startswith("diff --git "):
                if cur:
                    sections.append(cur)
                cur = [ln]
            elif cur:
                cur.append(ln)
        if cur:
            sections.append(cur)
        for sec in sections:
            matched_rel = _extract_diff_section_rel(sec, wanted)
            if matched_rel is not None and matched_rel not in previews:
                previews[matched_rel] = _truncate_diff_lines(sec, max_diff_lines)
        if len(chunk) == 1 and chunk[0] not in previews and not sections and cp.stdout.strip():
            previews[chunk[0]] = _truncate_diff_lines(cp.stdout.splitlines(), max_diff_lines)
    return previews


def _bounded_git_diff_preview(
    root: Path,
    rel: str,
    git_info: Mapping[str, Any],
    run_git_fn: Any,
    max_diff_lines: int,
) -> str:
    norm = normalize_relpath(rel)
    return _batch_bounded_git_diff_previews(root, [norm], git_info, run_git_fn, max_diff_lines).get(norm, "")


def prepare_sync_briefing(
    root: Path,
    git_info: Dict[str, Any],
    drift: Dict[str, Any],
    *,
    scaffold_fn: Any,
    run_git_fn: Any,
    max_diff_lines: int = 40,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cached_entries: Optional[Mapping[str, str]] = None,
    cached_parsed: Optional[Mapping[str, Any]] = None,
    cached_dependents: Optional[Mapping[str, Set[str]]] = None,
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
    contract_rels: List[str] = []
    for item in drift.get("modified", ()):
        rel = normalize_relpath(str(item["path"]))
        if item.get("change_kind") == "impl_only":
            impl_only_modified.append(dict(item))
        else:
            enriched = dict(item)
            enriched["path"] = rel
            enriched["previous_entry"] = code_entries.get(rel)
            enriched["current_outline"] = _safe_outline(root, rel, max_bytes)
            contract_or_unknown_modified.append(enriched)
            contract_rels.append(rel)

    diff_previews = _batch_bounded_git_diff_previews(
        root, contract_rels, git_info, run_git_fn, max_diff_lines
    )
    for enriched in contract_or_unknown_modified:
        enriched["diff_preview"] = diff_previews.get(enriched["path"], "")

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
        cache_usable = (
            cached_parsed is not None
            and (
                (cached_entries is not None and cached_entries == code_entries)
                or (cached_entries is None and set(cached_parsed.keys()) == set(code_entries.keys()))
            )
        )
        parsed = cached_parsed if cache_usable else parse_entries_map(code_entries)
        if cache_usable and cached_dependents is not None:
            dependents = cached_dependents
        else:
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
