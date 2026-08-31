#!/usr/bin/env bash
# OpenCode Session Manager — start BACKEND only (API + built SPA on :4096).
# For a separate UI on :5173, use start-frontend.sh after this.
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
cd "$ROOT"

DASH_PORT="${OSM_PORT:-4096}"
FRONTEND_PORT=5173
VENV_PY="$ROOT/.venv/bin/python"
OSM_PY=""
if [[ -x "$VENV_PY" ]]; then
  OSM_PY="$VENV_PY"
fi

export GIT_TERMINAL_PROMPT=0
export PYTHONUNBUFFERED=1
if [[ -d "$HOME/.opencode/bin" ]]; then
  export PATH="$HOME/.opencode/bin:$PATH"
fi
if [[ -d "$ROOT/vendor/bin" ]]; then
  export PATH="$ROOT/vendor/bin:$PATH"
fi

echo "========================================"
echo "  OpenCode Session Manager - Backend"
echo "========================================"
echo "Project : $ROOT"
echo "API+SPA : http://0.0.0.0:${DASH_PORT}/  (open http://127.0.0.1:${DASH_PORT}/jobs )"
echo

if [[ -z "$OSM_PY" ]]; then
  echo "[ERROR] .venv is missing."
  echo "Run scripts/install.sh first. It creates .venv from the bundled Python for this OS."
  exit 1
fi
echo "Python  : $OSM_PY"

if command -v opencode >/dev/null 2>&1; then
  echo "[OK] opencode on PATH"
else
  echo "[WARNING] opencode is not on PATH. Jobs will fail until OpenCode is installed."
  echo "          Run scripts/install-opencode.sh (wipes old CLI, copies vendor/bin)."
fi

if [[ ! -f "$ROOT/web/dist/index.html" ]]; then
  echo "[WARNING] web/dist/index.html missing — API will run but UI on :${DASH_PORT} will not load."
  echo "          Use the CI zip or run python3 packaging/build_dist.py --in-place."
fi

echo "Starting manager (Ctrl+C to stop)..."
exec "$OSM_PY" -m opencode_manager.app
