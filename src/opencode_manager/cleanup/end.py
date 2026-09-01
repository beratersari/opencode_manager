"""Job-end kill order (PLAN §8)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Set

from opencode_manager.cleanup.kill import (
    drop_git_locks,
    kill_file_holders,
    kill_job_tree,
    path_has_holders,
    protected_pids,
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
        kill_file_holders(clone, protect=guarded)
    except Exception:  # noqa: BLE001
        logger.exception("kill_file_holders failed clone=%s", clone)
    try:
        if path_has_holders(clone, protect=guarded):
            logger.warning("holders remain; leaving .git locks in place clone=%s", clone)
            return
        drop_git_locks(clone)
    except Exception:  # noqa: BLE001
        logger.exception("holder/lock pass failed clone=%s", clone)


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
    if still:
        logger.error("clone still present after %s delete clone_path=%s", reason, clone)
    else:
        logger.info("clone gone after %s delete clone_path=%s ok=%s", reason, clone, ok)
    return not still
