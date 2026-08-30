"""Sequential hard-delete with retries."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from opencode_manager.log import get_logger, log_command, log_command_result, log_fail

logger = get_logger()

_WIN_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def _chmod_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def win_extended_path(path: Path | str) -> str:
    """Windows long-path prefix. `rd /s /q \\\\?\\…`."""
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text.lstrip("\\")
    return "\\\\?\\" + text


def win_reserved_stem(name: str) -> bool:
    stem = name.split(".")[0].upper()
    return stem in _WIN_RESERVED


def _windows_del_reserved(root: Path) -> None:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in list(filenames) + list(dirnames):
            if not win_reserved_stem(name):
                continue
            target = Path(dirpath) / name
            extended = win_extended_path(target)
            logger.info("delete reserved Windows name %s", extended)
            del_cmd = ["cmd", "/c", "del", "/f", "/q", extended]
            log_command(logger, del_cmd)
            del_res = subprocess.run(del_cmd, capture_output=True, check=False, text=True)
            log_command_result(
                logger, del_cmd, returncode=del_res.returncode, stdout=del_res.stdout, stderr=del_res.stderr
            )
            if target.exists():
                rd_cmd = ["cmd", "/c", "rd", "/s", "/q", extended]
                log_command(logger, rd_cmd)
                rd_res = subprocess.run(rd_cmd, capture_output=True, check=False, text=True)
                log_command_result(
                    logger, rd_cmd, returncode=rd_res.returncode, stdout=rd_res.stdout, stderr=rd_res.stderr
                )


def windows_rd_cmd(path: Path) -> list[str]:
    return ["cmd", "/c", "rd", "/s", "/q", win_extended_path(path)]


def hard_delete(path: Path, *, attempts: int = 6) -> bool:
    if not path.exists():
        logger.info("hard-delete skip (missing) %s", path)
        return True
    logger.info("hard-delete start %s attempts=%s", path, attempts)
    delay = 0.2
    for n in range(attempts):
        if os.name == "nt":
            _windows_del_reserved(path)
            cmd = windows_rd_cmd(path)
            log_command(logger, cmd)
            result = subprocess.run(cmd, capture_output=True, check=False, text=True)
            log_command_result(
                logger,
                cmd,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
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
    log_fail(logger, "hard-delete left remnants", path=path, attempts=attempts)
    return not path.exists()
