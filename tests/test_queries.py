"""Tests for queries.py — one test per tool + edge cases."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from portfolio_health.indexer import build_full_index, open_index
from portfolio_health.queries import (
    _load_activity_summary,
    get_project,
    list_active,
    search_projects,
    stale_candidates,
    unshipped,
)

FIXTURE_MEMORY = Path(__file__).parent / "fixtures" / "memory"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def index_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "index.db"
    conn = open_index(db_path)
    build_full_index(conn, FIXTURE_MEMORY)
    return conn


@pytest.fixture()
def bridge_db(tmp_path: Path) -> Path:
    """Create a minimal bridge-db with activity_log records for tests."""
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )

    now = datetime.now(UTC)
    recent = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    old = (now - timedelta(days=120)).strftime("%Y-%m-%d")
    very_old = (now - timedelta(days=200)).strftime("%Y-%m-%d")

    rows = [
        # Active projects (recent activity)
        ("cc", recent, "Alpha Project", "Deployed v1.0 to TestFlight", None, '["SHIPPED"]'),
        ("cc", recent, "Alpha Project", "Fixed crash on launch", None, "[]"),
        ("cc", recent, "Beta Dashboard", "Merged feature/charts branch", None, "[]"),
        # Stale project — old activity, not abandoned
        ("cc", old, "Gamma CLI", "Initial scaffold", None, "[]"),
        # Eta Monitor — very old, never shipped
        ("cc", very_old, "Eta Monitor", "Phase 0 complete", None, "[]"),
        # Delta Engine — never SHIPPED even though description says v1.0 done
        ("cc", old, "Delta Engine", "v1.0 release prep", None, "[]"),
    ]
    sql = (
        "INSERT INTO activity_log"
        "(source, timestamp, project_name, summary, branch, tags)"
        " VALUES (?,?,?,?,?,?)"
    )
    conn.executemany(sql, rows)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# portfolio_list_active
# ---------------------------------------------------------------------------


def test_list_active_returns_recent(index_conn, bridge_db):
    results = list_active(index_conn, window_days=14, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    assert "Alpha Project" in names
    assert "Beta Dashboard" in names


def test_list_active_excludes_old(index_conn, bridge_db):
    results = list_active(index_conn, window_days=14, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    assert "Gamma CLI" not in names


def test_list_active_sorted_most_recent_first(index_conn, bridge_db):
    results = list_active(index_conn, window_days=14, bridge_path=bridge_db)
    timestamps = [r["last_activity_ts"] for r in results]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_active_fields(index_conn, bridge_db):
    results = list_active(index_conn, window_days=14, bridge_path=bridge_db)
    assert len(results) > 0
    r = results[0]
    assert "name" in r
    assert "last_activity_ts" in r
    assert "last_activity_summary" in r
    assert "activity_count" in r
    assert r["activity_count"] >= 1


def test_list_active_no_bridge(index_conn, tmp_path):
    """Missing bridge db returns empty list, not an error."""
    results = list_active(index_conn, bridge_path=tmp_path / "nonexistent.db")
    assert results == []


def test_list_active_alpha_count(index_conn, bridge_db):
    results = list_active(index_conn, window_days=14, bridge_path=bridge_db)
    alpha = next((r for r in results if r["name"] == "Alpha Project"), None)
    assert alpha is not None
    assert alpha["activity_count"] == 2


def test_list_active_ignores_session_boundary_rows(index_conn, bridge_db):
    conn = sqlite3.connect(str(bridge_db))
    recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        ("cc", recent, "Operant", "CC session ended", None, '["session-boundary"]'),
        ("cc", recent, "Alpha Project", "CC session ended", None, '["session-boundary"]'),
    ]
    conn.executemany(
        "INSERT INTO activity_log"
        " (source, timestamp, project_name, summary, branch, tags)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    results = list_active(index_conn, window_days=14, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    alpha = next((r for r in results if r["name"] == "Alpha Project"), None)

    assert "Operant" not in names
    assert alpha is not None
    assert alpha["activity_count"] == 2
    assert alpha["last_activity_summary"] != "CC session ended"


# ---------------------------------------------------------------------------
# portfolio_get_project
# ---------------------------------------------------------------------------


def test_get_project_exact_name(index_conn):
    result = get_project(index_conn, "Alpha Project")
    assert result["name"] == "Alpha Project"
    assert "frontmatter" in result
    assert "first_section" in result
    assert result.get("error") is None


def test_get_project_by_stem(index_conn):
    """Should find project by stem (gamma → Gamma CLI)."""
    result = get_project(index_conn, "gamma")
    assert result.get("error") is None
    assert "Gamma" in result["name"]


def test_get_project_not_found(index_conn):
    result = get_project(index_conn, "totally_nonexistent_xyz_123")
    assert result == {"error": "not found"}


def test_get_project_frontmatter_fields(index_conn):
    result = get_project(index_conn, "Alpha Project")
    fm = result["frontmatter"]
    assert fm["status"] == "active"
    assert fm["type"] == "project"


def test_get_project_first_section_stops_at_h2(index_conn):
    """first_section should not include content from or after the first ## heading."""
    result = get_project(index_conn, "Alpha Project")
    # Body starts with ## Overview at position 0, so first_section is empty (nothing before ##)
    fs = result["first_section"]
    # Must not contain any ## heading markers
    assert "##" not in fs
    # For Gamma CLI which has content before ## Overview, verify truncation
    result2 = get_project(index_conn, "Gamma CLI")
    assert "##" not in result2["first_section"]


