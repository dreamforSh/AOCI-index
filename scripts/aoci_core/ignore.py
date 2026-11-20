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
)


def _glob_to_regex(glob: str) -> str:
    out: List[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if glob[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j
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
        self._rules.append((re.compile(prefix + _glob_to_regex(body) + "$"), negate, dir_only))
        self.patterns.append(raw.strip())
        self._dir_cache.clear()
        self._fast_file_rx = None
        self._fast_dir_rx = None
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
        if not self._rules or any(neg for _, neg, _ in self._rules):
            self._fast_can_use = False
            return
        file_pats = [rx.pattern for rx, _, dir_only in self._rules if not dir_only]
        dir_pats = [rx.pattern for rx, _, _ in self._rules]
        try:
            self._fast_file_rx = re.compile("(?:" + ")|(?:".join(file_pats) + ")") if file_pats else None
            self._fast_dir_rx = re.compile("(?:" + ")|(?:".join(dir_pats) + ")") if dir_pats else None
            self._fast_can_use = True
        except re.error:
            self._fast_file_rx = None
            self._fast_dir_rx = None
            self._fast_can_use = False

    def _decide(self, subject: str, is_dir: bool) -> bool:
        self._ensure_fast_regexes()
        if self._fast_can_use:
            rx = self._fast_dir_rx if is_dir else self._fast_file_rx
            return bool(rx is not None and rx.match(subject))
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
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
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


def path_skip_reason(rel_posix: str, extra_ignores: Any) -> Optional[str]:
    """Cheap pure-string filter run before any filesystem stat/open."""
    if not rel_posix:
        return "empty-path"
    parts = rel_posix.split("/")
    if AOCI_DIR_NAME in parts or ".git" in parts:
        return "reserved-dir"
    if len(parts) > 1 and any(seg in ALWAYS_SKIP_DIRS for seg in parts[:-1]):
        return "skip-dir"
    filename = parts[-1]
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
