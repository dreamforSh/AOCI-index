"""Optional zero-hard-dependency `tree-sitter` AST accelerator (`pip install aoci-index[treesitter]`)."""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

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

_PROBE_DONE = False
_GET_PARSER_FN: Optional[Callable[[str], Any]] = None
_PARSER_CACHE: Dict[str, Any] = {}
_STRING_QUOTES = "\"'`"


def reset_treesitter_probe() -> None:
    """Reset cached `tree_sitter_language_pack` availability probe (used in tests and dynamic installs)."""
    global _PROBE_DONE, _GET_PARSER_FN
    _PROBE_DONE = False
    _GET_PARSER_FN = None
    _PARSER_CACHE.clear()


def _probe_backend() -> Optional[Callable[[str], Any]]:
    global _PROBE_DONE, _GET_PARSER_FN
    if _PROBE_DONE:
        return _GET_PARSER_FN
    _PROBE_DONE = True
    for mod_name in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            if importlib.util.find_spec(mod_name) is None:
                continue
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, "get_parser", None)
            if callable(fn):
                _GET_PARSER_FN = fn
                return fn
        except Exception:
            continue
    return None


def is_enabled() -> bool:
    """Return True when `tree-sitter` acceleration is installed and not disabled via `AOCI_DISABLE_TREESITTER`."""
    dis = os.environ.get("AOCI_DISABLE_TREESITTER", "").strip().lower()
    if dis in ("1", "true", "yes", "on"):
        return False
    return _probe_backend() is not None


def treesitter_status() -> Dict[str, Any]:
    """Diagnostic status of optional `tree-sitter` AST acceleration."""
    fn = _probe_backend()
    dis = os.environ.get("AOCI_DISABLE_TREESITTER", "").strip().lower() in ("1", "true", "yes", "on")
    return {
        "installed": fn is not None,
        "enabled": bool(fn is not None and not dis),
        "disabled_by_env": dis,
        "supported_extensions": sorted(EXT_TO_TS_LANG.keys()),
    }


def _get_parser(lang: str) -> Optional[Any]:
    if lang in _PARSER_CACHE:
        return _PARSER_CACHE[lang]
    fn = _probe_backend()
    if fn is None:
        return None
    try:
        p = fn(lang)
    except Exception:
        p = None
    _PARSER_CACHE[lang] = p
    return p


def _node_text(src_bytes: bytes, node: Any) -> str:
    start_b = getattr(node, "start_byte", 0)
    end_b = getattr(node, "end_byte", 0)
    return src_bytes[start_b:end_b].decode("utf-8", errors="replace")


def _node_line_1based(node: Any) -> int:
    sp = getattr(node, "start_point", (0, 0))
    return int(sp[0]) + 1


def _child_by_field_or_type(node: Any, field_name: str, fallback_types: Tuple[str, ...] = ()) -> Optional[Any]:
    by_field = getattr(node, "child_by_field_name", None)
    if callable(by_field):
        found = by_field(field_name)
        if found is not None:
            return found
    for ch in getattr(node, "children", ()) or ():
        if getattr(ch, "type", "") in fallback_types:
            return ch
    return None


