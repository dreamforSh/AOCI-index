"""Git work-tree detection, `.git/info/exclude` management, discovery cache keys, and hook wrappers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import githooks as _githooks
from .constants import (
    AOCI_DIR_NAME,
    CACHE_DIR,
    DEFAULT_MAX_BYTES,
    EXCLUDE_BEGIN,
    EXCLUDE_END,
    GIT_DISCOVERY_CACHE,
    GIT_EXCLUSION_CACHE,
    IGNORE_FILE,
    MANIFEST_FILE,
    SKIP_RULES_REV,
    _stat_key,
    atomic_write_text,
    normalize_relpath,
    write_text_lf,
)
from .ignore import path_skip_reason, under_install_manifest_dir


def run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


_GIT_BIN_CACHE: Tuple[Optional[str], Optional[bool]] = (None, None)


def _has_git_binary() -> bool:
    global _GIT_BIN_CACHE
    cur_path = os.environ.get("PATH", "")
    if _GIT_BIN_CACHE[0] == cur_path and _GIT_BIN_CACHE[1] is not None:
        return _GIT_BIN_CACHE[1]
    found = shutil.which("git") is not None
    _GIT_BIN_CACHE = (cur_path, found)
    return found


def _detect_git_info_fast(start_dir: Path) -> Optional[Dict[str, Any]]:
    """
    Pure-Python `.git` topology resolver for standard repos, worktrees (`commondir`), and submodules.
    Eliminates a ~55ms `git rev-parse` subprocess on every CLI/hook call; returns `None` on exotic
    configs (`GIT_*` env overrides, bare repos, or paths inside `.git/`) to fall back to `git`.
    """
    if any(k in os.environ for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")):
        return None
    if not _has_git_binary():
        return {"is_git": False, "git_available": False}
    try:
        cur = start_dir.resolve()
    except OSError:
        return None
    if ".git" in cur.parts:
        return None

    for cand in (cur, *cur.parents):
        dot_git = cand / ".git"
        try:
            if dot_git.is_dir():
                git_dir = dot_git.resolve()
                common_dir = git_dir
            elif dot_git.is_file():
                first = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
                if not first or not first[0].lower().startswith("gitdir:"):
                    return None
                raw_gd = first[0].split(":", 1)[1].strip()
                gd_path = Path(raw_gd)
                git_dir = (gd_path if gd_path.is_absolute() else (cand / gd_path)).resolve()
                common_dir = git_dir
                common_file = git_dir / "commondir"
                if common_file.is_file():
                    raw_cd = common_file.read_text(encoding="utf-8", errors="replace").strip()
                    if raw_cd:
                        cd_path = Path(raw_cd)
                        common_dir = (cd_path if cd_path.is_absolute() else (git_dir / cd_path)).resolve()
            else:
                continue
        except OSError:
            return None

        if not (git_dir / "HEAD").is_file():
            return None
        cfg_file = common_dir / "config"
        if cfg_file.is_file():
            try:
                cfg_txt = cfg_file.read_text(encoding="utf-8", errors="replace")
                if re.search(r"(?im)^\s*bare\s*=\s*true\b", cfg_txt):
                    return None
            except OSError:
                return None

        return {
            "is_git": True,
            "git_available": True,
            "toplevel": cand,
            "common_dir": common_dir,
            "exclude_file": common_dir / "info" / "exclude",
            "index_file": git_dir / "index",
        }
    return {"is_git": False, "git_available": True}


def detect_git_info(start_dir: Path) -> Dict[str, Any]:
    """Detect Git work tree, toplevel, `info/exclude`, and index paths (pure-Python fast path + `git` fallback)."""
    fast = _detect_git_info_fast(start_dir)
    if fast is not None:
        return fast
    try:
        res = run_git(
            [
                "rev-parse",
                "--is-inside-work-tree",
                "--show-toplevel",
                "--git-path",
                "info/exclude",
                "--git-path",
                "index",
                "--git-common-dir",
            ],
            start_dir,
        )
    except FileNotFoundError:
        return {"is_git": False, "git_available": False}

    if res.returncode != 0:
        return {"is_git": False, "git_available": True}

    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if len(lines) < 3 or lines[0].lower() != "true":
        return {"is_git": False, "git_available": True}

    toplevel = Path(lines[1]).resolve()

    def _abs(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (start_dir / p).resolve()

    info: Dict[str, Any] = {
        "is_git": True,
        "git_available": True,
        "toplevel": toplevel,
        "exclude_file": _abs(lines[2]),
    }
    if len(lines) >= 4:
        info["index_file"] = _abs(lines[3])
    if len(lines) >= 5:
        info["common_dir"] = _abs(lines[4])
    return info


def resolve_root(root_arg: Optional[str]) -> Path:
    if root_arg:
        return Path(root_arg).resolve()
    cwd = Path.cwd().resolve()
    git_info = detect_git_info(cwd)
    if git_info.get("is_git"):
        return git_info["toplevel"]
    return cwd


def git_exclude_pattern(root: Path, git_toplevel: Path) -> str:
    """Return a repo-root-anchored ignore pattern for `.aoci/` (e.g. `/.aoci/` or `/sub/.aoci/`)."""
    try:
        rel = root.resolve().relative_to(git_toplevel.resolve()).as_posix()
    except ValueError:
        rel = ""
    if not rel or rel == ".":
        return f"/{AOCI_DIR_NAME}/"
    return f"/{rel.strip('/')}/{AOCI_DIR_NAME}/"


def _git_exclusion_cache_key(root: Path, git_info: Dict[str, Any]) -> Dict[str, Any]:
    toplevel: Path = git_info["toplevel"]
    return {
        "root": str(root),
        "exclude": _stat_key(git_info.get("exclude_file")),
        "index": _stat_key(git_info.get("index_file")),
        "gitignore": _stat_key(toplevel / ".gitignore"),
        "sub_gitignore": _stat_key(root / ".gitignore") if root != toplevel else None,
    }


def check_git_exclusion(
    root: Path,
    git_info: Dict[str, Any],
    check_status: bool = False,
    use_cache: bool = True,
    write_cache: bool = True,
) -> Dict[str, Any]:
    """
    Verify `.aoci/` is ignored by Git and contains zero tracked files (and optionally zero `git status` leaks).
    Positive results are cached in `.aoci/.cache/` keyed on exclude/index/.gitignore stat fingerprints,
    so hot paths (hooks, `scan --fast`) skip two `git` subprocesses when nothing Git-related changed.
    """
    if not git_info.get("is_git"):
        return {
            "is_git": False,
            "excluded": True,
            "tracked_files": [],
            "status_leaks": [],
            "pattern": None,
            "exclude_file": None,
            "ok": True,
        }

    toplevel: Path = git_info["toplevel"]
    pattern = git_exclude_pattern(root, toplevel)
    aoci_rel = pattern.lstrip("/").rstrip("/")

    cache_path = root / AOCI_DIR_NAME / CACHE_DIR / GIT_EXCLUSION_CACHE
    cache_ok = use_cache and not check_status and "index_file" in git_info
    cache_key: Optional[Dict[str, Any]] = None
    if cache_ok:
        cache_key = _git_exclusion_cache_key(root, git_info)
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("key") == cache_key:
                return {
                    "is_git": True,
                    "excluded": True,
                    "tracked_files": [],
                    "status_leaks": [],
                    "pattern": pattern,
                    "exclude_file": str(git_info["exclude_file"]),
                    "ok": True,
                    "cached": True,
                }
        except (OSError, ValueError):
            pass

    # Probe a concrete child path so directory rules match reliably across Git versions
    probe_path = f"{aoci_rel}/{MANIFEST_FILE}"
    ign_res = run_git(["check-ignore", "-q", probe_path], toplevel)
    excluded = ign_res.returncode == 0

    ls_res = run_git(["ls-files", "-z", "--", aoci_rel], toplevel)
    tracked = [item.strip() for item in ls_res.stdout.split("\0") if item.strip()]

    status_leaks: List[str] = []
    if check_status:
        st_res = run_git(["status", "--porcelain=v1", "-z", "--", aoci_rel], toplevel)
        status_leaks = [item.rstrip() for item in st_res.stdout.split("\0") if item.strip()]

    ok = excluded and len(tracked) == 0 and len(status_leaks) == 0
    if ok and write_cache and (root / AOCI_DIR_NAME).is_dir():
        try:
            key = cache_key or _git_exclusion_cache_key(root, git_info)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cache_path, json.dumps({"key": key}))
        except OSError:
            pass

    return {
        "is_git": True,
        "excluded": excluded,
        "tracked_files": tracked,
        "status_leaks": status_leaks,
        "pattern": pattern,
        "exclude_file": str(git_info["exclude_file"]),
        "ok": ok,
    }


def sync_manifest_git_header(root: Path, git_excluded: bool) -> None:
    """Keep `#Git-Excluded:` in `.aoci/aoci.txt` synchronized if Git was initialized later."""
    manifest_path = root / AOCI_DIR_NAME / MANIFEST_FILE
    if not manifest_path.exists():
        return
    desired = (
        "#Git-Excluded: true (.git/info/exclude)"
        if git_excluded
        else "#Git-Excluded: n/a (non-git workspace)"
    )
    text = manifest_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    updated = False
    for idx, line in enumerate(lines):
        if line.startswith("#Git-Excluded:"):
            if line.strip() != desired:
                lines[idx] = desired
                updated = True
            break
    if updated:
        atomic_write_text(manifest_path, "\n".join(lines) + "\n")


def ensure_git_exclusion(
    root: Path,
    git_info: Dict[str, Any],
    check_status: bool = False,
) -> Dict[str, Any]:
    """Idempotently add `.aoci/` to `.git/info/exclude` and verify with Git."""
    status = check_git_exclusion(root, git_info, check_status=check_status)
    if not status["is_git"]:
        status["changed"] = False
        return status

    exclude_file: Path = git_info["exclude_file"]
    pattern: str = status["pattern"]
    changed = False

    if not status["excluded"]:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text(encoding="utf-8", errors="replace") if exclude_file.exists() else ""
        lines = existing.splitlines()
        bare_patterns = {pattern, pattern.lstrip("/"), pattern.rstrip("/"), pattern.strip("/")}
        if not any(line.strip() in bare_patterns for line in lines):
            block = f"{EXCLUDE_BEGIN}\n{pattern}\n{EXCLUDE_END}\n"
            if existing and not existing.endswith("\n"):
                existing += "\n"
            if existing and not existing.endswith("\n\n"):
                existing += "\n"
            write_text_lf(exclude_file, existing + block)
            changed = True

    post = check_git_exclusion(root, git_info, check_status=check_status) if changed else status
    post["changed"] = changed
    if post["excluded"]:
        sync_manifest_git_header(root, True)
    return post


def _git_head_key(git_info: Dict[str, Any]) -> Optional[str]:
    index_file = git_info.get("index_file")
    if not isinstance(index_file, Path):
        return None
    git_dir = index_file.parent
    head_file = git_dir / "HEAD"
    try:
        head_raw = head_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if head_raw.startswith("ref:"):
        ref_rel = head_raw.split(":", 1)[1].strip()
        common_dir = git_info.get("common_dir")
        for base in (git_dir, common_dir if isinstance(common_dir, Path) else None):
            if base is None:
                continue
            ref_path = base / ref_rel
            if ref_path.is_file():
                try:
                    return f"{head_raw}:{ref_path.read_text(encoding='utf-8', errors='replace').strip()}"
                except OSError:
                    break
        packed = (common_dir if isinstance(common_dir, Path) else git_dir) / "packed-refs"
        return f"{head_raw}:packed={_stat_key(packed)}"
    return head_raw


def _git_discovery_cache_key(root: Path, git_info: Dict[str, Any], max_bytes: int) -> Optional[Dict[str, Any]]:
    if not git_info.get("is_git") or git_info.get("toplevel") != root or "index_file" not in git_info:
        return None
    return {
        "head": _git_head_key(git_info),
        "index": _stat_key(git_info.get("index_file")),
        "exclude": _stat_key(git_info.get("exclude_file")),
        "gitignore": _stat_key(root / ".gitignore"),
        "aociignore": _stat_key(root / AOCI_DIR_NAME / IGNORE_FILE),
        "max_bytes": max_bytes,
        "skip_rules": SKIP_RULES_REV,
    }


def _parse_porcelain_z_paths(stdout: str) -> Set[str]:
    """Extract all repository-relative paths reported by `git status --porcelain=v1 -z` (incl. R/C sources)."""
    tokens = stdout.split("\0")
    paths: Set[str] = set()
    idx = 0
    while idx < len(tokens):
        rec = tokens[idx]
        idx += 1
        if len(rec) < 4:
            continue
        xy = rec[:2]
        p1 = normalize_relpath(rec[3:])
        if p1:
            paths.add(p1)
        if ("R" in xy or "C" in xy) and idx < len(tokens):
            p2 = normalize_relpath(tokens[idx])
            idx += 1
            if p2:
                paths.add(p2)
    return paths


def _save_git_discovery_cache(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    base_files: Dict[str, Any],
    fingerprints: Dict[str, Dict[str, Any]],
    max_bytes: int,
    tracked_paths: Optional[Set[str]] = None,
    install_dirs: Sequence[str] = (),
) -> None:
    key = _git_discovery_cache_key(root, git_info, max_bytes)
    aoci_dir = root / AOCI_DIR_NAME
    if key is None or not aoci_dir.is_dir():
        return
    if tracked_paths is None:
        res = run_git(["ls-files", "--cached", "-z", "--", "."], root)
        if res.returncode != 0:
            return
        raw_tracked = [normalize_relpath(item) for item in res.stdout.split("\0") if item]
    else:
        raw_tracked = sorted(tracked_paths)

    # Only files that are BOTH clean in Git status AND match the AOCI baseline may skip future reads.
    # Otherwise, an uncommitted file indexed in AOCI and later reverted to Git's committed version
    # would look clean to `git status` while diverging from `baseline.json`.
    st_res = run_git(
        ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=no", "--", "."],
        root,
    )
    if st_res.returncode != 0:
        return
    git_dirty = _parse_porcelain_z_paths(st_res.stdout)

    tracked_clean: List[str] = []
    tracked_recheck: List[str] = []
    skipped_tracked: Dict[str, int] = {}
    seen: Set[str] = set()
    for rel in raw_tracked:
        if not rel or rel in seen:
            continue
        seen.add(rel)
        reason = path_skip_reason(rel, extra_ignores)
        if reason is None and install_dirs and under_install_manifest_dir(rel, install_dirs):
            reason = "installed-skill"
        if reason is not None:
            skipped_tracked[reason] = skipped_tracked.get(reason, 0) + 1
            continue
        fp = fingerprints.get(rel)
        cached = base_files.get(rel)
        if (
            rel not in git_dirty
            and fp is not None
            and cached is not None
            and not fp.get("racy")
            and not cached.get("racy")
            and fp["sha256"] == cached.get("sha256")
        ):
            tracked_clean.append(rel)
        else:
            tracked_recheck.append(rel)

    payload = {
        "key": key,
        "tracked_clean": sorted(tracked_clean),
        "tracked_recheck": sorted(tracked_recheck),
        "skipped_tracked": skipped_tracked,
        "install_dirs": sorted(install_dirs),
    }
    cache_path = aoci_dir / CACHE_DIR / GIT_DISCOVERY_CACHE
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache_path, json.dumps(payload, separators=(",", ":")))
    except OSError:
        pass


def _demote_git_discovery_paths(aoci_dir: Path, touched_paths: Sequence[str]) -> None:
    """
    Incrementally move edited/upserted/acked paths from `tracked_clean` into `tracked_recheck`
    so single-file writes keep the 99%+ untouched clean table warm instead of deleting the cache.
    """
    cache_path = aoci_dir / CACHE_DIR / GIT_DISCOVERY_CACHE
    if not touched_paths or not cache_path.is_file():
        return
    if len(touched_paths) >= 256:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            return
        touched_set = set(touched_paths)
        clean_list = cached.get("tracked_clean", [])
        recheck_set = set(cached.get("tracked_recheck", []))
        moved = False
        new_clean: List[str] = []
        for rel in clean_list:
            if rel in touched_set:
                recheck_set.add(rel)
                moved = True
            else:
                new_clean.append(rel)
        if moved:
            cached["tracked_clean"] = new_clean
            cached["tracked_recheck"] = sorted(recheck_set)
            atomic_write_text(cache_path, json.dumps(cached, separators=(",", ":")))
    except (OSError, ValueError):
        pass


def resolve_git_hooks_dir(root: Path, git_info: Dict[str, Any]) -> Optional[Path]:
    return _githooks.resolve_git_hooks_dir(root, git_info, run_git_fn=run_git)


def install_git_hooks(
    root: Path,
    git_info: Dict[str, Any],
    *,
    python_cmd: Optional[str] = None,
    with_pre_commit: bool = False,
    pre_commit_verify: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    return _githooks.install_git_hooks(
        root,
        git_info,
        python_cmd=python_cmd,
        with_pre_commit=with_pre_commit,
        pre_commit_verify=pre_commit_verify,
        dry_run=dry_run,
        script_path=Path(__file__).resolve().parent.parent / "aoci_index.py",
        run_git_fn=run_git,
    )


def uninstall_git_hooks(
    root: Path,
    git_info: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    return _githooks.uninstall_git_hooks(
        root,
        git_info,
        dry_run=dry_run,
        run_git_fn=run_git,
    )


def read_git_blobs_batch(
    root: Path,
    git_info: Dict[str, Any],
    rev_prefix: str,
    rels: Sequence[str],
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, bytes]:
    """
    Fetch multiple `<rev_prefix>:<git_relpath>` blobs in a single `git cat-file --batch` subprocess.
    Supports revision prefixes (`HEAD`, `main`, `HEAD~1`) and index stage prefixes (`:0`).
    """
    if not git_info.get("is_git") or not rels or not _has_git_binary():
        return {}
    toplevel = git_info.get("toplevel")
    if not isinstance(toplevel, Path):
        return {}
    try:
        rel_sub = root.resolve().relative_to(toplevel.resolve()).as_posix()
    except (OSError, ValueError):
        rel_sub = ""
    sub_prefix = "" if rel_sub in ("", ".") else rel_sub.strip("/") + "/"

    ordered: List[str] = []
    specs: List[str] = []
    seen: Set[str] = set()
    for raw_r in rels:
        norm = normalize_relpath(str(raw_r))
        if not norm or norm in seen or "\n" in norm or "\r" in norm:
            continue
        seen.add(norm)
        git_rel = f"{sub_prefix}{norm}"
        spec = f"{rev_prefix}:{git_rel}"
        ordered.append(norm)
        specs.append(spec)
    if not ordered:
        return {}

    stdin_payload = ("\n".join(specs) + "\n").encode("utf-8")
    try:
        cp = subprocess.run(
            ["git", "--no-optional-locks", "cat-file", "--batch"],
            cwd=str(toplevel),
            input=stdin_payload,
            capture_output=True,
        )
    except OSError:
        return {}
    if cp.returncode != 0 or not cp.stdout:
        return {}

    out_buf = cp.stdout
    n_buf = len(out_buf)
    pos = 0
    blobs: Dict[str, bytes] = {}
    for norm in ordered:
        nl = out_buf.find(b"\n", pos)
        if nl == -1:
            break
        hdr = out_buf[pos:nl].decode("utf-8", errors="replace")
        pos = nl + 1
        if hdr.endswith(" missing") or hdr.endswith(" ambiguous"):
            continue
        parts = hdr.rsplit(" ", 2)
        if len(parts) < 3 or not parts[2].isdigit():
            break
        obj_type = parts[1]
        sz = int(parts[2])
        if pos + sz > n_buf:
            break
        body = out_buf[pos : pos + sz]
        pos += sz
        if pos < n_buf and out_buf[pos : pos + 1] == b"\n":
            pos += 1
        if obj_type == "blob" and sz <= max_bytes:
            blobs[norm] = body
    return blobs

