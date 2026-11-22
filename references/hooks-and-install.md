# Installation & Multi-Host Lifecycle Hooks Guide (Claude Code + Antigravity + Codex + Cursor + Windsurf + VS Code)

The `aoci-index/` directory is a self-contained Agent Skill with zero third-party dependencies (requires Python 3.8+ and optional Git). It supports six hosts out of the box (current as of **v1.6.0**):

| Host | `--host` | Skill | Lifecycle hooks | Rules | MCP |
| :--- | :--- | :---: | :---: | :---: | :---: |
| Claude Code | `claude-code` | yes | `SessionStart`, `UserPromptSubmit`, `Stop` | `CLAUDE.md` | yes |
| Antigravity | `antigravity` | yes | `SessionStart`, `Stop` (+ optional `PreInvocation`) | `GEMINI.md` / `AGENTS.md` | yes |
| Codex | `codex` | yes | `SessionStart`, `UserPromptSubmit`, `Stop` | `AGENTS.md` | yes |
| Cursor | `cursor` | yes | `sessionStart`, `beforeSubmitPrompt`, `stop` | `.cursor/rules/aoci-index.mdc` | yes |
| Windsurf | `windsurf` | yes | — | `global_rules.md` / `.windsurf/rules/` | yes |
| VS Code / GitHub Copilot | `vscode` | yes | — | `copilot-instructions.md` | yes |

`--host both` installs Claude Code + Antigravity; `--host all` installs all six. `--with-hooks` is ignored for Windsurf and VS Code, which have no lifecycle hooks.

---

## 1. One-Command Installation (`scripts/install.py`)

Use [../scripts/install.py](../scripts/install.py) (also the `aoci-install` console script; the implementation lives in `aoci_core.installer`) to copy the skill and optionally merge lifecycle hooks (with automatic `.bak` backups), rule snippets, and MCP registrations without clobbering existing config. Add `--dry-run` to preview changes first.

### Install for Claude Code

- **Global (recommended — available in all repos, invocable via `/aoci-index`)**:
  ```bash
  python scripts/install.py --host claude-code --scope global --with-hooks
  ```
  - Copies skill to `~/.claude/skills/aoci-index/`.
  - Merges `SessionStart`, `UserPromptSubmit`, and `Stop` hooks into `~/.claude/settings.json` (preserving all existing hooks and settings; pass `--no-turn-hook` to omit `UserPromptSubmit`).
  - Add `--with-rules` if you also want the fenced protocol block appended to `~/.claude/CLAUDE.md`, and `--with-mcp` to register the MCP server in `~/.claude.json`.
- **Project-scoped**:
  ```bash
  python scripts/install.py --host claude-code --scope project --project-root <repo> --with-hooks --with-rules --with-mcp
  ```
  - Installs into `<repo>/.claude/skills/aoci-index/`, `<repo>/.claude/settings.json` (using a shell-neutral Python `CLAUDE_PROJECT_DIR` runner that works identically on Windows `cmd.exe`, PowerShell, and POSIX `sh`), `<repo>/CLAUDE.md`, and `<repo>/.mcp.json`.

### Install for Antigravity

- **Global (recommended)**:
  ```bash
  python scripts/install.py --host antigravity --scope global --with-hooks
  ```
  - Copies skill to `~/.gemini/config/skills/aoci-index/`.
  - Merges `"aoci-index-guard"` (`SessionStart` + `Stop`) into `~/.gemini/config/hooks.json` (add `--with-turn-hook` to also register `PreInvocation` for mid-session IDE edit snapshotting; add `--with-rules` to append the fenced protocol block to `~/.gemini/GEMINI.md`).
- **Project-scoped**:
  ```bash
  python scripts/install.py --host antigravity --scope project --project-root <repo> --with-hooks --with-rules
  ```
  - Installs into `<repo>/.agents/skills/aoci-index/`, `<repo>/.agents/hooks.json`, and `<repo>/AGENTS.md`.

### Install for Codex