def extract_outline_with_treesitter(
    norm_bytes: bytes,
    ext: str,
) -> Optional[Tuple[List[str], List[str]]]:
    """
    Extract `(imports, symbols)` from `norm_bytes` via `tree-sitter` when available.
    Returns `None` when `tree-sitter` is disabled, unavailable, or does not cover `ext`.
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

    imports: List[str] = []
    symbols: List[str] = []

    def _visit(node: Any, depth: int = 0) -> None:
        ntype = getattr(node, "type", "")
        line_no = _node_line_1based(node)

        if lang in ("javascript", "typescript", "tsx"):
            if ntype == "import_statement":
                src_n = _child_by_field_or_type(node, "source", ("string",))
                if src_n is not None:
                    spec = _node_text(norm_bytes, src_n).strip(_STRING_QUOTES)
                    if spec:
                        imports.append(spec)
                return
            if ntype == "export_statement":
                src_n = _child_by_field_or_type(node, "source", ("string",))
                if src_n is not None:
                    spec = _node_text(norm_bytes, src_n).strip(_STRING_QUOTES)
                    if spec:
                        imports.append(spec)
                for ch in getattr(node, "children", ()) or ():
                    _visit(ch, depth + 1)
                return
            if ntype in ("function_declaration", "generator_function_declaration"):
                nm = _child_by_field_or_type(node, "name", ("identifier",))
                if nm is not None:
                    symbols.append(f"function {_node_text(norm_bytes, nm)} [L{line_no}]")
                return
            if ntype in ("class_declaration", "abstract_class_declaration"):
                nm = _child_by_field_or_type(node, "name", ("type_identifier", "identifier"))
                if nm is not None:
                    symbols.append(f"class {_node_text(norm_bytes, nm)} [L{line_no}]")
                return
            if ntype in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
                kind = ntype.split("_", 1)[0]
                nm = _child_by_field_or_type(node, "name", ("type_identifier", "identifier"))
                if nm is not None:
                    symbols.append(f"{kind} {_node_text(norm_bytes, nm)} [L{line_no}]")
                return

        elif lang == "go":
            if ntype == "import_spec":
                path_n = _child_by_field_or_type(node, "path", ("interpreted_string_literal", "raw_string_literal"))
                if path_n is not None:
                    spec = _node_text(norm_bytes, path_n).strip('"`')
                    if spec:
                        imports.append(spec)
                return
            if ntype == "function_declaration":
                nm = _child_by_field_or_type(node, "name", ("identifier",))
                if nm is not None:
                    symbols.append(f"func {_node_text(norm_bytes, nm)} [L{line_no}]")
                return
            if ntype == "method_declaration":
                nm = _child_by_field_or_type(node, "name", ("field_identifier", "identifier"))
                if nm is not None:
                    symbols.append(f"method {_node_text(norm_bytes, nm)} [L{line_no}]")
                return
            if ntype == "type_spec":
                nm = _child_by_field_or_type(node, "name", ("type_identifier",))
                if nm is not None:
                    symbols.append(f"type {_node_text(norm_bytes, nm)} [L{line_no}]")
                return

        elif lang == "rust":
            if ntype == "use_declaration":
                raw_u = re.sub(r"\s+", " ", _node_text(norm_bytes, node)).strip().rstrip(";")
                m_u = re.match(r"^(?:pub(?:\([^)]*\))?\s+)?use\s+(.+)$", raw_u)
                if m_u:
                    imports.append(m_u.group(1).strip())
                return
            if ntype in ("function_item", "struct_item", "enum_item", "trait_item", "mod_item", "type_item"):
                kind_map = {
                    "function_item": "fn",
                    "struct_item": "struct",
                    "enum_item": "enum",
                    "trait_item": "trait",
                    "mod_item": "mod",
                    "type_item": "type",
                }
                nm = _child_by_field_or_type(node, "name", ("identifier", "type_identifier"))
                if nm is not None:
                    symbols.append(f"{kind_map[ntype]} {_node_text(norm_bytes, nm)} [L{line_no}]")
                if ntype == "mod_item":
                    raw_m = _node_text(norm_bytes, node).strip()
                    if raw_m.endswith(";") and nm is not None:
                        imports.append(f"mod {_node_text(norm_bytes, nm)}")
                return

        elif lang in ("java", "c_sharp"):
            if ntype in ("import_declaration", "using_directive"):
                raw_i = re.sub(r"\s+", " ", _node_text(norm_bytes, node)).strip().rstrip(";")
                if raw_i:
                    imports.append(raw_i)
                return
            if ntype in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "struct_declaration"):
                kind = ntype.split("_", 1)[0]
                nm = _child_by_field_or_type(node, "name", ("identifier",))
                if nm is not None:
                    symbols.append(f"{kind} {_node_text(norm_bytes, nm)} [L{line_no}]")

        elif lang in ("c", "cpp"):
            if ntype == "preproc_include":
                path_n = _child_by_field_or_type(node, "path", ("string_literal", "system_lib_string"))
                if path_n is not None:
                    inc = _node_text(norm_bytes, path_n).strip("\"'<>")
                    if inc:
                        imports.append(inc)
                return
            if ntype in ("class_specifier", "struct_specifier", "enum_specifier"):
                kind = ntype.split("_", 1)[0]
                nm = _child_by_field_or_type(node, "name", ("type_identifier",))
                if nm is not None:
                    symbols.append(f"{kind} {_node_text(norm_bytes, nm)} [L{line_no}]")
                return
            if ntype == "function_definition" and depth <= 2:
                decl = _child_by_field_or_type(node, "declarator", ("function_declarator",))
                if decl is not None:
                    nm = _child_by_field_or_type(decl, "declarator", ("identifier", "field_identifier", "qualified_identifier"))
                    if nm is not None:
                        symbols.append(f"fn {_node_text(norm_bytes, nm)} [L{line_no}]")
                return

        if depth <= 3:
            for ch in getattr(node, "children", ()) or ():
                _visit(ch, depth + 1)

    _visit(root, 0)
    return imports, symbols
