"""App + per-job file logging."""

from __future__ import annotations

import logging
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from opencode_manager import log_context

_SECRET_USERINFO = re.compile(r"(://)([^/\s:@]+):([^@/\s]+)@")
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def redact(text: str) -> str:
    if not text:
        return text
    return _SECRET_USERINFO.sub(r"\1***:***@", text)


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        loc = f"[{Path(record.pathname).name}:{record.lineno}]"
        job_id = log_context.get_job_id() or "-"
        jira_id = log_context.get_jira_id() or "-"
        msg = redact(record.getMessage())
        return (
            f"{ts}  {record.levelname:<8}  {loc}  {record.funcName}  "
            f"[job_id={job_id} jira_id={jira_id}]  {msg}"
        )


class _JobFileHandler(logging.Handler):
    """Append lines for the current jira_id to that ticket's log file."""

    def __init__(self, job_log_dir: Path) -> None:
        super().__init__()
        self.job_log_dir = job_log_dir
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        jira_id = log_context.get_jira_id()
        if not jira_id:
            return
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in jira_id)
        path = self.job_log_dir / f"{safe}.log"
        line = self.format(record) + "\n"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def setup_logging(*, project_root: Path, job_log_dir: Path, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("opencode_manager")
    logger.handlers.clear()
    logger.setLevel(_LEVELS.get(level.upper(), logging.INFO))
    fmt = _Formatter()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    app_log = project_root / "logs" / "app.log"
    app_log.parent.mkdir(parents=True, exist_ok=True)
    file_h = logging.FileHandler(app_log, encoding="utf-8")
    file_h.setFormatter(fmt)
    logger.addHandler(file_h)
    job_h = _JobFileHandler(job_log_dir)
    job_h.setFormatter(fmt)
    logger.addHandler(job_h)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("opencode_manager")


def read_job_log_lines(job_log_dir: Path, jira_id: str, job_id: str, *, limit: int = 2000) -> list[dict]:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in jira_id)
    path = job_log_dir / f"{safe}.log"
    if not path.is_file():
        return []
    needle = f"job_id={job_id}"
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if needle not in line:
            continue
        ts = line[:23] if len(line) >= 23 else ""
        out.append({"timestamp": ts, "message": line, "job_id": job_id, "jira_id": jira_id})
    return out[-limit:]
