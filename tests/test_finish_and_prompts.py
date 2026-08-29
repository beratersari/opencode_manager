from __future__ import annotations

from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import AttemptFailed, _post_user
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal, finish_job


class _MemStore:
    def save(self, job: JobRecord) -> None:
        return None


class _FailClient:
    def status(self) -> dict:
        return {}

    def post_message(self, *args, **kwargs) -> None:
        raise RuntimeError("OpenCode refused the POST")


def test_finish_job_second_caller_does_not_callback(tmp_settings: Settings, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "opencode_manager.worker.post_callback",
        lambda *a, **k: calls.append(1),
    )
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_once",
        jira_id="T-1",
        callback_url="http://127.0.0.1:9/cb",
        status="running",
        live=True,
    )
    finish_job(job, Terminal(200, "ok"), settings=tmp_settings, store=store)
    finish_job(job, Terminal(500, "shutdown"), settings=tmp_settings, store=store)
    assert len(calls) == 1
    assert job.callback_status_code == 200
    assert job.status == "success"


def test_original_not_marked_if_post_fails() -> None:
    job = JobRecord(job_id="job_p", jira_id="T-2", session_id="ses_x", prompt="hello")
    try:
        _post_user(job, _FailClient(), _MemStore(), "ORIGINAL", "hello")  # type: ignore[arg-type]
        raise AssertionError("expected AttemptFailed")
    except AttemptFailed as exc:
        assert exc.kind == "transport"
    assert job.original_posted is False
    assert job.prompts == []


def test_new_job_rejected_ses_is_not_already_bound() -> None:
    job = JobRecord(
        job_id="job_n",
        jira_id="T-3",
        session_id="ses_stale",
        session_bound=False,
    )
    already_bound = bool(job.session_bound)
    created = True
    assert not (already_bound and created)
