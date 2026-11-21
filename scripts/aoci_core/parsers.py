"""Multi-language static outline extraction, `sig_hash`, and import graph resolution."""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .constants import (
    CACHE_DIR,
    OUTLINE_CACHE,
    PARALLEL_HASH_THRESHOLD,
    SIG_HASH_EXTS,
    atomic_write_text,
    compute_size_tier,
    normalize_relpath,
    strip_utf8_bom_bytes,
)
from . import ts_accel as _ts_accel

_JS_REGEX_PREV_CHARS: Set[str] = set("=([!&|?:;{},~^+-*%>")
_JS_REGEX_PREV_KW: Set[str] = {"return", "typeof", "void", "case", "throw", "delete", "in", "of", "yield", "await"}
_JS_STATIC_IMPORT_RE = re.compile(
    r"""(?:^|[;\n])\s*import\s+(?:type\s+)?(?:"""
    r"""(?:[A-Za-z_$][\w$]*(?:\s*,\s*(?:\*\s+as\s+[A-Za-z_$][\w$]*|\{[^}]*\}))?"""
    r"""|\*\s+as\s+[A-Za-z_$][\w$]*|\{[^}]*\})\s*from\s*['"]([^'"]+)['"]"""
    r"""|['"]([^'"]+)['"])"""
)
_JS_REEXPORT_RE = re.compile(
    r"""(?:^|[;\n])\s*export\s+(?:type\s+)?(?:\*(?:\s+as\s+([A-Za-z_$][\w$]*))?|\{([^}]*)\})\s*from\s*['"]([^'"]+)['"]"""
)
_JS_LOCAL_EXPORT_RE = re.compile(
    r"""(?:^|[;\n])\s*export\s+(?:type\s+)?\{([^}]*)\}(?!\s*from\b)"""
)
_JS_CALL_IMPORT_RE = re.compile(r"""(?<![.\w$])(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_CJS_EXPORT_RE = re.compile(r"""^\s*(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=""")
_JS_SYM_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(function|class|interface|type|const|enum)\s+([A-Za-z_$][\w$]*)"
)
_SFC_SCRIPT_RE = re.compile(r"<script\b[^>]*>([\s\S]*?)</script>", re.IGNORECASE)
_JS_LEADING_EXPORT_RE = re.compile(r"^\s*export\b")
_GO_SYM_RE = re.compile(r"^(func(?:\s*\([^)]+\))?\s+[A-Za-z_]\w*|type\s+[A-Za-z_]\w*\s+(?:struct|interface))")
_RS_SYM_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?(fn|struct|enum|trait|mod)\s+([A-Za-z_]\w*)")
_JVM_CS_SYM_RE = re.compile(
    r"^\s*(?:public|private|protected|internal|export|suspend|inline)?\s*(?:static\s+)?(?:abstract\s+|final\s+|sealed\s+|data\s+)?(class|interface|enum|record|object|struct|fun)\s+([A-Za-z_]\w*)"
)

_JS_RESOLVE_EXTS: Tuple[str, ...] = (
    "",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".vue",
    ".svelte",
    "/index.ts",
    "/index.tsx",
    "/index.js",
    "/index.jsx",
)

_CLEAN_SYM_RE = re.compile(
    r"^(?:async\s+def|def|class|export|function|interface|type|const|enum|fn|struct|trait|mod|fun|record|object|"
    r"func(?:\s*\([^)]*\))?|service|rpc|message|model|input|union|scalar|namespace|table|view|procedure|index)\s+"
    r"([A-Za-z_$][\w$:]*)"
)

_PROTO_IMPORT_RE = re.compile(r'^\s*import\s+(?:public\s+|weak\s+)?"([^"]+)"\s*;')
_PROTO_ITEM_RE = re.compile(r"^\s*(service|message|enum)\s+([A-Za-z_]\w*)")
_PROTO_RPC_RE = re.compile(r"^\s*rpc\s+([A-Za-z_]\w*)\s*\(")
_GRAPHQL_ITEM_RE = re.compile(r"^\s*(?:extend\s+)?(type|input|interface|enum|union|scalar)\s+([A-Za-z_]\w*)")
_PRISMA_ITEM_RE = re.compile(r"^\s*(model|enum|type|view)\s+([A-Za-z_]\w*)")
_SQL_CREATE_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:MATERIALIZED\s+)?"
    r"(TABLE|VIEW|FUNCTION|PROCEDURE|INDEX|TYPE)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?:[`"\[]?[A-Za-z_]\w*[`"\]]?\.)?[`"\[]?([A-Za-z_][\w$]*)',
    re.IGNORECASE,
)
_C_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]')
_CPP_TYPE_RE = re.compile(
    r"^\s*(?:template\s*<[^>]*>\s*)?(namespace|class|struct|enum(?:\s+class)?|union)\s+([A-Za-z_]\w*)"
)
_CPP_FN_RE = re.compile(
    r"^\s*(?:(?:static|inline|extern|virtual|constexpr|consteval|explicit|friend)\s+)*"
    r"(?:[A-Za-z_~][\w:*&<>,\s]*?\s+)?([A-Za-z_~][\w:]*(?:::[A-Za-z_~]\w*)?)\s*\([^;{}]*\)"
    r"(?:\s*(?:const|noexcept(?:\([^)]*\))?|override|final))*\s*(?:\{|;|$)"
)
_CPP_SKIP_KEYWORDS: Set[str] = {"if", "for", "while", "switch", "catch", "return", "sizeof", "alignof", "decltype"}


_JS_QUOTE_OR_SLASH_RX = re.compile(r"""["'`/]""")
_JS_STR_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    '"': re.compile(r'"[^"\\]*(?:\\[\s\S][^"\\]*)*(?:"|$)'),
    "'": re.compile(r"'[^'\\]*(?:\\[\s\S][^'\\]*)*(?:'|$)"),
    "`": re.compile(r"`[^`\\]*(?:\\[\s\S][^`\\]*)*(?:`|$)"),
}

_C_COMMENT_RX_CACHE: Dict[Tuple[bool, bool], "re.Pattern[str]"] = {}


def _get_c_comment_rx(allow_backtick: bool, single_quote_char_only: bool) -> "re.Pattern[str]":
    key = (allow_backtick, single_quote_char_only)
    rx = _C_COMMENT_RX_CACHE.get(key)
    if rx is not None:
        return rx
    parts = [
        r'"[^"\\]*(?:\\[\s\S][^"\\]*)*(?:"|$)',
        r"//[^\n]*",
        r"/\*[\s\S]*?(?:\*/|$)",
    ]
    if allow_backtick:
        parts.append(r"`[^`]*(?:`|$)")
    if single_quote_char_only:
        parts.append(r"'(?:\\[^\n]|[^'\\\n]){0,16}'")
    else:
        parts.append(r"'[^'\\\n]*(?:\\[^\n][^'\\\n]*)*(?:'|(?=\n|$))")
    rx = re.compile("|".join(parts))
    _C_COMMENT_RX_CACHE[key] = rx
    return rx


def _blank_block_comment(s: str) -> str:
    if "\n" not in s:
        return " " * len(s)
    return "\n".join(" " * len(part) for part in s.split("\n"))


def _strip_c_sub(m: "re.Match[str]") -> str:
    s = m.group(0)
    if s.startswith("//"):
        return " " * len(s)
    if s.startswith("/*"):
        return _blank_block_comment(s)
    return s


def _prev_js_tok(out: List[str]) -> str:
    tail_parts: List[str] = []
    non_ws = 0
    for chunk in reversed(out):
        if not chunk:
            continue
        tail_parts.append(chunk)
        stripped = chunk.rstrip()
        if stripped:
            non_ws += len(stripped)
            if non_ws >= 64:
                break
    tail = "".join(reversed(tail_parts)).rstrip()
    if not tail:
        return ""
    ch = tail[-1]
    if ch in ("+", "-") and len(tail) >= 2 and tail[-2] == ch:
        return ch * 2
    if ch.isalnum() or ch in "_$":
        j = len(tail) - 1
        while j >= 0 and (tail[j].isalnum() or tail[j] in "_$"):
            j -= 1
        word = tail[j + 1 :]
        return ch if word[0].isdigit() else word
    return ch


def _strip_js_comments(src: str) -> str:
    """
    Blank out `//` and `/* */` comments while preserving string literals, `/regex/` literals
    (including `//` or `/*` inside regexes), and exact line breaks.
    """
    if not src:
        return src
    out: List[str] = []
    pos = 0
    n = len(src)
    while pos < n:
        m = _JS_QUOTE_OR_SLASH_RX.search(src, pos)
        if m is None:
            out.append(src[pos:])
            break
        idx = m.start()
        if idx > pos:
            out.append(src[pos:idx])
        ch = src[idx]
        if ch in ("'", '"', "`"):
            sm = _JS_STR_PATTERNS[ch].match(src, idx)
            end = sm.end() if sm is not None else n
            out.append(src[idx:end])
            pos = end
            continue
        nxt = src[idx + 1] if idx + 1 < n else ""
        if nxt == "/":
            nl = src.find("\n", idx + 2)
            end = nl if nl != -1 else n
            out.append(" " * (end - idx))
            pos = end
        elif nxt == "*":
            close_c = src.find("*/", idx + 2)
            end = (close_c + 2) if close_c != -1 else n
            out.append(_blank_block_comment(src[idx:end]))
            pos = end
        else:
            prev_tok = _prev_js_tok(out)
            if not prev_tok or prev_tok in _JS_REGEX_PREV_CHARS or prev_tok in _JS_REGEX_PREV_KW:
                end = _skip_js_regex(src, idx)
                out.append(src[idx:end])
                pos = end
            else:
                out.append("/")
                pos = idx + 1
    return "".join(out)


def _parse_js_export_brace(brace_body: str, lineno: int, out: List[Tuple[int, str]]) -> None:
    for item in brace_body.split(","):
        tok = re.sub(r"^\s*type\s+", "", item.strip()).strip()
        if not tok:
            continue
        exported = tok.split()[-1] if " as " in tok else tok.split()[0]
        if re.match(r"^[A-Za-z_$][\w$]*$", exported):
            out.append((lineno, f"export {exported} [L{lineno}]"))


def _strip_c_comments(src: str, allow_backtick: bool = False, single_quote_char_only: bool = False) -> str:
    """
    Blank out `//` and `/* */` comments while preserving strings (including optional raw backtick
    strings), Rust single-quote lifetimes when `single_quote_char_only=True`, and exact line breaks.
    """
    if not src or ("/" not in src):
        return src
    return _get_c_comment_rx(allow_backtick, single_quote_char_only).sub(_strip_c_sub, src)


def _extract_js_ts_outline(
    src: str,
    line_offset: int,
    imports: List[str],
    symbols: List[str],
    precleaned: Optional[str] = None,
) -> None:
    clean = precleaned if precleaned is not None else _strip_js_comments(src)
    imp_hits: List[Tuple[int, str]] = []
    reexport_syms: List[Tuple[int, str]] = []

    for m in _JS_STATIC_IMPORT_RE.finditer(clean):
        spec = m.group(1) or m.group(2)
        if spec:
            imp_hits.append((m.start(), spec))

    for m in _JS_REEXPORT_RE.finditer(clean):
        ns_alias, brace_body, spec = m.group(1), m.group(2), m.group(3)
        if spec:
            imp_hits.append((m.start(), spec))
        stmt_offset = m.start() + (len(m.group(0)) - len(m.group(0).lstrip(";\n \t\r")))
        lineno = line_offset + clean.count("\n", 0, stmt_offset) + 1
        if ns_alias:
            reexport_syms.append((lineno, f"export {ns_alias} [L{lineno}]"))
        elif brace_body:
            _parse_js_export_brace(brace_body, lineno, reexport_syms)

    for m in _JS_LOCAL_EXPORT_RE.finditer(clean):
        stmt_offset = m.start() + (len(m.group(0)) - len(m.group(0).lstrip(";\n \t\r")))
        lineno = line_offset + clean.count("\n", 0, stmt_offset) + 1
        _parse_js_export_brace(m.group(1), lineno, reexport_syms)

    for m in _JS_CALL_IMPORT_RE.finditer(clean):
        imp_hits.append((m.start(), m.group(1)))

    imp_hits.sort(key=lambda pair: pair[0])
    for _, spec in imp_hits:
        imports.append(spec)

    sym_hits: List[Tuple[int, str]] = list(reexport_syms)
    src_lines = clean[:-1].split("\n") if clean.endswith("\n") else (clean.split("\n") if clean else [])
    for idx, ln in enumerate(src_lines, start=1):
        lineno = idx + line_offset
        ms = _JS_SYM_RE.match(ln)
        if ms and (_JS_LEADING_EXPORT_RE.match(ln) is not None or ms.group(1) in {"function", "class", "interface", "type", "enum"}):
            sym_hits.append((lineno, f"{ms.group(1)} {ms.group(2)} [L{lineno}]"))
            continue
        mc = _JS_CJS_EXPORT_RE.match(ln)
        if mc:
            sym_hits.append((lineno, f"export {mc.group(1)} [L{lineno}]"))

    sym_hits.sort(key=lambda pair: pair[0])
    for _, label in sym_hits:
        symbols.append(label)


_PY_CONTRACT_DUNDERS: Set[str] = {
    "__init__",
    "__new__",
    "__post_init__",
    "__call__",
    "__enter__",
    "__aenter__",
    "__exit__",
    "__aexit__",
    "__getitem__",
    "__setitem__",
    "__delitem__",
    "__len__",
    "__iter__",
    "__aiter__",
    "__next__",
    "__anext__",
    "__contains__",
    "__eq__",
    "__hash__",
}


def _py_route_decorator(node: ast.AST) -> str:
    for dec in getattr(node, "decorator_list", ()):
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
            and isinstance(dec.args[0].value, str)
            and dec.args[0].value.startswith("/")
        ):
            return f" [@{dec.func.attr}('{dec.args[0].value}')]"
    return ""


