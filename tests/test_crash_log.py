"""crash.log records start, uncaught, and clean vs abrupt exit."""

from __future__ import annotations

from pathlib import Path

from opencode_manager.crash import (
    crash_log_path,
    install_crash_logging,
    mark_clean_shutdown,
    service_wrapper_log_dir,
    wrapper_exit_log_path,
)
import opencode_manager.crash as crashmod


def test_wrapper_exit_path_is_under_job_log_dir(tmp_path: Path) -> None:
    assert wrapper_exit_log_path(tmp_path) == tmp_path / "wrapper-exit.log"
    assert service_wrapper_log_dir(tmp_path) == tmp_path / "service"
    assert crash_log_path(tmp_path) == tmp_path / "crash.log"


def test_crash_log_start_and_clean_exit(tmp_path: Path) -> None:
    path = install_crash_logging(tmp_path)
    assert path == tmp_path / "crash.log"
    text = path.read_text(encoding="utf-8")
    assert "process start pid=" in text
    mark_clean_shutdown()
    assert crashmod._clean is True
    crashmod._append("clean shutdown pid=1")
    text = path.read_text(encoding="utf-8")
    assert "clean shutdown" in text


def test_excepthook_writes_uncaught(tmp_path: Path) -> None:
    path = install_crash_logging(tmp_path)
    try:
        raise RuntimeError("boom-for-log")
    except RuntimeError:
        import sys

        sys.excepthook(*sys.exc_info())
    text = path.read_text(encoding="utf-8")
    assert "UNCAUGHT RuntimeError: boom-for-log" in text
