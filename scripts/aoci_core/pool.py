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
    HUBS_FILE,
    MANIFEST_FILE,
    MAX_SEMANTIC_POOL_ENTRIES,
    META_FILE,
    MIN_RENAME_LINES,
    VALID_LAYERS,
    VALID_SIZES,
    atomic_write_text,
    normalize_relpath,
    utc_now_iso,
)
from .graph import _load_layer_rules_from_aoci, parse_glossary, parse_layer_rules, validate_parsed_entry_tags
from .templates import parse_declared_domains, parse_manifest_field
from . import hubs as _hubs

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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pool_hubs_by_symbol_sha ("
            "relpath TEXT NOT NULL, symbol TEXT NOT NULL, span_sha256 TEXT NOT NULL, "
            "f_val TEXT NOT NULL, s_val TEXT NOT NULL, last_used INTEGER NOT NULL, "
            "PRIMARY KEY (relpath, symbol, span_sha256))"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS pool_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_sha ON pool_by_path_sha(sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_last_used ON pool_by_path_sha(last_used)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_s_last_used ON pool_by_sha(last_used)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hubs_last_used ON pool_hubs_by_symbol_sha(last_used)")

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
    for tbl in ("pool_by_path_sha", "pool_by_sha", "pool_hubs_by_symbol_sha"):
        try:
            cnt = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.Error:
            continue
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
    hub_cnt = 0
    if exists:
        try:
            with contextlib.closing(_open_semantic_pool(db_path, readonly=True)) as conn:
                ps_cnt = int(conn.execute("SELECT COUNT(*) FROM pool_by_path_sha").fetchone()[0])
                s_cnt = int(conn.execute("SELECT COUNT(*) FROM pool_by_sha").fetchone()[0])
                try:
                    hub_cnt = int(conn.execute("SELECT COUNT(*) FROM pool_hubs_by_symbol_sha").fetchone()[0])
                except sqlite3.Error:
                    hub_cnt = 0
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
        "by_hub_symbol_sha_count": hub_cnt,
        "max_entries": max_entries,
    }


def record_hub_pool_entries(
    root: Path,
    git_info: Dict[str, Any],
    rows: Sequence[Tuple[str, str, str, str, str]],
    max_entries: int = MAX_SEMANTIC_POOL_ENTRIES,
) -> None:
    """Best-effort persist `(relpath, symbol, span_sha256, f_val, s_val)` into `pool_hubs_by_symbol_sha`."""
    if not rows:
        return
    now_ns = time.time_ns()
    db_rows: List[Tuple[str, str, str, str, str, int]] = []
    for idx, (rel, sym, sha, f_val, s_val) in enumerate(rows):
        norm_rel, path_err = _hubs.validate_hub_relpath(rel)
        if path_err is not None or not norm_rel:
            continue
        sym_s = str(sym).strip()
        f_s = str(f_val).strip()
        s_s = str(s_val).strip()
        if (
            not sym_s
            or not isinstance(sha, str)
            or not _SHA256_HEX_RE.match(sha)
            or not f_s
            or f_s == "-"
            or not s_s
            or "<todo" in f_s.lower()
            or "<todo" in s_s.lower()
        ):
            continue
        db_rows.append((norm_rel, sym_s, sha, f_s, s_s, now_ns + idx))
    if not db_rows:
        return
    db_path = semantic_pool_path(root, git_info)
    legacy_json = _legacy_semantic_pool_json_path(root, git_info)
    try:
        with contextlib.closing(_open_semantic_pool(db_path, legacy_json=legacy_json)) as conn:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO pool_hubs_by_symbol_sha "
                    "(relpath, symbol, span_sha256, f_val, s_val, last_used) VALUES (?, ?, ?, ?, ?, ?)",
                    db_rows,
                )
                _evict_pool_excess(conn, max_entries)
    except (sqlite3.Error, OSError):
        pass


def list_pooled_hub_relpaths(root: Path, git_info: Dict[str, Any]) -> set:
    """Return distinct `relpath` values stored in `pool_hubs_by_symbol_sha` (read-only)."""
    db_path = semantic_pool_path(root, git_info)
    if not db_path.is_file():
        return set()
    try:
        with contextlib.closing(_open_semantic_pool(db_path, readonly=True)) as conn:
            return {
                normalize_relpath(str(r[0]))
                for r in conn.execute("SELECT DISTINCT relpath FROM pool_hubs_by_symbol_sha")
                if r and r[0]
            }
    except (sqlite3.Error, OSError):
        return set()


