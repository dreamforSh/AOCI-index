#!/usr/bin/env python3
"""
Dual-Host Lifecycle Hook Runner for AOCI Codebase Index (aoci_hook.py).

Supports both **Claude Code** (`settings.json` hooks) and **Antigravity**
(`hooks.json`) via `--host auto | claude-code | antigravity`:

  - SessionStart: When a workspace contains `.aoci/aoci.code.txt`, ensures
    `.git/info/exclude` covers `.aoci/`, runs a zero-read fast drift check,
    surfaces `#Global-Invariants:` from `aoci.txt`, injects startup context
    enforcing Read-Whole-First (including post-compaction refreshes in Claude
    Code), and snapshots the drift that ALREADY exists into
    `.aoci/.sessions/<session-id>.json`.
  - Stop: On normal turn completion, detects unsynced AOCI index drift
    (added/modified/renamed/deleted/malformed) or Git exclusion violations and
    asks the agent to synchronize `.aoci/` before stopping. Drift that was
    present at session start and is still byte-identical is NOT blamed on the
    agent (no wasted continuation turns for the user's own pending edits).
    Loop protection: `stop_hook_active` (Claude Code) / `continuationActive`
    (Antigravity); Antigravity stops with background work (`fullyIdle=false`)
    are never gated.

Conforms strictly to both hosts' stdin/stdout JSON contracts and always exits
with code 0 + valid JSON so hook failures never break an interactive session.
Failures are reported on stderr (and appended to `$AOCI_HOOK_LOG` if set).
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Import sibling module cleanly regardless of process CWD without cluttering __pycache__
sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import aoci_index  # noqa: E402

SESSION_TTL_S = 7 * 24 * 3600


def detect_host(host_arg: str, payload: Dict[str, Any]) -> str:
    if host_arg in ("antigravity", "claude-code"):
        return host_arg
    if any(k in payload for k in ("hook_event_name", "stop_hook_active", "session_id", "transcript_path")):
        return "claude-code"
    if any(k in payload for k in ("hookEventName", "workspacePaths", "continuationActive", "terminationReason")):
        return "antigravity"
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude-code"
    return "antigravity"


def session_id_of(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("session_id", "conversationId", "sessionId"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return re.sub(r"[^A-Za-z0-9_.-]", "_", val.strip())[:120]
    return None


def session_file(root: Path, sid: str) -> Path:
    return root / aoci_index.AOCI_DIR_NAME / aoci_index.SESSIONS_DIR / f"{sid}.json"


def _cleanup_old_sessions(sessions_dir: Path) -> None:
    now = time.time()
    try:
        for old in sessions_dir.glob("*.json"):
            try:
                if now - old.stat().st_mtime > SESSION_TTL_S:
                    old.unlink()
            except OSError:
                continue
    except OSError:
        pass


def write_session_snapshot(root: Path, sid: str, data: Dict[str, Any]) -> None:
    path = session_file(root, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    aoci_index.atomic_write_text(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def save_session_snapshot(root: Path, sid: str, drift: Dict[str, Any], overwrite: bool = False) -> None:
    """Record pre-existing drift for `sid` (SessionStart keeps existing; turn-start overwrites)."""
    path = session_file(root, sid)
    if path.exists() and not overwrite:
        return
    created_at: Optional[str] = None
    if overwrite and path.exists():
        existing = load_session_snapshot(root, sid)
        if existing:
            created_at = existing.get("created_at")
    malformed_texts = [m["text"] for m in drift.get("malformed_lines", [])]
    invalid_texts = [f"{it['path']}:{it['code']}:{it['message']}" for it in drift.get("invalid_entries", [])]
    data = {
        "created_at": created_at or aoci_index.utc_now_iso(),
        "signature": aoci_index.drift_signature(drift),
        "malformed": len(malformed_texts),
        "malformed_texts": malformed_texts,
        "invalid_texts": invalid_texts,
        "stop_retries": 0,
        "last_blocked_count": 0,
    }
    write_session_snapshot(root, sid, data)
    if not overwrite:
        _cleanup_old_sessions(path.parent)


def load_session_snapshot(root: Path, sid: Optional[str]) -> Optional[Dict[str, Any]]:
    if not sid:
        return None
    try:
        data = json.loads(session_file(root, sid).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("signature"), dict) else None
    except (OSError, ValueError):
        return None


def discover_indexed_roots(payload: Dict[str, Any]) -> List[Tuple[Path, Dict[str, Any]]]:
    """
    Find unique workspace or repository roots containing `.aoci/aoci.code.txt`.
    Checks `.aoci/aoci.code.txt` via pure-Python parent walks FIRST (stopping at `.git` boundaries)
    so workspaces without `.aoci/` never spawn a `git` subprocess.
    """
    raw_candidates: List[str] = []

    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        raw_candidates.append(cwd.strip())

    claude_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if claude_dir and claude_dir.strip():
        raw_candidates.append(claude_dir.strip())

    ws_paths = payload.get("workspacePaths")
    if isinstance(ws_paths, list):
        for item in ws_paths:
            if isinstance(item, str) and item.strip():
                raw_candidates.append(item.strip())

    seen_dirs: Set[Path] = set()
    found_roots: List[Path] = []

    for raw in raw_candidates:
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if not p.exists() or not p.is_dir():
            continue

        for cand in (p, *p.parents):
            if cand not in seen_dirs:
                seen_dirs.add(cand)
                if (cand / aoci_index.AOCI_DIR_NAME / aoci_index.CODE_FILE).is_file():
                    found_roots.append(cand)
            # Stop climbing once we reach the enclosing Git repo / worktree / submodule root
            if (cand / ".git").exists():
                break

    if not found_roots:
        return []

    return [(root, aoci_index.detect_git_info(root)) for root in found_roots]


def _quoted_script_path() -> str:
    p = (SCRIPT_DIR / "aoci_index.py").as_posix()
    return f'"{p}"' if " " in p else p


def handle_session_start(payload: Dict[str, Any], host: str) -> Dict[str, Any]:
    roots = discover_indexed_roots(payload)
    if not roots:
        return {}

    source = str(payload.get("source", "")).lower()
    header_suffix = " (post-compaction context refresh)" if source == "compact" else ""
    sid = session_id_of(payload)
    script_cli = _quoted_script_path()

    lines: List[str] = [
        f"[AOCI Index Active{header_suffix}] Detected local AOCI codebase index repository (`.aoci/`). Mandatory protocol:",
        f"1. Read-Whole-First: BEFORE searching, reading, or editing source files, read `.aoci/aoci.txt`, `.aoci/aoci.meta.txt`, and `.aoci/aoci.code.txt` (on >600-file repos, prefer `aoci_overview` / `aoci_context` / `aoci_query` MCP tools with `root` if available, or run `python {script_cli} overview --root <repo>` + `context` / `query`).",
        f"2. Sync After Code Updates: Whenever you add, edit, rename, or delete source files, update `.aoci/` via `aoci_upsert` / `aoci_ack` / `aoci_migrate_renames` MCP tools if available (or `python {script_cli} upsert` / `ack` / `migrate-renames`), and confirm zero drift via `aoci_verify` (or `python {script_cli} verify --root <repo>`) before finishing.",
        "3. Git Exclusion: `.aoci/` MUST remain excluded via `.git/info/exclude` and MUST NEVER be staged or committed.",
    ]

    for root, git_info in roots:
        aoci_dir = root / aoci_index.AOCI_DIR_NAME
        git_status = aoci_index.ensure_git_exclusion(root, git_info, check_status=False)
        drift = aoci_index.compute_drift(root, git_info, fast_mtime=True)
        invariants = aoci_index.parse_manifest_field(aoci_dir, "#Global-Invariants:")
        if sid:
            try:
                save_session_snapshot(root, sid, drift)
            except OSError:
                pass

        summary = (
            f"- Workspace `{root}`: indexed={drift['indexed_clean']}/{drift['total_eligible']} | "
            f"status={'CLEAN' if not drift['has_drift'] else 'DRIFT'}"
        )
        if drift["has_drift"]:
            restorable_cnt = aoci_index.count_restorable_from_pool(root, git_info, drift)
            restore_hint = (
                f"; {restorable_cnt} restorable with 0 tokens via `aoci_restore`/`restore`"
                if restorable_cnt > 0
                else ""
            )
            summary += (
                f" (+{len(drift['added'])} added, ~{len(drift['modified'])} modified, "
                f">{len(drift['renamed'])} renamed, -{len(drift['deleted'])} deleted; pre-existing, "
                f"so sync it when relevant to your task{restore_hint})"
            )
        if git_status["is_git"] and not git_status["ok"]:
            summary += " | WARNING: Git exclusion violation detected!"
            if git_status["tracked_files"]:
                summary += " Run `git rm -r --cached .aoci`!"
        lines.append(summary)

        if invariants and invariants != "-":
            lines.append(f"  Global-Invariants: {invariants}")

    message = "\n".join(lines)

    if host == "claude-code":
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": message,
            }
        }

    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": message,
                }
            }
        ]
    }


def format_sample(items: List[str], limit: int = 8) -> str:
    if not items:
        return ""
    head = ", ".join(items[:limit])
    if len(items) > limit:
        head += f", ... (+{len(items) - limit} more)"
    return head


def handle_turn_start(payload: Dict[str, Any], host: str) -> Dict[str, Any]:
    """
    Refresh the external-drift baseline at the start of a new user turn so manual user IDE edits
    between turns are never blamed on the agent at `Stop`:
      * Claude Code (`UserPromptSubmit`): fires once per user prompt -> refresh unconditionally.
      * Antigravity (`PreInvocation`): fires before each model step -> refresh only when
        `invocationNum <= 1` (never relying on a persisted flag that could linger after Ctrl+C).
    """
    if payload.get("continuationActive") is True or payload.get("stop_hook_active") is True:
        return {}
    if host == "antigravity" or "invocationNum" in payload:
        inv_num = payload.get("invocationNum")
        if inv_num is not None:
            try:
                if int(inv_num) > 1:
                    return {}
            except (TypeError, ValueError):
                pass
    sid = session_id_of(payload)
    if not sid:
        return {}
    roots = discover_indexed_roots(payload)
    for root, git_info in roots:
        drift = aoci_index.compute_drift(root, git_info, fast_mtime=True)
        try:
            save_session_snapshot(root, sid, drift, overwrite=True)
        except OSError:
            pass
    return {}


def handle_stop(payload: Dict[str, Any], host: str) -> Dict[str, Any]:
    # Antigravity: only gate normal model completion; never block cancels, errors, or budget stops
    term_reason = payload.get("terminationReason", "")
    if term_reason and term_reason != "NO_TOOL_CALL":
        return {}
    # Antigravity: background tasks still running -> the agent is legitimately waiting
    if payload.get("fullyIdle") is False:
        return {}

    # Loop guards for both hosts: inspect snapshot first so continuation only proceeds on retry #1
    is_continuation = payload.get("continuationActive") is True or payload.get("stop_hook_active") is True
    roots = discover_indexed_roots(payload)
    if not roots:
        return {}

    sid = session_id_of(payload)
    snapshots_by_root: Dict[Path, Optional[Dict[str, Any]]] = {}
    if sid:
        for root, _ in roots:
            snapshots_by_root[root] = load_session_snapshot(root, sid)

    if is_continuation:
        if not sid:
            return {}
        can_retry = False
        for snap in snapshots_by_root.values():
            if snap is not None:
                retries = int(snap.get("stop_retries", 0) or 0)
                last_cnt = int(snap.get("last_blocked_count", 0) or 0)
                if 1 <= retries < 2 and last_cnt > 0:
                    can_retry = True
                    break
        if not can_retry:
            return {}

    findings: List[str] = []
    ignored_total = 0
    total_unsynced = 0
    blocked_root_counts: Dict[Path, int] = {}
    for root, git_info in roots:
        git_status = aoci_index.check_git_exclusion(root, git_info, check_status=False)
        drift = aoci_index.compute_drift(root, git_info, fast_mtime=True)

        snapshot = snapshots_by_root.get(root) if sid else None
        snap_sig: Dict[str, Optional[str]] = snapshot["signature"] if snapshot else {}
        cur_sig = aoci_index.drift_signature(drift)

        def is_new(path: str) -> bool:
            if snapshot is None:
                return True
            return not (path in snap_sig and snap_sig[path] == cur_sig.get(path))

        added = [x["path"] for x in drift["added"] if is_new(x["path"])]
        modified = [x["path"] for x in drift["modified"] if is_new(x["path"])]
        missing = [x["path"] for x in drift["missing_in_code"] if is_new(x["path"])]
        deleted = [p for p in drift["deleted"] if is_new(p)]
        renamed = [f"{r['from']}->{r['to']}" for r in drift["renamed"] if is_new(r["to"]) or is_new(r["from"])]
        similar = [
            f"{sr['from']}~>{sr['to']}"
            for sr in drift.get("similar_renames", [])
            if is_new(sr["to"]) or is_new(sr["from"])
        ]
        total_drift = (
            len(drift["added"]) + len(drift["modified"]) + len(drift["missing_in_code"])
            + len(drift["deleted"]) + len(drift["renamed"])
        )
        ignored_total += total_drift - (len(added) + len(modified) + len(missing) + len(deleted) + len(renamed))
        malformed_texts_now = [m["text"] for m in drift["malformed_lines"]]
        if snapshot is None:
            malformed_new = len(malformed_texts_now)
        elif isinstance(snapshot.get("malformed_texts"), list):
            old_mal_counts = Counter(str(x) for x in snapshot["malformed_texts"])
            cur_mal_counts = Counter(malformed_texts_now)
            malformed_new = sum((cur_mal_counts - old_mal_counts).values())
        else:
            malformed_new = max(0, len(malformed_texts_now) - int(snapshot.get("malformed", 0)))

        invalid_texts_now = [f"{it['path']}:{it['code']}:{it['message']}" for it in drift.get("invalid_entries", [])]
        if snapshot is None:
            invalid_new = len(invalid_texts_now)
        elif isinstance(snapshot.get("invalid_texts"), list):
            old_inv_counts = Counter(str(x) for x in snapshot["invalid_texts"])
            cur_inv_counts = Counter(invalid_texts_now)
            invalid_new = sum((cur_inv_counts - old_inv_counts).values())
        else:
            invalid_new = len(invalid_texts_now)

        parts: List[str] = []
        if git_status["is_git"] and not git_status["ok"]:
            if not git_status["excluded"]:
                parts.append("`.aoci/` is not excluded in `.git/info/exclude` (run `ensure-git-exclude`)")
            if git_status["tracked_files"]:
                parts.append(
                    f"`.aoci/` has tracked Git files ({len(git_status['tracked_files'])}) — untrack via `git rm -r --cached .aoci`"
                )

        sim_new_objs = [
            sr
            for sr in drift.get("similar_renames", [])
            if is_new(sr["to"]) or is_new(sr["from"])
        ]
        sim_from_set = {sr["from"] for sr in sim_new_objs}
        sim_to_set = {sr["to"] for sr in sim_new_objs}
        display_added = [p for p in added if p not in sim_to_set]
        display_deleted = [p for p in deleted if p not in sim_from_set]

        if renamed:
            parts.append(f"renamed({len(renamed)}): {format_sample(renamed)}")
        if similar:
            parts.append(f"similar_moves({len(similar)}): {format_sample(similar)} (run `migrate-renames` then review)")
        if display_added:
            parts.append(f"added({len(display_added)}): {format_sample(display_added)}")
        if modified:
            parts.append(f"modified({len(modified)}): {format_sample(modified)}")
        if missing:
            parts.append(f"missing_in_code({len(missing)}): {format_sample(missing)}")
        if display_deleted:
            parts.append(f"deleted({len(display_deleted)}): {format_sample(display_deleted)}")
        if malformed_new:
            parts.append(f"malformed_lines({malformed_new})")
        if invalid_new:
            parts.append(f"invalid_entries({invalid_new})")

        root_unsynced = (
            len(renamed)
            + len(similar)
            + len(display_added)
            + len(modified)
            + len(missing)
            + len(display_deleted)
            + malformed_new
            + invalid_new
            + (1 if (git_status["is_git"] and not git_status["ok"]) else 0)
        )
        total_unsynced += root_unsynced

        if parts:
            findings.append(f"- `{root}`: " + "; ".join(parts))
            blocked_root_counts[root] = root_unsynced
        elif snapshot is not None and sid:
            # Turn is clean: retire any pre-existing exemptions that have since been resolved
            pruned_sig = {k: v for k, v in snap_sig.items() if k in cur_sig and cur_sig[k] == v}
            old_mal_list = snapshot.get("malformed_texts")
            if isinstance(old_mal_list, list):
                kept_mal = list((Counter(str(x) for x in old_mal_list) & Counter(malformed_texts_now)).elements())
            else:
                kept_mal = malformed_texts_now
            old_inv_list = snapshot.get("invalid_texts")
            if isinstance(old_inv_list, list):
                kept_inv = list((Counter(str(x) for x in old_inv_list) & Counter(invalid_texts_now)).elements())
            else:
                kept_inv = invalid_texts_now
            mal_changed = (not isinstance(old_mal_list, list)) or (
                Counter(kept_mal) != Counter(str(x) for x in old_mal_list)
            )
            inv_changed = (not isinstance(old_inv_list, list)) or (
                Counter(kept_inv) != Counter(str(x) for x in old_inv_list)
            )
            retries_dirty = int(snapshot.get("stop_retries", 0) or 0) != 0
            blocked_dirty = int(snapshot.get("last_blocked_count", 0) or 0) != 0
            if pruned_sig != snap_sig or mal_changed or inv_changed or retries_dirty or blocked_dirty:
                updated = {
                    "created_at": snapshot.get("created_at") or aoci_index.utc_now_iso(),
                    "signature": pruned_sig,
                    "malformed": len(kept_mal),
                    "malformed_texts": kept_mal,
                    "invalid_texts": kept_inv,
                    "stop_retries": 0,
                    "last_blocked_count": 0,
                }
                try:
                    write_session_snapshot(root, sid, updated)
                except OSError:
                    pass

    if not findings:
        return {}

    if is_continuation:
        prev_total_blocked = sum(
            int(s.get("last_blocked_count", 0) or 0)
            for s in snapshots_by_root.values()
            if s is not None
        )
        max_prev_retries = max(
            (int(s.get("stop_retries", 0) or 0) for s in snapshots_by_root.values() if s is not None),
            default=0,
        )
        if max_prev_retries >= 2 or not (0 < total_unsynced < prev_total_blocked):
            return {}

    if sid:
        for root, cur_cnt in blocked_root_counts.items():
            snap = snapshots_by_root.get(root)
            prev_retries = int(snap.get("stop_retries", 0) or 0) if (is_continuation and snap is not None) else 0
            if prev_retries >= 2:
                continue
            base_snap = dict(snap) if snap is not None else {
                "created_at": aoci_index.utc_now_iso(),
                "signature": {},
                "malformed": 0,
                "malformed_texts": [],
                "invalid_texts": [],
            }
            base_snap["stop_retries"] = prev_retries + 1
            base_snap["last_blocked_count"] = cur_cnt
            try:
                write_session_snapshot(root, sid, base_snap)
            except OSError:
                pass

    script_cli = _quoted_script_path()
    reason = (
        "[AOCI Index Drift Gate] Unsynced changes made during this session or Git-exclusion issues detected in `.aoci/`:\n"
        + "\n".join(findings)
        + "\nSync before finishing (prefer `aoci_*` MCP tools with `root` when available, otherwise CLI): "
        + f"`aoci_upsert` / `python {script_cli} upsert --root <repo> --entries-file .aoci/pending/<batch>.txt` for new/changed responsibilities, "
        + f"`aoci_ack` / `python {script_cli} ack --root <repo> <paths...>` when an existing entry is still accurate, "
        + "`aoci_migrate_renames` / `migrate-renames` for moves, `upsert --prune-only` for deletions; then confirm "
        + f"`aoci_verify` / `python {script_cli} verify --root <repo>` passes."
    )
    if ignored_total:
        reason += f" ({ignored_total} drifted path(s) that pre-date this session and are unchanged were not counted.)"

    decision = "block" if host == "claude-code" else "continue"
    return {
        "decision": decision,
        "reason": reason,
    }


def _log_failure() -> None:
    tb = traceback.format_exc()
    try:
        sys.stderr.write(f"[aoci_hook] internal error (hook returned {{}}):\n{tb}")
    except Exception:
        pass
    log_path = os.environ.get("AOCI_HOOK_LOG")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"--- {aoci_index.utc_now_iso()} ---\n{tb}\n")
        except OSError:
            pass


def main() -> int:
    aoci_index.configure_utf8_streams()
    parser = argparse.ArgumentParser(description="Dual-host lifecycle hook runner (Claude Code & Antigravity)")
    parser.add_argument(
        "--host",
        choices=("auto", "antigravity", "claude-code"),
        default="auto",
        help="Target host protocol (default: auto-detect from stdin JSON)",
    )
    parser.add_argument(
        "--event",
        choices=("auto", "session-start", "turn-start", "stop"),
        default="auto",
        help="Hook event type (default: auto-detect from stdin event field)",
    )
    args = parser.parse_args()

    try:
        raw_stdin = sys.stdin.read()
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
        if not isinstance(payload, dict):
            payload = {}

        host = detect_host(args.host, payload)
        event = args.event
        if event == "auto":
            ev_name = str(payload.get("hookEventName") or payload.get("hook_event_name") or "").lower()
            if "sessionstart" in ev_name or "session_start" in ev_name:
                event = "session-start"
            elif any(k in ev_name for k in ("userpromptsubmit", "user_prompt_submit", "preinvocation", "pre_invocation")):
                event = "turn-start"
            elif "stop" in ev_name:
                event = "stop"

        if event == "session-start":
            out = handle_session_start(payload, host)
        elif event == "turn-start":
            out = handle_turn_start(payload, host)
        elif event == "stop":
            out = handle_stop(payload, host)
        else:
            out = {}
    except Exception:
        # Never break the agent execution loop on unexpected hook failure, but leave a trace
        _log_failure()
        out = {}

    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
