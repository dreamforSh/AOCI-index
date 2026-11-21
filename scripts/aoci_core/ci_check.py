"""CI-native architecture guard (`aoci ci-check` / MCP `aoci_ci_check`) with SARIF 2.1.0, GitHub, and PR Markdown."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple
import zlib

from .constants import (
    AOCI_DIR_NAME,
    AOCI_VERSION,
    CODE_FILE,
    DEFAULT_MAX_BYTES,
    ENTRY_RE,
    normalize_relpath,
)
from .graph import (
    _load_layer_rules_from_aoci,
    bfs_neighbors,
    build_fras_graph,
    check_layer_violations,
    find_dependency_cycles,
    parse_entries_map,
    parse_layer_rules,
)
from .pool import _validate_pool_entry_row
from .test_impact import find_impacted_tests

SARIF_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
DEFAULT_CI_LAYER_RULES = "D!->E,U; P!->E,U"


def _load_pool_bundle_map(bundle_path: Path) -> Dict[Tuple[str, str], str]:
    """Load `(relpath, sha256) -> normalized_entry` from a `.json` or `.json.gz` semantic pool bundle in memory."""
    if not bundle_path.is_file():
        return {}
    try:
        raw = bundle_path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, zlib.error):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[Tuple[str, str], str] = {}
    for row in data.get("by_path_sha") or ():
        if not isinstance(row, dict):
            continue
        rel = row.get("path")
        sha = row.get("sha256")
        ent = row.get("entry")
        if isinstance(rel, str):
            clean = _validate_pool_entry_row(rel, sha, ent)
            if clean and isinstance(sha, str):
                out[(normalize_relpath(rel), sha)] = clean
    return out


def _git_changed_paths_against_base(
    root: Path,
    git_info: Mapping[str, Any],
    base_rev: str,
    run_git_fn: Callable[[Sequence[str], Path], Any],
) -> Tuple[List[str], Optional[str]]:
    if not git_info.get("is_git"):
        return [], "Not inside a Git repository."
    rev = base_rev.strip()
    if not rev or rev.startswith("-"):
        return [], f"Invalid `--base-rev` `{base_rev}`."
    cp = run_git_fn(["diff", "--name-only", "--relative", "-z", rev, "--", "."], root)
    if cp.returncode != 0:
        return [], (cp.stderr or f"git diff against `{rev}` failed").strip()
    changed = sorted({normalize_relpath(tok) for tok in cp.stdout.split("\0") if tok.strip()})
    return changed, None


def run_ci_check(
    root: Path,
    *,
    detect_git_info_fn: Callable[[Path], Dict[str, Any]],
    load_ignore_matcher_fn: Callable[[Path, Dict[str, Any]], Any],
    discover_and_fingerprint_fn: Callable[..., Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]],
    build_static_import_graph_fn: Callable[..., Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, Any]]],
    infer_layer_and_domain_fn: Callable[[str, Dict[str, str]], Tuple[str, str]],
    compute_drift_fn: Callable[..., Dict[str, Any]],
    audit_architecture_fn: Callable[..., Dict[str, Any]],
    compute_diff_briefing_fn: Callable[..., Dict[str, Any]],
    run_git_fn: Callable[[Sequence[str], Path], Any],
    base_rev: Optional[str] = None,
    pool_bundle: Optional[Path] = None,
    forbid_spec: Optional[str] = None,
    use_default_rules: bool = True,
    check_cycles: bool = True,
    check_layers: bool = True,
    check_static: bool = True,
    check_drift: bool = False,
    exclude_tests: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Execute CI architecture & PR impact checks on either an indexed `.aoci/` workspace or a clean
    zero-index CI checkout (synthesizing static graph + optional `--pool-bundle` semantics in memory).
    """
    aoci_dir = root / AOCI_DIR_NAME
    has_aoci = (aoci_dir / CODE_FILE).is_file()
    git_info = detect_git_info_fn(root)

    layer_violations: List[Dict[str, str]] = []
    cycles: List[Dict[str, Any]] = []
    missing_static_deps: List[Dict[str, str]] = []
    drift_issues: List[Dict[str, str]] = []
    pr_impact: Optional[Dict[str, Any]] = None
    mode = "indexed" if has_aoci else "zero-index-static"
    pool_hits = 0

    if has_aoci:
        aud = audit_architecture_fn(
            root,
            check_static=check_static,
            check_cycles=check_cycles,
            check_layers=check_layers,
            check_co_change=False,
            exclude_tests=exclude_tests,
            forbid_spec=forbid_spec,
            use_default_rules=use_default_rules,
            max_bytes=max_bytes,
            git_info=git_info,
        )
        if not aud.get("ok") and aud.get("error"):
            return {"ok": False, "error": str(aud["error"])}
        layer_violations = list(aud.get("layer_violations") or [])
        cycles = list(aud.get("cycles") or [])
        missing_static_deps = list(aud.get("missing_static_deps") or [])
        total_indexed = int((aud.get("summary") or {}).get("indexed", 0))

        if check_drift:
            drift = compute_drift_fn(root, git_info, fast_mtime=True, max_bytes=max_bytes, allow_heal=False)
            for item in drift.get("added", ()):
                drift_issues.append({"path": item["path"], "code": "drift-added", "message": f"Unindexed new file `{item['path']}`"})
            for item in drift.get("modified", ()):
                drift_issues.append(
                    {
                        "path": item["path"],
                        "code": "drift-modified",
                        "message": f"Unsynced modified file `{item['path']}` ({item.get('change_kind', 'unknown')})",
                    }
                )
            for r in drift.get("renamed", ()):
                drift_issues.append(
                    {"path": r["to"], "code": "drift-renamed", "message": f"Unmigrated rename `{r['from']} -> {r['to']}`"}
                )
            for p in drift.get("deleted", ()):
                drift_issues.append({"path": p, "code": "drift-deleted", "message": f"Deleted file `{p}` still in index"})
            for inv in drift.get("invalid_entries", ()):
                drift_issues.append({"path": inv["path"], "code": inv["code"], "message": inv["message"]})

        if base_rev:
            dbrief = compute_diff_briefing_fn(root, rev=base_rev, depth=1, max_bytes=max_bytes, with_tests=True)
            if dbrief.get("ok"):
                pr_impact = {
                    "base_rev": base_rev,
                    "changed_entries": dbrief.get("changed_entries", []),
                    "unindexed_changed": dbrief.get("unindexed_changed", []),
                    "high_risk_constraints": dbrief.get("high_risk_constraints", []),
                    "dependency_constraints": dbrief.get("dependency_constraints", []),
                    "affected_neighbors": dbrief.get("affected_neighbors", []),
                    "impacted_tests": dbrief.get("impacted_tests", []),
                }
            else:
                pr_impact = {"base_rev": base_rev, "error": dbrief.get("error", "diff failed")}
    else:
        ignores = load_ignore_matcher_fn(root, git_info)
        fps, _ = discover_and_fingerprint_fn(
            root, git_info, ignores, base_files={}, fast_mtime=False, max_bytes=max_bytes
        )
        discovered = sorted(fps.keys())
        static_deps_list, static_callers_list, outlines = build_static_import_graph_fn(root, discovered, fps=fps)
        pool_map = _load_pool_bundle_map(pool_bundle) if pool_bundle is not None else {}
        if pool_map:
            mode = "pool-bundle+static"

        synth_entries: Dict[str, str] = {}
        for rel in discovered:
            fp = fps[rel]
            sha = str(fp.get("sha256", ""))
            pooled = pool_map.get((rel, sha))
            if pooled and ENTRY_RE.match(pooled):
                synth_entries[rel] = pooled
                pool_hits += 1
            else:
                lyr, dom = infer_layer_and_domain_fn(rel, {"G": "governance", "X": "general"})
                r_deps = ",".join(static_deps_list.get(rel, ())[:12]) or "-"
                a_callers = ",".join(f"sym<-{c}" for c in static_callers_list.get(rel, ())[:8]) or "-"
                synth_entries[rel] = f"{rel}[{lyr}{dom}5{fp['size_tier']}]: F:synthesized CI module | R:{r_deps} | A:{a_callers} | S:-"

        parsed_map = parse_entries_map(synth_entries)
        deps_adj, dependents_adj = build_fras_graph(synth_entries, parsed_entries=parsed_map)
        # Ensure all resolved static edges are included even when a pooled entry omitted a new import
        for src_rel, dst_list in static_deps_list.items():
            for dst_rel in dst_list:
                if src_rel in deps_adj and dst_rel in deps_adj and src_rel != dst_rel:
                    deps_adj[src_rel].add(dst_rel)
                    dependents_adj[dst_rel].add(src_rel)

        eval_deps = deps_adj
        if exclude_tests:
            non_t = {r for r, pe in parsed_map.items() if pe.layer != "T"}
            eval_deps = {r: {d for d in dset if d in non_t} for r, dset in deps_adj.items() if r in non_t}

        if check_layers:
            rules = parse_layer_rules(forbid_spec) if forbid_spec else (
                _load_layer_rules_from_aoci(aoci_dir) if aoci_dir.is_dir() else {}
            )
            if not rules and use_default_rules:
                rules = parse_layer_rules(DEFAULT_CI_LAYER_RULES)
            if rules:
                layer_violations = check_layer_violations(synth_entries, eval_deps, rules, parsed_entries=parsed_map)

        if check_cycles:
            for scc in find_dependency_cycles(eval_deps):
                cycles.append({"size": len(scc), "nodes": scc})

        total_indexed = len(synth_entries)

        if base_rev:
            changed_rels, git_err = _git_changed_paths_against_base(root, git_info, base_rev, run_git_fn)
            if git_err is not None:
                pr_impact = {"base_rev": base_rev, "error": git_err}
            else:
                high_risk = []
                for c_rel in changed_rels:
                    pe = parsed_map.get(c_rel)
                    if pe is not None and pe.s not in ("-", ""):
                        high_risk.append({"path": c_rel, "importance": pe.imp, "s": pe.s})
                changed_set: Set[str] = set(changed_rels)
                neighbors = [
                    synth_entries[nb]
                    for nb in sorted(bfs_neighbors(dependents_adj, [c for c in changed_rels if c in synth_entries], 1))
                    if nb not in changed_set and nb in synth_entries
                ]
                outline_wrap = {r: {"outline": out_d} for r, out_d in outlines.items() if isinstance(out_d, dict)}
                impacted_tests = find_impacted_tests(
                    root,
                    synth_entries,
                    dependents_adj,
                    changed_rels,
                    max_depth=3,
                    parsed_entries=parsed_map,
                    preloaded_outline_files=outline_wrap,
                    live_imports=False,
                )
                pr_impact = {
                    "base_rev": base_rev,
                    "changed_entries": [synth_entries[c] for c in changed_rels if c in synth_entries],
                    "unindexed_changed": [c for c in changed_rels if c not in synth_entries],
                    "high_risk_constraints": high_risk,
                    "dependency_constraints": [],
                    "affected_neighbors": neighbors,
                    "impacted_tests": impacted_tests,
                }

    total_findings = len(layer_violations) + len(cycles) + len(missing_static_deps) + len(drift_issues)
    return {
        "ok": total_findings == 0,
        "root": str(root),
        "mode": mode,
        "indexed_files": total_indexed,
        "pool_hits": pool_hits,
        "total_findings": total_findings,
        "summary": {
            "layer_violations": len(layer_violations),
            "cycles": len(cycles),
            "missing_static_deps": len(missing_static_deps),
            "drift_issues": len(drift_issues),
        },
        "layer_violations": layer_violations,
        "cycles": cycles,
        "missing_static_deps": missing_static_deps,
        "drift_issues": drift_issues,
        "pr_impact": pr_impact,
    }


