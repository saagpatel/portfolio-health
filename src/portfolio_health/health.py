"""Health reporting for portfolio-health cache/source alignment."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from portfolio_health.indexer import (
    DEFAULT_INDEX_PATH,
    DEFAULT_MEMORY_DIR,
    build_full_index,
    open_index,
    refresh_index,
)
from portfolio_health.queries import DEFAULT_BRIDGE_PATH


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _duplicate_values(conn: sqlite3.Connection, column: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {column} AS value, COUNT(*) AS count
        FROM projects
        WHERE {column} IS NOT NULL AND {column} != ''
        GROUP BY {column}
        HAVING COUNT(*) > 1
        ORDER BY count DESC, value
        """
    ).fetchall()
    return [{"value": row["value"], "count": row["count"]} for row in rows]


def _read_index_health(
    conn: sqlite3.Connection,
    memory_paths: set[str] | None,
) -> dict[str, Any]:
    if not _table_exists(conn, "projects"):
        return {
            "schema_present": False,
            "project_row_count": 0,
            "fts_row_count": 0,
            "distinct_file_paths": 0,
            "distinct_slugs": 0,
            "duplicate_file_paths": [],
            "duplicate_slugs": [],
            "stale_cached_paths": None,
            "missing_cached_files": None,
        }

    project_row_count = int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
    fts_row_count = 0
    if _table_exists(conn, "projects_fts"):
        fts_row_count = int(conn.execute("SELECT COUNT(*) FROM projects_fts").fetchone()[0])

    cached_paths = {
        row["file_path"]
        for row in conn.execute(
            "SELECT file_path FROM projects WHERE file_path IS NOT NULL AND file_path != ''"
        ).fetchall()
    }
    stale_cached_paths = None
    missing_cached_files = None
    if memory_paths is not None:
        stale_cached_paths = len(cached_paths - memory_paths)
        missing_cached_files = len(memory_paths - cached_paths)

    distinct_slug_count = conn.execute(
        "SELECT COUNT(DISTINCT slug) FROM projects WHERE slug IS NOT NULL"
    ).fetchone()[0]

    return {
        "schema_present": True,
        "project_row_count": project_row_count,
        "fts_row_count": fts_row_count,
        "distinct_file_paths": len(cached_paths),
        "distinct_slugs": int(distinct_slug_count),
        "duplicate_file_paths": _duplicate_values(conn, "file_path"),
        "duplicate_slugs": _duplicate_values(conn, "slug"),
        "stale_cached_paths": stale_cached_paths,
        "missing_cached_files": missing_cached_files,
    }


def _read_bridge_health(bridge_path: Path) -> dict[str, Any]:
    bridge = _open_readonly(bridge_path)
    if bridge is None:
        return {
            "path": str(bridge_path),
            "exists": False,
            "activity_row_count": 0,
            "latest_activity_timestamp": None,
        }

    try:
        if not _table_exists(bridge, "activity_log"):
            return {
                "path": str(bridge_path),
                "exists": True,
                "activity_row_count": 0,
                "latest_activity_timestamp": None,
            }
        row = bridge.execute(
            "SELECT COUNT(*) AS count, MAX(timestamp) AS latest FROM activity_log"
        ).fetchone()
        return {
            "path": str(bridge_path),
            "exists": True,
            "activity_row_count": int(row["count"]),
            "latest_activity_timestamp": row["latest"],
        }
    finally:
        bridge.close()


def collect_health(
    *,
    index_path: Path = DEFAULT_INDEX_PATH,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
    bridge_path: Path = DEFAULT_BRIDGE_PATH,
    refresh: bool = False,
    full_rebuild: bool = False,
) -> dict[str, Any]:
    """Collect cache/source health without mutating live data unless requested."""
    memory_exists = memory_dir.exists()
    memory_files = sorted(memory_dir.glob("project_*.md")) if memory_exists else []
    memory_paths = {str(path) for path in memory_files} if memory_exists else None

    refreshed_rows = 0
    refresh_mode = "none"
    if full_rebuild:
        refresh_mode = "full"
        conn = open_index(index_path)
        try:
            refreshed_rows = build_full_index(conn, memory_dir)
            index_health = _read_index_health(conn, memory_paths)
        finally:
            conn.close()
    elif refresh:
        refresh_mode = "incremental"
        conn = open_index(index_path)
        try:
            refreshed_rows = refresh_index(conn, memory_dir)
            index_health = _read_index_health(conn, memory_paths)
        finally:
            conn.close()
    else:
        conn = _open_readonly(index_path)
        if conn is None:
            index_health = {
                "schema_present": False,
                "project_row_count": 0,
                "fts_row_count": 0,
                "distinct_file_paths": 0,
                "distinct_slugs": 0,
                "duplicate_file_paths": [],
                "duplicate_slugs": [],
                "stale_cached_paths": None,
                "missing_cached_files": None,
            }
        else:
            try:
                index_health = _read_index_health(conn, memory_paths)
            finally:
                conn.close()

    checks = {
        "cache_matches_memory_files": (
            memory_exists
            and index_health["project_row_count"] == len(memory_files)
            and index_health["stale_cached_paths"] == 0
            and index_health["missing_cached_files"] == 0
        ),
        "fts_matches_projects": index_health["fts_row_count"] == index_health["project_row_count"],
        "no_duplicate_file_paths": not index_health["duplicate_file_paths"],
        "no_duplicate_slugs": not index_health["duplicate_slugs"],
    }
    status = "ok" if all(checks.values()) and memory_exists else "warn"

    return {
        "status": status,
        "memory": {
            "path": str(memory_dir),
            "exists": memory_exists,
            "project_file_count": len(memory_files),
        },
        "index": {
            "path": str(index_path),
            "exists": index_path.exists(),
            "refresh_mode": refresh_mode,
            "refreshed_rows": refreshed_rows,
            **index_health,
        },
        "bridge": _read_bridge_health(bridge_path),
        "checks": checks,
    }


def format_health_text(report: dict[str, Any]) -> str:
    """Format a compact operator-readable health report."""
    lines = [
        f"status: {report['status']}",
        f"memory files: {report['memory']['project_file_count']} ({report['memory']['path']})",
        (
            "index rows: "
            f"{report['index']['project_row_count']} projects, "
            f"{report['index']['fts_row_count']} fts rows, "
            f"{report['index']['distinct_file_paths']} paths, "
            f"{report['index']['distinct_slugs']} slugs"
        ),
        (
            "index drift: "
            f"{report['index']['stale_cached_paths']} stale cached paths, "
            f"{report['index']['missing_cached_files']} missing cached files"
        ),
        (
            "duplicates: "
            f"{len(report['index']['duplicate_file_paths'])} path groups, "
            f"{len(report['index']['duplicate_slugs'])} slug groups"
        ),
        (
            "bridge activity: "
            f"{report['bridge']['activity_row_count']} rows, "
            f"latest {report['bridge']['latest_activity_timestamp']}"
        ),
    ]
    return "\n".join(lines)


def dumps_health_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
