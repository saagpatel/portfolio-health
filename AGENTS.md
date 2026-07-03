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

Portfolio truth currently marks this project as `active` with `boilerplate` context. Phase 104 recovered minimum-viable context so future sessions can resume without rediscovery.

## Stack

- Primary stack: Python

## How To Run

- Review the README and top-level scripts before the next session; this repo does not yet expose one canonical run command inside the new context block.

## Known Risks

- This repo only has minimum-viable recovery context today; deeper handoff details may still live in the README and supporting docs.

## Next Recommended Move

Use this context plus the README and supporting docs to resume the next active task, then promote the repo beyond minimum-viable by capturing a dedicated handoff, roadmap, or discovery artifact.

<!-- portfolio-context:end -->
