"""One JSON file per job_id."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional

from opencode_manager.models import JobRecord, utc_now


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        safe = job_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.json"

    def save(self, job: JobRecord) -> None:
        job.updated_at = utc_now()
        with self._lock:
            path = self._path(job.job_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(path)

    def get(self, job_id: str) -> Optional[JobRecord]:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list_all(self) -> List[JobRecord]:
        rows: List[JobRecord] = []
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    rows.append(JobRecord.model_validate_json(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        rows.sort(key=lambda j: j.accepted_at or j.updated_at or "", reverse=True)
        return rows

    def live_for_jira(self, jira_id: str) -> Optional[JobRecord]:
        for job in self.list_all():
            if job.jira_id == jira_id and job.status in {"queued", "running"}:
                return job
        return None
