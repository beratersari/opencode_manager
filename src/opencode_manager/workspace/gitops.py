from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse

from opencode_manager.git.auth import (
    argv_helper_off,
    creds_for_job,
    forget_job_creds,
    host_from_repo_url,
    isolated_git_env as ticket_git_env,
    is_git_auth_error,
    prompt_windows_credentials,
    remember_job_creds,
    uses_windows_stored_creds,
)
from opencode_manager.review_log import get_logger, log_command, log_command_result, log_fail, log_ok, redact_userinfo

logger = get_logger("gitops")


class GitError(RuntimeError):
    pass


@dataclass
class DiffIndex:
    merge_base: str
    stat: str
    paths: list[str]
    statuses: dict[str, str]


def isolated_git_env(token: str = "", *, auth_scheme: str = "gitlab") -> dict[str, str]:
    """Same Windows GCM / Linux helper-off env as n8n ticket clones.

    ``token`` / ``auth_scheme`` are ignored leftovers. Review git never
    injects a PAT into the URL or extraHeader.
    """
    del token, auth_scheme
    env = ticket_git_env()
    # INTENTIONAL: on-prem / TLS intercept. Same policy as httpx verify=False.
    env["GIT_SSL_NO_VERIFY"] = "1"
    return env


def public_git_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def _kill_git(proc: subprocess.Popen[str]) -> None:
    from opencode_manager.cleanup.kill import kill_job_tree

    try:
        kill_job_tree([proc.pid])
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _run_git(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: float = 120,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
    repo_url: str = "",
    job_id: str = "",
) -> subprocess.CompletedProcess[str]:
    env = env or isolated_git_env()
    cmd = ["git", *argv_helper_off(), "-c", "http.sslVerify=false", "-c", "core.longpaths=true"]
    cmd.extend(args)
    log_command(logger, args, cwd=cwd or ".", timeout=timeout)
    if should_stop is None and on_pid is None:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=env or isolated_git_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            log_fail(logger, "git timeout", argv=" ".join(str(a) for a in args), cwd=cwd or ".", timeout=timeout)
            raise GitError(f"git timed out after {timeout}s") from exc
        except OSError as exc:
            log_fail(logger, "git start", argv=" ".join(str(a) for a in args), cwd=cwd or ".", err=exc)
            raise GitError(f"git failed to start: {redact_userinfo(str(exc))}") from exc
    else:
        try:
            result = _run_git_killable(
                cmd,
                cwd=cwd,
                env=env or isolated_git_env(),
                timeout=timeout,
                should_stop=should_stop,
                on_pid=on_pid,
            )
        except GitError as exc:
            if str(exc) == "cancelled":
                log_ok(logger, "git cancelled", argv=" ".join(str(a) for a in args), cwd=cwd or ".")
            else:
                log_fail(logger, "git", argv=" ".join(str(a) for a in args), cwd=cwd or ".", err=exc)
            raise
    log_command_result(
        logger,
        args,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        cwd=cwd or ".",
        timeout=timeout,
    )
    if result.returncode != 0:
        err = redact_userinfo((result.stderr or result.stdout or "").strip())
        if (
            uses_windows_stored_creds()
            and is_git_auth_error(err)
            and repo_url
            and not creds_for_job(job_id)
        ):
            host = host_from_repo_url(repo_url)
            log_ok(logger, "git needs credentials; prompting Windows dialog", host=host)
            pair = prompt_windows_credentials(host)
            if pair:
                remember_job_creds(job_id, pair[0], pair[1])
                retry_env = ticket_git_env(username=pair[0], password=pair[1])
                retry_env["GIT_SSL_NO_VERIFY"] = "1"
                return _run_git(
                    args,
                    cwd=cwd,
                    env=retry_env,
                    timeout=timeout,
                    should_stop=should_stop,
                    on_pid=on_pid,
                    repo_url=repo_url,
                    job_id=job_id,
                )
            forget_job_creds(job_id)
            raise GitError(
                "git authentication required; Windows credential dialog was cancelled or empty"
            )
        raise GitError(f"git failed ({result.returncode}): {err[-800:]}")
    return result


def _run_git_killable(
    cmd: list[str],
    *,
    cwd: Optional[Path],
    env: dict[str, str],
    timeout: float,
    should_stop: Optional[Callable[[], bool]],
    on_pid: Optional[Callable[[int], None]],
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise GitError(f"git failed to start: {redact_userinfo(str(exc))}") from exc
    if on_pid and proc.pid:
        try:
            on_pid(proc.pid)
        except Exception:  # noqa: BLE001
            logger.warning("on_pid start failed pid=%s", proc.pid)
    deadline = time.time() + max(0.1, float(timeout))
    stdout = ""
    stderr = ""
    try:
        while True:
            if should_stop is not None and should_stop():
                _kill_git(proc)
                raise GitError("cancelled")
            remaining = deadline - time.time()
            if remaining <= 0:
                _kill_git(proc)
                raise GitError(f"git timed out after {timeout}s")
            try:
                stdout, stderr = proc.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if on_pid:
            try:
                on_pid(0)
            except Exception:  # noqa: BLE001
                logger.warning("on_pid clear failed")
    return subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout or "", stderr or "")


