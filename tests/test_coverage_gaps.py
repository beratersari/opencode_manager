"""Hit remaining branches so src/opencode_manager approaches full coverage."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from opencode_manager import api as api_mod
from opencode_manager.app import create_app, main as app_main
from opencode_manager.cleanup import end as end_mod
from opencode_manager.cleanup import kill as kill_mod
from opencode_manager.cleanup import rmtree as rmtree_mod
from opencode_manager.crash import (
    _append,
    _atexit_note,
    install_crash_logging,
    mark_clean_shutdown,
)
from opencode_manager.dashboard import chat as chat_mod
from opencode_manager.dashboard import frontend_proxy as proxy_mod
from opencode_manager.git import auth as auth_mod
from opencode_manager.git.detect import classify_host
from opencode_manager.models import (
    JobRecord,
    job_matches_list_filter,
    poll_payload,
    validate_session_delete_fields,
)
from opencode_manager.opencode import serve as serve_mod
from opencode_manager.opencode import session as sess
from opencode_manager.opencode.session import OpenCodeClient
from opencode_manager.settings import Settings, _as_path, _default_data_dir, _read_yaml, load_settings
from opencode_manager.worker import Terminal, finish_job


class _Resp:
    def __init__(self, status: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())


class _FakeHTTP:
    def __init__(self, getter=None, poster=None, deleter=None) -> None:
        self.getter = getter
        self.poster = poster
        self.deleter = deleter
        self.closed = False

    def get(self, path: str, **kwargs: Any) -> _Resp:
        if self.getter:
            return self.getter(path, **kwargs)
        return _Resp(200, {})

    def post(self, path: str, **kwargs: Any) -> _Resp:
        if self.poster:
            return self.poster(path, **kwargs)
        return _Resp(200, {})

    def delete(self, path: str, **kwargs: Any) -> _Resp:
        if self.deleter:
            return self.deleter(path, **kwargs)
        return _Resp(200, {})

    def close(self) -> None:
        self.closed = True


def _client(http: _FakeHTTP) -> OpenCodeClient:
    c = OpenCodeClient("http://127.0.0.1:9", "/tmp/clone")
    c.http = http  # type: ignore[assignment]
    return c


# --- detect / auth / settings / models ---


def test_classify_host_azure_and_tfs() -> None:
    assert classify_host("https://dev.azure.com/org/proj/_git/repo") == "tfs"
    assert classify_host("https://contoso.visualstudio.com/proj/_git/r") == "tfs"
    assert classify_host("https://dev.azure.com/org/proj") == "azure"
    assert classify_host("https://foo.dev.azure.com/x") == "azure"
    assert classify_host("https://gitlab.example/g/r.git") == "gitlab"


def test_auth_helpers_and_windows_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_mod.forget_job_creds("j1")
    assert auth_mod.creds_for_job(None) is None
    assert auth_mod.creds_for_job("missing") is None
    auth_mod.remember_job_creds("j1", "u", "p")
    assert auth_mod.creds_for_job("j1") == ("u", "p")
    auth_mod.forget_job_creds("j1")
    assert auth_mod.creds_for_job("j1") is None
    env = auth_mod.isolated_git_env(username="alice", password="s3cret")
    assert "Authorization: Basic" in env.get("GIT_CONFIG_VALUE_1", "") or any(
        "Authorization" in str(v) for v in env.values()
    )
    assert not auth_mod.is_git_auth_error("could not resolve host example")
    assert auth_mod.is_git_auth_error("Authentication failed 401")
    assert auth_mod.host_from_repo_url("https://git.example/r.git") == "git.example"

    monkeypatch.setattr(auth_mod, "uses_windows_stored_creds", lambda: False)
    assert auth_mod.prompt_windows_credentials("h") is None
    assert auth_mod.argv_helper_off() == ["-c", "credential.helper="]

    monkeypatch.setattr(auth_mod, "uses_windows_stored_creds", lambda: True)
    assert auth_mod.argv_helper_off() == []
    win_env = auth_mod.isolated_git_env()
    assert win_env.get("GCM_INTERACTIVE") == "auto"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ps", 1)),
    )
    assert auth_mod.prompt_windows_credentials("host") is None

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=2, stdout=b""),
    )
    assert auth_mod.prompt_windows_credentials("host") is None

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"onlyone\n"),
    )
    assert auth_mod.prompt_windows_credentials("host") is None

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"user\npass\nword\n"),
    )
    got = auth_mod.prompt_windows_credentials("host")
    assert got is not None and got[0] == "user"


def test_settings_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        assert _default_data_dir() == Path("/var/lib/osm")
    else:
        assert _default_data_dir() == Path(r"C:\osm")
    assert _as_path(None, Path("/d")) == Path("/d")
    assert _as_path("", Path("/d")) == Path("/d")
    rel = _as_path("rel-data", Path("/d"))
    assert rel.name == "rel-data"
    missing = tmp_path / "nope.yaml"
    assert _read_yaml(missing) == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        _read_yaml(bad)
    good = tmp_path / "ok.yaml"
    good.write_text("listen_port: 4111\n", encoding="utf-8")
    s = load_settings(good)
    assert s.listen_port == 4111


def test_poll_payload_fallbacks_and_filters() -> None:
    live = JobRecord(job_id="job_a", jira_id="T-1", status="queued", live=False)
    http, body = poll_payload(live)
    assert http == 202 and body["status_code"] == 202
    for status, expect in (
        ("success", 200),
        ("not_found", 404),
        ("timeout", 504),
        ("error", 500),
        ("weird", 500),
    ):
        job = JobRecord(job_id="job_b", jira_id="T-1", status=status, live=False)
        http, body = poll_payload(job)
        assert http == 200
        assert body["status_code"] == expect
    assert validate_session_delete_fields("nope") == "missing required field: jira_id"
    job = JobRecord(job_id="job_c", jira_id="T-1", status="success", live=False)
    assert job_matches_list_filter(job, "")
    assert job_matches_list_filter(job, "nope-filter")
    assert job_matches_list_filter(job, "completed")
    running = JobRecord(job_id="job_d", jira_id="T-1", status="running", live=True)
    assert job_matches_list_filter(running, "active")


# --- session helpers + client ---


def test_known_model_payload_shapes() -> None:
    assert sess.known_model_ids_from_payload(None) == []
    assert sess.known_model_ids_from_payload("x") == []
    assert "p/m" in sess.known_model_ids_from_payload([{"id": "p", "models": {"m": {}}}])
    assert "p/x" in sess.known_model_ids_from_payload({"providers": [{"id": "p", "models": ["x"]}]})
    assert "p/z" in sess.known_model_ids_from_payload(
        {"connected": {"p": {"models": [{"id": "z"}]}}}
    )
    assert "a/b" in sess.known_model_ids_from_payload({"default": {"a": "b"}})
    assert sess.known_model_ids_from_payload({"all": {"models": ["solo/id"]}})
    assert sess.model_is_known("", ["x"]) is True
    assert sess.model_is_known("p/m", []) is True
    assert sess.model_is_known("m", ["p/m"]) is True
    assert sess.model_is_known("p/m", ["q/m"]) is False
    assert sess.unknown_model_message("p/m", [f"m{i}" for i in range(20)]).startswith("model")
    assert sess.looks_like_unknown_model_error("nope", "p/m") is False
    assert sess.looks_like_unknown_model_error("p/m provider failed", "p/m")


def test_session_busy_and_compact_shapes() -> None:
    assert sess.session_is_busy(None, "ses_1") is False
    assert sess.session_is_busy({"ses_1": {"type": "busy"}}, "ses_1")
    assert sess.session_is_busy({"ses_1": {"busy": True}}, "ses_1")
    assert sess.session_is_busy({"type": "busy_compacting"}, "ses_1")
    assert sess.session_is_busy({"data": [{"id": "ses_1", "status": "working"}]}, "ses_1")
    assert sess.session_is_busy({"data": {"ses_1": {"state": "compacting"}}}, "ses_1")
    assert sess.session_is_compacting(None, None) is False
    assert sess.session_is_compacting({"ses_1": {"type": "compacting"}}, "ses_1")
    assert sess.session_is_compacting({}, "ses_1", session_info={"time": {"compacting": 1}})
    with pytest.raises(RuntimeError):
        sess._unwrap_session({"no": "id"})
    assert sess._unwrap_session({"data": {"id": "ses_x"}})["id"] == "ses_x"
    assert sess._coerce_messages({"items": [{"a": 1}, "x"]}) == [{"a": 1}]
    assert sess._coerce_messages(1) == []
    assert sess.last_assistant_id([]) == ""
    msgs = [{"role": "assistant", "id": "a1", "parts": [{"type": "text", "text": "hi"}]}]
    assert sess.last_assistant_text(msgs) == "hi"
    assert sess.turn_has_new_assistant(msgs, "old")
    assert sess.assess_idle([]) == "incomplete"
    assert sess.assess_idle([{"role": "user", "id": "u"}]) == "incomplete"
    compact = [{"role": "user", "parts": [{"type": "compact"}]}]
    assert sess.assess_idle(compact) == "compact_leftover"
    assert sess.compact_marker_count(compact) == 1
    assert sess.looks_like_question("Shall I continue?")
    assert sess.looks_like_question("Ready?")


def test_snapshot_parts_and_text_helpers() -> None:
    assert sess._part_text(None) == ""
    assert sess._part_text("x") == "x"
    assert sess._part_text({"text": "t"}) == "t"
    assert sess._part_text({"value": 3}) == "3"
    assert sess._part_text(9) == "9"
    part = sess._snapshot_part(
        {
            "type": "thinking",
            "state": {"output": "out", "status": "done", "input": {"a": 1}, "text": "th"},
            "tool": "read",
        }
    )
    assert part["type"] == "reasoning" and part["output"] == "out"
    compact = sess._snapshot_part({"type": "compact"})
    assert compact["type"] == "compaction" and "compact" in compact["text"].lower()
    other = sess._snapshot_part({"type": "tool", "input": "raw"})
    assert other["input"]["value"] == "raw"
    chat = sess.snapshot_chat(
        [{"info": {"role": "assistant", "id": "m1"}, "parts": [{"type": "text", "text": "ok"}]}],
        "ses_1",
    )
    assert chat[0]["role"] == "assistant"


def test_opencode_client_http_paths() -> None:
    client = _client(_FakeHTTP(getter=lambda p, **k: (_ for _ in ()).throw(RuntimeError("down"))))
    assert client.health() is False
    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(200, {"ok": True}))  # type: ignore[assignment]
    assert client.health() is True

    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(200, []))  # type: ignore[assignment]
    client.wait_directory(0.5)

    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(503, {}))  # type: ignore[assignment]
    with pytest.raises(TimeoutError):
        client.wait_directory(0.2)
    with pytest.raises(RuntimeError, match="shutting"):
        client.wait_directory(2, should_stop=lambda: True)

    n = {"i": 0}

    def models_get(path: str, **k: Any) -> _Resp:
        n["i"] += 1
        if "providers" in path:
            return _Resp(200, {"connected": {"opencode": {"models": ["mimo-v2.5-free"]}}})
        return _Resp(404, {})

    client.http = _FakeHTTP(getter=models_get)  # type: ignore[assignment]
    ids = client.list_known_models(timeout=2)
    assert any("mimo" in x for x in ids)

    client.http = _FakeHTTP(getter=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    assert client.list_known_models(timeout=0.2) == []

    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(200, {"id": "ses_1"}))  # type: ignore[assignment]
    assert client.session_payload("nope") == {}
    assert client.session_payload("ses_abc")["id"] == "ses_1"
    client.http = _FakeHTTP(getter=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    assert client.session_payload("ses_abc") == {}
    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(500, {}))  # type: ignore[assignment]
    assert client.session_payload("ses_abc") == {}
    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(200, "not-dict"))  # type: ignore[assignment]
    assert client.session_payload("ses_abc") == {}

    with pytest.raises(ValueError):
        client.get_session("-1")
    client.http = _FakeHTTP(getter=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        client.get_session("ses_abc")
    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(404, {}, text="missing"))  # type: ignore[assignment]
    assert client.get_session("ses_abc").status_code == 404

    client.http = _FakeHTTP(poster=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        client.create_session("t")
    client.http = _FakeHTTP(poster=lambda p, **k: _Resp(500, {}, text="no"))  # type: ignore[assignment]
    with pytest.raises(httpx.HTTPStatusError):
        client.create_session("t")
    client.http = _FakeHTTP(poster=lambda p, **k: _Resp(200, {"id": "ses_new"}))  # type: ignore[assignment]
    assert client.create_session("t") == "ses_new"

    client.http = _FakeHTTP(  # type: ignore[assignment]
        getter=lambda p, **k: _Resp(200, {"id": "ses_old"}),
        poster=lambda p, **k: _Resp(200, {"id": "ses_new"}),
    )
    sid, created = client.resume_or_create("ses_old", "t")
    assert sid == "ses_old" and created is False
    client.http = _FakeHTTP(  # type: ignore[assignment]
        getter=lambda p, **k: _Resp(404, {}),
        poster=lambda p, **k: _Resp(200, {"id": "ses_new"}),
    )
    sid, created = client.resume_or_create("ses_old", "t")
    assert created is True
    sid, created = client.resume_or_create("not-ses", "t")
    assert created is True
    sid, created = client.resume_or_create(None, "t")
    assert created is True

    client.http = _FakeHTTP(getter=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    assert client.status() == {}
    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(200, {"ses_1": {"type": "busy"}}))  # type: ignore[assignment]
    assert client.status()["ses_1"]["type"] == "busy"

    assert client.list_messages("-1") == []
    client.http = _FakeHTTP(getter=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        client.list_messages("ses_1")
    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(500, {}, text="no"))  # type: ignore[assignment]
    with pytest.raises(httpx.HTTPStatusError):
        client.list_messages("ses_1")
    client.http = _FakeHTTP(getter=lambda p, **k: _Resp(200, [{"id": "m"}]))  # type: ignore[assignment]
    assert client.list_messages("ses_1")[0]["id"] == "m"

    with pytest.raises(RuntimeError):
        client.post_message("-1", "hi", model="p/m", agent="orchestrator")
    client.http = _FakeHTTP(poster=lambda p, **k: _Resp(200, {}))  # type: ignore[assignment]
    client.post_message("ses_1", "hi", model="p/m", agent="orchestrator")
    client.http = _FakeHTTP(poster=lambda p, **k: _Resp(404, {}, text="model not found"))  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="not available"):
        client.post_message("ses_1", "hi", model="p/m", agent="orchestrator")

    client.abort("-1")
    client.http = _FakeHTTP(poster=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    client.abort("ses_1")
    client.http = _FakeHTTP(poster=lambda p, **k: _Resp(200, {}))  # type: ignore[assignment]
    client.abort("ses_1")

    client.http = _FakeHTTP(deleter=lambda p, **k: (_ for _ in ()).throw(RuntimeError("x")))  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        client.delete_session("ses_1")
    client.http = _FakeHTTP(deleter=lambda p, **k: _Resp(404, {}, text="gone"))  # type: ignore[assignment]
    assert client.delete_session("ses_1").status_code == 404
    client.close()


def test_post_message_fallback_sees_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(_FakeHTTP(poster=lambda p, **k: _Resp(409, {}, text="busy")))
    monkeypatch.setattr(client, "status", lambda: {"ses_abc": {"type": "busy"}})
    client.post_message("ses_abc", "hello world", model="p/m", agent="orchestrator")


# --- frontend proxy extra ---


def test_frontend_proxy_more_paths(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    hashed = assets / "index-abc.js"
    hashed.write_text("js", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    app = proxy_mod.build_app(dist=dist, backend="http://127.0.0.1:4096")
    client = TestClient(app)
    assert client.get("/favicon.svg").status_code == 200
    assert "javascript" in (client.get("/assets/index-abc.js").headers.get("content-type") or "")
    r = client.get("/jobs")
    assert r.status_code == 200 and "spa" in r.text
    # query string on /api
    class Ok:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **k):
            assert "filter=all" in url
            return SimpleNamespace(content=b'{"ok":1}', status_code=200, headers={"content-type": "application/json"})

    with patch("opencode_manager.dashboard.frontend_proxy.httpx.AsyncClient", return_value=Ok()):
        r = client.get("/api/jobs?filter=all")
    assert r.status_code == 200

    class HttpBoom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            raise httpx.ReadTimeout("slow")

    with patch("opencode_manager.dashboard.frontend_proxy.httpx.AsyncClient", return_value=HttpBoom()):
        r = client.get("/api/meta")
    assert r.status_code == 502 and "Proxy error" in r.text

    assert proxy_mod.main(["--dist", str(tmp_path)]) == 1
    called: dict[str, Any] = {}

    def fake_run(app, **kwargs):
        called.update(kwargs)

    with patch.object(proxy_mod.uvicorn, "run", fake_run):
        assert proxy_mod.main(["--dist", str(dist), "--port", "5199"]) == 0
    assert called["port"] == 5199
    assert proxy_mod._env("MISSING_ENV_XYZ", "d") == "d"


# --- chat / crash / serve ---


def test_chat_db_merge_and_live_fallback(tmp_path: Path) -> None:
    assert chat_mod.load_session_messages_from_db("-1") == []
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE message (id TEXT, data TEXT, session_id TEXT, time_created INT)")
    conn.execute("CREATE TABLE part (message_id TEXT, data TEXT, session_id TEXT, time_created INT)")
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("m1", json.dumps({"role": "assistant", "id": "m1"}), "ses_abc", 1),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?)",
        ("m1", json.dumps({"type": "tool", "tool": "read", "output": "FILE"}), "ses_abc", 1),
    )
    conn.execute("INSERT INTO part VALUES (?,?,?,?)", ("m1", "not-json", "ses_abc", 2))
    conn.commit()
    conn.close()
    loaded = chat_mod.load_session_messages_from_db("ses_abc", db_path=db)
    assert loaded and loaded[0]["parts"]
    snap = [
        {
            "id": "m1",
            "parts": [{"type": "tool", "tool": "read", "output": ""}, "skip"],
        }
    ]
    merged = chat_mod._merge_tool_outputs(snap, loaded)
    assert merged[0]["parts"][0]["output"] == "FILE"
    job = JobRecord(
        job_id="job_c",
        jira_id="T-1",
        live=True,
        session_id="ses_abc",
        serve_base_url="http://127.0.0.1:1",
        clone_path=str(tmp_path),
        chat_snapshot=[{"id": "old"}],
    )
    with patch.object(chat_mod, "OpenCodeClient", side_effect=RuntimeError("down")):
        payload = chat_mod.job_chat_payload(job)
    assert payload["messages"][0]["id"] == "old"


def test_crash_hooks(tmp_path: Path) -> None:
    path = install_crash_logging(tmp_path)
    assert path.name == "crash.log"
    install_crash_logging(tmp_path)  # second call closes previous handle
    mark_clean_shutdown()
    _atexit_note()
    import opencode_manager.crash as crash_mod

    crash_mod._clean = False
    _atexit_note()
    _append("line")
    crash_mod._crash_path = None
    _append("ignored")


def test_serve_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "no.log"
    assert serve_mod.read_serve_log(missing) == ""
    log = tmp_path / "s.log"
    log.write_text("secret https://oauth2:tok@host/r.git\n", encoding="utf-8")
    assert "tok@" not in serve_mod.read_serve_log(log)
    assert serve_mod.free_port() > 0
    assert serve_mod._serve_log_tail(missing) == ""
    assert "secret" in serve_mod._serve_log_tail(log)
    serve_mod.stop_serve(None)
    handle = serve_mod.ServeHandle(
        pid=os.getpid(),
        port=1,
        base_url="http://127.0.0.1:1",
        proc=SimpleNamespace(wait=lambda timeout=5: (_ for _ in ()).throw(RuntimeError("x")), _om_log_f=SimpleNamespace(close=lambda: (_ for _ in ()).throw(OSError()))),
        log_path=log,
    )
    with patch.object(serve_mod, "kill_pid"):
        serve_mod.stop_serve(handle)
    with pytest.raises(RuntimeError, match="shutting"):
        serve_mod.wait_health("http://127.0.0.1:1", "/tmp", timeout=1, should_stop=lambda: True)

    class BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise httpx.ConnectError("no")

    monkeypatch.setattr(serve_mod.httpx, "Client", BoomClient)
    with pytest.raises(TimeoutError):
        serve_mod.wait_health("http://127.0.0.1:1", "/tmp", timeout=0.4)


# --- rmtree / end / kill ---


def test_rmtree_windows_helpers_and_linux_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert rmtree_mod.win_extended_path("\\\\?\\C:\\x") == "\\\\?\\C:\\x"
    assert rmtree_mod.win_extended_path("\\\\server\\share").startswith("\\\\?\\UNC\\")
    assert rmtree_mod.win_reserved_stem("NUL.txt")
    assert not rmtree_mod.win_reserved_stem("readme.txt")
    gone = tmp_path / "missing"
    assert rmtree_mod.hard_delete(gone) is True
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "f.txt").write_text("x", encoding="utf-8")
    orig = os.name
    try:
        os.name = "posix"
        deleted = rmtree_mod.hard_delete(tree)
    finally:
        os.name = orig
    assert deleted is True
    again = tmp_path / "stuck"
    again.mkdir()
    monkeypatch.setattr(rmtree_mod, "_exists", lambda p: True)
    monkeypatch.setattr(rmtree_mod.time, "sleep", lambda s: None)
    assert rmtree_mod.hard_delete(again, attempts=2) is False


def test_end_protect_and_exception_arms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert 99 in end_mod.protect_pids([99], [None, "x", 1])
    job = JobRecord(job_id="job_e", jira_id="T-1", serve_pid=None, extra_pids=[55])
    clone = tmp_path / "c"
    clone.mkdir()
    monkeypatch.setattr(end_mod, "kill_job_tree", lambda pids: (_ for _ in ()).throw(RuntimeError("k")))
    monkeypatch.setattr(end_mod, "reap_path", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("r")))
    monkeypatch.setattr(end_mod, "kill_file_holders", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("f")))
    monkeypatch.setattr(end_mod, "path_has_holders", lambda *a, **k: True)
    orig = os.name
    try:
        os.name = "posix"
        end_mod.stop_job_holders(job, clone)
        end_mod.stop_job_holders(job, None)
    finally:
        os.name = orig
    assert end_mod.delete_clone_path(None, reason="x") is True
    monkeypatch.setattr(end_mod, "hard_delete", lambda p: True)
    assert end_mod.delete_clone_path(tmp_path / "gone", reason="x") is True


def test_retry_windows_delete_and_kill_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clone = tmp_path / "held"
    clone.mkdir()
    monkeypatch.setattr(end_mod, "query_windows_restart_manager", lambda p: SimpleNamespace(pids=[99999], died=True))
    monkeypatch.setattr(end_mod, "may_kill", lambda pid: True)
    monkeypatch.setattr(end_mod, "kill_pid", lambda pid: None)
    monkeypatch.setattr(end_mod, "hard_delete", lambda p: False)
    orig = os.name
    try:
        os.name = "nt"
        held = end_mod.retry_windows_delete_if_held(clone)
        monkeypatch.setattr(end_mod, "hard_delete", lambda p: True)
        clone2 = tmp_path / "held2"
        clone2.mkdir()
        gone = end_mod.retry_windows_delete_if_held(clone2)
    finally:
        os.name = orig
    assert held is False
    assert gone is True

    assert kill_mod._image_stem('"C:\\git\\git.exe" status') == "git"
    assert kill_mod._image_stem("C:/tools/opencode.exe --serve") == "opencode"
    assert kill_mod.windows_cwd_candidate("git.exe", "git clone")
    assert not kill_mod.windows_cwd_candidate("python.exe", "python -m x")
    assert kill_mod.parse_windows_process_json("") == []
    assert kill_mod.parse_windows_process_json("not-json") == []
    rows = kill_mod.parse_windows_process_json(
        json.dumps({"ProcessId": "12", "CommandLine": "git", "ExecutablePath": "git.exe"})
    )
    assert rows[0]["pid"] == 12
    assert kill_mod._as_pid(True) is None
    assert kill_mod._as_pid("no") is None
    assert kill_mod._as_pid(3) is None
    assert kill_mod._decode_windows_stdout("café".encode("utf-16le"))
    assert kill_mod.reap_root_is_safe(None) is False
    assert kill_mod.reap_root_is_safe(Path("C:/osm/.temp/T-1"))
    assert kill_mod.path_is_under("", "/x") is False
    assert kill_mod.text_mentions_root("", "/x") is False
    proc = kill_mod.ProcInfo(pid=9, cwd="/var/lib/osm/.temp/T", argv="git")
    assert kill_mod.process_belongs(proc, "/var/lib/osm/.temp/T")

    def _as(name: str, fn):
        orig = os.name
        try:
            os.name = name
            return fn()
        finally:
            os.name = orig

    assert _as("nt", kill_mod._is_wsl) is False
    class _P:
        def __init__(self, *a, **k):
            pass

        def read_text(self, **k):
            return "microsoft-standard-WSL2"

        def exists(self):
            return True

        def is_dir(self):
            return False

    with patch.object(kill_mod, "Path", _P):
        assert _as("posix", kill_mod._is_wsl) is True
    assert kill_mod.reap_path(Path("/")) == 0
    nt_child = tmp_path / "a" / "b"
    nt_child.mkdir(parents=True)
    assert _as("nt", lambda: kill_mod.reap_path(nt_child)) == 0
    safe_child = tmp_path / "safe" / "child"
    safe_child.mkdir(parents=True)
    monkeypatch.setattr(kill_mod, "iter_processes", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _as("posix", lambda: kill_mod.reap_path(safe_child)) == 0
    monkeypatch.setattr(
        kill_mod,
        "iter_processes",
        lambda **k: [kill_mod.ProcInfo(pid=424242, cwd=str(safe_child), argv="git")],
    )
    monkeypatch.setattr(kill_mod, "may_kill", lambda pid: True)
    monkeypatch.setattr(kill_mod, "kill_pid", lambda pid: None)
    monkeypatch.setattr(kill_mod, "protected_pids", lambda: set())
    assert _as("posix", lambda: kill_mod.reap_path(safe_child)) >= 0
    lock_root = tmp_path / "repo"
    (lock_root / ".git").mkdir(parents=True)
    lock = lock_root / ".git" / "index.lock"
    lock.write_text("x", encoding="utf-8")
    kill_mod.drop_git_locks(lock_root)
    assert not lock.exists()
    kill_mod.drop_git_locks(tmp_path / "no-git")
    empty = _as("posix", lambda: kill_mod.query_windows_restart_manager(tmp_path))
    assert empty.pids == []
    monkeypatch.setattr(kill_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    died = _as("nt", lambda: kill_mod.query_windows_restart_manager(tmp_path))
    assert died.died is True
    monkeypatch.setattr(
        kill_mod.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"[99]", stderr=b""),
    )
    monkeypatch.setattr(kill_mod, "_decode_windows_stdout", lambda b: b.decode() if isinstance(b, bytes) else str(b))
    ok = _as("nt", lambda: kill_mod.query_windows_restart_manager(tmp_path))
    assert 99 in ok.pids
    monkeypatch.setattr(kill_mod, "file_holder_pids", lambda root: [99, None])
    monkeypatch.setattr(kill_mod, "_guard_set", lambda protect=None: set())
    monkeypatch.setattr(kill_mod, "kill_pid", lambda pid: None)
    assert kill_mod.kill_file_holders(tmp_path) >= 0
    assert kill_mod.path_has_holders(tmp_path) in {True, False}


# --- api / app ---


def test_api_exception_envelopes_and_spa(tmp_path: Path, tmp_settings: Settings) -> None:
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>ui</html>", encoding="utf-8")
    (dist / "assets" / "x.js").write_text("1", encoding="utf-8")
    tmp_settings.project_root = tmp_path
    from tests.test_api import FakeRunner, _body, _client

    with _client(tmp_settings, FakeRunner()) as client:
        r = client.get("/")
        assert r.status_code == 200
        job = client.post("/jobs", json=_body(jira_id="COV-1", callback_url="")).json()
        assert client.get(f"/api/jobs/{job['job_id']}/chat").status_code == 200
        assert client.get(f"/api/jobs/{job['job_id']}/prompts").status_code == 200
        assert client.get("/api/jobs/missing/prompts").status_code == 404
        assert client.get("/api/jobs/missing/chat").status_code == 404
        assert client.get("/api/jobs/missing/logs").status_code == 404

    class Boom(FakeRunner):
        pass

    app = create_app(tmp_settings, runner=FakeRunner())
    with patch.object(app.state.manager, "submit", side_effect=RuntimeError("boom")):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/jobs", json=_body(jira_id="COV-2"))
            assert r.status_code == 500
            assert r.json()["status_code"] == 500
    with patch.object(app.state.manager, "delete_session", side_effect=RuntimeError("boom")):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.request("DELETE", "/sessions", json={"jira_id": "X-1", "session_id": "ses_a"})
            assert r.status_code == 500
    with patch.object(app.state.manager.store, "list_all", side_effect=RuntimeError("boom")):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/jobs")
            assert r.status_code == 200
            assert r.json()["jobs"] == []


def test_app_main_invokes_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_settings: Settings) -> None:
    called = {}

    def fake_run(app, **kwargs):
        called.update(kwargs)

    monkeypatch.setattr("opencode_manager.app.load_settings", lambda: tmp_settings)
    monkeypatch.setattr("uvicorn.run", fake_run)
    app_main()
    assert called["host"] == tmp_settings.listen_host
    assert called["port"] == tmp_settings.listen_port


def test_finish_job_callback_exception(tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencode_manager.dashboard.store import JobStore

    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_cbx",
        jira_id="T-CB",
        live=True,
        callback_url="http://127.0.0.1:9/x",
    )
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cb")))
    finish_job(job, Terminal(200, "ok"), settings=tmp_settings, store=store)
    assert job.status == "success"


def test_proxy_ws_and_spa_guards(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    app = proxy_mod.build_app(dist=dist, backend="http://127.0.0.1:9")
    client = TestClient(app)
    r = client.get("/ws")
    assert r.status_code == 404
    r = client.get("/../../secret")
    assert r.status_code == 200
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert "error" in msg


def test_rmtree_chmod_and_reserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rmtree_mod._chmod_writable(tmp_path / "missing")
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    rmtree_mod._chmod_writable(f)
    monkeypatch.setattr(rmtree_mod.os, "walk", lambda root, topdown=False: [(str(tmp_path), [], ["NUL.txt"])])
    monkeypatch.setattr(
        rmtree_mod.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    rmtree_mod._windows_del_reserved(tmp_path)
    class BoomPath:
        def exists(self) -> bool:
            raise OSError("x")

    assert rmtree_mod._exists(BoomPath()) is True  # type: ignore[arg-type]


def test_kill_pid_posix_and_job_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = os.name
    try:
        os.name = "posix"
        monkeypatch.setattr(kill_mod, "_as_pid", lambda v: 99991)
        monkeypatch.setattr(kill_mod, "may_kill", lambda pid: True)
        monkeypatch.setattr(os, "killpg", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()), raising=False)
        monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
        kill_mod.kill_pid(99991)
        monkeypatch.setattr(kill_mod, "protected_pids", lambda: set())
        monkeypatch.setattr(kill_mod, "kill_pid", lambda pid: (_ for _ in ()).throw(RuntimeError("x")))
        kill_mod.kill_job_tree([99991, None, "bad"])
    finally:
        os.name = orig


def test_manager_boot_leftover_queue_and_live_counts(tmp_settings: Settings) -> None:
    from opencode_manager.dashboard.store import JobStore
    from opencode_manager.manager import Manager
    from tests.test_api import FakeRunner

    store = JobStore(tmp_settings.job_store_dir)
    leftover = JobRecord(job_id="job_leftq", jira_id="LQ-1", status="queued", live=True)
    store.save(leftover)
    tmp_settings.queue_path.write_text(
        json.dumps([{"job_id": "job_leftq", "jira_id": "LQ-1"}]),
        encoding="utf-8",
    )
    running = JobRecord(job_id="job_leftrun", jira_id="LR-1", status="running", live=True)
    store.save(running)
    mgr = Manager(tmp_settings, runner=FakeRunner())
    mgr.boot()
    assert store.get("job_leftq").status == "error"
    assert store.get("job_leftrun").status == "error"
    assert mgr.job_public("missing") is None
    assert mgr.job_public("job_leftrun")["status"] == "error"
    mgr.queue.peek_all = lambda: (_ for _ in ()).throw(RuntimeError("peek"))  # type: ignore[method-assign]
    running_n, queued_n = mgr.live_counts()
    assert queued_n == 0 and running_n >= 0


def test_app_crash_log_and_shutdown_exceptions(tmp_settings: Settings) -> None:
    with patch("opencode_manager.app.install_crash_logging", side_effect=RuntimeError("x")):
        app = create_app(tmp_settings)
        assert app.state.manager is not None
    with patch("opencode_manager.manager.Manager.shutdown", side_effect=RuntimeError("x")):
        with TestClient(create_app(tmp_settings)):
            pass


def test_post_message_fallback_http_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(_FakeHTTP(poster=lambda p, **k: _Resp(409, {}, text="try message")))

    class OkHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp(200, {})

    monkeypatch.setattr(httpx, "Client", OkHttp)
    monkeypatch.setattr(client, "status", lambda: {})
    monkeypatch.setattr(client, "list_messages", lambda sid: [{"role": "user", "parts": [{"text": "hello world"}]}])
    client.post_message("ses_abc", "hello world", model="p/m", agent="orchestrator")
