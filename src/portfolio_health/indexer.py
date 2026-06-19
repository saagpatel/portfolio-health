"""Build and incrementally refresh the FTS5 SQLite index from project_*.md memory files."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

# Default paths (overridable for tests)
DEFAULT_MEMORY_DIR = Path.home() / ".claude/projects/-Users-d/memory"
DEFAULT_INDEX_PATH = Path.home() / ".local/share/portfolio-health/index.db"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _derive_slug(path: Path) -> str:
    """Derive a short bridge-style slug from a memory file path.

    Examples:
        project_afterimage.md       → "afterimage"
        project_asc_radar.md        → "asc-radar"
        project_github_repo_auditor → "github-repo-auditor"
    """
    stem = path.stem.removeprefix("project_").lower()
    return _SLUG_NON_ALNUM.sub("-", stem).strip("-")


def parse_memory_file(path: Path) -> dict[str, Any] | None:
    """Parse a project_*.md file into structured data. Returns None on parse error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    frontmatter: dict[str, Any] = {}
    body = text

    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            raw = yaml.safe_load(m.group(1))
            if isinstance(raw, dict):
                frontmatter = raw
        except yaml.YAMLError:
            pass
        body = text[m.end() :]

    # Flatten nested dicts (some files use metadata: {type:, node_type:})
    if "metadata" in frontmatter and isinstance(frontmatter["metadata"], dict):
        for k, v in frontmatter["metadata"].items():
            frontmatter.setdefault(k, v)

    name = str(frontmatter.get("name", path.stem.removeprefix("project_")))
    description = str(frontmatter.get("description", ""))
    status = str(frontmatter.get("status", ""))
    slug = _derive_slug(path)

    return {
        "name": name,
        "slug": slug,
        "file_path": str(path),
        "description": description,
        "status": status,
        "mtime": int(path.stat().st_mtime),
        "frontmatter_json": json.dumps(frontmatter),
        "body": body.strip(),
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            name TEXT PRIMARY KEY,
            slug TEXT,
            file_path TEXT NOT NULL,
            description TEXT,
            status TEXT,
            mtime INTEGER NOT NULL,
            frontmatter_json TEXT NOT NULL,
            body TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS projects_fts
            USING fts5(name, description, body, content='projects', content_rowid='rowid');

        CREATE TRIGGER IF NOT EXISTS projects_ai
            AFTER INSERT ON projects BEGIN
                INSERT INTO projects_fts(rowid, name, description, body)
                VALUES (new.rowid, new.name, new.description, new.body);
            END;

        CREATE TRIGGER IF NOT EXISTS projects_au
            AFTER UPDATE ON projects BEGIN
                INSERT INTO projects_fts(projects_fts, rowid, name, description, body)
                VALUES ('delete', old.rowid, old.name, old.description, old.body);
                INSERT INTO projects_fts(rowid, name, description, body)
                VALUES (new.rowid, new.name, new.description, new.body);
            END;

        CREATE TRIGGER IF NOT EXISTS projects_ad
            AFTER DELETE ON projects BEGIN
                INSERT INTO projects_fts(projects_fts, rowid, name, description, body)
                VALUES ('delete', old.rowid, old.name, old.description, old.body);
            END;
        """
    )
    # Migrate existing DBs that predate the slug column.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    if "slug" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN slug TEXT")
    conn.commit()


def open_index(index_path: Path = DEFAULT_INDEX_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the index database. Schema is guaranteed."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(index_path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _index_max_mtime(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(mtime), 0) FROM projects").fetchone()
    return int(row[0])


def _prune_missing_files(conn: sqlite3.Connection, file_paths: set[str]) -> int:
    """Remove cache rows whose backing memory file is no longer present."""
    if not file_paths:
        return 0

    placeholders = ",".join("?" for _ in file_paths)
    cursor = conn.execute(
        f"DELETE FROM projects WHERE file_path NOT IN ({placeholders})",
        tuple(sorted(file_paths)),
    )
    return cursor.rowcount


def _upsert_project(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    """Insert or update a single project record (triggers keep FTS in sync).

    The memory file path is the stable cache identity. The frontmatter name can
    change over time, so same-path rows with old names are stale cache residue.
    """
    path_rows = conn.execute(
        "SELECT name FROM projects WHERE file_path = ? ORDER BY name",
        (data["file_path"],),
    ).fetchall()
    if path_rows:
        existing_name = data["name"]
        row_names = {row["name"] for row in path_rows}
        if existing_name not in row_names:
            existing_name = path_rows[0]["name"]

        for row_name in row_names:
            if row_name != existing_name:
                conn.execute("DELETE FROM projects WHERE name = ?", (row_name,))

        conn.execute(
            """UPDATE projects
               SET name=?, slug=?, file_path=?, description=?, status=?,
                   mtime=?, frontmatter_json=?, body=?
               WHERE name=?""",
            (
                data["name"],
                data.get("slug"),
                data["file_path"],
                data["description"],
                data["status"],
                data["mtime"],
                data["frontmatter_json"],
                data["body"],
                existing_name,
            ),
        )
        return

    existing = conn.execute("SELECT name FROM projects WHERE name = ?", (data["name"],)).fetchone()
    if existing:
        conn.execute(
            """UPDATE projects
               SET slug=?, file_path=?, description=?, status=?, mtime=?, frontmatter_json=?, body=?
               WHERE name=?""",
            (
                data.get("slug"),
                data["file_path"],
                data["description"],
                data["status"],
                data["mtime"],
                data["frontmatter_json"],
                data["body"],
                data["name"],
            ),
        )
    else:
        conn.execute(
            """INSERT INTO projects
               (name, slug, file_path, description, status, mtime, frontmatter_json, body)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                data["name"],
                data.get("slug"),
                data["file_path"],
                data["description"],
                data["status"],
                data["mtime"],
                data["frontmatter_json"],
                data["body"],
            ),
        )


