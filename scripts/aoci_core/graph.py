"""FRAS graph builder, context budgeting, linting, BM25 search, architecture audit & export."""

from __future__ import annotations

import fnmatch
import html
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

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
    compute_size_tier,
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


class ParsedEntry:
    """Lightweight slotted representation of a parsed `object-fras-v2` line."""

    __slots__ = ("rel", "layer", "domain", "imp", "size", "f", "r", "a", "s", "raw")

    def __init__(
        self,
        rel: str,
        layer: str,
        domain: str,
        imp: int,
        size: str,
        f: str,
        r: str,
        a: str,
        s: str,
        raw: str,
    ) -> None:
        self.rel = rel
        self.layer = layer
        self.domain = domain
        self.imp = imp
        self.size = size
        self.f = f
        self.r = r
        self.a = a
        self.s = s
        self.raw = raw


def parse_entry_fields(line: str) -> Optional[Dict[str, str]]:
    m = ENTRY_RE.match(line)
    if not m:
        return None
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in m.groupdict().items()}


def parse_entry(rel: str, line: str) -> Optional[ParsedEntry]:
    """Parse one `object-fras-v2` line into a `ParsedEntry`, or `None` if malformed."""
    m = ENTRY_RE.match(line)
    if not m:
        return None
    return ParsedEntry(
        rel=rel,
        layer=m.group("layer"),
        domain=m.group("domain"),
        imp=int(m.group("imp")),
        size=m.group("size"),
        f=m.group("f").strip(),
        r=m.group("r").strip(),
        a=m.group("a").strip(),
        s=m.group("s").strip(),
        raw=line,
    )


def parse_entries_map(entries: Dict[str, str]) -> Dict[str, ParsedEntry]:
    """Parse all valid `object-fras-v2` lines in `entries` in a single pass."""
    parsed: Dict[str, ParsedEntry] = {}
    for rel, line in entries.items():
        pe = parse_entry(rel, line)
        if pe is not None:
            parsed[rel] = pe
    return parsed


def _validate_tag_fields(
    rel: str,
    layer: str,
    size: str,
    f_val: str,
    raw_line: str,
    actual_lines: Optional[int] = None,
) -> List[Tuple[str, str]]:
    errs: List[Tuple[str, str]] = []
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


