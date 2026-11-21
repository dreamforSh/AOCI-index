"""Line-balanced, stable 1-indexed batch planning for `.aoci/.cache/batch_plan.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import BATCH_PLAN_CACHE, CACHE_DIR, DEFAULT_MAX_BATCH_LINES, atomic_write_text


def _build_pending_items(drift: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Build directory-coherent pending items, excluding similar-rename targets and exact renames."""
    sim_to_set = {sr["to"] for sr in drift.get("similar_renames", ())}
    sim_from_set = {sr["from"] for sr in drift.get("similar_renames", ())}
    added_non_sim = [it for it in drift["added"] if it["path"] not in sim_to_set]
    deleted_non_sim = [p for p in drift["deleted"] if p not in sim_from_set]
    pending = added_non_sim + drift["modified"] + drift["missing_in_code"]
    pending.sort(key=lambda it: (it["path"].rsplit("/", 1)[0] if "/" in it["path"] else "", it["path"]))
    return pending, added_non_sim, deleted_non_sim


def make_batches(
    items: Sequence[Dict[str, Any]],
    max_files: int,
    max_lines: int = DEFAULT_MAX_BATCH_LINES,
) -> List[List[Dict[str, Any]]]:
    """Pack pending files into directory-coherent batches bounded by both file count and line budget."""
    if max_files <= 0:
        max_files = 20
    if max_lines <= 0:
        max_lines = DEFAULT_MAX_BATCH_LINES

    batches: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_lines = 0

    for item in items:
        item_lines = int(item.get("lines", 0))
        if cur and (len(cur) >= max_files or (cur_lines + item_lines > max_lines)):
            batches.append(cur)
            cur = []
            cur_lines = 0
        cur.append(item)
        cur_lines += item_lines

    if cur:
        batches.append(cur)
    return batches


def _load_batch_plan(aoci_dir: Path) -> Optional[Dict[str, Any]]:
    plan_path = aoci_dir / CACHE_DIR / BATCH_PLAN_CACHE
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("batches"), list)
        or int(data.get("max_files", 0) or 0) <= 0
        or int(data.get("max_lines", 0) or 0) <= 0
    ):
        return None
    return data


def _save_batch_plan(aoci_dir: Path, max_files: int, max_lines: int, raw_batches: Sequence[Sequence[str]]) -> None:
    if not aoci_dir.is_dir():
        return
    plan_path = aoci_dir / CACHE_DIR / BATCH_PLAN_CACHE
    try:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            plan_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "max_files": max_files,
                    "max_lines": max_lines,
                    "batches": [list(b) for b in raw_batches],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    except OSError:
        pass


def _clear_batch_plan(aoci_dir: Path) -> None:
    try:
        (aoci_dir / CACHE_DIR / BATCH_PLAN_CACHE).unlink()
    except OSError:
        pass


def resolve_stable_batches(
    aoci_dir: Path,
    pending: Sequence[Dict[str, Any]],
    max_files: Optional[int],
    max_lines: Optional[int],
    *,
    persist: bool = True,
    reset: bool = False,
) -> Tuple[List[List[Dict[str, Any]]], Dict[str, Any]]:
    """
    Maintain stable 1-indexed batch slots in `.aoci/.cache/batch_plan.json` so indexing earlier
    batches does not renumber later batches. Completed slots stay empty; newly added files append
    as trailing batches; `--reset-batches` or changing batch limits rebuilds cleanly.
    """
    saved = None if reset else _load_batch_plan(aoci_dir)
    req_files = int(max_files) if (max_files is not None and int(max_files) > 0) else None
    req_lines = int(max_lines) if (max_lines is not None and int(max_lines) > 0) else None

    if saved is not None:
        eff_files = req_files if req_files is not None else int(saved["max_files"])
        eff_lines = req_lines if req_lines is not None else int(saved["max_lines"])
    else:
        eff_files = req_files if req_files is not None else 20
        eff_lines = req_lines if req_lines is not None else DEFAULT_MAX_BATCH_LINES

    pending_by_path = {it["path"]: it for it in pending}
    if not pending:
        if persist:
            _clear_batch_plan(aoci_dir)
        return [], {
            "max_files": eff_files,
            "max_lines": eff_lines,
            "total_batches": 0,
            "completed_batches": [],
            "remaining_batches": [],
            "reused": False,
        }

    if (
        saved is not None
        and int(saved["max_files"]) == eff_files
        and int(saved["max_lines"]) == eff_lines
    ):
        saved_batches = [list(b) for b in saved["batches"] if isinstance(b, list)]
        scheduled_paths = {p for b in saved_batches for p in b}
        if scheduled_paths & set(pending_by_path.keys()):
            batches = [[pending_by_path[p] for p in b if p in pending_by_path] for b in saved_batches]
            unscheduled = [it for it in pending if it["path"] not in scheduled_paths]
            extra_batches: List[List[Dict[str, Any]]] = []
            if unscheduled:
                extra_batches = make_batches(unscheduled, eff_files, eff_lines)
                batches.extend(extra_batches)
                saved_batches.extend([[it["path"] for it in eb] for eb in extra_batches])
            if persist and (extra_batches or reset):
                _save_batch_plan(aoci_dir, eff_files, eff_lines, saved_batches)
            completed = [idx for idx, b in enumerate(batches, start=1) if not b]
            remaining = [idx for idx, b in enumerate(batches, start=1) if b]
            return batches, {
                "max_files": eff_files,
                "max_lines": eff_lines,
                "total_batches": len(batches),
                "completed_batches": completed,
                "remaining_batches": remaining,
                "reused": True,
            }

    batches = make_batches(pending, eff_files, eff_lines)
    if persist:
        _save_batch_plan(aoci_dir, eff_files, eff_lines, [[it["path"] for it in b] for b in batches])
    return batches, {
        "max_files": eff_files,
        "max_lines": eff_lines,
        "total_batches": len(batches),
        "completed_batches": [],
        "remaining_batches": list(range(1, len(batches) + 1)),
        "reused": False,
    }
