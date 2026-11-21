"""SQLite WAL semantic cache pool (legacy JSON migration, LRU, export/import, read-only restore count)."""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .constants import (
    AOCI_DIR_NAME,
    BASELINE_FILE,
    CACHE_DIR,
    CODE_FILE,
    ENTRY_RE,
    MAX_SEMANTIC_POOL_ENTRIES,
    MIN_RENAME_LINES,
    VALID_LAYERS,
    VALID_SIZES,
    normalize_relpath,
    utc_now_iso,
)
from .graph import validate_parsed_entry_tags

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _legacy_semantic_pool_json_path(root: Path, git_info: Dict[str, Any]) -> Path:
    common_dir = git_info.get("common_dir")
    toplevel = git_info.get("toplevel")
    if git_info.get("is_git") and isinstance(common_dir, Path) and isinstance(toplevel, Path):
        try:
            rel = root.resolve().relative_to(toplevel.resolve()).as_posix()
        except ValueError:
            rel = "."
        slug = "root" if rel in ("", ".") else re.sub(r"[^A-Za-z0-9_.-]", "_", rel.replace("/", "__"))
        return common_dir / "aoci-cache" / f"{slug}.pool.json"
    return root / AOCI_DIR_NAME / CACHE_DIR / "entry_pool.json"


def semantic_pool_path(root: Path, git_info: Dict[str, Any]) -> Path:
    """Resolve the local semantic entry cache pool SQLite WAL path."""
    common_dir = git_info.get("common_dir")
    toplevel = git_info.get("toplevel")
    if git_info.get("is_git") and isinstance(common_dir, Path) and isinstance(toplevel, Path):
        try:
            rel = root.resolve().relative_to(toplevel.resolve()).as_posix()
        except ValueError:
            rel = "."
        slug = "root" if rel in ("", ".") else re.sub(r"[^A-Za-z0-9_.-]", "_", rel.replace("/", "__"))
        return common_dir / "aoci-cache" / f"{slug}.pool.sqlite3"
    return root / AOCI_DIR_NAME / CACHE_DIR / "entry_pool.sqlite3"


def _load_legacy_semantic_pool_json(path: Path) -> Dict[str, Dict[str, str]]:
    empty: Dict[str, Dict[str, str]] = {"by_path_sha": {}, "by_sha": {}}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        by_ps = data.get("by_path_sha")
        by_s = data.get("by_sha")
        return {
            "by_path_sha": dict(by_ps) if isinstance(by_ps, dict) else {},
            "by_sha": dict(by_s) if isinstance(by_s, dict) else {},
        }
    except (OSError, ValueError):
        return empty


def _open_semantic_pool(
    db_path: Path,
    legacy_json: Optional[Path] = None,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    if readonly:
        try:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (sqlite3.Error, ValueError, OSError):
            conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
        return conn

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pool_by_path_sha ("
            "relpath TEXT NOT NULL, sha256 TEXT NOT NULL, entry TEXT NOT NULL, last_used INTEGER NOT NULL, "
            "PRIMARY KEY (relpath, sha256))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pool_by_sha ("
            "sha256 TEXT PRIMARY KEY, entry TEXT NOT NULL, last_used INTEGER NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS pool_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_sha ON pool_by_path_sha(sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_last_used ON pool_by_path_sha(last_used)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_s_last_used ON pool_by_sha(last_used)")

        if legacy_json is not None and legacy_json.is_file():
            migrated_row = conn.execute(
                "SELECT value FROM pool_meta WHERE key = 'legacy_migrated'"
            ).fetchone()
            if migrated_row is None:
                cnt_ps = conn.execute("SELECT COUNT(*) FROM pool_by_path_sha").fetchone()[0]
                cnt_s = conn.execute("SELECT COUNT(*) FROM pool_by_sha").fetchone()[0]
                if cnt_ps == 0 and cnt_s == 0:
                    legacy = _load_legacy_semantic_pool_json(legacy_json)
                    base_ns = time.time_ns()
                    ps_rows = []
                    for idx, (k, entry) in enumerate(legacy["by_path_sha"].items()):
                        if "\t" in k and entry:
                            rel, sha = k.rsplit("\t", 1)
                            ps_rows.append((rel, sha, str(entry), base_ns + idx))
                    s_rows = [
                        (sha, str(entry), base_ns + idx)
                        for idx, (sha, entry) in enumerate(legacy["by_sha"].items())
                        if sha and entry
                    ]
                    with conn:
                        if ps_rows:
                            conn.executemany(
                                "INSERT OR REPLACE INTO pool_by_path_sha (relpath, sha256, entry, last_used) VALUES (?, ?, ?, ?)",
                                ps_rows,
                            )
                        if s_rows:
                            conn.executemany(
                                "INSERT OR REPLACE INTO pool_by_sha (sha256, entry, last_used) VALUES (?, ?, ?)",
                                s_rows,
                            )
                        conn.execute(
                            "INSERT OR REPLACE INTO pool_meta (key, value) VALUES ('legacy_migrated', '1')"
                        )
                else:
                    with conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO pool_meta (key, value) VALUES ('legacy_migrated', '1')"
                        )
        return conn
    except Exception:
        conn.close()
        raise


