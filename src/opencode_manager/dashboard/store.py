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
_overlay_lock = threading.Lock()
_terminal_overlay: dict[str, JobRecord] = {}


def remember_unsaved_terminal(job: JobRecord) -> None:
    """Keep a finished row in memory when the last disk write failed."""
    with _overlay_lock:
        _terminal_overlay[job.job_id] = job


def clear_unsaved_terminal(job_id: str) -> None:
    with _overlay_lock:
        _terminal_overlay.pop(job_id, None)


def _overlay_rows(rows: List[JobRecord]) -> List[JobRecord]:
    with _overlay_lock:
        return [_terminal_overlay.get(job.job_id, job) for job in rows]


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

    def _read_json_bytes(self, path: Path) -> Optional[bytes]:
        """Retry transient Windows Access Denied from AV / indexer readers."""
        last: Optional[BaseException] = None
        for attempt in range(1, 6):
            try:
                return path.read_bytes()
            except OSError as exc:
                last = exc
                time.sleep(0.05 * attempt)
        if last is not None:
            logger.warning("job json read failed path=%s err=%s", path, last)
        return None

    def _load_record(self, path: Path, *, ignore_size: bool = False) -> Optional[JobRecord]:
        """json.loads(bytes) + model_validate. Do not use model_validate_json."""
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if not ignore_size and size > MAX_JSON_SIZE:
            logger.warning("skip oversized job json path=%s bytes=%s", path, size)
            return None
        raw = self._read_json_bytes(path)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return JobRecord.model_validate(data)
        except (OSError, ValueError, TypeError):
            return None

    def _peek_live_ticket(self, path: Path, jira_id: str) -> Optional[JobRecord]:
        """Live ticket row even when the JSON is too big for list_all."""
        raw = self._read_json_bytes(path)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if str(data.get("jira_id") or "") != jira_id:
            return None
        if str(data.get("job_kind") or "ticket") == "review":
            return None
        if str(data.get("status") or "") not in {"queued", "running"}:
            return None
        try:
            return JobRecord.model_validate(
                {
                    "job_id": data.get("job_id") or "",
                    "jira_id": data.get("jira_id") or "",
                    "status": data.get("status") or "",
                    "session_id": data.get("session_id") or "",
                    "live": data.get("live", True),
                }
            )
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
            rec = self._load_record(path, ignore_size=True) if path.is_file() else None
        with _overlay_lock:
            if job_id in _terminal_overlay:
                return _terminal_overlay[job_id]
        return rec

    def list_all(self) -> List[JobRecord]:
        with self._lock:
            now = time.monotonic()
            if self._cache is not None and (now - self._cache_ts) < CACHE_TTL_SECONDS:
                return _overlay_rows(self._cache)
            rows: List[JobRecord] = []
            for path in self.root.glob("*.json"):
                rec = self._load_record(path)
                if rec is not None:
                    rows.append(rec)
            rows.sort(key=lambda j: j.accepted_at or j.updated_at or "", reverse=True)
            self._cache = rows
            self._cache_ts = now
            return _overlay_rows(rows)

    def live_for_jira(self, jira_id: str) -> Optional[JobRecord]:
        """One live ticket job. Do not use list_all — oversized rows are skipped there."""
        key = (jira_id or "").strip()
        if not key:
            return None
        for path in self.root.glob("*.json"):
            rec = self._peek_live_ticket(path, key)
            if rec is None:
                continue
            with _overlay_lock:
                over = _terminal_overlay.get(rec.job_id)
            if over is not None and over.status not in {"queued", "running"}:
                continue
            return rec
        return None

    def running_for_mr(self, mr_key: str) -> Optional[JobRecord]:
        for job in self.list_all():
            if (
                getattr(job, "job_kind", "") == "review"
                and job.mr_key == mr_key
                and job.status == "running"
            ):
                return job
        return None


def persist_job(store: object, job: JobRecord) -> bool:
    """Save job history. Never raise — Windows file locks must not kill work."""
    try:
        saver = getattr(store, "save", None)
        if saver is None:
            return False
        saver(job)
        clear_unsaved_terminal(getattr(job, "job_id", "") or "")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("job store save failed job=%s", getattr(job, "job_id", ""))
        return False
