# AGENTS.md

## Repository Guidance

`portfolio-health` is a read-oriented MCP server for portfolio health signals.
It indexes project memory files into a local SQLite FTS5 database and joins
those indexed projects with bridge-db activity. Keep this repo focused on
inspection and query behavior; do not mutate memory files, bridge-db, repo
state, or external services from here.

## Data Boundaries

- Memory project files and bridge-db are read-only inputs for this repo.
- The local portfolio-health index is disposable cache, not source truth.
- Unit tests should stay fixture-backed and must not require live memory files
  or live bridge-db state.

## Verification

After code, query, parser, or MCP server changes, run:

```bash
uv run pytest -q
uv run ruff check src/ tests/
```

For manual runtime checks, use:

```bash
uv run python -m portfolio_health
```

