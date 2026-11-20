---
name: aoci-index
description: >-
  AI-Oriented Code Indexing (AOCI) fast codebase index builder, reader, and
  drift synchronizer for Claude Code and Antigravity. Use whenever: (1) the
  user asks to build, initialize, inspect, query, or refresh an AOCI index for
  a codebase (or invokes `/aoci-index`); OR (2) working in any repository that
  already contains a `.aoci/` index directory — always read the `.aoci/` index
  FIRST before exploring or editing source files (Read-Whole-First), keep
  `.aoci/` excluded from Git via `.git/info/exclude`, and incrementally update
  `.aoci/` after any code additions, edits, renames, or deletions.
---

# AOCI Codebase Fast Index (`aoci-index`)

Build and maintain a local **AOCI (AI-Oriented Code Indexing)** symbolic-semantic index repository under `<repo>/.aoci/`. Works identically in **Claude Code** and **Antigravity**. A single read pass gives the agent repository-scale architectural orientation, cross-file dependencies, and non-obvious invariants, while cached drift checks (roughly 0.1s on small repos and a few hundred ms at ~3k files) keep the index synchronized as code evolves.

## Four Non-Negotiable Rules

1. **Mandatory Git Exclusion**:
   - Whenever Git is present, `.aoci/` MUST be excluded locally via `.git/info/exclude` (resolved through `git rev-parse --git-path info/exclude` so standard repos, worktrees, and submodules all work without dirtying `.gitignore`).
   - Never run `git add` or commit `.aoci/`. If `.aoci/` was ever accidentally tracked, untrack it immediately with `git rm -r --cached .aoci`.
   - File discovery automatically excludes `.git/`, `.aoci/`, `.gitignore` patterns (all levels in Git repos; root and nested directory `.gitignore` files in non-Git workspaces), dependency/build caches, binaries, lockfiles, and files over `--max-bytes`.
2. **Read-Whole-First**:
   - Whenever `<repo>/.aoci/aoci.code.txt` exists, read the index repository (`aoci.txt` -> `aoci.meta.txt` -> `aoci.code.txt`) **before** grepping, reading, debugging, or modifying source files.
   - Immediately after building a new index, read the completed index to establish global context before starting implementation work.
3. **Sync After Code Updates**:
   - Whenever source files are created, modified, renamed, or deleted, run `scan` before finishing the task and resolve every drifted path: `upsert` a revised entry when responsibilities/contracts/invariants changed, `ack` when the existing entry is still accurate, `migrate-renames` for moves, `upsert --prune-only` for deletions. Then verify exit code `0` with `verify`.
   - When an edit alters exported contracts, file paths, or invariants, review the `AFFECTED NEIGHBORS` that `scan` lists (or run `context <relpath>` / `query --impact <relpath>`) and update entries whose `R:`, `A:`, or `S:` fields became stale.
4. **Evidence-Only Semantics**:
   - Never guess FRAS entries from filenames or directory names. Read the actual source code before authoring or revising an entry; use `outline` only to accelerate verifying imports (`R:`) and exports (`A:`), never to skip uncovering non-obvious `S:` constraints.

## Index Repository Layout (`<repo>/.aoci/`)

