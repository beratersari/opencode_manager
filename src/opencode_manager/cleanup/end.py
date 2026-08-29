"""Job-end kill order (PLAN §8)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Set

from opencode_manager.cleanup.kill import (
    drop_git_locks,
    kill_file_holders,
    kill_job_tree,
    path_has_holders,
    reap_path,
)
from opencode_manager.cleanup.rmtree import hard_delete
from opencode_manager.log import get_logger
from opencode_manager.models import JobRecord

logger = get_logger()


def protect_pids(
    *groups: Optional[Iterable[Optional[int]]],
) -> Set[int]:
    out: Set[int] = {os.getpid()}
    for group in groups:
        if not group:
            continue
        for pid in group:
            if pid:
                out.add(int(pid))
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
    kill_job_tree([job.serve_pid, *list(job.extra_pids)])
    job.extra_pids = []
    if clone is None:
        return
    reap_path(clone, protect=guarded)
    kill_file_holders(clone, protect=guarded)
    if path_has_holders(clone, protect=guarded):
        logger.warning("holders remain; leaving .git locks in place clone=%s", clone)
        return
    drop_git_locks(clone)


def delete_clone_path(clone: Optional[Path], *, reason: str) -> bool:
    if clone is None:
        return True
    logger.info("remove clone reason=%s clone_path=%s exists=%s", reason, clone, clone.exists())
    ok = hard_delete(clone)
    still = clone.exists()
    if still:
        logger.error("clone still present after %s delete clone_path=%s", reason, clone)
    else:
        logger.info("clone gone after %s delete clone_path=%s ok=%s", reason, clone, ok)
    return not still
