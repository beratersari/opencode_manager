from __future__ import annotations

import json
import threading
from pathlib import Path


class JobQueue:
    """Per-MR FIFO of job ids waiting to run."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._rows: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, list[str]] = {}
        for key, value in data.items():
            if isinstance(value, list):
                out[str(key)] = [str(v) for v in value if v]
        return out

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self._rows, indent=2), encoding="utf-8")

    def enqueue(self, mr_key: str, job_id: str) -> None:
        with self._lock:
            bucket = self._rows.setdefault(mr_key, [])
            if job_id not in bucket:
                bucket.append(job_id)
            self._persist()

    def peek(self, mr_key: str) -> str | None:
        with self._lock:
            bucket = self._rows.get(mr_key) or []
            return bucket[0] if bucket else None

    def pop(self, mr_key: str) -> str | None:
        with self._lock:
            bucket = self._rows.get(mr_key) or []
            if not bucket:
                return None
            job_id = bucket.pop(0)
            if bucket:
                self._rows[mr_key] = bucket
            else:
                self._rows.pop(mr_key, None)
            self._persist()
            return job_id

    def pop_if(self, mr_key: str, job_id: str) -> str | None:
        """Pop the head only if it is still ``job_id``. Else leave the FIFO alone."""
        with self._lock:
            bucket = self._rows.get(mr_key) or []
            if not bucket or bucket[0] != job_id:
                return None
            bucket.pop(0)
            if bucket:
                self._rows[mr_key] = bucket
            else:
                self._rows.pop(mr_key, None)
            self._persist()
            return job_id

    def remove(self, mr_key: str, job_id: str) -> bool:
        with self._lock:
            bucket = self._rows.get(mr_key) or []
            if job_id not in bucket:
                return False
            bucket = [j for j in bucket if j != job_id]
            if bucket:
                self._rows[mr_key] = bucket
            else:
                self._rows.pop(mr_key, None)
            self._persist()
            return True

    def drain(self, mr_key: str) -> list[str]:
        with self._lock:
            jobs = list(self._rows.pop(mr_key, []))
            self._persist()
            return jobs

    def queued_ids(self, mr_key: str | None = None) -> list[str]:
        with self._lock:
            if mr_key is not None:
                return list(self._rows.get(mr_key) or [])
            out: list[str] = []
            for bucket in self._rows.values():
                out.extend(bucket)
            return out

    def public_items(self, mr_key: str | None = None) -> list[dict[str, str]]:
        with self._lock:
            items: list[dict[str, str]] = []
            rows = {mr_key: self._rows.get(mr_key) or []} if mr_key else self._rows
            for key, bucket in rows.items():
                for index, job_id in enumerate(bucket):
                    items.append({"mr_key": key, "job_id": job_id, "position": str(index)})
            return items
