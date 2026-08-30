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

BUNDLED_PY="$ROOT/vendor/python/linux/bin/python3"
if [[ ! -x "$BUNDLED_PY" ]]; then
  echo "[ERROR] Missing $BUNDLED_PY"
  echo "The zip must include a bundled Python. Use the CI zip, or:"
  echo "  python3 packaging/build_dist.py --in-place"
  exit 1
fi
PYTHON_VERSION="$("$BUNDLED_PY" --version 2>&1)"
echo "[OK] Bundled $PYTHON_VERSION"
echo "     $BUNDLED_PY"

if command -v git >/dev/null 2>&1; then
  echo "[OK] git found"
else
  echo "[WARNING] git is not on PATH. Clone jobs will fail until Git is installed."
fi

echo
echo "Step 1: Python virtual environment from bundled python..."
if [[ -e "$VENV_DIR" ]]; then
  echo "Removing existing .venv so it matches the bundled interpreter..."
  rm -rf "$VENV_DIR"
fi
"$BUNDLED_PY" -m venv "$VENV_DIR"
echo "[OK] Created $VENV_DIR"
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
