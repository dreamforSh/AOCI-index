<div align="center">

<img src="assets/logo.svg" width="120" height="120" alt="AOCI logo">

<h1>AOCI Codebase Fast Index</h1>

<p>
  <b>AI-Oriented Codebase Indexing for coding agents</b><br>
  <sub>One read pass &rarr; repository-wide architecture, cross-file dependencies &amp; non-obvious invariants</sub>
</p>

<p>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/version-1.5.0-0EA5E9?style=flat-square" alt="Version 1.5.0"></a>
  <a href="https://github.com/dreamforSh/AOCI-index/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/dreamforSh/AOCI-index/ci.yml?style=flat-square&label=CI&logo=githubactions&logoColor=white" alt="CI status"></a>
  <img src="https://img.shields.io/badge/python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/dependencies-zero-10B981?style=flat-square" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/platform-Linux%20%C2%B7%20macOS%20%C2%B7%20Windows-64748B?style=flat-square" alt="Linux · macOS · Windows">
</p>
<p>
  <img src="https://img.shields.io/badge/MCP-2024--11--05%20%C2%B7%202025--03--26%20%C2%B7%202025--06--18-6366F1?style=flat-square&logo=modelcontextprotocol&logoColor=white" alt="MCP 2024-11-05 · 2025-03-26 · 2025-06-18">
  <img src="https://img.shields.io/badge/hosts-Claude%20Code%20%C2%B7%20Antigravity-D97757?style=flat-square&logo=claude&logoColor=white" alt="Hosts: Claude Code · Antigravity">
</p>

<p>
  <a href="#-index-format"><b>Format</b></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#-key-features"><b>Features</b></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#-quick-start"><b>Quick Start</b></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#-installation"><b>Install</b></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#-architecture"><b>Architecture</b></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#-cli--mcp-reference"><b>Reference</b></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#-documentation--testing"><b>Docs</b></a>
</p>

</div>

---

**`aoci-index`** is a zero-dependency Python 3.8+ **AI-Oriented Codebase Indexing** (`.aoci/`) engine — a BM25/FRAS graph reader, dual-host lifecycle guard, and stdio MCP server for **Claude Code** and **Antigravity**.

A single read pass gives coding agents repository-wide architectural orientation, cross-file dependencies, and non-obvious invariants — while `.aoci/` stays strictly local via `.git/info/exclude`.

## <img src="assets/icons/format.svg" width="28" height="28" align="top" alt=""> Index Format

What `.aoci/` looks like:

```text
# aoci.txt (Root Manifest)
#Global-Invariants: All write HTTP endpoints require JWT auth middleware; monetary values use int64 cents

# aoci.code.txt (object-fras-v2 — one dense line per managed file)
src/auth/jwt.py[MA8M]: F:Issues and verifies JWT access tokens with multi-KID key rotation | R:src/config/env.py | A:issue_token/verify_token<-src/routes/orders.py | S:Enforce explicit alg allowlist rejecting none; clock_skew fixed at 30s; keep previous KID active >=24h
src/order/service.py[AO9M]: F:Orchestrates idempotent order creation and stock reservation | R:src/config/env.py,src/order/repo.py | A:create_order/cancel_order<-src/routes/orders.py | S:Unique idempotency_key for retries; never invoke synchronous HTTP inside a DB transaction

# aoci.hubs.txt (Optional H-tier symbol zoom spans)
src/order/service.py#create_order[L42-L118]: F:Validates cents payload, reserves inventory row-locked, and records order atomically | S:Must hold SELECT FOR UPDATE before payment intent creation
```

| Coordinate | Meaning |
| :---: | :--- |
| **`L`** | Layer — `E A D K P I M U C R T O` |
| **`D`** | Domain — `[A-Z0-9]` |
| **`I`** | Importance — `1`–`9` |
| **`S`** | Size tier — `T/S/M/L/H`, auto-verified from line count |

