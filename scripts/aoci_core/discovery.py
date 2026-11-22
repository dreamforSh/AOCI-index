"""File discovery (`git ls-files` / `os.scandir`), LF SHA-256 + MinHash fingerprinting, and scan caches."""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import stat
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import git_ops as _git_ops
from . import parsers as _parsers
from .constants import (
    AOCI_DIR_NAME,
    CACHE_DIR,
    DEFAULT_MAX_BYTES,
    DEFAULT_SKIP_DIRS,
    GIT_DISCOVERY_CACHE,
    GIT_FAST_AUTO_THRESHOLD,
    INSTALL_MANIFEST_NAME,
    LINE_SKETCH_K,
    MIN_RENAME_LINES,
    PARALLEL_HASH_THRESHOLD,
    RACY_WINDOW_NS,
    SCAN_FP_CACHE,
    atomic_write_text,
    compute_size_tier,
    normalize_relpath,
)
from .ignore import (
    IgnoreMatcher,
    _read_ignore_lines,
    install_manifest_dirs,
    is_probably_binary_bytes,
    matches_ignore,
    path_skip_reason,
    under_install_manifest_dir,
)


def _matches_nested_gitignore(
    rel: str,
    is_dir: bool,
    nested_ignores: Sequence[Tuple[str, IgnoreMatcher]],
) -> bool:
    for scope_prefix, matcher in nested_ignores:
        sub_rel = rel[len(scope_prefix) + 1 :] if scope_prefix else rel
        if sub_rel and matcher.is_ignored(sub_rel, is_dir=is_dir):
            return True
    return False