def load_semantic_pool(root: Path, git_info: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    db_path = semantic_pool_path(root, git_info)
    legacy_json = _legacy_semantic_pool_json_path(root, git_info)
    if not db_path.is_file():
        return _load_legacy_semantic_pool_json(legacy_json)
    try:
        with contextlib.closing(_open_semantic_pool(db_path, readonly=True)) as conn:
            by_ps = {
                f"{rel}\t{sha}": entry
                for rel, sha, entry in conn.execute(
                    "SELECT relpath, sha256, entry FROM pool_by_path_sha ORDER BY last_used ASC"
                )
            }
            by_s = {
                sha: entry
                for sha, entry in conn.execute(
                    "SELECT sha256, entry FROM pool_by_sha ORDER BY last_used ASC"
                )
            }
            return {"by_path_sha": by_ps, "by_sha": by_s}
    except (sqlite3.Error, OSError):
        return {"by_path_sha": {}, "by_sha": {}}


def _evict_pool_excess(conn: sqlite3.Connection, max_entries: int) -> None:
    for tbl in ("pool_by_path_sha", "pool_by_sha"):
        cnt = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        if cnt > max_entries:
            excess = cnt - max_entries
            conn.execute(
                f"DELETE FROM {tbl} WHERE rowid IN "
                f"(SELECT rowid FROM {tbl} ORDER BY last_used ASC LIMIT ?)",
                (excess,),
            )


def record_semantic_pool_entries(
    root: Path,
    git_info: Dict[str, Any],
    records: Sequence[Tuple[str, Dict[str, Any], str]],
    max_entries: int = MAX_SEMANTIC_POOL_ENTRIES,
) -> None:
    """Best-effort persist `(relpath, fingerprint, normalized_entry)` into the shared SQLite WAL semantic pool."""
    if not records:
        return
    now_ns = time.time_ns()
    ps_rows: List[Tuple[str, str, str, int]] = []
    s_rows: List[Tuple[str, str, int]] = []
    for idx, (rel, fp, line) in enumerate(records):
        if not isinstance(fp, dict):
            continue
        sha = fp.get("sha256")
        if not isinstance(sha, str) or not sha or not line:
            continue
        ts = now_ns + idx
        ps_rows.append((rel, sha, line, ts))
        if int(fp.get("lines", 0)) >= MIN_RENAME_LINES:
            s_rows.append((sha, line, ts))
    if not ps_rows:
        return

    db_path = semantic_pool_path(root, git_info)
    legacy_json = _legacy_semantic_pool_json_path(root, git_info)
    try:
        with contextlib.closing(_open_semantic_pool(db_path, legacy_json=legacy_json)) as conn:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO pool_by_path_sha (relpath, sha256, entry, last_used) VALUES (?, ?, ?, ?)",
                    ps_rows,
                )
                if s_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO pool_by_sha (sha256, entry, last_used) VALUES (?, ?, ?)",
                        s_rows,
                    )
                _evict_pool_excess(conn, max_entries)
    except (sqlite3.Error, OSError):
        pass


