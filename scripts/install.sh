#!/usr/bin/env bash
# aMIR-mini — install manager (offline).
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
echo "  aMIR-mini"
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

# shellcheck source=osm-lib.sh
. "$ROOT/scripts/osm-lib.sh"
osm_chmod_launchers "$ROOT"
BUNDLED_PY="$(osm_require_bundled_python "$ROOT")" || exit 1
PYTHON_VERSION="$("$BUNDLED_PY" --version 2>&1)"
echo "[OK] Bundled $PYTHON_VERSION ($(osm_os_tag))"
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
if ! "$VENV_PY" -m pip install --no-index --find-links="$WHEELS" -e .; then
  echo "[ERROR] Offline package install failed."
  echo "Wheels must match the bundled interpreter. Need PyYAML + pydantic-core for this OS."
  echo "Present yaml / pydantic-core wheels:"
  ls -1 "$WHEELS"/*[Yy][Aa][Mm][Ll]* "$WHEELS"/*pydantic_core* 2>/dev/null || true
  exit 1
fi
echo "[OK] Manager installed into .venv from local wheels"

echo
echo "Step 3: Dashboard SPA..."
if [[ -d "$WEB_DIST/assets" ]]; then
  echo "[OK] Prebuilt dashboard SPA present: web/dist"
else
  echo "[WARNING] web/dist/assets missing — UI may not load"
fi

if [[ "$(osm_os_tag)" == "linux" ]]; then
  echo
  echo "Step 4: data_dir..."
  osm_ensure_linux_data_dir "$ROOT"
fi

echo
echo "========================================"
echo "  Manager install complete"
echo "========================================"
echo
echo "OpenCode is separate:"
echo "  scripts/install-opencode.sh"
echo "Then:"
echo "  scripts/start-backend.sh      API + SPA  http://127.0.0.1:4096/"
echo "  scripts/start-frontend.sh     SPA proxy  http://127.0.0.1:5173/"
echo "  scripts/start.sh              both"
echo
