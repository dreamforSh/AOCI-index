"""Shared `query` engine behind the CLI `query` subcommand and the MCP `aoci_query` tool (identical payloads)."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from . import freshness as _freshness
from .constants import _parse_code_filter_set, normalize_relpath
from .graph import ParsedEntry, bfs_neighbors
from .test_impact import find_impacted_tests


def run_query(
    root: Path,
    entries: Dict[str, str],
    parsed: Mapping[str, ParsedEntry],
    *,
    get_dependents: Callable[[], Dict[str, Set[str]]],
    get_retriever: Callable[[bool], Any],
    glossary: Sequence[Sequence[str]] = (),
    layer: Optional[str] = None,
    domain: Optional[str] = None,
    min_importance: Optional[int] = None,
    path_glob: Optional[str] = None,
    keyword: Optional[str] = None,
    search: Optional[str] = None,
    top: Optional[int] = None,
    impact: Optional[str] = None,
    tests_for: Optional[str] = None,
    depth: Optional[int] = None,
    live_test_imports: bool = False,
    hybrid: bool = False,
    test_index: Optional[Any] = None,
    preloaded_outline_files: Optional[Dict[str, Any]] = None,
    baseline_files: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Filter (`layer`, `domain`, `min_importance`, `path_glob`, `keyword`, `impact` dependents, `tests_for`
    impacted tests) and optionally rank (`search`, BM25 or `hybrid`) FRAS entries.

    Ranking always runs over the full index with the filter as a candidate set, so scores (IDF) never
    depend on how narrow the filter is. `get_dependents` / `get_retriever(hybrid)` are lazy so callers
    only build the graph or ranking index when needed (the MCP server passes its warm caches).
    Returns `{count, entries[, scores, glossary_expansions, impacted_tests, retrieval, stale_entries]}`.
    """
    layer_set = _parse_code_filter_set(layer)
    domain_set = _parse_code_filter_set(domain)
    min_imp = int(min_importance or 0)
    keyword_l = str(keyword).lower() if keyword else None
    top_k = (20 if search else 0) if top is None else int(top)

    impact_set: Optional[Set[str]] = None
    if impact:
        target = normalize_relpath(str(impact))
        hops = int(depth) if depth is not None else 1
        impact_set = {target} | set(bfs_neighbors(get_dependents(), [target], hops))

    impacted_tests: Optional[List[Dict[str, Any]]] = None
    tests_set: Optional[Set[str]] = None
    if tests_for:
        target = normalize_relpath(str(tests_for))
        hops = int(depth) if depth is not None else 3
        impacted_tests = find_impacted_tests(
            root,
            entries,
            get_dependents(),
            [target],
            max_depth=hops,
            parsed_entries=parsed,
            preloaded_outline_files=preloaded_outline_files if live_test_imports else None,
            live_imports=bool(live_test_imports),
            test_index=None if live_test_imports else test_index,
        )
        tests_set = {item["path"] for item in impacted_tests}

    has_filters = any(
        (
            layer_set is not None,
            domain_set is not None,
            min_imp,
            path_glob,
            keyword_l,
            impact_set is not None,
            tests_set is not None,
        )
    )
    candidates: Optional[Set[str]] = None
    lines: List[str] = []
    if has_filters or not search:
        candidates = set()
        for rel, pe in sorted(parsed.items()):
            if layer_set is not None and pe.layer not in layer_set:
                continue
            if domain_set is not None and pe.domain not in domain_set:
                continue
            if min_imp and pe.imp < min_imp:
                continue
            if path_glob and not fnmatch.fnmatchcase(rel, str(path_glob)):
                continue
            if keyword_l and keyword_l not in pe.raw.lower():
                continue
            if impact_set is not None and rel not in impact_set:
                continue
            if tests_set is not None and rel not in tests_set:
                continue
            candidates.add(rel)
            lines.append(pe.raw)

    if impacted_tests is not None:
        impacted_tests = [item for item in impacted_tests if candidates is None or item["path"] in candidates]
        if not search:
            lines = [item["entry"] for item in impacted_tests]

    payload: Dict[str, Any]
    retrieval: Optional[Dict[str, Any]] = None
    if search:
        retriever = get_retriever(bool(hybrid))
        describe = getattr(retriever, "describe", None)
        if callable(describe):
            retrieval = describe()
        if hasattr(retriever, "search_detailed"):
            hits, expansions = retriever.search_detailed(
                str(search), candidate_paths=candidates, top_k=top_k, glossary=glossary
            )
        else:
            ranked = retriever.search(str(search), candidate_paths=candidates, top_k=top_k)
            hits = [{"path": r, "score": round(sc, 3), "matched_terms": [], "entry": ln} for sc, r, ln in ranked]
            expansions = []
        payload = {"count": len(hits), "entries": [h["entry"] for h in hits], "scores": hits}
        if expansions:
            payload["glossary_expansions"] = expansions
        if impacted_tests is not None:
            hit_paths = {h["path"] for h in hits}
            impacted_tests = [item for item in impacted_tests if item["path"] in hit_paths]
        result_paths = [str(h["path"]) for h in hits]
    else:
        if top_k > 0:
            lines = lines[:top_k]
            if impacted_tests is not None:
                impacted_tests = impacted_tests[:top_k]
        payload = {"count": len(lines), "entries": lines}
        result_paths = [normalize_relpath(ln.split("[", 1)[0].strip()) for ln in lines]

    if impacted_tests is not None:
        payload["impacted_tests"] = impacted_tests
    if retrieval is not None:
        payload["retrieval"] = retrieval
    stale = _freshness.check_freshness(root, result_paths, baseline_files)
    if stale:
        payload["stale_entries"] = _freshness.stale_list(stale)
    return payload


def stale_map(payload: Mapping[str, Any]) -> Dict[str, str]:
    """`{path: reason}` view of a payload's `stale_entries` (for text renderers)."""
    return {str(s["path"]): str(s["reason"]) for s in payload.get("stale_entries") or ()}
