#!/usr/bin/env bash
# OpenCode Session Manager — start FRONTEND only (SPA proxy on :5173).
# Requires: prebuilt web/dist, backend already running. No Node/Vite.
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

FRONTEND_HOST="${OSM_FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${OSM_FRONTEND_PORT:-5173}"
BACKEND_URL="${OSM_BACKEND_URL:-http://127.0.0.1:8080}"
WEB_DIST="$ROOT/web/dist"
VENV_PY="$ROOT/.venv/bin/python"
OSM_PY=""
if [[ -x "$VENV_PY" ]]; then
  OSM_PY="$VENV_PY"
fi

echo "========================================"
echo "  OpenCode Session Manager - Frontend"
echo "========================================"
echo "Project  : $ROOT"
echo "UI       : http://${FRONTEND_HOST}:${FRONTEND_PORT}/  (open http://127.0.0.1:${FRONTEND_PORT}/ )"
echo "Proxies  : /api and /ws  ->  ${BACKEND_URL}"
echo

if [[ -z "$OSM_PY" ]]; then
  echo "[ERROR] .venv is missing."
  echo "Run scripts/install.sh first. It creates .venv from vendor/python/linux/bin/python3."
  exit 1
fi
echo "Python   : $OSM_PY"

if [[ ! -f "$WEB_DIST/index.html" ]]; then
  echo "[ERROR] Missing $WEB_DIST/index.html"
  echo "Use a CI zip that includes web/dist, or run python3 packaging/build_dist.py --in-place."
  exit 1
fi

echo "Checking backend at ${BACKEND_URL}/api/meta ..."
if command -v curl >/dev/null 2>&1; then
  if ! curl -sf --max-time 5 "${BACKEND_URL}/api/meta" >/dev/null; then
    echo "[ERROR] Backend is not reachable at ${BACKEND_URL}"
    echo "Start it first:  scripts/start-backend.sh"
    exit 1
  fi
else
  if ! "$OSM_PY" -c "import urllib.request; urllib.request.urlopen('${BACKEND_URL}/api/meta', timeout=5)" >/dev/null 2>&1; then
    echo "[ERROR] Backend is not reachable at ${BACKEND_URL}"
    echo "Start it first:  scripts/start-backend.sh"
    exit 1
  fi
fi
echo "[OK] Backend is reachable."

echo "Starting SPA proxy (Ctrl+C to stop)..."
echo "Open: http://127.0.0.1:${FRONTEND_PORT}/"
exec "$OSM_PY" -m opencode_manager.dashboard.frontend_proxy \
  --dist "$WEB_DIST" \
  --backend "$BACKEND_URL" \
  --host "$FRONTEND_HOST" \
  --port "$FRONTEND_PORT"
