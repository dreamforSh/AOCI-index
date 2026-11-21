"""Gitignore-style ignore matching and path/binary classification for `aoci_index`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from .constants import (
    ALWAYS_SKIP_DIRS,
    AOCI_DIR_NAME,
    DEFAULT_SKIP_EXTENSIONS,
    DEFAULT_SKIP_FILENAMES,
    DEFAULT_SKIP_SUFFIXES,
    IGNORE_FILE,
    INSTALL_MANIFEST_NAME,
    INSTALLED_SKILL_DIR_NAME,
)


def _glob_to_regex(glob: str) -> str:
    out: List[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            prev_is_slash = (i == 0) or (glob[i - 1] == "/")
            if prev_is_slash and glob[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if glob[i : i + 2] == "**":
                next_is_end_or_slash = (i + 2 >= n) or (glob[i + 2] == "/")
                out.append(".*" if (prev_is_slash and next_is_end_or_slash) else "[^/]*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            k = i + 1
            neg_cls = False
            if k < n and glob[k] in ("!", "^"):
                neg_cls = True
                k += 1
            cls_start = k
            if k < n and glob[k] == "]":
                k += 1
            while k < n:
                if glob[k] == "\\" and k + 1 < n:
                    k += 2
                    continue
                if glob[k] == "]":
                    break
                k += 1
            if k >= n:
                out.append(re.escape(c))
            else:
                raw_body = glob[cls_start:k]
                if raw_body.startswith("]"):
                    safe_body = r"\]" + raw_body[1:]
                else:
                    safe_body = raw_body
                cand_cls = f"[{'^' if neg_cls else ''}{safe_body}]"
                try:
                    re.compile(cand_cls)
                    out.append(cand_cls)
                except re.error:
                    out.append(re.escape(glob[i : k + 1]))
                i = k
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(glob[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class IgnoreMatcher:
    """
    Ordered gitignore-like rules: last match wins, `!` re-includes, leading or inner `/`
    anchors to the root, trailing `/` matches directories only, `**` spans directories,
    and a file inside an ignored directory cannot be re-included (Git semantics).
    """

    def __init__(self, patterns: Sequence[str] = ()) -> None:
        self.patterns: List[str] = []
        self._rules: List[Tuple[Any, bool, bool]] = []
        self._dir_cache: dict = {}
        self._fast_file_rx: Optional[Any] = None
        self._fast_dir_rx: Optional[Any] = None
        self._fast_neg_file_rx: Optional[Any] = None
        self._fast_neg_dir_rx: Optional[Any] = None
        self._fast_built: bool = False
        self._fast_can_use: bool = False
        for raw in patterns:
            self.add(raw)

    def add(self, raw: str) -> None:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            return
        line = line.strip()
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        dir_only = line.endswith("/")
        body = line.rstrip("/")
        if not body:
            return
        anchored = body.startswith("/") or "/" in body.lstrip("/")
        body = body.lstrip("/")
        prefix = "^" if anchored else "^(?:.*/)?"
        try:
            compiled = re.compile(prefix + _glob_to_regex(body) + "$")
        except re.error:
            compiled = re.compile(prefix + re.escape(body) + "$")
        self._rules.append((compiled, negate, dir_only))
        self.patterns.append(raw.strip())
        self._dir_cache.clear()
        self._fast_file_rx = None
        self._fast_dir_rx = None
        self._fast_neg_file_rx = None
        self._fast_neg_dir_rx = None
        self._fast_built = False
        self._fast_can_use = False

    def __bool__(self) -> bool:
        return bool(self._rules)

    def __iter__(self):  # backward compatibility with list-of-patterns callers
        return iter(self.patterns)

    def _ensure_fast_regexes(self) -> None:
        if self._fast_built:
            return
        self._fast_built = True
        if not self._rules:
            self._fast_can_use = False
            return
        pos_file_pats = [rx.pattern for rx, neg, dir_only in self._rules if not neg and not dir_only]
        pos_dir_pats = [rx.pattern for rx, neg, _ in self._rules if not neg]
        neg_file_pats = [rx.pattern for rx, neg, dir_only in self._rules if neg and not dir_only]
        neg_dir_pats = [rx.pattern for rx, neg, _ in self._rules if neg]
        try:
            self._fast_file_rx = re.compile("(?:" + ")|(?:".join(pos_file_pats) + ")") if pos_file_pats else None
            self._fast_dir_rx = re.compile("(?:" + ")|(?:".join(pos_dir_pats) + ")") if pos_dir_pats else None
            self._fast_neg_file_rx = re.compile("(?:" + ")|(?:".join(neg_file_pats) + ")") if neg_file_pats else None
            self._fast_neg_dir_rx = re.compile("(?:" + ")|(?:".join(neg_dir_pats) + ")") if neg_dir_pats else None
            self._fast_can_use = True
        except re.error:
            self._fast_file_rx = None
            self._fast_dir_rx = None
            self._fast_neg_file_rx = None
            self._fast_neg_dir_rx = None
            self._fast_can_use = False

    def _decide(self, subject: str, is_dir: bool) -> bool:
        self._ensure_fast_regexes()
        if self._fast_can_use:
            pos_rx = self._fast_dir_rx if is_dir else self._fast_file_rx
            if pos_rx is None or not pos_rx.match(subject):
                return False
            neg_rx = self._fast_neg_dir_rx if is_dir else self._fast_neg_file_rx
            if neg_rx is None or not neg_rx.match(subject):
                return True
        result = False
        for rx, negate, dir_only in self._rules:
            if dir_only and not is_dir:
                continue
            if rx.match(subject):
                result = not negate
        return result

    def _dir_ignored(self, dir_rel: str) -> bool:
        cached = self._dir_cache.get(dir_rel)
        if cached is not None:
            return cached
        parent = dir_rel.rsplit("/", 1)[0] if "/" in dir_rel else ""
        val = (bool(parent) and self._dir_ignored(parent)) or self._decide(dir_rel, True)
        self._dir_cache[dir_rel] = val
        return val

    def is_ignored(self, rel_posix: str, is_dir: bool = False) -> bool:
        if not self._rules:
            return False
        rel = rel_posix.strip("/")
        if not rel:
            return False
        if is_dir:
            return self._dir_ignored(rel)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent and self._dir_ignored(parent):
            return True
        return self._decide(rel, False)


def _read_ignore_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []


def load_aociignore(aoci_dir: Path, include_root_gitignore: bool = False) -> IgnoreMatcher:
    """
    Build the extra-ignore matcher from `.aoci/.aociignore` (gitignore syntax incl. `!`, `/`, `**`).
    In non-Git workspaces, the root `.gitignore` is layered underneath as a best-effort filter.
    """
    lines: List[str] = []
    if include_root_gitignore:
        lines.extend(_read_ignore_lines(aoci_dir.parent / ".gitignore"))
    lines.extend(_read_ignore_lines(aoci_dir / IGNORE_FILE))
    return IgnoreMatcher(lines)


def load_ignore_matcher(root: Path, git_info: dict) -> IgnoreMatcher:
    return load_aociignore(root / AOCI_DIR_NAME, include_root_gitignore=not git_info.get("is_git"))


def matches_ignore(rel_posix: str, patterns: Any) -> bool:
    """Return True if `rel_posix` (trailing `/` = directory) is ignored by an IgnoreMatcher or pattern list."""
    if not patterns:
        return False
    matcher = patterns if isinstance(patterns, IgnoreMatcher) else IgnoreMatcher(list(patterns))
    if rel_posix.endswith("/"):
        return matcher.is_ignored(rel_posix.rstrip("/"), is_dir=True)
    return matcher.is_ignored(rel_posix)


def is_probably_binary_bytes(chunk: bytes) -> bool:
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
        return False
    except UnicodeDecodeError:
        try:
            chunk[:-4].decode("utf-8")
            return False
        except UnicodeDecodeError:
            pass
    control_bytes = sum(1 for b in chunk if b < 9 or (13 < b < 32))
    return (control_bytes / len(chunk)) > 0.15


def _is_installed_skill_path(parts: Sequence[str]) -> bool:
    """
    True for files inside an AOCI skill copy installed under a hidden agent-host dir, e.g.
    `.github/skills/aoci-index/...` (VS Code / Copilot project scope) or `.foo/skills/aoci-index/...`.
    """
    # parts[-1] is the filename; need at least `<.host>/skills/aoci-index/<file>`
    for idx in range(1, len(parts) - 2):
        if (
            parts[idx] == "skills"
            and parts[idx + 1] == INSTALLED_SKILL_DIR_NAME
            and parts[idx - 1].startswith(".")
        ):
            return True
    return False


def install_manifest_dirs(rel_paths: Sequence[str]) -> Tuple[str, ...]:
    """Return directory prefixes (`dir/`) that contain an `installer.py` manifest (`.aoci-install.json`)."""
    suffix = "/" + INSTALL_MANIFEST_NAME
    found: List[str] = []
    for rel in rel_paths:
        if rel == INSTALL_MANIFEST_NAME:
            # A manifest at the repo root would exclude everything; never treat the root as an installed copy
            continue
        if rel.endswith(suffix):
            found.append(rel[: -len(INSTALL_MANIFEST_NAME)])
    return tuple(sorted(set(found)))


def under_install_manifest_dir(rel_posix: str, manifest_dirs: Sequence[str]) -> bool:
    return bool(manifest_dirs) and any(rel_posix.startswith(prefix) for prefix in manifest_dirs)


def path_skip_reason(rel_posix: str, extra_ignores: Any) -> Optional[str]:
    """Cheap pure-string filter run before any filesystem stat/open."""
    if not rel_posix:
        return "empty-path"
    parts = rel_posix.split("/")
    if AOCI_DIR_NAME in parts or ".git" in parts:
        return "reserved-dir"
    if len(parts) > 1 and any(seg in ALWAYS_SKIP_DIRS for seg in parts[:-1]):
        return "skip-dir"
    if len(parts) > 3 and _is_installed_skill_path(parts):
        return "installed-skill"
    filename = parts[-1]
    if filename == INSTALL_MANIFEST_NAME:
        return "installed-skill"
    if filename in DEFAULT_SKIP_FILENAMES:
        return "lock-or-os-file"
    lower_name = filename.lower()
    if lower_name.endswith(DEFAULT_SKIP_SUFFIXES):
        return "minified-asset"
    dot_idx = lower_name.rfind(".")
    if dot_idx > 0 and lower_name[dot_idx:] in DEFAULT_SKIP_EXTENSIONS:
        return "binary-ext"
    if extra_ignores and matches_ignore(rel_posix, extra_ignores):
        return "aociignore"
    return None
