# AOCI Codebase Fast Index (`aoci-index`)

![Version 1.4.0](https://img.shields.io/badge/version-1.4.0-0ea5e9)
![Python 3.8+ Zero Deps](https://img.shields.io/badge/python-3.8%2B%20%7C%20zero--deps-3776ab)
![MCP 2024-11-05 · 2025-03-26 · 2025-06-18](https://img.shields.io/badge/MCP-2024--11--05%20%C2%B7%202025--03--26%20%C2%B7%202025--06--18-6366f1)
![Hosts: Claude Code · Antigravity](https://img.shields.io/badge/hosts-Claude%20Code%20%C2%B7%20Antigravity-10b981)

A zero-dependency Python 3.8+ **AI-Oriented Codebase Indexing** (`.aoci/`) engine, BM25/FRAS graph reader, dual-host lifecycle guard, and stdio MCP server for **Claude Code** and **Antigravity**. A single read pass gives coding agents repository-wide architectural orientation, cross-file dependencies, and non-obvious invariants while keeping `.aoci/` strictly local via `.git/info/exclude`.

## What `.aoci/` Looks Like

```text
# aoci.txt (Root Manifest)
#Global-Invariants: All write HTTP endpoints require JWT auth middleware; monetary values use int64 cents

# aoci.code.txt (object-fras-v2 — one dense line per managed file)
src/auth/jwt.py[MA8M]: F:Issues and verifies JWT access tokens with multi-KID key rotation | R:src/config/env.py | A:issue_token/verify_token<-src/routes/orders.py | S:Enforce explicit alg allowlist rejecting none; clock_skew fixed at 30s; keep previous KID active >=24h
src/order/service.py[AO9M]: F:Orchestrates idempotent order creation and stock reservation | R:src/config/env.py,src/order/repo.py | A:create_order/cancel_order<-src/routes/orders.py | S:Unique idempotency_key for retries; never invoke synchronous HTTP inside a DB transaction

# aoci.hubs.txt (Optional H-tier symbol zoom spans)
src/order/service.py#create_order[L42-L118]: F:Validates cents payload, reserves inventory row-locked, and records order atomically | S:Must hold SELECT FOR UPDATE before payment intent creation
```

- **`[LDIS]`**: `L` Layer (`E A D K P I M U C R T O`) · `D` Domain (`[A-Z0-9]`) · `I` Importance (`1`-`9`) · `S` Size tier (`T/S/M/L/H`, auto-verified from line count).
- **`F/R/A/S`**: `F:` Responsibility · `R:` Read-along dependencies · `A:` Exports & `<-callers` · `S:` Non-obvious stability, ordering, or security traps.

## Key Features

- **Single-Pass Codebase Map (`object-fras-v2`)**: Dense one-line-per-file codebase map in `.aoci/aoci.code.txt`, paired with `#Global-Invariants:` (`aoci.txt`), `[LDIS]` coordinates and bilingual `#Glossary:` aliases (`aoci.meta.txt`), and auto-synced directory shards (`shard`) for 10k+ file monorepos.
- **Task-Driven Context Packing & Test Impact**: `pack` / `aoci_pack` builds token-budgeted context packets from BM25, glossary expansion, explicit path mentions, and working-tree Git diffs (`R:`/`A:` graph neighborhood, `S:` traps, and `open_next` symbol anchors); `--with-tests` and `--tests-for` derive impacted `T`-layer tests via FRAS edges, static outlines, and naming conventions.
- **H-Tier Symbol Zoom (`aoci.hubs.txt`)**: `zoom` / `aoci_zoom` extracts and verifies `[Lstart-Lend]` symbol-level sections for large (`>= 1000`-line) hub files without bloating `aoci.code.txt`.
- **Sub-Second Drift, Rename & Branch Sync**: LF-normalized SHA-256 + `(mtime, size)` baseline (`baseline.json`, schema v2), `sig_hash` interface classification (`impl_only` vs `contract_changed`), exact and MinHash moved+edited rename migration (`migrate-renames`), and zero-token `restore` from a cross-worktree SQLite WAL semantic pool (`<git-common-dir>/aoci-cache`).
- **Warm In-Memory Stdio MCP Server (`aoci_mcp.py`)**: Serves **21 annotated tools**, **5 static resources**, **2 URI templates**, and **3 workflow prompts** over newline-delimited JSON-RPC 2.0 or `Content-Length` framing.
- **Architectural Audit & Graph Export**: Cross-checks FRAS edges against static imports (Python, JS/TS/Vue/Svelte, Go package-level, Rust), detects Tarjan SCC dependency cycles, enforces `#Layer-Rules:`, and exports Mermaid or Graphviz DOT graphs.
- **Dual-Host Lifecycle Guardrails & Local Git Exclusion**: `SessionStart`, optional `UserPromptSubmit`/`PreInvocation` turn-start snapshot refresh, and session-scoped convergent `Stop` gate for **Claude Code** and **Antigravity**, keeping `.aoci/` excluded in `.git/info/exclude` without dirtying tracked `.gitignore`.

## Quick Start

```bash
# 1. Initialize .aoci/ and enforce local Git exclusion
python scripts/aoci_index.py init --root <repo>

# 2. Scan for unindexed files and scaffold a line-balanced batch (batch_01.txt)
python scripts/aoci_index.py scan --root <repo> --batch-size 20
python scripts/aoci_index.py scaffold --root <repo> --batch 1 --output .aoci/pending/batch_01.txt

# 3. After reading the source files and filling <TODO> F:/S: fields, merge the batch
python scripts/aoci_index.py upsert --root <repo> --entries-file .aoci/pending/batch_01.txt

# 4. Pack task-scoped context, preview diff + test impact, and verify index health
python scripts/aoci_index.py pack --root <repo> --task "Fix JWT rotation skew" --budget 2500
python scripts/aoci_index.py diff --root <repo> --with-tests
python scripts/aoci_index.py verify --root <repo> && python scripts/aoci_index.py lint --root <repo>
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

# Install for both hosts at once (pass --dry-run to preview, or --uninstall to remove cleanly)
python scripts/install.py --host both --scope global --with-hooks --with-mcp
```

### Optional Editable CLI Package

```bash
pip install -e .
aoci-index --version   # aoci-index 1.4.0
aoci-mcp               # launches the stdio MCP server
```

See [references/hooks-and-install.md](./references/hooks-and-install.md) for manual setup, `--keep-config`, and hook contract verification.

## Architecture & `.aoci/` Layout

```text
scripts/
├── aoci_index.py        # CLI & core orchestration (22 commands + `status` alias)
├── aoci_mcp.py          # Stdio JSON-RPC 2.0 MCP server (21 tools, 5 resources, 2 templates, 3 prompts)
├── aoci_hook.py         # Claude Code & Antigravity lifecycle hooks
├── install.py           # Cross-host installer / uninstaller & plugin bundler
└── aoci_core/
    ├── constants.py     # Version constant, tag sets, regexes, path normalization, atomic I/O
    ├── parsers.py       # Static outlines, sig_hash, import graph & Go package resolution
    ├── graph.py         # ParsedEntry, FRAS graph, context budgeting, lint, BM25, audit, export
    ├── pack.py          # Task-driven token-budgeted context packer (`pack` / `aoci_pack`)
    ├── test_impact.py   # Impacted test derivation (`--with-tests` / `--tests-for`)
    ├── hubs.py          # H-tier symbol sub-volume `.aoci/aoci.hubs.txt` (`zoom` / `aoci_zoom`)
    ├── pool.py          # SQLite WAL semantic pool cache & portable .json(.gz) export/import
    └── githooks.py      # Idempotent post-checkout/post-merge/post-rewrite/pre-commit hooks
```

```text
<repo>/.aoci/
├── aoci.txt             # Root manifest: #Project, #Global-Invariants, volume layout, #Read-Order
├── aoci.meta.txt        # Meta volume: [LDIS] tag dictionary, bilingual #Glossary, FRAS rules
├── aoci.code.txt        # Code volume: object-fras-v2 dense single-line map
├── aoci.hubs.txt        # Optional H-tier symbol sub-volume (`zoom`)
├── baseline.json        # Compact LF-normalized SHA-256, sig_hash, sketch & (mtime, size) baseline
├── .aociignore          # Optional extra ignore globs (full .gitignore syntax)
├── shards/              # Optional per-directory `.code.txt` slices & `INDEX.txt` (`shard`)
├── pending/             # Scratch directory for batch entry files (never indexed or committed)
├── .sessions/           # Hook-managed per-session drift snapshots
└── .cache/              # Git exclusion, Git discovery, outline, scan_fps & batch_plan caches
```

## CLI & MCP Reference

| Action | CLI (`python scripts/aoci_index.py ...`) | MCP Tool (`scripts/aoci_mcp.py`) |
| :--- | :--- | :--- |
| Initialize `.aoci/` | `init --root <repo>` | `aoci_init` |
| Digest & core hubs | `overview --root <repo>` | `aoci_overview` |
| Search / impact / tests | `query --root <repo> --search "<terms>"` / `--tests-for <path>` | `aoci_query` (`tests_for`) |
| Dependency subgraph | `context <path> --root <repo> --depth 1 [--with-tests]` | `aoci_context` (`with_tests`) |
| Task-driven context pack | `pack --task "<description>" --root <repo> [--budget 2500]` | `aoci_pack` |
| Diff impact briefing | `diff [rev] --root <repo> [--with-tests]` | `aoci_diff` (`with_tests`) |
| Drift & batch plan | `scan` / `status --root <repo> --batch-size 20` | `aoci_scan` |
| Static outline | `outline <paths...> --root <repo>` | `aoci_outline` |
| Pre-filled batch scaffold | `scaffold --root <repo> --batch 1` | `aoci_scaffold` |
| H-tier symbol sub-volume | `zoom <path> --root <repo>` / `--scaffold` / `--upsert` | `aoci_zoom` |
| Merge FRAS entries | `upsert --root <repo> --entries-file <file>` | `aoci_upsert` |
| Acknowledge clean edits | `ack <paths...> --root <repo>` | `aoci_ack` |
| Rename migration | `migrate-renames --root <repo>` | `aoci_migrate_renames` |
| Branch / worktree restore | `restore --root <repo>` | `aoci_restore` |
| Full health gate | `verify --root <repo>` | `aoci_verify` |
| FRAS quality lint | `lint --root <repo>` | `aoci_lint` |
| Architecture & cycle audit | `audit --root <repo>` | `aoci_audit` |
| Mermaid / DOT graph export | `export --root <repo> --format mermaid\|dot` | `aoci_export` |
| Per-directory shards | `shard --root <repo> [--depth 1 \| --clean]` | `aoci_shard` |
| Semantic pool cache | `pool stats\|export\|import --root <repo>` | `aoci_pool` |
| Repair Git exclusion | `ensure-git-exclude --root <repo>` | `aoci_ensure_git_exclude` |
| Local Git hooks | `install-git-hooks --root <repo> [--with-pre-commit]` | `—` *(CLI-only)* |

- **MCP Resources (5)**: `aoci://manifest`, `aoci://meta`, `aoci://code`, `aoci://hubs`, `aoci://shards/INDEX`.
- **MCP URI Templates (2)**: `aoci://shards/{shard}`, `aoci://{volume}?root={root}`.
- **MCP Prompts (3)**: `aoci-bootstrap`, `aoci-review-diff`, `aoci-sync`.

## Documentation & Testing

- Skill protocol & workflows: [SKILL.md](./SKILL.md)
- Format spec & scaling architecture: [references/aoci-format-spec.md](./references/aoci-format-spec.md)
- Installation & lifecycle hook contracts: [references/hooks-and-install.md](./references/hooks-and-install.md)
- Worked index examples: [examples/](./examples/)

```bash
# Run with base Python on PATH (stdlib unittest only, requires git)
python -B tests/test_aoci.py
python -B -m unittest discover -s tests -p "test_*.py"
```
