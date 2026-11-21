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
- **Monorepo Scale Optimizations**: Added dual positive/negation union regexes in `IgnoreMatcher`, `O(1)` Python suffix index + single-pass comment stripping in `parsers.py`, zero-stat dangling-ref checks + single-parse `verify`, bucketed `TestImpactIndex`, composite PK row-value SQLite pool lookups, same-thread re-entrant `IndexLock` across `apply-sync`, and per-root locks + short-TTL diff caching in `aoci_mcp.py`.

### Fixed
- **Packaging, MCP & Sync Hardening**: Moved `install` into `aoci_core.installer` with embedded wheel `_skill_bundle` (`setup.py`), fixed Windows `%3A` file URIs and thread-safe stdio isolation in `aoci_mcp.py`, synced `aoci.hubs.txt` on `restore`, decoupled `apply-sync --ack-impl-only` + `--ack-path`, added UTF-8 BOM handling across volumes/parsers, and enforced read-only `?mode=ro` SQLite pool queries.

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
