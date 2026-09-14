"""Live OpenCode 1.18.10 proofs for the OpenCode-path true positives.

Starts a real ``opencode serve``. No HTTP stub of the compact/hang/model
APIs. Other TPs (queue, git 401, store size, list payload, review FIFO)
are covered in ``test_daily_usage_real.py`` with real git/store/Manager.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import JobFailed, _inner_loop
from opencode_manager.opencode.session import (
    OpenCodeClient,
    assess_idle,
    assistant_turn_is_substantive,
    last_assistant_id,
    last_assistant_text_since,
    looks_like_unknown_model_error,
    messages_after_id,
)
from opencode_manager.settings import Settings

PREFERRED = (
    "opencode/mimo-v2.5-free",
    "opencode/ling-3.0-flash-free",
    "opencode/laguna-s-2.1-free",
    "opencode/deepseek-v4-flash-free",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _info(msg: dict) -> dict:
    info = msg.get("info")
    return info if isinstance(info, dict) else msg


class _MemStore:
    def save(self, job: JobRecord) -> None:
        return None


class LiveServe:
    def __init__(self, cwd: Path, log_path: Path) -> None:
        bin_path = shutil.which("opencode")
        if not bin_path:
            pytest.skip("opencode binary not on PATH")
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.cwd = cwd
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log_path, "w", encoding="utf-8")
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = ""
        self.proc = subprocess.Popen(
            [
                bin_path,
                "serve",
                "--pure",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--print-logs",
                "--log-level",
                "INFO",
            ],
            cwd=str(cwd),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.client = OpenCodeClient(self.base, str(cwd))

    def wait_ready(self) -> None:
        deadline = time.time() + 90
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                pytest.fail(f"opencode serve exited {self.proc.returncode}: {tail}")
            try:
                if self.client.health():
                    self.client.wait_directory(timeout=30)
                    return
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.3)
        pytest.fail(f"serve not ready: {last}")

    def pick_model(self) -> tuple[str, str]:
        listed = self.client.list_known_models(timeout=20) or []
        cli = []
        bin_path = shutil.which("opencode")
        if bin_path:
            got = subprocess.run(
                [bin_path, "models"], capture_output=True, text=True, timeout=90
            )
            cli = [
                ln.strip()
                for ln in (got.stdout or "").splitlines()
                if ln.strip() and "free" in ln.lower()
            ]
        bag = list(dict.fromkeys([*listed, *cli]))
        free = [m for m in bag if "free" in m.lower() and "/" in m]
        for pref in PREFERRED:
            if pref in free or pref in bag:
                return pref.split("/", 1)[0], pref.split("/", 1)[1]
        if free:
            return free[0].split("/", 1)[0], free[0].split("/", 1)[1]
        pytest.fail(f"no free model on live serve: {bag[:20]}")

    def create(self, title: str) -> str:
        return self.client.create_session(title)

    def prompt(self, sid: str, text: str, *, provider: str, model: str, agent: str = "orchestrator") -> None:
        self.client.post_message(sid, text, model=f"{provider}/{model}", agent=agent)

    def wait_idle(self, sid: str, timeout: float = 180) -> list:
        deadline = time.time() + timeout
        last: Optional[str] = None
        while time.time() < deadline:
            try:
                status = self.client.status()
                info = self.client.session_payload(sid)
                msgs = self.client.list_messages(sid)
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                time.sleep(0.3)
                continue
            from opencode_manager.opencode.session import session_is_busy, session_is_compacting

            busy = session_is_busy(status, sid)
            compacting = session_is_compacting(status, sid, session_info=info)
            if msgs and not busy and not compacting:
                last_a = None
                for msg in reversed(msgs):
                    inf = _info(msg)
                    if (inf.get("role") or msg.get("role")) == "assistant":
                        last_a = inf
                        break
                finish = str((last_a or {}).get("finish") or "").strip().lower()
                if last_a is not None and (finish == "stop" or (last_a.get("error"))):
                    return msgs
            last = f"busy={busy} compacting={compacting} n={len(msgs)}"
            time.sleep(0.25)
        pytest.fail(f"session {sid} did not go idle: {last}")

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            self._log.close()
        except Exception:
            pass


@pytest.fixture
def live_serve(tmp_path: Path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    (cwd / "README.md").write_text("live tp fixture\n", encoding="utf-8")
    serve = LiveServe(cwd, tmp_path / "serve.log")
    try:
        serve.wait_ready()
        yield serve
    finally:
        serve.close()


def _job(sid: str, model: str) -> JobRecord:
    return JobRecord(
        job_id="job_live_tp",
        jira_id="LIVETP-1",
        session_id=sid,
        model=model,
        agent_mode="orchestrator",
        prompt="do it",
        timeout_in_seconds=60,
        retry_count=1,
        original_posted=True,
        session_bound=True,
    )


@pytest.mark.live
def test_live_stub_is_not_substantive_and_compact_recap_is_pending(
    live_serve: LiveServe, tmp_settings: Settings
) -> None:
    """TP 6 + 8: real stub is not progress; manual compact recap is not success."""
    provider, model = live_serve.pick_model()
    sid = live_serve.create("tp-stub-compact")
    live_serve.prompt(
        sid,
        "Reply with exactly the word PING and nothing else. Do not use tools.",
        provider=provider,
        model=model,
    )
    saw_stub = False
    deadline = time.time() + 30
    while time.time() < deadline:
        msgs = live_serve.client.list_messages(sid)
        if msgs and not assistant_turn_is_substantive(msgs, ""):
            for msg in reversed(msgs):
                inf = _info(msg)
                if (inf.get("role") or msg.get("role")) != "assistant":
                    continue
                finish = str(inf.get("finish") or "")
                parts = msg.get("parts") or []
                texts = [
                    str(p.get("text") or "")
                    for p in parts
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                if not finish and not any(t.strip() for t in texts):
                    saw_stub = True
                break
        if saw_stub:
            break
        time.sleep(0.05)
    idle = live_serve.wait_idle(sid, timeout=180)
    assert last_assistant_id(idle)
    assert assistant_turn_is_substantive(idle, "") is True
    assert saw_stub, (
        "live OpenCode did not expose a blank stub assistant in 30s; "
        "cannot prove the hang predicate on a real stub this run"
    )

    baseline = last_assistant_id(idle)
    r = live_serve.client.http.post(
        f"/session/{sid}/summarize",
        json={"providerID": provider, "modelID": model, "auto": False},
        timeout=300,
    )
    assert r.status_code < 400, r.text[:400]
    after = live_serve.wait_idle(sid, timeout=240)
    last = after[-1]
    inf = _info(last)
    assert (inf.get("role") or last.get("role")) == "assistant"
    assert inf.get("summary") is True or str(inf.get("mode") or "") == "compaction"
    assert str(inf.get("finish") or "").lower() == "stop"
    assert assess_idle(after, baseline_assistant_id=baseline) == "pending"
    assert assess_idle(after, baseline_assistant_id=baseline) != "success"


@pytest.mark.live
def test_live_auto_continue_is_pending_not_leftover(live_serve: LiveServe) -> None:
    """TP 9: real synthetic Continue is not compact_leftover."""
    provider, model = live_serve.pick_model()
    sid = live_serve.create("tp-auto-continue")
    live_serve.prompt(
        sid,
        "Reply with exactly the word PONG and nothing else. Do not use tools.",
        provider=provider,
        model=model,
    )
    first = live_serve.wait_idle(sid, timeout=180)
    baseline = last_assistant_id(first)

    poller = httpx.Client(
        base_url=live_serve.base,
        headers={"x-opencode-directory": str(live_serve.cwd)},
        timeout=30.0,
        verify=False,
    )
    err: list[str] = []

    def _summarize() -> None:
        try:
            r = httpx.post(
                f"{live_serve.base}/session/{sid}/summarize",
                json={"providerID": provider, "modelID": model, "auto": True},
                headers={"x-opencode-directory": str(live_serve.cwd)},
                timeout=300.0,
            )
            if r.status_code >= 400:
                err.append(f"HTTP {r.status_code} {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            err.append(str(exc))

    import threading

    threading.Thread(target=_summarize, daemon=True).start()
    continue_idx = None
    continue_assess = None
    deadline = time.time() + 320
    while time.time() < deadline:
        try:
            msgs = poller.get(f"/session/{sid}/message", params={"limit": 400}).json()
        except Exception:
            time.sleep(0.3)
            continue
        if not isinstance(msgs, list):
            time.sleep(0.3)
            continue
        for i, msg in enumerate(msgs):
            parts = msg.get("parts") or []
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                meta = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
                if meta.get("compaction_continue") is True:
                    continue_idx = i
                    continue_assess = assess_idle(
                        msgs[: i + 1], baseline_assistant_id=baseline
                    )
                    break
            if continue_idx is not None:
                break
        if continue_idx is not None:
            break
        time.sleep(0.25)
    poller.close()
    assert continue_idx is not None, (
        f"live auto-summarize did not insert compaction_continue err={err}"
    )
    assert continue_assess == "pending"
    assert continue_assess != "compact_leftover"


@pytest.mark.live
def test_live_prior_unknown_model_does_not_fail_this_turn(
    live_serve: LiveServe, tmp_settings: Settings
) -> None:
    """TP 7: a previous ProviderModelNotFoundError must not 500 this turn."""
    provider, model = live_serve.pick_model()
    sid = live_serve.create("tp-unknown-prior")
    # A completed real turn first, then a bad-model POST so the error is
    # prior history for the next turn (the daily resume case).
    live_serve.prompt(
        sid,
        "Reply with exactly the word X and nothing else. Do not use tools.",
        provider=provider,
        model=model,
    )
    live_serve.wait_idle(sid, timeout=180)
    try:
        live_serve.client.http.post(
            f"/session/{sid}/message",
            json={
                "agent": "orchestrator",
                "parts": [{"type": "text", "text": "now use a missing model"}],
                "model": {"providerID": "opencode", "modelID": "does-not-exist-xyz-zz"},
            },
            timeout=60,
        )
    except Exception:
        pass
    try:
        live_serve.prompt(
            sid,
            "now use a missing model",
            provider="opencode",
            model="does-not-exist-xyz-zz",
        )
    except Exception:
        pass
    deadline = time.time() + 60
    prior = []
    while time.time() < deadline:
        prior = live_serve.client.list_messages(sid)
        if looks_like_unknown_model_error(str(prior)):
            break
        time.sleep(0.5)
    if not looks_like_unknown_model_error(str(prior)):
        pytest.skip(
            "this OpenCode 1.18.10 serve does not persist ProviderModelNotFoundError "
            "on GET /session/:id/message (only the user part is stored). "
            "TP 7 is covered by test_unknown_model_scan_ignores_prior_job_history "
            "with a real HTTP peer that returns that error shape."
        )
    baseline = last_assistant_id(prior)
    live_serve.prompt(
        sid,
        "Reply with exactly the word OKAY and nothing else. Do not use tools.",
        provider=provider,
        model=model,
    )
    tmp_settings.hang_timeout_seconds = 30
    job = _job(sid, f"{provider}/{model}")
    try:
        outcome = _inner_loop(
            job,
            live_serve.client,
            _MemStore(),
            settings=tmp_settings,
            deadline=time.time() + 180,
            should_stop=lambda: False,
            baseline_assistant_id=baseline,
            baseline_n=len(prior),
            baseline_compact_n=0,
        )
    except JobFailed as exc:
        pytest.fail(f"prior unknown-model leaked into this turn: {exc}")
    assert outcome != "success" or "not available" not in (job.text or "").lower()
    # Must not raise JobFailed unknown-model. If we got here, it did not.
    assert outcome in {"success", "question", "incomplete", "timeout", "hang", "asking"}
    if outcome == "success":
        product = job.text or last_assistant_text_since(
            live_serve.client.list_messages(sid), baseline
        )
        assert "not available" not in product.lower()
        assert looks_like_unknown_model_error(
            str(messages_after_id(live_serve.client.list_messages(sid), baseline)),
            f"{provider}/{model}",
        ) is False
