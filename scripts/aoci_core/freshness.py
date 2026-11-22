"""
Index freshness for read results: flag returned FRAS entries whose source changed since it was indexed.

Read paths (`query`, `pack`, `context`, MCP) call `check_freshness` on the handful of paths they return.
An unchanged non-racy `(mtime_ns, size)` against `baseline.json` costs one `stat`; anything else is confirmed
with an LF-normalized SHA-256, so `touch` alone never produces a false "modified".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .constants import AOCI_DIR_NAME, BASELINE_FILE

STALE_REASONS = ("modified", "missing", "unbaselined")
MAX_FRESHNESS_CHECKS = 200

_REASON_TEXT = {
    "modified": "source changed since this entry was indexed",
    "missing": "source file no longer exists",
    "unbaselined": "entry has no verified baseline fingerprint",
}


def load_baseline_files(root: Path) -> Dict[str, Dict[str, Any]]:
    """Read `baseline.json` `files` without importing the drift machinery (empty dict on any error)."""
    try:
        data = json.loads((root / AOCI_DIR_NAME / BASELINE_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def _sha256_lf(path: Path) -> Optional[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    norm = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    return hashlib.sha256(norm).hexdigest()


def check_freshness(
    root: Path,
    rels: Sequence[str],
    baseline_files: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    limit: int = MAX_FRESHNESS_CHECKS,
) -> Dict[str, str]:
    """
    Return `{relpath: reason}` for stale paths among the first `limit` unique `rels` (fresh paths omitted).
    Reasons: `modified` (content differs from the baseline SHA-256), `missing`, `unbaselined`.
    """
    files = baseline_files if baseline_files is not None else load_baseline_files(root)
    stale: Dict[str, str] = {}
    for rel in list(dict.fromkeys(rels))[: max(0, int(limit))]:
        segs = rel.replace("\\", "/").split("/")
        if not rel or rel.startswith(("/", "\\")) or ":" in segs[0] or ".." in segs:
            stale[rel] = "missing"  # never stat/read outside the repository for a malformed entry path
            continue
        full = root / rel
        try:
            st = os.stat(full)
        except OSError:
            stale[rel] = "missing"
            continue
        rec = files.get(rel)
        if not isinstance(rec, Mapping) or not rec.get("sha256"):
            stale[rel] = "unbaselined"
            continue
        if not rec.get("racy") and rec.get("mtime_ns") == st.st_mtime_ns and rec.get("bytes") == st.st_size:
            continue
        cur = _sha256_lf(full)
        if cur is None:
            stale[rel] = "missing"
        elif cur != rec.get("sha256"):
            stale[rel] = "modified"
    return stale


def stale_list(stale: Mapping[str, str]) -> List[Dict[str, str]]:
    """JSON-friendly `[{path, reason, note}]`, sorted by path."""
    return [{"path": p, "reason": r, "note": _REASON_TEXT.get(r, r)} for p, r in sorted(stale.items())]


def stale_note(reason: str) -> str:
    return _REASON_TEXT.get(reason, reason)


def _entry_path(line: str) -> str:
    return line.split("[", 1)[0].strip().replace("\\", "/")


def annotate_payload_freshness(
    root: Path,
    payload: Dict[str, Any],
    line_keys: Sequence[str] = ("targets", "dependencies", "callers"),
    baseline_files: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    """Attach `stale_entries` to a payload whose `line_keys` hold raw FRAS lines; returns the stale map."""
    rels: List[str] = []
    for key in line_keys:
        for line in payload.get(key) or ():
            if isinstance(line, str) and line:
                rels.append(_entry_path(line))
    stale = check_freshness(root, rels, baseline_files)
    if stale:
        payload["stale_entries"] = stale_list(stale)
    return stale


def format_stale_lines(stale_entries: Sequence[Mapping[str, str]]) -> List[str]:
    """Text-mode section warning agents to re-read stale sources before trusting their entries."""
    if not stale_entries:
        return []
    out = [f"\n## Stale Entries ({len(stale_entries)}) - re-read these sources before trusting their entries"]
    out.extend(f"  ! {item['path']}: {item['note']}" for item in stale_entries)
    return out