def test_get_project_file_path_exists(index_conn):
    result = get_project(index_conn, "Gamma CLI")
    assert Path(result["file_path"]).exists()


# ---------------------------------------------------------------------------
# portfolio_search
# ---------------------------------------------------------------------------


def test_search_returns_results(index_conn):
    # "Swift" should prefix-match "SwiftUI" in the Alpha Project body
    results = search_projects(index_conn, "Swift")
    assert len(results) >= 1
    names = [r["name"] for r in results]
    assert "Alpha Project" in names


def test_search_returns_results_by_description(index_conn):
    # "habit" is in Alpha description — guaranteed match
    results = search_projects(index_conn, "habit")
    assert len(results) >= 1
    names = [r["name"] for r in results]
    assert "Alpha Project" in names


def test_search_fields(index_conn):
    results = search_projects(index_conn, "React")
    assert len(results) >= 1
    r = results[0]
    assert "name" in r
    assert "description" in r
    assert "snippet" in r
    assert "rank" in r


def test_search_snippet_contains_term(index_conn):
    results = search_projects(index_conn, "Rust")
    assert len(results) >= 1
    # snippet should have the match bracket markers or the term
    combined = " ".join(r["snippet"] for r in results)
    assert "Rust" in combined or "[" in combined


def test_search_limit(index_conn):
    results = search_projects(index_conn, "project", limit=2)
    assert len(results) <= 2


def test_search_empty_query(index_conn):
    """Empty / whitespace query should return [] not raise."""
    assert search_projects(index_conn, "") == []
    assert search_projects(index_conn, "   ") == []


def test_search_fts_special_chars_sanitized(index_conn):
    """FTS5 special chars should not cause OperationalError."""
    results = search_projects(index_conn, "AND OR NOT * ( )")
    # May return empty, but must not raise
    assert isinstance(results, list)


def test_search_no_match(index_conn):
    results = search_projects(index_conn, "zzznomatchxxx")
    assert results == []


# ---------------------------------------------------------------------------
# portfolio_stale_candidates
# ---------------------------------------------------------------------------


