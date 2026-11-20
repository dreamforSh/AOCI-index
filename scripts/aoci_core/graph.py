"""FRAS graph builder, context budgeting, linting, BM25 search, architecture audit & export."""

from __future__ import annotations

import fnmatch
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .constants import (
    AOCI_DIR_NAME,
    CJK_CHAR_RE,
    ENTRY_RE,
    GLOSSARY_FILE,
    IMPORTANCE_TOKEN_BUDGET,
    MANIFEST_FILE,
    META_FILE,
    RELPATH_TOKEN_RE,
    ROOT_FILE_TOKEN_RE,
    VALID_LAYERS,
    VALID_SIZES,
    VAGUE_F_RE,
    _LIKELY_FILE_EXTS,
    _REF_BOUNDARY_LEFT,
    _REF_BOUNDARY_RIGHT,
    _parse_code_filter_set,
    normalize_relpath,
)


def path_ref_pattern(path: str) -> "re.Pattern[str]":
    """Regex for an exact path mention inside R:/A: text."""
    return re.compile(rf"{_REF_BOUNDARY_LEFT}{re.escape(path)}{_REF_BOUNDARY_RIGHT}")


def compile_path_ref_rewriter(rename_map: Dict[str, str]):
    """Compile a single-pass path reference rewriter over `rename_map`."""
    if not rename_map:
        return lambda text: text
    ordered = sorted(rename_map.keys(), key=len, reverse=True)
    alt = "|".join(re.escape(p) for p in ordered)
    combined = re.compile(rf"{_REF_BOUNDARY_LEFT}(?:{alt}){_REF_BOUNDARY_RIGHT}")
    return lambda text: (text if not text or text.strip() == "-" else combined.sub(lambda m: rename_map[m.group(0)], text))


def rewrite_path_refs(text: str, rename_map: Dict[str, str]) -> str:
    """Atomically rewrite all old->new path mentions in an R:/A: field in one pass."""
    return compile_path_ref_rewriter(rename_map)(text)


def extract_path_refs(text: str, known: Optional[Set[str]] = None) -> List[str]:
    """Extract path-like references from an R:/A: field."""
    if not text or text.strip() == "-":
        return []
    found: List[str] = []
    if known is not None:
        for chunk in re.split(r"[,;|]", text):
            rhs = chunk.split("<-", 1)[1] if "<-" in chunk else chunk
            trimmed = rhs.strip().strip("\"'`")
            if trimmed and trimmed != "-":
                cand = normalize_relpath(trimmed.rstrip("."))
                if cand in known:
                    found.append(cand)
    cleaned = text.replace("<-", " ")
    for tok in RELPATH_TOKEN_RE.findall(cleaned):
        found.append(normalize_relpath(tok.lstrip("-").rstrip(".")))
    if known is not None:
        for tok in ROOT_FILE_TOKEN_RE.findall(cleaned):
            norm_root = normalize_relpath(tok.rstrip("."))
            if norm_root in known:
                found.append(norm_root)
        found = [p for p in found if p in known]
    return list(dict.fromkeys(found))


def parse_entry_fields(line: str) -> Optional[Dict[str, str]]:
    m = ENTRY_RE.match(line)
    if not m:
        return None
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in m.groupdict().items()}


