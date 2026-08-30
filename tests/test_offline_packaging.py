"""Offline packager helpers (no network)."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "packaging" / "build_dist.py"
    spec = importlib.util.spec_from_file_location("osm_build_dist", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_read_versions(tmp_path: Path) -> None:
    p = tmp_path / "versions.env"
    p.write_text(
        "# comment\nOPENCODE_VERSION=1.18.10\nPYTHON_MIN_VERSION=3.11\n",
        encoding="utf-8",
    )
    data = _load().read_versions(p)
    assert data["OPENCODE_VERSION"] == "1.18.10"
    assert data.get("PYTHON_MIN_VERSION") == "3.11"


def test_runtime_requirements_from_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    deps = _load().runtime_requirements(root)
    joined = " ".join(deps).lower()
    assert "fastapi" in joined
    assert "uvicorn" in joined
    assert "httpx" in joined


def test_standalone_python_asset_names() -> None:
    ver = {
        "PYTHON_FULL_VERSION": "3.12.14",
        "PYTHON_STANDALONE_TAG": "20260825",
    }
    mod = _load()
    win = mod.standalone_python_asset(ver, "windows")
    linux = mod.standalone_python_asset(ver, "linux")
    assert win.endswith("x86_64-pc-windows-msvc-install_only.tar.gz")
    assert linux.endswith("x86_64-unknown-linux-gnu-install_only.tar.gz")
    assert "3.12.14" in win and "3.12.14" in linux


def test_opencode_asset_names() -> None:
    ver = {
        "OPENCODE_WINDOWS_ASSET": "opencode-windows-x64.zip",
        "OPENCODE_LINUX_ASSET": "opencode-linux-x64.tar.gz",
        "OPENCODE_DARWIN_ARM64_ASSET": "opencode-darwin-arm64.zip",
    }
    mod = _load()
    assert mod.opencode_asset(ver, "windows", "x64").endswith(".zip")
    assert mod.opencode_asset(ver, "linux", "x64").endswith(".tar.gz")
    assert "arm64" in mod.opencode_asset(ver, "darwin", "arm64")


def test_supported_python_detects_pydantic_core(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "pydantic_core-2.0.0-cp312-cp312-manylinux2014_x86_64.whl").write_bytes(b"x")
    supported = _load().supported_python_versions(wheels, ["3.11", "3.12", "3.13"])
    assert supported == ["3.12"]


def test_default_dist_name_has_no_os_suffix() -> None:
    root = Path(__file__).resolve().parents[1]
    src = (root / "packaging" / "build_dist.py").read_text(encoding="utf-8")
    assert 'opencode-manager-{version}' in src
    assert "opencode-manager-{os_name}" not in src


def test_install_scripts_are_offline_only() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "scripts" / "install-backend.bat").exists()
    assert not (root / "scripts" / "install-frontend.bat").exists()
    manager_bat = (root / "scripts" / "install.bat").read_text(encoding="utf-8")
    manager_sh = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
    oc_bat = (root / "scripts" / "install-opencode.bat").read_text(encoding="utf-8")
    oc_sh = (root / "scripts" / "install-opencode.sh").read_text(encoding="utf-8")
    for text in (manager_bat, manager_sh):
        assert "--no-index" in text
        assert "vendor" in text
        assert "pypi" not in text.lower()
        assert "web" in text and "dist" in text
        assert "install-opencode" in text
        assert "npm" not in text.lower() or "does not run npm" in text.lower()
        assert "vendor/python" in text.replace("\\", "/")
        assert "BUNDLED" in text or "bundled" in text.lower()
        assert "where python" not in text.lower()
    for text in (oc_bat, oc_sh):
        assert "vendor" in text
        assert "from scratch" in text.lower()
        assert "install_opencode.py" in text
        assert "vendor/python" in text.replace("\\", "/")
