#!/usr/bin/env python3
"""Static outline / import parsers across languages and `sig_hash` payloads."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    aoci_index,
    cli_json,
)


class OutlineTests(RepoTestCase):
    def test_js_ts_multiline_imports_reexports_and_comments(self):
        ts_src = (
            "// import ghost from './ghost'\n"
            "/*\n"
            "import blockGhost from './block-ghost'\n"
            "export function commentedOut() {}\n"
            "*/\n"
            "import {\n"
            "  signToken,\n"
            "  type JwtPayload,\n"
            "} from '../auth/jwt'\n"
            "import defaultCfg, {\n"
            "  loadEnv,\n"
            "} from './config/env'\n"
            "export {\n"
            "  createOrder,\n"
            "  cancelOrder as revokeOrder,\n"
            "} from './order/service'\n"
            "const legacy = require('./legacy/cjs')\n"
            "export async function bootApp() {\n"
            "  return import('./lazy/panel')\n"
            "}\n"
        )
        self.write("src/index.ts", ts_src)
        data = cli_json("outline", "--root", str(self.root), "src/index.ts")
        item = data["outlines"][0]
        self.assertEqual(
            item["imports"],
            ["../auth/jwt", "./config/env", "./order/service", "./legacy/cjs", "./lazy/panel"],
        )
        syms = " | ".join(item["symbols"])
        self.assertIn("export createOrder [L13]", syms)
        self.assertIn("export revokeOrder [L13]", syms)
        self.assertIn("function bootApp [L18]", syms)
        self.assertNotIn("commentedOut", syms)

    def test_vue_and_svelte_sfc_outline_preserves_line_numbers(self):
        vue_src = (
            "<template>\n"
            "  <div>{{ count }}</div>\n"
            "</template>\n"
            "<script setup lang=\"ts\">\n"
            "import {\n"
            "  ref,\n"
            "} from 'vue'\n"
            "export function useCounter() {\n"
            "  return ref(0)\n"
            "}\n"
            "</script>\n"
        )
        self.write("src/Counter.vue", vue_src)
        item = cli_json("outline", "--root", str(self.root), "src/Counter.vue")["outlines"][0]
        self.assertEqual(item["imports"], ["vue"])
        self.assertIn("function useCounter [L8]", item["symbols"])


class P0RegressionTests(unittest.TestCase):

    def test_outline_regex_literals_local_exports_py_relative_rust_and_kotlin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ts_file = root / "mod.ts"
            ts_file.write_text(
                "const PROTO_RE = /^https?:\\/\\//; // trailing comment\n"
                "const CHAR_CLASS_RE = /[/*]/;\n"
                "function internalOrder() { return 1 }\n"
                "export { internalOrder as publicOrder }\n",
                encoding="utf-8",
            )
            ts_out = aoci_index.extract_file_outline(ts_file, "mod.ts")
            self.assertIn("function internalOrder [L3]", ts_out["symbols"])
            self.assertIn("export publicOrder [L4]", ts_out["symbols"])

            py_file = root / "routes.py"
            py_file.write_text(
                "from . import auth, db\n"
                "@router.post('/orders')\n"
                "async def create_order(req):\n"
                "    return req\n",
                encoding="utf-8",
            )
            py_out = aoci_index.extract_file_outline(py_file, "routes.py")
            self.assertEqual(py_out["imports"], [".auth", ".db"])
            self.assertIn("async def create_order(req) [@post('/orders')] [L3]", py_out["symbols"])

            rs_file = root / "lib.rs"
            rs_file.write_text("use crate::auth::{\n    sign,\n    verify,\n};\npub fn run() {}\n", encoding="utf-8")
            rs_out = aoci_index.extract_file_outline(rs_file, "lib.rs")
            self.assertEqual(rs_out["imports"], ["crate::auth::{ sign, verify, }"])
            self.assertIn("fn run [L5]", rs_out["symbols"])

            kt_file = root / "App.kt"
            kt_file.write_text("import java.time.Instant\nsuspend fun bootServer() {}\n", encoding="utf-8")
            kt_out = aoci_index.extract_file_outline(kt_file, "App.kt")
            self.assertEqual(kt_out["imports"], ["import java.time.Instant"])
            self.assertIn("fun bootServer [L2]", kt_out["symbols"])


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditV2RegressionTests(RepoTestCase):

    def test_p0_redos_long_identifiers_complete_instantly(self):
        self.write("a.py", "x = 1\n")
        self.init()
        long_sym_a = "process_long_identifier_without_slashes_or_dots_alpha"
        long_sym_b = "validate_super_long_camel_case_identifier_token_beta"
        entries = {
            f"mod_{i}/file_{i}.py": (
                f"mod_{i}/file_{i}.py[AX6T]: F:handles long symbol workflow {i} | "
                f"R:{long_sym_a},a.py | A:{long_sym_b}<-a.py | S:-"
            )
            for i in range(40)
        }
        entries["a.py"] = "a.py[CG5T]: F:root entry module | R:- | A:boot | S:-"
        t0 = time.perf_counter()
        deps, dependents = aoci_index.build_fras_graph(entries)
        dangling = aoci_index.find_dangling_refs(self.root, entries)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.25)
        self.assertEqual(deps["mod_0/file_0.py"], {"a.py"})
        self.assertIn("mod_0/file_0.py", dependents["a.py"])
        self.assertEqual(dangling, {})

    def test_p2_parsers_go_rust_js_and_tsconfig_extends(self):
        # Go func + receiver + commented import ignored
        self.write("go.mod", "module example.com/app\n")
        self.write(
            "server/srv.go",
            'package server\nimport (\n\t"fmt"\n\t// "os"\n)\ntype Server struct{}\nfunc NewServer() *Server { return nil }\nfunc (s *Server) Start() {}\n',
        )
        # Rust crate::{...} + super:: resolution
        self.write("src/lib.rs", "use crate::{auth, order::repo};\npub fn run() {}\n")
        self.write("src/auth.rs", "pub fn check() {}\n")
        self.write("src/order/mod.rs", "pub mod repo;\n")
        self.write("src/order/repo.rs", "use super::super::auth::check;\npub fn save() {}\n")
        # JS string containing "export" is not treated as export
        self.write("web/util.ts", 'const LABEL = "export button";\nexport function renderBtn() { return LABEL }\n')
        # tsconfig extends chain
        self.write("tsconfig.base.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@core/*": ["web/*"]}}}\n')
        self.write("tsconfig.json", '{"extends": "./tsconfig.base.json"}\n')
        self.write("web/main.ts", "import { renderBtn } from '@core/util'\nexport const ok = renderBtn()\n")
        self.init()

        go_out = aoci_index.extract_file_outline(self.root / "server/srv.go", "server/srv.go")
        self.assertEqual(go_out["imports"], ["fmt"])
        self.assertEqual(aoci_index._clean_symbol_names(go_out["symbols"]), ["Server", "NewServer", "Start"])

        ts_out = aoci_index.extract_file_outline(self.root / "web/util.ts", "web/util.ts")
        self.assertEqual(ts_out["symbols"], ["function renderBtn [L2]"])

        scaf = cli_json("scaffold", "--root", str(self.root), "--unindexed")
        by_path = {ln.split("[", 1)[0]: ln for ln in scaf["entries"]}
        self.assertIn("A:Server/NewServer/Start", by_path["server/srv.go"])
        self.assertIn("R:src/auth.rs,src/order/repo.rs", by_path["src/lib.rs"])
        self.assertIn("R:src/auth.rs", by_path["src/order/repo.rs"])
        self.assertIn("R:web/util.ts", by_path["web/main.ts"])


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_python_type_checking_try_imports_and_class_annassign_sig_hash(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/types.py", "class Token:\n    pass\n")
        self.write("pkg/fast.py", "def parse():\n    return 1\n")
        self.write("pkg/slow.py", "def parse():\n    return 2\n")
        self.write(
            "pkg/models.py",
            "from __future__ import annotations\n"
            "from dataclasses import dataclass\n"
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg import types\n"
            "try:\n"
            "    from pkg import fast\n"
            "except ImportError:\n"
            "    from pkg import slow\n\n"
            "__all__ = ['Item']\n\n"
            "@dataclass\n"
            "class Item:\n"
            "    id: int\n"
            "    label: str\n"
            "    _cache: int = 0\n\n"
            "    def render(self) -> str:\n"
            "        return self.label\n",
        )
        self.init()

        outline = aoci_index.extract_file_outline(self.root / "pkg/models.py", "pkg/models.py")
        self.assertIn("pkg", outline["imports"])
        self.assertIn("pkg.types", outline["py_from_members"])
        self.assertIn("pkg.fast", outline["py_from_members"])
        self.assertIn("pkg.slow", outline["py_from_members"])

        self.assertEqual(
            self.upsert(
                "pkg/__init__.py[CX1T]: F:marks package root | R:- | A:- | S:-",
                "pkg/types.py[DX2T]: F:defines token type | R:- | A:Token | S:-",
                "pkg/fast.py[CX2T]: F:provides fast parser | R:- | A:parse | S:-",
                "pkg/slow.py[CX2T]: F:provides fallback parser | R:- | A:parse | S:-",
                "pkg/models.py[DX5T]: F:defines item dataclass model | R:pkg/types.py,pkg/fast.py,pkg/slow.py | A:Item | S:-",
            )[0],
            0,
        )

        # Private `_cache` type or method body edit stays `impl_only`
        base_src = (self.root / "pkg/models.py").read_text(encoding="utf-8")
        self.write("pkg/models.py", base_src.replace("_cache: int = 0", "_cache: str = ''").replace("return self.label", "return self.label.strip()"))
        scan1 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p1 = {it["path"]: it for it in scan1["modified"]}
        self.assertEqual(by_p1["pkg/models.py"]["change_kind"], "impl_only")

        # Public dataclass `AnnAssign` change triggers `contract_changed`
        self.write("pkg/models.py", base_src.replace("label: str", "label: int"))
        scan2 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p2 = {it["path"]: it for it in scan2["modified"]}
        self.assertEqual(by_p2["pkg/models.py"]["change_kind"], "contract_changed")

        # `__all__` export list change triggers `contract_changed`
        self.write("pkg/models.py", base_src.replace("__all__ = ['Item']", "__all__ = ['Item', 'extra']"))
        scan3 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p3 = {it["path"]: it for it in scan3["modified"]}
        self.assertEqual(by_p3["pkg/models.py"]["change_kind"], "contract_changed")

        # `if TYPE_CHECKING:` import change triggers `contract_changed`
        self.write("pkg/models.py", base_src.replace("from pkg import types", "from pkg import types, extra_types"))
        scan4 = cli_json("scan", "--root", str(self.root))["drift"]
        by_p4 = {it["path"]: it for it in scan4["modified"]}
        self.assertEqual(by_p4["pkg/models.py"]["change_kind"], "contract_changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
