"""Sequential hard-delete with retries."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from opencode_manager.log import get_logger

logger = get_logger()


def _chmod_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def hard_delete(path: Path, *, attempts: int = 6) -> bool:
    if not path.exists():
        logger.info("hard-delete skip (missing) %s", path)
        return True
    logger.info("hard-delete start %s attempts=%s", path, attempts)
    delay = 0.2
    for n in range(attempts):
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "rd", "/s", "/q", str(path)],
                capture_output=True,
                check=False,
            )
        else:
            def _onerror(func, name, _exc):  # noqa: ARG001
                _chmod_writable(Path(name))
                try:
                    func(name)
                except OSError:
                    pass

            try:
                shutil.rmtree(path, onerror=_onerror)
            except OSError:
                pass
        if not path.exists():
            logger.info("hard-delete ok on try %s/%s %s", n + 1, attempts, path)
            return True
        logger.warning("hard-delete still present try %s/%s %s", n + 1, attempts, path)
        time.sleep(delay)
        delay = min(delay * 2, 2.0)
    logger.error("hard-delete left remnants at %s", path)
    return not path.exists()
