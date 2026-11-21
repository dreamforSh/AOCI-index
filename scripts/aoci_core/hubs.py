"""H-tier symbol-level sub-volume (`aoci.hubs.txt`) extraction, validation, and span maintenance."""

from __future__ import annotations

import ast
from bisect import bisect_right
import contextlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .constants import (
    AOCI_DIR_NAME,
    HUBS_FILE,
    MANIFEST_FILE,
    _is_within_root,
    atomic_write_text,
    normalize_relpath,
    utc_now_iso,
)
from .parsers import (
    _CLEAN_SYM_RE,
    _JS_REGEX_PREV_CHARS,
    _JS_REGEX_PREV_KW,
    _find_matching_delim,
    _skip_js_regex,
    _skip_string_literal,
    _strip_c_comments,
    _strip_js_comments,
    extract_file_outline,
)

HUB_ENTRY_RE = re.compile(
    r"^(?P<path>[^\s#:][^#:\r\n]*?)#(?P<symbol>[^\[\r\n]+?)\[L(?P<start>\d+)-L(?P<end>\d+)\]:\s*"
    r"F:(?P<f>.*?)\s*\|\s*S:(?P<s>.*?)\s*$"
)
_OUTLINE_LINE_RE = re.compile(r"^(?P<body>.+?)\s*\[L(?P<line>\d+)\]\s*$")
_GO_RECV_SYM_RE = re.compile(r"^func\s*\((?:[^)]*?[\s*.])?([A-Za-z_]\w*)\)\s+([A-Za-z_]\w*)")

_OUTLINE_SPAN_EXTS: Set[str] = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".vue",
    ".svelte",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".cs",
    ".proto",
    ".graphql",
    ".gql",
    ".prisma",
    ".sql",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
}


_BRACE_SPAN_EXTS: Set[str] = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".cs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
    ".proto",
    ".graphql",
    ".gql",
    ".prisma",
}

_JS_SPAN_EXTS: Set[str] = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
}

_HASH_COMMENT_RX = re.compile(r'"[^"\\]*(?:\\[\s\S][^"\\]*)*(?:"|$)|#[^\n]*')


def format_hub_entry(relpath: str, symbol: str, start: int, end: int, f_val: str, s_val: str) -> str:
    """Format a canonical single-line `aoci.hubs.txt` entry."""
    return f"{relpath}#{symbol}[L{start}-L{end}]: F:{f_val} | S:{s_val}"


def _count_norm_lines(norm_bytes: bytes) -> int:
    if not norm_bytes:
        return 0
    return norm_bytes.count(b"\n") + (0 if norm_bytes.endswith(b"\n") else 1)


def _read_norm_bytes(full_path: Path) -> Optional[bytes]:
    try:
        raw = full_path.read_bytes()
    except OSError:
        return None
    return raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw


