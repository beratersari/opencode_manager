"""Fake (mocked git / OpenCode) coverage of every job-end and retry path.

Every finished job must kill recorded pids and delete the clone, including
error / 404 / 504. Mid-retry must keep the clone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencode_manager.git.clone import GitError
from opencode_manager.opencode.retry import JobFailed, run_opencode_job
from opencode_manager.opencode import prompts
from opencode_manager.settings import Settings
from opencode_manager.worker import OpenCodeRunner, Terminal, finish_job, run_pipeline

from tests.job_end_helpers import (
    ScriptedClient,
    dest_for,
    dummy_handle,
    make_job,
    patch_opencode_loop,
    store_for,
)


def _clone_ok(dest):
    def impl(*_a, **_k) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "ok").write_text("tree", encoding="utf-8")

    return impl


def test_fake_success_deletes_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = store_for(tmp_settings)
    job = make_job("F-OK")
    dest = dest_for(tmp_settings, job)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", _clone_ok(dest))
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: SimpleNamespace(status_code=200, text="done"),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 200
    assert not dest.exists()


@pytest.mark.parametrize(
    "factory,status,needle",
    [
        (
            lambda dest: (
                lambda *_a, **_k: (_ for _ in ()).throw(GitError("clone exploded")),
                None,
            ),
            500,
            "clone exploded",
        ),
        (
            lambda dest: (
                lambda *_a, **_k: (_ for _ in ()).throw(
                    GitError("git clone timed out after 1.0s")
                ),
                None,
            ),
            500,
            "timed out",
        ),
        (
            lambda dest: (
                _clone_ok(dest),
                lambda *_a, **_k: (_ for _ in ()).throw(JobFailed(500, "still asking")),
            ),
            500,
            "still asking",
        ),
        (
            lambda dest: (
                _clone_ok(dest),
                lambda *_a, **_k: (_ for _ in ()).throw(
                    JobFailed(500, "compact leftover after COMPACT_LOOP_NUDGE")
                ),
            ),
            500,
            "compact leftover",
        ),
        (
            lambda dest: (
                _clone_ok(dest),
                lambda *_a, **_k: (_ for _ in ()).throw(JobFailed(504, "attempt clock")),
            ),
            504,
            "attempt clock",
        ),
        (
            lambda dest: (
                _clone_ok(dest),
                lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("worker boom")),
            ),
            500,
            "worker boom",
        ),
        (
            lambda dest: (
                _clone_ok(dest),
                lambda *_a, **_k: (_ for _ in ()).throw(JobFailed(500, "manager shutting down")),
            ),
            500,
            "shutting down",
        ),
    ],
)
def test_fake_every_runner_error_deletes_clone(
    tmp_settings: Settings, monkeypatch, factory, status, needle
) -> None:
    store = store_for(tmp_settings)
    job = make_job("F-ERR")
    dest = dest_for(tmp_settings, job)
    dest.mkdir(parents=True)
    (dest / "stale").write_text("x", encoding="utf-8")
    clone_fn, oc_fn = factory(dest)
    if clone_fn is not None:
        monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_fn)
    if oc_fn is not None:
        monkeypatch.setattr("opencode_manager.worker.run_opencode_job", oc_fn)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == status
    assert needle.lower() in terminal.text.lower()
    assert not dest.exists()


def test_fake_should_stop_before_clone_deletes_nothing_new(
    tmp_settings: Settings, monkeypatch
) -> None:
    store = store_for(tmp_settings)
    job = make_job("F-STOP")
    dest = dest_for(tmp_settings, job)
    cloned = {"n": 0}
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo",
        lambda *_a, **_k: cloned.__setitem__("n", 1),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: True)
    assert terminal.status_code == 500
    assert cloned["n"] == 0
    assert not dest.exists()


def test_fake_should_stop_after_leftover_delete_skips_clone(
    tmp_settings: Settings, monkeypatch
) -> None:
    store = store_for(tmp_settings)
    job = make_job("F-STOP2")
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    cloned = {"n": 0}
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo",
        lambda *_a, **_k: cloned.__setitem__("n", 1),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(
        job, should_stop=lambda: not dest.exists()
    )
    assert terminal.status_code == 500
    assert cloned["n"] == 0
    assert not dest.exists()


def test_fake_leftover_stuck_does_not_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = store_for(tmp_settings)
    job = make_job("F-STUCK")
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    cloned = {"n": 0}

    def stay(*_a, **_k) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        return False

    monkeypatch.setattr("opencode_manager.cleanup.end.hard_delete", stay)
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo",
        lambda *_a, **_k: cloned.__setitem__("n", cloned["n"] + 1),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert "could not remove leftover" in terminal.text
    assert cloned["n"] == 0
    assert dest.exists()


@pytest.mark.parametrize(
    "code,status_name",
    [(200, "success"), (404, "not_found"), (500, "error"), (504, "timeout")],
)
def test_fake_finish_job_maps_terminal_status(
    tmp_settings: Settings, monkeypatch, code, status_name
) -> None:
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *_a, **_k: None)
    store = store_for(tmp_settings)
    job = make_job(f"F-FIN-{code}", callback_url="http://127.0.0.1:9/cb")
    finish_job(job, Terminal(code, "text"), settings=tmp_settings, store=store)
    assert job.status == status_name
    assert job.live is False
    assert job.callback_status_code == code
    saved = store.get(job.job_id)
    assert saved is not None
    assert saved.status == status_name


def test_fake_pipeline_error_is_terminal(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *_a, **_k: None)
    store = store_for(tmp_settings)
    job = make_job("F-PIPE", callback_url="http://127.0.0.1:9/cb")

    class Boom:
        def run(self, job, *, should_stop):  # noqa: ARG002
            raise RuntimeError("pipeline exploded")

    run_pipeline(
        job,
        settings=tmp_settings,
        store=store,
        runner=Boom(),
        should_stop=lambda: False,
    )
    assert job.status == "error"
    assert "pipeline exploded" in (job.error_message or "")


@pytest.mark.parametrize(
    "script,retry_count,status,prompt_tail",
    [
        ("success", 1, 200, "ORIGINAL"),
        ("asking", 1, 500, "UNATTENDED_NUDGE"),
        ("compact_leftover", 1, 500, "COMPACT_LOOP_NUDGE"),
        ("incomplete", 1, 500, "ORIGINAL"),
        ("hang", 1, 500, "ORIGINAL"),
        ("timeout", 1, 504, "ORIGINAL"),
        ("serve-dead", 1, 500, "ORIGINAL"),
    ],
)
def test_fake_opencode_outcomes_end_job(
    tmp_settings: Settings, monkeypatch, script, retry_count, status, prompt_tail
) -> None:
    tmp_settings.hang_timeout_seconds = 0.0
    tmp_settings.retry_backoff_seconds = 0.0
    job = make_job("F-OC", retry_count=retry_count, timeout_in_seconds=2)
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    client = ScriptedClient(script)
    patch_opencode_loop(monkeypatch, client)
    store = store_for(tmp_settings)
    try:
        result = run_opencode_job(
            job, settings=tmp_settings, store=store, clone=dest, should_stop=lambda: False
        )
        assert result.status_code == status
    except JobFailed as exc:
        assert exc.status_code == status
    assert dest.exists()
    assert any(p.id == prompt_tail for p in job.prompts) or prompt_tail == "ORIGINAL"
    if script == "success":
        assert job.original_posted is True
        assert job.prompts[0].id == "ORIGINAL"


def test_fake_transport_post_fail_does_not_mark_original(
    tmp_settings: Settings, monkeypatch
) -> None:
    job = make_job("F-POST")
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    client = ScriptedClient("success")
    client.post_error = RuntimeError("refused")
    patch_opencode_loop(monkeypatch, client)
    with pytest.raises(JobFailed) as exc:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store_for(tmp_settings),
            clone=dest,
            should_stop=lambda: False,
        )
    assert exc.value.status_code == 500
    assert job.original_posted is False
    assert job.prompts == []
    assert dest.exists()


def test_fake_midjob_resume_reject_fails_attempt(
    tmp_settings: Settings, monkeypatch
) -> None:
    job = make_job("F-RESUME", session_id="ses_live", session_bound=True)
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    client = ScriptedClient("success")
    client.force_create = True
    patch_opencode_loop(monkeypatch, client)
    with pytest.raises(JobFailed) as exc:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store_for(tmp_settings),
            clone=dest,
            should_stop=lambda: False,
        )
    assert exc.value.status_code == 500
    assert "blank session" in exc.value.message
    assert dest.exists()


def test_fake_first_session_create_fail_retries_then_500(
    tmp_settings: Settings, monkeypatch
) -> None:
    job = make_job("F-CREATE", retry_count=2, session_bound=False)
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    client = ScriptedClient("success")
    client.resume_error = RuntimeError("create exploded")
    patch_opencode_loop(monkeypatch, client)
    tmp_settings.retry_backoff_seconds = 0
    with pytest.raises(JobFailed) as exc:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store_for(tmp_settings),
            clone=dest,
            should_stop=lambda: False,
        )
    assert exc.value.status_code == 500
    assert len(job.attempts) == 2
    assert all(a.kind == "create-fail" for a in job.attempts)
    assert dest.exists()


def test_fake_hang_retry_keeps_clone_and_sends_hang_resume(
    tmp_settings: Settings, monkeypatch
) -> None:
    tmp_settings.hang_timeout_seconds = 0.0
    tmp_settings.retry_backoff_seconds = 0.0
    job = make_job("F-HANG2", retry_count=2, timeout_in_seconds=5)
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    (dest / "keep.txt").write_text("stay", encoding="utf-8")
    client = ScriptedClient("hang_then_success")
    starts = {"n": 0}

    def start_serve(**kwargs):
        starts["n"] += 1
        assert dest.exists()
        handle = dummy_handle(pid=1000 + starts["n"])
        on_spawn = kwargs.get("on_spawn")
        if on_spawn:
            on_spawn(handle)
        return handle

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", start_serve)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", lambda *_a, **_k: client)
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry._backoff", lambda *_a, **_k: None)
    result = run_opencode_job(
        job,
        settings=tmp_settings,
        store=store_for(tmp_settings),
        clone=dest,
        should_stop=lambda: False,
    )
    assert result.status_code == 200
    assert starts["n"] == 2
    assert dest.exists()
    assert job.prompts[0].id == "ORIGINAL"
    assert job.prompts[1].id == "HANG_RESUME"
    assert job.prompts[1].text == prompts.HANG_RESUME


def test_fake_incomplete_same_serve_sends_incomplete_resume(
    tmp_settings: Settings, monkeypatch
) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    job = make_job("F-INC2", retry_count=2, timeout_in_seconds=5)
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    client = ScriptedClient("incomplete_then_success")
    starts = {"n": 0}

    def start_serve(**kwargs):
        starts["n"] += 1
        handle = dummy_handle()
        on_spawn = kwargs.get("on_spawn")
        if on_spawn:
            on_spawn(handle)
        return handle

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", start_serve)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", lambda *_a, **_k: client)
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    result = run_opencode_job(
        job,
        settings=tmp_settings,
        store=store_for(tmp_settings),
        clone=dest,
        should_stop=lambda: False,
    )
    assert result.status_code == 200
    assert starts["n"] == 1
    assert dest.exists()
    assert [p.id for p in job.prompts] == ["ORIGINAL", "INCOMPLETE_RESUME"]
    assert job.prompts[1].text == prompts.INCOMPLETE_RESUME


def test_fake_should_stop_during_loop_fails_500(
    tmp_settings: Settings, monkeypatch
) -> None:
    job = make_job("F-STOP3", timeout_in_seconds=5)
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    client = ScriptedClient("timeout")
    patch_opencode_loop(monkeypatch, client)
    with pytest.raises(JobFailed) as exc:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store_for(tmp_settings),
            clone=dest,
            should_stop=lambda: True,
        )
    assert exc.value.status_code == 500
    assert "shutting down" in exc.value.message
    assert dest.exists()


@pytest.mark.parametrize(
    "script,status",
    [
        ("success", 200),
        ("timeout", 504),
        ("hang", 500),
        ("asking", 500),
        ("serve-dead", 500),
        ("incomplete", 500),
        ("compact_leftover", 500),
    ],
)
def test_fake_worker_wraps_each_opencode_outcome_and_deletes(
    tmp_settings: Settings, monkeypatch, script, status
) -> None:
    tmp_settings.hang_timeout_seconds = 0.0
    tmp_settings.retry_backoff_seconds = 0.0
    store = store_for(tmp_settings)
    job = make_job("F-WRAP", timeout_in_seconds=2)
    dest = dest_for(tmp_settings, job)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", _clone_ok(dest))
    client = ScriptedClient(script)
    patch_opencode_loop(monkeypatch, client)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == status
    assert not dest.exists()
    assert client.closed is True
