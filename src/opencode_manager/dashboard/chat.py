"""Chat payload for the dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from opencode_manager.models import JobRecord, usable_session_id
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
    if not usable_session_id(session_id):
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


def _merge_tool_outputs(
    snapshot: List[Dict[str, Any]], db_messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Fill empty tool outputs on this job's messages only. Never append later turns."""
    by_id = {str(item.get("id") or ""): item for item in db_messages if item.get("id")}
    merged: List[Dict[str, Any]] = []
    for message in snapshot:
        donor = by_id.get(str(message.get("id") or ""))
        if not donor:
            merged.append(message)
            continue
        parts_out: List[Dict[str, Any]] = []
        snap_parts = list(message.get("parts") or [])
        db_parts = [p for p in (donor.get("parts") or []) if isinstance(p, dict)]
        for index, part in enumerate(snap_parts):
            if not isinstance(part, dict):
                parts_out.append(part)
                continue
            kind = str(part.get("type") or "").lower()
            is_tool = kind == "tool" or bool(part.get("tool"))
            if is_tool and not str(part.get("output") or "").strip():
                match = db_parts[index] if index < len(db_parts) else None
                tool = part.get("tool")
                if tool:
                    named = [
                        p
                        for p in db_parts
                        if p.get("tool") == tool and str(p.get("output") or "").strip()
                    ]
                    if named:
                        match = named[0]
                if match and str(match.get("output") or "").strip():
                    filled = dict(part)
                    filled["output"] = match["output"]
                    if match.get("status"):
                        filled["status"] = match["status"]
                    if match.get("input") is not None:
                        filled["input"] = match["input"]
                    parts_out.append(filled)
                    continue
            parts_out.append(part)
        item = dict(message)
        item["parts"] = parts_out
        merged.append(item)
    return merged


def job_chat_payload(job: JobRecord) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = list(job.chat_snapshot or [])
    sid = usable_session_id(job.session_id)
    if job.live and job.serve_base_url and sid and job.clone_path:
        try:
            client = OpenCodeClient(job.serve_base_url, job.clone_path)
            try:
                messages = snapshot_chat(client.list_messages(sid), sid)
            finally:
                client.close()
        except Exception:
            messages = list(job.chat_snapshot or [])
    # Finished jobs use this job's snapshot. The global opencode.db is keyed
    # by session_id; later jobs reuse the same ses_* / clone path, so replacing
    # the transcript from the db mixes another run into this job_id.
    if sid and messages and _tools_missing_output(messages):
        raw = load_session_messages_from_db(sid)
        if raw:
            messages = _merge_tool_outputs(messages, snapshot_chat(raw, sid))
    return {
        "job_id": job.job_id,
        "session_ids": [sid] if sid else [],
        "sessions": [
            {
                "session_id": sid,
                "directory": job.clone_path,
                "message_count": len(messages),
            }
        ]
        if sid
        else [],
        "messages": messages,
    }
