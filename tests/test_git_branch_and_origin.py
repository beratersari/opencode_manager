"""Regression: exact remote-branch match and origin scrub keeps host:port."""

from __future__ import annotations

import os
import subprocess

import pytest

from opencode_manager.git.clone import (
    GitError,
    _run_git,
    ls_remote_has_branch,
    ls_remote_ref_is_branch,
    origin_has_userinfo,
    public_git_url,
)
from opencode_manager.models import JobRecord


def test_ls_remote_ref_is_exact_heads_name() -> None:
    assert ls_remote_ref_is_branch("deadbeef\trefs/heads/develop", "develop") is True
    assert ls_remote_ref_is_branch("deadbeef\trefs/heads/develop-old", "develop") is False
    assert ls_remote_ref_is_branch("deadbeef\trefs/heads/develop", "dev") is False
    assert ls_remote_ref_is_branch("deadbeef refs/heads/develop", "develop") is True


def test_ls_remote_prefix_output_is_not_a_hit(monkeypatch) -> None:
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="abc123\trefs/heads/develop-old\n",
            stderr="",
        )

    monkeypatch.setattr("opencode_manager.git.clone._run_git", fake_run)
    assert (
        ls_remote_has_branch("https://example/r.git", "develop", timeout=1.0)
        is False
    )


def test_public_git_url_keeps_nondefault_port() -> None:
    assert (
        public_git_url("https://oauth2:TOKEN@gitlab.example.com:8443/g/r.git")
        == "https://gitlab.example.com:8443/g/r.git"
    )
    assert public_git_url("https://gitlab.example.com/g/r.git") == "https://gitlab.example.com/g/r.git"
    assert origin_has_userinfo("https://oauth2:TOKEN@host/g/r.git") is True
    assert origin_has_userinfo("https://host/g/r.git") is False


def test_run_git_tracks_and_untracks_pid(monkeypatch) -> None:
    job = JobRecord(job_id="job_pid", jira_id="P-1")
    saved: list[list[int]] = []

    class Store:
        def save(self, rec: JobRecord) -> None:
            saved.append(list(rec.extra_pids))

    class FakeProc:
        pid = 4242
        returncode = 0

        def communicate(self, timeout=None):  # noqa: ARG002
            assert 4242 in job.extra_pids
            return "ok\n", ""

    monkeypatch.setattr(
        "opencode_manager.git.clone.subprocess.Popen",
        lambda *_a, **_k: FakeProc(),
    )
    _run_git(["status"], env=os.environ.copy(), timeout=1.0, job=job, store=Store())
    assert job.extra_pids == []
    assert saved[0] == [4242]
    assert saved[-1] == []


def test_run_git_timeout_kills_child(monkeypatch) -> None:
    killed: list[int] = []

    class FakeProc:
        pid = 77
        returncode = None

        def communicate(self, timeout=None):
            if timeout == 1.0:
                raise subprocess.TimeoutExpired(cmd="git", timeout=1)
            return "", ""

    monkeypatch.setattr(
        "opencode_manager.git.clone.subprocess.Popen",
        lambda *_a, **_k: FakeProc(),
    )
    monkeypatch.setattr("opencode_manager.git.clone.kill_pid", lambda pid: killed.append(pid))
    with pytest.raises(GitError, match="timed out"):
        _run_git(["clone", "x"], env=os.environ.copy(), timeout=1.0)
    assert killed == [77]
