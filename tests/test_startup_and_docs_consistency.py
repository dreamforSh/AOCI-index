#!/usr/bin/env python3
"""Cold-start paths (lazy facade, selective CLI parser, bytecode cache), deferred MinHash sketches, and docs consistency."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.dont_write_bytecode = True

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from aoci_testkit import RepoTestCase, SKILL_DIR, aoci_index, cli  # noqa: E402
from aoci_core import cli_parser  # noqa: E402

SCRIPTS_DIR = SKILL_DIR / "scripts"
_HEAVY_MODULES = (
    "aoci_core.cli_check",
    "aoci_core.cli_read",
    "aoci_core.cli_write",
    "aoci_core.audit",
    "aoci_core.pool",
    "aoci_core.hubs",
)


def _subparsers(p: argparse.ArgumentParser) -> Any:
    return next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))


def _env_without_bytecode_overrides(**extra: str) -> Dict[str, str]:
    drop = ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "AOCI_NO_PYCACHE")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra)
    return env


class LazyFacadeTests(unittest.TestCase):
    def _loaded_core_modules(self, body: str) -> Dict[str, Any]:
        code = (
            f"import sys, json\nsys.path.insert(0, {str(SCRIPTS_DIR)!r})\nimport aoci_index\n"
            + body
            + "\nprint(json.dumps({'core': sorted(m for m in sys.modules if m.startswith('aoci_core')),"
            " 'futures': 'concurrent.futures' in sys.modules}))"
        )
        r = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(SKILL_DIR),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_import_help_and_drift_paths_skip_heavy_modules(self) -> None:
        self.assertEqual(self._loaded_core_modules("")["core"], ["aoci_core", "aoci_core.constants"])

        help_body = (
            "import contextlib, io\n"
            "with contextlib.redirect_stdout(io.StringIO()):\n"
            "    try:\n"
            "        aoci_index.main(['scan', '--help'])\n"
            "    except SystemExit:\n"
            "        pass\n"
        )
        self.assertEqual(
            self._loaded_core_modules(help_body)["core"],
            ["aoci_core", "aoci_core.cli_parser", "aoci_core.constants"],
        )

        # The hook hot path (`aoci_index.compute_drift`) loads only the drift chain, without `logging`
        drift_state = self._loaded_core_modules("aoci_index.compute_drift")
        self.assertIn("aoci_core.drift", drift_state["core"])
        for heavy in _HEAVY_MODULES:
            self.assertNotIn(heavy, drift_state["core"])
        self.assertFalse(drift_state["futures"], "`concurrent.futures` must stay deferred until a pool is needed")

    def test_lazy_attributes_resolve_live_and_unknown_names_raise(self) -> None:
        from aoci_core import graph

        self.assertIs(aoci_index.BM25Index, graph.BM25Index)
        self.assertIs(aoci_index._graph, graph)
        self.assertIn("compute_drift", dir(aoci_index))
        self.assertIn("_write_ops", dir(aoci_index))
        missing_name = "definitely_not_an_aoci_symbol"  # via a variable: the facade test scans `aoci_index.<attr>`
        with self.assertRaises(AttributeError):
            getattr(aoci_index, missing_name)

        sentinel = object()
        with mock.patch.object(aoci_index, "estimate_tokens", sentinel):
            self.assertIs(aoci_index.estimate_tokens, sentinel)
        self.assertIs(aoci_index.estimate_tokens, graph.estimate_tokens)


class SelectiveParserTests(unittest.TestCase):
    def test_selective_build_matches_full_parser_for_every_subcommand(self) -> None:
        with mock.patch.dict(os.environ, {"COLUMNS": "100", "NO_COLOR": "1", "FORCE_COLOR": "0"}, clear=False):
            full = cli_parser.build_parser()
            full_sub = _subparsers(full)
            self.assertGreaterEqual(len(full_sub.choices), 30)
            for name, full_p in full_sub.choices.items():
                sel = cli_parser.build_parser(only=name)
                sel_sub = _subparsers(sel)
                self.assertEqual(list(sel_sub.choices), list(full_sub.choices), name)
                self.assertEqual(sel.format_help(), full.format_help(), name)
                self.assertEqual(sel.format_usage(), full.format_usage(), name)
                self.assertEqual(sel_sub.choices[name].format_help(), full_p.format_help(), name)
                self.assertEqual(
                    getattr(sel_sub.choices[name].get_default("func"), "__name__", None),
                    getattr(full_p.get_default("func"), "__name__", None),
                    name,
                )
            # An unknown subcommand falls back to the full tree, so argparse errors stay identical
            fallback = _subparsers(cli_parser.build_parser(only="nosuch"))
            self.assertEqual(fallback.choices["scan"].format_help(), full_sub.choices["scan"].format_help())

    def test_requested_subcommand_detection(self) -> None:
        detect = cli_parser.requested_subcommand
        self.assertEqual(detect(["scan", "--root", "x"]), "scan")
        self.assertEqual(detect(["-V", "scan"]), "scan")
        self.assertEqual(detect(["pool", "export", "--help"]), "pool")
        self.assertIsNone(detect([]))
        self.assertIsNone(detect(["--help"]))
        self.assertIsNone(detect(["-h", "scan"]))
        self.assertIsNone(detect(["--", "scan"]))

    def test_lazy_handlers_dispatch_to_live_module_attributes(self) -> None:
        from aoci_core import cli_read

        calls: List[str] = []
        with mock.patch.object(cli_read, "cmd_overview", side_effect=lambda args: calls.append(args.command) or 0):
            self.assertEqual(aoci_index.main(["overview", "--root", str(SKILL_DIR)]), 0)
        self.assertEqual(calls, ["overview"])


class BytecodeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aoci-pyc-"))
        self.scripts = self.tmp / "skill" / "scripts"
        shutil.copytree(SCRIPTS_DIR, self.scripts, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.cache_home = self.tmp / "home"
        self.cache_home.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, **extra: str) -> Dict[str, str]:
        home = str(self.cache_home)
        return _env_without_bytecode_overrides(
            HOME=home, USERPROFILE=home, LOCALAPPDATA=home, XDG_CACHE_HOME=home, **extra
        )

    def _run(self, script: str, *args: str, env: Dict[str, str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, str(self.scripts / script), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(self.tmp),
        )

    @staticmethod
    def _pycs(base: Path) -> List[str]:
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*.pyc"))

    def test_bytecode_lands_in_user_cache_never_beside_skill_sources(self) -> None:
        for script, args in (
            ("aoci_index.py", ["scan", "--help"]),
            ("aoci_index.py", ["--version"]),
            ("aoci_hook.py", ["--help"]),
        ):
            r = self._run(script, *args, env=self._env())
            self.assertEqual(r.returncode, 0, f"{script} {args}: {r.stderr}")
        self.assertEqual(self._pycs(self.scripts), [], "no `.pyc` may be written inside the skill directory")
        cached = self._pycs(self.cache_home)
        self.assertTrue(any("aoci-index/pycache" in p and "constants" in p for p in cached), cached)
        self.assertTrue(any("cli_parser" in p for p in cached), cached)

    def test_opt_outs_disable_bytecode_writes(self) -> None:
        for extra in ({"AOCI_NO_PYCACHE": "1"}, {"PYTHONDONTWRITEBYTECODE": "1"}):
            r = self._run("aoci_index.py", "scan", "--help", env=self._env(**extra))
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._pycs(self.tmp), [])


class DeferredSketchTests(RepoTestCase):
    def _drift(self) -> Dict[str, Any]:
        return aoci_index.compute_drift(self.root, aoci_index.detect_git_info(self.root))

    def test_drift_defers_sketches_without_losing_rename_detection(self) -> None:
        flow = [f"def step_{i}(ctx):\n    return ctx + {i}\n" for i in range(10)]
        consts = "".join(f"CONST_{i} = {i * 7}  # tuning constant number {i}\n" for i in range(10))
        self.write("src/flow.py", "".join(flow))
        self.write("src/consts.py", consts)
        self.init()
        self.upsert(
            "src/flow.py[AX6T]: F:executes ten-step payment pipeline | R:- | A:step_0 | S:-",
            "src/consts.py[CX3T]: F:declares tuning constants | R:- | A:CONST_0 | S:-",
        )
        baseline = aoci_index.load_baseline(self.root / ".aoci")["files"]
        self.assertIn("sketch", baseline["src/flow.py"], "upsert must still persist sketches into baseline.json")

        # Read-only drift over unindexed files: no rename candidates, so no sketch work at all
        self.write("src/fresh.py", "".join(f"def fresh_{i}():\n    return {i}\n" for i in range(6)))
        drift = self._drift()
        self.assertEqual([it["path"] for it in drift["added"]], ["src/fresh.py"])
        self.assertNotIn("sketch", drift["_fingerprints"]["src/fresh.py"])

        # Exact rename: the migrated baseline entry keeps a sketch for future moved+edited detection
        (self.root / "src/consts.py").rename(self.root / "src/tuning.py")
        drift = self._drift()
        self.assertEqual([(r["from"], r["to"]) for r in drift["renamed"]], [("src/consts.py", "src/tuning.py")])
        self.assertIn("sketch", drift["_renamed_full"][0]["fingerprint"])
        code, out = cli("migrate-renames", "--root", str(self.root))
        self.assertEqual(code, 0, out)
        self.assertIn("sketch", aoci_index.load_baseline(self.root / ".aoci")["files"]["src/tuning.py"])

        # Moved + edited: sketches are attached lazily for added files and the pair is still found
        (self.root / "src/flow.py").unlink()
        edited = list(flow)
        edited[-1] = "def step_9(ctx):\n    return ctx + 999\n"
        self.write("src/pay/new_flow.py", "".join(edited))
        drift = self._drift()
        self.assertEqual(
            [(sr["from"], sr["to"]) for sr in drift["similar_renames"]],
            [("src/flow.py", "src/pay/new_flow.py")],
        )
        self.assertIn("sketch", drift["_fingerprints"]["src/pay/new_flow.py"])

    def test_attach_line_sketches_skips_content_changed_since_fingerprint(self) -> None:
        p = self.write("src/mod.py", "".join(f"value_{i} = {i}\n" for i in range(8)))
        fps = {"src/mod.py": aoci_index.fingerprint_file(p)}
        fps["src/mod.py"].pop("sketch", None)
        p.write_text("".join(f"other_{i} = {i}\n" for i in range(8)), encoding="utf-8")
        self.assertEqual(aoci_index.attach_line_sketches(self.root, fps, ["src/mod.py", "missing.py"]), 0)
        self.assertNotIn("sketch", fps["src/mod.py"])


class DocsConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import aoci_mcp

        cls.tool_names = [t["name"] for t in aoci_mcp.TOOLS_SPEC]
        cls.readme = (SKILL_DIR / "README.md").read_text(encoding="utf-8")
        cls.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    def test_mcp_tool_counts_and_names_match_tools_spec(self) -> None:
        n = len(self.tool_names)
        self.assertEqual(len(set(self.tool_names)), n, "duplicate MCP tool names")
        readme_counts = (
            re.findall(r"MCP-(\d+)%20tools", self.readme)
            + re.findall(r"<b>(\d+) MCP Tools</b>", self.readme)
            + re.findall(r"<b>(\d+) annotated tools</b>", self.readme)
        )
        self.assertEqual(len(readme_counts), 3, readme_counts)
        self.assertEqual(set(readme_counts), {str(n)}, f"README tool counts {readme_counts} != TOOLS_SPEC ({n})")
        skill_counts = re.findall(r"; (\d+) tools\)", self.skill)
        self.assertEqual(skill_counts, [str(n)])
        for name in self.tool_names:
            self.assertIn(f"`{name}`", self.readme, f"README is missing MCP tool `{name}`")
            self.assertIn(f"`{name}`", self.skill, f"SKILL.md is missing MCP tool `{name}`")

    def test_markdown_code_fences_are_balanced(self) -> None:
        docs = [SKILL_DIR / "README.md", SKILL_DIR / "SKILL.md", SKILL_DIR / "CHANGELOG.md"]
        docs += sorted((SKILL_DIR / "references").glob("*.md"))
        for doc in docs:
            fences = [ln for ln in doc.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith("```")]
            self.assertEqual(len(fences) % 2, 0, f"unclosed ``` code fence in {doc.name}")

    def test_changelog_has_section_for_current_version(self) -> None:
        changelog = (SKILL_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
        ver = aoci_index.AOCI_VERSION
        self.assertRegex(changelog, rf"(?m)^## \[{re.escape(ver)}\] - \d{{4}}-\d{{2}}-\d{{2}}$")
        self.assertIn(f"[{ver}]: https://github.com/dreamforSh/AOCI-index/", changelog)


if __name__ == "__main__":
    unittest.main(verbosity=2)
