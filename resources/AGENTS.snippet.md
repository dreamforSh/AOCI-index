## AOCI Codebase Index Protocol

- **Git Exclusion**: `.aoci/` is a local-only index repository excluded via `.git/info/exclude`. Never `git add` or commit `.aoci/`.
- **MCP-First Routing**: When `aoci_*` MCP tools are available, prefer calling them directly (always passing `root: "<repo>"`) over running shell commands.
- **Read-Whole-First**: When `.aoci/aoci.code.txt` exists, ALWAYS read `.aoci/aoci.txt`, `.aoci/aoci.meta.txt`, and `.aoci/aoci.code.txt` (or call `aoci_overview` + `aoci_context` / `aoci_query`, or `aoci_index.py overview` + `context` / `query` on >600-file repos) BEFORE exploring or editing source files.
- **Sync After Code Updates**: Whenever source files are added, modified, renamed, or deleted, activate the `aoci-index` skill to run `aoci_scan` / `scan`, then `aoci_upsert` / `upsert` revised entries (or `aoci_ack` / `ack` files whose entries are still accurate, `aoci_migrate_renames` / `migrate-renames` for moves), and pass `aoci_verify` / `verify` before finishing.
