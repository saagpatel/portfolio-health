"""SQL query helpers used by server.py tools. Each function takes an open sqlite3.Connection."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
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


def _resolve_default_bridge_path() -> Path:
    """Resolve the bridge-db path, honoring the ``PORTFOLIO_HEALTH_BRIDGE_DB``
    environment override (parity with ``PORTFOLIO_HEALTH_MEMORY_DIR`` in indexer.py)
    before falling back to the standard ~/.local/share location. This keeps
    portfolio-health working if bridge-db is relocated.
    """
    env_override = os.environ.get("PORTFOLIO_HEALTH_BRIDGE_DB")
    if env_override:
        return Path(env_override).expanduser()
    return Path.home() / ".local/share/bridge-db/bridge.db"


DEFAULT_BRIDGE_PATH = _resolve_default_bridge_path()


# ---------------------------------------------------------------------------
# Shared activity rollup — one scan feeds list_active / stale_candidates / unshipped
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivitySummary:
    """Per-project rollup of bridge-db activity_log, built in a single scan.

    Timestamp tuples are descending (most-recent first) and pre-filtered:
    session-boundary rows are dropped, and when the bridge schema carries the
    `source_trust` provenance column, low-trust ``ingested`` rows are excluded so
    auto-imported noise never reads as real project activity. Consumers apply
    their own time window against these tuples.
    """

    last_ts: str | None
    last_summary: str | None
    last_shipped_ts: str | None
    activity_timestamps: tuple[str, ...]
    shipped_timestamps: tuple[str, ...]


# Provenance labels that do NOT count as real project activity (bridge-db v7+).
_IGNORED_SOURCE_TRUST = frozenset({"ingested"})

_ACTIVITY_SQL = (
    "SELECT project_name, timestamp, summary, tags FROM activity_log ORDER BY timestamp DESC"
)
_ACTIVITY_SQL_TRUST = (
    "SELECT project_name, timestamp, summary, tags, source_trust "
    "FROM activity_log ORDER BY timestamp DESC"
)


def _bridge_has_source_trust(conn: sqlite3.Connection) -> bool:
    """True if activity_log carries the source_trust column (bridge-db v7+).

    Older bridges and the test fixtures predate the column, so every read must
    probe rather than assume — selecting a missing column raises OperationalError.
    """
    cols = conn.execute("PRAGMA table_info(activity_log)").fetchall()
    return any(col["name"] == "source_trust" for col in cols)


def _load_activity_summary(bridge_path: Path) -> dict[str, ActivitySummary]:
    """Single-scan rollup of activity_log keyed by exact bridge project_name.

    Replaces the five ad-hoc full-table scans (across two separate opens) the
    query helpers previously issued. Tag parsing and provenance filtering happen
    once per row here; the connection is opened and closed exactly once.
    """
    bridge = _open_bridge(bridge_path)
    if bridge is None:
        return {}

    try:
        has_trust = _bridge_has_source_trust(bridge)
        rows = bridge.execute(_ACTIVITY_SQL_TRUST if has_trust else _ACTIVITY_SQL).fetchall()
    finally:
        bridge.close()

    acc: dict[str, dict[str, Any]] = {}
    for row in rows:
        if has_trust and row["source_trust"] in _IGNORED_SOURCE_TRUST:
            continue
        tags = _parse_tags(row["tags"])
        if "session-boundary" in tags:
            continue
        entry = acc.setdefault(
            row["project_name"],
            {"acts": [], "ships": [], "last_summary": None},
        )
        entry["acts"].append(row["timestamp"])
        if entry["last_summary"] is None:
            entry["last_summary"] = row["summary"]
        if "SHIPPED" in tags:
            entry["ships"].append(row["timestamp"])

    return {
        name: ActivitySummary(
            last_ts=e["acts"][0] if e["acts"] else None,
            last_summary=e["last_summary"],
            last_shipped_ts=e["ships"][0] if e["ships"] else None,
            activity_timestamps=tuple(e["acts"]),
            shipped_timestamps=tuple(e["ships"]),
        )
        for name, e in acc.items()
    }


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
    summary = _load_activity_summary(bridge_path)

    # Timestamps are compared lexicographically against the bare-date cutoff —
    # valid because both ISO8601 ("...T..Z") and bare-date forms sort correctly as
    # TEXT, matching the old SQL `WHERE timestamp >= ?`. `s.last_ts` is the global
    # non-boundary max; whenever in_window is non-empty it is necessarily >= cutoff,
    # so it equals the in-window max the pre-refactor code reported.
    results: list[dict[str, Any]] = []
    for name, s in summary.items():
        in_window = [ts for ts in s.activity_timestamps if ts >= cutoff]
        if not in_window:
            continue
        results.append(
            {
                "name": name,
                "last_activity_ts": s.last_ts,
                "last_activity_summary": s.last_summary,
                "activity_count": len(in_window),
            }
        )

    results.sort(key=lambda x: x["last_activity_ts"], reverse=True)
    return results


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
    summary = _load_activity_summary(bridge_path)

    # Names that have had activity recently. Store both literal and lowercase
    # variants so a lowercase index slug can match a CamelCase bridge project_name
    # (e.g., bridge "Wavelength" against index slug "wavelength").
    active_names: set[str] = set()
    last_activity_map: dict[str, str] = {}
    for name, s in summary.items():
        if any(ts >= cutoff for ts in s.activity_timestamps):
            active_names.add(name)
            active_names.add(name.lower())
        if s.last_ts is not None:
            last_activity_map.setdefault(name, s.last_ts)
            last_activity_map.setdefault(name.lower(), s.last_ts)

    excluded_statuses = {"abandoned", "archived"}
    now = datetime.now(UTC)

    projects = index_conn.execute(
        "SELECT name, slug, description, status, file_path, last_git_commit_ts FROM projects"
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

        # Git fallback: bridge says stale, but a recent commit means active. The
        # git recency is precomputed into the index (last_git_commit_ts) at refresh
        # time, so this stays a column read — no per-call subprocess. Lexicographic
        # compare is valid: ISO timestamps and the bare-date cutoff both sort as TEXT.
        git_ts = p["last_git_commit_ts"]
        if git_ts and git_ts >= cutoff:
            continue  # git says active — not stale

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
    summary = _load_activity_summary(bridge_path)

    recently_shipped = {
        name for name, s in summary.items() if any(ts >= cutoff_30 for ts in s.shipped_timestamps)
    }
    # Last SHIPPED timestamp per project, for days_since_last_shipped.
    shipped_dates = {
        name: s.last_shipped_ts for name, s in summary.items() if s.last_shipped_ts is not None
    }

    now = datetime.now(UTC)

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