def _lookup_pool_entries(
    root: Path,
    git_info: Dict[str, Any],
    ps_queries: Sequence[Tuple[str, str]],
    sha_queries: Sequence[str],
    touch_lru: bool = False,
) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    """Query only requested `(relpath, sha256)` and `sha256` keys from the semantic pool."""
    ps_hits: Dict[Tuple[str, str], str] = {}
    s_hits: Dict[str, str] = {}
    if not ps_queries and not sha_queries:
        return ps_hits, s_hits

    db_path = semantic_pool_path(root, git_info)
    legacy_json = _legacy_semantic_pool_json_path(root, git_info)
    if not db_path.is_file():
        if not legacy_json.is_file():
            return ps_hits, s_hits
        if not touch_lru:
            legacy = _load_legacy_semantic_pool_json(legacy_json)
            by_ps = legacy["by_path_sha"]
            by_s = legacy["by_sha"]
            for rel, sha in ps_queries:
                ent = by_ps.get(f"{rel}\t{sha}")
                if ent:
                    ps_hits[(rel, sha)] = ent
            for sha in sha_queries:
                ent = by_s.get(sha)
                if ent:
                    s_hits[sha] = ent
            return ps_hits, s_hits

    want_ps = sorted(set(ps_queries))
    unique_shas = sorted(set(sha_queries))
    chunk_size = 400
    pair_chunk_size = 200
    try:
        with contextlib.closing(
            _open_semantic_pool(
                db_path,
                legacy_json=legacy_json if touch_lru else None,
                readonly=not touch_lru,
            )
        ) as conn:
            if want_ps:
                row_value_supported = True
                for i in range(0, len(want_ps), pair_chunk_size):
                    pair_chunk = want_ps[i : i + pair_chunk_size]
                    pair_ph = ",".join("(?,?)" for _ in pair_chunk)
                    flat_params = [val for pair in pair_chunk for val in pair]
                    try:
                        for rel, sha, entry in conn.execute(
                            f"SELECT relpath, sha256, entry FROM pool_by_path_sha WHERE (relpath, sha256) IN ({pair_ph})",
                            flat_params,
                        ):
                            ps_hits[(rel, sha)] = entry
                    except sqlite3.OperationalError:
                        row_value_supported = False
                        break
                if not row_value_supported:
                    want_ps_set = set(want_ps)
                    unique_ps_shas = sorted({sha for _, sha in want_ps})
                    for i in range(0, len(unique_ps_shas), chunk_size):
                        chunk = unique_ps_shas[i : i + chunk_size]
                        ph = ",".join("?" for _ in chunk)
                        for rel, sha, entry in conn.execute(
                            f"SELECT relpath, sha256, entry FROM pool_by_path_sha WHERE sha256 IN ({ph})",
                            chunk,
                        ):
                            if (rel, sha) in want_ps_set:
                                ps_hits[(rel, sha)] = entry

            for i in range(0, len(unique_shas), chunk_size):
                chunk = unique_shas[i : i + chunk_size]
                ph = ",".join("?" for _ in chunk)
                for sha, entry in conn.execute(
                    f"SELECT sha256, entry FROM pool_by_sha WHERE sha256 IN ({ph})",
                    chunk,
                ):
                    s_hits[sha] = entry

            if touch_lru and (ps_hits or s_hits):
                now_ns = time.time_ns()
                with conn:
                    if ps_hits:
                        conn.executemany(
                            "UPDATE pool_by_path_sha SET last_used = ? WHERE relpath = ? AND sha256 = ?",
                            [(now_ns, rel, sha) for rel, sha in ps_hits],
                        )
                    if s_hits:
                        conn.executemany(
                            "UPDATE pool_by_sha SET last_used = ? WHERE sha256 = ?",
                            [(now_ns, sha) for sha in s_hits],
                        )
    except (sqlite3.Error, OSError):
        pass
    return ps_hits, s_hits


