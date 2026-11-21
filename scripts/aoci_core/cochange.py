"""Git history co-change mining for undocumented architectural coupling (`aoci audit --co-change`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .constants import normalize_relpath
from .graph import parse_entry

_COMMIT_MARKER = "__AOCI_COMMIT__:"


def find_cochange_unlinked_pairs(
    root: Path,
    git_info: Dict[str, Any],
    entries: Mapping[str, str],
    deps_adj: Mapping[str, Set[str]],
    dependents_adj: Mapping[str, Set[str]],
    run_git_fn: Any,
    *,
    max_commits: int = 200,
    min_co_changes: int = 3,
    min_confidence: float = 0.6,
    max_commit_files: int = 30,
    parsed_entries: Optional[Mapping[str, Any]] = None,
    exclude_tests: bool = False,
) -> List[Dict[str, Any]]:
    """
    Mine recent non-merge Git commits for pairs of indexed files that frequently change together
    without any explicit FRAS `R:` or `A:` edge linking them.
    """
    if not git_info.get("is_git") or len(entries) < 2:
        return []

    excluded_set: Set[str] = set()
    if exclude_tests:
        for rel, line in entries.items():
            pe = parsed_entries.get(rel) if parsed_entries is not None else parse_entry(rel, line)
            if pe is not None and getattr(pe, "layer", "") == "T":
                excluded_set.add(rel)

    n_commits = max(1, int(max_commits))
    min_co = max(1, int(min_co_changes))
    min_conf = float(min_confidence)

    cp = run_git_fn(
        [
            "-c",
            "core.quotePath=false",
            "log",
            "--no-merges",
            f"-n{n_commits}",
            f"--pretty=format:{_COMMIT_MARKER}%H",
            "--name-only",
            "--relative",
        ],
        root,
    )
    if cp.returncode != 0 or not cp.stdout:
        return []

    commits_raw: List[Tuple[Set[str], List[str]]] = []

    raw_set: Set[str] = set()
    idx_set: Set[str] = set()
    in_commit = False

    def _flush() -> None:
        if in_commit and len(raw_set) <= max_commit_files and len(idx_set) >= 1:
            commits_raw.append((set(raw_set), sorted(idx_set)))

    for raw_ln in cp.stdout.splitlines():
        ln = raw_ln.strip()
        if ln.startswith(_COMMIT_MARKER):
            _flush()
            raw_set.clear()
            idx_set.clear()
            in_commit = True
            continue
        if not ln or not in_commit:
            continue
        norm = normalize_relpath(ln)
        if not norm:
            continue
        raw_set.add(norm)
        if norm in entries and norm not in excluded_set:
            idx_set.add(norm)
    _flush()

    file_commits: Dict[str, int] = {}
    pair_commits: Dict[Tuple[str, str], int] = {}
    for _, files in commits_raw:
        for p in files:
            file_commits[p] = file_commits.get(p, 0) + 1
        n_files = len(files)
        for i in range(n_files):
            fa = files[i]
            for j in range(i + 1, n_files):
                fb = files[j]
                pair_commits[(fa, fb)] = pair_commits.get((fa, fb), 0) + 1

    undir_cache: Dict[str, Set[str]] = {}

    def _undir(rel: str) -> Set[str]:
        s = undir_cache.get(rel)
        if s is None:
            s = set(deps_adj.get(rel, ())) | set(dependents_adj.get(rel, ()))
            s.discard(rel)
            undir_cache[rel] = s
        return s

    out: List[Dict[str, Any]] = []
    for (fa, fb), co_cnt in pair_commits.items():
        if co_cnt < min_co:
            continue
        if (
            fb in deps_adj.get(fa, ())
            or fa in deps_adj.get(fb, ())
            or fb in dependents_adj.get(fa, ())
            or fa in dependents_adj.get(fb, ())
        ):
            continue
        ca = file_commits.get(fa, 0)
        cb = file_commits.get(fb, 0)
        if ca <= 0 or cb <= 0:
            continue
        conf_a = co_cnt / ca
        conf_b = co_cnt / cb
        conf = max(conf_a, conf_b)
        if conf + 1e-12 < min_conf:
            continue
        union_cnt = ca + cb - co_cnt
        jaccard = (co_cnt / union_cnt) if union_cnt > 0 else 0.0
        is_two_hop = bool(_undir(fa) & _undir(fb))
        out.append(
            {
                "file_a": fa,
                "file_b": fb,
                "co_changes": co_cnt,
                "commits_a": ca,
                "commits_b": cb,
                "confidence": round(conf, 3),
                "confidence_a_to_b": round(conf_a, 3),
                "confidence_b_to_a": round(conf_b, 3),
                "jaccard": round(jaccard, 3),
                "is_two_hop": is_two_hop,
            }
        )

    out.sort(key=lambda r: (-int(r["co_changes"]), -float(r["confidence"]), r["file_a"], r["file_b"]))
    return out[:50]
