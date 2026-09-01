"""Job-end kill order (PLAN §8)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Set

from opencode_manager.cleanup.kill import (
    drop_git_locks,
    kill_file_holders,
    kill_job_tree,
    kill_pid,
    may_kill,
    path_has_holders,
    protected_pids,
    query_windows_restart_manager,
    reap_path,
)
from opencode_manager.cleanup.rmtree import hard_delete
from opencode_manager.log import get_logger
from opencode_manager.models import JobRecord

logger = get_logger()


def protect_pids(
    *groups: Optional[Iterable[Optional[int]]],
) -> Set[int]:
    out: Set[int] = set(protected_pids())
    for group in groups:
        if not group:
            continue
        for pid in group:
            try:
                if pid and int(pid) > 4:
                    out.add(int(pid))
            except (TypeError, ValueError):
                continue
    return out


def stop_job_holders(
    job: JobRecord,
    clone: Optional[Path],
    *,
    protect: Optional[Iterable[int]] = None,
) -> None:
    """Abort is the caller's job. Then kill this tree, leftovers, holders, stale locks."""
    guarded = protect_pids(protect)
    logger.info(
        "stop job holders job_id=%s serve_pid=%s extra_pids=%s clone=%s",
        job.job_id,
        job.serve_pid,
        list(job.extra_pids),
        clone,
    )
    try:
        extra = list(job.extra_pids or [])
    except Exception:  # noqa: BLE001
        extra = []
    try:
        kill_job_tree([job.serve_pid, *extra])
    except Exception:  # noqa: BLE001
        logger.exception("kill_job_tree failed job_id=%s", job.job_id)
    job.extra_pids = []
    if clone is None:
        return
    try:
        exists = clone.exists()
    except OSError:
        exists = True
    if not exists:
        logger.info("clone missing; skip process scan clone=%s", clone)
        return
    try:
        reap_path(clone, protect=guarded)
    except Exception:  # noqa: BLE001
        logger.exception("reap_path failed clone=%s", clone)
    try:
        # Windows: do not call RM here. delete_clone_path tries rd first;
        # RM runs only if the folder is still there (at most two children).
        if os.name != "nt":
            kill_file_holders(clone, protect=guarded)
    except Exception:  # noqa: BLE001
        logger.exception("kill_file_holders failed clone=%s", clone)
    try:
        if os.name == "nt":
            drop_git_locks(clone)
            return
        if path_has_holders(clone, protect=guarded):
            logger.warning("holders remain; leaving .git locks in place clone=%s", clone)
            return
        drop_git_locks(clone)
    except Exception:  # noqa: BLE001
        logger.exception("holder/lock pass failed clone=%s", clone)


def retry_windows_delete_if_held(
    clone: Path,
    *,
    protect: Optional[Iterable[int]] = None,
) -> bool:
    """If the clone remains, ask RM (new child). Retry once only if that child died."""
    guarded = protect_pids(protect)
    helper_died = False
    for attempt in (1, 2):
        try:
            exists = clone.exists()
        except OSError:
            exists = True
        if not exists:
            return True
        if attempt == 2 and not helper_died:
            logger.info("rm helper survived; not retrying RM clone=%s", clone)
            return False
        logger.info("restart manager leftover clone attempt=%s/2 clone=%s", attempt, clone)
        try:
            result = query_windows_restart_manager(clone)
        except Exception:  # noqa: BLE001
            logger.exception("restart manager query failed clone=%s", clone)
            result = None
        helper_died = bool(result is None or result.died)
        if result is not None:
            for raw in result.pids:
                if raw in guarded or not may_kill(raw):
                    continue
                try:
                    logger.info("kill leftover holder pid=%s clone=%s", raw, clone)
                    kill_pid(raw)
                except Exception:  # noqa: BLE001
                    logger.exception("kill leftover holder failed pid=%s", raw)
        try:
            if hard_delete(clone):
                return True
        except Exception:  # noqa: BLE001
            logger.exception("hard_delete after RM failed clone=%s", clone)
    try:
        return not clone.exists()
    except OSError:
        return False


def delete_clone_path(clone: Optional[Path], *, reason: str) -> bool:
    if clone is None:
        return True
    try:
        exists = clone.exists()
    except OSError:
        exists = True
    logger.info("remove clone reason=%s clone_path=%s exists=%s", reason, clone, exists)
    try:
        ok = hard_delete(clone)
    except Exception:  # noqa: BLE001
        logger.exception("hard_delete raised clone_path=%s", clone)
        ok = False
    try:
        still = clone.exists()
    except OSError:
        still = True
    if still and os.name == "nt":
        try:
            ok = retry_windows_delete_if_held(clone)
        except Exception:  # noqa: BLE001
            logger.exception("retry_windows_delete_if_held failed clone_path=%s", clone)
            ok = False
        try:
            still = clone.exists()
        except OSError:
            still = True
    if still:
        logger.error("clone still present after %s delete clone_path=%s", reason, clone)
    else:
        logger.info("clone gone after %s delete clone_path=%s ok=%s", reason, clone, ok)
    return not still
