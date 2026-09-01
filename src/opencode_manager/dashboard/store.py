"""One JSON file per job_id."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from opencode_manager.atomic import write_text_atomic
from opencode_manager.log import get_logger
from opencode_manager.models import JobRecord, utc_now

logger = get_logger()


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        safe = job_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.json"

    def save(self, job: JobRecord) -> None:
        """Atomic write. Retries Windows Access Denied when the json is being read."""
        job.updated_at = utc_now()
        payload = job.model_dump_json(indent=2)
        with self._lock:
            write_text_atomic(self._path(job.job_id), payload)

    def try_save(self, job: JobRecord) -> bool:
        """Same as save, but a lock must not abort a live job."""
        return persist_job(self, job)

    def get(self, job_id: str) -> Optional[JobRecord]:
        path = self._path(job_id)
        with self._lock:
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


def persist_job(store: object, job: JobRecord) -> bool:
    """Save job history. Never raise — Windows file locks must not kill work."""
    try:
        saver = getattr(store, "save", None)
        if saver is None:
            return False
        saver(job)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("job store save failed job=%s", getattr(job, "job_id", ""))
        return False