```text
<repo>/.aoci/
├── aoci.txt        # Root manifest: project name, #Global-Invariants, volume registry, read order
├── aoci.meta.txt   # Meta volume: [LDIS] 4-char tag dictionary (Layer/Domain/Importance/Size) & FRAS rules
├── aoci.code.txt   # Code volume: object-fras-v2 dense single-line map (one line per managed source file)
├── aoci.hubs.txt   # Optional H-tier symbol sub-volume (`<relpath>#<sym>[Lstart-Lend]: F:... | S:...`, managed by `zoom`)
├── baseline.json   # Compact normalized-LF SHA-256, line count, and mtime baseline for fast drift detection
├── .aociignore     # Optional extra ignore globs (full gitignore syntax: `!`, `/`-anchoring, `**`)
├── pending/        # Scratch area for batch entry files (never indexed, never committed)
├── .sessions/      # Hook-managed per-session drift snapshots (auto-cleaned after 7 days)
├── .cache/         # Cached Git exclusion, unindexed scan_fps snapshot, and stable batch_plan
└── .lock           # Transient writer lock (exists only while upsert/ack/migrate-renames run)
```

> Full tag taxonomy, token budgets, FRAS authoring rubric, and large-repo strategy: [references/aoci-format-spec.md](./references/aoci-format-spec.md). Worked examples: [examples/](./examples/).

## Workflows

> **Tool Routing Priority (MCP-First, CLI-Fallback)**:
> - Whenever `aoci_*` MCP tools (`aoci_overview`, `aoci_query`, `aoci_context`, `aoci_pack`, `aoci_diff`, `aoci_scan`, `aoci_prepare_sync`, `aoci_outline`, `aoci_scaffold`, `aoci_ack`, `aoci_upsert`, `aoci_migrate_renames`, `aoci_restore`, `aoci_zoom`, `aoci_shard`, `aoci_ensure_git_exclude`, `aoci_lint`, `aoci_verify`, `aoci_audit`, `aoci_export`, `aoci_pool`, `aoci_init`) are registered in your toolset, **prefer calling them directly** (always passing `root: "<repo>"`) instead of spawning shell commands — this keeps parsed graphs and BM25 indexes warm in memory, exposes MCP resources (`aoci://manifest`, `aoci://meta`, `aoci://code`, `aoci://hubs`, `aoci://shards/INDEX`), URI templates (`aoci://shards/{shard}`, `aoci://{volume}?root={root}`), and prompts (`aoci-bootstrap`, `aoci-review-diff`, `aoci-sync`), marks read-only tools with `readOnlyHint`, and avoids terminal permission prompts.
> - Otherwise, in all commands below, `<SCRIPT>` refers to [scripts/aoci_index.py](./scripts/aoci_index.py) (zero external dependencies; Python 3.8+ standard library only, backed by internal modules in `scripts/aoci_core/`).

### Workflow A: Initialize & Build a New AOCI Index

1. **Initialize `.aoci/` and enforce Git exclusion**:
   ```bash
   python <SCRIPT> init --root <repo>
   ```
   - Confirm `[Git Exclude] OK` when Git is present and confirm `git status` does not list `.aoci/`.
   - Add any project-specific generated/vendor globs (e.g. `generated/`, `*.pb.go`) to `.aoci/.aociignore`.
2. **Customize the domain dictionary and global invariants**:
   - Review the top-level directory summary from `init` and declare project-specific single-character Domain (`D`) codes in `.aoci/aoci.meta.txt`.
   - Read the README, build manifests, and primary entrypoints, then populate `#Global-Invariants:` in `.aoci/aoci.txt`.
3. **Plan line-balanced, stable indexing batches**:
   ```bash
   python <SCRIPT> scan --root <repo> --batch-size 20 --max-batch-lines 2500
   ```
   - Saves a cached unindexed fingerprint snapshot (`.aoci/.cache/scan_fps.json`) and a stable 1-indexed `.aoci/.cache/batch_plan.json` (pass `--reset-batches` or pick a new batch size to rebuild) so completed slots stay `DONE` instead of renumbering later batches while `baseline.json` stays strictly human-verified.
4. **Read source files and upsert FRAS entries batch by batch**:
   - Optionally run `python <SCRIPT> scaffold --root <repo> --batch 1 --output .aoci/pending/batch_01.txt` (inherits active `--batch-size` from `batch_plan.json`, or pass `--batch-size 20`) or `outline <files...>` to pre-fill `[LDIS]`, resolved internal `R:` paths, and repo-wide `A: sym<-caller` edges, then **read the actual source files** to replace `<TODO:...>` with real `F:` responsibilities and non-obvious `S:` invariants (`upsert` rejects unfilled `<TODO:...>`).
   - For >100-file repos, fan out batches across parallel read-only subagents to draft entries, then review and merge centrally (subagents with write access may also `upsert` their own batch directly; writers are serialized by `.aoci/.lock`).
   - Author strictly one `object-fras-v2` line per file:
     ```text
     <relpath>[LDIS]: F:<Responsibility> | R:<Read-Along/Deps> | A:<API/Callers> | S:<Non-Obvious Constraints>
     ```
   - Validate and atomically merge each batch (pass `--dry-run` first for lockless, read-only pre-flight validation if desired). Write batch files under `.aoci/pending/`, because a batch file inside the repo itself would be picked up as a new source file:
     ```bash
     python <SCRIPT> upsert --root <repo> --entries-file .aoci/pending/batch_01.txt
     ```
   - `upsert` validates syntax, Layer codes, Domain declarations, Size tiers (`T/S/M/L/H`), and that each path belongs to the **discovered file set**. It rejects gitignored, too-large, binary, and `..` paths, and auto-corrects path case. It updates `baseline.json` only for indexed files so multi-batch runs are crash-resilient, and it refuses to silently drop pre-existing malformed lines (`--drop-malformed` to override).
