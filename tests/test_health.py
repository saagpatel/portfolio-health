"""Tests for portfolio-health health reporting."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from portfolio_health.__main__ import main
from portfolio_health.health import collect_health

FIXTURE_MEMORY = Path(__file__).parent / "fixtures" / "memory"


def _make_bridge(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY,
            source TEXT,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT,
            branch TEXT,
            tags TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO activity_log "
        "(source, timestamp, project_name, summary, tags) "
        "VALUES (?,?,?,?,?)",
        ("test", "2026-06-19T05:11:26Z", "Alpha Project", "latest", "[]"),
    )
    conn.commit()
    conn.close()


def test_collect_health_full_rebuild_reports_aligned_cache(tmp_path: Path):
    index_path = tmp_path / "index.db"
    bridge_path = tmp_path / "bridge.db"
    _make_bridge(bridge_path)

    report = collect_health(
        index_path=index_path,
        memory_dir=FIXTURE_MEMORY,
        bridge_path=bridge_path,
        full_rebuild=True,
    )

    assert report["status"] == "ok"
    assert report["memory"]["project_file_count"] == 8
    assert report["index"]["project_row_count"] == 8
    assert report["index"]["fts_row_count"] == 8
    assert report["index"]["schema_current"] is True
    assert report["index"]["missing_columns"] == []
    assert report["index"]["stale_cached_paths"] == 0
    assert report["index"]["missing_cached_files"] == 0
    assert report["index"]["duplicate_file_paths"] == []
    assert report["bridge"]["schema_present"] is True
    assert report["bridge"]["activity_row_count"] == 1
    assert report["bridge"]["latest_activity_timestamp"] == "2026-06-19T05:11:26Z"
    assert report["checks"]["bridge_activity_log_present"] is True


def test_collect_health_readonly_warns_for_legacy_cache_without_slug(tmp_path: Path):
    index_path = tmp_path / "legacy.db"
    memory_dir = tmp_path / "memory"
    bridge_path = tmp_path / "bridge.db"
    memory_dir.mkdir()
    legacy_file = memory_dir / "project_legacy.md"
    legacy_file.write_text("---\nname: Legacy\n---\n\nBody\n", encoding="utf-8")
    _make_bridge(bridge_path)

    conn = sqlite3.connect(index_path)
    conn.execute(
        """
        CREATE TABLE projects (
            name TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            description TEXT,
            status TEXT,
            mtime INTEGER NOT NULL,
            frontmatter_json TEXT NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE TABLE projects_fts (body TEXT)")
    conn.execute(
        """
        INSERT INTO projects
        (name, file_path, description, status, mtime, frontmatter_json, body)
        VALUES (?,?,?,?,?,?,?)
        """,
        ("Legacy", str(legacy_file), "", "active", 1, "{}", "Body"),
    )
    conn.execute("INSERT INTO projects_fts (body) VALUES (?)", ("Body",))
    conn.commit()
    conn.close()

    report = collect_health(
        index_path=index_path,
        memory_dir=memory_dir,
        bridge_path=bridge_path,
    )

    assert report["status"] == "warn"
    assert report["index"]["schema_present"] is True
    assert report["index"]["schema_current"] is False
    assert report["index"]["missing_columns"] == ["slug"]
    assert report["index"]["project_row_count"] == 1
    assert report["index"]["distinct_slugs"] == 0
    assert report["index"]["duplicate_slugs"] == []
    assert report["checks"]["index_schema_current"] is False


def test_collect_health_readonly_reports_cache_drift(tmp_path: Path):
    index_path = tmp_path / "index.db"
    bridge_path = tmp_path / "bridge.db"
    empty_memory = tmp_path / "memory"
    empty_memory.mkdir()
    _make_bridge(bridge_path)
    collect_health(
        index_path=index_path,
        memory_dir=FIXTURE_MEMORY,
        bridge_path=bridge_path,
        full_rebuild=True,
    )

    report = collect_health(
        index_path=index_path,
        memory_dir=empty_memory,
        bridge_path=bridge_path,
    )

    assert report["status"] == "warn"
    assert report["memory"]["project_file_count"] == 0
    assert report["index"]["project_row_count"] == 8
    assert report["index"]["stale_cached_paths"] == 8


def test_health_cli_json(tmp_path: Path, capsys):
    index_path = tmp_path / "index.db"
    bridge_path = tmp_path / "bridge.db"
    _make_bridge(bridge_path)

    main(
        [
            "health",
            "--index-path",
            str(index_path),
            "--memory-dir",
            str(FIXTURE_MEMORY),
            "--bridge-path",
            str(bridge_path),
            "--full-rebuild",
            "--json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["checks"]["cache_matches_memory_files"] is True
