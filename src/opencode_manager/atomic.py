"""Atomic text write that survives Windows readers locking the dest file."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional

from opencode_manager.log import get_logger

logger = get_logger()
_ATTEMPTS = 8


def write_text_atomic(path: Path, text: str, *, attempts: int = _ATTEMPTS) -> None:
    """Write via a unique tmp, then replace. Retry Access Denied; last try is in-place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    last_exc: Optional[BaseException] = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                tmp.write_text(text, encoding="utf-8")
                break
            except OSError as exc:
                last_exc = exc
                if attempt == attempts:
                    raise
                time.sleep(0.05 * attempt)
        for attempt in range(1, attempts + 1):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(0.05 * attempt)
        try:
            path.write_text(text, encoding="utf-8")
            logger.warning("atomic replace failed; wrote in place path=%s err=%s", path, last_exc)
            return
        except OSError as exc:
            raise last_exc or exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
