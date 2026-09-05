#!/usr/bin/env bash
# aMIR-mini — install as a systemd service (backend only).
# Does not change the two-window exe.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$HERE/pyproject.toml" ]]; then
  ROOT="$HERE"
elif [[ -f "$HERE/../pyproject.toml" ]]; then
  ROOT="$(cd "$HERE/.." && pwd)"
else
  ROOT="$HERE"
fi
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -m opencode_manager.service_install install --root "$ROOT"
fi
exec python3 -m opencode_manager.service_install install --root "$ROOT"
