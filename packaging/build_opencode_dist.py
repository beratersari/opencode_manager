#!/usr/bin/env python3
"""Build a separate offline OpenCode installer zip. Always 1.18.10.

  python packaging/build_opencode_dist.py
  python packaging/build_opencode_dist.py --out-dir dist

Does not change the two-window exe or the four OSM zips.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path

PINNED_OPENCODE_VERSION = "1.18.10"


def _load_build_dist():
    path = Path(__file__).resolve().parent / "build_dist.py"
    spec = importlib.util.spec_from_file_location("osm_build_dist", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pinned_versions(root: Path) -> dict[str, str]:
    bd = _load_build_dist()
    ver = bd.read_versions(root / "packaging" / "versions.env")
    ver["OPENCODE_VERSION"] = PINNED_OPENCODE_VERSION
    return ver


def stage_installer(root: Path, payload: Path, *, os_name: str, bin_src: Path) -> None:
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    if os_name == "windows":
        dest_bin = payload / "vendor" / "bin" / "windows"
        dest_bin.mkdir(parents=True)
        src = bin_src / "opencode.exe"
        if not src.is_file():
            raise SystemExit(f"missing {src}")
        shutil.copy2(src, dest_bin / "opencode.exe")
        shutil.copy2(root / "packaging" / "opencode-installer" / "install.bat", payload / "install.bat")
    else:
        dest_bin = payload / "vendor" / "bin" / "linux"
        dest_bin.mkdir(parents=True)
        src = bin_src / "opencode"
        if not src.is_file():
            raise SystemExit(f"missing {src}")
        shutil.copy2(src, dest_bin / "opencode")
        (dest_bin / "opencode").chmod((dest_bin / "opencode").stat().st_mode | 0o111)
        shutil.copy2(root / "packaging" / "opencode-installer" / "install.sh", payload / "install.sh")
        (payload / "install.sh").chmod((payload / "install.sh").stat().st_mode | 0o111)
    (payload / "OPENCODE_VERSION.txt").write_text(PINNED_OPENCODE_VERSION + "\n", encoding="utf-8")
    (payload / "README.txt").write_text(
        "aMIR-mini offline OpenCode installer\n"
        f"This zip always installs OpenCode {PINNED_OPENCODE_VERSION}.\n"
        "No network. Does not install aMIR-mini.\n\n"
        "Windows:  install.bat\n"
        "Linux:    ./install.sh\n\n"
        "Installs to <user>/.opencode (Windows: %USERPROFILE%\\.opencode).\n"
        "Wipes that folder first. autoupdate is off.\n",
        encoding="utf-8",
    )


def zip_payload(payload: Path, dest_zip: Path) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in payload.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline OpenCode 1.18.10 installer zips")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    bd = _load_build_dist()
    ver = pinned_versions(root)
    if ver["OPENCODE_VERSION"] != PINNED_OPENCODE_VERSION:
        raise SystemExit(f"refusing to build: version is {ver['OPENCODE_VERSION']}")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else root / "dist"
    cache = out_dir / "_vendor_cache" / "opencode-installer"
    print(f"Building offline OpenCode {PINNED_OPENCODE_VERSION} installers...")
    win_dir = cache / "windows"
    lin_dir = cache / "linux"
    bd.fetch_opencode(ver, "windows", "x64", win_dir)
    bd.fetch_opencode(ver, "linux", "x64", lin_dir)

    packs = (
        ("windows", "windows-x64", win_dir),
        ("linux", "linux-x64", lin_dir),
    )
    written: list[Path] = []
    for os_name, suffix, bin_src in packs:
        name = f"amir-mini-opencode-{PINNED_OPENCODE_VERSION}-{suffix}"
        payload = out_dir / "stage" / name
        print(f"\nStaging {name}...")
        stage_installer(root, payload, os_name=os_name, bin_src=bin_src)
        dest_zip = out_dir / f"{name}.zip"
        zip_payload(payload, dest_zip)
        written.append(dest_zip)
        print(f"[OK] {dest_zip}")
    print("\nOpenCode installer zips (always 1.18.10):")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
