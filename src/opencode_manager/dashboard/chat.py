"""Chat payload for the dashboard."""

from __future__ import annotations

from typing import Any, Dict, List

from opencode_manager.models import JobRecord
from opencode_manager.opencode.session import OpenCodeClient, snapshot_chat


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
