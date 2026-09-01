"""Real-git regression: origin scrub after a direct clone."""

from __future__ import annotations

import subprocess
from pathlib import Path

from opencode_manager.git.auth import isolated_git_env
from opencode_manager.git.clone import (
    _scrub_origin,
    clone_repo,
    origin_has_userinfo,
)
from tests.job_end_helpers import file_url, seed_git_repo


def _stored_origin(dest: Path) -> str:
    got = subprocess.run(
        ["git", "config", "--local", "--get", "remote.origin.url"],
        cwd=dest,
        check=True,
        capture_output=True,
        text=True,
    )
    return got.stdout.strip()


def test_clone_repo_leaves_clean_origin(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="develop")
    dest = tmp_path / "clone"
    clone_repo(file_url(src), dest, "develop", timeout=30.0)
    origin = "https://gitlab.example.com/g/r.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", origin],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    env = isolated_git_env()
    _scrub_origin(dest, env, timeout=15.0)
    stored = _stored_origin(dest)
    assert stored == origin
    assert not origin_has_userinfo(stored)


def test_direct_clone_scrubs_stored_userinfo_and_keeps_port(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="develop")
    dest = tmp_path / "clone"
    clone_repo(file_url(src), dest, "develop", timeout=30.0)
    dirty = "https://oauth2:SECRET@gitlab.example.com:8443/g/r.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", dirty],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    env = isolated_git_env()
    _scrub_origin(dest, env, timeout=15.0)
    stored = _stored_origin(dest)
    assert stored == "https://gitlab.example.com:8443/g/r.git"
    assert "SECRET" not in stored
    assert not origin_has_userinfo(stored)
