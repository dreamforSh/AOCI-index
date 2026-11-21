"""Graph-pruned cross-file symbol reference finder (`aoci refs` / MCP `aoci_refs`)."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .constants import AOCI_DIR_NAME, DEFAULT_MAX_BYTES, HUBS_FILE, _is_within_root, normalize_relpath, strip_utf8_bom_bytes
from .graph import ParsedEntry, bfs_neighbors_with_distance, parse_entries_map
from .hubs import parse_hubs_volume
from .ignore import is_probably_binary_bytes
from .parsers import (
    _SFC_SCRIPT_RE,
    _blank_block_comment,
    _strip_c_comments,
    _strip_hash_comments,
    _strip_js_comments,
    _strip_sql_comments,
)
from .symbols import lookup_symbol
from .test_impact import _load_outline_cache, is_outline_record_fresh

_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?(?:-->|$)")
_IMPORT_LINE_RE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\b|import\b|export\s+(?:type\s+)?(?:\*|\{[^}]*\})\s+from\b|"
    r"(?:pub(?:\([^)]*\))?\s+)?use\b|#\s*include\b|using\b)"
)
_CALL_REQUIRE_RE = re.compile(r"\brequire\s*\(")
_VALID_IDENT_RE = re.compile(r"^[A-Za-z_$~][A-Za-z0-9_$]*$")


def _strip_comments_preserving_lines(text: str, ext: str) -> str:
    ext_l = ext.lower()
    if ext_l in {".py", ".pyi", ".graphql", ".gql", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".rb"}:
        return _strip_hash_comments(text)
    if ext_l in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        return _strip_js_comments(text)
    if ext_l in {".vue", ".svelte"}:
        no_html = _HTML_COMMENT_RE.sub(lambda m: _blank_block_comment(m.group(0)), text)
        chars = list(no_html)
        for m_sfc in _SFC_SCRIPT_RE.finditer(no_html):
            s_i, e_i = m_sfc.span(1)
            chars[s_i:e_i] = list(_strip_js_comments(no_html[s_i:e_i]))
        return "".join(chars)
    if ext_l == ".go":
        return _strip_c_comments(text, allow_backtick=True)
    if ext_l == ".rs":
        return _strip_c_comments(text, single_quote_char_only=True)
    if ext_l in {".java", ".kt", ".kts", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".proto", ".prisma"}:
        return _strip_c_comments(text)
    if ext_l == ".sql":
        return _strip_sql_comments(text)
    return text


def _build_decl_line_re(leaf_sym: str) -> "re.Pattern[str]":
    esc = re.escape(leaf_sym)
    return re.compile(
        rf"^\s*(?:export\s+(?:default\s+)?)?"
        rf"(?:(?:public|private|protected|internal|static|abstract|final|sealed|data|open|override|"
        rf"suspend|inline|async|unsafe|const|declare|pub(?:\([^)]*\))?)\s+)*"
        rf"(?:def|class|function|interface|type|enum(?:\s+class)?|struct|trait|fn|fun|record|object|"
        rf"func(?:\s*\([^)]*\))?)\s+{esc}\b"
    )


def _split_symbol_query(raw_symbol: str, explicit_path: Optional[str]) -> Tuple[Optional[str], str, str, Optional[str]]:
    """Return `(def_path, lookup_query, leaf_ident, owner_hint)`."""
    q = (raw_symbol or "").strip()
    def_p = normalize_relpath(explicit_path) if (explicit_path and str(explicit_path).strip()) else None
    if "#" in q and def_p is None:
        lhs, rhs = q.split("#", 1)
        if lhs.strip() and rhs.strip():
            def_p = normalize_relpath(lhs.strip())
            q = rhs.strip()
    owner_hint: Optional[str] = None
    leaf = q
    if "::" in q:
        owner_hint, leaf = q.rsplit("::", 1)
    elif "." in q:
        owner_hint, leaf = q.rsplit(".", 1)
    return def_p, q, leaf.strip(), (owner_hint.strip() if owner_hint else None)


def _outline_mentions_target(
    outline_obj: Mapping[str, Any],
    def_files: Set[str],
    def_stems: Set[str],
    leaf_sym: str,
) -> bool:
    for imp in outline_obj.get("imports") or ():
        imp_s = str(imp)
        norm_imp = imp_s.replace("\\", "/").strip("./")
        if norm_imp in def_files:
            return True
        tail = norm_imp.rsplit("/", 1)[-1].rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        if tail in def_stems or tail == leaf_sym:
            return True
    for mem in outline_obj.get("py_from_members") or ():
        mem_s = str(mem)
        if mem_s.endswith("." + leaf_sym) or any(st in mem_s for st in def_stems):
            return True
    return False


def find_symbol_references(
    root: Path,
    entries: Mapping[str, str],
    deps_adj: Mapping[str, Set[str]],
    dependents_adj: Mapping[str, Set[str]],
    symbol: str,
    *,
    def_path: Optional[str] = None,
    depth: int = 2,
    all_files: bool = False,
    include_defs: bool = False,
    limit: int = 50,
    max_bytes: int = DEFAULT_MAX_BYTES,
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
    preloaded_outline_files: Optional[Dict[str, Any]] = None,
    hub_records: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Locate exact line-level references and call sites for `symbol` by pruning the search space via
    the FRAS dependency graph (`dependents_adj` + `deps_adj`), cached static imports, and T-layer test
    stems before running comment-stripped identifier boundary matching.
    """
    target_def_path, lookup_q, leaf_sym, owner_hint = _split_symbol_query(symbol, def_path)
    if not leaf_sym or not _VALID_IDENT_RE.match(leaf_sym):
        return {
            "ok": False,
            "root": str(root),
            "query": (symbol or "").strip(),
            "error": f"Invalid or empty symbol identifier `{symbol}`.",
        }

    parsed_map = parsed_entries if parsed_entries is not None else parse_entries_map(dict(entries))
    outline_files = preloaded_outline_files if preloaded_outline_files is not None else _load_outline_cache(root)
    if hub_records is None:
        hubs_file = root / AOCI_DIR_NAME / HUBS_FILE
        loaded_hubs, _ = parse_hubs_volume(hubs_file) if hubs_file.is_file() else ([], [])
    else:
        loaded_hubs = list(hub_records)

    sym_res = lookup_symbol(
        root,
        entries,
        deps_adj,
        dependents_adj,
        lookup_q,
        exact=True,
        depth=1,
        limit=50,
        live=True,
        parsed_entries=parsed_map,
        preloaded_outline_files=outline_files,
        hub_records=loaded_hubs,
    )
    raw_defs = sym_res.get("matches") or []
    if target_def_path is not None:
        raw_defs = [m for m in raw_defs if normalize_relpath(str(m.get("path", ""))) == target_def_path]

    definitions: List[Dict[str, Any]] = []
    def_files: Set[str] = set()
    def_line_pairs: Set[Tuple[str, int]] = set()
    for d in raw_defs:
        dp = normalize_relpath(str(d.get("path", "")))
        if not dp:
            continue
        def_files.add(dp)
        span = d.get("line_span")
        if isinstance(span, list) and span:
            def_line_pairs.add((dp, int(span[0])))
        definitions.append(
            {
                "path": dp,
                "symbol": d.get("symbol"),
                "line_span": span,
                "signature": d.get("signature"),
            }
        )
    if target_def_path is not None and target_def_path in entries and target_def_path not in def_files:
        def_files.add(target_def_path)

    hops = max(1, int(depth))
    candidate_priority: Dict[str, int] = {}

    def _mark_cand(rel: str, prio: int) -> None:
        if not rel:
            return
        prev = candidate_priority.get(rel)
        if prev is None or prio < prev:
            candidate_priority[rel] = prio

    if not all_files and def_files and entries:
        mode = "fras-graph"
        def_list = sorted(def_files)
        for df in def_list:
            _mark_cand(df, 1)
        for nb, dist in bfs_neighbors_with_distance(dict(dependents_adj), def_list, hops).items():
            _mark_cand(nb, 0 if dist == 1 else 1)
        for dep_nb in bfs_neighbors_with_distance(dict(deps_adj), def_list, 1):
            _mark_cand(dep_nb, 2)

        def_stems = {Path(df).stem for df in def_files if Path(df).stem not in ("__init__", "index", "mod")}
        def_stems_low = {s.lower() for s in def_stems}
        for rel in entries:
            if rel in candidate_priority:
                continue
            raw_rec = outline_files.get(rel) if outline_files else None
            if isinstance(raw_rec, Mapping) and is_outline_record_fresh(root, rel, raw_rec):
                out_obj = raw_rec.get("outline") if isinstance(raw_rec.get("outline"), Mapping) else raw_rec
                if isinstance(out_obj, Mapping) and _outline_mentions_target(out_obj, def_files, def_stems, leaf_sym):
                    _mark_cand(rel, 1)
                    continue
            pe = parsed_map.get(rel)
            if pe is not None and pe.layer == "T" and def_stems_low:
                stem_l = Path(rel).stem.lower()
                if any(ds in stem_l for ds in def_stems_low):
                    _mark_cand(rel, 2)
    else:
        mode = "all-indexed"
        for rel in sorted(entries.keys()):
            _mark_cand(rel, 0 if rel in def_files else 1)

    ident_re = re.compile(rf"(?<![\w$]){re.escape(leaf_sym)}(?![\w$])")
    call_re = re.compile(rf"(?<![\w$]){re.escape(leaf_sym)}\s*\(")
    decl_re = _build_decl_line_re(leaf_sym)
    owner_re = re.compile(rf"\b{re.escape(owner_hint)}\b") if owner_hint else None

    kind_order = {"call": 0, "reference": 1, "import": 2, "definition": 3}
    ranked_hits: List[Tuple[int, int, int, str, int, Dict[str, Any]]] = []

    for rel in sorted(candidate_priority.keys()):
        full = root / rel
        if not _is_within_root(root, full):
            continue
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            continue
        try:
            raw_bytes = full.read_bytes()
        except OSError:
            continue
        if is_probably_binary_bytes(raw_bytes[:8192]):
            continue
        norm_bytes = strip_utf8_bom_bytes(raw_bytes)
        if b"\r\n" in norm_bytes:
            norm_bytes = norm_bytes.replace(b"\r\n", b"\n")
        raw_text = norm_bytes.decode("utf-8", errors="replace")
        if leaf_sym not in raw_text:
            continue

        clean_text = _strip_comments_preserving_lines(raw_text, full.suffix)
        raw_lines = raw_text.splitlines()
        clean_lines = clean_text.splitlines()
        file_prio = candidate_priority[rel]

        for idx, clean_ln in enumerate(clean_lines):
            if leaf_sym not in clean_ln or not ident_re.search(clean_ln):
                continue
            lineno = idx + 1
            is_def = (rel, lineno) in def_line_pairs or (rel in def_files and bool(decl_re.match(clean_ln)))
            if is_def:
                if not include_defs:
                    continue
                kind = "definition"
            elif _IMPORT_LINE_RE.match(clean_ln) or _CALL_REQUIRE_RE.search(clean_ln):
                kind = "import"
            elif call_re.search(clean_ln):
                kind = "call"
            else:
                kind = "reference"

            orig_ln = raw_lines[idx].strip() if idx < len(raw_lines) else clean_ln.strip()
            snippet = orig_ln[:180] + ("..." if len(orig_ln) > 180 else "")
            owner_bonus = 0 if (owner_re is None or owner_re.search(clean_ln)) else 1
            hit_obj: Dict[str, Any] = {
                "path": rel,
                "line": lineno,
                "kind": kind,
                "snippet": snippet,
            }
            ranked_hits.append((file_prio, owner_bonus, kind_order.get(kind, 9), rel, lineno, hit_obj))

    ranked_hits.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))
    all_refs = [row[5] for row in ranked_hits]
    lim = int(limit)
    truncated = bool(lim > 0 and len(all_refs) > lim)
    sliced = all_refs[:lim] if lim > 0 else all_refs

    return {
        "ok": True,
        "root": str(root),
        "query": (symbol or "").strip(),
        "symbol": leaf_sym,
        "mode": mode,
        "definitions": definitions,
        "candidate_files_searched": len(candidate_priority),
        "total_indexed_files": len(entries),
        "total_references": len(all_refs),
        "truncated": truncated,
        "references": sliced,
    }
