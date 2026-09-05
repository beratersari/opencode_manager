"""OSM clones only. It never checks out source_branch — the agent does."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

from opencode_manager.git.auth import isolated_git_env
from opencode_manager.git.clone import clone_repo
from opencode_manager.models import JobRecord
from opencode_manager.settings import Settings
from opencode_manager.worker import OpenCodeRunner, Terminal
from tests.job_end_helpers import file_url, seed_git_repo, store_for


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip()


def _head(dest: Path) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)


def _remote_heads(dest: Path) -> List[str]:
    out = _git("branch", "-r", cwd=dest)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def test_clone_argv_is_clone_url_dest_only(tmp_path: Path, monkeypatch) -> None:
    recorded: List[List[str]] = []

    def fake_run(args, **_k):  # noqa: ANN001
        recorded.append(list(args))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr("opencode_manager.git.clone._run_git_maybe_prompt", fake_run)
    monkeypatch.setattr("opencode_manager.git.clone._run_git", fake_run)
    monkeypatch.setattr("opencode_manager.git.clone._scrub_origin", lambda *_a, **_k: None)
    dest = tmp_path / "clone"
    clone_repo("https://gitlab.example/g/r.git", dest, "feature/KAN-9", timeout=10.0)
    clone_calls = [a for a in recorded if a and a[0] == "clone"]
    assert clone_calls == [["clone", "https://gitlab.example/g/r.git", str(dest)]]
    joined = [" ".join(a) for a in recorded]
    assert not any("--branch" in j or "--single-branch" in j for j in joined)
    assert not any(a and a[0] == "checkout" for a in recorded)


def test_clone_does_not_checkout_source_branch(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="develop")
    _git("checkout", "main", cwd=src)
    dest = tmp_path / "clone"
    clone_repo(file_url(src), dest, "develop", timeout=30.0)
    assert dest.is_dir()
    assert (dest / "README.md").is_file()
    assert _head(dest) == "main"
    remotes = _remote_heads(dest)
    assert any(r.endswith("/develop") for r in remotes)
    assert any(r.endswith("/main") for r in remotes)
    assert not (dest / "NOTE.txt").exists()


def test_clone_keeps_all_remote_heads_not_single_branch(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="feature/KAN-9")
    _git("checkout", "main", cwd=src)
    dest = tmp_path / "work"
    clone_repo(file_url(src), dest, "feature/KAN-9", timeout=30.0)
    remotes = _remote_heads(dest)
    assert any("feature/KAN-9" in r for r in remotes)
    assert any(r.endswith("/main") for r in remotes)
    assert _head(dest) != "feature/KAN-9"


def test_agent_can_checkout_source_branch_after_clone(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="develop")
    _git("checkout", "main", cwd=src)
    dest = tmp_path / "clone"
    clone_repo(file_url(src), dest, "develop", timeout=30.0)
    _git("checkout", "develop", cwd=dest)
    assert _head(dest) == "develop"
    assert (dest / "NOTE.txt").read_text(encoding="utf-8") == "on branch\n"


def test_clone_without_source_branch_arg(tmp_path: Path) -> None:
    src = seed_git_repo(tmp_path / "src", branch="main")
    dest = tmp_path / "clone"
    clone_repo(file_url(src), dest, timeout=30.0)
    assert dest.is_dir()
    assert _head(dest)


def test_clone_still_scrubs_origin_userinfo(tmp_path: Path) -> None:
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
    from opencode_manager.git.clone import _scrub_origin, origin_has_userinfo

    _scrub_origin(dest, isolated_git_env(), timeout=15.0)
    stored = _git("config", "--local", "--get", "remote.origin.url", cwd=dest)
    assert stored == "https://gitlab.example.com:8443/g/r.git"
    assert "SECRET" not in stored
    assert not origin_has_userinfo(stored)


def test_worker_clones_without_ls_remote(tmp_settings: Settings, monkeypatch) -> None:
    cloned = {"n": 0}
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo",
        lambda *_a, **_k: cloned.__setitem__("n", cloned["n"] + 1),
    )
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: Terminal(200, "ok"),
    )
    store = store_for(tmp_settings)
    job = JobRecord(
        job_id="job_nobr",
        jira_id="NOBR-1",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="ghost",
        prompt="x",
        model="opencode/x",
        agent_mode="planner",
        retry_count=1,
        timeout_in_seconds=30,
        status="running",
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 200
    assert cloned["n"] == 1


def test_worker_clone_passes_branch_only_for_log(tmp_settings: Settings, monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def capture(url, dest, source_branch="", **_k):  # noqa: ANN001
        seen["url"] = url
        seen["dest"] = dest
        seen["branch"] = source_branch

    monkeypatch.setattr("opencode_manager.worker.clone_repo", capture)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: Terminal(200, "ok"),
    )
    store = store_for(tmp_settings)
    job = JobRecord(
        job_id="job_pass",
        jira_id="PASS-1",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="feature/x",
        prompt="x",
        model="opencode/x",
        agent_mode="orchestrator",
        retry_count=1,
        timeout_in_seconds=30,
        status="running",
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 200
    assert seen["branch"] == "feature/x"
    assert seen["url"] == job.repo_url


def test_clone_env_skips_lfs_smudge(tmp_path: Path, monkeypatch) -> None:
    envs: List[dict] = []

    def fake_run(args, *, env=None, **_k):  # noqa: ANN001
        if env is not None:
            envs.append(env)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr("opencode_manager.git.clone._run_git_maybe_prompt", fake_run)
    monkeypatch.setattr("opencode_manager.git.clone._run_git", fake_run)
    monkeypatch.setattr("opencode_manager.git.clone._scrub_origin", lambda dest, env, **_k: envs.append(env))
    clone_repo("https://gitlab.example/g/r.git", tmp_path / "c", "develop", timeout=5.0)
    assert envs
    assert all(e.get("GIT_LFS_SKIP_SMUDGE") == "1" for e in envs)
    assert all(e.get("GIT_TERMINAL_PROMPT") == "0" for e in envs)
