from opencode_manager.git.auth import (
    argv_helper_off,
    forget_job_creds,
    isolated_git_env,
    is_git_auth_error,
    prompt_windows_credentials,
    uses_windows_stored_creds,
)
from opencode_manager.git.clone import clone_identity
from opencode_manager.git.detect import classify_host


def test_classify_gitlab_default():
    assert classify_host("https://gitlab.example/g/r.git") == "gitlab"


def test_classify_azure():
    assert classify_host("https://dev.azure.com/org/proj/_git/repo") == "tfs"


def test_classify_tfs_path():
    assert classify_host("https://tfs.corp.local/tfs/DefaultCollection/proj/_git/repo") == "tfs"


def test_identity_is_ticket_only():
    assert clone_identity("A-1") == "A-1"
    assert clone_identity("A-1") == clone_identity("A-1")
    assert clone_identity("A-2") == "A-2"
    assert clone_identity("A-1") != clone_identity("A-2")
    assert clone_identity("PROJ-99") == "PROJ-99"


def test_direct_clone_env_has_no_auth_rewrite():
    env = isolated_git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    keys = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_KEY_"))
    values = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_"))
    assert "insteadOf" not in keys
    assert "Authorization" not in values
    assert "extraHeader" not in keys
    if uses_windows_stored_creds():
        assert env.get("GCM_INTERACTIVE") == "auto"
        assert env.get("GCM_MODAL_PROMPT") == "true"
        assert argv_helper_off() == []
    else:
        assert "credential.helper" in keys
        assert argv_helper_off() == ["-c", "credential.helper="]


def test_windows_uses_stored_creds_or_gcm_popup(monkeypatch) -> None:
    import opencode_manager.git.auth as auth

    monkeypatch.setattr(auth.os, "name", "nt")
    assert auth.uses_windows_stored_creds() is True
    env = auth.isolated_git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "auto"
    assert env.get("GCM_MODAL_PROMPT") == "true"
    keys = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_KEY_"))
    values = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_"))
    assert "credential.helper" in keys
    assert "manager" in values
    assert auth.argv_helper_off() == []


def test_is_git_auth_error_not_dns() -> None:
    assert is_git_auth_error("fatal: could not read Username for 'https://x': terminal prompts disabled")
    assert is_git_auth_error("remote: HTTP Basic: Access denied")
    assert is_git_auth_error("Authentication failed for 'https://gitlab.example/g/r.git'")
    assert not is_git_auth_error("fatal: Could not resolve host: hostname.company.com.tr")
    assert not is_git_auth_error("git timed out after 30s")


def test_dialog_creds_go_in_header_not_argv() -> None:
    env = isolated_git_env(username="alice", password="s3cret")
    values = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_"))
    keys = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_KEY_"))
    assert "extraHeader" in keys
    assert "Authorization" in values
    assert "s3cret" not in " ".join(["git", "ls-remote", "https://gitlab.example/g/r.git"])


def test_prompt_windows_credentials_noop_on_linux(monkeypatch) -> None:
    import opencode_manager.git.auth as auth

    monkeypatch.setattr(auth.os, "name", "posix")
    assert prompt_windows_credentials("gitlab.example") is None


def test_ls_remote_prompts_on_windows_auth_error(monkeypatch) -> None:
    from opencode_manager.git.clone import GitError, ls_remote_has_branch
    from opencode_manager.models import JobRecord
    import opencode_manager.git.clone as clone
    import opencode_manager.git.auth as auth

    monkeypatch.setattr(auth.os, "name", "nt")
    calls = {"n": 0}

    def fake_run(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitError("git failed (128): could not read Username: terminal prompts disabled")
        return type("R", (), {"stdout": "abc\trefs/heads/develop\n", "returncode": 0})()

    monkeypatch.setattr(clone, "_run_git", fake_run)
    monkeypatch.setattr(clone, "prompt_windows_credentials", lambda _h: ("alice", "pw"))
    job = JobRecord(job_id="job_prompt1", jira_id="P-1")
    try:
        assert ls_remote_has_branch("https://gitlab.example/g/r.git", "develop", timeout=1.0, job=job) is True
        assert calls["n"] == 2
    finally:
        forget_job_creds(job.job_id)


def test_linux_still_disables_credential_helper(monkeypatch) -> None:
    import opencode_manager.git.auth as auth

    monkeypatch.setattr(auth.os, "name", "posix")
    assert auth.uses_windows_stored_creds() is False
    env = auth.isolated_git_env()
    keys = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_KEY_"))
    assert "credential.helper" in keys
    assert env.get("GCM_INTERACTIVE") is None
    assert auth.argv_helper_off() == ["-c", "credential.helper="]