def test_stale_candidates_finds_stale(index_conn, bridge_db):
    results = stale_candidates(index_conn, days=90, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    # Gamma CLI had activity 120 days ago
    assert "Gamma CLI" in names


def test_stale_candidates_excludes_abandoned(index_conn, bridge_db):
    results = stale_candidates(index_conn, days=90, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    assert "Zeta Archive" not in names


def test_stale_candidates_excludes_recently_active(index_conn, bridge_db):
    results = stale_candidates(index_conn, days=90, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    # Alpha and Beta had recent activity
    assert "Alpha Project" not in names
    assert "Beta Dashboard" not in names


def test_stale_candidates_sorted_longest_first(index_conn, bridge_db):
    results = stale_candidates(index_conn, days=90, bridge_path=bridge_db)
    days_list = [r["days_since_last_activity"] for r in results]
    assert days_list == sorted(days_list, reverse=True)


def test_stale_candidates_fields(index_conn, bridge_db):
    results = stale_candidates(index_conn, days=90, bridge_path=bridge_db)
    assert len(results) > 0
    r = results[0]
    assert "name" in r
    assert "description" in r
    assert "status" in r
    assert "days_since_last_activity" in r
    assert "file_path" in r


def test_stale_candidates_no_bridge(index_conn, tmp_path):
    """Missing bridge treats all projects as stale (no recent activity known)."""
    results = stale_candidates(index_conn, days=90, bridge_path=tmp_path / "no.db")
    # All non-abandoned/archived projects should appear
    names = [r["name"] for r in results]
    assert "Alpha Project" in names
    assert "Zeta Archive" not in names


def test_stale_candidates_slug_match(tmp_path: Path) -> None:
    """Slug-based lookup: display name "Foo Project State" matches bridge key "foo"."""
    from portfolio_health.indexer import open_index

    # Seed the index with a project whose slug is "foo" but display name is "Foo Project State"
    db_path = tmp_path / "index.db"
    conn = open_index(db_path)
    conn.execute(
        "INSERT INTO projects "
        "(name, slug, file_path, description, status, mtime, frontmatter_json, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Foo Project State",
            "foo",
            str(tmp_path / "project_foo.md"),
            "A test project",
            "active",
            1_000_000,
            "{}",
            "body text",
        ),
    )
    conn.commit()

    # Seed bridge-db with activity keyed by slug "foo", dated 30 days ago
    bridge_path = tmp_path / "bridge.db"
    bridge_conn = sqlite3.connect(str(bridge_path))
    bridge_conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    thirty_days_ago = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
    bridge_conn.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags)"
        " VALUES (?, ?, ?, ?, ?)",
        ("cc", thirty_days_ago, "foo", "did some work", "[]"),
    )
    bridge_conn.commit()
    bridge_conn.close()

    results = stale_candidates(conn, days=90, bridge_path=bridge_path)
    names = [r["name"] for r in results]

    # The project has activity 30 days ago (within the 90-day window) so it should NOT appear
    assert "Foo Project State" not in names, (
        "Slug-matched project still appeared in stale candidates — lookup failed"
    )


def test_stale_candidates_slug_match_days_since(tmp_path: Path) -> None:
    """When a slug-matched project is stale, days_since reflects actual activity age, not 91."""
    from portfolio_health.indexer import open_index

    db_path = tmp_path / "index.db"
    conn = open_index(db_path)
    conn.execute(
        "INSERT INTO projects "
        "(name, slug, file_path, description, status, mtime, frontmatter_json, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Bar Project State",
            "bar",
            str(tmp_path / "project_bar.md"),
            "Another test project",
            "active",
            1_000_000,
            "{}",
            "body text",
        ),
    )
    conn.commit()

    # Activity 30 days ago, but stale window is 20 days — project IS stale
    bridge_path = tmp_path / "bridge.db"
    bridge_conn = sqlite3.connect(str(bridge_path))
    bridge_conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    thirty_days_ago = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
    bridge_conn.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags)"
        " VALUES (?, ?, ?, ?, ?)",
        ("cc", thirty_days_ago, "bar", "some work", "[]"),
    )
    bridge_conn.commit()
    bridge_conn.close()

    results = stale_candidates(conn, days=20, bridge_path=bridge_path)
    match = next((r for r in results if r["name"] == "Bar Project State"), None)

    assert match is not None, "Stale slug-matched project should appear in results"
    assert match["days_since_last_activity"] != 21, (
        "days_since should NOT be the fallback 21 (days+1)"
    )
    # Activity was ~30 days ago; allow ±1 for day boundary
    assert 29 <= match["days_since_last_activity"] <= 31, (
        f"Expected ~30 days since activity, got {match['days_since_last_activity']}"
    )