5. **Run the verification gate**:
   ```bash
   python <SCRIPT> verify --root <repo>
   python <SCRIPT> lint --root <repo>
   ```
   - `verify` must print `[AOCI Verify] PASS` (exit code `0`): Git exclusion verified, 0 tracked `.aoci/` files, 0 format errors, 0 SHA-256 drift.
   - `lint` flags vague/short `F:`, entries far outside their Importance token budget, `S:-` on Importance 8-9 hubs, dangling `R:`/`A:` paths, and duplicates. Fix those findings before you finish.
6. **Read the newly built index**:
   - Read `.aoci/aoci.txt`, `.aoci/aoci.meta.txt`, and `.aoci/aoci.code.txt` (or run `overview` on >600-file repos) before beginning follow-on coding tasks.

### Workflow B: Read-Whole-First at Task Start

Before exploring, searching, or modifying source files in an indexed repository:

1. **Check index health and Git exclusion (zero-read fast path)**:
   ```bash
   python <SCRIPT> scan --root <repo> --fast
   ```
   - If external drift is detected:
     - Exact-content renames: run `python <SCRIPT> migrate-renames --root <repo>`.
     - Small drift (<=10 files): sync those entries first so your global map is accurate.
     - Large drift: note the drifted paths, trust live source files for those paths while reading the index, and sync before finishing as appropriate.
2. **Read the index repository first**:
   - **Standard repositories (<=600 files)**: read `.aoci/aoci.txt` -> `.aoci/aoci.meta.txt` -> `.aoci/aoci.code.txt` in full.
   - **Large repositories (>600 files)**: run `overview` first, then expand exact working subgraphs with `context` and `query`:
     ```bash
     python <SCRIPT> overview --root <repo> --min-importance 8
     python <SCRIPT> context --root <repo> <target_relpath> --depth 1
     python <SCRIPT> query --root <repo> --domain <D>
     python <SCRIPT> query --root <repo> --keyword <concept>
     ```
3. **Jump directly to relevant source files**: follow `F:`, `R:`, `A:`, and `S:` coordinates to open only the target file and its required read-along dependencies.

### Workflow C: Incremental Sync After Code Updates

After creating, editing, renaming, or deleting any managed source file — **before concluding the task**:

1. **Detect exact drift or fetch a single-pass sync briefing**:
   ```bash
   python <SCRIPT> scan --root <repo>
   # Or collect scaffolds, outlines, bounded diffs, neighbors & recommended actions in one call:
   python <SCRIPT> prepare-sync --root <repo> --json
   ```
2. **Migrate exact-content renames automatically (if any)**:
   ```bash
   python <SCRIPT> migrate-renames --root <repo>
   ```
   - Automatically moves entries in `aoci.code.txt`, rewrites matching `R:`/`A:` path references across other entries, and updates `baseline.json`.
3. **Trace architectural ripple effects**:
   - `scan` prints `AFFECTED NEIGHBORS`: every entry whose `R:` or `A:` explicitly mentions (or depends on) a modified, renamed, or deleted file (catching callers, dependencies whose `A:` still cites a changed caller, and stale references). For deeper analysis, run:
     ```bash
     python <SCRIPT> context --root <repo> <changed_relpath>
     ```
     (`Dependencies` = what the file reads/calls; `Dependents` = who reads/calls it. Direction follows `R:` and `A:` `sym<-caller` semantics.)
   - Include any neighboring entries whose `R:`, `A:`, or `S:` fields now need updating.
4. **Apply incremental updates** (decide per drifted file after re-reading it):
   - **Responsibilities, contracts, or invariants changed** → write updated FRAS entries and merge:
     ```bash
     python <SCRIPT> upsert --root <repo> --entries-file .aoci/pending/delta.txt
     ```
     (Single-file edits can use `--entry "src/foo.py[AX7S]: F:... | R:... | A:... | S:..."` directly.)
   - **Entry is still accurate** (body-only refactor, typo, internal fix) → re-baseline without rewriting it (Size tier refreshes automatically):
     ```bash
     python <SCRIPT> ack --root <repo> src/foo.py src/bar.py
     ```
   - **Deletions only** → prune removed entries cleanly (warns about `R:`/`A:` refs left dangling):
     ```bash
     python <SCRIPT> upsert --root <repo> --prune-only
     ```
   - Update `.aoci/aoci.txt` (`#Global-Invariants:`) or `.aoci/aoci.meta.txt` (Domain codes) if project-wide architecture changed.
