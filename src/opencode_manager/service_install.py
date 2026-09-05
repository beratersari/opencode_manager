"""Install or remove aMIR-mini as an OS service.

Does not change the double-click exe (two console windows). The service
runs backend-only: API + dashboard on listen_port. Git GCM popups cannot
appear in Session 0 — store credentials for the service account first.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from opencode_manager.brand import APP_NAME, APP_SLUG, BACKEND_TITLE
from opencode_manager.settings import executable_dir, resource_root

SERVICE_ID = APP_SLUG
SERVICE_DISPLAY = APP_NAME


def find_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve()
    for cand in (Path.cwd(), executable_dir(), resource_root(), here.parents[2], here.parents[1]):
        if (cand / "pyproject.toml").is_file() or list(cand.glob(f"{APP_SLUG}*.exe")):
            return cand
    return Path.cwd()


def find_winsw(root: Path) -> Optional[Path]:
    names = ("WinSW.exe", "WinSW-x64.exe", "winsw.exe")
    places = (
        root / "vendor" / "bin" / "windows",
        root / "vendor" / "bin",
        root / "service",
        root,
    )
    for folder in places:
        for name in names:
            path = folder / name
            if path.is_file():
                return path
    return None


def find_payload_exe(root: Path) -> Optional[Path]:
    """The two-window product exe. Service wraps it with --backend-only."""
    skip = {f"{SERVICE_ID}.exe".lower(), "winsw.exe", "winsw-x64.exe"}
    found: list[Path] = []
    for path in root.glob("*.exe"):
        low = path.name.lower()
        if low in skip:
            continue
        if low.startswith(APP_SLUG) and "service" not in low:
            found.append(path)
    if found:
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return found[0]
    return None


def find_venv_python(root: Path) -> Optional[Path]:
    if os.name == "nt":
        cand = root / ".venv" / "Scripts" / "python.exe"
    else:
        cand = root / ".venv" / "bin" / "python"
    return cand if cand.is_file() else None


def command_for_service(root: Path) -> tuple[Path, list[str]]:
    exe = find_payload_exe(root)
    if exe is not None:
        return exe, ["--backend-only"]
    py = find_venv_python(root)
    if py is not None:
        return py, ["-m", "opencode_manager.app"]
    raise SystemExit(
        "No aMIR-mini payload found. Put the exe in this folder, "
        "or run install.bat / install.sh first (needs .venv)."
    )


def winsw_xml(root: Path, executable: Path, arguments: list[str]) -> str:
    args = " ".join(_xml_escape(a) for a in arguments)
    opencode = Path.home() / ".opencode" / "bin"
    path_extra = ""
    if opencode.is_dir():
        path_extra = f"{opencode};"
    log_dir = root / "service" / "logs"
    return f"""<service>
  <id>{SERVICE_ID}</id>
  <name>{_xml_escape(SERVICE_DISPLAY)}</name>
  <description>{_xml_escape(APP_NAME)} API and dashboard (backend only). Git credential popups do not work here; store GCM credentials for this Windows account first.</description>
  <executable>{_xml_escape(str(executable))}</executable>
  <arguments>{args}</arguments>
  <workingdirectory>{_xml_escape(str(root))}</workingdirectory>
  <logpath>{_xml_escape(str(log_dir))}</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>8</keepFiles>
  </log>
  <onfailure action="restart" delay="10 sec"/>
  <env name="GIT_TERMINAL_PROMPT" value="0"/>
  <env name="PYTHONUNBUFFERED" value="1"/>
  <env name="PATH" value="{_xml_escape(path_extra)}%PATH%"/>
</service>
"""


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def systemd_unit(root: Path, executable: Path, arguments: list[str]) -> str:
    args = " ".join(arguments)
    path_extra = ""
    oc = Path.home() / ".opencode" / "bin"
    if oc.is_dir():
        path_extra = f"Environment=PATH={oc}:/usr/local/bin:/usr/bin\n"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    user_line = f"User={user}\n" if user and is_root else ""
    return f"""[Unit]