- **Global (recommended — available across all repos)**:
  ```bash
  python scripts/install.py --host codex --scope global --with-hooks --with-mcp
  ```
  - Copies skill to `~/.agents/skills/aoci-index/`.
  - Merges `SessionStart`, `UserPromptSubmit`, and `Stop` hooks under `"hooks"` into `~/.codex/hooks.json` (or `$CODEX_HOME/hooks.json` when `CODEX_HOME` is set; pass `--no-turn-hook` to omit `UserPromptSubmit`).
  - With `--with-mcp`, merges a marker-fenced `[mcp_servers.aoci-index]` table into `~/.codex/config.toml` (`$CODEX_HOME/config.toml`).
  - Add `--with-rules` to append the fenced protocol block to `~/.codex/AGENTS.md`.
- **Project-scoped**:
  ```bash
  python scripts/install.py --host codex --scope project --project-root <repo> --with-hooks --with-rules --with-mcp
  ```
  - Installs into `<repo>/.agents/skills/aoci-index/`, `<repo>/.codex/hooks.json` (using a shell-neutral Python runner that locates `.agents/skills/aoci-index` or `.codex/skills/aoci-index` from CWD or parent directories across `cmd.exe`, PowerShell, and POSIX `sh`), `<repo>/AGENTS.md`, and `<repo>/.codex/config.toml`.
  - The project skill directory and `AGENTS.md` are shared with a project-scoped Antigravity install; uninstalling one host keeps them while the other still uses them.
  - **Trust requirement**: in Codex CLI, review and trust project-local hooks via `/hooks` before first execution.

### Install for Cursor

```bash
python scripts/install.py --host cursor --scope project --project-root <repo> --with-hooks --with-rules --with-mcp
```

- Installs into `<repo>/.cursor/skills/aoci-index/` (global: `~/.cursor/skills/aoci-index/`).
- `--with-hooks` merges `sessionStart`, `beforeSubmitPrompt`, and `stop` entries (`{"command": ..., "timeout": 10}`) into `.cursor/hooks.json` (`"version": 1`), keeping other hooks. Project hooks use a Python runner that walks up from CWD to `.cursor/skills/aoci-index/scripts/aoci_hook.py` and prints `{}` when it is absent. `--no-turn-hook` omits `beforeSubmitPrompt`.
- `--with-rules` writes the dedicated rule file `.cursor/rules/aoci-index.mdc` (`alwaysApply: true`).
- `--with-mcp` registers the server under `mcpServers` in `.cursor/mcp.json` (global: `~/.cursor/mcp.json`).

### Install for Windsurf

```bash
python scripts/install.py --host windsurf --scope global --with-rules --with-mcp
```

- Global: skill in `~/.codeium/windsurf/skills/aoci-index/`, fenced protocol block in `~/.codeium/windsurf/memories/global_rules.md`, MCP in `~/.codeium/windsurf/mcp_config.json`.
- Project: skill in `<repo>/.windsurf/skills/aoci-index/`, dedicated rule `<repo>/.windsurf/rules/aoci-index.md` (`trigger: always_on`), MCP in `<repo>/.windsurf/mcp_config.json`.
- No lifecycle hooks: drift is caught by the rules protocol (`scan` / `verify` before finishing) rather than a `Stop` gate.

### Install for VS Code / GitHub Copilot

```bash
python scripts/install.py --host vscode --scope project --project-root <repo> --with-rules --with-mcp
```

- Project: skill in `<repo>/.github/skills/aoci-index/`, fenced protocol block in `<repo>/.github/copilot-instructions.md`, MCP under `servers` in `<repo>/.vscode/mcp.json`.
- Global: skill in `~/.copilot/skills/aoci-index/`, rules in `~/.copilot/copilot-instructions.md`, MCP in the VS Code user settings directory (`%APPDATA%/Code/User/mcp.json`, `~/Library/Application Support/Code/User/mcp.json`, or `${XDG_CONFIG_HOME:-~/.config}/Code/User/mcp.json`). With a sandboxed `--home-dir`, the path stays inside the sandbox. Install and uninstall also remove the stale `aoci-index` server that versions <= 1.5.0 wrote to `~/.vscode/mcp.json`, which VS Code never reads.
- No lifecycle hooks (same as Windsurf).

