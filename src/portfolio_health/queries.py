"""SQL query helpers used by server.py tools. Each function takes an open sqlite3.Connection."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_iso(days: int) -> str:
    from datetime import timedelta

    dt = datetime.now(UTC) - timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def _open_bridge(bridge_path: Path) -> sqlite3.Connection | None:
    """Open bridge-db read-only. Returns None if file not found."""
    if not bridge_path.exists():
        return None
    conn = sqlite3.connect(f"file:{bridge_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_tags(raw_tags: str | None) -> list[str]:
    try:
        tags = json.loads(raw_tags or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(tags, list):
        return []
    return [tag for tag in tags if isinstance(tag, str)]


def _has_tag(raw_tags: str | None, tag: str) -> bool:
    return tag in _parse_tags(raw_tags)


def _is_session_boundary(raw_tags: str | None) -> bool:
    return _has_tag(raw_tags, "session-boundary")


def _sanitize_fts_query(query: str) -> str:
    """Strip FTS5 special operators to prevent syntax errors, then add prefix wildcards.

    Each resulting token gets a trailing * so partial words (e.g. "Swift" matches
    "SwiftUI") are found. This is safe because * is only appended after stripping it
    from user input.
    """
    # Remove FTS5 special chars: quotes, parens, colon, caret, asterisk
    stripped = re.sub(r'["\'\(\)\*\:\^]', " ", query)
    # Collapse whitespace
    tokens = stripped.split()
    if not tokens:
        return ""
    # Append prefix wildcard to each token for substring-start matching
    return " ".join(f"{t}*" for t in tokens)


DEFAULT_BRIDGE_PATH = Path.home() / ".local/share/bridge-db/bridge.db"


# ---------------------------------------------------------------------------
# Tool: portfolio_list_active
# ---------------------------------------------------------------------------


def list_active(
    index_conn: sqlite3.Connection,
    window_days: int = 14,
    bridge_path: Path = DEFAULT_BRIDGE_PATH,
) -> list[dict[str, Any]]:
    """Projects with bridge-db activity in the last N days, most recent first."""
    cutoff = _days_ago_iso(window_days)
    bridge = _open_bridge(bridge_path)
    if bridge is None:
        return []

    try:
        rows = bridge.execute(
            """
            SELECT project_name, timestamp, summary, tags
            FROM activity_log
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (cutoff,),
        ).fetchall()
    finally:
        bridge.close()

    activity_by_project: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _is_session_boundary(row["tags"]):
            continue
        project = row["project_name"]
        current = activity_by_project.setdefault(
            project,
            {
                "name": project,
                "last_activity_ts": row["timestamp"],
                "last_activity_summary": row["summary"],
                "activity_count": 0,
            },
        )
        current["activity_count"] += 1

    return [
        item
        for item in sorted(
            activity_by_project.values(),
            key=lambda x: x["last_activity_ts"],
            reverse=True,
        )
    ]


# ---------------------------------------------------------------------------
# Tool: portfolio_get_project
# ---------------------------------------------------------------------------


def get_project(
    index_conn: sqlite3.Connection,
    name: str,
) -> dict[str, Any]:
    """Return full project detail from the index. Fuzzy-matches on name stem."""
    # Try exact match first, then prefix/suffix match
    row = index_conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()

    if row is None:
        # Try matching the file_path stem (project_<name>.md)
        row = index_conn.execute(
            "SELECT * FROM projects WHERE LOWER(file_path) LIKE LOWER(?)",
            (f"%project_{name}.md",),
        ).fetchone()

    if row is None:
        # Partial name match
        row = index_conn.execute(
            "SELECT * FROM projects WHERE LOWER(name) LIKE LOWER(?)",
            (f"%{name}%",),
        ).fetchone()

    if row is None:
        return {"error": "not found"}

    body: str = row["body"] or ""
    # first_section = body up to first ## heading OR first 500 chars.
    # Search for ## at start of any line (including line 0).
    import re as _re

    h2_match = _re.search(r"(?:^|\n)##", body)
    if h2_match and h2_match.start() < 500:
        # Cut before the ## marker (keep the newline belonging to previous content)
        cut = h2_match.start()
        first_section = body[:cut]
    else:
        first_section = body[:500]

    try:
        frontmatter = json.loads(row["frontmatter_json"])
    except (json.JSONDecodeError, TypeError):
        frontmatter = {}

    return {
        "name": row["name"],
        "description": row["description"],
        "file_path": row["file_path"],
        "frontmatter": frontmatter,
        "first_section": first_section.strip(),
    }


# ---------------------------------------------------------------------------
# Tool: portfolio_search
# ---------------------------------------------------------------------------


