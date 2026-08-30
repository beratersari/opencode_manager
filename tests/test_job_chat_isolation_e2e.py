"""Real HTTP + real store + real sqlite: chat is this job's snapshot only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord
from opencode_manager.settings import Settings

_SESSION = "ses_e2e_shared_chat_isolation"
_LATER_USER = "msg_e2e_later_other_job"


def _seed_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE message (
            id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
        );
        CREATE TABLE part (
            id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
        );
        """
    )
    rows = [
        ("msg_e2e_u1", 1, {"role": "user", "id": "msg_e2e_u1"}),
        ("msg_e2e_a1", 2, {"role": "assistant", "id": "msg_e2e_a1"}),
        (_LATER_USER, 3, {"role": "user", "id": _LATER_USER}),
    ]
    for mid, when, info in rows:
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (mid, _SESSION, when, when, json.dumps(info)),
        )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        (
            "p_u1",
            "msg_e2e_u1",
            _SESSION,
            1,
            1,
            json.dumps({"type": "text", "text": "original prompt"}),
        ),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        (
            "p_a1",
            "msg_e2e_a1",
            _SESSION,
            2,
            2,
            json.dumps(
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "output": "6\n",
                        "input": {"command": "echo 6"},
                    },
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        (
            "p_later",
            _LATER_USER,
            _SESSION,
            3,
            3,
            json.dumps({"type": "text", "text": "hang resume from a later job"}),
        ),
    )
    conn.commit()
    conn.close()


def _job(**kwargs) -> JobRecord:
    data = dict(
        repo_url="https://gitlab.example/g/r.git",
        source_branch="develop",
        prompt="do work",
        model="opencode/hy3-free",
        agent_mode="build",
        session_id=_SESSION,
        live=False,
        clone_path="",
        serve_base_url="",
    )
    data.update(kwargs)
    return JobRecord(**data)


def test_chat_api_does_not_mix_later_session_turns(
    tmp_settings: Settings, monkeypatch
) -> None:
    xdg = tmp_settings.project_root / "xdg"
    db = xdg / "opencode" / "opencode.db"
    _seed_db(db)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    store = JobStore(tmp_settings.job_store_dir)
    empty = _job(
        job_id="job_e2e_empty404",
        jira_id="ISO-404",
        status="not_found",
        original_posted=False,
        chat_snapshot=[],
    )
    partial = _job(
        job_id="job_e2e_partial",
        jira_id="ISO-OK",
        status="success",
        original_posted=True,
        chat_snapshot=[
            {
                "id": "msg_e2e_u1",
                "role": "user",
                "parts": [{"type": "text", "text": "original prompt"}],
            },
            {
                "id": "msg_e2e_a1",
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "text": "",
                        "status": "",
                        "output": "",
                    }
                ],
            },
        ],
    )
    store.save(empty)
    store.save(partial)

    with TestClient(create_app(tmp_settings)) as client:
        empty_chat = client.get("/api/jobs/job_e2e_empty404/chat")
        assert empty_chat.status_code == 200
        empty_ids = [m["id"] for m in empty_chat.json()["messages"]]
        assert empty_ids == []
        assert _LATER_USER not in empty_ids

        partial_chat = client.get("/api/jobs/job_e2e_partial/chat")
        assert partial_chat.status_code == 200
        body = partial_chat.json()
        ids = [m["id"] for m in body["messages"]]
        assert ids == ["msg_e2e_u1", "msg_e2e_a1"]
        assert _LATER_USER not in ids
        tool = body["messages"][1]["parts"][0]
        assert tool["output"] == "6\n"
        assert tool["status"] == "completed"
        assert tool["input"]["command"] == "echo 6"

        logs = client.get("/api/jobs/job_e2e_empty404/logs")
        assert logs.status_code == 200
        assert logs.json()["job_id"] == "job_e2e_empty404"
