"""App + per-job file logging."""

from __future__ import annotations

import logging
import re
import shlex
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from opencode_manager import log_context

_SECRET_USERINFO = re.compile(r"(://)([^/\s:@]+):([^@/\s]+)@")
_SECRET_PASS_ONLY = re.compile(r"(://):([^@/\s]+)@")
_SECRET_USER_ONLY = re.compile(r"(://)([^/\s:@]+)@")
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
    # user:pass@ first, then :pass@, then Azure-style user@ (PAT as username).
    text = _SECRET_USERINFO.sub(r"\1***:***@", text)
    text = _SECRET_PASS_ONLY.sub(r"\1:***@", text)
    return _SECRET_USER_ONLY.sub(r"\1***@", text)


def clip(text: Any, limit: int = 800) -> str:
    """Redact then truncate so fail logs stay readable."""
    raw = "" if text is None else str(text)
    cleaned = redact(raw).replace("\n", "\\n")
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}...(+{len(cleaned) - limit} chars)"


def fmt_cmd(argv: Sequence[Any]) -> str:
    parts = [str(a) for a in argv]
    try:
        joined = shlex.join(parts)
    except Exception:
        joined = " ".join(parts)
    return redact(joined)


def log_command(
    logger: logging.Logger,
    argv: Sequence[Any],
    *,
    cwd: Any = ".",
    timeout: Any = None,
    pid: Any = None,
    extra: str = "",
) -> None:
    """INFO: every subprocess we start, with full (redacted) argv."""
    tail = f" {extra}" if extra else ""
    logger.info(
        "command argv=%s cwd=%s timeout=%s pid=%s%s",
        fmt_cmd(argv),
        cwd or ".",
        timeout if timeout is not None else "-",
        pid if pid is not None else "-",
        tail,
    )


def log_command_result(
    logger: logging.Logger,
    argv: Sequence[Any],
    *,
    returncode: Any,
    stdout: Any = "",
    stderr: Any = "",
    cwd: Any = ".",
    timeout: Any = None,
    pid: Any = None,
) -> None:
    code = 0 if returncode is None else int(returncode)
    if code == 0:
        out = clip(stdout, 400)
        logger.info(
            "command ok exit=0 argv=%s cwd=%s pid=%s stdout=%s",
            fmt_cmd(argv),
            cwd or ".",
            pid if pid is not None else "-",
            out or "(empty)",
        )
        return
    logger.error(
        "command FAIL exit=%s argv=%s cwd=%s timeout=%s pid=%s stdout=%s stderr=%s",
        code,
        fmt_cmd(argv),
        cwd or ".",
        timeout if timeout is not None else "-",
        pid if pid is not None else "-",
        clip(stdout, 1200),
        clip(stderr, 1200),
    )


def log_http(
    logger: logging.Logger,
    method: str,
    url: str,
    *,
    status: Any = None,
    body: Any = None,
    err: Any = None,
    params: Any = None,
    ok: Optional[bool] = None,
) -> None:
    failed = ok is False or err is not None or (
        status is not None and int(status) >= 400
    )
    line = (
        f"http {method.upper()} {redact(str(url))}"
        f" status={status if status is not None else '-'}"
    )
    if params:
        line += f" params={clip(params, 200)}"
    if body not in (None, ""):
        line += f" body={clip(body, 600)}"
    if err:
        line += f" err={clip(err, 400)}"
    if failed:
        logger.error("%s", line)
    else:
        logger.info("%s", line)


def log_fail(logger: logging.Logger, headline: str, **fields: Any) -> None:
    """Direct fail line: headline plus key=value facts (redacted)."""
    bits = [f"{key}={clip(value, 500)}" for key, value in fields.items()]
    logger.error("%s %s", headline, " ".join(bits) if bits else "")


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (value or "")) or "unknown"


def _stamp(when: Optional[str] = None) -> str:
    if when:
        compact = (
            when.replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
        )
        if len(compact) >= 15 and compact[8] == "_":
            return compact[:15]
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def job_log_filename(jira_id: str, job_id: str, when: Optional[str] = None) -> str:
    """`{jiraid}_{jobid}_{YYYYMMDD}_{HHMMSS}.log` — one file per job."""
    return f"{_safe_id(jira_id)}_{_safe_id(job_id)}_{_stamp(when)}.log"


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
    """Append lines to `{jiraid}_{jobid}_{YYYYMMDD}_{HHMMSS}.log`."""

    def __init__(self, job_log_dir: Path) -> None:
        super().__init__()
        self.job_log_dir = job_log_dir
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        name = log_context.get_log_file()
        if not name:
            return
        path = self.job_log_dir / name
        line = self.format(record) + "\n"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def setup_logging(
    *,
    job_log_dir: Path,
    level: str = "INFO",
    app_log: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> logging.Logger:
    logger = logging.getLogger("opencode_manager")
    logger.handlers.clear()
    logger.setLevel(_LEVELS.get(level.upper(), logging.INFO))
    fmt = _Formatter()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    if app_log is None:
        app_log = (project_root / "logs" / "app.log") if project_root else Path(job_log_dir) / "app.log"
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


def read_job_log_lines(
    job_log_dir: Path,
    jira_id: str,
    job_id: str,
    *,
    log_file: Optional[str] = None,
    limit: Optional[int] = 2000,
) -> list[dict]:
    path: Optional[Path] = None
    if log_file:
        candidate = job_log_dir / Path(log_file).name
        if candidate.is_file():
            path = candidate
    if path is None:
        matches = sorted(job_log_dir.glob(f"{_safe_id(jira_id)}_{_safe_id(job_id)}_*.log"))
        path = matches[-1] if matches else None
    if path is None or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        ts = line[:23] if len(line) >= 23 else ""
        out.append({"timestamp": ts, "message": line, "job_id": job_id, "jira_id": jira_id})
    # limit 0 / None = whole file (report zip). Positive = last N for the tab.
    if limit is None or int(limit) <= 0:
        return out
    return out[-int(limit) :]