def clone_repo(
    repo_url: str,
    dest: Path,
    token: str = "",
    *,
    timeout: float,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
    auth_scheme: str = "gitlab",
    job_id: str = "",
) -> None:
    del token, auth_scheme
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise GitError(f"clone dest already exists: {dest}")
    env = isolated_git_env()
    auth_url = public_git_url(repo_url)
    git_kw = {
        "should_stop": should_stop,
        "on_pid": on_pid,
        "repo_url": auth_url,
        "job_id": job_id,
    }
    try:
        _run_git(
            ["clone", "--no-single-branch", auth_url, str(dest)],
            env=env,
            timeout=timeout,
            **git_kw,
        )
        _scrub_origin(dest, env, timeout=min(30.0, timeout), **git_kw)
    except Exception as exc:
        log_fail(logger, "git clone", dest=dest, url=redact_userinfo(repo_url), err=exc)
        if dest.exists():
            delete_clone(dest)
        raise
    log_ok(logger, "git clone", dest=dest, url=redact_userinfo(repo_url))


def _scrub_origin(
    dest: Path,
    env: dict[str, str],
    timeout: float,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
    **_ignored: object,
) -> None:
    result = _run_git(
        ["config", "--local", "--get", "remote.origin.url"],
        cwd=dest,
        env=env,
        timeout=timeout,
        should_stop=should_stop,
        on_pid=on_pid,
    )
    url = (result.stdout or "").strip()
    if urlparse(url).username or urlparse(url).password:
        clean = public_git_url(url)
        _run_git(
            ["remote", "set-url", "origin", clean],
            cwd=dest,
            env=env,
            timeout=timeout,
            should_stop=should_stop,
            on_pid=on_pid,
        )


def clone_is_usable(dest: Path) -> bool:
    """True when dest is a git work tree with an origin remote.

    A killed first clone leaves ``dest/.git`` behind. Later jobs must
    not treat that as a workspace they can fetch into.
    """
    root = Path(dest)
    try:
        if not root.exists() or not (root / ".git").exists():
            return False
    except OSError:
        return False
    env = isolated_git_env()
    try:
        inside = _run_git(
            ["rev-parse", "--is-inside-work-tree"],
            cwd=root,
            env=env,
            timeout=15,
        )
        if (inside.stdout or "").strip().lower() != "true":
            return False
        origin = _run_git(
            ["config", "--local", "--get", "remote.origin.url"],
            cwd=root,
            env=env,
            timeout=15,
        )
    except GitError:
        return False
    return bool((origin.stdout or "").strip())


def fetch_and_checkout(
    dest: Path,
    *,
    source_branch: str,
    target_branch: str,
    sha: str,
    token: str = "",
    timeout: float,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
    auth_scheme: str = "gitlab",
    job_id: str = "",
    repo_url: str = "",
) -> str:
    del token, auth_scheme
    env = isolated_git_env()
    origin = _origin_url(dest, env, timeout=min(30.0, timeout), should_stop=should_stop, on_pid=on_pid)
    auth = public_git_url(origin) if origin else public_git_url(repo_url) if repo_url else origin
    git_kw = {
        "should_stop": should_stop,
        "on_pid": on_pid,
        "repo_url": auth or repo_url,
        "job_id": job_id,
    }
    if auth and auth != origin:
        _run_git(
            ["remote", "set-url", "origin", auth],
            cwd=dest,
            env=env,
            timeout=min(30.0, timeout),
            **git_kw,
        )
    try:
        refs = [b for b in (source_branch, target_branch) if b]
        fetch_args = ["fetch", "--force", "origin", *refs]
        _run_git(fetch_args, cwd=dest, env=env, timeout=timeout, **git_kw)
        target = sha or f"origin/{source_branch}"
        _run_git(
            ["checkout", "--force", "--detach", target],
            cwd=dest,
            env=env,
            timeout=min(60.0, timeout),
            **git_kw,
        )
        _run_git(["reset", "--hard", "HEAD"], cwd=dest, env=env, timeout=min(60.0, timeout), **git_kw)
        _run_git(["clean", "-fd"], cwd=dest, env=env, timeout=min(60.0, timeout), **git_kw)
        head = _run_git(["rev-parse", "HEAD"], cwd=dest, env=env, timeout=30, **git_kw)
        sha_out = (head.stdout or "").strip()
        log_ok(
            logger,
            "git checkout",
            dest=dest,
            sha=sha_out or "-",
            source=source_branch or "-",
            target=target_branch or "-",
        )
        return sha_out
    except Exception as exc:
        log_fail(
            logger,
            "git checkout",
            dest=dest,
            source=source_branch or "-",
            target=target_branch or "-",
            err=exc,
        )
        raise
    finally:
        try:
            _scrub_origin(dest, env, timeout=min(30.0, timeout), should_stop=should_stop, on_pid=on_pid)
        except Exception as exc:
            log_fail(logger, "git scrub origin", dest=dest, err=exc)


