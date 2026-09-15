import base64
import os
from pathlib import Path

from opencode_manager.azure.auth import azure_basic_auth
from opencode_manager.azure.client import _is_git_http, _is_http, _ssh_to_https
from opencode_manager.review_worker import RunResult, discussion_sha_attempts
from opencode_manager.workspace.gitops import clone_is_usable, isolated_git_env, public_git_url


def test_review_git_env_matches_ticket_clone(monkeypatch):
    env = isolated_git_env("ignored-pat", auth_scheme="azure")
    assert env.get("CREASY_AZURE_GIT") is None
    assert env.get("GIT_CONFIG_KEY_0") != "http.extraHeader"
    assert "ignored-pat" not in str(env)
    assert env.get("GIT_SSL_NO_VERIFY") == "1"
    if os.name == "nt":
        assert env.get("GCM_INTERACTIVE") == "auto"
    else:
        assert env.get("GIT_ASKPASS") == ""


def test_azure_basic_auth_is_not_empty_username():
    header = azure_basic_auth("secret-pat")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("ascii")
    assert decoded == "pat:secret-pat"
    assert not decoded.startswith(":")


def test_review_clone_uses_public_url(tmp_path, monkeypatch):
    from opencode_manager.workspace import gitops as gitops_mod

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.append(list(cmd))
        if "clone" in cmd:
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    dest = tmp_path / "ws"
    dirty = "https://oauth2:secret-pat@ado.example/tfs/DefaultCollection/App/_git/app"
    gitops_mod.clone_repo(dirty, dest, timeout=5)
    joined = " ".join(" ".join(row) for row in captured)
    assert "secret-pat" not in joined
    assert "oauth2:" not in joined
    assert public_git_url(dirty) in joined


def test_review_git_run_has_no_extraheader(tmp_path, monkeypatch):
    from opencode_manager.workspace import gitops as gitops_mod

    env = isolated_git_env()
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.append(list(cmd))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    gitops_mod._run_git(["status"], env=env, timeout=1.0)
    assert captured
    joined = " ".join(captured[0])
    assert "extraHeader" not in joined
    assert "Authorization" not in joined


def test_api_url_is_not_a_git_clone_url():
    assert _is_http("https://tfs02.company.com.tr/tfs/ExampleCollection/_apis/git/repositories/x")
    assert not _is_git_http("https://tfs02.company.com.tr/tfs/ExampleCollection/_apis/git/repositories/x")
    assert _is_git_http("https://tfs02.company.com.tr/tfs/ExampleCollection/Example%20Projeleri/_git/ProjectX")


def test_ssh_remote_converts_to_https():
    assert _is_http("https://ado.example/col/App/_git/app")
    assert not _is_http("ssh://ado.example/col/App/_git/app")
    assert _ssh_to_https("git@ado.example:tfs/DefaultCollection/App/_git/app") == (
        "https://ado.example/tfs/DefaultCollection/App/_git/app"
    )
    assert _ssh_to_https("ssh://git@ado.example/tfs/DefaultCollection/App/_git/app") == (
        "https://ado.example/tfs/DefaultCollection/App/_git/app"
    )


def test_partial_dot_git_is_not_usable(tmp_path: Path) -> None:
    dest = tmp_path / "ws"
    dest.mkdir()
    (dest / ".git").mkdir()
    assert clone_is_usable(dest) is False


def test_discussion_sha_attempts_prefer_merge_base() -> None:
    pairs = discussion_sha_attempts(
        RunResult(merge_base="livebase", base_sha="stale", start_sha="stale", sha="head")
    )
    assert pairs[0] == ("livebase", "livebase")
    assert ("stale", "stale") in pairs