def count_restorable_from_pool(
    root: Path,
    git_info: Dict[str, Any],
    drift: Dict[str, Any],
    allow_sha_fallback: bool = False,
) -> int:
    """
    Read-only count of drifted files in `drift` that can be zero-token restored from the semantic pool.
    Defaults to exact `(relpath, sha256)` matches so identical copies at different paths are never counted
    as auto-restorable.
    """
    fps: Dict[str, Dict[str, Any]] = drift.get("_fingerprints", {})
    if not fps:
        return 0
    cand_paths = (
        [it["path"] for it in drift.get("added", ())]
        + [it["path"] for it in drift.get("modified", ())]
        + [it["path"] for it in drift.get("missing_in_code", ())]
    )
    if not cand_paths:
        return 0

    live_sha_counts: Dict[str, int] = {}
    for fp_all in fps.values():
        sha = fp_all.get("sha256")
        if sha:
            live_sha_counts[sha] = live_sha_counts.get(sha, 0) + 1

    unindexed_sha_counts: Dict[str, int] = {}
    for p in cand_paths + [r["to"] for r in drift.get("renamed", ())]:
        sha = fps.get(p, {}).get("sha256")
        if sha:
            unindexed_sha_counts[sha] = unindexed_sha_counts.get(sha, 0) + 1

    ps_queries: List[Tuple[str, str]] = []
    sha_queries: List[str] = []
    for rel in cand_paths:
        fp = fps.get(rel)
        if not fp or not fp.get("sha256"):
            continue
        sha = fp["sha256"]
        ps_queries.append((rel, sha))
        if (
            allow_sha_fallback
            and int(fp.get("lines", 0)) >= MIN_RENAME_LINES
            and unindexed_sha_counts.get(sha, 0) == 1
            and live_sha_counts.get(sha, 0) == 1
        ):
            sha_queries.append(sha)

    ps_hits, s_hits = _lookup_pool_entries(root, git_info, ps_queries, sha_queries, touch_lru=False)
    restorable = 0
    for rel in cand_paths:
        fp = fps.get(rel)
        if not fp or not fp.get("sha256"):
            continue
        sha = fp["sha256"]
        cand = ps_hits.get((rel, sha))
        via_sha = False
        if (
            cand is None
            and allow_sha_fallback
            and int(fp.get("lines", 0)) >= MIN_RENAME_LINES
            and unindexed_sha_counts.get(sha, 0) == 1
            and live_sha_counts.get(sha, 0) == 1
        ):
            cand = s_hits.get(sha)
            via_sha = cand is not None
        if not cand:
            continue
        m = ENTRY_RE.match(cand)
        if not m or m.group("layer") not in VALID_LAYERS or m.group("size") not in VALID_SIZES:
            continue
        if via_sha:
            orig_rel = normalize_relpath(m.group("path"))
            if orig_rel and orig_rel != rel and orig_rel in fps:
                continue
        restorable += 1
    return restorable


def pool_stats(root: Path, git_info: Dict[str, Any], max_entries: int = MAX_SEMANTIC_POOL_ENTRIES) -> Dict[str, Any]:
    db_path = semantic_pool_path(root, git_info)
    legacy_json = _legacy_semantic_pool_json_path(root, git_info)
    exists = db_path.is_file()
    size_bytes = db_path.stat().st_size if exists else 0
    ps_cnt = 0
    s_cnt = 0
    if exists:
        try:
            with contextlib.closing(_open_semantic_pool(db_path, readonly=True)) as conn:
                ps_cnt = int(conn.execute("SELECT COUNT(*) FROM pool_by_path_sha").fetchone()[0])
                s_cnt = int(conn.execute("SELECT COUNT(*) FROM pool_by_sha").fetchone()[0])
        except (sqlite3.Error, OSError):
            pass
    elif legacy_json.is_file():
        legacy = _load_legacy_semantic_pool_json(legacy_json)
        ps_cnt = len(legacy["by_path_sha"])
        s_cnt = len(legacy["by_sha"])
    return {
        "ok": True,
        "root": str(root),
        "pool_path": str(db_path),
        "exists": exists,
        "bytes": size_bytes,
        "by_path_sha_count": ps_cnt,
        "by_sha_count": s_cnt,
        "max_entries": max_entries,
    }


