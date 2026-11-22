# AOCI Index Format & Authoring Specification (Reference)

This reference defines the local **AOCI (AI-Oriented Code Indexing)** repository format used by the `aoci-index` skill (current as of **v1.6.0**). It combines the AOCI **Symbolic-Semantic Split** and **`object-fras-v2`** dense single-line schema with strict local Git exclusion.

---

## 1. Index Volume Architecture (`.aoci/`)

### 1.1 Root Manifest: `.aoci/aoci.txt`

Declares project identity, locale, Git exclusion state, repository-wide invariants, optional layer rules and domain directives, active volumes, and the required read order:

```text
#AOCI-ROOT-MANIFEST: 1
#Format-Version: cognition-volumes/v1
#Locale: en-US
#Project: order-hub
#Git-Excluded: true (.git/info/exclude)
#Layer-Rules: D!->E,U; P!->E,U; C!->E,A,D,P,U
#Global-Invariants: All write HTTP endpoints require JWT auth middleware; all money amounts use int64 minor units (cents); services must not bypass Repository to issue raw SQL

#Volume: id=meta kind=meta path=aoci.meta.txt format=meta-v1 depends=- state=enabled
#Volume: id=code kind=code path=aoci.code.txt format=object-fras-v2 depends=meta state=enabled
#Volume: id=hubs kind=hubs path=aoci.hubs.txt format=symbol-hubs-v1 depends=code state=enabled

#Read-Order: aoci.txt -> aoci.meta.txt -> aoci.code.txt
#Protocol: Read-Whole-First before exploring source files; sync after code updates.
```

> Keep `#Global-Invariants:` concise (typically 3-6 repository-wide hard rules covering auth boundaries, units/timezones, persistence rules, or forbidden patterns). Readers also accept continuation lines (indented, or `#   - rule` comment bullets) until the next `#Directive:` line or blank line, joined with `; `.
> Optional `#Layer-Rules:` header (in `aoci.txt` or `aoci.meta.txt`, e.g. `#Layer-Rules: D!->E,U; P!->E`) adds repository-specific forbidden Layer transitions enforced by `audit` and `ci-check` alongside the built-in defaults (`D/K/P/I!->E,U`, `C!->E,A,U`, production `!->T`).
> The `id=hubs` volume line is managed automatically: it is inserted after the `id=code` line when `aoci.hubs.txt` exists and removed when the file goes away. `init` writes only the `meta` and `code` volumes.
> `#Domains:` directives may also live here (see §1.2).

---

### 1.2 Meta Volume: `.aoci/aoci.meta.txt`

Defines the project's 4-character symbolic coordinate dictionary `[LDIS]` and FRAS authoring rules.

#### Symbolic Coordinate `[LDIS]`

| Position | Dimension | Values | Purpose |
| :--- | :--- | :--- | :--- |
| 1 | **`L` (Layer)** | `E A D K P I M U C R T O` | Vertical architectural tier (see table below) |
| 2 | **`D` (Domain)** | `[A-Z0-9]` | Horizontal module/business domain (`G`=Global, `X`=Core, plus project-specific single-char codes declared in `aoci.meta.txt` or by a `#Domains:` directive) |
| 3 | **`I` (Importance)** | `1`-`9` | Non-uniform architectural centrality; sets token budget per entry |
| 4 | **`S` (Size)** | `T S M L H` | File scale tier, auto-computed and checked by `aoci_index.py` from actual line count |

**`L` (Layer) Taxonomy:**