def validate_hub_relpath(raw_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate that `raw_path` is a plain repo-relative POSIX path outside `.aoci/` and `.git/`."""
    cleaned = str(raw_path).strip()
    if (
        not cleaned
        or cleaned.startswith(("/", "\\"))
        or Path(cleaned).is_absolute()
        or re.match(r"^[A-Za-z]:", cleaned)
    ):
        return None, f"Path must be a plain repo-relative POSIX path (no leading `/` or drive): `{raw_path}`"
    rel = normalize_relpath(cleaned)
    if not rel or any(seg in ("..", ".") for seg in rel.split("/")):
        return None, f"Path must be a plain repo-relative POSIX path (no `..` or `.`): `{raw_path}`"
    if rel == AOCI_DIR_NAME or rel.startswith(f"{AOCI_DIR_NAME}/") or rel == ".git" or rel.startswith(".git/"):
        return None, f"Refusing to reference reserved path `{rel}`."
    return rel, None


def _ast_node_span(node: ast.AST, max_lines: int) -> Tuple[int, int]:
    """Compute inclusive `[start, end]` line span with a Python 3.8 max-child-`lineno` fallback."""
    start = int(getattr(node, "lineno", 1) or 1)
    end_raw = getattr(node, "end_lineno", None)
    if isinstance(end_raw, int) and end_raw >= start:
        end = end_raw
    else:
        end = start
        for child in ast.walk(node):
            c_end = getattr(child, "end_lineno", None)
            if isinstance(c_end, int) and c_end > end:
                end = c_end
            c_ln = getattr(child, "lineno", None)
            if isinstance(c_ln, int) and c_ln > end:
                end = c_ln
    start = max(1, min(start, max_lines))
    end = max(start, min(end, max_lines))
    return start, end


def _extract_python_spans(norm_bytes: bytes, rel: str) -> Tuple[List[Dict[str, Any]], bool]:
    line_count = _count_norm_lines(norm_bytes)
    if line_count <= 0:
        return [], True
    text = norm_bytes.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], False

    spans: List[Dict[str, Any]] = []
    seen_syms: Set[str] = set()

    def _add(sym: str, kind: str, node: ast.AST) -> None:
        if not sym or sym in seen_syms:
            return
        seen_syms.add(sym)
        start, end = _ast_node_span(node, line_count)
        spans.append({"path": rel, "symbol": sym, "kind": kind, "start": start, "end": end})

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            _add(node.name, "function", node)
        elif isinstance(node, ast.AsyncFunctionDef):
            _add(node.name, "async_function", node)
        elif isinstance(node, ast.ClassDef):
            _add(node.name, "class", node)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _add(f"{node.name}.{sub.name}", "method", sub)

    return spans, True


def _clean_outline_symbol(core: str, ext: str) -> Tuple[str, str]:
    body = re.sub(r"\s*\[@[^\]]+\]\s*$", "", core).strip()
    if ext == ".go":
        m_recv = _GO_RECV_SYM_RE.match(body)
        if m_recv:
            return f"{m_recv.group(1)}.{m_recv.group(2)}", "method"
    m = _CLEAN_SYM_RE.match(body)
    if m:
        sym = m.group(1)
        head = body.split(None, 1)[0]
        return sym, head
    return "", "symbol"


def _strip_hash_comments(src: str) -> str:
    """Blank `#` line comments outside double-quoted strings while preserving exact offsets."""
    if not src or "#" not in src:
        return src
    return _HASH_COMMENT_RX.sub(
        lambda m: " " * len(m.group(0)) if m.group(0).startswith("#") else m.group(0),
        src,
    )


def _build_line_offsets(text: str) -> List[int]:
    """Return 1-based line start character offsets (`offsets[1..line_count]`)."""
    offsets: List[int] = [0, 0]
    pos = 0
    n = len(text)
    while True:
        nl = text.find("\n", pos)
        if nl == -1:
            break
        if nl + 1 < n:
            offsets.append(nl + 1)
        pos = nl + 1
    return offsets


def _offset_to_line(offsets: Sequence[int], pos: int, line_count: int) -> int:
    idx = bisect_right(offsets, pos, lo=1) - 1
    return max(1, min(idx, line_count))


def _find_brace_span_end(
    clean: str,
    offsets: Sequence[int],
    line_count: int,
    start_line: int,
    next_start: Optional[int],
    ext: str,
    kind: str,
) -> Optional[int]:
    if not clean or start_line < 1 or start_line >= len(offsets):
        return None
    n = len(clean)
    i = offsets[start_line]
    if next_start is not None and 1 <= next_start < len(offsets):
        limit = min(n, offsets[next_start])
    else:
        limit = n
    if i >= limit:
        return None

    js_mode = ext in _JS_SPAN_EXTS
    allow_backtick = js_mode or ext == ".go"
    single_quote_char_only = ext == ".rs"
    paren_depth = 0
    bracket_depth = 0
    prev_tok = ""

    while i < limit:
        c = clean[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in ('"', "'") or (allow_backtick and c == "`"):
            nxt_i = _skip_string_literal(
                clean, i, allow_backtick=allow_backtick, single_quote_char_only=single_quote_char_only
            )
            prev_tok = c if nxt_i > i + 1 or not single_quote_char_only else "'"
            i = nxt_i
            continue
        if js_mode and c == "/":
            if not prev_tok or prev_tok in _JS_REGEX_PREV_CHARS or prev_tok in _JS_REGEX_PREV_KW:
                i = _skip_js_regex(clean, i)
                prev_tok = "/"
                continue
            prev_tok = "/"
            i += 1
            continue
        if c == "(":
            paren_depth += 1
            prev_tok = c
            i += 1
            continue
        if c == ")":
            paren_depth = max(0, paren_depth - 1)
            prev_tok = c
            i += 1
            continue
        if c == "[":
            bracket_depth += 1
            prev_tok = c
            i += 1
            continue
        if c == "]":
            bracket_depth = max(0, bracket_depth - 1)
            prev_tok = c
            i += 1
            continue
        if c == "{" and paren_depth == 0 and bracket_depth == 0:
            close_b = _find_matching_delim(
                clean,
                i,
                "{",
                "}",
                allow_backtick=allow_backtick,
                single_quote_char_only=single_quote_char_only,
                js_mode=js_mode,
            )
            if close_b >= n or clean[close_b] != "}":
                return None
            if (ext == ".go" and kind in ("func", "method")) or (js_mode and kind in ("function", "async")):
                while True:
                    j = close_b + 1
                    while j < n and clean[j] in " \t":
                        j += 1
                    if j < n and clean[j] == "{":
                        close_b = _find_matching_delim(
                            clean,
                            j,
                            "{",
                            "}",
                            allow_backtick=allow_backtick,
                            single_quote_char_only=single_quote_char_only,
                            js_mode=js_mode,
                        )
                        if close_b >= n or clean[close_b] != "}":
                            return None
                    else:
                        break
            return _offset_to_line(offsets, close_b, line_count)
        if c == ";" and paren_depth == 0 and bracket_depth == 0:
            return _offset_to_line(offsets, i, line_count)
        if js_mode and (c.isalpha() or c in "_$"):
            j = i + 1
            while j < limit and (clean[j].isalnum() or clean[j] in "_$"):
                j += 1
            prev_tok = clean[i:j]
            i = j
            continue
        if js_mode and c in "+-" and i + 1 < limit and clean[i + 1] == c:
            prev_tok = c * 2
            i += 2
            continue
        prev_tok = c
        i += 1
    return None


def _extract_outline_spans(full_path: Path, rel: str, ext: str) -> List[Dict[str, Any]]:
    outline = extract_file_outline(full_path, rel, max_items=500)
    line_count = int(outline.get("lines") or 0)
    if line_count <= 0:
        return []

    raw_items: List[Tuple[int, int, str, str]] = []
    for order_idx, item in enumerate(outline.get("symbols", ())):
        m_ln = _OUTLINE_LINE_RE.match(str(item).strip())
        if not m_ln:
            continue
        start = int(m_ln.group("line"))
        sym, kind = _clean_outline_symbol(m_ln.group("body"), ext)
        if not sym:
            continue
        start = max(1, min(start, line_count))
        raw_items.append((start, order_idx, sym, kind))

    if not raw_items:
        return []

    clean_src: Optional[str] = None
    offsets: List[int] = []
    if ext in _BRACE_SPAN_EXTS:
        norm_bytes = _read_norm_bytes(full_path)
        if norm_bytes is not None:
            raw_text = norm_bytes.decode("utf-8", errors="replace")
            if ext in _JS_SPAN_EXTS:
                clean_src = _strip_js_comments(raw_text)
            elif ext in (".graphql", ".gql"):
                clean_src = _strip_hash_comments(raw_text)
            else:
                clean_src = _strip_c_comments(
                    raw_text,
                    allow_backtick=(ext == ".go"),
                    single_quote_char_only=(ext == ".rs"),
                )
            offsets = _build_line_offsets(clean_src)

    raw_items.sort(key=lambda row: (row[0], row[1]))
    spans: List[Dict[str, Any]] = []
    seen_syms: Set[str] = set()
    n_items = len(raw_items)
    for idx, (start, _, sym, kind) in enumerate(raw_items):
        if sym in seen_syms:
            continue
        seen_syms.add(sym)
        next_start: Optional[int] = None
        for j in range(idx + 1, n_items):
            if raw_items[j][0] > start:
                next_start = raw_items[j][0]
                break
        end: Optional[int] = None
        if clean_src is not None and offsets:
            end = _find_brace_span_end(clean_src, offsets, line_count, start, next_start, ext, kind)
        if end is None:
            end = (next_start - 1) if next_start is not None else line_count
        end = max(start, min(end, line_count))
        spans.append({"path": rel, "symbol": sym, "kind": kind, "start": start, "end": end})

    return spans


def extract_symbol_spans(full_path: Path, rel: str) -> List[Dict[str, Any]]:
    """Extract top-level classes/methods/functions (`.py`) or outline spans (JS/TS/Go/Rust/JVM/C#)."""
    ext = full_path.suffix.lower()
    if ext == ".py":
        norm_bytes = _read_norm_bytes(full_path)
        if norm_bytes is None:
            return []
        spans, _ = _extract_python_spans(norm_bytes, rel)
        return spans
    if ext in _OUTLINE_SPAN_EXTS:
        try:
            if not full_path.is_file():
                return []
        except OSError:
            return []
        return _extract_outline_spans(full_path, rel, ext)
    return []


def _record_sort_key(rec: Mapping[str, Any]) -> Tuple[str, int, str]:
    return (str(rec["relpath"]), int(rec["start"]), str(rec["symbol"]))


def _parse_hub_line(line: str) -> Optional[Dict[str, Any]]:
    m = HUB_ENTRY_RE.match(line)
    if not m:
        return None
    rel = normalize_relpath(m.group("path").strip())
    sym = m.group("symbol").strip()
    start = int(m.group("start"))
    end = int(m.group("end"))
    f_val = m.group("f").strip()
    s_val = m.group("s").strip()
    return {
        "relpath": rel,
        "symbol": sym,
        "start": start,
        "end": end,
        "f": f_val,
        "s": s_val,
        "line": format_hub_entry(rel, sym, start, end, f_val, s_val),
    }


def parse_hubs_volume(hubs_path: Path) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """
    Parse `.aoci/aoci.hubs.txt` into `(records, malformed)`.
    Repeated `(relpath, symbol)` keys keep the last occurrence; `records` is sorted by `(relpath, start, symbol)`.
    """
    if not hubs_path.exists():
        return [], []
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    malformed: List[Tuple[int, str]] = []
    text = hubs_path.read_text(encoding="utf-8", errors="replace")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or (line.startswith("===") and line.endswith("===")):
            continue
        rec = _parse_hub_line(line)
        if rec is None or not rec["symbol"]:
            malformed.append((lineno, line))
            continue
        by_key[(rec["relpath"], rec["symbol"])] = rec
    records = sorted(by_key.values(), key=_record_sort_key)
    return records, malformed


def write_hubs_volume(hubs_path: Path, records: Any, updated_at: Optional[str] = None) -> str:
    """Deterministically sort hub records by `(relpath, start, symbol)` and write via `atomic_write_text`."""
    items = records.values() if isinstance(records, Mapping) else records
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in items:
        if isinstance(item, str):
            rec = _parse_hub_line(item.strip())
            if rec is None:
                raise ValueError(f"Malformed hub record line: {item!r}")
        elif isinstance(item, Mapping):
            rel = normalize_relpath(str(item.get("relpath") or item.get("path") or "").strip())
            sym = str(item["symbol"]).strip()
            start = int(item["start"])
            end = int(item["end"])
            f_val = str(item["f"]).strip()
            s_val = str(item["s"]).strip()
            rec = {
                "relpath": rel,
                "symbol": sym,
                "start": start,
                "end": end,
                "f": f_val,
                "s": s_val,
                "line": format_hub_entry(rel, sym, start, end, f_val, s_val),
            }
        else:
            raise TypeError(f"Unsupported hub record type: {type(item).__name__}")
        by_key[(rec["relpath"], rec["symbol"])] = rec

    ordered = sorted(by_key.values(), key=_record_sort_key)
    out_lines: List[str] = [
        "#AOCI-HUBS-VOLUME: 1",
        "#Format: symbol-hubs-v1",
        f"#Updated-At: {updated_at or utc_now_iso()}",
        f"#Tracked-Sections: {len(ordered)}",
        "",
    ]
    for rec in ordered:
        out_lines.append(rec["line"])
    if ordered:
        out_lines.append("")
    rendered = "\n".join(out_lines)
    hubs_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(hubs_path, rendered)
    return rendered


def scaffold_hub_entries(root: Path, rel: str) -> List[str]:
    """Generate `<relpath>#<sym>[Lstart-Lend]: F:<TODO:read-section> | S:-` skeletons."""
    norm_rel = normalize_relpath(rel)
    full_path = Path(root) / norm_rel
    if not _is_within_root(Path(root), full_path):
        return []
    spans = extract_symbol_spans(full_path, norm_rel)
    return [
        format_hub_entry(norm_rel, sp["symbol"], int(sp["start"]), int(sp["end"]), "<TODO:read-section>", "-")
        for sp in spans
    ]


def _check_regular_target_within_root(root: Path, rel: str) -> Tuple[Optional[Path], Optional[int], Optional[str]]:
    full = root / rel
    if not _is_within_root(root, full):
        return None, None, f"Path `{rel}` escapes repository root."
    try:
        st = os.lstat(full)
    except OSError:
        return None, None, f"Target file does not exist on disk: `{rel}`"
    if stat.S_ISLNK(st.st_mode):
        return None, None, f"Refusing symlink target `{rel}`."
    if not stat.S_ISREG(st.st_mode):
        return None, None, f"Target `{rel}` is not a regular file."
    norm_bytes = _read_norm_bytes(full)
    if norm_bytes is None:
        return None, None, f"Cannot read target file `{rel}`."
    return full, _count_norm_lines(norm_bytes), None


def _extract_live_symbol_span_map(
    full_path: Path,
    rel: str,
) -> Tuple[Optional[Dict[str, Tuple[int, int]]], bool]:
    ext = full_path.suffix.lower()
    if ext == ".py":
        norm_bytes = _read_norm_bytes(full_path)
        if norm_bytes is None:
            return None, False
        spans, parsed_ok = _extract_python_spans(norm_bytes, rel)
        if not parsed_ok:
            return None, False
    elif ext in _OUTLINE_SPAN_EXTS:
        spans = _extract_outline_spans(full_path, rel, ext)
    else:
        return None, False
    span_map: Dict[str, Tuple[int, int]] = {}
    for sp in spans:
        span_map.setdefault(str(sp["symbol"]), (int(sp["start"]), int(sp["end"])))
    return span_map, True


def _extract_live_symbol_set_for_prune(full_path: Path, rel: str) -> Tuple[Optional[Set[str]], bool]:
    """
    Return `(symbol_names, supported_and_clean)` for `--prune-missing`.
    Returns `(None, False)` for unsupported extensions or `.py` files with SyntaxError so they aren't pruned.
    """
    span_map, clean_parse = _extract_live_symbol_span_map(full_path, rel)
    if not clean_parse or span_map is None:
        return None, False
    return set(span_map.keys()), True


def upsert_hub_entries(
    root: Path,
    aoci_dir: Path,
    raw_lines: Sequence[str],
    *,
    prune_missing: bool = False,
    dry_run: bool = False,
    lock_cls: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Validate and atomically merge hub entries into `.aoci/aoci.hubs.txt`.
    Validates path within `root`, `1 <= start <= end <= actual_lines`, rejects `<TODO`,
    and checks `F:` non-empty. Uses `lock_cls(aoci_dir)` unless `dry_run=True` or `lock_cls is None`.
    """
    root_res = Path(root).resolve()
    aoci_path = Path(aoci_dir)
    hubs_path = aoci_path / HUBS_FILE

    if not aoci_path.is_dir():
        return {
            "ok": False,
            "dry_run": dry_run,
            "hubs_path": str(hubs_path),
            "errors": [f"`.aoci/` is not initialized at {root_res}. Run `init` first."],
            "warnings": [],
        }

    errors: List[str] = []
    warnings: List[str] = []
    parsed_inputs: List[Dict[str, Any]] = []
    line_count_cache: Dict[str, Optional[int]] = {}
    seen_input_keys: Set[Tuple[str, str]] = set()

    for raw in raw_lines:
        line = str(raw).strip()
        if not line or line.startswith("#") or (line.startswith("===") and line.endswith("===")):
            continue
        if "\n" in line or "\r" in line:
            errors.append(f"Hub entry must be strictly single-line: {line[:120]}")
            continue
        m = HUB_ENTRY_RE.match(line)
        if not m:
            errors.append(
                f"Syntax mismatch (expected `<relpath>#<symbol>[L<start>-L<end>]: F:<...> | S:<...>`): {line[:120]}"
            )
            continue

        raw_rel = m.group("path").strip()
        rel, path_err = validate_hub_relpath(raw_rel)
        if path_err is not None or rel is None:
            errors.append(path_err or f"Invalid path: `{raw_rel}`")
            continue

        sym = m.group("symbol").strip()
        if not sym:
            errors.append(f"Symbol name cannot be empty in `{rel}`.")
            continue

        if "<todo" in line.lower():
            errors.append(
                f"Unfilled `<TODO:...>` placeholder in `{rel}#{sym}`; read the section and author concrete semantics."
            )
            continue

        f_val = m.group("f").strip()
        s_val = m.group("s").strip()
        if not f_val or f_val == "-":
            errors.append(
                f"Field `F:` must describe section responsibility (cannot be `-` or empty) in `{rel}#{sym}`."
            )
            continue
        if not s_val:
            errors.append(
                f"Field `S:` cannot be empty (use `-` if no local invariant) in `{rel}#{sym}`."
            )
            continue

        if rel not in line_count_cache:
            _, actual_lines, target_err = _check_regular_target_within_root(root_res, rel)
            if target_err is not None:
                errors.append(target_err)
                line_count_cache[rel] = None
                continue
            line_count_cache[rel] = actual_lines
        actual_lines = line_count_cache[rel]
        if actual_lines is None:
            errors.append(f"Target file `{rel}` is unavailable on disk.")
            continue

        start = int(m.group("start"))
        end = int(m.group("end"))
        if start < 1 or start > end:
            errors.append(
                f"Invalid line span `[L{start}-L{end}]` for `{rel}#{sym}` (expected `1 <= start <= end`)."
            )
            continue
        if end > actual_lines:
            errors.append(
                f"Line span `[L{start}-L{end}]` exceeds `{rel}` line count ({actual_lines}) for `{rel}#{sym}`."
            )
            continue

        key = (rel, sym)
        if key in seen_input_keys:
            warnings.append(f"Duplicate hub entry for `{rel}#{sym}` in input; the last one wins.")
        seen_input_keys.add(key)
        parsed_inputs.append(
            {
                "relpath": rel,
                "symbol": sym,
                "start": start,
                "end": end,
                "f": f_val,
                "s": s_val,
                "line": format_hub_entry(rel, sym, start, end, f_val, s_val),
            }
        )

    if errors:
        return {
            "ok": False,
            "dry_run": dry_run,
            "hubs_path": str(hubs_path),
            "errors": errors,
            "warnings": warnings,
        }

    if not parsed_inputs and not prune_missing:
        return {
            "ok": False,
            "dry_run": dry_run,
            "hubs_path": str(hubs_path),
            "errors": ["No hub entries provided (pass `--upsert`, `--upsert-file`, or `--prune-missing`)."],
            "warnings": warnings,
        }

    lock_ctx: Any = contextlib.nullcontext() if (dry_run or lock_cls is None) else lock_cls(aoci_path)
    with lock_ctx:
        existing_records, malformed = parse_hubs_volume(hubs_path)
        if malformed:
            return {
                "ok": False,
                "dry_run": dry_run,
                "hubs_path": str(hubs_path),
                "errors": [
                    f"`{HUBS_FILE}` contains {len(malformed)} malformed line(s); fix them before rewriting."
                ],
                "malformed": [{"line": ln, "text": txt[:120]} for ln, txt in malformed[:10]],
                "warnings": warnings,
            }

        merged: Dict[Tuple[str, str], Dict[str, Any]] = {
            (rec["relpath"], rec["symbol"]): rec for rec in existing_records
        }
        upserted: List[str] = []
        upserted_seen: Set[Tuple[str, str]] = set()
        for rec in parsed_inputs:
            key = (rec["relpath"], rec["symbol"])
            merged[key] = rec
            if key not in upserted_seen:
                upserted_seen.add(key)
                upserted.append(f"{rec['relpath']}#{rec['symbol']}")

        pruned: List[str] = []
        if prune_missing:
            live_sym_cache: Dict[str, Tuple[Optional[Set[str]], bool]] = {}
            for key in list(merged.keys()):
                rel_k, sym_k = key
                full_k = root_res / rel_k
                if not _is_within_root(root_res, full_k):
                    del merged[key]
                    pruned.append(f"{rel_k}#{sym_k}")
                    continue
                try:
                    st = os.lstat(full_k)
                    is_reg = not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode)
                except OSError:
                    is_reg = False
                if not is_reg:
                    del merged[key]
                    pruned.append(f"{rel_k}#{sym_k}")
                    continue
                if key not in upserted_seen:
                    if rel_k not in live_sym_cache:
                        live_sym_cache[rel_k] = _extract_live_symbol_set_for_prune(full_k, rel_k)
                    live_syms, clean_parse = live_sym_cache[rel_k]
                    if clean_parse and live_syms is not None and sym_k not in live_syms:
                        del merged[key]
                        pruned.append(f"{rel_k}#{sym_k}")

        ordered = sorted(merged.values(), key=_record_sort_key)
        if not dry_run and (upserted or pruned):
            write_hubs_volume(hubs_path, ordered)
            sync_manifest_hubs_volume(aoci_path)

    return {
        "ok": True,
        "dry_run": dry_run,
        "hubs_path": str(hubs_path),
        "upserted": upserted,
        "pruned": pruned,
        "total": len(ordered),
        "entries": [rec["line"] for rec in ordered],
        "warnings": warnings,
    }


_HUBS_VOLUME_LINE = "#Volume: id=hubs kind=hubs path=aoci.hubs.txt format=symbol-hubs-v1 depends=code state=enabled"


def sync_manifest_hubs_volume(aoci_dir: Path) -> None:
    """Idempotently register or unregister `aoci.hubs.txt` in `.aoci/aoci.txt`."""
    manifest_path = Path(aoci_dir) / MANIFEST_FILE
    if not manifest_path.is_file():
        return
    hubs_exists = (Path(aoci_dir) / HUBS_FILE).is_file()
    try:
        raw = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = raw.splitlines()
    has_hub_vol = any(ln.strip().startswith("#Volume:") and "id=hubs" in ln for ln in lines)
    if hubs_exists and not has_hub_vol:
        new_lines: List[str] = []
        inserted = False
        for ln in lines:
            new_lines.append(ln)
            if not inserted and ln.strip().startswith("#Volume:") and "id=code" in ln:
                new_lines.append(_HUBS_VOLUME_LINE)
                inserted = True
        if not inserted:
            new_lines.append(_HUBS_VOLUME_LINE)
        atomic_write_text(manifest_path, "\n".join(new_lines) + "\n")
    elif not hubs_exists and has_hub_vol:
        new_lines = [ln for ln in lines if not (ln.strip().startswith("#Volume:") and "id=hubs" in ln)]
        atomic_write_text(manifest_path, "\n".join(new_lines) + "\n")


def _refresh_spans_in_records(
    root_res: Path,
    records: List[Dict[str, Any]],
    target_paths: Set[str],
) -> List[str]:
    relevant_paths = {rec["relpath"] for rec in records} & target_paths
    if not relevant_paths:
        return []
    span_lookup: Dict[str, Dict[str, Tuple[int, int]]] = {}
    for rel in sorted(relevant_paths):
        full = root_res / rel
        if not _is_within_root(root_res, full):
            continue
        try:
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
        except OSError:
            continue
        spans = extract_symbol_spans(full, rel)
        if spans:
            span_lookup[rel] = {sp["symbol"]: (int(sp["start"]), int(sp["end"])) for sp in spans}
    if not span_lookup:
        return []
    refreshed: List[str] = []
    for rec in records:
        rel = rec["relpath"]
        sym_map = span_lookup.get(rel)
        if not sym_map:
            continue
        new_span = sym_map.get(rec["symbol"])
        if new_span is None:
            continue
        new_start, new_end = new_span
        old_start, old_end = int(rec["start"]), int(rec["end"])
        if old_start != new_start or old_end != new_end:
            rec["start"] = new_start
            rec["end"] = new_end
            rec["line"] = format_hub_entry(rel, rec["symbol"], new_start, new_end, rec["f"], rec["s"])
            refreshed.append(f"{rel}#{rec['symbol']} [L{old_start}-L{old_end}]->[L{new_start}-L{new_end}]")
    return refreshed


def refresh_hub_spans_for_paths(root: Path, aoci_dir: Path, paths: Sequence[str]) -> List[str]:
    """
    Refresh `[Lstart-Lend]` spans in `.aoci/aoci.hubs.txt` for matching `<relpath>#<symbol>` entries
    whenever symbol line numbers shift on disk. Caller must hold any index lock if applicable.
    """
    hubs_path = Path(aoci_dir) / HUBS_FILE
    if not hubs_path.is_file() or not paths:
        return []
    records, malformed = parse_hubs_volume(hubs_path)
    if not records or malformed:
        return []
    target_set = {normalize_relpath(str(p)) for p in paths if str(p).strip()}
    refreshed = _refresh_spans_in_records(Path(root).resolve(), records, target_set)
    if refreshed:
        write_hubs_volume(hubs_path, records)
    return refreshed


def sync_hubs_on_upsert(
    root: Path,
    aoci_dir: Path,
    upserted_paths: Sequence[str],
    pruned_paths: Sequence[str],
) -> Dict[str, List[str]]:
    """
    Keep `.aoci/aoci.hubs.txt` aligned during `upsert`: drop entries for pruned files and
    refresh `[Lstart-Lend]` spans for upserted files. Caller must hold `.aoci/.lock`.
    """
    hubs_path = Path(aoci_dir) / HUBS_FILE
    if not hubs_path.is_file() or (not upserted_paths and not pruned_paths):
        return {"refreshed": [], "pruned": []}
    records, malformed = parse_hubs_volume(hubs_path)
    if not records or malformed:
        return {"refreshed": [], "pruned": []}

    pruned_set = {normalize_relpath(str(p)) for p in pruned_paths if str(p).strip()}
    kept: List[Dict[str, Any]] = []
    pruned_labels: List[str] = []
    for rec in records:
        if rec["relpath"] in pruned_set:
            pruned_labels.append(f"{rec['relpath']}#{rec['symbol']}")
        else:
            kept.append(rec)

    upserted_set = {normalize_relpath(str(p)) for p in upserted_paths if str(p).strip()} - pruned_set
    refreshed = _refresh_spans_in_records(Path(root).resolve(), kept, upserted_set) if upserted_set else []

    if pruned_labels or refreshed:
        write_hubs_volume(hubs_path, kept)
        sync_manifest_hubs_volume(aoci_dir)
    return {"refreshed": refreshed, "pruned": pruned_labels}


def migrate_hubs_renames(
    root: Path,
    aoci_dir: Path,
    rename_map: Mapping[str, str],
    dry_run: bool = False,
) -> List[str]:
    """
    Migrate `<old_rel>#<sym>` entries in `.aoci/aoci.hubs.txt` after `migrate-renames` and
    refresh `[Lstart-Lend]` spans for moved+edited targets.
    """
    hubs_path = Path(aoci_dir) / HUBS_FILE
    if not rename_map or not hubs_path.is_file():
        return []
    records, malformed = parse_hubs_volume(hubs_path)
    if not records or malformed:
        return []

    migrated: List[str] = []
    new_paths: Set[str] = set()
    updated_records: List[Dict[str, Any]] = []
    for rec in records:
        old_rel = rec["relpath"]
        if old_rel in rename_map:
            new_rel = normalize_relpath(str(rename_map[old_rel]))
            new_rec = dict(rec)
            new_rec["relpath"] = new_rel
            new_rec["line"] = format_hub_entry(new_rel, rec["symbol"], rec["start"], rec["end"], rec["f"], rec["s"])
            updated_records.append(new_rec)
            new_paths.add(new_rel)
            migrated.append(f"{old_rel}#{rec['symbol']}->{new_rel}#{rec['symbol']}")
        else:
            updated_records.append(rec)

    if not migrated:
        return []

    if not dry_run:
        _refresh_spans_in_records(Path(root).resolve(), updated_records, new_paths)
        write_hubs_volume(hubs_path, updated_records)
        sync_manifest_hubs_volume(aoci_dir)
    return migrated


def validate_hubs_volume(
    root: Path,
    aoci_dir: Path,
    code_entries: Mapping[str, str],
    fingerprints: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    check_live_symbols: bool = True,
) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Validate `.aoci/aoci.hubs.txt` for `verify` and `lint`.
    Returns `(verify_issues, lint_findings)`.
    """
    hubs_path = Path(aoci_dir) / HUBS_FILE
    if not hubs_path.is_file():
        return [], []
    records, malformed = parse_hubs_volume(hubs_path)
    verify_issues: List[str] = []
    lint_findings: List[Dict[str, str]] = []

    for ln, txt in malformed:
        msg = f"`{HUBS_FILE}` L{ln} is malformed: {txt[:100]}"
        verify_issues.append(msg)
        lint_findings.append({"path": f"{HUBS_FILE}:L{ln}", "code": "hub-malformed", "message": txt[:120]})

    root_res = Path(root).resolve()
    line_count_cache: Dict[str, Optional[int]] = {}
    live_sym_cache: Dict[str, Tuple[Optional[Dict[str, Tuple[int, int]]], bool]] = {}

    for rec in records:
        rel = rec["relpath"]
        sym = rec["symbol"]
        hub_id = f"{rel}#{sym}"
        start = int(rec["start"])
        end = int(rec["end"])
        f_val = rec["f"]
        s_val = rec["s"]

        if "<todo" in rec["line"].lower():
            msg = f"Unfilled `<TODO:...>` placeholder in `{HUBS_FILE}` for `{hub_id}`."
            verify_issues.append(msg)
            lint_findings.append({"path": hub_id, "code": "hub-todo", "message": msg})
        if not f_val or f_val == "-":
            msg = f"Field `F:` cannot be `-` or empty in `{HUBS_FILE}` for `{hub_id}`."
            verify_issues.append(msg)
            lint_findings.append({"path": hub_id, "code": "hub-F-empty", "message": msg})
        if not s_val:
            msg = f"Field `S:` cannot be empty in `{HUBS_FILE}` for `{hub_id}`."
            verify_issues.append(msg)
            lint_findings.append({"path": hub_id, "code": "hub-S-empty", "message": msg})

        if rel not in code_entries:
            lint_findings.append(
                {
                    "path": hub_id,
                    "code": "hub-unindexed-file",
                    "message": f"Hub parent file `{rel}` is not indexed in `aoci.code.txt`.",
                }
            )

        if rel not in line_count_cache:
            fp_meta = fingerprints.get(rel) if fingerprints is not None else None
            if fp_meta is not None and "lines" in fp_meta:
                line_count_cache[rel] = int(fp_meta["lines"])
            else:
                _, actual_l, err_s = _check_regular_target_within_root(root_res, rel)
                line_count_cache[rel] = actual_l if err_s is None else None
                if err_s is not None:
                    verify_issues.append(f"`{HUBS_FILE}` `{hub_id}`: {err_s}")
                    lint_findings.append({"path": hub_id, "code": "hub-file-missing", "message": err_s})

        actual_lines = line_count_cache[rel]
        if actual_lines is not None:
            if start < 1 or start > end or end > actual_lines:
                msg = (
                    f"`{HUBS_FILE}` line span `[L{start}-L{end}]` is out of bounds for `{rel}` "
                    f"({actual_lines} lines) in `{hub_id}`."
                )
                verify_issues.append(msg)
                lint_findings.append({"path": hub_id, "code": "hub-span-oob", "message": msg})

            if check_live_symbols:
                full_k = root_res / rel
                if rel not in live_sym_cache and full_k.is_file():
                    live_sym_cache[rel] = _extract_live_symbol_span_map(full_k, rel)
                live_spans, clean_parse = live_sym_cache.get(rel, (None, False))
                if clean_parse and live_spans is not None:
                    if sym not in live_spans:
                        lint_findings.append(
                            {
                                "path": hub_id,
                                "code": "hub-symbol-missing",
                                "message": f"Symbol `{sym}` was not found in `{rel}`; update or run `zoom --prune-missing`.",
                            }
                        )
                    else:
                        live_s, live_e = live_spans[sym]
                        if (live_s, live_e) != (start, end):
                            lint_findings.append(
                                {
                                    "path": hub_id,
                                    "code": "hub-span-outdated",
                                    "message": (
                                        f"Span `[L{start}-L{end}]` for `{hub_id}` differs from live span "
                                        f"`[L{live_s}-L{live_e}]`; run `ack` or `upsert` to refresh."
                                    ),
                                }
                            )

    return verify_issues, lint_findings


def lookup_hub_sections(
    aoci_dir: Path,
    target_paths: Iterable[str],
    preloaded_records: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return matching `.aoci/aoci.hubs.txt` sections for `target_paths`."""
    wanted = {normalize_relpath(str(p)) for p in target_paths if str(p).strip()}
    if not wanted:
        return []
    if preloaded_records is not None:
        records = preloaded_records
    else:
        hubs_path = Path(aoci_dir) / HUBS_FILE
        if not hubs_path.is_file():
            return []
        records, _ = parse_hubs_volume(hubs_path)
    out: List[Dict[str, Any]] = []
    for rec in records:
        if rec["relpath"] in wanted:
            out.append(
                {
                    "path": rec["relpath"],
                    "symbol": rec["symbol"],
                    "start": int(rec["start"]),
                    "end": int(rec["end"]),
                    "f": rec["f"],
                    "s": rec["s"],
                    "entry": rec["line"],
                }
            )
    return out
