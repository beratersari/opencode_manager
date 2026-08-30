#!/usr/bin/env python3
"""Build an offline distribution. This script needs network. Target install does not.

  python packaging/build_dist.py              # zip under dist/
  python packaging/build_dist.py --in-place   # write vendor/ + web/dist into the repo
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


SKIP_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".git",
    ".egg-info",
    "opencode_manager.egg-info",
}

COPY_FILES = (
    "pyproject.toml",
    "pytest.ini",
    "settings.yaml",
    "README.md",
    "AGENTS.md",
    "Agents.md",
    "PLAN.md",
    "VERSION",
    "n8nflow.json",
)

COPY_DIRS = (
    "src",
    "scripts",
    "tests",
    "tester",
    "agents",
    "packaging",
)


def read_versions(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def host_platform() -> tuple[str, str]:
    system = sys.platform
    machine = platform.machine().lower()
    if system.startswith("win"):
        os_name = "windows"
    elif system.startswith("linux"):
        os_name = "linux"
    elif system == "darwin":
        os_name = "darwin"
    else:
        raise SystemExit(f"Unsupported platform: {system}")
    if machine in {"x86_64", "amd64"}:
        arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise SystemExit(f"Unsupported architecture: {machine}")
    return os_name, arch


def product_version(root: Path) -> str:
    vf = root / "VERSION"
    if vf.is_file():
        return vf.read_text(encoding="utf-8").strip()
    return "0.0.0-dev"


def runtime_requirements(root: Path) -> list[str]:
    pyproject = root / "pyproject.toml"
    if tomllib is None:
        raise SystemExit("Python 3.11+ (tomllib) is required to read pyproject.toml")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = list(data.get("project", {}).get("dependencies") or [])
    if not deps:
        raise SystemExit("No [project].dependencies in pyproject.toml")
    return deps


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("  +", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in SKIP_DIR_NAMES or item.name.endswith(".egg-info"):
            continue
        if item.name == "dist" and src.name == "src":
            continue
        target = dest / item.name
        if item.is_dir():
            copy_tree(item, target)
        else:
            if item.suffix in {".pyc", ".pyo"}:
                continue
            shutil.copy2(item, target)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading {url}")
    print(f"           -> {dest}")
    with urllib.request.urlopen(url) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    size = dest.stat().st_size
    print(f"  OK ({size / (1024 * 1024):.1f} MB)")


def extract_archive(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest)
        return
    raise SystemExit(f"Unknown archive type: {archive}")


def find_file(root: Path, names: tuple[str, ...]) -> Path | None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in names:
            if name in filenames:
                return Path(dirpath) / name
    return None


def opencode_asset(ver: dict[str, str], os_name: str, arch: str) -> str:
    if os_name == "windows":
        return ver.get("OPENCODE_WINDOWS_ASSET") or "opencode-windows-x64.zip"
    if os_name == "linux":
        if arch == "arm64":
            return "opencode-linux-arm64.tar.gz"
        return ver.get("OPENCODE_LINUX_ASSET") or "opencode-linux-x64.tar.gz"
    if arch == "arm64":
        return ver.get("OPENCODE_DARWIN_ARM64_ASSET") or "opencode-darwin-arm64.zip"
    return ver.get("OPENCODE_DARWIN_X64_ASSET") or "opencode-darwin-x64.zip"


WHEEL_PLATFORMS = {
    "windows": "win_amd64",
    "linux": "manylinux2014_x86_64",
    "darwin-arm64": "macosx_11_0_arm64",
    "darwin-x64": "macosx_11_0_x86_64",
}

# Three CI zips: Windows, Linux, macOS (arm64 + x64 in the darwin zip).
PACKS: dict[str, dict[str, object]] = {
    "windows": {
        "suffix": "windows-x64",
        "wheels": ("windows",),
        "pythons": (("windows", "windows"),),
        "opencodes": (("windows", "x64", "windows"),),
    },
    "linux": {
        "suffix": "linux-x64",
        "wheels": ("linux",),
        "pythons": (("linux", "linux"),),
        "opencodes": (("linux", "x64", "linux"),),
    },
    "darwin": {
        "suffix": "darwin",
        "wheels": ("darwin-arm64", "darwin-x64"),
        "pythons": (("darwin-arm64", "darwin-arm64"), ("darwin-x64", "darwin-x64")),
        "opencodes": (("darwin", "arm64", "darwin-arm64"), ("darwin", "x64", "darwin-x64")),
    },
}


def pip_download_wheels(
    reqs: list[str],
    wheels: Path,
    py_versions: list[str],
    platforms: list[str],
) -> None:
    wheels.mkdir(parents=True, exist_ok=True)
    req_file = wheels.parent / "requirements.txt"
    req_file.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    print("  pip download (host Python, prefer-binary)...")
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "-r",
            str(req_file),
            "-d",
            str(wheels),
            "--prefer-binary",
        ]
    )
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "pip",
            "setuptools",
            "wheel",
            "-d",
            str(wheels),
            "--prefer-binary",
        ]
    )
    plats = [WHEEL_PLATFORMS[p] for p in platforms if p in WHEEL_PLATFORMS]
    if not plats:
        return
    for plat in plats:
        for pv in py_versions:
            tag = pv.replace(".", "")
            print(f"  pip download {plat} cp{tag} (Python {pv})...")
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "-r",
                str(req_file),
                "-d",
                str(wheels),
                "--python-version",
                pv,
                "--platform",
                plat,
                "--implementation",
                "cp",
                "--abi",
                f"cp{tag}",
                "--only-binary=:all:",
                "--prefer-binary",
            ]
            try:
                run(cmd)
            except subprocess.CalledProcessError:
                print(f"  WARNING: incomplete wheel set for Python {pv} ({plat})")
            helper = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "pip",
                "setuptools",
                "wheel",
                "-d",
                str(wheels),
                "--python-version",
                pv,
                "--platform",
                plat,
                "--implementation",
                "cp",
                "--abi",
                f"cp{tag}",
                "--only-binary=:all:",
                "--prefer-binary",
            ]
            try:
                run(helper)
            except subprocess.CalledProcessError:
                pass


def supported_python_versions(wheels: Path, candidates: list[str]) -> list[str]:
    names = [p.name for p in wheels.iterdir() if p.is_file()]
    supported: list[str] = []
    for pv in candidates:
        tag = pv.replace(".", "")
        if any(f"cp{tag}" in n and "pydantic_core" in n for n in names):
            supported.append(pv)
            print(f"  supported Python {pv}")
        else:
            print(f"  UNSUPPORTED Python {pv} (no pydantic-core cp{tag} wheel)")
    if not supported:
        # Host download may have produced a py3-none-any set or current-abi only.
        # Still list the running interpreter if any wheel exists.
        supported = [f"{sys.version_info.major}.{sys.version_info.minor}"]
        print(f"  falling back to host Python {supported[0]}")
    return supported


def build_spa(root: Path) -> Path:
    web = root / "web"
    if not (web / "package.json").is_file():
        raise SystemExit("web/package.json missing")
    if shutil.which("npm") is None:
        raise SystemExit("Node.js + npm required on PATH to build web/ (CI: setup-node)")
    print("  node:", subprocess.check_output(["node", "--version"], text=True).strip())
    if (web / "package-lock.json").is_file():
        try:
            run(["npm", "ci", "--no-fund", "--no-audit"], cwd=web)
        except subprocess.CalledProcessError:
            print("  npm ci failed — retrying with npm install...")
            run(["npm", "install", "--no-fund", "--no-audit"], cwd=web)
    else:
        run(["npm", "install", "--no-fund", "--no-audit"], cwd=web)
    run(["npm", "run", "build"], cwd=web)
    dist = web / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("web/dist/index.html missing after build")
    assets = dist / "assets"
    if not assets.is_dir():
        raise SystemExit("web/dist/assets missing after build")
    if not any(assets.glob("index-*.js")) or not any(assets.glob("index-*.css")):
        raise SystemExit("npm run build did not produce hashed index JS/CSS")
    return dist


def standalone_python_asset(ver: dict[str, str], os_name: str) -> str:
    full = ver.get("PYTHON_FULL_VERSION") or "3.12.14"
    tag = ver.get("PYTHON_STANDALONE_TAG") or "20260825"
    if os_name == "windows":
        triple = "x86_64-pc-windows-msvc"
    elif os_name == "linux":
        triple = "x86_64-unknown-linux-gnu"
    elif os_name in {"darwin-arm64", "darwin"}:
        triple = "aarch64-apple-darwin"
    elif os_name == "darwin-x64":
        triple = "x86_64-apple-darwin"
    else:
        raise SystemExit(f"No standalone Python asset for {os_name}")
    return f"cpython-{full}+{tag}-{triple}-install_only.tar.gz"


def find_python_root(extracted: Path, os_name: str) -> Path:
    if os_name == "windows":
        hit = find_file(extracted, ("python.exe",))
        if hit is None:
            raise SystemExit("python.exe not found in standalone archive")
        return hit.parent
    hit = find_file(extracted, ("python3", "python"))
    if hit is None:
        raise SystemExit("python3 not found in standalone archive")
    # install_only: <root>/bin/python3
    if hit.parent.name == "bin":
        return hit.parent.parent
    return hit.parent


def fetch_standalone_python(ver: dict[str, str], os_name: str, dest: Path) -> str:
    asset = standalone_python_asset(ver, os_name)
    tag = ver.get("PYTHON_STANDALONE_TAG") or "20260825"
    repo = ver.get("PYTHON_STANDALONE_REPO") or "astral-sh/python-build-standalone"
    url = f"https://github.com/{repo}/releases/download/{tag}/{asset}"
    with tempfile.TemporaryDirectory(prefix="osm-py-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset
        download(url, archive)
        extracted = tmp_path / "extract"
        extract_archive(archive, extracted)
        root = find_python_root(extracted, os_name)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(root, dest)
    if os_name == "windows":
        exe = dest / "python.exe"
        if not exe.is_file():
            raise SystemExit(f"Missing {exe} after extract")
        print(f"  Bundled Python: {exe} ({exe.stat().st_size / (1024 * 1024):.1f} MB)")
    else:
        exe = dest / "bin" / "python3"
        if not exe.is_file():
            raise SystemExit(f"Missing {exe} after extract")
        exe.chmod(exe.stat().st_mode | 0o111)
        print(f"  Bundled Python: {exe} ({exe.stat().st_size / (1024 * 1024):.1f} MB)")
    return asset


def fetch_opencode(ver: dict[str, str], os_name: str, arch: str, dest_bin: Path) -> str:
    version = ver["OPENCODE_VERSION"]
    repo = ver.get("OPENCODE_REPO") or "anomalyco/opencode"
    asset = opencode_asset(ver, os_name, arch)
    url = f"https://github.com/{repo}/releases/download/v{version}/{asset}"
    dest_bin.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="osm-oc-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset
        download(url, archive)
        extracted = tmp_path / "extract"
        extract_archive(archive, extracted)
        names = ("opencode.exe", "opencode") if os_name == "windows" else ("opencode", "opencode.exe")
        binary = find_file(extracted, names)
        if binary is None:
            raise SystemExit(f"OpenCode binary not found in {asset}")
        target = dest_bin / binary.name
        shutil.copy2(binary, target)
        if os_name != "windows":
            target.chmod(target.stat().st_mode | 0o111)
        print(f"  OpenCode binary: {target} ({target.stat().st_size / (1024 * 1024):.1f} MB)")
    return asset


def stage_app(root: Path, payload: Path) -> None:
    payload.mkdir(parents=True, exist_ok=True)
    for name in COPY_FILES:
        src = root / name
        if src.is_file():
            shutil.copy2(src, payload / name)
            print(f"  + {name}")
    for name in COPY_DIRS:
        src = root / name
        if not src.is_dir():
            print(f"  skip missing: {name}")
            continue
        copy_tree(src, payload / name)
        print(f"  + {name}/")
    # Launchers at zip root so Windows users can double-click.
    scripts = root / "scripts"
    for launcher in (
        "install.bat",
        "install.sh",
        "install-opencode.bat",
        "install-opencode.sh",
        "start.bat",
        "start.sh",
        "start-backend.bat",
        "start-backend.sh",
        "start-frontend.bat",
        "start-frontend.sh",
    ):
        src = scripts / launcher
        if src.is_file():
            shutil.copy2(src, payload / launcher)
            print(f"  + {launcher} (zip root)")


def wheel_for_pack(name: str, pack: str) -> bool:
    n = name.lower()
    if "py3-none-any" in n or "py2.py3-none-any" in n:
        return True
    if pack == "windows":
        return "win_amd64" in n or "win32" in n
    if pack == "linux":
        return "manylinux" in n or "linux_x86_64" in n or "musllinux" in n
    if pack == "darwin":
        return "macosx" in n
    return False


def copy_wheels_for_pack(src: Path, dest: Path, pack: str) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    if not src.is_dir():
        return 0
    for whl in src.iterdir():
        if whl.is_file() and wheel_for_pack(whl.name, pack):
            shutil.copy2(whl, dest / whl.name)
            count += 1
    return count


def copy_spa(spa: Path, payload: Path) -> None:
    dest = payload / "web" / "dist"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for item in spa.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    pkg = spa.parent / "package.json"
    if pkg.is_file():
        shutil.copy2(pkg, payload / "web" / "package.json")
    (payload / "web" / "DIST_SPA.txt").write_text(
        "OpenCode Session Manager dashboard SPA (production build).\n"
        "Served by the manager at http://127.0.0.1:8080/jobs\n"
        "No Node required at runtime.\n",
        encoding="utf-8",
    )
    files = list(dest.rglob("*"))
    print(f"  + web/dist ({sum(1 for f in files if f.is_file())} files)")
    if (payload / "web" / "node_modules").exists():
        raise SystemExit("FAIL: web/node_modules must not be staged")


def write_zip(source: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in source.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(source))
    print(f"  Zip: {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build OSM offline distribution")
    parser.add_argument("--root", default="", help="Repo root (default: parent of packaging/)")
    parser.add_argument("--out-dir", default="", help="Output directory for zip (default: <root>/dist)")
    parser.add_argument("--dist-name", default="", help="Payload / zip name")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Write vendor/ + build web/dist into the repo (no zip)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    ver = read_versions(root / "packaging" / "versions.env")
    os_name, arch = host_platform()
    version = os.environ.get("OSM_PRODUCT_VERSION") or product_version(root)
    prefix = args.dist_name or f"opencode-manager-{version}"
    reqs = runtime_requirements(root)
    wheel_versions = [
        x.strip()
        for x in (ver.get("PYTHON_WHEEL_VERSIONS") or "3.12").split(",")
        if x.strip()
    ]
    if os_name == "windows":
        host_pack = "windows"
    elif os_name == "linux":
        host_pack = "linux"
    else:
        host_pack = "darwin"

    print("========================================")
    print("  OpenCode Session Manager - offline dist")
    print("========================================")
    print(f"Repo     : {root}")
    print(f"Host     : {os_name}-{arch}")
    print(f"Zips     : windows-x64, linux-x64, darwin (arm64+x64)")
    print(f"Version  : {version}")
    print(f"OpenCode : {ver.get('OPENCODE_VERSION')}")
    print(f"Wheels   : {', '.join(wheel_versions)}")
    print()

    print("Step 1: Building dashboard SPA...")
    spa = build_spa(root)

    if args.in_place:
        pack_ids = (host_pack,)
        cache = root / "vendor"
        print(f"\nIn-place: host pack only ({host_pack})")
    else:
        pack_ids = ("windows", "linux", "darwin")
        cache = (Path(args.out_dir).resolve() if args.out_dir else root / "dist") / "_vendor_cache"

    wheel_src = cache / "python-wheels" if not args.in_place else cache / "python-wheels"
    print("\nStep 2: Downloading Python wheels...")
    wheel_plats = []
    for pid in pack_ids:
        wheel_plats.extend(list(PACKS[pid]["wheels"]))  # type: ignore[arg-type]
    pip_download_wheels(reqs, wheel_src, wheel_versions, wheel_plats)
    supported = supported_python_versions(wheel_src, wheel_versions)

    print("\nStep 3: Fetching bundled CPython...")
    py_assets: list[str] = []
    pythons_needed = []
    for pid in pack_ids:
        pythons_needed.extend(list(PACKS[pid]["pythons"]))  # type: ignore[arg-type]
    py_root = cache / "python"
    for pack_os, dest_name in pythons_needed:
        py_assets.append(fetch_standalone_python(ver, pack_os, py_root / dest_name))

    print("\nStep 4: Fetching OpenCode CLI...")
    oc_assets: list[str] = []
    oc_needed = []
    for pid in pack_ids:
        oc_needed.extend(list(PACKS[pid]["opencodes"]))  # type: ignore[arg-type]
    oc_root = cache / "bin"
    for pack_os, pack_arch, dest_name in oc_needed:
        oc_assets.append(fetch_opencode(ver, pack_os, pack_arch, oc_root / dest_name))

    if args.in_place:
        (cache / "SUPPORTED_PYTHON.txt").write_text(
            "# Python minors with a complete offline wheel set\n" + "\n".join(supported) + "\n",
            encoding="utf-8",
        )
        (cache / "requirements.txt").write_text("\n".join(reqs) + "\n", encoding="utf-8")
        (cache / "DIST_VERSION.txt").write_text(
            f"in-place vendor pack={host_pack}\nProductVersion={version}\n"
            f"OpenCode={ver.get('OPENCODE_VERSION')}\n"
            f"OpenCodeAsset={','.join(oc_assets)}\n"
            f"PythonStandalone={','.join(py_assets)}\n"
            f"PythonWheels={','.join(supported)}\n",
            encoding="utf-8",
        )
        print("\n[OK] In-place vendor ready.")
        print(f"  {wheel_src}")
        print(f"  {py_root}")
        print(f"  {oc_root}")
        print("  web/dist")
        print("Run scripts/install.sh (or scripts\\install.bat).")
        return 0

    out_dir = Path(args.out_dir).resolve() if args.out_dir else root / "dist"
    written: list[Path] = []
    for pid in pack_ids:
        spec = PACKS[pid]
        suffix = str(spec["suffix"])
        name = f"{prefix}-{suffix}"
        payload = out_dir / "stage" / name
        if payload.exists():
            shutil.rmtree(payload)
        print(f"\nStep 5: Staging {name}...")
        stage_app(root, payload)
        copy_spa(spa, payload)
        vendor = payload / "vendor"
        n_wheels = copy_wheels_for_pack(wheel_src, vendor / "python-wheels", pid)
        print(f"  wheels copied for {pid}: {n_wheels}")
        for pack_os, dest_name in spec["pythons"]:  # type: ignore[misc]
            src_py = py_root / dest_name
            dest_py = vendor / "python" / dest_name
            if dest_py.exists():
                shutil.rmtree(dest_py)
            shutil.copytree(src_py, dest_py)
            print(f"  + python/{dest_name}")
        for _os, _arch, dest_name in spec["opencodes"]:  # type: ignore[misc]
            src_oc = oc_root / dest_name
            dest_oc = vendor / "bin" / dest_name
            if dest_oc.exists():
                shutil.rmtree(dest_oc)
            shutil.copytree(src_oc, dest_oc)
            print(f"  + bin/{dest_name}")
        (vendor / "SUPPORTED_PYTHON.txt").write_text(
            "# Python minors with a complete offline wheel set\n" + "\n".join(supported) + "\n",
            encoding="utf-8",
        )
        (vendor / "requirements.txt").write_text("\n".join(reqs) + "\n", encoding="utf-8")
        (payload / "DIST_VERSION.txt").write_text(
            "opencode-manager offline distribution\n"
            f"ProductVersion={version}\n"
            f"Pack={pid}\n"
            f"DistName={name}\n"
            f"OpenCode={ver.get('OPENCODE_VERSION')}\n"
            f"PythonWheels={','.join(supported)}\n"
            "Bundled CPython creates .venv. No Node.\n",
            encoding="utf-8",
        )
        (payload / "VERSION").write_text(version + "\n", encoding="utf-8")
        zip_path = out_dir / f"{name}.zip"
        print(f"  Writing {zip_path.name}...")
        write_zip(payload, zip_path)
        written.append(zip_path)

    print("\n[OK] Offline zips ready.")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
