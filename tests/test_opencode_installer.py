"""Separate offline OpenCode 1.18.10 installer artifact."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    path = ROOT / "packaging" / "build_opencode_dist.py"
    spec = importlib.util.spec_from_file_location("osm_oc_dist", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_opencode_installer_is_pinned_to_1_18_10() -> None:
    mod = _load_builder()
    assert mod.PINNED_OPENCODE_VERSION == "1.18.10"
    ver = mod.pinned_versions(ROOT)
    assert ver["OPENCODE_VERSION"] == "1.18.10"


def test_opencode_installer_scripts_are_offline() -> None:
    bat = (ROOT / "packaging" / "opencode-installer" / "install.bat").read_text(encoding="utf-8")
    sh = (ROOT / "packaging" / "opencode-installer" / "install.sh").read_text(encoding="utf-8")
    for text in (bat, sh):
        assert "1.18.10" in text
        assert "autoupdate" in text
        assert "pypi" not in text.lower()
        assert "npm" not in text.lower()
        assert "curl" not in text.lower()
        assert "wget" not in text.lower()


def test_stage_installer_windows_layout(tmp_path: Path) -> None:
    mod = _load_builder()
    bin_src = tmp_path / "bin"
    bin_src.mkdir()
    (bin_src / "opencode.exe").write_bytes(b"MZ")
    payload = tmp_path / "stage"
    mod.stage_installer(ROOT, payload, os_name="windows", bin_src=bin_src)
    assert (payload / "install.bat").is_file()
    assert (payload / "vendor" / "bin" / "windows" / "opencode.exe").read_bytes() == b"MZ"
    assert (payload / "OPENCODE_VERSION.txt").read_text(encoding="utf-8").strip() == "1.18.10"
    assert not (payload / "vendor" / "bin" / "linux").exists()
    assert not (payload / "src").exists()


def test_stage_installer_linux_layout(tmp_path: Path) -> None:
    mod = _load_builder()
    bin_src = tmp_path / "bin"
    bin_src.mkdir()
    (bin_src / "opencode").write_bytes(b"ELF")
    payload = tmp_path / "stage"
    mod.stage_installer(ROOT, payload, os_name="linux", bin_src=bin_src)
    assert (payload / "install.sh").is_file()
    assert (payload / "vendor" / "bin" / "linux" / "opencode").read_bytes() == b"ELF"
    assert (payload / "OPENCODE_VERSION.txt").read_text(encoding="utf-8").strip() == "1.18.10"


def test_ci_uploads_opencode_installer() -> None:
    text = (ROOT / ".github" / "workflows" / "offline-dist.yml").read_text(encoding="utf-8")
    assert "packaging/build_opencode_dist.py" in text
    assert "amir-mini-opencode-1.18.10-windows-x64" in text
    assert "amir-mini-opencode-1.18.10-linux-x64" in text
