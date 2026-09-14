from __future__ import annotations

import time

from opencode_manager.gitlab.events import CleanupTrigger, ReviewTrigger
from opencode_manager.review_manager import ReviewManager
from tests.review_fakes import FakeRunner


def _review(kind="review", explicit=True, comment="", project=1, iid=1) -> ReviewTrigger:
    return ReviewTrigger(
        kind=kind,
        project_id=project,
        mr_iid=iid,
        source_branch="feat",
        target_branch="main",
        comment_text=comment,
        explicit=explicit,
    )


def test_second_comment_is_queued_then_runs(tmp_config):
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    ack1, job1, _ = manager.submit(_review("review", comment="one"))
    assert ack1 == "accepted"
    assert runner.started.wait(2)
    ack2, job2, _ = manager.submit(_review("ask", comment="two"))
    assert ack2 == "queued"
    assert job2 is not None
    assert job2.status == "queued"
    runner.release.set()
    deadline = time.time() + 3
    while time.time() < deadline and len(runner.runs) < 2:
        time.sleep(0.05)
        runner.release.set()
    assert runner.runs[0].startswith("review")
    assert any(r.startswith("ask") for r in runner.runs)
    manager.shutdown()


def test_two_mrs_can_run_in_parallel(tmp_config):
    runner = FakeRunner()
    tmp_config.max_concurrent_jobs = 2
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    manager.submit(_review(project=1, iid=1))
    manager.submit(_review(project=1, iid=2))
    time.sleep(0.2)
    running = [j for j in manager.store.list_all() if j.status == "running"]
    assert len(running) == 2
    runner.release.set()
    manager.shutdown()


def test_assign_while_review_busy_is_ignored(tmp_config):
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    manager.submit(_review("review", comment="first"))
    assert runner.started.wait(2)
    ack, job, message = manager.submit(_review("review"))
    assert ack == "ignored"
    assert job is None
    assert "already" in message
    runner.release.set()
    manager.shutdown()


def test_assign_while_usage_job_is_accepted(tmp_config):
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    ack1, job1, _ = manager.submit(_review("usage", comment="how to"))
    assert ack1 == "accepted"
    assert runner.started.wait(2)
    ack2, job2, _ = manager.submit(_review("review"))
    assert ack2 == "queued"
    assert job2 is not None
    runner.release.set()
    manager.shutdown()


def test_auto_event_skipped_when_busy(tmp_config):
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    manager.submit(_review("review"))
    assert runner.started.wait(2)
    ack, job, _ = manager.submit(_review("update", explicit=False))
    assert ack == "ignored"
    assert job is None
    runner.release.set()
    manager.shutdown()


def test_close_cancels_running_and_queued(tmp_config):
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    manager.submit(_review("review", comment="a"))
    assert runner.started.wait(2)
    manager.submit(_review("ask", comment="b"))
    manager.cleanup_mr(CleanupTrigger(project_id=1, mr_iid=1, action="close"))
    time.sleep(0.3)
    statuses = {j.comment_text: j.status for j in manager.store.list_all()}
    assert "queued" not in statuses.values()
    assert statuses.get("b") == "cancelled"
    assert "running" not in statuses.values()
    assert manager.queue.queued_ids("1-1") == []
    assert not any(r.startswith("ask:") for r in runner.runs)
    runner.release.set()
    manager.shutdown()
