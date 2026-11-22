"""Test impact derivation for `aoci_index` (`--with-tests` & `--tests-for`)."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .constants import AOCI_DIR_NAME, CACHE_DIR, DEFAULT_MAX_BYTES, OUTLINE_CACHE, _is_within_root, normalize_relpath
from .graph import ParsedEntry, parse_entries_map
from .parsers import (
    _expand_rust_use_tree,
    _norm_join_posix,
    _resolve_gomod_for_dir,
    _resolve_tsconfig_for_dir,
    _static_ecosystem,
    extract_file_outline,
)

REASON_RANK: Dict[str, int] = {
    "direct-fras": 0,
    "transitive-fras": 1,
    "static-import": 2,
    "naming-convention": 3,
}

_GENERIC_STEMS: Set[str] = {"__init__", "index", "mod", "main", "lib", "app", "init"}
_PY_EXTS: Set[str] = {".py", ".pyi"}
_JS_EXTS: Set[str] = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".astro"}
_JVM_EXTS: Set[str] = {".java", ".kt", ".kts", ".scala", ".groovy"}
_CS_EXTS: Set[str] = {".cs"}
_RS_EXTS: Set[str] = {".rs"}
_CPP_EXTS: Set[str] = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}
_RB_EXTS: Set[str] = {".rb"}
_PHP_EXTS: Set[str] = {".php"}
_JS_STRIP_EXTS: Tuple[str, ...] = (
    ".tsx",
    ".ts",
    ".jsx",
    ".js",
    ".mts",
    ".cts",
    ".mjs",
    ".cjs",
    ".vue",
    ".svelte",
    ".astro",
)
_SOURCE_ROOT_PREFIXES: Tuple[str, ...] = ("src/", "lib/", "app/")


def _norm_stem(stem: str) -> str:
    return re.sub(r"[_-]+", "", stem).lower()


def _strip_js_ext(path: str) -> str:
    low = path.lower()
    for ext in _JS_STRIP_EXTS:
        if low.endswith(ext):
            return path[: -len(ext)]
    return path


def _same_test_ecosystem(target_ext: str, test_ext: str) -> bool:
    if not target_ext or target_ext == test_ext:
        return True
    for fam in (_PY_EXTS, _JS_EXTS, _JVM_EXTS, _CS_EXTS, _RS_EXTS, _CPP_EXTS, _RB_EXTS, _PHP_EXTS):
        if target_ext in fam and test_ext in fam:
            return True
    return False


def _is_test_filename(bname: str) -> bool:
    low = bname.lower()
    ext = Path(bname).suffix.lower()
    stem = bname[: -len(ext)] if ext else bname
    stem_l = stem.lower()
    if ext == ".py" and (low.startswith("test_") or low.endswith("_test.py")):
        return True
    if low.endswith("_test.go"):
        return True
    if ext == ".rs" and (stem_l.startswith("test_") or stem_l.endswith(("_test", "_tests"))):
        return True
    if ext in _CPP_EXTS and (stem_l.startswith("test_") or stem_l.endswith(("_test", "_unittest"))):
        return True
    if ext == ".rb" and (stem_l.startswith("test_") or stem_l.endswith(("_spec", "_test"))):
        return True
    if ".test." in low or ".spec." in low:
        return True
    if (len(stem) > 4 and stem.endswith(("Test", "Spec"))) or (len(stem) > 5 and stem.endswith(("Tests", "Specs"))):
        return True
    return False


def _build_target_info(target: str) -> Dict[str, Any]:
    parent_dir = target.rsplit("/", 1)[0] if "/" in target else ""
    bname = target.rsplit("/", 1)[-1]
    ext = Path(bname).suffix.lower()
    stem = bname[: -len(ext)] if ext else bname
    no_ext = target[: -len(ext)] if ext else target
    pkg_dir = parent_dir if stem in {"__init__", "index", "mod"} and parent_dir else ""
    pkg_stem = parent_dir.rsplit("/", 1)[-1] if pkg_dir else ""
    return {
        "rel": target,
        "dir": parent_dir,
        "bname": bname,
        "ext": ext,
        "stem": stem,
        "no_ext": no_ext,
        "no_ext_js": _strip_js_ext(target),
        "pkg_dir": pkg_dir,
        "pkg_stem": pkg_stem,
        "norm_stem": _norm_stem(stem),
        "norm_pkg_stem": _norm_stem(pkg_stem) if pkg_stem else "",
        "is_test_target": _is_test_filename(bname),
    }


def _load_outline_cache(root: Path) -> Dict[str, Dict[str, Any]]:
    cache_path = root / AOCI_DIR_NAME / CACHE_DIR / OUTLINE_CACHE
    if not cache_path.is_file():
        return {}
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    if "schema_version" in loaded:
        try:
            if int(loaded.get("schema_version") or 0) != 2:
                return {}
        except (ValueError, TypeError):
            return {}
    files = loaded.get("files")
    return files if isinstance(files, dict) else {}


def is_outline_record_fresh(root: Path, rel: str, rec: Any) -> bool:
    """
    Verify a cached `outline_cache.json` entry still matches the file's `(st_mtime_ns, st_size)` on disk.
    Synthetic test fixtures without `(mtime_ns, bytes)` metadata are treated as fresh.
    """
    if not isinstance(rec, Mapping):
        return False
    if "mtime_ns" not in rec or "bytes" not in rec:
        return True
    full = root / rel
    if not _is_within_root(root, full):
        return False
    try:
        st = os.lstat(full)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    try:
        return st.st_mtime_ns == int(rec["mtime_ns"]) and st.st_size == int(rec["bytes"])
    except (TypeError, ValueError):
        return False


def _extract_cached_imports(rec: Any) -> Tuple[List[str], List[str]]:
    if not isinstance(rec, dict):
        return [], []
    outline = rec.get("outline") if isinstance(rec.get("outline"), dict) else rec
    if not isinstance(outline, dict):
        return [], []
    raw_imp = outline.get("imports")
    raw_py = outline.get("py_from_members")
    imports = [s.strip() for s in raw_imp if isinstance(s, str) and s.strip()] if isinstance(raw_imp, list) else []
    py_members = [s.strip() for s in raw_py if isinstance(s, str) and s.strip()] if isinstance(raw_py, list) else []
    return imports, py_members


def _py_mod_matches_target(mod_slash: str, t_info: Dict[str, Any]) -> bool:
    if not mod_slash:
        return False
    t_no_ext = t_info["no_ext"]
    t_pkg_dir = t_info["pkg_dir"]
    if "/" not in mod_slash:
        if t_info["stem"] not in _GENERIC_STEMS and mod_slash == t_info["stem"]:
            return True
        if t_info["pkg_stem"] and mod_slash == t_info["pkg_stem"]:
            return True
        return False
    if mod_slash == t_no_ext or (t_pkg_dir and mod_slash == t_pkg_dir):
        return True
    for pfx in _SOURCE_ROOT_PREFIXES:
        cand = f"{pfx}{mod_slash}"
        if cand == t_no_ext or (t_pkg_dir and cand == t_pkg_dir):
            return True
    if t_no_ext.endswith("/" + mod_slash) or (t_pkg_dir and t_pkg_dir.endswith("/" + mod_slash)):
        return True
    return False


def _py_import_matches_target(
    test_dir: str,
    imp: str,
    is_from_member: bool,
    t_info: Dict[str, Any],
) -> bool:
    target = t_info["rel"]
    if imp.startswith("."):
        lvl = len(imp) - len(imp.lstrip("."))
        rem = imp.lstrip(".").replace(".", "/")
        pkg_parts = test_dir.split("/") if test_dir else []
        up = max(0, lvl - 1)
        base_parts = pkg_parts[: max(0, len(pkg_parts) - up)] if up else pkg_parts
        stem = ("/".join([*base_parts, rem]) if rem else "/".join(base_parts)).strip("/")
        if stem:
            if f"{stem}.py" == target or f"{stem}/__init__.py" == target:
                return True
            if is_from_member and "/" in stem:
                parent_stem = stem.rsplit("/", 1)[0]
                if f"{parent_stem}.py" == target or f"{parent_stem}/__init__.py" == target:
                    return True
        return False

    mod_slash = imp.replace(".", "/").strip("/")
    if _py_mod_matches_target(mod_slash, t_info):
        return True
    if is_from_member and "/" in mod_slash:
        parent_slash = mod_slash.rsplit("/", 1)[0]
        if _py_mod_matches_target(parent_slash, t_info):
            return True
    return False


def _js_cand_matches_target(cand: str, t_info: Dict[str, Any]) -> bool:
    if not cand:
        return False
    cand_norm = normalize_relpath(cand)
    cand_no_ext = _strip_js_ext(cand_norm)
    return (
        cand_norm == t_info["rel"]
        or cand_no_ext == t_info["no_ext_js"]
        or (bool(t_info["pkg_dir"]) and cand_no_ext == t_info["pkg_dir"])
    )


def _js_import_matches_target(
    test_dir: str,
    spec: str,
    t_info: Dict[str, Any],
    ts_base: str,
    ts_aliases: Sequence[Tuple[str, List[str]]],
) -> bool:
    if spec.startswith("."):
        return _js_cand_matches_target(_norm_join_posix(test_dir, spec), t_info)

    for pat, targets in ts_aliases:
        if pat.endswith("*"):
            pfx = pat[:-1]
            if spec.startswith(pfx):
                tail = spec[len(pfx) :]
                for tgt in targets:
                    mapped = (tgt[:-1] + tail) if tgt.endswith("*") else tgt
                    if _js_cand_matches_target(_norm_join_posix(ts_base, mapped), t_info):
                        return True
        elif spec == pat:
            for tgt in targets:
                if _js_cand_matches_target(_norm_join_posix(ts_base, tgt), t_info):
                    return True

    if ts_base and _js_cand_matches_target(_norm_join_posix(ts_base, spec), t_info):
        return True

    clean_spec = spec
    if clean_spec.startswith(("@/", "~/")):
        clean_spec = clean_spec[2:]
    spec_no_ext = _strip_js_ext(normalize_relpath(clean_spec))
    if not spec_no_ext:
        return False
    if "/" in spec_no_ext:
        if _js_cand_matches_target(spec_no_ext, t_info):
            return True
        for pfx in _SOURCE_ROOT_PREFIXES:
            if _js_cand_matches_target(f"{pfx}{spec_no_ext}", t_info):
                return True
        if t_info["no_ext_js"].endswith("/" + spec_no_ext):
            return True
        if t_info["pkg_dir"] and t_info["pkg_dir"].endswith("/" + spec_no_ext):
            return True
        return False

    if t_info["stem"] not in _GENERIC_STEMS and spec_no_ext == t_info["stem"]:
        return True
    if t_info["pkg_stem"] and spec_no_ext == t_info["pkg_stem"]:
        return True
    return False


def _rs_import_matches_target(test_rel: str, test_dir: str, raw_spec: str, t_info: Dict[str, Any]) -> bool:
    if t_info["ext"] != ".rs":
        return False
    target = t_info["rel"]
    if test_rel.startswith("src/"):
        crate_src = "src"
    elif "/src/" in test_rel:
        crate_src = test_rel.split("/src/", 1)[0] + "/src"
    elif "/tests/" in test_rel:
        crate_src = test_rel.split("/tests/", 1)[0] + "/src"
    else:
        crate_src = "src"
    bname = test_rel.rsplit("/", 1)[-1]
    cur_parts = test_dir.split("/") if test_dir else []

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
        elif "::" in spec:
            # Integration tests (`tests/*.rs`) import via `<crate_name>::<module>...`
            parts = [s for s in spec.split("::") if s]
            if len(parts) >= 2:
                base_parts = crate_src.split("/")
                segs = parts[1:]
        if base_parts is None or not segs:
            continue
        for cut in range(len(segs), 0, -1):
            stem = "/".join([*base_parts, *segs[:cut]])
            if f"{stem}.rs" == target or f"{stem}/mod.rs" == target:
                return True
    return False


def _jvm_import_matches_target(imp: str, t_info: Dict[str, Any]) -> bool:
    s = imp.strip().rstrip(";").strip()
    for pfx in ("import static ", "import ", "using static ", "using "):
        if s.startswith(pfx):
            s = s[len(pfx) :].strip()
            break
    if "=" in s:
        s = s.split("=", 1)[1].strip()
    if " as " in s:
        s = s.split(" as ", 1)[0].strip()
    if not s:
        return False
    if s.endswith(".*"):
        pkg_slash = s[:-2].replace(".", "/").strip("/")
        return bool(pkg_slash) and (t_info["dir"] == pkg_slash or t_info["dir"].endswith("/" + pkg_slash))
    cls_slash = s.replace(".", "/").strip("/")
    if not cls_slash:
        return False
    if "/" not in cls_slash:
        return t_info["stem"] not in _GENERIC_STEMS and cls_slash == t_info["stem"]
    if t_info["no_ext"] == cls_slash or t_info["no_ext"].endswith("/" + cls_slash):
        return True
    parent_slash = cls_slash.rsplit("/", 1)[0]
    if t_info["no_ext"] == parent_slash or t_info["no_ext"].endswith("/" + parent_slash):
        return True
    return False


def _test_imports_match_target(
    test_rel: str,
    imports: Sequence[str],
    py_members: Sequence[str],
    t_info: Dict[str, Any],
    go_mod: str,
    ts_base: str,
    ts_aliases: Sequence[Tuple[str, List[str]]],
) -> bool:
    if test_rel == t_info["rel"]:
        return False
    eco = _static_ecosystem(test_rel)
    test_dir = test_rel.rsplit("/", 1)[0] if "/" in test_rel else ""
    test_ext = Path(test_rel).suffix.lower()

    if eco == "py":
        for imp in imports:
            if _py_import_matches_target(test_dir, imp, False, t_info):
                return True
        for imp in py_members:
            if _py_import_matches_target(test_dir, imp, True, t_info):
                return True
        return False

    if eco == "js":
        for spec in imports:
            if _js_import_matches_target(test_dir, spec, t_info, ts_base, ts_aliases):
                return True
        return False

    if eco == "go":
        if t_info["ext"] != ".go":
            return False
        t_dir = t_info["dir"]
        for spec in imports:
            if go_mod and (spec == go_mod or spec.startswith(go_mod + "/")):
                sub_dir = spec[len(go_mod) :].lstrip("/")
                if sub_dir == t_dir or t_dir.endswith("/" + sub_dir):
                    return True
            elif t_dir and (spec == t_dir or spec.endswith("/" + t_dir)):
                return True
        return False

    if eco == "rs":
        for raw_spec in imports:
            if _rs_import_matches_target(test_rel, test_dir, raw_spec, t_info):
                return True
        return False

    if test_ext in {".java", ".kt", ".kts", ".cs"}:
        for imp in imports:
            if _jvm_import_matches_target(imp, t_info):
                return True
        return False

    for imp in imports:
        clean = normalize_relpath(imp.strip("\"'<> "))
        if not clean:
            continue
        if clean == t_info["rel"] or clean == t_info["no_ext"] or t_info["rel"].endswith("/" + clean):
            return True
        if "/" not in clean and t_info["stem"] not in _GENERIC_STEMS and clean == t_info["stem"]:
            return True
        clean_bname = clean.rsplit("/", 1)[-1]
        clean_stem = clean_bname.rsplit(".", 1)[0] if "." in clean_bname else clean_bname
        if t_info["stem"] not in _GENERIC_STEMS and clean_stem == t_info["stem"]:
            if "/" not in clean or t_info["no_ext"].endswith("/" + clean.rsplit(".", 1)[0]):
                return True
    return False


def _matches_naming_convention(test_rel: str, t_info: Dict[str, Any]) -> bool:
    if test_rel == t_info["rel"] or t_info["is_test_target"]:
        return False
    test_dir = test_rel.rsplit("/", 1)[0] if "/" in test_rel else ""
    test_bname = test_rel.rsplit("/", 1)[-1]
    test_bname_l = test_bname.lower()
    test_ext = Path(test_bname).suffix.lower()
    test_stem = test_bname[: -len(test_ext)] if test_ext else test_bname
    test_stem_l = test_stem.lower()
    test_dir_parts = [seg.lower() for seg in test_dir.split("/") if seg]

    if t_info["ext"] == ".go" and test_bname_l.endswith("_test.go") and test_dir == t_info["dir"]:
        return True

    stems: List[Tuple[str, str]] = []
    if t_info["stem"] and t_info["stem"].lower() not in _GENERIC_STEMS:
        stems.append((t_info["stem"], t_info["norm_stem"]))
    elif t_info["pkg_stem"]:
        stems.append((t_info["pkg_stem"], t_info["norm_pkg_stem"]))
        if test_dir == t_info["dir"] and t_info["stem"]:
            stems.append((t_info["stem"], t_info["norm_stem"]))
    if not stems:
        return False

    if not _same_test_ecosystem(t_info["ext"], test_ext):
        return False

    for raw_stem, norm_target_stem in stems:
        stem_l = raw_stem.lower()
        if test_ext == ".py" and (test_bname_l == f"test_{stem_l}.py" or test_bname_l == f"{stem_l}_test.py"):
            return True
        if test_ext == ".rs" and (
            test_bname_l in (f"{stem_l}_test.rs", f"{stem_l}_tests.rs", f"test_{stem_l}.rs")
            or ("tests" in test_dir_parts and test_bname_l == f"{stem_l}.rs")
        ):
            return True
        if test_ext in _CPP_EXTS and test_stem_l in (
            f"{stem_l}_test",
            f"{stem_l}_unittest",
            f"test_{stem_l}",
        ):
            return True
        if test_ext == ".rb" and test_bname_l in (f"{stem_l}_spec.rb", f"test_{stem_l}.rb", f"{stem_l}_test.rb"):
            return True
        if test_ext == ".php" and len(test_stem) > 4 and test_stem.endswith("Test"):
            if _norm_stem(test_stem[:-4]) == norm_target_stem:
                return True
        if test_ext and (
            test_bname_l.startswith(f"{stem_l}.test.") or test_bname_l.startswith(f"{stem_l}.spec.")
        ):
            return True
        if len(test_stem) > 4 and test_stem.endswith(("Test", "Spec")):
            if _norm_stem(test_stem[:-4]) == norm_target_stem:
                return True
        if len(test_stem) > 5 and test_stem.endswith(("Tests", "Specs")):
            if _norm_stem(test_stem[:-5]) == norm_target_stem:
                return True
    return False


def _extract_live_test_imports(root: Path, test_rel: str) -> Tuple[List[str], List[str]]:
    full = root / test_rel
    if not _is_within_root(root, full):
        return [], []
    try:
        st = os.lstat(full)
    except OSError:
        return [], []
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > DEFAULT_MAX_BYTES:
        return [], []
    try:
        outline = extract_file_outline(full, test_rel, max_items=200)
    except OSError:
        return [], []
    return _extract_cached_imports(outline)


def _candidate_norm_stems_for_test(test_rel: str) -> Tuple[str, Optional[str], Set[str]]:
    """
    Extract `(test_dir, go_test_dir_if_go, candidate_norm_target_stems)` for `O(1)` naming-convention bucketing.
    Every test that `_matches_naming_convention` can accept maps to either `go_test_dir_if_go` or at least one
    normalized stem in `candidate_norm_target_stems`.
    """
    test_dir = test_rel.rsplit("/", 1)[0] if "/" in test_rel else ""
    test_bname = test_rel.rsplit("/", 1)[-1]
    test_bname_l = test_bname.lower()
    test_ext = Path(test_bname).suffix.lower()
    test_stem = test_bname[: -len(test_ext)] if test_ext else test_bname
    test_stem_l = test_stem.lower()
    go_dir: Optional[str] = test_dir if test_bname_l.endswith("_test.go") else None

    cands: Set[str] = set()
    if test_stem_l.startswith("test_"):
        ns = _norm_stem(test_stem_l[5:])
        if ns:
            cands.add(ns)
    for sfx in ("_test", "_tests", "_unittest", "_spec"):
        if test_stem_l.endswith(sfx):
            ns = _norm_stem(test_stem_l[: -len(sfx)])
            if ns:
                cands.add(ns)
    if test_ext == ".rs":
        test_dir_parts = [seg.lower() for seg in test_dir.split("/") if seg]
        if "tests" in test_dir_parts:
            ns = _norm_stem(test_stem_l)
            if ns:
                cands.add(ns)
    for marker in (".test.", ".spec."):
        start = 0
        while True:
            idx = test_bname_l.find(marker, start)
            if idx == -1:
                break
            if idx > 0:
                ns = _norm_stem(test_bname_l[:idx])
                if ns:
                    cands.add(ns)
            start = idx + 1
    if len(test_stem) > 4 and test_stem.endswith(("Test", "Spec")):
        ns = _norm_stem(test_stem[:-4])
        if ns:
            cands.add(ns)
    if len(test_stem) > 5 and test_stem.endswith(("Tests", "Specs")):
        ns = _norm_stem(test_stem[:-5])
        if ns:
            cands.add(ns)
    return test_dir, go_dir, cands


class TestImpactIndex:
    """
    Pre-indexed `T`-layer naming-convention buckets and static-import records for sub-millisecond
    repeated `find_impacted_tests` queries (e.g. inside `aoci_mcp` or multi-target scans).
    """

    def __init__(
        self,
        root: Path,
        parsed_entries: Mapping[str, ParsedEntry],
        *,
        preloaded_outline_files: Optional[Dict[str, Dict[str, Any]]] = None,
        live_imports: bool = False,
    ) -> None:
        self.root = root
        self.live_imports = live_imports
        self.test_entries: Dict[str, ParsedEntry] = {
            rel: pe for rel, pe in parsed_entries.items() if pe.layer == "T"
        }
        self.sorted_tests: List[str] = sorted(self.test_entries.keys())
        self.go_tests_by_dir: Dict[str, List[str]] = {}
        self.tests_by_norm_stem: Dict[str, List[str]] = {}

        for test_rel in self.sorted_tests:
            _, go_dir, cands = _candidate_norm_stems_for_test(test_rel)
            if go_dir is not None:
                self.go_tests_by_dir.setdefault(go_dir, []).append(test_rel)
            for ns in cands:
                self.tests_by_norm_stem.setdefault(ns, []).append(test_rel)

        self.static_records: List[
            Tuple[str, Optional[str], str, Tuple[str, ...], Tuple[str, ...], str, str, Tuple[Tuple[str, List[str]], ...]]
        ] = []
        cache_files = preloaded_outline_files if preloaded_outline_files is not None else _load_outline_cache(root)
        if cache_files or live_imports:
            go_cache: Dict[str, Tuple[str, str]] = {}
            ts_cache: Dict[str, Tuple[str, List[Tuple[str, List[str]]]]] = {}
            for test_rel in self.sorted_tests:
                cached_rec = cache_files.get(test_rel) if cache_files else None
                if cached_rec is not None and (
                    not live_imports or is_outline_record_fresh(root, test_rel, cached_rec)
                ):
                    imports, py_members = _extract_cached_imports(cached_rec)
                elif live_imports:
                    imports, py_members = _extract_live_test_imports(root, test_rel)
                else:
                    imports, py_members = [], []
                if not imports and not py_members:
                    continue
                eco = _static_ecosystem(test_rel)
                test_dir = test_rel.rsplit("/", 1)[0] if "/" in test_rel else ""
                go_mod_str = ""
                ts_base = ""
                ts_aliases: Tuple[Tuple[str, List[str]], ...] = ()
                if eco == "go":
                    _, go_mod_str = _resolve_gomod_for_dir(root, test_dir, go_cache)
                elif eco == "js":
                    ts_base, ts_aliases_list = _resolve_tsconfig_for_dir(root, test_dir, ts_cache)
                    ts_aliases = tuple(ts_aliases_list)
                self.static_records.append(
                    (
                        test_rel,
                        eco,
                        test_dir,
                        tuple(imports),
                        tuple(py_members),
                        go_mod_str,
                        ts_base,
                        ts_aliases,
                    )
                )

    def query(
        self,
        dependents_adj: Dict[str, Set[str]],
        target_paths: Sequence[str],
        max_depth: int = 3,
    ) -> List[Dict[str, Any]]:
        targets = list(dict.fromkeys(normalize_relpath(str(p)) for p in target_paths if str(p).strip()))
        if not targets or not self.test_entries:
            return []

        best: Dict[str, Tuple[int, int, str, str]] = {}

        def _record(test_path: str, reason: str, hop: int, via: str) -> None:
            rank = REASON_RANK[reason]
            cand = (rank, hop, via, reason)
            prev = best.get(test_path)
            if prev is None or (rank, hop, via) < (prev[0], prev[1], prev[2]):
                best[test_path] = cand

        # 1) FRAS Graph T-Layer BFS
        depth_limit = max(0, int(max_depth))
        if depth_limit >= 1:
            visited: Set[str] = set(targets)
            frontier: List[str] = []
            for cur in targets:
                for nb in sorted(dependents_adj.get(cur, ())):
                    if nb == cur:
                        continue
                    if nb in self.test_entries:
                        _record(nb, "direct-fras", 1, cur)
                    if nb not in visited:
                        visited.add(nb)
                        frontier.append(nb)
            for hop in range(2, depth_limit + 1):
                if not frontier:
                    break
                nxt: List[str] = []
                for cur in frontier:
                    for nb in sorted(dependents_adj.get(cur, ())):
                        if nb not in visited:
                            visited.add(nb)
                            nxt.append(nb)
                            if nb in self.test_entries:
                                _record(nb, "transitive-fras", hop, cur)
                frontier = nxt

        target_infos = [_build_target_info(t) for t in targets]

        # 2) Pre-parsed Static Import Fallback
        if self.static_records:
            has_go_target = any(ti["ext"] == ".go" for ti in target_infos)
            has_rs_target = any(ti["ext"] == ".rs" for ti in target_infos)
            for test_rel, eco, _test_dir, imports, py_members, go_mod_str, ts_base, ts_aliases in self.static_records:
                if eco == "go" and not has_go_target:
                    continue
                if eco == "rs" and not has_rs_target:
                    continue
                for t_info in target_infos:
                    if _test_imports_match_target(
                        test_rel,
                        imports,
                        py_members,
                        t_info,
                        go_mod_str,
                        ts_base,
                        ts_aliases,
                    ):
                        _record(test_rel, "static-import", 1, t_info["rel"])

        # 3) Bucketed Multi-language Naming Conventions (`O(|targets|)` candidate lookup)
        for t_info in target_infos:
            if t_info["is_test_target"]:
                continue
            cand_tests: Dict[str, None] = {}
            if t_info["ext"] == ".go":
                for tr in self.go_tests_by_dir.get(t_info["dir"], ()):
                    cand_tests[tr] = None
            if t_info["stem"] and t_info["stem"].lower() not in _GENERIC_STEMS:
                for tr in self.tests_by_norm_stem.get(t_info["norm_stem"], ()):
                    cand_tests[tr] = None
            elif t_info["pkg_stem"]:
                for tr in self.tests_by_norm_stem.get(t_info["norm_pkg_stem"], ()):
                    cand_tests[tr] = None
                if t_info["norm_stem"]:
                    for tr in self.tests_by_norm_stem.get(t_info["norm_stem"], ()):
                        cand_tests[tr] = None
            for test_rel in cand_tests:
                if _matches_naming_convention(test_rel, t_info):
                    _record(test_rel, "naming-convention", 1, t_info["rel"])

        results: List[Dict[str, Any]] = []
        for test_path, (_, hop, via, reason) in best.items():
            pe = self.test_entries[test_path]
            results.append(
                {
                    "path": test_path,
                    "reason": reason,
                    "hop": hop,
                    "importance": pe.imp,
                    "via": via,
                    "entry": pe.raw,
                }
            )

        results.sort(key=lambda item: (REASON_RANK[item["reason"]], item["hop"], -item["importance"], item["path"]))
        return results


def find_impacted_tests(
    root: Path,
    entries: Dict[str, str],
    dependents_adj: Dict[str, Set[str]],
    target_paths: Sequence[str],
    max_depth: int = 3,
    *,
    parsed_entries: Optional[Mapping[str, ParsedEntry]] = None,
    preloaded_outline_files: Optional[Dict[str, Dict[str, Any]]] = None,
    live_imports: bool = False,
    test_index: Optional[TestImpactIndex] = None,
) -> List[Dict[str, Any]]:
    """
    Derive impacted `T`-layer tests for `target_paths` by combining:
      1) FRAS graph `dependents_adj` BFS up to `max_depth` (`direct-fras`, `transitive-fras`)
      2) Warm `.aoci/.cache/outline_cache.json` (or opt-in read-only `live_imports`) static import fallback (`static-import`)
      3) Multi-language test naming conventions (`naming-convention`)
    """
    targets = list(dict.fromkeys(normalize_relpath(str(p)) for p in target_paths if str(p).strip()))
    if not targets or not entries:
        return []

    if test_index is not None and not live_imports and preloaded_outline_files is None:
        return test_index.query(dependents_adj, targets, max_depth=max_depth)

    parsed = parsed_entries if parsed_entries is not None else parse_entries_map(entries)
    if not any(pe.layer == "T" for pe in parsed.values()):
        return []
    idx = TestImpactIndex(
        root,
        parsed,
        preloaded_outline_files=preloaded_outline_files,
        live_imports=live_imports,
    )
    return idx.query(dependents_adj, targets, max_depth=max_depth)
