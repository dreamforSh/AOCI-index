#!/usr/bin/env python3
"""v1.5.1 P0 regressions: project-scope installs must never self-index; tree-sitter must be thread-safe and lossless."""

from __future__ import annotations

import concurrent.futures
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
import sys  # noqa: E402

sys.dont_write_bytecode = True
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from aoci_testkit import RepoTestCase, cli_json  # noqa: E402
from aoci_core import constants, ignore, installer, parsers, ts_accel  # noqa: E402


def _added_paths(root: Path, *extra: str) -> List[str]:
    data = cli_json("scan", "--root", str(root), *extra)
    return sorted(x["path"] for x in data["drift"]["added"])


class InstalledSkillSelfIndexTests(RepoTestCase):
    def test_path_skip_reason_covers_every_project_scope_host_dir(self):
        skipped = [
            ".codex/hooks.json",
            ".codex/config.toml",
            ".cursor/skills/aoci-index/scripts/aoci_index.py",
            ".cursor/rules/aoci-index.mdc",
            ".windsurf/skills/aoci-index/SKILL.md",
            ".copilot/skills/aoci-index/scripts/aoci_mcp.py",
            ".agents/skills/aoci-index/scripts/aoci_hook.py",
            ".github/skills/aoci-index/scripts/aoci_core/parsers.py",
            ".github/skills/aoci-index/SKILL.md",
            "vendor/.aoci-install.json",
        ]
        for rel in skipped:
            self.assertIsNotNone(ignore.path_skip_reason(rel, None), rel)
        self.assertEqual(ignore.path_skip_reason(".github/skills/aoci-index/SKILL.md", None), "installed-skill")

        kept = [
            ".github/workflows/ci.yml",
            ".github/scripts/build_release.py",
            ".github/copilot-instructions.md",
            "skills/aoci-index/README.md",  # not under a hidden host dir -> real project content
            "src/skills/aoci-index.py",
        ]
        for rel in kept:
            self.assertIsNone(ignore.path_skip_reason(rel, None), rel)
        self.assertEqual(constants.INSTALL_MANIFEST_NAME, installer.INSTALL_MANIFEST)

    def test_project_scope_install_for_all_hosts_is_never_indexed(self):
        self.write("app.py", "X = 1\n")
        with tempfile.TemporaryDirectory() as td:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                rc = installer.main(
                    [
                        "--host", "all", "--scope", "project",
                        "--project-root", str(self.root), "--home-dir", td,
                        "--with-hooks", "--with-rules", "--with-mcp",
                    ]
                )
            self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        self.assertTrue((self.root / ".github" / "skills" / "aoci-index" / "SKILL.md").is_file())
        self.assertTrue((self.root / ".cursor" / "skills" / "aoci-index" / "SKILL.md").is_file())
        self.assertTrue((self.root / ".windsurf" / "skills" / "aoci-index" / "SKILL.md").is_file())

        self.init()
        for extra in ((), ("--git-fast",), ("--git-fast",)):
            added = _added_paths(self.root, *extra)
            self.assertIn("app.py", added)
            leaked = [p for p in added if "aoci-index/" in p or p.split("/", 1)[0] in constants.ALWAYS_SKIP_DIRS]
            self.assertEqual(leaked, [], f"installed AOCI copy leaked into scan ({extra}): {leaked}")
            self.assertFalse(any(p.endswith((".py",)) and p != "app.py" for p in added), added)

    def test_manifest_marked_vendored_copy_is_skipped_in_full_and_git_fast_scans(self):
        self.write("src/a.py", "A = 1\n")
        self.write("tools/aoci/.aoci-install.json", "{}\n")
        self.write("tools/aoci/scripts/aoci_index.py", "print('aoci')\n")
        self.init()
        self.assertEqual(_added_paths(self.root), ["src/a.py"])
        self.assertEqual(_added_paths(self.root, "--git-fast"), ["src/a.py"])
        self.assertEqual(_added_paths(self.root, "--git-fast"), ["src/a.py"])

        # A second copy appearing later is picked up by the git-fast path too (manifest change -> full rediscovery)
        self.write("third_party/aoci2/.aoci-install.json", "{}\n")
        self.write("third_party/aoci2/scripts/aoci_mcp.py", "print('mcp')\n")
        self.assertEqual(_added_paths(self.root, "--git-fast"), ["src/a.py"])
        self.assertEqual(_added_paths(self.root, "--git-fast"), ["src/a.py"])

        # Removing the manifest turns the directory back into ordinary project content
        (self.root / "tools" / "aoci" / ".aoci-install.json").unlink()
        self.assertIn("tools/aoci/scripts/aoci_index.py", _added_paths(self.root, "--git-fast"))


