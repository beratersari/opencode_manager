"""Direct clone of the request URL + remote branch check."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.dashboard.store import persist_job
from opencode_manager.git.auth import (
    argv_helper_off,
    creds_for_job,
    forget_job_creds,
    host_from_repo_url,
    isolated_git_env,
    is_git_auth_error,
    prompt_windows_credentials,
    remember_job_creds,
    uses_windows_stored_creds,
)
from opencode_manager.log import (
    clip,
    fmt_cmd,
    get_logger,
    log_command,
    log_command_result,
    log_fail,
    redact,
)

if TYPE_CHECKING:
    from opencode_manager.dashboard.store import JobStore
    from opencode_manager.models import JobRecord

logger = get_logger()


class GitError(RuntimeError):
    def __init__(self, message: str, *, missing_branch: bool = False):
        super().__init__(message)
        self.missing_branch = missing_branch


_SAFE_TICKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def clone_identity(jira_id: str) -> str:
    """One live job per ticket, so the folder is the ticket id only."""
    ticket = (jira_id or "").strip()
    if not _SAFE_TICKET.match(ticket):
        raise GitError(f"unsafe jira_id {ticket!r} is not a clone folder")
    return ticket


def clone_path_for(work_dir: Path, jira_id: str) -> Path:
    dest = work_dir / clone_identity(jira_id)
    root = work_dir.resolve()
    resolved = dest.resolve()
    if resolved == root or root not in resolved.parents:
        raise GitError(f"clone path {resolved} is not under {root}")
    return dest


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
        if store is not None and not persist_job(store, job):
            logger.warning("could not persist extra_pids after track pid=%s", pid)


def _untrack_pid(job: Optional["JobRecord"], pid: Optional[int], store: Optional["JobStore"]) -> None:
    if job is None or not pid:
        return
    job.extra_pids = [p for p in job.extra_pids if p != int(pid)]
    if store is not None and not persist_job(store, job):
        logger.warning("could not persist extra_pids after untrack pid=%s", pid)


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
    work = cwd or "."
    logger.info("git run timeout=%ss cwd=%s -- %s", timeout, work, redact(" ".join(args)))
    log_command(logger, cmd, cwd=work, timeout=timeout)
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
        log_fail(logger, "git timeout before start", timeout=timeout, argv=fmt_cmd(cmd), cwd=work)
        raise GitError(f"git timed out after {timeout}s") from exc
    except OSError as exc:
        log_fail(logger, "git failed to start", err=exc, argv=fmt_cmd(cmd), cwd=work, timeout=timeout)
        raise GitError(f"git failed to start: {exc}") from exc
    logger.info("git spawned pid=%s argv=%s", proc.pid, fmt_cmd(cmd))
    _track_pid(job, proc.pid, store)
    try:
        if should_stop and should_stop():
            kill_pid(proc.pid)
            raise GitError("manager shutting down")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            log_fail(
                logger,
                "git timeout",
                timeout=timeout,
                pid=proc.pid,
                argv=fmt_cmd(cmd),
                cwd=work,
            )
            kill_pid(proc.pid)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise GitError(f"git timed out after {timeout}s") from exc
    finally:
        _untrack_pid(job, proc.pid, store)
    result = subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout or "", stderr or "")
    log_command_result(
        logger,
        cmd,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        cwd=work,
        timeout=timeout,
        pid=proc.pid,
    )
    if result.returncode != 0:
        err = redact((result.stderr or result.stdout or "").strip())
        logger.error("git exit=%s stderr=%s", result.returncode, clip(err, 800))
        raise GitError(f"git failed ({result.returncode}): {err[-800:]}")
    out = redact((result.stdout or "").strip())
    if out:
        logger.info("git ok stdout=%s", clip(out, 400))
    else:
        logger.info("git ok exit=0")
    return result


def _env_for_job(job: Optional["JobRecord"]) -> dict:
    pair = creds_for_job(job.job_id if job else None)
    if pair:
        return isolated_git_env(username=pair[0], password=pair[1])
    return isolated_git_env()


def _run_git_maybe_prompt(
    args: List[str],
    *,
    repo_url: str,
    env: dict,
    cwd: Optional[Path] = None,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> subprocess.CompletedProcess:
    """Run git; on Windows auth failure, show a username/password dialog and retry once."""
    try:
        return _run_git(
            args, env=env, cwd=cwd, timeout=timeout, job=job, store=store, should_stop=should_stop
        )
    except GitError as exc:
        if not uses_windows_stored_creds() or not is_git_auth_error(str(exc)):
            raise
        job_id = job.job_id if job else ""
        if creds_for_job(job_id):
            raise
        host = host_from_repo_url(repo_url)
        logger.info("git needs credentials; prompting Windows dialog host=%s", host)
        pair = prompt_windows_credentials(host)
        if not pair:
            raise GitError(
                "git authentication required; Windows credential dialog was cancelled or empty"
            ) from exc
        remember_job_creds(job_id, pair[0], pair[1])
        retry_env = isolated_git_env(username=pair[0], password=pair[1])
        return _run_git(
            args,
            env=retry_env,
            cwd=cwd,
            timeout=timeout,
            job=job,
            store=store,
            should_stop=should_stop,
        )


def ls_remote_has_branch(
    repo_url: str,
    branch: str,
    *,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    logger.info("ls-remote --heads %s refs/heads/%s", redact(repo_url), branch)
    logger.info(
        "git params op=ls-remote branch=%s timeout=%ss win_stored_creds=%s",
        branch,
        timeout,
        uses_windows_stored_creds(),
    )
    env = _env_for_job(job)
    try:
        result = _run_git_maybe_prompt(
            ["ls-remote", "--heads", repo_url, f"refs/heads/{branch}"],
            repo_url=repo_url,
            env=env,
            timeout=timeout,
            job=job,
            store=store,
            should_stop=should_stop,
        )
    except Exception:
        forget_job_creds(job.job_id if job else None)
        raise
    found = any(ls_remote_ref_is_branch(line, branch) for line in (result.stdout or "").splitlines())
    logger.info("ls-remote branch %s found=%s", branch, found)
    return found


def clone_repo(
    repo_url: str,
    dest: Path,
    source_branch: str = "",
    *,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Clone only. Do not checkout `source_branch` — the OpenCode agent does that."""
    logger.info(
        "git params op=clone dest=%s no_checkout=1 source_branch=%s timeout=%ss "
        "win_stored_creds=%s repo=%s",
        dest,
        source_branch or "(agent)",
        timeout,
        uses_windows_stored_creds(),
        redact(repo_url),
    )
    dest.mkdir(parents=True, exist_ok=True)
    git_kw = dict(job=job, store=store, should_stop=should_stop)
    try:
        env = _env_for_job(job)
        # Ticket folder is cwd. Do not pass dest on argv (`git clone <url> dest`).
        _run_git_maybe_prompt(
            ["clone", repo_url, "."],
            repo_url=repo_url,
            env=env,
            cwd=dest,
            timeout=timeout,
            **git_kw,
        )
        env = _env_for_job(job)
        _scrub_origin(dest, env, timeout=min(30.0, timeout), **git_kw)
    finally:
        forget_job_creds(job.job_id if job else None)


