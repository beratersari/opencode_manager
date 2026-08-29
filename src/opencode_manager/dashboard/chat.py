"""Chat payload for the dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from opencode_manager.models import JobRecord
from opencode_manager.opencode.session import OpenCodeClient, snapshot_chat


def _tools_missing_output(messages: List[Dict[str, Any]]) -> bool:
    for message in messages:
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "").lower()
            if (kind == "tool" or part.get("tool")) and not str(part.get("output") or "").strip():
                return True
    return False


def _opencode_db_candidates() -> List[Path]:
    home = Path.home()
    xdg = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local/share"))
    local = os.environ.get("LOCALAPPDATA") or ""
    appdata = os.environ.get("APPDATA") or ""
    return [
        xdg / "opencode" / "opencode.db",
        home / ".local/share/opencode/opencode.db",
        home / "Library/Application Support/opencode/opencode.db",
        Path(local) / "opencode" / "opencode.db" if local else None,
        Path(appdata) / "opencode" / "opencode.db" if appdata else None,
    ]


def load_session_messages_from_db(
    session_id: str, *, db_path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Rebuild OpenCode {info, parts} rows from the global opencode.db."""
    if not session_id:
        return []
    paths = [db_path] if db_path is not None else _opencode_db_candidates()
    for path in paths:
        if path is None or not path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            messages_raw = conn.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
            parts_raw = conn.execute(
                "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for message_id, blob in parts_raw:
            try:
                data = json.loads(blob)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                grouped.setdefault(str(message_id), []).append(data)
        out: List[Dict[str, Any]] = []
        for mid, blob in messages_raw:
            try:
                info = json.loads(blob)
            except (TypeError, ValueError):
                info = {}
            if not isinstance(info, dict):
                info = {}
            info.setdefault("id", mid)
            out.append({"id": mid, "info": info, "parts": grouped.get(str(mid), [])})
        return out
    return []


def job_chat_payload(job: JobRecord) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = list(job.chat_snapshot or [])
    if job.live and job.serve_base_url and job.session_id and job.clone_path:
        try:
            client = OpenCodeClient(job.serve_base_url, job.clone_path)
            try:
                messages = snapshot_chat(client.list_messages(job.session_id), job.session_id)
            finally:
                client.close()
        except Exception:
            pass
    if job.session_id and (not messages or _tools_missing_output(messages)):
        raw = load_session_messages_from_db(job.session_id)
        if raw:
            messages = snapshot_chat(raw, job.session_id)
    return {
        "job_id": job.job_id,
        "session_ids": [job.session_id] if job.session_id else [],
        "sessions": [
            {
                "session_id": job.session_id,
                "directory": job.clone_path,
                "message_count": len(messages),
            }
        ]
        if job.session_id
        else [],
        "messages": messages,
    }
