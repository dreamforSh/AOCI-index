"""Read-only CLI subcommands (`query`, `context`, `pack`, `overview`, `symbol`, `export`, `outline`) and shared helpers."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import audit as _audit
from . import diff_brief as _diff_brief
from . import git_ops as _git_ops
from . import graph as _graph
from . import hubs as _hubs
from . import ignore as _ignore
from . import pack as _pack
from . import shards as _shards
from . import symbols as _symbols
from . import test_impact as _test_impact
from .constants import (
    AOCI_DIR_NAME,
    CODE_FILE,
    DEFAULT_MAX_BYTES,
    ENTRY_RE,
    HUBS_FILE,
    _is_within_root,
    _parse_code_filter_set,
    _resolve_cli_file_path,
    normalize_relpath,
    write_text_lf,
)
from .graph import (
    bfs_neighbors,
    build_context_subgraph,
    build_fras_graph,
    estimate_tokens,
    export_dependency_graph,
    parse_entries_map,
    parse_glossary,
)
from .templates import parse_declared_domains, parse_manifest_field


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
        index_cls=_graph.BM25Index,
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
        index_cls=_graph.BM25Index,
        glossary=glossary,
        parsed_entries=parsed_entries,
    )


def _print_impacted_tests(impacted_tests: Sequence[Dict[str, Any]]) -> None:
    print(f"\n## Impacted Tests ({len(impacted_tests)})")
    for item in impacted_tests:
        print(f"  [{item['reason']}, hop={item['hop']}, via={item['via']}] {item['entry']}")


def _emit_cli_error(as_json: bool, msg: str, rc: int = 2, extra: Optional[Dict[str, Any]] = None) -> int:
    if as_json:
        payload: Dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
        payload["ok"] = False
        payload.setdefault("error", msg)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"ERROR: {msg}", file=sys.stderr)
    return rc


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
    if _ignore.is_probably_binary_bytes(head):
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
        code_entries, _ = _shards.parse_code_volume(code_path) if code_path.is_file() else ({}, [])
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


def cmd_query(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    code_path = root / AOCI_DIR_NAME / CODE_FILE
    if not code_path.exists():
        return _emit_cli_error(bool(getattr(args, "json", False)), f"`{code_path}` not found.", 2)

    entries, _ = _shards.parse_code_volume(code_path)
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

    has_filters = any(
        (
            layer_set is not None,
            domain_set is not None,
            args.min_importance,
            args.path,
            keyword,
            impact_set is not None,
            impacted_test_paths is not None,
        )
    )
    filtered_paths: Optional[Set[str]] = None
    results: List[str] = []
    if has_filters or not search_q:
        filtered_paths = set()
        for rel, line in sorted(entries.items()):
            pe = parsed_map.get(rel)
            if pe is None:
                continue
            if layer_set is not None and pe.layer not in layer_set:
                continue
            if domain_set is not None and pe.domain not in domain_set:
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
        impacted_tests = [
            item for item in impacted_tests if filtered_paths is None or item["path"] in filtered_paths
        ]
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
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return _emit_cli_error(
            bool(getattr(args, "json", False)),
            f"`{code_path}` not found. Run `init` first.",
            2,
        )

    entries, _ = _shards.parse_code_volume(code_path)
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
    raw_hub_secs = _hubs.lookup_hub_sections(aoci_dir, ctx_hub_paths)
    raw_tests: Optional[List[Dict[str, Any]]] = None
    if with_tests:
        raw_tests = _test_impact.find_impacted_tests(
            root,
            entries,
            dependents_adj,
            args.paths,
            max_depth=max(3, int(args.depth)),
            parsed_entries=parsed_map,
            live_imports=bool(getattr(args, "live_test_imports", False)),
        )
    _graph.attach_budgeted_context_extras(payload, raw_hub_secs, raw_tests, args.paths, budget)
    hub_secs = payload.get("hub_sections") or []

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
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return _emit_cli_error(
            bool(getattr(args, "json", False)),
            f"`{code_path}` not found. Run `init` first.",
            2,
        )

    raw_code_texts: List[str] = []
    entries, _ = _shards.parse_code_volume(code_path, raw_text_out=raw_code_texts)
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


def cmd_outline(args: argparse.Namespace) -> int:
    """Extract lightweight static imports & top-level symbols to speed up R:/A: authoring."""
    root = _git_ops.resolve_root(args.root)
    max_bytes = getattr(args, "max_bytes", DEFAULT_MAX_BYTES)
    res = _audit.compute_outlines(root, args.paths, max_items=args.max_items, max_bytes=max_bytes)
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


def cmd_pack(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        msg = f"`{code_path}` not found. Run `init` first."
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    entries, _ = _shards.parse_code_volume(code_path)
    parsed_map = parse_entries_map(entries)
    deps_adj, dependents_adj = build_fras_graph(entries, parsed_entries=parsed_map)
    invariants = parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-"
    domains = parse_declared_domains(aoci_dir)
    glossary = parse_glossary(aoci_dir)

    diff_brief: Optional[Dict[str, Any]] = None
    if not args.no_diff:
        cand_diff = _diff_brief.compute_diff_briefing(
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
        bm25 = _graph.BM25Index(entries, parsed_entries=parsed_map)
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


def cmd_symbol(args: argparse.Namespace) -> int:
    """Look up a symbol across `.aoci/aoci.hubs.txt`, outline cache (or `--live`), and FRAS `A:` exports."""
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

    entries, _ = _shards.parse_code_volume(code_path)
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
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        msg = f"`{code_path}` not found. Run `init` first."
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    entries, _ = _shards.parse_code_volume(code_path)
    parsed_map = parse_entries_map(entries)
    deps_adj, dependents_adj = build_fras_graph(entries, parsed_entries=parsed_map)
    declared_domains = parse_declared_domains(aoci_dir)
    changed_paths_exp: List[str] = []
    affected_paths_exp: List[str] = []
    if getattr(args, "diff", False) or getattr(args, "diff_rev", None):
        dbrief = _diff_brief.compute_diff_briefing(
            root,
            rev=getattr(args, "diff_rev", None),
            depth=1,
            cached_entries=entries,
            cached_deps=deps_adj,
            cached_dependents=dependents_adj,
            cached_parsed=parsed_map,
        )
        if dbrief.get("ok"):
            for ln in dbrief.get("changed_entries") or ():
                changed_paths_exp.append(normalize_relpath(ln.split("[", 1)[0].strip()))
            changed_paths_exp.extend(normalize_relpath(u) for u in (dbrief.get("unindexed_changed") or ()))
            for ln in dbrief.get("affected_neighbors") or ():
                affected_paths_exp.append(normalize_relpath(ln.split("[", 1)[0].strip()))

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
        changed_paths=changed_paths_exp,
        affected_paths=affected_paths_exp,
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