> Installed skill copies are never indexed: discovery skips host config directories (`.claude`, `.agents`, `.codex`, `.cursor`, `.windsurf`, `.copilot`, `.codeium`), `<.host>/skills/aoci-index/` trees such as `.github/skills/aoci-index/`, and any directory holding `.aoci-install.json`.

### Install for Multiple Hosts at Once (with Optional Stdio MCP Server or Plugin Bundle)

```bash
# Install for Claude Code + Antigravity:
python scripts/install.py --host both --scope global --with-hooks --with-mcp

# Install for all six hosts (claude-code, antigravity, codex, cursor, windsurf, vscode):
python scripts/install.py --host all --scope global --with-hooks --with-mcp
```

- `--with-mcp` registers the zero-dependency in-memory Stdio MCP server ([../scripts/aoci_mcp.py](../scripts/aoci_mcp.py), also the `aoci-mcp` console script) into:
  - Antigravity: `~/.gemini/config/mcp_config.json` (global) or `<repo>/.agents/plugins/aoci-index/mcp_config.json` (project — portable via `PLUGIN_ROOT` with `"name": "aoci-index-project-mcp"` in its lightweight wrapper `plugin.json`);
  - Claude Code: `~/.claude.json` / `<repo>/.mcp.json` (portable via `CLAUDE_PROJECT_DIR`);
  - Codex: `~/.codex/config.toml` / `<repo>/.codex/config.toml` (marker-fenced `[mcp_servers.aoci-index]` TOML table);
  - Cursor: `~/.cursor/mcp.json` / `<repo>/.cursor/mcp.json`;
  - Windsurf: `~/.codeium/windsurf/mcp_config.json` / `<repo>/.windsurf/mcp_config.json`;
  - VS Code: user `Code/User/mcp.json` / `<repo>/.vscode/mcp.json` (`servers` key).
- The server speaks MCP `2024-11-05`, `2025-03-26`, and `2025-06-18` and exposes 29 annotated tools (`aoci_init`, `aoci_overview`, `aoci_query`, `aoci_symbol`, `aoci_refs`, `aoci_context`, `aoci_pack`, `aoci_diff`, `aoci_scan`, `aoci_prepare_sync`, `aoci_apply_sync`, `aoci_outline`, `aoci_scaffold`, `aoci_ack`, `aoci_upsert`, `aoci_migrate_renames`, `aoci_restore`, `aoci_zoom`, `aoci_shard`, `aoci_ensure_git_exclude`, `aoci_install_git_hooks`, `aoci_lint`, `aoci_doctor`, `aoci_verify`, `aoci_audit`, `aoci_export`, `aoci_pool`, `aoci_batch_pack`, `aoci_ci_check`) with `outputSchema` + `structuredContent`, MCP Resources (`aoci://manifest`, `aoci://meta`, `aoci://code`, `aoci://hubs`, `aoci://shards/INDEX`) with `resources/subscribe`, URI Templates (`aoci://shards/{shard}`, `aoci://{volume}?root={root}`), MCP Prompts (`aoci-bootstrap`, `aoci-review-diff`, `aoci-sync`), and `roots/list` multi-root discovery, all without shell commands. `eval` is CLI-only.
- `--mcp-profile core|sync|full` appends `--profile <name>` to the registered server's args for every host: `core` = 8 read-only tools (`aoci_overview`, `aoci_query`, `aoci_pack`, `aoci_context`, `aoci_symbol`, `aoci_refs`, `aoci_diff`, `aoci_scan`; about 3.3k tool-definition tokens instead of about 10k), `sync` = core + 14 sync/maintenance tools, `full` = default. The server also reads `AOCI_MCP_PROFILE`. Calls to tools outside the profile return `isError` with a hint; use the CLI for those.
- `--as-plugin` (Antigravity) packages the skill, `plugin.json`, `assets/logo.svg`, `hooks.json`, portable `mcp_config.json`, and `rules/AGENTS.md` into a single toggleable plugin directory under `~/.gemini/config/plugins/aoci-index/` (or `<repo>/.agents/plugins/aoci-index/`).