def test_stale_candidates_no_activity_fallback(tmp_path: Path) -> None:
    """Project with truly no bridge activity should still return days+1 (91 for 90-day window)."""
    from portfolio_health.indexer import open_index

    db_path = tmp_path / "index.db"
    conn = open_index(db_path)
    conn.execute(
        "INSERT INTO projects "
        "(name, slug, file_path, description, status, mtime, frontmatter_json, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Baz Project State",
            "baz",
            str(tmp_path / "project_baz.md"),
            "No activity project",
            "active",
            1_000_000,
            "{}",
            "body text",
        ),
    )
    conn.commit()

    # Empty bridge — no activity_log rows at all
    bridge_path = tmp_path / "bridge.db"
    bridge_conn = sqlite3.connect(str(bridge_path))
    bridge_conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    bridge_conn.commit()
    bridge_conn.close()

    results = stale_candidates(conn, days=90, bridge_path=bridge_path)
    match = next((r for r in results if r["name"] == "Baz Project State"), None)

    assert match is not None, "Project with no activity should appear as stale"
    assert match["days_since_last_activity"] == 91, (
        f"Expected fallback 91, got {match['days_since_last_activity']}"
    )


def test_stale_candidates_case_insensitive_match(tmp_path: Path) -> None:
    """Lowercase index slug should match CamelCase bridge project_name."""
    from portfolio_health.indexer import open_index

    db_path = tmp_path / "index.db"
    conn = open_index(db_path)
    conn.execute(
        "INSERT INTO projects "
        "(name, slug, file_path, description, status, mtime, frontmatter_json, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Wavelength Project",
            "wavelength",
            str(tmp_path / "project_wavelength.md"),
            "Test of case-mismatch handling",
            "active",
            1_000_000,
            "{}",
            "body text",
        ),
    )
    conn.commit()

    bridge_path = tmp_path / "bridge.db"
    bridge_conn = sqlite3.connect(str(bridge_path))
    bridge_conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    thirty_days_ago = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
    # Bridge logs under CamelCase "Wavelength" — slug is lowercase "wavelength"
    bridge_conn.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags)"
        " VALUES (?, ?, ?, ?, ?)",
        ("cc", thirty_days_ago, "Wavelength", "did work", "[]"),
    )
    bridge_conn.commit()
    bridge_conn.close()

    results = stale_candidates(conn, days=90, bridge_path=bridge_path)
    names = [r["name"] for r in results]
    assert "Wavelength Project" not in names, (
        "Case-mismatch between slug and bridge project_name should not block active detection"
    )


def test_stale_candidates_ignores_session_boundary_activity(tmp_path: Path) -> None:
    """Session-boundary-only activity should not make a project look active."""
    from portfolio_health.indexer import open_index

    db_path = tmp_path / "index.db"
    conn = open_index(db_path)
    conn.execute(
        "INSERT INTO projects "
        "(name, slug, file_path, description, status, mtime, frontmatter_json, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Boundary Only",
            "boundary-only",
            str(tmp_path / "project_boundary_only.md"),
            "Only has session boundary activity",
            "active",
            1_000_000,
            "{}",
            "body text",
        ),
    )
    conn.commit()

    bridge_path = tmp_path / "bridge.db"
    bridge_conn = sqlite3.connect(str(bridge_path))
    bridge_conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    bridge_conn.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags)"
        " VALUES (?, ?, ?, ?, ?)",
        ("cc", recent, "Boundary Only", "CC session ended", '["session-boundary"]'),
    )
    bridge_conn.commit()
    bridge_conn.close()

    results = stale_candidates(conn, days=90, bridge_path=bridge_path)
    match = next((r for r in results if r["name"] == "Boundary Only"), None)

    assert match is not None
    assert match["days_since_last_activity"] == 91