| Code | Layer | Typical Files |
| :--- | :--- | :--- |
| `E` | Entry | `main.*`, CLI entrypoints, HTTP/gRPC routers & controllers, webhook/queue consumers |
| `A` | Application | Use cases, application services, workflow orchestrators, command handlers |
| `D` | Domain | Entities, value objects, aggregates, domain rules, state machines |
| `K` | Compute | Parsers, compilers, indexers, simulation/DSP/ML/scheduling engines |
| `P` | Persistence | Database schemas, migrations, ORM models, repositories/DAOs, cache stores |
| `I` | Integration | External SDK/API clients, payment/email/storage adapters, MCP/IPC bridges |
| `M` | Middleware | Auth, rate limiting, logging/tracing, error boundaries, transaction wrappers |
| `U` | UI | React/Vue/Svelte components, pages, views, styles, client state stores |
| `C` | Common | Shared utilities, types/interfaces, constants, error definitions, config loaders |
| `R` | Runtime | `package.json`, `pyproject.toml`, `Cargo.toml`, `Dockerfile`, CI pipelines, build scripts |
| `T` | Test | Unit/integration/e2e tests, mocks, fixtures, benchmarks |
| `O` | Docs | `README.md`, ADRs, OpenAPI/Protobuf specs, architecture docs |

> `R` and `O` are **non-code layers**: `audit` / `ci-check` ignore dependency edges whose *both* endpoints are `R`/`O` when searching for cycles, so mutual co-reads such as `pyproject.toml <-> setup.py` or `README.md <-> SKILL.md` are not reported as `AOCI002` cycles. Mixed config <-> code cycles are still reported, and layer-rule checks are unchanged.

**`S` (Size) Line-Count Tiers (Auto-Verified):**

| Code | Tier | Line Count |
| :--- | :--- | :--- |
| `T` | Tiny | `< 50` lines |
| `S` | Small | `50 - 199` lines |
| `M` | Medium | `200 - 499` lines |
| `L` | Large | `500 - 999` lines |
| `H` | Huge | `>= 1000` lines |

**`I` (Importance) Token-Allocation Budget:**

| Rating | Role | Target Budget | Authoring Focus |
| :--- | :--- | :--- | :--- |
| `9-8` | Core architectural hub / high-risk path | 80-150 tokens | Exhaustive key exports, required co-reads, and all non-obvious invariants in `S:` |
| `7-5` | Primary module or public contract | 45-80 tokens | Complete FRAS with clear boundaries and upstream callers |
| `4-3` | Supporting helper or routine config | 25-45 tokens | Crisp responsibility & key deps; `S:-` if no special constraints |
| `2-1` | Peripheral script, boilerplate, simple test | 15-30 tokens | Minimal one-phrase summary |

#### Domain Declarations (`# D (Domain ...)` / `#Domains:`)

`upsert`, `scan`, and `verify` warn about Domain codes that are not declared (`UNDECLARED DOMAINS`). Two equivalent declaration styles are accepted:

```text
# D (Domain - project-specific single-character module codes for order-hub):
#   G=Global  X=Core
#   A=Auth  O=Order  P=Payment

#Single-line alternative (in aoci.meta.txt or aoci.txt):
#Domains: A=Auth, O=Order, P=Payment
```

- The `# D (Domain ...)` comment section ends at the next `# X (` section or `# [` header; lines marked as examples (`(Example`, `示例`) are skipped.
- `#Domains:` / `#Domain:` directives may appear anywhere in `aoci.meta.txt` or `aoci.txt`. When both styles declare the same code, the directive wins.

#### Bilingual / Domain Glossary (`# [Glossary]` / `#Glossary:` / `.aoci/glossary.txt`)

Declare lightweight synonym / bilingual alias groups in `.aoci/aoci.meta.txt` (or `.aoci/glossary.txt`) so `query --search`, `pack`, and MCP `aoci_query` / `aoci_pack` expand across CJK/English and domain terms with zero external dependencies:

```text
# [Glossary: Bilingual / Domain Term Aliases for BM25 Search]
#   密钥轮换 = key rotation, kid rotation
#   预留库存 = reserve stock, inventory reservation
#Single-line alternative:
#Glossary: 幂等键 = idempotency key; 访问令牌 = access token, jwt
```

- Direct query tokens carry weight `1.0`; expanded glossary tokens carry weight `0.85`.
- JSON results expose `"scores"[i]["matched_terms"]`, `"matched_glossary"`, and top-level `"glossary_expansions"` showing which terms matched.
- Only real alias rows are parsed. The template's `Syntax:` documentation row and directive lines (`#Domains:`, `#Layer-Rules:`, ...) that follow the `# [Glossary ...]` header are not alias groups.
- `pool export` carries glossary groups in the bundle `meta` block; `pool import` appends only groups the target index has not seen yet (see §1.5).

