"""Self-contained subagent batch work-pack builder (`aoci batch-pack` / MCP `aoci_batch_pack`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .batches import _build_pending_items, resolve_stable_batches
from .constants import (
    AOCI_DIR_NAME,
    DEFAULT_MAX_BYTES,
    IMPORTANCE_TOKEN_BUDGET,
    _is_within_root,
    normalize_relpath,
    strip_utf8_bom_bytes,
)
from .sync_prep import _safe_outline
from .templates import parse_declared_domains, parse_manifest_field
from .test_impact import _load_outline_cache


def _read_bounded_source(root: Path, rel: str, max_lines: int, max_bytes: int) -> Tuple[str, bool]:
    if max_lines <= 0:
        return "", False
    full = root / rel
    if not _is_within_root(root, full):
        return "", False
    try:
        if full.is_symlink() or not full.is_file():
            return "", False
        if full.stat().st_size > max_bytes:
            return "", False
        raw = strip_utf8_bom_bytes(full.read_bytes())
    except OSError:
        return "", False
    text = raw.replace(b"\r\n", b"\n").decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines), False
    return "\n".join(lines[:max_lines]), True


def build_batch_pack(
    root: Path,
    drift: Dict[str, Any],
    batch_num: int,
    *,
    scaffold_fn: Any,
    batch_size: Optional[int] = None,
    max_batch_lines: Optional[int] = None,
    reset_batches: bool = False,
    with_source: bool = False,
    max_source_lines: int = 200,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Assemble a self-contained subagent work-pack for 1-indexed batch `batch_num`, bundling:
      - Global invariants, declared Domain dictionary, and Importance token budgets
      - Auto-generated FRAS scaffolds (`[LDIS]`, internal `R:`, `A: sym<-caller`)
      - Previous FRAS entries & `change_kind` for modified files
      - Structural outlines (and optional bounded source code excerpts)
    """
    if batch_num < 1:
        return {"ok": False, "error": f"`batch` must be >= 1 (got {batch_num})."}

    aoci_dir = root / AOCI_DIR_NAME
    pending, _, _ = _build_pending_items(drift)
    batches, plan_meta = resolve_stable_batches(
        aoci_dir,
        pending,
        batch_size,
        max_batch_lines,
        persist=aoci_dir.is_dir(),
        reset=reset_batches,
    )
    total_batches = len(batches)
    if total_batches == 0:
        return {
            "ok": True,
            "root": str(root),
            "batch": batch_num,
            "total_batches": 0,
            "status": "no_pending_files",
            "batch_plan": plan_meta,
            "files": [],
        }
    if batch_num > total_batches:
        return {
            "ok": False,
            "error": f"Batch {batch_num} out of range (total batches: {total_batches}).",
            "batch_plan": plan_meta,
        }

    selected = batches[batch_num - 1]
    batch_paths = [normalize_relpath(str(it["path"])) for it in selected]
    fps: Dict[str, Dict[str, Any]] = drift.get("_fingerprints") or {}
    code_entries: Mapping[str, str] = drift.get("_code_entries") or {}
    mod_by_path = {normalize_relpath(str(m["path"])): m for m in drift.get("modified", ())}
    added_set = {normalize_relpath(str(a["path"])) for a in drift.get("added", ())}

    scaffold_by_path: Dict[str, str] = {}
    if batch_paths:
        scaffold_lines = scaffold_fn(
            root,
            batch_paths,
            max_bytes=max_bytes,
            precomputed_fps=fps,
        )
        for ln in scaffold_lines:
            p_raw = ln.split("[", 1)[0].strip()
            scaffold_by_path[normalize_relpath(p_raw)] = ln

    outline_files = _load_outline_cache(root)
    files_out: List[Dict[str, Any]] = []
    total_lines = 0
    for item in selected:
        rel = normalize_relpath(str(item["path"]))
        lines_cnt = int(item.get("lines", 0))
        total_lines += lines_cnt
        status_kind = "added" if rel in added_set else ("modified" if rel in mod_by_path else "missing_in_code")
        file_card: Dict[str, Any] = {
            "path": rel,
            "status": status_kind,
            "lines": lines_cnt,
            "size_tier": item.get("size_tier", "S"),
            "bytes": int(item.get("bytes", 0)),
            "scaffold": scaffold_by_path.get(rel, ""),
            "outline": _safe_outline(root, rel, max_bytes, max_items=35, outline_cache_files=outline_files),
        }
        if rel in mod_by_path:
            file_card["change_kind"] = mod_by_path[rel].get("change_kind", "unknown")
            file_card["previous_entry"] = code_entries.get(rel)
        if with_source:
            excerpt, truncated = _read_bounded_source(root, rel, max_source_lines, max_bytes)
            file_card["source_excerpt"] = excerpt
            file_card["source_truncated"] = truncated
        files_out.append(file_card)

    invariants = parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-"
    domains = parse_declared_domains(aoci_dir)
    budgets = {str(k): f"{lo}-{hi} tokens" for k, (lo, hi) in sorted(IMPORTANCE_TOKEN_BUDGET.items())}

    return {
        "ok": True,
        "root": str(root),
        "batch": batch_num,
        "total_batches": total_batches,
        "status": "completed_slot" if not files_out else "ready",
        "SuggestedPendingFile": f".aoci/pending/batch_{batch_num:02d}.txt",
        "total_files": len(files_out),
        "total_lines": total_lines,
        "global_invariants": invariants,
        "domains": domains,
        "importance_token_budgets": budgets,
        "batch_plan": plan_meta,
        "files": files_out,
    }