# ---------------------------------------------------------------------------
# portfolio_unshipped
# ---------------------------------------------------------------------------


def test_unshipped_finds_ready_projects(index_conn, bridge_db):
    results = unshipped(index_conn, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    # Alpha was SHIPPED recently — should NOT be in unshipped list
    assert "Alpha Project" not in names
    # Beta: "all phases complete, deploy-ready" — no SHIPPED tag
    assert "Beta Dashboard" in names


def test_unshipped_excludes_recently_shipped(index_conn, bridge_db):
    results = unshipped(index_conn, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    assert "Alpha Project" not in names


def test_unshipped_includes_v1_done(index_conn, bridge_db):
    results = unshipped(index_conn, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    # Delta Engine: "v1.0 done" — no SHIPPED tag in last 30 days
    assert "Delta Engine" in names


def test_unshipped_fields(index_conn, bridge_db):
    results = unshipped(index_conn, bridge_path=bridge_db)
    assert len(results) > 0
    r = results[0]
    assert "name" in r
    assert "description" in r
    assert "file_path" in r
    assert "days_since_last_shipped" in r


def test_unshipped_excludes_no_pattern(index_conn, bridge_db):
    """Gamma CLI has no ship-ready pattern — should not appear."""
    results = unshipped(index_conn, bridge_path=bridge_db)
    names = [r["name"] for r in results]
    assert "Gamma CLI" not in names


def test_unshipped_does_not_treat_partial_tag_as_shipped(index_conn, bridge_db):
    conn = sqlite3.connect(str(bridge_db))
    recent = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags)"
        " VALUES (?, ?, ?, ?, ?)",
        ("cc", recent, "Beta Dashboard", "Not actually shipped", '["NOT_SHIPPED"]'),
    )
    conn.commit()
    conn.close()

    results = unshipped(index_conn, bridge_path=bridge_db)
    beta = next((r for r in results if r["name"] == "Beta Dashboard"), None)

    assert beta is not None
    assert beta["days_since_last_shipped"] is None


def test_unshipped_no_bridge(index_conn, tmp_path):
    """Missing bridge: unshipped returns ship-ready projects (none excluded as shipped)."""
    results = unshipped(index_conn, bridge_path=tmp_path / "no.db")
    # All ship-ready descriptions should appear
    names = [r["name"] for r in results]
    # Alpha, Beta, Delta, Eta, Theta all have ship-ready descriptions
    assert len(names) >= 1


# ---------------------------------------------------------------------------
# git staleness fallback
# ---------------------------------------------------------------------------


def _make_bridge_no_activity(tmp_path: Path) -> Path:
    """Helper: bridge-db with the schema but no activity rows for the target project."""
    bridge_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(bridge_path))
    conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    conn.commit()
    conn.close()
    return bridge_path


def _seed_project(conn: sqlite3.Connection, tmp_path: Path, name: str, slug: str) -> None:
    conn.execute(
        "INSERT INTO projects "
        "(name, slug, file_path, description, status, mtime, frontmatter_json, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            slug,
            str(tmp_path / f"project_{slug}.md"),
            "A test project",
            "active",
            1_000_000,
            "{}",
            "body text",
        ),
    )
    conn.commit()


def test_stale_candidates_git_rescue(tmp_path: Path) -> None:
    """A recent commit recorded in last_git_commit_ts rescues a bridge-stale project."""
    conn = open_index(tmp_path / "index.db")
    _seed_project(conn, tmp_path, "Afterimage", "afterimage")
    recent = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    conn.execute(
        "UPDATE projects SET last_git_commit_ts = ? WHERE name = ?",
        (recent, "Afterimage"),
    )
    conn.commit()

    bridge_path = _make_bridge_no_activity(tmp_path)
    results = stale_candidates(conn, days=90, bridge_path=bridge_path)

    names = [r["name"] for r in results]
    assert "Afterimage" not in names, (
        "Project with a recent git commit should be rescued from the stale list"
    )