def _scandir_walk(
    base_dir: Path,
    rel_prefix: str,
    extra_ignores: Any,
    out: List[Tuple[str, Optional[os.stat_result]]],
    *,
    honor_nested_gitignore: bool = False,
    _nested_ignores: Tuple[Tuple[str, IgnoreMatcher], ...] = (),
) -> None:
    """Fast `os.scandir` recursive walker reusing OS-cached `DirEntry.stat()` on Windows."""
    if honor_nested_gitignore and rel_prefix:
        gi_path = base_dir / ".gitignore"
        if gi_path.is_file():
            gi_lines = _read_ignore_lines(gi_path)
            if gi_lines:
                _nested_ignores = (*_nested_ignores, (rel_prefix.rstrip("/"), IgnoreMatcher(gi_lines)))

    try:
        with os.scandir(base_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return

    for entry in entries:
        name = entry.name
        rel = f"{rel_prefix}{name}"
        try:
            if entry.is_dir(follow_symlinks=False):
                if name in DEFAULT_SKIP_DIRS:
                    continue
                if extra_ignores and matches_ignore(rel + "/", extra_ignores):
                    continue
                if _nested_ignores and _matches_nested_gitignore(rel, True, _nested_ignores):
                    continue
                _scandir_walk(
                    Path(entry.path),
                    f"{rel}/",
                    extra_ignores,
                    out,
                    honor_nested_gitignore=honor_nested_gitignore,
                    _nested_ignores=_nested_ignores,
                )
            elif entry.is_file(follow_symlinks=False):
                if _nested_ignores and _matches_nested_gitignore(rel, False, _nested_ignores):
                    continue
                st = entry.stat(follow_symlinks=False)
                out.append((rel, st))
        except OSError:
            continue


def _attach_dir_stats(
    root: Path,
    candidates: List[Tuple[str, Optional[os.stat_result]]],
) -> List[Tuple[str, Optional[os.stat_result]]]:
    """
    Windows fast path for Git mode: one `os.scandir` per directory returns stat data straight from
    the directory enumeration (FindNextFileW), replacing one `os.stat` syscall per file.
    Symlinks and any names not found keep `None` and fall back to `os.stat`.
    """
    by_dir: Dict[str, Set[str]] = {}
    for rel, _ in candidates:
        parent, _, name = rel.rpartition("/")
        by_dir.setdefault(parent, set()).add(name)

    stats: Dict[str, os.stat_result] = {}
    for parent, names in by_dir.items():
        base = root / parent if parent else root
        if len(names) <= 2:
            # Sparse directory fast path: avoid enumerating thousands of ignored files via scandir
            for name in names:
                try:
                    lst = os.lstat(base / name)
                    if stat.S_ISREG(lst.st_mode):
                        stats[f"{parent}/{name}" if parent else name] = lst
                except OSError:
                    continue
            continue
        try:
            with os.scandir(base) as it:
                for entry in it:
                    if entry.name in names:
                        try:
                            if entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                                stats[f"{parent}/{entry.name}" if parent else entry.name] = entry.stat(
                                    follow_symlinks=False
                                )
                        except OSError:
                            continue
        except OSError:
            continue
    return [(rel, stats.get(rel)) for rel, _ in candidates]


def discover_candidates(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    tracked_out: Optional[Set[str]] = None,
    install_dirs_out: Optional[List[str]] = None,
) -> Tuple[List[Tuple[str, Optional[os.stat_result]]], Dict[str, int]]:
    """
    Discover candidate files as `(rel_posix, optional_stat_result)` after fast path-level filtering.
    When `tracked_out` is passed in Git mode, fills it with tracked paths from the same single `git ls-files -t` call.
    Directories holding an `installer.py` manifest (`.aoci-install.json`) are AOCI skill copies and are skipped;
    their prefixes are reported through `install_dirs_out` so Git discovery caches can honor them.
    """
    raw_candidates: List[Tuple[str, Optional[os.stat_result]]] = []
    skipped: Dict[str, int] = {}
    used_git = False

    if git_info.get("is_git"):
        res = _git_ops.run_git(["ls-files", "-t", "--cached", "--others", "--exclude-standard", "-z", "--", "."], root)
        if res.returncode == 0:
            used_git = True
            seen: Set[str] = set()
            for item in res.stdout.split("\0"):
                if not item:
                    continue
                is_untracked = item.startswith("? ")
                raw_rel = item[2:] if len(item) > 2 and item[1] == " " else item
                norm = normalize_relpath(raw_rel)
                if norm:
                    if tracked_out is not None and not is_untracked:
                        tracked_out.add(norm)
                    if norm not in seen:
                        seen.add(norm)
                        raw_candidates.append((norm, None))
            raw_candidates.sort(key=lambda pair: pair[0])
        else:
            git_info = {"is_git": False}

    if not git_info.get("is_git"):
        _scandir_walk(root, "", extra_ignores, raw_candidates, honor_nested_gitignore=True)
        raw_candidates.sort(key=lambda pair: pair[0])

    install_dirs = install_manifest_dirs([rel for rel, _ in raw_candidates])
    if install_dirs_out is not None:
        install_dirs_out.extend(install_dirs)

    filtered: List[Tuple[str, Optional[os.stat_result]]] = []
    for rel, st in raw_candidates:
        reason = path_skip_reason(rel, extra_ignores)
        if reason is None and install_dirs and under_install_manifest_dir(rel, install_dirs):
            reason = "installed-skill"
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        filtered.append((rel, st))

    if used_git and os.name == "nt" and filtered:
        filtered = _attach_dir_stats(root, filtered)

    return filtered, skipped


def _line_sketch(normalized: bytes) -> Optional[List[int]]:
    """
    Bottom-k CRC32 MinHash sketch over distinct non-trivial lines (>=4 stripped bytes).
    Enables fast, zero-source-storage similarity matching for moved+edited files.
    """
    hashes: Set[int] = set()
    for raw_ln in normalized.split(b"\n"):
        stripped = raw_ln.strip()
        if len(stripped) >= 4:
            hashes.add(zlib.crc32(stripped) & 0xFFFFFFFF)
    if len(hashes) < MIN_RENAME_LINES:
        return None
    return heapq.nsmallest(LINE_SKETCH_K, hashes)


def _sketch_similarity(sk_a: Optional[Sequence[int]], sk_b: Optional[Sequence[int]]) -> float:
    if not sk_a or not sk_b:
        return 0.0
    set_a = set(sk_a)
    set_b = set(sk_b)
    union_top = set(heapq.nsmallest(LINE_SKETCH_K, set_a | set_b))
    if not union_top:
        return 0.0
    shared = union_top & set_a & set_b
    if len(shared) < MIN_RENAME_LINES:
        return 0.0
    return len(shared) / len(union_top)


def _fingerprint_bytes_norm(normalized: bytes, st: os.stat_result, with_sketch: bool = True) -> Dict[str, Any]:
    """
    Fingerprint normalized-LF bytes. `with_sketch=False` skips the MinHash line sketch (roughly 40% of
    per-file cost); `compute_drift` attaches sketches later only for real rename candidates.
    """
    lines = 0 if not normalized else normalized.count(b"\n") + (0 if normalized.endswith(b"\n") else 1)
    fp: Dict[str, Any] = {
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "lines": lines,
        "bytes": st.st_size,
        "size_tier": compute_size_tier(lines),
        "mtime_ns": st.st_mtime_ns,
    }
    if with_sketch and lines >= MIN_RENAME_LINES:
        sketch = _line_sketch(normalized)
        if sketch is not None:
            fp["sketch"] = sketch
    # Racy-clean guard: a write landing in the same timestamp tick after hashing would keep
    # (mtime, size) identical, so never trust the stat shortcut for freshly modified files.
    if time.time_ns() - st.st_mtime_ns < RACY_WINDOW_NS:
        fp["racy"] = True
    return fp


def _fingerprint_bytes(raw: bytes, st: os.stat_result) -> Dict[str, Any]:
    normalized = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    return _fingerprint_bytes_norm(normalized, st)


def attach_line_sketches(root: Path, fingerprints: Dict[str, Dict[str, Any]], rels: Sequence[str]) -> int:
    """
    Compute deferred MinHash sketches in place for `rels` (fingerprints produced with `with_sketch=False`).
    Re-reads each candidate once and only attaches a sketch when the content still matches the recorded
    SHA-256, so a file edited mid-scan can never pair with a stale sketch. Returns the number attached.
    """
    attached = 0
    for rel in rels:
        fp = fingerprints.get(rel)
        if fp is None or "sketch" in fp or int(fp.get("lines", 0) or 0) < MIN_RENAME_LINES:
            continue
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            continue
        normalized = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
        if hashlib.sha256(normalized).hexdigest() != fp.get("sha256"):
            continue
        sketch = _line_sketch(normalized)
        if sketch is not None:
            fp["sketch"] = sketch
            attached += 1
    return attached


def inspect_and_fingerprint_one(
    root: Path,
    rel: str,
    pre_stat: Optional[os.stat_result],
    cached: Optional[Dict[str, Any]],
    fast_mtime: bool,
    max_bytes: int,
    with_sketch: bool = True,
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """
    Single-stat, single-read file classifier + fingerprinter.
    On fast_mtime cache hit: ZERO file opens and ZERO SHA-256 work.
    Returns `(rel, fingerprint_or_None, skip_reason_or_None)`.
    """
    full = root / rel
    st = pre_stat
    if st is None:
        try:
            st = os.lstat(full)
        except OSError:
            return rel, None, "missing"

    if stat.S_ISLNK(st.st_mode):
        return rel, None, "symlink"
    if not stat.S_ISREG(st.st_mode):
        return rel, None, "not-regular-file"
    if st.st_size > max_bytes:
        return rel, None, f"too-large(>{max_bytes}B)"

    # Fast cache hit: already validated text file with unchanged mtime_ns and byte size (never racy)
    if (
        fast_mtime
        and cached is not None
        and not cached.get("racy")
        and cached.get("mtime_ns") == st.st_mtime_ns
        and cached.get("bytes") == st.st_size
        and "sha256" in cached
        and "lines" in cached
    ):
        fp = dict(cached)
        fp["size_tier"] = compute_size_tier(int(fp["lines"]))
        return rel, fp, None

    try:
        raw = full.read_bytes()
    except OSError:
        return rel, None, "unreadable"

    if is_probably_binary_bytes(raw[:8192]):
        return rel, None, "binary-content"

    normalized = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    fp = _fingerprint_bytes_norm(normalized, st, with_sketch=with_sketch)
    if (
        cached is not None
        and fp["sha256"] != cached.get("sha256")
        and isinstance(cached.get("sig_hash"), str)
    ):
        fp["_cur_sig_hash"] = _parsers.compute_signature_hash_from_bytes(normalized, full.suffix.lower(), rel)
    return rel, fp, None


def _try_git_incremental_scan(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    base_files: Dict[str, Any],
    max_bytes: int,
    with_sketch: bool = True,
) -> Optional[Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]]:
    """
    10k-file Git fast path: when `.git/index` and ignore files match cached stats, run a single
    `git status --porcelain=v1 -z -uall` (which uses Git's native multi-threaded C stat cache)
    instead of `git ls-files` + statting thousands of unchanged tracked files in Python.
    """
    key = _git_ops._git_discovery_cache_key(root, git_info, max_bytes)
    if key is None:
        return None
    cache_path = root / AOCI_DIR_NAME / CACHE_DIR / GIT_DISCOVERY_CACHE
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict) or cached.get("key") != key:
        return None

    res = _git_ops.run_git(
        ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
        root,
    )
    if res.returncode != 0:
        return None

    status_paths = _git_ops._parse_porcelain_z_paths(res.stdout)

    # Nested `.gitignore` edited -> fall back to full discovery so new ignore rules apply
    if any(p == ".gitignore" or p.endswith("/.gitignore") for p in status_paths):
        return None
    # Installed AOCI skill copy added/removed (`.aoci-install.json`) -> full discovery recomputes skip prefixes
    if any(p.rsplit("/", 1)[-1] == INSTALL_MANIFEST_NAME for p in status_paths):
        return None
    install_dirs: Tuple[str, ...] = tuple(d for d in cached.get("install_dirs", []) if isinstance(d, str))

    skipped: Dict[str, int] = dict(cached.get("skipped_tracked", {}))
    fingerprints: Dict[str, Dict[str, Any]] = {}
    recheck_orig: Set[str] = set(cached.get("tracked_recheck", []))
    to_probe: Set[str] = set(recheck_orig)

    for rel in cached.get("tracked_clean", []):
        c = base_files.get(rel)
        if rel not in status_paths and c is not None and not c.get("racy") and "sha256" in c and "lines" in c:
            fp = dict(c)
            fp["size_tier"] = compute_size_tier(int(fp["lines"]))
            fingerprints[rel] = fp
        else:
            to_probe.add(rel)

    if install_dirs:
        to_probe = {rel for rel in to_probe if not under_install_manifest_dir(rel, install_dirs)}

    for rel in status_paths:
        reason = path_skip_reason(rel, extra_ignores)
        if reason is None and install_dirs and under_install_manifest_dir(rel, install_dirs):
            reason = "installed-skill"
        if reason is not None:
            if rel not in to_probe:
                skipped[reason] = skipped.get(reason, 0) + 1
            continue
        to_probe.add(rel)

    sorted_probe = sorted(to_probe)
    probe_results: List[Tuple[str, Optional[Dict[str, Any]], Optional[str]]]
    if len(sorted_probe) >= PARALLEL_HASH_THRESHOLD:
        workers = min(8, max(2, (os.cpu_count() or 4) * 2))
        import concurrent.futures  # deferred: pulls in `logging` (~20ms cold start)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    inspect_and_fingerprint_one,
                    root,
                    rel,
                    None,
                    base_files.get(rel),
                    True,
                    max_bytes,
                    with_sketch=with_sketch,
                )
                for rel in sorted_probe
            ]
            probe_results = [fut.result() for fut in futures]
    else:
        probe_results = [
            inspect_and_fingerprint_one(root, rel, None, base_files.get(rel), True, max_bytes, with_sketch=with_sketch)
            for rel in sorted_probe
        ]

    promoted: List[str] = []
    for rel, fp, reason in probe_results:
        c_base = base_files.get(rel)
        if reason is not None:
            if reason != "missing":
                skipped[reason] = skipped.get(reason, 0) + 1
        elif fp is not None:
            fingerprints[rel] = fp
            if (
                rel in recheck_orig
                and rel not in status_paths
                and c_base is not None
                and not fp.get("racy")
                and not c_base.get("racy")
                and fp["sha256"] == c_base.get("sha256")
            ):
                promoted.append(rel)

    if promoted:
        try:
            prom_set = set(promoted)
            cached["tracked_clean"] = sorted(set(cached.get("tracked_clean", [])) | prom_set)
            cached["tracked_recheck"] = sorted(recheck_orig - prom_set)
            atomic_write_text(cache_path, json.dumps(cached, separators=(",", ":")))
        except OSError:
            pass

    return fingerprints, skipped


