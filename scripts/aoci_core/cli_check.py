"""Verification, drift, lint, audit, diff, sync-prep, batch-pack, and CI CLI subcommands."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import audit as _audit
from . import batch_pack as _batch_pack
from . import ci_check as _ci_check
from . import cli_read as _cli_read
from . import diff_brief as _diff_brief
from . import drift as _drift
from . import git_ops as _git_ops
from . import graph as _graph
from . import hubs as _hubs
from . import sync_prep as _sync_prep
from . import write_ops as _write_ops
from .batches import _build_pending_items, _clear_batch_plan, resolve_stable_batches
from .constants import (
    AOCI_DIR_NAME,
    BASELINE_FILE,
    CODE_FILE,
    DEFAULT_MAX_BYTES,
    MANIFEST_FILE,
    META_FILE,
    SHARDS_DIR,
    SHARDS_MANIFEST,
    _resolve_cli_file_path,
    write_text_lf,
)
from .graph import (
    build_explicit_mention_index,
    build_fras_graph,
    estimate_tokens,
    find_dangling_refs,
    parse_entries_map,
)


def cmd_scan(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    git_info = _git_ops.detect_git_info(root)

    if args.check_only:
        git_status = _git_ops.check_git_exclusion(root, git_info, check_status=False)
    else:
        git_status = _git_ops.ensure_git_exclusion(root, git_info, check_status=False)

    restored_info: Optional[Dict[str, Any]] = None
    if getattr(args, "auto_restore", False) and not args.check_only and (root / AOCI_DIR_NAME / CODE_FILE).exists():
        restored_info = _write_ops.restore_from_semantic_pool(
            root, git_info, allow_sha_fallback=False, prune_deleted=False, max_bytes=args.max_bytes
        )

    drift = _drift.compute_drift(
        root,
        git_info,
        fast_mtime=args.fast or getattr(args, "git_fast", False),
        max_bytes=args.max_bytes,
        git_incremental=getattr(args, "git_fast", False),
        allow_heal=not args.check_only,
        save_scan_cache=not args.check_only,
    )

    # Exact/similar renames should be migrated via `migrate-renames` first rather than re-authored in batches
    pending, added_non_sim, deleted_non_sim = _build_pending_items(drift)
    batches: List[List[Dict[str, Any]]] = []
    batch_plan_meta: Optional[Dict[str, Any]] = None
    if args.batches:
        batches, batch_plan_meta = resolve_stable_batches(
            root / AOCI_DIR_NAME,
            pending,
            args.batches,
            args.max_batch_lines,
            persist=not args.check_only,
            reset=bool(getattr(args, "reset_batches", False)),
        )
    elif not pending and not args.check_only:
        _clear_batch_plan(root / AOCI_DIR_NAME)

    # Entries whose R:/A: explicitly mention (or depend on) a modified/renamed/deleted file need review
    affected: Dict[str, List[str]] = {}
    changed_paths = [it["path"] for it in drift["modified"]] + [r["from"] for r in drift["renamed"]] + list(
        drift["deleted"]
    )
    if changed_paths and drift["_code_entries"]:
        code_map = drift["_code_entries"]
        parsed_map = drift.get("_parsed_entries") or parse_entries_map(code_map)
        _, dependents = build_fras_graph(code_map, parsed_entries=parsed_map)
        mentioned_by = build_explicit_mention_index(code_map, parsed_entries=parsed_map)
        changed_set = set(changed_paths)
        for p in changed_paths:
            nbs = sorted((mentioned_by.get(p, set()) | dependents.get(p, set())) - changed_set)
            if nbs:
                affected[p] = nbs

    payload: Dict[str, Any] = {
        "git": git_status,
        "drift": _drift.public_drift(drift),
        "affected_neighbors": affected,
        "batches": batches,
    }
    if batch_plan_meta is not None:
        payload["batch_plan"] = batch_plan_meta
    if restored_info is not None:
        payload["auto_restored"] = restored_info.get("restored", [])
        if restored_info.get("sha_candidates"):
            payload["auto_restore_sha_candidates"] = restored_info["sha_candidates"]

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        status_label = "CLEAN" if not drift["has_drift"] else "DRIFT"
        print(f"[AOCI Scan] Status: {status_label} | Root: {root} | {drift['elapsed_ms']}ms")
        if restored_info and restored_info.get("restored"):
            print(f"[Auto-Restore] Restored {len(restored_info['restored'])} entry(ies) from semantic cache pool.")
        if git_status["is_git"]:
            git_lbl = "OK" if git_status["ok"] else "FAIL"
            extra = " (auto-added to .git/info/exclude)" if git_status.get("changed") else ""
            print(f"[Git Exclude] {git_lbl}{extra}")
            if git_status["tracked_files"]:
                print(f"  CRITICAL: tracked `.aoci` files found -> run `git rm -r --cached {AOCI_DIR_NAME}`")
        print(
            f"[Counts] Total={drift['total_eligible']} | Clean={drift['indexed_clean']} | "
            f"Added={len(drift['added'])} | Modified={len(drift['modified'])} | "
            f"Renamed={len(drift['renamed'])} | MissingInCode={len(drift['missing_in_code'])} | "
            f"Deleted={len(drift['deleted'])} | Malformed={len(drift['malformed_lines'])}"
        )

        if drift["renamed"]:
            print("\nRENAMED (exact content match - run `migrate-renames` or migrate manually):")
            for r in drift["renamed"][:30]:
                print(f"  > {r['from']} -> {r['to']}  [size={r['size_tier']}, {r['lines']}L]")
                if r.get("previous_entry"):
                    print(f"    prev: {r['previous_entry']}")

        if drift.get("similar_renames"):
            print(
                "\nSIMILAR RENAMES (moved + edited; `migrate-renames` carries entry & refs, "
                "leaving file MODIFIED for `ack`/`upsert` review):"
            )
            for sr in drift["similar_renames"][:30]:
                pct = int(round(sr["similarity"] * 100))
                print(f"  ~> {sr['from']} -> {sr['to']}  [{pct}% similar, size={sr['size_tier']}, {sr['lines']}L]")
                if sr.get("previous_entry"):
                    print(f"     prev: {sr['previous_entry']}")

        if added_non_sim:
            print("\nADDED (need new FRAS entries):")
            for it in added_non_sim[:50]:
                print(f"  + {it['path']}  [size={it['size_tier']}, {it['lines']}L]")
            if len(added_non_sim) > 50:
                print(f"  ... (+{len(added_non_sim) - 50} more; use --batches or --json)")

        if drift["modified"]:
            print("\nMODIFIED (review entry; if F/R/A/S still accurate run `ack <path>`, else `upsert`):")
            for it in drift["modified"][:50]:
                ck = str(it.get("change_kind", "unknown"))
                ck_tag = f", {ck.replace('_', '-')}" if ck != "unknown" else ""
                print(f"  ~ {it['path']}  [size={it['size_tier']}, {it['lines']}L{ck_tag}]")
            if len(drift["modified"]) > 50:
                print(f"  ... (+{len(drift['modified']) - 50} more)")

        if drift["missing_in_code"]:
            print("\nMISSING IN aoci.code.txt:")
            for it in drift["missing_in_code"][:50]:
                print(f"  ? {it['path']}  [size={it['size_tier']}, {it['lines']}L]")

        if deleted_non_sim:
            print("\nDELETED (will be pruned by `upsert`):")
            for p in deleted_non_sim[:50]:
                print(f"  - {p}")
            if len(deleted_non_sim) > 50:
                print(f"  ... (+{len(deleted_non_sim) - 50} more)")

        if drift["malformed_lines"]:
            print("\nMALFORMED LINES IN aoci.code.txt:")
            for ml in drift["malformed_lines"][:20]:
                print(f"  ! L{ml['line']}: {ml['text'][:100]}")

        if drift["invalid_entries"]:
            print("\nINVALID ENTRY TAGS / FIELDS IN aoci.code.txt:")
            for inv in drift["invalid_entries"][:20]:
                print(f"  ! [{inv['code']}] {inv['message']}")

        if drift["duplicate_entries"]:
            print("\nDUPLICATE ENTRIES IN aoci.code.txt (last occurrence wins; next upsert dedups):")
            for p in drift["duplicate_entries"][:20]:
                print(f"  ! {p}")

        if affected:
            print("\nAFFECTED NEIGHBORS (entries whose R:/A: mention or depend on changed files; re-check their R:/A:/S:):")
            for p, nbs in list(affected.items())[:30]:
                more = f" (+{len(nbs) - 8} more)" if len(nbs) > 8 else ""
                print(f"  * {p} <- {', '.join(nbs[:8])}{more}")

        if drift["undeclared_domains"]:
            print("\nUNDECLARED DOMAINS (add to `.aoci/aoci.meta.txt`):")
            for dom, paths in drift["undeclared_domains"].items():
                print(f"  ! Domain `{dom}` used by {len(paths)} file(s) (e.g. {paths[0]})")

        if batches:
            eff_lines = (batch_plan_meta or {}).get("max_lines", args.max_batch_lines)
            print(
                f"\nBATCH PLAN ({len(batches)} batch(es), max_files={args.batches}, "
                f"max_lines={eff_lines}):"
            )
            for idx, batch in enumerate(batches, start=1):
                if not batch:
                    print(f"  Batch {idx}/{len(batches)} (DONE)")
                else:
                    tot_l = sum(int(b.get("lines", 0)) for b in batch)
                    paths = ", ".join(f"{b['path']}[{b['size_tier']}]" for b in batch)
                    print(f"  Batch {idx}/{len(batches)} ({len(batch)} files, {tot_l}L): {paths}")

    if args.fail_on_drift and (drift["has_drift"] or not git_status["ok"]):
        return 1
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    git_info = _git_ops.detect_git_info(root)
    git_status = _git_ops.check_git_exclusion(root, git_info, check_status=True)

    issues: List[str] = []
    warnings: List[str] = []

    if git_status["is_git"]:
        if not git_status["excluded"]:
            issues.append(
                f"Git exclusion missing: `{git_status['pattern']}` is NOT ignored. "
                "Run `aoci_index.py ensure-git-exclude`."
            )
        if git_status["tracked_files"]:
            issues.append(
                f"Git index tracks {len(git_status['tracked_files'])} `.aoci/` file(s)! "
                f"Run `git rm -r --cached {AOCI_DIR_NAME}`."
            )
        if git_status["status_leaks"] and git_status["excluded"] and not git_status["tracked_files"]:
            issues.append(
                f"`git status` still reports `.aoci/`: {', '.join(git_status['status_leaks'][:5])}"
            )

    for fname in (MANIFEST_FILE, META_FILE, CODE_FILE, BASELINE_FILE):
        if not (aoci_dir / fname).exists():
            issues.append(f"Missing required index file: `.aoci/{fname}`")

    if issues and not (aoci_dir / CODE_FILE).exists():
        if args.json:
            print(json.dumps({"ok": False, "issues": issues, "git": git_status}, indent=2, ensure_ascii=False))
        else:
            print("[AOCI Verify] FAIL")
            for i in issues:
                print(f"  - {i}")
        return 1

    drift = _drift.compute_drift(
        root,
        git_info,
        fast_mtime=getattr(args, "fast", False) or getattr(args, "git_fast", False),
        max_bytes=args.max_bytes,
        git_incremental=getattr(args, "git_fast", False),
        allow_heal=False,
    )
    if drift["renamed"]:
        sample = drift["renamed"][0]
        issues.append(
            f"{len(drift['renamed'])} renamed file(s) need migration "
            f"(e.g. {sample['from']} -> {sample['to']}; run `migrate-renames`)"
        )
    if drift["added"]:
        issues.append(f"{len(drift['added'])} unindexed new file(s) (e.g. {drift['added'][0]['path']})")
    if drift["modified"]:
        issues.append(
            f"{len(drift['modified'])} modified/stale file(s) (e.g. {drift['modified'][0]['path']}; "
            "`upsert` a revised entry, or `ack` if the entry is still accurate)"
        )
    if drift["missing_in_code"]:
        issues.append(f"{len(drift['missing_in_code'])} file(s) missing from `aoci.code.txt`")
    if drift["deleted"]:
        issues.append(f"{len(drift['deleted'])} deleted/orphan file(s) still in index (run `upsert --prune-only`)")
    if drift["malformed_lines"]:
        issues.append(f"{len(drift['malformed_lines'])} malformed line(s) in `aoci.code.txt`")
    for inv in drift["invalid_entries"]:
        issues.append(inv["message"])
    hub_verify_issues, _ = _hubs.validate_hubs_volume(
        root, aoci_dir, drift["_code_entries"], fingerprints=drift.get("_fingerprints"), check_live_symbols=False
    )
    issues.extend(hub_verify_issues)

    for dom, paths in drift["undeclared_domains"].items():
        warnings.append(f"Undeclared Domain `{dom}` in `.aoci/aoci.meta.txt` (used by {len(paths)} file(s))")
    if drift["duplicate_entries"]:
        warnings.append(
            f"{len(drift['duplicate_entries'])} duplicate path(s) in `aoci.code.txt` "
            f"(e.g. {drift['duplicate_entries'][0]}); the next upsert keeps the last occurrence"
        )
    entries: Dict[str, str] = drift["_code_entries"]
    parsed_map = drift.get("_parsed_entries") or _graph.parse_entries_map(entries)
    fps_obj = drift.get("_fingerprints")
    existing_fps = set(fps_obj.keys()) if isinstance(fps_obj, dict) else None
    dangling = find_dangling_refs(
        root,
        entries,
        parsed_entries=parsed_map,
        existing_paths=existing_fps,
    )
    if dangling:
        sample_rel, sample_refs = next(iter(sorted(dangling.items())))
        warnings.append(
            f"{len(dangling)} entr(ies) reference missing paths in R:/A: "
            f"(e.g. {sample_rel} -> {sample_refs[0]}; run `lint`)"
        )

    code_text = drift.get("_code_text") or (aoci_dir / CODE_FILE).read_text(encoding="utf-8", errors="replace")

    manifest_p = aoci_dir / MANIFEST_FILE
    if manifest_p.is_file():
        if "#AOCI-ROOT-MANIFEST:" not in manifest_p.read_text(encoding="utf-8", errors="replace"):
            warnings.append("`.aoci/aoci.txt` is missing the `#AOCI-ROOT-MANIFEST:` header.")
    meta_p = aoci_dir / META_FILE
    if meta_p.is_file():
        if "#AOCI-META-VOLUME:" not in meta_p.read_text(encoding="utf-8", errors="replace"):
            warnings.append("`.aoci/aoci.meta.txt` is missing the `#AOCI-META-VOLUME:` header.")
    shards_manifest_p = aoci_dir / SHARDS_DIR / SHARDS_MANIFEST
    if shards_manifest_p.is_file():
        try:
            sm = json.loads(shards_manifest_p.read_text(encoding="utf-8"))
            if isinstance(sm, dict) and int(sm.get("total_files", -1)) != len(entries):
                warnings.append(
                    f"`.aoci/{SHARDS_DIR}/{SHARDS_MANIFEST}` is out of sync "
                    f"(total_files={sm.get('total_files')}, code entries={len(entries)}); run `shard`."
                )
        except (OSError, ValueError, TypeError):
            warnings.append(f"`.aoci/{SHARDS_DIR}/{SHARDS_MANIFEST}` is unreadable or invalid JSON; run `shard`.")
    est_tokens = estimate_tokens(code_text)
    layer_dist: Dict[str, int] = {}
    domain_dist: Dict[str, int] = {}
    imp_high = 0
    for pe in parsed_map.values():
        layer_dist[pe.layer] = layer_dist.get(pe.layer, 0) + 1
        domain_dist[pe.domain] = domain_dist.get(pe.domain, 0) + 1
        if pe.imp >= 8:
            imp_high += 1

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    ok = len(issues) == 0
    report = {
        "ok": ok,
        "root": str(root),
        "elapsed_ms": elapsed_ms,
        "git": git_status,
        "indexed_files": len(entries),
        "high_importance_files": imp_high,
        "code_volume_bytes": len(code_text.encode("utf-8")),
        "estimated_tokens": est_tokens,
        "layers": dict(sorted(layer_dist.items())),
        "domains": dict(sorted(domain_dist.items())),
        "issues": issues,
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"[AOCI Verify] {'PASS' if ok else 'FAIL'} | Root: {root} | {elapsed_ms}ms")
        print(
            f"[Stats] Indexed={len(entries)} | HighImp(8-9)={imp_high} | "
            f"Size={report['code_volume_bytes']}B (~{est_tokens} tokens)"
        )
        if layer_dist:
            ld = ", ".join(f"{k}:{v}" for k, v in sorted(layer_dist.items()))
            print(f"[Layers] {ld}")
        if domain_dist:
            dd = ", ".join(f"{k}:{v}" for k, v in sorted(domain_dist.items()))
            print(f"[Domains] {dd}")
        if git_status["is_git"]:
            print(f"[Git Exclude] {'OK' if git_status['ok'] else 'FAIL'} ({git_status['pattern']})")
        if warnings:
            print("Warnings:")
            for w in warnings:
                print(f"  - {w}")
        if issues:
            print("Issues:")
            for iss in issues:
                print(f"  - {iss}")

    return 0 if ok else 1


def cmd_lint(args: argparse.Namespace) -> int:
    """FRAS quality gate: vague/short F:, token budget vs Importance, empty S: on hubs, dangling refs, duplicates."""
    root = _git_ops.resolve_root(args.root)
    res = _audit.compute_lint(
        root,
        code_filter=args.code,
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
    )
    if res.get("error"):
        return _cli_read._emit_cli_error(bool(getattr(args, "json", False)), str(res["error"]), 2, extra=res)

    findings = res["findings"]
    by_code = res["counts"]
    indexed = res["indexed"]
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        label = "CLEAN" if not findings else f"{len(findings)} finding(s)"
        print(f"[AOCI Lint] {label} | Indexed={indexed}")
        if by_code:
            print("[Counts] " + ", ".join(f"{k}:{v}" for k, v in sorted(by_code.items())))
        for f in findings[: args.limit]:
            print(f"  [{f['code']}] {f['path']}: {f['message']}")
        if len(findings) > args.limit:
            print(f"  ... (+{len(findings) - args.limit} more; use --json or --limit)")
    return 1 if (findings and args.strict) else 0


def cmd_diff(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    with_tests = bool(getattr(args, "with_tests", False))
    brief = _diff_brief.compute_diff_briefing(
        root,
        rev=args.rev,
        staged=args.staged,
        depth=args.depth,
        max_bytes=args.max_bytes,
        with_tests=with_tests,
        live_test_imports=bool(getattr(args, "live_test_imports", False)),
    )
    if not brief.get("ok"):
        return _cli_read._emit_cli_error(bool(getattr(args, "json", False)), str(brief.get("error")), 2, extra=brief)

    if args.json:
        print(json.dumps(brief, indent=2, ensure_ascii=False))
        return 0

    print(f"# AOCI Diff Impact Briefing ({brief['scope']})")
    print(f"# Global-Invariants: {brief['global_invariants']}")
    if brief["high_risk_constraints"]:
        print(f"\n## High-Risk S: Constraints on Touched Files ({len(brief['high_risk_constraints'])})")
        for item in brief["high_risk_constraints"]:
            print(f"  ! {item['path']} [I={item['importance']}]: {item['s']}")
    if brief.get("dependency_constraints"):
        print(f"\n## Downstream R: Dependency S: Constraints ({len(brief['dependency_constraints'])})")
        for item in brief["dependency_constraints"]:
            print(f"  * {item['path']} [I={item['importance']}]: {item['s']}")
    for ln in brief["changed_entries"]:
        print(ln)
    if brief.get("hub_sections"):
        print(f"\n## Hub Sections ({len(brief['hub_sections'])})")
        for sec in brief["hub_sections"]:
            print(sec["entry"])
    if brief["unindexed_changed"]:
        print(f"\n## Unindexed Changed/New Files ({len(brief['unindexed_changed'])})")
        for p in brief["unindexed_changed"]:
            print(f"  + {p}")
    if brief["deleted_entries"]:
        print(f"\n## Deleted Indexed Entries ({len(brief['deleted_entries'])})")
        for ln in brief["deleted_entries"]:
            print(f"  - {ln}")
    print(f"\n## Affected Upstream Neighbors (depth={args.depth}, count={len(brief['affected_neighbors'])})")
    for ln in brief["affected_neighbors"]:
        print(ln)
    if with_tests:
        _cli_read._print_impacted_tests(brief.get("impacted_tests", []))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    root = _git_ops.resolve_root(args.root)
    check_co = bool(getattr(args, "co_change", False))
    res = _audit.audit_architecture(
        root,
        check_static=not getattr(args, "no_static", False),
        check_cycles=not getattr(args, "no_cycles", False),
        check_layers=not getattr(args, "no_layers", False),
        check_co_change=check_co,
        co_change_commits=int(getattr(args, "co_change_commits", 200)),
        min_co_changes=int(getattr(args, "min_co_changes", 3)),
        min_co_confidence=float(getattr(args, "min_co_confidence", 0.6)),
        exclude_tests=bool(getattr(args, "exclude_tests", False)),
        forbid_spec=getattr(args, "forbid", None),
        use_default_rules=not getattr(args, "no_default_rules", False),
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
    )
    if res.get("error"):
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {res['error']}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        sm = res["summary"]
        status = "CLEAN" if res["ok"] else "ISSUES"
        co_note = f" | CoChangeUnlinked={sm['co_change_unlinked']}" if "co_change_unlinked" in sm else ""
        print(
            f"[AOCI Audit] {status} | Indexed={sm['indexed']} | "
            f"MissingStaticDeps={sm['missing_static_deps']} | PhantomFrasInfo={sm['phantom_fras_deps']} | "
            f"Cycles={sm['cycles']} | LayerViolations={sm['layer_violations']}{co_note}"
        )
        if res["missing_static_deps"]:
            print(f"\nMISSING STATIC DEPS IN FRAS R:/A: ({len(res['missing_static_deps'])}):")
            for item in res["missing_static_deps"][:40]:
                print(f"  ! {item['from']} -> {item['to']}")
        if res["layer_violations"]:
            print(f"\nLAYER VIOLATIONS ({len(res['layer_violations'])}):")
            for item in res["layer_violations"][:40]:
                print(f"  ! [{item['rule']}] {item['from']} -> {item['to']}")
        if res["cycles"]:
            print(f"\nDEPENDENCY CYCLES ({len(res['cycles'])}):")
            for cyc in res["cycles"][:20]:
                print(f"  * ({cyc['size']}) {' <-> '.join(cyc['nodes'][:10])}")
        if res["phantom_fras_deps"]:
            print(f"\nPHANTOM FRAS R: DEPS [info] ({len(res['phantom_fras_deps'])}):")
            for item in res["phantom_fras_deps"][:30]:
                print(f"  ? {item['from']} -> {item['to']}")
        if res.get("co_change_unlinked"):
            print(f"\nCO-CHANGE UNLINKED PAIRS ({len(res['co_change_unlinked'])}):")
            for item in res["co_change_unlinked"][:30]:
                hop_tag = " [2-hop]" if item.get("is_two_hop") else ""
                print(
                    f"  ~ {item['file_a']} <-> {item['file_b']} "
                    f"(co={item['co_changes']}, conf={item['confidence']} "
                    f"[{item.get('confidence_a_to_b', item['confidence'])}/{item.get('confidence_b_to_a', item['confidence'])}]){hop_tag}"
                )

    return 1 if (getattr(args, "strict", False) and not res["ok"]) else 0


def cmd_prepare_sync(args: argparse.Namespace) -> int:
    """Collect single-pass drift briefing, scaffolds, outlines, diffs, and recommended sync actions."""
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    as_json = bool(getattr(args, "json", False))
    if not (aoci_dir / CODE_FILE).exists():
        msg = f"`{aoci_dir / CODE_FILE}` not found. Run `init` first."
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 2

    git_info = _git_ops.detect_git_info(root)
    drift = _drift.compute_drift(
        root,
        git_info,
        fast_mtime=not bool(getattr(args, "no_fast", False)),
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
        allow_heal=False,
        save_scan_cache=False,
    )
    brief = _sync_prep.prepare_sync_briefing(
        root,
        git_info,
        drift,
        scaffold_fn=_audit.scaffold_entries,
        run_git_fn=_git_ops.run_git,
        max_diff_lines=int(getattr(args, "max_diff_lines", 40)),
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
    )

    if as_json:
        print(json.dumps(brief, indent=2, ensure_ascii=False))
    else:
        c = brief["counts"]
        status = "DRIFT" if brief["has_drift"] else "CLEAN"
        print(
            f"[AOCI Prepare-Sync] {status} | PoolRestorable={c['restorable_from_pool']} | "
            f"Renames={c['renamed']}+{c['similar_renames']} | "
            f"ImplOnly={c['impl_only_modified']} | ContractReview={c['contract_or_unknown_modified']} | "
            f"Added={c['added']} | Deleted={c['deleted']}"
        )
        for act in brief["recommended_actions"]:
            print(f"  -> {act}")
    return 0


def cmd_batch_pack(args: argparse.Namespace) -> int:
    """Assemble a self-contained subagent briefing work-pack for 1-indexed batch N."""
    root = _git_ops.resolve_root(args.root)
    aoci_dir = root / AOCI_DIR_NAME
    as_json = bool(getattr(args, "json", False))
    if not (aoci_dir / CODE_FILE).exists():
        return _cli_read._emit_cli_error(as_json, f"`{aoci_dir / CODE_FILE}` not found. Run `init` first.", 2)

    git_info = _git_ops.detect_git_info(root)
    drift = _drift.compute_drift(
        root,
        git_info,
        fast_mtime=True,
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
        allow_heal=False,
        save_scan_cache=False,
    )
    pack = _batch_pack.build_batch_pack(
        root,
        drift,
        int(args.batch),
        scaffold_fn=_audit.scaffold_entries,
        batch_size=getattr(args, "batch_size", None),
        max_batch_lines=getattr(args, "max_batch_lines", None),
        reset_batches=bool(getattr(args, "reset_batches", False)),
        with_source=bool(getattr(args, "with_source", False)),
        max_source_lines=int(getattr(args, "max_source_lines", 200)),
        max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
    )
    if not pack.get("ok"):
        return _cli_read._emit_cli_error(as_json, str(pack.get("error", "Batch pack failed.")), 2, extra=pack)

    rendered_md = _batch_pack.render_batch_pack_markdown(pack)
    if getattr(args, "output", None):
        out_p = _resolve_cli_file_path(root, args.output, must_exist=False)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(out_p, json.dumps(pack, indent=2, ensure_ascii=False) + "\n" if as_json else rendered_md)
        pack = dict(pack)
        pack["output_path"] = str(out_p)

    if as_json:
        print(json.dumps(pack, indent=2, ensure_ascii=False))
    elif getattr(args, "output", None):
        print(
            f"[AOCI Batch-Pack] OK | Batch={pack['batch']}/{pack['total_batches']} | "
            f"Files={pack.get('total_files', 0)} -> {pack['output_path']}"
        )
    else:
        print(rendered_md, end="")
    return 0


def cmd_ci_check(args: argparse.Namespace) -> int:
    """CI-native architecture guard supporting JSON, GitHub Actions annotations, SARIF 2.1.0, and PR Markdown."""
    root = _git_ops.resolve_root(args.root)
    fmt = "json" if getattr(args, "json", False) else str(getattr(args, "format", "text")).lower()
    try:
        report = _audit.run_ci_architecture_check(
            root,
            base_rev=getattr(args, "base_rev", None),
            pool_bundle=getattr(args, "pool_bundle", None),
            forbid_spec=getattr(args, "forbid", None),
            use_default_rules=not bool(getattr(args, "no_default_rules", False)),
            check_cycles=not bool(getattr(args, "no_cycles", False)),
            check_layers=not bool(getattr(args, "no_layers", False)),
            check_static=not bool(getattr(args, "no_static", False)),
            check_drift=bool(getattr(args, "check_drift", False)),
            exclude_tests=bool(getattr(args, "exclude_tests", False)),
            max_bytes=getattr(args, "max_bytes", DEFAULT_MAX_BYTES),
        )
    except Exception as exc:
        return _cli_read._emit_cli_error(fmt == "json", str(exc), 2)

    if report.get("error"):
        return _cli_read._emit_cli_error(fmt == "json", str(report["error"]), 2, extra=report)

    if getattr(args, "sarif_output", None):
        sarif_path = _resolve_cli_file_path(root, args.sarif_output, must_exist=False)
        sarif_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(sarif_path, _ci_check.format_ci_sarif(report))
        report["sarif_output_path"] = str(sarif_path)

    if fmt == "json":
        rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    elif fmt == "sarif":
        rendered = _ci_check.format_ci_sarif(report)
    elif fmt == "github":
        rendered = _ci_check.format_ci_github_annotations(report)
    elif fmt == "markdown":
        rendered = _ci_check.format_ci_markdown(report)
    else:
        sm = report["summary"]
        st_lbl = "CLEAN" if report["ok"] else f"ISSUES ({report['total_findings']})"
        rendered = (
            f"[AOCI CI-Check] {st_lbl} | Mode={report['mode']} | Files={report['indexed_files']} | "
            f"LayerViolations={sm['layer_violations']} | Cycles={sm['cycles']} | "
            f"MissingStaticDeps={sm['missing_static_deps']} | Drift={sm['drift_issues']}\n"
        )

    if getattr(args, "output", None):
        out_p = _resolve_cli_file_path(root, args.output, must_exist=False)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(out_p, rendered)

    print(rendered, end="")
    return 1 if (getattr(args, "strict", False) and not report["ok"]) else 0