---

### 1.3 Code Volume: `.aoci/aoci.code.txt` (`object-fras-v2`)

Every managed source file occupies **strictly one line**, grouped under deterministic directory banners `===<dir>/===` (repo root is `===./===`):

```text
<relpath>[LDIS]: F:<Function> | R:<Relations> | A:<API/Callers> | S:<Constraints>
```

#### FRAS Field Guidelines

- **`F:` (Function / Responsibility)** — **Required; never `-` or empty**
  - State the concrete operational responsibility in one compact phrase.
  - Reject vague filler (e.g., "helper functions", "core logic"). Name the actual domain objects and operations.
- **`R:` (Relations / Read-Along Dependencies)** — Use `-` if none
  - List internal relative paths (comma-separated) or schemas that **must be read alongside this file** when modifying it.
  - Omit trivial stdlib imports; prioritize tight behavioral couplings.
  - Graph semantics: every indexed path in `R:` is an edge *this file → dependency* (used by `context`, `query --impact`, and `scan` affected-neighbor detection). Root-level files (`pyproject.toml`) are recognized too.
- **`A:` (API / Public Surface & Callers)** — Use `-` if none
  - Name the critical exported symbols, routes, or CLI commands and primary upstream callers (e.g., `issue_token/verify_token<-src/routes/orders.py`).
  - Graph semantics: indexed paths in `A:` are **callers**, i.e. edges *caller → this file*. Do not put this file's own dependencies in `A:`; they belong in `R:`.
- **`S:` (Stability / Security / Non-Obvious Constraints)** — Use `-` if none
  - Capture high-entropy invariants that an AST parser or type signature cannot reveal:
    - Concurrency & locking assumptions (not thread-safe, requires external lock)
    - Ordering & idempotency requirements (must validate before debit, idempotency key uniqueness)
    - Security boundaries (algorithm allowlist, parameterized queries only, no secret logging)
    - Serialization / backward-compatibility traps (persisted payload field renames break active sessions)
    - Platform / encoding invariants (LF line endings, UTC timestamps, fail-fast on missing env vars)

#### Weak vs. Strong Entry Comparison

```text
# Weak (vague, missing exact paths, useless S):
src/auth/jwt.ts[CG5M]: F:JWT helper utilities | R:config | A:exports auth helpers | S:be careful with security

# Strong (exact coordinates, actionable co-reads, concrete non-obvious invariants):
src/auth/jwt.ts[MA8M]: F:Issues and verifies JWT access/refresh tokens with multi-KID key rotation | R:src/config/env.ts,src/auth/types.ts | A:signToken/verifyToken<-src/middleware/auth.ts,src/routes/login.ts | S:Enforce explicit alg allowlist rejecting none; clockSkew fixed at 30s; keep previous KID active >=24h during rotation
```

#### Mechanical Maintenance (`doctor` / `lint --fix`)

`doctor` (MCP `aoci_doctor`, or `lint --fix`) repairs only deterministic metadata and never writes `F:` or `S:` text or re-baselines unreviewed edits: it removes duplicate rows, refreshes stale Size tiers, prunes dangling `R:`/`A:` references to deleted files, optionally adds confirmed internal static imports to `R:` (`--sync-static-r`), and re-syncs `aoci.hubs.txt` spans/registration, `.git/info/exclude`, and shards. Use `--dry-run` to preview.

---

### 1.4 Hubs Volume: `.aoci/aoci.hubs.txt` (`symbol-hubs-v1`, optional)

A symbol-level ("L1.5" / H-tier) sub-volume that zooms into individual functions, classes, or methods of large hub files. One line per `(relpath, symbol)` pair, sorted by `(relpath, start line, symbol)`:

```text
#AOCI-HUBS-VOLUME: 1
#Format: symbol-hubs-v1
#Updated-At: 2026-11-20T00:00:00Z
#Tracked-Sections: 3

src/order/service.py#create_order[L1-L40]: F:Validates int64 cents amount, reserves inventory row-locked, and persists idempotent order | S:Must hold idempotency_key lock; never call external HTTP inside DB tx
```

