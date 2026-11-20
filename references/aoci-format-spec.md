# AOCI Index Format & Authoring Specification (Reference)

This reference defines the local **AOCI (AI-Oriented Code Indexing)** repository format used by the `aoci-index` skill. It combines the AOCI **Symbolic-Semantic Split** and **`object-fras-v2`** dense single-line schema with strict local Git exclusion.

---

## 1. Index Volume Architecture (`.aoci/`)

### 1.1 Root Manifest: `.aoci/aoci.txt`

Declares project identity, locale, Git exclusion state, repository-wide invariants, active volumes, and the required read order:

```text
#AOCI-ROOT-MANIFEST: 1
#Format-Version: cognition-volumes/v1
#Locale: en-US
#Project: order-hub
#Git-Excluded: true (.git/info/exclude)
#Global-Invariants: All write HTTP endpoints require JWT auth middleware; all money amounts use int64 minor units (cents); services must not bypass Repository to issue raw SQL

#Volume: id=meta kind=meta path=aoci.meta.txt format=meta-v1 depends=- state=enabled
#Volume: id=code kind=code path=aoci.code.txt format=object-fras-v2 depends=meta state=enabled

#Read-Order: aoci.txt -> aoci.meta.txt -> aoci.code.txt
#Protocol: Read-Whole-First before exploring source files; sync after code updates.
```

> Keep `#Global-Invariants:` concise (typically 3-6 repository-wide hard rules covering auth boundaries, units/timezones, persistence rules, or forbidden patterns).
> Optional `#Layer-Rules:` header (in `aoci.txt` or `aoci.meta.txt`, e.g. `#Layer-Rules: D!->E,U; P!->E`) adds repository-specific forbidden Layer transitions enforced by `aoci_index.py audit` alongside the built-in defaults (`D/K/P/I!->E,U`, `C!->E,A,U`, production `!->T`).

---

### 1.2 Meta Volume: `.aoci/aoci.meta.txt`

Defines the project's 4-character symbolic coordinate dictionary `[LDIS]` and FRAS authoring rules.

#### Symbolic Coordinate `[LDIS]`

| Position | Dimension | Values | Purpose |
| :--- | :--- | :--- | :--- |
| 1 | **`L` (Layer)** | `E A D K P I M U C R T O` | Vertical architectural tier (see table below) |
| 2 | **`D` (Domain)** | `[A-Z0-9]` | Horizontal module/business domain (`G`=Global, `X`=Core, plus project-specific single-char codes declared in `aoci.meta.txt`) |
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

#### Bilingual / Domain Glossary (`# [Glossary]` / `#Glossary:` / `.aoci/glossary.txt`)

Declare lightweight synonym / bilingual alias groups in `.aoci/aoci.meta.txt` (or `.aoci/glossary.txt`) so `query --search` and MCP `aoci_query` expand across CJK/English and domain terms with zero external dependencies:

```text
# [Glossary: Bilingual / Domain Term Aliases for BM25 Search]
#   密钥轮换 = key rotation, kid rotation
#   预留库存 = reserve stock, inventory reservation
#Single-line alternative:
#Glossary: 幂等键 = idempotency key; 访问令牌 = access token, jwt
```

- Direct query tokens carry weight `1.0`; expanded glossary tokens carry weight `0.85`.
- JSON results expose `"scores"[i]["matched_terms"]`, `"matched_glossary"`, and top-level `"glossary_expansions"` showing which terms matched.

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
   - Writers (`upsert`, `ack`) validate every path against this exact discovered set, so the index can never hold an entry that drift detection would report as deleted.

---

## 3. Scaling Strategy & Performance Architecture (>600 Files)

- **<= 600 managed files**: `.aoci/aoci.code.txt` typically stays under ~50 KB (~10k-15k tokens) and should be read whole in a single pass.
- **> 600 managed files**:
  1. **Indexing**: run `scan --batch-size 25 --max-batch-lines 2500` to create directory-coherent, line-budgeted batches. Use `outline <files...>` to quickly confirm imports and exported symbols while reading source files, and optionally dispatch parallel subagents; concurrent `upsert` calls are safe because all writers hold `.aoci/.lock`.
  2. **Reading**: run `aoci_index.py overview --min-importance 8` first to ingest global invariants, domain dictionary, directory distribution, and core hubs. Then use `context <target_relpath> --depth 1` to expand the target plus its dependencies (`R:` + files that list it as a caller) and dependents (files whose `R:` names it + callers in its `A:`), or drill down via `query --domain`, `query --path`, `query --keyword`, and `query --impact [--depth N]`. For 10k-file monorepos, run `aoci_index.py shard --depth 2` to generate auto-synced `.aoci/shards/<dir>.code.txt` slices and `.aoci/shards/INDEX.txt`.
  3. **Refactoring & renames**: when moving or renaming files, run `aoci_index.py migrate-renames` to move FRAS entries and rewrite `R:`/`A:` path references automatically. Byte-identical moves become clean immediately; high-similarity moved+edited files (>=60% Bottom-k line sketch match) carry their entry and rewritten refs forward while remaining `MODIFIED` until reviewed with `ack` or `upsert` (`--exact-only` to skip moved+edited matches).
  4. **Edit hygiene**: for edits that leave a file's responsibilities, contracts, and invariants intact, run `ack <paths>` instead of rewriting the entry (`--all-modified` requires `--reviewed`).
  5. **Noise hygiene**: exclude generated code (Protobuf stubs, OpenAPI codegen, test snapshots, vendored bundles) in `.aoci/.aociignore` so every line in `aoci.code.txt` carries high architectural signal.
