#!/usr/bin/env python3
"""Index volumes: manifest parsing, CJK paths, shards, and cross-manifest version alignment."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    SKILL_DIR,
    age_file,
    aoci_index,
    cli,
    cli_json,
)


class P0RegressionTests(unittest.TestCase):

    def test_cjk_domains_and_multiline_invariants(self):
        with tempfile.TemporaryDirectory() as td:
            aoci_dir = Path(td) / ".aoci"
            aoci_dir.mkdir()
            (aoci_dir / "aoci.meta.txt").write_text(
                "# D (Domain):\n#   G=全局  X=核心\n#   A=认证  O=订单-履约\n#   （示例：Z=示例模块）\n",
                encoding="utf-8",
            )
            domains = aoci_index.parse_declared_domains(aoci_dir)
            self.assertEqual(domains, {"G": "全局", "X": "核心", "A": "认证", "O": "订单-履约"})

            (aoci_dir / "aoci.txt").write_text(
                "#AOCI-ROOT-MANIFEST: 1\n"
                "#Global-Invariants:\n"
                "#  - 写接口必须经过 JWT 鉴权\n"
                "#  - 金额统一使用 int64 分\n"
                "#Volume: id=meta kind=meta path=aoci.meta.txt format=meta-v1 depends=- state=enabled\n",
                encoding="utf-8",
            )
            inv = aoci_index.parse_manifest_field(aoci_dir, "#Global-Invariants:")
            self.assertEqual(inv, "写接口必须经过 JWT 鉴权; 金额统一使用 int64 分")


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase2PerformanceTests(RepoTestCase):

    def test_incremental_shard_sync_rewrites_only_dirty_shard(self):
        self.write("pkg_a/x.py", "x = 1\n")
        self.write("pkg_b/y.py", "y = 2\n")
        self.init()
        self.upsert(
            "pkg_a/x.py[DX4T]: F:defines pkg_a constant x | R:- | A:- | S:-",
            "pkg_b/y.py[DX4T]: F:defines pkg_b constant y | R:- | A:- | S:-",
        )
        self.assertEqual(cli("shard", "--root", str(self.root), "--depth", "1")[0], 0)
        shard_a = self.root / ".aoci" / "shards" / "pkg_a.code.txt"
        shard_b = self.root / ".aoci" / "shards" / "pkg_b.code.txt"
        age_file(shard_a, seconds=120)
        age_file(shard_b, seconds=120)
        mtime_b_before = os.stat(shard_b).st_mtime_ns

        self.write("pkg_a/x.py", "x = 99\n")
        self.assertEqual(
            self.upsert("pkg_a/x.py[DX4T]: F:defines updated pkg_a constant x | R:- | A:- | S:-")[0],
            0,
        )
        self.assertEqual(os.stat(shard_b).st_mtime_ns, mtime_b_before)
        self.assertIn("updated pkg_a", shard_a.read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditFixesRegressionTests(RepoTestCase):

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            check=True,
        )

    def test_cjk_paths_forward_reverse_context_and_distinct_shards(self):
        self.write("main.py", "print('boot')\n")
        self.write("模块/a.py", "def run_a():\n    return 1\n")
        self.write("服务/b.py", "def serve_b():\n    return 2\n")
        self.init()
        self.assertEqual(
            self.upsert(
                "main.py[EG6T]: F:主入口编排核心模块与后端服务 | R:模块/a.py,服务/b.py | A:main | S:-",
                "模块/a.py[DX5T]: F:实现领域计算模块逻辑 | R:- | A:run_a | S:-",
                "服务/b.py[AX5T]: F:提供业务编排服务接口 | R:- | A:serve_b<-main.py | S:-",
            )[0],
            0,
        )

        # Forward query on main.py -> must return BOTH 模块/a.py and 服务/b.py
        ctx_main = cli_json("context", "--root", str(self.root), "main.py")
        dep_paths = [ln.split("[", 1)[0] for ln in ctx_main["dependencies"]]
        self.assertEqual(dep_paths, ["服务/b.py", "模块/a.py"])

        # Reverse queries on 模块/a.py and 服务/b.py -> both must trace back to main.py
        ctx_a = cli_json("context", "--root", str(self.root), "模块/a.py")
        self.assertEqual([ln.split("[", 1)[0] for ln in ctx_a["callers"]], ["main.py"])
        imp_b = cli_json("query", "--root", str(self.root), "--impact", "服务/b.py")
        imp_b_paths = sorted(ln.split("[", 1)[0] for ln in imp_b["entries"])
        self.assertEqual(imp_b_paths, ["main.py", "服务/b.py"])

        # Shards: `./`, `模块/`, and `服务/` must map to 3 distinct shard files (never colliding on `__`)
        shard_manifest = cli_json("shard", "--root", str(self.root), "--depth", "1")
        self.assertEqual(len(shard_manifest["shards"]), 3)
        by_prefix = {s["prefix"]: s for s in shard_manifest["shards"]}
        self.assertEqual(set(by_prefix.keys()), {"./", "模块/", "服务/"})
        shard_files = {s["file"] for s in shard_manifest["shards"]}
        self.assertEqual(len(shard_files), 3)
        mod_txt = (self.root / ".aoci" / by_prefix["模块/"]["file"]).read_text(encoding="utf-8")
        svc_txt = (self.root / ".aoci" / by_prefix["服务/"]["file"]).read_text(encoding="utf-8")
        self.assertIn("模块/a.py[DX5T]", mod_txt)
        self.assertNotIn("服务/b.py", mod_txt)
        self.assertIn("服务/b.py[AX5T]", svc_txt)
        self.assertNotIn("模块/a.py", svc_txt)


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_version_alignment_across_manifests_cli_and_mcp(self):
        import re

        import aoci_core
        import aoci_mcp

        ver = aoci_index.AOCI_VERSION
        self.assertEqual(ver, "1.6.0")
        self.assertEqual(aoci_index.__version__, ver)
        self.assertEqual(aoci_core.__version__, ver)
        self.assertEqual(aoci_mcp.SERVER_VERSION, ver)

        pyproject_text = (SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), ver)

        plugin_data = json.loads((SKILL_DIR / "resources" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin_data.get("version"), ver)

        readme_text = (SKILL_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"version-{ver}-", readme_text)
        self.assertNotIn("1.5.0", readme_text)
        self.assertNotIn("1.4.0", readme_text)
        self.assertNotIn("1.3.0", readme_text)
        self.assertNotIn("1.2.0", readme_text)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                aoci_index.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(ver, buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
