"""Static import graph wrapper, entry scaffolding, FRAS quality linting, outline extraction, and architecture audit."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import ci_check as _ci_check
from . import cochange as _cochange
from . import diff_brief as _diff_brief
from . import discovery as _discovery
from . import drift as _drift
from . import git_ops as _git_ops
from . import hubs as _hubs
from . import ignore as _ignore
from . import parsers as _parsers
from . import shards as _shards
from .constants import (
    AOCI_DIR_NAME,
    BASELINE_FILE,
    CODE_FILE,
    DEFAULT_FORBIDDEN_LAYER_EDGES,
    DEFAULT_MAX_BYTES,
    _is_within_root,
    _resolve_cli_file_path,
    normalize_relpath,
)
from .graph import (
    ParsedEntry,
    _load_layer_rules_from_aoci,
    build_fras_graph,
    check_layer_violations,
    code_dependency_view,
    extract_path_refs,
    find_dependency_cycles,
    lint_entries,
    parse_entries_map,
    parse_layer_rules,
)
from .templates import parse_declared_domains, parse_manifest_field


def build_static_import_graph(
    root: Path,
    discovered_files: Sequence[str],
    fps: Optional[Dict[str, Dict[str, Any]]] = None,
    aoci_dir: Optional[Path] = None,
    resolution_extras: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    return _parsers.build_static_import_graph(
        root,
        discovered_files,
        fps=fps,
        aoci_dir=aoci_dir,
        resolution_extras=resolution_extras,
    )


_DOMAIN_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_DOMAIN_PATH_HINT_RE = re.compile(r"(?:^|[\s(\[,;])([a-z0-9_.-]+(?:/[a-z0-9_.-]+)+)/?(?=$|[\s)\],;])")
_DOMAIN_STOPWORDS: Set[str] = {
    "and",
    "the",
    "for",
    "with",
    "from",
    "src",
    "lib",
    "pkg",
    "app",
    "core",
    "service",
    "services",
    "module",
    "modules",
    "layer",
    "system",
    "common",
    "shared",
    "utils",
    "util",
    "test",
    "tests",
}


def _tokenize_domain_words(text: str) -> List[str]:
    return [tok for tok in _DOMAIN_WORD_SPLIT_RE.split(text.lower()) if len(tok) >= 3 and tok not in _DOMAIN_STOPWORDS]


def _singular_stem(tok: str) -> str:
    if len(tok) >= 5 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) >= 4 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _infer_layer_and_domain(rel: str, declared_domains: Dict[str, str]) -> Tuple[str, str]:
    low = rel.lower()
    parts = low.split("/")
    bname = parts[-1]

    layer = "C"
    if (
        "test" in parts
        or "tests" in parts
        or "__tests__" in parts
        or bname.startswith("test_")
        or any(k in bname for k in (".test.", ".spec.", "_test."))
    ):
        layer = "T"
    elif bname.endswith((".md", ".rst", ".mdx")) or "docs" in parts:
        layer = "O"
    elif bname in {
        "package.json",
        "pyproject.toml",
        "cargo.toml",
        "go.mod",
        "dockerfile",
        "makefile",
    } or ".github" in parts:
        layer = "R"
    elif bname.endswith((".vue", ".svelte", ".css", ".scss")) or any(
        p in parts for p in ("components", "pages", "views", "ui")
    ):
        layer = "U"
    elif any(p in parts for p in ("middleware", "middlewares", "guards", "interceptors")):
        layer = "M"
    elif any(p in parts for p in ("db", "dao", "repo", "repos", "repositories", "migrations", "models", "prisma")) or bname.endswith(
        (".sql", ".prisma")
    ):
        layer = "P"
    elif any(p in parts for p in ("clients", "adapters", "sdk", "providers", "integrations")):
        layer = "I"
    elif any(p in parts for p in ("routes", "controllers", "handlers", "cli", "cmd")) or bname.startswith(
        ("main.", "index.", "app.", "server.")
    ):
        layer = "E"
    elif any(p in parts for p in ("services", "usecases", "workflows", "orchestrators")):
        layer = "A"
    elif any(p in parts for p in ("domain", "entities", "aggregates")):
        layer = "D"
    elif any(p in parts for p in ("engine", "compiler", "parser", "indexer")):
        layer = "K"

    domain = "X" if "X" in declared_domains or not declared_domains else next(iter(sorted(declared_domains)))
    if layer in ("R", "O") and ("G" in declared_domains or not declared_domains):
        domain = "G"

    dir_parts = parts[:-1]
    # 1) Explicit slash-containing path prefix hints inside domain label (longest prefix wins)
    best_prefix_len = 0
    best_prefix_code: Optional[str] = None
    for code, label in sorted(declared_domains.items()):
        if code in ("G", "X") or not label:
            continue
        for m_hint in _DOMAIN_PATH_HINT_RE.finditer(label.lower()):
            hint = m_hint.group(1).strip("/")
            if hint and (low == hint or low.startswith(f"{hint}/")) and len(hint) > best_prefix_len:
                best_prefix_len = len(hint)
                best_prefix_code = code
    if best_prefix_code is not None:
        return layer, best_prefix_code

    # 2) Direct full-label substring match against directory segments (preserves legacy priority)
    for code, label in declared_domains.items():
        if code in ("G", "X"):
            continue
        lbl_low = label.lower().strip()
        if lbl_low and any(lbl_low in seg for seg in dir_parts):
            return layer, code

    # 3) Token / singular-stem / >=4-char prefix matching across directory segments & filename stem
    dir_tokens = _tokenize_domain_words("/".join(dir_parts))
    dir_stems = {_singular_stem(t) for t in dir_tokens} | set(dir_tokens)
    stem_name = bname.rsplit(".", 1)[0]
    file_tokens = _tokenize_domain_words(stem_name)
    file_stems = {_singular_stem(t) for t in file_tokens} | set(file_tokens)

    best_score = 0
    best_code: Optional[str] = None
    for code, label in sorted(declared_domains.items()):
        if code in ("G", "X") or not label:
            continue
        ltoks = _tokenize_domain_words(label)
        if not ltoks:
            continue
        lstems = {_singular_stem(t) for t in ltoks} | set(ltoks)
        score = 0
        for d_st in dir_stems:
            if d_st in lstems:
                score += 4
            elif len(d_st) >= 4 and any(
                len(l_t) >= 4 and (l_t.startswith(d_st) or d_st.startswith(l_t)) for l_t in ltoks
            ):
                score += 3
        for f_st in file_stems:
            if f_st in lstems:
                score += 2
        if score > best_score:
            best_score = score
            best_code = code
    if best_code is not None:
        domain = best_code
    return layer, domain


def scaffold_entries(
    root: Path,
    target_paths: Sequence[str],
    max_bytes: int = DEFAULT_MAX_BYTES,
    precomputed_fps: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """
    Generate FRAS entry skeletons with auto-computed `[LDIS]`, resolved internal `R:` dependencies,
    and repo-wide `A: sym<-caller` edges. Leaves `<TODO:read-source>` in `F:` so `upsert` enforces
    reading the actual source file before merge (Rule #4).
    """
    aoci_dir = root / AOCI_DIR_NAME
    git_info = _git_ops.detect_git_info(root)
    ignores = _ignore.load_ignore_matcher(root, git_info)
    declared_domains = parse_declared_domains(aoci_dir)
    if precomputed_fps is not None:
        fps = precomputed_fps
    else:
        base_files = _drift.load_baseline(aoci_dir).get("files", {}) if (aoci_dir / BASELINE_FILE).is_file() else {}
        fps, _ = _discovery.discover_and_fingerprint(
            root, git_info, ignores, base_files=base_files, fast_mtime=True, max_bytes=max_bytes, save_scan_cache=True
        )

    all_discovered = sorted(fps.keys())
    static_deps, static_callers, outlines = build_static_import_graph(
        root,
        all_discovered,
        fps=fps,
        aoci_dir=aoci_dir if aoci_dir.is_dir() else None,
    )

    lines_out: List[str] = []
    for raw_t in target_paths:
        rel = normalize_relpath(raw_t)
        if rel not in fps:
            continue
        fp = fps[rel]
        out = outlines.get(rel) or _parsers.extract_file_outline(root / rel, rel)
        layer, domain = _infer_layer_and_domain(rel, declared_domains)
        in_deg = len(static_callers.get(rel, ()))
        if layer == "T":
            imp = 2
        elif layer in ("R", "O"):
            imp = 4
        else:
            imp = min(8, 4 + (1 if in_deg >= 1 else 0) + (1 if in_deg >= 3 else 0) + (2 if in_deg >= 6 else 0))

        r_deps = static_deps.get(rel, [])[:6]
        r_str = ",".join(r_deps) if r_deps else "-"

        syms = _parsers._clean_symbol_names(out.get("symbols", ()), limit=4)
        callers = static_callers.get(rel, [])[:5]
        if syms and callers:
            a_str = f"{'/'.join(syms)}<-{','.join(callers)}"
        elif syms:
            a_str = "/".join(syms)
        elif callers:
            a_str = f"module<-{','.join(callers)}"
        else:
            a_str = "-"

        s_str = "<TODO:document-hub-invariants>" if imp >= 8 else "-"
        lines_out.append(
            f"{rel}[{layer}{domain}{imp}{fp['size_tier']}]: F:<TODO:read-source> | R:{r_str} | A:{a_str} | S:{s_str}"
        )
    return lines_out


def compute_outlines(
    root: Path,
    raw_paths: Sequence[str],
    max_items: int = 30,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """Validate target paths safely under `root` and extract lightweight static outlines."""
    outlines: List[Dict[str, Any]] = []
    missing: List[str] = []
    rejected: List[Dict[str, str]] = []

    for raw_p in raw_paths:
        raw_str = str(raw_p).strip()
        rel = normalize_relpath(raw_str)
        if (
            not rel
            or raw_str.startswith(("/", "\\"))
            or Path(raw_str).is_absolute()
            or re.match(r"^[A-Za-z]:", raw_str)
            or any(seg in ("..", ".") for seg in rel.split("/"))
        ):
            rejected.append({"path": raw_str, "reason": "path-invalid"})
            continue
        full = root / rel
        if not _is_within_root(root, full):
            rejected.append({"path": rel, "reason": "outside-root"})
            continue
        try:
            st = os.lstat(full)
        except OSError:
            missing.append(rel)
            continue
        if stat.S_ISLNK(st.st_mode):
            rejected.append({"path": rel, "reason": "symlink"})
            continue
        if not stat.S_ISREG(st.st_mode):
            rejected.append({"path": rel, "reason": "not-regular-file"})
            continue
        if st.st_size > max_bytes:
            rejected.append({"path": rel, "reason": f"too-large(>{max_bytes}B)"})
            continue
        try:
            raw_bytes = full.read_bytes()
        except OSError:
            rejected.append({"path": rel, "reason": "unreadable"})
            continue
        if _ignore.is_probably_binary_bytes(raw_bytes[:8192]):
            rejected.append({"path": rel, "reason": "binary-content"})
            continue
        norm_bytes = raw_bytes.replace(b"\r\n", b"\n") if b"\r\n" in raw_bytes else raw_bytes
        outlines.append(_parsers._extract_outline_from_bytes(norm_bytes, full.suffix.lower(), rel, max_items=max_items))

    return {"outlines": outlines, "missing": missing, "rejected": rejected}


def compute_lint(
    root: Path,
    *,
    code_filter: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cached_entries: Optional[Dict[str, str]] = None,
    cached_parsed: Optional[Dict[str, ParsedEntry]] = None,
    cached_malformed: Optional[Sequence[Tuple[int, str]]] = None,
    cached_duplicates: Optional[Sequence[str]] = None,
    cached_domains: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run FRAS L1 + L1.5 (`aoci.hubs.txt`) quality checks and return structured payload."""
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    duplicates: List[str] = []
    if cached_entries is not None and cached_malformed is not None:
        entries = cached_entries
        malformed = list(cached_malformed)
        if cached_duplicates is not None:
            duplicates = list(cached_duplicates)
    else:
        entries, malformed = _shards.parse_code_volume(code_path, duplicates=duplicates)

    git_info = _git_ops.detect_git_info(root)
    ignores = _ignore.load_ignore_matcher(root, git_info)
    base_files = _drift.load_baseline(aoci_dir).get("files", {})
    fps, _ = _discovery.discover_and_fingerprint(
        root,
        git_info,
        ignores,
        base_files=base_files,
        fast_mtime=True,
        max_bytes=max_bytes,
    )
    parsed_map = cached_parsed if cached_parsed is not None else parse_entries_map(entries)
    domains = cached_domains if cached_domains is not None else parse_declared_domains(aoci_dir)
    findings = lint_entries(
        root,
        entries,
        domains,
        fingerprints=fps,
        parsed_entries=parsed_map,
    )
    _, hub_lint_findings = _hubs.validate_hubs_volume(root, aoci_dir, entries, fingerprints=fps)
    findings.extend(hub_lint_findings)
    for rel in sorted(set(duplicates)):
        findings.append({"path": rel, "code": "duplicate", "message": "path appears more than once in aoci.code.txt"})
    for ln, txt in malformed:
        findings.append({"path": f"L{ln}", "code": "malformed", "message": txt[:120]})
    if code_filter:
        wanted = {c.strip() for c in code_filter.split(",") if c.strip()}
        findings = [f for f in findings if f["code"] in wanted]

    by_code: Dict[str, int] = {}
    for f in findings:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1

    return {"ok": not findings, "indexed": len(entries), "counts": by_code, "findings": findings}


def audit_architecture(
    root: Path,
    git_info: Optional[Dict[str, Any]] = None,
    *,
    check_static: bool = True,
    check_cycles: bool = True,
    check_layers: bool = True,
    check_co_change: bool = False,
    co_change_commits: int = 200,
    min_co_changes: int = 3,
    min_co_confidence: float = 0.6,
    exclude_tests: bool = False,
    forbid_spec: Optional[str] = None,
    use_default_rules: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cached_entries: Optional[Dict[str, str]] = None,
    cached_deps: Optional[Dict[str, Set[str]]] = None,
    cached_dependents: Optional[Dict[str, Set[str]]] = None,
    cached_parsed: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Cross-check `.aoci/aoci.code.txt` against static imports, strongly connected dependency cycles,
    architectural layer redlines, and optional Git co-change coupling analysis.
    """
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    if cached_entries is not None:
        entries = cached_entries
    else:
        entries, _ = _shards.parse_code_volume(code_path)

    parsed_map = cached_parsed if cached_parsed is not None else parse_entries_map(entries)
    if cached_deps is not None and (cached_dependents is not None or not check_co_change):
        fras_deps = cached_deps
        fras_dependents = cached_dependents if cached_dependents is not None else {}
    else:
        fras_deps, fras_dependents = build_fras_graph(entries, parsed_entries=parsed_map)

    ginfo: Optional[Dict[str, Any]] = git_info
    missing_static_deps: List[Dict[str, str]] = []
    phantom_fras_deps: List[Dict[str, str]] = []
    if check_static:
        if ginfo is None:
            ginfo = _git_ops.detect_git_info(root)
        ignores = _ignore.load_ignore_matcher(root, ginfo)
        base_files = _drift.load_baseline(aoci_dir).get("files", {}) if (aoci_dir / BASELINE_FILE).is_file() else {}
        fps, _ = _discovery.discover_and_fingerprint(
            root, ginfo, ignores, base_files=base_files, fast_mtime=True, max_bytes=max_bytes
        )
        discovered = sorted(fps.keys())
        res_extras: Dict[str, Any] = {}
        static_deps, _, outlines = build_static_import_graph(
            root,
            discovered,
            fps=fps,
            aoci_dir=aoci_dir,
            resolution_extras=res_extras,
        )
        if "go_imported_dirs_by_file" in res_extras and "go_files_by_dir" in res_extras:
            go_imported_dirs_by_file = res_extras["go_imported_dirs_by_file"]
            go_files_by_dir = res_extras["go_files_by_dir"]
        else:
            go_imported_dirs_by_file, go_files_by_dir = _parsers.resolve_go_package_imports(root, outlines, discovered)
        if "jvm_cs_resolved" in res_extras:
            jvm_cs_resolved = res_extras["jvm_cs_resolved"]
        else:
            jvm_cs_resolved = _parsers.resolve_jvm_cs_package_imports(outlines, discovered)
        known = set(entries.keys())

        static_sets: Dict[str, Set[str]] = {k: set(v) for k, v in static_deps.items()}
        for rel in sorted(entries.keys()):
            s_deps = static_sets.get(rel, set())
            f_deps = fras_deps.get(rel, set())
            rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
            rel_eco = _parsers._static_ecosystem(rel)
            if rel.endswith(".go"):
                for imp_dir in sorted(go_imported_dirs_by_file.get(rel, ())):
                    if imp_dir == rel_dir:
                        continue
                    pkg_indexed = [f for f in go_files_by_dir.get(imp_dir, ()) if f in entries and f != rel]
                    if pkg_indexed and not (set(pkg_indexed) & f_deps):
                        missing_static_deps.append({"from": rel, "to": pkg_indexed[0], "code": "missing-static-dep"})
            elif rel_eco in ("jvm", "cs") and rel in jvm_cs_resolved:
                exact_hits, group_hits = jvm_cs_resolved[rel]
                reported_missing: Set[str] = set()
                for dep in sorted(set(exact_hits)):
                    if dep in entries and dep not in f_deps and dep not in reported_missing:
                        reported_missing.add(dep)
                        missing_static_deps.append({"from": rel, "to": dep, "code": "missing-static-dep"})
                for grp in group_hits:
                    grp_indexed = [f for f in grp if f in entries and f != rel]
                    if grp_indexed and not (set(grp_indexed) & f_deps):
                        cand_to = grp_indexed[0]
                        if cand_to not in reported_missing:
                            reported_missing.add(cand_to)
                            missing_static_deps.append({"from": rel, "to": cand_to, "code": "missing-static-dep"})
            else:
                for dep in sorted(s_deps):
                    if dep in entries and dep not in f_deps:
                        missing_static_deps.append({"from": rel, "to": dep, "code": "missing-static-dep"})
            if rel_eco is not None:
                pe = parsed_map.get(rel)
                if pe is not None:
                    for r_dep in extract_path_refs(pe.r, known):
                        if r_dep == rel or _parsers._static_ecosystem(r_dep) != rel_eco:
                            continue
                        if rel.endswith(".go") and r_dep.endswith(".go"):
                            r_dir = r_dep.rsplit("/", 1)[0] if "/" in r_dep else ""
                            if r_dir == rel_dir or r_dir in go_imported_dirs_by_file.get(rel, ()):
                                continue
                        elif rel_eco in ("jvm", "cs"):
                            r_dir = r_dep.rsplit("/", 1)[0] if "/" in r_dep else ""
                            if r_dir == rel_dir:
                                continue
                        if r_dep not in s_deps:
                            phantom_fras_deps.append(
                                {
                                    "from": rel,
                                    "to": r_dep,
                                    "code": "phantom-fras-dep",
                                    "severity": "info",
                                }
                            )

    cycles: List[Dict[str, Any]] = []
    if check_cycles:
        # `R`/`O`-layer co-read pairs (e.g. `pyproject.toml` <-> `setup.py`) are not executable import cycles
        for comp in find_dependency_cycles(code_dependency_view(fras_deps, parsed_map)):
            cycles.append({"nodes": comp, "size": len(comp)})

    forbidden: Dict[str, Set[str]] = {}
    layer_violations: List[Dict[str, str]] = []
    if check_layers:
        if use_default_rules:
            for src_l, dsts in DEFAULT_FORBIDDEN_LAYER_EDGES.items():
                forbidden.setdefault(src_l, set()).update(dsts)
        for src_l, dsts in _load_layer_rules_from_aoci(
            aoci_dir, manifest_spec=parse_manifest_field(aoci_dir, "#Layer-Rules:")
        ).items():
            forbidden.setdefault(src_l, set()).update(dsts)
        if forbid_spec:
            for src_l, dsts in parse_layer_rules(forbid_spec).items():
                forbidden.setdefault(src_l, set()).update(dsts)
        layer_violations = check_layer_violations(entries, fras_deps, forbidden, parsed_entries=parsed_map)

    co_change_unlinked: Optional[List[Dict[str, Any]]] = None
    if check_co_change:
        if ginfo is None:
            ginfo = _git_ops.detect_git_info(root)
        co_change_unlinked = _cochange.find_cochange_unlinked_pairs(
            root,
            ginfo,
            entries,
            fras_deps,
            fras_dependents,
            _git_ops.run_git,
            max_commits=co_change_commits,
            min_co_changes=min_co_changes,
            min_confidence=min_co_confidence,
            parsed_entries=parsed_map,
            exclude_tests=exclude_tests,
        )

    ok = not (missing_static_deps or cycles or layer_violations)
    rules_used = {k: sorted(v) for k, v in sorted(forbidden.items()) if v}
    summary: Dict[str, Any] = {
        "indexed": len(entries),
        "missing_static_deps": len(missing_static_deps),
        "phantom_fras_deps": len(phantom_fras_deps),
        "cycles": len(cycles),
        "layer_violations": len(layer_violations),
    }
    res: Dict[str, Any] = {
        "ok": ok,
        "root": str(root),
        "summary": summary,
        "missing_static_deps": missing_static_deps,
        "phantom_fras_deps": phantom_fras_deps,
        "cycles": cycles,
        "layer_violations": layer_violations,
        "rules_used": rules_used,
    }
    if co_change_unlinked is not None:
        summary["co_change_unlinked"] = len(co_change_unlinked)
        res["co_change_unlinked"] = co_change_unlinked
    return res


def run_ci_architecture_check(
    root: Path,
    *,
    base_rev: Optional[str] = None,
    pool_bundle: Optional[str] = None,
    forbid_spec: Optional[str] = None,
    use_default_rules: bool = True,
    check_cycles: bool = True,
    check_layers: bool = True,
    check_static: bool = True,
    check_drift: bool = False,
    exclude_tests: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    bundle_p = _resolve_cli_file_path(root, pool_bundle, must_exist=True) if pool_bundle else None
    return _ci_check.run_ci_check(
        root,
        detect_git_info_fn=_git_ops.detect_git_info,
        load_ignore_matcher_fn=_ignore.load_ignore_matcher,
        discover_and_fingerprint_fn=_discovery.discover_and_fingerprint,
        build_static_import_graph_fn=build_static_import_graph,
        infer_layer_and_domain_fn=_infer_layer_and_domain,
        compute_drift_fn=_drift.compute_drift,
        audit_architecture_fn=audit_architecture,
        compute_diff_briefing_fn=_diff_brief.compute_diff_briefing,
        run_git_fn=_git_ops.run_git,
        base_rev=base_rev,
        pool_bundle=bundle_p,
        forbid_spec=forbid_spec,
        use_default_rules=use_default_rules,
        check_cycles=check_cycles,
        check_layers=check_layers,
        check_static=check_static,
        check_drift=check_drift,
        exclude_tests=exclude_tests,
        max_bytes=max_bytes,
    )
