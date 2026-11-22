#!/usr/bin/env python3
"""Cross-host installer: skill copies, hooks wiring, MCP registration, and uninstall."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from aoci_testkit import (  # noqa: E402
    HAS_GIT,
    INSTALL,
    RepoTestCase,
    SKILL_DIR,
    age_file,
    cli,
    cli_json,
)


class InstallAndNonGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AOCI_TEST_TMP"))
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_install(self, *args: str):
        r = subprocess.run(
            [sys.executable, str(INSTALL), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return r.returncode, r.stdout + r.stderr

    def test_install_manifest_guard_and_clean_uninstall(self):
        home = self.tmp / "home"
        proj = self.tmp / "proj"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo keep"}]}]}}),
            encoding="utf-8",
        )
        proj.mkdir()

        code, out = self.run_install(
            "--host", "both", "--scope", "global", "--home-dir", str(home), "--with-hooks", "--with-rules"
        )
        self.assertEqual(code, 0, out)
        claude_skill = home / ".claude" / "skills" / "aoci-index" / "SKILL.md"
        agy_hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))
        self.assertTrue(claude_skill.exists())
        self.assertIn("aoci-index-guard", agy_hooks)

        # Locally editing an installed file must block overwrite unless --force is passed
        claude_skill.write_text(claude_skill.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
        code, out = self.run_install("--host", "claude-code", "--scope", "global", "--home-dir", str(home))
        self.assertEqual(code, 1)
        self.assertIn("--force", out)
        code, out = self.run_install("--host", "claude-code", "--scope", "global", "--home-dir", str(home), "--force")
        self.assertEqual(code, 0, out)

        # Uninstall cleans skill, aoci hooks, and rule snippets while keeping unrelated user hooks
        code, out = self.run_install("--host", "both", "--scope", "global", "--home-dir", str(home), "--uninstall")
        self.assertEqual(code, 0, out)
        self.assertFalse(claude_skill.parent.exists())
        remaining = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(remaining["hooks"]["Stop"]), 1)
        self.assertIn("echo keep", remaining["hooks"]["Stop"][0]["hooks"][0]["command"])

        # Project-scoped Antigravity rule snippet round-trip
        code, out = self.run_install(
            "--host", "antigravity", "--scope", "project", "--project-root", str(proj), "--with-rules"
        )
        self.assertEqual(code, 0, out)
        self.assertIn("overview", (proj / "AGENTS.md").read_text(encoding="utf-8"))

    def test_install_skips_vcs_dirs_and_removes_readonly_files(self):
        home = self.tmp / "home"
        proj = self.tmp / "proj"
        home.mkdir()
        proj.mkdir()
        ro_paths = []

        def _make_ro(path: Path, data: str = "locked\n") -> Path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
            os.chmod(path, 0o444)
            ro_paths.append(path)
            return path

        try:
            # 1) Claude Code install must never copy `.git/` or cache/IDE/venv dirs
            code, out = self.run_install(
                "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home)
            )
            self.assertEqual(code, 0, out)
            cc_skill = proj / ".claude" / "skills" / "aoci-index"
            self.assertTrue((cc_skill / "SKILL.md").is_file())
            for skipped in (".git", ".hg", ".svn", ".aoci", ".venv", "venv", "__pycache__", "tests", ".idea"):
                self.assertFalse((cc_skill / skipped).exists(), f"{skipped} must not be copied to {cc_skill}")

            # `.bak` / `.tmp` / `.git/` files inside dest_dir are ignored by `_tree_hashes`
            _make_ro(cc_skill / "scratch.bak")
            _make_ro(cc_skill / "scratch.tmp")
            ro_cc_nested = _make_ro(cc_skill / ".git" / "objects" / "pack" / "pack-test.pack")

            # Read-only files (0o444) inside an existing Claude Code install must not fail --force reinstall or --uninstall
            ro_cc_top = _make_ro(cc_skill / "SKILL.md", "# locked\n")
            code, out = self.run_install(
                "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home), "--force"
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(ro_cc_nested.exists())
            self.assertFalse((cc_skill / ".git").exists())
            self.assertFalse((cc_skill / "scratch.bak").exists())
            self.assertTrue(ro_cc_top.is_file())

            _make_ro(cc_skill / "ro_uninstall.txt")
            code, out = self.run_install(
                "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home), "--uninstall"
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(cc_skill.exists())

            # 2) Antigravity `--as-plugin` install must also skip `.git/` and handle 0o444 files on --force and --uninstall
            code, out = self.run_install(
                "--host",
                "antigravity",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--as-plugin",
                "--with-hooks",
                "--with-rules",
                "--with-mcp",
            )
            self.assertEqual(code, 0, out)
            plugin_dir = proj / ".agents" / "plugins" / "aoci-index"
            plugin_skill = plugin_dir / "skills" / "aoci-index"
            self.assertTrue((plugin_skill / "SKILL.md").is_file())
            self.assertFalse((plugin_dir / ".git").exists())
            for skipped in (".git", ".hg", ".svn", ".aoci", ".venv", "venv", "__pycache__", "tests", ".idea"):
                self.assertFalse((plugin_skill / skipped).exists(), f"{skipped} must not be copied to {plugin_skill}")

            ro_plug_nested = _make_ro(plugin_skill / ".git" / "objects" / "aa" / "bbcc")
            _make_ro(plugin_skill / "SKILL.md", "# locked plugin\n")
            code, out = self.run_install(
                "--host",
                "antigravity",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--as-plugin",
                "--force",
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(ro_plug_nested.exists())
            self.assertFalse((plugin_skill / ".git").exists())
            self.assertTrue((plugin_skill / "SKILL.md").is_file())

            _make_ro(plugin_skill / "ro_plugin_uninstall.txt")
            code, out = self.run_install(
                "--host",
                "antigravity",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--as-plugin",
                "--uninstall",
            )
            self.assertEqual(code, 0, out)
            self.assertFalse(plugin_dir.exists())
        finally:
            for target in ro_paths:
                if target.exists():
                    try:
                        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass

    def test_non_git_workspace_honors_root_gitignore(self):
        ws = self.tmp / "nongit"
        ws.mkdir()
        (ws / ".gitignore").write_text("secret.env\ngen_out/\n", encoding="utf-8")
        (ws / "secret.env").write_text("TOKEN=1\n", encoding="utf-8")
        (ws / "gen_out").mkdir()
        (ws / "gen_out" / "x.py").write_text("x = 1\n", encoding="utf-8")
        (ws / "app.py").write_text("y = 2\n", encoding="utf-8")

        code, out = cli("init", "--root", str(ws))
        self.assertEqual(code, 0, out)
        scan = cli_json("scan", "--root", str(ws))
        added_paths = [it["path"] for it in scan["drift"]["added"]]
        self.assertIn("app.py", added_paths)
        self.assertNotIn("secret.env", added_paths)
        self.assertNotIn("gen_out/x.py", added_paths)

    def test_codex_install_global_project_runner_toml_mcp_and_clean_uninstall(self):
        # 0) Validate static Codex manual templates conform strictly to HooksFile schema
        for tpl_name in ("codex.hooks.global.json", "codex.hooks.project.json"):
            tpl = json.loads((SKILL_DIR / "resources" / tpl_name).read_text(encoding="utf-8"))
            self.assertLessEqual(set(tpl.keys()), {"description", "hooks"}, tpl_name)
            self.assertEqual(set(tpl["hooks"].keys()), {"SessionStart", "UserPromptSubmit", "Stop"}, tpl_name)

        # 1) Global install preserving existing Codex hooks, TOML tables, comments, and sibling MCP servers
        home = self.tmp / "codex_home"
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "hooks.json").write_text(
            json.dumps(
                {
                    "description": "Custom Codex hooks",
                    "hooks": {
                        "PreToolUse": [
                            {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "echo guard"}]}
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        (codex_dir / "config.toml").write_text(
            "# User global Codex config\n"
            'model = "gpt-5"\n\n'
            "[features]\n"
            "hooks = true\n\n"
            "[mcp_servers.other]\n"
            'command = "node"\n'
            'args = ["other.js"]\n',
            encoding="utf-8",
        )

        code, out = self.run_install(
            "--host", "codex", "--scope", "global", "--home-dir", str(home),
            "--with-hooks", "--with-rules", "--with-mcp",
        )
        self.assertEqual(code, 0, out)
        codex_skill = home / ".agents" / "skills" / "aoci-index" / "SKILL.md"
        self.assertTrue(codex_skill.is_file())
        self.assertIn("overview", (codex_dir / "AGENTS.md").read_text(encoding="utf-8"))

        hooks_cfg = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(hooks_cfg.get("description"), "Custom Codex hooks")
        self.assertEqual(hooks_cfg["hooks"]["PreToolUse"][0]["matcher"], "^Bash$")
        for ev in ("SessionStart", "UserPromptSubmit", "Stop"):
            self.assertEqual(len(hooks_cfg["hooks"][ev]), 1)
            self.assertNotIn("matcher", hooks_cfg["hooks"][ev][0])
            self.assertIn("--host codex", hooks_cfg["hooks"][ev][0]["hooks"][0]["command"])

        toml_text = (codex_dir / "config.toml").read_text(encoding="utf-8")
        self.assertIn("# User global Codex config", toml_text)
        self.assertIn("[mcp_servers.other]", toml_text)
        self.assertIn("[mcp_servers.aoci-index]", toml_text)
        if sys.version_info >= (3, 11):
            import tomllib

            parsed_toml = tomllib.loads(toml_text)
            self.assertEqual(parsed_toml["mcp_servers"]["other"]["command"], "node")
            aoci_srv = parsed_toml["mcp_servers"]["aoci-index"]
            self.assertTrue(any("aoci_mcp.py" in a for a in aoci_srv["args"]))

        # Re-install with custom interpreter updates hooks & MCP in-place without duplicating entries
        code, out = self.run_install(
            "--host", "codex", "--scope", "global", "--home-dir", str(home),
            "--python-cmd", "python-custom", "--with-hooks", "--with-mcp",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual((codex_dir / "config.toml").read_text(encoding="utf-8").count("[mcp_servers.aoci-index]"), 1)
        hooks_re = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("python-custom", hooks_re["hooks"]["SessionStart"][0]["hooks"][0]["command"])

        # Uninstall cleans Codex skill, AGENTS.md snippet, AOCI hooks, and MCP table while keeping user data
        code, out = self.run_install("--host", "codex", "--scope", "global", "--home-dir", str(home), "--uninstall")
        self.assertEqual(code, 0, out)
        self.assertFalse(codex_skill.parent.exists())
        self.assertFalse((codex_dir / "AGENTS.md").exists())
        hooks_after = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(hooks_after["hooks"].keys()), ["PreToolUse"])
        toml_after = (codex_dir / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("[mcp_servers.aoci-index]", toml_after)
        self.assertIn("[mcp_servers.other]", toml_after)
        self.assertIn("hooks = true", toml_after)
        if sys.version_info >= (3, 11):
            import tomllib

            parsed_after = tomllib.loads(toml_after)
            self.assertNotIn("aoci-index", parsed_after["mcp_servers"])
            self.assertIn("other", parsed_after["mcp_servers"])

        # 2) Project install in path with spaces: run generated hooks from root AND nested subdir via real OS shell
        proj = self.tmp / "codex proj"
        (proj / "nested" / "pkg").mkdir(parents=True)
        (proj / "nested" / "pkg" / "app.py").write_text("x = 1\n", encoding="utf-8")
        age_file(proj / "nested" / "pkg" / "app.py")
        cli("init", "--root", str(proj))
        cli("upsert", "--root", str(proj), "--entry", "nested/pkg/app.py[CX2T]: F:defines app x | R:- | A:- | S:-")

        py_arg = f'"{sys.executable}"' if " " in sys.executable else sys.executable
        code, out = self.run_install(
            "--host", "codex", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home),
            "--python-cmd", py_arg, "--with-hooks", "--with-rules", "--with-mcp",
        )
        self.assertEqual(code, 0, out)
        self.assertTrue((proj / ".agents" / "skills" / "aoci-index" / "SKILL.md").is_file())
        self.assertIn("overview", (proj / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("[mcp_servers.aoci-index]", (proj / ".codex" / "config.toml").read_text(encoding="utf-8"))

        proj_hooks = json.loads((proj / ".codex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        start_cmd = proj_hooks["SessionStart"][0]["hooks"][0]["command"]
        turn_cmd = proj_hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        stop_cmd = proj_hooks["Stop"][0]["hooks"][0]["command"]

        for work_dir in (proj, proj / "nested" / "pkg"):
            sp_start = subprocess.run(
                start_cmd,
                shell=True,
                cwd=str(work_dir),
                input=json.dumps({"cwd": str(work_dir), "session_id": f"s-{work_dir.name}", "hook_event_name": "SessionStart", "source": "startup"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(sp_start.returncode, 0, sp_start.stderr)
            out_start = json.loads(sp_start.stdout)
            self.assertIn("aoci_index.py", out_start["hookSpecificOutput"]["additionalContext"])

        # Turn-start and Stop from nested subdir after modifying app.py
        sp_turn = subprocess.run(
            turn_cmd,
            shell=True,
            cwd=str(proj / "nested" / "pkg"),
            input=json.dumps({"cwd": str(proj / "nested" / "pkg"), "session_id": "s-pkg", "turn_id": "t1", "hook_event_name": "UserPromptSubmit", "prompt": "edit"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(sp_turn.returncode, 0, sp_turn.stderr)
        self.assertEqual(json.loads(sp_turn.stdout), {})

        (proj / "nested" / "pkg" / "app.py").write_text("x = 99\n", encoding="utf-8")
        sp_stop = subprocess.run(
            stop_cmd,
            shell=True,
            cwd=str(proj / "nested" / "pkg"),
            input=json.dumps({"cwd": str(proj / "nested" / "pkg"), "session_id": "s-pkg", "turn_id": "t1", "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "ok"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(sp_stop.returncode, 0, sp_stop.stderr)
        stop_obj = json.loads(sp_stop.stdout)
        self.assertEqual(stop_obj["decision"], "block")
        self.assertNotIn("hookSpecificOutput", stop_obj)
        self.assertIn("nested/pkg/app.py", stop_obj["reason"])

        # `--no-turn-hook` removes UserPromptSubmit cleanly
        code, out = self.run_install(
            "--host", "codex", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home),
            "--with-hooks", "--no-turn-hook",
        )
        self.assertEqual(code, 0, out)
        proj_hooks_noturn = json.loads((proj / ".codex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.assertNotIn("UserPromptSubmit", proj_hooks_noturn)
        self.assertIn("SessionStart", proj_hooks_noturn)
        self.assertIn("Stop", proj_hooks_noturn)

        # 3) `--host all` + selective `--host codex --uninstall` keeps shared `.agents/skills/aoci-index` for Antigravity
        proj_all = self.tmp / "all_hosts_proj"
        proj_all.mkdir()
        code, out = self.run_install(
            "--host", "all", "--scope", "project", "--project-root", str(proj_all), "--home-dir", str(home),
            "--with-hooks", "--with-rules", "--with-mcp",
        )
        self.assertEqual(code, 0, out)
        self.assertTrue((proj_all / ".claude" / "skills" / "aoci-index" / "SKILL.md").is_file())
        self.assertTrue((proj_all / ".agents" / "skills" / "aoci-index" / "SKILL.md").is_file())
        self.assertTrue((proj_all / ".codex" / "hooks.json").is_file())
        self.assertTrue((proj_all / ".codex" / "config.toml").is_file())

        code, out = self.run_install(
            "--host", "codex", "--scope", "project", "--project-root", str(proj_all), "--home-dir", str(home), "--uninstall"
        )
        self.assertEqual(code, 0, out)
        self.assertFalse((proj_all / ".codex" / "hooks.json").exists())
        self.assertFalse((proj_all / ".codex" / "config.toml").exists())
        self.assertTrue((proj_all / ".agents" / "skills" / "aoci-index" / "SKILL.md").is_file())
        self.assertTrue((proj_all / "AGENTS.md").is_file())

        code, out = self.run_install(
            "--host", "all", "--scope", "project", "--project-root", str(proj_all), "--home-dir", str(home), "--uninstall"
        )
        self.assertEqual(code, 0, out)
        self.assertFalse((proj_all / ".claude" / "skills" / "aoci-index").exists())
        self.assertFalse((proj_all / ".agents" / "skills" / "aoci-index").exists())
        self.assertFalse((proj_all / "AGENTS.md").exists())


class P0RegressionTests(unittest.TestCase):

    @unittest.skipUnless(HAS_GIT, "git is required")
    def test_turn_hook_wiring_and_cross_platform_claude_project_runner(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            proj = tmp / "space in project"
            proj.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
            (proj / "app.py").write_text("x = 1\n", encoding="utf-8")
            age_file(proj / "app.py")
            cli("init", "--root", str(proj))
            cli("upsert", "--root", str(proj), "--entry", "app.py[CX2T]: F:defines app constant x | R:- | A:- | S:-")

            # Install project-scoped Claude Code hooks + Antigravity --with-turn-hook
            r = subprocess.run(
                [
                    sys.executable, str(INSTALL),
                    "--host", "both", "--scope", "project",
                    "--project-root", str(proj), "--home-dir", str(home),
                    "--python-cmd", f'"{sys.executable}"' if " " in sys.executable else sys.executable,
                    "--with-hooks", "--with-turn-hook",
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            cc_settings = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertIn("UserPromptSubmit", cc_settings["hooks"])
            agy_hooks = json.loads((proj / ".agents" / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("PreInvocation", agy_hooks["aoci-index-guard"])

            # Execute the generated Claude Code project SessionStart command through the native OS shell
            # from an unrelated working directory with CLAUDE_PROJECT_DIR containing spaces!
            cc_start_cmd = cc_settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj))
            shell_proc = subprocess.run(
                cc_start_cmd,
                shell=True,
                cwd=str(tmp),
                env=env,
                input=json.dumps({"cwd": str(proj), "session_id": "sp1", "hook_event_name": "SessionStart"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(shell_proc.returncode, 0, shell_proc.stderr)
            parsed = json.loads(shell_proc.stdout)
            ctx_msg = parsed["hookSpecificOutput"]["additionalContext"]
            self.assertIn("aoci_index.py", ctx_msg)
            self.assertIn(".claude/skills/aoci-index/scripts/aoci_index.py", ctx_msg)

            # `--batch-size` alias works on `scan`
            (proj / "b.py").write_text("b = 2\n", encoding="utf-8")
            scan_data = cli_json("scan", "--root", str(proj), "--batch-size", "5")
            self.assertEqual(len(scan_data["batches"]), 1)

            # Clean uninstall removes UserPromptSubmit as well
            r_un = subprocess.run(
                [
                    sys.executable, str(INSTALL),
                    "--host", "both", "--scope", "project",
                    "--project-root", str(proj), "--home-dir", str(home),
                    "--uninstall",
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
            cc_after = json.loads((proj / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertNotIn("hooks", cc_after)


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditFixesRegressionTests(RepoTestCase):

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=self.root,
            check=True,
        )

    def test_reinstall_and_uninstall_preserve_sibling_hooks_order_and_matcher_in_same_claude_group(self):
        home = Path(self._tmp.name) / "claude_home"
        r1 = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "global", "--home-dir", str(home), "--with-hooks"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)

        settings_path = home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        stop_group = settings["hooks"]["Stop"][0]
        aoci_handler = stop_group["hooks"][0]
        # Place custom commands BEFORE and AFTER AOCI in the SAME Stop group with a custom matcher
        stop_group["matcher"] = "CustomStopMatcher"
        stop_group["hooks"] = [
            {"type": "command", "command": "echo before-aoci"},
            aoci_handler,
            {"type": "command", "command": "echo after-aoci"},
        ]
        with settings_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(settings, indent=2) + "\n")

        # Re-install with --with-hooks: must replace ONLY the AOCI command in-place
        r2 = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "--host",
                "claude-code",
                "--scope",
                "global",
                "--home-dir",
                str(home),
                "--with-hooks",
                "--python-cmd",
                "python-custom",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        after_reinstall = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(len(after_reinstall["hooks"]["Stop"]), 1)
        grp_re = after_reinstall["hooks"]["Stop"][0]
        self.assertEqual(grp_re["matcher"], "CustomStopMatcher")
        cmds_re = [h["command"] for h in grp_re["hooks"]]
        self.assertEqual(len(cmds_re), 3)
        self.assertEqual(cmds_re[0], "echo before-aoci")
        self.assertIn("python-custom", cmds_re[1])
        self.assertIn("aoci_hook.py", cmds_re[1])
        self.assertEqual(cmds_re[2], "echo after-aoci")

        # Uninstall: must strip ONLY AOCI command while preserving sibling commands, order, and matcher
        r3 = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "global", "--home-dir", str(home), "--uninstall"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        after_un = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertNotIn("SessionStart", after_un["hooks"])
        self.assertNotIn("UserPromptSubmit", after_un["hooks"])
        self.assertEqual(len(after_un["hooks"]["Stop"]), 1)
        grp_un = after_un["hooks"]["Stop"][0]
        self.assertEqual(grp_un["matcher"], "CustomStopMatcher")
        self.assertEqual([h["command"] for h in grp_un["hooks"]], ["echo before-aoci", "echo after-aoci"])


@unittest.skipUnless(HAS_GIT, "git is required")
class Phase1FixesTests(RepoTestCase):

    def _git_at(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@e", *args],
            cwd=cwd,
            check=True,
        )

    def test_project_mcp_portability_plugin_root_and_no_tests_bloat(self):
        proj = Path(self._tmp.name) / "portable_proj"
        home = Path(self._tmp.name) / "portable_home"
        proj.mkdir(parents=True)
        home.mkdir(parents=True)

        # Install project-scoped for BOTH hosts with --with-mcp (without --as-plugin)
        r = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "--host",
                "both",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--python-cmd",
                sys.executable,
                "--with-mcp",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # `tests/` must NOT be copied into installed skill directories
        self.assertFalse((proj / ".agents" / "skills" / "aoci-index" / "tests").exists())
        self.assertFalse((proj / ".claude" / "skills" / "aoci-index" / "tests").exists())

        # Antigravity project MCP config must be under `.agents/plugins/aoci-index/mcp_config.json`, NOT `.agents/mcp_config.json`
        self.assertFalse((proj / ".agents" / "mcp_config.json").exists())
        agy_mcp_path = proj / ".agents" / "plugins" / "aoci-index" / "mcp_config.json"
        cc_mcp_path = proj / ".mcp.json"
        self.assertTrue(agy_mcp_path.is_file())
        self.assertTrue(cc_mcp_path.is_file())

        # Neither project MCP config may contain machine-specific absolute project paths
        agy_raw = agy_mcp_path.read_text(encoding="utf-8")
        cc_raw = cc_mcp_path.read_text(encoding="utf-8")
        self.assertNotIn(proj.resolve().as_posix(), agy_raw)
        self.assertNotIn(str(proj.resolve()), agy_raw)
        self.assertNotIn(proj.resolve().as_posix(), cc_raw)
        self.assertNotIn(str(proj.resolve()), cc_raw)
        self.assertIn("PLUGIN_ROOT", agy_raw)
        self.assertIn("CLAUDE_PROJECT_DIR", cc_raw)

        # Actually spawn the generated Antigravity plugin MCP server using PLUGIN_ROOT from an unrelated CWD
        agy_srv = json.loads(agy_raw)["mcpServers"]["aoci-index"]
        ping_req = json.dumps({"jsonrpc": "2.0", "id": 77, "method": "ping"}) + "\n"
        proc = subprocess.run(
            [agy_srv["command"], *agy_srv["args"]],
            cwd=str(home),
            env=dict(os.environ, PLUGIN_ROOT=str(proj / ".agents" / "plugins" / "aoci-index")),
            input=ping_req,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.strip())["id"], 77)

        # Clean uninstall removes both the skill and the lightweight MCP plugin wrapper
        r_un = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "--host",
                "both",
                "--scope",
                "project",
                "--project-root",
                str(proj),
                "--home-dir",
                str(home),
                "--uninstall",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
        self.assertFalse((proj / ".agents" / "plugins" / "aoci-index").exists())
        self.assertFalse(json.loads(cc_mcp_path.read_text(encoding="utf-8")).get("mcpServers"))


@unittest.skipUnless(HAS_GIT, "git is required")
class AuditV2RegressionTests(RepoTestCase):

    def test_p2_installer_split_python_cmd_and_antigravity_plugin_uninstall(self):
        home = Path(self._tmp.name) / "inst_home"
        proj = Path(self._tmp.name) / "inst_proj"
        home.mkdir()
        proj.mkdir()

        r1 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home),
                "--with-hooks", "--with-rules", "--with-mcp", "--as-plugin", "--python-cmd", "py -3",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        pdir = proj / ".agents" / "plugins" / "aoci-index"
        mcp_cfg = json.loads((pdir / "mcp_config.json").read_text(encoding="utf-8"))
        srv = mcp_cfg["mcpServers"]["aoci-index"]
        self.assertEqual(srv["command"], "py")
        self.assertEqual(srv["args"][0], "-3")
        self.assertIn(".agents/plugins/aoci-index", srv["args"][2])

        # Uninstall WITHOUT `--as-plugin` still cleanly removes the Antigravity plugin bundle
        r2 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home), "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertFalse(pdir.exists())


@unittest.skipUnless(HAS_GIT, "git is required")
class EmpiricalReviewRegressionTests(RepoTestCase):

    def test_install_refuses_unmanifested_nonempty_dir_and_preserves_second_mcp_on_plugin_uninstall(self):
        proj = Path(self._tmp.name) / "safe_proj"
        home = Path(self._tmp.name) / "safe_home"
        proj.mkdir()
        home.mkdir()

        # Repro A: manual install without `.aoci-install.json` + user notes must NOT be wiped without --force
        manual_dir = proj / ".claude" / "skills" / "aoci-index"
        manual_dir.mkdir(parents=True)
        (manual_dir / "SKILL.md").write_text("# manual skill\n", encoding="utf-8")
        (manual_dir / "user_notes.md").write_text("# keep my notes\n", encoding="utf-8")

        r_abort = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_abort.returncode, 1)
        self.assertIn("ownership cannot be verified", r_abort.stderr)
        self.assertTrue((manual_dir / "user_notes.md").is_file())

        r_force = subprocess.run(
            [sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "project", "--project-root", str(proj), "--home-dir", str(home), "--force"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_force.returncode, 0, r_force.stdout + r_force.stderr)
        self.assertFalse((manual_dir / "user_notes.md").exists())

        # Repro B: Antigravity plugin with user-added 2nd MCP server, sibling hook, custom rule, and note
        r_plug = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home),
                "--with-hooks", "--with-rules", "--with-mcp", "--as-plugin",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_plug.returncode, 0, r_plug.stdout + r_plug.stderr)
        pdir = proj / ".agents" / "plugins" / "aoci-index"

        mcp_path = pdir / "mcp_config.json"
        mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
        mcp_data["mcpServers"]["second-user-mcp"] = {"command": "node", "args": ["srv.js"]}
        mcp_path.write_text(json.dumps(mcp_data, indent=2), encoding="utf-8")

        hooks_path = pdir / "hooks.json"
        hooks_data = json.loads(hooks_path.read_text(encoding="utf-8"))
        hooks_data["my-custom-hook"] = {"SessionStart": [{"type": "command", "command": "echo hi"}]}
        hooks_path.write_text(json.dumps(hooks_data, indent=2), encoding="utf-8")

        agents_md = pdir / "rules" / "AGENTS.md"
        agents_md.write_text("# My Custom Rule\n\n" + agents_md.read_text(encoding="utf-8"), encoding="utf-8")

        # Uninstall MUST strip only AOCI keys and preserve the 2nd MCP server, sibling hook, and custom rule
        r_un = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj), "--home-dir", str(home), "--as-plugin", "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
        self.assertFalse((pdir / "skills" / "aoci-index").exists())
        self.assertTrue((pdir / "plugin.json").is_file())
        after_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index", after_mcp.get("mcpServers", {}))
        self.assertIn("second-user-mcp", after_mcp.get("mcpServers", {}))
        after_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index-guard", after_hooks)
        self.assertIn("my-custom-hook", after_hooks)
        self.assertIn("# My Custom Rule", agents_md.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index", agents_md.read_text(encoding="utf-8"))

        # Reinstall plugin cleanly in a 2nd project and verify uninstall WITHOUT `--as-plugin` also preserves 2nd MCP server
        proj2 = Path(self._tmp.name) / "safe_proj2"
        proj2.mkdir()
        r_plug2 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj2), "--home-dir", str(home),
                "--with-hooks", "--with-rules", "--with-mcp", "--as-plugin",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_plug2.returncode, 0, r_plug2.stdout + r_plug2.stderr)
        pdir2 = proj2 / ".agents" / "plugins" / "aoci-index"
        mcp_path2 = pdir2 / "mcp_config.json"
        mcp_data2 = json.loads(mcp_path2.read_text(encoding="utf-8"))
        mcp_data2["mcpServers"]["second-user-mcp"] = {"command": "node", "args": ["srv.js"]}
        mcp_path2.write_text(json.dumps(mcp_data2, indent=2), encoding="utf-8")
        r_un2 = subprocess.run(
            [
                sys.executable, str(INSTALL), "--host", "antigravity", "--scope", "project",
                "--project-root", str(proj2), "--home-dir", str(home), "--uninstall",
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r_un2.returncode, 0, r_un2.stdout + r_un2.stderr)
        after_mcp2 = json.loads(mcp_path2.read_text(encoding="utf-8"))
        self.assertNotIn("aoci-index", after_mcp2.get("mcpServers", {}))
        self.assertIn("second-user-mcp", after_mcp2.get("mcpServers", {}))

    def test_global_claude_hook_spaced_interpreter_and_spaced_home_cmd_c(self):
        self.write("app.py", "x = 1\n")
        self.init()
        self.upsert("app.py[CX2T]: F:defines app constant x | R:- | A:- | S:-")

        tmp_base = Path(self._tmp.name)
        home = tmp_base / "space home"
        home.mkdir()
        py_dir = tmp_base / "py dir"
        base_exe = getattr(sys, "_base_executable", "") or sys.executable
        if not Path(base_exe).is_file():
            base_exe = sys.executable
        real_py_dir = Path(base_exe).parent
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(py_dir), str(real_py_dir)], check=True, capture_output=True)
        else:
            os.symlink(str(real_py_dir), str(py_dir))
        try:
            spaced_py = py_dir / Path(base_exe).name
            quoted_py = f'"{spaced_py.as_posix()}"'
            r = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "claude-code",
                    "--scope",
                    "global",
                    "--home-dir",
                    str(home),
                    "--python-cmd",
                    quoted_py,
                    "--with-hooks",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            settings_path = home / ".claude" / "settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            start_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertTrue(start_cmd.startswith(quoted_py), start_cmd)
            self.assertIn('-c "import runpy;runpy.run_path(', start_cmd)
            self.assertIn("aoci_hook.py", start_cmd)

            shell_proc = subprocess.run(
                start_cmd,
                shell=True,
                cwd=str(self.root),
                input=json.dumps({"cwd": str(self.root), "session_id": "win-sp", "hook_event_name": "SessionStart"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(shell_proc.returncode, 0, shell_proc.stderr)
            parsed = json.loads(shell_proc.stdout)
            self.assertIn("aoci_index.py", parsed["hookSpecificOutput"]["additionalContext"])

            # Uninstall still matches and removes the `-c` hook cleanly
            r_un = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "claude-code",
                    "--scope",
                    "global",
                    "--home-dir",
                    str(home),
                    "--uninstall",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_un.returncode, 0, r_un.stdout + r_un.stderr)
            after_un = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertNotIn("hooks", after_un)

            # Unquoted `--python-cmd` keeps direct `"<script>"` invocation
            r_unq = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL),
                    "--host",
                    "claude-code",
                    "--scope",
                    "global",
                    "--home-dir",
                    str(home),
                    "--python-cmd",
                    "python3",
                    "--with-hooks",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(r_unq.returncode, 0, r_unq.stdout + r_unq.stderr)
            unq_cmd = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertTrue(unq_cmd.startswith('python3 "'), unq_cmd)
        finally:
            if os.name == "nt" and py_dir.exists():
                subprocess.run(["cmd", "/c", "rmdir", str(py_dir)], capture_output=True)
            elif py_dir.is_symlink():
                py_dir.unlink()


class McpProfileInstallTests(unittest.TestCase):
    def test_mcp_profile_flag_is_written_into_server_args(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("AOCI_TEST_TMP")) as td:
            base = Path(td)
            for profile, expect in (("core", ["--profile", "core"]), ("full", [])):
                proj = base / f"proj-{profile}"
                proj.mkdir()
                r = subprocess.run(
                    [
                        sys.executable, str(INSTALL), "--host", "claude-code", "--scope", "project",
                        "--project-root", str(proj), "--home-dir", str(base / "home"),
                        "--python-cmd", "python", "--with-mcp", "--mcp-profile", profile,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                args = json.loads((proj / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["aoci-index"]["args"]
                tail = args[-2:] if expect else args[-1:]
                if expect:
                    self.assertEqual(tail, expect)
                else:
                    self.assertNotIn("--profile", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
