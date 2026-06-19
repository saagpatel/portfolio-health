"""Tests for indexer.py — builds index from fixture directory."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from portfolio_health.indexer import (
    build_full_index,
    maybe_refresh,
    open_index,
    parse_memory_file,
    refresh_index,
)

FIXTURE_MEMORY = Path(__file__).parent / "fixtures" / "memory"


@pytest.fixture()
def mem_conn(tmp_path: Path) -> sqlite3.Connection:
    """In-memory index pre-loaded with fixture data."""
    db_path = tmp_path / "test_index.db"
    conn = open_index(db_path)
    build_full_index(conn, FIXTURE_MEMORY)
    return conn


# ---------------------------------------------------------------------------
# parse_memory_file
# ---------------------------------------------------------------------------


def test_parse_frontmatter_simple():
    path = FIXTURE_MEMORY / "project_alpha.md"
    data = parse_memory_file(path)
    assert data is not None
    assert data["name"] == "Alpha Project"
    assert "launch-ready" in data["description"]
    assert data["status"] == "active"


def test_parse_frontmatter_quoted_description():
    """project_theta.md has a quoted description with commas."""
    path = FIXTURE_MEMORY / "project_theta.md"
    data = parse_memory_file(path)
    assert data is not None
    assert "GraphQL" in data["description"]


def test_parse_frontmatter_nested_metadata():
    """project_eta.md nests type inside metadata: block."""
    path = FIXTURE_MEMORY / "project_eta.md"
    data = parse_memory_file(path)
    assert data is not None
    assert data["name"] == "Eta Monitor"
    fm = json.loads(data["frontmatter_json"])
    # nested metadata keys should be lifted
    assert fm.get("node_type") == "memory" or fm.get("metadata", {}).get("node_type") == "memory"


def test_parse_body_present():
    path = FIXTURE_MEMORY / "project_gamma.md"
    data = parse_memory_file(path)
    assert data is not None
    assert "log analysis" in data["body"] or "Gamma" in data["body"]


def test_parse_missing_file():
    result = parse_memory_file(Path("/nonexistent/project_fake.md"))
    assert result is None


# ---------------------------------------------------------------------------
# build_full_index
# ---------------------------------------------------------------------------


def test_build_full_index_counts(mem_conn: sqlite3.Connection):
    rows = mem_conn.execute("SELECT COUNT(*) FROM projects").fetchone()
    # We have 8 fixture files
    assert rows[0] == 8


def test_build_full_index_all_present(mem_conn: sqlite3.Connection):
    rows = mem_conn.execute("SELECT name FROM projects").fetchall()
    names = {r[0] for r in rows}
    assert "Alpha Project" in names
    assert "Beta Dashboard" in names
    assert "Gamma CLI" in names
    assert "Zeta Archive" in names


def test_build_full_index_frontmatter_json(mem_conn: sqlite3.Connection):
    row = mem_conn.execute(
        "SELECT frontmatter_json FROM projects WHERE name = 'Alpha Project'"
    ).fetchone()
    fm = json.loads(row[0])
    assert fm["status"] == "active"
    assert fm["type"] == "project"


def test_fts_index_populated(mem_conn: sqlite3.Connection):
    count = mem_conn.execute("SELECT COUNT(*) FROM projects_fts").fetchone()[0]
    assert count == 8


# ---------------------------------------------------------------------------
# refresh_index (incremental)
# ---------------------------------------------------------------------------


def test_refresh_index_no_changes(mem_conn: sqlite3.Connection):
    """Second refresh with no file changes should update 0 rows."""
    updated = refresh_index(mem_conn, FIXTURE_MEMORY)
    assert updated == 0


def test_refresh_index_new_file(mem_conn: sqlite3.Connection, tmp_path: Path):
    """A new file added to the memory dir triggers an incremental update."""
    # Create a temp copy of memory dir with an extra file
    import shutil

    fake_mem = tmp_path / "memory"
    shutil.copytree(FIXTURE_MEMORY, fake_mem)

    # Create a new project file with a future mtime
    new_file = fake_mem / "project_iota.md"
    new_file.write_text(
        "---\nname: Iota New\ndescription: Brand new project\n"
        "type: project\nstatus: active\n---\n\nBody text.\n"
    )
    import os
    import time

    os.utime(new_file, (time.time() + 10, time.time() + 10))

    updated = refresh_index(mem_conn, fake_mem)
    assert updated >= 1

    row = mem_conn.execute("SELECT name FROM projects WHERE name = 'Iota New'").fetchone()
    assert row is not None


def test_refresh_index_renamed_frontmatter_updates_existing_row(tmp_path: Path):
    """Changing a file's frontmatter name should not leave an old-name row behind."""
    import os
    import shutil
    import time

    fake_mem = tmp_path / "memory"
    shutil.copytree(FIXTURE_MEMORY, fake_mem)
    conn = open_index(tmp_path / "index.db")
    build_full_index(conn, fake_mem)

    alpha_path = fake_mem / "project_alpha.md"
    alpha_path.write_text(
        "---\n"
        "name: Alpha Renamed\n"
        "description: iOS app for tracking daily habits - launch-ready\n"
        "type: project\n"
        "status: active\n"
        "---\n\n"
        "## Overview\n\n"
        "Alpha is still the same project file.\n"
    )
    os.utime(alpha_path, (time.time() + 10, time.time() + 10))

    updated = refresh_index(conn, fake_mem)

    assert updated >= 1
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 8
    assert conn.execute("SELECT name FROM projects WHERE name = 'Alpha Project'").fetchone() is None
    renamed = conn.execute("SELECT name FROM projects WHERE name = 'Alpha Renamed'").fetchone()
    assert renamed is not None
    path_count = conn.execute(
        "SELECT COUNT(*) FROM projects WHERE file_path = ?",
        (str(alpha_path),),
    ).fetchone()[0]
    assert path_count == 1


