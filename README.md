# portfolio-health

MCP server for project portfolio health monitoring. Indexes `project_*.md` memory files via SQLite FTS5 and joins against bridge-db activity to surface active, stale, and unshipped projects.

`portfolio-health` is active-infra: a read-oriented local MCP helper and cache/query
surface. It is not the portfolio source of truth. Treat bridge-db, memory files,
and generated portfolio truth as upstream evidence; use this repo to inspect and
cross-check those sources.

## Install

```bash
cd /Users/d/Projects/portfolio-health
uv sync
```

## Run (stdio transport for MCP)

```bash
uv run python -m portfolio_health
```

The installed script exposes the same MCP server by default:

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
| `portfolio_unshipped()` | Ship-ready projects (by description pattern) with no SHIPPED tag in 30 days |

## Health report

Use the health command to check cache/source alignment before audits or MCP
registration work:

```bash
uv run portfolio-health health
```

The default report reads the existing cache. For a live-safe source smoke that
does not mutate the default cache, point the command at a temp index:

```bash
tmp_index="$(mktemp -t portfolio-health.XXXXXX.db)"
uv run portfolio-health health \
  --index-path "$tmp_index" \
  --memory-dir "$HOME/.claude/projects/-Users-d/memory" \
  --bridge-path "$HOME/.local/share/bridge-db/bridge.db" \
  --full-rebuild \
  --json
rm -f "$tmp_index"
```

The report includes memory file count, cache row count, FTS row count, duplicate
file paths/slugs, stale or missing cache paths, bridge-db activity row count, and
latest bridge-db activity timestamp.

## Data sources (read-only)

- **Memory files**: `~/.claude/projects/-Users-d/memory/project_*.md`
- **Bridge-db**: `~/.local/share/bridge-db/bridge.db`

## Index location

`~/.local/share/portfolio-health/index.db` — auto-created, incrementally refreshed on every tool call.

The index is disposable cache. Rebuild or delete it when it drifts; do not treat
it as source truth.

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
uv run pytest -q
uv run ruff check src/ tests/
```
