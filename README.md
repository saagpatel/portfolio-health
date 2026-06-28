# portfolio-health

[![CI](https://github.com/saagpatel/portfolio-health/actions/workflows/ci.yml/badge.svg)](https://github.com/saagpatel/portfolio-health/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

MCP server for project portfolio health monitoring. Indexes `project_*.md` memory files via SQLite FTS5 and joins against bridge-db activity to surface active, stale, and unshipped projects.

`portfolio-health` is a read-oriented local MCP helper and cache/query surface. It is not the portfolio source of truth. Treat bridge-db, memory files, and generated portfolio truth as upstream evidence; use this repo to inspect and cross-check those sources.

## Prerequisites

- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)** (`pip install uv` or `brew install uv`)
- **Claude Code** installed locally — this server reads two data sources that Claude Code writes:
  - **Memory files**: `~/.claude/projects/<encoded-home>/memory/project_*.md`
  - **Bridge-db**: `~/.local/share/bridge-db/bridge.db` (written by the bridge-db MCP server)

> **What is `<encoded-home>`?** Claude Code stores per-project memory under `~/.claude/projects/` and encodes your home directory path into the folder name by replacing every `/` with `-`. For example, if your home is `/Users/alice`, the folder becomes `-Users-alice`. The server detects this automatically at startup — you do not need to configure it by hand. If auto-detection fails (no `project_*.md` files found), set the `PORTFOLIO_HEALTH_MEMORY_DIR` environment variable to the full path of your memory directory.

## Install

```bash
git clone https://github.com/saagpatel/portfolio-health.git
cd portfolio-health
uv sync
```

## Run (stdio transport for MCP)

```bash
uv run python -m portfolio_health
```

The installed script exposes the same MCP server:

```bash
uv run portfolio-health
```

## Tools

| Tool | Description |
|---|---|
| `portfolio_list_active(window_days=14)` | Projects with bridge-db activity in last N days, most-recent first |
| `portfolio_get_project(name)` | Full detail for one project: frontmatter, first section, file path |
| `portfolio_search(query, limit=10)` | FTS5 full-text search across name + description + body |
| `portfolio_stale_candidates(days=90)` | Projects with no activity in N days, excluding abandoned/archived |
| `portfolio_unshipped()` | Ship-ready projects with no `SHIPPED` bridge-db tag in 30 days |

### `portfolio_unshipped` — what it matches

`portfolio_unshipped` scans project descriptions for phrases that signal the project is done but not yet logged as shipped. It matches descriptions containing any of:

- `v1.0 complete`, `v1.0 done`, `v1.0 ready`
- `deploy-ready`
- `launch-ready`
- `all phases done`, `all phases complete`

Projects are excluded from the result if a bridge-db `activity_log` row with `SHIPPED` in its `tags` field exists within the last 30 days.

## Health report

Use the health command to check cache/source alignment before audits or MCP registration work:

```bash
uv run portfolio-health health
```

For a live-safe smoke that does not mutate the default cache, point the command at a temp index:

```bash
tmp_index="$(mktemp -t portfolio-health.XXXXXX.db)"
uv run portfolio-health health \
  --index-path "$tmp_index" \
  --memory-dir "$HOME/.claude/projects/$(python3 -c "import os; h=os.path.expanduser('~').lstrip('/'); print('-' + h.replace('/', '-'))")/memory" \
  --bridge-path "$HOME/.local/share/bridge-db/bridge.db" \
  --full-rebuild \
  --json
rm -f "$tmp_index"
```

The report includes memory file count, cache row count, FTS row count, duplicate file paths/slugs, stale or missing cache paths, bridge-db activity row count, and latest bridge-db activity timestamp.

## Data sources (read-only)

- **Memory files**: `~/.claude/projects/<encoded-home>/memory/project_*.md` — auto-detected at startup
- **Bridge-db**: `~/.local/share/bridge-db/bridge.db`

## Index location

`~/.local/share/portfolio-health/index.db` — auto-created, incrementally refreshed on every tool call. Disposable cache; rebuild or delete it when it drifts.

## MCP registration

Add to your Claude Code `mcp.json` or `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "portfolio-health": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/portfolio-health", "python", "-m", "portfolio_health"],
      "env": {}
    }
  }
}
```

Replace `/path/to/portfolio-health` with the absolute path to your clone. If the memory directory is not auto-detected, or bridge-db lives outside the default `~/.local/share` location, set the matching env vars:

```json
{
  "mcpServers": {
    "portfolio-health": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/portfolio-health", "python", "-m", "portfolio_health"],
      "env": {
        "PORTFOLIO_HEALTH_MEMORY_DIR": "/path/to/.claude/projects/<encoded-home>/memory",
        "PORTFOLIO_HEALTH_BRIDGE_DB": "/path/to/.local/share/bridge-db/bridge.db"
      }
    }
  }
}
```

`PORTFOLIO_HEALTH_BRIDGE_DB` overrides the bridge-db path (default `~/.local/share/bridge-db/bridge.db`); `PORTFOLIO_HEALTH_MEMORY_DIR` overrides the memory directory.

## Dev

```bash
uv run pytest -q
uv run ruff check src/ tests/
```
