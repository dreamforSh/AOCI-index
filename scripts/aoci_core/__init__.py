"""
Internal modular core package for `aoci_index` (zero external dependencies, Python 3.8+).

Flat modules with an acyclic import graph, roughly from the bottom up:
  constants
  -> templates, batches, lock, ignore, githooks, parsers (+ optional ts_accel), graph, shards, hubs, pool
  -> git_ops -> discovery -> drift
  -> feature modules: write_ops, diff_brief, audit, freshness, hybrid, query_ops, pack, symbols, refs,
     test_impact, cochange, doctor, sync_prep, apply_sync, batch_pack, ci_check, evalbench
  -> cli_read / cli_write / cli_check, whose `cmd_*` handlers `cli_parser` imports lazily on dispatch.
`installer` depends only on the standard library. No module here imports `aoci_index`, `aoci_mcp`, or
`aoci_hook`; those entry points reach the package through the `aoci_index` facade.
"""

from __future__ import annotations

from .constants import AOCI_VERSION as __version__  # noqa: F401
