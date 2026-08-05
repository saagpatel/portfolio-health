"""Build and incrementally refresh the FTS5 SQLite index from project_*.md memory files."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


def _json_safe(value: Any) -> str:
    """Coerce a YAML scalar that ``json`` cannot encode into a string.

    PyYAML resolves an unquoted ISO date or timestamp in frontmatter (``modified:
    2026-08-05T03:17:27Z``) into a ``date``/``datetime`` object rather than a string.
    ``json.dumps`` then raises ``TypeError: Object of type datetime is not JSON
    serializable``, and because every tool refreshes the index before querying, one
    such key took down all five MCP tools rather than the one project it appeared in
    (observed 2026-08-04). Dates round-trip as ISO 8601; anything else degrades to
    ``str`` so an unexpected scalar can never again break the whole index.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _resolve_memory_dir() -> Path:
    """Resolve the Claude Code memory directory for the current user at import time.

    Claude Code encodes the home directory path into the projects directory name by
    replacing ``/`` with ``-``.  For example, a home of ``/home/alice`` becomes
    ``-home-alice``, and the standard macOS home ``/Users/alice`` becomes ``-Users-alice``.

    Resolution order:
    1. ``PORTFOLIO_HEALTH_MEMORY_DIR`` environment variable (absolute path).
    2. Current-home encoded directory: derive ``~/.claude/projects/<encoded-home>/``
       from ``Path.home()`` and return it immediately if it contains at least one
       ``project_*.md`` file.  This prevents a stale encoded-home directory (e.g.
       from a renamed user account) from being selected over the correct one.
    3. Fallback glob: scan sibling directories under ``~/.claude/projects/`` for any
       that contain a ``memory/project_*.md`` file (covers the edge case where the
       home directory was recently renamed).
    4. Deterministic derivation: return the canonical ``<encoded-home>/memory`` path
       even if it does not exist yet — callers check ``path.exists()`` themselves.

    A missing directory does not raise at import time — callers that need the dir
    to exist should check ``path.exists()`` themselves.
    """
    # 1. Explicit override via env var
    env_override = os.environ.get("PORTFOLIO_HEALTH_MEMORY_DIR")
    if env_override:
        return Path(env_override).expanduser()

    projects_root = Path.home() / ".claude" / "projects"

    # Derive the encoded-home segment from Path.home() — Claude Code encodes the home
    # path by stripping the leading "/" and replacing remaining "/" with "-".
    # e.g. /Users/alice  →  -Users-alice
    home_str = str(Path.home())
    encoded = home_str.lstrip("/").replace("/", "-")
    encoded_segment = f"-{encoded}" if not encoded.startswith("-") else encoded
    current_home_mem = projects_root / encoded_segment / "memory"

    # 2. Prefer the current-home encoded directory: if it exists and has project_*.md
    #    files, return it immediately without scanning siblings.  This prevents a stale
    #    encoded-home directory (e.g. from a previous macOS user account or a renamed
    #    home) from winning just because it sorts first.
    if current_home_mem.is_dir() and any(current_home_mem.glob("project_*.md")):
        return current_home_mem

    # 3. Fallback glob: scan siblings in case the current-home dir doesn't exist yet
    #    but another encoded-home dir does (uncommon — e.g. home was recently renamed).
    if projects_root.is_dir():
        try:
            for entry in sorted(projects_root.iterdir()):
                if not entry.is_dir() or entry == current_home_mem.parent:
                    continue
                mem = entry / "memory"
                if mem.is_dir() and any(mem.glob("project_*.md")):
                    return mem
        except OSError:
            pass

    # 4. Deterministic derivation: return the canonical path even if it doesn't exist
    #    yet — callers that need the dir to exist check path.exists() themselves.
    return current_home_mem


# Default paths (overridable for tests)
DEFAULT_MEMORY_DIR = _resolve_memory_dir()
DEFAULT_INDEX_PATH = Path.home() / ".local/share/portfolio-health/index.db"
# Seconds a connection waits on a lock held by another session's server process
# before giving up. See open_index for why concurrent writers are expected here.
_BUSY_TIMEOUT_SECONDS = 10.0
_PROJECTS_ROOT = Path.home() / "Projects"

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
        "frontmatter_json": json.dumps(frontmatter, default=_json_safe),
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
            body TEXT NOT NULL,
            last_git_commit_ts TEXT
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
    if "last_git_commit_ts" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN last_git_commit_ts TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# Git recency — precomputed into the index so stale_candidates stays a pure
# column read (no per-query subprocess fan-out).
# ---------------------------------------------------------------------------


def _resolve_project_dir(name: str, projects_root: Path = _PROJECTS_ROOT) -> Path | None:
    """Find the project's git directory under *projects_root* (default ~/Projects)."""
    for candidate in (name, name.title(), name.lower(), name.upper()):
        p = projects_root / candidate
        if p.is_dir():
            return p
    try:
        for entry in projects_root.iterdir():
            if entry.is_dir() and entry.name.lower() == name.lower():
                return entry
    except OSError:
        pass
    return None


