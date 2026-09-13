"""Review boot must not resume leftovers; finish save must free the MR slot."""

from __future__ import annotations

import time

from opencode_manager.dashboard.store import JobStore
from opencode_manager.gitlab.events import ReviewTrigger
from opencode_manager.models import JobRecord, utc_now
from opencode_manager.review_fifo import JobQueue
from opencode_manager.review_manager import ReviewManager
from opencode_manager.review_worker import RunResult
from tests.review_fakes import FakeRunner


def _review(**kwargs) -> ReviewTrigger:
    data = dict(
        kind="review",
        project_id=1,
        mr_iid=9,
        source_branch="feat",
        target_branch="main",
        explicit=True,
        web_url="https://gitlab.example/g/r",
        title="t",
    )
    data.update(kwargs)
    return ReviewTrigger(**data)


def _leftover(**kwargs) -> JobRecord:
    data = dict(
        job_id="job_leftover_review",
        jira_id="1-9",
        job_kind="review",
        status="queued",
        live=True,
        mr_key="1-9",
        project_id=1,
        mr_iid=9,
        prompt="review",
        model="x/y",
        agent_mode="code-reviewer",
        repo_url="https://example.com/r.git",
        source_branch="main",
        timeout_in_seconds=60,
        retry_count=1,
        accepted_at=utc_now(),
        trigger="review",
    )
    data.update(kwargs)
    return JobRecord(**data)


def _wait_until(pred, timeout: float = 4.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_review_fifo_clear_empties_memory_and_disk(tmp_path) -> None:
    q = JobQueue(tmp_path / "review_queue.json")
    q.enqueue("1-9", "job_a")
    q.enqueue("2-1", "job_b")
    old = q.clear()
    assert "1-9" in old and "2-1" in old
    assert q.queued_ids() == []
    assert q.queued_ids("1-9") == []
    q2 = JobQueue(tmp_path / "review_queue.json")
    assert q2.queued_ids() == []


def test_review_boot_does_not_resume_leftover_queued(tmp_config) -> None:
    store = JobStore(tmp_config.job_dir)
    store.save(_leftover(status="queued", live=True))
    q = JobQueue(tmp_config.data_dir / "review_queue.json")
    q.enqueue("1-9", "job_leftover_review")
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner, store=store, queue=q)
    manager.boot()
    time.sleep(0.3)
    assert runner.runs == []
    row = store.get("job_leftover_review")
    assert row is not None
    assert row.status == "error"
    assert row.live is False
    assert manager.queue.queued_ids() == []
    assert manager._running == 0
    assert manager._running_mr == set()
    ack, job, _ = manager.submit(_review())
    assert ack == "accepted"
    assert job is not None
    assert job.job_id != "job_leftover_review"
    assert runner.started.wait(2)
    runner.release.set()
    manager.shutdown()


def test_review_boot_errors_two_queued_mrs(tmp_config) -> None:
    store = JobStore(tmp_config.job_dir)
    store.save(_leftover(job_id="job_a", jira_id="1-1", mr_key="1-1", project_id=1, mr_iid=1))
    store.save(_leftover(job_id="job_b", jira_id="1-2", mr_key="1-2", project_id=1, mr_iid=2))
    q = JobQueue(tmp_config.data_dir / "review_queue.json")
    q.enqueue("1-1", "job_a")
    q.enqueue("1-2", "job_b")
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner, store=store, queue=q)
    manager.boot()
    time.sleep(0.2)
    assert runner.runs == []
    assert store.get("job_a").status == "error"
    assert store.get("job_b").status == "error"
    assert manager.queue.queued_ids() == []


def test_review_boot_does_not_resume_leftover_running(tmp_config) -> None:
    store = JobStore(tmp_config.job_dir)
    store.save(_leftover(job_id="job_leftover_running", status="running", live=True))
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner, store=store)
    manager.boot()
    time.sleep(0.3)
    assert runner.runs == []
    row = store.get("job_leftover_running")
    assert row is not None
    assert row.status == "error"
    assert row.live is False


def test_review_boot_drains_queue_file_even_without_store_row(tmp_config) -> None:
    q = JobQueue(tmp_config.data_dir / "review_queue.json")
    q.enqueue("9-9", "job_ghost")
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner, queue=q)
    manager.boot()
    assert manager.queue.queued_ids() == []
    assert runner.runs == []


def test_review_boot_queue_clear_fail_still_errors_leftover(tmp_config, monkeypatch) -> None:
    store = JobStore(tmp_config.job_dir)
    store.save(_leftover())
    q = JobQueue(tmp_config.data_dir / "review_queue.json")
    q.enqueue("1-9", "job_leftover_review")

    def boom(self):  # noqa: ANN001
        self._rows = {}
        raise OSError("Access is denied")

    monkeypatch.setattr(JobQueue, "clear", boom)
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner, store=store, queue=q)
    manager.boot()
    time.sleep(0.2)
    assert runner.runs == []
    row = store.get("job_leftover_review")
    assert row is not None
    assert row.status == "error"
    assert manager.queue.queued_ids() == []


def test_review_finish_save_failure_does_not_freeze_fifo(tmp_config, monkeypatch) -> None:
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    real_save = manager.store.save

    def flaky(job):  # noqa: ANN001
        if getattr(job, "status", "") in {"success", "error", "timeout", "cancelled"}:
            raise OSError("Access is denied")
        return real_save(job)

    monkeypatch.setattr(manager.store, "save", flaky)
    ack1, job1, _ = manager.submit(_review(comment_text="one"))
    assert ack1 == "accepted"
    assert job1 is not None
    assert runner.started.wait(2)
    runner.release.set()
    _wait_until(lambda: manager._running == 0)
    assert manager._running_mr == set()
    overlay = manager.store.get(job1.job_id)
    assert overlay is not None
    assert overlay.status == "success"
    assert overlay.live is False
    assert manager.store.running_for_mr("1-9") is None
    ack2, job2, _ = manager.submit(_review(kind="ask", comment_text="two"))
    assert ack2 == "accepted"
    assert job2 is not None
    assert job2.job_id != job1.job_id
    assert runner.started.wait(2)
    runner.release.set()
    _wait_until(lambda: any(r.startswith("ask") for r in runner.runs))
    manager.shutdown()


def test_review_finish_save_failure_still_dispatches_other_mr(tmp_config, monkeypatch) -> None:
    tmp_config.max_concurrent_jobs = 1
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    real_save = manager.store.save

    def flaky(job):  # noqa: ANN001
        if getattr(job, "status", "") == "success":
            raise OSError("Access is denied")
        return real_save(job)

    monkeypatch.setattr(manager.store, "save", flaky)
    manager.submit(_review(project_id=1, mr_iid=1, comment_text="a"))
    assert runner.started.wait(2)
    runner.started.clear()
    ack, job, _ = manager.submit(_review(project_id=1, mr_iid=2, comment_text="b"))
    assert ack == "queued"
    runner.release.set()
    _wait_until(lambda: job is not None and manager.store.get(job.job_id).status == "running")
    runner.release.set()
    manager.shutdown()