class _Node:
    """Minimal stand-in for `tree_sitter.Node` (type, byte span, start_point, children, fields)."""

    def __init__(self, src: bytes, ntype: str, sub: Optional[str] = None, after: int = 0,
                 children: Optional[List["_Node"]] = None, fields: Optional[Dict[str, "_Node"]] = None,
                 has_error: bool = False):
        self.type = ntype
        if sub is None:
            self.start_byte, self.end_byte = 0, len(src)
        else:
            idx = src.index(sub.encode("utf-8"), after)
            self.start_byte, self.end_byte = idx, idx + len(sub.encode("utf-8"))
        self.start_point = (src.count(b"\n", 0, self.start_byte), 0)
        self.children = children or []
        self._fields = fields or {}
        self.has_error = has_error

    def child_by_field_name(self, name: str) -> Optional["_Node"]:
        return self._fields.get(name)


class _FakeParser:
    def __init__(self, root: _Node):
        self._root = root

    def parse(self, _b: bytes) -> Any:
        return type("FakeTree", (), {"root_node": self._root})()


class TreeSitterAcceleratorTests(unittest.TestCase):
    def setUp(self) -> None:
        ts_accel.reset_treesitter_probe()
        self._env = mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        ts_accel.reset_treesitter_probe()

    def test_parsers_are_thread_local_never_shared_across_threads(self):
        created: List[Any] = []
        lock = threading.Lock()

        def factory(_lang: str) -> Any:
            obj = object()
            with lock:
                created.append((threading.get_ident(), obj))
            return obj

        barrier = threading.Barrier(8)

        def worker() -> Any:
            barrier.wait()
            first = ts_accel._get_parser("typescript")
            again = ts_accel._get_parser("typescript")
            assert first is again
            return threading.get_ident(), first

        with mock.patch.object(ts_accel, "_probe_backend", return_value=factory):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = [f.result() for f in [pool.submit(worker) for _ in range(8)]]
        parser_ids = {id(p) for _, p in results}
        self.assertEqual(len(parser_ids), len({tid for tid, _ in results}))
        self.assertEqual(len(created), len(parser_ids))  # exactly one parser constructed per thread

    def test_backend_language_aliases_and_negative_cache(self):
        calls: List[str] = []

        def factory(lang: str) -> Any:
            calls.append(lang)
            if lang == "csharp":
                return "cs-parser"
            raise LookupError(lang)

        with mock.patch.object(ts_accel, "_probe_backend", return_value=factory):
            self.assertEqual(ts_accel._get_parser("c_sharp"), "cs-parser")
            self.assertIsNone(ts_accel._get_parser("go"))
            t = threading.Thread(target=lambda: ts_accel._get_parser("go"))
            t.start()
            t.join()
        self.assertEqual(calls.count("go"), 1)  # missing grammar is not retried per worker thread

    def test_syntax_error_tree_falls_back_to_regex_path(self):
        src = b"export const App = () => 1;\n"
        root = _Node(src, "program", has_error=True)
        with mock.patch.object(ts_accel, "_probe_backend", return_value=lambda _l: _FakeParser(root)):
            self.assertIsNone(ts_accel.extract_outline_with_treesitter(src, ".ts"))
            out = parsers._extract_outline_from_bytes(src, ".ts", "app.ts")
        self.assertIn("const App [L1]", out["symbols"])

    def test_js_exported_consts_cjs_require_dynamic_import_and_reexports(self):
        src = (
            b"const fs = require('fs');\n"
            b"export const App = () => 1;\n"
            b"export { a, b as c } from './re';\n"
            b"exports.helper = 1;\n"
            b"load(import('./lazy'));\n"
        )
        s = src.decode()
        # L1: const fs = require('fs');
        req_str = _Node(src, "string", "'fs'")
        req_args = _Node(src, "arguments", "('fs')", children=[_Node(src, "(", "("), req_str, _Node(src, ")", ")", s.index("'fs'"))])
        req_fn = _Node(src, "identifier", "require")
        req_call = _Node(src, "call_expression", "require('fs')", children=[req_fn, req_args],
                         fields={"function": req_fn, "arguments": req_args})
        fs_id = _Node(src, "identifier", "fs")
        decl1 = _Node(src, "variable_declarator", "fs = require('fs')", children=[fs_id, req_call], fields={"name": fs_id})
        lex1 = _Node(src, "lexical_declaration", "const fs = require('fs');", children=[_Node(src, "const", "const"), decl1])
        # L2: export const App = () => 1;
        l2 = s.index("export const")
        app_id = _Node(src, "identifier", "App")
        decl2 = _Node(src, "variable_declarator", "App = () => 1", children=[app_id], fields={"name": app_id})
        lex2 = _Node(src, "lexical_declaration", "const App = () => 1;", l2, children=[_Node(src, "const", "const", l2), decl2])
        exp2 = _Node(src, "export_statement", "export const App = () => 1;", children=[_Node(src, "export", "export"), lex2])
        # L3: export { a, b as c } from './re';
        l3 = s.index("export {")
        spec_a = _Node(src, "export_specifier", "a", l3 + 8, children=[_Node(src, "identifier", "a", l3 + 8)],
                       fields={"name": _Node(src, "identifier", "a", l3 + 8)})
        b_id, c_id = _Node(src, "identifier", "b", l3 + 10), _Node(src, "identifier", "c", l3 + 15)
        spec_bc = _Node(src, "export_specifier", "b as c", l3, children=[b_id, c_id], fields={"name": b_id, "alias": c_id})
        clause = _Node(src, "export_clause", "{ a, b as c }", children=[spec_a, spec_bc])
        re_str = _Node(src, "string", "'./re'")
        exp3 = _Node(src, "export_statement", "export { a, b as c } from './re';", children=[clause, re_str],
                     fields={"source": re_str})
        # L4: exports.helper = 1;
        obj = _Node(src, "identifier", "exports", s.index("exports.helper"))
        prop = _Node(src, "property_identifier", "helper")
        member = _Node(src, "member_expression", "exports.helper", children=[obj, prop], fields={"object": obj, "property": prop})
        assign = _Node(src, "assignment_expression", "exports.helper = 1", children=[member], fields={"left": member})
        stmt4 = _Node(src, "expression_statement", "exports.helper = 1;", children=[assign])
        # L5: load(import('./lazy'));
        lazy_str = _Node(src, "string", "'./lazy'")
        imp_kw = _Node(src, "import", "import", s.index("import("))
        lazy_args = _Node(src, "arguments", "('./lazy')", children=[lazy_str])
        dyn_call = _Node(src, "call_expression", "import('./lazy')", children=[imp_kw, lazy_args],
                         fields={"function": imp_kw, "arguments": lazy_args})
        stmt5 = _Node(src, "expression_statement", "load(import('./lazy'));", children=[dyn_call])

        root = _Node(src, "program", children=[lex1, exp2, exp3, stmt4, stmt5])
        with mock.patch.object(ts_accel, "_probe_backend", return_value=lambda _l: _FakeParser(root)):
            imports, symbols = ts_accel.extract_outline_with_treesitter(src, ".ts")
        self.assertEqual(imports, ["fs", "./re", "./lazy"])
        self.assertEqual(symbols, ["const App [L2]", "export a [L3]", "export c [L3]", "export helper [L4]"])

        # Parity: everything the zero-dependency regex path reports must also be reported by tree-sitter
        with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "1"}, clear=False):
            regex_out = parsers._extract_outline_from_bytes(src, ".ts", "x.ts", max_items=100, max_imports=0)
        self.assertEqual([i for i in regex_out["imports"] if i not in imports], [])
        self.assertEqual([x for x in regex_out["symbols"] if x not in symbols], [])

    def test_outline_cache_is_tagged_and_rejects_other_extractor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            aoci_dir = root / ".aoci"
            (aoci_dir / ".cache").mkdir(parents=True)
            src = root / "m.ts"
            src.write_text("export const Real = 1;\n", encoding="utf-8")
            st = src.stat()
            stale = {"path": "m.ts", "imports": [], "symbols": ["const Stale [L1]"], "lines": 1}
            cache_file = aoci_dir / ".cache" / constants.OUTLINE_CACHE
            cache_file.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "extractor": "ts1",
                        "files": {"m.ts": {"mtime_ns": st.st_mtime_ns, "bytes": st.st_size, "outline": stale}},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "1"}, clear=False):
                _, _, outlines = parsers.build_static_import_graph(root, ["m.ts"], aoci_dir=aoci_dir)
            self.assertIn("const Real [L1]", outlines["m.ts"]["symbols"])
            saved = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["extractor"], "regex")


