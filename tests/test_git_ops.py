#!/usr/bin/env python3
"""Git integration: pure-Python worktree detection and local Git hooks."""

from __future__ import annotations

import shutil
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
    aoci_index,
    cli_json,
    git_ops,
)


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase2PerformanceTests(RepoTestCase):

    def test_detect_git_info_pure_python_standard_and_worktree_zero_subprocesses(self):
        sub = self.root / "nested" / "pkg"
        sub.mkdir(parents=True)
        with mock.patch.object(git_ops, "run_git", side_effect=AssertionError("git must not be spawned")):
            info = aoci_index.detect_git_info(sub)
        self.assertTrue(info["is_git"])
        self.assertEqual(info["toplevel"], self.root.resolve())
        self.assertEqual(info["exclude_file"], (self.root / ".git" / "info" / "exclude").resolve())
        self.assertEqual(info["index_file"], (self.root / ".git" / "index").resolve())

        # Create a commit and a secondary Git worktree (`commondir` layout)
        self.write("seed.py", "s = 1\n")
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", "add", "seed.py"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"], cwd=self.root, check=True)
        wt_dir = Path(self._tmp.name) / "wt2"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt_dir)], cwd=self.root, check=True)
        with mock.patch.object(git_ops, "run_git", side_effect=AssertionError("git must not be spawned")):
            wt_info = aoci_index.detect_git_info(wt_dir)
        self.assertTrue(wt_info["is_git"])
        self.assertEqual(wt_info["toplevel"], wt_dir.resolve())
        self.assertEqual(wt_info["exclude_file"], (self.root / ".git" / "info" / "exclude").resolve())
        self.assertIn("worktrees", wt_info["index_file"].as_posix())


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

    def test_m2_install_git_hooks_preserves_existing_custom_hooks_and_core_hookspath(self):
        self.write("app.py", "def run():\n    return 1\n\n\nFLAG = True\n")
        self._git("add", "app.py")
        self._git("commit", "-q", "-m", "base")
        self.init()

        hooks_dir = self.root / ".git" / "hooks"
        existing_pc = hooks_dir / "post-checkout"
        existing_pc.write_text("#!/bin/sh\necho user-post-checkout\n", encoding="utf-8")

        ins = cli_json(
            "install-git-hooks", "--root", str(self.root),
            "--with-pre-commit", "--pre-commit-verify",
        )
        self.assertTrue(ins["ok"])
        self.assertEqual(ins["hooks"]["post-checkout"], "appended")
        self.assertEqual(ins["hooks"]["post-merge"], "created")
        self.assertEqual(ins["hooks"]["pre-commit"], "created")

        pc_txt = existing_pc.read_text(encoding="utf-8")
        self.assertIn("echo user-post-checkout", pc_txt)
        self.assertIn(aoci_index.GIT_HOOK_BEGIN, pc_txt)
        self.assertIn("restore --root", pc_txt)

        # Re-installing is strictly idempotent (does not duplicate managed block)
        ins2 = cli_json("install-git-hooks", "--root", str(self.root), "--with-pre-commit")
        self.assertEqual(ins2["hooks"]["post-checkout"], "updated")
        self.assertEqual(existing_pc.read_text(encoding="utf-8").count(aoci_index.GIT_HOOK_BEGIN), 1)

        # Custom `core.hooksPath` is honored for both install and uninstall
        custom_hooks = self.root / ".githooks"
        custom_hooks.mkdir()
        self._git("config", "core.hooksPath", ".githooks")
        ins3 = cli_json("install-git-hooks", "--root", str(self.root), "--with-pre-commit")
        self.assertEqual(Path(ins3["hooks_dir"]).resolve(), custom_hooks.resolve())
        self.assertTrue((custom_hooks / "post-checkout").is_file())
        self.assertTrue((custom_hooks / "pre-commit").is_file())

        un3 = cli_json("install-git-hooks", "--root", str(self.root), "--uninstall")
        self.assertEqual(un3["hooks"]["post-checkout"], "removed")
        self.assertFalse((custom_hooks / "post-checkout").exists())
        self.assertFalse((custom_hooks / "pre-commit").exists())

        # Unset `core.hooksPath` and uninstall from `.git/hooks` -> preserves pre-existing post-checkout
        self._git("config", "--unset", "core.hooksPath")
        un_def = cli_json("install-git-hooks", "--root", str(self.root), "--uninstall")
        self.assertEqual(un_def["hooks"]["post-checkout"], "cleaned")
        self.assertEqual(un_def["hooks"]["pre-commit"], "removed")
        self.assertTrue(existing_pc.is_file())
        self.assertIn("echo user-post-checkout", existing_pc.read_text(encoding="utf-8"))
        self.assertNotIn(aoci_index.GIT_HOOK_BEGIN, existing_pc.read_text(encoding="utf-8"))
        self.assertFalse((hooks_dir / "post-merge").exists())


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_post_checkout_hook_skips_file_checkout_arg3_zero(self):
        self.write("app.py", "def run():\n    return 1\n\n\nFLAG = True\n")
        self.init()
        ins = cli_json("install-git-hooks", "--root", str(self.root))
        self.assertTrue(ins["ok"])
        pc_path = self.root / ".git" / "hooks" / "post-checkout"
        pm_path = self.root / ".git" / "hooks" / "post-merge"
        pc_txt = pc_path.read_text(encoding="utf-8")
        pm_txt = pm_path.read_text(encoding="utf-8")
        self.assertIn('${3:-1}" = "1"', pc_txt)
        self.assertNotIn('${3:-1}', pm_txt)

        sh_bin = shutil.which("sh")
        if sh_bin:
            marker = self.root / ".aoci" / "hook_ran.txt"
            spy_script = self.root / "spy_restore.py"
            spy_script.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )
            ginfo = aoci_index.detect_git_info(self.root)
            from aoci_core import githooks as _gh

            _gh.install_git_hooks(self.root, ginfo, python_cmd=sys.executable, script_path=spy_script)
            # `$3 == 0` (single-file `git checkout -- <file>`) MUST skip restore
            r0 = subprocess.run([sh_bin, str(pc_path), "abc", "def", "0"], cwd=str(self.root), capture_output=True, text=True)
            self.assertEqual(r0.returncode, 0, r0.stderr)
            self.assertFalse(marker.exists())
            # `$3 == 1` (branch switch) MUST invoke restore
            r1 = subprocess.run([sh_bin, str(pc_path), "abc", "def", "1"], cwd=str(self.root), capture_output=True, text=True)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertTrue(marker.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