def _is_scan_cache_fresh(fp: Dict[str, Any], now_ns: Optional[int] = None) -> Dict[str, Any]:
    """
    Normalize an unindexed fingerprint for `.aoci/.cache/scan_fps.json`.
    Clears `racy` once sub-second mtime timestamps have stabilized (>5ms since write) so rapid
    multi-batch cold bootstrap skips redundant hashing while keeping 1s coarse FS protections intact.
    """
    cleaned = {k: v for k, v in fp.items() if k != "size_tier" and not k.startswith("_")}
    if cleaned.get("racy"):
        mtime_ns = int(cleaned.get("mtime_ns", 0) or 0)
        ts_now = time.time_ns() if now_ns is None else now_ns
        if (mtime_ns % 1_000_000_000 != 0 and ts_now - mtime_ns >= 5_000_000) or (
            ts_now - mtime_ns >= RACY_WINDOW_NS
        ):
            cleaned.pop("racy", None)
    return cleaned


def _load_scan_fp_cache(aoci_dir: Path, max_bytes: int) -> Dict[str, Dict[str, Any]]:
    cache_path = aoci_dir / CACHE_DIR / SCAN_FP_CACHE
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("max_bytes") != max_bytes:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_scan_fp_cache(
    aoci_dir: Path,
    fingerprints: Dict[str, Dict[str, Any]],
    base_files: Dict[str, Any],
    max_bytes: int,
    prev_files: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    if not aoci_dir.is_dir():
        return
    cache_path = aoci_dir / CACHE_DIR / SCAN_FP_CACHE
    now_ns = time.time_ns()
    unindexed = {
        rel: _is_scan_cache_fresh(fp, now_ns=now_ns)
        for rel, fp in fingerprints.items()
        if rel not in base_files
    }
    if not unindexed:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return
    if prev_files is not None and unindexed == prev_files:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            cache_path,
            json.dumps(
                {"schema_version": 1, "max_bytes": max_bytes, "files": unindexed},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    except OSError:
        pass


def _prune_scan_fp_cache(aoci_dir: Path, touched_paths: Sequence[str]) -> bool:
    """Drop newly indexed/pruned paths from `scan_fps.json`; returns True when cache is now empty/absent."""
    cache_path = aoci_dir / CACHE_DIR / SCAN_FP_CACHE
    if not cache_path.is_file():
        return True
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return False
    removed = False
    for p in touched_paths:
        if p in files:
            del files[p]
            removed = True
    if not files:
        try:
            cache_path.unlink()
        except OSError:
            pass
        return True
    if removed:
        try:
            atomic_write_text(
                cache_path,
                json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        except OSError:
            pass
    return False


def discover_and_fingerprint(
    root: Path,
    git_info: Dict[str, Any],
    extra_ignores: Any,
    base_files: Dict[str, Any],
    fast_mtime: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    skip_paths: Optional[Dict[str, str]] = None,
    git_incremental: bool = False,
    read_scan_cache: bool = True,
    save_scan_cache: bool = False,
    with_sketch: bool = True,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """
    Discover eligible source files and compute fingerprints in one pass (multi-threaded on large cold scans).
    When `fast_mtime=True` and `read_scan_cache=True`, unindexed files also reuse `.aoci/.cache/scan_fps.json`
    on matching non-racy `(mtime_ns, bytes)`, keeping `baseline.json` strictly human-verified.
    `with_sketch=False` defers MinHash sketches (see `attach_line_sketches`) for read-only drift scans.
    """
    use_git_inc = skip_paths is None and (git_incremental or (fast_mtime and len(base_files) >= GIT_FAST_AUTO_THRESHOLD))
    if use_git_inc:
        inc = _try_git_incremental_scan(root, git_info, extra_ignores, base_files, max_bytes, with_sketch=with_sketch)
        if inc is not None:
            return inc

    want_cache = skip_paths is None and git_info.get("is_git")
    tracked_set: Optional[Set[str]] = set() if want_cache else None
    install_dirs: List[str] = []
    candidates, skipped = discover_candidates(
        root, git_info, extra_ignores, tracked_out=tracked_set, install_dirs_out=install_dirs
    )

    aoci_dir = root / AOCI_DIR_NAME
    scan_fps: Dict[str, Dict[str, Any]] = {}
    if read_scan_cache and skip_paths is None and aoci_dir.is_dir():
        scan_fps = _load_scan_fp_cache(aoci_dir, max_bytes)

    # Decide whether parallel hashing helps (count how many files likely need disk reads)
    need_io = 0
    if not fast_mtime:
        need_io = len(candidates)
    else:
        for rel, st in candidates:
            c = base_files.get(rel) if rel in base_files else scan_fps.get(rel)
            if (
                c is None
                or c.get("racy")
                or (st is not None and (c.get("mtime_ns") != st.st_mtime_ns or c.get("bytes") != st.st_size))
            ):
                need_io += 1

    fingerprints: Dict[str, Dict[str, Any]] = {}

    def _collect(rel: str, fp: Optional[Dict[str, Any]], reason: Optional[str]) -> None:
        if reason is not None:
            if reason != "missing":
                skipped[reason] = skipped.get(reason, 0) + 1
            if skip_paths is not None:
                skip_paths[rel] = reason
        elif fp is not None:
            fingerprints[rel] = fp

    def _cached_fp_for(rel_path: str) -> Optional[Dict[str, Any]]:
        return base_files.get(rel_path) if rel_path in base_files else scan_fps.get(rel_path)

    if need_io >= PARALLEL_HASH_THRESHOLD:
        workers = min(8, max(2, (os.cpu_count() or 4) * 2))
        import concurrent.futures  # deferred: pulls in `logging` (~20ms cold start)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    inspect_and_fingerprint_one,
                    root,
                    rel,
                    st,
                    _cached_fp_for(rel),
                    fast_mtime,
                    max_bytes,
                    with_sketch=with_sketch,
                )
                for rel, st in candidates
            ]
            for fut in futures:
                _collect(*fut.result())
    else:
        for rel, st in candidates:
            _collect(
                *inspect_and_fingerprint_one(
                    root, rel, st, _cached_fp_for(rel), fast_mtime, max_bytes, with_sketch=with_sketch
                )
            )

    if save_scan_cache and skip_paths is None and aoci_dir.is_dir():
        _save_scan_fp_cache(
            aoci_dir,
            fingerprints,
            base_files,
            max_bytes,
            prev_files=scan_fps if fast_mtime else None,
        )

    if skip_paths is None and (git_incremental or len(fingerprints) >= GIT_FAST_AUTO_THRESHOLD):
        _git_ops._save_git_discovery_cache(
            root,
            git_info,
            extra_ignores,
            base_files,
            fingerprints,
            max_bytes,
            tracked_paths=tracked_set,
            install_dirs=install_dirs,
        )

    return fingerprints, skipped


def fingerprint_file(full_path: Path) -> Dict[str, Any]:
    """Single-file fingerprinter used during targeted `upsert` / `ack` writes (stat before read)."""
    st = os.stat(full_path)
    raw = full_path.read_bytes()
    norm = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    fp = _fingerprint_bytes_norm(norm, st)
    sig_hash = _parsers.compute_signature_hash_from_bytes(norm, full_path.suffix.lower(), full_path.name)
    if sig_hash is not None:
        fp["sig_hash"] = sig_hash
    return fp
