"""Real-subprocess tests for the indexer git-recency helpers and server global-conn reuse."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from portfolio_health.indexer import _last_git_commit_iso, _resolve_project_dir

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in cwd, raise on failure."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_real_git_repo(tmp_path: Path) -> Path:
    """Init a minimal git repo with one commit and return its path."""
    repo = tmp_path / "real_repo"
    repo.mkdir()
    _git(["init"], repo)
    # Throwaway identity — no real user info in test output
    _git(["config", "user.email", "test@example.invalid"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


# ---------------------------------------------------------------------------
# Real-subprocess tests for the indexer git-recency helpers
# ---------------------------------------------------------------------------


def test_last_git_commit_iso_real_repo(tmp_path: Path) -> None:
    """A repo with one commit yields a strict-ISO committer timestamp."""
    repo = _make_real_git_repo(tmp_path)
    ts = _last_git_commit_iso(repo)
    assert ts is not None
    from datetime import datetime as _dt

    # %cI is strict ISO 8601 — fromisoformat parses it without massaging.
    assert _dt.fromisoformat(ts).year >= 2024


def test_last_git_commit_iso_not_a_git_repo(tmp_path: Path) -> None:
    """A real directory that is NOT a git repo returns None without raising."""
    plain_dir = tmp_path / "plain_dir"
    plain_dir.mkdir()
    assert _last_git_commit_iso(plain_dir) is None


def test_last_git_commit_iso_nonexistent_dir(tmp_path: Path) -> None:
    """A path that does not exist at all returns None."""
    assert _last_git_commit_iso(tmp_path / "does_not_exist") is None


def test_last_git_commit_iso_empty_repo(tmp_path: Path) -> None:
    """An initialized repo with no commits returns None (no committer date)."""
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    _git(["init"], repo)
    assert _last_git_commit_iso(repo) is None


def test_resolve_project_dir_case_insensitive(tmp_path: Path) -> None:
    """_resolve_project_dir finds a directory regardless of case, scoped to projects_root."""
    fake_projects = tmp_path / "Projects"
    fake_projects.mkdir()
    (fake_projects / "Afterimage").mkdir()

    for query in ("Afterimage", "afterimage", "AFTERIMAGE"):
        result = _resolve_project_dir(query, fake_projects)
        assert result is not None and result.is_dir()
        assert result.name.lower() == "afterimage"

    assert _resolve_project_dir("nonexistent", fake_projects) is None


# ---------------------------------------------------------------------------
# server.py global-conn reuse tests
# ---------------------------------------------------------------------------


def test_get_conn_returns_same_connection(tmp_path: Path) -> None:
    """_get_conn() returns the same connection object across multiple calls."""
    import portfolio_health.server as srv

    # Build an isolated index so we never touch ~/.local/share/portfolio-health/index.db
    index_path = tmp_path / "index.db"
    empty_memory = tmp_path / "memory"
    empty_memory.mkdir()

    # Patch the default paths used inside _get_conn
    with (
        patch.object(srv, "_conn", None),
        patch("portfolio_health.server.DEFAULT_INDEX_PATH", index_path),
        patch("portfolio_health.server._memory_dir", empty_memory),
    ):
        conn1 = srv._get_conn()
        conn2 = srv._get_conn()
        assert conn1 is conn2, "_get_conn should reuse the module-level connection"


def test_get_conn_module_global_is_set_after_first_call(tmp_path: Path) -> None:
    """After the first _get_conn() call, srv._conn is no longer None."""
    import portfolio_health.server as srv

    index_path = tmp_path / "index2.db"
    empty_memory = tmp_path / "memory2"
    empty_memory.mkdir()

    with (
        patch.object(srv, "_conn", None),
        patch("portfolio_health.server.DEFAULT_INDEX_PATH", index_path),
        patch("portfolio_health.server._memory_dir", empty_memory),
    ):
        assert srv._conn is None
        srv._get_conn()
        assert srv._conn is not None


def test_get_conn_state_does_not_leak_between_tests(tmp_path: Path) -> None:
    """Resetting srv._conn to None between tests resets all state correctly."""
    import portfolio_health.server as srv

    index_path = tmp_path / "index3.db"
    empty_memory = tmp_path / "memory3"
    empty_memory.mkdir()

    with (
        patch.object(srv, "_conn", None),
        patch("portfolio_health.server.DEFAULT_INDEX_PATH", index_path),
        patch("portfolio_health.server._memory_dir", empty_memory),
    ):
        conn_a = srv._get_conn()

    # After the context manager exits, _conn is patched back to whatever patch.object
    # captured (None). A second independent test block gets a fresh connection.
    index_path2 = tmp_path / "index4.db"
    empty_memory2 = tmp_path / "memory4"
    empty_memory2.mkdir()

    with (
        patch.object(srv, "_conn", None),
        patch("portfolio_health.server.DEFAULT_INDEX_PATH", index_path2),
        patch("portfolio_health.server._memory_dir", empty_memory2),
    ):
        conn_b = srv._get_conn()

    # Two separate "sessions" produce separate connection objects
    assert conn_a is not conn_b


def test_maybe_refresh_called_on_each_get_conn(tmp_path: Path) -> None:
    """maybe_refresh is invoked on every _get_conn() call, including reuse."""
    import portfolio_health.server as srv

    index_path = tmp_path / "index5.db"
    empty_memory = tmp_path / "memory5"
    empty_memory.mkdir()

    with (
        patch.object(srv, "_conn", None),
        patch("portfolio_health.server.DEFAULT_INDEX_PATH", index_path),
        patch("portfolio_health.server._memory_dir", empty_memory),
        patch("portfolio_health.server.maybe_refresh") as mock_refresh,
    ):
        srv._get_conn()
        srv._get_conn()
        # Called once per _get_conn() invocation — including on reuse
        assert mock_refresh.call_count == 2


def test_get_conn_populates_git_recency_on_cold_start(tmp_path: Path) -> None:
    """A freshly migrated index (NULL git column) gets populated on the first
    _get_conn(), even with no memory change — closes the cold-start gap."""
    import portfolio_health.server as srv
    from portfolio_health.indexer import build_full_index, open_index

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "project_afterimage.md").write_text(
        "---\nname: Afterimage\ndescription: d\ntype: project\nstatus: active\n---\n\nbody\n"
    )

    projects_root = tmp_path / "Projects"
    repo = projects_root / "Afterimage"
    repo.mkdir(parents=True)
    _git(["init"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "f.txt").write_text("x\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-m", "c"], repo)

    # Pre-build the index with the git column left NULL, as a migrated-but-unscanned
    # index would be.
    index_path = tmp_path / "index.db"
    seed = open_index(index_path)
    build_full_index(seed, memory, projects_root=None)
    assert (
        seed.execute(
            "SELECT last_git_commit_ts FROM projects WHERE name = 'Afterimage'"
        ).fetchone()[0]
        is None
    )
    seed.close()

    with (
        patch.object(srv, "_conn", None),
        patch("portfolio_health.server.DEFAULT_INDEX_PATH", index_path),
        patch("portfolio_health.server._PROJECTS_ROOT", projects_root),
        patch("portfolio_health.server._memory_dir", memory),
    ):
        conn = srv._get_conn()
        ts = conn.execute(
            "SELECT last_git_commit_ts FROM projects WHERE name = 'Afterimage'"
        ).fetchone()[0]

    assert ts is not None, "cold-start _get_conn must populate last_git_commit_ts"
