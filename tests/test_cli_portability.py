#!/usr/bin/env python3
"""CLI portability (Python 3.8, Windows paths), ignore matching, and input hardening."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    RepoTestCase,
    SCRIPT,
    SKILL_DIR,
    aoci_index,
    cli,
)


class PortabilityAndPython38Tests(unittest.TestCase):
    def test_python38_ast_and_stdlib_compatibility(self):
        py_files = sorted((SKILL_DIR / "scripts").rglob("*.py")) + sorted((SKILL_DIR / "tests").rglob("*.py"))
        banned_39_methods = {
            "removeprefix",
            "removesuffix",
            "is_relative_to",
            "readlink",
            "with_stem",
            "hardlink_to",
            "bit_count",
        }
        banned_method_kwargs = {
            "write_text": {"newline"},  # Python 3.10+
            "glob": {"root_dir", "dir_fd", "case_sensitive"},  # Python 3.10+/3.12+
            "rglob": {"case_sensitive"},  # Python 3.12+
            "relative_to": {"walk_up"},  # Python 3.12+
            "TemporaryDirectory": {"ignore_cleanup_errors"},  # Python 3.10+
            "zip": {"strict"},  # Python 3.10+
        }
        for py_path in py_files:
            src = py_path.read_text(encoding="utf-8")
            # Enforces Python 3.8 grammar (rejects match/case, parenthesized with-items, etc.)
            tree = ast.parse(src, filename=str(py_path), feature_version=(3, 8))
            has_future_annotations = any(
                isinstance(n, ast.ImportFrom)
                and n.module == "__future__"
                and any(a.name == "annotations" for a in n.names)
                for n in tree.body[:5]
            )
            self.assertTrue(has_future_annotations, f"{py_path.name} must import __future__.annotations for Python 3.8")
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in banned_39_methods:
                    self.fail(f"{py_path.name}:{node.lineno} uses Python 3.9+ method .{node.attr}()")
                if isinstance(node, ast.Call):
                    fn_name = (
                        node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else (node.func.id if isinstance(node.func, ast.Name) else "")
                    )
                    bad_kw = banned_method_kwargs.get(fn_name)
                    if bad_kw:
                        used_kw = {k.arg for k in node.keywords if k.arg} & bad_kw
                        if used_kw:
                            self.fail(
                                f"{py_path.name}:{node.lineno} passes Python 3.10+ kwargs {sorted(used_kw)} to {fn_name}()"
                            )

    @unittest.skipUnless(HAS_GIT, "git is required")
    def test_python38_strict_path_write_text_signature_end_to_end(self):
        import install as install_mod

        orig_write_text = Path.write_text

        def py38_write_text(self_path: Path, data: str, encoding=None, errors=None) -> int:
            return orig_write_text(self_path, data, encoding=encoding, errors=errors)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo = tmp / "repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            with (repo / "app.py").open("w", encoding="utf-8", newline="\n") as fh:
                fh.write("x = 1\n")

            with mock.patch.object(Path, "write_text", py38_write_text):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        install_mod.main(
                            [
                                "--host",
                                "both",
                                "--scope",
                                "global",
                                "--home-dir",
                                str(home),
                                "--with-hooks",
                                "--with-rules",
                                "--with-mcp",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(aoci_index.main(["init", "--root", str(repo)]), 0)
                    self.assertEqual(
                        aoci_index.main(
                            [
                                "upsert",
                                "--root",
                                str(repo),
                                "--entry",
                                "app.py[CX3T]: F:defines module constant x | R:- | A:- | S:-",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(aoci_index.main(["verify", "--root", str(repo)]), 0)

    @unittest.skipUnless(HAS_GIT, "git is required")
    def test_case_sensitive_fs_rejects_wrong_case_in_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "Auth.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            git_info = aoci_index.detect_git_info(root)
            live = aoci_index.LiveFileSet(root, git_info, {}, aoci_index.DEFAULT_MAX_BYTES)
            # Simulate Linux case-sensitive filesystem where `src/auth.py` does not exist on disk
            orig_exists = Path.exists

            def fake_exists(p: Path) -> bool:
                if p.name == "auth.py":
                    return False
                return orig_exists(p)

            with mock.patch.object(Path, "exists", fake_exists):
                self.assertIsNone(live.canonical("src/auth.py"))
                self.assertEqual(live.canonical("src/Auth.py"), "src/Auth.py")


class IgnoreMatcherTests(unittest.TestCase):
    def test_gitignore_semantics(self):
        m = aoci_index.IgnoreMatcher(["generated/", "*.pb.go", "!keep.pb.go", "/rootonly.txt", "docs/**/draft*"])
        self.assertTrue(m.is_ignored("a/generated/x.py"))
        self.assertTrue(m.is_ignored("api/foo.pb.go"))
        self.assertFalse(m.is_ignored("api/keep.pb.go"))
        self.assertTrue(m.is_ignored("rootonly.txt"))
        self.assertFalse(m.is_ignored("sub/rootonly.txt"))
        self.assertTrue(m.is_ignored("docs/a/b/draft1.md"))
        self.assertFalse(m.is_ignored("docs/a/final.md"))
        self.assertTrue(m.is_ignored("generated", is_dir=True))

    def test_cannot_reinclude_inside_ignored_dir(self):
        m = aoci_index.IgnoreMatcher(["vendor/", "!vendor/keep.go"])
        self.assertTrue(m.is_ignored("vendor/keep.go"))


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditV2RegressionTests(RepoTestCase):

    def test_p1_cli_relative_output_and_entries_file_resolve_against_root_from_unrelated_cwd(self):
        self.write("src/app.py", "def run():\n    return 1\n")
        self.init()
        unrelated_cwd = Path(self._tmp.name) / "outside_cwd"
        unrelated_cwd.mkdir()

        # `scaffold --output .aoci/pending/batch.txt` from an unrelated CWD writes inside `--root`
        r_scaf = subprocess.run(
            [
                sys.executable, str(SCRIPT), "scaffold", "--root", str(self.root),
                "--unindexed", "--output", ".aoci/pending/batch_01.txt", "--json",
            ],
            cwd=str(unrelated_cwd),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_scaf.returncode, 0, r_scaf.stdout + r_scaf.stderr)
        batch_file = self.root / ".aoci" / "pending" / "batch_01.txt"
        self.assertTrue(batch_file.is_file())
        self.assertFalse((unrelated_cwd / ".aoci").exists())

        # Replace <TODO> and run `upsert --entries-file .aoci/pending/batch_01.txt --json` from unrelated CWD
        txt = batch_file.read_text(encoding="utf-8").replace("<TODO:read-source>", "runs application entrypoint")
        with batch_file.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(txt)
        r_up = subprocess.run(
            [
                sys.executable, str(SCRIPT), "upsert", "--root", str(self.root),
                "--entries-file", ".aoci/pending/batch_01.txt", "--json",
            ],
            cwd=str(unrelated_cwd),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_up.returncode, 0, r_up.stdout + r_up.stderr)
        up_json = json.loads(r_up.stdout)
        self.assertTrue(up_json["ok"])
        self.assertEqual(up_json["upserted"], ["src/app.py"])

        # Missing `--entries-file` returns clean exit code 2 (no unhandled Traceback)
        r_miss = subprocess.run(
            [
                sys.executable, str(SCRIPT), "upsert", "--root", str(self.root),
                "--entries-file", ".aoci/pending/does_not_exist.txt", "--json",
            ],
            cwd=str(unrelated_cwd),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_miss.returncode, 2)
        self.assertFalse(json.loads(r_miss.stdout)["ok"])

    def test_p1_security_diff_rev_injection_and_outline_hardening(self):
        self.write("-dash.py", "def ok():\n    return 1\n")
        self.write("huge.py", "x = 1\n" * 50)
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
        outside = Path(self._tmp.name) / "secret.py"
        outside.write_text("SECRET = 'leak'\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", "-dash.py", "huge.py"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"],
            cwd=self.root, check=True,
        )
        self.init()

        # `diff` rejects flag-like revision specs
        r_diff = cli("diff", "--root", str(self.root), "--", "--output=evil")
        self.assertEqual(r_diff[0], 2)
        self.assertIn("Invalid Git revision", r_diff[1])

        # `outline` allows `-dash.py` inside root, rejects `../secret.py`, binary, and oversized files
        r_out = subprocess.run(
            [
                sys.executable, str(SCRIPT), "outline", "--root", str(self.root),
                "--max-bytes", "64", "--json", "--", "-dash.py", "../secret.py", "blob.bin", "huge.py",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_out.returncode, 1)
        data = json.loads(r_out.stdout)
        self.assertEqual([o["path"] for o in data["outlines"]], ["-dash.py"])
        rej = {r["path"]: r["reason"] for r in data["rejected"]}
        self.assertEqual(rej["../secret.py"], "path-invalid")
        self.assertEqual(rej["blob.bin"], "binary-content")
        self.assertTrue(rej["huge.py"].startswith("too-large("))


if __name__ == "__main__":
    unittest.main(verbosity=2)