def lookup_hub_pool_entries(
    root: Path,
    git_info: Dict[str, Any],
    queries: Sequence[Tuple[str, str, str]],
    *,
    touch_lru: bool = False,
) -> Dict[Tuple[str, str, str], Tuple[str, str]]:
    """Batch-lookup `(relpath, symbol, span_sha256) -> (f_val, s_val)` from `pool_hubs_by_symbol_sha`."""
    hits: Dict[Tuple[str, str, str], Tuple[str, str]] = {}
    if not queries:
        return hits
    db_path = semantic_pool_path(root, git_info)
    if not db_path.is_file():
        return hits
    want = sorted({(normalize_relpath(r), str(s).strip(), str(h)) for r, s, h in queries if r and s and h})
    if not want:
        return hits
    triple_chunk = 150
    try:
        with contextlib.closing(_open_semantic_pool(db_path, readonly=not touch_lru)) as conn:
            row_val_ok = True
            for i in range(0, len(want), triple_chunk):
                sub = want[i : i + triple_chunk]
                ph = ",".join("(?,?,?)" for _ in sub)
                params = [v for tup in sub for v in tup]
                try:
                    for rel, sym, sha, fv, sv in conn.execute(
                        f"SELECT relpath, symbol, span_sha256, f_val, s_val FROM pool_hubs_by_symbol_sha "
                        f"WHERE (relpath, symbol, span_sha256) IN ({ph})",
                        params,
                    ):
                        hits[(rel, sym, sha)] = (fv, sv)
                except sqlite3.OperationalError:
                    row_val_ok = False
                    break
            if not row_val_ok:
                want_set = set(want)
                shas = sorted({sha for _, _, sha in want})
                for i in range(0, len(shas), 300):
                    sc = shas[i : i + 300]
                    ph = ",".join("?" for _ in sc)
                    try:
                        for rel, sym, sha, fv, sv in conn.execute(
                            f"SELECT relpath, symbol, span_sha256, f_val, s_val FROM pool_hubs_by_symbol_sha "
                            f"WHERE span_sha256 IN ({ph})",
                            sc,
                        ):
                            if (rel, sym, sha) in want_set:
                                hits[(rel, sym, sha)] = (fv, sv)
                    except sqlite3.OperationalError:
                        break
            if touch_lru and hits:
                now_ns = time.time_ns()
                with conn:
                    conn.executemany(
                        "UPDATE pool_hubs_by_symbol_sha SET last_used = ? "
                        "WHERE relpath = ? AND symbol = ? AND span_sha256 = ?",
                        [(now_ns, r, s, h) for r, s, h in hits],
                    )
    except (sqlite3.Error, OSError):
        pass
    return hits


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


_META_MAX_TEXT = 2000
_META_PLACEHOLDERS = ("", "-")


def _meta_text(value: Any, limit: int = _META_MAX_TEXT) -> str:
    """Single-line, length-capped text safe to write into a `#Directive:` line."""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def _layer_rule_specs(aoci_dir: Path) -> List[str]:
    specs: List[str] = []
    for fname in (MANIFEST_FILE, META_FILE):
        try:
            lines = (aoci_dir / fname).read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        specs.extend(ln[len("#Layer-Rules:") :].strip() for ln in lines if ln.startswith("#Layer-Rules:"))
    return [s for s in specs if s]


def collect_bundle_meta(aoci_dir: Path) -> Dict[str, Any]:
    """
    Index-level context worth shipping with a pool bundle so a zero-token bootstrap is fully usable:
    `#Global-Invariants:`, `#Project:`, declared Domain codes, `#Glossary:` groups, and `#Layer-Rules:`.
    """
    meta: Dict[str, Any] = {}
    invariants = _meta_text(parse_manifest_field(aoci_dir, "#Global-Invariants:"))
    if invariants not in _META_PLACEHOLDERS and "<todo" not in invariants.lower():
        meta["global_invariants"] = invariants
    project = _meta_text(parse_manifest_field(aoci_dir, "#Project:"), 200)
    if project:
        meta["project"] = project
    domains = parse_declared_domains(aoci_dir)
    if domains:
        meta["domains"] = dict(sorted(domains.items()))
    glossary = parse_glossary(aoci_dir)
    if glossary:
        meta["glossary"] = [list(g) for g in glossary]
    rules = _layer_rule_specs(aoci_dir)
    if rules:
        meta["layer_rules"] = "; ".join(rules)
    return meta