#### MCP Server Environment

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `AOCI_MCP_PROFILE` | `full` | Advertised tool profile (`core`, `sync`, `full`); `--profile` overrides it. |
| `AOCI_MCP_ALLOW_OUTSIDE_ROOT` | unset | MCP tools that write files (`output`, `sarif_output`, `markdown_output`, pool export `file`, `pending_dir` with `clean_pending`) only accept targets inside the repository root and never under `.git/`. `1` lifts only the root rule. The CLI is unrestricted. |
| `AOCI_MCP_WATCH_INTERVAL` | `2` | Seconds between `stat` polls of subscribed `aoci://` resources; changes made outside the server (CLI, Git hooks, editors) push `notifications/resources/updated`. `0` disables the watcher. |
| `AOCI_EMBED_BACKEND` / `AOCI_EMBED_MODEL` | unset | Optional dense channel for `hybrid: true` (`model2vec`; needs the `[embed]` extra). |
| `AOCI_DISABLE_TREESITTER` | unset | `1` forces the regex extractors even when the `[treesitter]` extra is installed. |

### Clean Uninstall

Surgically removes **only** the `aoci-index` skill **and** its managed hook/rule/MCP entries by default (hooks pointing at a deleted script would break every session), preserving sibling hooks, extra MCP servers (including inside Antigravity plugin `mcp_config.json`), custom rule text, and extra files in the plugin directory. Empty host directories that the installer created (for example `.cursor/` or `.github/skills/`) are pruned. Add `--keep-config` to remove only the skill directory:

```bash
python scripts/install.py --host all --scope global --uninstall
```

### Re-installs, Interpreter, and Tests

- Each install writes `.aoci-install.json` (file hashes). Re-installing over a copy you edited locally — or over an existing non-empty directory without `.aoci-install.json` whose ownership cannot be verified (e.g., manual install) — aborts unless you pass `--force`.
- Global hooks default to the absolute base interpreter that ran the installer (never a virtualenv's interpreter, and never the Windows Store `python` stub). Project-scoped hooks keep portable `python`/`python3`. Override either default with `--python-cmd`.
- Regression suite (stdlib `unittest`, needs `git`): `python -B -m unittest discover -s tests -p "test_*.py"` (shared helpers: `tests/aoci_testkit.py`). CI also runs `ruff` + `mypy` and branch coverage (`pip install -e ".[dev]"`).
- Bytecode cache: when `aoci_index.py`, `aoci_hook.py`, or `aoci_mcp.py` run as scripts, they cache `.pyc` files under a per-user `sys.pycache_prefix` (`%LOCALAPPDATA%\aoci-index\pycache`, `~/Library/Caches/aoci-index/pycache`, or `${XDG_CACHE_HOME:-~/.cache}/aoci-index/pycache`). Nothing is ever written into the installed skill directory, so `.aoci-install.json` hash checks stay clean. Disable it with `AOCI_NO_PYCACHE=1`, `python -B`, or `PYTHONDONTWRITEBYTECODE=1`. On Windows without long-path support, very deep install paths fall back to no cache.

---

## 2. How Lifecycle Hooks Work (`scripts/aoci_hook.py`)

[../scripts/aoci_hook.py](../scripts/aoci_hook.py) (also the `aoci-hook` console script) implements four hosts' exact JSON stdin/stdout contracts (`--host auto | claude-code | antigravity | codex | cursor`; `--event auto | session-start | turn-start | stop`). With `auto`, the host is detected from the payload (`conversationId` / `hookEventName` / `continuationActive` → Antigravity, `cursor_version` / `workspace_roots` → Cursor, `turn_id` → Codex, then `CODEX_HOME` / `CLAUDE_PROJECT_DIR` and remaining payload fields), and the event from `hook_event_name` / `hookEventName`. Pass `--host` explicitly in hook commands; the installer always does.

| Hook Event | Trigger | Automatic Behavior | Claude Code Output | Codex Output | Cursor Output | Antigravity Output |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`SessionStart`** / `sessionStart` | Session startup, resume, or post-compaction (`source == "compact"`) when `.aoci/aoci.code.txt` exists | 1) Ensures `.git/info/exclude` protects `.aoci/`;<br>2) Zero-read fast drift check (+ 0-token restorable count from the SQLite semantic pool);<br>3) Surfaces `#Global-Invariants:` and enforces **Read-Whole-First**;<br>4) Snapshots pre-existing drift to `.aoci/.sessions/<session id>.json` (first start only; resume/compact keep it). | `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` | `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` | `{"additional_context": "..."}` | `{"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}` |
| **`UserPromptSubmit`** / `beforeSubmitPrompt` / `PreInvocation` *(`--event turn-start`)* | Start of each new user turn when `.aoci/` exists | Refreshes the session's external-drift snapshot before the agent runs any tools, so edits you made between turns are never blamed on the agent: Claude Code & Codex `UserPromptSubmit` and Cursor `beforeSubmitPrompt` refresh unconditionally once per turn; Antigravity `PreInvocation` refreshes only when `invocationNum <= 1` (stateless — no flag can linger after `Ctrl+C`). Paths blocked at the previous `Stop` stay blocked. Pure-Python `.aoci` check runs before any `git` call. Injects nothing. | `{}` | `{}` | `{}` | `{}` |
| **`Stop`** / `stop` | Normal turn completion when `.aoci/` exists | Zero-read fast drift check (added / modified / renamed / moved+edited / deleted / exact malformed-line and invalid-entry multisets / `aoci.hubs.txt` issues) + cached Git exclusion check. **Session-scoped**: drift that existed at session/turn start and is still byte-identical is not counted, and resolved exemptions are retired when a turn finishes cleanly. Optional **safe auto-heal** (below) runs first. | `{"decision": "block", "reason": "..."}` (or `{}` when clean) | `{"decision": "block", "reason": "..."}` (or `{}` when clean — never emits `hookSpecificOutput`) | `{"followup_message": "...", "decision": "block", "reason": "..."}` (or `{}` when clean) | `{"decision": "continue", "reason": "..."}` (or `{}` when clean) |

