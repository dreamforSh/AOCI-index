"""Optional zero-hard-dependency `tree-sitter` AST accelerator (`pip install aoci-index[treesitter]`).

Thread-safety: a single `tree_sitter.Parser` must never be shared across threads, so parsers are cached
per thread (`threading.local()`); `build_static_import_graph()` extracts outlines from an 8-worker pool.

Output parity: symbol labels and import specs intentionally mirror the regex extractors in `parsers.py`
(`function X`, `const X`, `export X`, `func (r *T) M`, `fn X`, `namespace X`, ...), so enabling the
accelerator never drops symbols the zero-dependency path would have reported. Files whose tree contains
syntax errors fall back to the regex path (`None`).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

EXT_TO_TS_LANG: Dict[str, str] = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "c_sharp",
    ".c": "c",
    ".h": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
}

# Backend grammar names differ: `tree_sitter_languages` uses `c_sharp`, `tree_sitter_language_pack` uses `csharp`
_BACKEND_LANG_ALIASES: Dict[str, Tuple[str, ...]] = {
    "c_sharp": ("csharp", "c_sharp"),
}

# Bumped whenever extraction semantics change so mtime-keyed outline caches built by an older
# accelerator (or by the regex path) are re-extracted instead of served stale.
TS_EXTRACTOR_REV = 2

_PROBE_LOCK = threading.Lock()
_PROBE_DONE = False
_GET_PARSER_FN: Optional[Callable[[str], Any]] = None
_PARSER_GEN = 0
_UNAVAILABLE_LANGS: Set[str] = set()
_TLS = threading.local()
_STRING_QUOTES = "\"'`"
_JS_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
_WS_RE = re.compile(r"\s+")


def reset_treesitter_probe() -> None:
    """Reset cached `tree_sitter_language_pack` availability probe (used in tests and dynamic installs)."""
    global _PROBE_DONE, _GET_PARSER_FN, _PARSER_GEN
    with _PROBE_LOCK:
        _PROBE_DONE = False
        _GET_PARSER_FN = None
        _PARSER_GEN += 1
        _UNAVAILABLE_LANGS.clear()


def _probe_backend() -> Optional[Callable[[str], Any]]:
    global _PROBE_DONE, _GET_PARSER_FN
    if _PROBE_DONE:
        return _GET_PARSER_FN
    with _PROBE_LOCK:
        if _PROBE_DONE:
            return _GET_PARSER_FN
        found: Optional[Callable[[str], Any]] = None
        for mod_name in ("tree_sitter_language_pack", "tree_sitter_languages"):
            try:
                if importlib.util.find_spec(mod_name) is None:
                    continue
                mod = importlib.import_module(mod_name)
                fn = getattr(mod, "get_parser", None)
                if callable(fn):
                    found = fn
                    break
            except Exception:
                continue
        _GET_PARSER_FN = found
        _PROBE_DONE = True
        return found


def _disabled_by_env() -> bool:
    return os.environ.get("AOCI_DISABLE_TREESITTER", "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Return True when `tree-sitter` acceleration is installed and not disabled via `AOCI_DISABLE_TREESITTER`."""
    if _disabled_by_env():
        return False
    return _probe_backend() is not None


def extractor_cache_tag() -> str:
    """Stable tag identifying which outline extractor produced cached outlines (`regex` or `ts<rev>`)."""
    return f"ts{TS_EXTRACTOR_REV}" if is_enabled() else "regex"


def treesitter_status() -> Dict[str, Any]:
    """Diagnostic status of optional `tree-sitter` AST acceleration."""
    fn = _probe_backend()
    dis = _disabled_by_env()
    return {
        "installed": fn is not None,
        "enabled": bool(fn is not None and not dis),
        "disabled_by_env": dis,
        "supported_extensions": sorted(EXT_TO_TS_LANG.keys()),
    }


def _thread_parser_cache() -> Dict[str, Any]:
    cache = getattr(_TLS, "parsers", None)
    if cache is None or getattr(_TLS, "gen", -1) != _PARSER_GEN:
        cache = {}
        _TLS.parsers = cache
        _TLS.gen = _PARSER_GEN
    return cache


