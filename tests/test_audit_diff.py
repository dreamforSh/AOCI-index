#!/usr/bin/env python3
"""Architecture audit, scaffolding, and diff impact briefings."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    age_file,
    aoci_index,
    cli,
    cli_json,
    git_ops,
    parsers,
)


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase3FeatureTests(RepoTestCase):

    def _git(self, *args: str, cwd: Path = None) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd or self.root,
            check=True,
        )

    def test_diff_impact_briefing_highlights_constraints_and_callers(self):
        self.write("src/env.py", "SECRET = 'x'\n")
        self.write("src/jwt.py", "from src import env\n")
        self.write("src/routes.py", "from src import jwt\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")

        self.init()
        self.upsert(
            "src/env.py[CG7T]: F:loads startup secrets | R:- | A:SECRET<-src/jwt.py | S:fail fast on missing secret",
            "src/jwt.py[MA8T]: F:verifies access tokens | R:src/env.py | A:verify<-src/routes.py | S:alg allowlist",
            "src/routes.py[EA6T]: F:mounts HTTP routes | R:src/jwt.py | A:router | S:-",
        )
        self.write("src/jwt.py", "from src import env\nSKEW = 30\n")
        self.write("src/new_helper.py", "h = 1\n")

        brief = cli_json("diff", "--root", str(self.root))
        self.assertEqual([c["path"] for c in brief["high_risk_constraints"]], ["src/jwt.py"])
        self.assertEqual(brief["unindexed_changed"], ["src/new_helper.py"])
        self.assertTrue(any(ln.startswith("src/routes.py[") for ln in brief["affected_neighbors"]))

        self._git("add", "src/jwt.py")
        self._git("commit", "-q", "-m", "update jwt")
        rev_brief = cli_json("diff", "HEAD~1..HEAD", "--root", str(self.root))
        self.assertEqual(len(rev_brief["changed_entries"]), 1)
        self.assertIn("src/jwt.py[", rev_brief["changed_entries"][0])

    def test_scaffold_resolves_tsconfig_aliases_and_blocks_unfilled_todo_upsert(self):
        self.write(
            "tsconfig.json",
            '{\n  // JSONC comment\n  "compilerOptions": {\n    "baseUrl": ".",\n    "paths": {"@/*": ["src/*"],}\n  }\n}\n',
        )
        self.write("src/auth/jwt.ts", "export function signToken(id: string) {\n  return id\n}\n")
        self.write(
            "src/routes/login.ts",
            "import { signToken } from '@/auth/jwt'\nexport function loginHandler() {\n  return signToken('u1')\n}\n",
        )
        self.init()

        scaf = cli_json("scaffold", "--root", str(self.root), "--unindexed")
        by_prefix = {ln.split("[", 1)[0]: ln for ln in scaf["entries"]}
        self.assertIn("R:src/auth/jwt.ts", by_prefix["src/routes/login.ts"])
        self.assertIn("A:signToken<-src/routes/login.ts", by_prefix["src/auth/jwt.ts"])

        # Attempting to upsert raw `<TODO:...>` without reading & authoring must fail Rule #4 validation
        code, out = self.upsert(by_prefix["src/auth/jwt.ts"])
        self.assertEqual(code, 1)
        self.assertIn("TODO", out)


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase1FixesTests(RepoTestCase):

    def _git_at(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd,
            check=True,
        )

    def test_monorepo_subproject_diff_briefing_and_git_copy_not_deleted(self):
        sub = self.root / "packages" / "api"
        sub.mkdir(parents=True)
        for rel, body in (("app.py", "PORT = 8080\n"), ("caller.py", "import app\n")):
            p = sub / rel
            with p.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            age_file(p)
        self._git_at(self.root, "add", ".")
        self._git_at(self.root, "commit", "-q", "-m", "init monorepo")

        self.assertEqual(cli("init", "--root", str(sub))[0], 0)
        self.assertEqual(
            cli(
                "upsert",
                "--root",
                str(sub),
                "--entry",
                "app.py[CG8T]: F:defines API listen port | R:- | A:PORT<-caller.py | S:must stay unprivileged >=1024",
                "--entry",
                "caller.py[EA5T]: F:boots API listener using app config | R:app.py | A:boot | S:-",
            )[0],
            0,
        )

        # Working-tree edit + untracked file inside subproject `packages/api`
        with (sub / "app.py").open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("PORT = 9090\n")
        with (sub / "extra.py").open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("EXTRA = 1\n")

        wt_brief = cli_json("diff", "--root", str(sub))
        self.assertEqual([c["path"] for c in wt_brief["high_risk_constraints"]], ["app.py"])
        self.assertTrue(any(ln.startswith("app.py[") for ln in wt_brief["changed_entries"]))
        self.assertEqual(wt_brief["unindexed_changed"], ["extra.py"])
        self.assertTrue(any(ln.startswith("caller.py[") for ln in wt_brief["affected_neighbors"]))

        # Commit edit and check Git revision diff (`HEAD~1..HEAD`) inside subproject
        self._git_at(self.root, "add", "packages/api/app.py")
        self._git_at(self.root, "commit", "-q", "-m", "change port")
        rev_brief = cli_json("diff", "HEAD~1..HEAD", "--root", str(sub))
        self.assertEqual([c["path"] for c in rev_brief["high_risk_constraints"]], ["app.py"])
        self.assertTrue(any(ln.startswith("caller.py[") for ln in rev_brief["affected_neighbors"]))

        # Git Copy (`C100\0app.py\0app_copy.py\0`) must NOT report source `app.py` as deleted
        fake_cp = subprocess.CompletedProcess(["git"], 0, stdout="C100\0app.py\0app_copy.py\0", stderr="")
        with mock.patch.object(git_ops, "run_git", return_value=fake_cp) as spy_copy_git:
            cp_brief = aoci_index.compute_diff_briefing(sub, rev="HEAD~1..HEAD")
        self.assertGreaterEqual(spy_copy_git.call_count, 1)
        self.assertEqual(cp_brief["deleted_entries"], [])
        self.assertEqual(cp_brief["unindexed_changed"], ["app_copy.py"])


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditV2RegressionTests(RepoTestCase):

    def test_p1_scaffold_batch_matches_scan_and_uses_outline_cache(self):
        orig = "".join(f"def step_{i}(v):\n    return v + {i}\n" for i in range(8))
        self.write("old/flow.py", orig)
        self.write("pending_a.py", "def pa():\n    return 1\n")
        self.write("pending_b.py", "def pb():\n    return 2\n")
        self.init()
        self.upsert("old/flow.py[AX6T]: F:executes eight step flow | R:- | A:step_0 | S:-")

        (self.root / "old/flow.py").unlink()
        self.write("new/flow.py", orig.replace("return v + 7", "return v + 777"))

        scan_data = cli_json("scan", "--root", str(self.root), "--batch-size", "1")
        batch1_scan = [it["path"] for it in scan_data["batches"][0]]
        scaf_data = cli_json("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "1")
        batch1_scaf = [ln.split("[", 1)[0] for ln in scaf_data["entries"]]
        self.assertEqual(batch1_scaf, batch1_scan)
        self.assertNotIn("new/flow.py", batch1_scaf)
        self.assertTrue((self.root / ".aoci" / ".cache" / "outline_cache.json").is_file())

        # Second scaffold run hits `outline_cache.json` cleanly without re-extracting unchanged files
        with mock.patch.object(parsers, "extract_file_outline", side_effect=AssertionError("must hit outline_cache")):
            scaf_again = cli_json("scaffold", "--root", str(self.root), "--batch", "1", "--batch-size", "1")
        self.assertEqual([ln.split("[", 1)[0] for ln in scaf_again["entries"]], batch1_scan)


@unittest.skipUnless(HAS_GIT, "git is required")
class Milestone2FeatureTests(RepoTestCase):

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

    def test_m2_audit_detects_missing_static_phantom_cycles_and_layer_rules(self):
        import aoci_mcp

        self.write("pyproject.toml", "[project]\nname='demo'\n")
        self.write("src/entry.py", "from src import domain, repo\n")
        self.write("src/domain.py", "from src import entry, repo\n")
        self.write("src/repo.py", "from src import entry, domain\n")
        self.write("src/phantom.py", "def helper():\n    return 1\n")
        self.init()
        # Add custom rule in manifest (`A!->K`) on top of built-in rules (`D!->E,U`, `P!->E,U`)
        manifest_p = self.root / ".aoci" / "aoci.txt"
        manifest_p.write_text(
            manifest_p.read_text(encoding="utf-8") + "#Layer-Rules: A!->K\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self.upsert(
                "pyproject.toml[RG3T]: F:declares package metadata | R:- | A:- | S:-",
                # R: mentions repo.py and pyproject.toml, omits static import src/domain.py -> missing-static-dep
                "src/entry.py[EX7T]: F:handles entry routing | R:src/repo.py,pyproject.toml | A:main | S:-",
                # R: omits repo.py (static) and lists entry.py + phantom.py -> D!->E violation + phantom info
                "src/domain.py[DX7T]: F:implements core domain rules | R:src/entry.py,src/phantom.py | A:calc | S:-",
                # repo depends on entry and domain -> 3-node cycle (entry->repo->domain->entry) + P!->E violation
                "src/repo.py[PX7T]: F:persists repository state | R:src/entry.py,src/domain.py | A:save | S:-",
                "src/phantom.py[CX3T]: F:provides auxiliary helper routine | R:- | A:helper | S:-",
            )[0],
            0,
        )

        # Deep linear chain test for iterative Tarjan SCC (> 1200 nodes won't hit recursion limit)
        deep_adj = {f"n{i}": ({f"n{i+1}"} if i < 1499 else set()) for i in range(1500)}
        self.assertEqual(aoci_index.find_dependency_cycles(deep_adj), [])

        aud = cli_json("audit", "--root", str(self.root))
        self.assertFalse(aud["ok"])
        missing_pairs = {(x["from"], x["to"]) for x in aud["missing_static_deps"]}
        self.assertIn(("src/entry.py", "src/domain.py"), missing_pairs)
        self.assertIn(("src/domain.py", "src/repo.py"), missing_pairs)

        # Only same-ecosystem Python R: phantom (src/phantom.py), NOT pyproject.toml read-along!
        phantom_pairs = {(x["from"], x["to"]) for x in aud["phantom_fras_deps"]}
        self.assertIn(("src/domain.py", "src/phantom.py"), phantom_pairs)
        self.assertNotIn(("src/entry.py", "pyproject.toml"), phantom_pairs)

        cycle_sets = [set(c["nodes"]) for c in aud["cycles"]]
        self.assertTrue(any({"src/entry.py", "src/repo.py", "src/domain.py"}.issubset(cs) for cs in cycle_sets))

        rules_hit = {v["rule"] for v in aud["layer_violations"]}
        self.assertIn("D!->E", rules_hit)
        self.assertIn("P!->E", rules_hit)

        # `--strict` returns exit code 1 when issues exist, and custom `--forbid E!->P` catches E->P
        strict_code, _ = cli("audit", "--root", str(self.root), "--strict", "--forbid", "E!->P")
        self.assertEqual(strict_code, 1)
        is_err, mcp_txt = aoci_mcp.call_tool("aoci_audit", {"root": str(self.root), "forbid": "E!->P"})
        self.assertFalse(is_err, mcp_txt)
        mcp_aud = json.loads(mcp_txt)
        self.assertIn("E!->P", {v["rule"] for v in mcp_aud["layer_violations"]})


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_go_package_level_audit_5_file_pkg_no_false_missing_or_phantom(self):
        self.write("go.mod", "module example.com/demo\n")
        for i in range(1, 6):
            self.write(
                f"pkg/f{i}.go",
                f"package pkg\n\nfunc Func{i}() int {{ return {i} }}\n",
            )
        self.write("pkg/f1_test.go", "package pkg\nimport \"testing\"\nfunc TestF1(t *testing.T) {}\n")
        self.write(
            "cmd/app/main.go",
            'package main\n\nimport (\n\t"fmt"\n\t"example.com/demo/pkg"\n)\n\nfunc main() { fmt.Println(pkg.Func4()) }\n',
        )
        self.write(
            "cmd/app/unlinked.go",
            'package main\n\nimport "example.com/demo/pkg"\n\nfunc helper() int { return pkg.Func1() }\n',
        )
        self.write("other/extra.go", "package other\n\nfunc Extra() int { return 9 }\n")
        self.init()
        entries = [
            "pkg/f1.go[DX3T]: F:implements package part one | R:pkg/f4.go | A:Func1 | S:-",
            "pkg/f2.go[DX3T]: F:implements package part two | R:- | A:Func2 | S:-",
            "pkg/f3.go[DX3T]: F:implements package part three | R:- | A:Func3 | S:-",
            "pkg/f4.go[DX3T]: F:implements package part four | R:- | A:Func4 | S:-",
            "pkg/f5.go[DX3T]: F:implements package part five | R:- | A:Func5 | S:-",
            "pkg/f1_test.go[CX2T]: F:tests package func one | R:pkg/f1.go | A:TestF1 | S:-",
            "cmd/app/main.go[EX4T]: F:runs application entrypoint | R:pkg/f4.go | A:main | S:-",
            "cmd/app/unlinked.go[EX3T]: F:runs secondary app helper | R:- | A:helper | S:-",
            "other/extra.go[CX2T]: F:provides unrelated helper | R:- | A:Extra | S:-",
        ]
        self.assertEqual(self.upsert(*entries)[0], 0)

        aud = cli_json("audit", "--root", str(self.root))
        missing_pairs = [(x["from"], x["to"]) for x in aud["missing_static_deps"]]
        phantom_pairs = [(x["from"], x["to"]) for x in aud["phantom_fras_deps"]]

        # `cmd/app/main.go` cites 4th file `pkg/f4.go` -> satisfies `pkg` import with zero missing/phantom
        self.assertFalse(any(src == "cmd/app/main.go" for src, _ in missing_pairs), missing_pairs)
        self.assertFalse(any(src == "cmd/app/main.go" for src, _ in phantom_pairs), phantom_pairs)
        # Same-package sibling `pkg/f1.go` -> `pkg/f4.go` is NOT flagged as phantom
        self.assertNotIn(("pkg/f1.go", "pkg/f4.go"), phantom_pairs)
        # `cmd/app/unlinked.go` omits all files of imported `pkg` -> exactly one missing-static-dep (`pkg/f1.go`)
        unlinked_missing = [x for x in aud["missing_static_deps"] if x["from"] == "cmd/app/unlinked.go"]
        self.assertEqual(unlinked_missing, [{"from": "cmd/app/unlinked.go", "to": "pkg/f1.go", "code": "missing-static-dep"}])

        # Citing a `.go` file from an unimported package (`other/extra.go`) still triggers `phantom-fras-dep`
        self.assertEqual(
            self.upsert("cmd/app/main.go[EX4T]: F:runs application entrypoint | R:pkg/f4.go,other/extra.go | A:main | S:-")[0],
            0,
        )
        aud2 = cli_json("audit", "--root", str(self.root))
        self.assertIn(("cmd/app/main.go", "other/extra.go"), [(x["from"], x["to"]) for x in aud2["phantom_fras_deps"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
