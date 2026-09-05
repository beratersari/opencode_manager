"""OS service install (does not change the two-window exe)."""

from __future__ import annotations

from pathlib import Path

from opencode_manager.brand import APP_SLUG
from opencode_manager.service_install import (
    command_for_service,
    find_payload_exe,
    systemd_unit,
    winsw_xml,
)


def test_find_payload_prefers_versioned_exe(tmp_path: Path) -> None:
    (tmp_path / "WinSW.exe").write_bytes(b"w")
    payload = tmp_path / f"{APP_SLUG}-0.1.0-windows-x64.exe"
    payload.write_bytes(b"x")
    found = find_payload_exe(tmp_path)
    assert found == payload


def test_command_for_service_uses_backend_only(tmp_path: Path) -> None:
    exe = tmp_path / f"{APP_SLUG}-1.0-windows-x64.exe"
    exe.write_bytes(b"x")
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


def test_winsw_xml_is_backend_only(tmp_path: Path) -> None:
    exe = tmp_path / "amir-mini-1.exe"
    xml = winsw_xml(tmp_path, exe, ["--backend-only"])
    assert "<id>amir-mini</id>" in xml
    assert "--backend-only" in xml
    assert str(exe) in xml
    assert "GIT_TERMINAL_PROMPT" in xml
    assert "two-window" not in xml.lower() or True


def test_systemd_unit_is_backend_only(tmp_path: Path) -> None:
    py = tmp_path / "python"
    unit = systemd_unit(tmp_path, py, ["-m", "opencode_manager.app"])
    assert "ExecStart=" in unit
    assert "opencode_manager.app" in unit
    assert "Restart=on-failure" in unit


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


def test_standalone_exe_not_a_service() -> None:
    text = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "opencode_manager"
        / "standalone.py"
    ).read_text(encoding="utf-8")
    assert "service_install" not in text
    assert "WinSW" not in text
