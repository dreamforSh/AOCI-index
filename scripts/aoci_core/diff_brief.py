"""Architectural diff impact briefing (`compute_diff_briefing`) for working-tree and Git revision diffs."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import git_ops as _git_ops
from . import hubs as _hubs
from . import ignore as _ignore
from . import shards as _shards
from . import test_impact as _test_impact
from .constants import AOCI_DIR_NAME, CODE_FILE, DEFAULT_MAX_BYTES, normalize_relpath
from .graph import bfs_neighbors, build_fras_graph, parse_entries_map
from .templates import parse_manifest_field


def compute_diff_briefing(
    root: Path,
    rev: Optional[str] = None,
    staged: bool = False,
    depth: int = 1,
    max_bytes: int = DEFAULT_MAX_BYTES,
    cached_entries: Optional[Dict[str, str]] = None,
    cached_deps: Optional[Dict[str, Set[str]]] = None,
    cached_dependents: Optional[Dict[str, Set[str]]] = None,
    cached_invariants: Optional[str] = None,
    with_tests: bool = False,
    cached_parsed: Optional[Any] = None,
    cached_hub_records: Optional[Sequence[Dict[str, Any]]] = None,
    preloaded_outline_files: Optional[Dict[str, Any]] = None,
    live_test_imports: bool = False,
    test_index: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Build a focused architectural impact packet for Git working-tree or revision diffs:
    Global Invariants + changed FRAS entries + non-obvious S: traps + downstream R: deps + affected callers.
    """
    if rev is not None and str(rev).strip().startswith("-"):
        return {"ok": False, "error": f"Invalid Git revision `{rev}`: revision specs cannot start with `-`."}

    aoci_dir = root / AOCI_DIR_NAME
    code_path = aoci_dir / CODE_FILE
    if not code_path.exists():
        return {"ok": False, "error": f"`{code_path}` not found. Run `init` first."}

    git_info = _git_ops.detect_git_info(root)
    if not git_info.get("is_git"):
        return {"ok": False, "error": f"`{root}` is not inside a Git repository."}

    ignores = _ignore.load_ignore_matcher(root, git_info)
    if cached_entries is not None:
        entries = cached_entries
    else:
        entries, _ = _shards.parse_code_volume(code_path)
    invariants = (
        cached_invariants
        if cached_invariants is not None
        else (parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-")
    )

    toplevel: Path = git_info["toplevel"]
    try:
        rel_sub = root.resolve().relative_to(toplevel.resolve()).as_posix()
    except ValueError:
        rel_sub = ""
    sub_prefix = "" if rel_sub in ("", ".") else rel_sub.strip("/") + "/"

    def _to_root_rel(git_rel: str) -> Optional[str]:
        norm = normalize_relpath(git_rel)
        if not norm:
            return None
        if not sub_prefix:
            return norm
        if norm.startswith(sub_prefix):
            tail = norm[len(sub_prefix) :]
            return tail or None
        return None

    changed_active: Set[str] = set()
    deleted_paths: Set[str] = set()
    renamed_pairs: List[Dict[str, str]] = []

    if rev or staged:
        git_args = ["diff", "--name-status", "-M", "--relative", "-z"]
        if staged:
            git_args.append("--cached")
        if rev:
            git_args.append(rev)
        git_args.extend(["--", "."])
        res = _git_ops.run_git(git_args, root)
        if res.returncode != 0:
            return {"ok": False, "error": f"`git diff` failed: {res.stderr.strip()}"}
        toks = res.stdout.split("\0")
        idx = 0
        while idx < len(toks):
            st_tok = toks[idx]
            idx += 1
            if not st_tok or idx >= len(toks):
                continue
            p1 = normalize_relpath(toks[idx])
            idx += 1
            code_ch = st_tok[0].upper()
            if code_ch in ("R", "C") and idx < len(toks):
                p2 = normalize_relpath(toks[idx])
                idx += 1
                if code_ch == "R" and p1:
                    deleted_paths.add(p1)
                if p2:
                    changed_active.add(p2)
                    if code_ch == "R" and p1:
                        renamed_pairs.append({"from": p1, "to": p2})
            elif code_ch == "D":
                if p1:
                    deleted_paths.add(p1)
            else:
                if p1:
                    changed_active.add(p1)
    else:
        # Working tree + staged + untracked new files (`git status` always emits paths relative to `toplevel`)
        res = _git_ops.run_git(
            ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."], root
        )
        if res.returncode != 0:
            return {"ok": False, "error": f"`git status` failed: {res.stderr.strip()}"}
        toks = res.stdout.split("\0")
        idx = 0
        while idx < len(toks):
            rec = toks[idx]
            idx += 1
            if len(rec) < 4:
                continue
            xy = rec[:2]
            p1 = _to_root_rel(rec[3:])
            if ("R" in xy or "C" in xy) and idx < len(toks):
                old_p = _to_root_rel(toks[idx])
                idx += 1
                if "R" in xy and old_p and not (root / old_p).exists():
                    deleted_paths.add(old_p)
                if p1:
                    changed_active.add(p1)
                    if "R" in xy and old_p:
                        renamed_pairs.append({"from": old_p, "to": p1})
            elif "D" in xy and p1 and not (root / p1).exists():
                deleted_paths.add(p1)
            elif p1:
                changed_active.add(p1)

    eligible_changed: List[str] = []
    for p in sorted(changed_active):
        if _ignore.path_skip_reason(p, ignores) is not None:
            continue
        full = root / p
        try:
            lst = os.lstat(full)
        except OSError:
            lst = None
        if lst is not None:
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
                continue
            if lst.st_size > max_bytes:
                continue
            try:
                with full.open("rb") as fh:
                    head = fh.read(8192)
            except OSError:
                continue
            if _ignore.is_probably_binary_bytes(head):
                continue
        eligible_changed.append(p)

    parsed_map = cached_parsed if cached_parsed is not None else parse_entries_map(entries)
    indexed_rows: List[Tuple[int, str, str]] = []
    high_risk: List[Dict[str, Any]] = []
    unindexed_changed: List[str] = []
    for p in eligible_changed:
        line = entries.get(p)
        if line is None:
            unindexed_changed.append(p)
            continue
        pe = parsed_map.get(p)
        imp = pe.imp if pe is not None else 5
        indexed_rows.append((imp, p, line))
        if pe is not None and pe.s not in ("-", ""):
            high_risk.append({"path": p, "importance": imp, "s": pe.s})

    indexed_rows.sort(key=lambda row: (-row[0], row[1]))
    high_risk.sort(key=lambda item: (-int(item["importance"]), str(item["path"])))
    deleted_indexed = [entries[p] for p in sorted(deleted_paths) if p in entries]

    if cached_deps is not None and cached_dependents is not None:
        deps_adj, dependents = cached_deps, cached_dependents
    else:
        deps_adj, dependents = build_fras_graph(entries, parsed_entries=parsed_map)
    seed_paths = [p for _, p, _ in indexed_rows] + [p for p in sorted(deleted_paths) if p in entries]
    exclude_seeds = set(eligible_changed) | deleted_paths
    neighbor_paths = [nb for nb in bfs_neighbors(dependents, seed_paths, max(1, depth)) if nb not in exclude_seeds]
    neighbor_lines = [entries[nb] for nb in sorted(neighbor_paths) if nb in entries]

    dep_paths = [
        dp
        for dp in bfs_neighbors(deps_adj, [p for _, p, _ in indexed_rows], 1)
        if dp not in exclude_seeds and dp in entries
    ]
    dep_constraints: List[Dict[str, Any]] = []
    for dp in dep_paths:
        dpe = parsed_map.get(dp)
        if dpe is not None and dpe.s not in ("-", ""):
            dep_constraints.append({"path": dp, "importance": dpe.imp, "s": dpe.s})
    dep_constraints.sort(key=lambda item: (-int(item["importance"]), str(item["path"])))

    hub_secs = _hubs.lookup_hub_sections(
        aoci_dir,
        [p for _, p, _ in indexed_rows],
        preloaded_records=cached_hub_records,
    )

    brief: Dict[str, Any] = {
        "ok": True,
        "root": str(root),
        "scope": rev or ("--staged" if staged else "working-tree"),
        "global_invariants": invariants,
        "renames": renamed_pairs,
        "changed_entries": [ln for _, _, ln in indexed_rows],
        "high_risk_constraints": high_risk,
        "dependency_constraints": dep_constraints,
        "unindexed_changed": unindexed_changed,
        "deleted_entries": deleted_indexed,
        "affected_neighbors": neighbor_lines,
    }
    if hub_secs:
        brief["hub_sections"] = hub_secs
    if with_tests:
        brief["impacted_tests"] = _test_impact.find_impacted_tests(
            root,
            entries,
            dependents,
            seed_paths,
            max_depth=max(3, int(depth)),
            parsed_entries=parsed_map,
            preloaded_outline_files=preloaded_outline_files,
            live_imports=live_test_imports,
            test_index=test_index,
        )
    return brief
