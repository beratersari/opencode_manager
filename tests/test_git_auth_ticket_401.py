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


def test_windows_auth_retry_fetches_existing_dest(tmp_path: Path, monkeypatch) -> None:
    """Choice 9: dialog retry must not `git clone` into dest the first clone created."""
    from subprocess import CompletedProcess

    from opencode_manager.git import clone as clone_mod
    from opencode_manager.models import JobRecord

    dest = tmp_path / "work" / "AUD-9"
    dest.mkdir(parents=True)
    (dest / ".git").mkdir()
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        if args and args[0] == "clone":
            raise clone_mod.GitError(
                "fatal: Authentication failed for 'https://example.test/r.git/'"
            )
        return CompletedProcess(["git", *args], 0, stdout="", stderr="")

    monkeypatch.setattr(clone_mod, "_run_git", fake_run)
    monkeypatch.setattr(clone_mod, "uses_windows_stored_creds", lambda: True)
    monkeypatch.setattr(clone_mod, "prompt_windows_credentials", lambda host: ("u", "p"))

    job = JobRecord(job_id="job_git_retry", jira_id="AUD-9")
    clone_mod.clone_repo("https://example.test/r.git", dest, "", timeout=5, job=job)

    clone_calls = [c for c in calls if c and c[0] == "clone"]
    assert len(clone_calls) == 1, calls
    assert any("fetch" in c for c in calls), calls
    assert dest.exists()


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
