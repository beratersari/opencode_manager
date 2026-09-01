"""One JSON file per job_id."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import List, Optional

from opencode_manager.log import get_logger
from opencode_manager.models import JobRecord, utc_now

logger = get_logger()
_SAVE_ATTEMPTS = 8


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        safe = job_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.json"

    def save(self, job: JobRecord) -> None:
        """Atomic write. Retry Windows Access Denied when the json is being read."""
        job.updated_at = utc_now()
        payload = job.model_dump_json(indent=2)
        with self._lock:
            path = self._path(job.job_id)
            tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(payload, encoding="utf-8")
            last_exc: Optional[BaseException] = None
            try:
                for attempt in range(1, _SAVE_ATTEMPTS + 1):
                    try:
                        os.replace(tmp, path)
                        return
                    except OSError as exc:
                        last_exc = exc
                        if attempt < _SAVE_ATTEMPTS:
                            time.sleep(0.05 * attempt)
                try:
                    path.write_text(payload, encoding="utf-8")
                    logger.warning(
                        "job store replace failed; wrote in place job=%s err=%s",
                        job.job_id,
                        last_exc,
                    )
                    return
                except OSError as exc:
                    last_exc = exc
                raise last_exc or OSError("job store save failed")
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

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
