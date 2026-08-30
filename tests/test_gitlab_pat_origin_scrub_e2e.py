"""Real-git regression: GitLab PAT insteadOf must not fail origin scrub."""

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

_PAT = "glpat-E2E-SUPERSECRET-origin-scrub"


def _stored_origin(dest: Path) -> str:
    got = subprocess.run(
        ["git", "config", "--local", "--get", "remote.origin.url"],
        cwd=dest,
        check=True,
        capture_output=True,
        text=True,
    )
    return got.stdout.strip()


def _get_url(dest: Path, env: dict) -> str:
    got = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=dest,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return got.stdout.strip()


def test_clone_repo_then_gitlab_pat_env_does_not_fail_clean_origin(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="develop")
    dest = tmp_path / "clone"
    clone_repo(file_url(src), dest, "develop", pat=_PAT, timeout=30.0)
    # Default https port — this is the insteadOf prefix git actually matches.
    origin = "https://gitlab.example.com/g/r.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", origin],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    env = isolated_git_env(origin, _PAT)
    rewritten = _get_url(dest, env)
    assert origin_has_userinfo(rewritten)
    assert _PAT in rewritten
    _scrub_origin(dest, env, timeout=15.0)
    stored = _stored_origin(dest)
    assert stored == origin
    assert _PAT not in stored
    assert not origin_has_userinfo(stored)


def test_gitlab_pat_env_scrubs_stored_userinfo_and_keeps_port(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="develop")
    dest = tmp_path / "clone"
    clone_repo(file_url(src), dest, "develop", pat=_PAT, timeout=30.0)
    dirty = f"https://oauth2:{_PAT}@gitlab.example.com:8443/g/r.git"
    subprocess.run(
        ["git", "remote", "set-url", "origin", dirty],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    # PAT env is built from a default-port GitLab URL so insteadOf is active;
    # stored origin still carries :8443 and must keep it after scrub.
    env = isolated_git_env("https://gitlab.example.com/g/r.git", _PAT)
    _scrub_origin(dest, env, timeout=15.0)
    stored = _stored_origin(dest)
    assert stored == "https://gitlab.example.com:8443/g/r.git"
    assert _PAT not in stored
    assert not origin_has_userinfo(stored)
    # Prove insteadOf is live on the matching (no-port) prefix by pointing
    # origin at that prefix after the scrub — get-url must still rewrite.
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://gitlab.example.com/g/r.git"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    assert origin_has_userinfo(_get_url(dest, env))
    assert _PAT in _get_url(dest, env)