def _real_backend_available() -> bool:
    ts_accel.reset_treesitter_probe()
    try:
        with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False):
            return ts_accel.is_enabled() and ts_accel._get_parser("typescript") is not None
    except Exception:
        return False
    finally:
        ts_accel.reset_treesitter_probe()


_PARITY_FIXTURES: Dict[str, bytes] = {
    "app.tsx": (
        b"import React from 'react';\nimport './side.css';\nconst lodash = require('lodash');\n"
        b"export const App = () => <div/>;\nexport let mutable = 1;\nexport { a, b as c } from './re';\n"
        b"export * as ns from './ns';\nexport { localA, localB as lb };\n"
        b"export default function Main() { return null; }\n"
        b"export async function loader() { return import('./lazy'); }\nfunction helper() {}\nclass Plain {}\n"
        b"export interface Iface { x: number }\nexport type Alias = string;\nexport enum Color { Red }\n"
        b"exports.cjsA = 1;\nmodule.exports.cjsB = 2;\n"
    ),
    "lib.go": b'package lib\n\nimport (\n\t"fmt"\n\talias "os"\n)\n\ntype S struct{}\ntype I interface{}\nfunc Top() {}\nfunc (s *S) Meth() {}\n',
    "lib.rs": (
        b"use std::io;\nmod inline_mod {\n    pub fn in_mod() {}\n}\nmod ext;\npub struct P;\nenum E { A }\n"
        b"impl P {\n    pub fn method_a(&self) {}\n}\ntrait T {\n    fn sig(&self);\n}\npub async fn top() {}\n"
    ),
    "Outer.java": b"package p;\nimport java.util.List;\npublic class Outer {\n  class Inner {}\n}\nenum E { A }\nrecord R(int x) {}\n",
    "Service.kt": (
        b"package com.app\nimport foo.Bar\nimport baz.*\nclass User(val id: Int) {\n  fun greet(): String = \"hi\"\n  class Inner\n}\n"
        b"interface Repo {\n  suspend fun find(id: Int): User?\n}\nobject Singleton\nenum class Status { OK, ERR }\n"
        b"data class Dto(val x: Int)\nsealed interface Event\nfun topFn() {}\n"
    ),
    "Prog.cs": b"using System;\nnamespace A.B {\n  namespace C {\n    public class Outer { public class Inner {} }\n  }\n}\npublic struct S {}\n",
    "w.hpp": (
        b'#pragma once\n#include "a.h"\n#include <vector>\nnamespace outer {\nnamespace inner {\nclass Widget {\npublic:\n'
        b"  void m();\n};\n}\n}\nint *ptr_fn(int a);\nvoid plain(void);\nenum class Color { Red };\n"
    ),
    "w.cpp": b'#include "w.hpp"\nvoid Widget::m() {}\nWidget::~Widget() {}\nstatic int helper(int x) { return x; }\n',
}