def format_ci_sarif(report: Mapping[str, Any]) -> str:
    """Render a `run_ci_check` report as a valid SARIF 2.1.0 JSON document for GitHub Code Scanning."""
    rules = [
        {
            "id": "AOCI001",
            "name": "LayerViolation",
            "shortDescription": {"text": "Forbidden cross-layer architectural dependency"},
            "defaultConfiguration": {"level": "error"},
        },
        {
            "id": "AOCI002",
            "name": "DependencyCycle",
            "shortDescription": {"text": "Strongly connected circular dependency cycle"},
            "defaultConfiguration": {"level": "warning"},
        },
        {
            "id": "AOCI003",
            "name": "MissingStaticDependency",
            "shortDescription": {"text": "Resolved static import missing from FRAS R:/A: graph"},
            "defaultConfiguration": {"level": "warning"},
        },
        {
            "id": "AOCI004",
            "name": "IndexDrift",
            "shortDescription": {"text": "Unsynchronized AOCI codebase index drift"},
            "defaultConfiguration": {"level": "warning"},
        },
    ]
    results: List[Dict[str, Any]] = []

    def _loc(uri_path: str) -> List[Dict[str, Any]]:
        return [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": normalize_relpath(uri_path)},
                    "region": {"startLine": 1},
                }
            }
        ]

    for lv in report.get("layer_violations") or ():
        results.append(
            {
                "ruleId": "AOCI001",
                "level": "error",
                "message": {
                    "text": f"Layer violation [{lv.get('rule', '')}]: `{lv['from']}` imports `{lv['to']}`."
                },
                "locations": _loc(lv["from"]),
            }
        )
    for cyc in report.get("cycles") or ():
        nodes = cyc.get("nodes") or ["."]
        results.append(
            {
                "ruleId": "AOCI002",
                "level": "warning",
                "message": {"text": f"Dependency cycle ({cyc.get('size', len(nodes))} files): {' <-> '.join(nodes[:12])}"},
                "locations": _loc(nodes[0]),
            }
        )
    for ms in report.get("missing_static_deps") or ():
        results.append(
            {
                "ruleId": "AOCI003",
                "level": "warning",
                "message": {"text": f"Static import `{ms['from']} -> {ms['to']}` is not recorded in FRAS R:/A:."},
                "locations": _loc(ms["from"]),
            }
        )
    for di in report.get("drift_issues") or ():
        results.append(
            {
                "ruleId": "AOCI004",
                "level": "warning",
                "message": {"text": str(di.get("message", ""))},
                "locations": _loc(str(di.get("path", "."))),
            }
        )

    sarif_doc = {
        "$schema": SARIF_SCHEMA_URI,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "aoci-index",
                        "version": AOCI_VERSION,
                        "informationUri": "https://github.com/CancriRecoleta/AOCI-index",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(sarif_doc, indent=2, ensure_ascii=False) + "\n"


def format_ci_github_annotations(report: Mapping[str, Any]) -> str:
    """Render GitHub Actions workflow commands (`::error`, `::warning`, `::notice`)."""
    lines: List[str] = []

    def _esc(s: str) -> str:
        return str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    for lv in report.get("layer_violations") or ():
        f_rel = normalize_relpath(lv["from"])
        t_rel = lv["to"]
        rule_lbl = lv.get("rule", "")
        msg_lv = _esc(f"{f_rel} -> {t_rel} violates layer rule {rule_lbl}")
        lines.append(f"::error file={f_rel},line=1,title=AOCI Layer Violation ({rule_lbl})::{msg_lv}")
    for cyc in report.get("cycles") or ():
        nodes = cyc.get("nodes") or ["."]
        f_rel = normalize_relpath(nodes[0])
        msg_cyc = _esc(" <-> ".join(nodes[:10]))
        lines.append(f"::warning file={f_rel},line=1,title=AOCI Dependency Cycle::{msg_cyc}")
    for ms in report.get("missing_static_deps") or ():
        f_rel = normalize_relpath(ms["from"])
        t_ms = ms["to"]
        msg_ms = _esc(f"{f_rel} imports {t_ms} without R:/A: link")
        lines.append(f"::warning file={f_rel},line=1,title=AOCI Missing Static Dep::{msg_ms}")
    for di in report.get("drift_issues") or ():
        f_rel = normalize_relpath(str(di.get("path", ".")))
        lines.append(f"::warning file={f_rel},line=1,title=AOCI Drift::{_esc(str(di.get('message', '')))}")

    pr = report.get("pr_impact")
    if isinstance(pr, Mapping):
        for hr in pr.get("high_risk_constraints") or ():
            f_rel = normalize_relpath(str(hr["path"]))
            lines.append(
                f"::notice file={f_rel},line=1,title=AOCI S: Invariant (Imp {hr['importance']})::"
                f"{_esc(str(hr['s']))}"
            )
    status = "CLEAN" if report.get("ok") else f"ISSUES ({report.get('total_findings', 0)})"
    lines.append(
        f"[AOCI CI-Check] {status} | Mode={report.get('mode')} | Files={report.get('indexed_files', 0)}"
    )
    return "\n".join(lines) + "\n"


def format_ci_markdown(report: Mapping[str, Any]) -> str:
    """Render a PR comment / `$GITHUB_STEP_SUMMARY` Markdown architectural report."""
    status_badge = "PASS" if report.get("ok") else "ISSUES DETECTED"
    sm = report.get("summary") or {}
    lines: List[str] = [
        f"## AOCI Architecture Check — {status_badge}",
        "",
        "| Metric | Value |",
        "| :--- | ---: |",
        f"| Analysis Mode | `{report.get('mode', 'indexed')}` |",
        f"| Analyzed Files | {report.get('indexed_files', 0)} |",
        f"| Layer Violations | {sm.get('layer_violations', 0)} |",
        f"| Dependency Cycles | {sm.get('cycles', 0)} |",
        f"| Missing Static Deps | {sm.get('missing_static_deps', 0)} |",
        f"| Drift Issues | {sm.get('drift_issues', 0)} |",
    ]
    if report.get("layer_violations"):
        lines.append("\n### Layer Violations")
        for lv in (report.get("layer_violations") or ())[:25]:
            lines.append(f"- **[{lv.get('rule', '')}]** `{lv['from']}` -> `{lv['to']}`")
    if report.get("cycles"):
        lines.append("\n### Dependency Cycles")
        for cyc in (report.get("cycles") or ())[:15]:
            lines.append(f"- ({cyc.get('size', 0)} files) `{' <-> '.join((cyc.get('nodes') or ())[:8])}`")

    pr = report.get("pr_impact")
    if isinstance(pr, Mapping) and not pr.get("error"):
        lines.append(f"\n### PR Impact Briefing (vs `{pr.get('base_rev')}`)")
        if pr.get("high_risk_constraints"):
            lines.append("#### High-Risk `S:` Constraints on Touched Files")
            for hr in (pr.get("high_risk_constraints") or ())[:15]:
                lines.append(f"- `{hr['path']}` (`I={hr['importance']}`): {hr['s']}")
        if pr.get("impacted_tests"):
            lines.append(f"#### Impacted Tests ({len(pr['impacted_tests'])})")
            for t in (pr.get("impacted_tests") or ())[:20]:
                lines.append(f"- `{t['path']}` (`{t['reason']}`, hop={t['hop']}, via `{t['via']}`)")
    return "\n".join(lines).rstrip() + "\n"
