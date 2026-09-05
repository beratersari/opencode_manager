#!/usr/bin/env bash
# aMIR-mini — remove the systemd service.
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
  exec "$ROOT/.venv/bin/python" -m opencode_manager.service_install uninstall --root "$ROOT"
fi
exec python3 -m opencode_manager.service_install uninstall --root "$ROOT"
