"""Cross-file symbol lookup fusing `aoci.hubs.txt`, outline cache (or live fallback), and FRAS `A:`."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .constants import AOCI_DIR_NAME, DEFAULT_MAX_BYTES, HUBS_FILE, _is_within_root, normalize_relpath
from .graph import ParsedEntry, bfs_neighbors, parse_entries_map
from .hubs import _OUTLINE_LINE_RE, _clean_outline_symbol, parse_hubs_volume
from .parsers import extract_file_outline
from .test_impact import _load_outline_cache, is_outline_record_fresh

_HTTP_VERB_PREFIX_RE = re.compile(r"^(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+", re.IGNORECASE)
_FRAS_SYM_TOKEN_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.:#-]*$")
_PY_METHODS_RE = re.compile(r"\bmethods=\[([^\]]+)\]")
_SOURCE_ORDER: Tuple[str, ...] = ("hub", "outline", "fras")


def _parse_fras_api_symbols(a_field: str) -> List[str]:
    """Extract clean exported symbol names from the LHS of FRAS `A:` entries."""
    raw = (a_field or "").strip()
    if not raw or raw == "-":
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for chunk in re.split(r"[,;]", raw):
        lhs = chunk.split("<-", 1)[0].strip()
        if not lhs or lhs == "-":
            continue
        lhs = _HTTP_VERB_PREFIX_RE.sub("", lhs).strip()
        if not lhs or lhs.startswith("/"):
            continue
        lhs = re.sub(r"\([^)]*\)", "", lhs).strip()
        if not lhs:
            continue
        parts = [lhs] if "/" not in lhs else [p.strip() for p in lhs.split("/") if p.strip()]
        for tok in parts:
            if tok.startswith("/") or len(tok) < 2:
                continue
            if _FRAS_SYM_TOKEN_RE.match(tok) and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _extract_live_outline_symbols(root: Path, rel: str) -> List[str]:
    full = root / rel
    if not _is_within_root(root, full):
        return []
    try:
        st = os.lstat(full)
    except OSError:
        return []
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > DEFAULT_MAX_BYTES:
        return []
    try:
        outline = extract_file_outline(full, rel, max_items=100)
    except OSError:
        return []
    raw_syms = outline.get("symbols") if isinstance(outline, dict) else None
    if not isinstance(raw_syms, list):
        return []
    return [str(s).strip() for s in raw_syms if isinstance(s, str) and str(s).strip()]


def _match_symbol_tier(sym: str, query: str, q_low: str, exact: bool) -> Optional[int]:
    if sym == query:
        return 0
    s_low = sym.lower()
    if s_low == q_low:
        return 1
    if s_low.endswith("." + q_low) or s_low.endswith("::" + q_low):
        return 2
    if exact:
        return None
    tail = s_low.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
    if s_low.startswith(q_low) or tail.startswith(q_low):
        return 3
    if q_low in s_low:
        return 4
    return None


def lookup_symbol(
    root: Path,
    entries: Mapping[str, str],
    deps_adj: Mapping[str, Set[str]],
    dependents_adj: Mapping[str, Set[str]],
    name: str,
    *,
    exact: bool = False,
    depth: int = 1,
    limit: int = 20,
    live: bool = False,
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
    preloaded_outline_files: Optional[Dict[str, Any]] = None,
    hub_records: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Search symbols across `.aoci/aoci.hubs.txt`, outline cache (plus optional read-only `live`
    source fallback), and FRAS `A:` LHS exports, returning ranked matches with FRAS graph context.
    """
    query = (name or "").strip()
    if not query:
        return {
            "ok": False,
            "root": str(root),
            "query": "",
            "exact": exact,
            "depth": depth,
            "total_matches": 0,
            "matches": [],
            "error": "Symbol `name` must not be empty.",
        }

    parsed_map = parsed_entries if parsed_entries is not None else parse_entries_map(entries)
    records_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _slot(path: str, sym: str) -> Dict[str, Any]:
        rec = records_by_key.get((path, sym))
        if rec is None:
            rec = {
                "path": path,
                "symbol": sym,
                "sources": set(),
                "line_span": None,
                "signature": None,
                "hub_f": None,
                "hub_s": None,
            }
            records_by_key[(path, sym)] = rec
        return rec

    # 1) Hub sections
    if hub_records is not None:
        hubs_list = list(hub_records)
    else:
        hubs_path = root / AOCI_DIR_NAME / HUBS_FILE
        hubs_list, _ = parse_hubs_volume(hubs_path) if hubs_path.is_file() else ([], [])
    for hr in hubs_list:
        rel = normalize_relpath(str(hr.get("relpath", "")))
        sym = str(hr.get("symbol", "")).strip()
        if not rel or not sym:
            continue
        if entries and rel not in entries:
            continue
        rec = _slot(rel, sym)
        rec["sources"].add("hub")
        if "start" in hr and "end" in hr:
            rec["line_span"] = [int(hr["start"]), int(hr["end"])]
        f_txt = str(hr.get("f", "")).strip()
        s_txt = str(hr.get("s", "")).strip()
        if f_txt and f_txt != "-":
            rec["hub_f"] = f_txt
        if s_txt and s_txt != "-":
            rec["hub_s"] = s_txt

    # 2) Warm outline cache + optional read-only live fallback
    outline_files = preloaded_outline_files if preloaded_outline_files is not None else _load_outline_cache(root)
    target_paths = sorted(entries.keys()) if entries else sorted(outline_files.keys())

    def _ingest_outline_list(rel: str, sym_items: Sequence[Any]) -> None:
        ext = Path(rel).suffix.lower()
        for raw_item in sym_items:
            m_ln = _OUTLINE_LINE_RE.match(str(raw_item).strip())
            if not m_ln:
                continue
            body = m_ln.group("body").strip()
            lineno = int(m_ln.group("line"))
            sym, _ = _clean_outline_symbol(body, ext)
            if not sym:
                continue
            rec = _slot(rel, sym)
            rec["sources"].add("outline")
            if rec["signature"] is None and body:
                rec["signature"] = body
            if rec["line_span"] is None:
                rec["line_span"] = [lineno, lineno]
            m_meth = _PY_METHODS_RE.search(body)
            if m_meth and "." not in sym and "::" not in sym:
                for raw_m in m_meth.group(1).split(","):
                    m_name = raw_m.strip()
                    if not m_name or not _FRAS_SYM_TOKEN_RE.match(m_name):
                        continue
                    m_rec = _slot(rel, f"{sym}.{m_name}")
                    m_rec["sources"].add("outline")
                    if m_rec["signature"] is None:
                        m_rec["signature"] = f"method {sym}.{m_name}"
                    if m_rec["line_span"] is None:
                        m_rec["line_span"] = [lineno, lineno]

    for rel in target_paths:
        raw_rec = outline_files.get(rel) if outline_files else None
        if isinstance(raw_rec, dict) and is_outline_record_fresh(root, rel, raw_rec):
            out_obj = raw_rec.get("outline") if isinstance(raw_rec.get("outline"), dict) else raw_rec
            syms_list = out_obj.get("symbols") if isinstance(out_obj, dict) else None
            if isinstance(syms_list, list):
                _ingest_outline_list(rel, syms_list)
        elif live and rel in entries:
            live_syms = _extract_live_outline_symbols(root, rel)
            if live_syms:
                _ingest_outline_list(rel, live_syms)

    # 3) FRAS `A:` LHS exports
    for rel, pe in parsed_map.items():
        for sym in _parse_fras_api_symbols(pe.a):
            rec = _slot(rel, sym)
            rec["sources"].add("fras")

    q_low = query.lower()
    ranked: List[Tuple[int, int, int, str, str, Dict[str, Any]]] = []
    for (rel, sym), rec in records_by_key.items():
        tier = _match_symbol_tier(sym, query, q_low, exact)
        if tier is None:
            continue
        pe = parsed_map.get(rel)
        imp = pe.imp if pe is not None else 0
        has_hub = 0 if "hub" in rec["sources"] else 1
        ranked.append((tier, has_hub, -imp, rel, sym, rec))

    ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))
    total_matches = len(ranked)
    lim = int(limit)
    sliced = ranked if lim <= 0 else ranked[:lim]

    hops = max(0, int(depth))
    dep_cache: Dict[str, List[str]] = {}
    caller_cache: Dict[str, List[str]] = {}

    matches_out: List[Dict[str, Any]] = []
    for tier, _, _, rel, sym, rec in sliced:
        pe = parsed_map.get(rel)
        imp = pe.imp if pe is not None else 0
        if hops > 0:
            if rel not in dep_cache:
                dep_cache[rel] = sorted(nb for nb in bfs_neighbors(deps_adj, [rel], hops) if nb != rel)
            if rel not in caller_cache:
                caller_cache[rel] = sorted(nb for nb in bfs_neighbors(dependents_adj, [rel], hops) if nb != rel)
            deps_list = dep_cache[rel]
            callers_list = caller_cache[rel]
        else:
            deps_list = []
            callers_list = []

        src_list = [s for s in _SOURCE_ORDER if s in rec["sources"]]
        hit: Dict[str, Any] = {
            "symbol": sym,
            "path": rel,
            "match_tier": tier,
            "sources": src_list,
            "line_span": rec["line_span"],
            "signature": rec["signature"],
            "importance": imp,
            "file_entry": entries.get(rel),
            "file_s": pe.s if (pe is not None and pe.s not in ("-", "")) else None,
            "dependencies": deps_list,
            "callers": callers_list,
        }
        if rec["hub_f"] is not None:
            hit["hub_f"] = rec["hub_f"]
        if rec["hub_s"] is not None:
            hit["hub_s"] = rec["hub_s"]
        matches_out.append(hit)

    return {
        "ok": True,
        "root": str(root),
        "query": query,
        "exact": exact,
        "depth": hops,
        "total_matches": total_matches,
        "matches": matches_out,
    }