def _origin_url(
    dest: Path,
    env: dict[str, str],
    timeout: float,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
    **_ignored: object,
) -> str:
    result = _run_git(
        ["config", "--local", "--get", "remote.origin.url"],
        cwd=dest,
        env=env,
        timeout=timeout,
        should_stop=should_stop,
        on_pid=on_pid,
    )
    return (result.stdout or "").strip()


def resolve_merge_base(
    dest: Path,
    *,
    target_branch: str,
    preferred_base: str = "",
    timeout: float = 60,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
) -> str:
    """Separation point = current merge-base of target and HEAD.

    After a rebase onto a moved target, that is the new target tip — not the
    original fork commit. GitLab ``base_sha`` is only a fallback: it can be
    stale right after a force-push, and if we used the old SHA the three-dot
    diff would include target-branch commits that are not part of the MR.
    """
    env = isolated_git_env()
    git_kw = {"should_stop": should_stop, "on_pid": on_pid}
    target_ref = f"origin/{target_branch}" if target_branch else "origin/HEAD"
    try:
        computed = (
            _run_git(
                ["merge-base", target_ref, "HEAD"],
                cwd=dest,
                env=env,
                timeout=timeout,
                **git_kw,
            ).stdout
            or ""
        ).strip()
    except GitError as exc:
        if str(exc) == "cancelled":
            raise
        computed = ""
    if computed:
        if preferred_base and preferred_base != computed:
            logger.info(
                "ignoring GitLab base_sha=%s; using merge-base(%s, HEAD)=%s",
                preferred_base,
                target_ref,
                computed,
            )
        log_ok(logger, "git merge-base", dest=dest, target=target_ref, sha=computed)
        return computed
    if preferred_base:
        try:
            _run_git(
                ["cat-file", "-e", f"{preferred_base}^{{commit}}"],
                cwd=dest,
                env=env,
                timeout=timeout,
                **git_kw,
            )
            log_fail(logger, "git merge-base", dest=dest, target=target_ref, fallback=preferred_base)
            logger.warning("merge-base failed; falling back to GitLab base_sha %s", preferred_base)
            return preferred_base
        except GitError:
            pass
    log_fail(logger, "git merge-base", dest=dest, target=target_ref)
    raise GitError(f"could not resolve merge-base against {target_ref}")


def diff_stat(
    dest: Path,
    merge_base: str,
    *,
    timeout: float = 60,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
) -> DiffIndex:
    env = isolated_git_env()
    git_kw = {"should_stop": should_stop, "on_pid": on_pid}
    stat = _run_git(
        ["diff", "--stat", f"{merge_base}...HEAD"],
        cwd=dest,
        env=env,
        timeout=timeout,
        **git_kw,
    )
    name_status = _run_git(
        ["diff", "--name-status", f"{merge_base}...HEAD"],
        cwd=dest,
        env=env,
        timeout=timeout,
        **git_kw,
    )
    paths: list[str] = []
    statuses: dict[str, str] = {}
    for line in (name_status.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = parts[-1].strip()
        if not path:
            continue
        paths.append(path)
        statuses[path] = status
    index = DiffIndex(merge_base=merge_base, stat=(stat.stdout or "").strip(), paths=paths, statuses=statuses)
    log_ok(logger, "git diff-stat", dest=dest, merge_base=merge_base, paths=len(index.paths))
    return index


def unified_diff(
    dest: Path,
    merge_base: str,
    *,
    timeout: float = 60,
    should_stop: Optional[Callable[[], bool]] = None,
    on_pid: Optional[Callable[[int], None]] = None,
) -> str:
    env = isolated_git_env()
    result = _run_git(
        ["diff", "--find-renames", f"{merge_base}...HEAD"],
        cwd=dest,
        env=env,
        timeout=timeout,
        should_stop=should_stop,
        on_pid=on_pid,
    )
    return result.stdout or ""


def delete_clone(path: Optional[Path], retries: int = 8) -> None:
    """OSM hard-delete cascade. ``retries`` is ignored; OSM uses its own attempt budget."""
    from opencode_manager.cleanup.end import delete_clone_path

    dest = None if path is None else Path(path)
    ok = delete_clone_path(dest, reason="delete_clone")
    if dest is not None:
        try:
            still = dest.exists()
        except OSError:
            still = True
        if still and not ok:
            log_fail(logger, "git delete clone", dest=dest)
            raise GitError(f"could not remove clone at {dest}")
        log_ok(logger, "git delete clone", dest=dest)
