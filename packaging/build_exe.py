#!/usr/bin/env python3
"""Build a single-file Windows or Linux executable (this OS only).

  python packaging/build_exe.py
  python packaging/build_exe.py --out-dir dist

Does not change start.bat / start.sh. Does not vendor Git or OpenCode.
Must run on the target OS (PyInstaller cannot cross-compile).
Needs web/dist/index.html (npm run build, or packaging/build_dist.py --in-place).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import zipfile
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_product_version(root: Path) -> str:
    env = (os.environ.get("OSM_PRODUCT_VERSION") or "").strip()
    if env:
        return env
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def host_suffix() -> str:
    if sys.platform.startswith("win"):
        return "windows-x64"
    if sys.platform.startswith("linux"):
        return "linux-x64"
    raise SystemExit(
        f"Single-file exe is Windows and Linux only (this host is {sys.platform})."
    )


def service_kit_filename(version: str, suffix: str) -> str:
    src = repo_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from opencode_manager.brand import APP_SLUG

    return f"{APP_SLUG}-{version}-{suffix}-service.zip"


def artifact_filename(version: str, suffix: str) -> str:
    src = repo_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from opencode_manager.brand import APP_SLUG

    name = f"{APP_SLUG}-{version}-{suffix}"
    if suffix.startswith("windows"):
        return f"{name}.exe"
    return name


def add_data_sep() -> str:
    return ";" if sys.platform.startswith("win") else ":"


def uvicorn_hidden_imports() -> list[str]:
    names = [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        "httpx",
        "yaml",
        "pydantic",
        "pydantic_core",
        "multipart",
        "email_validator",
        "anyio",
        "starlette",
        "fastapi",
        "watchfiles",
        "httptools",
        "colorama",
    ]
    if not sys.platform.startswith("win"):
        names.append("uvloop")
        names.append("uvicorn.loops.uvloop")
    return names


def package_hidden_imports() -> list[str]:
    import pkgutil

    src = repo_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    import opencode_manager

    names = ["opencode_manager"]
    for info in pkgutil.walk_packages(
        opencode_manager.__path__, opencode_manager.__name__ + "."
    ):
        names.append(info.name)
    return names


def pyinstaller_args(
    *,
    entry: Path,
    src: Path,
    settings_yaml: Path,
    web_dist: Path,
    work: Path,
    out_dir: Path,
    internal_name: str = "amir-mini",
) -> list[str]:
    sep = add_data_sep()
    args = [
        str(entry),
        "--onefile",
        "--console",
        "--noupx",
        "--clean",
        "--noconfirm",
        f"--name={internal_name}",
        f"--distpath={out_dir}",
        f"--workpath={work}",
        f"--specpath={work}",
        f"--paths={src}",
        f"--add-data={settings_yaml}{sep}.",
        f"--add-data={web_dist}{sep}web/dist",
        "--exclude-module=tkinter",
        "--exclude-module=matplotlib",
        "--exclude-module=numpy",
        "--exclude-module=pytest",
        "--exclude-module=IPython",
        "--collect-submodules=opencode_manager",
        "--copy-metadata=pydantic",
        "--copy-metadata=pydantic_core",
        "--copy-metadata=uvicorn",
        "--copy-metadata=fastapi",
        "--copy-metadata=starlette",
        "--copy-metadata=httpx",
        "--copy-metadata=anyio",
    ]
    if sys.platform.startswith("win"):
        args.append("--exclude-module=uvloop")
    for name in package_hidden_imports() + uvicorn_hidden_imports():
        args.append(f"--hidden-import={name}")
    return args


def ensure_winsw(root: Path) -> Path:
    """WinSW next to the service installer. Fetch if vendor/ is empty."""
    target = root / "vendor" / "bin" / "windows" / "WinSW.exe"
    if target.is_file():
        return target
    dist_py = root / "packaging" / "build_dist.py"
    spec = importlib.util.spec_from_file_location("osm_build_dist", dist_py)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load {dist_py} to fetch WinSW")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ver = mod.read_versions(root / "packaging" / "versions.env")
    mod.fetch_winsw(ver, target.parent)
    if not target.is_file():
        raise SystemExit("WinSW.exe is required for the Windows service zip")
    return target


def write_service_kit(zip_path: Path, files: list[tuple[Path, str]], readme: str) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", readme)
        for src, arcname in files:
            if not src.is_file():
                raise SystemExit(f"Service kit missing {src}")
            zf.write(src, arcname=arcname)


def service_kit_readme(*, windows: bool) -> str:
    if windows:
        return (
            "aMIR-mini — Windows service kit\n"
            "\n"
            "Extract this zip to a permanent folder (not Desktop or Downloads).\n"
            "Keep every file in that same folder.\n"
            "\n"
            "  amir-mini-*-windows-x64.exe\n"
            "  install-service.bat\n"
            "  uninstall-service.bat\n"
            "  WinSW.exe\n"
            "  settings.local.yaml          (data_dir C:\\osm)\n"
            "\n"
            "Install (starts now and at every boot):\n"
            "  1. Open Command Prompt as Administrator\n"
            "  2. cd to the extracted folder\n"
            "  3. install-service.bat\n"
            "\n"
            "Dashboard: http://127.0.0.1:4096/jobs\n"
            "Do not double-click the exe while the service is running (same port).\n"
            "\n"
            "If it does not start:\n"
            "  C:\\osm\\logs\\wrapper-exit.log\n"
            "  C:\\osm\\logs\\service\\\n"
            "  C:\\osm\\logs\\crash.log\n"
        )
    return (
        "aMIR-mini — Linux service kit\n"
        "\n"
        "Extract this zip to a permanent folder. Keep every file there.\n"
        "\n"
        "  ./install-service.sh     # enable --now (starts at boot)\n"
        "  ./uninstall-service.sh\n"
        "\n"
        "User systemd units also need:  loginctl enable-linger \"$USER\"\n"
        "Dashboard: http://127.0.0.1:4096/jobs\n"
        "Logs: {data_dir}/logs/wrapper-exit.log and {data_dir}/logs/service/\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build OSM single-file exe for this OS")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory for the final exe (default: <repo>/dist)",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    suffix = host_suffix()
    version = read_product_version(root)
    dest_name = artifact_filename(version, suffix)
    web_dist = root / "web" / "dist"
    if not (web_dist / "index.html").is_file():
        print(
            f"[ERROR] {web_dist / 'index.html'} is missing.",
            file=sys.stderr,
        )
        print(
            "Build the SPA first:  cd web && npm ci && npm run build",
            file=sys.stderr,
        )
        print(
            "Or:  python packaging/build_dist.py --in-place",
            file=sys.stderr,
        )
        return 1
    settings_yaml = root / "settings.yaml"
    if not settings_yaml.is_file():
        print(f"[ERROR] {settings_yaml} is missing.", file=sys.stderr)
        return 1

    try:
        import PyInstaller.__main__
    except ImportError:
        print(
            "[ERROR] PyInstaller is not installed. "
            'pip install -e ".[exe]"  (or pip install pyinstaller)',
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out_dir).resolve() if args.out_dir else root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "exe-build" / suffix
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    entry = root / "src" / "opencode_manager" / "standalone.py"
    src = root / "src"
    print(f"Building {dest_name} (onefile, {suffix})...")
    PyInstaller.__main__.run(
        pyinstaller_args(
            entry=entry,
            src=src,
            settings_yaml=settings_yaml,
            web_dist=web_dist,
            work=work,
            out_dir=work / "dist",
        )
    )

    built = work / "dist" / (
        "amir-mini.exe" if suffix.startswith("windows") else "amir-mini"
    )
    if not built.is_file():
        print(f"[ERROR] PyInstaller did not write {built}", file=sys.stderr)
        return 1
    final = out_dir / dest_name
    if final.exists():
        final.unlink()
    shutil.copy2(built, final)
    if not suffix.startswith("windows"):
        final.chmod(final.stat().st_mode | 0o111)
    overlay_src = root / "packaging" / (
        "settings.local.windows.yaml"
        if suffix.startswith("windows")
        else "settings.local.linux.yaml"
    )
    overlay_dest = out_dir / "settings.local.yaml"
    shutil.copy2(overlay_src, overlay_dest)
    kit_files: list[tuple[Path, str]] = [
        (final, dest_name),
        (overlay_dest, "settings.local.yaml"),
    ]
    if suffix.startswith("windows"):
        for launcher in ("install-service.bat", "uninstall-service.bat"):
            src_l = root / "scripts" / launcher
            if src_l.is_file():
                dest_l = out_dir / launcher
                shutil.copy2(src_l, dest_l)
                kit_files.append((dest_l, launcher))
                print(f"[OK] {dest_l}")
        winsw = ensure_winsw(root)
        dest_w = out_dir / "WinSW.exe"
        shutil.copy2(winsw, dest_w)
        kit_files.append((dest_w, "WinSW.exe"))
        print(f"[OK] {dest_w}")
    else:
        for launcher in ("install-service.sh", "uninstall-service.sh"):
            src_l = root / "scripts" / launcher
            if src_l.is_file():
                dest_l = out_dir / launcher
                shutil.copy2(src_l, dest_l)
                dest_l.chmod(dest_l.stat().st_mode | 0o111)
                kit_files.append((dest_l, launcher))
                print(f"[OK] {dest_l}")
    kit_name = service_kit_filename(version, suffix)
    kit_path = out_dir / kit_name
    write_service_kit(kit_path, kit_files, service_kit_readme(windows=suffix.startswith("windows")))
    size_mb = final.stat().st_size / (1024 * 1024)
    print(f"[OK] {final} ({size_mb:.1f} MB)")
    print(f"[OK] {overlay_dest}")
    print(f"[OK] {kit_path}")
    print("Two-window exe is unchanged. Use install-service.* from the service zip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