def test_stale_candidates_truly_stale(tmp_path: Path) -> None:
    """No bridge activity AND no recorded git commit (column NULL) -> stale."""
    conn = open_index(tmp_path / "index.db")
    _seed_project(conn, tmp_path, "DeadProject", "deadproject")  # last_git_commit_ts NULL

    bridge_path = _make_bridge_no_activity(tmp_path)
    results = stale_candidates(conn, days=90, bridge_path=bridge_path)

    names = [r["name"] for r in results]
    assert "DeadProject" in names, (
        "Project with no bridge activity and no git commit should be stale"
    )


def test_stale_candidates_old_git_commit_is_stale(tmp_path: Path) -> None:
    """A git commit older than the window does not rescue a bridge-stale project."""
    conn = open_index(tmp_path / "index.db")
    _seed_project(conn, tmp_path, "OldProject", "oldproject")
    old = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    conn.execute(
        "UPDATE projects SET last_git_commit_ts = ? WHERE name = ?",
        (old, "OldProject"),
    )
    conn.commit()

    bridge_path = _make_bridge_no_activity(tmp_path)
    results = stale_candidates(conn, days=90, bridge_path=bridge_path)

    names = [r["name"] for r in results]
    assert "OldProject" in names, "A commit older than the window must not rescue"


# ---------------------------------------------------------------------------
# _load_activity_summary — single-scan rollup (T3-6)
# ---------------------------------------------------------------------------


def _make_bridge_with_trust(tmp_path: Path, rows: list[tuple]) -> Path:
    """Bridge fixture whose activity_log carries the source_trust column.

    rows: (source, timestamp, project_name, summary, tags, source_trust)
    """
    bridge_path = tmp_path / "bridge_trust.db"
    conn = sqlite3.connect(str(bridge_path))
    conn.execute(
        """CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source_trust TEXT NOT NULL DEFAULT 'agent'
                CHECK(source_trust IN ('operator', 'agent', 'ingested'))
        )"""
    )
    conn.executemany(
        "INSERT INTO activity_log"
        " (source, timestamp, project_name, summary, tags, source_trust)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return bridge_path


def test_load_activity_summary_aggregates_per_project(bridge_db):
    summary = _load_activity_summary(bridge_db)
    assert "Alpha Project" in summary
    alpha = summary["Alpha Project"]
    # Two non-boundary rows in the fixture.
    assert len(alpha.activity_timestamps) == 2
    # Contract: tuple is most-recent-first and last_ts is its head.
    assert list(alpha.activity_timestamps) == sorted(alpha.activity_timestamps, reverse=True)
    assert alpha.last_ts == alpha.activity_timestamps[0]
    # last_summary is the summary of a real (non-boundary) Alpha row.
    assert alpha.last_summary in {"Deployed v1.0 to TestFlight", "Fixed crash on launch"}


def test_load_activity_summary_excludes_session_boundary(bridge_db):
    conn = sqlite3.connect(str(bridge_db))
    recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags)"
        " VALUES (?,?,?,?,?)",
        ("cc", recent, "Boundilo", "CC session ended", '["session-boundary"]'),
    )
    conn.commit()
    conn.close()
    summary = _load_activity_summary(bridge_db)
    assert "Boundilo" not in summary, "session-boundary-only project must not appear"


def test_load_activity_summary_tracks_shipped(bridge_db):
    summary = _load_activity_summary(bridge_db)
    # Alpha has exactly one SHIPPED row in the fixture.
    alpha = summary["Alpha Project"]
    assert alpha.last_shipped_ts is not None
    assert len(alpha.shipped_timestamps) == 1
    # Beta has no SHIPPED activity.
    assert summary["Beta Dashboard"].last_shipped_ts is None
    assert summary["Beta Dashboard"].shipped_timestamps == ()


