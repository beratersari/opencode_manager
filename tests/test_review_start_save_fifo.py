"""A failed review start-save must not freeze the MR FIFO."""

import time
from pathlib import Path

from opencode_manager.gitlab.events import ReviewTrigger
from opencode_manager.models import JobRecord
from opencode_manager.review_manager import ReviewManager
from opencode_manager.review_worker import RunResult


def test_review_start_save_failure_does_not_freeze_mr(tmp_config, tmp_path: Path) -> None:
    class HoldRunner:
        def run(self, job, should_stop):  # noqa: ANN001, ARG002
            return RunResult(text="ok", posted=True, session_id="ses_rev")

    manager = ReviewManager(tmp_config, HoldRunner())
    manager.ready = True
    real_save = manager.store.save

    def boom(job: JobRecord) -> None:
        if job.status == "running":
            raise OSError("Access is denied")
        return real_save(job)

    manager.store.save = boom  # type: ignore[method-assign]
    trigger = ReviewTrigger(
        kind="ask",
        explicit=True,
        project_id=1,
        mr_iid=9,
        source_branch="feature",
        target_branch="main",
        sha="abc",
        web_url="https://gitlab.example/g/r/-/merge_requests/9",
        title="fix",
        comment_text="please look",
    )
    ack, job, _msg = manager.submit(trigger)
    assert job is not None
    assert ack in {"accepted", "queued"}
    time.sleep(0.2)
    assert job.mr_key not in manager._running_mr
    assert manager._running == 0
    queued = manager.queue.queued_ids(job.mr_key)
    assert job.job_id in queued or (manager.store.get(job.job_id) or job).status == "queued"
