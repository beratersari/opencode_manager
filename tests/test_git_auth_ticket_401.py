"""Ticket folder 401 must not look like HTTP 401."""

from pathlib import Path

from opencode_manager.git.auth import is_git_auth_error
from opencode_manager.git.clone import GitError, clone_repo
from tests.job_end_helpers import file_url, seed_git_repo


def test_ticket_id_401_in_clone_stderr_is_not_auth() -> None:
    path_err = (
        "fatal: destination path 'C:\\osm\\.temp\\KAN-401' already exists "
        "and is not an empty directory."
    )
    assert is_git_auth_error(path_err) is False
    assert is_git_auth_error("The requested URL returned error: 401") is True
    assert is_git_auth_error("fatal: Authentication failed for 'https://x/y.git'") is True
    assert is_git_auth_error("could not read Username: terminal prompts disabled") is True


def test_real_git_clone_into_existing_dest_fails(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src")
    dest = tmp_path / "KAN-401"
    dest.mkdir()
    (dest / "stale").write_text("leftover", encoding="utf-8")
    try:
        clone_repo(file_url(src), dest, timeout=30.0)
        raise AssertionError("clone into existing dest must fail")
    except GitError as exc:
        assert is_git_auth_error(str(exc)) is False
