#!/usr/bin/env python3
"""Facade compatibility, CLI `--help` snapshot, module-boundary, and patch-seam safety-net tests."""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tokenize
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set
from unittest import mock

sys.dont_write_bytecode = True

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_aoci import HAS_GIT, RepoTestCase, SKILL_DIR, aoci_index  # noqa: E402
from aoci_core import discovery, git_ops, graph, lock, parsers, shards  # noqa: E402

SNAPSHOT_PATH = _TESTS_DIR / "cli_help_snapshot.json"

# Authoritative defining modules for monkeypatch seams (updated stage-by-stage as functions move).
SEAM_TARGETS: Dict[str, Any] = {
    "run_git": git_ops,
    "detect_git_info": git_ops,
    "_fingerprint_bytes_norm": discovery,
    "inspect_and_fingerprint_one": discovery,
    "parse_code_volume": shards,
    "extract_file_outline": parsers,
    "compute_signature_hash": parsers,
    "compute_signature_hash_from_bytes": parsers,
    "IndexLock": lock,
    "BM25Index": graph,
}


def _serialize_parser(p: argparse.ArgumentParser, prog_override: str) -> Dict[str, Any]:
    p.prog = prog_override
    if hasattr(p, "color"):
        p.color = False  # type: ignore[attr-defined]
    actions_out: List[Dict[str, Any]] = []
    subparsers_out: Dict[str, Any] = {}
    for act in p._actions:
        if isinstance(act, argparse._SubParsersAction):
            choices_keys = list(act.choices.keys())
            actions_out.append(
                {
                    "kind": "subparsers",
                    "dest": act.dest,
                    "required": bool(getattr(act, "required", False)),
                    "choices": choices_keys,
                }
            )
            for sub_name, sub_p in act.choices.items():
                subparsers_out[sub_name] = _serialize_parser(sub_p, f"{prog_override} {sub_name}")
            continue
        default_val = act.default
        default_repr: Any = "==SUPPRESS==" if default_val is argparse.SUPPRESS else default_val
        type_name = getattr(act.type, "__name__", str(act.type)) if act.type is not None else None
        # CPython <3.10 (bpo-41854) sets `act.required = True` on `nargs="*"` positionals without explicit `default`.
        is_optional_positional = (not act.option_strings) and (act.nargs in ("?", "*", argparse.REMAINDER))
        req_val = False if is_optional_positional else bool(getattr(act, "required", False))
        actions_out.append(
            {
                "kind": type(act).__name__,
                "option_strings": list(act.option_strings),
                "dest": act.dest,
                "nargs": act.nargs,
                "const": act.const,
                "default": default_repr,
                "type": type_name,
                "choices": list(act.choices) if act.choices is not None else None,
                "required": req_val,
                "help": act.help,
                "version": getattr(act, "version", None),
            }
        )
    func_default = p.get_default("func")
    return {
        "prog": p.prog,
        "description": p.description,
        "func_name": getattr(func_default, "__name__", None) if func_default is not None else None,
        "actions": actions_out,
        "subparsers": subparsers_out,
        "help_text": p.format_help(),
    }


def _collect_help_texts(node: Dict[str, Any], out: Dict[str, str]) -> None:
    out[node["prog"]] = node["help_text"]
    for sub_node in node["subparsers"].values():
        _collect_help_texts(sub_node, out)


def _strip_help_text(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "prog": node["prog"],
        "description": node["description"],
        "func_name": node["func_name"],
        "actions": node["actions"],
        "subparsers": {k: _strip_help_text(v) for k, v in node["subparsers"].items()},
    }