Description={APP_NAME} API and dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{user_line}WorkingDirectory={root}
ExecStart={executable} {args}
Restart=on-failure
RestartSec=5
Environment=GIT_TERMINAL_PROMPT=0
Environment=PYTHONUNBUFFERED=1
{path_extra}
[Install]
WantedBy=multi-user.target
"""


def _run(cmd: list[str]) -> int:
    print("  +", " ".join(cmd))
    return subprocess.run(cmd, check=False).returncode


def install_windows(root: Path) -> int:
    winsw = find_winsw(root)
    if winsw is None:
        print(
            "[ERROR] WinSW.exe is missing. Use the Windows zip (vendor\\bin\\windows\\WinSW.exe) "
            "or copy WinSW-x64.exe next to this script.",
            file=sys.stderr,
        )
        return 1
    executable, arguments = command_for_service(root)
    svc_dir = root / "service"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / "logs").mkdir(parents=True, exist_ok=True)
    wrapper = svc_dir / f"{SERVICE_ID}.exe"
    xml_path = svc_dir / f"{SERVICE_ID}.xml"
    shutil.copy2(winsw, wrapper)
    xml_path.write_text(winsw_xml(root, executable, arguments), encoding="utf-8")
    print(f"[OK] wrapper {wrapper}")
    print(f"[OK] config  {xml_path}")
    print(f"[OK] runs    {executable} {' '.join(arguments)}")
    _run([str(wrapper), "stop"])
    _run([str(wrapper), "uninstall"])
    rc = _run([str(wrapper), "install"])
    if rc != 0:
        print("[ERROR] Service install failed. Run this from an elevated command prompt.", file=sys.stderr)
        return rc
    rc = _run([str(wrapper), "start"])
    if rc != 0:
        print("[ERROR] Service installed but did not start. Check Windows Event Viewer / service\\logs.", file=sys.stderr)
        return rc
    print()
    print(f"{APP_NAME} is running as a Windows service ({SERVICE_ID}).")
    print("Dashboard: http://127.0.0.1:4096/jobs")
    print("The two-window exe is unchanged. Stop the service before using that exe on the same port.")
    print("Git: no GCM popup in a service. Store credentials for this Windows user first.")
    print("If clone fails, set the service Log On account in services.msc to your user.")
    return 0


def uninstall_windows(root: Path) -> int:
    wrapper = root / "service" / f"{SERVICE_ID}.exe"
    if not wrapper.is_file():
        print(f"[OK] no service wrapper at {wrapper}")
        return 0
    _run([str(wrapper), "stop"])
    rc = _run([str(wrapper), "uninstall"])
    print(f"[OK] uninstalled {SERVICE_ID}" if rc == 0 else "[ERROR] uninstall failed")
    return rc


def status_windows(root: Path) -> int:
    wrapper = root / "service" / f"{SERVICE_ID}.exe"
    if not wrapper.is_file():
        print("not installed")
        return 1
    return _run([str(wrapper), "status"])


def _systemd_cmd(user_unit: bool) -> list[str]:
    if user_unit:
        return ["systemctl", "--user"]
    return ["systemctl"]


def install_linux(root: Path) -> int:
    executable, arguments = command_for_service(root)
    unit = systemd_unit(root, executable, arguments)
    user_unit = True
    dest = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_ID}.service"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        user_unit = False
        dest = Path("/etc/systemd/system") / f"{SERVICE_ID}.service"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit, encoding="utf-8")
    print(f"[OK] unit {dest}")
    print(f"[OK] runs {executable} {' '.join(arguments)}")
    ctl = _systemd_cmd(user_unit)
    _run([*ctl, "daemon-reload"])
    rc = _run([*ctl, "enable", "--now", f"{SERVICE_ID}.service"])
    if rc != 0:
        print("[ERROR] systemctl enable --now failed.", file=sys.stderr)
        return rc
    print()
    print(f"{APP_NAME} is running as a systemd service ({dest}).")
    print("Dashboard: http://127.0.0.1:4096/jobs")
    print("The two-window exe is unchanged.")
    return 0


def uninstall_linux() -> int:
    user_unit = not (hasattr(os, "geteuid") and os.geteuid() == 0)
    ctl = _systemd_cmd(user_unit)
    _run([*ctl, "disable", "--now", f"{SERVICE_ID}.service"])
    dest = (
        Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_ID}.service"
        if user_unit
        else Path("/etc/systemd/system") / f"{SERVICE_ID}.service"
    )
    if dest.is_file():
        dest.unlink()
        print(f"[OK] removed {dest}")
    _run([*ctl, "daemon-reload"])
    return 0


def status_linux() -> int:
    user_unit = not (hasattr(os, "geteuid") and os.geteuid() == 0)
    return _run([*_systemd_cmd(user_unit), "status", f"{SERVICE_ID}.service"])


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Install {APP_NAME} as a Windows service or systemd unit. "
        f"Does not change {BACKEND_TITLE} / two-window exe."
    )
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--root", default=None, help="Install root (folder with exe or pyproject.toml)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else find_root()
    if args.action == "install":
        return install_windows(root) if os.name == "nt" else install_linux(root)
    if args.action == "uninstall":
        return uninstall_windows(root) if os.name == "nt" else uninstall_linux()
    return status_windows(root) if os.name == "nt" else status_linux()


if __name__ == "__main__":
    raise SystemExit(main())
