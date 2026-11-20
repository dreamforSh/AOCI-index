"""Task-driven, token-budgeted context packer (`aoci pack` / MCP `aoci_pack`)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .constants import (
    AOCI_DIR_NAME,
    CACHE_DIR,
    OUTLINE_CACHE,
    normalize_relpath,
)
from .graph import (
    BM25Index,
    bfs_neighbors_with_distance,
    estimate_tokens,
    parse_entry_fields,
)

_SLASH_PATH_TOK_RE = re.compile(r"[A-Za-z0-9_.$@\[\]()~+-]+(?:/[A-Za-z0-9_.$@\[\]()~+-]+)+")
_SPECIAL_ROOT_FILES: Set[str] = {
    "Makefile",
    "Dockerfile",
    "Jenkinsfile",
    "Procfile",
    "Vagrantfile",
    "Gemfile",
    "Rakefile",
}
_REASON_ORDER: Dict[str, int] = {"path-mention": 0, "diff": 1, "bm25": 2}


def _has_path_boundary(text: str, start: int, end: int) -> bool:
    """Confirm a candidate path/basename span in `text` is not embedded inside a larger path or identifier."""
    prev_ch = text[start - 1] if start > 0 else ""
    next_ch = text[end] if end < len(text) else ""
    after_next = text[end + 1] if end + 1 < len(text) else ""
    if prev_ch in ("/", "\\"):
        return False
    if prev_ch and prev_ch.isascii() and (prev_ch.isalnum() or prev_ch in "_.-"):
        return False
    if next_ch in ("/", "\\"):
        return False
    if next_ch and next_ch.isascii() and (next_ch.isalnum() or next_ch in "_-"):
        return False
    if next_ch == "." and after_next and after_next.isascii() and (after_next.isalnum() or after_next == "_"):
        return False
    return True


def _find_bounded_substring(text: str, needle: str) -> bool:
    if not text or not needle:
        return False
    n_len = len(needle)
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx == -1:
            return False
        if _has_path_boundary(text, idx, idx + n_len):
            return True
        pos = idx + 1


def _extract_task_path_mentions(task: str, known_sorted: Sequence[str]) -> List[str]:
    """Find explicit relative path, suffix path, or basename mentions from `task` present in `known_sorted`."""
    if not task or not known_sorted:
        return []
    norm_task = task.replace("\\", "/")
    known_set = set(known_sorted)
    matched: List[str] = []
    seen: Set[str] = set()

    def _add(rel: str) -> None:
        if rel not in seen:
            seen.add(rel)
            matched.append(rel)

    for rel in known_sorted:
        if rel in norm_task and _find_bounded_substring(norm_task, rel):
            _add(rel)

    for m in _SLASH_PATH_TOK_RE.finditer(norm_task):
        tok = normalize_relpath(m.group(0).rstrip("."))
        if not tok or "/" not in tok or tok in known_set:
            continue
        if not _has_path_boundary(norm_task, m.start(), m.start() + len(tok)):
            continue
        suffix = "/" + tok
        for rel in known_sorted:
            if rel.endswith(suffix):
                _add(rel)

    by_basename: Dict[str, List[str]] = {}
    for rel in known_sorted:
        bname = rel.rsplit("/", 1)[-1]
        if "." in bname or bname in _SPECIAL_ROOT_FILES:
            by_basename.setdefault(bname, []).append(rel)
    for bname, rels in by_basename.items():
        if bname in norm_task and _find_bounded_substring(norm_task, bname):
            for rel in rels:
                _add(rel)

    return matched


def _read_outline_anchors(root: Path, limit: int = 5) -> Dict[str, List[str]]:
    """Opportunistically read up to `limit` symbol `[L...]` anchors per file from `.aoci/.cache/outline_cache.json`."""
    cache_path = root / AOCI_DIR_NAME / CACHE_DIR / OUTLINE_CACHE
    if not cache_path.is_file():
        return {}
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    sv = loaded.get("schema_version")
    if sv is not None:
        try:
            if int(sv) != 2:
                return {}
        except (ValueError, TypeError):
            return {}
    files_obj = loaded.get("files") if isinstance(loaded.get("files"), dict) else loaded
    if not isinstance(files_obj, dict):
        return {}

    anchors: Dict[str, List[str]] = {}
    for raw_rel, rec in files_obj.items():
        if not isinstance(rec, dict):
            continue
        out_obj = rec.get("outline") if isinstance(rec.get("outline"), dict) else rec
        syms = out_obj.get("symbols") if isinstance(out_obj, dict) else None
        if isinstance(syms, list):
            cleaned = [str(s).strip() for s in syms if isinstance(s, str) and s.strip()][:limit]
            if cleaned:
                anchors[normalize_relpath(str(raw_rel))] = cleaned
    return anchors


def build_task_pack(
    root: Path,
    entries: Dict[str, str],
    deps_adj: Dict[str, Set[str]],
    dependents_adj: Dict[str, Set[str]],
    invariants: str,
    domains: Dict[str, str],
    glossary: Sequence[Sequence[str]],
    *,
    task: str,
    diff_brief: Optional[Dict[str, Any]] = None,
    depth: int = 1,
    budget: int = 2500,
    top_seeds: int = 8,
    bm25_index: Optional[BM25Index] = None,
) -> Dict[str, Any]:
    """
    Assemble a task-driven, token-budgeted context packet from FRAS index entries,
    bilingual glossary expansion, explicit path mentions, optional Git diff signals,
    and cached outline symbol anchors.
    """
    _ = domains  # Kept in public signature for callers passing full index slot context
    parsed_fields: Dict[str, Dict[str, str]] = {}
    for rel, line in entries.items():
        fields = parse_entry_fields(line)
        if fields:
            parsed_fields[rel] = fields

    def _entry_stats(rel: str) -> Tuple[int, bool]:
        fields = parsed_fields.get(rel)
        if not fields:
            return 0, False
        return int(fields["imp"]), fields["s"] not in ("-", "")

    # a) Seed selection: BM25 + glossary, explicit path/basename mentions, and diff_brief
    seed_reasons: Dict[str, Set[str]] = {}
    seed_scores: Dict[str, float] = {}
    seed_terms: Dict[str, List[str]] = {}
    seed_glossary: Dict[str, List[str]] = {}
    glossary_expansions: List[Dict[str, Any]] = []

    def _touch_seed(rel: str, reason: str) -> None:
        if rel not in entries:
            return
        seed_reasons.setdefault(rel, set()).add(reason)
        seed_scores.setdefault(rel, 0.0)

    if entries and task.strip():
        idx = bm25_index if bm25_index is not None else BM25Index(entries)
        k_seeds = max(1, int(top_seeds))
        if hasattr(idx, "search_detailed"):
            bm25_hits, glossary_expansions = idx.search_detailed(
                task, top_k=k_seeds, glossary=glossary or ()
            )
        else:
            ranked = idx.search(task, top_k=k_seeds)
            bm25_hits = [
                {"path": r, "score": round(sc, 3), "matched_terms": [], "entry": ln}
                for sc, r, ln in ranked
            ]
            glossary_expansions = []
        for hit in bm25_hits:
            rel = normalize_relpath(str(hit.get("path", "")))
            if rel not in entries:
                continue
            _touch_seed(rel, "bm25")
            seed_scores[rel] = max(seed_scores.get(rel, 0.0), float(hit.get("score", 0.0)))
            if hit.get("matched_terms"):
                seed_terms[rel] = list(hit["matched_terms"])
            if hit.get("matched_glossary"):
                seed_glossary[rel] = list(hit["matched_glossary"])

    known_sorted = sorted(entries.keys())
    for rel in _extract_task_path_mentions(task, known_sorted):
        _touch_seed(rel, "path-mention")

    unindexed_changed: List[str] = []
    if diff_brief and diff_brief.get("ok", True):
        for ln in diff_brief.get("changed_entries") or ():
            if not isinstance(ln, str):
                continue
            fields = parse_entry_fields(ln)
            rel = normalize_relpath(fields["path"]) if fields else normalize_relpath(ln.split("[", 1)[0].strip())
            if rel in entries:
                _touch_seed(rel, "diff")
        seen_unidx: Set[str] = set()
        for raw_u in diff_brief.get("unindexed_changed") or ():
            u_rel = normalize_relpath(str(raw_u))
            if u_rel and u_rel not in seen_unidx:
                seen_unidx.add(u_rel)
                unindexed_changed.append(u_rel)

    def _seed_rank_key(rel: str) -> Tuple[int, int, int, float, int, int, int, str]:
        rs = seed_reasons[rel]
        sc = seed_scores.get(rel, 0.0)
        imp, has_s = _entry_stats(rel)
        return (
            0 if "path-mention" in rs else 1,
            0 if (sc > 0.0 and "diff" in rs) else 1,
            0 if sc > 0.0 else 1,
            -sc,
            0 if "diff" in rs else 1,
            -imp,
            0 if has_s else 1,
            rel,
        )

    ordered_seeds = sorted(seed_reasons.keys(), key=_seed_rank_key)
    seed_set = set(ordered_seeds)

    # b) Subgraph expansion: BFS `depth` hops along R: (deps_adj) and A: (dependents_adj)
    hops = max(0, int(depth))
    if hops > 0 and ordered_seeds:
        dep_dist = {
            p: d
            for p, d in bfs_neighbors_with_distance(deps_adj, ordered_seeds, hops).items()
            if p not in seed_set and p in entries
        }
        dep_set = set(dep_dist)
        caller_dist = {
            p: d
            for p, d in bfs_neighbors_with_distance(dependents_adj, ordered_seeds, hops).items()
            if p not in seed_set and p not in dep_set and p in entries
        }
    else:
        dep_dist = {}
        caller_dist = {}

    # c) Budgeted packing
    used_tokens = estimate_tokens(invariants)
    packed_seeds: List[str] = []
    packed_deps: List[str] = []
    packed_callers: List[str] = []
    omitted_paths: List[str] = []

    if budget <= 0:
        packed_seeds = list(ordered_seeds)
        packed_deps = list(dep_dist.keys())
        packed_callers = list(caller_dist.keys())
        for rel in (*packed_seeds, *packed_deps, *packed_callers):
            used_tokens += estimate_tokens(entries[rel])
        truncated = False
    else:
        reserved_seed_set: Set[str] = set()
        if ordered_seeds:
            top_rel = ordered_seeds[0]
            packed_seeds.append(top_rel)
            reserved_seed_set.add(top_rel)
            used_tokens += estimate_tokens(entries[top_rel])
            for rel in ordered_seeds[1:]:
                rs = seed_reasons[rel]
                if "path-mention" in rs or "diff" in rs:
                    cost = estimate_tokens(entries[rel])
                    if used_tokens + cost <= budget:
                        packed_seeds.append(rel)
                        reserved_seed_set.add(rel)
                        used_tokens += cost

        candidates: List[Tuple[int, int, int, int, float, str, str]] = []
        for rel in ordered_seeds:
            if rel in reserved_seed_set:
                continue
            imp, has_s = _entry_stats(rel)
            sc = seed_scores.get(rel, 0.0)
            candidates.append((0, 0, -imp, 0 if has_s else 1, -sc, rel, "seed"))
        for rel, hop in dep_dist.items():
            imp, has_s = _entry_stats(rel)
            candidates.append((1, hop, -imp, 0 if has_s else 1, 0.0, rel, "dep"))
        for rel, hop in caller_dist.items():
            imp, has_s = _entry_stats(rel)
            candidates.append((1, hop, -imp, 0 if has_s else 1, 0.0, rel, "caller"))

        candidates.sort()
        for _, _, _, _, _, rel, role_kind in candidates:
            cost = estimate_tokens(entries[rel])
            if used_tokens + cost <= budget:
                used_tokens += cost
                if role_kind == "seed":
                    packed_seeds.append(rel)
                elif role_kind == "dep":
                    packed_deps.append(rel)
                else:
                    packed_callers.append(rel)
            else:
                omitted_paths.append(rel)

        omitted_paths.sort()
        truncated = bool(omitted_paths)

    packed_seeds.sort(key=_seed_rank_key)
    packed_deps.sort(key=lambda rel: (dep_dist[rel], -_entry_stats(rel)[0], 0 if _entry_stats(rel)[1] else 1, rel))
    packed_callers.sort(
        key=lambda rel: (caller_dist[rel], -_entry_stats(rel)[0], 0 if _entry_stats(rel)[1] else 1, rel)
    )

    # d) Collect critical_constraints (`S:` not in ("-", "")) from packed seeds and 1-hop R: dependencies
    critical_constraints: List[Dict[str, Any]] = []
    for rel in packed_seeds:
        imp, has_s = _entry_stats(rel)
        if has_s:
            critical_constraints.append(
                {
                    "path": rel,
                    "importance": imp,
                    "s": parsed_fields[rel]["s"],
                    "role": "seed",
                }
            )
    for rel in packed_deps:
        if dep_dist.get(rel) != 1:
            continue
        imp, has_s = _entry_stats(rel)
        if has_s:
            critical_constraints.append(
                {
                    "path": rel,
                    "importance": imp,
                    "s": parsed_fields[rel]["s"],
                    "role": "dependency",
                }
            )
    critical_constraints.sort(
        key=lambda item: (-int(item["importance"]), 0 if item["role"] == "seed" else 1, str(item["path"]))
    )

    # e) Build open_next with opportunistic `[L...]` anchors from `.aoci/.cache/outline_cache.json`
    outline_anchors = _read_outline_anchors(root, limit=5)
    open_next: List[Dict[str, Any]] = []
    open_seen: Set[str] = set()

    for rel in packed_seeds:
        if rel in open_seen:
            continue
        open_seen.add(rel)
        imp, _ = _entry_stats(rel)
        reasons_list = sorted(seed_reasons[rel], key=lambda r: (_REASON_ORDER.get(r, 9), r))
        open_next.append(
            {
                "path": rel,
                "kind": "seed",
                "reasons": reasons_list,
                "importance": imp,
                "symbols": outline_anchors.get(rel, []),
            }
        )

    for rel in unindexed_changed:
        if rel in open_seen:
            continue
        open_seen.add(rel)
        open_next.append(
            {
                "path": rel,
                "kind": "unindexed_changed",
                "reasons": ["diff", "unindexed"],
                "importance": None,
                "symbols": outline_anchors.get(rel, []),
            }
        )

    high_imp_deps: List[Tuple[int, int, str]] = []
    for rel in packed_deps:
        if rel in open_seen or dep_dist.get(rel) != 1:
            continue
        imp, has_s = _entry_stats(rel)
        if imp >= 7:
            high_imp_deps.append((-imp, 0 if has_s else 1, rel))
    high_imp_deps.sort()
    for _, _, rel in high_imp_deps[: max(5, int(top_seeds))]:
        open_seen.add(rel)
        imp, _ = _entry_stats(rel)
        open_next.append(
            {
                "path": rel,
                "kind": "dependency",
                "reasons": ["1-hop-dep"],
                "importance": imp,
                "symbols": outline_anchors.get(rel, []),
            }
        )

    # f) Assemble result packet
    seeds_out: List[Dict[str, Any]] = []
    for rel in packed_seeds:
        imp, _ = _entry_stats(rel)
        reasons_list = sorted(seed_reasons[rel], key=lambda r: (_REASON_ORDER.get(r, 9), r))
        seed_obj: Dict[str, Any] = {
            "path": rel,
            "reasons": reasons_list,
            "score": round(seed_scores.get(rel, 0.0), 3),
            "importance": imp,
            "entry": entries[rel],
        }
        if seed_terms.get(rel):
            seed_obj["matched_terms"] = seed_terms[rel]
        if seed_glossary.get(rel):
            seed_obj["matched_glossary"] = seed_glossary[rel]
        seeds_out.append(seed_obj)

    payload: Dict[str, Any] = {
        "ok": True,
        "root": str(root),
        "task": task,
        "global_invariants": invariants,
        "seeds": seeds_out,
        "critical_constraints": critical_constraints,
        "dependencies": [entries[p] for p in packed_deps],
        "callers": [entries[p] for p in packed_callers],
        "unindexed_changed": unindexed_changed,
        "open_next": open_next,
        "estimated_tokens": used_tokens,
        "budget": budget,
        "truncated": truncated,
        "omitted_count": len(omitted_paths),
        "omitted_paths": omitted_paths,
    }
    if glossary_expansions:
        payload["glossary_expansions"] = glossary_expansions
    return payload