@unittest.skipUnless(_real_backend_available(), "tree-sitter backend not installed (pip install aoci-index[treesitter])")
class TreeSitterRealBackendParityTests(unittest.TestCase):
    def setUp(self) -> None:
        ts_accel.reset_treesitter_probe()

    def tearDown(self) -> None:
        ts_accel.reset_treesitter_probe()

    def _outline(self, name: str, src: bytes, ts_on: bool) -> Dict[str, Any]:
        with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0" if ts_on else "1"}, clear=False):
            return parsers._extract_outline_from_bytes(src, Path(name).suffix, name, max_items=200, max_imports=0)

    def test_treesitter_output_is_superset_of_regex_output(self):
        for name, src in _PARITY_FIXTURES.items():
            with self.subTest(name=name):
                with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False):
                    self.assertIsNotNone(ts_accel.extract_outline_with_treesitter(src, Path(name).suffix), name)
                rx = self._outline(name, src, False)
                ts = self._outline(name, src, True)
                self.assertEqual([i for i in rx["imports"] if i not in ts["imports"]], [], name)
                self.assertEqual([s for s in rx["symbols"] if s not in ts["symbols"]], [], name)

    def test_concurrent_extraction_is_deterministic(self):
        expected = {name: self._outline(name, src, True) for name, src in _PARITY_FIXTURES.items()}
        jobs = list(_PARITY_FIXTURES.items()) * 40
        with mock.patch.dict(os.environ, {"AOCI_DISABLE_TREESITTER": "0"}, clear=False):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futs = [
                    (name, pool.submit(parsers._extract_outline_from_bytes, src, Path(name).suffix, name, 200, None, 0))
                    for name, src in jobs
                ]
                for name, fut in futs:
                    got = fut.result()
                    self.assertEqual(got["symbols"], expected[name]["symbols"], name)
                    self.assertEqual(got["imports"], expected[name]["imports"], name)


if __name__ == "__main__":
    unittest.main()
