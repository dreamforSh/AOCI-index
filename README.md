# AOCI Codebase Fast Index (`aoci-index`)

Zero-dependency AI-Oriented Codebase Indexing (`.aoci/`) builder, BM25/FRAS graph reader, dual-host lifecycle guard, and stdio MCP server for **Claude Code** and **Antigravity**. Runs on Python 3.8+ standard library only (with optional Git acceleration).

## Key Features

- **Single-Pass Codebase Map (`.aoci/aoci.code.txt`)**: Dense `object-fras-v2` lines (`<relpath>[LDIS]: F:... | R:... | A:... | S:...`) paired with `#Global-Invariants:` in `aoci.txt` and `[LDIS]` / `#Glossary:` rules in `aoci.meta.txt`.
- **Fast Drift & Rename Synchronization**: LF-normalized SHA-256 + mtime fingerprinting (`baseline.json`), `sig_hash` classification (`impl_only` vs `contract_changed`), exact and line-sketch similar rename migration, and zero-token restore from a SQLite WAL semantic pool under `<git-common-dir>/aoci-cache`.
- **In-Memory Stdio MCP Server (`scripts/aoci_mcp.py`)**: Serves 21 MCP tools, 5 static resources, 2 URI templates (`aoci://shards/{shard}`, `aoci://{volume}?root={root}`), and 3 workflow prompts over newline-delimited JSON-RPC 2.0 or `Content-Length` framing.
- **Task-Driven Context & H-Tier Symbol Zoom**: `aoci pack` / `aoci_pack` assembles budgeted context packets (`BM25` + path-mention + diff seeds, graph neighborhood, `S:` constraints, `open_next` symbol anchors), `--with-tests` / `--tests-for` derives impacted `T`-layer tests, and `aoci zoom` / `aoci_zoom` maintains symbol-level line spans in `.aoci/aoci.hubs.txt`.
- **Architectural Audit & Export**: Cross-checks FRAS edges against static imports (Python, JS/TS/Vue/Svelte, Go package-level, Rust), detects Tarjan SCC dependency cycles, enforces `#Layer-Rules:`, and exports Mermaid / Graphviz DOT graphs.
- **Dual-Host Lifecycle Hooks (`scripts/aoci_hook.py`)**: `SessionStart`, optional `UserPromptSubmit`/`PreInvocation` turn refresh, and session-scoped convergent `Stop` gate.
- **Local Git Exclusion**: Keeps `.aoci/` excluded via `.git/info/exclude` without dirtying tracked `.gitignore`.

## Architecture

```text
scripts/
├── aoci_index.py        # CLI & core orchestration (22 commands + `status` alias)
├── aoci_mcp.py          # Stdio JSON-RPC 2.0 MCP server (21 tools, 5 resources, 2 templates, 3 prompts)
├── aoci_hook.py         # Claude Code & Antigravity lifecycle hooks
├── install.py           # Cross-host installer / uninstaller & plugin bundler
└── aoci_core/
    ├── constants.py     # Tag sets, regexes, path normalization, atomic I/O
    ├── parsers.py       # Static outlines, sig_hash, import graph & Go package resolution
    ├── graph.py         # ParsedEntry, FRAS graph, context budgeting, lint, BM25, audit, export
    ├── pack.py          # Task-driven token-budgeted context packer (`aoci pack` / `aoci_pack`)
    ├── test_impact.py   # Impacted test derivation (`--with-tests` / `--tests-for`)
    ├── hubs.py          # H-tier symbol sub-volume `.aoci/aoci.hubs.txt` (`aoci zoom` / `aoci_zoom`)
    ├── pool.py          # SQLite WAL semantic pool cache & portable .json(.gz) export/import
    └── githooks.py      # Idempotent post-checkout/post-merge/post-rewrite/pre-commit hooks
```

## Installation

### Claude Code

```bash
# Global install (skill + lifecycle hooks + optional MCP server & CLAUDE.md snippet)
python scripts/install.py --host claude-code --scope global --with-hooks --with-mcp

# Project-scoped install
python scripts/install.py --host claude-code --scope project --project-root <repo> --with-hooks --with-rules --with-mcp
```

### Antigravity

```bash
# Global Antigravity plugin bundle (skill + plugin.json + hooks + rules + MCP)
python scripts/install.py --host antigravity --scope global --as-plugin --with-hooks --with-rules --with-mcp

# Install for both hosts at once (or pass --uninstall to remove cleanly)
python scripts/install.py --host both --scope global --with-hooks --with-mcp
```

See [references/hooks-and-install.md](./references/hooks-and-install.md) for manual configuration, `--dry-run`, and `--uninstall` options.

## CLI & MCP Quick Reference

| Action | CLI (`python scripts/aoci_index.py ...`) | MCP Tool (`aoci_mcp.py`) |
| :--- | :--- | :--- |
| Initialize `.aoci/` | `init --root <repo>` | `aoci_init` |
| Digest & core hubs | `overview --root <repo>` | `aoci_overview` |
| Search / impact / tests | `query --root <repo> --search "<terms>"` / `--tests-for <path>` | `aoci_query` (`tests_for`) |
| Dependency subgraph | `context --root <repo> <path> --depth 1 [--with-tests]` | `aoci_context` (`with_tests`) |
| Task-driven context pack | `pack --root <repo> --task "<description>" [--budget 2500]` | `aoci_pack` |
| Diff impact briefing | `diff [rev] --root <repo> [--with-tests]` | `aoci_diff` (`with_tests`) |
| Drift & batch plan | `scan --root <repo> --batch-size 20` | `aoci_scan` |
| Static outline / scaffold | `outline` / `scaffold --root <repo> --batch 1` | `aoci_outline` / `aoci_scaffold` |
| H-tier symbol sub-volume | `zoom <path> --root <repo>` / `--scaffold` / `--upsert` | `aoci_zoom` |
| Update / acknowledge | `upsert` / `ack --root <repo> <paths...>` | `aoci_upsert` / `aoci_ack` |
| Renames & pool restore | `migrate-renames` / `restore --root <repo>` | `aoci_migrate_renames` / `aoci_restore` |
| Quality & architecture | `verify` / `lint` / `audit --root <repo>` | `aoci_verify` / `aoci_lint` / `aoci_audit` |
| Graph export & shards | `export --format mermaid\|dot` / `shard` | `aoci_export` / `aoci_shard` |
| Pool & Git hooks | `pool stats\|export\|import` / `install-git-hooks` | `aoci_pool` / `aoci_ensure_git_exclude` |

## Testing

```bash
python -B tests/test_aoci.py
python -B -m unittest discover -s tests -p "test_*.py"
```
