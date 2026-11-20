## AOCI Codebase Index Protocol

- **Git Exclusion**: `.aoci/` is a local-only index repository excluded via `.git/info/exclude`. Never `git add` or commit `.aoci/`.
- **MCP-First Routing**: When `aoci_*` MCP tools are available, prefer calling them directly (always passing `root: "<repo>"`) over running shell commands.
- **Read-Whole-First**: Whenever `.aoci/aoci.code.txt` exists, ALWAYS read `.aoci/aoci.txt`, `.aoci/aoci.meta.txt`, and `.aoci/aoci.code.txt` (or call `aoci_overview` + `aoci_context` / `aoci_query`, or `aoci_index.py overview` + `context` / `query` on >600-file repos) BEFORE grepping, reading, or editing source files.
- **Sync After Code Updates**: Whenever source files are added, edited, renamed, or deleted, invoke `/aoci-index` (using `aoci_*` MCP tools or `aoci_index.py scan` -> `upsert` / `ack` / `migrate-renames` -> `verify`) before concluding.
