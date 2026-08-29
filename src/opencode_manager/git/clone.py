"""PAT clone + remote branch check."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.git.auth import argv_helper_off, isolated_git_env
from opencode_manager.log import get_logger, redact

if TYPE_CHECKING:
    from opencode_manager.dashboard.store import JobStore
    from opencode_manager.models import JobRecord

logger = get_logger()


class GitError(RuntimeError):
    def __init__(self, message: str, *, missing_branch: bool = False):
        super().__init__(message)
        self.missing_branch = missing_branch


def clone_identity(jira_id: str) -> str:
    """One live job per ticket, so the folder is the ticket id only."""
    ticket = re.sub(r"[^A-Za-z0-9._-]+", "_", (jira_id or "").strip())[:40]
    return ticket or "ticket"


def clone_path_for(work_dir: Path, jira_id: str) -> Path:
    return work_dir / clone_identity(jira_id)


def public_git_url(url: str) -> str:
    """Strip userinfo, keep host:port."""
    parsed = urlparse(url)
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    if not host:
        raise GitError("URL has userinfo but no host")
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def origin_has_userinfo(url: str) -> bool:
    parsed = urlparse(url)
    return bool(parsed.username or parsed.password)


def ls_remote_ref_is_branch(line: str, branch: str) -> bool:
    """Exact `refs/heads/{branch}` — `dev` must not match `develop`."""
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return False
    if "\t" in text:
        ref = text.split("\t", 1)[1].strip()
    else:
        parts = text.split()
        if len(parts) < 2:
            return False
        ref = parts[-1]
    return ref == f"refs/heads/{branch}"


def _track_pid(job: Optional["JobRecord"], pid: Optional[int], store: Optional["JobStore"]) -> None:
    if job is None or not pid:
        return
    if int(pid) not in job.extra_pids:
        job.extra_pids.append(int(pid))
        if store is not None:
            store.save(job)


def _untrack_pid(job: Optional["JobRecord"], pid: Optional[int], store: Optional["JobStore"]) -> None:
    if job is None or not pid:
        return
    job.extra_pids = [p for p in job.extra_pids if p != int(pid)]
    if store is not None:
        store.save(job)


def _run_git(
    args: List[str],
    *,
    env: dict,
    cwd: Optional[Path] = None,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> subprocess.CompletedProcess:
    cmd = ["git", *argv_helper_off(), *args]
    logger.info("git run timeout=%ss cwd=%s -- %s", timeout, cwd or ".", redact(" ".join(args)))
    if should_stop and should_stop():
        raise GitError("manager shutting down")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("git timeout after %ss -- %s", timeout, redact(" ".join(args)))
        raise GitError(f"git timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitError(f"git failed to start: {exc}") from exc
    _track_pid(job, proc.pid, store)
    try:
        if should_stop and should_stop():
            kill_pid(proc.pid)
            raise GitError("manager shutting down")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            logger.error("git timeout after %ss -- %s", timeout, redact(" ".join(args)))
            kill_pid(proc.pid)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise GitError(f"git timed out after {timeout}s") from exc
    finally:
        _untrack_pid(job, proc.pid, store)
    result = subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout or "", stderr or "")
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


def ls_remote_has_branch(
    repo_url: str,
    branch: str,
    *,
    pat: str,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    logger.info("ls-remote --heads %s refs/heads/%s", redact(repo_url), branch)
    env = isolated_git_env(repo_url, pat)
    result = _run_git(
        ["ls-remote", "--heads", repo_url, f"refs/heads/{branch}"],
        env=env,
        timeout=timeout,
        job=job,
        store=store,
        should_stop=should_stop,
    )
    found = any(ls_remote_ref_is_branch(line, branch) for line in (result.stdout or "").splitlines())
    logger.info("ls-remote branch %s found=%s", branch, found)
    return found


def clone_repo(
    repo_url: str,
    dest: Path,
    source_branch: str,
    *,
    pat: str,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    env = isolated_git_env(repo_url, pat)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git_kw = dict(job=job, store=store, should_stop=should_stop)
    _run_git(
        ["clone", "--branch", source_branch, "--single-branch", repo_url, str(dest)],
        env=env,
        timeout=timeout,
        **git_kw,
    )
    _run_git(
        ["checkout", source_branch],
        env=env,
        cwd=dest,
        timeout=min(60.0, timeout),
        **git_kw,
    )
    _scrub_origin(dest, env, timeout=min(30.0, timeout), **git_kw)
    gitmodules = dest / ".gitmodules"
    if gitmodules.is_file():
        logger.info(".gitmodules present; submodule update")
        _run_git(
            ["submodule", "update", "--init", "--recursive"],
            env=env,
            cwd=dest,
            timeout=timeout,
            **git_kw,
        )


def _scrub_origin(
    dest: Path,
    env: dict,
    *,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    result = _run_git(
        ["remote", "get-url", "origin"],
        env=env,
        cwd=dest,
        timeout=timeout,
        job=job,
        store=store,
        should_stop=should_stop,
    )
    url = (result.stdout or "").strip()
    if origin_has_userinfo(url):
        clean = public_git_url(url)
        logger.info("scrub origin userinfo -> %s", redact(clean))
        _run_git(
            ["remote", "set-url", "origin", clean],
            env=env,
            cwd=dest,
            timeout=timeout,
            job=job,
            store=store,
            should_stop=should_stop,
        )
        check = _run_git(
            ["remote", "get-url", "origin"],
            env=env,
            cwd=dest,
            timeout=timeout,
            job=job,
            store=store,
            should_stop=should_stop,
        )
        final = (check.stdout or "").strip()
        if origin_has_userinfo(final):
            raise GitError("origin still has userinfo after scrub")
    else:
        logger.info("origin has no userinfo")
