"""Retrieval & context-pack evaluation harness (`eval`): recall@k, precision@k, MRR, seed/pack recall per task case."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import graph as _graph
from . import pack as _pack
from . import shards as _shards
from .constants import AOCI_DIR_NAME, CODE_FILE, normalize_relpath
from .templates import parse_declared_domains, parse_manifest_field

EVAL_MODES = ("bm25", "hybrid")
_RANK_DEPTH = 20  # MRR / "found at" look this deep into the ranking regardless of `k`


class EvalCaseError(ValueError):
    """Raised for malformed evaluation case files (bad JSON, missing `task` / `expected`)."""


def load_eval_cases(path: Path) -> List[Dict[str, Any]]:
    """
    Load task cases from JSON Lines (`.jsonl`, one object per line, `#` comments allowed) or JSON
    (a list, or `{"cases": [...]}`). Each case needs `task` (str) and `expected` (non-empty list of
    repo-relative paths); `id` and `tags` are optional.
    """
    raw = path.read_text(encoding="utf-8-sig")
    items: List[Any] = []
    stripped = raw.lstrip()
    if path.suffix.lower() == ".jsonl" or not stripped.startswith(("[", "{")):
        for lineno, line in enumerate(raw.splitlines(), start=1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                items.append(json.loads(s))
            except ValueError as exc:
                raise EvalCaseError(f"{path.name}:{lineno}: invalid JSON ({exc})") from exc
    else:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise EvalCaseError(f"{path.name}: invalid JSON ({exc})") from exc
        items = data.get("cases", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise EvalCaseError(f"{path.name}: expected a JSON list or an object with a `cases` list")

    cases: List[Dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise EvalCaseError(f"{path.name}: case #{idx} is not an object")
        task = item.get("task")
        expected = item.get("expected")
        if not isinstance(task, str) or not task.strip():
            raise EvalCaseError(f"{path.name}: case #{idx} has no `task` string")
        if isinstance(expected, str):
            expected = [expected]
        if not isinstance(expected, list) or not expected or not all(isinstance(e, str) for e in expected):
            raise EvalCaseError(f"{path.name}: case #{idx} needs a non-empty `expected` list of paths")
        cases.append(
            {
                "id": str(item.get("id") or f"case-{idx}"),
                "task": task.strip(),
                "expected": list(dict.fromkeys(normalize_relpath(e) for e in expected)),
                "tags": [str(t) for t in item.get("tags", [])] if isinstance(item.get("tags"), list) else [],
            }
        )
    if not cases:
        raise EvalCaseError(f"{path.name}: no cases found")
    return cases


def _load_index(root: Path) -> Dict[str, Any]:
    aoci_dir = root / AOCI_DIR_NAME
    entries, _ = _shards.parse_code_volume(aoci_dir / CODE_FILE)
    parsed = _graph.parse_entries_map(entries)
    deps, dependents = _graph.build_fras_graph(entries, parsed_entries=parsed)
    return {
        "entries": entries,
        "parsed": parsed,
        "deps": deps,
        "dependents": dependents,
        "invariants": parse_manifest_field(aoci_dir, "#Global-Invariants:") or "-",
        "domains": parse_declared_domains(aoci_dir),
        "glossary": _graph.parse_glossary(aoci_dir),
    }


def build_retrieval_index(
    mode: str,
    entries: Dict[str, str],
    parsed: Mapping[str, Any],
    root: Optional[Path] = None,
    hybrid_options: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Return an object exposing `search_detailed(query, candidate_paths=, top_k=, glossary=)` for `mode`."""
    if mode == "bm25":
        return _graph.BM25Index(entries, parsed_entries=parsed)
    if mode == "hybrid":
        from . import hybrid as _hybrid

        return _hybrid.HybridIndex(entries, parsed_entries=parsed, root=root, **dict(hybrid_options or {}))
    raise ValueError(f"Unknown eval mode `{mode}` (expected one of: {', '.join(EVAL_MODES)})")


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def evaluate_cases(
    root: Path,
    cases: Sequence[Mapping[str, Any]],
    *,
    mode: str = "bm25",
    k: int = 8,
    budget: int = 2500,
    depth: int = 1,
    top_seeds: int = 8,
    hybrid_options: Optional[Mapping[str, Any]] = None,
    index_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Score `cases` against the `.aoci/` index at `root` with one retrieval `mode`.

    Ranking metrics use the raw retrieval order (`recall@k`, `precision@k`, `MRR` over the top 20).
    Pack metrics run the real `build_task_pack` (no working-tree diff, so results are reproducible) and
    count expected files that land in seeds (`seed_recall`) or anywhere in the packet (`pack_recall`).
    Expected paths that are not indexed at all are reported as `unknown_expected` and excluded.
    """
    k = max(1, int(k))
    st = dict(index_state) if index_state is not None else _load_index(root)
    entries: Dict[str, str] = st["entries"]
    parsed = st["parsed"]
    retriever = build_retrieval_index(mode, entries, parsed, root=root, hybrid_options=hybrid_options)
    line_to_rel = {line: rel for rel, line in entries.items()}
    mention_index = _pack.PathMentionIndex(sorted(entries.keys()))

    rows: List[Dict[str, Any]] = []
    for case in cases:
        expected_all = [normalize_relpath(e) for e in case["expected"]]
        expected = [e for e in expected_all if e in entries]
        unknown = [e for e in expected_all if e not in entries]
        hits, _ = retriever.search_detailed(case["task"], top_k=max(k, _RANK_DEPTH), glossary=st["glossary"])
        ranked = [str(h["path"]) for h in hits]
        exp_set: Set[str] = set(expected)
        top_k_paths = ranked[:k]
        hits_at_k = [p for p in top_k_paths if p in exp_set]
        first_rank = next((i + 1 for i, p in enumerate(ranked[:_RANK_DEPTH]) if p in exp_set), None)

        pack_obj = _pack.build_task_pack(
            root,
            entries,
            st["deps"],
            st["dependents"],
            st["invariants"],
            st["domains"],
            st["glossary"],
            task=case["task"],
            diff_brief=None,
            depth=depth,
            budget=budget,
            top_seeds=top_seeds,
            bm25_index=retriever,
            parsed_entries=parsed,
            mention_index=mention_index,
            check_stale=False,
        )
        seed_paths = [str(s["path"]) for s in pack_obj["seeds"]]
        packed: Set[str] = set(seed_paths)
        for line in (*pack_obj["dependencies"], *pack_obj["callers"]):
            rel = line_to_rel.get(line)
            if rel is not None:
                packed.add(rel)

        denom = len(expected)
        row: Dict[str, Any] = {
            "id": case["id"],
            "task": case["task"],
            "expected": expected,
            "recall_at_k": round(len(hits_at_k) / denom, 4) if denom else None,
            "precision_at_k": round(len(hits_at_k) / k, 4) if denom else None,
            "mrr": round(1.0 / first_rank, 4) if first_rank else 0.0,
            "first_rank": first_rank,
            "seed_recall": round(len(exp_set & set(seed_paths)) / denom, 4) if denom else None,
            "pack_recall": round(len(exp_set & packed) / denom, 4) if denom else None,
            "pack_tokens": int(pack_obj["estimated_tokens"]),
            "missing_at_k": [e for e in expected if e not in top_k_paths],
            "missing_in_pack": [e for e in expected if e not in packed],
            "top_k": top_k_paths,
        }
        if unknown:
            row["unknown_expected"] = unknown
        if case.get("tags"):
            row["tags"] = list(case["tags"])
        rows.append(row)

    scored = [r for r in rows if r["recall_at_k"] is not None]
    metrics = {
        "recall_at_k": _mean([r["recall_at_k"] for r in scored]),
        "precision_at_k": _mean([r["precision_at_k"] for r in scored]),
        "mrr": _mean([r["mrr"] for r in scored]),
        "seed_recall": _mean([r["seed_recall"] for r in scored]),
        "pack_recall": _mean([r["pack_recall"] for r in scored]),
        "avg_pack_tokens": round(sum(r["pack_tokens"] for r in scored) / len(scored)) if scored else 0,
    }
    out: Dict[str, Any] = {
        "mode": mode,
        "k": k,
        "budget": budget,
        "depth": depth,
        "cases": len(rows),
        "scored_cases": len(scored),
        "metrics": metrics,
        "results": rows,
    }
    channel_info = getattr(retriever, "channel_info", None)
    if callable(channel_info):
        out["channels"] = channel_info()
    return out


def run_eval(
    root: Path,
    cases: Sequence[Mapping[str, Any]],
    *,
    modes: Sequence[str] = ("bm25",),
    k: int = 8,
    budget: int = 2500,
    depth: int = 1,
    top_seeds: int = 8,
    hybrid_options: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate every mode in `modes` over the same loaded index and return per-mode reports."""
    index_state = _load_index(root)
    reports = [
        evaluate_cases(
            root,
            cases,
            mode=m,
            k=k,
            budget=budget,
            depth=depth,
            top_seeds=top_seeds,
            hybrid_options=hybrid_options,
            index_state=index_state,
        )
        for m in modes
    ]
    return {"ok": True, "root": str(root), "indexed_files": len(index_state["entries"]), "reports": reports}


def threshold_failures(
    result: Mapping[str, Any],
    min_recall: Optional[float] = None,
    min_pack_recall: Optional[float] = None,
) -> List[str]:
    """Return human-readable gate failures (`--min-recall` on recall@k, `--min-pack-recall`)."""
    failures: List[str] = []
    for rep in result.get("reports", ()):
        m = rep["metrics"]
        if min_recall is not None and m["recall_at_k"] < min_recall:
            failures.append(f"{rep['mode']}: recall@{rep['k']}={m['recall_at_k']:.3f} < {min_recall:.3f}")
        if min_pack_recall is not None and m["pack_recall"] < min_pack_recall:
            failures.append(f"{rep['mode']}: pack_recall={m['pack_recall']:.3f} < {min_pack_recall:.3f}")
    return failures


def format_eval_text(result: Mapping[str, Any], max_misses: int = 12) -> str:
    lines: List[str] = []
    for rep in result.get("reports", ()):
        m = rep["metrics"]
        lines.append(
            f"# AOCI Eval [{rep['mode']}]: {rep['scored_cases']}/{rep['cases']} cases | "
            f"k={rep['k']} | budget={rep['budget']} | depth={rep['depth']}"
        )
        lines.append(
            f"recall@{rep['k']}={m['recall_at_k']:.3f}  precision@{rep['k']}={m['precision_at_k']:.3f}  "
            f"MRR={m['mrr']:.3f}  seed_recall={m['seed_recall']:.3f}  pack_recall={m['pack_recall']:.3f}  "
            f"avg_pack_tokens={m['avg_pack_tokens']}"
        )
        if rep.get("channels"):
            lines.append(f"channels: {', '.join(rep['channels'])}")
        misses: List[Tuple[str, str, List[str]]] = [
            (r["id"], r["task"], r["missing_in_pack"]) for r in rep["results"] if r.get("missing_in_pack")
        ]
        unknown = [(r["id"], r["unknown_expected"]) for r in rep["results"] if r.get("unknown_expected")]
        if misses:
            lines.append(f"## Missing from pack ({len(misses)})")
            for cid, task, miss in misses[:max_misses]:
                lines.append(f"  - [{cid}] {task[:70]} -> {', '.join(miss)}")
            if len(misses) > max_misses:
                lines.append(f"  ... (+{len(misses) - max_misses} more; use --json)")
        if unknown:
            lines.append(f"## Expected paths not in index ({len(unknown)})")
            for cid, paths in unknown[:max_misses]:
                lines.append(f"  ? [{cid}] {', '.join(paths)}")
        lines.append("")
    reports = list(result.get("reports", ()))
    if len(reports) > 1:
        base = reports[0]["metrics"]
        lines.append("## Mode comparison (delta vs first mode)")
        for rep in reports[1:]:
            m = rep["metrics"]
            deltas = ", ".join(
                f"{key}={m[key] - base[key]:+.3f}" for key in ("recall_at_k", "mrr", "seed_recall", "pack_recall")
            )
            lines.append(f"  {rep['mode']} vs {reports[0]['mode']}: {deltas}")
    return "\n".join(lines).rstrip() + "\n"