def _stored_origin_url(
    dest: Path,
    env: dict,
    *,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> str:
    """Raw `remote.origin.url`. Never `git remote get-url` — insteadOf rewrites that."""
    result = _run_git(
        ["config", "--local", "--get", "remote.origin.url"],
        env=env,
        cwd=dest,
        timeout=timeout,
        job=job,
        store=store,
        should_stop=should_stop,
    )
    return (result.stdout or "").strip()


def _scrub_origin(
    dest: Path,
    env: dict,
    *,
    timeout: float,
    job: Optional["JobRecord"] = None,
    store: Optional["JobStore"] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    # Read the stored config value, not `git remote get-url` (rewrites can lie).
    git_kw = dict(env=env, timeout=timeout, job=job, store=store, should_stop=should_stop)
    url = _stored_origin_url(dest, **git_kw)
    if origin_has_userinfo(url):
        clean = public_git_url(url)
        logger.info("scrub origin userinfo -> %s", redact(clean))
        _run_git(
            ["remote", "set-url", "origin", clean],
            cwd=dest,
            **git_kw,
        )
        final = _stored_origin_url(dest, **git_kw)
        if origin_has_userinfo(final):
            raise GitError("origin still has userinfo after scrub")
    else:
        logger.info("origin has no userinfo")