def _get_parser(lang: str) -> Optional[Any]:
    """Return a parser owned by the CURRENT thread (never shared across threads)."""
    cache = _thread_parser_cache()
    if lang in cache:
        return cache[lang]
    if lang in _UNAVAILABLE_LANGS:
        return None
    fn = _probe_backend()
    if fn is None:
        return None
    parser = None
    for backend_name in _BACKEND_LANG_ALIASES.get(lang, (lang,)):
        try:
            parser = fn(backend_name)
        except Exception:
            parser = None
        if parser is not None:
            break
    if parser is None:
        # Remember globally so N worker threads don't each retry a missing/undownloadable grammar
        with _PROBE_LOCK:
            _UNAVAILABLE_LANGS.add(lang)
    cache[lang] = parser
    return parser


def _node_text(src_bytes: bytes, node: Any) -> str:
    start_b = getattr(node, "start_byte", 0)
    end_b = getattr(node, "end_byte", 0)
    return src_bytes[start_b:end_b].decode("utf-8", errors="replace")


def _node_line_1based(node: Any) -> int:
    sp = getattr(node, "start_point", (0, 0))
    return int(sp[0]) + 1


def _children(node: Any) -> Tuple[Any, ...]:
    return tuple(getattr(node, "children", ()) or ())


def _child_by_field_or_type(node: Any, field_name: str, fallback_types: Tuple[str, ...] = ()) -> Optional[Any]:
    by_field = getattr(node, "child_by_field_name", None)
    if callable(by_field):
        found = by_field(field_name)
        if found is not None:
            return found
    for ch in _children(node):
        if getattr(ch, "type", "") in fallback_types:
            return ch
    return None


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------------------------
# JavaScript / TypeScript / TSX
# ---------------------------------------------------------------------------------------------

_JS_NO_DESCEND = frozenset({"comment", "string", "template_string", "regex", "number", "html_comment"})
_JS_FN_DECLS = frozenset({"function_declaration", "generator_function_declaration", "function_signature"})
_JS_CLASS_DECLS = frozenset({"class_declaration", "abstract_class_declaration"})
_JS_TYPE_DECLS = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_JS_PUNCT = frozenset({"(", ")", ","})


def _js_string_spec(src: bytes, node: Optional[Any]) -> str:
    if node is None or getattr(node, "type", "") != "string":
        return ""
    return _node_text(src, node).strip(_STRING_QUOTES)


def _js_export_clause_names(src: bytes, clause: Any) -> List[str]:
    names: List[str] = []
    for spec in _children(clause):
        if getattr(spec, "type", "") != "export_specifier":
            continue
        alias = _child_by_field_or_type(spec, "alias")
        name_n = alias if alias is not None else _child_by_field_or_type(spec, "name", ("identifier",))
        if name_n is None:
            idents = [c for c in _children(spec) if getattr(c, "type", "") == "identifier"]
            name_n = idents[-1] if idents else None
        if name_n is not None:
            nm = _node_text(src, name_n).strip()
            if _JS_IDENT_RE.match(nm):
                names.append(nm)
    return names