def render_batch_pack_markdown(pack_obj: Mapping[str, Any]) -> str:
    """Render a `build_batch_pack` dict as a self-contained subagent briefing Markdown document."""
    b_num = pack_obj.get("batch", 1)
    b_tot = pack_obj.get("total_batches", 0)
    sug_out = pack_obj.get("SuggestedPendingFile", f".aoci/pending/batch_{int(b_num):02d}.txt")
    lines: List[str] = [
        f"# AOCI Subagent Batch Briefing — Batch {b_num} / {b_tot}",
        f"- **Root**: `{pack_obj.get('root', '')}`",
        f"- **Files in Batch**: {pack_obj.get('total_files', 0)} ({pack_obj.get('total_lines', 0)} lines)",
        f"- **Target Output File**: `{sug_out}`",
        f"- **Global Invariants**: {pack_obj.get('global_invariants', '-')}",
    ]
    domains = pack_obj.get("domains") or {}
    if isinstance(domains, Mapping) and domains:
        dom_str = ", ".join(f"`{k}`={v}" for k, v in sorted(domains.items()))
        lines.append(f"- **Declared Domains**: {dom_str}")
    lines.extend(
        [
            "",
            "## Authoring Rules (`object-fras-v2`)",
            "1. Output strictly one single-line entry per file: `<relpath>[<Layer><Domain><Imp><Size>]: F:<...> | R:<...> | A:<...> | S:<...>`",
            "2. Read the actual source code (read full file if truncated below), replace `<TODO:read-source>` with concrete responsibility (`F:`) and non-obvious invariants/traps (`S:`, or `-` if none).",
            "3. Never leave `<TODO:...>` placeholders; write completed lines to the target `.aoci/pending/` file.",
            "",
        ]
    )
    for idx, fc in enumerate(pack_obj.get("files") or (), start=1):
        ck = f", change={fc['change_kind']}" if fc.get("change_kind") else ""
        lines.append(f"### {idx}. `{fc['path']}` ({fc['status']}, {fc['lines']}L, tier={fc['size_tier']}{ck})")
        if fc.get("previous_entry"):
            lines.append(f"- **Previous Entry**: `{fc['previous_entry']}`")
        if fc.get("scaffold"):
            lines.append(f"- **Scaffold**: `{fc['scaffold']}`")
        out_obj = fc.get("outline")
        if isinstance(out_obj, Mapping):
            syms = out_obj.get("symbols") or []
            if syms:
                lines.append(f"- **Symbols**: {', '.join(f'`{s}`' for s in syms[:15])}")
        if "source_excerpt" in fc and fc["source_excerpt"]:
            trunc_tag = " (truncated — read full file on disk if needed)" if fc.get("source_truncated") else ""
            lines.append(f"```text{trunc_tag}\n{fc['source_excerpt']}\n```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
