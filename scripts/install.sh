#!/usr/bin/env bash
# OpenCode Session Manager — install manager (offline).
# Python venv + wheels + prebuilt dashboard. Does NOT install OpenCode.
# Use install-opencode.sh for the CLI.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$HERE/pyproject.toml" ]]; then
  ROOT="$HERE"
elif [[ -f "$HERE/../pyproject.toml" ]]; then
  ROOT="$(cd "$HERE/.." && pwd)"
else
  echo "[ERROR] Cannot find repo root (pyproject.toml)."
  exit 1
fi
VENV_DIR="$ROOT/.venv"
WHEELS="$ROOT/vendor/python-wheels"
WEB_DIST="$ROOT/web/dist"
cd "$ROOT"

echo "========================================"
echo "  OpenCode Session Manager"
echo "  Install (offline)"
echo "========================================"
echo
echo "Project : $ROOT"
echo

if [[ ! -d "$WHEELS" ]]; then
  echo "[ERROR] vendor/python-wheels is missing."
  echo "This installer is offline-only. Use the CI zip, or on a machine with network:"
  echo "  python3 packaging/build_dist.py --in-place"
  exit 1
fi
if [[ ! -f "$WEB_DIST/index.html" ]]; then
  echo "[ERROR] Missing $WEB_DIST/index.html"
  echo "This installer is offline-only and does not run npm."
  echo "Use the CI zip, or on a machine with network:"
  echo "  python3 packaging/build_dist.py --in-place"
  exit 1
fi

PY=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [[ -z "$PY" ]]; then
  echo "[ERROR] Python 3 is not installed or not on PATH."
  echo "Install a supported Python (see vendor/SUPPORTED_PYTHON.txt)."
  exit 1
fi

PYTHON_VERSION="$("$PY" --version 2>&1)"
echo "[OK] $PYTHON_VERSION"
if ! "$PY" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)"; then
  echo "[ERROR] Python 3.11 or newer is required."
  echo "Found: $PYTHON_VERSION"
  exit 1
fi

if [[ -f "$ROOT/vendor/SUPPORTED_PYTHON.txt" ]]; then
  if ! "$PY" -c "
import sys, pathlib
p = pathlib.Path(r'''$ROOT''') / 'vendor' / 'SUPPORTED_PYTHON.txt'
lines = [l.strip() for l in p.read_text(encoding='utf-8', errors='ignore').splitlines() if l.strip() and not l.strip().startswith('#')]
ver = f'{sys.version_info.major}.{sys.version_info.minor}'
print('Supported in this package:', ', '.join(lines))
print('Your Python minor:', ver)
raise SystemExit(0 if (not lines or ver in lines) else 1)
"; then
    echo "[ERROR] Your Python is not in vendor/SUPPORTED_PYTHON.txt."
    exit 1
  fi
fi

if command -v git >/dev/null 2>&1; then
  echo "[OK] git found"
else
  echo "[WARNING] git is not on PATH. Clone jobs will fail until Git is installed."
fi

echo
echo "Step 1: Python virtual environment..."
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PY" -m venv "$VENV_DIR"
  echo "[OK] Created $VENV_DIR"
else
  echo "[OK] Using existing $VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"

echo
echo "Step 2: Installing packages from vendor/python-wheels (no network)..."
"$VENV_PY" -m pip install --upgrade pip --no-index --find-links="$WHEELS"
"$VENV_PY" -m pip install --no-index --find-links="$WHEELS" -e .
echo "[OK] Manager installed into .venv from local wheels"

echo
echo "Step 3: Dashboard SPA..."
if [[ -d "$WEB_DIST/assets" ]]; then
  echo "[OK] Prebuilt dashboard SPA present: web/dist"
else
  echo "[WARNING] web/dist/assets missing — UI may not load"
fi

echo
echo "========================================"
echo "  Manager install complete"
echo "========================================"
echo
echo "OpenCode is separate:"
echo "  scripts/install-opencode.sh"
echo "Then:"
echo "  scripts/start-backend.sh      API + SPA  http://127.0.0.1:8080/"
echo "  scripts/start-frontend.sh     SPA proxy  http://127.0.0.1:5173/"
echo "  scripts/start.sh              both"
echo