| Field | Meaning |
| :---: | :--- |
| **`F:`** | Responsibility |
| **`R:`** | Read-along dependencies |
| **`A:`** | Exports &amp; `<-callers` |
| **`S:`** | Non-obvious stability, ordering, or security traps |

## <img src="assets/icons/features.svg" width="28" height="28" align="top" alt=""> Key Features

- **Single-Pass Codebase Map (`object-fras-v2`)** — Dense one-line-per-file codebase map in `.aoci/aoci.code.txt`, paired with `#Global-Invariants:` (`aoci.txt`), `[LDIS]` coordinates and bilingual `#Glossary:` aliases (`aoci.meta.txt`), and auto-synced directory shards (`shard`) for 10k+ file monorepos.
- **Task-Driven Context Packing &amp; Test Impact** — `pack` / `aoci_pack` builds token-budgeted context packets from BM25, glossary expansion, explicit path mentions (`PathMentionIndex`), L1.5 hub sections, and working-tree Git diffs (`--with-tests` attaches impacted tests; `--live-anchors` extracts symbol anchors on cold cache without mutating disk).
- **H-Tier Symbol Zoom &amp; Auto-Lifecycle (`aoci.hubs.txt`)** — `zoom` / `aoci_zoom` extracts and verifies `[Lstart-Lend]` symbol-level sections for large (`>= 1000`-line) hub files, automatically registering `hubs` in `aoci.txt`, refreshing/pruning spans during `upsert`, migrating renames in `migrate-renames`, surfacing `hub_sections` in `context`/`diff`/`pack`, and validating spans in `verify`/`lint`.
- **Single-Pass Sync (`prepare-sync` &amp; `apply-sync`)** — `prepare-sync` / `aoci_prepare_sync` aggregates pool restorable counts, renames, `impl_only` vs `contract_changed` splits (with previous entries, outlines, and bounded git diffs), auto-generated scaffolds, affected neighbors, and ordered sync steps; `apply-sync` / `aoci_apply_sync` executes `restore -> migrate-renames -> ack -> upsert -> verify` in one orchestrated call.
- **Cross-File Symbol Lookup (`symbol` / `aoci_symbol`)** — Searches across `aoci.hubs.txt`, cached/live outlines, and FRAS `A:` exports, returning spans, signatures, and graph callers/dependencies.
- **Sub-Second Drift, Rename &amp; Branch Sync** — LF-normalized SHA-256 + `(mtime, size)` baseline (`baseline.json`, schema v2), multi-language rich `sig_hash` classification (`impl_only` vs `contract_changed` for Python, JS/TS/Vue/Svelte, Go, Rust, JVM/C#, C/C++, Proto/GraphQL/Prisma/SQL), exact and MinHash moved+edited rename migration (`migrate-renames`), and zero-token `restore` from a cross-worktree SQLite WAL semantic pool (`<git-common-dir>/aoci-cache`).
- **Warm In-Memory Stdio MCP Server (`aoci_mcp.py`)** — Serves **24 annotated tools** (lock-free `contextvars` CLI capture + thread-safe in-memory index/BM25/outline/hubs cache), **5 static resources**, **2 URI templates**, and **3 workflow prompts** over newline-delimited JSON-RPC 2.0 or `Content-Length` framing.
- **Architectural Audit &amp; Interactive Graph Export** — Cross-checks FRAS edges against static imports (Python, JS/TS, Go, Rust, JVM/C#, `.proto`, and C/C++ includes, plus monorepo nested `tsconfig.json`/`go.mod`), detects Tarjan SCC dependency cycles, enforces `#Layer-Rules:`, mines Git co-change coupling (`audit --co-change [--exclude-tests]`), and exports Mermaid, Graphviz DOT, or standalone zero-CDN interactive HTML graphs with pan/zoom, layered view, and focus dimming (`export --format html`).
- **Dual-Host Lifecycle Guardrails &amp; Local Git Exclusion** — `SessionStart`, optional `UserPromptSubmit`/`PreInvocation` turn-start snapshot refresh, and session-scoped convergent `Stop` gate for **Claude Code** and **Antigravity**, keeping `.aoci/` excluded in `.git/info/exclude` without dirtying tracked `.gitignore`.

## <img src="assets/icons/quickstart.svg" width="28" height="28" align="top" alt=""> Quick Start

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

## <img src="assets/icons/install.svg" width="28" height="28" align="top" alt=""> Installation

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
aoci-index --version   # aoci-index 1.5.0
aoci-mcp               # launches the stdio MCP server
```

> [!TIP]
> See [references/hooks-and-install.md](./references/hooks-and-install.md) for manual setup, `--keep-config`, and hook contract verification.

## <img src="assets/icons/architecture.svg" width="28" height="28" align="top" alt=""> Architecture

### Source layout

```text
scripts/
├── aoci_index.py        # CLI & core orchestration (25 commands + `status` alias)
├── aoci_mcp.py          # Stdio JSON-RPC 2.0 MCP server (24 tools, 5 resources, 2 templates, 3 prompts)
├── aoci_hook.py         # Claude Code & Antigravity lifecycle hooks
├── install.py           # Cross-host installer / uninstaller & plugin bundler
└── aoci_core/
    ├── constants.py     # Version constant, tag sets, regexes, path normalization, atomic I/O
    ├── lock.py          # Exclusive cross-process `.aoci/.lock` with dead-PID recovery
    ├── templates.py     # Manifest, meta dictionary, and `.aociignore` parsing/rendering
    ├── batches.py       # Line-balanced, stable 1-indexed `.cache/batch_plan.json` planning
    ├── parsers.py       # Static outlines, rich sig_hash, import graph & monorepo resolution
    ├── graph.py         # ParsedEntry, FRAS graph, context budgeting, lint, BM25, audit, HTML/Mermaid/DOT export
    ├── pack.py          # Task-driven token-budgeted context packer (`pack` / `aoci_pack`)
    ├── test_impact.py   # Impacted test derivation (`--with-tests` / `--tests-for` / `--live-test-imports`)
    ├── hubs.py          # H-tier symbol sub-volume `.aoci/aoci.hubs.txt` (`zoom` lifecycle sync & validation)
    ├── symbols.py       # Cross-file symbol lookup across hubs, outlines, and FRAS (`symbol` / `aoci_symbol`)
    ├── ignore.py        # `.aociignore` / `.gitignore` matcher & path/binary skip heuristics
    ├── shards.py        # Code volume rendering & incremental per-directory shard management
    ├── sync_prep.py     # Single-pass sync briefing (`prepare-sync` / `aoci_prepare_sync`)
    ├── apply_sync.py    # One-shot ordered sync pipeline (`apply-sync` / `aoci_apply_sync`)
    ├── cochange.py      # Git history co-change mining (`audit --co-change`)
    ├── pool.py          # SQLite WAL semantic pool cache & portable .json(.gz) export/import
    └── githooks.py      # Idempotent post-checkout/post-merge/post-rewrite/pre-commit hooks
```

### `.aoci/` layout

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

## <img src="assets/icons/cli.svg" width="28" height="28" align="top" alt=""> CLI & MCP Reference

| Action | CLI (`python scripts/aoci_index.py ...`) | MCP Tool (`scripts/aoci_mcp.py`) |
| :--- | :--- | :--- |
| Initialize `.aoci/` | `init --root <repo>` | `aoci_init` |
| Digest & core hubs | `overview --root <repo>` | `aoci_overview` |
| Search / impact / tests | `query --root <repo> --search "<terms>"` / `--tests-for <path>` | `aoci_query` (`tests_for`, `live_test_imports`) |
| Cross-file symbol lookup | `symbol <name> --root <repo> [--exact] [--live]` | `aoci_symbol` |
| Dependency subgraph | `context <path> --root <repo> --depth 1 [--with-tests]` | `aoci_context` (`with_tests`) |
| Task-driven context pack | `pack --task "<description>" --root <repo> [--budget 2500] [--with-tests] [--live-anchors]` | `aoci_pack` |
| Diff impact briefing | `diff [rev] --root <repo> [--with-tests]` | `aoci_diff` (`with_tests`) |
| Drift & batch plan | `scan` / `status --root <repo> --batch-size 20` | `aoci_scan` |
| Single-pass sync briefing | `prepare-sync --root <repo> [--max-diff-lines 40]` | `aoci_prepare_sync` |
| One-shot ordered sync | `apply-sync --root <repo> [--restore] [--migrate-renames] ...` | `aoci_apply_sync` |
| Static outline | `outline <paths...> --root <repo>` | `aoci_outline` |
| Pre-filled batch scaffold | `scaffold --root <repo> --batch 1` | `aoci_scaffold` |
| H-tier symbol sub-volume | `zoom <path> --root <repo>` / `--scaffold` / `--upsert` | `aoci_zoom` |
| Merge FRAS entries | `upsert --root <repo> --entries-file <file>` | `aoci_upsert` |
| Acknowledge clean edits | `ack <paths...> --root <repo>` | `aoci_ack` |
| Rename migration | `migrate-renames --root <repo>` | `aoci_migrate_renames` |
| Branch / worktree restore | `restore --root <repo>` | `aoci_restore` |
| Full health gate | `verify --root <repo>` | `aoci_verify` |
| FRAS quality lint | `lint --root <repo> [--limit 100]` | `aoci_lint` (`limit`) |
| Architecture & cycle audit | `audit --root <repo> [--co-change [--exclude-tests]]` | `aoci_audit` (`exclude_tests`) |
| Graph export (Mermaid/DOT/HTML) | `export --root <repo> --format mermaid\|dot\|html` | `aoci_export` |
| Per-directory shards | `shard --root <repo> [--depth 1 \| --clean]` | `aoci_shard` |
| Semantic pool cache | `pool stats\|export\|import --root <repo>` | `aoci_pool` |
| Repair Git exclusion | `ensure-git-exclude --root <repo>` | `aoci_ensure_git_exclude` |
| Local Git hooks | `install-git-hooks --root <repo> [--with-pre-commit]` | `—` *(CLI-only)* |

| MCP surface | Entries |
| :--- | :--- |
| **Resources (5)** | `aoci://manifest` · `aoci://meta` · `aoci://code` · `aoci://hubs` · `aoci://shards/INDEX` |
| **URI Templates (2)** | `aoci://shards/{shard}` · `aoci://{volume}?root={root}` |
| **Prompts (3)** | `aoci-bootstrap` · `aoci-review-diff` · `aoci-sync` |

## <img src="assets/icons/docs.svg" width="28" height="28" align="top" alt=""> Documentation & Testing

| Topic | Link |
| :--- | :--- |
| Skill protocol & workflows | [SKILL.md](./SKILL.md) |
| Format spec & scaling architecture | [references/aoci-format-spec.md](./references/aoci-format-spec.md) |
| Installation & lifecycle hook contracts | [references/hooks-and-install.md](./references/hooks-and-install.md) |
| Worked index examples | [examples/](./examples/) |

```bash
# Run with base Python on PATH (stdlib unittest only, requires git)
python -B tests/test_aoci.py
python -B -m unittest discover -s tests -p "test_*.py"
```

---

<div align="center">
  <img src="assets/logo.svg" width="32" height="32" alt="">
  <br>
  <sub>Zero dependencies &middot; Local-only <code>.aoci/</code> &middot; Built for Claude Code &amp; Antigravity</sub>
  <br>
  <sub><a href="#readme">Back to top</a></sub>
</div>
