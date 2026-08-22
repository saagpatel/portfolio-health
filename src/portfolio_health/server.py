"""MCP server — 5 portfolio-health tools using FastMCP-style decorators."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from portfolio_health.indexer import (
    _PROJECTS_ROOT,
    DEFAULT_INDEX_PATH,
    DEFAULT_MEMORY_DIR,
    _refresh_git_recency,
    maybe_refresh,
    open_index,
)
from portfolio_health.queries import (
    DEFAULT_BRIDGE_PATH,
    get_project,
    list_active,
    search_projects,
    stale_candidates,
    unshipped,
)

mcp = FastMCP("portfolio-health")

# Module-level connection — opened once per server process, reused across calls.
_conn = None
_memory_dir: Path = DEFAULT_MEMORY_DIR
_bridge_path: Path = DEFAULT_BRIDGE_PATH


def _get_conn():
    global _conn
    if _conn is None:
        _conn = open_index(DEFAULT_INDEX_PATH)
        # One-time git-recency populate at process start: closes the cold-start gap
        # where a freshly migrated index has a NULL last_git_commit_ts column and
        # stale_candidates would briefly miss git-active rescues until a memory edit.
        _refresh_git_recency(_conn, _PROJECTS_ROOT)
    # Ongoing: markdown-change-triggered refresh also refreshes git recency.
    maybe_refresh(_conn, _memory_dir, projects_root=_PROJECTS_ROOT)
    return _conn


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def portfolio_list_active(window_days: int = 14) -> list[dict]:
    """Return projects with bridge-db activity in the last N days, most recent first.

    Each item: {name, last_activity_ts, last_activity_summary, activity_count}.
    """
    conn = _get_conn()
    return list_active(conn, window_days=window_days, bridge_path=_bridge_path)


@mcp.tool()
def portfolio_get_project(name: str) -> dict:
    """Return full detail for a project by name.

    Returns: {name, description, file_path, frontmatter, first_section}.
    Returns {error: "not found"} if no matching project_<name>.md exists.
    """
    conn = _get_conn()
    return get_project(conn, name)


@mcp.tool()
def portfolio_search(query: str, limit: int = 10) -> list[dict]:
    """Full-text search across project name, description, and body.

    Each result: {name, description, snippet, rank}.
    FTS5 special characters are sanitized automatically.
    """
    conn = _get_conn()
    return search_projects(conn, query, limit=limit)


@mcp.tool()
def portfolio_stale_candidates(days: int = 90) -> list[dict]:
    """Return projects with no bridge-db activity in N days, excluding abandoned/archived.

    Each item: {name, description, status, days_since_last_activity, file_path}.
    Sorted longest-stale first.
    """
    conn = _get_conn()
    return stale_candidates(conn, days=days, bridge_path=_bridge_path)


@mcp.tool()
def portfolio_unshipped() -> list[dict]:
    """Return ship-ready-looking projects carrying no SHIPPED activity tag in 30 days.

    NOT deployment status. A result means the memory description claims ship-ready
    ('v1.0 complete/done/ready', 'deploy-ready', 'launch-ready', 'all phases
    done/complete') and bookkeeping shows no recent SHIPPED event. Both inputs are
    prose and records, not reality: a project already live for months reads identically
    to one never deployed, if nobody logged the event or refreshed the description.

    Each item: {name, description, file_path, shipped_ever, last_shipped_ts,
    days_since_last_shipped, repo_dir, deploy_target, basis}. Read 'deploy_target' and 'basis'
    before acting: a non-null deploy_target means the repo is linked to a deployment
    and is probably already live. Confirm against the live URL either way.
    """
    conn = _get_conn()
    return unshipped(conn, bridge_path=_bridge_path)
