"""Local Git hook installer & uninstaller (`post-checkout`, `post-merge`, `post-rewrite`, optional `pre-commit`)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .constants import (
    AOCI_DIR_NAME,
    CODE_FILE,
    GIT_HOOK_BEGIN,
    GIT_HOOK_END,
    MANAGED_GIT_HOOKS,
    write_text_lf,
)


def _default_run_git(args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def resolve_git_hooks_dir(
    root: Path,
    git_info: Dict[str, Any],
    run_git_fn: Optional[Callable[[Sequence[str], Path], Any]] = None,
) -> Optional[Path]:
    if not git_info.get("is_git"):
        return None
    toplevel = git_info.get("toplevel")
    base_dir = toplevel if isinstance(toplevel, Path) else root
    git_runner = run_git_fn or _default_run_git
    try:
        res = git_runner(["config", "--get", "core.hooksPath"], base_dir)
        if res.returncode == 0 and res.stdout.strip():
            hp = Path(res.stdout.strip())
            return hp if hp.is_absolute() else (base_dir / hp).resolve()
    except OSError:
        pass
    common_dir = git_info.get("common_dir")
    if isinstance(common_dir, Path):
        return common_dir / "hooks"
    idx_file = git_info.get("index_file")
    if isinstance(idx_file, Path):
        return idx_file.parent / "hooks"
    return None


def _format_hook_python_cmd(raw_py: Optional[str]) -> str:
    if not raw_py:
        return "python" if os.name == "nt" else "python3"
    s = raw_py.strip()
    if not s.startswith(('"', "'")):
        p = Path(s)
        if p.exists() or "/" in s or "\\" in s:
            posix = p.as_posix() if ("\\" in s or " " in s) else s
            return f'"{posix}"' if " " in posix else posix
    return s


def _strip_git_hook_marker(text: str) -> Tuple[str, bool]:
    lines = text.splitlines()
    out: List[str] = []
    in_block = False
    found = False
    for ln in lines:
        s = ln.strip()
        if s == GIT_HOOK_BEGIN:
            in_block = True
            found = True
            continue
        if in_block:
            if s == GIT_HOOK_END:
                in_block = False
            continue
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return ("\n".join(out) + "\n" if out else ""), found


def _upsert_git_hook_block(hook_path: Path, block: str, dry_run: bool) -> str:
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        cleaned, had_old = _strip_git_hook_marker(existing)
        prefix = cleaned if cleaned else "#!/bin/sh\n"
        if not prefix.endswith("\n"):
            prefix += "\n"
        if prefix.strip() != "#!/bin/sh":
            prefix += "\n"
        new_text = prefix + block
        action = "updated" if had_old else "appended"
    else:
        new_text = "#!/bin/sh\n" + block
        action = "created"

    if not dry_run:
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(hook_path, new_text)
        if os.name != "nt":
            try:
                mode = hook_path.stat().st_mode & 0o777
                os.chmod(hook_path, mode | 0o755)
            except OSError:
                pass
    return action


def install_git_hooks(
    root: Path,
    git_info: Dict[str, Any],
    *,
    python_cmd: Optional[str] = None,
    with_pre_commit: bool = False,
    pre_commit_verify: bool = False,
    dry_run: bool = False,
    script_path: Optional[Path] = None,
    run_git_fn: Optional[Callable[[Sequence[str], Path], Any]] = None,
) -> Dict[str, Any]:
    hooks_dir = resolve_git_hooks_dir(root, git_info, run_git_fn=run_git_fn)
    if hooks_dir is None:
        return {"ok": False, "error": f"`{root}` is not inside a Git repository."}
    toplevel = git_info.get("toplevel")
    sub_rel = "."
    if isinstance(toplevel, Path):
        try:
            rel_s = root.resolve().relative_to(toplevel.resolve()).as_posix()
            sub_rel = "." if rel_s in ("", ".") else rel_s
        except ValueError:
            sub_rel = "."

    py_tok = _format_hook_python_cmd(python_cmd)
    cli_path = script_path if script_path is not None else (Path(__file__).resolve().parent.parent / "aoci_index.py")
    script_posix = cli_path.resolve().as_posix()
    aoci_rel = f"{AOCI_DIR_NAME}" if sub_rel == "." else f"{sub_rel.strip('/')}/{AOCI_DIR_NAME}"

    post_checkout_block = "\n".join(
        [
            GIT_HOOK_BEGIN,
            f'if [ "${{3:-1}}" = "1" ] && [ -f "{sub_rel}/{AOCI_DIR_NAME}/{CODE_FILE}" ]; then',
            f'  {py_tok} "{script_posix}" restore --root "{sub_rel}" >/dev/null 2>&1 || true',
            "fi",
            GIT_HOOK_END,
        ]
    ) + "\n"

    restore_block = "\n".join(
        [
            GIT_HOOK_BEGIN,
            f'if [ -f "{sub_rel}/{AOCI_DIR_NAME}/{CODE_FILE}" ]; then',
            f'  {py_tok} "{script_posix}" restore --root "{sub_rel}" >/dev/null 2>&1 || true',
            "fi",
            GIT_HOOK_END,
        ]
    ) + "\n"

    actions: Dict[str, str] = {}
    actions["post-checkout"] = _upsert_git_hook_block(hooks_dir / "post-checkout", post_checkout_block, dry_run)
    for hook_name in ("post-merge", "post-rewrite"):
        actions[hook_name] = _upsert_git_hook_block(hooks_dir / hook_name, restore_block, dry_run)

    if with_pre_commit:
        pc_lines = [
            GIT_HOOK_BEGIN,
            f'if [ -n "$(git diff --cached --name-only -- "{aoci_rel}" 2>/dev/null)" ]; then',
            f'  echo "ERROR: Refusing to commit local AOCI index files under {aoci_rel}. Unstage with: git reset HEAD -- {aoci_rel}" >&2',
            "  exit 1",
            "fi",
        ]
        if pre_commit_verify:
            pc_lines.extend(
                [
                    f'if [ -f "{sub_rel}/{AOCI_DIR_NAME}/{CODE_FILE}" ]; then',
                    f'  {py_tok} "{script_posix}" verify --fast --root "{sub_rel}" || exit 1',
                    "fi",
                ]
            )
        pc_lines.append(GIT_HOOK_END)
        actions["pre-commit"] = _upsert_git_hook_block(hooks_dir / "pre-commit", "\n".join(pc_lines) + "\n", dry_run)

    return {
        "ok": True,
        "action": "install",
        "dry_run": dry_run,
        "root": str(root),
        "hooks_dir": str(hooks_dir),
        "hooks": actions,
    }


def uninstall_git_hooks(
    root: Path,
    git_info: Dict[str, Any],
    *,
    dry_run: bool = False,
    run_git_fn: Optional[Callable[[Sequence[str], Path], Any]] = None,
) -> Dict[str, Any]:
    hooks_dir = resolve_git_hooks_dir(root, git_info, run_git_fn=run_git_fn)
    if hooks_dir is None:
        return {"ok": False, "error": f"`{root}` is not inside a Git repository."}
    actions: Dict[str, str] = {}
    for hook_name in MANAGED_GIT_HOOKS:
        hp = hooks_dir / hook_name
        if not hp.is_file():
            continue
        cleaned, found = _strip_git_hook_marker(hp.read_text(encoding="utf-8", errors="replace"))
        if not found:
            actions[hook_name] = "skipped"
            continue
        non_empty = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        only_shebang = all(ln.startswith("#!") and "sh" in ln for ln in non_empty)
        if not non_empty or only_shebang:
            actions[hook_name] = "removed"
            if not dry_run:
                hp.unlink()
        else:
            actions[hook_name] = "cleaned"
            if not dry_run:
                write_text_lf(hp, cleaned)
    return {
        "ok": True,
        "action": "uninstall",
        "dry_run": dry_run,
        "root": str(root),
        "hooks_dir": str(hooks_dir),
        "hooks": actions,
    }