> In workspaces **without** `.aoci/`, all hooks return `{}` immediately with zero overhead and zero prompts.
> In Claude Code and Codex, `SessionStart` also fires after context compaction, automatically re-injecting the Read-Whole-First reminder and Global Invariants so context never drifts.
> **Loop protection**: a continuation `Stop` (`stop_hook_active` in Claude Code / Codex, `continuationActive` in Antigravity) blocks again at most once more per streak, and only while the number of unsynced items is still shrinking. Antigravity stops with `terminationReason` other than `NO_TOOL_CALL`, or with `fullyIdle: false` (background tasks running), are never gated. Cursor sends no continuation flag; its follow-up loop is bounded by Cursor's own stop-hook loop limit.
> **Safe auto-heal** (`AOCI_HOOK_AUTO_HEAL=safe`, or `--auto-heal` in the hook command): when *every* session-new drift item is deterministic — exact byte-identical renames, added/missing files with an exact semantic-pool hit, or deletions that leave no dangling `R:`/`A:` refs — `Stop` silently runs `restore -> migrate-renames --exact-only -> upsert --prune-only` and re-checks. Any modified, moved+edited, malformed, invalid, or hub-issue item disables it for that turn. It also re-adds a missing `.git/info/exclude` entry (never when `.aoci/` files are tracked).
> Hook failures still return `{}` (never break a session) but print a traceback to stderr; set `AOCI_HOOK_LOG=<file>` to also append failures to a log file.
> Without a session snapshot (hooks installed mid-session, or no session id in the payload), `Stop` falls back to reporting all drift.

---

## 3. Manual Installation (Without `install.py`)

### Claude Code Manual Setup

1. Copy `aoci-index/` to `~/.claude/skills/aoci-index/` (or `<repo>/.claude/skills/aoci-index/`).
2. Optional hooks: merge [../resources/claude.settings.global.json](../resources/claude.settings.global.json) into `~/.claude/settings.json` (on Windows, replace `python3 ~/.claude/...` with `python "C:/Users/<you>/.claude/skills/aoci-index/scripts/aoci_hook.py"`), or merge [../resources/claude.settings.project.json](../resources/claude.settings.project.json) into `<repo>/.claude/settings.json`.
3. Optional rule snippet: append [../resources/CLAUDE.snippet.md](../resources/CLAUDE.snippet.md) to `CLAUDE.md`.