def _js_visit(
    src: bytes, node: Any, parent_type: str, imports: List[str], symbols: List[str]
) -> bool:
    """Handle one JS/TS node; return True to descend into its children."""
    ntype = getattr(node, "type", "")
    if ntype in _JS_NO_DESCEND:
        return False

    if ntype == "import_statement":
        spec = _js_string_spec(src, _child_by_field_or_type(node, "source", ("string",)))
        if spec:
            imports.append(spec)
        return False

    if ntype == "export_statement":
        spec = _js_string_spec(src, _child_by_field_or_type(node, "source", ("string",)))
        if spec:
            imports.append(spec)
        line_no = _node_line_1based(node)
        for ch in _children(node):
            ch_type = getattr(ch, "type", "")
            if ch_type == "export_clause":
                for nm in _js_export_clause_names(src, ch):
                    symbols.append(f"export {nm} [L{line_no}]")
            elif ch_type == "namespace_export":
                idents = [c for c in _children(ch) if getattr(c, "type", "") in ("identifier", "string")]
                if idents:
                    nm = _node_text(src, idents[-1]).strip(_STRING_QUOTES)
                    if _JS_IDENT_RE.match(nm):
                        symbols.append(f"export {nm} [L{line_no}]")
        return True

    if ntype in _JS_FN_DECLS:
        nm = _child_by_field_or_type(node, "name", ("identifier",))
        if nm is not None:
            symbols.append(f"function {_node_text(src, nm)} [L{_node_line_1based(nm)}]")
        return True

    if ntype in _JS_CLASS_DECLS:
        nm = _child_by_field_or_type(node, "name", ("type_identifier", "identifier"))
        if nm is not None:
            symbols.append(f"class {_node_text(src, nm)} [L{_node_line_1based(nm)}]")
        return True

    kind = _JS_TYPE_DECLS.get(ntype)
    if kind is not None:
        nm = _child_by_field_or_type(node, "name", ("type_identifier", "identifier"))
        if nm is not None:
            symbols.append(f"{kind} {_node_text(src, nm)} [L{_node_line_1based(nm)}]")
        return False

    if ntype in ("lexical_declaration", "variable_declaration") and parent_type == "export_statement":
        first = _children(node)[:1]
        is_const = bool(first) and _node_text(src, first[0]).strip() == "const"
        label = "const" if is_const else "export"
        for decl in _children(node):
            if getattr(decl, "type", "") != "variable_declarator":
                continue
            nm = _child_by_field_or_type(decl, "name", ("identifier",))
            if nm is not None and getattr(nm, "type", "identifier") == "identifier":
                symbols.append(f"{label} {_node_text(src, nm)} [L{_node_line_1based(nm)}]")
        return True

    if ntype == "call_expression":
        fn_n = _child_by_field_or_type(node, "function")
        fn_type = getattr(fn_n, "type", "") if fn_n is not None else ""
        if fn_type == "import" or (fn_type == "identifier" and _node_text(src, fn_n) == "require"):
            args = _child_by_field_or_type(node, "arguments", ("arguments",))
            if args is not None:
                real = [a for a in _children(args) if getattr(a, "type", "") not in _JS_PUNCT]
                if len(real) == 1:
                    spec = _js_string_spec(src, real[0])
                    if spec:
                        imports.append(spec)
        return True

    if ntype == "assignment_expression":
        left = _child_by_field_or_type(node, "left", ("member_expression",))
        if left is not None and getattr(left, "type", "") == "member_expression":
            obj = _child_by_field_or_type(left, "object")
            prop = _child_by_field_or_type(left, "property", ("property_identifier",))
            if obj is not None and prop is not None:
                obj_txt = _node_text(src, obj).replace(" ", "")
                if obj_txt in ("exports", "module.exports"):
                    nm = _node_text(src, prop).strip()
                    if _JS_IDENT_RE.match(nm):
                        symbols.append(f"export {nm} [L{_node_line_1based(node)}]")
        return True

    return True


# ---------------------------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------------------------

_GO_DESCEND = frozenset({"source_file", "import_declaration", "import_spec_list", "type_declaration"})


def _go_visit(src: bytes, node: Any, _parent: str, imports: List[str], symbols: List[str]) -> bool:
    ntype = getattr(node, "type", "")
    if ntype == "import_spec":
        path_n = _child_by_field_or_type(node, "path", ("interpreted_string_literal", "raw_string_literal"))
        if path_n is not None:
            spec = _node_text(src, path_n).strip('"`')
            if spec:
                imports.append(spec)
        return False
    if ntype == "function_declaration":
        nm = _child_by_field_or_type(node, "name", ("identifier",))
        if nm is not None:
            symbols.append(f"func {_node_text(src, nm)} [L{_node_line_1based(node)}]")
        return False
    if ntype == "method_declaration":
        nm = _child_by_field_or_type(node, "name", ("field_identifier", "identifier"))
        recv = _child_by_field_or_type(node, "receiver", ("parameter_list",))
        if nm is not None:
            recv_txt = f" {_collapse(_node_text(src, recv))}" if recv is not None else ""
            symbols.append(f"func{recv_txt} {_node_text(src, nm)} [L{_node_line_1based(node)}]")
        return False
    if ntype in ("type_spec", "type_alias"):
        nm = _child_by_field_or_type(node, "name", ("type_identifier",))
        if nm is not None:
            ty = _child_by_field_or_type(node, "type")
            ty_type = getattr(ty, "type", "") if ty is not None else ""
            suffix = " struct" if ty_type == "struct_type" else (" interface" if ty_type == "interface_type" else "")
            symbols.append(f"type {_node_text(src, nm)}{suffix} [L{_node_line_1based(node)}]")
        return False
    return ntype in _GO_DESCEND


