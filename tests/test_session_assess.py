import json
import time

import pytest

from opencode_manager.opencode.retry import _log_every
from opencode_manager.opencode.session import (
    assess_idle,
    known_model_ids_from_payload,
    last_assistant_id,
    last_assistant_text_since,
    looks_like_question,
    model_is_known,
    session_is_busy,
    session_is_compacting,
    snapshot_chat,
    turn_has_new_assistant,
    unknown_model_message,
)


def test_repeat_inner_logs_every_50() -> None:
    assert [n for n in range(0, 151) if _log_every(n)] == [1, 50, 100, 150]


def test_busy_status_shapes() -> None:
    assert session_is_busy({"ses_1": {"type": "busy"}}, "ses_1")
    assert session_is_busy({"ses_1": {"type": "busy_compacting"}}, "ses_1")
    assert not session_is_busy({"ses_1": {"type": "idle"}}, "ses_1")


def test_opencode_busy_is_not_compacting_without_time_field() -> None:
    assert not session_is_compacting({"ses_1": {"type": "busy"}}, "ses_1")
    assert session_is_compacting({"ses_1": {"type": "compacting"}}, "ses_1")
    assert session_is_compacting(
        {"ses_1": {"type": "busy"}},
        "ses_1",
        session_info={"id": "ses_1", "time": {"created": 1, "updated": 2, "compacting": 99}},
    )


def test_question_detect() -> None:
    assert looks_like_question("Shall I create the file?")
    assert not looks_like_question("Created the file.")


def test_assess_stop_success() -> None:
    messages = [{"info": {"role": "assistant", "finish": "stop"}, "parts": [{"type": "text", "text": "done"}]}]
    assert assess_idle(messages) == "success"


def test_known_model_ids_from_config_providers() -> None:
    payload = {
        "providers": [
            {
                "id": "opencode",
                "models": {
                    "ling-3.0-flash-fin-free": {},
                    "mimo-v2.5-free": {},
                },
            }
        ]
    }
    ids = known_model_ids_from_payload(payload)
    assert "opencode/ling-3.0-flash-fin-free" in ids
    assert "opencode/mimo-v2.5-free" in ids
    assert model_is_known("opencode/ling-3.0-flash-fin-free", ids)
    assert not model_is_known("opencode/hy3-free", ids)
    assert model_is_known("opencode/hy3-free", []) is True
    msg = unknown_model_message("opencode/hy3-free", ids)
    assert "hy3-free" in msg
    assert "ling-3.0-flash-fin-free" in msg


def test_known_model_ids_from_connected_providers() -> None:
    payload = {
        "connected": [
            {"id": "opencode", "models": {"mimo-v2.5-free": {}}},
        ]
    }
    ids = known_model_ids_from_payload(payload)
    assert "opencode/mimo-v2.5-free" in ids
    assert not model_is_known("opencode/hy3-free", ids)


def test_looks_like_unknown_model_error() -> None:
    from opencode_manager.opencode.session import looks_like_unknown_model_error

    assert looks_like_unknown_model_error(
        '{"name":"ProviderModelNotFoundError","data":{"modelID":"hy3-free"}}'
    )
    assert looks_like_unknown_model_error("Model not found: opencode/hy3-free", "opencode/hy3-free")
    assert not looks_like_unknown_model_error("session busy")


def test_prompt_async_unknown_model_fails_job_immediately() -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from opencode_manager.opencode.session import OpenCodeClient

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # noqa: ANN002
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            raw = json.dumps(
                {
                    "name": "ProviderModelNotFoundError",
                    "data": {"message": "Model not found: opencode/hy3-free"},
                }
            ).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", "/tmp/clone")
    started = time.time()
    try:
        with pytest.raises(RuntimeError, match="not available"):
            client.post_message(
                "ses_x",
                "do it",
                model="opencode/hy3-free",
                agent="build",
            )
        assert time.time() - started < 5.0
    finally:
        client.close()
        server.shutdown()


def test_assess_incomplete_tool_calls() -> None:
    messages = [{"info": {"role": "assistant", "finish": "tool-calls"}, "parts": []}]
    assert assess_idle(messages) == "incomplete"


def test_assess_length_finish_is_incomplete() -> None:
    """OpenCode `finish=length` is a max-token cutoff, not a clean stop."""
    messages = [
        {
            "info": {"role": "assistant", "id": "a_stop", "finish": "stop"},
            "parts": [{"type": "text", "text": "I need to fix several issues before tests."}],
        },
        {
            "info": {"role": "assistant", "id": "a_cut", "finish": "length"},
            "parts": [{"type": "reasoning", "text": "OK, I'm going to"}],
        },
    ]
    assert assess_idle(messages) == "incomplete"


def test_assess_unknown_unfinished_finish_is_incomplete() -> None:
    messages = [{"info": {"role": "assistant", "finish": "max_tokens"}, "parts": []}]
    assert assess_idle(messages) == "incomplete"