def validate_parsed_entry_tags(
    rel: str,
    fields: Dict[str, str],
    raw_line: str,
    actual_lines: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """Shared [LDIS] tag & field validator used across `upsert`, `verify`, `scan`, `ack`, `lint`, and `pool`."""
    from .constants import compute_size_tier

    errs: List[Tuple[str, str]] = []
    layer = fields["layer"]
    size = fields["size"]
    f_val = fields["f"]

    if layer not in VALID_LAYERS:
        errs.append(
            ("layer-invalid", f"Invalid Layer `{layer}` in `{rel}` (allowed: {''.join(sorted(VALID_LAYERS))})")
        )
    if size not in VALID_SIZES:
        errs.append(("size-invalid", f"Invalid Size `{size}` in `{rel}` (allowed: T/S/M/L/H)"))
    if any(seg in ("..", ".") for seg in rel.split("/")) or re.match(r"^[A-Za-z]:", rel):
        errs.append(
            ("path-invalid", f"Path must be a plain repo-relative POSIX path (no `..`, `.`, or drive): `{rel}`")
        )
    elif rel.startswith(f"{AOCI_DIR_NAME}/") or rel == AOCI_DIR_NAME or rel.startswith(".git/"):
        errs.append(("path-invalid", f"Refusing to index reserved path `{rel}`."))
    if not f_val or f_val == "-":
        errs.append(
            (
                "F-empty",
                f"Field `F:` must describe the file's concrete responsibility (cannot be `-` or empty) in `{rel}`.",
            )
        )
    if "<todo" in raw_line.lower():
        errs.append(
            (
                "todo-unfilled",
                f"Unfilled `<TODO:...>` placeholder in `{rel}`; read the actual source file and author concrete F:/S: semantics (Rule #4).",
            )
        )
    if actual_lines is not None and size in VALID_SIZES:
        actual_size = compute_size_tier(actual_lines)
        if size != actual_size:
            errs.append(
                (
                    "size-mismatch",
                    f"Size tier mismatch for `{rel}`: declared `{size}`, actual `{actual_size}` ({actual_lines} lines).",
                )
            )
    return errs


def build_fras_graph(entries: Dict[str, str]) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Build directed edges `X -> Y` (`X` depends on `Y`) and reverse callers in O(total refs)."""
    known = set(entries.keys())
    deps: Dict[str, Set[str]] = {rel: set() for rel in known}
    dependents: Dict[str, Set[str]] = {rel: set() for rel in known}
    for rel, line in entries.items():
        f = parse_entry_fields(line)
        if not f:
            continue
        for dep in extract_path_refs(f["r"], known):
            if dep != rel:
                deps[rel].add(dep)
                dependents[dep].add(rel)
        for caller in extract_path_refs(f["a"], known):
            if caller != rel:
                deps[caller].add(rel)
                dependents[rel].add(caller)
    return deps, dependents


def build_explicit_mention_index(entries: Dict[str, str]) -> Dict[str, Set[str]]:
    """Map each indexed path `P` to every entry whose `R:` or `A:` explicitly mentions `P`."""
    known = set(entries.keys())
    mentioned_by: Dict[str, Set[str]] = {rel: set() for rel in known}
    for rel, line in entries.items():
        f = parse_entry_fields(line)
        if not f:
            continue
        for target in extract_path_refs(f["r"], known) + extract_path_refs(f["a"], known):
            if target != rel:
                mentioned_by[target].add(rel)
    return mentioned_by


def bfs_neighbors_with_distance(adj: Dict[str, Set[str]], starts: Sequence[str], depth: int) -> Dict[str, int]:
    seen: Set[str] = set(starts)
    frontier = [s for s in starts if s in adj]
    distances: Dict[str, int] = {}
    for hop in range(1, max(1, depth) + 1):
        nxt: List[str] = []
        for cur in frontier:
            for nb in sorted(adj.get(cur, ())):
                if nb not in seen:
                    seen.add(nb)
                    distances[nb] = hop
                    nxt.append(nb)
        frontier = nxt
        if not frontier:
            break
    return distances


def bfs_neighbors(adj: Dict[str, Set[str]], starts: Sequence[str], depth: int) -> List[str]:
    return list(bfs_neighbors_with_distance(adj, starts, depth).keys())


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token for ASCII, ~1 token per CJK/non-ASCII char."""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return max(1, int((len(text) - non_ascii) / 4.0 + non_ascii * 0.9))


def build_context_subgraph(
    root: Path,
    entries: Dict[str, str],
    deps_adj: Dict[str, Set[str]],
    dependents_adj: Dict[str, Set[str]],
    invariants: str,
    raw_paths: Sequence[str],
    depth: int = 1,
    budget: int = 0,
) -> Dict[str, Any]:
    """Build architectural subgraph payload for `raw_paths` with optional `--budget` token pruning."""
    targets = list(dict.fromkeys(normalize_relpath(str(p)) for p in raw_paths))
    missing_targets = [t for t in targets if t not in entries]
    present = [t for t in targets if t in entries]
    target_set = set(present)

    dep_dist = {
        p: d
        for p, d in bfs_neighbors_with_distance(deps_adj, present, depth).items()
        if p not in target_set
    }
    dep_set = set(dep_dist)
    caller_dist = {
        p: d
        for p, d in bfs_neighbors_with_distance(dependents_adj, present, depth).items()
        if p not in target_set and p not in dep_set
    }

    target_lines = [entries[t] for t in present]
    used_tokens = estimate_tokens(invariants) + sum(estimate_tokens(ln) for ln in target_lines)

    if budget <= 0:
        kept_deps = sorted(dep_dist.keys())
        kept_callers = sorted(caller_dist.keys())
        dep_lines = [entries[p] for p in kept_deps]
        caller_lines = [entries[p] for p in kept_callers]
        total_tokens = (
            used_tokens
            + sum(estimate_tokens(ln) for ln in dep_lines)
            + sum(estimate_tokens(ln) for ln in caller_lines)
        )
        return {
            "root": str(root),
            "global_invariants": invariants,
            "targets": target_lines,
            "missing_targets": missing_targets,
            "dependencies": dep_lines,
            "callers": caller_lines,
            "estimated_tokens": total_tokens,
        }

    candidates: List[Tuple[int, int, int, int, str, str]] = []
    for role_rank, dist_map, role_name in (
        (0, dep_dist, "dep"),
        (1, caller_dist, "caller"),
    ):
        for rel, hop in dist_map.items():
            line = entries.get(rel)
            if not line:
                continue
            f = parse_entry_fields(line)
            imp = int(f["imp"]) if f else 0
            has_s = 0 if (f and f["s"] not in ("-", "")) else 1
            candidates.append((hop, -imp, has_s, role_rank, rel, role_name))

    candidates.sort()
    kept_deps_b: List[str] = []
    kept_callers_b: List[str] = []
    omitted_paths: List[str] = []
    for _, _, _, _, rel, role_name in candidates:
        cost = estimate_tokens(entries[rel])
        if used_tokens + cost <= budget:
            used_tokens += cost
            if role_name == "dep":
                kept_deps_b.append(rel)
            else:
                kept_callers_b.append(rel)
        else:
            omitted_paths.append(rel)

    kept_deps_b.sort()
    kept_callers_b.sort()
    omitted_paths.sort()
    return {
        "root": str(root),
        "global_invariants": invariants,
        "targets": target_lines,
        "missing_targets": missing_targets,
        "dependencies": [entries[p] for p in kept_deps_b],
        "callers": [entries[p] for p in kept_callers_b],
        "estimated_tokens": used_tokens,
        "budget": budget,
        "truncated": bool(omitted_paths),
        "omitted_count": len(omitted_paths),
        "omitted_paths": omitted_paths,
    }


def find_dangling_refs(root: Path, entries: Dict[str, str]) -> Dict[str, List[str]]:
    """Path-like R:/A: references that point neither to an indexed entry nor to a file/dir on disk."""
    known = set(entries.keys())
    known_top_dirs = {p.split("/", 1)[0] for p in known if "/" in p}
    dangling: Dict[str, List[str]] = {}

    def _ref_segments(field: str, is_api_field: bool) -> List[str]:
        if not field or field.strip() == "-":
            return []
        if not is_api_field:
            return [field]
        segs: List[str] = []
        for chunk in re.split(r"[,;]", field):
            segs.append(chunk.split("<-", 1)[1] if "<-" in chunk else chunk)
        return segs

    for rel, line in entries.items():
        f = parse_entry_fields(line)
        if not f:
            continue
        bad: List[str] = []
        for field, is_api in ((f["r"], False), (f["a"], True)):
            for seg in _ref_segments(field, is_api):
                cleaned = seg.replace("<-", " ")
                for tok in RELPATH_TOKEN_RE.findall(cleaned):
                    ref = normalize_relpath(tok.lstrip("-").rstrip("."))
                    if not ref or ref in known or (root / ref).exists():
                        continue
                    bname = ref.rsplit("/", 1)[-1]
                    m_ext = re.search(r"(\.[A-Za-z][A-Za-z0-9]*)$", bname)
                    if not m_ext:
                        continue
                    ext = m_ext.group(1).lower()
                    top_dir = ref.split("/", 1)[0]
                    if ext in _LIKELY_FILE_EXTS or top_dir in known_top_dirs or (root / top_dir).is_dir():
                        bad.append(ref)
                for tok in ROOT_FILE_TOKEN_RE.findall(cleaned):
                    ref = normalize_relpath(tok.rstrip("."))
                    if not ref or "/" in ref or ref in known or (root / ref).exists():
                        continue
                    m_ext = re.search(r"(\.[A-Za-z][A-Za-z0-9]*)$", ref)
                    if (m_ext and m_ext.group(1).lower() in _LIKELY_FILE_EXTS) or ref in {
                        ".gitignore",
                        ".dockerignore",
                        ".editorconfig",
                        ".env",
                    }:
                        bad.append(ref)
        if bad:
            dangling[rel] = sorted(set(bad))
    return dangling


def semantic_word_count(text: str) -> int:
    """Count word-equivalents across whitespace-delimited scripts and spaceless CJK (~2 chars/word)."""
    cjk_chars = len(CJK_CHAR_RE.findall(text))
    non_cjk = CJK_CHAR_RE.sub(" ", text)
    alpha_words = len(re.findall(r"\w+", non_cjk))
    return alpha_words + (cjk_chars // 2)


def lint_entries(
    root: Path,
    entries: Dict[str, str],
    declared_domains: Dict[str, str],
    fingerprints: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """Return lint findings `{path, code, message}` for FRAS tag/size errors and quality issues."""
    findings: List[Dict[str, str]] = []

    def add(path: str, code: str, msg: str) -> None:
        findings.append({"path": path, "code": code, "message": msg})

    for rel, line in sorted(entries.items()):
        f = parse_entry_fields(line)
        if not f:
            continue
        fp = fingerprints.get(rel) if fingerprints is not None else None
        actual_l = int(fp["lines"]) if fp is not None else None
        for err_code, err_msg in validate_parsed_entry_tags(rel, f, line, actual_lines=actual_l):
            add(rel, err_code, err_msg)
        imp = int(f["imp"])
        f_units = semantic_word_count(f["f"])
        if f["f"] not in ("", "-") and "<todo" not in line.lower():
            if f_units < 3:
                add(rel, "F-too-short", f"F: has only {f_units} word-unit(s); name concrete objects and operations")
            elif VAGUE_F_RE.search(f["f"]) and f_units <= 5:
                add(rel, "F-vague", f"F: looks like filler (`{f['f']}`); state the actual responsibility")
        body = line.split("]:", 1)[1] if "]:" in line else line
        tokens = estimate_tokens(body)
        lo, hi = IMPORTANCE_TOKEN_BUDGET.get(imp, (15, 150))
        if tokens > int(hi * 1.5):
            add(rel, "budget-over", f"~{tokens} tokens exceeds Importance {imp} budget ({lo}-{hi}); trim or lower I")
        elif imp >= 5 and tokens < int(lo * 0.5):
            add(rel, "budget-under", f"~{tokens} tokens is thin for Importance {imp} (target {lo}-{hi})")
        if imp >= 8 and f["s"] in ("-", ""):
            add(rel, "S-empty-hub", f"Importance {imp} hub has `S:-`; capture its non-obvious invariants")
        if declared_domains and f["domain"] not in declared_domains:
            add(rel, "domain-undeclared", f"Domain `{f['domain']}` is not declared in aoci.meta.txt")

    for rel, refs in find_dangling_refs(root, entries).items():
        add(rel, "ref-dangling", f"R:/A: references missing path(s): {', '.join(refs)}")
    return findings


def parse_layer_rules(raw: str) -> Dict[str, Set[str]]:
    """Parse layer dependency restriction rules such as `D!->E,U; P->E,U` into `{src_layer: {dst_layers}}`."""
    rules: Dict[str, Set[str]] = {}
    if not raw or raw.strip() in ("", "-"):
        return rules
    norm = re.sub(r"\s*,\s*", ",", raw)
    norm = re.sub(r"\s*(!?->)\s*", r"\1", norm)
    for clause in re.split(r"[;\s]+", norm):
        clause = clause.strip()
        if not clause:
            continue
        if "!->" in clause:
            lhs, rhs = clause.split("!->", 1)
        elif "->" in clause:
            lhs, rhs = clause.split("->", 1)
        else:
            continue
        srcs = [ch for ch in (s.strip().upper() for s in lhs.split(",")) if len(ch) == 1 and ch in VALID_LAYERS]
        dsts = [ch for ch in (d.strip().upper() for d in rhs.split(",")) if len(ch) == 1 and ch in VALID_LAYERS]
        if srcs and dsts:
            for src in srcs:
                rules.setdefault(src, set()).update(dsts)
    return rules


def _load_layer_rules_from_aoci(aoci_dir: Path, manifest_spec: Optional[str] = None) -> Dict[str, Set[str]]:
    rules: Dict[str, Set[str]] = {}
    if manifest_spec:
        for s, dset in parse_layer_rules(manifest_spec).items():
            rules.setdefault(s, set()).update(dset)
    elif (aoci_dir / MANIFEST_FILE).is_file():
        try:
            for ln in (aoci_dir / MANIFEST_FILE).read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.startswith("#Layer-Rules:"):
                    for s, dset in parse_layer_rules(ln[len("#Layer-Rules:") :]).items():
                        rules.setdefault(s, set()).update(dset)
        except OSError:
            pass
    meta_path = aoci_dir / META_FILE
    if meta_path.is_file():
        try:
            for ln in meta_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.startswith("#Layer-Rules:"):
                    for s, dset in parse_layer_rules(ln[len("#Layer-Rules:") :]).items():
                        rules.setdefault(s, set()).update(dset)
        except OSError:
            pass
    return rules


def find_dependency_cycles(adj: Dict[str, Set[str]]) -> List[List[str]]:
    """Iterative Tarjan's strongly connected components algorithm (returns SCCs with >= 2 nodes)."""
    nodes = sorted(adj.keys())
    index_counter = 0
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    scc_stack: List[str] = []
    on_stack: Set[str] = set()
    sccs: List[List[str]] = []

    for start in nodes:
        if start in indices:
            continue
        indices[start] = lowlink[start] = index_counter
        index_counter += 1
        scc_stack.append(start)
        on_stack.add(start)
        call_stack: List[List[Any]] = [[start, sorted(p for p in adj.get(start, ()) if p in adj), 0]]

        while call_stack:
            v, nbs, ni = call_stack[-1]
            if ni < len(nbs):
                w = nbs[ni]
                call_stack[-1][2] = ni + 1
                if w not in indices:
                    indices[w] = lowlink[w] = index_counter
                    index_counter += 1
                    scc_stack.append(w)
                    on_stack.add(w)
                    call_stack.append([w, sorted(p for p in adj.get(w, ()) if p in adj), 0])
                elif w in on_stack and indices[w] < lowlink[v]:
                    lowlink[v] = indices[w]
            else:
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    if lowlink[v] < lowlink[parent]:
                        lowlink[parent] = lowlink[v]
                if lowlink[v] == indices[v]:
                    comp: List[str] = []
                    while True:
                        w = scc_stack.pop()
                        on_stack.remove(w)
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) >= 2:
                        sccs.append(sorted(comp))

    sccs.sort(key=lambda comp: (-len(comp), comp))
    return sccs


def check_layer_violations(
    entries: Dict[str, str],
    deps_adj: Dict[str, Set[str]],
    forbidden: Dict[str, Set[str]],
) -> List[Dict[str, str]]:
    """Check every directed dependency edge `(src -> dst)` against forbidden layer transitions."""
    if not forbidden:
        return []
    layers: Dict[str, str] = {}
    for rel, line in entries.items():
        f = parse_entry_fields(line)
        if f:
            layers[rel] = f["layer"]

    violations: List[Dict[str, str]] = []
    for src in sorted(deps_adj.keys()):
        src_l = layers.get(src)
        if not src_l:
            continue
        disallowed = forbidden.get(src_l)
        if not disallowed:
            continue
        for dst in sorted(deps_adj.get(src, ())):
            dst_l = layers.get(dst)
            if dst_l and dst_l in disallowed:
                violations.append(
                    {
                        "from": src,
                        "to": dst,
                        "from_layer": src_l,
                        "to_layer": dst_l,
                        "rule": f"{src_l}!->{dst_l}",
                        "code": "layer-violation",
                    }
                )
    return violations


def _sanitize_graph_label(text: str, fmt: str) -> str:
    cleaned = re.sub(r"[\r\n]+", " ", text).strip()
    cleaned = cleaned.replace("<", "").replace(">", "")
    if fmt == "mermaid":
        return cleaned.replace('"', "'")
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def export_dependency_graph(
    entries: Dict[str, str],
    deps_adj: Dict[str, Set[str]],
    dependents_adj: Dict[str, Set[str]],
    declared_domains: Dict[str, str],
    *,
    fmt: str = "mermaid",
    level: str = "file",
    group_by: str = "domain",
    direction: str = "LR",
    layer: Optional[str] = None,
    domain: Optional[str] = None,
    min_importance: int = 0,
    path_glob: Optional[str] = None,
    focus: Optional[str] = None,
    depth: int = 1,
) -> Dict[str, Any]:
    """Export filtered or aggregated FRAS dependency graph in Mermaid flowchart or Graphviz DOT syntax."""
    fmt_l = fmt.lower().strip()
    if fmt_l not in ("mermaid", "dot"):
        raise ValueError(f"Unsupported export format `{fmt}` (expected `mermaid` or `dot`).")
    level_l = level.lower().strip()
    if level_l not in ("file", "dir", "domain"):
        raise ValueError(f"Unsupported export level `{level}` (expected `file`, `dir`, or `domain`).")
    group_l = group_by.lower().strip()
    if group_l not in ("domain", "dir", "layer", "none"):
        raise ValueError(f"Unsupported group_by `{group_by}`.")
    dir_u = direction.upper().strip()
    if dir_u not in ("LR", "TD", "TB"):
        dir_u = "LR"

    layer_set = _parse_code_filter_set(layer)
    domain_set = _parse_code_filter_set(domain)
    focus_set: Optional[Set[str]] = None
    if focus:
        focus_rel = normalize_relpath(focus)
        focus_set = {focus_rel}
        if focus_rel in entries:
            focus_set.update(bfs_neighbors(deps_adj, [focus_rel], max(1, depth)))
            focus_set.update(bfs_neighbors(dependents_adj, [focus_rel], max(1, depth)))

    parsed_sel: Dict[str, Dict[str, str]] = {}
    for rel in sorted(entries.keys()):
        if focus_set is not None and rel not in focus_set:
            continue
        f = parse_entry_fields(entries[rel])
        if not f:
            continue
        if layer_set and f["layer"] not in layer_set:
            continue
        if domain_set and f["domain"] not in domain_set:
            continue
        if min_importance and int(f["imp"]) < min_importance:
            continue
        if path_glob and not fnmatch.fnmatchcase(rel, path_glob):
            continue
        parsed_sel[rel] = f

    node_labels: Dict[str, str] = {}
    node_groups: Dict[str, Optional[str]] = {}
    group_labels: Dict[str, str] = {}
    edge_counts: Dict[Tuple[str, str], int] = {}

    actual_group_by = group_l if level_l == "file" else "none"

    if level_l == "file":
        for rel, f in parsed_sel.items():
            node_labels[rel] = f"{rel} [{f['layer']}{f['domain']}{f['imp']}]"
            if group_l == "none":
                node_groups[rel] = None
            elif group_l == "domain":
                gk = f["domain"]
                node_groups[rel] = gk
                dom_name = declared_domains.get(gk)
                group_labels[gk] = f"{gk}: {dom_name}" if dom_name else f"Domain {gk}"
            elif group_l == "layer":
                gk = f["layer"]
                node_groups[rel] = gk
                group_labels[gk] = f"Layer {gk}"
            else:
                gk = (rel.rsplit("/", 1)[0] + "/") if "/" in rel else "./"
                node_groups[rel] = gk
                group_labels[gk] = gk
        for src in parsed_sel:
            for dst in sorted(deps_adj.get(src, ())):
                if dst in parsed_sel and dst != src:
                    edge_counts[(src, dst)] = edge_counts.get((src, dst), 0) + 1
    elif level_l == "dir":
        file_to_bucket: Dict[str, str] = {}
        bucket_counts: Dict[str, int] = {}
        for rel in parsed_sel:
            dk = (rel.rsplit("/", 1)[0] + "/") if "/" in rel else "./"
            file_to_bucket[rel] = dk
            bucket_counts[dk] = bucket_counts.get(dk, 0) + 1
        for dk, cnt in bucket_counts.items():
            node_labels[dk] = f"{dk} ({cnt})"
            node_groups[dk] = None
        for src, s_bucket in file_to_bucket.items():
            for dst in deps_adj.get(src, ()):
                d_bucket = file_to_bucket.get(dst)
                if d_bucket and d_bucket != s_bucket:
                    edge_counts[(s_bucket, d_bucket)] = edge_counts.get((s_bucket, d_bucket), 0) + 1
    else:
        file_to_dom: Dict[str, str] = {}
        dom_counts: Dict[str, int] = {}
        for rel, f in parsed_sel.items():
            dk = f["domain"]
            file_to_dom[rel] = dk
            dom_counts[dk] = dom_counts.get(dk, 0) + 1
        for dk, cnt in dom_counts.items():
            dom_name = declared_domains.get(dk)
            base_lbl = f"{dk}: {dom_name}" if dom_name else f"Domain {dk}"
            node_labels[dk] = f"{base_lbl} ({cnt})"
            node_groups[dk] = None
        for src, s_dom in file_to_dom.items():
            for dst in deps_adj.get(src, ()):
                d_dom = file_to_dom.get(dst)
                if d_dom and d_dom != s_dom:
                    edge_counts[(s_dom, d_dom)] = edge_counts.get((s_dom, d_dom), 0) + 1

    sorted_nodes = sorted(node_labels.keys())
    node_ids = {k: f"n{idx}" for idx, k in enumerate(sorted_nodes)}
    sorted_groups = sorted({g for g in node_groups.values() if g is not None})
    group_ids = {g: f"sg_{idx}" for idx, g in enumerate(sorted_groups)}
    sorted_edges = sorted(edge_counts.items(), key=lambda kv: kv[0])

    lines_out: List[str] = []
    if fmt_l == "mermaid":
        m_dir = "TD" if dir_u in ("TD", "TB") else "LR"
        lines_out.append(f"flowchart {m_dir}")
        for gk in sorted_groups:
            gid = group_ids[gk]
            glbl = _sanitize_graph_label(group_labels[gk], "mermaid")
            lines_out.append(f'  subgraph {gid}["{glbl}"]')
            for nk in sorted_nodes:
                if node_groups[nk] == gk:
                    nlbl = _sanitize_graph_label(node_labels[nk], "mermaid")
                    lines_out.append(f'    {node_ids[nk]}["{nlbl}"]')
            lines_out.append("  end")
        for nk in sorted_nodes:
            if node_groups[nk] is None:
                nlbl = _sanitize_graph_label(node_labels[nk], "mermaid")
                lines_out.append(f'  {node_ids[nk]}["{nlbl}"]')
        for (src, dst), cnt in sorted_edges:
            if cnt > 1:
                lines_out.append(f"  {node_ids[src]} -->|{cnt}| {node_ids[dst]}")
            else:
                lines_out.append(f"  {node_ids[src]} --> {node_ids[dst]}")
        out_dir = m_dir
    else:
        d_dir = "TB" if dir_u in ("TD", "TB") else "LR"
        lines_out.append("digraph aoci {")
        lines_out.append(f"  rankdir={d_dir};")
        lines_out.append('  node [shape=box];')
        for gk in sorted_groups:
            gid = f"cluster_{group_ids[gk]}"
            glbl = _sanitize_graph_label(group_labels[gk], "dot")
            lines_out.append(f"  subgraph {gid} {{")
            lines_out.append(f'    label="{glbl}";')
            for nk in sorted_nodes:
                if node_groups[nk] == gk:
                    nlbl = _sanitize_graph_label(node_labels[nk], "dot")
                    lines_out.append(f'    {node_ids[nk]} [label="{nlbl}"];')
            lines_out.append("  }")
        for nk in sorted_nodes:
            if node_groups[nk] is None:
                nlbl = _sanitize_graph_label(node_labels[nk], "dot")
                lines_out.append(f'  {node_ids[nk]} [label="{nlbl}"];')
        for (src, dst), cnt in sorted_edges:
            if cnt > 1:
                lines_out.append(f'  {node_ids[src]} -> {node_ids[dst]} [label="{cnt}"];')
            else:
                lines_out.append(f"  {node_ids[src]} -> {node_ids[dst]};")
        lines_out.append("}")
        out_dir = d_dir

    rendered = "\n".join(lines_out) + "\n"
    return {
        "ok": True,
        "format": fmt_l,
        "level": level_l,
        "group_by": actual_group_by,
        "direction": out_dir,
        "nodes": len(sorted_nodes),
        "edges": len(sorted_edges),
        "content": rendered,
    }


_CAMEL_PART_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|$)|\d+")
_CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+")


def _tokenize_search(text: str) -> List[str]:
    """Tokenize identifiers (camelCase, snake_case, paths) and CJK unigrams/bigrams for BM25 search."""
    if not text:
        return []
    tokens: List[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", text):
        low = word.lower()
        if len(low) >= 2 or low.isdigit():
            tokens.append(low)
        parts = _CAMEL_PART_RE.findall(word)
        if len(parts) > 1:
            for p in parts:
                pl = p.lower()
                if len(pl) >= 2:
                    tokens.append(pl)
    for run in _CJK_RUN_RE.findall(text):
        for ch in run:
            tokens.append(ch)
        for idx in range(len(run) - 1):
            tokens.append(run[idx : idx + 2])
    return tokens


_GLOSSARY_EXAMPLE_MARKERS = ("(example", "example:", "（示例", "示例:", "示例：")


def _parse_glossary_groups(text: str) -> List[List[str]]:
    groups: List[List[str]] = []
    for raw_grp in re.split(r"[;；\n]+", text):
        grp = raw_grp.strip()
        if not grp or ("=" not in grp and "＝" not in grp):
            continue
        sides = re.split(r"[=＝]+", grp)
        aliases: List[str] = []
        seen_low: Set[str] = set()
        for side in sides:
            for item in re.split(r"[,，、|]+", side):
                term = re.sub(r"\s+", " ", item.strip().strip("\"'`"))
                if not term or term == "-":
                    continue
                low = term.lower()
                if len(low) == 1 and ord(low) < 128:
                    continue
                if low not in seen_low:
                    seen_low.add(low)
                    aliases.append(term)
        if len(aliases) >= 2:
            groups.append(aliases)
    return groups


def parse_glossary(aoci_dir: Path) -> List[List[str]]:
    """
    Parse bilingual / synonym project glossary groups from `.aoci/aoci.meta.txt`
    (`#Glossary: a = b` or `# [Glossary...]` sections) and optional `.aoci/glossary.txt`.
    """
    groups: List[List[str]] = []
    seen_sigs: Set[Tuple[str, ...]] = set()

    def _add_groups(raw_groups: Sequence[List[str]]) -> None:
        for grp in raw_groups:
            sig = tuple(sorted(t.lower() for t in grp))
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                groups.append(list(grp))

    meta_path = aoci_dir / META_FILE
    if meta_path.is_file():
        try:
            in_glossary_section = False
            for raw in meta_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if re.match(r"^#\s*\[\s*Glossary\b", line, re.IGNORECASE):
                    in_glossary_section = True
                    continue
                if re.match(r"^#Glossary\s*:", line, re.IGNORECASE):
                    body = line.split(":", 1)[1].strip()
                    _add_groups(_parse_glossary_groups(body))
                    continue
                if in_glossary_section:
                    if line.startswith("# ["):
                        in_glossary_section = False
                        continue
                    if not line.startswith("#"):
                        if line:
                            in_glossary_section = False
                        continue
                    body = line.lstrip("#").strip()
                    low = body.lower()
                    if not body or any(marker in low for marker in _GLOSSARY_EXAMPLE_MARKERS):
                        continue
                    _add_groups(_parse_glossary_groups(body))
        except OSError:
            pass

    glossary_path = aoci_dir / GLOSSARY_FILE
    if glossary_path.is_file():
        try:
            for raw in glossary_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                low = line.lower()
                if any(marker in low for marker in _GLOSSARY_EXAMPLE_MARKERS):
                    continue
                _add_groups(_parse_glossary_groups(line))
        except OSError:
            pass

    return groups


def _query_contains_alias(query_text: str, camel_norm_query: str, alias: str) -> bool:
    if not alias:
        return False
    if CJK_CHAR_RE.search(alias):
        return alias.lower() in query_text.lower()
    parts = [re.escape(p) for p in re.split(r"[\s_-]+", alias.strip()) if p]
    if not parts:
        return False
    pat = re.compile(rf"(?<![A-Za-z0-9]){r'[\s_-]+'.join(parts)}(?![A-Za-z0-9])", re.IGNORECASE)
    return bool(pat.search(query_text) or (camel_norm_query != query_text and pat.search(camel_norm_query)))


def expand_query_with_glossary(
    query_text: str,
    glossary: Sequence[Sequence[str]],
) -> Tuple[Dict[str, float], Dict[str, List[str]], List[Dict[str, Any]]]:
    """
    Expand `query_text` across declared glossary synonym groups.
    Returns `(token_weights, token_sources, expansions_meta)`.
    Direct query tokens receive weight `1.0`; glossary-added tokens receive weight `0.85`.
    """
    token_weights: Dict[str, float] = {}
    token_sources: Dict[str, List[str]] = {}
    for qt in _tokenize_search(query_text):
        token_weights[qt] = 1.0

    expansions: List[Dict[str, Any]] = []
    if not glossary or not query_text.strip():
        return token_weights, token_sources, expansions

    camel_norm_query = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", query_text)
    for group in glossary:
        matched_aliases = [al for al in group if _query_contains_alias(query_text, camel_norm_query, al)]
        if not matched_aliases:
            continue
        matched_set_low = {al.lower() for al in matched_aliases}
        others = [al for al in group if al.lower() not in matched_set_low]
        if not others:
            continue
        primary_src = " / ".join(matched_aliases)
        expansions.append({"term": primary_src, "aliases": others})
        for other in others:
            src_label = f"{primary_src} -> {other}"
            for tok in _tokenize_search(other):
                if token_weights.get(tok, 0.0) < 0.85:
                    token_weights[tok] = 0.85
                src_list = token_sources.setdefault(tok, [])
                if src_label not in src_list:
                    src_list.append(src_label)

    return token_weights, token_sources, expansions


class BM25Index:
    """Pre-tokenized field-weighted Okapi BM25 + Importance inverted index."""

    def __init__(self, entries: Dict[str, str]) -> None:
        self.parsed: Dict[str, Dict[str, Any]] = {}
        self.postings: Dict[str, List[Tuple[int, float]]] = {}
        self.df: Dict[str, int] = {}
        self.doc_meta: List[Tuple[str, str, int, float]] = []

        for rel, line in sorted(entries.items()):
            f = parse_entry_fields(line)
            if not f:
                continue
            imp = int(f["imp"])
            self.parsed[rel] = {
                "rel": rel,
                "line": line,
                "layer": f["layer"],
                "domain": f["domain"],
                "imp": imp,
                "size": f["size"],
                "f": f["f"],
                "r": f["r"],
                "a": f["a"],
                "s": f["s"],
            }
            a_lhs_parts = [chunk.split("<-", 1)[0] for chunk in re.split(r"[,;]", f["a"])]
            a_rhs_parts = [chunk.split("<-", 1)[1] for chunk in re.split(r"[,;]", f["a"]) if "<-" in chunk]

            tf: Dict[str, float] = {}
            doc_len = 0.0
            zones = (
                (rel, 2.5),
                (" ".join(a_lhs_parts), 2.2),
                (f["f"], 2.0),
                (f["s"], 1.5),
                (f["r"] + " " + " ".join(a_rhs_parts), 1.0),
            )
            for z_txt, weight in zones:
                if not z_txt or z_txt.strip() == "-":
                    continue
                for tok in _tokenize_search(z_txt):
                    tf[tok] = tf.get(tok, 0.0) + weight
                    doc_len += weight

            doc_idx = len(self.doc_meta)
            self.doc_meta.append((rel, line, imp, max(1.0, doc_len)))
            for tok, freq in tf.items():
                self.postings.setdefault(tok, []).append((doc_idx, freq))
                self.df[tok] = self.df.get(tok, 0) + 1

        self.n_docs = len(self.doc_meta)
        self.avgdl = (sum(d[3] for d in self.doc_meta) / self.n_docs) if self.n_docs else 1.0

    def _search_internal(
        self,
        query_text: str,
        candidate_paths: Optional[Set[str]] = None,
        top_k: int = 20,
        glossary: Optional[Sequence[Sequence[str]]] = None,
    ) -> Tuple[List[Tuple[float, Dict[str, Any]]], List[Dict[str, Any]]]:
        if candidate_paths is not None and not candidate_paths:
            return [], []
        token_weights, token_sources, expansions = expand_query_with_glossary(query_text, glossary or ())
        if not token_weights or self.n_docs == 0:
            return [], expansions

        k1 = 1.5
        b = 0.75
        scores: Dict[int, float] = {}
        doc_term_contrib: Dict[int, Dict[str, float]] = {}
        doc_gloss_contrib: Dict[int, Dict[str, float]] = {}
        for qt, q_weight in token_weights.items():
            plist = self.postings.get(qt)
            if not plist:
                continue
            doc_f = self.df[qt]
            idf = math.log(1.0 + (self.n_docs - doc_f + 0.5) / (doc_f + 0.5))
            g_sources = token_sources.get(qt) or ()
            for doc_idx, freq in plist:
                rel, _, _, dl = self.doc_meta[doc_idx]
                if candidate_paths is not None and rel not in candidate_paths:
                    continue
                denom = freq + k1 * (1.0 - b + b * (dl / self.avgdl))
                contrib = q_weight * idf * ((freq * (k1 + 1.0)) / denom)
                scores[doc_idx] = scores.get(doc_idx, 0.0) + contrib
                tc = doc_term_contrib.setdefault(doc_idx, {})
                tc[qt] = tc.get(qt, 0.0) + contrib
                if g_sources:
                    gc = doc_gloss_contrib.setdefault(doc_idx, {})
                    for src_lbl in g_sources:
                        gc[src_lbl] = gc.get(src_lbl, 0.0) + contrib

        ranked: List[Tuple[float, int, str, str, int]] = []
        for doc_idx, raw_score in scores.items():
            if raw_score <= 0.0:
                continue
            rel, line, imp, _ = self.doc_meta[doc_idx]
            sc = raw_score * (1.0 + 0.06 * (imp - 5))
            ranked.append((sc, imp, rel, line, doc_idx))

        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        limit = max(1, top_k) if top_k > 0 else len(ranked)
        out: List[Tuple[float, Dict[str, Any]]] = []
        for sc, _, rel, line, doc_idx in ranked[:limit]:
            terms_sorted = [
                t for t, _ in sorted(doc_term_contrib.get(doc_idx, {}).items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            gloss_sorted = [
                g for g, _ in sorted(doc_gloss_contrib.get(doc_idx, {}).items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            hit: Dict[str, Any] = {
                "path": rel,
                "score": round(sc, 3),
                "matched_terms": terms_sorted[:12],
                "entry": line,
            }
            if gloss_sorted:
                hit["matched_glossary"] = gloss_sorted[:6]
            out.append((sc, hit))
        return out, expansions

    def search_detailed(
        self,
        query_text: str,
        candidate_paths: Optional[Set[str]] = None,
        top_k: int = 20,
        glossary: Optional[Sequence[Sequence[str]]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        rows, expansions = self._search_internal(
            query_text,
            candidate_paths=candidate_paths,
            top_k=top_k,
            glossary=glossary,
        )
        return [hit for _, hit in rows], expansions

    def search(
        self,
        query_text: str,
        candidate_paths: Optional[Set[str]] = None,
        top_k: int = 20,
        glossary: Optional[Sequence[Sequence[str]]] = None,
    ) -> List[Tuple[float, str, str]]:
        rows, _ = self._search_internal(
            query_text,
            candidate_paths=candidate_paths,
            top_k=top_k,
            glossary=glossary,
        )
        return [(sc, hit["path"], hit["entry"]) for sc, hit in rows]


def search_entries_bm25(
    entries: Dict[str, str],
    query_text: str,
    candidate_paths: Optional[Set[str]] = None,
    top_k: int = 20,
    index_cls: Any = None,
    glossary: Optional[Sequence[Sequence[str]]] = None,
) -> List[Tuple[float, str, str]]:
    """Rank FRAS entries with field-weighted Okapi BM25 + Importance centrality prior."""
    if not entries or (candidate_paths is not None and not candidate_paths):
        return []
    sub = entries if candidate_paths is None else {p: entries[p] for p in candidate_paths if p in entries}
    if not sub:
        return []
    cls = index_cls or BM25Index
    return cls(sub).search(query_text, top_k=top_k, glossary=glossary)


def search_entries_bm25_detailed(
    entries: Dict[str, str],
    query_text: str,
    candidate_paths: Optional[Set[str]] = None,
    top_k: int = 20,
    index_cls: Any = None,
    glossary: Optional[Sequence[Sequence[str]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Rank FRAS entries with BM25 + glossary expansion and return per-hit term attribution."""
    if not entries or (candidate_paths is not None and not candidate_paths):
        return [], []
    sub = entries if candidate_paths is None else {p: entries[p] for p in candidate_paths if p in entries}
    if not sub:
        return [], []
    cls = index_cls or BM25Index
    idx = cls(sub)
    if hasattr(idx, "search_detailed"):
        return idx.search_detailed(query_text, top_k=top_k, glossary=glossary)
    ranked = idx.search(query_text, top_k=top_k)
    return [{"path": r, "score": round(sc, 3), "matched_terms": [], "entry": ln} for sc, r, ln in ranked], []