### Antigravity Manual Setup

1. Copy `aoci-index/` to `~/.gemini/config/skills/aoci-index/` (or `<repo>/.agents/skills/aoci-index/`).
2. Optional hooks: merge [../resources/hooks.global.json](../resources/hooks.global.json) into `~/.gemini/config/hooks.json`, or [../resources/hooks.project.json](../resources/hooks.project.json) into `<repo>/.agents/hooks.json`.
3. Optional rule snippet: append [../resources/AGENTS.snippet.md](../resources/AGENTS.snippet.md) to `AGENTS.md` or `GEMINI.md`.
4. Optional plugin manifest: [../resources/plugin.json](../resources/plugin.json) is the template used by `--as-plugin`.

### Codex Manual Setup

1. Copy `aoci-index/` to `~/.agents/skills/aoci-index/` (or `<repo>/.agents/skills/aoci-index/`).
2. Optional hooks: merge [../resources/codex.hooks.global.json](../resources/codex.hooks.global.json) into `~/.codex/hooks.json` (on Windows, use `python "C:/Users/<you>/.agents/skills/aoci-index/scripts/aoci_hook.py"`), or merge [../resources/codex.hooks.project.json](../resources/codex.hooks.project.json) into `<repo>/.codex/hooks.json` (then trust via `/hooks` inside Codex CLI).
3. Optional rule snippet: append [../resources/AGENTS.snippet.md](../resources/AGENTS.snippet.md) to `~/.codex/AGENTS.md` or `<repo>/AGENTS.md`.
4. Optional MCP server: add `[mcp_servers.aoci-index]` to `~/.codex/config.toml` or `<repo>/.codex/config.toml` pointing to `scripts/aoci_mcp.py`.

### Cursor Manual Setup

1. Copy `aoci-index/` to `~/.cursor/skills/aoci-index/` (or `<repo>/.cursor/skills/aoci-index/`).
2. Optional hooks: add to `.cursor/hooks.json`:
   ```json
   {
     "version": 1,
     "hooks": {
       "sessionStart": [{"command": "python <skill>/scripts/aoci_hook.py --host cursor --event session-start", "timeout": 10}],
       "beforeSubmitPrompt": [{"command": "python <skill>/scripts/aoci_hook.py --host cursor --event turn-start", "timeout": 10}],
       "stop": [{"command": "python <skill>/scripts/aoci_hook.py --host cursor --event stop", "timeout": 10}]
     }
   }
   ```
3. Optional rule: create `.cursor/rules/aoci-index.mdc` with `alwaysApply: true` front matter followed by [../resources/AGENTS.snippet.md](../resources/AGENTS.snippet.md).
4. Optional MCP server: add an `aoci-index` entry under `mcpServers` in `.cursor/mcp.json` running `python <skill>/scripts/aoci_mcp.py`.

### Windsurf & VS Code Manual Setup

1. Copy `aoci-index/` to the host skill directory listed in §1 (`~/.codeium/windsurf/skills/`, `<repo>/.windsurf/skills/`, `~/.copilot/skills/`, or `<repo>/.github/skills/`).
2. Optional rules: append [../resources/AGENTS.snippet.md](../resources/AGENTS.snippet.md) to `~/.codeium/windsurf/memories/global_rules.md`, `<repo>/.windsurf/rules/aoci-index.md` (front matter `trigger: always_on`), or `.github/copilot-instructions.md`.
3. Optional MCP server: register `python <skill>/scripts/aoci_mcp.py` under `mcpServers` in Windsurf `mcp_config.json`, or under `servers` in VS Code `mcp.json`.

---

## 4. Manual Hook Contract Verification

### Test Claude Code Hook Contract