def validate_parsed_entry_tags(
    rel: str,
    fields: Dict[str, str],
    raw_line: str,
    actual_lines: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """Shared [LDIS] tag & field validator used across `upsert`, `verify`, `scan`, `ack`, `lint`, and `pool`."""
    return _validate_tag_fields(
        rel,
        fields["layer"],
        fields["size"],
        fields["f"],
        raw_line,
        actual_lines=actual_lines,
    )


def _build_fras_graph_from_parsed(
    known: Set[str],
    parsed: Mapping[str, ParsedEntry],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    deps: Dict[str, Set[str]] = {rel: set() for rel in known}
    dependents: Dict[str, Set[str]] = {rel: set() for rel in known}
    for rel, pe in parsed.items():
        for dep in extract_path_refs(pe.r, known):
            if dep != rel:
                deps[rel].add(dep)
                dependents[dep].add(rel)
        for caller in extract_path_refs(pe.a, known):
            if caller != rel:
                deps[caller].add(rel)
                dependents[rel].add(caller)
    return deps, dependents


def build_fras_graph(
    entries: Dict[str, str],
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Build directed edges `X -> Y` (`X` depends on `Y`) and reverse callers in O(total refs)."""
    parsed = parsed_entries if parsed_entries is not None else parse_entries_map(entries)
    return _build_fras_graph_from_parsed(set(entries.keys()), parsed)


def _build_explicit_mention_index_from_parsed(
    known: Set[str],
    parsed: Mapping[str, ParsedEntry],
) -> Dict[str, Set[str]]:
    mentioned_by: Dict[str, Set[str]] = {rel: set() for rel in known}
    for rel, pe in parsed.items():
        for target in extract_path_refs(pe.r, known) + extract_path_refs(pe.a, known):
            if target != rel:
                mentioned_by[target].add(rel)
    return mentioned_by


def build_explicit_mention_index(
    entries: Dict[str, str],
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
) -> Dict[str, Set[str]]:
    """Map each indexed path `P` to every entry whose `R:` or `A:` explicitly mentions `P`."""
    parsed = parsed_entries if parsed_entries is not None else parse_entries_map(entries)
    return _build_explicit_mention_index_from_parsed(set(entries.keys()), parsed)


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
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
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
            pe = parsed_entries.get(rel) if parsed_entries is not None else parse_entry(rel, line)
            imp = pe.imp if pe is not None else 0
            has_s = 0 if (pe is not None and pe.s not in ("-", "")) else 1
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
        "truncated": bool(omitted_paths or used_tokens > budget),
        "omitted_count": len(omitted_paths),
        "omitted_paths": omitted_paths,
    }


def attach_budgeted_context_extras(
    payload: Dict[str, Any],
    hub_sections: Sequence[Dict[str, Any]],
    impacted_tests: Optional[Sequence[Dict[str, Any]]],
    target_paths: Sequence[str],
    budget: int,
) -> None:
    """Attach `hub_sections` and `impacted_tests` to a `context` payload while enforcing `--budget`."""
    used_tokens = int(payload.get("estimated_tokens", 0) or 0)
    target_set = {normalize_relpath(str(p)) for p in target_paths if str(p).strip()}

    if hub_sections:
        if budget > 0:
            ranked_hubs = [
                sec
                for _, sec in sorted(
                    enumerate(hub_sections),
                    key=lambda idx_sec: (0 if str(idx_sec[1].get("path", "")) in target_set else 1, idx_sec[0]),
                )
            ]
            kept_hubs: List[Dict[str, Any]] = []
            hub_omitted = 0
            for sec in ranked_hubs:
                cost = estimate_tokens(str(sec.get("entry", "")))
                if used_tokens + cost <= budget:
                    used_tokens += cost
                    kept_hubs.append(sec)
                else:
                    hub_omitted += 1
            if kept_hubs:
                payload["hub_sections"] = kept_hubs
            if hub_omitted > 0:
                payload["hub_sections_omitted"] = hub_omitted
                payload["truncated"] = True
        else:
            payload["hub_sections"] = list(hub_sections)
            for sec in hub_sections:
                used_tokens += estimate_tokens(str(sec.get("entry", "")))

    if impacted_tests is not None:
        if budget > 0:
            kept_tests: List[Dict[str, Any]] = []
            tests_omitted = 0
            for item in impacted_tests:
                cost = estimate_tokens(str(item.get("entry", "")))
                if used_tokens + cost <= budget:
                    used_tokens += cost
                    kept_tests.append(item)
                else:
                    tests_omitted += 1
            payload["impacted_tests"] = kept_tests
            if tests_omitted > 0:
                payload["impacted_tests_omitted"] = tests_omitted
                payload["truncated"] = True
        else:
            payload["impacted_tests"] = list(impacted_tests)
            for item in impacted_tests:
                used_tokens += estimate_tokens(str(item.get("entry", "")))

    payload["estimated_tokens"] = used_tokens
    if budget > 0 and used_tokens > budget:
        payload["truncated"] = True


_FILE_EXT_TAIL_RE = re.compile(r"(\.[A-Za-z][A-Za-z0-9]*)$")
_ROOT_DOTFILES_REF = frozenset({".gitignore", ".dockerignore", ".editorconfig", ".env"})


def _find_dangling_refs_from_parsed(
    root: Path,
    known: Set[str],
    parsed: Mapping[str, ParsedEntry],
    existing_paths: Optional[Set[str]] = None,
) -> Dict[str, List[str]]:
    known_top_dirs = {p.split("/", 1)[0] for p in known if "/" in p}
    if existing_paths:
        known_top_dirs.update(p.split("/", 1)[0] for p in existing_paths if "/" in p)
    exists_cache: Dict[str, bool] = {}
    dir_cache: Dict[str, bool] = {}
    dangling: Dict[str, List[str]] = {}

    def _path_exists(rel_ref: str) -> bool:
        if existing_paths is not None and rel_ref in existing_paths:
            return True
        cached = exists_cache.get(rel_ref)
        if cached is not None:
            return cached
        res = (root / rel_ref).exists()
        exists_cache[rel_ref] = res
        return res

    def _dir_exists(top_dir: str) -> bool:
        if top_dir in known_top_dirs:
            return True
        cached = dir_cache.get(top_dir)
        if cached is not None:
            return cached
        res = (root / top_dir).is_dir()
        dir_cache[top_dir] = res
        return res

    def _ref_segments(field: str, is_api_field: bool) -> List[str]:
        if not field or field.strip() == "-":
            return []
        if not is_api_field:
            return [field]
        segs: List[str] = []
        for chunk in re.split(r"[,;]", field):
            segs.append(chunk.split("<-", 1)[1] if "<-" in chunk else chunk)
        return segs

    for rel, pe in parsed.items():
        bad: List[str] = []
        for field, is_api in ((pe.r, False), (pe.a, True)):
            for seg in _ref_segments(field, is_api):
                cleaned = seg.replace("<-", " ")
                for tok in RELPATH_TOKEN_RE.findall(cleaned):
                    ref = normalize_relpath(tok.lstrip("-").rstrip("."))
                    if not ref or ref in known or (existing_paths is not None and ref in existing_paths):
                        continue
                    bname = ref.rsplit("/", 1)[-1]
                    m_ext = _FILE_EXT_TAIL_RE.search(bname)
                    if not m_ext:
                        continue
                    ext = m_ext.group(1).lower()
                    top_dir = ref.split("/", 1)[0]
                    if not (ext in _LIKELY_FILE_EXTS or _dir_exists(top_dir)):
                        continue
                    if _path_exists(ref):
                        continue
                    bad.append(ref)
                for tok in ROOT_FILE_TOKEN_RE.findall(cleaned):
                    ref = normalize_relpath(tok.rstrip("."))
                    if not ref or "/" in ref or ref in known or (existing_paths is not None and ref in existing_paths):
                        continue
                    m_ext = _FILE_EXT_TAIL_RE.search(ref)
                    if not ((m_ext and m_ext.group(1).lower() in _LIKELY_FILE_EXTS) or ref in _ROOT_DOTFILES_REF):
                        continue
                    if _path_exists(ref):
                        continue
                    bad.append(ref)
        if bad:
            dangling[rel] = sorted(set(bad))
    return dangling


def find_dangling_refs(
    root: Path,
    entries: Dict[str, str],
    *,
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
    existing_paths: Optional[Set[str]] = None,
) -> Dict[str, List[str]]:
    """Path-like R:/A: references that point neither to an indexed entry nor to a file/dir on disk."""
    parsed = parsed_entries if parsed_entries is not None else parse_entries_map(entries)
    return _find_dangling_refs_from_parsed(root, set(entries.keys()), parsed, existing_paths=existing_paths)


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
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
) -> List[Dict[str, str]]:
    """Return lint findings `{path, code, message}` for FRAS tag/size errors and quality issues."""
    findings: List[Dict[str, str]] = []
    parsed = parsed_entries if parsed_entries is not None else parse_entries_map(entries)

    def add(path: str, code: str, msg: str) -> None:
        findings.append({"path": path, "code": code, "message": msg})

    for rel in sorted(parsed.keys()):
        pe = parsed[rel]
        fp = fingerprints.get(rel) if fingerprints is not None else None
        actual_l = int(fp["lines"]) if fp is not None else None
        for err_code, err_msg in _validate_tag_fields(rel, pe.layer, pe.size, pe.f, pe.raw, actual_lines=actual_l):
            add(rel, err_code, err_msg)
        f_units = semantic_word_count(pe.f)
        if pe.f not in ("", "-") and "<todo" not in pe.raw.lower():
            if f_units < 3:
                add(rel, "F-too-short", f"F: has only {f_units} word-unit(s); name concrete objects and operations")
            elif VAGUE_F_RE.search(pe.f) and f_units <= 5:
                add(rel, "F-vague", f"F: looks like filler (`{pe.f}`); state the actual responsibility")
        body = pe.raw.split("]:", 1)[1] if "]:" in pe.raw else pe.raw
        tokens = estimate_tokens(body)
        lo, hi = IMPORTANCE_TOKEN_BUDGET.get(pe.imp, (15, 150))
        if tokens > int(hi * 1.5):
            add(rel, "budget-over", f"~{tokens} tokens exceeds Importance {pe.imp} budget ({lo}-{hi}); trim or lower I")
        elif pe.imp >= 5 and tokens < int(lo * 0.5):
            add(rel, "budget-under", f"~{tokens} tokens is thin for Importance {pe.imp} (target {lo}-{hi})")
        if pe.imp >= 8 and pe.s in ("-", ""):
            add(rel, "S-empty-hub", f"Importance {pe.imp} hub has `S:-`; capture its non-obvious invariants")
        if declared_domains and pe.domain not in declared_domains:
            add(rel, "domain-undeclared", f"Domain `{pe.domain}` is not declared in aoci.meta.txt")

    existing_fp = set(fingerprints.keys()) if fingerprints is not None else None
    for rel, refs in _find_dangling_refs_from_parsed(
        root,
        set(entries.keys()),
        parsed,
        existing_paths=existing_fp,
    ).items():
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


def _check_layer_violations_from_parsed(
    parsed: Mapping[str, ParsedEntry],
    deps_adj: Dict[str, Set[str]],
    forbidden: Dict[str, Set[str]],
) -> List[Dict[str, str]]:
    if not forbidden:
        return []
    layers = {rel: pe.layer for rel, pe in parsed.items()}

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


def check_layer_violations(
    entries: Dict[str, str],
    deps_adj: Dict[str, Set[str]],
    forbidden: Dict[str, Set[str]],
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
) -> List[Dict[str, str]]:
    """Check every directed dependency edge `(src -> dst)` against forbidden layer transitions."""
    if not forbidden:
        return []
    parsed = parsed_entries if parsed_entries is not None else parse_entries_map(entries)
    return _check_layer_violations_from_parsed(parsed, deps_adj, forbidden)


def _sanitize_graph_label(text: str, fmt: str) -> str:
    cleaned = re.sub(r"[\r\n]+", " ", text).strip()
    cleaned = cleaned.replace("<", "").replace(">", "")
    if fmt == "mermaid":
        return cleaned.replace('"', "'")
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def _render_html_dependency_graph(
    *,
    level: str,
    group_by: str,
    direction: str,
    sorted_nodes: Sequence[str],
    node_ids: Mapping[str, str],
    node_labels: Mapping[str, str],
    node_groups: Mapping[str, Optional[str]],
    group_labels: Mapping[str, str],
    sorted_edges: Sequence[Tuple[Tuple[str, str], int]],
    parsed_sel: Mapping[str, ParsedEntry],
    declared_domains: Mapping[str, str],
) -> str:
    proj_adj: Dict[str, Set[str]] = {nk: set() for nk in sorted_nodes}
    for (src, dst), _ in sorted_edges:
        proj_adj.setdefault(src, set()).add(dst)
    sccs = find_dependency_cycles(proj_adj)
    cycle_node_to_idx: Dict[str, int] = {}
    for c_idx, comp in enumerate(sccs, start=1):
        for nk in comp:
            cycle_node_to_idx[nk] = c_idx

    nodes_payload: List[Dict[str, Any]] = []
    for nk in sorted_nodes:
        pe = parsed_sel.get(nk) if level == "file" else None
        gk = node_groups.get(nk)
        dom_code = pe.domain if pe is not None else (nk if level == "domain" else "")
        nodes_payload.append(
            {
                "id": node_ids[nk],
                "key": nk,
                "label": node_labels[nk],
                "group": gk or "",
                "groupLabel": group_labels.get(gk, gk or "ungrouped") if gk else "ungrouped",
                "layer": pe.layer if pe is not None else "",
                "domain": dom_code,
                "domainName": declared_domains.get(dom_code, ""),
                "imp": pe.imp if pe is not None else 5,
                "size": pe.size if pe is not None else "",
                "f": pe.f if pe is not None else "",
                "r": pe.r if pe is not None else "",
                "a": pe.a if pe is not None else "",
                "s": pe.s if pe is not None else "",
                "cycle": cycle_node_to_idx.get(nk, 0),
            }
        )

    edges_payload: List[Dict[str, Any]] = []
    for (src, dst), cnt in sorted_edges:
        c_src = cycle_node_to_idx.get(src, 0)
        c_dst = cycle_node_to_idx.get(dst, 0)
        in_cycle = bool(c_src and c_src == c_dst)
        edges_payload.append(
            {
                "src": node_ids[src],
                "dst": node_ids[dst],
                "count": cnt,
                "inCycle": in_cycle,
            }
        )

    graph_json = json.dumps(
        {
            "level": level,
            "groupBy": group_by,
            "direction": direction,
            "cycles": sccs,
            "nodes": nodes_payload,
            "edges": edges_payload,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    title_txt = html.escape(f"AOCI Dependency Graph ({level}, {len(nodes_payload)} nodes)")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_txt}</title>
<style>
  :root {{ --bg:#0f172a; --panel:#1e293b; --border:#334155; --text:#e2e8f0; --muted:#94a3b8; --accent:#38bdf8; --danger:#f43f5e; --warn:#f59e0b; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); display:flex; flex-direction:column; height:100vh; }}
  header {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:10px 14px; background:var(--panel); border-bottom:1px solid var(--border); }}
  header input, header select, header button {{ background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px 10px; font-size:13px; }}
  header label {{ font-size:13px; color:var(--muted); display:flex; align-items:center; gap:5px; }}
  .stats {{ margin-left:auto; font-size:12px; color:var(--muted); }}
  main {{ flex:1; display:flex; min-height:0; }}
  #canvas-wrap {{ flex:1; position:relative; overflow:hidden; background:radial-gradient(circle at top left,#172554 0%,var(--bg) 55%); }}
  svg {{ display:block; width:100%; height:100%; cursor:grab; user-select:none; }}
  svg.grabbing {{ cursor:grabbing; }}
  #drawer {{ width:340px; max-width:40vw; background:var(--panel); border-left:1px solid var(--border); padding:14px; overflow:auto; font-size:13px; line-height:1.45; }}
  .kv {{ margin:8px 0; }}
  .kv .k {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .kv .v {{ white-space:pre-wrap; word-break:break-word; }}
  .badge {{ display:inline-block; padding:2px 7px; border-radius:999px; font-size:11px; background:#0c4a6e; color:var(--accent); margin-right:4px; }}
  .badge.risk {{ background:#451a03; color:var(--warn); }}
  .badge.cycle {{ background:#4c0519; color:var(--danger); }}
  .node-box {{ cursor:pointer; transition:opacity .15s; }}
  .node-box.dim {{ opacity:0.18; }}
  .node-box rect {{ fill:#1e293b; stroke:#475569; stroke-width:1.4; rx:6; }}
  .node-box.selected rect {{ stroke:var(--accent); stroke-width:2.4; fill:#0f2942; }}
  .node-box.cycle rect {{ stroke:var(--danger); stroke-width:2; }}
  .node-box text {{ fill:var(--text); font-size:11px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .edge {{ fill:none; stroke:#475569; stroke-width:1.25; opacity:0.65; transition:opacity .15s; }}
  .edge.hi {{ stroke:var(--accent); stroke-width:2.2; opacity:1; }}
  .edge.cycle {{ stroke:var(--danger); stroke-width:1.9; opacity:0.9; }}
  .edge.dim {{ opacity:0.18; }}
  .nbr-btn {{ display:block; width:100%; text-align:left; background:var(--bg); color:var(--accent); border:1px solid var(--border); border-radius:4px; padding:4px 8px; margin:3px 0; font-size:12px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; cursor:pointer; }}
  .nbr-btn:hover {{ border-color:var(--accent); }}
</style>
</head>
<body>
<header>
  <strong style="font-size:14px;">AOCI Graph</strong>
  <input id="q" type="search" placeholder="Search path, F:, S:...">
  <select id="layer"><option value="">All Layers</option></select>
  <select id="domain"><option value="">All Domains</option></select>
  <select id="minImp">
    <option value="0">Imp >= 0</option>
    <option value="5">Imp >= 5</option>
    <option value="7">Imp >= 7</option>
    <option value="9">Imp >= 9</option>
  </select>
  <select id="layoutMode">
    <option value="grouped">Grouped</option>
    <option value="layered">Layered (E→A→D→P→C)</option>
  </select>
  <label><input id="cyclesOnly" type="checkbox"> Cycles Only</label>
  <button id="zoomIn" type="button" title="Zoom In">+</button>
  <button id="zoomOut" type="button" title="Zoom Out">-</button>
  <button id="zoomReset" type="button" title="Reset Zoom">100%</button>
  <button id="resetBtn" type="button">Reset</button>
  <span class="stats" id="stats"></span>
</header>
<main>
  <div id="canvas-wrap"><svg id="svg" xmlns="http://www.w3.org/2000/svg"></svg></div>
  <aside id="drawer"><p style="color:var(--muted)">Click any node to inspect FRAS metadata, R:/A: links, S: risk notes, and cycle membership.</p></aside>
</main>
<script type="application/json" id="aoci-graph-data">{graph_json}</script>
<script>
(function() {{
  const data = JSON.parse(document.getElementById("aoci-graph-data").textContent);
  const svg = document.getElementById("svg");
  const drawer = document.getElementById("drawer");
  const qEl = document.getElementById("q");
  const layerEl = document.getElementById("layer");
  const domainEl = document.getElementById("domain");
  const minImpEl = document.getElementById("minImp");
  const layoutEl = document.getElementById("layoutMode");
  const cyclesEl = document.getElementById("cyclesOnly");
  const statsEl = document.getElementById("stats");
  const zoomInBtn = document.getElementById("zoomIn");
  const zoomOutBtn = document.getElementById("zoomOut");
  const zoomResetBtn = document.getElementById("zoomReset");
  const ns = "http://www.w3.org/2000/svg";
  const LAYER_ORDER = ["E", "U", "M", "A", "D", "K", "P", "I", "C", "R", "T", "O"];
  const isTD = data.direction === "TD" || data.direction === "TB";
  const nodeById = new Map(data.nodes.map(n => [n.id, n]));
  const layers = Array.from(new Set(data.nodes.map(n => n.layer).filter(Boolean))).sort();
  const domains = Array.from(new Set(data.nodes.map(n => n.domain).filter(Boolean))).sort();
  layers.forEach(l => {{ const o = document.createElement("option"); o.value = l; o.textContent = "Layer " + l; layerEl.appendChild(o); }});
  domains.forEach(d => {{ const o = document.createElement("option"); o.value = d; o.textContent = "Domain " + d; domainEl.appendChild(o); }});
  let selectedId = null;
  let panX = 0, panY = 0, scale = 1.0;
  let viewportG = null;
  let dragging = false, didDrag = false, dragSX = 0, dragSY = 0, basePX = 0, basePY = 0;

  function applyTransform() {{
    if (viewportG) {{
      viewportG.setAttribute("transform", `translate(${{panX}} ${{panY}}) scale(${{scale}})`);
    }}
    zoomResetBtn.textContent = Math.round(scale * 100) + "%";
  }}

  function zoomAt(factor, clientX, clientY) {{
    const next = Math.max(0.25, Math.min(3.0, scale * factor));
    const ratio = next / scale;
    const rect = svg.getBoundingClientRect();
    const ox = (clientX !== undefined ? clientX - rect.left : rect.width / 2);
    const oy = (clientY !== undefined ? clientY - rect.top : rect.height / 2);
    panX = ox - (ox - panX) * ratio;
    panY = oy - (oy - panY) * ratio;
    scale = next;
    applyTransform();
  }}

  function addField(parent, k, v) {{
    if (!v || v === "-") return;
    const wrap = document.createElement("div"); wrap.className = "kv";
    const kl = document.createElement("div"); kl.className = "k"; kl.textContent = k;
    const vl = document.createElement("div"); vl.className = "v"; vl.textContent = v;
    wrap.appendChild(kl); wrap.appendChild(vl); parent.appendChild(wrap);
  }}

  function addNeighborButtons(parent, title, nodeList) {{
    if (!nodeList.length) return;
    const wrap = document.createElement("div"); wrap.className = "kv";
    const kl = document.createElement("div"); kl.className = "k"; kl.textContent = title + " (" + nodeList.length + ")";
    wrap.appendChild(kl);
    nodeList.forEach(target => {{
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "nbr-btn";
      btn.textContent = target.key;
      btn.addEventListener("click", () => {{
        selectedId = target.id;
        showDetails(target);
        render();
      }});
      wrap.appendChild(btn);
    }});
    parent.appendChild(wrap);
  }}

  function showDetails(n) {{
    drawer.textContent = "";
    const h = document.createElement("h3"); h.style.margin = "0 0 8px"; h.style.fontSize = "14px";
    h.textContent = n.key; drawer.appendChild(h);
    const b1 = document.createElement("span"); b1.className = "badge";
    b1.textContent = n.layer ? (n.layer + n.domain + n.imp + " " + n.size) : n.groupLabel;
    drawer.appendChild(b1);
    if (n.cycle) {{
      const bc = document.createElement("span"); bc.className = "badge cycle";
      bc.textContent = "SCC Cycle #" + n.cycle; drawer.appendChild(bc);
    }}
    if (n.s && n.s !== "-") {{
      const br = document.createElement("span"); br.className = "badge risk";
      br.textContent = "Has S: risk/invariant"; drawer.appendChild(br);
    }}
    addField(drawer, "Group", n.groupLabel);
    addField(drawer, "Function (F:)", n.f);
    addField(drawer, "Requires (R:)", n.r);
    addField(drawer, "API / Exports (A:)", n.a);
    addField(drawer, "Risk / Side-effects (S:)", n.s);
    const outNodes = data.edges.filter(e => e.src === n.id).map(e => nodeById.get(e.dst)).filter(Boolean);
    const inNodes = data.edges.filter(e => e.dst === n.id).map(e => nodeById.get(e.src)).filter(Boolean);
    addNeighborButtons(drawer, "Dependencies (outgoing)", outNodes);
    addNeighborButtons(drawer, "Callers (incoming)", inNodes);
  }}

  function render() {{
    const q = qEl.value.trim().toLowerCase();
    const lf = layerEl.value;
    const df = domainEl.value;
    const mi = parseInt(minImpEl.value || "0", 10);
    const cyc = cyclesEl.checked;
    const layoutMode = layoutEl.value;
    const visible = data.nodes.filter(n => {{
      if (lf && n.layer !== lf) return false;
      if (df && n.domain !== df) return false;
      if (n.imp < mi) return false;
      if (cyc && !n.cycle) return false;
      if (q && !(n.key + " " + n.f + " " + n.s + " " + n.a).toLowerCase().includes(q)) return false;
      return true;
    }});
    const visSet = new Set(visible.map(n => n.id));
    if (selectedId && !visSet.has(selectedId)) {{
      selectedId = null;
    }}
    const visEdges = data.edges.filter(e => visSet.has(e.src) && visSet.has(e.dst));
    statsEl.textContent = visible.length + " nodes | " + visEdges.length + " edges | " + data.cycles.length + " cycle(s)";

    const focusSet = new Set();
    if (selectedId) {{
      focusSet.add(selectedId);
      visEdges.forEach(e => {{
        if (e.src === selectedId) focusSet.add(e.dst);
        if (e.dst === selectedId) focusSet.add(e.src);
      }});
    }}

    const groups = new Map();
    const groupTitle = new Map();
    visible.forEach(n => {{
      const g = layoutMode === "layered" ? (n.layer || "?") : (n.group || "ungrouped");
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(n);
      if (!groupTitle.has(g)) {{
        groupTitle.set(g, layoutMode === "layered" ? (g === "?" ? "Ungrouped" : "Layer " + g) : (n.groupLabel || g));
      }}
    }});
    const sortedGroups = Array.from(groups.keys()).sort((a, b) => {{
      if (layoutMode === "layered") {{
        const ia = LAYER_ORDER.indexOf(a);
        const ib = LAYER_ORDER.indexOf(b);
        const ra = ia === -1 ? 99 : ia;
        const rb = ib === -1 ? 99 : ib;
        if (ra !== rb) return ra - rb;
      }}
      return a.localeCompare(b);
    }});

    const pos = {{}};
    const nodeW = 236, nodeH = 36, pad = 28;
    const colW = 280, rowH = 52, colCardW = 256, rowGroupH = 96;
    const hdrCoords = [];
    sortedGroups.forEach((g, gi) => {{
      const items = groups.get(g);
      if (layoutMode === "layered") {{
        items.sort((a, b) => (b.imp - a.imp) || a.key.localeCompare(b.key));
      }}
      if (isTD) {{
        hdrCoords.push({{ x: pad, y: pad + gi * rowGroupH, label: groupTitle.get(g) || g }});
        items.forEach((n, idx) => {{
          const x = pad + idx * colCardW;
          const y = pad + 18 + gi * rowGroupH;
          pos[n.id] = {{ x, y, w: nodeW, h: nodeH }};
        }});
      }} else {{
        hdrCoords.push({{ x: pad + gi * colW, y: 18, label: groupTitle.get(g) || g }});
        items.forEach((n, idx) => {{
          const x = pad + gi * colW;
          const y = pad + 26 + idx * rowH;
          pos[n.id] = {{ x, y, w: nodeW, h: nodeH }};
        }});
      }}
    }});

    svg.textContent = "";
    const defs = document.createElementNS(ns, "defs");
    const marker = document.createElementNS(ns, "marker");
    marker.setAttribute("id", "arrow"); marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "9"); marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "6"); marker.setAttribute("markerHeight", "6");
    marker.setAttribute("orient", "auto-start-reverse");
    const mp = document.createElementNS(ns, "path");
    mp.setAttribute("d", "M 0 1 L 10 5 L 0 9 z"); mp.setAttribute("fill", "#64748b");
    marker.appendChild(mp); defs.appendChild(marker); svg.appendChild(defs);

    viewportG = document.createElementNS(ns, "g");
    viewportG.setAttribute("id", "viewport");
    svg.appendChild(viewportG);
    applyTransform();

    visEdges.forEach(e => {{
      const a = pos[e.src], b = pos[e.dst];
      if (!a || !b) return;
      let d = "";
      if (isTD) {{
        if (Math.abs(a.y - b.y) < 1) {{
          const x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x + b.w / 2, y2 = b.y + b.h;
          const bend = 40 + Math.min(80, Math.abs(x2 - x1) * 0.15);
          d = `M ${{x1}} ${{y1}} C ${{x1}} ${{y1 + bend}}, ${{x2}} ${{y2 + bend}}, ${{x2}} ${{y2}}`;
        }} else if (b.y > a.y) {{
          const x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x + b.w / 2, y2 = b.y, my = (y1 + y2) / 2;
          d = `M ${{x1}} ${{y1}} C ${{x1}} ${{my}}, ${{x2}} ${{my}}, ${{x2}} ${{y2}}`;
        }} else {{
          const x1 = a.x + a.w / 2, y1 = a.y, x2 = b.x + b.w / 2, y2 = b.y + b.h, my = (y1 + y2) / 2;
          d = `M ${{x1}} ${{y1}} C ${{x1}} ${{my}}, ${{x2}} ${{my}}, ${{x2}} ${{y2}}`;
        }}
      }} else {{
        if (Math.abs(a.x - b.x) < 1) {{
          const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x + b.w, y2 = b.y + b.h / 2;
          const bend = 45 + Math.min(90, Math.abs(y2 - y1) * 0.2);
          d = `M ${{x1}} ${{y1}} C ${{x1 + bend}} ${{y1}}, ${{x2 + bend}} ${{y2}}, ${{x2}} ${{y2}}`;
        }} else if (b.x > a.x) {{
          const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2, mx = (x1 + x2) / 2;
          d = `M ${{x1}} ${{y1}} C ${{mx}} ${{y1}}, ${{mx}} ${{y2}}, ${{x2}} ${{y2}}`;
        }} else {{
          const x1 = a.x, y1 = a.y + a.h / 2, x2 = b.x + b.w, y2 = b.y + b.h / 2, mx = (x1 + x2) / 2;
          d = `M ${{x1}} ${{y1}} C ${{mx}} ${{y1}}, ${{mx}} ${{y2}}, ${{x2}} ${{y2}}`;
        }}
      }}
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      p.setAttribute("marker-end", "url(#arrow)");
      const hi = selectedId && (e.src === selectedId || e.dst === selectedId);
      const dim = selectedId && !hi;
      p.setAttribute("class", "edge" + (e.inCycle ? " cycle" : "") + (hi ? " hi" : "") + (dim ? " dim" : ""));
      viewportG.appendChild(p);
    }});

    hdrCoords.forEach(h => {{
      const lbl = document.createElementNS(ns, "text");
      lbl.setAttribute("x", String(h.x)); lbl.setAttribute("y", String(h.y));
      lbl.setAttribute("fill", "#94a3b8"); lbl.setAttribute("font-size", "12");
      lbl.textContent = h.label;
      viewportG.appendChild(lbl);
    }});

    visible.forEach(n => {{
      const p = pos[n.id];
      const g = document.createElementNS(ns, "g");
      const dim = selectedId && !focusSet.has(n.id);
      g.setAttribute("class", "node-box" + (n.cycle ? " cycle" : "") + (n.id === selectedId ? " selected" : "") + (dim ? " dim" : ""));
      const rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", String(p.x)); rect.setAttribute("y", String(p.y));
      rect.setAttribute("width", String(p.w)); rect.setAttribute("height", String(p.h));
      const txt = document.createElementNS(ns, "text");
      txt.setAttribute("x", String(p.x + 8)); txt.setAttribute("y", String(p.y + 22));
      const shortLbl = n.label.length > 34 ? ("..." + n.label.slice(-31)) : n.label;
      txt.textContent = shortLbl;
      const title = document.createElementNS(ns, "title");
      title.textContent = n.label;
      g.appendChild(rect); g.appendChild(txt); g.appendChild(title);
      g.addEventListener("click", ev => {{ ev.stopPropagation(); selectedId = n.id; showDetails(n); render(); }});
      viewportG.appendChild(g);
    }});
  }}

  svg.addEventListener("mousedown", ev => {{
    if (ev.button !== 0 || ev.target.closest(".node-box")) return;
    dragging = true; didDrag = false;
    dragSX = ev.clientX; dragSY = ev.clientY;
    basePX = panX; basePY = panY;
    svg.classList.add("grabbing");
  }});
  window.addEventListener("mousemove", ev => {{
    if (!dragging) return;
    const dx = ev.clientX - dragSX, dy = ev.clientY - dragSY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) didDrag = true;
    panX = basePX + dx; panY = basePY + dy;
    applyTransform();
  }});
  window.addEventListener("mouseup", () => {{
    if (dragging) {{ dragging = false; svg.classList.remove("grabbing"); }}
  }});
  svg.addEventListener("click", ev => {{
    if (!didDrag && !ev.target.closest(".node-box") && selectedId !== null) {{
      selectedId = null; render();
    }}
  }});
  svg.addEventListener("wheel", ev => {{
    ev.preventDefault();
    zoomAt(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX, ev.clientY);
  }}, {{ passive: false }});

  zoomInBtn.addEventListener("click", () => zoomAt(1.2));
  zoomOutBtn.addEventListener("click", () => zoomAt(1 / 1.2));
  zoomResetBtn.addEventListener("click", () => {{ panX = 0; panY = 0; scale = 1.0; applyTransform(); }});
  let qTimer = null;
  qEl.addEventListener("input", () => {{
    if (qTimer !== null) clearTimeout(qTimer);
    qTimer = setTimeout(() => {{ qTimer = null; render(); }}, 120);
  }});
  [layerEl, domainEl, minImpEl, layoutEl, cyclesEl].forEach(el => el.addEventListener("input", render));
  document.getElementById("resetBtn").addEventListener("click", () => {{
    if (qTimer !== null) {{ clearTimeout(qTimer); qTimer = null; }}
    qEl.value = ""; layerEl.value = ""; domainEl.value = ""; minImpEl.value = "0"; layoutEl.value = "grouped";
    cyclesEl.checked = false; selectedId = null; panX = 0; panY = 0; scale = 1.0; render();
  }});
  render();
}})();
</script>
</body>
</html>
"""


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
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
) -> Dict[str, Any]:
    """Export filtered or aggregated FRAS dependency graph in Mermaid, Graphviz DOT, or standalone HTML."""
    fmt_l = fmt.lower().strip()
    if fmt_l not in ("mermaid", "dot", "html"):
        raise ValueError(f"Unsupported export format `{fmt}` (expected `mermaid`, `dot`, or `html`).")
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

    parsed_sel: Dict[str, ParsedEntry] = {}
    for rel in sorted(entries.keys()):
        if focus_set is not None and rel not in focus_set:
            continue
        pe = parsed_entries.get(rel) if parsed_entries is not None else parse_entry(rel, entries[rel])
        if pe is None:
            continue
        if layer_set is not None and pe.layer not in layer_set:
            continue
        if domain_set is not None and pe.domain not in domain_set:
            continue
        if min_importance and pe.imp < min_importance:
            continue
        if path_glob and not fnmatch.fnmatchcase(rel, path_glob):
            continue
        parsed_sel[rel] = pe

    node_labels: Dict[str, str] = {}
    node_groups: Dict[str, Optional[str]] = {}
    group_labels: Dict[str, str] = {}
    edge_counts: Dict[Tuple[str, str], int] = {}

    actual_group_by = group_l if level_l == "file" else "none"

    if level_l == "file":
        for rel, pe in parsed_sel.items():
            node_labels[rel] = f"{rel} [{pe.layer}{pe.domain}{pe.imp}]"
            if group_l == "none":
                node_groups[rel] = None
            elif group_l == "domain":
                gk = pe.domain
                node_groups[rel] = gk
                dom_name = declared_domains.get(gk)
                group_labels[gk] = f"{gk}: {dom_name}" if dom_name else f"Domain {gk}"
            elif group_l == "layer":
                gk = pe.layer
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
        for rel, pe in parsed_sel.items():
            dk = pe.domain
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

    rendered: str
    if fmt_l == "html":
        out_dir = "TD" if dir_u in ("TD", "TB") else "LR"
        rendered = _render_html_dependency_graph(
            level=level_l,
            group_by=actual_group_by,
            direction=out_dir,
            sorted_nodes=sorted_nodes,
            node_ids=node_ids,
            node_labels=node_labels,
            node_groups=node_groups,
            group_labels=group_labels,
            sorted_edges=sorted_edges,
            parsed_sel=parsed_sel,
            declared_domains=declared_domains,
        )
    elif fmt_l == "mermaid":
        m_dir = "TD" if dir_u in ("TD", "TB") else "LR"
        lines_out: List[str] = [f"flowchart {m_dir}"]
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
        rendered = "\n".join(lines_out) + "\n"
    else:
        d_dir = "TB" if dir_u in ("TD", "TB") else "LR"
        lines_out = ["digraph aoci {", f"  rankdir={d_dir};", "  node [shape=box];"]
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
            for raw in meta_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
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
            for raw in glossary_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
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


_ALIAS_SPLIT_RE = re.compile(r"[\s_-]+")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_ALIAS_REGEX_CACHE: Dict[str, Optional[re.Pattern[str]]] = {}
_ALIAS_REGEX_CACHE_MAX = 4096


def _query_contains_alias(query_text: str, camel_norm_query: str, alias: str) -> bool:
    if not alias:
        return False
    if CJK_CHAR_RE.search(alias):
        return alias.lower() in query_text.lower()
    if alias in _ALIAS_REGEX_CACHE:
        pat = _ALIAS_REGEX_CACHE[alias]
    else:
        parts = [re.escape(p) for p in _ALIAS_SPLIT_RE.split(alias.strip()) if p]
        if not parts:
            pat = None
        else:
            sep = r"[\s_-]+"
            pat = re.compile(rf"(?<![A-Za-z0-9]){sep.join(parts)}(?![A-Za-z0-9])", re.IGNORECASE)
        if len(_ALIAS_REGEX_CACHE) >= _ALIAS_REGEX_CACHE_MAX:
            _ALIAS_REGEX_CACHE.clear()
        _ALIAS_REGEX_CACHE[alias] = pat
    if pat is None:
        return False
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

    camel_norm_query = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", query_text)
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

    def __init__(
        self,
        entries: Dict[str, str],
        parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
    ) -> None:
        self.parsed: Dict[str, ParsedEntry] = {}
        self.postings: Dict[str, List[Tuple[int, float]]] = {}
        self.df: Dict[str, int] = {}
        self.doc_meta: List[Tuple[str, str, int, float]] = []

        for rel, line in sorted(entries.items()):
            pe = parsed_entries.get(rel) if parsed_entries is not None else None
            if pe is None:
                pe = parse_entry(rel, line)
            if pe is None:
                continue
            imp = pe.imp
            self.parsed[rel] = pe
            a_chunks = re.split(r"[,;]", pe.a)
            a_lhs_parts = [chunk.split("<-", 1)[0] for chunk in a_chunks]
            a_rhs_parts = [chunk.split("<-", 1)[1] for chunk in a_chunks if "<-" in chunk]

            tf: Dict[str, float] = {}
            doc_len = 0.0
            zones = (
                (rel, 2.5),
                (" ".join(a_lhs_parts), 2.2),
                (pe.f, 2.0),
                (pe.s, 1.5),
                (pe.r + " " + " ".join(a_rhs_parts), 1.0),
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
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
) -> List[Tuple[float, str, str]]:
    """Rank FRAS entries with field-weighted Okapi BM25 + Importance centrality prior."""
    if not entries or (candidate_paths is not None and not candidate_paths):
        return []
    if candidate_paths is None or (len(candidate_paths) >= len(entries) and entries.keys() <= candidate_paths):
        sub = entries
    else:
        sub = {p: entries[p] for p in candidate_paths if p in entries}
    if not sub:
        return []
    cls = index_cls or BM25Index
    idx = cls(sub, parsed_entries=parsed_entries) if cls is BM25Index else cls(sub)
    return idx.search(query_text, top_k=top_k, glossary=glossary)


def search_entries_bm25_detailed(
    entries: Dict[str, str],
    query_text: str,
    candidate_paths: Optional[Set[str]] = None,
    top_k: int = 20,
    index_cls: Any = None,
    glossary: Optional[Sequence[Sequence[str]]] = None,
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Rank FRAS entries with BM25 + glossary expansion and return per-hit term attribution."""
    if not entries or (candidate_paths is not None and not candidate_paths):
        return [], []
    if candidate_paths is None or (len(candidate_paths) >= len(entries) and entries.keys() <= candidate_paths):
        sub = entries
    else:
        sub = {p: entries[p] for p in candidate_paths if p in entries}
    if not sub:
        return [], []
    cls = index_cls or BM25Index
    idx = cls(sub, parsed_entries=parsed_entries) if cls is BM25Index else cls(sub)
    if hasattr(idx, "search_detailed"):
        return idx.search_detailed(query_text, top_k=top_k, glossary=glossary)
    ranked = idx.search(query_text, top_k=top_k)
    return [{"path": r, "score": round(sc, 3), "matched_terms": [], "entry": ln} for sc, r, ln in ranked], []
