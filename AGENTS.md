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

<!-- portfolio-context:start -->
# Portfolio Context

## What This Project Is

portfolio-health: MCP server for project portfolio health monitoring. Indexes `project_*.md` memory files via SQLite FTS5 and joins against bridge-db activity to surface active, stale, and unshipped projects.

## Current State

This is active-infra support for portfolio readbacks. The repo has a focused
README, test suite, lint target, and health command. Treat generated portfolio
truth, bridge-db, and Claude memory files as upstream evidence; this repo's
SQLite index is cache only.

## Stack

- Primary stack: Python

## How To Run

- `uv run pytest -q`
- `uv run ruff check src/ tests/`
- `uv run python -m portfolio_health`
- `uv run portfolio-health health`

## Known Risks

- Live memory and bridge-db paths can drift, so tests should remain
  fixture-backed and health checks should use temp indexes when probing live
  state.
- Do not add writeback behavior here without an explicit boundary change; the
  repo is intentionally read-oriented.

## Next Recommended Move

Keep the README, AGENTS boundary, and MCP health command aligned. If this repo
receives more operator-facing work, add fixture coverage before touching live
portfolio state.

<!-- portfolio-context:end -->
