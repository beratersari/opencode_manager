#!/usr/bin/env bash
# OpenCode Session Manager — start BOTH backend (:8080) and frontend (:5173).
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
LAUNCH="$HERE"
if [[ -f "$ROOT/scripts/start-backend.sh" ]]; then
  LAUNCH="$ROOT/scripts"
fi
DASH_PORT="${OSM_PORT:-8080}"
BACKEND_PID=""

cleanup() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo
    echo "Stopping backend (pid $BACKEND_PID)..."
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "========================================"
echo "  OpenCode Session Manager - Start all"
echo "========================================"
echo
echo "  Backend  : http://127.0.0.1:${DASH_PORT}/   (API + built SPA)"
echo "  Frontend : http://127.0.0.1:5173/           (SPA proxy, no Node)"
echo

echo "=== [1/2] Backend ==="
"$LAUNCH/start-backend.sh" &
BACKEND_PID=$!

echo "Waiting for API http://127.0.0.1:${DASH_PORT}/api/meta ..."
ready=0
for _ in $(seq 1 45); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID" || true
    echo "[ERROR] Backend failed to start. See output above."
    BACKEND_PID=""
    exit 1
  fi
  if command -v curl >/dev/null 2>&1; then
    if curl -sf --max-time 2 "http://127.0.0.1:${DASH_PORT}/api/meta" >/dev/null; then
      ready=1
      break
    fi
  elif python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${DASH_PORT}/api/meta', timeout=2)" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != "1" ]]; then
  echo "[ERROR] Backend did not become ready on port ${DASH_PORT}."
  exit 1
fi
echo "[OK] Backend is up."

echo
echo "=== [2/2] Frontend ==="
"$LAUNCH/start-frontend.sh"
