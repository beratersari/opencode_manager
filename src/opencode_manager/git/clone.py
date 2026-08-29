"""PAT clone + remote branch check."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

from opencode_manager.git.auth import argv_helper_off, isolated_git_env
from opencode_manager.log import get_logger, redact

logger = get_logger()


class GitError(RuntimeError):
    def __init__(self, message: str, *, missing_branch: bool = False):
        super().__init__(message)
        self.missing_branch = missing_branch


def clone_identity(jira_id: str, repo_url: str, source_branch: str) -> str:
    digest = hashlib.sha256(
        f"{jira_id}\n{repo_url.strip()}\n{source_branch.strip()}".encode("utf-8")
    ).hexdigest()[:12]
    ticket = re.sub(r"[^A-Za-z0-9._-]+", "_", jira_id)[:40] or "ticket"
    return f"{ticket}_{digest}"


def clone_path_for(work_dir: Path, jira_id: str, repo_url: str, source_branch: str) -> Path:
    return work_dir / clone_identity(jira_id, repo_url, source_branch)


def _run_git(
    args: List[str],
    *,
    env: dict,
    cwd: Optional[Path] = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    cmd = ["git", *argv_helper_off(), *args]
    logger.info("git run timeout=%ss cwd=%s -- %s", timeout, cwd or ".", redact(" ".join(args)))
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        err = redact((result.stderr or result.stdout or "").strip())
        logger.error("git exit=%s stderr=%s", result.returncode, err[-800:])
        raise GitError(f"git failed ({result.returncode}): {err[-800:]}")
    out = redact((result.stdout or "").strip())
    if out:
        logger.info("git ok stdout=%s", out[-400:])
    else:
        logger.info("git ok exit=0")
    return result


def ls_remote_has_branch(repo_url: str, branch: str, *, pat: str, timeout: float) -> bool:
    logger.info("ls-remote --heads %s %s", redact(repo_url), branch)
    env = isolated_git_env(repo_url, pat)
    result = subprocess.run(
        ["git", *argv_helper_off(), "ls-remote", "--heads", repo_url, branch],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        err = redact((result.stderr or "").strip())
        logger.error("ls-remote exit=%s stderr=%s", result.returncode, err[-800:])
        raise GitError(f"ls-remote failed: {err[-800:]}")
    needle = f"refs/heads/{branch}"
    found = any(needle in line for line in (result.stdout or "").splitlines())
    logger.info("ls-remote branch %s found=%s", branch, found)
    return found


def clone_repo(
    repo_url: str,
    dest: Path,
    source_branch: str,
    *,
    pat: str,
    timeout: float,
) -> None:
    env = isolated_git_env(repo_url, pat)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["clone", "--branch", source_branch, "--single-branch", repo_url, str(dest)],
        env=env,
        timeout=timeout,
    )
    _run_git(
        ["checkout", source_branch],
        env=env,
        cwd=dest,
        timeout=min(60.0, timeout),
    )
    _scrub_origin(dest, env, timeout=min(30.0, timeout))
    gitmodules = dest / ".gitmodules"
    if gitmodules.is_file():
        logger.info(".gitmodules present; submodule update")
        _run_git(
            ["submodule", "update", "--init", "--recursive"],
            env=env,
            cwd=dest,
            timeout=timeout,
        )


def _scrub_origin(dest: Path, env: dict, *, timeout: float) -> None:
    try:
        result = _run_git(
            ["remote", "get-url", "origin"],
            env=env,
            cwd=dest,
            timeout=timeout,
        )
    except GitError:
        return
    url = (result.stdout or "").strip()
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        clean = urlunparse(parsed._replace(netloc=parsed.hostname or ""))
        logger.info("scrub origin userinfo -> %s", redact(clean))
        _run_git(["remote", "set-url", "origin", clean], env=env, cwd=dest, timeout=timeout)
    else:
        logger.info("origin has no userinfo")
