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

portfolio-health is an active local project in the /Users/d/Projects portfolio.

## Current State

Portfolio truth generated on 2026-06-19 marks this project as `active-infra`
with minimum-viable context. PR #3 fixed disposable-cache reconciliation and
bridge activity noise handling. Keep this repo as a read-oriented MCP helper,
not a replacement source of portfolio truth.

## Stack

- Primary stack: Python

## How To Run

- MCP server: `uv run python -m portfolio_health`
- Health report: `uv run portfolio-health health`
- Live-safe source smoke: use `portfolio-health health --index-path <temp-db> --full-rebuild`
  so the default live cache is not mutated.

## Known Risks

- The local SQLite index is disposable cache; bridge-db, memory files, and
  generated portfolio truth remain upstream evidence sources.
- This repo overlaps with GithubRepoAuditor and portfolio-intelligence only as
  a query/signal helper. Do not let it become a second portfolio truth generator.
- MCP registration is documented but should not be changed casually from this
  repo.

## Next Recommended Move

Before audits or integrations, run the health report against a temp index and
confirm memory-file count, cache rows, duplicate paths/slugs, and latest
bridge-db activity timestamp.

<!-- portfolio-context:end -->
