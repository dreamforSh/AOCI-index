# Changelog

All notable changes to `aoci-index` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **6-Host Ecosystem Installer (`cursor`, `windsurf`, `vscode`)**: Added `--host cursor | windsurf | vscode` (and expanded `--host all`) in `aoci_core.installer` / `scripts/install.py`, plus `--host cursor` lifecycle hook support in `scripts/aoci_hook.py`.
- **Hub-Aware Semantic Pool**: Added `pool_hubs_by_symbol_sha` in `aoci_core.pool` & `aoci_core.hubs` for zero-token, line-shift-resilient `aoci.hubs.txt` restore across branch switches and `pool export/import` bundles.
- **Subagent Batch Work-Pack & Pending Merge**: Added `batch-pack` / `aoci_batch_pack` (`--with-source`) and `upsert --pending-dir [--clean-pending]` with per-file batch provenance on validation errors.
- **CI-Native Architecture Guard & GitHub Action**: Added `ci-check` / `aoci_ci_check` and root `action.yml`, supporting zero-index CI checkouts, `--pool-bundle`, `--base-rev` PR diff test derivation, SARIF 2.1.0, GitHub Actions workflow annotations, and PR Markdown reports.
- **MCP `2025-06-18` Protocol Enhancements**: Added `roots/list` & `notifications/roots/list_changed` multi-root discovery, `outputSchema` + `structuredContent`, `resources/subscribe` / `notifications/resources/updated`, and `aoci_install_git_hooks` (27 MCP tools total).
- **Optional `tree-sitter` Accelerator & Self-Hosting Seed Pool**: Added optional `aoci-index[treesitter]` AST acceleration (`aoci_core.ts_accel`), interactive HTML/Mermaid `--diff` / `--diff-rev` highlighting, and `examples/self-pool.json.gz`.

### Changed
- **Modular `aoci_core` Split with Full `aoci_index` Facade Compatibility**: Split the monolithic `scripts/aoci_index.py` into unidirectional flat modules under `scripts/aoci_core/` (`git_ops.py`, `discovery.py`, `drift.py`, `write_ops.py`, `diff_brief.py`, `audit.py`, `cli_read.py`, `cli_write.py`, `cli_check.py`, `cli_parser.py`, plus `parse_code_volume` in `shards.py`). `scripts/aoci_index.py` remains a lightweight `<450`-line CLI entrypoint and backwards-compatible facade re-exporting all existing public and internal symbols for `aoci_mcp.py`, `aoci_hook.py`, and external scripts; added `.github/scripts/refresh_self_pool.py --check` for deterministic `examples/self-pool.json.gz` maintenance.
- **Monorepo Scale Optimizations**: Added dual positive/negation union regexes in `IgnoreMatcher`, `O(1)` Python suffix index + single-pass comment stripping in `parsers.py`, zero-stat dangling-ref checks + single-parse `verify`, bucketed `TestImpactIndex`, composite PK row-value SQLite pool lookups, same-thread re-entrant `IndexLock` across `apply-sync`, and per-root locks + short-TTL diff caching in `aoci_mcp.py`.

### Fixed
- **P0 · Project-scope installs no longer self-index**: `ALWAYS_SKIP_DIRS` now also covers `.codex`, `.cursor`, `.windsurf`, `.copilot`, and `.codeium`. `path_skip_reason()` skips `<.host>/skills/aoci-index/` (e.g. `.github/skills/aoci-index/` from `--host vscode --scope project`), and discovery skips any directory that holds an installer manifest (`.aoci-install.json`), including on the `--git-fast` incremental path. The Git discovery cache key now has a `skip_rules` revision, so stale cached tables get rebuilt.
- **P0 · `tree-sitter` accelerator is thread-safe and matches the regex extractors**: parsers are now cached per thread (`threading.local()`) instead of one shared `Parser` used by the 8-worker import-graph pool. The probe is locked, and a grammar that is missing or fails to download is negatively cached once instead of being retried by every thread. JS/TS now covers exported `const`/`let` (arrow components, router instances), CommonJS `require()` / `exports.x =`, dynamic `import()`, `export { a, b as c } [from]`, `export * as ns`, and ambient/overload `function` signatures. Rust now covers `impl`/`trait` methods and inline `mod` bodies. C/C++/C#/Java now recurse through nested namespaces/classes with no depth limit, and Go method labels keep the `func (r *T) Name` form. `.cs` now resolves the `csharp` grammar name used by `tree_sitter_language_pack`. Trees that contain syntax errors fall back to the regex path. `outline_cache.json` is now tagged with the extractor (`regex` / `ts<rev>`), so outlines from the other mode are re-extracted instead of being served stale.
- **Packaging, MCP & Sync Hardening**: Moved `install` into `aoci_core.installer` with embedded wheel `_skill_bundle` (`setup.py`), fixed Windows `%3A` file URIs and thread-safe stdio isolation in `aoci_mcp.py`, synced `aoci.hubs.txt` on `restore`, decoupled `apply-sync --ack-impl-only` + `--ack-path`, added UTF-8 BOM handling across volumes/parsers, and enforced read-only `?mode=ro` SQLite pool queries.
- **P1 · `R`/`O`-layer co-read pairs are no longer reported as dependency cycles**: `audit` / `ci-check` now find cycles on `graph.code_dependency_view()`, which drops only edges where both ends are non-code layers (`R` runtime config, `O` docs). Mutual co-reads such as `pyproject.toml <-> setup.py` or README <-> SKILL.md no longer trigger `AOCI002`. Real code cycles and mixed config <-> code cycles are still reported. Layer-rule checks are unchanged. The self-check `ci-check --root . --pool-bundle examples/self-pool.json.gz --exclude-tests` is CLEAN again.
- **P1 · MCP `2025-06-18` `structuredContent` compliance**: when a tool that declares `outputSchema` succeeds, it now always returns `structuredContent`. `aoci_ci_check` (`markdown` / `github` / `sarif`) and `aoci_batch_pack` (`markdown`) publish their underlying report dict through a per-call side channel; any other non-JSON success text is wrapped as `{"text": ...}`. `_CLIENT_ROOT_HINTS`, `_CLIENT_ROOT_HINT`, and `_SUBSCRIBED_URIS` are now guarded by `_STATE_LOCK`.
- **P1 · `#Domains:` one-line directive**: `parse_declared_domains()` now also accepts `#Domains:` / `#Domain:` lines anywhere in `aoci.meta.txt` or `aoci.txt` (like `#Glossary:` / `#Layer-Rules:`). Codes declared by a directive override the `# D (Domain ...)` section on conflict.
- **P1 · VS Code global MCP path**: `--host vscode --scope global` now writes to the VS Code user settings dir (`%APPDATA%/Code/User/mcp.json`, `~/Library/Application Support/Code/User/mcp.json`, `${XDG_CONFIG_HOME:-~/.config}/Code/User/mcp.json`) instead of `~/.vscode/mcp.json`, which VS Code never reads. Install and uninstall also remove the stale `aoci-index` server left in that legacy file. With a sandboxed `--home-dir`, the path stays inside the sandbox.
- **P1 · Docs & metadata**: `SKILL.md` now covers all 6 hosts, all 27 MCP tools (`aoci_batch_pack`, `aoci_ci_check`), `batch-pack`, `ci-check`, `upsert --pending-dir/--clean-pending`, `export --diff/--diff-rev`, and the `[treesitter]` extra. The SARIF `informationUri` now points to `github.com/dreamforSh/AOCI-index`, matching README / CHANGELOG / the git remote. The `examples/self-pool.json.gz` hashes have been refreshed.

