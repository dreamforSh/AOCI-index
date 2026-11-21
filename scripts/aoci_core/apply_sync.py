"""One-shot drift synchronization workflow orchestrating restore, rename migration, ack, upsert, and verify."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .constants import AOCI_DIR_NAME


def _parse_step_json(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        pass
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj, _ = json.JSONDecoder().raw_decode(stripped)
            return obj
        except ValueError:
            pass
    return {"raw": text}


def run_apply_sync(
    root: Path,
    run_subcommand_fn: Callable[[List[str]], Tuple[int, str]],
    *,
    restore: bool = False,
    allow_sha_fallback: bool = False,
    migrate_renames: bool = False,
    exact_only: bool = False,
    no_rewrite_refs: bool = False,
    ack_paths: Optional[Sequence[str]] = None,
    ack_impl_only: bool = False,
    reviewed: bool = False,
    entries: Optional[Sequence[str]] = None,
    entries_file: Optional[str] = None,
    pending_dir: Optional[str] = None,
    clean_pending: bool = False,
    prune_only: bool = False,
    no_prune: bool = False,
    strict_size: bool = False,
    drop_malformed: bool = False,
    verify: bool = True,
    full_verify: bool = False,
    max_bytes: Optional[int] = None,
    dry_run: bool = False,
    lock_cls: Any = None,
) -> Dict[str, Any]:
    """
    Execute requested sync stages in canonical order (`restore -> migrate-renames -> ack -> upsert -> verify`).
    Halts immediately on the first failing step (`rc != 0`) and returns per-step structured outcomes.
    """
    ack_list = list(dict.fromkeys(str(p) for p in (ack_paths or ()) if str(p).strip()))
    entry_list = [str(e) for e in (entries or ()) if str(e).strip()]
    want_ack = bool(ack_impl_only or ack_list)
    want_upsert = bool(entry_list or entries_file or pending_dir or prune_only)

    if want_ack and not dry_run and not reviewed:
        err_msg = "Refusing `ack` in apply-sync without `reviewed=True` / `--reviewed` (pass `--dry-run` to preview)."
        return {
            "ok": False,
            "dry_run": False,
            "root": str(root),
            "steps": [
                {
                    "step": "ack",
                    "ok": False,
                    "rc": 2,
                    "result": {"ok": False, "error": err_msg},
                }
            ],
            "failed_step": "ack",
            "verify": None,
        }

    def _with_max_bytes(cmd: List[str]) -> List[str]:
        if max_bytes is not None:
            cmd.extend(["--max-bytes", str(int(max_bytes))])
        return cmd

    plan: List[Tuple[str, List[str]]] = []

    if restore:
        cmd_r = _with_max_bytes(["restore", "--root", str(root), "--json"])
        # Keep deleted entries during `restore` only when `no_prune`, `migrate_renames` (which needs
        # deleted origin entries), or a subsequent pruning `upsert` will handle them.
        if no_prune or migrate_renames or want_upsert:
            cmd_r.append("--no-prune")
        if allow_sha_fallback:
            cmd_r.append("--allow-sha-fallback")
        if dry_run:
            cmd_r.append("--dry-run")
        plan.append(("restore", cmd_r))

    if migrate_renames:
        cmd_m = _with_max_bytes(["migrate-renames", "--root", str(root), "--json"])
        if exact_only:
            cmd_m.append("--exact-only")
        if no_rewrite_refs:
            cmd_m.append("--no-rewrite-refs")
        if drop_malformed:
            cmd_m.append("--drop-malformed")
        if dry_run:
            cmd_m.append("--dry-run")
        plan.append(("migrate_renames", cmd_m))

    # Split `ack_impl_only` auto-selection from explicit `ack_paths` so passing both neither
    # suppresses `impl_only` discovery nor rejects reviewed contract-changed paths in `ack_paths`.
    if ack_impl_only:
        cmd_ai = _with_max_bytes(["ack", "--root", str(root), "--impl-only", "--json"])
        if reviewed:
            cmd_ai.append("--reviewed")
        if dry_run:
            cmd_ai.append("--dry-run")
        plan.append(("ack_impl_only" if ack_list else "ack", cmd_ai))

    if ack_list:
        cmd_ap = _with_max_bytes(["ack", "--root", str(root), "--json"])
        if reviewed:
            cmd_ap.append("--reviewed")
        if dry_run:
            cmd_ap.append("--dry-run")
        cmd_ap.extend(["--", *ack_list])
        plan.append(("ack", cmd_ap))

    need_trailing_prune = bool((restore and migrate_renames) and not want_upsert and not no_prune)
    if want_upsert or need_trailing_prune:
        cmd_u = _with_max_bytes(["upsert", "--root", str(root), "--json"])
        if entries_file:
            cmd_u.append(f"--entries-file={entries_file}")
        if pending_dir:
            cmd_u.append(f"--pending-dir={pending_dir}")
        if clean_pending:
            cmd_u.append("--clean-pending")
        if prune_only or (need_trailing_prune and not want_upsert):
            cmd_u.append("--prune-only")
        elif no_prune:
            cmd_u.append("--no-prune")
        if strict_size:
            cmd_u.append("--strict-size")
        if drop_malformed:
            cmd_u.append("--drop-malformed")
        if dry_run:
            cmd_u.append("--dry-run")
        for ln in entry_list:
            cmd_u.append(f"--entry={ln}")
        plan.append(("upsert", cmd_u))

    if verify and not dry_run:
        cmd_v = _with_max_bytes(["verify", "--root", str(root), "--json"])
        if not full_verify:
            cmd_v.append("--fast")
        plan.append(("verify", cmd_v))

    steps_out: List[Dict[str, Any]] = []
    failed_step: Optional[str] = None
    verify_result: Any = None

    has_mutating_step = any(s != "verify" for s, _ in plan)
    aoci_dir = root / AOCI_DIR_NAME
    lock_ctx: Any = (
        lock_cls(aoci_dir)
        if (lock_cls is not None and not dry_run and has_mutating_step and aoci_dir.is_dir())
        else contextlib.nullcontext()
    )

    try:
        with lock_ctx:
            for step_name, argv in plan:
                rc, raw_out = run_subcommand_fn(argv)
                parsed = _parse_step_json(raw_out)
                step_ok = rc == 0
                steps_out.append(
                    {
                        "step": step_name,
                        "ok": step_ok,
                        "rc": int(rc),
                        "result": parsed,
                    }
                )
                if step_name == "verify":
                    verify_result = parsed
                if not step_ok:
                    failed_step = step_name
                    break
    except TimeoutError as exc:
        first_mut = next((s for s, _ in plan if s != "verify"), plan[0][0] if plan else "lock")
        steps_out.append(
            {
                "step": first_mut,
                "ok": False,
                "rc": 3,
                "result": {"ok": False, "error": str(exc)},
            }
        )
        failed_step = first_mut

    return {
        "ok": failed_step is None,
        "dry_run": dry_run,
        "root": str(root),
        "steps": steps_out,
        "failed_step": failed_step,
        "verify": verify_result,
    }
