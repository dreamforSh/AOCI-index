#!/usr/bin/env python3
"""
Cross-Host Installer & Uninstaller for the `aoci-index` Skill (`aoci_core.installer` / `scripts/install.py`).

Supports installing and uninstalling `aoci-index` for:
  - Claude Code  (`~/.claude/skills/aoci-index` or `<repo>/.claude/skills/aoci-index`)
                 + optional hooks in `settings.json` + optional `CLAUDE.md` snippet
                 + optional MCP in `~/.claude.json` / `<repo>/.mcp.json`
  - Antigravity  (`~/.gemini/config/skills/aoci-index` or `<repo>/.agents/skills/aoci-index`)
                 + optional hooks in `hooks.json` + optional `AGENTS.md` / `GEMINI.md` snippet
                 + optional MCP in `mcp_config.json` / `.agents/plugins/aoci-index`
  - Codex        (`~/.agents/skills/aoci-index` or `<repo>/.agents/skills/aoci-index`)
                 + optional hooks in `~/.codex/hooks.json` / `<repo>/.codex/hooks.json`
                 + optional `AGENTS.md` snippet (`~/.codex/AGENTS.md` / `<repo>/AGENTS.md`)
                 + optional MCP in `~/.codex/config.toml` / `<repo>/.codex/config.toml`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.dont_write_bytecode = True

SKILL_NAME = "aoci-index"
AGY_HOOK_KEY = "aoci-index-guard"
CLAUDE_HOOK_MARKER = "aoci_hook.py"
INSTALL_MANIFEST = ".aoci-install.json"
_SKIP_DIR_NAMES = {
    "__pycache__",
    "_skill_bundle",
    "site-packages",
    "tests",
    "build",
    "dist",
    ".git",
    ".hg",
    ".svn",
    ".aoci",
    ".github",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".vscode",
    ".idea",
}
COPY_IGNORE = tuple(sorted(_SKIP_DIR_NAMES)) + (
    "*.pyc",
    "*.pyo",
    "*.bak",
    "*.orig.bak",
    "*.tmp",
    "*.egg-info",
    ".DS_Store",
    INSTALL_MANIFEST,
)

RULE_BEGIN = "<!-- >>> aoci-index -->"
RULE_END = "<!-- <<< aoci-index -->"
TOML_MCP_BEGIN = "# >>> aoci-index mcp"
TOML_MCP_END = "# <<< aoci-index mcp"


def is_valid_skill_source(cand: Optional[Path]) -> bool:
    """Verify `cand` is an actual AOCI skill tree (`SKILL.md` + `scripts/aoci_index.py`)."""
    if cand is None:
        return False
    try:
        return (
            cand.is_dir()
            and (cand / "SKILL.md").is_file()
            and (cand / "scripts" / "aoci_index.py").is_file()
        )
    except OSError:
        return False


def resolve_source_dir(anchor_file: Optional[Path] = None) -> Optional[Path]:
    """
    Locate the verified skill bundle directory across:
      1) Repo / skill checkout (`scripts/aoci_core/installer.py` -> `../../..`, `scripts/install.py` -> `../..`)
      2) Packaged wheel bundle (`site-packages/aoci_core/_skill_bundle`)
    Returns `None` if no verified skill tree is found (never falls back to `site-packages` or stdlib `Lib`).
    """
    here = (anchor_file or Path(__file__)).resolve()
    candidates = [
        here.parent / "_skill_bundle",
        here.parent.parent,
        here.parent.parent.parent,
    ]
    # Prefer repo checkout when running directly inside a repo/skill tree, else wheel `_skill_bundle`
    ordered = [
        here.parent.parent,
        here.parent.parent.parent,
        here.parent / "_skill_bundle",
    ]
    del candidates
    for cand in ordered:
        if is_valid_skill_source(cand):
            return cand
    return None


SOURCE_DIR: Optional[Path] = resolve_source_dir()


def _require_source_dir() -> Path:
    src = SOURCE_DIR if is_valid_skill_source(SOURCE_DIR) else resolve_source_dir()
    if src is None:
        raise RuntimeError(
            "Valid `aoci-index` skill bundle (`SKILL.md` + `scripts/aoci_index.py`) could not be located. "
            "Reinstall `aoci-index` from a complete wheel/sdist or run `scripts/install.py` from the repository."
        )
    return src


def configure_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def default_python_cmd(scope: str = "project") -> str:
    """
    Project-scoped hooks may be shared via the repo, so they use a portable `python`/`python3`.
    Global hooks use the absolute base interpreter running the installer (avoids Windows Store
    `python` stubs and PATH drift), but never a virtualenv interpreter that may be deleted later.
    """
    portable = "python" if os.name == "nt" else "python3"
    if scope != "global":
        return portable
    exe = sys.executable
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        exe = getattr(sys, "_base_executable", "") or ""
    if not exe or not Path(exe).exists():
        return portable
    exe = Path(exe).as_posix()
    return f'"{exe}"' if " " in exe else exe


def _tree_hashes(base: Path) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    if not base.exists():
        return hashes
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        parts = rel.split("/")
        if (
            any(seg in _SKIP_DIR_NAMES or seg.endswith(".egg-info") for seg in parts)
            or rel.endswith((".pyc", ".pyo", ".bak", ".tmp"))
            or p.name in (".DS_Store", INSTALL_MANIFEST)
        ):
            continue
        hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def locally_modified_files(dest_dir: Path) -> Optional[List[str]]:
    """Files in an existing install that differ from what the installer last wrote (None = no manifest)."""
    manifest_path = dest_dir / INSTALL_MANIFEST
    if not manifest_path.exists():
        return None
    try:
        recorded: Dict[str, str] = json.loads(manifest_path.read_text(encoding="utf-8-sig")).get("files", {})
    except (OSError, ValueError):
        return None
    current = _tree_hashes(dest_dir)
    changed = [rel for rel, h in current.items() if recorded.get(rel) != h]
    changed += [rel for rel in recorded if rel not in current]
    return sorted(set(changed))


def load_json_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected top-level JSON object in `{path}`.")
    return data


def write_text_lf(path: Path, text: str) -> None:
    """Write UTF-8 LF text using Python 3.8-compatible `Path.open(..., newline='\\n')`."""
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_text_with_backup(path: Path, rendered: str, dry_run: bool, dry_run_label: str = "write file") -> None:
    if dry_run:
        print(f"  [dry-run] {dry_run_label}: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        orig_backup = path.with_name(path.name + ".orig.bak")
        if not orig_backup.exists():
            shutil.copy2(path, orig_backup)
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    write_text_lf(tmp, rendered)
    tmp.replace(path)


def save_json_dict(path: Path, data: Dict[str, Any], dry_run: bool) -> None:
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _write_text_with_backup(path, rendered, dry_run, dry_run_label="write JSON")


def _dir_has_any_entries(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return True


def _check_overwrite_guard(dest_dir: Path, force: bool) -> bool:
    """
    Refuse to overwrite an existing install whose files were modified OR whose ownership cannot be
    verified (e.g. non-empty directory lacking `.aoci-install.json`), unless `--force` is passed.
    """
    if not dest_dir.exists() or force:
        return True
    changed = locally_modified_files(dest_dir)
    if changed is None:
        if not dest_dir.is_dir() or _dir_has_any_entries(dest_dir):
            print(
                f"  [abort] {dest_dir} already exists without `{INSTALL_MANIFEST}` "
                "(ownership cannot be verified). Re-run with --force to overwrite.",
                file=sys.stderr,
            )
            return False
        return True
    if changed:
        print(
            f"  [abort] {dest_dir} has {len(changed)} locally modified file(s) since the last install "
            f"(e.g. {', '.join(changed[:3])}). Re-run with --force to overwrite.",
            file=sys.stderr,
        )
        return False
    return True


def _rmtree_ro(path: Path) -> None:
    """Remove a directory tree, clearing read-only attributes before retrying."""

    def _handle_ro(func: Any, fpath: str, _exc: Any) -> None:
        os.chmod(fpath, stat.S_IWRITE)
        func(fpath)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_handle_ro)
    else:
        shutil.rmtree(path, onerror=_handle_ro)


def copy_skill_tree(dest_dir: Path, dry_run: bool, force: bool = False) -> bool:
    try:
        src_dir = _require_source_dir()
    except RuntimeError as exc:
        print(f"  [abort] {exc}", file=sys.stderr)
        return False
    if dest_dir.exists() and dest_dir.resolve() == src_dir.resolve():
        print(f"  [skip] destination equals source: {dest_dir}")
        return True
    if not _check_overwrite_guard(dest_dir, force):
        return False
    if dry_run:
        print(f"  [dry-run] copy skill `{src_dir}` -> `{dest_dir}`")
        return True
    if dest_dir.exists():
        _rmtree_ro(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dest_dir, ignore=shutil.ignore_patterns(*COPY_IGNORE))
    write_text_lf(
        dest_dir / INSTALL_MANIFEST,
        json.dumps({"skill": SKILL_NAME, "source": str(src_dir), "files": _tree_hashes(dest_dir)}, indent=1),
    )
    print(f"  [ok] installed skill -> {dest_dir}")
    return True


def remove_skill_tree(dest_dir: Path, dry_run: bool) -> None:
    if not dest_dir.exists():
        print(f"  [skip] skill directory not present: {dest_dir}")
        return
    src_dir = SOURCE_DIR if is_valid_skill_source(SOURCE_DIR) else resolve_source_dir()
    if src_dir is not None and dest_dir.resolve() == src_dir.resolve():
        print(f"  [skip] refusing to delete source directory: {dest_dir}")
        return
    if dry_run:
        print(f"  [dry-run] remove skill directory: {dest_dir}")
        return
    _rmtree_ro(dest_dir)
    print(f"  [ok] removed skill -> {dest_dir}")


def _is_aoci_hook_handler(handler: Any) -> bool:
    return isinstance(handler, dict) and CLAUDE_HOOK_MARKER in str(handler.get("command", ""))


def _upsert_aoci_in_handler_list(handlers: List[Any], new_handler: Dict[str, Any]) -> List[Any]:
    out: List[Any] = []
    replaced = False
    for h in handlers:
        if _is_aoci_hook_handler(h):
            if not replaced:
                out.append(new_handler)
                replaced = True
        else:
            out.append(h)
    if not replaced:
        out.append(new_handler)
    return out


def upsert_antigravity_hooks(
    hooks_json: Path,
    py_cmd: str,
    dry_run: bool,
    with_turn_hook: bool = False,
) -> None:
    data = load_json_dict(hooks_json)
    existing_spec = data.get(AGY_HOOK_KEY)
    spec: Dict[str, Any] = dict(existing_spec) if isinstance(existing_spec, dict) else {}

    desired_agy: Dict[str, str] = {
        "SessionStart": f"{py_cmd} skills/{SKILL_NAME}/scripts/aoci_hook.py --host antigravity --event session-start",
    }
    if with_turn_hook:
        desired_agy["PreInvocation"] = (
            f"{py_cmd} skills/{SKILL_NAME}/scripts/aoci_hook.py --host antigravity --event turn-start"
        )
    elif isinstance(spec.get("PreInvocation"), list):
        kept_turn = [h for h in spec["PreInvocation"] if not _is_aoci_hook_handler(h)]
        if kept_turn:
            spec["PreInvocation"] = kept_turn
        else:
            del spec["PreInvocation"]
    desired_agy["Stop"] = f"{py_cmd} skills/{SKILL_NAME}/scripts/aoci_hook.py --host antigravity --event stop"

    for ev_name, cmd_str in desired_agy.items():
        cur_list = spec.get(ev_name)
        if not isinstance(cur_list, list):
            cur_list = []
        spec[ev_name] = _upsert_aoci_in_handler_list(
            cur_list,
            {"type": "command", "command": cmd_str, "timeout": 10},
        )

    data[AGY_HOOK_KEY] = spec
    save_json_dict(hooks_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] merged Antigravity hooks -> {hooks_json}")


def remove_antigravity_hooks(hooks_json: Path, dry_run: bool, remove_if_empty: bool = False) -> None:
    if not hooks_json.exists():
        print(f"  [skip] hooks file not present: {hooks_json}")
        return
    data = load_json_dict(hooks_json)
    if AGY_HOOK_KEY not in data:
        print(f"  [skip] `{AGY_HOOK_KEY}` not found in {hooks_json}")
        return
    spec = data.get(AGY_HOOK_KEY)
    if isinstance(spec, dict):
        for ev_name in list(spec.keys()):
            val = spec.get(ev_name)
            if isinstance(val, list):
                kept = [h for h in val if not _is_aoci_hook_handler(h)]
                if kept:
                    spec[ev_name] = kept
                else:
                    del spec[ev_name]
        if not spec or set(spec.keys()) <= {"enabled"}:
            del data[AGY_HOOK_KEY]
    else:
        del data[AGY_HOOK_KEY]
    if remove_if_empty and not data:
        if dry_run:
            print(f"  [dry-run] remove empty hooks config: {hooks_json}")
            return
        hooks_json.unlink()
        print(f"  [ok] removed empty hooks config {hooks_json}")
        return
    save_json_dict(hooks_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] removed `{AGY_HOOK_KEY}` from {hooks_json}")


def _strip_aoci_from_claude_groups(groups: List[Any]) -> Tuple[List[Any], bool]:
    """
    Remove only AOCI commands from Claude Code hook groups, preserving sibling custom commands,
    their relative order, and the group's `matcher`. Drops a group only when its `hooks` list becomes empty.
    """
    cleaned_groups: List[Any] = []
    changed = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            cleaned_groups.append(group)
            continue
        orig_hooks = group["hooks"]
        kept_hooks = [h for h in orig_hooks if not _is_aoci_hook_handler(h)]
        if len(kept_hooks) != len(orig_hooks):
            changed = True
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                cleaned_groups.append(new_group)
        else:
            cleaned_groups.append(group)
    return cleaned_groups, changed


def _upsert_aoci_in_claude_groups(
    groups: List[Any],
    new_handler: Dict[str, Any],
    include_matcher: bool = True,
) -> List[Any]:
    """
    Replace an existing AOCI command in-place inside its hook group (preserving sibling
    commands, order, and `matcher`), or append a new group when AOCI is not yet present.
    """
    out_groups: List[Any] = []
    replaced = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            out_groups.append(group)
            continue
        orig_hooks = group["hooks"]
        if not any(_is_aoci_hook_handler(h) for h in orig_hooks):
            out_groups.append(group)
            continue
        new_hooks: List[Any] = []
        for h in orig_hooks:
            if _is_aoci_hook_handler(h):
                if not replaced:
                    new_hooks.append(new_handler)
                    replaced = True
            else:
                new_hooks.append(h)
        if new_hooks:
            new_group = dict(group)
            new_group["hooks"] = new_hooks
            out_groups.append(new_group)

    if not replaced:
        new_group_entry: Dict[str, Any] = {"hooks": [new_handler]}
        if include_matcher:
            new_group_entry = {"matcher": "", "hooks": [new_handler]}
        out_groups.append(new_group_entry)
    return out_groups


def _global_hook_runner(skill_dest: Path, py_cmd: str) -> str:
    script_posix = (skill_dest / "scripts" / "aoci_hook.py").resolve().as_posix()
    if py_cmd.startswith('"') and " " in script_posix:
        return f'{py_cmd} -c "import runpy;runpy.run_path({script_posix!r},run_name=\'__main__\')"'
    return f'{py_cmd} "{script_posix}"'


def _claude_project_runner(py_cmd: str) -> str:
    """
    Build a shell-neutral Python hook command for project-scoped Claude Code installs.
    Reads `CLAUDE_PROJECT_DIR` via `os.environ` inside Python and emits `{}` safely if the skill
    directory is absent (e.g. after `--uninstall --keep-config`).
    """
    rel_hook = f".claude/skills/{SKILL_NAME}/scripts/aoci_hook.py"
    return (
        f'{py_cmd} -c "import os,runpy,sys;'
        f"p=os.path.join(os.environ.get('CLAUDE_PROJECT_DIR','.'),'{rel_hook}');"
        f"runpy.run_path(p,run_name='__main__') if os.path.isfile(p) else sys.stdout.write('{{}}')\""
    )


def _codex_project_expr(script_name: str, emit_empty_json: bool = False) -> str:
    rel_agents = f".agents/skills/{SKILL_NAME}/scripts/{script_name}"
    rel_codex = f".codex/skills/{SKILL_NAME}/scripts/{script_name}"
    if emit_empty_json:
        return (
            "import pathlib,runpy,sys;"
            f"p=next((d/r for d in (pathlib.Path.cwd(),*pathlib.Path.cwd().parents) "
            f"for r in ({rel_agents!r},{rel_codex!r}) if (d/r).is_file()),None);"
            "runpy.run_path(str(p),run_name='__main__') if p else sys.stdout.write('{}')"
        )
    return (
        "import pathlib,runpy;"
        f"p=next((d/r for d in (pathlib.Path.cwd(),*pathlib.Path.cwd().parents) "
        f"for r in ({rel_agents!r},{rel_codex!r}) if (d/r).is_file()),None);"
        "p and runpy.run_path(str(p),run_name='__main__')"
    )


def _codex_project_runner(py_cmd: str) -> str:
    """
    Shell-neutral Python hook runner for project-scoped Codex installs.
    Walks upward from CWD to locate `.agents/skills/aoci-index/scripts/aoci_hook.py`
    (falling back to `.codex/skills/aoci-index/scripts/aoci_hook.py`) and emits `{}` if absent.
    """
    return f'{py_cmd} -c "{_codex_project_expr("aoci_hook.py", emit_empty_json=True)}"'


def _codex_project_mcp_args() -> List[str]:
    return ["-c", _codex_project_expr("aoci_mcp.py", emit_empty_json=False)]


def _merge_standard_hooks(
    hooks_obj: Dict[str, Any],
    runner: str,
    host_flag: str,
    with_turn_hook: bool,
    include_matcher: bool,
) -> None:
    desired_events: Dict[str, str] = {
        "SessionStart": f"{runner} --host {host_flag} --event session-start",
    }
    if with_turn_hook:
        desired_events["UserPromptSubmit"] = f"{runner} --host {host_flag} --event turn-start"
    elif isinstance(hooks_obj.get("UserPromptSubmit"), list):
        cleaned_turn, _ = _strip_aoci_from_claude_groups(hooks_obj["UserPromptSubmit"])
        if cleaned_turn:
            hooks_obj["UserPromptSubmit"] = cleaned_turn
        else:
            del hooks_obj["UserPromptSubmit"]
    desired_events["Stop"] = f"{runner} --host {host_flag} --event stop"

    for ev_name, cmd_str in desired_events.items():
        existing = hooks_obj.get(ev_name)
        if not isinstance(existing, list):
            existing = []
        hooks_obj[ev_name] = _upsert_aoci_in_claude_groups(
            existing,
            {
                "type": "command",
                "command": cmd_str,
                "timeout": 10,
            },
            include_matcher=include_matcher,
        )


def _strip_hooks_dict(hooks_obj: Dict[str, Any]) -> bool:
    changed = False
    for ev_name in list(hooks_obj.keys()):
        existing = hooks_obj.get(ev_name)
        if isinstance(existing, list):
            cleaned, ev_changed = _strip_aoci_from_claude_groups(existing)
            if ev_changed:
                changed = True
                if cleaned:
                    hooks_obj[ev_name] = cleaned
                else:
                    del hooks_obj[ev_name]
    return changed


def upsert_claude_hooks(
    settings_json: Path,
    skill_dest: Path,
    scope: str,
    py_cmd: str,
    dry_run: bool,
    with_turn_hook: bool = True,
) -> None:
    data = load_json_dict(settings_json)
    hooks_obj = data.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        raise ValueError(f"`hooks` key in `{settings_json}` must be a JSON object.")

    runner = _claude_project_runner(py_cmd) if scope == "project" else _global_hook_runner(skill_dest, py_cmd)
    _merge_standard_hooks(hooks_obj, runner, "claude-code", with_turn_hook=with_turn_hook, include_matcher=True)

    save_json_dict(settings_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] merged Claude Code hooks -> {settings_json}")


def remove_claude_hooks(settings_json: Path, dry_run: bool) -> None:
    if not settings_json.exists():
        print(f"  [skip] settings file not present: {settings_json}")
        return
    data = load_json_dict(settings_json)
    hooks_obj = data.get("hooks")
    if not isinstance(hooks_obj, dict):
        print(f"  [skip] no `hooks` block in {settings_json}")
        return

    if not _strip_hooks_dict(hooks_obj):
        print(f"  [skip] no `aoci-index` hooks found in {settings_json}")
        return
    if not hooks_obj:
        del data["hooks"]
    save_json_dict(settings_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] removed Claude Code `aoci-index` hooks from {settings_json}")


def upsert_codex_hooks(
    hooks_json: Path,
    skill_dest: Path,
    scope: str,
    py_cmd: str,
    dry_run: bool,
    with_turn_hook: bool = True,
) -> None:
    data = load_json_dict(hooks_json)
    hooks_obj = data.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        raise ValueError(f"`hooks` key in `{hooks_json}` must be a JSON object.")

    runner = _codex_project_runner(py_cmd) if scope == "project" else _global_hook_runner(skill_dest, py_cmd)
    _merge_standard_hooks(hooks_obj, runner, "codex", with_turn_hook=with_turn_hook, include_matcher=False)

    save_json_dict(hooks_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] merged Codex hooks -> {hooks_json}")
        if scope == "project":
            print("  [note] In Codex CLI, review and trust project hooks via `/hooks` before first run.")


def remove_codex_hooks(hooks_json: Path, dry_run: bool, remove_if_empty: bool = True) -> None:
    if not hooks_json.exists():
        print(f"  [skip] hooks file not present: {hooks_json}")
        return
    data = load_json_dict(hooks_json)
    hooks_obj = data.get("hooks")
    if not isinstance(hooks_obj, dict) or not _strip_hooks_dict(hooks_obj):
        print(f"  [skip] no `aoci-index` hooks found in {hooks_json}")
        return
    if not hooks_obj:
        del data["hooks"]
    if remove_if_empty and not data:
        if dry_run:
            print(f"  [dry-run] remove empty hooks config: {hooks_json}")
            return
        hooks_json.unlink()
        print(f"  [ok] removed empty hooks config {hooks_json}")
        return
    save_json_dict(hooks_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] removed Codex `aoci-index` hooks from {hooks_json}")


def upsert_rule_snippet(rule_file: Path, snippet_path: Path, dry_run: bool) -> None:
    snippet = snippet_path.read_text(encoding="utf-8-sig").strip()
    fenced = f"{RULE_BEGIN}\n{snippet}\n{RULE_END}\n"
    existing = rule_file.read_text(encoding="utf-8-sig", errors="replace") if rule_file.exists() else ""

    pattern = re.compile(rf"{re.escape(RULE_BEGIN)}.*?{re.escape(RULE_END)}\r?\n?", re.DOTALL)
    if pattern.search(existing):
        updated = pattern.sub(fenced, existing)
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        blank = "\n" if existing and not existing.endswith("\n\n") else ""
        updated = existing + sep + blank + fenced

    if dry_run:
        print(f"  [dry-run] update rule snippet: {rule_file}")
        return
    rule_file.parent.mkdir(parents=True, exist_ok=True)
    write_text_lf(rule_file, updated)
    print(f"  [ok] updated rule snippet -> {rule_file}")


def remove_rule_snippet(rule_file: Path, dry_run: bool) -> None:
    if not rule_file.exists():
        print(f"  [skip] rule file not present: {rule_file}")
        return
    existing = rule_file.read_text(encoding="utf-8-sig", errors="replace")
    pattern = re.compile(rf"\r?\n?{re.escape(RULE_BEGIN)}.*?{re.escape(RULE_END)}\r?\n?", re.DOTALL)
    if not pattern.search(existing):
        print(f"  [skip] no `aoci-index` snippet in {rule_file}")
        return
    updated = pattern.sub("\n", existing).lstrip("\n")
    if dry_run:
        print(f"  [dry-run] remove rule snippet from: {rule_file}")
        return
    if not updated.strip():
        rule_file.unlink()
        print(f"  [ok] removed empty rule file {rule_file}")
        return
    write_text_lf(rule_file, updated)
    print(f"  [ok] removed rule snippet from {rule_file}")


def _split_python_cmd(py_cmd: str) -> Tuple[str, List[str]]:
    """Split a Python invocation string (e.g. `'py -3'` or `'\"C:/Program Files/py.exe\"'`) into `(exe, args)`."""
    raw = (py_cmd or "").strip()
    if not raw:
        return ("python" if os.name == "nt" else "python3"), []
    tokens = shlex.split(raw, posix=(os.name != "nt"))
    cleaned: List[str] = []
    for tok in tokens:
        if len(tok) >= 2 and ((tok[0] == tok[-1] == '"') or (tok[0] == tok[-1] == "'")):
            tok = tok[1:-1]
        if tok:
            cleaned.append(tok)
    if not cleaned:
        return ("python" if os.name == "nt" else "python3"), []
    return cleaned[0], cleaned[1:]


def _portable_env_mcp_args(env_var: str, rel_script: str, fallback_base: str = ".") -> List[str]:
    expr = (
        "import os,runpy;"
        f"p=os.path.normpath(os.path.join(os.environ.get({env_var!r},{fallback_base!r}),{rel_script!r}));"
        "os.path.isfile(p) and runpy.run_path(p,run_name='__main__')"
    )
    return ["-c", expr]


def _upsert_mcp_server(mcp_json: Path, py_cmd: str, mcp_args: List[str], dry_run: bool) -> None:
    data = load_json_dict(mcp_json)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"`mcpServers` key in `{mcp_json}` must be a JSON object.")
    cmd_exe, prefix_args = _split_python_cmd(py_cmd)
    servers[SKILL_NAME] = {
        "command": cmd_exe,
        "args": [*prefix_args, *mcp_args],
    }
    save_json_dict(mcp_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] merged MCP server `{SKILL_NAME}` -> {mcp_json}")


def _remove_mcp_server(mcp_json: Path, dry_run: bool, remove_if_empty: bool = False) -> None:
    if not mcp_json.exists():
        print(f"  [skip] MCP config not present: {mcp_json}")
        return
    data = load_json_dict(mcp_json)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SKILL_NAME not in servers:
        print(f"  [skip] `{SKILL_NAME}` not found in {mcp_json}")
        return
    del servers[SKILL_NAME]
    if not servers and list(data.keys()) == ["mcpServers"]:
        data.pop("mcpServers", None)
    if remove_if_empty and not data:
        if dry_run:
            print(f"  [dry-run] remove empty MCP config: {mcp_json}")
            return
        mcp_json.unlink()
        print(f"  [ok] removed empty MCP config {mcp_json}")
        return
    save_json_dict(mcp_json, data, dry_run)
    if not dry_run:
        print(f"  [ok] removed MCP server `{SKILL_NAME}` from {mcp_json}")


_TOML_FENCED_MCP_RE = re.compile(
    rf"(?ms)^[ \t]*{re.escape(TOML_MCP_BEGIN)}\r?\n.*?^[ \t]*{re.escape(TOML_MCP_END)}[ \t]*\r?\n?"
)
_TOML_TABLE_MCP_RE = re.compile(
    rf"(?ms)^[ \t]*\[mcp_servers\.(?:{re.escape(SKILL_NAME)}|\"{re.escape(SKILL_NAME)}\"|'{re.escape(SKILL_NAME)}')\][ \t]*(?:#[^\r\n]*)?\r?\n.*?(?=^[ \t]*\[(?!mcp_servers\.(?:{re.escape(SKILL_NAME)}|\"{re.escape(SKILL_NAME)}\"|'{re.escape(SKILL_NAME)}')\.)|\Z)"
)


def _render_codex_mcp_block(py_cmd: str, mcp_args: List[str]) -> str:
    cmd_exe, prefix_args = _split_python_cmd(py_cmd)
    all_args = [*prefix_args, *mcp_args]
    args_toml = "[" + ", ".join(json.dumps(a, ensure_ascii=False) for a in all_args) + "]"
    return (
        f"{TOML_MCP_BEGIN}\n"
        f"[mcp_servers.{SKILL_NAME}]\n"
        f"command = {json.dumps(cmd_exe, ensure_ascii=False)}\n"
        f"args = {args_toml}\n"
        f"{TOML_MCP_END}\n"
    )


def _strip_toml_match(existing: str, match: "re.Match[str]") -> str:
    before = existing[: match.start()].rstrip("\r\n")
    after = existing[match.end() :].lstrip("\r\n")
    if before and after:
        return before + "\n\n" + after
    if before:
        return before + "\n"
    if after:
        return after
    return ""


def upsert_codex_mcp_toml(config_toml: Path, py_cmd: str, mcp_args: List[str], dry_run: bool) -> None:
    block = _render_codex_mcp_block(py_cmd, mcp_args)
    existing = config_toml.read_text(encoding="utf-8-sig", errors="replace") if config_toml.exists() else ""

    if _TOML_FENCED_MCP_RE.search(existing):
        updated = _TOML_FENCED_MCP_RE.sub(lambda _m: block, existing, count=1)
    elif _TOML_TABLE_MCP_RE.search(existing):
        updated = _TOML_TABLE_MCP_RE.sub(lambda _m: block, existing, count=1)
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        blank = "\n" if existing and not existing.endswith("\n\n") else ""
        updated = existing + sep + blank + block

    _write_text_with_backup(config_toml, updated, dry_run, dry_run_label="write Codex MCP TOML")
    if not dry_run:
        print(f"  [ok] merged Codex MCP server `{SKILL_NAME}` -> {config_toml}")


def remove_codex_mcp_toml(config_toml: Path, dry_run: bool, remove_if_empty: bool = True) -> None:
    if not config_toml.exists():
        print(f"  [skip] Codex config not present: {config_toml}")
        return
    existing = config_toml.read_text(encoding="utf-8-sig", errors="replace")
    match = _TOML_FENCED_MCP_RE.search(existing) or _TOML_TABLE_MCP_RE.search(existing)
    if not match:
        print(f"  [skip] `{SKILL_NAME}` MCP table not found in {config_toml}")
        return
    updated = _strip_toml_match(existing, match)
    if remove_if_empty and not updated.strip():
        if dry_run:
            print(f"  [dry-run] remove empty Codex config: {config_toml}")
            return
        config_toml.unlink()
        print(f"  [ok] removed empty Codex config {config_toml}")
        return
    _write_text_with_backup(config_toml, updated, dry_run, dry_run_label="update Codex MCP TOML")
    if not dry_run:
        print(f"  [ok] removed Codex MCP server `{SKILL_NAME}` from {config_toml}")


def _antigravity_uses_project_skill(project_root: Path) -> bool:
    """Return True if project-scoped Antigravity hooks or MCP config reference `.agents/skills/aoci-index`."""
    agy_hooks = project_root / ".agents" / "hooks.json"
    if agy_hooks.is_file():
        try:
            data = load_json_dict(agy_hooks)
            spec = data.get(AGY_HOOK_KEY)
            if isinstance(spec, dict) and any(
                isinstance(handlers, list) and any(_is_aoci_hook_handler(h) for h in handlers)
                for handlers in spec.values()
            ):
                return True
        except (OSError, ValueError):
            pass
    agy_plugin_mcp = project_root / ".agents" / "plugins" / SKILL_NAME / "mcp_config.json"
    if agy_plugin_mcp.is_file():
        try:
            raw = agy_plugin_mcp.read_text(encoding="utf-8-sig", errors="replace")
            if f"skills/{SKILL_NAME}/scripts/aoci_mcp.py" in raw:
                return True
        except OSError:
            pass
    return False


def _codex_uses_project_skill(project_root: Path) -> bool:
    """Return True if project-scoped Codex hooks or TOML MCP config reference `.agents/skills/aoci-index`."""
    codex_hooks = project_root / ".codex" / "hooks.json"
    if codex_hooks.is_file():
        try:
            data = load_json_dict(codex_hooks)
            hooks_obj = data.get("hooks")
            if isinstance(hooks_obj, dict):
                for groups in hooks_obj.values():
                    if isinstance(groups, list) and any(
                        isinstance(g, dict)
                        and isinstance(g.get("hooks"), list)
                        and any(_is_aoci_hook_handler(h) for h in g["hooks"])
                        for g in groups
                    ):
                        return True
        except (OSError, ValueError):
            pass
    codex_toml = project_root / ".codex" / "config.toml"
    if codex_toml.is_file():
        try:
            raw = codex_toml.read_text(encoding="utf-8-sig", errors="replace")
            if _TOML_FENCED_MCP_RE.search(raw) or _TOML_TABLE_MCP_RE.search(raw):
                return True
        except OSError:
            pass
    return False


def install_antigravity_plugin_bundle(
    plugin_dir: Path,
    scope: str,
    py_cmd: str,
    with_hooks: bool,
    with_rules: bool,
    with_mcp: bool,
    with_turn_hook: bool,
    dry_run: bool,
    force: bool,
) -> bool:
    try:
        src_dir = _require_source_dir()
    except RuntimeError as exc:
        print(f"  [abort] {exc}", file=sys.stderr)
        return False

    skill_dest = plugin_dir / "skills" / SKILL_NAME
    if plugin_dir.exists() and not force:
        manifest_file = plugin_dir / "plugin.json"
        if manifest_file.is_file():
            try:
                pdata = load_json_dict(manifest_file)
            except (OSError, ValueError):
                pdata = {}
            if pdata.get("name") not in (SKILL_NAME, f"{SKILL_NAME}-project-mcp"):
                print(
                    f"  [abort] {plugin_dir} belongs to another plugin (`{pdata.get('name', 'unknown')}`). "
                    "Re-run with --force to overwrite.",
                    file=sys.stderr,
                )
                return False
        if not _check_overwrite_guard(skill_dest, force):
            return False
    if dry_run:
        print(f"  [dry-run] assemble Antigravity plugin bundle -> `{plugin_dir}`")
        return True
    plugin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_dir / "resources" / "plugin.json", plugin_dir / "plugin.json")
    logo_src = src_dir / "assets" / "logo.svg"
    if logo_src.is_file():
        (plugin_dir / "assets").mkdir(exist_ok=True)
        shutil.copy2(logo_src, plugin_dir / "assets" / "logo.svg")
    if not copy_skill_tree(skill_dest, dry_run=False, force=True):
        return False
    if with_hooks:
        upsert_antigravity_hooks(plugin_dir / "hooks.json", py_cmd, dry_run=False, with_turn_hook=with_turn_hook)
    if with_rules:
        rules_dir = plugin_dir / "rules"
        rules_dir.mkdir(exist_ok=True)
        upsert_rule_snippet(rules_dir / "AGENTS.md", src_dir / "resources" / "AGENTS.snippet.md", dry_run=False)
    if with_mcp:
        fallback_base = f".agents/plugins/{SKILL_NAME}" if scope == "project" else plugin_dir.resolve().as_posix()
        mcp_args = _portable_env_mcp_args(
            "PLUGIN_ROOT",
            f"skills/{SKILL_NAME}/scripts/aoci_mcp.py",
            fallback_base=fallback_base,
        )
        _upsert_mcp_server(plugin_dir / "mcp_config.json", py_cmd, mcp_args, dry_run=False)
    print(f"  [ok] assembled Antigravity plugin bundle -> {plugin_dir}")
    return True


def _prune_empty_dir(path: Path) -> None:
    try:
        if path.is_dir() and not _dir_has_any_entries(path):
            path.rmdir()
    except OSError:
        pass


def uninstall_antigravity_plugin_bundle(
    plugin_dir: Path,
    with_hooks: bool,
    with_rules: bool,
    with_mcp: bool,
    dry_run: bool,
) -> None:
    """
    Surgically strip only AOCI components from an Antigravity plugin directory, preserving any
    user-added MCP servers, sibling hooks, rule snippets, or extra files inside `plugin_dir`.
    """
    if not plugin_dir.exists():
        print(f"  [skip] plugin directory not present: {plugin_dir}")
        return
    src_dir = SOURCE_DIR if is_valid_skill_source(SOURCE_DIR) else resolve_source_dir()
    if src_dir is not None and plugin_dir.resolve() == src_dir.resolve():
        print(f"  [skip] refusing to modify source directory: {plugin_dir}")
        return

    skill_dest = plugin_dir / "skills" / SKILL_NAME
    if skill_dest.exists():
        remove_skill_tree(skill_dest, dry_run)
        if not dry_run:
            _prune_empty_dir(plugin_dir / "skills")
    if with_hooks and (plugin_dir / "hooks.json").exists():
        remove_antigravity_hooks(plugin_dir / "hooks.json", dry_run, remove_if_empty=True)
    if with_rules and (plugin_dir / "rules" / "AGENTS.md").exists():
        remove_rule_snippet(plugin_dir / "rules" / "AGENTS.md", dry_run)
        if not dry_run:
            _prune_empty_dir(plugin_dir / "rules")
    if with_mcp and (plugin_dir / "mcp_config.json").exists():
        _remove_mcp_server(plugin_dir / "mcp_config.json", dry_run, remove_if_empty=True)

    if dry_run:
        return

    manifest_file = plugin_dir / "plugin.json"
    if manifest_file.is_file():
        try:
            pdata = load_json_dict(manifest_file)
        except (OSError, ValueError):
            pdata = {}
        if pdata.get("name") not in (SKILL_NAME, f"{SKILL_NAME}-project-mcp"):
            print(f"  [keep] preserved non-AOCI plugin directory: {plugin_dir}")
            return

    leftovers: List[str] = []
    try:
        for entry in sorted(plugin_dir.iterdir(), key=lambda p: p.name):
            if entry.name == "plugin.json" and entry.is_file():
                continue
            if entry.name == "assets" and entry.is_dir():
                asset_items = [a.name for a in entry.iterdir()]
                if all(name == "logo.svg" for name in asset_items):
                    continue
            leftovers.append(entry.name)
    except OSError:
        return

    if leftovers:
        print(f"  [keep] preserved user content in {plugin_dir}: {', '.join(leftovers)}")
        return

    try:
        logo = plugin_dir / "assets" / "logo.svg"
        if logo.is_file():
            logo.unlink()
        _prune_empty_dir(plugin_dir / "assets")
        if manifest_file.is_file():
            manifest_file.unlink()
        _prune_empty_dir(plugin_dir)
        print(f"  [ok] removed empty Antigravity plugin bundle -> {plugin_dir}")
    except OSError:
        pass


def _upsert_antigravity_project_mcp_plugin(plugin_dir: Path, py_cmd: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] write project MCP plugin wrapper -> {plugin_dir / 'mcp_config.json'}")
        return
    src_dir = _require_source_dir()
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if not (plugin_dir / "plugin.json").exists():
        try:
            pdata = load_json_dict(src_dir / "resources" / "plugin.json")
        except (OSError, ValueError):
            pdata = {}
        pdata["name"] = f"{SKILL_NAME}-project-mcp"
        write_text_lf(plugin_dir / "plugin.json", json.dumps(pdata, indent=2, ensure_ascii=False) + "\n")
    logo_src = src_dir / "assets" / "logo.svg"
    if logo_src.is_file() and not (plugin_dir / "assets" / "logo.svg").exists():
        (plugin_dir / "assets").mkdir(exist_ok=True)
        shutil.copy2(logo_src, plugin_dir / "assets" / "logo.svg")
    rel_target = (
        f"skills/{SKILL_NAME}/scripts/aoci_mcp.py"
        if (plugin_dir / "skills" / SKILL_NAME / "scripts" / "aoci_mcp.py").exists()
        else f"../../skills/{SKILL_NAME}/scripts/aoci_mcp.py"
    )
    mcp_args = _portable_env_mcp_args("PLUGIN_ROOT", rel_target, fallback_base=f".agents/plugins/{SKILL_NAME}")
    _upsert_mcp_server(plugin_dir / "mcp_config.json", py_cmd, mcp_args, dry_run=False)
    print(
        "  [note] Antigravity loads project-scoped MCP servers from `.agents/plugins/`; "
        "tip: pass `--as-plugin` to bundle skill + hooks + rules + MCP in one directory."
    )


def _remove_antigravity_project_mcp_plugin(plugin_dir: Path, legacy_mcp_file: Path, dry_run: bool) -> None:
    if legacy_mcp_file.exists():
        _remove_mcp_server(legacy_mcp_file, dry_run)
    plugin_mcp = plugin_dir / "mcp_config.json"
    if not plugin_mcp.exists():
        return
    uninstall_antigravity_plugin_bundle(plugin_dir, with_hooks=False, with_rules=False, with_mcp=True, dry_run=dry_run)


def process_host(
    host: str,
    scope: str,
    home_dir: Path,
    project_root: Path,
    py_cmd: Optional[str],
    with_hooks: bool,
    with_rules: bool,
    uninstall: bool,
    dry_run: bool,
    force: bool = False,
    with_turn_hook: bool = False,
    no_turn_hook: bool = False,
    with_mcp: bool = False,
    as_plugin: bool = False,
    codex_home: Optional[Path] = None,
) -> bool:
    print(f"==> {'Uninstalling' if uninstall else 'Installing'} `{SKILL_NAME}` for {host} ({scope} scope)")
    py_cmd = py_cmd or default_python_cmd(scope)

    if host == "antigravity":
        base_dir = (home_dir / ".gemini" / "config") if scope == "global" else (project_root / ".agents")
        plugin_dir = base_dir / "plugins" / SKILL_NAME
        if as_plugin:
            if uninstall:
                uninstall_antigravity_plugin_bundle(
                    plugin_dir,
                    with_hooks=with_hooks,
                    with_rules=with_rules,
                    with_mcp=with_mcp,
                    dry_run=dry_run,
                )
                return True
            return install_antigravity_plugin_bundle(
                plugin_dir=plugin_dir,
                scope=scope,
                py_cmd=py_cmd,
                with_hooks=with_hooks,
                with_rules=with_rules,
                with_mcp=with_mcp,
                with_turn_hook=bool(with_turn_hook and not no_turn_hook),
                dry_run=dry_run,
                force=force,
            )

        skill_dest = base_dir / "skills" / SKILL_NAME
        hooks_file = base_dir / "hooks.json"
        mcp_file = base_dir / "mcp_config.json"
        rule_file = (home_dir / ".gemini" / "GEMINI.md") if scope == "global" else (project_root / "AGENTS.md")

        if uninstall:
            shared_with_codex = scope == "project" and _codex_uses_project_skill(project_root)
            if shared_with_codex:
                print(f"  [keep] preserved shared project skill for Codex: {skill_dest}")
            else:
                remove_skill_tree(skill_dest, dry_run)
            if with_hooks:
                remove_antigravity_hooks(hooks_file, dry_run)
            if with_rules:
                if shared_with_codex:
                    print(f"  [keep] preserved shared AGENTS.md snippet for Codex: {rule_file}")
                else:
                    remove_rule_snippet(rule_file, dry_run)
            if with_mcp:
                if scope == "project":
                    _remove_antigravity_project_mcp_plugin(plugin_dir, mcp_file, dry_run)
                else:
                    _remove_mcp_server(mcp_file, dry_run)
            if plugin_dir.exists() and (plugin_dir / "plugin.json").is_file():
                uninstall_antigravity_plugin_bundle(
                    plugin_dir,
                    with_hooks=with_hooks,
                    with_rules=with_rules,
                    with_mcp=with_mcp,
                    dry_run=dry_run,
                )
        else:
            try:
                src_dir = _require_source_dir()
            except RuntimeError as exc:
                print(f"  [abort] {exc}", file=sys.stderr)
                return False
            snippet_path = src_dir / "resources" / "AGENTS.snippet.md"
            if not copy_skill_tree(skill_dest, dry_run, force):
                return False
            if with_hooks:
                upsert_antigravity_hooks(
                    hooks_file,
                    py_cmd,
                    dry_run,
                    with_turn_hook=bool(with_turn_hook and not no_turn_hook),
                )
            if with_rules:
                upsert_rule_snippet(rule_file, snippet_path, dry_run)
            if with_mcp:
                if scope == "project":
                    _upsert_antigravity_project_mcp_plugin(plugin_dir, py_cmd, dry_run)
                else:
                    mcp_abs = [(skill_dest / "scripts" / "aoci_mcp.py").resolve().as_posix()]
                    _upsert_mcp_server(mcp_file, py_cmd, mcp_abs, dry_run)

    elif host == "claude-code":
        base_dir = (home_dir / ".claude") if scope == "global" else (project_root / ".claude")
        skill_dest = base_dir / "skills" / SKILL_NAME
        settings_file = base_dir / "settings.json"
        mcp_file = (home_dir / ".claude.json") if scope == "global" else (project_root / ".mcp.json")
        rule_file = (home_dir / ".claude" / "CLAUDE.md") if scope == "global" else (project_root / "CLAUDE.md")

        if uninstall:
            remove_skill_tree(skill_dest, dry_run)
            if with_hooks:
                remove_claude_hooks(settings_file, dry_run)
            if with_rules:
                remove_rule_snippet(rule_file, dry_run)
            if with_mcp:
                _remove_mcp_server(mcp_file, dry_run)
        else:
            try:
                src_dir = _require_source_dir()
            except RuntimeError as exc:
                print(f"  [abort] {exc}", file=sys.stderr)
                return False
            snippet_path = src_dir / "resources" / "CLAUDE.snippet.md"
            if not copy_skill_tree(skill_dest, dry_run, force):
                return False
            if with_hooks:
                upsert_claude_hooks(
                    settings_file,
                    skill_dest,
                    scope,
                    py_cmd,
                    dry_run,
                    with_turn_hook=not no_turn_hook,
                )
            if with_rules:
                upsert_rule_snippet(rule_file, snippet_path, dry_run)
            if with_mcp:
                if scope == "project":
                    mcp_args = _portable_env_mcp_args(
                        "CLAUDE_PROJECT_DIR",
                        f".claude/skills/{SKILL_NAME}/scripts/aoci_mcp.py",
                        fallback_base=".",
                    )
                else:
                    mcp_args = [(skill_dest / "scripts" / "aoci_mcp.py").resolve().as_posix()]
                _upsert_mcp_server(mcp_file, py_cmd, mcp_args, dry_run)

    elif host == "codex":
        codex_dir = (codex_home or (home_dir / ".codex")) if scope == "global" else (project_root / ".codex")
        skill_dest = (
            (home_dir / ".agents" / "skills" / SKILL_NAME)
            if scope == "global"
            else (project_root / ".agents" / "skills" / SKILL_NAME)
        )
        hooks_file = codex_dir / "hooks.json"
        mcp_file = codex_dir / "config.toml"
        rule_file = (codex_dir / "AGENTS.md") if scope == "global" else (project_root / "AGENTS.md")

        if uninstall:
            shared_with_agy = scope == "project" and _antigravity_uses_project_skill(project_root)
            if shared_with_agy:
                print(f"  [keep] preserved shared project skill for Antigravity: {skill_dest}")
            else:
                remove_skill_tree(skill_dest, dry_run)
            if with_hooks:
                remove_codex_hooks(hooks_file, dry_run, remove_if_empty=True)
            if with_rules:
                if shared_with_agy:
                    print(f"  [keep] preserved shared AGENTS.md snippet for Antigravity: {rule_file}")
                else:
                    remove_rule_snippet(rule_file, dry_run)
            if with_mcp:
                remove_codex_mcp_toml(mcp_file, dry_run, remove_if_empty=True)
        else:
            try:
                src_dir = _require_source_dir()
            except RuntimeError as exc:
                print(f"  [abort] {exc}", file=sys.stderr)
                return False
            snippet_path = src_dir / "resources" / "AGENTS.snippet.md"
            if not copy_skill_tree(skill_dest, dry_run, force):
                return False
            if with_hooks:
                upsert_codex_hooks(
                    hooks_file,
                    skill_dest,
                    scope,
                    py_cmd,
                    dry_run,
                    with_turn_hook=not no_turn_hook,
                )
            if with_rules:
                upsert_rule_snippet(rule_file, snippet_path, dry_run)
            if with_mcp:
                if scope == "project":
                    mcp_args = _codex_project_mcp_args()
                else:
                    mcp_args = [(skill_dest / "scripts" / "aoci_mcp.py").resolve().as_posix()]
                upsert_codex_mcp_toml(mcp_file, py_cmd, mcp_args, dry_run)
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Install or uninstall the `aoci-index` skill for Claude Code, Antigravity, and Codex"
    )
    p.add_argument(
        "--host",
        choices=("claude-code", "antigravity", "codex", "both", "all"),
        required=True,
        help="Target agent host(s) (`both` = Claude Code + Antigravity; `all` = Claude Code + Antigravity + Codex)",
    )
    p.add_argument(
        "--scope",
        choices=("global", "project"),
        default="global",
        help="Install globally in user home or locally inside a project (default: global)",
    )
    p.add_argument("--project-root", help="Target repository root (used with `--scope project`; defaults to CWD)")
    p.add_argument("--home-dir", help="Override user home directory (defaults to Path.home())")
    p.add_argument(
        "--with-hooks",
        action="store_true",
        help="Merge lifecycle hooks (SessionStart + Stop, plus UserPromptSubmit on Claude Code and Codex)",
    )
    p.add_argument(
        "--with-turn-hook",
        action="store_true",
        help="Also install Antigravity `PreInvocation` turn-start hook to snapshot manual IDE edits between turns",
    )
    p.add_argument(
        "--no-turn-hook",
        action="store_true",
        help="Skip installing turn-start (`UserPromptSubmit` / `PreInvocation`) hooks",
    )
    p.add_argument("--with-rules", action="store_true", help="Append fenced protocol snippet in CLAUDE.md / GEMINI.md / AGENTS.md")
    p.add_argument("--with-mcp", action="store_true", help="Register the zero-dependency `aoci_mcp.py` stdio MCP server")
    p.add_argument(
        "--as-plugin",
        action="store_true",
        help="Package into a self-contained Antigravity plugin bundle (`plugins/aoci-index/`)",
    )
    p.add_argument(
        "--python-cmd",
        default=None,
        help="Python executable for hook commands (default: absolute base interpreter for global scope, "
        "`python`/`python3` for project scope)",
    )
    p.add_argument("--force", action="store_true", help="Overwrite an existing install even if it was edited locally")
    p.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the `aoci-index` skill plus its hooks, rules, and MCP registrations",
    )
    p.add_argument(
        "--keep-config",
        action="store_true",
        help="With --uninstall: only remove the skill directory, keep hooks/rule snippets",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview actions without modifying any files")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_utf8_streams()
    args = build_parser().parse_args(argv)

    home_dir = Path(args.home_dir).resolve() if args.home_dir else Path.home().resolve()
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd().resolve()
    if args.host == "both":
        hosts: List[str] = ["claude-code", "antigravity"]
    elif args.host == "all":
        hosts = ["claude-code", "antigravity", "codex"]
    else:
        hosts = [args.host]

    if args.home_dir:
        codex_home = home_dir / ".codex"
    elif os.environ.get("CODEX_HOME", "").strip():
        codex_home = Path(os.environ["CODEX_HOME"].strip()).resolve()
    else:
        codex_home = home_dir / ".codex"

    if args.uninstall and args.keep_config:
        print(
            "  [warn] `--keep-config` leaves hook/MCP entries registered while removing skill scripts; "
            "global absolute-path hooks may fail until reinstalled or uninstalled without `--keep-config`.",
            file=sys.stderr,
        )

    with_hooks = args.with_hooks or args.with_turn_hook or (args.uninstall and not args.keep_config)
    with_rules = args.with_rules or (args.uninstall and not args.keep_config)
    with_mcp = args.with_mcp or (args.uninstall and not args.keep_config)

    ok = True
    for h in hosts:
        ok = process_host(
            host=h,
            scope=args.scope,
            home_dir=home_dir,
            project_root=project_root,
            py_cmd=args.python_cmd,
            with_hooks=with_hooks,
            with_rules=with_rules,
            uninstall=args.uninstall,
            dry_run=args.dry_run,
            force=args.force,
            with_turn_hook=args.with_turn_hook,
            no_turn_hook=args.no_turn_hook,
            with_mcp=with_mcp,
            as_plugin=args.as_plugin,
            codex_home=codex_home,
        ) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