# ---------------------------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------------------------

_RS_ITEMS = {
    "function_item": "fn",
    "function_signature_item": "fn",
    "struct_item": "struct",
    "enum_item": "enum",
    "trait_item": "trait",
    "mod_item": "mod",
    "type_item": "type",
}
_RS_DESCEND = frozenset({"source_file", "declaration_list", "impl_item", "trait_item", "mod_item", "foreign_mod_item"})


def _rs_visit(src: bytes, node: Any, _parent: str, imports: List[str], symbols: List[str]) -> bool:
    ntype = getattr(node, "type", "")
    if ntype == "use_declaration":
        raw_u = _collapse(_node_text(src, node)).rstrip(";")
        m_u = re.match(r"^(?:pub(?:\([^)]*\))?\s+)?use\s+(.+)$", raw_u)
        if m_u:
            imports.append(m_u.group(1).strip())
        return False
    kind = _RS_ITEMS.get(ntype)
    if kind is not None:
        nm = _child_by_field_or_type(node, "name", ("identifier", "type_identifier"))
        if nm is not None:
            symbols.append(f"{kind} {_node_text(src, nm)} [L{_node_line_1based(node)}]")
    return ntype in _RS_DESCEND


# ---------------------------------------------------------------------------------------------
# Java / C#
# ---------------------------------------------------------------------------------------------

_JVM_TYPE_DECLS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "record_struct_declaration",
        "struct_declaration",
    }
)
_JVM_DESCEND = frozenset(
    {
        "program",
        "compilation_unit",
        "namespace_declaration",
        "file_scoped_namespace_declaration",
        "declaration_list",
        "class_body",
        "interface_body",
        "enum_body",
        "enum_body_declarations",
    }
)


def _jvm_visit(src: bytes, node: Any, _parent: str, imports: List[str], symbols: List[str]) -> bool:
    ntype = getattr(node, "type", "")
    if ntype in ("import_declaration", "using_directive"):
        raw_i = _collapse(_node_text(src, node)).rstrip(";").strip()
        if raw_i:
            imports.append(raw_i)
        return False
    if ntype in _JVM_TYPE_DECLS:
        kind = ntype.split("_", 1)[0]
        nm = _child_by_field_or_type(node, "name", ("identifier",))
        if nm is not None:
            symbols.append(f"{kind} {_node_text(src, nm)} [L{_node_line_1based(nm)}]")
        return True
    return ntype in _JVM_DESCEND


# ---------------------------------------------------------------------------------------------
# C / C++
# ---------------------------------------------------------------------------------------------

_C_TYPE_SPECS = {
    "class_specifier": "class",
    "struct_specifier": "struct",
    "enum_specifier": "enum",
    "union_specifier": "union",
}
_C_TYPE_BODIES = frozenset({"field_declaration_list", "enumerator_list"})
_C_DECLARATOR_WRAPPERS = frozenset(
    {"pointer_declarator", "reference_declarator", "parenthesized_declarator", "attributed_declarator"}
)
_C_FN_NAME_TYPES = (
    "identifier",
    "field_identifier",
    "qualified_identifier",
    "destructor_name",
    "operator_name",
    "template_function",
)
# Containers whose children are file/namespace-scope declarations (functions + types are reported)
_C_SCOPE_CONTAINERS = frozenset(
    {
        "translation_unit",
        "declaration_list",
        "linkage_specification",
        "template_declaration",
        "preproc_if",
        "preproc_ifdef",
        "preproc_else",
        "preproc_elif",
        "preproc_elifdef",
    }
)
# Containers that may hold nested TYPE definitions only (class bodies, typedefs, declarations)
_C_TYPE_CONTAINERS = frozenset({"field_declaration_list", "field_declaration", "declaration", "type_definition"})


def _c_function_name(src: bytes, declarator: Optional[Any]) -> Optional[str]:
    cur = declarator
    for _ in range(8):
        if cur is None:
            return None
        ctype = getattr(cur, "type", "")
        if ctype == "function_declarator":
            nm = _child_by_field_or_type(cur, "declarator", _C_FN_NAME_TYPES)
            if nm is None:
                return None
            return _collapse(_node_text(src, nm))
        if ctype in _C_DECLARATOR_WRAPPERS:
            nxt = _child_by_field_or_type(cur, "declarator")
            if nxt is None:
                named = [c for c in _children(cur) if getattr(c, "type", "") not in ("*", "&", "&&", "(", ")")]
                nxt = named[-1] if named else None
            cur = nxt
            continue
        return None
    return None