def test_resume_old_stop_is_not_a_new_turn() -> None:
    prior = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "3+4"}]},
        {
            "id": "a1",
            "info": {"role": "assistant", "id": "a1", "finish": "stop"},
            "parts": [{"type": "text", "text": "done 3+4"}],
        },
    ]
    assert assess_idle(prior) == "success"
    assert assess_idle(prior, baseline_assistant_id="a1") == "incomplete"
    assert last_assistant_id(prior) == "a1"
    assert last_assistant_text_since(prior, "a1") == ""
    assert not turn_has_new_assistant(prior, "a1")
    continued = prior + [
        {"id": "u2", "info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "now 3+5"}]},
    ]
    assert not turn_has_new_assistant(continued, "a1")
    after = continued + [
        {
            "id": "a2",
            "info": {"role": "assistant", "id": "a2", "finish": "stop"},
            "parts": [{"type": "text", "text": "done 3+5"}],
        },
    ]
    assert turn_has_new_assistant(after, "a1")
    assert assess_idle(continued, baseline_assistant_id="a1") == "incomplete"
    assert assess_idle(after, baseline_assistant_id="a1") == "success"
    assert last_assistant_text_since(after, "a1") == "done 3+5"
    assert last_assistant_text_since(after, "") == "done 3+5"


def test_snapshot_chat_pulls_tool_state_and_reasoning() -> None:
    raw = [
        {
            "info": {"id": "u1", "role": "user"},
            "parts": [{"id": "p0", "type": "text", "text": "do it"}],
        },
        {
            "info": {"id": "a1", "role": "assistant", "finish": "tool-calls"},
            "parts": [
                {"id": "p1", "type": "step-start"},
                {"id": "p2", "type": "reasoning", "text": "I will run bash"},
                {
                    "id": "p3",
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "title": "bash",
                        "input": {"command": "echo 6"},
                        "output": "6\n",
                    },
                },
                {"id": "p4", "type": "text", "text": "done"},
            ],
        },
    ]
    snap = snapshot_chat(raw, "ses_1")
    assert [m["role"] for m in snap] == ["user", "assistant"]
    tools = [p for p in snap[1]["parts"] if p["type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool"] == "bash"
    assert tools[0]["status"] == "completed"
    assert tools[0]["output"] == "6\n"
    assert tools[0]["input"] == {"command": "echo 6"}
    think = [p for p in snap[1]["parts"] if p["type"] == "reasoning"]
    assert think[0]["text"] == "I will run bash"


def test_wait_directory_retries_until_session_list_returns() -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from opencode_manager.opencode.session import OpenCodeClient

    hits = {"n": 0}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # noqa: ANN002
            return

        def do_GET(self) -> None:  # noqa: N802
            hits["n"] += 1
            if hits["n"] == 1:
                self.close_connection = True
                return
            raw = json.dumps([]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", "/tmp/clone")
    try:
        client.wait_directory(timeout=3.0)
        assert hits["n"] >= 2
    finally:
        client.close()
        server.shutdown()


def test_wait_directory_times_out_when_session_list_never_answers() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from opencode_manager.opencode.session import OpenCodeClient

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # noqa: ANN002
            return

        def do_GET(self) -> None:  # noqa: N802
            time.sleep(2.0)
            self.send_response(503)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", "/tmp/clone")
    try:
        with pytest.raises(TimeoutError, match="directory instance not ready"):
            client.wait_directory(timeout=0.6)
    finally:
        client.close()
        server.shutdown()


def test_live_chat_does_not_call_opencode_with_dash_one_session(monkeypatch) -> None:
    from opencode_manager.dashboard.chat import job_chat_payload
    from opencode_manager.models import JobRecord

    called: list[str] = []

    class Boom:
        def __init__(self, *_a, **_k) -> None:
            called.append("init")

        def list_messages(self, sid: str) -> list:
            called.append(sid)
            return []

        def close(self) -> None:
            return None

    monkeypatch.setattr("opencode_manager.dashboard.chat.OpenCodeClient", Boom)
    job = JobRecord(
        job_id="job_50730ae7ef434dca",
        jira_id="ARI-259",
        live=True,
        serve_base_url="http://127.0.0.1:4096",
        clone_path="/tmp/ARI-259",
        session_id="-1",
    )
    assert job.session_id == ""
    payload = job_chat_payload(job)
    assert payload["session_ids"] == []
    assert payload["sessions"] == []
    assert payload["messages"] == []
    assert called == []

    # Leftover assignment after construct (old store / worker) still must not hit OpenCode.
    job.session_id = "-1"
    leftover = job_chat_payload(job)
    assert leftover["session_ids"] == []
    assert leftover["sessions"] == []
    assert called == []


def test_client_skips_http_for_unusable_session() -> None:
    from opencode_manager.opencode.session import OpenCodeClient

    client = OpenCodeClient("http://127.0.0.1:9", "/tmp")
    hits: list[str] = []

    def _get(*_a, **_k):
        hits.append("get")
        raise AssertionError("OpenCode HTTP must not run for session_id=-1")

    def _post(*_a, **_k):
        hits.append("post")
        raise AssertionError("OpenCode HTTP must not run for session_id=-1")

    client.http.get = _get  # type: ignore[method-assign]
    client.http.post = _post  # type: ignore[method-assign]
    assert client.list_messages("-1") == []
    assert client.session_payload("-1") == {}
    client.abort("-1")
    assert hits == []
    client.close()


def test_job_chat_refills_tool_output_from_opencode_db(tmp_path, monkeypatch) -> None:
    import sqlite3

    from opencode_manager.dashboard.chat import job_chat_payload
    from opencode_manager.models import JobRecord

    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        """
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("a1", "ses_db", 1, 1, json.dumps({"role": "assistant", "id": "a1"})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        (
            "p1",
            "a1",
            "ses_db",
            1,
            1,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "output": "6\n", "input": {"command": "echo 6"}},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "opencode_manager.dashboard.chat._opencode_db_candidates",
        lambda: [db],
    )
    job = JobRecord(
        job_id="job_c",
        jira_id="T-1",
        session_id="ses_db",
        live=False,
        chat_snapshot=[
            {
                "id": "a1",
                "role": "assistant",
                "parts": [{"type": "tool", "tool": "bash", "text": "", "status": "", "output": ""}],
            }
        ],
    )
    payload = job_chat_payload(job)
    tool = payload["messages"][0]["parts"][0]
    assert tool["output"] == "6\n"
    assert tool["status"] == "completed"
    assert tool["input"]["command"] == "echo 6"