def _last_git_commit_iso(project_dir: Path) -> str | None:
    """Last commit's committer date (strict ISO 8601), or None.

    None when the directory is missing, is not a git repo, has no commits, or git
    is unavailable — the column then reads as "no git signal" and staleness falls
    back to bridge activity alone.
    """
    if not project_dir.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "log", "-1", "--format=%cI"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return result.stdout.strip() or None


def _refresh_git_recency(conn: sqlite3.Connection, projects_root: Path) -> None:
    """Update last_git_commit_ts for every indexed project from on-disk git state.

    Runs at index-refresh time, not per query: this is what lets stale_candidates
    read a column instead of shelling out to git N times on every call. Only rows
    whose value actually changes are written, so the FTS-sync trigger stays quiet
    for the common no-op refresh.
    """
    rows = conn.execute("SELECT name, last_git_commit_ts FROM projects").fetchall()
    changed = False
    for row in rows:
        project_dir = _resolve_project_dir(row["name"], projects_root)
        new_ts = _last_git_commit_iso(project_dir) if project_dir is not None else None
        if new_ts != row["last_git_commit_ts"]:
            conn.execute(
                "UPDATE projects SET last_git_commit_ts = ? WHERE name = ?",
                (new_ts, row["name"]),
            )
            changed = True
    if changed:
        conn.commit()


def open_index(index_path: Path = DEFAULT_INDEX_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the index database. Schema is guaranteed.

    Every Claude Code session starts its own MCP server process, and each one both
    reads and writes this file. Under SQLite's default rollback journal a single
    writer locks out every reader, so concurrent sessions surfaced as "database is
    locked" on all five tools (observed 2026-08-04 with two live server processes
    holding the index). WAL lets readers proceed while one writer works, and the
    connect timeout gives the remaining writer-writer overlap room to resolve
    instead of failing the call outright.
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(index_path), timeout=_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    _enable_wal(conn, index_path)
    _ensure_schema(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection, index_path: Path) -> None:
    """Switch the index to WAL, tolerating a process that already holds it.

    Changing journal mode needs an exclusive lock, which a sibling session's server
    process holding the index in rollback mode will not yield. The mode is persisted
    in the database header, so the first start that finds no contender converts the
    file for everyone afterwards; until then the connect timeout still absorbs
    ordinary contention. Refusing to open at all would be strictly worse than running
    on the old journal mode, so report it and continue.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        print(
            f"portfolio-health: could not switch {index_path} to WAL ({exc}); "
            "continuing on the existing journal mode",
            file=sys.stderr,
        )


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Roll back on any error so a failed refresh cannot strand a write lock.

    The first DELETE of a refresh opens a write transaction. Leaving this block with
    that transaction open holds a RESERVED lock on a connection that lives as long as
    the server process, which every other session then sees as "database is locked" —
    forever, since nothing ever closes it. That is how a single unencodable frontmatter
    value took down portfolio-health machine-wide on 2026-08-04 rather than failing the
    one call that hit it. Release the lock, then let the caller see the real error.
    """
    try:
        yield
    except BaseException:
        conn.rollback()
        raise


def _index_max_mtime(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(mtime), 0) FROM projects").fetchone()
    return int(row[0])


def _prune_missing_files(conn: sqlite3.Connection, file_paths: set[str]) -> int:
    """Remove cache rows whose backing memory file is no longer present."""
    if not file_paths:
        cursor = conn.execute("DELETE FROM projects")
        return cursor.rowcount

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
    projects_root: Path | None = None,
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

    with _write_transaction(conn):
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

    if projects_root is not None:
        _refresh_git_recency(conn, projects_root)

    return updated


def build_full_index(
    conn: sqlite3.Connection,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
    projects_root: Path | None = None,
) -> int:
    """Index ALL project_*.md files regardless of mtime (used in tests / force-rebuild)."""
    if not memory_dir.exists():
        return 0

    indexed = 0
    files = sorted(memory_dir.glob("project_*.md"))

    with _write_transaction(conn):
        pruned = _prune_missing_files(conn, {str(path) for path in files})

        for path in files:
            data = parse_memory_file(path)
            if data is None:
                continue
            _upsert_project(conn, data)
            indexed += 1
        if indexed or pruned:
            conn.commit()
    if projects_root is not None:
        _refresh_git_recency(conn, projects_root)
    return indexed


def maybe_refresh(
    conn: sqlite3.Connection,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
    projects_root: Path | None = None,
) -> None:
    """Called at the start of every MCP tool call — cheap stat-based refresh."""
    # If any file is newer or the cached path set drifted, do the incremental pass.
    current_max = _index_max_mtime(conn)
    needs_refresh = False
    try:
        if not memory_dir.exists():
            return
        files = sorted(memory_dir.glob("project_*.md"))
        current_paths = {str(path) for path in files}
        for path in files:
            try:
                if int(path.stat().st_mtime) > current_max:
                    needs_refresh = True
                    break
            except OSError:
                continue
    except OSError:
        return

    if not needs_refresh:
        cached_paths = [row["file_path"] for row in conn.execute("SELECT file_path FROM projects")]
        if set(cached_paths) != current_paths or len(cached_paths) != len(current_paths):
            needs_refresh = True

    if needs_refresh:
        refresh_index(conn, memory_dir, projects_root)
