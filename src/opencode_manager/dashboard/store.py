"""One JSON file per job_id."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import List, Optional

from opencode_manager.atomic import write_text_atomic
from opencode_manager.log import get_logger
from opencode_manager.models import JobRecord, utc_now

logger = get_logger()

# Skip a row rather than parse a runaway snapshot in the WS loop.
MAX_JSON_SIZE = 50 * 1024 * 1024
# Other-machine report used 3s. Overlapping /ws + GET /api/jobs +
# live_for_jira share this window. save() drops the cache first.
CACHE_TTL_SECONDS = 3.0


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Optional[List[JobRecord]] = None
        self._cache_ts: float = 0.0

    def _path(self, job_id: str) -> Path:
        safe = job_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.json"

    def _invalidate_list_cache(self) -> None:
        self._cache = None
        self._cache_ts = 0.0

    def _load_record(self, path: Path) -> Optional[JobRecord]:
        """json.loads(bytes) + model_validate. Do not use model_validate_json."""
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > MAX_JSON_SIZE:
            logger.warning("skip oversized job json path=%s bytes=%s", path, size)
            return None
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return JobRecord.model_validate(data)
        except (OSError, ValueError, TypeError):
            return None

    def save(self, job: JobRecord) -> None:
        """Atomic write. Retries Windows Access Denied when the json is being read."""
        job.updated_at = utc_now()
        payload = job.model_dump_json(indent=2)
        with self._lock:
            self._invalidate_list_cache()
            write_text_atomic(self._path(job.job_id), payload)

    def try_save(self, job: JobRecord) -> bool:
        """Same as save, but a lock must not abort a live job."""
        return persist_job(self, job)

    def get(self, job_id: str) -> Optional[JobRecord]:
        path = self._path(job_id)
        with self._lock:
            if not path.is_file():
                return None
            return self._load_record(path)

    def list_all(self) -> List[JobRecord]:
        with self._lock:
            now = time.monotonic()
            if self._cache is not None and (now - self._cache_ts) < CACHE_TTL_SECONDS:
                return list(self._cache)
            rows: List[JobRecord] = []
            for path in self.root.glob("*.json"):
                rec = self._load_record(path)
                if rec is not None:
                    rows.append(rec)
            rows.sort(key=lambda j: j.accepted_at or j.updated_at or "", reverse=True)
            self._cache = rows
            self._cache_ts = now
            return list(rows)

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
