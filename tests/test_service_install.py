"""OS service install (does not change the two-window exe)."""

from __future__ import annotations

from pathlib import Path

from opencode_manager.brand import APP_SLUG
from opencode_manager.crash import service_wrapper_log_dir, wrapper_exit_log_path
from opencode_manager.service_install import (
    command_for_service,
    find_payload_exe,
    linux_run_wrapper,
    systemd_unit,
    windows_run_wrapper,
    winsw_xml,
)
from opencode_manager.settings import data_dir_from_root


def test_find_payload_prefers_versioned_exe(tmp_path: Path) -> None:
    (tmp_path / "WinSW.exe").write_bytes(b"w")
    payload = tmp_path / f"{APP_SLUG}-0.1.0-windows-x64.exe"
    payload.write_bytes(b"x")
    found = find_payload_exe(tmp_path)
    assert found == payload


def test_command_for_service_uses_backend_only(tmp_path: Path) -> None:
    import os

    exe = tmp_path / f"{APP_SLUG}-1.0-windows-x64.exe"
    exe.write_bytes(b"x")
    if os.name != "nt":
        linux_bin = tmp_path / f"{APP_SLUG}-1.0-linux-x64"
        linux_bin.write_bytes(b"x")
        path, args = command_for_service(tmp_path)
        assert path == linux_bin
        assert args == ["--backend-only"]
        return
    path, args = command_for_service(tmp_path)
    assert path == exe
    assert args == ["--backend-only"]


def test_command_for_service_falls_back_to_venv(tmp_path: Path) -> None:
    py = tmp_path / ".venv" / "Scripts" / "python.exe"
    if __import__("os").name != "nt":
        py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"x")
    path, args = command_for_service(tmp_path)
    assert path == py
    assert args == ["-m", "opencode_manager.app"]


def test_winsw_xml_logs_under_data_dir(tmp_path: Path) -> None:
    run_bat = tmp_path / "service" / "run.bat"
    run_bat.parent.mkdir(parents=True)
    run_bat.write_text("@echo off\n", encoding="utf-8")
    data_dir = tmp_path / "osm"
    xml = winsw_xml(tmp_path, run_bat, data_dir)
    assert "<id>amir-mini</id>" in xml
    assert "/c " in xml
    assert str(run_bat) in xml
    assert str(service_wrapper_log_dir(data_dir / "logs")) in xml
    assert "GIT_TERMINAL_PROMPT" in xml


def test_windows_run_wrapper_writes_wrapper_exit(tmp_path: Path) -> None:
    exe = tmp_path / "amir-mini-1.exe"
    data_dir = tmp_path / "osm"
    text = windows_run_wrapper(exe, ["--backend-only"], data_dir)
    assert "--backend-only" in text
    assert str(wrapper_exit_log_path(data_dir / "logs")) in text
    assert "source=service" in text


def test_systemd_unit_logs_under_data_dir(tmp_path: Path) -> None:
    run_sh = tmp_path / "service" / "run.sh"
    run_sh.parent.mkdir(parents=True)
    run_sh.write_text("#!/bin/sh\n", encoding="utf-8")
    data_dir = tmp_path / "osm"
    unit = systemd_unit(tmp_path, run_sh, data_dir, user_unit=True)
    assert f"ExecStart=/bin/sh {run_sh}" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "WantedBy=multi-user.target" not in unit
    assert str(service_wrapper_log_dir(data_dir / "logs") / "stdout.log") in unit
    sys_unit = systemd_unit(tmp_path, run_sh, data_dir, user_unit=False)
    assert "WantedBy=multi-user.target" in sys_unit
    sh = linux_run_wrapper(tmp_path / "python", ["-m", "opencode_manager.app"], data_dir)
    assert "opencode_manager.app" in sh
    assert str(wrapper_exit_log_path(data_dir / "logs")) in sh


def test_data_dir_from_root_reads_overlay(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OSM_DATA_DIR", raising=False)
    custom = tmp_path / "custom-osm"
    (tmp_path / "settings.local.yaml").write_text(
        f"data_dir: {custom.as_posix()}\n", encoding="utf-8"
    )
    assert data_dir_from_root(tmp_path) == custom
    monkeypatch.setenv("OSM_DATA_DIR", str(tmp_path / "env-osm"))
    assert data_dir_from_root(tmp_path) == tmp_path / "env-osm"


def test_install_service_scripts_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "scripts" / "install-service.bat").is_file()
    assert (root / "scripts" / "uninstall-service.bat").is_file()
    assert (root / "scripts" / "install-service.sh").is_file()
    assert (root / "scripts" / "uninstall-service.sh").is_file()
    bat = (root / "scripts" / "install-service.bat").read_text(encoding="utf-8")
    assert "--backend-only" in bat
    assert "two-window" in bat.lower() or "Does not change" in bat
    assert "WinSW" in bat
    assert r"C:\osm\logs\wrapper-exit.log" in bat
    assert r"C:\osm\logs\service" in bat


def test_standalone_exe_not_a_service() -> None:
    text = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "opencode_manager"
        / "standalone.py"
    ).read_text(encoding="utf-8")
    assert "service_install" not in text
    assert "WinSW" not in text