def apply_bundle_meta(aoci_dir: Path, meta: Any, dry_run: bool = False) -> Dict[str, Any]:
    """
    Fill gaps in `.aoci/aoci.txt` / `aoci.meta.txt` from a bundle's `meta` without overriding local choices:
    undeclared Domain codes -> `#Domains:`, unseen glossary groups -> `#Glossary:`, `#Layer-Rules:` only when
    none exist, and `#Global-Invariants:` only while it is still the `-` placeholder. Bundle text is
    sanitized to single directive lines. Returns what was (or, with `dry_run`, would be) applied.
    """
    if not isinstance(meta, dict):
        return {}
    manifest_p = aoci_dir / MANIFEST_FILE
    meta_p = aoci_dir / META_FILE
    if not manifest_p.is_file() or not meta_p.is_file():
        return {"skipped": "index not initialized"}

    applied: Dict[str, Any] = {}
    meta_lines: List[str] = []

    declared = parse_declared_domains(aoci_dir)
    new_domains: List[str] = []
    meta_domains = meta.get("domains")
    raw_domains: Dict[str, Any] = meta_domains if isinstance(meta_domains, dict) else {}
    for code, label in sorted(raw_domains.items()):
        if not (isinstance(code, str) and re.fullmatch(r"[A-Z0-9]", code)) or code in declared:
            continue
        clean = re.sub(r"[^\w.-]+", "-", _meta_text(label, 40)).strip("-") or "domain"
        new_domains.append(f"{code}={clean}")
    if new_domains:
        meta_lines.append("#Domains: " + ", ".join(new_domains))
        applied["domains"] = [d.split("=", 1)[0] for d in new_domains]

    seen = {tuple(sorted(t.lower() for t in grp)) for grp in parse_glossary(aoci_dir)}
    new_groups: List[str] = []
    for grp in meta.get("glossary") or []:
        if not isinstance(grp, list):
            continue
        terms = [re.sub(r"[;；=＝]+", " ", _meta_text(t, 60)).strip() for t in grp if isinstance(t, str)]
        terms = [t for t in dict.fromkeys(terms) if t]
        sig = tuple(sorted(t.lower() for t in terms))
        if len(terms) >= 2 and sig not in seen:
            seen.add(sig)
            new_groups.append(" = ".join(terms))
    if new_groups:
        meta_lines.append("#Glossary: " + "; ".join(new_groups))
        applied["glossary_groups"] = len(new_groups)

    rules_spec = _meta_text(meta.get("layer_rules", ""))
    if rules_spec and parse_layer_rules(rules_spec) and not _load_layer_rules_from_aoci(aoci_dir):
        meta_lines.append(f"#Layer-Rules: {rules_spec}")
        applied["layer_rules"] = True

    new_manifest: Optional[str] = None
    invariants = _meta_text(meta.get("global_invariants", ""))
    current_inv = _meta_text(parse_manifest_field(aoci_dir, "#Global-Invariants:"))
    if invariants and invariants not in _META_PLACEHOLDERS and current_inv in _META_PLACEHOLDERS:
        text = manifest_p.read_text(encoding="utf-8-sig", errors="replace")
        lines = text.splitlines()
        for idx, ln in enumerate(lines):
            if ln.startswith("#Global-Invariants:"):
                lines[idx] = f"#Global-Invariants: {invariants}"
                new_manifest = "\n".join(lines) + "\n"
                applied["global_invariants"] = True
                break

    if dry_run or not (meta_lines or new_manifest):
        return applied
    from .lock import IndexLock

    with IndexLock(aoci_dir):
        if meta_lines:
            body = meta_p.read_text(encoding="utf-8-sig", errors="replace")
            if body and not body.endswith("\n"):
                body += "\n"
            atomic_write_text(meta_p, body + "\n".join(meta_lines) + "\n")
        if new_manifest is not None:
            atomic_write_text(manifest_p, new_manifest)
    return applied


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
    hub_valid: Dict[Tuple[str, str, str], Tuple[str, str]] = {}

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

    db_path = semantic_pool_path(root, git_info)
    if db_path.is_file():
        try:
            with contextlib.closing(_open_semantic_pool(db_path, readonly=True)) as conn:
                for rel_h, sym_h, sha_h, f_h, s_h in conn.execute(
                    "SELECT relpath, symbol, span_sha256, f_val, s_val FROM pool_hubs_by_symbol_sha ORDER BY last_used ASC"
                ):
                    norm_r, p_err = _hubs.validate_hub_relpath(str(rel_h))
                    if (
                        p_err is None
                        and norm_r
                        and str(sym_h).strip()
                        and _SHA256_HEX_RE.match(str(sha_h))
                        and str(f_h).strip()
                        and str(f_h).strip() != "-"
                        and str(s_h).strip()
                    ):
                        hub_valid[(norm_r, str(sym_h).strip(), str(sha_h))] = (str(f_h).strip(), str(s_h).strip())
        except (sqlite3.Error, OSError):
            pass

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
        hubs_path = aoci_dir / HUBS_FILE
        if hubs_path.is_file():
            h_recs, _ = _hubs.parse_hubs_volume(hubs_path)
            for rel_h, sym_h, sha_h, f_h, s_h in _hubs.collect_hub_pool_rows(root, h_recs):
                hub_valid[(rel_h, sym_h, sha_h)] = (f_h, s_h)

    ps_list = [{"path": rel, "sha256": sha, "entry": line} for (rel, sha), line in sorted(ps_valid.items())]
    s_list = [{"sha256": sha, "entry": line} for sha, line in sorted(s_valid.items())]
    hub_list = [
        {"path": rel, "symbol": sym, "span_sha256": sha, "f": fv, "s": sv}
        for (rel, sym, sha), (fv, sv) in sorted(hub_valid.items())
    ]
    bundle: Dict[str, Any] = {
        "schema_version": 1,
        "format": "aoci-semantic-pool-v1",
        "exported_at": utc_now_iso(),
    }
    bundle_meta = collect_bundle_meta(root / AOCI_DIR_NAME) if include_current else {}
    if bundle_meta:
        bundle["meta"] = bundle_meta
    bundle.update({"by_path_sha": ps_list, "by_sha": s_list, "hubs_by_symbol_sha": hub_list})
    raw_json = (json.dumps(bundle, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    use_gz = out_path.name.lower().endswith(".gz")
    payload_bytes = gzip.compress(raw_json, mtime=0) if use_gz else raw_json

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
        "exported_hubs": len(hub_list),
        "exported_meta": sorted(bundle_meta),
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
        or ("by_path_sha" not in data and "by_sha" not in data and "hubs_by_symbol_sha" not in data)
    ):
        return {"ok": False, "error": f"Unrecognized pool bundle schema in `{in_path}`."}

    now_ns = time.time_ns()
    ps_rows: List[Tuple[str, str, str, int]] = []
    s_rows: List[Tuple[str, str, int]] = []
    hub_rows: List[Tuple[str, str, str, str, str, int]] = []
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

    raw_hubs = data.get("hubs_by_symbol_sha") or []
    if not isinstance(raw_hubs, list):
        return {"ok": False, "error": "`hubs_by_symbol_sha` in pool bundle must be a list."}
    for idx, item in enumerate(raw_hubs):
        if not isinstance(item, dict):
            rejected.append({"section": "hubs_by_symbol_sha", "reason": "non-object row"})
            continue
        rel_h = item.get("path")
        sym_h = item.get("symbol")
        sha_h = item.get("span_sha256")
        f_h = item.get("f")
        s_h = item.get("s")
        norm_r, p_err = _hubs.validate_hub_relpath(str(rel_h or ""))
        if (
            not isinstance(rel_h, str)
            or p_err is not None
            or not norm_r
            or not isinstance(sym_h, str)
            or not sym_h.strip()
            or not isinstance(sha_h, str)
            or not _SHA256_HEX_RE.match(sha_h)
            or not isinstance(f_h, str)
            or not f_h.strip()
            or f_h.strip() == "-"
            or not isinstance(s_h, str)
            or not s_h.strip()
            or "<todo" in f_h.lower()
            or "<todo" in s_h.lower()
        ):
            rejected.append({"section": "hubs_by_symbol_sha", "path": str(rel_h), "reason": "validation failed"})
            continue
        hub_rows.append((norm_r, sym_h.strip(), sha_h, f_h.strip(), s_h.strip(), now_ns + idx))

    db_path = semantic_pool_path(root, git_info)
    legacy_json = _legacy_semantic_pool_json_path(root, git_info)
    if not dry_run and (ps_rows or s_rows or hub_rows):
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
                    if hub_rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO pool_hubs_by_symbol_sha "
                            "(relpath, symbol, span_sha256, f_val, s_val, last_used) VALUES (?, ?, ?, ?, ?, ?)",
                            hub_rows,
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
        "imported_hubs": len(hub_rows),
        "rejected": len(rejected),
        "rejected_samples": rejected[:10],
        "meta_applied": apply_bundle_meta(root / AOCI_DIR_NAME, data.get("meta"), dry_run=dry_run),
    }
