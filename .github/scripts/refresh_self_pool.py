#!/usr/bin/env python3
"""Refresh or check `examples/self-pool.json.gz` hashes against the working tree deterministically."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DETERMINISTIC_EXPORTED_AT = "2026-01-01T00:00:00Z"


def _bootstrap_imports(repo_root: Path) -> Tuple[Any, Any, Any, Any, Any, Any]:
    scripts_dir = str((repo_root / "scripts").resolve())
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from aoci_core import hubs  # noqa: E402
    from aoci_core.constants import (  # noqa: E402
        ENTRY_RE,
        MIN_RENAME_LINES,
        compute_size_tier,
        normalize_relpath,
    )
    from aoci_core.pool import _validate_pool_entry_row  # noqa: E402

    return hubs, ENTRY_RE, MIN_RENAME_LINES, compute_size_tier, normalize_relpath, _validate_pool_entry_row


def _file_sha_and_lines(full_path: Path) -> Tuple[str, int]:
    raw = full_path.read_bytes()
    norm = raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else raw
    lines = 0 if not norm else norm.count(b"\n") + (0 if norm.endswith(b"\n") else 1)
    return hashlib.sha256(norm).hexdigest(), lines


def _load_entry_overrides(
    entry_files: Sequence[str],
    repo_root: Path,
    entry_re: Any,
    normalize_relpath_fn: Any,
) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for raw_p in entry_files:
        p = Path(raw_p)
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        text = p.read_text(encoding="utf-8-sig")
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#") or (line.startswith("===") and line.endswith("===")):
                continue
            m = entry_re.match(line)
            if not m:
                raise ValueError(f"Invalid FRAS entry in `{p}` at L{lineno}: {line[:120]}")
            rel = normalize_relpath_fn(m.group("path").strip())
            overrides[rel] = line
    return overrides


def _parse_hub_moves(raw_moves: Sequence[str], normalize_relpath_fn: Any) -> Dict[Tuple[str, str], Tuple[str, str]]:
    moves: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for spec in raw_moves:
        if "=" not in spec:
            raise ValueError(f"Invalid `--move-hub` `{spec}` (expected `old_path#sym=new_path#sym`).")
        lhs, rhs = spec.split("=", 1)
        if "#" not in lhs or "#" not in rhs:
            raise ValueError(f"Invalid `--move-hub` `{spec}` (expected `old_path#sym=new_path#sym`).")
        old_p, old_s = lhs.split("#", 1)
        new_p, new_s = rhs.split("#", 1)
        moves[(normalize_relpath_fn(old_p.strip()), old_s.strip())] = (
            normalize_relpath_fn(new_p.strip()),
            new_s.strip(),
        )
    return moves


def _refresh_entry_size_tier(line: str, rel: str, actual_size: str, entry_re: Any) -> Tuple[str, bool]:
    m = entry_re.match(line.strip())
    if not m:
        return line.strip(), False
    cur_size = m.group("size")
    rebuilt = (
        f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{actual_size}]: "
        f"F:{m.group('f').strip()} | R:{m.group('r').strip()} | "
        f"A:{m.group('a').strip()} | S:{m.group('s').strip()}"
    )
    return rebuilt, (cur_size != actual_size or rebuilt != line.strip())


def run_refresh(
    repo_root: Path,
    bundle_path: Path,
    *,
    entry_files: Sequence[str] = (),
    move_hubs: Sequence[str] = (),
    drop_missing: bool = False,
    check_only: bool = False,
) -> Dict[str, Any]:
    (
        hubs,
        entry_re,
        min_rename_lines,
        compute_size_tier_fn,
        normalize_relpath_fn,
        validate_pool_row_fn,
    ) = _bootstrap_imports(repo_root)

    if not bundle_path.is_file():
        return {
            "ok": False,
            "stale": [],
            "errors": [f"Bundle file not found: `{bundle_path}`"],
        }

    raw_bytes = bundle_path.read_bytes()
    if raw_bytes[:2] == b"\x1f\x8b":
        raw_txt = gzip.decompress(raw_bytes).decode("utf-8")
    else:
        raw_txt = raw_bytes.decode("utf-8")
    data = json.loads(raw_txt)

    overrides = _load_entry_overrides(entry_files, repo_root, entry_re, normalize_relpath_fn)
    hub_moves = _parse_hub_moves(move_hubs, normalize_relpath_fn)

    stale: List[str] = []
    errors: List[str] = []

    by_ps_existing: Dict[str, Dict[str, str]] = {}
    for item in data.get("by_path_sha", []):
        rel = normalize_relpath_fn(item["path"])
        by_ps_existing[rel] = dict(item, path=rel)

    for rel, ov_line in overrides.items():
        if rel not in by_ps_existing:
            by_ps_existing[rel] = {"path": rel, "sha256": "", "entry": ov_line}

    old_sha_to_new: Dict[str, Tuple[str, str, str, int]] = {}
    new_ps_list: List[Dict[str, str]] = []

    for rel in sorted(by_ps_existing.keys()):
        item = by_ps_existing[rel]
        full = repo_root / rel
        if not full.is_file():
            if drop_missing:
                stale.append(f"{rel} (dropped-missing)")
                continue
            errors.append(f"Missing pooled file on disk: `{rel}`")
            continue

        new_sha, line_count = _file_sha_and_lines(full)
        actual_size = compute_size_tier_fn(line_count)
        old_sha = item.get("sha256", "")
        raw_entry = overrides.get(rel, item["entry"])
        refreshed_entry, entry_changed = _refresh_entry_size_tier(raw_entry, rel, actual_size, entry_re)
        clean_entry = validate_pool_row_fn(rel, new_sha, refreshed_entry)
        if clean_entry is None:
            errors.append(f"Invalid FRAS entry for `{rel}`: {refreshed_entry[:120]}")
            continue

        reasons: List[str] = []
        if new_sha != old_sha:
            reasons.append("sha256")
        if rel in overrides and clean_entry != item.get("entry"):
            reasons.append("entry-override")
        elif entry_changed:
            reasons.append(f"size->{actual_size}")
        if reasons:
            stale.append(f"{rel} ({','.join(reasons)})")

        if old_sha:
            old_sha_to_new[old_sha] = (rel, new_sha, clean_entry, line_count)
        new_ps_list.append({"path": rel, "sha256": new_sha, "entry": clean_entry})

    s_map: Dict[str, str] = {}
    for item in data.get("by_sha", []):
        old_s = item.get("sha256", "")
        mapped = old_sha_to_new.get(old_s)
        if mapped is not None:
            _rel, new_sha, clean_entry, line_count = mapped
            if line_count >= min_rename_lines:
                s_map[new_sha] = clean_entry
            else:
                stale.append(f"by_sha:{_rel} (<{min_rename_lines} lines)")
        elif not drop_missing:
            s_map[old_s] = item["entry"]

    for ps_item in new_ps_list:
        rel = ps_item["path"]
        full = repo_root / rel
        if full.is_file():
            _, lc = _file_sha_and_lines(full)
            if lc >= min_rename_lines and ps_item["sha256"] not in s_map:
                s_map[ps_item["sha256"]] = ps_item["entry"]
                if f"{rel} (" not in " ".join(stale):
                    stale.append(f"by_sha:{rel} (missing-by-sha)")

    new_s_list = [{"sha256": sha, "entry": ent} for sha, ent in sorted(s_map.items())]

    new_hub_list: List[Dict[str, str]] = []
    for hub in data.get("hubs_by_symbol_sha", []):
        rel_h = normalize_relpath_fn(hub["path"])
        sym_h = str(hub["symbol"]).strip()
        moved = hub_moves.get((rel_h, sym_h))
        hub_reasons: List[str] = []
        if moved is not None:
            rel_h, sym_h = moved
            hub_reasons.append("moved-hub")

        full_h = repo_root / rel_h
        if not full_h.is_file():
            errors.append(f"Missing hub file on disk: `{rel_h}#{sym_h}`")
            continue

        norm_b = hubs._read_norm_bytes(full_h)
        live_map, ok_parse = hubs._extract_live_symbol_span_map(full_h, rel_h)
        if not ok_parse or not live_map or sym_h not in live_map:
            errors.append(f"Missing live hub symbol: `{rel_h}#{sym_h}`")
            continue

        start, end = live_map[sym_h]
        new_span_sha = hubs.compute_span_body_sha256(norm_b, start, end)
        if not new_span_sha:
            errors.append(f"Cannot compute span SHA-256 for `{rel_h}#{sym_h}`")
            continue

        if new_span_sha != hub.get("span_sha256"):
            hub_reasons.append("span_sha256")
        if hub_reasons:
            stale.append(f"{rel_h}#{sym_h} ({','.join(hub_reasons)})")

        new_hub_list.append(
            {
                "path": rel_h,
                "symbol": sym_h,
                "span_sha256": new_span_sha,
                "f": str(hub["f"]).strip(),
                "s": str(hub["s"]).strip(),
            }
        )

    out_obj = {
        "schema_version": int(data.get("schema_version", 1)),
        "format": str(data.get("format", "aoci-semantic-pool-v1")),
        "exported_at": DETERMINISTIC_EXPORTED_AT,
        "by_path_sha": sorted(new_ps_list, key=lambda x: (x["path"], x["sha256"])),
        "by_sha": new_s_list,
        "hubs_by_symbol_sha": new_hub_list,
    }
    out_txt = json.dumps(out_obj, indent=2, ensure_ascii=False) + "\n"
    if not stale and not errors and out_txt != raw_txt:
        stale.append("bundle-formatting (non-canonical JSON or exported_at)")

    if errors:
        return {
            "ok": False,
            "check_only": check_only,
            "bundle": str(bundle_path),
            "files": len(new_ps_list),
            "hubs": len(new_hub_list),
            "stale": stale,
            "errors": errors,
        }

    if check_only:
        return {
            "ok": len(stale) == 0,
            "check_only": True,
            "bundle": str(bundle_path),
            "files": len(new_ps_list),
            "hubs": len(new_hub_list),
            "stale": stale,
            "errors": [],
        }

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    if bundle_path.name.lower().endswith(".gz"):
        payload = gzip.compress(out_txt.encode("utf-8"), compresslevel=9, mtime=0)
    else:
        payload = out_txt.encode("utf-8")
    bundle_path.write_bytes(payload)
    return {
        "ok": True,
        "check_only": False,
        "bundle": str(bundle_path),
        "bytes": len(payload),
        "files": len(new_ps_list),
        "hubs": len(new_hub_list),
        "refreshed": stale,
        "errors": [],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Refresh or check `examples/self-pool.json.gz` SHAs against the current working tree."
    )
    p.add_argument("--root", help="Repository root directory (defaults to repo containing this script)")
    p.add_argument(
        "--bundle",
        default="examples/self-pool.json.gz",
        help="Path to semantic pool bundle relative to --root or absolute (default: examples/self-pool.json.gz)",
    )
    p.add_argument(
        "--entry-file",
        action="append",
        default=[],
        help="Optional file containing object-fras-v2 lines to add or override in the bundle (repeatable)",
    )
    p.add_argument(
        "--move-hub",
        action="append",
        default=[],
        help="Relocate a hub symbol `old_path#sym=new_path#sym` when refactoring (repeatable)",
    )
    p.add_argument(
        "--drop-missing",
        action="store_true",
        help="Drop entries whose source files were deleted from the working tree",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Read-only check: exit 1 and list stale files/hubs without modifying the bundle",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON result")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent.parent
    bundle_path = Path(args.bundle)
    if not bundle_path.is_absolute():
        bundle_path = repo_root / bundle_path

    try:
        res = run_refresh(
            repo_root,
            bundle_path,
            entry_files=args.entry_file,
            move_hubs=args.move_hub,
            drop_missing=bool(args.drop_missing),
            check_only=bool(args.check),
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"[self-pool] ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["ok"] else 1

    if res.get("errors"):
        print(f"[self-pool] ERROR ({len(res['errors'])} issue(s)):", file=sys.stderr)
        for err in res["errors"]:
            print(f"  - {err}", file=sys.stderr)
        print("Run `python .github/scripts/refresh_self_pool.py` after resolving the issues above.", file=sys.stderr)
        return 1

    if args.check:
        if res["ok"]:
            print(f"[self-pool] OK: {res['files']} files, {res['hubs']} hubs up to date (0 stale).")
            return 0
        print(f"[self-pool] STALE ({len(res['stale'])} item(s) out of date):", file=sys.stderr)
        for item in res["stale"]:
            print(f"  - {item}", file=sys.stderr)
        print(
            "Run `python .github/scripts/refresh_self_pool.py` to refresh `examples/self-pool.json.gz`.",
            file=sys.stderr,
        )
        return 1

    refreshed = res.get("refreshed", [])
    print(
        f"[self-pool] Refreshed {bundle_path} ({res['bytes']} bytes, "
        f"{res['files']} files, {res['hubs']} hubs, {len(refreshed)} updated)."
    )
    for item in refreshed:
        print(f"  ~ {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