```powershell
'{"cwd":"<your-indexed-repo>","session_id":"s1","hook_event_name":"SessionStart","source":"compact"}' | python .\scripts\aoci_hook.py --host claude-code --event session-start
'{"cwd":"<your-indexed-repo>","session_id":"s1","hook_event_name":"Stop","stop_hook_active":false}' | python .\scripts\aoci_hook.py --host claude-code --event stop
```

### Test Codex Hook Contract

```powershell
'{"cwd":"<your-indexed-repo>","session_id":"s1","hook_event_name":"SessionStart","source":"startup","model":"gpt-5","permission_mode":"default"}' | python .\scripts\aoci_hook.py --host codex --event session-start
'{"cwd":"<your-indexed-repo>","session_id":"s1","turn_id":"t1","hook_event_name":"UserPromptSubmit","prompt":"check drift"}' | python .\scripts\aoci_hook.py --host codex --event turn-start
'{"cwd":"<your-indexed-repo>","session_id":"s1","turn_id":"t1","hook_event_name":"Stop","stop_hook_active":false,"last_assistant_message":null}' | python .\scripts\aoci_hook.py --host codex --event stop
```

### Test Cursor Hook Contract

```powershell
'{"conversation_id":"c1","hook_event_name":"sessionStart","workspace_roots":["<your-indexed-repo>"]}' | python .\scripts\aoci_hook.py --host cursor --event session-start
'{"conversation_id":"c1","hook_event_name":"stop","workspace_roots":["<your-indexed-repo>"]}' | python .\scripts\aoci_hook.py --host cursor --event stop
```

### Test Antigravity Hook Contract

```powershell
'{"cwd":"<your-indexed-repo>","conversationId":"c1","hookEventName":"SessionStart"}' | python .\scripts\aoci_hook.py --host antigravity --event session-start
'{"cwd":"<your-indexed-repo>","conversationId":"c1","hookEventName":"Stop","terminationReason":"NO_TOOL_CALL","continuationActive":false,"fullyIdle":true}' | python .\scripts\aoci_hook.py --host antigravity --event stop
```

### Test Safe Auto-Heal

```powershell
$env:AOCI_HOOK_AUTO_HEAL = "safe"
'{"cwd":"<your-indexed-repo>","session_id":"s1","hook_event_name":"Stop","stop_hook_active":false}' | python .\scripts\aoci_hook.py --host claude-code --event stop
```

---

## 5. Local Git Hooks (`aoci_index.py install-git-hooks`)

To auto-restore cached FRAS entries (and `aoci.hubs.txt` sections) from the SQLite WAL pool immediately after `git checkout`, `git merge`, or `git rebase` (and optionally reject accidentally staged `.aoci/` files on `git commit`), install idempotent marker-fenced Git hooks (MCP: `aoci_install_git_hooks`):

```bash
python scripts/aoci_index.py install-git-hooks --root <repo> [--with-pre-commit] [--pre-commit-verify]
```

- Respects `git config --get core.hooksPath` (resolving relative paths from the Git toplevel) and falls back to `<git-common-dir>/hooks`.
- Preserves any existing user or Husky hook content outside the `# >>> aoci-index git-hook` / `# <<< aoci-index git-hook` marker block.
- Remove cleanly with `python scripts/aoci_index.py install-git-hooks --root <repo> --uninstall`.

---

## 6. CI Integration (`ci-check` / `action.yml`)

`ci-check` (MCP `aoci_ci_check`) is an architecture guard that also runs in zero-index CI checkouts, where it synthesizes the static import graph in memory: pass `--pool-bundle <file>` to overlay FRAS semantics from a committed `aoci-semantic-pool-v1` bundle (nothing is written to the checkout), and `--base-rev <rev>` to derive PR diff impact and impacted tests. It checks layer rules, code-level dependency cycles (`R`/`O`-layer co-read pairs ignored), and static imports vs FRAS, and emits `text`, `json`, `github` (workflow annotations), `sarif` (2.1.0), or `markdown` (PR report). The repository root [../action.yml](../action.yml) wraps it as a GitHub Action. Self-check example:

```bash
python scripts/aoci_index.py ci-check --root . --pool-bundle examples/self-pool.json.gz --exclude-tests
```
