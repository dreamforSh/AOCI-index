# Installation & Tri-Host Lifecycle Hooks Guide (Claude Code + Antigravity + Codex)

The `aoci-index/` directory is a self-contained Agent Skill with zero third-party dependencies (requires Python 3.8+ and optional Git). It supports **Claude Code**, **Antigravity**, and **Codex** out of the box.

---

## 1. One-Command Installation (`scripts/install.py`)

Use [../scripts/install.py](../scripts/install.py) to copy the skill and optionally merge `SessionStart` + `Stop` hooks (with automatic `.bak` backups) and rule snippets without clobbering existing config. Add `--dry-run` to preview changes first.

### Install for Claude Code

- **Global (recommended — available in all repos, invocable via `/aoci-index`)**:
  ```bash
  python scripts/install.py --host claude-code --scope global --with-hooks
  ```
  - Copies skill to `~/.claude/skills/aoci-index/`.
  - Merges `SessionStart`, `UserPromptSubmit`, and `Stop` hooks into `~/.claude/settings.json` (preserving all existing hooks and settings; pass `--no-turn-hook` to omit `UserPromptSubmit`).
  - Add `--with-rules` if you also want the fenced protocol block appended to `~/.claude/CLAUDE.md`.
- **Project-scoped**:
  ```bash
  python scripts/install.py --host claude-code --scope project --project-root <repo> --with-hooks --with-rules
  ```
  - Installs into `<repo>/.claude/skills/aoci-index/`, `<repo>/.claude/settings.json` (using a shell-neutral Python `CLAUDE_PROJECT_DIR` runner that works identically on Windows `cmd.exe`, PowerShell, and POSIX `sh`), and `<repo>/CLAUDE.md`.

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
  - **Trust requirement**: in Codex CLI, review and trust project-local hooks via `/hooks` before first execution.

### Install for Multiple Hosts at Once (with Optional Stdio MCP Server or Plugin Bundle)

```bash
# Install for Claude Code + Antigravity:
python scripts/install.py --host both --scope global --with-hooks --with-mcp

# Install for Claude Code + Antigravity + Codex:
python scripts/install.py --host all --scope global --with-hooks --with-mcp
```

- `--with-mcp` registers the zero-dependency in-memory Stdio MCP server ([../scripts/aoci_mcp.py](../scripts/aoci_mcp.py)) into `~/.gemini/config/mcp_config.json` (Antigravity global), `<repo>/.agents/plugins/aoci-index/mcp_config.json` (Antigravity project — portable via `PLUGIN_ROOT` with `"name": "aoci-index-project-mcp"` in its lightweight wrapper `plugin.json`), `~/.claude.json` / `<repo>/.mcp.json` (Claude Code — portable via `CLAUDE_PROJECT_DIR`), and `~/.codex/config.toml` / `<repo>/.codex/config.toml` (Codex — marker-fenced `[mcp_servers.aoci-index]` TOML table), exposing 25 annotated tools (`aoci_init`, `aoci_overview`, `aoci_query`, `aoci_symbol`, `aoci_context`, `aoci_pack`, `aoci_diff`, `aoci_scan`, `aoci_prepare_sync`, `aoci_apply_sync`, `aoci_outline`, `aoci_scaffold`, `aoci_ack`, `aoci_upsert`, `aoci_migrate_renames`, `aoci_restore`, `aoci_zoom`, `aoci_shard`, `aoci_ensure_git_exclude`, `aoci_install_git_hooks`, `aoci_lint`, `aoci_verify`, `aoci_audit`, `aoci_export`, `aoci_pool`), MCP Resources (`aoci://manifest`, `aoci://meta`, `aoci://code`, `aoci://hubs`, `aoci://shards/INDEX`), URI Templates (`aoci://shards/{shard}`, `aoci://{volume}?root={root}`), and MCP Prompts (`aoci-bootstrap`, `aoci-review-diff`, `aoci-sync`) without shell commands.
- `--as-plugin` (Antigravity) packages the skill, `plugin.json`, `assets/logo.svg`, `hooks.json`, portable `mcp_config.json`, and `rules/AGENTS.md` into a single toggleable plugin directory under `~/.gemini/config/plugins/aoci-index/` (or `<repo>/.agents/plugins/aoci-index/`).

### Clean Uninstall

Surgically removes **only** the `aoci-index` skill **and** its managed hook/rule/MCP entries by default (hooks pointing at a deleted script would break every session), preserving sibling hooks, extra MCP servers (including inside Antigravity plugin `mcp_config.json`), custom rule text, and extra files in the plugin directory. Add `--keep-config` to remove only the skill directory:

```bash
python scripts/install.py --host both --scope global --uninstall
```

### Re-installs, Interpreter, and Tests

