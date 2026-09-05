"""Single-file exe launcher (backend + frontend). Does not use start.*."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import pytest

from opencode_manager.settings import Settings, executable_dir, load_settings, resource_root
from opencode_manager.standalone import (
    apply_writable_data_dir,
    fallback_data_dir,
    prepare,
    prepare_backend,
    prepare_frontend,
    prepend_opencode_path,
    self_command,
    serve_backend,
    serve_prepared,
    spawn_frontend_window,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_build_exe():
    path = ROOT / "packaging" / "build_exe.py"
    spec = importlib.util.spec_from_file_location("osm_build_exe", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resource_root_unfrozen_is_repo() -> None:
    assert resource_root() == ROOT
    assert executable_dir() == ROOT


def test_resource_root_frozen_uses_meipass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meipass = tmp_path / "meipass"
    meipass.mkdir()
    exe = tmp_path / "payload" / "osm.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert resource_root() == meipass
    assert executable_dir() == exe.parent


def test_frozen_loads_local_overlay_next_to_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meipass = tmp_path / "meipass"
    meipass.mkdir()
    (meipass / "settings.yaml").write_text("listen_port: 4096\n", encoding="utf-8")
    exe_dir = tmp_path / "payload"
    exe_dir.mkdir()
    (exe_dir / "osm.exe").write_bytes(b"x")
    (exe_dir / "settings.local.yaml").write_text("listen_port: 5999\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "osm.exe"))
    monkeypatch.delenv("OSM_SETTINGS", raising=False)
    s = load_settings()
    assert s.listen_port == 5999
    assert s.project_root == meipass


def test_fallback_data_dir_respects_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    path = fallback_data_dir()
    assert path == tmp_path / "xdg" / "osm"


def test_apply_writable_data_dir_windows_keeps_c_osm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    s = Settings(data_dir=blocked)
    real_mkdir = Path.mkdir

    def boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if blocked in self.parents or self == blocked:
            raise PermissionError("denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", boom)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    if sys.platform.startswith("win"):
        with pytest.raises(PermissionError):
            apply_writable_data_dir(s)
        return
    out = apply_writable_data_dir(s)
    assert out.data_dir == tmp_path / "xdg" / "osm"
    assert out.work_dir == out.data_dir / ".temp"
    assert out.job_store_dir == out.data_dir / "jobs"


def test_prepare_starts_both_apps(tmp_settings: Settings, tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    tmp_settings.project_root = tmp_path
    tmp_settings.listen_port = 4096
    prepared = prepare(tmp_settings, frontend_host="127.0.0.1", frontend_port=5173)
    assert prepared.frontend_port == 5173
    assert prepared.frontend_host == "127.0.0.1"
    assert prepared.backend_app is not None
    assert prepared.frontend_app is not None
    assert prepared.dist == dist
    routes = {getattr(r, "path", "") for r in prepared.backend_app.routes}
    assert "/api/meta" in routes or any(str(r).find("meta") >= 0 for r in prepared.backend_app.routes)


def test_prepare_requires_spa(tmp_settings: Settings, tmp_path: Path) -> None:
    tmp_settings.project_root = tmp_path
    with pytest.raises(FileNotFoundError, match="SPA missing"):
        prepare(tmp_settings)


def test_prepare_frontend_does_not_boot_manager(tmp_settings: Settings, tmp_path: Path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    tmp_settings.project_root = tmp_path
    prepared = prepare_frontend(tmp_settings, frontend_port=5173)
    assert prepared.backend_app is None
    assert prepared.frontend_app is not None
    backend = prepare_backend(tmp_settings, frontend_port=5173)
    assert backend.backend_app is not None
    assert backend.frontend_app is None


def test_self_command_frozen_is_the_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "opencode-manager.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    cmd = self_command("--frontend-only", "--frontend-port", "5173")
    assert cmd[0] == str(exe.resolve())
    assert cmd[1:] == ["--frontend-only", "--frontend-port", "5173"]


def test_self_command_unfrozen_uses_module() -> None:
    cmd = self_command("--frontend-only")
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "opencode_manager.standalone"]
    assert "--frontend-only" in cmd


def test_spawn_frontend_window_uses_new_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    class FakePopen:
        def __init__(self, args, **kwargs):  # noqa: ANN001
            seen["args"] = list(args)
            seen["kwargs"] = kwargs

    monkeypatch.setattr("opencode_manager.standalone.subprocess.Popen", FakePopen)
    spawn_frontend_window(frontend_host="0.0.0.0", frontend_port=5173)
    assert "--frontend-only" in seen["args"]
    assert "5173" in seen["args"]
    if sys.platform.startswith("win"):
        import subprocess

        assert seen["kwargs"].get("creationflags") == subprocess.CREATE_NEW_CONSOLE


def test_serve_backend_opens_frontend_window(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    tmp_settings.project_root = tmp_path
    prepared = prepare_backend(tmp_settings, frontend_port=5173)
    served: list[int] = []
    spawned: list[tuple[str, int]] = []

    class FakeConfig:
        def __init__(self, app, host="127.0.0.1", port=0, log_level="info"):  # noqa: ANN001
            self.app = app
            self.host = host
            self.port = port
            self.log_level = log_level

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config
            self.install_signal_handlers = True
            self.should_exit = False
            self.started = False

        async def serve(self) -> None:
            self.started = True
            served.append(self.config.port)
            await asyncio.sleep(0.05)

    def fake_spawn(*, frontend_host: str, frontend_port: int):  # noqa: ANN001
        spawned.append((frontend_host, frontend_port))
        return object()

    monkeypatch.setattr("opencode_manager.standalone.uvicorn.Config", FakeConfig)
    monkeypatch.setattr("opencode_manager.standalone.uvicorn.Server", FakeServer)
    monkeypatch.setattr("opencode_manager.standalone.spawn_frontend_window", fake_spawn)
    rc = asyncio.run(serve_backend(prepared, spawn_frontend=True))
    assert rc == 0
    assert tmp_settings.listen_port in served
    assert spawned == [(prepared.frontend_host, 5173)]


def test_serve_prepared_runs_backend_only(
    tmp_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    tmp_settings.project_root = tmp_path
    prepared = prepare(tmp_settings, frontend_port=5173)
    served: list[int] = []

    class FakeConfig:
        def __init__(self, app, host="127.0.0.1", port=0, log_level="info"):  # noqa: ANN001
            self.app = app
            self.host = host
            self.port = port
            self.log_level = log_level

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config
            self.install_signal_handlers = True
            self.should_exit = False
            self.started = False

        async def serve(self) -> None:
            self.started = True
            served.append(self.config.port)

    monkeypatch.setattr("opencode_manager.standalone.uvicorn.Config", FakeConfig)
    monkeypatch.setattr("opencode_manager.standalone.uvicorn.Server", FakeServer)
    rc = asyncio.run(serve_prepared(prepared))
    assert rc == 0
    assert tmp_settings.listen_port in served
    assert 5173 not in served


def test_prepend_opencode_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / ".opencode" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr("opencode_manager.standalone.Path.home", lambda: tmp_path)
    monkeypatch.setenv("PATH", "C:\\windows\\system32" if sys.platform.startswith("win") else "/usr/bin")
    prepend_opencode_path()
    assert str(bin_dir) == os_path_head()


def os_path_head() -> str:
    import os

    return os.environ.get("PATH", "").split(os.pathsep)[0]


def test_build_exe_names_and_does_not_touch_start_scripts() -> None:
    mod = _load_build_exe()
    assert mod.artifact_filename("1.2.3", "windows-x64") == "opencode-manager-1.2.3-windows-x64.exe"
    assert mod.artifact_filename("1.2.3", "linux-x64") == "opencode-manager-1.2.3-linux-x64"
    text = (ROOT / "packaging" / "build_exe.py").read_text(encoding="utf-8")
    assert "start.bat" in text and "Does not change" in text
    start_bat = (ROOT / "scripts" / "start.bat").read_text(encoding="utf-8")
    start_sh = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    assert "OSM-Backend" in start_bat
    assert "start-backend.sh" in start_sh


def test_build_exe_pyinstaller_args_onefile(tmp_path: Path) -> None:
    mod = _load_build_exe()
    settings = tmp_path / "settings.yaml"
    settings.write_text("listen_port: 4096\n", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("x", encoding="utf-8")
    args = mod.pyinstaller_args(
        entry=tmp_path / "standalone.py",
        src=tmp_path,
        settings_yaml=settings,
        web_dist=dist,
        work=tmp_path / "work",
        out_dir=tmp_path / "out",
    )
    assert "--onefile" in args
    assert "--console" in args
    assert "--noupx" in args
    assert "--collect-submodules=opencode_manager" in args
    assert any(a.startswith("--add-data=") and "web/dist" in a.replace("\\", "/") for a in args)
    assert any(a == "--hidden-import=opencode_manager.standalone" for a in args)
    assert any(a.startswith("--hidden-import=uvicorn") for a in args)


def test_ci_uploads_single_exe_artifact() -> None:
    text = (ROOT / ".github" / "workflows" / "offline-dist.yml").read_text(encoding="utf-8")
    assert "packaging/build_exe.py" in text
    assert "Single-file exe" in text
    assert "windows-latest" in text
    assert "ubuntu-latest" in text
    assert "dist/settings.local.yaml" in text
    assert "name: Single-file exe" in text
    assert "packaging/build_exe.py" in text


def test_standalone_does_not_import_start_scripts() -> None:
    text = (ROOT / "src" / "opencode_manager" / "standalone.py").read_text(encoding="utf-8")
    assert "scripts/start" not in text
    assert "scripts\\start" not in text
    assert "scripts/start" not in text.split('"""', 2)[-1]
    assert "--frontend-only" in text
    assert "CREATE_NEW_CONSOLE" in text
