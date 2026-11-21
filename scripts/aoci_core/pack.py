"""Task-driven, token-budgeted context packer (`aoci pack` / MCP `aoci_pack`)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .constants import (
    AOCI_DIR_NAME,
    CACHE_DIR,
    OUTLINE_CACHE,
    _is_within_root,
    normalize_relpath,
)
from .graph import (
    BM25Index,
    ParsedEntry,
    bfs_neighbors_with_distance,
    estimate_tokens,
    parse_entries_map,
    parse_entry_fields,
)
from .hubs import lookup_hub_sections
from .parsers import extract_file_outline
from .test_impact import find_impacted_tests, is_outline_record_fresh

_SLASH_PATH_TOK_RE = re.compile(r"[A-Za-z0-9_.$@\[\]()~+-]+(?:/[A-Za-z0-9_.$@\[\]()~+-]+)+")
_ANY_PATH_TOK_RE = re.compile(r"[A-Za-z0-9_.$@\[\]()~+-]+(?:/[A-Za-z0-9_.$@\[\]()~+-]+)*")
_ASCII_TOKEN_SAFE_RE = re.compile(r"^[A-Za-z0-9_.$@\[\]()~+-]+(?:/[A-Za-z0-9_.$@\[\]()~+-]+)*$")
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


class PathMentionIndex:
    """Pre-indexed full path, suffix, and basename lookup for O(|task|) task-mention matching."""

    __slots__ = ("known_sorted", "known_set", "by_basename", "by_suffix", "non_ascii_paths")

    def __init__(self, known_sorted: Sequence[str]) -> None:
        self.known_sorted: Tuple[str, ...] = tuple(known_sorted)
        self.known_set: Set[str] = set(self.known_sorted)
        self.by_basename: Dict[str, List[str]] = {}
        self.by_suffix: Dict[str, List[str]] = {}
        non_ascii: List[str] = []
        for rel in self.known_sorted:
            if not _ASCII_TOKEN_SAFE_RE.match(rel):
                non_ascii.append(rel)
            bname = rel.rsplit("/", 1)[-1]
            if "." in bname or bname in _SPECIAL_ROOT_FILES:
                self.by_basename.setdefault(bname, []).append(rel)
            parts = rel.split("/")
            if len(parts) > 1:
                for i in range(1, len(parts)):
                    suf = "/".join(parts[i:])
                    self.by_suffix.setdefault(suf, []).append(rel)
        self.non_ascii_paths: Tuple[str, ...] = tuple(non_ascii)


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


def _extract_task_path_mentions(
    task: str,
    known_sorted: Sequence[str],
    mention_index: Optional[PathMentionIndex] = None,
) -> List[str]:
    """Find explicit relative path, suffix path, or basename mentions from `task` present in `known_sorted`."""
    if not task or not known_sorted:
        return []
    idx = mention_index if mention_index is not None else PathMentionIndex(known_sorted)
    norm_task = task.replace("\\", "/")
    full_hits: List[str] = []
    suffix_hits: List[str] = []
    base_hits: List[str] = []
    seen_full: Set[str] = set()
    seen_suf: Set[str] = set()
    seen_base: Set[str] = set()

    def _candidate_spans(text: str, s: int, e: int) -> List[Tuple[int, int, str]]:
        out: List[Tuple[int, int, str]] = []
        raw_tok = text[s:e]
        variants = [(s, e, raw_tok)]
        stripped_tok = raw_tok
        ss, ee = s, e
        while stripped_tok and stripped_tok[0] in "([":
            ss += 1
            stripped_tok = stripped_tok[1:]
        while stripped_tok and stripped_tok[-1] in ")]":
            ee -= 1
            stripped_tok = stripped_tok[:-1]
        if ss < ee and (ss != s or ee != e):
            variants.append((ss, ee, stripped_tok))
        for vs, ve, vtok in variants:
            out.append((vs, ve, vtok))
            rtok = vtok.rstrip(".")
            if rtok and len(rtok) != len(vtok):
                out.append((vs, vs + len(rtok), rtok))
        return out

    slash_tokens: List[str] = []
    single_tokens: List[str] = []
    for m in _ANY_PATH_TOK_RE.finditer(norm_task):
        m_s, m_e = m.span()
        for cs, ce, ctok in _candidate_spans(norm_task, m_s, m_e):
            if not ctok or not _has_path_boundary(norm_task, cs, ce):
                continue
            norm_tok = normalize_relpath(ctok)
            if not norm_tok:
                continue
            if "/" in norm_tok:
                if norm_tok in idx.known_set and norm_tok not in seen_full:
                    seen_full.add(norm_tok)
                    full_hits.append(norm_tok)
                elif norm_tok in idx.by_suffix:
                    slash_tokens.append(norm_tok)
            else:
                if norm_tok in idx.known_set and norm_tok not in seen_full:
                    seen_full.add(norm_tok)
                    full_hits.append(norm_tok)
                if norm_tok in idx.by_basename:
                    single_tokens.append(norm_tok)

    for rel in idx.non_ascii_paths:
        if rel not in seen_full and rel in norm_task and _find_bounded_substring(norm_task, rel):
            seen_full.add(rel)
            full_hits.append(rel)

    for stok in slash_tokens:
        for rel in idx.by_suffix.get(stok, ()):
            if rel not in seen_full and rel not in seen_suf:
                seen_suf.add(rel)
                suffix_hits.append(rel)

    for btok in single_tokens:
        for rel in idx.by_basename.get(btok, ()):
            if rel not in seen_full and rel not in seen_suf and rel not in seen_base:
                seen_base.add(rel)
                base_hits.append(rel)

    full_hits.sort()
    matched: List[str] = list(full_hits)
    matched.extend(suffix_hits)
    base_hits.sort()
    matched.extend(base_hits)
    return matched


def _read_outline_anchors(
    root: Path,
    limit: int = 5,
    *,
    wanted_paths: Optional[Set[str]] = None,
    cached_files: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Opportunistically read up to `limit` symbol `[L...]` anchors per file from outline cache."""
    files_obj: Optional[Dict[str, Any]] = cached_files
    if files_obj is None:
        cache_path = root / AOCI_DIR_NAME / CACHE_DIR / OUTLINE_CACHE
        if not cache_path.is_file():
            return {}
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
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
        raw_files = loaded.get("files") if isinstance(loaded.get("files"), dict) else loaded
        if not isinstance(raw_files, dict):
            return {}
        files_obj = raw_files

    anchors: Dict[str, List[str]] = {}
    if wanted_paths is not None and len(wanted_paths) <= len(files_obj):
        iter_items: List[Tuple[str, Any]] = []
        for rel in wanted_paths:
            rec = files_obj.get(rel)
            if rec is not None:
                iter_items.append((rel, rec))
    else:
        iter_items = list(files_obj.items())

    for raw_rel, rec in iter_items:
        if not isinstance(rec, dict):
            continue
        rel_norm = normalize_relpath(str(raw_rel))
        if wanted_paths is not None and rel_norm not in wanted_paths:
            continue
        if not is_outline_record_fresh(root, rel_norm, rec):
            continue
        out_obj = rec.get("outline") if isinstance(rec.get("outline"), dict) else rec
        syms = out_obj.get("symbols") if isinstance(out_obj, dict) else None
        if isinstance(syms, list):
            cleaned = [str(s).strip() for s in syms if isinstance(s, str) and s.strip()][:limit]
            if cleaned:
                anchors[rel_norm] = cleaned
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
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
    mention_index: Optional[PathMentionIndex] = None,
    preloaded_outline_files: Optional[Dict[str, Any]] = None,
    hub_records: Optional[Sequence[Dict[str, Any]]] = None,
    live_anchors: bool = False,
    with_tests: bool = False,
    live_test_imports: bool = False,
) -> Dict[str, Any]:
    """
    Assemble a task-driven, token-budgeted context packet from FRAS index entries,
    bilingual glossary expansion, explicit path mentions, optional Git diff signals,
    L1.5 hub sections, impacted tests, and cached or live outline symbol anchors.
    """
    _ = domains  # Kept in public signature for callers passing full index slot context
    parsed_map: Mapping[str, ParsedEntry] = (
        parsed_entries if parsed_entries is not None else parse_entries_map(entries)
    )

    def _entry_stats(rel: str) -> Tuple[int, bool]:
        pe = parsed_map.get(rel)
        if pe is None:
            return 0, False
        return pe.imp, pe.s not in ("-", "")

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
        idx = bm25_index if bm25_index is not None else BM25Index(entries, parsed_entries=parsed_map)
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

    known_sorted = mention_index.known_sorted if mention_index is not None else sorted(entries.keys())
    for rel in _extract_task_path_mentions(task, known_sorted, mention_index=mention_index):
        _touch_seed(rel, "path-mention")

    unindexed_changed: List[str] = []
    if diff_brief and diff_brief.get("ok", True):
        for ln in diff_brief.get("changed_entries") or ():
            if not isinstance(ln, str):
                continue
            pe_diff = parse_entry_fields(ln)
            rel = normalize_relpath(pe_diff["path"]) if pe_diff else normalize_relpath(ln.split("[", 1)[0].strip())
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
        truncated = bool(omitted_paths or used_tokens > budget)

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
                    "s": parsed_map[rel].s,
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
                    "s": parsed_map[rel].s,
                    "role": "dependency",
                }
            )
    critical_constraints.sort(
        key=lambda item: (-int(item["importance"]), 0 if item["role"] == "seed" else 1, str(item["path"]))
    )

    # e) Build open_next with `[L...]` anchors (from warm cache, hub sections, or opt-in live_anchors)
    open_plan: List[Tuple[str, str, List[str], Optional[int]]] = []
    open_seen: Set[str] = set()

    for rel in packed_seeds:
        if rel in open_seen:
            continue
        open_seen.add(rel)
        imp, _ = _entry_stats(rel)
        reasons_list = sorted(seed_reasons[rel], key=lambda r: (_REASON_ORDER.get(r, 9), r))
        open_plan.append((rel, "seed", reasons_list, imp))

    for rel in unindexed_changed:
        if rel in open_seen:
            continue
        open_seen.add(rel)
        open_plan.append((rel, "unindexed_changed", ["diff", "unindexed"], None))

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
        open_plan.append((rel, "dependency", ["1-hop-dep"], imp))

    wanted_open_paths = {item[0] for item in open_plan}
    outline_anchors = _read_outline_anchors(
        root,
        limit=5,
        wanted_paths=wanted_open_paths,
        cached_files=preloaded_outline_files,
    )
    aoci_dir = root / AOCI_DIR_NAME
    hub_sections = lookup_hub_sections(
        aoci_dir,
        [item[0] for item in open_plan],
        preloaded_records=hub_records,
    )
    hub_syms_by_path: Dict[str, List[str]] = {}
    for sec in hub_sections:
        p = str(sec.get("path", ""))
        sym = str(sec.get("symbol", "")).strip()
        span = f"[L{sec['start']}-L{sec['end']}]" if ("start" in sec and "end" in sec) else ""
        label = f"{sym} {span}".strip() if (sym and span) else (sym or span)
        if p and label and len(hub_syms_by_path.setdefault(p, [])) < 5:
            hub_syms_by_path[p].append(label)

    open_next: List[Dict[str, Any]] = []
    for rel, kind, reasons_list, imp in open_plan:
        syms = outline_anchors.get(rel) or []
        if not syms and rel in hub_syms_by_path:
            syms = hub_syms_by_path[rel]
        if not syms and live_anchors:
            full_p = root / rel
            if _is_within_root(root, full_p) and not full_p.is_symlink() and full_p.is_file():
                try:
                    live = extract_file_outline(full_p, rel, max_items=5)
                except OSError:
                    live = None
                if isinstance(live, dict):
                    raw_syms = live.get("symbols") or []
                    syms = [str(s).strip() for s in raw_syms if isinstance(s, str) and s.strip()][:5]
        open_next.append(
            {
                "path": rel,
                "kind": kind,
                "reasons": reasons_list,
                "importance": imp,
                "symbols": syms,
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

    hub_sections_omitted = 0
    if hub_sections:
        if budget > 0:
            seed_path_set = set(packed_seeds)
            ranked_hubs = [
                sec
                for _, sec in sorted(
                    enumerate(hub_sections),
                    key=lambda idx_sec: (0 if str(idx_sec[1].get("path", "")) in seed_path_set else 1, idx_sec[0]),
                )
            ]
            kept_hubs: List[Dict[str, Any]] = []
            for sec in ranked_hubs:
                cost = estimate_tokens(str(sec.get("entry", "")))
                if used_tokens + cost <= budget:
                    used_tokens += cost
                    kept_hubs.append(sec)
                else:
                    hub_sections_omitted += 1
            hub_sections = kept_hubs
            if hub_sections_omitted > 0:
                truncated = True
        else:
            for sec in hub_sections:
                used_tokens += estimate_tokens(str(sec.get("entry", "")))

    impacted_tests: Optional[List[Dict[str, Any]]] = None
    impacted_tests_omitted = 0
    if with_tests:
        impact_targets = list(dict.fromkeys([*packed_seeds, *unindexed_changed]))
        if impact_targets:
            raw_tests = find_impacted_tests(
                root,
                entries,
                dependents_adj,
                impact_targets,
                max_depth=max(3, hops),
                parsed_entries=parsed_map,
                preloaded_outline_files=preloaded_outline_files,
                live_imports=live_test_imports,
            )
        else:
            raw_tests = []
        if budget > 0:
            kept_tests: List[Dict[str, Any]] = []
            for t_item in raw_tests:
                cost = estimate_tokens(str(t_item.get("entry", "")))
                if used_tokens + cost <= budget:
                    used_tokens += cost
                    kept_tests.append(t_item)
                else:
                    impacted_tests_omitted += 1
            impacted_tests = kept_tests
            if impacted_tests_omitted > 0:
                truncated = True
        else:
            for t_item in raw_tests:
                used_tokens += estimate_tokens(str(t_item.get("entry", "")))
            impacted_tests = raw_tests

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
    if hub_sections:
        payload["hub_sections"] = hub_sections
    if hub_sections_omitted > 0:
        payload["hub_sections_omitted"] = hub_sections_omitted
    if impacted_tests is not None:
        payload["impacted_tests"] = impacted_tests
    if impacted_tests_omitted > 0:
        payload["impacted_tests_omitted"] = impacted_tests_omitted
    return payload