def search_projects(
    index_conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """FTS5 search across name + description + body."""
    safe_q = _sanitize_fts_query(query)
    if not safe_q:
        return []

    try:
        rows = index_conn.execute(
            """
            SELECT
                p.name,
                p.description,
                snippet(projects_fts, 2, '[', ']', '…', 12) AS snippet,
                projects_fts.rank AS rank
            FROM projects_fts
            JOIN projects p ON p.rowid = projects_fts.rowid
            WHERE projects_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_q, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    return [
        {
            "name": r["name"],
            "description": r["description"],
            "snippet": r["snippet"],
            "rank": r["rank"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Tool: portfolio_stale_candidates
# ---------------------------------------------------------------------------


def stale_candidates(
    index_conn: sqlite3.Connection,
    days: int = 90,
    bridge_path: Path = DEFAULT_BRIDGE_PATH,
) -> list[dict[str, Any]]:
    """Projects with no bridge-db activity in N days AND not abandoned/archived."""
    cutoff = _days_ago_iso(days)
    bridge = _open_bridge(bridge_path)

    # Names that have had activity recently. Store both literal and lowercase
    # variants so a lowercase index slug can match a CamelCase bridge project_name
    # (e.g., bridge "Wavelength" against index slug "wavelength").
    active_names: set[str] = set()
    if bridge:
        try:
            rows = bridge.execute(
                "SELECT project_name, tags FROM activity_log WHERE timestamp >= ?",
                (cutoff,),
            ).fetchall()
            for r in rows:
                if _is_session_boundary(r["tags"]):
                    continue
                pn = r["project_name"]
                active_names.add(pn)
                active_names.add(pn.lower())

            # Get last activity date per project (for days_since calc)
            sql_last = (
                "SELECT project_name, timestamp, tags"
                " FROM activity_log ORDER BY timestamp DESC"
            )
            last_activity_map: dict[str, str] = {}
            for r in bridge.execute(sql_last).fetchall():
                if _is_session_boundary(r["tags"]):
                    continue
                pn = r["project_name"]
                ts = r["timestamp"]
                last_activity_map.setdefault(pn, ts)
                last_activity_map.setdefault(pn.lower(), ts)
        finally:
            bridge.close()
    else:
        last_activity_map = {}

    excluded_statuses = {"abandoned", "archived"}
    now = datetime.now(UTC)

    projects = index_conn.execute(
        "SELECT name, slug, description, status, file_path FROM projects"
    ).fetchall()

    results = []
    for p in projects:
        name = p["name"]
        slug = p["slug"] or ""
        status = (p["status"] or "").lower()
        if status in excluded_statuses:
            continue
        # Check if active using same multi-key strategy as last_activity lookup.
        if name in active_names or slug in active_names or name.lower() in active_names:
            continue

        # Try multiple keys: display name, slug, lowercased name — first hit wins.
        last_ts = (
            last_activity_map.get(name)
            or last_activity_map.get(slug)
            or last_activity_map.get(name.lower())
        )
        if last_ts:
            try:
                # Timestamps may be bare dates ("2026-05-01") or ISO8601 with/without Z
                ts_str = last_ts.replace("Z", "+00:00")
                last_dt = datetime.fromisoformat(ts_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                days_since = (now - last_dt).days
            except ValueError:
                days_since = days + 1
        else:
            days_since = days + 1  # Never seen = definitely stale

        results.append(
            {
                "name": name,
                "description": p["description"],
                "status": p["status"],
                "days_since_last_activity": days_since,
                "file_path": p["file_path"],
            }
        )

    results.sort(key=lambda x: x["days_since_last_activity"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Tool: portfolio_unshipped
# ---------------------------------------------------------------------------

_UNSHIPPED_PATTERNS = [
    re.compile(r"v1\.0 (?:complete|done|ready)", re.IGNORECASE),
    re.compile(r"deploy.?ready", re.IGNORECASE),
    re.compile(r"launch.?ready", re.IGNORECASE),
    re.compile(r"all phases (?:done|complete)", re.IGNORECASE),
]


def unshipped(
    index_conn: sqlite3.Connection,
    bridge_path: Path = DEFAULT_BRIDGE_PATH,
) -> list[dict[str, Any]]:
    """Projects that look ship-ready but have no SHIPPED activity in last 30 days."""
    cutoff_30 = _days_ago_iso(30)
    bridge = _open_bridge(bridge_path)

    recently_shipped: set[str] = set()
    if bridge:
        try:
            rows = bridge.execute(
                "SELECT project_name, tags FROM activity_log WHERE timestamp >= ?",
                (cutoff_30,),
            ).fetchall()
            for r in rows:
                if _has_tag(r["tags"], "SHIPPED"):
                    recently_shipped.add(r["project_name"])
        finally:
            bridge.close()

    now = datetime.now(UTC)

    # Get last SHIPPED timestamp per project for days_since_last_shipped
    shipped_dates: dict[str, str] = {}
    bridge2 = _open_bridge(bridge_path)
    if bridge2:
        try:
            rows = bridge2.execute(
                "SELECT project_name, timestamp, tags FROM activity_log ORDER BY timestamp DESC"
            ).fetchall()
            for r in rows:
                if _has_tag(r["tags"], "SHIPPED"):
                    shipped_dates.setdefault(r["project_name"], r["timestamp"])
        finally:
            bridge2.close()

    projects = index_conn.execute("SELECT name, description, file_path FROM projects").fetchall()

    results = []
    for p in projects:
        name = p["name"]
        desc = p["description"] or ""

        # Check if description matches any ship-ready pattern
        if not any(pat.search(desc) for pat in _UNSHIPPED_PATTERNS):
            continue

        if name in recently_shipped:
            continue

        last_shipped_ts = shipped_dates.get(name)
        if last_shipped_ts:
            try:
                ts_str = last_shipped_ts.replace("Z", "+00:00")
                last_dt = datetime.fromisoformat(ts_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                days_since = (now - last_dt).days
            except ValueError:
                days_since = None
        else:
            days_since = None

        results.append(
            {
                "name": name,
                "description": desc,
                "file_path": p["file_path"],
                "days_since_last_shipped": days_since,
            }
        )

    return results