def _c_visit(src: bytes, node: Any, parent_type: str, imports: List[str], symbols: List[str]) -> bool:
    ntype = getattr(node, "type", "")
    in_class = parent_type in ("field_declaration_list", "field_declaration")
    if ntype == "preproc_include":
        path_n = _child_by_field_or_type(node, "path", ("string_literal", "system_lib_string"))
        if path_n is not None:
            inc = _node_text(src, path_n).strip("\"'<>")
            if inc:
                imports.append(inc)
        return False
    if ntype == "namespace_definition":
        nm = _child_by_field_or_type(node, "name", ("namespace_identifier", "nested_namespace_specifier", "identifier"))
        if nm is not None:
            symbols.append(f"namespace {_collapse(_node_text(src, nm))} [L{_node_line_1based(node)}]")
        return True
    kind = _C_TYPE_SPECS.get(ntype)
    if kind is not None:
        nm = _child_by_field_or_type(node, "name", ("type_identifier",))
        has_body = any(getattr(c, "type", "") in _C_TYPE_BODIES for c in _children(node))
        if nm is not None and has_body:
            symbols.append(f"{kind} {_collapse(_node_text(src, nm))} [L{_node_line_1based(node)}]")
        return has_body
    if ntype == "function_definition":
        # In-class inline methods are skipped (parity with the regex path's brace-depth-0 rule)
        if not in_class:
            fn_name = _c_function_name(src, _child_by_field_or_type(node, "declarator", ("function_declarator",)))
            if fn_name:
                symbols.append(f"fn {fn_name} [L{_node_line_1based(node)}]")
        return False
    if ntype == "declaration" and not in_class:
        # File/namespace-scope prototypes (`int *foo(int);`) mirror the regex path's `;`-terminated match
        fn_name = _c_function_name(src, _child_by_field_or_type(node, "declarator"))
        if fn_name:
            symbols.append(f"fn {fn_name} [L{_node_line_1based(node)}]")
        return True
    return ntype in _C_SCOPE_CONTAINERS or ntype in _C_TYPE_CONTAINERS


# ---------------------------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------------------------


def _walk(
    root: Any,
    src: bytes,
    visit: Callable[[bytes, Any, str, List[str], List[str]], bool],
    imports: List[str],
    symbols: List[str],
) -> None:
    """Iterative pre-order traversal (source order, no recursion/depth limits)."""
    stack: List[Tuple[Any, str]] = [(ch, getattr(root, "type", "")) for ch in reversed(_children(root))]
    while stack:
        node, parent_type = stack.pop()
        if visit(src, node, parent_type, imports, symbols):
            ntype = getattr(node, "type", "")
            kids = _children(node)
            for ch in reversed(kids):
                stack.append((ch, ntype))


def extract_outline_with_treesitter(
    norm_bytes: bytes,
    ext: str,
) -> Optional[Tuple[List[str], List[str]]]:
    """
    Extract `(imports, symbols)` from `norm_bytes` via `tree-sitter` when available.
    Returns `None` when `tree-sitter` is disabled, unavailable, does not cover `ext`, or the parse tree
    contains syntax errors (so the caller falls back to the regex extractor instead of degrading).
    """
    if not is_enabled():
        return None
    lang = EXT_TO_TS_LANG.get(ext.lower())
    if not lang:
        return None
    parser = _get_parser(lang)
    if parser is None:
        return None
    try:
        tree = parser.parse(norm_bytes)
        root = tree.root_node
    except Exception:
        return None
    if getattr(root, "has_error", False) is True:
        return None

    imports: List[str] = []
    symbols: List[str] = []
    visit: Callable[[bytes, Any, str, List[str], List[str]], bool]
    if lang in ("javascript", "typescript", "tsx"):
        visit = _js_visit
    elif lang == "go":
        visit = _go_visit
    elif lang == "rust":
        visit = _rs_visit
    elif lang in ("java", "c_sharp"):
        visit = _jvm_visit
    elif lang in ("c", "cpp"):
        visit = _c_visit
    else:
        return None
    try:
        _walk(root, norm_bytes, visit, imports, symbols)
    except Exception:
        return None
    return imports, symbols
