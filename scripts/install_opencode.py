#!/usr/bin/env python3
"""Wipe any previous OpenCode user install and copy the vendored CLI (offline, stdlib)."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import sys
import time
from pathlib import Path

STOCK_CONFIG = """{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "plugin": []
}
"""


def home() -> Path:
    # Windows: always %USERPROFILE% so we land in <user>\.opencode, not AppData.
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        if profile:
            return Path(profile)
    return Path.home()


def opencode_home() -> Path:
    return home() / ".opencode"


def dest_dir() -> Path:
    return opencode_home() / "bin"


def vendor_binary(root: Path) -> Path | None:
    vendor_bin = root / "vendor" / "bin"
    if os.name == "nt":
        candidates = (vendor_bin / "windows" / "opencode.exe", vendor_bin / "opencode.exe")
    elif sys.platform == "darwin":
        machine = platform.machine().lower()
        tag = "darwin-arm64" if machine in {"arm64", "aarch64"} else "darwin-x64"
        candidates = (vendor_bin / tag / "opencode", vendor_bin / "opencode")
    else:
        candidates = (vendor_bin / "linux" / "opencode", vendor_bin / "opencode")
    for path in candidates:
        if path.is_file():
            return path
    return None


def candidate_old_paths() -> list[Path]:
    # Install and wipe only <user>/.opencode (Windows: %USERPROFILE%\.opencode).
    return [opencode_home()]


def existing_locations() -> list[Path]:
    found: list[Path] = []
    for path in candidate_old_paths():
        try:
            if path.exists():
                found.append(path)
        except OSError:
            continue
    return found


def remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    last_err: OSError | None = None
    for _ in range(8):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:

                def _onexc(func, p, _exc):  # type: ignore[no-untyped-def]
                    try:
                        os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                    except OSError:
                        pass
                    func(p)

                if sys.version_info >= (3, 12):
                    shutil.rmtree(path, onexc=_onexc)
                else:
                    shutil.rmtree(path, onerror=lambda func, p, _i: _onexc(func, p, None))
            return
        except OSError as e:
            last_err = e
            time.sleep(0.4)
    raise OSError(f"Could not remove {path}: {last_err}")


def wipe_old() -> list[Path]:
    found = existing_locations()
    if not found:
        print("[OK] No previous OpenCode install found")
        return []
    print("Previous OpenCode install detected:")
    for path in found:
        print(f"  {path}")
    print("Removing it so this install is from scratch...")
    for path in found:
        print(f"  Removing {path}")
        remove_tree(path)
        if path.exists():
            raise OSError(f"Still present after delete: {path}")
        print(f"  [OK] gone: {path}")
    return found


def write_stock_config(oc_home: Path) -> Path:
    cfg = oc_home / "opencode.json"
    cfg.write_text(STOCK_CONFIG, encoding="utf-8")
    return cfg


def prepend_user_path(directory: Path) -> None:
    if os.name != "nt":
        return
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        try:
            raw, _typ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            raw = ""
        parts = [p for p in str(raw).split(";") if p]
        norm = str(directory).rstrip("\\")
        parts = [p for p in parts if p.rstrip("\\").lower() != norm.lower()]
        new = norm + (";" + ";".join(parts) if parts else "")
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new)
        print(f"[OK] Prepended to user PATH: {norm}")
    finally:
        key.Close()


def install(root: Path) -> Path:
    src = vendor_binary(root)
    if src is None:
        raise FileNotFoundError(f"No OpenCode binary under {root / 'vendor' / 'bin'}")
    wipe_old()
    dest = dest_dir()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / src.name
    shutil.copy2(src, target)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | 0o111)
    write_stock_config(opencode_home())
    prepend_user_path(dest)
    print(f"Install root: {opencode_home()}")
    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Wipe previous OpenCode and install the vendored CLI (offline)"
    )
    p.add_argument("--root", required=True, help="Repo / zip root that contains vendor/bin")
    args = p.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    try:
        target = install(root)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        print("Use a CI zip from packaging/build_dist.py.", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    print(f"[OK] OpenCode installed from scratch: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
