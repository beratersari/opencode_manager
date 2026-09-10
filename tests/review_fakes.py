"""Shared review-path test doubles."""

from __future__ import annotations

import threading

from opencode_manager.review_worker import RunResult


class FakeRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.runs: list[str] = []
        self.current: str = ""

    def run(self, job, should_stop):  # noqa: ANN001
        self.current = job.job_id
        self.runs.append(job.trigger + ":" + (job.comment_text or ""))
        self.started.set()
        while not self.release.wait(0.05):
            if should_stop():
                return RunResult(cancelled=True, error="cancelled")
        self.release.clear()
        return RunResult(text="ok " + job.trigger, session_id="ses_test", posted=True)
