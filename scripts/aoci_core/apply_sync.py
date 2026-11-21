"""One-shot drift synchronization workflow orchestrating restore, rename migration, ack, upsert, and verify."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


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
    ack_paths: Optional[Sequence[str]] = None,
    ack_impl_only: bool = False,
    reviewed: bool = False,
    entries: Optional[Sequence[str]] = None,
    entries_file: Optional[str] = None,
    prune_only: bool = False,
    no_prune: bool = False,
    strict_size: bool = False,
    drop_malformed: bool = False,
    verify: bool = True,
    full_verify: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Execute requested sync stages in canonical order (`restore -> migrate-renames -> ack -> upsert -> verify`).
    Halts immediately on the first failing step (`rc != 0`) and returns per-step structured outcomes.
    """
    ack_list = [str(p) for p in (ack_paths or ()) if str(p).strip()]
    entry_list = [str(e) for e in (entries or ()) if str(e).strip()]
    want_ack = bool(ack_impl_only or ack_list)
    want_upsert = bool(entry_list or entries_file or prune_only)

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

    plan: List[Tuple[str, List[str]]] = []

    if restore:
        cmd_r = ["restore", "--root", str(root), "--no-prune", "--json"]
        if allow_sha_fallback:
            cmd_r.append("--allow-sha-fallback")
        if dry_run:
            cmd_r.append("--dry-run")
        plan.append(("restore", cmd_r))

    if migrate_renames:
        cmd_m = ["migrate-renames", "--root", str(root), "--json"]
        if exact_only:
            cmd_m.append("--exact-only")
        if drop_malformed:
            cmd_m.append("--drop-malformed")
        if dry_run:
            cmd_m.append("--dry-run")
        plan.append(("migrate_renames", cmd_m))

    if want_ack:
        cmd_a = ["ack", "--root", str(root), "--json"]
        if ack_impl_only:
            cmd_a.append("--impl-only")
        if reviewed:
            cmd_a.append("--reviewed")
        if dry_run:
            cmd_a.append("--dry-run")
        if ack_list:
            cmd_a.extend(["--", *ack_list])
        plan.append(("ack", cmd_a))

    if want_upsert:
        cmd_u = ["upsert", "--root", str(root), "--json"]
        if entries_file:
            cmd_u.append(f"--entries-file={entries_file}")
        if prune_only:
            cmd_u.append("--prune-only")
        if no_prune and not prune_only:
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
        cmd_v = ["verify", "--root", str(root), "--json"]
        if not full_verify:
            cmd_v.append("--fast")
        plan.append(("verify", cmd_v))

    steps_out: List[Dict[str, Any]] = []
    failed_step: Optional[str] = None
    verify_result: Any = None

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

    return {
        "ok": failed_step is None,
        "dry_run": dry_run,
        "root": str(root),
        "steps": steps_out,
        "failed_step": failed_step,
        "verify": verify_result,
    }