def test_load_activity_summary_missing_bridge(tmp_path):
    assert _load_activity_summary(tmp_path / "nope.db") == {}


def test_load_activity_summary_counts_all_when_no_trust_column(bridge_db):
    """Fixtures without source_trust must not error and must count every row."""
    summary = _load_activity_summary(bridge_db)
    assert "Gamma CLI" in summary  # would raise 'no such column' if we assumed source_trust
    # Every non-boundary row is counted (Alpha has 2) — proves rows aren't silently
    # dropped on the pre-v7 schema, which this test's name promises.
    assert len(summary["Alpha Project"].activity_timestamps) == 2


def test_load_activity_summary_excludes_ingested_when_column_present(tmp_path):
    """When source_trust exists, 'ingested' rows are dropped; operator/agent count."""
    recent = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d")
    rows = [
        ("ingest", recent, "Imported Repo", "auto-imported", "[]", "ingested"),
        ("cc", recent, "Real Work", "operator did work", "[]", "operator"),
        ("cc", recent, "Agent Work", "agent committed", "[]", "agent"),
    ]
    bridge = _make_bridge_with_trust(tmp_path, rows)
    summary = _load_activity_summary(bridge)
    assert "Imported Repo" not in summary, "ingested rows must not count as activity"
    assert "Real Work" in summary
    assert "Agent Work" in summary


def test_load_activity_summary_ingested_shipped_does_not_ship(tmp_path):
    """An ingested SHIPPED row must not register as a real ship event."""
    recent = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d")
    rows = [
        ("ingest", recent, "Faux Ship", "imported release note", '["SHIPPED"]', "ingested"),
    ]
    bridge = _make_bridge_with_trust(tmp_path, rows)
    summary = _load_activity_summary(bridge)
    assert "Faux Ship" not in summary


def test_load_activity_summary_session_boundary_shipped_not_counted(bridge_db):
    """A row tagged BOTH session-boundary and SHIPPED is a boundary marker, not a
    ship event — it must register as neither activity nor a ship. This unifies the
    boundary filtering that unshipped's first pass previously skipped."""
    conn = sqlite3.connect(str(bridge_db))
    recent = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags)"
        " VALUES (?,?,?,?,?)",
        (
            "cc",
            recent,
            "Boundary Ship",
            "session ended after release",
            '["session-boundary", "SHIPPED"]',
        ),
    )
    conn.commit()
    conn.close()
    summary = _load_activity_summary(bridge_db)
    assert "Boundary Ship" not in summary


def test_unshipped_opens_bridge_once(index_conn, bridge_db):
    """Regression: unshipped previously opened the bridge twice (bridge + bridge2)."""
    import portfolio_health.queries as q

    calls = {"n": 0}
    real_open = q._open_bridge

    def counting_open(path):
        calls["n"] += 1
        return real_open(path)

    with patch.object(q, "_open_bridge", side_effect=counting_open):
        unshipped(index_conn, bridge_path=bridge_db)
    assert calls["n"] == 1, f"expected a single bridge open, got {calls['n']}"


def test_stale_candidates_ingested_only_is_stale(tmp_path):
    """A project whose only recent activity is `ingested` must read as STALE."""
    from portfolio_health.indexer import open_index

    conn = open_index(tmp_path / "index.db")
    _seed_project(conn, tmp_path, "GhostRepo", "ghostrepo")  # last_git_commit_ts NULL

    recent = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%d")
    bridge = _make_bridge_with_trust(
        tmp_path,
        [("ingest", recent, "GhostRepo", "auto-imported commit", "[]", "ingested")],
    )
    results = stale_candidates(conn, days=90, bridge_path=bridge)
    names = [r["name"] for r in results]
    assert "GhostRepo" in names, "ingested-only activity must not count as active"
