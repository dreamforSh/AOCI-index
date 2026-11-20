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
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .constants import (
    CACHE_DIR,
    OUTLINE_CACHE,
    PARALLEL_HASH_THRESHOLD,
    SIG_HASH_EXTS,
    atomic_write_text,
    compute_size_tier,
    normalize_relpath,
)

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
    r"func(?:\s*\([^)]*\))?)\s+([A-Za-z_$][\w$]*)"
)


def _strip_js_comments(src: str) -> str:
    """
    Blank out `//` and `/* */` comments while preserving string literals, `/regex/` literals
    (including `//` or `/*` inside regexes), and exact line breaks.
    """
    out: List[str] = []
    i, n = 0, len(src)
    prev_tok = ""
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            out.append(c)
            i += 1
        elif c in ("'", '"', "`"):
            q = c
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                out.append(ch)
                i += 1
                if ch == "\\" and i < n:
                    out.append(src[i])
                    i += 1
                    continue
                if ch == q:
                    break
            prev_tok = q
        elif c == "/":
            nxt = src[i + 1] if i + 1 < n else ""
            if nxt == "/":
                out.append("  ")
                i += 2
                while i < n and src[i] != "\n":
                    out.append(" ")
                    i += 1
            elif nxt == "*":
                out.append("  ")
                i += 2
                while i < n:
                    if src[i] == "*" and i + 1 < n and src[i + 1] == "/":
                        out.append("  ")
                        i += 2
                        break
                    out.append("\n" if src[i] == "\n" else " ")
                    i += 1
            elif not prev_tok or prev_tok in _JS_REGEX_PREV_CHARS or prev_tok in _JS_REGEX_PREV_KW:
                out.append("/")
                i += 1
                in_class = False
                while i < n and src[i] != "\n":
                    ch = src[i]
                    out.append(ch)
                    i += 1
                    if ch == "\\" and i < n and src[i] != "\n":
                        out.append(src[i])
                        i += 1
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        while i < n and src[i].isalpha():
                            out.append(src[i])
                            i += 1
                        break
                prev_tok = "/"
            else:
                out.append("/")
                i += 1
                prev_tok = "/"
        elif c.isalpha() or c in "_$":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            word = src[i:j]
            out.append(word)
            prev_tok = word
            i = j
        elif c in "+-" and i + 1 < n and src[i + 1] == c:
            out.append(c * 2)
            prev_tok = c * 2
            i += 2
        else:
            out.append(c)
            prev_tok = c
            i += 1
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
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                out.append(ch)
                i += 1
                if ch == "\\" and i < n:
                    out.append(src[i])
                    i += 1
                    continue
                if ch == '"':
                    break
        elif c == "'":
            if single_quote_char_only:
                j = i + 1
                closed = False
                limit = min(n, i + 18)
                while j < limit and src[j] != "\n":
                    if src[j] == "\\":
                        j += 2
                        continue
                    if src[j] == "'":
                        closed = True
                        j += 1
                        break
                    j += 1
                if closed:
                    out.append(src[i:j])
                    i = j
                else:
                    out.append(c)
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
                    if ch == "'":
                        break
        elif allow_backtick and c == "`":
            out.append(c)
            i += 1
            while i < n:
                ch = src[i]
                out.append(ch)
                i += 1
                if ch == "`":
                    break
        elif c == "/":
            nxt = src[i + 1] if i + 1 < n else ""
            if nxt == "/":
                out.append("  ")
                i += 2
                while i < n and src[i] != "\n":
                    out.append(" ")
                    i += 1
            elif nxt == "*":
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
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _extract_js_ts_outline(src: str, line_offset: int, imports: List[str], symbols: List[str]) -> None:
    clean = _strip_js_comments(src)
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