def _ast_decorator_str(dec: ast.AST) -> str:
    """Format a Python decorator AST node into a compact canonical string."""
    if isinstance(dec, ast.Call):
        fn_name = _ast_type_str(dec.func)
        if not fn_name:
            return ""
        pos_args = [_ast_type_str(a) for a in dec.args]
        kw_args = [
            f"{kw.arg}={_ast_type_str(kw.value)}" if kw.arg else f"**{_ast_type_str(kw.value)}"
            for kw in dec.keywords
        ]
        all_args = ",".join([*pos_args, *kw_args])
        return f"@{fn_name}({all_args})"
    name = _ast_type_str(dec)
    return f"@{name}" if name else ""


def _ast_type_str(node: Optional[ast.AST]) -> str:
    """Python 3.8-compatible lightweight formatter for AST type annotations and base classes."""
    if node is None:
        return ""
    if type(node).__name__ == "Index":
        return _ast_type_str(getattr(node, "value", None))
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _ast_type_str(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    if isinstance(node, ast.Subscript):
        val = _ast_type_str(node.value)
        slc = _ast_type_str(node.slice)
        return f"{val}[{slc}]"
    if isinstance(node, ast.Tuple):
        return ",".join(_ast_type_str(el) for el in node.elts)
    if isinstance(node, ast.List):
        return "[" + ",".join(_ast_type_str(el) for el in node.elts) + "]"
    if isinstance(node, ast.Constant):
        if node.value is Ellipsis:
            return "..."
        return repr(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_ast_type_str(node.left)}|{_ast_type_str(node.right)}"
    if type(node).__name__ == "Ellipsis":
        return "..."
    if type(node).__name__ == "Str":
        return repr(getattr(node, "s", ""))
    return type(node).__name__


def _ast_default_str(node: Optional[ast.AST]) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return repr(node.value)[:64]
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _ast_type_str(node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = "-" if isinstance(node.op, ast.USub) else "+"
        inner = _ast_default_str(node.operand)
        return f"{sign}{inner}" if inner else ast.dump(node)[:64]
    return ast.dump(node)[:96]


def _py_fn_sig(fn_node: ast.AST) -> str:
    args_obj = getattr(fn_node, "args", None)
    if args_obj is None:
        return getattr(fn_node, "name", "")

    def _fmt_arg(a: ast.arg, default_node: Optional[ast.AST] = None) -> str:
        ann = _ast_type_str(a.annotation)
        base = f"{a.arg}:{ann}" if ann else a.arg
        if default_node is not None:
            return f"{base}={_ast_default_str(default_node)}"
        return base

    raw_posonly = list(getattr(args_obj, "posonlyargs", ()))
    raw_reg = list(getattr(args_obj, "args", ()))
    pos_and_reg = raw_posonly + raw_reg
    raw_defaults = list(getattr(args_obj, "defaults", ()))
    pad_none = max(0, len(pos_and_reg) - len(raw_defaults))
    aligned_defaults: List[Optional[ast.AST]] = [None] * pad_none + raw_defaults[-len(pos_and_reg) :] if pos_and_reg else []

    posonly = [_fmt_arg(a, aligned_defaults[idx]) for idx, a in enumerate(raw_posonly)]
    reg = [_fmt_arg(a, aligned_defaults[len(raw_posonly) + idx]) for idx, a in enumerate(raw_reg)]
    parts: List[str] = list(posonly)
    if posonly:
        parts.append("/")
    parts.extend(reg)
    vararg = getattr(args_obj, "vararg", None)
    kwonlyargs = list(getattr(args_obj, "kwonlyargs", ()))
    kw_defaults = list(getattr(args_obj, "kw_defaults", ()))
    if vararg is not None:
        parts.append("*" + _fmt_arg(vararg))
    elif kwonlyargs:
        parts.append("*")
    for idx, a in enumerate(kwonlyargs):
        kw_def = kw_defaults[idx] if idx < len(kw_defaults) else None
        parts.append(_fmt_arg(a, kw_def))
    kwarg = getattr(args_obj, "kwarg", None)
    if kwarg is not None:
        parts.append("**" + _fmt_arg(kwarg))

    prefix = "async def" if isinstance(fn_node, ast.AsyncFunctionDef) else "def"
    ret = _ast_type_str(getattr(fn_node, "returns", None))
    ret_str = f"->{ret}" if ret else ""
    route_tag = _py_route_decorator(fn_node)
    dec_tags = [t for t in (_ast_decorator_str(d) for d in getattr(fn_node, "decorator_list", ())) if t]
    dec_prefix = f"[{','.join(dec_tags)}] " if dec_tags else ""
    return f"{dec_prefix}{prefix} {getattr(fn_node, 'name', '')}({','.join(parts)}){ret_str}{route_tag}"


def _iter_py_import_nodes(body: Sequence[ast.AST], _depth: int = 0) -> Iterator[ast.AST]:
    """Yield top-level Import/ImportFrom nodes plus those inside nested If/Try/With/Match blocks."""
    if _depth >= 4:
        return
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            yield from _iter_py_import_nodes(node.body, _depth + 1)
            cur_else = node.orelse
            while cur_else:
                if len(cur_else) == 1 and isinstance(cur_else[0], ast.If):
                    elif_node = cur_else[0]
                    yield from _iter_py_import_nodes(elif_node.body, _depth + 1)
                    cur_else = elif_node.orelse
                else:
                    yield from _iter_py_import_nodes(cur_else, _depth + 1)
                    break
        elif isinstance(node, ast.Try):
            sub_lists: List[Sequence[ast.AST]] = [node.body, node.orelse, node.finalbody]
            for handler in node.handlers:
                sub_lists.append(handler.body)
            for stmts in sub_lists:
                yield from _iter_py_import_nodes(stmts, _depth + 1)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _iter_py_import_nodes(node.body, _depth + 1)
        elif type(node).__name__ == "Match":
            for case_node in getattr(node, "cases", ()):
                yield from _iter_py_import_nodes(getattr(case_node, "body", ()), _depth + 1)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _strip_hash_comments(src: str) -> str:
    """Blank `#` line comments while preserving single/double/triple-quoted strings and exact line breaks."""
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            if src[i : i + 3] == '"""':
                out.append('"""')
                i += 3
                while i < n:
                    if src[i : i + 3] == '"""':
                        out.append('"""')
                        i += 3
                        break
                    ch = src[i]
                    out.append(ch)
                    i += 1
                    if ch == "\\" and i < n:
                        out.append(src[i])
                        i += 1
            else:
                out.append(c)
                i += 1
                while i < n and src[i] != "\n":
                    ch = src[i]
                    out.append(ch)
                    i += 1
                    if ch == "\\" and i < n and src[i] != "\n":
                        out.append(src[i])
                        i += 1
                        continue
                    if ch == '"':
                        break
        elif c == "#":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_sql_comments(src: str) -> str:
    """Blank `--` and `/* */` comments while preserving `'...'` and `"..."` literals and line breaks."""
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ("'", '"'):
            q = c
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                out.append(ch)
                i += 1
                if ch == q:
                    if i < n and src[i] == q:
                        out.append(src[i])
                        i += 1
                        continue
                    break
                if ch == "\\" and i < n:
                    out.append(src[i])
                    i += 1
        elif c == "-" and i + 1 < n and src[i + 1] == "-":
            out.append("  ")
            i += 2
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n:
                if src[i] == "*" and i + 1 < n and src[i + 1] == "/":
                    out.append("  ")
                    i += 2
                    break
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _skip_string_literal(src: str, i: int, allow_backtick: bool = True, single_quote_char_only: bool = False) -> int:
    n = len(src)
    c = src[i]
    if c == '"' or (allow_backtick and c == "`"):
        i += 1
        while i < n:
            ch = src[i]
            i += 1
            if ch == "\\" and c != "`" and i < n:
                i += 1
                continue
            if ch == c:
                break
        return i
    if c == "'":
        if single_quote_char_only:
            j = i + 1
            limit = min(n, i + 18)
            while j < limit and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "'":
                    return j + 1
                j += 1
            return i + 1
        i += 1
        while i < n and src[i] != "\n":
            ch = src[i]
            i += 1
            if ch == "\\" and i < n and src[i] != "\n":
                i += 1
                continue
            if ch == "'":
                break
        return i
    return i + 1


def _skip_js_regex(src: str, i: int) -> int:
    n = len(src)
    i += 1
    in_class = False
    while i < n and src[i] != "\n":
        ch = src[i]
        i += 1
        if ch == "\\" and i < n and src[i] != "\n":
            i += 1
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            while i < n and src[i].isalpha():
                i += 1
            break
    return i


def _find_matching_delim(
    src: str,
    start: int,
    open_ch: str,
    close_ch: str,
    allow_backtick: bool = True,
    single_quote_char_only: bool = False,
    js_mode: bool = False,
) -> int:
    n = len(src)
    if start < 0 or start >= n or src[start] != open_ch:
        return start
    depth = 1
    i = start + 1
    prev_tok = open_ch
    while i < n and depth > 0:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in ('"', "'") or (allow_backtick and c == "`"):
            nxt_i = _skip_string_literal(src, i, allow_backtick=allow_backtick, single_quote_char_only=single_quote_char_only)
            prev_tok = c if nxt_i > i + 1 or not single_quote_char_only else "'"
            i = nxt_i
            continue
        if js_mode and c == "/":
            if not prev_tok or prev_tok in _JS_REGEX_PREV_CHARS or prev_tok in _JS_REGEX_PREV_KW:
                i = _skip_js_regex(src, i)
                prev_tok = "/"
                continue
            prev_tok = "/"
            i += 1
            continue
        if c == open_ch:
            depth += 1
            prev_tok = c
            i += 1
            continue
        if c == close_ch:
            depth -= 1
            if depth == 0:
                return i
            prev_tok = c
            i += 1
            continue
        if js_mode and (c.isalpha() or c in "_$"):
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            prev_tok = src[i:j]
            i = j
            continue
        if js_mode and c in "+-" and i + 1 < n and src[i + 1] == c:
            prev_tok = c * 2
            i += 2
            continue
        prev_tok = c
        i += 1
    return n - 1


def _extract_js_ts_class_members(body: str, cls_name: str, sigs_out: List[str]) -> None:
    """Extract non-private member signatures from a JS/TS class body."""
    n = len(body)
    i = 0
    stmt_start = 0
    paren_depth = 0
    bracket_depth = 0
    prev_tok = ""

    def _emit_member(raw_hdr: str) -> None:
        hdr = _collapse_ws(raw_hdr)
        if not hdr or hdr.startswith(("private ", "#")):
            return
        if re.match(r"^(?:static\s+)?$", hdr):
            return
        if "(" not in hdr and "=" in hdr:
            hdr = _collapse_ws(hdr.split("=", 1)[0])
        if hdr and not hdr.startswith(("private ", "#")):
            sigs_out.append(f"{cls_name}::{hdr}")

    while i < n:
        c = body[i]
        if c in ('"', "'", "`"):
            i = _skip_string_literal(body, i, allow_backtick=True)
            prev_tok = c
            continue
        if c == "/":
            if not prev_tok or prev_tok in _JS_REGEX_PREV_CHARS or prev_tok in _JS_REGEX_PREV_KW:
                i = _skip_js_regex(body, i)
                prev_tok = "/"
                continue
            prev_tok = "/"
            i += 1
            continue
        if c in "([":
            if c == "(":
                paren_depth += 1
            else:
                bracket_depth += 1
            prev_tok = c
            i += 1
            continue
        if c == ")":
            paren_depth = max(0, paren_depth - 1)
            prev_tok = c
            i += 1
            continue
        if c == "]":
            bracket_depth = max(0, bracket_depth - 1)
            prev_tok = c
            i += 1
            continue
        if c == "{" and paren_depth == 0 and bracket_depth == 0:
            hdr_part = _collapse_ws(body[stmt_start:i])
            close_b = _find_matching_delim(body, i, "{", "}", allow_backtick=True, js_mode=True)
            while hdr_part.endswith((":", "|", "&")):
                hdr_part = _collapse_ws(hdr_part + " " + body[i : close_b + 1])
                j = close_b + 1
                while j < n and body[j] in " \t\r\n":
                    j += 1
                if j < n and body[j] == "{":
                    i = j
                    close_b = _find_matching_delim(body, i, "{", "}", allow_backtick=True, js_mode=True)
                else:
                    break
            _emit_member(hdr_part)
            i = close_b + 1
            stmt_start = i
            prev_tok = "}"
            continue
        if c == ";" and paren_depth == 0 and bracket_depth == 0:
            _emit_member(body[stmt_start:i])
            i += 1
            stmt_start = i
            prev_tok = ";"
            continue
        if c == "\n" and paren_depth == 0 and bracket_depth == 0:
            cur_s = _collapse_ws(body[stmt_start:i])
            if cur_s and not cur_s.endswith(("=", ":", "|", "&", ",", "(", "<")):
                j = i + 1
                while j < n and body[j] in " \t\r\n":
                    j += 1
                if j >= n or body[j] not in ("|", "&", "?", ":", "="):
                    _emit_member(cur_s)
                    stmt_start = i + 1
                    prev_tok = ""
                    i += 1
                    continue
        if c.isalpha() or c in "_$":
            j = i + 1
            while j < n and (body[j].isalnum() or body[j] in "_$"):
                j += 1
            prev_tok = body[i:j]
            i = j
            continue
        if not c.isspace():
            prev_tok = c
        i += 1
    tail = _collapse_ws(body[stmt_start:])
    if tail:
        _emit_member(tail)


def _extract_js_ts_rich_signatures(
    src: str,
    imports_out: List[str],
    sigs_out: List[str],
    precleaned: Optional[str] = None,
) -> None:
    """Extract imports + full JS/TS function/class/interface/type/enum/export signatures."""
    clean = precleaned if precleaned is not None else _strip_js_comments(src)
    for m in _JS_STATIC_IMPORT_RE.finditer(clean):
        spec = m.group(1) or m.group(2)
        if spec:
            imports_out.append(spec)
    reexport_syms: List[Tuple[int, str]] = []
    for m in _JS_REEXPORT_RE.finditer(clean):
        ns_alias, brace_body, spec = m.group(1), m.group(2), m.group(3)
        if spec:
            imports_out.append(spec)
        if ns_alias:
            sigs_out.append(f"export * as {ns_alias}")
        elif brace_body:
            _parse_js_export_brace(brace_body, 1, reexport_syms)
    for m in _JS_LOCAL_EXPORT_RE.finditer(clean):
        _parse_js_export_brace(m.group(1), 1, reexport_syms)
    for m in _JS_CALL_IMPORT_RE.finditer(clean):
        imports_out.append(m.group(1))
    for _, lbl in reexport_syms:
        sigs_out.append(re.sub(r"\s*\[L\d+\]\s*$", "", lbl).strip())

    n = len(clean)
    i = 0
    stmt_start = 0
    paren_depth = 0
    bracket_depth = 0
    prev_tok = ""

    def _emit_stmt_no_brace(raw_stmt: str) -> None:
        stmt = _collapse_ws(raw_stmt)
        if not stmt:
            return
        if re.match(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?type\s+[A-Za-z_$][\w$]*", stmt):
            sigs_out.append(stmt)
        elif re.match(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\b", stmt):
            sigs_out.append(stmt)
        elif re.match(r"^export\s+(?:const|let|var)\b", stmt):
            if "=>" in stmt:
                sigs_out.append(_collapse_ws(stmt.split("=>", 1)[0]) + " =>")
            else:
                sigs_out.append(_collapse_ws(stmt.split("=", 1)[0]))
        elif re.match(r"^export\s+default\b", stmt):
            sigs_out.append(stmt[:120])
        else:
            mc = _JS_CJS_EXPORT_RE.match(stmt)
            if mc:
                sigs_out.append(f"cjs:{mc.group(1)}")

    while i < n:
        c = clean[i]
        if c in ('"', "'", "`"):
            i = _skip_string_literal(clean, i, allow_backtick=True)
            prev_tok = c
            continue
        if c == "/":
            if not prev_tok or prev_tok in _JS_REGEX_PREV_CHARS or prev_tok in _JS_REGEX_PREV_KW:
                i = _skip_js_regex(clean, i)
                prev_tok = "/"
                continue
            prev_tok = "/"
            i += 1
            continue
        if c in "([":
            if c == "(":
                paren_depth += 1
            else:
                bracket_depth += 1
            prev_tok = c
            i += 1
            continue
        if c == ")":
            paren_depth = max(0, paren_depth - 1)
            prev_tok = c
            i += 1
            continue
        if c == "]":
            bracket_depth = max(0, bracket_depth - 1)
            prev_tok = c
            i += 1
            continue
        if c == "{" and paren_depth == 0 and bracket_depth == 0:
            hdr = _collapse_ws(clean[stmt_start:i])
            close_b = _find_matching_delim(clean, i, "{", "}", allow_backtick=True, js_mode=True)
            if re.match(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:interface|enum)\b", hdr):
                sigs_out.append(_collapse_ws(clean[stmt_start : close_b + 1]))
            elif re.match(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?class\b", hdr):
                sigs_out.append(hdr)
                m_cls = re.search(r"\bclass\s+([A-Za-z_$][\w$]*)", hdr)
                cls_name = m_cls.group(1) if m_cls else "class"
                _extract_js_ts_class_members(clean[i + 1 : close_b], cls_name, sigs_out)
            elif re.match(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\b", hdr) or (
                re.match(r"^export\b", hdr) and "=>" in hdr
            ):
                while hdr.endswith((":", "|", "&")):
                    hdr = _collapse_ws(hdr + " " + clean[i : close_b + 1])
                    j = close_b + 1
                    while j < n and clean[j] in " \t\r\n":
                        j += 1
                    if j < n and clean[j] == "{":
                        i = j
                        close_b = _find_matching_delim(clean, i, "{", "}", allow_backtick=True, js_mode=True)
                    else:
                        break
                sigs_out.append(hdr)
            elif re.match(r"^export\s+(?:const|let|var)\b", hdr):
                sigs_out.append(_collapse_ws(hdr.split("=", 1)[0]))
            elif re.match(r"^export\s+default\b", hdr):
                sigs_out.append(hdr)
            i = close_b + 1
            stmt_start = i
            prev_tok = "}"
            continue
        if c == ";" and paren_depth == 0 and bracket_depth == 0:
            _emit_stmt_no_brace(clean[stmt_start:i])
            i += 1
            stmt_start = i
            prev_tok = ";"
            continue
        if c == "\n" and paren_depth == 0 and bracket_depth == 0:
            cur_s = _collapse_ws(clean[stmt_start:i])
            if cur_s and not cur_s.endswith(("=", ":", "|", "&", ",", "(", "<", "=>")):
                j = i + 1
                while j < n and clean[j] in " \t\r\n":
                    j += 1
                if j >= n or clean[j] not in ("|", "&", "?", ":", "="):
                    _emit_stmt_no_brace(cur_s)
                    stmt_start = i + 1
                    prev_tok = ""
                    i += 1
                    continue
        if c.isalpha() or c in "_$":
            j = i + 1
            while j < n and (clean[j].isalnum() or clean[j] in "_$"):
                j += 1
            prev_tok = clean[i:j]
            i = j
            continue
        if not c.isspace():
            prev_tok = c
        i += 1
    _emit_stmt_no_brace(clean[stmt_start:])


def _extract_go_rich_signatures(
    src: str,
    imports_out: List[str],
    sigs_out: List[str],
    precleaned: Optional[str] = None,
) -> None:
    clean = precleaned if precleaned is not None else _strip_c_comments(src, allow_backtick=True)
    go_lines = clean[:-1].split("\n") if clean.endswith("\n") else (clean.split("\n") if clean else [])
    in_imp = False
    for ln in go_lines:
        s = ln.strip()
        if s.startswith("import ("):
            in_imp = True
            continue
        if in_imp:
            if s.startswith(")"):
                in_imp = False
            elif '"' in s:
                m = re.search(r'"([^"]+)"', s)
                if m:
                    imports_out.append(m.group(1))
            continue
        if s.startswith("import "):
            m = re.search(r'"([^"]+)"', s)
            if m:
                imports_out.append(m.group(1))

    n = len(clean)
    i = 0
    stmt_start = 0
    paren_depth = 0
    bracket_depth = 0
    while i < n:
        c = clean[i]
        if c in ('"', "'", "`"):
            i = _skip_string_literal(clean, i, allow_backtick=True)
            continue
        if c == "(":
            if paren_depth == 0 and bracket_depth == 0:
                hdr_p = _collapse_ws(clean[stmt_start:i])
                if hdr_p in ("type", "const", "var"):
                    close_p = _find_matching_delim(clean, i, "(", ")", allow_backtick=True)
                    inner = clean[i + 1 : close_p]
                    if hdr_p == "type":
                        sigs_out.append(_collapse_ws(clean[stmt_start : close_p + 1]))
                    else:
                        for sub_ln in inner.splitlines():
                            sub_s = _collapse_ws(sub_ln)
                            m_ev = re.match(r"^([A-Z]\w*)\b(.*)", sub_s)
                            if m_ev:
                                lhs = _collapse_ws(sub_s.split("=", 1)[0])
                                sigs_out.append(f"{hdr_p} {lhs}")
                    i = close_p + 1
                    stmt_start = i
                    continue
            paren_depth += 1
            i += 1
            continue
        if c == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue
        if c == "[":
            bracket_depth += 1
            i += 1
            continue
        if c == "]":
            bracket_depth = max(0, bracket_depth - 1)
            i += 1
            continue
        if c == "{" and paren_depth == 0 and bracket_depth == 0:
            hdr = _collapse_ws(clean[stmt_start:i])
            close_b = _find_matching_delim(clean, i, "{", "}", allow_backtick=True)
            if hdr.startswith("func"):
                while re.search(r"\b(?:interface|struct)$", hdr):
                    hdr = _collapse_ws(hdr + clean[i : close_b + 1])
                    j = close_b + 1
                    while j < n and clean[j] in " \t":
                        j += 1
                    if j < n and clean[j] == "{":
                        i = j
                        close_b = _find_matching_delim(clean, i, "{", "}", allow_backtick=True)
                    else:
                        break
                sigs_out.append(hdr)
            elif hdr.startswith("type "):
                sigs_out.append(_collapse_ws(clean[stmt_start : close_b + 1]))
            i = close_b + 1
            stmt_start = i
            continue
        if c in "\n;" and paren_depth == 0 and bracket_depth == 0:
            stmt = _collapse_ws(clean[stmt_start:i])
            if stmt.startswith("type "):
                sigs_out.append(stmt)
            elif stmt.startswith(("const ", "var ")):
                kind_kw, rest = stmt.split(None, 1)
                lhs = _collapse_ws(rest.split("=", 1)[0])
                if re.match(r"^[A-Z]\w*\b", lhs):
                    sigs_out.append(f"{kind_kw} {lhs}")
            stmt_start = i + 1
        i += 1
    tail = _collapse_ws(clean[stmt_start:])
    if tail.startswith("type "):
        sigs_out.append(tail)


def _extract_rust_impl_methods(body: str, impl_hdr: str, sigs_out: List[str]) -> None:
    n = len(body)
    i = 0
    stmt_start = 0
    paren_depth = 0
    bracket_depth = 0
    fn_pat = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|unsafe\s+|const\s+|extern\s+(?:\"[^\"]*\"\s+)?)?fn\b")
    while i < n:
        c = body[i]
        if c in ('"', "'"):
            i = _skip_string_literal(body, i, allow_backtick=False, single_quote_char_only=True)
            continue
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth = max(0, paren_depth - 1)
        elif c == "[":
            bracket_depth += 1
        elif c == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif c == "{" and paren_depth == 0 and bracket_depth == 0:
            hdr = _collapse_ws(body[stmt_start:i])
            close_b = _find_matching_delim(body, i, "{", "}", allow_backtick=False, single_quote_char_only=True)
            if fn_pat.match(hdr):
                sigs_out.append(f"{impl_hdr}::{hdr}")
            i = close_b + 1
            stmt_start = i
            continue
        elif c == ";" and paren_depth == 0 and bracket_depth == 0:
            stmt = _collapse_ws(body[stmt_start:i])
            if fn_pat.match(stmt) or re.match(r"^(?:pub(?:\([^)]*\))?\s+)?(?:type|const)\b", stmt):
                sigs_out.append(f"{impl_hdr}::{_collapse_ws(stmt.split('=', 1)[0])}")
            stmt_start = i + 1
        i += 1


def _extract_rust_rich_signatures(
    src: str,
    imports_out: List[str],
    sigs_out: List[str],
    precleaned: Optional[str] = None,
) -> None:
    clean = precleaned if precleaned is not None else _strip_c_comments(src, single_quote_char_only=True)
    rs_lines = clean[:-1].split("\n") if clean.endswith("\n") else (clean.split("\n") if clean else [])
    use_buf: Optional[List[str]] = None
    for ln in rs_lines:
        s = ln.strip()
        if use_buf is not None:
            if s:
                use_buf.append(s)
            if ";" in s:
                joined = " ".join(use_buf)
                imports_out.append(joined[4 : joined.index(";")].strip())
                use_buf = None
            continue
        if s.startswith("use ") or s.startswith("pub use "):
            body = s[4:] if s.startswith("use ") else s[8:]
            if ";" in body:
                imports_out.append(body[: body.index(";")].strip())
            else:
                use_buf = [f"use {body}"]

    n = len(clean)
    i = 0
    stmt_start = 0
    paren_depth = 0
    bracket_depth = 0
    fn_re = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+|const\s+|async\s+|extern\s+(?:\"[^\"]*\"\s+)?)?fn\b")
    type_blk_re = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\b")
    impl_re = re.compile(r"^(?:unsafe\s+)?impl\b")
    while i < n:
        c = clean[i]
        if c in ('"', "'"):
            i = _skip_string_literal(clean, i, allow_backtick=False, single_quote_char_only=True)
            continue
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth = max(0, paren_depth - 1)
        elif c == "[":
            bracket_depth += 1
        elif c == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif c == "{" and paren_depth == 0 and bracket_depth == 0:
            hdr = _collapse_ws(clean[stmt_start:i])
            close_b = _find_matching_delim(clean, i, "{", "}", allow_backtick=False, single_quote_char_only=True)
            if fn_re.match(hdr):
                sigs_out.append(hdr)
            elif type_blk_re.match(hdr):
                sigs_out.append(_collapse_ws(clean[stmt_start : close_b + 1]))
            elif impl_re.match(hdr):
                sigs_out.append(hdr)
                _extract_rust_impl_methods(clean[i + 1 : close_b], hdr, sigs_out)
            elif re.match(r"^(?:pub(?:\([^)]*\))?\s+)?mod\b", hdr):
                sigs_out.append(hdr)
            i = close_b + 1
            stmt_start = i
            continue
        elif c == ";" and paren_depth == 0 and bracket_depth == 0:
            stmt = _collapse_ws(clean[stmt_start:i])
            if fn_re.match(stmt) or type_blk_re.match(stmt) or re.match(
                r"^(?:pub(?:\([^)]*\))?\s+)?(?:type|mod)\b", stmt
            ):
                sigs_out.append(stmt)
            elif re.match(r"^(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\b", stmt):
                sigs_out.append(_collapse_ws(stmt.split("=", 1)[0]))
            stmt_start = i + 1
        i += 1


_JVM_TYPE_HDR_RE = re.compile(
    r"\b(class|interface|enum|record|object|struct|namespace)\s+([A-Za-z_]\w*)"
)
_JVM_SKIP_CTRL_RE = re.compile(r"^(?:if|for|while|switch|catch|try|else|finally|synchronized|static|init)\b")


def _extract_jvm_cs_block(src: str, owner: str, sigs_out: List[str]) -> None:
    n = len(src)
    i = 0
    stmt_start = 0
    paren_depth = 0
    bracket_depth = 0

    def _emit_decl(raw_s: str, has_body: bool) -> None:
        s = _collapse_ws(raw_s)
        if not s or s.startswith(("import ", "using ", "package ")):
            return
        if re.match(r"^private\b", s) or _JVM_SKIP_CTRL_RE.match(s):
            return
        if not has_body and "=" in s and "(" not in s.split("=", 1)[0]:
            s = _collapse_ws(s.split("=", 1)[0])
        elif "=" in s and "=>" not in s:
            lhs = _collapse_ws(s.split("=", 1)[0])
            if "(" in lhs:
                s = lhs
        if "=>" in s:
            s = _collapse_ws(s.split("=>", 1)[0])
        if s and not re.match(r"^private\b", s):
            sigs_out.append(f"{owner}::{s}" if owner else s)

    while i < n:
        c = src[i]
        if c in ('"', "'"):
            i = _skip_string_literal(src, i, allow_backtick=False)
            continue
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth = max(0, paren_depth - 1)
        elif c == "[":
            bracket_depth += 1
        elif c == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif c == "{" and paren_depth == 0 and bracket_depth == 0:
            hdr = _collapse_ws(src[stmt_start:i])
            close_b = _find_matching_delim(src, i, "{", "}", allow_backtick=False)
            m_t = _JVM_TYPE_HDR_RE.search(hdr)
            if m_t:
                t_name = m_t.group(2)
                full_owner = f"{owner}.{t_name}" if owner else t_name
                if m_t.group(1) != "namespace":
                    sigs_out.append(hdr)
                _extract_jvm_cs_block(src[i + 1 : close_b], full_owner, sigs_out)
            else:
                _emit_decl(hdr, True)
            i = close_b + 1
            stmt_start = i
            continue
        elif c == ";" and paren_depth == 0 and bracket_depth == 0:
            _emit_decl(src[stmt_start:i], False)
            stmt_start = i + 1
        elif c == "\n" and paren_depth == 0 and bracket_depth == 0:
            cur_s = _collapse_ws(src[stmt_start:i])
            if cur_s.startswith(("import ", "using ", "package ")):
                stmt_start = i + 1
            elif cur_s and not cur_s.endswith(("=", "=>", ":", ",", "(", "<", "{")):
                if _JVM_TYPE_HDR_RE.search(cur_s) or re.match(
                    r"^(?:(?:public|protected|internal|override|open|abstract|suspend|inline|operator|infix|static|virtual|async)\s+)*fun\b",
                    cur_s,
                ):
                    _emit_decl(cur_s, False)
                    stmt_start = i + 1
        i += 1
    tail = _collapse_ws(src[stmt_start:])
    if tail:
        _emit_decl(tail, False)


def _extract_jvm_cs_rich_signatures(
    src: str,
    imports_out: List[str],
    sigs_out: List[str],
    precleaned: Optional[str] = None,
) -> None:
    clean = precleaned if precleaned is not None else _strip_c_comments(src)
    for ln in clean.splitlines():
        s = ln.strip()
        if s.startswith(("import ", "using ")):
            imports_out.append(s.rstrip(";"))
        elif s.startswith("package "):
            sigs_out.append(s.rstrip(";"))
    _extract_jvm_cs_block(clean, "", sigs_out)


def _strip_cpp_ctor_init(hdr: str) -> str:
    last_paren = hdr.rfind(")")
    if last_paren == -1:
        return hdr
    k = last_paren + 1
    n = len(hdr)
    while k < n:
        if hdr[k] == ":":
            prev_colon = k > 0 and hdr[k - 1] == ":"
            next_colon = k + 1 < n and hdr[k + 1] == ":"
            if not prev_colon and not next_colon:
                return _collapse_ws(hdr[:k])
            k += 2
            continue
        k += 1
    return hdr


def _extract_cpp_block(src: str, owner: str, default_access: str, sigs_out: List[str]) -> None:
    n = len(src)
    i = 0
    stmt_start = 0
    paren_depth = 0
    bracket_depth = 0
    access = default_access

    while i < n:
        c = src[i]
        if c in ('"', "'"):
            i = _skip_string_literal(src, i, allow_backtick=False)
            continue
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth = max(0, paren_depth - 1)
        elif c == "[":
            bracket_depth += 1
        elif c == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif (
            c == ":"
            and paren_depth == 0
            and bracket_depth == 0
            and (i + 1 >= n or src[i + 1] != ":")
            and (i == 0 or src[i - 1] != ":")
        ):
            lbl = _collapse_ws(src[stmt_start:i])
            if lbl in ("public", "protected", "private"):
                access = lbl
                stmt_start = i + 1
                i += 1
                continue
        elif c == "{" and paren_depth == 0 and bracket_depth == 0:
            hdr = _collapse_ws(src[stmt_start:i])
            close_b = _find_matching_delim(src, i, "{", "}", allow_backtick=False)
            m_t = _CPP_TYPE_RE.match(hdr)
            if m_t:
                kind = m_t.group(1)
                t_name = m_t.group(2)
                if kind.startswith("enum"):
                    if access != "private":
                        full_enum = _collapse_ws(src[stmt_start : close_b + 1])
                        sigs_out.append(f"{owner}::{full_enum}" if owner else full_enum)
                elif access != "private":
                    sigs_out.append(f"{owner}::{hdr}" if owner else hdr)
                    child_access = "private" if kind == "class" else "public"
                    full_owner = f"{owner}::{t_name}" if owner else t_name
                    _extract_cpp_block(src[i + 1 : close_b], full_owner, child_access, sigs_out)
            elif hdr and not hdr.startswith("#") and "(" in hdr:
                if access != "private":
                    sig_hdr = _strip_cpp_ctor_init(hdr)
                    sigs_out.append(f"{owner}::{sig_hdr}" if owner else sig_hdr)
            elif hdr.startswith(("typedef enum", "typedef struct", "typedef union", "enum", "struct", "union")):
                if access != "private":
                    sigs_out.append(_collapse_ws(src[stmt_start : close_b + 1]))
            i = close_b + 1
            stmt_start = i
            continue
        elif c == ";" and paren_depth == 0 and bracket_depth == 0:
            stmt = _collapse_ws(src[stmt_start:i])
            if stmt and not stmt.startswith("#") and access != "private":
                if "=" in stmt and not stmt.startswith("using ") and "(" not in stmt.split("=", 1)[0]:
                    stmt = _collapse_ws(stmt.split("=", 1)[0])
                if owner or _CPP_TYPE_RE.match(stmt) or "(" in stmt or stmt.startswith(("extern ", "typedef ", "using ")):
                    sigs_out.append(f"{owner}::{stmt}" if owner else stmt)
            stmt_start = i + 1
        elif c == "\n" and paren_depth == 0 and bracket_depth == 0:
            cur_line = src[stmt_start:i]
            cur_s = _collapse_ws(cur_line)
            if cur_s.startswith("#") and not cur_line.rstrip().endswith("\\"):
                stmt_start = i + 1
        i += 1


def _extract_cpp_rich_signatures(
    src: str,
    imports_out: List[str],
    sigs_out: List[str],
    precleaned: Optional[str] = None,
) -> None:
    clean = precleaned if precleaned is not None else _strip_c_comments(src)
    for ln in clean.splitlines():
        m_inc = _C_INCLUDE_RE.match(ln)
        if m_inc:
            imports_out.append(m_inc.group(1))
    _extract_cpp_block(clean, "", "public", sigs_out)


def _preclean_non_py_source(text: str, ext_l: str) -> Optional[str]:
    if ext_l in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        return _strip_js_comments(text)
    if ext_l == ".go":
        return _strip_c_comments(text, allow_backtick=True)
    if ext_l == ".rs":
        return _strip_c_comments(text, single_quote_char_only=True)
    if ext_l in {".java", ".kt", ".kts", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".proto", ".prisma"}:
        return _strip_c_comments(text)
    if ext_l in {".graphql", ".gql"}:
        return _strip_hash_comments(text)
    if ext_l == ".sql":
        return _strip_sql_comments(text)
    return None


def _extract_rich_non_py_signatures(
    norm_bytes: bytes,
    ext_l: str,
    *,
    predecoded_text: Optional[str] = None,
    precleaned_text: Optional[str] = None,
) -> Optional[Dict[str, List[str]]]:
    text = predecoded_text if predecoded_text is not None else strip_utf8_bom_bytes(norm_bytes).decode("utf-8", errors="replace")
    imports: List[str] = []
    sigs: List[str] = []
    if ext_l in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        _extract_js_ts_rich_signatures(text, imports, sigs, precleaned=precleaned_text)
    elif ext_l in {".vue", ".svelte"}:
        script_chunks = [m.group(1) for m in _SFC_SCRIPT_RE.finditer(text)]
        _extract_js_ts_rich_signatures("\n".join(script_chunks), imports, sigs)
    elif ext_l == ".go":
        _extract_go_rich_signatures(text, imports, sigs, precleaned=precleaned_text)
    elif ext_l == ".rs":
        _extract_rust_rich_signatures(text, imports, sigs, precleaned=precleaned_text)
    elif ext_l in {".java", ".kt", ".kts", ".cs"}:
        _extract_jvm_cs_rich_signatures(text, imports, sigs, precleaned=precleaned_text)
    elif ext_l in {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"}:
        _extract_cpp_rich_signatures(text, imports, sigs, precleaned=precleaned_text)
    elif ext_l == ".proto":
        clean_p = precleaned_text if precleaned_text is not None else _strip_c_comments(text)
        for ln in clean_p.splitlines():
            m_imp = _PROTO_IMPORT_RE.match(ln)
            if m_imp:
                imports.append(m_imp.group(1))
        body_norm = _collapse_ws(clean_p)
        if body_norm:
            sigs.append(body_norm)
    elif ext_l in {".graphql", ".gql"}:
        body_norm = _collapse_ws(precleaned_text if precleaned_text is not None else _strip_hash_comments(text))
        if body_norm:
            sigs.append(body_norm)
    elif ext_l == ".prisma":
        body_norm = _collapse_ws(precleaned_text if precleaned_text is not None else _strip_c_comments(text))
        if body_norm:
            sigs.append(body_norm)
    elif ext_l == ".sql":
        body_norm = _collapse_ws(precleaned_text if precleaned_text is not None else _strip_sql_comments(text))
        if body_norm:
            sigs.append(body_norm)
    else:
        return None
    return {"i": sorted(set(imports)), "s": sorted(set(sigs))}


def _extract_py_outline_from_tree(
    tree: ast.Module,
    imports: List[str],
    symbols: List[str],
    py_from_members: List[str],
) -> None:
    for node in _iter_py_import_nodes(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            dots = "." * node.level
            if node.module:
                imports.append(f"{dots}{node.module}")
                for alias in node.names:
                    if alias.name and alias.name != "*":
                        py_from_members.append(f"{dots}{node.module}.{alias.name}")
            else:
                for alias in node.names:
                    imports.append(f"{dots}{alias.name}")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args: List[str] = [a.arg for a in getattr(node.args, "posonlyargs", ())]
            args.extend(a.arg for a in node.args.args)
            if node.args.vararg is not None:
                args.append(f"*{node.args.vararg.arg}")
            elif getattr(node.args, "kwonlyargs", ()):
                args.append("*")
            args.extend(a.arg for a in getattr(node.args, "kwonlyargs", ()))
            if node.args.kwarg is not None:
                args.append(f"**{node.args.kwarg.arg}")
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            route_tag = _py_route_decorator(node)
            symbols.append(f"{prefix} {node.name}({', '.join(args[:6])}){route_tag} [L{node.lineno}]")
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("__")
            ][:8]
            m_str = f" methods=[{', '.join(methods)}]" if methods else ""
            symbols.append(f"class {node.name}{m_str} [L{node.lineno}]")


def _extract_outline_from_bytes(
    norm_bytes: bytes,
    ext: str,
    rel: str,
    max_items: int = 30,
    preparsed_py_tree: Optional[ast.Module] = None,
    max_imports: Optional[int] = None,
    predecoded_text: Optional[str] = None,
    precleaned_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure byte-buffer implementation of `extract_file_outline` (expects LF-normalized bytes)."""
    clean_bytes = strip_utf8_bom_bytes(norm_bytes)
    line_count = 0 if not clean_bytes else clean_bytes.count(b"\n") + (0 if clean_bytes.endswith(b"\n") else 1)
    text = predecoded_text if predecoded_text is not None else clean_bytes.decode("utf-8", errors="replace")
    lines = text[:-1].split("\n") if text.endswith("\n") else (text.split("\n") if text else [])
    ext = ext.lower()

    imports: List[str] = []
    symbols: List[str] = []
    py_from_members: List[str] = []

    ts_res = _ts_accel.extract_outline_with_treesitter(clean_bytes, ext) if ext != ".py" else None
    if ts_res is not None:
        imports.extend(ts_res[0])
        symbols.extend(ts_res[1])
    elif ext == ".py":
        try:
            tree = preparsed_py_tree if preparsed_py_tree is not None else ast.parse(text)
            _extract_py_outline_from_tree(tree, imports, symbols, py_from_members)
        except SyntaxError:
            pass
    elif ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        _extract_js_ts_outline(text, 0, imports, symbols, precleaned=precleaned_text)
    elif ext in {".vue", ".svelte"}:
        for m in _SFC_SCRIPT_RE.finditer(text):
            offset = text.count("\n", 0, m.start(1))
            _extract_js_ts_outline(m.group(1), offset, imports, symbols)
    elif ext == ".go":
        clean_go = precleaned_text if precleaned_text is not None else _strip_c_comments(text, allow_backtick=True)
        go_lines = clean_go[:-1].split("\n") if clean_go.endswith("\n") else (clean_go.split("\n") if clean_go else [])
        in_imp = False
        for idx, ln in enumerate(go_lines, start=1):
            s = ln.strip()
            if s.startswith("import ("):
                in_imp = True
                continue
            if in_imp:
                if s.startswith(")"):
                    in_imp = False
                elif '"' in s:
                    m = re.search(r'"([^"]+)"', s)
                    if m:
                        imports.append(m.group(1))
                continue
            if s.startswith("import "):
                m = re.search(r'"([^"]+)"', s)
                if m:
                    imports.append(m.group(1))
                continue
            ms = _GO_SYM_RE.match(ln)
            if ms:
                symbols.append(f"{ms.group(1).strip()} [L{idx}]")
    elif ext == ".rs":
        clean_rs = precleaned_text if precleaned_text is not None else _strip_c_comments(text, single_quote_char_only=True)
        rs_lines = clean_rs[:-1].split("\n") if clean_rs.endswith("\n") else (clean_rs.split("\n") if clean_rs else [])
        use_buf: Optional[List[str]] = None
        for idx, ln in enumerate(rs_lines, start=1):
            s = ln.strip()
            if use_buf is not None:
                if s:
                    use_buf.append(s)
                if ";" in s:
                    joined = " ".join(use_buf)
                    imports.append(joined[4 : joined.index(";")].strip())
                    use_buf = None
                continue
            if s.startswith("use ") or s.startswith("pub use "):
                body = s[4:] if s.startswith("use ") else s[8:]
                if ";" in body:
                    imports.append(body[: body.index(";")].strip())
                else:
                    use_buf = [f"use {body}"]
                continue
            ms = _RS_SYM_RE.match(ln)
            if ms:
                symbols.append(f"{ms.group(1)} {ms.group(2)} [L{idx}]")
    elif ext in {".java", ".kt", ".kts", ".cs"}:
        clean_jvm = precleaned_text if precleaned_text is not None else _strip_c_comments(text)
        jvm_lines = clean_jvm[:-1].split("\n") if clean_jvm.endswith("\n") else (clean_jvm.split("\n") if clean_jvm else [])
        for idx, ln in enumerate(jvm_lines, start=1):
            s = ln.strip()
            if s.startswith(("import ", "using ")):
                imports.append(s.rstrip(";"))
                continue
            ms = _JVM_CS_SYM_RE.match(ln)
            if ms:
                symbols.append(f"{ms.group(1)} {ms.group(2)} [L{idx}]")
    elif ext == ".proto":
        clean_p = precleaned_text if precleaned_text is not None else _strip_c_comments(text)
        p_lines = clean_p[:-1].split("\n") if clean_p.endswith("\n") else (clean_p.split("\n") if clean_p else [])
        for idx, ln in enumerate(p_lines, start=1):
            m_imp = _PROTO_IMPORT_RE.match(ln)
            if m_imp:
                imports.append(m_imp.group(1))
                continue
            m_item = _PROTO_ITEM_RE.match(ln)
            if m_item:
                symbols.append(f"{m_item.group(1)} {m_item.group(2)} [L{idx}]")
                continue
            m_rpc = _PROTO_RPC_RE.match(ln)
            if m_rpc:
                symbols.append(f"rpc {m_rpc.group(1)} [L{idx}]")
    elif ext in {".graphql", ".gql"}:
        clean_g = precleaned_text if precleaned_text is not None else _strip_hash_comments(text)
        g_lines = clean_g[:-1].split("\n") if clean_g.endswith("\n") else (clean_g.split("\n") if clean_g else [])
        for idx, ln in enumerate(g_lines, start=1):
            m_g = _GRAPHQL_ITEM_RE.match(ln)
            if m_g:
                symbols.append(f"{m_g.group(1)} {m_g.group(2)} [L{idx}]")
    elif ext == ".prisma":
        clean_pr = precleaned_text if precleaned_text is not None else _strip_c_comments(text)
        pr_lines = clean_pr[:-1].split("\n") if clean_pr.endswith("\n") else (clean_pr.split("\n") if clean_pr else [])
        for idx, ln in enumerate(pr_lines, start=1):
            m_pr = _PRISMA_ITEM_RE.match(ln)
            if m_pr:
                symbols.append(f"{m_pr.group(1)} {m_pr.group(2)} [L{idx}]")
    elif ext == ".sql":
        clean_sql = precleaned_text if precleaned_text is not None else _strip_sql_comments(text)
        sql_lines = clean_sql[:-1].split("\n") if clean_sql.endswith("\n") else (clean_sql.split("\n") if clean_sql else [])
        for idx, ln in enumerate(sql_lines, start=1):
            m_sql = _SQL_CREATE_RE.match(ln)
            if m_sql:
                symbols.append(f"{m_sql.group(1).lower()} {m_sql.group(2)} [L{idx}]")
    elif ext in {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"}:
        clean_cpp = precleaned_text if precleaned_text is not None else _strip_c_comments(text)
        cpp_lines = clean_cpp[:-1].split("\n") if clean_cpp.endswith("\n") else (clean_cpp.split("\n") if clean_cpp else [])
        brace_depth = 0
        for idx, ln in enumerate(cpp_lines, start=1):
            m_inc = _C_INCLUDE_RE.match(ln)
            if m_inc:
                imports.append(m_inc.group(1))
            else:
                m_t = _CPP_TYPE_RE.match(ln)
                if m_t:
                    kind = "enum" if m_t.group(1).startswith("enum") else m_t.group(1)
                    symbols.append(f"{kind} {m_t.group(2)} [L{idx}]")
                elif brace_depth == 0:
                    m_fn = _CPP_FN_RE.match(ln)
                    if m_fn:
                        fn_name = m_fn.group(1)
                        if fn_name not in _CPP_SKIP_KEYWORDS and not ln.lstrip().startswith("#"):
                            symbols.append(f"fn {fn_name} [L{idx}]")
            brace_depth = max(0, brace_depth + ln.count("{") - ln.count("}"))
    else:
        for idx, ln in enumerate(lines, start=1):
            s = ln.strip()
            if s.startswith("#include "):
                imports.append(s[9:].strip())
            elif ext == ".md" and s.startswith(("# ", "## ")):
                symbols.append(f"{s} [L{idx}]")

    imp_limit = max_items if max_imports is None else max_imports
    dedup_all_imp = list(dict.fromkeys(imports))
    dedup_imp = dedup_all_imp if imp_limit <= 0 else dedup_all_imp[:imp_limit]
    dedup_sym = list(dict.fromkeys(symbols))[:max_items]

    out_obj: Dict[str, Any] = {
        "path": rel,
        "lines": line_count,
        "size_tier": compute_size_tier(line_count),
        "imports": dedup_imp,
        "symbols": dedup_sym,
    }
    if py_from_members:
        dedup_members = list(dict.fromkeys(py_from_members))
        out_obj["py_from_members"] = dedup_members if imp_limit <= 0 else dedup_members[: max(max_items, imp_limit) * 4]
    return out_obj


def _py_all_sig(value: Optional[ast.AST]) -> str:
    if value is None:
        return "__all__:None"
    try:
        val = ast.literal_eval(value)
        return "__all__:" + json.dumps(val, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError):
        return "__all__:" + ast.dump(value)


def _compute_py_sig_hash_from_tree(tree: ast.Module) -> str:
    py_imps: List[str] = []
    py_sigs: List[str] = []
    for node in _iter_py_import_nodes(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                py_imps.append(f"imp:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            dots = "." * node.level
            mod = node.module or ""
            names = ",".join(sorted(a.name for a in node.names))
            py_imps.append(f"from:{dots}{mod}:{names}")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            py_sigs.append(_py_fn_sig(node))
        elif isinstance(node, ast.ClassDef):
            bases = ",".join(_ast_type_str(b) for b in node.bases)
            cls_decs = [t for t in (_ast_decorator_str(d) for d in getattr(node, "decorator_list", ())) if t]
            cls_dec_prefix = f"[{','.join(cls_decs)}] " if cls_decs else ""
            members = [
                _py_fn_sig(n)
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (not n.name.startswith("__") or n.name in _PY_CONTRACT_DUNDERS)
            ]
            for n in node.body:
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and not n.target.id.startswith("_"):
                    members.append(f"{n.target.id}:{_ast_type_str(n.annotation)}")
            members.sort()
            py_sigs.append(f"{cls_dec_prefix}class {node.name}({bases})[{';'.join(members)}]")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "__all__":
                        py_sigs.append(_py_all_sig(node.value))
                    elif not t.id.startswith("_") and any(c.isupper() for c in t.id) and t.id == t.id.upper():
                        py_sigs.append(f"const:{t.id}={_ast_default_str(node.value)}")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                ann = _ast_type_str(node.annotation)
                if node.target.id == "__all__":
                    py_sigs.append(f"__all__[{ann}]:{_py_all_sig(node.value)}")
                elif not node.target.id.startswith("_"):
                    val_part = f"={_ast_default_str(node.value)}" if node.value is not None else ""
                    py_sigs.append(f"ann:{node.target.id}:{ann}{val_part}")
        elif type(node).__name__ == "TypeAlias":
            ta_name = _ast_type_str(getattr(node, "name", None))
            ta_val = _ast_type_str(getattr(node, "value", None))
            py_sigs.append(f"typealias:{ta_name}={ta_val}")
    payload = {"i": sorted(set(py_imps)), "s": sorted(py_sigs)}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def extract_outline_and_sig_hash_from_bytes(
    norm_bytes: bytes,
    ext: str,
    rel: str = "",
    max_items: int = 30,
    max_imports: Optional[int] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Extract outline and `sig_hash` together (reusing one decode and comment-strip/AST pass per file)."""
    ext_l = ext.lower()
    norm_bytes = strip_utf8_bom_bytes(norm_bytes)
    if b"\r\n" in norm_bytes:
        norm_bytes = norm_bytes.replace(b"\r\n", b"\n")
    text = norm_bytes.decode("utf-8", errors="replace")
    tree: Optional[ast.Module] = None
    sig_hash: Optional[str] = None
    precleaned: Optional[str] = None
    if ext_l == ".py":
        try:
            tree = ast.parse(text)
            sig_hash = _compute_py_sig_hash_from_tree(tree)
        except SyntaxError:
            tree = None
            sig_hash = None
    elif ext_l in SIG_HASH_EXTS:
        precleaned = _preclean_non_py_source(text, ext_l)
        rich_payload = _extract_rich_non_py_signatures(
            norm_bytes,
            ext_l,
            predecoded_text=text,
            precleaned_text=precleaned,
        )
        if rich_payload is not None:
            encoded = json.dumps(rich_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            sig_hash = hashlib.sha256(encoded).hexdigest()[:16]
        else:
            sig_hash = compute_signature_hash_from_bytes(norm_bytes, ext_l, rel or f"file{ext_l}")
    out = _extract_outline_from_bytes(
        norm_bytes,
        ext_l,
        rel,
        max_items=max_items,
        preparsed_py_tree=tree,
        max_imports=max_imports,
        predecoded_text=text,
        precleaned_text=precleaned,
    )
    return out, sig_hash


def compute_signature_hash_from_bytes(norm_bytes: bytes, ext: str, rel: str = "") -> Optional[str]:
    """Compute a 16-hex-char fingerprint of a file's public interface & imports."""
    ext_l = ext.lower()
    if ext_l not in SIG_HASH_EXTS:
        return None
    norm_bytes = strip_utf8_bom_bytes(norm_bytes)
    if b"\r\n" in norm_bytes:
        norm_bytes = norm_bytes.replace(b"\r\n", b"\n")

    if ext_l == ".py":
        text = norm_bytes.decode("utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
        return _compute_py_sig_hash_from_tree(tree)

    rich_payload = _extract_rich_non_py_signatures(norm_bytes, ext_l)
    if rich_payload is not None:
        encoded = json.dumps(rich_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    out = _extract_outline_from_bytes(norm_bytes, ext_l, rel or f"file{ext_l}", max_items=200, max_imports=0)
    norm_syms = sorted(re.sub(r"\s*\[L\d+\]\s*$", "", sym).strip() for sym in out["symbols"])
    payload = {"i": sorted(set(out["imports"])), "s": norm_syms}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def compute_signature_hash(full_path: Path, rel: str = "") -> Optional[str]:
    ext_l = full_path.suffix.lower()
    if ext_l not in SIG_HASH_EXTS:
        return None
    try:
        raw = full_path.read_bytes()
    except OSError:
        return None
    norm = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    return compute_signature_hash_from_bytes(norm, ext_l, rel or full_path.name)


def extract_file_outline(
    full_path: Path,
    rel: str,
    max_items: int = 30,
    max_imports: Optional[int] = None,
) -> Dict[str, Any]:
    """Lightweight static outline extractor (imports + top-level symbols/signatures)."""
    raw_bytes = full_path.read_bytes()
    norm_bytes = raw_bytes.replace(b"\r\n", b"\n") if b"\r\n" in raw_bytes else raw_bytes
    return _extract_outline_from_bytes(
        norm_bytes,
        full_path.suffix.lower(),
        rel,
        max_items=max_items,
        max_imports=max_imports,
    )


def _clean_symbol_names(raw_symbols: Sequence[str], limit: int = 4) -> List[str]:
    names: List[str] = []
    for item in raw_symbols:
        core = re.sub(r"\s*\[L\d+\]\s*$", "", item).strip()
        core = re.sub(r"\s*\[@[^\]]+\]\s*$", "", core).strip()
        m = _CLEAN_SYM_RE.match(core)
        if m:
            sym = m.group(1)
            if not sym.startswith("_") and sym not in names:
                names.append(sym)
        if len(names) >= limit:
            break
    return names


def _norm_join_posix(base_dir: str, rel_spec: str) -> str:
    combined = f"{base_dir}/{rel_spec}" if base_dir else rel_spec
    stack: List[str] = []
    for seg in combined.replace("\\", "/").split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if stack:
                stack.pop()
        else:
            stack.append(seg)
    return "/".join(stack)


def _load_tsconfig_file(root: Path, cfg_path: Path, seen: Set[Path], depth: int = 0) -> Tuple[Optional[str], Dict[str, List[str]]]:
    if depth > 5 or not cfg_path.is_file():
        return None, {}
    try:
        resolved = cfg_path.resolve()
    except OSError:
        return None, {}
    if resolved in seen:
        return None, {}
    seen.add(resolved)

    try:
        raw = cfg_path.read_text(encoding="utf-8-sig", errors="replace")
        clean = re.sub(r",\s*([}\]])", r"\1", _strip_js_comments(raw))
        data = json.loads(clean)
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}

    base_url: Optional[str] = None
    aliases: Dict[str, List[str]] = {}
    ext_val = data.get("extends")
    if isinstance(ext_val, str) and ext_val.strip():
        ext_str = ext_val.strip()
        cand = cfg_path.parent / ext_str
        if not cand.is_file() and not ext_str.endswith(".json"):
            cand_json = cfg_path.parent / f"{ext_str}.json"
            if cand_json.is_file():
                cand = cand_json
        if cand.is_file():
            p_base, p_aliases = _load_tsconfig_file(root, cand, seen, depth + 1)
            base_url = p_base
            aliases.update(p_aliases)

    co = data.get("compilerOptions", {})
    if isinstance(co, dict):
        try:
            rel_cfg_dir = cfg_path.parent.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel_cfg_dir = ""
        if rel_cfg_dir == ".":
            rel_cfg_dir = ""
        if "baseUrl" in co and isinstance(co.get("baseUrl"), str):
            raw_b = normalize_relpath(str(co["baseUrl"]))
            base_url = rel_cfg_dir if raw_b in ("", ".") else _norm_join_posix(rel_cfg_dir, raw_b)
        paths_obj = co.get("paths", {})
        if isinstance(paths_obj, dict):
            added_aliases = False
            for pat, targets in paths_obj.items():
                if isinstance(pat, str) and isinstance(targets, list):
                    str_targets = [normalize_relpath(str(t)) for t in targets if isinstance(t, str)]
                    if str_targets:
                        aliases[pat] = str_targets
                        added_aliases = True
            if added_aliases and "baseUrl" not in co and base_url is None:
                base_url = rel_cfg_dir
    return base_url, aliases


def _load_tsconfig_aliases(root: Path, cfg_dir: Optional[Path] = None) -> Tuple[str, List[Tuple[str, List[str]]]]:
    """Parse `baseUrl` and `paths` aliases (following relative `extends`) from `tsconfig.json` / `jsconfig.json`."""
    search_dir = cfg_dir if cfg_dir is not None else root
    for cfg_name in ("tsconfig.json", "jsconfig.json"):
        cfg_path = search_dir / cfg_name
        if not cfg_path.is_file():
            continue
        base_url, aliases_map = _load_tsconfig_file(root, cfg_path, set())
        if base_url is not None or aliases_map:
            return base_url or "", list(aliases_map.items())
    return "", []


def _resolve_tsconfig_for_dir(
    root: Path,
    cur_dir: str,
    cache: Dict[str, Tuple[str, List[Tuple[str, List[str]]]]],
) -> Tuple[str, List[Tuple[str, List[str]]]]:
    """Resolve nearest enclosing `tsconfig.json` / `jsconfig.json` for a monorepo directory."""
    if cur_dir in cache:
        return cache[cur_dir]
    parts = [p for p in cur_dir.split("/") if p] if cur_dir else []
    for cut in range(len(parts), -1, -1):
        sub_dir = "/".join(parts[:cut])
        if sub_dir in cache:
            res = cache[sub_dir]
            cache[cur_dir] = res
            return res
        cand_dir = (root / sub_dir) if sub_dir else root
        if (cand_dir / "tsconfig.json").is_file() or (cand_dir / "jsconfig.json").is_file():
            res = _load_tsconfig_aliases(root, cfg_dir=cand_dir)
            cache[sub_dir] = res
            cache[cur_dir] = res
            return res
    res = ("", [])
    cache[cur_dir] = res
    return res


def _expand_rust_use_tree(spec: str) -> List[str]:
    """Expand nested Rust `use` brace expressions like `crate::{auth, order::repo}` into flat paths."""
    s = spec.strip().rstrip(";").strip()
    if not s:
        return []
    if "{" not in s:
        head = re.split(r"\s+as\s+", s, maxsplit=1)[0].strip().rstrip(":")
        return [head] if head else []
    lbrace = s.find("{")
    rbrace = s.rfind("}")
    if rbrace <= lbrace:
        head = s[:lbrace].strip().rstrip(":")
        return [head] if head else []
    prefix = s[:lbrace].strip().rstrip(":")
    inner = s[lbrace + 1 : rbrace].strip()

    parts: List[str] = []
    cur: List[str] = []
    depth = 0
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            item = "".join(cur).strip()
            if item:
                parts.append(item)
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)

    expanded: List[str] = []
    for part in parts:
        for sub in _expand_rust_use_tree(part):
            sub_clean = sub.strip()
            if sub_clean in ("", "self", "*"):
                if prefix:
                    expanded.append(prefix)
            elif prefix:
                expanded.append(f"{prefix}::{sub_clean}")
            else:
                expanded.append(sub_clean)
    return expanded


def _load_go_module_prefix(root: Path, mod_dir: Optional[Path] = None) -> str:
    gomod = (mod_dir if mod_dir is not None else root) / "go.mod"
    if not gomod.is_file():
        return ""
    try:
        for ln in gomod.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*module\s+(\S+)", ln)
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return ""


def _resolve_gomod_for_dir(
    root: Path,
    cur_dir: str,
    cache: Dict[str, Tuple[str, str]],
) -> Tuple[str, str]:
    """Return `(mod_rel_dir, module_prefix)` for the nearest enclosing `go.mod`."""
    if cur_dir in cache:
        return cache[cur_dir]
    parts = [p for p in cur_dir.split("/") if p] if cur_dir else []
    for cut in range(len(parts), -1, -1):
        sub_dir = "/".join(parts[:cut])
        if sub_dir in cache:
            res = cache[sub_dir]
            cache[cur_dir] = res
            return res
        cand_dir = (root / sub_dir) if sub_dir else root
        if (cand_dir / "go.mod").is_file():
            mod_pfx = _load_go_module_prefix(root, mod_dir=cand_dir)
            if mod_pfx:
                res = (sub_dir, mod_pfx)
                cache[sub_dir] = res
                cache[cur_dir] = res
                return res
    res = ("", "")
    cache[cur_dir] = res
    return res


def resolve_go_package_imports(
    root: Path,
    outlines: Dict[str, Dict[str, Any]],
    discovered_files: Sequence[str],
) -> Tuple[Dict[str, Set[str]], Dict[str, List[str]]]:
    """
    Resolve Go package directory imports per `.go` file and map each Go package directory to its
    non-`_test.go` files using the nearest enclosing `go.mod`.
    """
    go_files_by_dir: Dict[str, List[str]] = {}
    for f_rel in sorted(discovered_files):
        if f_rel.endswith(".go") and not f_rel.endswith("_test.go"):
            parent = f_rel.rsplit("/", 1)[0] if "/" in f_rel else ""
            go_files_by_dir.setdefault(parent, []).append(f_rel)

    go_cache: Dict[str, Tuple[str, str]] = {}
    go_imported_dirs_by_file: Dict[str, Set[str]] = {}
    for rel in discovered_files:
        if not rel.endswith(".go"):
            continue
        out = outlines.get(rel)
        if not isinstance(out, dict):
            continue
        cur_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        mod_root, go_mod = _resolve_gomod_for_dir(root, cur_dir, go_cache)
        if not go_mod:
            continue
        imp_dirs: Set[str] = set()
        for spec in out.get("imports", ()):
            if spec == go_mod or spec.startswith(go_mod + "/"):
                sub_dir = spec[len(go_mod) :].lstrip("/")
                full_dir = f"{mod_root}/{sub_dir}".strip("/") if mod_root else sub_dir
                imp_dirs.add(full_dir)
        if imp_dirs:
            go_imported_dirs_by_file[rel] = imp_dirs
    return go_imported_dirs_by_file, go_files_by_dir


def _probe_js_path(cand_stem: str, known_set: Set[str]) -> Optional[str]:
    stems = [cand_stem]
    if cand_stem.endswith((".js", ".jsx", ".mjs")):
        base_no_ext = cand_stem.rsplit(".", 1)[0]
        stems.append(base_no_ext)
    for stem in stems:
        for sfx in _JS_RESOLVE_EXTS:
            probe = f"{stem}{sfx}"
            if probe in known_set:
                return probe
    return None


def _static_ecosystem(rel: str) -> Optional[str]:
    ext = Path(rel).suffix.lower()
    if ext == ".py":
        return "py"
    if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"}:
        return "js"
    if ext == ".go":
        return "go"
    if ext == ".rs":
        return "rs"
    if ext in {".java", ".kt", ".kts"}:
        return "jvm"
    if ext == ".cs":
        return "cs"
    if ext == ".proto":
        return "proto"
    if ext in {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"}:
        return "cpp"
    return None


_JVM_EXTS: Set[str] = {".java", ".kt", ".kts"}
_CS_FRAMEWORK_PREFIXES: Set[str] = {
    "system",
    "microsoft",
    "windows",
    "netstandard",
    "xunit",
    "nunit",
    "moq",
    "fluentassertions",
    "serilog",
    "newtonsoft",
}


def _is_jvm_cs_test_rel(rel: str) -> bool:
    parts = rel.lower().split("/")
    if any(seg in {"test", "tests", "__tests__", "spec", "specs"} for seg in parts[:-1]):
        return True
    bname = parts[-1]
    stem = bname.rsplit(".", 1)[0]
    return stem.endswith(("test", "tests", "spec", "specs"))


def _build_jvm_cs_indexes(
    discovered_files: Sequence[str],
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, List[str]], Dict[str, List[str]]]:
    jvm_cls_map: Dict[str, List[str]] = {}
    jvm_pkg_map: Dict[str, List[str]] = {}
    cs_cls_map: Dict[str, List[str]] = {}
    cs_ns_map: Dict[str, List[str]] = {}

    for f_rel in sorted(discovered_files):
        ext = Path(f_rel).suffix.lower()
        if ext in _JVM_EXTS:
            no_ext = f_rel[: -len(ext)]
            parts = no_ext.split("/")
            for i in range(len(parts)):
                suf = "/".join(parts[i:])
                jvm_cls_map.setdefault(suf, []).append(f_rel)
            dir_parts = parts[:-1]
            for i in range(len(dir_parts)):
                dsuf = "/".join(dir_parts[i:])
                jvm_pkg_map.setdefault(dsuf, []).append(f_rel)
        elif ext == ".cs":
            no_ext_l = f_rel[:-3].lower()
            parts = no_ext_l.split("/")
            for i in range(len(parts)):
                suf = "/".join(parts[i:])
                cs_cls_map.setdefault(suf, []).append(f_rel)
            dir_parts = parts[:-1]
            for i in range(len(dir_parts)):
                dsuf = "/".join(dir_parts[i:])
                cs_ns_map.setdefault(dsuf, []).append(f_rel)
    return jvm_cls_map, jvm_pkg_map, cs_cls_map, cs_ns_map


def _filter_jvm_cs_cands(cands: Sequence[str], src_rel: str, src_is_test: bool) -> List[str]:
    filtered = [c for c in cands if c != src_rel]
    if not src_is_test:
        non_test = [c for c in filtered if not _is_jvm_cs_test_rel(c)]
        if non_test:
            return non_test
    return filtered


def _resolve_jvm_imports_for_file(
    src_rel: str,
    imports: Sequence[str],
    jvm_cls_map: Mapping[str, List[str]],
    jvm_pkg_map: Mapping[str, List[str]],
) -> Tuple[List[str], List[List[str]]]:
    src_is_test = _is_jvm_cs_test_rel(src_rel)
    exact_hits: List[str] = []
    group_hits: List[List[str]] = []

    for raw_imp in imports:
        s = raw_imp.strip().rstrip(";").strip()
        is_static = False
        if s.startswith("import static "):
            s = s[14:].strip()
            is_static = True
        elif s.startswith("import "):
            s = s[7:].strip()
        if " as " in s:
            s = s.split(" as ", 1)[0].strip()
        if not s:
            continue

        if s.endswith(".*"):
            pkg_slash = s[:-2].replace(".", "/").strip("/")
            if pkg_slash:
                cands = _filter_jvm_cs_cands(jvm_pkg_map.get(pkg_slash, ()), src_rel, src_is_test)[:5]
                if cands:
                    group_hits.append(cands)
            continue

        cls_slash = s.replace(".", "/").strip("/")
        if not cls_slash:
            continue
        direct = _filter_jvm_cs_cands(jvm_cls_map.get(cls_slash, ()), src_rel, src_is_test)
        if direct and ("/" in cls_slash or "/" not in direct[0]):
            exact_hits.extend(direct)
            continue
        if "/" in cls_slash:
            parent_slash = cls_slash.rsplit("/", 1)[0]
            parent_cls = _filter_jvm_cs_cands(jvm_cls_map.get(parent_slash, ()), src_rel, src_is_test)
            if parent_cls:
                exact_hits.extend(parent_cls)
                continue
            if not is_static:
                kt_pkg = [
                    c
                    for c in _filter_jvm_cs_cands(jvm_pkg_map.get(parent_slash, ()), src_rel, src_is_test)
                    if c.lower().endswith((".kt", ".kts"))
                ][:5]
                if kt_pkg:
                    group_hits.append(kt_pkg)

    return exact_hits, group_hits


def _resolve_cs_imports_for_file(
    src_rel: str,
    imports: Sequence[str],
    cs_cls_map: Mapping[str, List[str]],
    cs_ns_map: Mapping[str, List[str]],
) -> Tuple[List[str], List[List[str]]]:
    src_is_test = _is_jvm_cs_test_rel(src_rel)
    exact_hits: List[str] = []
    group_hits: List[List[str]] = []

    for raw_imp in imports:
        s = raw_imp.strip().rstrip(";").strip()
        is_static_or_alias = False
        if s.startswith("using static "):
            s = s[13:].strip()
            is_static_or_alias = True
        elif s.startswith("using "):
            s = s[6:].strip()
        if "=" in s:
            s = s.split("=", 1)[1].strip()
            is_static_or_alias = True
        if s.startswith("global::"):
            s = s[8:]
        if not s:
            continue

        spec_slash = s.replace(".", "/").strip("/").lower()
        if not spec_slash:
            continue
        probes = [spec_slash]
        if "/" in spec_slash:
            first_seg, rest = spec_slash.split("/", 1)
            if first_seg not in _CS_FRAMEWORK_PREFIXES and rest:
                probes.append(rest)

        matched = False
        for pr in probes:
            cls_cands = _filter_jvm_cs_cands(cs_cls_map.get(pr, ()), src_rel, src_is_test)
            if cls_cands:
                exact_hits.extend(cls_cands)
                matched = True
                break
            if is_static_or_alias and "/" in pr:
                parent_pr = pr.rsplit("/", 1)[0]
                p_cands = _filter_jvm_cs_cands(cs_cls_map.get(parent_pr, ()), src_rel, src_is_test)
                if p_cands:
                    exact_hits.extend(p_cands)
                    matched = True
                    break
        if matched or is_static_or_alias:
            continue

        for pr in probes:
            ns_cands = _filter_jvm_cs_cands(cs_ns_map.get(pr, ()), src_rel, src_is_test)[:5]
            if ns_cands:
                group_hits.append(ns_cands)
                break

    return exact_hits, group_hits


def resolve_jvm_cs_package_imports(
    outlines: Mapping[str, Mapping[str, Any]],
    discovered_files: Sequence[str],
) -> Dict[str, Tuple[List[str], List[List[str]]]]:
    """Resolve JVM (`import`) and C# (`using`) imports into `(exact_files, package_or_namespace_groups)`."""
    jvm_cls_map, jvm_pkg_map, cs_cls_map, cs_ns_map = _build_jvm_cs_indexes(discovered_files)
    result: Dict[str, Tuple[List[str], List[List[str]]]] = {}
    for rel in discovered_files:
        ext = Path(rel).suffix.lower()
        if ext not in _JVM_EXTS and ext != ".cs":
            continue
        out = outlines.get(rel)
        if not isinstance(out, Mapping):
            continue
        imps = out.get("imports", ())
        if ext in _JVM_EXTS:
            result[rel] = _resolve_jvm_imports_for_file(rel, imps, jvm_cls_map, jvm_pkg_map)
        else:
            result[rel] = _resolve_cs_imports_for_file(rel, imps, cs_cls_map, cs_ns_map)
    return result


def _python_candidate_prefixes(cur_dir: str, subpkg_roots: Sequence[str]) -> List[str]:
    seen: Dict[str, None] = {"": None, "src/": None}
    if cur_dir:
        parts = cur_dir.split("/")
        for cut in range(len(parts), 0, -1):
            sub = "/".join(parts[:cut])
            seen.setdefault(f"{sub}/", None)
            seen.setdefault(f"{sub}/src/", None)
    for sr in subpkg_roots:
        seen.setdefault(f"{sr}/", None)
        seen.setdefault(f"{sr}/src/", None)
    return list(seen.keys())


def _build_python_suffix_index(discovered_files: Sequence[str]) -> Dict[str, List[Tuple[str, int, str]]]:
    """
    Pre-index `.py` files by slash-bounded module suffix `suf -> [(prefix, is_init, rel), ...]`
    so external/stdlib imports skip prefix probing in `O(1)` across 50k-file Python monorepos.
    """
    by_suffix: Dict[str, List[Tuple[str, int, str]]] = {}
    for py_rel in discovered_files:
        if not py_rel.endswith(".py") or py_rel == "__init__.py":
            continue
        if py_rel.endswith("/__init__.py"):
            mod_full = py_rel[:-12]
            is_init = 1
        else:
            mod_full = py_rel[:-3]
            is_init = 0
        if not mod_full:
            continue
        segs = mod_full.split("/")
        n_segs = len(segs)
        for cut in range(n_segs):
            suf = "/".join(segs[cut:])
            pfx = ("/".join(segs[:cut]) + "/") if cut > 0 else ""
            by_suffix.setdefault(suf, []).append((pfx, is_init, py_rel))
    return by_suffix


def invalidate_outline_cache(
    root: Path,
    aoci_dir: Path,
    touched_paths: Sequence[str] = (),
    pruned_paths: Sequence[str] = (),
    rename_map: Optional[Mapping[str, str]] = None,
) -> None:
    """
    Synchronize `.aoci/.cache/outline_cache.json` on writes:
      - Always delete `pruned_paths`.
      - Migrate `rename_map` keys.
      - Evict `touched_paths` ONLY when their cached `(mtime_ns, bytes)` no longer matches disk
        (preserving fresh outlines populated immediately prior by `scaffold`).
    """
    cache_path = aoci_dir / CACHE_DIR / OUTLINE_CACHE
    if not cache_path.is_file():
        return
    if not touched_paths and not pruned_paths and not rename_map:
        return
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(loaded, dict) or int(loaded.get("schema_version", 0) or 0) != 2:
        return
    files_obj = loaded.get("files")
    if not isinstance(files_obj, dict):
        return

    changed = False
    if rename_map:
        staged: Dict[str, Any] = {}
        for old_p, new_p in rename_map.items():
            old_norm = normalize_relpath(old_p)
            new_norm = normalize_relpath(new_p)
            if old_norm in files_obj:
                staged[new_norm] = files_obj.pop(old_norm)
                changed = True
        for new_norm, rec in staged.items():
            if isinstance(rec, dict) and isinstance(rec.get("outline"), dict):
                rec = dict(rec)
                out_copy = dict(rec["outline"])
                out_copy["path"] = new_norm
                rec["outline"] = out_copy
            files_obj[new_norm] = rec

    for raw_del in pruned_paths:
        norm_del = normalize_relpath(str(raw_del))
        if norm_del in files_obj:
            del files_obj[norm_del]
            changed = True

    for raw_p in touched_paths:
        norm_p = normalize_relpath(str(raw_p))
        rec = files_obj.get(norm_p)
        if not isinstance(rec, dict):
            continue
        try:
            st = os.lstat(root / norm_p)
            is_fresh = (
                stat.S_ISREG(st.st_mode)
                and not stat.S_ISLNK(st.st_mode)
                and st.st_mtime_ns == int(rec.get("mtime_ns", -1))
                and st.st_size == int(rec.get("bytes", -1))
            )
        except (OSError, TypeError, ValueError):
            is_fresh = False
        if not is_fresh:
            del files_obj[norm_p]
            changed = True

    if changed:
        try:
            atomic_write_text(
                cache_path,
                json.dumps({"schema_version": 2, "files": files_obj}, ensure_ascii=False, separators=(",", ":")),
            )
        except OSError:
            pass


def build_static_import_graph(
    root: Path,
    discovered_files: Sequence[str],
    fps: Optional[Dict[str, Dict[str, Any]]] = None,
    aoci_dir: Optional[Path] = None,
    outline_fn: Optional[Callable[[Path, str, int], Dict[str, Any]]] = None,
    resolution_extras: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """
    Scan discovered source files once (using mtime-keyed `outline_cache.json` and parallel extraction
    when needed) to resolve internal file-to-file imports across Python, JS/TS/Vue/Svelte, Go, Rust,
    Protobuf, and C/C++ (including monorepo nested configs).
    """
    def _default_graph_extractor(full: Path, rel: str, max_items: int) -> Dict[str, Any]:
        return extract_file_outline(full, rel, max_items=max_items, max_imports=0)

    extractor = outline_fn or _default_graph_extractor
    known_set = set(discovered_files)
    ts_cache: Dict[str, Tuple[str, List[Tuple[str, List[str]]]]] = {}
    go_cache: Dict[str, Tuple[str, str]] = {}

    py_subpkg_roots: List[str] = []
    seen_subpkgs: Set[str] = set()
    go_dir_files: Dict[str, List[str]] = {}
    has_py = False
    has_jvm_or_cs = False
    for f_rel in discovered_files:
        if not has_py and f_rel.endswith(".py"):
            has_py = True
        if f_rel.endswith(".go") and not f_rel.endswith("_test.go"):
            parent = f_rel.rsplit("/", 1)[0] if "/" in f_rel else ""
            go_dir_files.setdefault(parent, []).append(f_rel)
        elif not has_jvm_or_cs and f_rel.lower().endswith((".java", ".kt", ".kts", ".cs")):
            has_jvm_or_cs = True
        bname = f_rel.rsplit("/", 1)[-1]
        if bname in ("pyproject.toml", "setup.py", "setup.cfg") and "/" in f_rel:
            pkg_root = f_rel.rsplit("/", 1)[0]
            if pkg_root not in seen_subpkgs:
                seen_subpkgs.add(pkg_root)
                py_subpkg_roots.append(pkg_root)

    py_suffix_index: Dict[str, List[Tuple[str, int, str]]] = (
        _build_python_suffix_index(discovered_files) if has_py else {}
    )
    py_prefix_rank_cache: Dict[str, Dict[str, int]] = {}

    jvm_cls_map: Dict[str, List[str]] = {}
    jvm_pkg_map: Dict[str, List[str]] = {}
    cs_cls_map: Dict[str, List[str]] = {}
    cs_ns_map: Dict[str, List[str]] = {}
    if has_jvm_or_cs:
        jvm_cls_map, jvm_pkg_map, cs_cls_map, cs_ns_map = _build_jvm_cs_indexes(discovered_files)

    cache_path = (aoci_dir / CACHE_DIR / OUTLINE_CACHE) if aoci_dir is not None else None
    old_cache: Dict[str, Dict[str, Any]] = {}
    if cache_path is not None and cache_path.is_file():
        try:
            loaded_c = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(loaded_c, dict)
                and int(loaded_c.get("schema_version", 0)) == 2
                and isinstance(loaded_c.get("files"), dict)
            ):
                old_cache = loaded_c["files"]
        except (OSError, ValueError, TypeError):
            old_cache = {}

    outlines: Dict[str, Dict[str, Any]] = {}
    new_cache: Dict[str, Dict[str, Any]] = {}
    to_extract: List[Tuple[str, Optional[int], Optional[int]]] = []
    cache_dirty = False

    for rel in discovered_files:
        full = root / rel
        fp_meta = fps.get(rel) if fps else None
        if fp_meta is not None and "mtime_ns" in fp_meta and "bytes" in fp_meta:
            m_ns = int(fp_meta["mtime_ns"])
            sz = int(fp_meta["bytes"])
        else:
            try:
                st = os.lstat(full)
                if not stat.S_ISREG(st.st_mode):
                    continue
                m_ns = st.st_mtime_ns
                sz = st.st_size
            except OSError:
                continue

        c_rec = old_cache.get(rel)
        if (
            isinstance(c_rec, dict)
            and c_rec.get("mtime_ns") == m_ns
            and c_rec.get("bytes") == sz
            and isinstance(c_rec.get("outline"), dict)
        ):
            outlines[rel] = c_rec["outline"]
            new_cache[rel] = c_rec
        else:
            to_extract.append((rel, m_ns, sz))

    if to_extract:
        cache_dirty = True
        if len(to_extract) >= PARALLEL_HASH_THRESHOLD:
            workers = min(8, max(2, (os.cpu_count() or 4) * 2))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [
                    (rel, m_ns, sz, pool.submit(extractor, root / rel, rel, 40))
                    for rel, m_ns, sz in to_extract
                ]
                for rel, m_ns, sz, fut in futs:
                    out = fut.result()
                    outlines[rel] = out
                    new_cache[rel] = {"mtime_ns": m_ns, "bytes": sz, "outline": out}
        else:
            for rel, m_ns, sz in to_extract:
                out = extractor(root / rel, rel, 40)
                outlines[rel] = out
                new_cache[rel] = {"mtime_ns": m_ns, "bytes": sz, "outline": out}

    if len(new_cache) != len(old_cache):
        cache_dirty = True
    if cache_dirty and cache_path is not None and aoci_dir is not None and aoci_dir.is_dir():
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                cache_path,
                json.dumps({"schema_version": 2, "files": new_cache}, ensure_ascii=False, separators=(",", ":")),
            )
        except OSError:
            pass

    deps: Dict[str, List[str]] = {rel: [] for rel in discovered_files}
    callers_set: Dict[str, Set[str]] = {rel: set() for rel in discovered_files}
    go_imported_dirs_by_file: Dict[str, Set[str]] = {}
    jvm_cs_resolved_map: Dict[str, Tuple[List[str], List[List[str]]]] = {}

    for rel in discovered_files:
        out = outlines.get(rel)
        if out is None:
            continue
        ext = Path(rel).suffix.lower()
        cur_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        resolved: List[str] = []

        if ext == ".py":
            py_specs = list(dict.fromkeys([*out["imports"], *out.get("py_from_members", ())]))
            pfx_ranks: Optional[Dict[str, int]] = None
            for imp in py_specs:
                if imp.startswith("."):
                    lvl = len(imp) - len(imp.lstrip("."))
                    rem = imp.lstrip(".").replace(".", "/")
                    pkg_parts = cur_dir.split("/") if cur_dir else []
                    up = max(0, lvl - 1)
                    base_parts = pkg_parts[: max(0, len(pkg_parts) - up)] if up else pkg_parts
                    stem = "/".join([*base_parts, rem]) if rem else "/".join(base_parts)
                    for cand in (f"{stem}.py", f"{stem}/__init__.py"):
                        cand_norm = cand.strip("/")
                        if cand_norm in known_set and cand_norm != rel:
                            resolved.append(cand_norm)
                            break
                else:
                    mod_path = imp.replace(".", "/")
                    suf_hits = py_suffix_index.get(mod_path)
                    if not suf_hits:
                        continue
                    if pfx_ranks is None:
                        pfx_ranks = py_prefix_rank_cache.get(cur_dir)
                        if pfx_ranks is None:
                            pfx_ranks = {
                                pfx: r_idx
                                for r_idx, pfx in enumerate(_python_candidate_prefixes(cur_dir, py_subpkg_roots))
                            }
                            py_prefix_rank_cache[cur_dir] = pfx_ranks
                    best_key: Optional[Tuple[int, int]] = None
                    best_target: Optional[str] = None
                    for pfx, is_init, target_rel in suf_hits:
                        if target_rel == rel:
                            continue
                        r_idx = pfx_ranks.get(pfx)
                        if r_idx is None:
                            continue
                        cand_key = (r_idx, is_init)
                        if best_key is None or cand_key < best_key:
                            best_key = cand_key
                            best_target = target_rel
                            if r_idx == 0 and is_init == 0:
                                break
                    if best_target is not None:
                        resolved.append(best_target)
        elif ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"}:
            ts_base, ts_aliases = _resolve_tsconfig_for_dir(root, cur_dir, ts_cache)
            for spec in out["imports"]:
                hit: Optional[str] = None
                if spec.startswith("."):
                    hit = _probe_js_path(_norm_join_posix(cur_dir, spec), known_set)
                else:
                    for pat, targets in ts_aliases:
                        if pat.endswith("*"):
                            pfx = pat[:-1]
                            if spec.startswith(pfx):
                                tail = spec[len(pfx) :]
                                for tgt in targets:
                                    mapped = (tgt[:-1] + tail) if tgt.endswith("*") else tgt
                                    hit = _probe_js_path(_norm_join_posix(ts_base, mapped), known_set)
                                    if hit:
                                        break
                        elif spec == pat:
                            for tgt in targets:
                                hit = _probe_js_path(_norm_join_posix(ts_base, tgt), known_set)
                                if hit:
                                    break
                        if hit:
                            break
                    if hit is None and ts_base:
                        hit = _probe_js_path(_norm_join_posix(ts_base, spec), known_set)
                if hit and hit != rel:
                    resolved.append(hit)
        elif ext == ".go":
            mod_root, go_mod = _resolve_gomod_for_dir(root, cur_dir, go_cache)
            if go_mod:
                imp_dirs: Set[str] = set()
                for spec in out["imports"]:
                    if spec == go_mod or spec.startswith(go_mod + "/"):
                        sub_dir = spec[len(go_mod) :].lstrip("/")
                        full_dir = f"{mod_root}/{sub_dir}".strip("/") if mod_root else sub_dir
                        if full_dir in go_dir_files:
                            imp_dirs.add(full_dir)
                        for gf in go_dir_files.get(full_dir, ())[:3]:
                            if gf != rel:
                                resolved.append(gf)
                if imp_dirs and resolution_extras is not None:
                    go_imported_dirs_by_file[rel] = imp_dirs
        elif ext == ".rs":
            if rel.startswith("src/"):
                crate_src = "src"
            elif "/src/" in rel:
                crate_src = rel.split("/src/", 1)[0] + "/src"
            else:
                crate_src = "src"
            bname = rel.rsplit("/", 1)[-1]
            cur_parts = cur_dir.split("/") if cur_dir else []
            for raw_spec in out["imports"]:
                for spec in _expand_rust_use_tree(raw_spec):
                    base_parts: Optional[List[str]] = None
                    segs: List[str] = []
                    if spec.startswith("crate::"):
                        base_parts = crate_src.split("/")
                        segs = [s for s in spec[7:].split("::") if s]
                    elif spec.startswith("self::"):
                        base_parts = list(cur_parts)
                        segs = [s for s in spec[6:].split("::") if s]
                    elif spec.startswith("super::"):
                        sup_n = 0
                        rem_spec = spec
                        while rem_spec.startswith("super::"):
                            sup_n += 1
                            rem_spec = rem_spec[7:]
                        up = sup_n - (0 if bname in ("mod.rs", "lib.rs", "main.rs") else 1)
                        base_parts = cur_parts[: max(0, len(cur_parts) - up)] if up > 0 else list(cur_parts)
                        segs = [s for s in rem_spec.split("::") if s]
                    if base_parts is None or not segs:
                        continue
                    for cut in range(len(segs), 0, -1):
                        stem = "/".join([*base_parts, *segs[:cut]])
                        for cand in (f"{stem}.rs", f"{stem}/mod.rs"):
                            if cand in known_set and cand != rel:
                                resolved.append(cand)
                                break
                        else:
                            continue
                        break
        elif ext in _JVM_EXTS:
            exact_hits, group_hits = _resolve_jvm_imports_for_file(
                rel, out["imports"], jvm_cls_map, jvm_pkg_map
            )
            if resolution_extras is not None:
                jvm_cs_resolved_map[rel] = (exact_hits, group_hits)
            resolved.extend(exact_hits)
            for grp in group_hits:
                resolved.extend(grp)
        elif ext == ".cs":
            exact_hits, group_hits = _resolve_cs_imports_for_file(
                rel, out["imports"], cs_cls_map, cs_ns_map
            )
            if resolution_extras is not None:
                jvm_cs_resolved_map[rel] = (exact_hits, group_hits)
            resolved.extend(exact_hits)
            for grp in group_hits:
                resolved.extend(grp)
        elif ext in {".proto", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"}:
            for spec in out["imports"]:
                clean_spec = normalize_relpath(spec.strip("\"'<> "))
                if not clean_spec:
                    continue
                for cand in (
                    _norm_join_posix(cur_dir, clean_spec),
                    clean_spec,
                    f"include/{clean_spec}",
                    f"src/{clean_spec}",
                    f"proto/{clean_spec}",
                ):
                    if cand in known_set and cand != rel:
                        resolved.append(cand)
                        break

        dedup = list(dict.fromkeys(resolved))
        deps[rel] = dedup
        for target in dedup:
            callers_set[target].add(rel)

    if resolution_extras is not None:
        resolution_extras["go_imported_dirs_by_file"] = go_imported_dirs_by_file
        resolution_extras["go_files_by_dir"] = go_dir_files
        resolution_extras["jvm_cs_resolved"] = jvm_cs_resolved_map

    callers = {k: sorted(v) for k, v in callers_set.items()}
    return deps, callers, outlines
