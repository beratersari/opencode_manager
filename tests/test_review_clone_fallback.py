from __future__ import annotations

from pathlib import Path

import pytest

from opencode_manager.git.auth import is_git_auth_error
from opencode_manager.workspace import gitops as gitops_mod
from opencode_manager.workspace.gitops import GitError, public_git_url, system_git_env


def test_system_git_env_linux_keeps_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitops_mod, "uses_windows_stored_creds", lambda: False)
    env = system_git_env()
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env.get("GIT_SSL_NO_VERIFY") == "1"
    keys = [env[k] for k in env if k.startswith("GIT_CONFIG_KEY_")]
    assert "credential.helper" not in keys


def test_system_git_env_windows_uses_gcm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitops_mod, "uses_windows_stored_creds", lambda: True)
    env = system_git_env()
    assert env.get("GCM_INTERACTIVE") == "auto"
    values = [env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_")]
    assert "manager" in values


def test_clone_pat_success_does_not_use_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.append(list(cmd))
        if "clone" in cmd:
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    dest = tmp_path / "ws"
    gitops_mod.clone_repo("https://gitlab.example/g/r.git", dest, "good-pat", timeout=5)
    joined = " ".join(" ".join(row) for row in captured)
    assert "oauth2:good-pat@" in joined
    assert captured[0].count("credential.helper=") == 2


def test_clone_pat_auth_fail_retries_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        text = " ".join(str(part) for part in cmd)
        if "clone" in cmd:
            if "oauth2:" in text:
                attempts.append("pat")
                return type(
                    "R",
                    (),
                    {"returncode": 128, "stdout": "", "stderr": "HTTP 401 unauthorized"},
                )()
            attempts.append("system")
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    dest = tmp_path / "ws"
    gitops_mod.clone_repo("https://gitlab.example/g/r.git", dest, "bad-pat", timeout=5)
    assert attempts == ["pat", "system"]
    assert dest.exists()


def test_clone_both_auth_fail_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):  # noqa: ANN001
        if "clone" in cmd:
            return type(
                "R",
                (),
                {"returncode": 128, "stdout": "", "stderr": "authentication failed"},
            )()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(gitops_mod, "uses_windows_stored_creds", lambda: False)
    dest = tmp_path / "ws"
    with pytest.raises(GitError, match="PAT and system credentials"):
        gitops_mod.clone_repo("https://gitlab.example/g/r.git", dest, "bad-pat", timeout=5)


def test_clone_no_token_uses_system_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.append(" ".join(str(part) for part in cmd))
        if "clone" in cmd:
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    dest = tmp_path / "ws"
    dirty = "https://oauth2:secret@gitlab.example/g/r.git"
    gitops_mod.clone_repo(dirty, dest, "", timeout=5)
    joined = " ".join(captured)
    assert "secret" not in joined
    assert public_git_url(dirty) in joined
    assert "credential.helper=" not in captured[0]


def test_is_git_auth_error_does_not_match_ticket_folder() -> None:
    assert is_git_auth_error("fatal: destination path 'KAN-401' already exists") is False
    assert is_git_auth_error("HTTP 401 unauthorized") is True