def _py_fn_sig(fn_node: ast.AST) -> str:
    args_obj = getattr(fn_node, "args", None)
    if args_obj is None:
        return getattr(fn_node, "name", "")

    def _fmt_arg(a: ast.arg) -> str:
        ann = _ast_type_str(a.annotation)
        return f"{a.arg}:{ann}" if ann else a.arg

    posonly = [_fmt_arg(a) for a in getattr(args_obj, "posonlyargs", ())]
    reg = [_fmt_arg(a) for a in getattr(args_obj, "args", ())]
    parts: List[str] = list(posonly)
    if posonly:
        parts.append("/")
    parts.extend(reg)
    vararg = getattr(args_obj, "vararg", None)
    kwonlyargs = getattr(args_obj, "kwonlyargs", ())
    if vararg is not None:
        parts.append("*" + _fmt_arg(vararg))
    elif kwonlyargs:
        parts.append("*")
    parts.extend(_fmt_arg(a) for a in kwonlyargs)
    kwarg = getattr(args_obj, "kwarg", None)
    if kwarg is not None:
        parts.append("**" + _fmt_arg(kwarg))

    prefix = "async def" if isinstance(fn_node, ast.AsyncFunctionDef) else "def"
    ret = _ast_type_str(getattr(fn_node, "returns", None))
    ret_str = f"->{ret}" if ret else ""
    route_tag = _py_route_decorator(fn_node)
    return f"{prefix} {getattr(fn_node, 'name', '')}({','.join(parts)}){ret_str}{route_tag}"


