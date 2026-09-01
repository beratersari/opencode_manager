"""crash.log records start, uncaught, and clean vs abrupt exit."""

from __future__ import annotations

from pathlib import Path

from opencode_manager.crash import install_crash_logging, mark_clean_shutdown
import opencode_manager.crash as crashmod


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