5. **Verify zero drift and clean Git state**:
   ```bash
   python <SCRIPT> verify --root <repo>
   ```
   - Confirm `[AOCI Verify] PASS` (exit code `0`) before reporting completion.

## CLI Command Reference

| Command | Purpose | Key Options |
| :--- | :--- | :--- |
| `init` | Scaffold `.aoci/` and write `/.aoci/` into `.git/info/exclude` | `--root`, `--project`, `--locale`, `--force`, `--json` |
| `scan` / `status` | Detect SHA-256 drift (with `sig_hash` `impl-only` / `contract-changed` classification), renames, explicit-mention affected neighbors; build stable batches | `--root`, `--batch-size N` (alias `--batches N`), `--max-batch-lines L`, `--reset-batches`, `--fast`, `--git-fast`, `--auto-restore`, `--check-only`, `--fail-on-drift`, `--json` |
| `prepare-sync` | Single-pass sync briefing: pool restorable count, renames, `impl-only` vs contract-changed files (+ outlines & bounded diffs), scaffolds, neighbors & actions | `--root`, `--no-fast`, `--max-diff-lines N`, `--max-bytes`, `--json` |
| `upsert` | Validate against the discovered file set & atomically merge FRAS entries (locked; auto-refreshes/prunes `aoci.hubs.txt`; warns on stale `A:` mentions) | `--root`, `--entries-file`, `--entry`, `--stdin`, `--prune-only`, `--no-prune`, `--strict-size`, `--drop-malformed`, `--max-bytes`, `--dry-run`, `--json` |
| `ack` | Re-baseline modified files whose entries are still accurate (auto Size tier; `--impl-only` selects unchanged `sig_hash` files) | `paths...`, `--root`, `--all-modified --reviewed`, `--impl-only --reviewed`, `--dry-run`, `--json` |
| `migrate-renames` | Migrate unambiguous exact (and moved+edited) renames and rewrite `R:`/`A:` refs (migrates `aoci.hubs.txt` too) | `--root`, `--exact-only`, `--no-rewrite-refs`, `--drop-malformed`, `--dry-run`, `--json` |
| `restore` | Zero-token exact `(path, sha256)` restore from `<git-common-dir>/aoci-cache` SQLite WAL pool (cross-path matches surfaced as `sha_candidates`) | `--root`, `--allow-sha-fallback`, `--exact-path-only`, `--no-prune`, `--dry-run`, `--json` |
| `pool` | Inspect SQLite WAL semantic cache stats or export/import portable `aoci-semantic-pool-v1` bundles | `stats`, `export --output <file[.gz]>`, `import --input <file[.gz]> [--dry-run]`, `--root`, `--json` |
| `verify` | Full health gate (Git exclusion + format + domain check + zero drift + `aoci.hubs.txt` consistency) | `--root`, `--fast`, `--git-fast`, `--json` |
| `lint` | FRAS quality findings (vague F, token budget, hub `S:-`, dangling refs, duplicates, `aoci.hubs.txt` spans/symbols) | `--root`, `--code`, `--strict`, `--limit`, `--json` |
| `audit` | Cross-check FRAS graph vs static imports, Tarjan SCC dependency cycles, forbidden layer edges (`#Layer-Rules:` / `--forbid`), and optional Git co-change mining (`--co-change`) | `--root`, `--forbid`, `--no-default-rules`, `--no-static`, `--no-cycles`, `--no-layers`, `--co-change`, `--co-change-commits`, `--min-co-changes`, `--min-co-confidence`, `--strict`, `--json` |
| `query` | Filter or BM25-rank entries (`--search` with bilingual `#Glossary:` expansion & match attribution), list dependents (`--impact`), or derive impacted tests (`--tests-for`) | `--search`, `--top`, `--layer`, `--domain`, `--min-importance`, `--path`, `--keyword`, `--impact`, `--tests-for`, `--depth`, `--json` |
| `context` | Directed subgraph: invariants + target(s) + hub sections + dependencies + dependents (optional greedy `--budget` token ceiling and `--with-tests`) | `paths...`, `--root`, `--depth N`, `--budget <tokens>`, `--with-tests`, `--json` |
| `pack` | Build a task-driven, token-budgeted context packet (BM25 + glossary + path-mention + diff seeds, `R:`/`A:` graph, `S:` constraints, `hub_sections`, `open_next` symbol anchors, optional `--with-tests`/`--live-anchors`) | `--task <desc>`, `--root`, `--no-diff`, `--depth N`, `--budget <tokens>`, `--top-seeds N`, `--with-tests`, `--live-anchors`, `--max-bytes`, `--json` |
| `export` | Export filtered or aggregated FRAS dependency graph in Mermaid, Graphviz DOT, or standalone interactive HTML format | `--root`, `--format mermaid\|dot\|html`, `--level file\|dir\|domain`, `--group-by`, `--direction`, `--focus`, `--output`, `--json` |
| `diff` | Architectural impact briefing (FRAS + `S:` traps + downstream `R:` deps + callers + optional `--with-tests`) for diffs | `[rev]`, `--root`, `--staged`, `--depth N`, `--with-tests`, `--json` |
| `overview` | Emit compact global digest (manifest, domains, dirs, core files) | `--root`, `--min-importance`, `--max-core`, `--json` |
| `outline` | Extract static imports and top-level symbols to aid `R:`/`A:` authoring | `paths...`, `--root`, `--max-items`, `--max-bytes`, `--json` |
| `scaffold` | Pre-fill `[LDIS]`, resolved `R:` imports, and repo-wide `A:` callers (`<TODO>` `F:`/`S:`) | `paths...`, `--unindexed`, `--batch N`, `--batch-size M`, `--max-batch-lines L`, `--reset-batches`, `--output`, `--json` |
| `zoom` | Read, scaffold, or upsert symbol-level H-tier sections in `.aoci/aoci.hubs.txt` (`<relpath>#<sym>[Lstart-Lend]: F:... \| S:...`) | `[path]`, `--root`, `--scaffold`, `--output`, `--upsert`, `--upsert-file`, `--prune-missing`, `--dry-run`, `--json` |
| `shard` | Split `aoci.code.txt` into per-directory `.aoci/shards/<dir>.code.txt` slices (10k+ repos) | `--root`, `--depth N`, `--clean`, `--json` |
| `ensure-git-exclude` | Idempotently repair `.git/info/exclude` (e.g. after late `git init`) | `--root`, `--json` |
| `install-git-hooks` | Install/remove idempotent local Git hooks (`post-checkout`/`post-merge`/`post-rewrite` auto-restore + optional `pre-commit`) | `--root`, `--python-cmd`, `--with-pre-commit`, `--pre-commit-verify`, `--uninstall`, `--dry-run`, `--json` |

