# portfolio-health

MCP server for project portfolio health monitoring. Indexes `project_*.md` memory files via SQLite FTS5 and joins against bridge-db activity to surface active, stale, and unshipped projects.

## Install

```bash
cd /Users/d/Projects/portfolio-health
uv sync
```

## Run (stdio transport for MCP)

```bash
uv run python -m portfolio_health
```

## Tools

| Tool | Description |
|---|---|
| `portfolio_list_active(window_days=14)` | Projects with bridge-db activity in last N days, most-recent first |
| `portfolio_get_project(name)` | Full detail for one project: frontmatter, first section, file path |
| `portfolio_search(query, limit=10)` | FTS5 full-text search across name + description + body |
| `portfolio_stale_candidates(days=90)` | Projects with no activity in N days, excluding abandoned/archived |
| `portfolio_unshipped()` | Ship-ready projects (by description pattern) with no SHIPPED tag in 30 days |

## Data sources (read-only)

- **Memory files**: `~/.claude/projects/-Users-d/memory/project_*.md`
- **Bridge-db**: `~/.local/share/bridge-db/bridge.db`

## Index location

`~/.local/share/portfolio-health/index.db` — auto-created, incrementally refreshed on every tool call.

## mcp.json registration

Add to `~/.claude/claude_desktop_config.json` or your CC `mcp.json`:

```json
{
  "mcpServers": {
    "portfolio-health": {
      "command": "uv",
      "args": ["run", "--project", "/Users/d/Projects/portfolio-health", "python", "-m", "portfolio_health"],
      "env": {}
    }
  }
}
```

## Dev

```bash
uv run pytest -v
uv run ruff check src/ tests/
```
