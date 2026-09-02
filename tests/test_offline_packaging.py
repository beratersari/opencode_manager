"""Offline packager helpers (no network)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


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
    dar = mod.standalone_python_asset(ver, "darwin-arm64")
    assert win.endswith("x86_64-pc-windows-msvc-install_only.tar.gz")
    assert linux.endswith("x86_64-unknown-linux-gnu-install_only.tar.gz")
    assert dar.endswith("aarch64-apple-darwin-install_only.tar.gz")
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


def test_four_platform_packs() -> None:
    mod = _load()
    assert set(mod.PACKS) == {"windows", "linux", "darwin", "winlinux"}
    assert mod.PACKS["windows"]["suffix"] == "windows-x64"
    assert mod.PACKS["linux"]["suffix"] == "linux-x64"
    assert mod.PACKS["darwin"]["suffix"] == "darwin"
    assert mod.PACKS["winlinux"]["suffix"] == "windows-linux"
    assert mod.wheel_for_pack("foo-1.0-py3-none-any.whl", "windows")
    assert mod.wheel_for_pack("foo-1.0-cp312-cp312-win_amd64.whl", "windows")
    assert not mod.wheel_for_pack("foo-1.0-cp312-cp312-win_amd64.whl", "linux")
    assert mod.wheel_for_pack("foo-1.0-cp312-cp312-macosx_11_0_arm64.whl", "darwin")
    assert mod.wheel_for_pack("foo-1.0-cp312-cp312-win_amd64.whl", "winlinux")
    assert mod.wheel_for_pack("foo-1.0-cp312-cp312-manylinux2014_x86_64.whl", "winlinux")
    assert not mod.wheel_for_pack("foo-1.0-cp312-cp312-macosx_11_0_arm64.whl", "winlinux")


def test_ci_uploads_stage_dir_not_nested_zip() -> None:
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "offline-dist.yml").read_text(
        encoding="utf-8"
    )
    assert "path: dist/stage/${{ steps.ver.outputs.dist_name }}-windows-x64/" in text
    assert "path: dist/stage/${{ steps.ver.outputs.dist_name }}-linux-x64/" in text
    assert "path: dist/stage/${{ steps.ver.outputs.dist_name }}-darwin/" in text
    assert "path: dist/stage/${{ steps.ver.outputs.dist_name }}-windows-linux/" in text
    assert "path: dist/${{ steps.ver.outputs.dist_name }}-windows-x64.zip" not in text.split("Create GitHub Release")[0]


def test_zip_does_not_stage_agents_folder() -> None:
    mod = _load()
    assert "agents" not in mod.COPY_DIRS
    assert "agents" in mod.SKIP_DIR_NAMES


def test_reqs_for_wheel_platform_strips_uvloop_on_windows() -> None:
    mod = _load()
    reqs = [
        "fastapi>=0.115",
        "uvicorn[standard]>=0.32",
        "httpx>=0.27",
        "pydantic>=2.7",
        "PyYAML>=6.0",
    ]
    win = [r.lower() for r in mod.reqs_for_wheel_platform(reqs, "windows")]
    linux = [r.lower() for r in mod.reqs_for_wheel_platform(reqs, "linux")]
    assert any(r.startswith("uvicorn") and "standard" not in r for r in win)
    assert not any("uvloop" in r for r in win)
    assert any("colorama" in r for r in win)
    assert any(r.startswith("pyyaml") for r in win)
    assert any("pydantic-core" in r or r.startswith("pydantic-core") for r in win)
    assert any("uvloop" in r for r in linux)
    assert not any("colorama" in r for r in linux)


def test_assert_pack_wheels_requires_native_windows(tmp_path: Path) -> None:
    mod = _load()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "fastapi-0.1-py3-none-any.whl").write_bytes(b"x")
    with pytest.raises(SystemExit, match="pyyaml"):
        mod.assert_pack_wheels(wheels, "windows")
    (wheels / "pyyaml-6.0.3-cp312-cp312-win_amd64.whl").write_bytes(b"x")
    (wheels / "pydantic_core-2.46.5-cp312-cp312-win_amd64.whl").write_bytes(b"x")
    mod.assert_pack_wheels(wheels, "windows")


def test_assert_pack_wheels_winlinux_needs_both_oses(tmp_path: Path) -> None:
    mod = _load()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "PyYAML-6.0.2-cp312-cp312-manylinux2014_x86_64.whl").write_bytes(b"x")
    (wheels / "pydantic_core-2.0.0-cp312-cp312-manylinux2014_x86_64.whl").write_bytes(b"x")
    with pytest.raises(SystemExit, match="windows"):
        mod.assert_pack_wheels(wheels, "winlinux")
    (wheels / "PyYAML-6.0.2-cp312-cp312-win_amd64.whl").write_bytes(b"x")
    (wheels / "pydantic_core-2.0.0-cp312-cp312-win_amd64.whl").write_bytes(b"x")
    mod.assert_pack_wheels(wheels, "winlinux")


def test_install_sh_picks_os_specific_python() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "osm-lib.sh" in text
    assert "osm_require_bundled_python" in text
    lib = (root / "scripts" / "osm-lib.sh").read_text(encoding="utf-8")
    assert "darwin-arm64" in lib
    assert "darwin-x64" in lib
    assert "osm_ensure_linux_data_dir" in lib
    assert "osm_chmod_launchers" in lib
    assert "osm_ensure_linux_data_dir" in text
    assert "osm_chmod_launchers" in text


def test_shipped_settings_yaml_does_not_force_linux_data_dir() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "settings.yaml").read_text(encoding="utf-8")
    active = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any(line.startswith("data_dir:") for line in active)


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
        assert "vendor/python" in text.replace("\\", "/") or "osm-lib.sh" in text
        assert "BUNDLED" in text or "bundled" in text.lower() or "osm_require_bundled_python" in text
        assert "where python" not in text.lower()
    for text in (oc_bat, oc_sh):
        assert "vendor" in text
        assert "from scratch" in text.lower()
        assert "install_opencode.py" in text
        assert "vendor/python" in text.replace("\\", "/") or "osm-lib.sh" in text