## [1.5.0] - 2026-11-20

### Added
- **OpenAI Codex CLI Support**: Added `--host codex` and `--host all` in `scripts/install.py` and `scripts/aoci_hook.py`, supporting `~/.agents/skills/aoci-index/`, `~/.codex/hooks.json` / `<repo>/.codex/hooks.json` (`SessionStart`, `UserPromptSubmit`, `Stop`), `~/.codex/AGENTS.md` / `<repo>/AGENTS.md`, marker-fenced `[mcp_servers.aoci-index]` in `config.toml`, and manual templates (`resources/codex.hooks.*.json`).
- **Single-Pass Sync Pipeline**: Added `prepare-sync` (`aoci_prepare_sync`) and `apply-sync` (`aoci_apply_sync`) to collect sync briefings, outlines, batched git diffs, and execute `restore -> migrate-renames -> ack -> upsert -> verify` in one orchestrated pass.
- **Cross-File Symbol Lookup**: Added `symbol` / `aoci_symbol` searching across `aoci.hubs.txt`, outline cache (or opt-in read-only `--live`), and FRAS `A:` exports.
- **Git Co-Change & Expanded Language Audit**: Added `audit --co-change [--exclude-tests]` coupling analysis plus JVM (Java/Kotlin), C#, Protobuf, and C/C++ import/outline parsing (with monorepo nested `tsconfig.json` and `go.mod` resolution).
- **CI Release Automation**: Added tag-triggered GitHub Release publishing (wheels, sdists, skill `.tar.gz`/`.zip`, `SHA256SUMS.txt`) and automated changelog generation.

### Changed
- **H-Tier Symbol Sub-Volume (`aoci.hubs.txt`)**: Automatic `hubs` registration in `aoci.txt`, span refresh/pruning during `upsert`, rename migration in `migrate-renames`, and `verify`/`lint` line-bound checks without live re-parsing.
- **Interactive HTML Dependency Export**: Enhanced `export --format html` with direction toggle, pan/zoom controls, focus dimming, and safe `</script>` escaping.
- **Documentation & Visual Refresh**: Streamlined `README.md` with Lucide CDN outline icons and a redesigned no-fill `assets/logo.svg`.

### Fixed
- **Cross-Platform CI & Python 3.8 Compatibility**: Fixed f-string backslash syntax in `scripts/aoci_core/graph.py`, parenthesized multi-context `with` in `tests/test_v16_optimizations.py`, Ubuntu 24.04 Python 3.8 matrix target, missing Git test author config in `tests/test_ext_tests.py`, and canonical `Path.resolve()` keys in `aoci_mcp._MemoryIndexCache`.

## [1.4.0] - 2026-11-20

### Changed
- Expanded `README.md`, `pyproject.toml`, and core CLI/MCP documentation for `aoci-index` v1.4.0.

## [1.3.0] - 2026-11-20

### Added
- **Task-Driven Context Packing**: Added `pack` / `aoci_pack` combining BM25, bilingual glossary expansion, path mentions, L1.5 hub sections, and working-tree Git diffs.
- **Test Impact Derivation**: Added `--with-tests` on `context`/`diff`/`pack` and `--tests-for` on `query` (`aoci_context`, `aoci_diff`, `aoci_query`).
- **H-Tier Symbol Zoom**: Added `zoom` / `aoci_zoom` and `.aoci/aoci.hubs.txt` for symbol-level line spans on large hub files.

[Unreleased]: https://github.com/dreamforSh/AOCI-index/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/dreamforSh/AOCI-index/releases/tag/v1.5.0
[1.4.0]: https://github.com/dreamforSh/AOCI-index/releases/tag/v1.4.0
[1.3.0]: https://github.com/dreamforSh/AOCI-index/releases/tag/v1.3.0