## Cross-Host Installation, MCP Server & Lifecycle Hooks (Claude Code + Antigravity)

Use the bundled zero-dependency installer [scripts/install.py](./scripts/install.py) to install this skill — plus optional `SessionStart` + `UserPromptSubmit`/`PreInvocation` + `Stop` lifecycle hooks ([scripts/aoci_hook.py](./scripts/aoci_hook.py)), in-memory Stdio MCP server ([scripts/aoci_mcp.py](./scripts/aoci_mcp.py) via `--with-mcp`), rule snippets, or a self-contained Antigravity plugin bundle (`--as-plugin` using [resources/plugin.json](./resources/plugin.json)) — for **Claude Code**, **Antigravity**, or **both**:

- Full installation, hook configuration, and manual test guide: [references/hooks-and-install.md](./references/hooks-and-install.md)
- The `Stop` hook only blames drift introduced **during the current session**. Drift that existed at `SessionStart` and is still byte-identical is reported once at startup but never blocks a turn.
- Regression suite (stdlib only): `python tests/test_aoci.py`
- Claude Code templates: [resources/claude.settings.global.json](./resources/claude.settings.global.json), [resources/claude.settings.project.json](./resources/claude.settings.project.json), [resources/CLAUDE.snippet.md](./resources/CLAUDE.snippet.md)
- Antigravity templates: [resources/hooks.global.json](./resources/hooks.global.json), [resources/hooks.project.json](./resources/hooks.project.json), [resources/AGENTS.snippet.md](./resources/AGENTS.snippet.md)
