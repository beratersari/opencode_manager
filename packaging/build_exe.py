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
    env = (os.environ.get("OSM_LINUX_SUFFIX") or "").strip()
    if env:
        return env
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


def exe_kit_filename(version: str, suffix: str) -> str:
    src = repo_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from opencode_manager.brand import APP_SLUG

    if suffix.startswith("windows"):
        return f"{APP_SLUG}-{version}-{suffix}-exe.zip"
    return f"{APP_SLUG}-{version}-{suffix}.zip"


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


def write_linux_spec(
    *,
    entry: Path,
    src: Path,
    settings_yaml: Path,
    web_dist: Path,
    work: Path,
    out_dir: Path,
    internal_name: str = "amir-mini",
) -> Path:
    """Onefile spec. Do not bundle libz.so.1 (fails to map on other Ubuntu)."""
    portable = (os.environ.get("OSM_LINUX_PORTABLE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    hidden = package_hidden_imports() + uvicorn_hidden_imports()
    spec = work / f"{internal_name}.spec"
    spec.write_text(
        "\n".join(
            [
                "# Generated by packaging/build_exe.py. Do not edit.",
                "# -*- mode: python ; coding: utf-8 -*-",
                "from pathlib import Path",
                "",
                f"ENTRY = Path({str(entry)!r})",
                f"SRC = Path({str(src)!r})",
                f"SETTINGS = Path({str(settings_yaml)!r})",
                f"WEB = Path({str(web_dist)!r})",
                f"DIST = Path({str(out_dir)!r})",
                f"WORK = Path({str(work)!r})",
                f"HIDDEN = {hidden!r}",
                f"PORTABLE = {portable!r}",
                "",
                "a = Analysis(",
                "    [str(ENTRY)],",
                "    pathex=[str(SRC)],",
                "    binaries=[],",
                "    datas=[(str(SETTINGS), '.'), (str(WEB), 'web/dist')],",
                "    hiddenimports=HIDDEN,",
                "    hookspath=[],",
                "    hooksconfig={},",
                "    runtime_hooks=[],",
                "    excludes=['tkinter', 'matplotlib', 'numpy', 'pytest', 'IPython'],",
                "    noarchive=False,",
                ")",
                "",
                "_drop_pref = (",
                "    'libz.so.', 'libssl.so.', 'libcrypto.so.', 'libffi.so.',",
                "    'libbz2.so.', 'liblzma.so.', 'libtinfo.so.', 'libreadline.so.',",
                "    'libncurses.so.', 'libncursesw.so.', 'libsqlite3.so.',",
                "    'libnsl.so.', 'libuuid.so.', 'libexpat.so.',",
                ")",
                "",
                "def _keep(item):",
                "    name = str(item[0] if item else '')",
                "    base = name.replace('\\\\', '/').rsplit('/', 1)[-1]",
                "    if base == 'libz.so.1' or base.startswith('libz.so.'):",
                "        return False",
                "    if PORTABLE and any(base.startswith(p) for p in _drop_pref):",
                "        return False",
                "    return True",
                "",
                "a.binaries = [item for item in a.binaries if _keep(item)]",
                "pyz = PYZ(a.pure)",
                "exe = EXE(",
                "    pyz, a.scripts, a.binaries, a.datas, [],",
                f"    name={internal_name!r},",
                "    debug=False,",
                "    bootloader_ignore_signals=False,",
                "    strip=False,",
                "    upx=False,",
                "    console=True,",
                "    runtime_tmpdir='.',",
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return spec


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


REVIEW_AGENT_SCRIPTS = (
    "install-review-agent.bat",
    "install-review-agent.sh",
)


def opencoderman_zip_entries(opencoderman: Path) -> list[tuple[Path, str]]:
    """Only agents/*.md and skills/*/SKILL.md. Never .git or the rest."""
    src = Path(opencoderman)
    agent = src / "agents" / "code-reviewer.md"
    if not agent.is_file():
        raise SystemExit(
            "opencoderman/agents/code-reviewer.md missing. "
            "git submodule update --init --recursive"
        )
    skills = src / "skills"
    if not skills.is_dir():
        raise SystemExit("opencoderman/skills missing")
    entries: list[tuple[Path, str]] = []
    for path in sorted(p for p in (src / "agents").glob("*.md") if p.is_file()):
        entries.append((path, f"opencoderman/agents/{path.name}"))
    skill_count = 0
    for skill_dir in sorted(p for p in skills.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        entries.append((skill_md, f"opencoderman/skills/{skill_dir.name}/SKILL.md"))
        skill_count += 1
    if skill_count == 0:
        raise SystemExit("no skills with SKILL.md under opencoderman/skills")
    return entries


def review_agent_script_entries(scripts_dir: Path) -> list[tuple[Path, str]]:
    entries: list[tuple[Path, str]] = []
    for name in REVIEW_AGENT_SCRIPTS:
        path = Path(scripts_dir) / name
        if not path.is_file():
            raise SystemExit(f"missing {path}")
        entries.append((path, name))
    return entries


def write_zip_kit(zip_path: Path, files: list[tuple[Path, str]], readme: str) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", readme)
        for src, arcname in files:
            if not src.is_file():
                raise SystemExit(f"Zip kit missing {src}")
            zf.write(src, arcname=arcname)


def write_service_kit(zip_path: Path, files: list[tuple[Path, str]], readme: str) -> None:
    write_zip_kit(zip_path, files, readme)


def exe_kit_readme(*, windows: bool) -> str:
    if windows:
        return (
            "aMIR-mini — two-window exe\n"
            "\n"
            "Extract this zip. Keep both files in the same folder.\n"
            "\n"
            "  amir-mini-*-windows-x64.exe\n"
            "  settings.local.yaml          (data_dir C:\\osm)\n"
            "\n"
            "Open the exe. This console is the backend (:4096).\n"
            "A second console is the frontend proxy (:5173).\n"
            "\n"
            "Git and OpenCode stay on PATH.\n"
            "\n"
            "Review webhooks: POST /amirmini/webhook/gitlab and\n"
            "POST /amirmini/webhook/azure. Set gitlab_token / azure_token\n"
            "in settings.local.yaml. Run install-review-agent.bat once to\n"
            "copy opencoderman agents and skills into %USERPROFILE%\\.opencode.\n"
        )
    return (
        "aMIR-mini — two-window binary\n"
        "\n"
        "Extract this zip. Keep both files in the same folder.\n"
        "\n"
        "  amir-mini-*-linux-x64  (or amir-mini-*-linux-ubuntu-<ver>-x64)\n"
        "  settings.local.yaml          (data_dir /var/lib/osm)\n"
        "\n"
        "Run the binary. This terminal is the backend (:4096).\n"
        "A second terminal is the frontend proxy (:5173).\n"
        "\n"
        "Git and OpenCode stay on PATH.\n"
        "If /var/lib/osm is not writable, the binary falls back to\n"
        "$XDG_DATA_HOME/osm or ~/.local/share/osm.\n"
        "\n"
        "Review webhooks: POST /amirmini/webhook/gitlab and\n"
        "POST /amirmini/webhook/azure. Set gitlab_token / azure_token\n"
        "in settings.local.yaml. Run ./install-review-agent.sh once to\n"
        "copy opencoderman agents and skills into ~/.opencode.\n"
    )


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
    parser.add_argument(
        "--suffix",
        default="",
        help="Artifact suffix (windows-x64, linux-x64, linux-ubuntu-22.04-x64)",
    )
    parser.add_argument(
        "--skip-web",
        action="store_true",
        help="Require an existing web/dist (do not print the npm hint as the only path)",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    suffix = (args.suffix or "").strip() or host_suffix()
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
    if suffix.startswith("linux"):
        spec = write_linux_spec(
            entry=entry,
            src=src,
            settings_yaml=settings_yaml,
            web_dist=web_dist,
            work=work,
            out_dir=work / "dist",
        )
        PyInstaller.__main__.run(
            [
                str(spec),
                "--noconfirm",
                "--clean",
                f"--distpath={work / 'dist'}",
                f"--workpath={work}",
            ]
        )
    else:
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
        *review_agent_script_entries(root / "scripts"),
        *opencoderman_zip_entries(root / "opencoderman"),
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
    exe_zip_name = exe_kit_filename(version, suffix)
    exe_zip_path = out_dir / exe_zip_name
    write_zip_kit(
        exe_zip_path,
        [
            (final, dest_name),
            (overlay_dest, "settings.local.yaml"),
            *review_agent_script_entries(root / "scripts"),
            *opencoderman_zip_entries(root / "opencoderman"),
        ],
        exe_kit_readme(windows=suffix.startswith("windows")),
    )
    size_mb = final.stat().st_size / (1024 * 1024)
    print(f"[OK] {final} ({size_mb:.1f} MB)")
    print(f"[OK] {overlay_dest}")
    print(f"[OK] {exe_zip_path}")
    print(f"[OK] {kit_path}")
    print("Two-window exe is unchanged. Use install-service.* from the service zip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
