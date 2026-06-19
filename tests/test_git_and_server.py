"""Real-subprocess tests for _has_recent_git_commit and server.py global-conn reuse."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from portfolio_health.queries import _has_recent_git_commit

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
# Real-subprocess tests for _has_recent_git_commit
# ---------------------------------------------------------------------------


def test_has_recent_git_commit_real_repo(tmp_path: Path) -> None:
    """A repo with a commit today returns True against a past cutoff date."""
    repo = _make_real_git_repo(tmp_path)

    # cutoff yesterday — the commit we just made is newer, so should return True
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    assert _has_recent_git_commit(repo, yesterday) is True


def test_has_recent_git_commit_future_cutoff(tmp_path: Path) -> None:
    """Cutoff in the future means no commit is recent enough — returns False."""
    repo = _make_real_git_repo(tmp_path)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    assert _has_recent_git_commit(repo, tomorrow) is False


def test_has_recent_git_commit_not_a_git_repo(tmp_path: Path) -> None:
    """A real directory that is NOT a git repo returns False without raising."""
    plain_dir = tmp_path / "plain_dir"
    plain_dir.mkdir()
    # git log on a non-git dir exits nonzero; the function must swallow that
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    result = _has_recent_git_commit(plain_dir, yesterday)
    assert result is False


def test_has_recent_git_commit_nonexistent_dir(tmp_path: Path) -> None:
    """A path that does not exist at all returns False."""
    missing = tmp_path / "does_not_exist"
    result = _has_recent_git_commit(missing, "2026-01-01")
    assert result is False


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