def test_refresh_index_collapses_duplicate_rows_for_same_file_path(tmp_path: Path):
    """Existing duplicate rows for one file path should collapse to the parsed current name."""
    import shutil

    fake_mem = tmp_path / "memory"
    shutil.copytree(FIXTURE_MEMORY, fake_mem)
    conn = open_index(tmp_path / "index.db")
    build_full_index(conn, fake_mem)

    alpha_path = fake_mem / "project_alpha.md"
    alpha = conn.execute("SELECT * FROM projects WHERE name = 'Alpha Project'").fetchone()
    conn.execute(
        """INSERT INTO projects
           (name, slug, file_path, description, status, mtime, frontmatter_json, body)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "Old Alpha Name",
            alpha["slug"],
            alpha["file_path"],
            alpha["description"],
            alpha["status"],
            alpha["mtime"],
            alpha["frontmatter_json"],
            alpha["body"],
        ),
    )
    conn.commit()

    updated = refresh_index(conn, fake_mem)

    assert updated >= 1
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 8
    old_alpha = conn.execute("SELECT name FROM projects WHERE name = 'Old Alpha Name'").fetchone()
    assert old_alpha is None
    path_count = conn.execute(
        "SELECT COUNT(*) FROM projects WHERE file_path = ?",
        (str(alpha_path),),
    ).fetchone()[0]
    assert path_count == 1


def test_refresh_index_prunes_deleted_memory_files(tmp_path: Path):
    """Rows for deleted memory files should be removed from the disposable cache."""
    import shutil

    fake_mem = tmp_path / "memory"
    shutil.copytree(FIXTURE_MEMORY, fake_mem)
    conn = open_index(tmp_path / "index.db")
    build_full_index(conn, fake_mem)

    zeta_path = fake_mem / "project_zeta.md"
    zeta_path.unlink()

    updated = refresh_index(conn, fake_mem)

    assert updated >= 1
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 7
    assert conn.execute("SELECT name FROM projects WHERE name = 'Zeta Archive'").fetchone() is None
    assert (
        conn.execute("SELECT name FROM projects WHERE file_path = ?", (str(zeta_path),)).fetchone()
        is None
    )


# ---------------------------------------------------------------------------
# maybe_refresh
# ---------------------------------------------------------------------------


def test_maybe_refresh_noop(mem_conn: sqlite3.Connection):
    """maybe_refresh with no new files should not raise and not crash."""
    maybe_refresh(mem_conn, FIXTURE_MEMORY)
    count = mem_conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    assert count == 8


def test_maybe_refresh_missing_dir(mem_conn: sqlite3.Connection, tmp_path: Path):
    """maybe_refresh with a nonexistent directory should not raise."""
    maybe_refresh(mem_conn, tmp_path / "does_not_exist")


# ---------------------------------------------------------------------------
# Schema idempotency
# ---------------------------------------------------------------------------


def test_open_index_idempotent(tmp_path: Path):
    """Opening the same db twice should not raise (schema IF NOT EXISTS)."""
    db_path = tmp_path / "idx.db"
    conn1 = open_index(db_path)
    conn1.close()
    conn2 = open_index(db_path)
    count = conn2.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    assert count == 0
    conn2.close()