- **Syntax**: `<relpath>#<symbol>[L<start>-L<end>]: F:<section responsibility> | S:<section constraints>` (1-based inclusive line span; `S:-` when none). The file itself should also have an entry in `aoci.code.txt` (`lint` reports `hub-unindexed-file` otherwise).
- **Authoring**: `zoom <path> --scaffold` emits `F:<TODO:read-section> | S:-` skeletons with spans from the Python AST, the optional tree-sitter accelerator, or brace matching (JS/TS/Vue/Svelte, Go, Rust, JVM/C#, C/C++, IDL/SQL). Replace every `<TODO>` after reading the section, then `zoom --upsert-file <file>` (or `--upsert "<line>"`); `--prune-missing` drops sections whose symbol no longer exists.
- **Maintenance**: `upsert` refreshes spans of re-indexed files and prunes sections of removed files; `migrate-renames` moves sections with their file. `verify` (and the `Stop` hook) fail on malformed lines, `<TODO>` or empty `F:`/`S:`, missing files, and out-of-range spans without re-parsing source; `lint` additionally re-parses the live file and reports `hub-symbol-missing` and `hub-span-outdated`.
- **Read surfaces**: `context`, `diff`, and `pack` attach `hub_sections`. `diff` / `pack` report hunk-filtered `touched_hub_sections` (only sections whose span overlaps a changed hunk). `symbol` searches hub sections alongside outlines and FRAS `A:` exports.
- **Branch restore**: the semantic pool stores each section keyed by `(relpath, symbol, span_sha256)`, a SHA-256 over the whitespace-trimmed body lines of the span. `restore` and `pool import` can therefore rebuild sections after branch switches even when the symbol moved to different line numbers, recomputing `[Lstart-Lend]` from the live file.

---

### 1.5 Local State, Caches & Shared Pool

| Path | Owner | Contents |
| :--- | :--- | :--- |
| `.aoci/baseline.json` | writers (`upsert`, `ack`, `migrate-renames`, `restore`) | Schema v2 compact JSON: per file `sha256` (LF-normalized), `lines`, `bytes`, `mtime_ns`, optional `racy`, `sig_hash` (interface digest), and `sketch` (MinHash line sketch for rename pairing). The derived Size tier is not stored. |
| `.aoci/.aociignore` | user | Extra ignore globs (full gitignore semantics, §2). |
| `.aoci/glossary.txt` | user (optional) | Extra glossary alias groups (§1.2). |
| `.aoci/pending/` | agents | Scratch batch files for `upsert --entries-file` / `--pending-dir` (never indexed). |
| `.aoci/shards/` | `shard` (auto-synced by writers) | Per-directory `<dir>.code.txt` slices, `INDEX.txt`, and `manifest.json`. |
| `.aoci/.sessions/` | lifecycle hooks | Per-session drift snapshots (`<session-id>.json`), pruned after 7 days. |
| `.aoci/.lock` | every writer | Transient exclusive lock; one line `<pid> <iso-time> <token> <hostname>` (see §3). |
| `.aoci/.cache/git_exclusion.json` | reads & hooks | Cached Git-exclusion verdict keyed on `info/exclude`, Git index, and `.gitignore` stat fingerprints. |
| `.aoci/.cache/git_discovery.json` | `scan --git-fast` / large repos | Cached discovered-file table (keyed with the skip-rule revision, so changed skip rules rebuild it). |
| `.aoci/.cache/outline_cache.json` | `scaffold`, `audit`, `pack --with-tests`, `symbol` | mtime-keyed outlines tagged with the extractor (`regex` or `ts<rev>`); outlines from the other extractor are re-extracted, never served. |
| `.aoci/.cache/scan_fps.json`, `batch_plan.json` | `scan --batch-size` | Unindexed-file fingerprints and the stable 1-indexed batch plan for cold bootstraps. |
| `.aoci/.cache/embed/` | `--hybrid --embed-backend model2vec` | Unit embedding vectors keyed by the SHA-1 of each entry's text. |
| `<git-common-dir>/aoci-cache/<slug>.pool.sqlite3` | writers, `restore`, `pool` | Cross-worktree SQLite WAL semantic pool: `pool_by_path_sha`, `pool_by_sha`, and `pool_hubs_by_symbol_sha`, LRU-capped at 50,000 entries. Outside Git it lives at `.aoci/.cache/entry_pool.sqlite3`; legacy `.pool.json` files are migrated once. |

**Portable pool bundles** (`pool export|import`, format `aoci-semantic-pool-v1`, `.json` or `.json.gz`) carry validated file entries, hub sections, and an optional `meta` block (`global_invariants`, `project`, declared `domains`, `glossary` groups, `layer_rules`). `pool import` fills only gaps, under the index lock: undeclared Domain codes become a `#Domains:` line, unseen glossary groups a `#Glossary:` line, `#Layer-Rules:` is added only when none exist, and `#Global-Invariants:` is set only while it is still `-`. Bundle text is sanitized to single directive lines, the result is reported as `meta_applied`, and repeated imports change nothing.

---

## 2. Two-Way Git Exclusion Design

Whenever the target codebase resides inside a Git repository, `aoci_index.py` enforces **bidirectional exclusion**:

1. **Excluding `.aoci/` from Git (`.git/info/exclude`)**:
   - Resolves the real exclude file via `git rev-parse --git-path info/exclude`, transparently supporting normal repositories, Git worktrees (where `.git` is a file pointer), and submodules.
   - Inserts an idempotent fenced block (`/.aoci/` or `/<subpath>/.aoci/` for subproject roots) **without ever editing `.gitignore`**, preventing unwanted diffs in team PRs.
   - Triple-checks exclusion via `git check-ignore -q`, `git ls-files`, and `git status --porcelain`. If `.aoci/` was accidentally staged (`git add -f .aoci`), `verify` and the `Stop` hook immediately flag it with the exact recovery command (`git rm -r --cached .aoci`).
   - If a project was indexed before `git init` was run, subsequent `scan`, `upsert`, `ensure-git-exclude`, or `SessionStart` hook invocations automatically add `/.aoci/` to `.git/info/exclude` and update `#Git-Excluded:` in `aoci.txt`.
2. **Excluding Git internals and noise from indexing**:
   - In Git repos, `git ls-files --cached --others --exclude-standard -z` discovers source files while honoring `.gitignore`, `.git/info/exclude`, and global excludes, automatically omitting `.git/`, `.aoci/`, and ignored build artifacts.
   - In non-Git workspaces, an `os.scandir` walk skips common build/vendor directories and honors both the root `.gitignore` and nested subdirectory `.gitignore` files.
   - Universal tool caches (`node_modules`, `.venv`, `__pycache__`, `.next`, etc.), binaries, lockfiles, minified bundles, and files larger than 512 KB (configurable via `--max-bytes`) are always filtered out, plus any custom patterns in `.aoci/.aociignore`. These use full gitignore semantics: last match wins, `!` re-includes, a leading or inner `/` anchors to the root, a trailing `/` matches directories only, `**` spans directories, and files under an ignored directory cannot be re-included.
   - **Installed skill copies are never indexed**: agent-host config directories (`.claude`, `.agents`, `.codex`, `.cursor`, `.windsurf`, `.copilot`, `.codeium`, ...) are always skipped, as are `<.host>/skills/aoci-index/` trees (e.g. `.github/skills/aoci-index/` from `--host vscode --scope project`) and any directory holding an installer manifest (`.aoci-install.json`), including on the `--git-fast` incremental path.
   - Writers (`upsert`, `ack`) validate every path against this exact discovered set, so the index can never hold an entry that drift detection would report as deleted.

---

## 3. Scaling Strategy & Performance Architecture (>600 Files)

- **<= 600 managed files**: `.aoci/aoci.code.txt` typically stays under ~50 KB (~10k-15k tokens) and should be read whole in a single pass.
- **> 600 managed files**:
  1. **Indexing**: run `scan --batch-size 25 --max-batch-lines 2500` to create directory-coherent, line-budgeted batches. Use `outline <files...>` to quickly confirm imports and exported symbols while reading source files. To fan out, hand each subagent a self-contained `batch-pack --batch N [--with-source]` work-pack, have it write `.aoci/pending/batch_NN.txt`, and merge them all atomically with `upsert --pending-dir .aoci/pending [--clean-pending]` (validation errors cite `[batch_NN.txt:Lx]`). Concurrent `upsert` calls are also safe because all writers hold `.aoci/.lock`.
  2. **Reading**: run `aoci_index.py overview --min-importance 8` first to ingest global invariants, domain dictionary, directory distribution, and core hubs. Then use `context <target_relpath> --depth 1` to expand the target plus its dependencies (`R:` + files that list it as a caller) and dependents (files whose `R:` names it + callers in its `A:`), `pack --task "<description>" --budget 2500` for a task-scoped packet, or drill down via `query --domain`, `query --path`, `query --keyword`, `query --search`, and `query --impact [--depth N]`. For symbol-level questions use `symbol <name>` and `refs <symbol>` (graph-pruned call-site search). For 10k-file monorepos, run `aoci_index.py shard --depth 2` to generate auto-synced `.aoci/shards/<dir>.code.txt` slices and `.aoci/shards/INDEX.txt`.
  3. **Refactoring & renames**: when moving or renaming files, run `aoci_index.py migrate-renames` to move FRAS entries and rewrite `R:`/`A:` path references automatically. Byte-identical moves become clean immediately; high-similarity moved+edited files (>=60% Bottom-k line sketch match) carry their entry and rewritten refs forward while remaining `MODIFIED` until reviewed with `ack` or `upsert` (`--exact-only` to skip moved+edited matches).
  4. **Edit hygiene**: for edits that leave a file's responsibilities, contracts, and invariants intact, run `ack <paths>` instead of rewriting the entry (`--all-modified` requires `--reviewed`). `prepare-sync` reports `contract_deltas` (added/removed/changed signatures) for contract-changed files so you can decide between `ack` and `upsert` without re-reading unchanged code.
  5. **Noise hygiene**: exclude generated code (Protobuf stubs, OpenAPI codegen, test snapshots, vendored bundles) in `.aoci/.aociignore` so every line in `aoci.code.txt` carries high architectural signal. Run `doctor` periodically for mechanical cleanup (§1.3).
- **Engine Performance Notes**:
  - Git topology (`toplevel`, `info/exclude`, `index`) is resolved via a pure-Python `.git` directory / worktree `commondir` / submodule inspection first (falling back to `git rev-parse` only on `GIT_*` env overrides or bare repos), eliminating a ~55 ms `git` subprocess on every CLI and hook invocation.
  - Git-exclusion verification is cached in `.aoci/.cache/`, keyed on the stat fingerprints of `info/exclude`, the Git index, and `.gitignore`. Hot paths skip two `git` subprocesses when nothing Git-related changed.
  - Under `--fast` (and inside hooks), unchanged files matching cached `(mtime_ns, bytes)` perform **zero file opens and zero SHA-256 hashing**. On >=2,000-file Git repos (or with `--git-fast`), unchanged `.git/index` runs a single `git --no-optional-locks status --porcelain=v1 -z -uall` instead of `git ls-files` + statting thousands of clean tracked files; targeted `ack <paths>` and `upsert --no-prune` validate only the requested paths and incrementally update `git_discovery.json` instead of invalidating the full cache.
  - **Bulk moved+edited detection & dirty-only shards**: `similar_renames` uses an inverted MinHash CRC32 posting list + O(S) bucket tie-resolution (scaling to hundreds/thousands of moved+edited files without pair caps), and `.aoci/shards/` rewrites only shard slices whose entry digest changed.
  - **Deferred MinHash sketches**: drift scans fingerprint without line sketches; `attach_line_sketches()` computes them afterwards only for exact-rename targets and, when a deleted baseline entry carries a sketch, for added files (skipping any file whose SHA-256 changed since fingerprinting). Baseline writes still persist sketches. A full rehash scan of 4,000 unindexed files dropped from ~1,009 ms to ~592 ms.
  - **Racy-clean guard & self-healing**: files hashed within 3 s of their last modification are flagged `racy` in `baseline.json` and re-hashed; once a subsequent `--fast` scan runs after the 3 s window and confirms the hash still matches, the `racy` flag is automatically cleared in `baseline.json` (preserving `git_discovery.json`) so re-hashing stops permanently.
  - **SQLite WAL pool, cached outlines, rich multi-language `sig_hash`, L1.5 `aoci.hubs.txt` sync & in-memory BM25**: the cross-worktree semantic cache pool (§1.5) uses SQLite WAL with LRU eviction, read-only `?mode=ro` lookups, and portable team bundles (`aoci-semantic-pool-v1` JSON/JSON.gz via `pool export|import`). `restore` defaults to exact `(path, sha256)` matching and surfaces cross-path content matches as `sha_candidates` unless `--allow-sha-fallback` is explicitly requested (still blocked if the origin path still exists or multiple files share that SHA). `scaffold`, `audit`, and `pack --with-tests` share `.aoci/.cache/outline_cache.json`; cold multi-batch bootstraps reuse `.aoci/.cache/scan_fps.json` and `.aoci/.cache/batch_plan.json`; targeted baseline writes record an interface signature digest (`sig_hash`) in `baseline.json` across Python, JS/TS/Vue/Svelte, Go, Rust, JVM/C#, C/C++, and IDL/SQL (`proto`/`graphql`/`prisma`/`sql`) so `scan` classifies modified files as `impl_only` vs `contract_changed` (enabling `ack --impl-only --reviewed`) at zero cost on clean files; and `.aoci/aoci.hubs.txt` stays synchronized across `upsert`, `migrate-renames`, `verify`, `lint`, `context`, `diff`, and `pack`.
  - **Optional tree-sitter accelerator** (`pip install "aoci-index[treesitter]"`, disable with `AOCI_DISABLE_TREESITTER=1`): parsers are cached per thread, missing grammars are negatively cached once, trees with syntax errors fall back to the regex extractors, and symbol labels match the regex path, so outlines and `sig_hash` stay label-compatible.
  - Cold scans and full `verify` runs read each file once (combining binary detection, LF normalization, line count, and SHA-256) and switch automatically to multi-threaded hashing (`ThreadPoolExecutor`) above 96 uncached files. `upsert` hashes each written file once alongside `sig_hash`.
  - `baseline.json` (schema v2) is compact JSON without the derived `size_tier`, storing optional `sig_hash` for supported source languages and `sketch` for similarity renames. The dependency graph used by `context`/`query --impact`/`scan`/`audit`/`export` is built in a single O(refs) pass.
  - **Index writer lock**: `.aoci/.lock` is created with `O_CREAT|O_EXCL`, is re-entrant within one thread (so `apply-sync` runs every step under one lock), and is refreshed by a heartbeat every `stale / 4` seconds (`stale` = 300 s). Dead same-host PIDs are reaped at once; a live same-host PID only after `hard_stale` (default `stale * 12`); other-host or legacy (pre-1.6, hostless) locks keep the plain age rule.
  - **MCP server cache**: `aoci_mcp.py` caches slotted `ParsedEntry`, `PathMentionIndex`, `BM25Index`, outlines, hubs, glossary, and the FRAS graph per workspace, and captures CLI calls lock-free via `contextvars`. After a mutating tool call (or any change to `aoci.code.txt` while manifest, meta, and glossary are unchanged), it re-applies only changed or removed entries: `FrasGraphState` keeps per-entry edge contributions with reference counts, and `BM25Index.updated()` tombstones old documents (compacting once tombstones exceed half of the live documents) and copies only touched posting lists. Both are copy-on-write and match a full rebuild exactly; changes larger than max(256, 25% of entries), or more than 64 new paths, fall back to a full rebuild. On 10,000 entries a small edit refreshes in ~16 ms instead of ~1.1 s.
  - **Cold start**: `aoci_index.py` re-exports `aoci_core` lazily (PEP 562), `cli_parser` builds only the requested subcommand's arguments, and script entry points cache bytecode under a per-user `sys.pycache_prefix` (opt out with `AOCI_NO_PYCACHE=1`, `python -B`, or `PYTHONDONTWRITEBYTECODE=1`). `--version`, the `UserPromptSubmit` hook, `scan --fast`, and `query --search` start in ~115-165 ms on Windows / CPython 3.14.
  - Typical timings: ~20-30 ms in-process for targeted `ack`/`upsert --no-prune` (zero `git` subprocesses on warm tracked files); ~60-120 ms for fast scans at 1k-3k files on Windows.

---

## 4. Retrieval, Freshness & Evaluation

### 4.1 Stale-Entry Flags on Read Paths

`query`, `context`, and `pack` (CLI and MCP) check the paths they return against `baseline.json` (at most 200 paths per call). An unchanged, non-racy `(mtime_ns, size)` costs one `stat`; anything else is confirmed with an LF-normalized SHA-256, so a `touch` alone is never reported. Stale paths appear as:

- `stale_entries: [{path, reason, note}]` with `reason` one of `modified`, `missing`, or `unbaselined`;
- `seeds[].stale` in `pack`;
- a `## Stale Entries` section or `! stale:` line in text output.

Treat a stale entry as a hint only: read the live source before relying on it, and sync the entry before finishing.

### 4.2 Ranking: BM25 Default and Opt-In Hybrid

- **BM25** (default) ranks over the full index with glossary expansion (§1.2). Filters (`--layer`, `--domain`, `--path`, ...) combined with `--search` act as a candidate set over the full-index ranking, so IDF never depends on how narrow the filter is. The CLI `query` and MCP `aoci_query` share one engine (`aoci_core.query_ops.run_query`) and return identical payloads.
- **Hybrid** (`--hybrid`, MCP `hybrid: true`) fuses BM25 with a zero-dependency character-trigram TF-IDF cosine channel (minimum similarity 0.15) via Reciprocal Rank Fusion, `w / (60 + rank)` with weights bm25 1.0 and trigram 0.5. It catches word-form mismatches (`subscribe` vs `subscriptions`) but not synonyms; declare those in the glossary. An optional dense channel (`--embed-backend model2vec`, `--embed-model`, or `AOCI_EMBED_BACKEND` / `AOCI_EMBED_MODEL`; install the `[embed]` extra; default model `minishlab/potion-base-8M`) caches vectors in `.aoci/.cache/embed/`. If it is unavailable, retrieval falls back to BM25 + trigram with a warning. Hits gain `channels` (rank per channel) and `bm25_score`; JSON output gains `retrieval`.

### 4.3 `pack` Seed Selection

`pack` seeds come from BM25 (or hybrid) hits, glossary expansion, explicit path mentions in the task, the working-tree diff, and L1.5 hub sections. Ranked seeds scoring below 20% of the best seed are dropped (`SEED_MIN_REL_SCORE`). When the task never mentions tests, T-layer hits are scored at 0.35x (`TEST_LAYER_DEMOTE`) and extra candidates are fetched to refill the slots. Hub sections that share at least 2 informative terms with the task (corpus-wide BM25 IDF) add or boost their file as a seed (`reason: hub`).

### 4.4 Evaluation Harness (`eval`, CLI only)

`eval --cases <file.jsonl|.json>` scores retrieval and `pack` against task cases (`{"id", "task", "expected": [paths], "tags"}`; `#` comment lines are allowed in `.jsonl`). It reports `recall@k`, `precision@k`, `MRR` (top 20), `seed_recall`, and `pack_recall`. Expected paths that are not indexed are listed as `unknown_expected` and excluded from the scores. `--modes bm25,hybrid` compares modes side by side, and `--min-recall` / `--min-pack-recall` exit 1 below the given thresholds. Packs are evaluated without the working-tree diff, so results are reproducible. `examples/eval/self-eval.jsonl` ships 34 developer-phrased cases for AOCI-index itself.