- **Engine Performance Notes**:
  - Git topology (`toplevel`, `info/exclude`, `index`) is resolved via a pure-Python `.git` directory / worktree `commondir` / submodule inspection first (falling back to `git rev-parse` only on `GIT_*` env overrides or bare repos), eliminating a ~55 ms `git` subprocess on every CLI and hook invocation.
  - Git-exclusion verification is cached in `.aoci/.cache/`, keyed on the stat fingerprints of `info/exclude`, the Git index, and `.gitignore`. Hot paths skip two `git` subprocesses when nothing Git-related changed.
  - Under `--fast` (and inside hooks), unchanged files matching cached `(mtime_ns, bytes)` perform **zero file opens and zero SHA-256 hashing**. On >=2,000-file Git repos (or with `--git-fast`), unchanged `.git/index` runs a single `git --no-optional-locks status --porcelain=v1 -z -uall` instead of `git ls-files` + statting thousands of clean tracked files; targeted `ack <paths>` and `upsert --no-prune` validate only the requested paths and incrementally update `git_discovery.json` instead of invalidating the full cache.
  - **Bulk moved+edited detection & dirty-only shards**: `similar_renames` uses an inverted MinHash CRC32 posting list + O(S) bucket tie-resolution (scaling to hundreds/thousands of moved+edited files without pair caps), and `.aoci/shards/` rewrites only shard slices whose entry digest changed.
  - **Racy-clean guard & self-healing**: files hashed within 3 s of their last modification are flagged `racy` in `baseline.json` and re-hashed; once a subsequent `--fast` scan runs after the 3 s window and confirms the hash still matches, the `racy` flag is automatically cleared in `baseline.json` (preserving `git_discovery.json`) so re-hashing stops permanently.
  - **SQLite WAL pool, cached outlines, rich multi-language `sig_hash`, L1.5 `aoci.hubs.txt` sync & in-memory BM25**: the cross-worktree semantic cache pool uses SQLite WAL (`<git-common-dir>/aoci-cache/<slug>.pool.sqlite3`) with LRU eviction (transparently migrating legacy `.pool.json`) and portable team bundles (`aoci-semantic-pool-v1` JSON/JSON.gz via `pool export|import`). `restore` defaults to exact `(path, sha256)` matching and surfaces cross-path content matches as `sha_candidates` unless `--allow-sha-fallback` is explicitly requested (still blocked if the origin path still exists or multiple files share that SHA). `scaffold`, `audit`, and `pack --with-tests` share `.aoci/.cache/outline_cache.json`; cold multi-batch bootstraps reuse `.aoci/.cache/scan_fps.json` and `.aoci/.cache/batch_plan.json`; targeted baseline writes record an interface signature digest (`sig_hash`) in `baseline.json` across Python, JS/TS/Vue/Svelte, Go, Rust, JVM/C#, C/C++, and IDL/SQL (`proto`/`graphql`/`prisma`/`sql`) so `scan` classifies modified files as `impl_only` vs `contract_changed` (enabling `ack --impl-only --reviewed`) at zero cost on clean files; and `.aoci/aoci.hubs.txt` stays synchronized across `upsert`, `migrate-renames`, `verify`, `lint`, `context`, `diff`, and `pack`. The MCP server caches slotted `ParsedEntry`, `PathMentionIndex`, `BM25Index`, outlines, hubs, glossary, and FRAS graph per workspace and captures CLI calls lock-free via `contextvars`.
  - Cold scans and full `verify` runs read each file once (combining binary detection, LF normalization, line count, and SHA-256) and switch automatically to multi-threaded hashing (`ThreadPoolExecutor`) above 96 uncached files. `upsert` hashes each written file once alongside `sig_hash`.
  - `baseline.json` (schema v2) is compact JSON without the derived `size_tier`, storing optional `sig_hash` for supported source languages and `sketch` for similarity renames. The dependency graph used by `context`/`query --impact`/`scan`/`audit`/`export` is built in a single O(refs) pass.
  - Typical timings: ~20-30 ms in-process for targeted `ack`/`upsert --no-prune` (zero `git` subprocesses on warm tracked files); ~60-120 ms for fast scans at 1k-3k files on Windows.
