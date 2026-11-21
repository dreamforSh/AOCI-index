"""Zero-token mechanical index self-healer (`aoci doctor` / `aoci lint --fix` / MCP `aoci_doctor`).

Strictly enforces Rule #4: only repairs deterministic symbolic/structural metadata (duplicate rows,
stale `[LDIS]` Size tiers, dangling deleted file references in `R:`/`A:`, optional confirmed internal
static imports into `R:`, `aoci.hubs.txt` spans/registration, `.git/info/exclude`, and shards) while
never fabricating `F:` or `S:` semantics or re-baselining unreviewed modified files.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import discovery as _discovery
from . import drift as _drift
from . import git_ops as _git_ops
from . import hubs as _hubs
from . import ignore as _ignore
from . import lock as _lock
from . import parsers as _parsers
from . import shards as _shards
from . import write_ops as _write_ops
from .constants import (
    AOCI_DIR_NAME,
    CODE_FILE,
    DEFAULT_MAX_BYTES,
    ENTRY_RE,
    HUBS_FILE,
    MANIFEST_FILE,
    SHARDS_DIR,
    SHARDS_MANIFEST,
    normalize_relpath,
)
from .graph import (
    build_fras_graph,
    compile_path_ref_rewriter,
    extract_path_refs,
    find_dangling_refs,
    parse_entries_map,
)


def _split_top_level_delim(text: str, delim: str) -> List[str]:
    parts: List[str] = []
    cur: List[str] = []
    depth = 0
    for ch in text:
        if ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == delim and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _item_target_rel(item: str) -> str:
    head = item.split("(", 1)[0].split("[", 1)[0].strip().lstrip("-").rstrip(".")
    return normalize_relpath(head) if head else ""


def _prune_dangling_from_r(r_val: str, bad_set: Set[str]) -> str:
    raw = r_val.strip()
    if not raw or raw == "-":
        return "-"
    kept_semi: List[str] = []
    for semi_chunk in _split_top_level_delim(raw, ";"):
        kept_comma: List[str] = []
        for item in _split_top_level_delim(semi_chunk, ","):
            tok = item.strip()
            if not tok or tok == "-":
                continue
            cand = _item_target_rel(tok)
            if cand in bad_set:
                continue
            refs_in_tok = extract_path_refs(tok, bad_set)
            if refs_in_tok and len(refs_in_tok) == 1 and tok == refs_in_tok[0]:
                continue
            kept_comma.append(tok)
        if kept_comma:
            kept_semi.append(",".join(kept_comma))
    if not kept_semi:
        return "-"
    return "; ".join(kept_semi)


def _prune_dangling_from_a(a_val: str, bad_set: Set[str]) -> str:
    raw = a_val.strip()
    if not raw or raw == "-":
        return "-"
    kept_semi: List[str] = []
    for semi_chunk in _split_top_level_delim(raw, ";"):
        seg = semi_chunk.strip()
        if not seg or seg == "-":
            continue
        if "<-" in seg:
            lhs, rhs = seg.split("<-", 1)
            kept_callers: List[str] = []
            for caller_tok in _split_top_level_delim(rhs, ","):
                ct = caller_tok.strip()
                if not ct or ct == "-":
                    continue
                if _item_target_rel(ct) in bad_set:
                    continue
                kept_callers.append(ct)
            lhs_clean = lhs.strip()
            if kept_callers:
                kept_semi.append(f"{lhs_clean}<-{','.join(kept_callers)}")
            elif lhs_clean and lhs_clean != "-":
                kept_semi.append(lhs_clean)
        else:
            kept_items: List[str] = []
            for item in _split_top_level_delim(seg, ","):
                it = item.strip()
                if not it or it == "-":
                    continue
                if _item_target_rel(it) in bad_set:
                    continue
                kept_items.append(it)
            if kept_items:
                kept_semi.append(",".join(kept_items))
    if not kept_semi:
        return "-"
    return "; ".join(kept_semi)


def _append_static_deps_to_r(r_val: str, extra_deps: List[str]) -> str:
    if not extra_deps:
        return r_val
    cur = r_val.strip()
    if not cur or cur == "-":
        return ",".join(extra_deps)
    return f"{cur},{','.join(extra_deps)}"


def run_index_doctor(
    root: Path,
    *,
    sync_static_r: bool = False,
    drop_malformed: bool = False,
    dry_run: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Inspect `.aoci/` and deterministically repair mechanical metadata drift without touching F:/S: prose.
    """
    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.is_file():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    git_info = _git_ops.detect_git_info(root)
    git_status = _git_ops.check_git_exclusion(root, git_info, check_status=False, write_cache=not dry_run)
    git_exclude_repaired = False
    if git_status["is_git"] and not git_status["excluded"]:
        if not dry_run:
            repaired_status = _git_ops.ensure_git_exclusion(root, git_info, check_status=False)
            git_exclude_repaired = bool(repaired_status.get("excluded"))
        else:
            git_exclude_repaired = True

    lock_ctx: Any = contextlib.nullcontext() if dry_run else _lock.IndexLock(aoci_dir)
    with lock_ctx:
        duplicates: List[str] = []
        entries, malformed = _shards.parse_code_volume(code_path, duplicates=duplicates)
        if malformed and not drop_malformed:
            return {
                "ok": False,
                "dry_run": dry_run,
                "error": (
                    f"`aoci.code.txt` contains {len(malformed)} malformed line(s) "
                    "(pass `--drop-malformed` to discard them)."
                ),
                "malformed": [{"line": ln, "text": txt[:120]} for ln, txt in malformed[:10]],
            }

        baseline = _drift.load_baseline(aoci_dir)
        base_files: Dict[str, Any] = baseline.get("files", {})
        ignores = _ignore.load_ignore_matcher(root, git_info)
        live_fps, _ = _discovery.discover_and_fingerprint(
            root,
            git_info,
            ignores,
            base_files=base_files,
            fast_mtime=True,
            max_bytes=max_bytes,
            save_scan_cache=not dry_run,
        )

        deduplicated = sorted(set(duplicates))
        size_refreshed: List[str] = []
        dangling_pruned: Dict[str, List[str]] = {}
        static_deps_added: Dict[str, List[str]] = {}
        touched_entries: Set[str] = set(deduplicated)

        # 1) Refresh stale [LDIS] Size tiers from actual live line counts
        for rel, line in list(entries.items()):
            fp = live_fps.get(rel)
            if fp is None:
                continue
            m = ENTRY_RE.match(line)
            if not m:
                continue
            actual_sz = str(fp["size_tier"])
            if m.group("size") != actual_sz:
                entries[rel] = (
                    f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{actual_sz}]: "
                    f"F:{m.group('f').strip()} | R:{m.group('r').strip()} | "
                    f"A:{m.group('a').strip()} | S:{m.group('s').strip()}"
                )
                size_refreshed.append(rel)
                touched_entries.add(rel)

        # 2) Prune dangling non-existent path references from R: and A:
        parsed_map = parse_entries_map(entries)
        dangling_map = find_dangling_refs(
            root,
            entries,
            parsed_entries=parsed_map,
            existing_paths=set(live_fps.keys()),
        )
        for rel, bad_list in sorted(dangling_map.items()):
            line = entries.get(rel)
            if not line:
                continue
            m = ENTRY_RE.match(line)
            if not m:
                continue
            bad_set = set(bad_list)
            old_r = m.group("r").strip()
            old_a = m.group("a").strip()
            new_r = _prune_dangling_from_r(old_r, bad_set)
            new_a = _prune_dangling_from_a(old_a, bad_set)
            # Fallback token rewriter for inline prose mentions of deleted paths if still present
            still_bad = find_dangling_refs(
                root,
                {rel: f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{m.group('size')}]: F:{m.group('f').strip()} | R:{new_r} | A:{new_a} | S:{m.group('s').strip()}"},
                existing_paths=set(live_fps.keys()),
            ).get(rel, [])
            if still_bad:
                blank_rewriter = compile_path_ref_rewriter({bp: "" for bp in still_bad})
                cand_r = _prune_dangling_from_r(blank_rewriter(new_r), bad_set)
                cand_a = _prune_dangling_from_a(blank_rewriter(new_a), bad_set)
                new_r, new_a = cand_r, cand_a
            if new_r != old_r or new_a != old_a:
                entries[rel] = (
                    f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{m.group('size')}]: "
                    f"F:{m.group('f').strip()} | R:{new_r} | A:{new_a} | S:{m.group('s').strip()}"
                )
                dangling_pruned[rel] = list(bad_list)
                touched_entries.add(rel)

        # 3) Optional: append confirmed internal static imports (`missing-static-dep`) into R:
        if sync_static_r and entries:
            parsed_map = parse_entries_map(entries)
            deps_adj, _ = build_fras_graph(entries, parsed_entries=parsed_map)
            static_deps, _, _ = _parsers.build_static_import_graph(
                root,
                sorted(live_fps.keys()),
                fps=live_fps,
                aoci_dir=aoci_dir if not dry_run else None,
            )
            for rel in sorted(entries.keys()):
                s_targets = static_deps.get(rel) or []
                if not s_targets:
                    continue
                cur_deps = deps_adj.get(rel, set())
                missing_targets = [t for t in s_targets if t in entries and t != rel and t not in cur_deps]
                if not missing_targets:
                    continue
                m = ENTRY_RE.match(entries[rel])
                if not m:
                    continue
                old_r = m.group("r").strip()
                new_r = _append_static_deps_to_r(old_r, missing_targets)
                entries[rel] = (
                    f"{rel}[{m.group('layer')}{m.group('domain')}{m.group('imp')}{m.group('size')}]: "
                    f"F:{m.group('f').strip()} | R:{new_r} | "
                    f"A:{m.group('a').strip()} | S:{m.group('s').strip()}"
                )
                static_deps_added[rel] = missing_targets
                touched_entries.add(rel)

        # 4) Hubs volume span refresh / dead-symbol prune & manifest registration check
        hubs_path = aoci_dir / HUBS_FILE
        manifest_path = aoci_dir / MANIFEST_FILE
        manifest_repaired = False
        if manifest_path.is_file():
            m_txt = manifest_path.read_text(encoding="utf-8-sig", errors="replace")
            has_hub_vol = "id=hubs" in m_txt
            hubs_exists = hubs_path.is_file()
            if hubs_exists != has_hub_vol:
                manifest_repaired = True
                if not dry_run:
                    _hubs.sync_manifest_hubs_volume(aoci_dir)

        hubs_refreshed: List[str] = []
        hubs_pruned: List[str] = []
        if hubs_path.is_file():
            if not dry_run:
                hub_sync = _hubs.sync_hubs_on_upsert(root, aoci_dir, sorted(entries.keys()), [])
                hubs_refreshed = list(hub_sync.get("refreshed") or [])
                hubs_pruned = list(hub_sync.get("pruned") or [])
            else:
                records, _ = _hubs.parse_hubs_volume(hubs_path)
                span_cache: Dict[str, Tuple[Optional[Dict[str, Tuple[int, int]]], bool]] = {}
                for rec in records:
                    r_p = rec["relpath"]
                    sym = rec["symbol"]
                    full_p = root / r_p
                    if not full_p.is_file():
                        hubs_pruned.append(f"{r_p}#{sym}")
                        continue
                    if r_p not in span_cache:
                        span_cache[r_p] = _hubs._extract_live_symbol_span_map(full_p, r_p)
                    s_map, ok_parse = span_cache[r_p]
                    if not ok_parse or s_map is None:
                        continue
                    if sym not in s_map:
                        hubs_pruned.append(f"{r_p}#{sym}")
                    elif s_map[sym] != (int(rec["start"]), int(rec["end"])):
                        new_s, new_e = s_map[sym]
                        hubs_refreshed.append(
                            f"{r_p}#{sym} [L{rec['start']}-L{rec['end']}]->[L{new_s}-L{new_e}]"
                        )

        # 5) Shards check & write-out
        shards_manifest_p = aoci_dir / SHARDS_DIR / SHARDS_MANIFEST
        shards_resynced = False
        if shards_manifest_p.is_file():
            try:
                sm = json.loads(shards_manifest_p.read_text(encoding="utf-8"))
                if not isinstance(sm, dict) or int(sm.get("total_files", -1)) != len(entries) or touched_entries:
                    shards_resynced = True
            except (OSError, ValueError, TypeError):
                shards_resynced = True

        wrote_code = bool(touched_entries or (malformed and drop_malformed))
        if not dry_run:
            if wrote_code:
                _shards.write_code_volume(code_path, entries)
            if wrote_code or shards_resynced:
                _shards.sync_shards_if_enabled(aoci_dir, entries)

            # Update baseline size_tier & semantic pool ONLY for files already clean against baseline
            # so unreviewed modified source files are never silently acknowledged!
            clean_pool_updates: List[Tuple[str, Dict[str, Any], str]] = []
            baseline_touched: List[str] = []
            for rel in sorted(touched_entries):
                b_fp = base_files.get(rel)
                l_fp = live_fps.get(rel)
                if isinstance(b_fp, dict) and l_fp is not None and b_fp.get("sha256") == l_fp.get("sha256"):
                    if b_fp.get("size_tier") != l_fp["size_tier"]:
                        b_fp["size_tier"] = l_fp["size_tier"]
                        baseline_touched.append(rel)
                    clean_pool_updates.append((rel, b_fp, entries[rel]))
            if baseline_touched:
                baseline["files"] = base_files
                _drift.save_baseline(aoci_dir, baseline, touched_paths=baseline_touched)
            if clean_pool_updates:
                _write_ops.record_semantic_pool_entries(root, git_info, clean_pool_updates)

        total_fixes = (
            (1 if git_exclude_repaired else 0)
            + len(deduplicated)
            + len(size_refreshed)
            + sum(len(v) for v in dangling_pruned.values())
            + sum(len(v) for v in static_deps_added.values())
            + len(hubs_refreshed)
            + len(hubs_pruned)
            + (1 if manifest_repaired else 0)
            + (1 if shards_resynced and not wrote_code else 0)
        )

        return {
            "ok": True,
            "dry_run": dry_run,
            "root": str(root),
            "indexed_files": len(entries),
            "total_fixes": total_fixes,
            "git_exclude_repaired": git_exclude_repaired,
            "deduplicated": deduplicated,
            "size_refreshed": sorted(size_refreshed),
            "dangling_refs_pruned": dangling_pruned,
            "static_deps_added": static_deps_added,
            "hubs_refreshed": hubs_refreshed,
            "hubs_pruned": hubs_pruned,
            "manifest_repaired": manifest_repaired,
            "shards_resynced": bool(shards_resynced or (wrote_code and shards_manifest_p.is_file())),
        }
