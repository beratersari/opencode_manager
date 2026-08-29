"""Persisted FIFO of queued jobs (includes PAT for this process only)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


class JobQueue:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def enqueue(self, row: Dict[str, Any]) -> None:
        with self._lock:
            rows = self._load()
            rows.append(row)
            self._save(rows)

    def dequeue(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            rows = self._load()
            if not rows:
                return None
            first = rows.pop(0)
            self._save(rows)
            return first

    def peek_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._load())

    def clear(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._load()
            self._save([])
            return rows

    def public_items(self, jira_id: Optional[str] = None) -> List[Dict[str, Any]]:
        key = (jira_id or "").strip()
        out = []
        for row in self.peek_all():
            if key and str(row.get("jira_id") or "") != key:
                continue
            out.append(
                {
                    "job_id": row.get("job_id"),
                    "jira_id": row.get("jira_id"),
                    "status": "queued",
                    "accepted_at": row.get("accepted_at"),
                    "source_branch": row.get("source_branch"),
                    "model": row.get("model"),
                    "agent_mode": row.get("agent_mode"),
                }
            )
        return out