def _collect_aoci_index_attrs(py_files: List[Path]) -> Set[str]:
    attrs: Set[str] = set()
    for p in py_files:
        tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
        aliases = {"aoci_index"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "aoci_index":
                        aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "aoci_index":
                for alias in node.names:
                    if alias.name != "*":
                        attrs.add(alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                attrs.add(node.attr)
    return attrs


class FacadeAndCliSnapshotTests(RepoTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    def test_1_facade_reexports_all_snapshotted_and_referenced_symbols(self) -> None:
        missing_snap = [sym for sym in self.snapshot["facade_symbols"] if not hasattr(aoci_index, sym)]
        self.assertEqual(
            missing_snap,
            [],
            f"Missing re-exported symbols on `aoci_index` facade: {missing_snap}",
        )

        caller_files = [
            SKILL_DIR / "scripts" / "aoci_mcp.py",
            SKILL_DIR / "scripts" / "aoci_hook.py",
            *sorted((SKILL_DIR / "tests").glob("test_*.py")),
        ]
        referenced = sorted(_collect_aoci_index_attrs(caller_files))
        self.assertGreaterEqual(len(referenced), 60)
        missing_ref = [sym for sym in referenced if not hasattr(aoci_index, sym)]
        self.assertEqual(
            missing_ref,
            [],
            f"`aoci_mcp` / `aoci_hook` / tests reference missing `aoci_index` attributes: {missing_ref}",
        )

    def test_2_cli_parser_spec_and_subcommand_help_snapshot(self) -> None:
        with mock.patch.dict(os.environ, {"COLUMNS": "100", "NO_COLOR": "1", "FORCE_COLOR": "0"}, clear=False):
            parser = aoci_index.build_parser()
            tree = _serialize_parser(parser, "aoci_index.py")

        actual_spec = _strip_help_text(tree)
        self.assertEqual(actual_spec, self.snapshot["parser_spec"])

        # Simulate CPython 3.8 bpo-41854 (`act.required = True` on `nargs="*"` positionals without `default`)
        py38_parser = aoci_index.build_parser()

        def _force_py38_star_required(p: argparse.ArgumentParser) -> int:
            mutated = 0
            for act in p._actions:
                if isinstance(act, argparse._SubParsersAction):
                    for sub_p in act.choices.values():
                        mutated += _force_py38_star_required(sub_p)
                elif (not act.option_strings) and act.nargs == "*":
                    act.required = True
                    mutated += 1
            return mutated

        self.assertGreaterEqual(_force_py38_star_required(py38_parser), 2)
        with mock.patch.dict(os.environ, {"COLUMNS": "100", "NO_COLOR": "1", "FORCE_COLOR": "0"}, clear=False):
            py38_spec = _strip_help_text(_serialize_parser(py38_parser, "aoci_index.py"))
        self.assertEqual(py38_spec, self.snapshot["parser_spec"])

        actual_help: Dict[str, str] = {}
        _collect_help_texts(tree, actual_help)
        expected_help: Dict[str, str] = self.snapshot["help_texts"]
        self.assertEqual(sorted(actual_help.keys()), sorted(expected_help.keys()))

        if list(sys.version_info[:2]) == self.snapshot["help_python_version"]:
            for prog_key, exp_txt in expected_help.items():
                self.assertEqual(
                    actual_help[prog_key],
                    exp_txt,
                    f"CLI `--help` output changed for `{prog_key}`",
                )
        else:
            for prog_key in expected_help:
                self.assertIn(prog_key, actual_help[prog_key])

        # Verify `main([sub, "--help"])` exits 0 cleanly for root and representative subcommands
        for argv in (["--help"], ["scan", "--help"], ["upsert", "--help"], ["pool", "export", "--help"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    aoci_index.main(argv)
            self.assertEqual(ctx.exception.code, 0, argv)
            self.assertIn("usage:", buf.getvalue())

    def test_3_aoci_core_forbids_upward_imports_and_keeps_py38_syntax(self) -> None:
        forbidden_tops = {"aoci_index", "aoci_mcp", "aoci_hook", "install"}
        core_files = sorted((SKILL_DIR / "scripts" / "aoci_core").glob("*.py"))
        self.assertGreaterEqual(len(core_files), 15)

        for cf in core_files:
            tree = ast.parse(cf.read_text(encoding="utf-8-sig"), filename=str(cf))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".", 1)[0]
                        self.assertNotIn(top, forbidden_tops, f"`{cf.name}` must not import `{alias.name}`")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".", 1)[0]
                    self.assertNotIn(top, forbidden_tops, f"`{cf.name}` must not import from `{node.module}`")

        # Check every Python file in scripts/, .github/scripts/, and tests/ for Python 3.8 syntax hazards
        all_py = [
            *sorted((SKILL_DIR / "scripts").rglob("*.py")),
            *sorted((SKILL_DIR / ".github" / "scripts").glob("*.py")),
            *sorted((SKILL_DIR / "tests").glob("*.py")),
        ]
        fstring_middle_type = getattr(tokenize, "FSTRING_MIDDLE", None)
        for pf in all_py:
            src = pf.read_text(encoding="utf-8-sig")
            tree = ast.parse(src, filename=str(pf))
            for node in ast.walk(tree):
                self.assertNotEqual(type(node).__name__, "Match", f"Python 3.10+ `match` in `{pf}`")
                self.assertNotEqual(type(node).__name__, "TryStar", f"Python 3.11+ `except*` in `{pf}`")
                self.assertNotEqual(type(node).__name__, "TypeAlias", f"Python 3.12+ `type X =` in `{pf}`")

            # On Python 3.12+, verify no `{...}` expression inside an f-string contains a backslash `\`
            if fstring_middle_type is not None:
                tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
                f_depth = 0
                f_start_type = getattr(tokenize, "FSTRING_START", -1)
                f_end_type = getattr(tokenize, "FSTRING_END", -2)
                brace_depth = 0
                for tok in tokens:
                    if tok.type == f_start_type:
                        f_depth += 1
                    elif tok.type == f_end_type:
                        f_depth = max(0, f_depth - 1)
                    elif f_depth > 0 and tok.type != fstring_middle_type:
                        if tok.string == "{":
                            brace_depth += 1
                        elif tok.string == "}":
                            brace_depth = max(0, brace_depth - 1)
                        elif brace_depth > 0 and "\\" in tok.string:
                            self.fail(
                                f"Python <3.12 SyntaxError hazard: backslash inside f-string expression at "
                                f"`{pf}:{tok.start[0]}` ({tok.string!r})"
                            )

    def test_4_patch_seams_intercept_live_execution(self) -> None:
        self.write("src/alpha.py", "def alpha(x: int) -> int:\n    return x + 1\n")
        self.write("src/beta.py", "from src.alpha import alpha\ndef beta() -> int:\n    return alpha(2)\n")
        if HAS_GIT:
            subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

        # 1) detect_git_info + run_git
        det_mod = SEAM_TARGETS["detect_git_info"]
        git_mod = SEAM_TARGETS["run_git"]
        with mock.patch.object(det_mod, "detect_git_info", wraps=det_mod.detect_git_info) as spy_det:
            with contextlib.redirect_stdout(io.StringIO()):
                aoci_index.main(["init", "--root", str(self.root), "--json"])
            self.assertGreaterEqual(spy_det.call_count, 1)

        if HAS_GIT:
            with mock.patch.object(git_mod, "run_git", wraps=git_mod.run_git) as spy_git:
                git_info = det_mod.detect_git_info(self.root)
                aoci_index.check_git_exclusion(self.root, git_info, check_status=True, use_cache=False)
                self.assertGreaterEqual(spy_git.call_count, 1)

        # 2) inspect_and_fingerprint_one + _fingerprint_bytes_norm
        fp_mod = SEAM_TARGETS["_fingerprint_bytes_norm"]
        insp_mod = SEAM_TARGETS["inspect_and_fingerprint_one"]
        with mock.patch.object(fp_mod, "_fingerprint_bytes_norm", wraps=fp_mod._fingerprint_bytes_norm) as spy_fp:
            with mock.patch.object(
                insp_mod, "inspect_and_fingerprint_one", wraps=insp_mod.inspect_and_fingerprint_one
            ) as spy_insp:
                git_info = det_mod.detect_git_info(self.root)
                ignores = aoci_index.load_ignore_matcher(self.root, git_info)
                aoci_index.discover_and_fingerprint(self.root, git_info, ignores, base_files={}, fast_mtime=False)
                self.assertGreaterEqual(spy_fp.call_count, 1)
                self.assertGreaterEqual(spy_insp.call_count, 1)

        # 3) extract_file_outline (cold outline cache)
        out_mod = SEAM_TARGETS["extract_file_outline"]
        cache_file = self.root / ".aoci" / ".cache" / "outline_cache.json"
        if cache_file.exists():
            cache_file.unlink()
        with mock.patch.object(out_mod, "extract_file_outline", wraps=out_mod.extract_file_outline) as spy_out:
            aoci_index.scaffold_entries(self.root, ["src/alpha.py", "src/beta.py"])
            self.assertGreaterEqual(spy_out.call_count, 1)

        # 4) IndexLock + parse_code_volume + compute_signature_hash_from_bytes + compute_signature_hash
        lock_mod = SEAM_TARGETS["IndexLock"]
        pcv_mod = SEAM_TARGETS["parse_code_volume"]
        sig_b_mod = SEAM_TARGETS["compute_signature_hash_from_bytes"]
        sig_mod = SEAM_TARGETS["compute_signature_hash"]
        with mock.patch.object(lock_mod, "IndexLock", wraps=lock_mod.IndexLock) as spy_lock:
            with mock.patch.object(pcv_mod, "parse_code_volume", wraps=pcv_mod.parse_code_volume) as spy_pcv:
                with mock.patch.object(
                    sig_b_mod, "compute_signature_hash_from_bytes", wraps=sig_b_mod.compute_signature_hash_from_bytes
                ) as spy_sig_b:
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc_u = aoci_index.main(
                            [
                                "upsert",
                                "--root",
                                str(self.root),
                                "--entry",
                                "src/alpha.py[KX6T]: F:increments integer value | R:- | A:alpha<-src/beta.py | S:-",
                                "--entry",
                                "src/beta.py[AX5T]: F:calls alpha helper | R:src/alpha.py | A:beta | S:-",
                                "--json",
                            ]
                        )
                    self.assertEqual(rc_u, 0)
                    self.assertGreaterEqual(spy_lock.call_count, 1)
                    self.assertGreaterEqual(spy_pcv.call_count, 1)
                    self.assertGreaterEqual(spy_sig_b.call_count, 1)

        self.write("src/alpha.py", "def alpha(x: int) -> int:\n    return x + 99\n")
        with mock.patch.object(sig_mod, "compute_signature_hash", wraps=sig_mod.compute_signature_hash) as spy_sig:
            # Force `_classify_modified_entry` fallback without `_cur_sig_hash`
            base_rec = aoci_index.load_baseline(self.root / ".aoci")["files"]["src/alpha.py"]
            kind = aoci_index._classify_modified_entry(self.root, "src/alpha.py", base_rec, cur_fp={})
            self.assertEqual(kind, "impl_only")
            self.assertGreaterEqual(spy_sig.call_count, 1)

        # 5) BM25Index via `pack`
        bm_mod = SEAM_TARGETS["BM25Index"]
        with mock.patch.object(bm_mod, "BM25Index", wraps=bm_mod.BM25Index) as spy_bm:
            with contextlib.redirect_stdout(io.StringIO()):
                rc_p = aoci_index.main(["pack", "--root", str(self.root), "--task", "alpha helper", "--no-diff", "--json"])
            self.assertEqual(rc_p, 0)
            self.assertGreaterEqual(spy_bm.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