def _validate_pool_entry_row(expected_rel: Optional[str], sha: Any, entry: Any) -> Optional[str]:
    if not isinstance(sha, str) or not _SHA256_HEX_RE.match(sha):
        return None
    if not isinstance(entry, str):
        return None
    line = entry.strip()
    if not line or "\n" in line or "\r" in line:
        return None
    m = ENTRY_RE.match(line)
    if not m:
        return None
    entry_rel = normalize_relpath(m.group("path").strip())
    if expected_rel is not None:
        norm_rel = normalize_relpath(expected_rel)
        if not norm_rel or norm_rel != entry_rel:
            return None
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in m.groupdict().items()}
    if validate_parsed_entry_tags(entry_rel, fields, line, actual_lines=None):
        return None
    return (
        f"{entry_rel}[{fields['layer']}{fields['domain']}{fields['imp']}{fields['size']}]: "
        f"F:{fields['f']} | R:{fields['r']} | A:{fields['a']} | S:{fields['s']}"
    )


def _fallback_parse_code(code_path: Path) -> Dict[str, str]:
    entries: Dict[str, str] = {}
    for raw in code_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or (line.startswith("===") and line.endswith("===")):
            continue
        m = ENTRY_RE.match(line)
        if m:
            entries[normalize_relpath(m.group("path"))] = line
    return entries


def _fallback_load_baseline(aoci_dir: Path) -> Dict[str, Any]:
    bp = aoci_dir / BASELINE_FILE
    if not bp.is_file():
        return {"files": {}}
    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"files": {}}
    except (OSError, ValueError):
        return {"files": {}}