def refresh_index(
    conn: sqlite3.Connection,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
) -> int:
    """Incrementally refresh index for changed files and prune stale cache rows.

    Returns the number of files indexed/updated plus stale rows pruned.
    """
    if not memory_dir.exists():
        return 0

    current_max = _index_max_mtime(conn)
    updated = 0

    files = sorted(memory_dir.glob("project_*.md"))
    current_paths = {str(path) for path in files}
    updated += _prune_missing_files(conn, current_paths)

    for path in files:
        try:
            file_mtime = int(path.stat().st_mtime)
        except OSError:
            continue

        rows = conn.execute(
            "SELECT name, mtime FROM projects WHERE file_path = ?",
            (str(path),),
        ).fetchall()
        if file_mtime <= current_max:
            # Check if this specific file is already in the index at this mtime.
            # If duplicate rows exist for the same file path, parse once and let
            # _upsert_project collapse the stale display-name rows.
            current_row = next((row for row in rows if int(row["mtime"]) >= file_mtime), None)
            if len(rows) == 1 and current_row:
                continue

        data = parse_memory_file(path)
        if data is None:
            continue

        _upsert_project(conn, data)
        updated += 1

    if updated:
        conn.commit()

    return updated


def build_full_index(
    conn: sqlite3.Connection,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
) -> int:
    """Index ALL project_*.md files regardless of mtime (used in tests / force-rebuild)."""
    if not memory_dir.exists():
        return 0

    indexed = 0
    files = sorted(memory_dir.glob("project_*.md"))
    pruned = _prune_missing_files(conn, {str(path) for path in files})

    for path in files:
        data = parse_memory_file(path)
        if data is None:
            continue
        _upsert_project(conn, data)
        indexed += 1
    if indexed or pruned:
        conn.commit()
    return indexed


def maybe_refresh(
    conn: sqlite3.Connection,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
) -> None:
    """Called at the start of every MCP tool call — cheap stat-based refresh."""
    # If any file is newer than our max mtime, do the incremental pass
    current_max = _index_max_mtime(conn)
    needs_refresh = False
    try:
        for path in memory_dir.glob("project_*.md"):
            try:
                if int(path.stat().st_mtime) > current_max:
                    needs_refresh = True
                    break
            except OSError:
                continue
    except OSError:
        return

    if needs_refresh:
        refresh_index(conn, memory_dir)