def _iter_py_import_nodes(body: Sequence[ast.AST]) -> Iterator[ast.AST]:
    """Yield top-level Import/ImportFrom nodes plus those directly inside top-level If/Try blocks."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            for sub in node.body:
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    yield sub
            cur_else = node.orelse
            while cur_else:
                for sub in cur_else:
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        yield sub
                if len(cur_else) == 1 and isinstance(cur_else[0], ast.If):
                    elif_node = cur_else[0]
                    for sub in elif_node.body:
                        if isinstance(sub, (ast.Import, ast.ImportFrom)):
                            yield sub
                    cur_else = elif_node.orelse
                else:
                    break
        elif isinstance(node, ast.Try):
            sub_lists: List[Sequence[ast.AST]] = [node.body, node.orelse, node.finalbody]
            for handler in node.handlers:
                sub_lists.append(handler.body)
            for stmts in sub_lists:
                for sub in stmts:
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        yield sub


def _extract_outline_from_bytes(
    norm_bytes: bytes,
    ext: str,
    rel: str,
    max_items: int = 30,
) -> Dict[str, Any]:
    """Pure byte-buffer implementation of `extract_file_outline` (expects LF-normalized bytes)."""
    line_count = 0 if not norm_bytes else norm_bytes.count(b"\n") + (0 if norm_bytes.endswith(b"\n") else 1)
    text = norm_bytes.decode("utf-8", errors="replace")
    lines = text[:-1].split("\n") if text.endswith("\n") else (text.split("\n") if text else [])
    ext = ext.lower()

    imports: List[str] = []
    symbols: List[str] = []
    py_from_members: List[str] = []

    if ext == ".py":
        try:
            tree = ast.parse(text)
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
                    args = [a.arg for a in node.args.args]
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
        except SyntaxError:
            pass
    elif ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
        _extract_js_ts_outline(text, 0, imports, symbols)
    elif ext in {".vue", ".svelte"}:
        for m in _SFC_SCRIPT_RE.finditer(text):
            offset = text.count("\n", 0, m.start(1))
            _extract_js_ts_outline(m.group(1), offset, imports, symbols)
    elif ext == ".go":
        clean_go = _strip_c_comments(text, allow_backtick=True)
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
        clean_rs = _strip_c_comments(text, single_quote_char_only=True)
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
    elif ext in {".java", ".kt", ".cs"}:
        clean_jvm = _strip_c_comments(text)
        jvm_lines = clean_jvm[:-1].split("\n") if clean_jvm.endswith("\n") else (clean_jvm.split("\n") if clean_jvm else [])
        for idx, ln in enumerate(jvm_lines, start=1):
            s = ln.strip()
            if s.startswith(("import ", "using ")):
                imports.append(s.rstrip(";"))
                continue
            ms = _JVM_CS_SYM_RE.match(ln)
            if ms:
                symbols.append(f"{ms.group(1)} {ms.group(2)} [L{idx}]")
    else:
        for idx, ln in enumerate(lines, start=1):
            s = ln.strip()
            if s.startswith("#include "):
                imports.append(s[9:].strip())
            elif ext == ".md" and s.startswith(("# ", "## ")):
                symbols.append(f"{s} [L{idx}]")

    dedup_imp = list(dict.fromkeys(imports))[:max_items]
    dedup_sym = list(dict.fromkeys(symbols))[:max_items]

    out_obj: Dict[str, Any] = {
        "path": rel,
        "lines": line_count,
        "size_tier": compute_size_tier(line_count),
        "imports": dedup_imp,
        "symbols": dedup_sym,
    }
    if py_from_members:
        out_obj["py_from_members"] = list(dict.fromkeys(py_from_members))[: max_items * 4]
    return out_obj


def _py_all_sig(value: Optional[ast.AST]) -> str:
    if value is None:
        return "__all__:None"
    try:
        val = ast.literal_eval(value)
        return "__all__:" + json.dumps(val, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError):
        return "__all__:" + ast.dump(value)


def compute_signature_hash_from_bytes(norm_bytes: bytes, ext: str, rel: str = "") -> Optional[str]:
    """Compute a 16-hex-char fingerprint of a file's public interface & imports."""
    ext_l = ext.lower()
    if ext_l not in SIG_HASH_EXTS:
        return None
    if b"\r\n" in norm_bytes:
        norm_bytes = norm_bytes.replace(b"\r\n", b"\n")

    if ext_l == ".py":
        text = norm_bytes.decode("utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
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
                members = [
                    _py_fn_sig(n)
                    for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("__")
                ]
                for n in node.body:
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and not n.target.id.startswith("_"):
                        members.append(f"{n.target.id}:{_ast_type_str(n.annotation)}")
                members.sort()
                py_sigs.append(f"class {node.name}({bases})[{';'.join(members)}]")
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                    py_sigs.append(_py_all_sig(node.value))
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                    ann = _ast_type_str(node.annotation)
                    py_sigs.append(f"__all__[{ann}]:{_py_all_sig(node.value)}")
        payload = {"i": sorted(set(py_imps)), "s": sorted(py_sigs)}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    out = _extract_outline_from_bytes(norm_bytes, ext_l, rel or f"file{ext_l}", max_items=200)
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


def extract_file_outline(full_path: Path, rel: str, max_items: int = 30) -> Dict[str, Any]:
    """Lightweight static outline extractor (imports + top-level symbols/signatures)."""
    raw_bytes = full_path.read_bytes()
    norm_bytes = raw_bytes.replace(b"\r\n", b"\n") if b"\r\n" in raw_bytes else raw_bytes
    return _extract_outline_from_bytes(norm_bytes, full_path.suffix.lower(), rel, max_items=max_items)


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
        raw = cfg_path.read_text(encoding="utf-8", errors="replace")
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
            for pat, targets in paths_obj.items():
                if isinstance(pat, str) and isinstance(targets, list):
                    str_targets = [normalize_relpath(str(t)) for t in targets if isinstance(t, str)]
                    if str_targets:
                        aliases[pat] = str_targets
    return base_url, aliases


def _load_tsconfig_aliases(root: Path) -> Tuple[str, List[Tuple[str, List[str]]]]:
    """Parse `baseUrl` and `paths` aliases (following relative `extends`) from `tsconfig.json` / `jsconfig.json`."""
    for cfg_name in ("tsconfig.json", "jsconfig.json"):
        cfg_path = root / cfg_name
        if not cfg_path.is_file():
            continue
        base_url, aliases_map = _load_tsconfig_file(root, cfg_path, set())
        if base_url is not None or aliases_map:
            return base_url or "", list(aliases_map.items())
    return "", []


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


def _load_go_module_prefix(root: Path) -> str:
    gomod = root / "go.mod"
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


def resolve_go_package_imports(
    root: Path,
    outlines: Dict[str, Dict[str, Any]],
    discovered_files: Sequence[str],
) -> Tuple[Dict[str, Set[str]], Dict[str, List[str]]]:
    """
    Resolve Go package directory imports per `.go` file and map each Go package directory to its
    non-`_test.go` files using `_load_go_module_prefix(root)`.
    """
    go_mod = _load_go_module_prefix(root)
    go_files_by_dir: Dict[str, List[str]] = {}
    for f_rel in sorted(discovered_files):
        if f_rel.endswith(".go") and not f_rel.endswith("_test.go"):
            parent = f_rel.rsplit("/", 1)[0] if "/" in f_rel else ""
            go_files_by_dir.setdefault(parent, []).append(f_rel)

    go_imported_dirs_by_file: Dict[str, Set[str]] = {}
    if go_mod:
        for rel in discovered_files:
            if not rel.endswith(".go"):
                continue
            out = outlines.get(rel)
            if not isinstance(out, dict):
                continue
            imp_dirs: Set[str] = set()
            for spec in out.get("imports", ()):
                if spec == go_mod or spec.startswith(go_mod + "/"):
                    sub_dir = spec[len(go_mod) :].lstrip("/")
                    imp_dirs.add(sub_dir)
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
    return None


def build_static_import_graph(
    root: Path,
    discovered_files: Sequence[str],
    fps: Optional[Dict[str, Dict[str, Any]]] = None,
    aoci_dir: Optional[Path] = None,
    outline_fn: Optional[Callable[[Path, str, int], Dict[str, Any]]] = None,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """
    Scan discovered source files once (using mtime-keyed `outline_cache.json` and parallel extraction
    when needed) to resolve internal file-to-file imports across Python, JS/TS/Vue/Svelte, Go, and Rust.
    """
    extractor = outline_fn or extract_file_outline
    known_set = set(discovered_files)
    ts_base, ts_aliases = _load_tsconfig_aliases(root)
    go_mod = _load_go_module_prefix(root)

    go_dir_files: Dict[str, List[str]] = {}
    for f_rel in discovered_files:
        if f_rel.endswith(".go") and not f_rel.endswith("_test.go"):
            parent = f_rel.rsplit("/", 1)[0] if "/" in f_rel else ""
            go_dir_files.setdefault(parent, []).append(f_rel)

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

    for rel in discovered_files:
        out = outlines.get(rel)
        if out is None:
            continue
        ext = Path(rel).suffix.lower()
        cur_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        resolved: List[str] = []

        if ext == ".py":
            py_specs = list(dict.fromkeys([*out["imports"], *out.get("py_from_members", ())]))
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
                    for prefix in ("", "src/"):
                        for cand in (f"{prefix}{mod_path}.py", f"{prefix}{mod_path}/__init__.py"):
                            if cand in known_set and cand != rel:
                                resolved.append(cand)
                                break
                        else:
                            continue
                        break
        elif ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"}:
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
        elif ext == ".go" and go_mod:
            for spec in out["imports"]:
                if spec == go_mod or spec.startswith(go_mod + "/"):
                    sub_dir = spec[len(go_mod) :].lstrip("/")
                    for gf in go_dir_files.get(sub_dir, ())[:3]:
                        if gf != rel:
                            resolved.append(gf)
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

        dedup = list(dict.fromkeys(resolved))
        deps[rel] = dedup
        for target in dedup:
            callers_set[target].add(rel)

    callers = {k: sorted(v) for k, v in callers_set.items()}
    return deps, callers, outlines
