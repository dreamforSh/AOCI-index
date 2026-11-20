"""Deterministic code volume rendering and directory shard synchronization for `aoci_index`."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .constants import (
    ENTRY_RE,
    SHARDS_DIR,
    SHARDS_INDEX,
    SHARDS_MANIFEST,
    atomic_write_text,
    utc_now_iso,
)
from .graph import estimate_tokens

_WIN_RESERVED_SHARD_NAMES: Set[str] = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def render_code_volume(entries: Dict[str, str], updated_at: Optional[str] = None) -> str:
    """Render `object-fras-v2` volume text deterministically grouped by directory banner."""
    grouped: Dict[str, List[Tuple[str, str]]] = {}
    for rel, line in entries.items():
        parent = rel.rsplit("/", 1)[0] + "/" if "/" in rel else "./"
        grouped.setdefault(parent, []).append((rel, line))

    out_lines: List[str] = [
        "#AOCI-CODE-VOLUME: 1",
        "#Format: object-fras-v2",
        f"#Updated-At: {updated_at or utc_now_iso()}",
        f"#Tracked-Files: {len(entries)}",
        "",
    ]

    sorted_dirs = sorted(grouped.keys(), key=lambda d: ("" if d == "./" else d))
    for d in sorted_dirs:
        out_lines.append(f"==={d}===")
        for _, line in sorted(grouped[d], key=lambda item: item[0]):
            out_lines.append(line)
        out_lines.append("")

    return "\n".join(out_lines)


def write_code_volume(code_path: Path, entries: Dict[str, str]) -> str:
    """Write `aoci.code.txt` deterministically grouped by directory banner and return rendered text."""
    rendered = render_code_volume(entries)
    atomic_write_text(code_path, rendered)
    return rendered


def shard_prefix_for_path(rel: str, depth: int = 1) -> str:
    """Return the exact directory prefix (`'./'` for root files, `'dir1/.../'` up to `depth`)."""
    if "/" not in rel:
        return "./"
    parts = rel.split("/")[:-1]
    chosen = parts[: max(1, depth)]
    return "/".join(chosen) + "/"


def _base_shard_slug(prefix: str) -> str:
    if prefix == "./":
        return "_root"
    segs = [s for s in prefix.strip("/").split("/") if s]
    cleaned_segs: List[str] = []
    for seg in segs:
        s = re.sub(r"[^\w.-]", "_", seg).strip(" .")
        if not s or s.lower() in _WIN_RESERVED_SHARD_NAMES:
            s = f"_{s}" if s else "_"
        cleaned_segs.append(s)
    slug = "__".join(cleaned_segs)
    if not slug.strip("_.") or slug.lower() == "_root" or len(slug) > 100:
        digest = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:8]
        head = slug[:60].strip("_.") or "shard"
        return f"{head}_{digest}"
    return slug


def allocate_shard_ids(prefixes: Sequence[str]) -> Dict[str, str]:
    """Assign a unique, case-collision-safe shard filename stem to each distinct directory prefix."""
    sorted_prefixes = sorted(set(prefixes), key=lambda p: ("" if p == "./" else p))
    raw_slugs = {p: _base_shard_slug(p) for p in sorted_prefixes}
    lower_counts: Dict[str, int] = {}
    for s in raw_slugs.values():
        low = s.lower()
        lower_counts[low] = lower_counts.get(low, 0) + 1

    assigned: Dict[str, str] = {}
    used_lower: Set[str] = set()
    for p in sorted_prefixes:
        slug = raw_slugs[p]
        if p != "./" and (lower_counts[slug.lower()] > 1 or slug.lower() in used_lower):
            digest = hashlib.sha256(p.encode("utf-8")).hexdigest()[:8]
            slug = f"{slug}_{digest}"
        used_lower.add(slug.lower())
        assigned[p] = slug
    return assigned


def shard_key_for_path(rel: str, depth: int = 1) -> Tuple[str, str]:
    """Return `(shard_id, dir_prefix)` for directory-sharded code volumes."""
    prefix = shard_prefix_for_path(rel, depth)
    return _base_shard_slug(prefix), prefix


def write_shards(
    aoci_dir: Path,
    entries: Dict[str, str],
    depth: int = 1,
    incremental: bool = False,
    prev_manifest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Split `aoci.code.txt` into per-directory `.aoci/shards/<id>.code.txt` slices + `INDEX.txt`.
    Groups strictly by full directory prefix first, then assigns unique shard filenames.
    When `incremental=True`, only dirty shard files whose entry digest changed are rewritten to disk.
    """
    shards_dir = aoci_dir / SHARDS_DIR
    shards_dir.mkdir(parents=True, exist_ok=True)
    depth = max(1, int(depth))
    now_iso = utc_now_iso()

    prev_by_prefix: Dict[str, Dict[str, Any]] = {}
    if incremental and isinstance(prev_manifest, dict) and prev_manifest.get("depth") == depth:
        for item in prev_manifest.get("shards", ()):
            if isinstance(item, dict) and isinstance(item.get("prefix"), str):
                prev_by_prefix[item["prefix"]] = item

    buckets_by_prefix: Dict[str, Dict[str, str]] = {}
    for rel, line in entries.items():
        prefix = shard_prefix_for_path(rel, depth)
        buckets_by_prefix.setdefault(prefix, {})[rel] = line

    sorted_prefixes = sorted(buckets_by_prefix.keys(), key=lambda p: ("" if p == "./" else p))
    prefix_to_sid = allocate_shard_ids(sorted_prefixes)

    expected_files: Set[str] = set()
    shard_meta: List[Dict[str, Any]] = []
    any_shard_changed = not incremental or (len(buckets_by_prefix) != len(prev_by_prefix))
    index_lines: List[str] = [
        "#AOCI-SHARD-INDEX: 1",
        f"#Depth: {depth}",
        f"#Updated-At: {now_iso}",
        f"#Total-Files: {len(entries)}",
        "",
    ]

    for prefix in sorted_prefixes:
        sid = prefix_to_sid[prefix]
        fname = f"{sid}.code.txt"
        expected_files.add(fname)
        shard_entries = buckets_by_prefix[prefix]
        shard_path = shards_dir / fname
        digest = hashlib.sha256(
            "\n".join(f"{k}\t{v}" for k, v in sorted(shard_entries.items())).encode("utf-8")
        ).hexdigest()[:16]
        prev_info = prev_by_prefix.get(prefix)
        if (
            incremental
            and prev_info is not None
            and prev_info.get("id") == sid
            and prev_info.get("digest") == digest
            and shard_path.is_file()
        ):
            tokens = int(prev_info.get("estimated_tokens", 1))
            hubs = int(prev_info.get("hubs", 0))
        else:
            any_shard_changed = True
            rendered = render_code_volume(shard_entries, updated_at=now_iso)
            atomic_write_text(shard_path, rendered)
            tokens = estimate_tokens(rendered)
            hubs = sum(
                1
                for ln in shard_entries.values()
                for m in (ENTRY_RE.match(ln),)
                if m and int(m.group("imp")) >= 8
            )
        info = {
            "id": sid,
            "prefix": prefix,
            "file": f"{SHARDS_DIR}/{fname}",
            "count": len(shard_entries),
            "hubs": hubs,
            "estimated_tokens": tokens,
            "digest": digest,
        }
        shard_meta.append(info)
        index_lines.append(
            f"{prefix} -> .aoci/{SHARDS_DIR}/{fname} "
            f"[files={len(shard_entries)}, hubs={hubs}, ~{tokens} tokens]"
        )

    for old in shards_dir.glob("*.code.txt"):
        if old.name not in expected_files:
            any_shard_changed = True
            try:
                old.unlink()
            except OSError:
                pass

    manifest = {
        "schema_version": 1,
        "depth": depth,
        "updated_at": now_iso,
        "total_files": len(entries),
        "shards": shard_meta,
    }
    if any_shard_changed or not (shards_dir / SHARDS_INDEX).is_file():
        atomic_write_text(shards_dir / SHARDS_INDEX, "\n".join(index_lines) + "\n")
        atomic_write_text(shards_dir / SHARDS_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def sync_shards_if_enabled(aoci_dir: Path, entries: Dict[str, str]) -> None:
    manifest_path = aoci_dir / SHARDS_DIR / SHARDS_MANIFEST
    if not manifest_path.exists():
        return
    depth = 1
    prev_data: Optional[Dict[str, Any]] = None
    try:
        prev_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(prev_data, dict):
            depth = max(1, int(prev_data.get("depth", 1)))
    except (OSError, ValueError):
        prev_data = None
    write_shards(aoci_dir, entries, depth=depth, incremental=True, prev_manifest=prev_data)