def export_semantic_pool(
    root: Path,
    git_info: Dict[str, Any],
    out_path: Path,
    *,
    include_current: bool = True,
    parse_code_volume_fn: Optional[Callable[[Path], Tuple[Dict[str, str], Any]]] = None,
    load_baseline_fn: Optional[Callable[[Path], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    pool_data = load_semantic_pool(root, git_info)
    ps_valid: Dict[Tuple[str, str], str] = {}
    s_valid: Dict[str, str] = {}

    for key_str, entry in pool_data.get("by_path_sha", {}).items():
        if "\t" not in key_str:
            continue
        rel, sha = key_str.rsplit("\t", 1)
        clean_e = _validate_pool_entry_row(rel, sha, entry)
        if clean_e:
            ps_valid[(normalize_relpath(rel), sha)] = clean_e

    for sha, entry in pool_data.get("by_sha", {}).items():
        clean_e = _validate_pool_entry_row(None, sha, entry)
        if clean_e:
            s_valid[sha] = clean_e

    if include_current:
        aoci_dir = root / AOCI_DIR_NAME
        code_path = aoci_dir / CODE_FILE
        if code_path.is_file():
            entries = parse_code_volume_fn(code_path)[0] if parse_code_volume_fn else _fallback_parse_code(code_path)
            base_obj = load_baseline_fn(aoci_dir) if load_baseline_fn else _fallback_load_baseline(aoci_dir)
            base_files = base_obj.get("files", {})
            for rel, line in entries.items():
                fp = base_files.get(rel)
                if not isinstance(fp, dict):
                    continue
                sha = fp.get("sha256")
                clean_e = _validate_pool_entry_row(rel, sha, line)
                if clean_e and isinstance(sha, str):
                    ps_valid[(rel, sha)] = clean_e
                    if int(fp.get("lines", 0)) >= MIN_RENAME_LINES:
                        s_valid[sha] = clean_e

    ps_list = [{"path": rel, "sha256": sha, "entry": line} for (rel, sha), line in sorted(ps_valid.items())]
    s_list = [{"sha256": sha, "entry": line} for sha, line in sorted(s_valid.items())]
    bundle = {
        "schema_version": 1,
        "format": "aoci-semantic-pool-v1",
        "exported_at": utc_now_iso(),
        "by_path_sha": ps_list,
        "by_sha": s_list,
    }
    raw_json = (json.dumps(bundle, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    use_gz = out_path.name.lower().endswith(".gz")
    payload_bytes = gzip.compress(raw_json) if use_gz else raw_json

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with tmp_path.open("wb") as fh:
            fh.write(payload_bytes)
        tmp_path.replace(out_path)
    except OSError as exc:
        return {"ok": False, "error": f"Cannot write pool bundle `{out_path}`: {exc}"}
    return {
        "ok": True,
        "output_path": str(out_path),
        "gzip": use_gz,
        "bytes": len(payload_bytes),
        "exported_by_path_sha": len(ps_list),
        "exported_by_sha": len(s_list),
    }


def import_semantic_pool(
    root: Path,
    git_info: Dict[str, Any],
    in_path: Path,
    *,
    dry_run: bool = False,
    max_entries: int = MAX_SEMANTIC_POOL_ENTRIES,
) -> Dict[str, Any]:
    if not in_path.is_file():
        return {"ok": False, "error": f"Pool bundle file not found: `{in_path}`"}
    try:
        raw = in_path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, zlib.error) as exc:
        return {"ok": False, "error": f"Cannot parse pool bundle `{in_path}`: {exc}"}

    if not isinstance(data, dict) or (
        data.get("format") not in (None, "aoci-semantic-pool-v1")
        or ("by_path_sha" not in data and "by_sha" not in data)
    ):
        return {"ok": False, "error": f"Unrecognized pool bundle schema in `{in_path}`."}

    now_ns = time.time_ns()
    ps_rows: List[Tuple[str, str, str, int]] = []
    s_rows: List[Tuple[str, str, int]] = []
    rejected: List[Dict[str, str]] = []

    raw_ps = data.get("by_path_sha") or []
    if not isinstance(raw_ps, list):
        return {"ok": False, "error": "`by_path_sha` in pool bundle must be a list."}
    for idx, item in enumerate(raw_ps):
        if not isinstance(item, dict):
            rejected.append({"section": "by_path_sha", "reason": "non-object row"})
            continue
        rel = item.get("path")
        sha = item.get("sha256")
        entry = item.get("entry")
        if not isinstance(rel, str):
            rejected.append({"section": "by_path_sha", "reason": "missing path"})
            continue
        clean_e = _validate_pool_entry_row(rel, sha, entry)
        if clean_e is None:
            rejected.append({"section": "by_path_sha", "path": str(rel), "reason": "validation failed"})
            continue
        ps_rows.append((normalize_relpath(rel), str(sha), clean_e, now_ns + idx))

    raw_s = data.get("by_sha") or []
    if not isinstance(raw_s, list):
        return {"ok": False, "error": "`by_sha` in pool bundle must be a list."}
    for idx, item in enumerate(raw_s):
        if not isinstance(item, dict):
            rejected.append({"section": "by_sha", "reason": "non-object row"})
            continue
        sha = item.get("sha256")
        entry = item.get("entry")
        clean_e = _validate_pool_entry_row(None, sha, entry)
        if clean_e is None:
            rejected.append({"section": "by_sha", "sha256": str(sha)[:16], "reason": "validation failed"})
            continue
        s_rows.append((str(sha), clean_e, now_ns + idx))

    db_path = semantic_pool_path(root, git_info)
    legacy_json = _legacy_semantic_pool_json_path(root, git_info)
    if not dry_run and (ps_rows or s_rows):
        try:
            with contextlib.closing(_open_semantic_pool(db_path, legacy_json=legacy_json)) as conn:
                with conn:
                    if ps_rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO pool_by_path_sha (relpath, sha256, entry, last_used) VALUES (?, ?, ?, ?)",
                            ps_rows,
                        )
                    if s_rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO pool_by_sha (sha256, entry, last_used) VALUES (?, ?, ?)",
                            s_rows,
                        )
                    _evict_pool_excess(conn, max_entries)
        except (sqlite3.Error, OSError) as exc:
            return {"ok": False, "error": f"Cannot write to semantic pool `{db_path}`: {exc}"}

    return {
        "ok": True,
        "dry_run": dry_run,
        "pool_path": str(db_path),
        "input_path": str(in_path),
        "imported_by_path_sha": len(ps_rows),
        "imported_by_sha": len(s_rows),
        "rejected": len(rejected),
        "rejected_samples": rejected[:10],
    }