- Each install writes `.aoci-install.json` (file hashes). Re-installing over a copy you edited locally — or over an existing non-empty directory without `.aoci-install.json` whose ownership cannot be verified (e.g., manual install) — aborts unless you pass `--force`.
- Global hooks default to the absolute base interpreter that ran the installer (never a virtualenv's interpreter, and never the Windows Store `python` stub). Project-scoped hooks keep portable `python`/`python3`. Override either default with `--python-cmd`.
- Regression suite (stdlib `unittest`, needs `git`): `python tests/test_aoci.py`.
- Bytecode cache: when `aoci_index.py`, `aoci_hook.py`, or `aoci_mcp.py` run as scripts, they cache `.pyc` files under a per-user `sys.pycache_prefix` (`%LOCALAPPDATA%\aoci-index\pycache`, `~/Library/Caches/aoci-index/pycache`, or `${XDG_CACHE_HOME:-~/.cache}/aoci-index/pycache`). Nothing is ever written into the installed skill directory, so `.aoci-install.json` hash checks stay clean. Disable it with `AOCI_NO_PYCACHE=1`, `python -B`, or `PYTHONDONTWRITEBYTECODE=1`. On Windows without long-path support, very deep install paths fall back to no cache.

---

## 2. How Tri-Host Lifecycle Hooks Work (`scripts/aoci_hook.py`)

[../scripts/aoci_hook.py](../scripts/aoci_hook.py) implements all three hosts' exact JSON stdin/stdout contracts (`--host auto | claude-code | antigravity | codex`):

| Hook Event | Trigger | Automatic Behavior | Claude Code Output | Codex Output | Antigravity Output |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`SessionStart`** | Session startup, resume, or post-compaction (`source == "compact"`) when `.aoci/aoci.code.txt` exists | 1) Ensures `.git/info/exclude` protects `.aoci/`;<br>2) Zero-read fast drift check (+ 0-token restorable count from the SQLite semantic pool);<br>3) Surfaces `#Global-Invariants:` and enforces **Read-Whole-First**;<br>4) Snapshots pre-existing drift to `.aoci/.sessions/<session_id or conversationId>.json` (first start only; resume/compact keep it). | `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` | `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` | `{"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}` |
| **`UserPromptSubmit` / `PreInvocation`** *(optional `--event turn-start`)* | Start of each new user turn when `.aoci/` exists | Refreshes the session's external-drift snapshot before the agent runs any tools: Claude Code & Codex `UserPromptSubmit` refresh unconditionally once per turn; Antigravity `PreInvocation` refreshes only when `invocationNum <= 1` (stateless — no flag can linger after `Ctrl+C`). Pure-Python `.aoci` check runs before any `git` call. | `{}` | `{}` | `{}` |
| **`Stop`** | Normal turn completion when `.aoci/` exists | Zero-read fast drift check (added / modified / renamed / deleted / exact malformed-line multiset) + cached Git exclusion check. **Session-scoped**: drift that existed at session/turn start and is still byte-identical is not counted, and resolved exemptions are retired when a turn finishes cleanly. Loop-guarded by `stop_hook_active` (Claude Code / Codex) and `continuationActive` (Antigravity); Antigravity stops with `fullyIdle: false` (background tasks running) are never gated. | `{"decision": "block", "reason": "..."}` (or `{}` when clean) | `{"decision": "block", "reason": "..."}` (or `{}` when clean — never emits `hookSpecificOutput`) | `{"decision": "continue", "reason": "..."}` (or `{}` when clean) |

> In workspaces **without** `.aoci/`, all hooks return `{}` immediately with zero overhead and zero prompts.
> In Claude Code and Codex, `SessionStart` also fires after context compaction, automatically re-injecting the Read-Whole-First reminder and Global Invariants so context never drifts.
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

### Codex Manual Setup

1. Copy `aoci-index/` to `~/.agents/skills/aoci-index/` (or `<repo>/.agents/skills/aoci-index/`).
2. Optional hooks: merge [../resources/codex.hooks.global.json](../resources/codex.hooks.global.json) into `~/.codex/hooks.json` (on Windows, use `python "C:/Users/<you>/.agents/skills/aoci-index/scripts/aoci_hook.py"`), or merge [../resources/codex.hooks.project.json](../resources/codex.hooks.project.json) into `<repo>/.codex/hooks.json` (then trust via `/hooks` inside Codex CLI).
3. Optional rule snippet: append [../resources/AGENTS.snippet.md](../resources/AGENTS.snippet.md) to `~/.codex/AGENTS.md` or `<repo>/AGENTS.md`.
4. Optional MCP server: add `[mcp_servers.aoci-index]` to `~/.codex/config.toml` or `<repo>/.codex/config.toml` pointing to `scripts/aoci_mcp.py`.

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

### Test Antigravity Hook Contract

```powershell
'{"cwd":"<your-indexed-repo>","conversationId":"c1","hookEventName":"SessionStart"}' | python .\scripts\aoci_hook.py --host antigravity --event session-start
'{"cwd":"<your-indexed-repo>","conversationId":"c1","hookEventName":"Stop","terminationReason":"NO_TOOL_CALL","continuationActive":false,"fullyIdle":true}' | python .\scripts\aoci_hook.py --host antigravity --event stop
```

---

## 5. Local Git Hooks (`aoci_index.py install-git-hooks`)

To auto-restore cached FRAS entries from the SQLite WAL pool immediately after `git checkout`, `git merge`, or `git rebase` (and optionally reject accidentally staged `.aoci/` files on `git commit`), install idempotent marker-fenced Git hooks:

```bash
python scripts/aoci_index.py install-git-hooks --root <repo> [--with-pre-commit] [--pre-commit-verify]
```

- Respects `git config --get core.hooksPath` (resolving relative paths from the Git toplevel) and falls back to `<git-common-dir>/hooks`.
- Preserves any existing user or Husky hook content outside the `# >>> aoci-index git-hook` / `# <<< aoci-index git-hook` marker block.
- Remove cleanly with `python scripts/aoci_index.py install-git-hooks --root <repo> --uninstall`.
