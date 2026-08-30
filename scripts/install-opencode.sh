#!/usr/bin/env bash
# OpenCode Session Manager — install OpenCode CLI (offline).
# Detects a previous user install, deletes it, copies vendor/bin from scratch.
# Does not install Python / the dashboard. Use install.sh for that.
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

echo "========================================"
echo "  OpenCode Session Manager"
echo "  OpenCode CLI install (offline, from scratch)"
echo "========================================"
echo
echo "Project : $ROOT"
echo "Target  : $HOME/.opencode"
echo

if [[ ! -f "$ROOT/vendor/bin/opencode" && ! -f "$ROOT/vendor/bin/opencode.exe" ]]; then
  echo "[ERROR] vendor/bin/opencode is missing."
  echo "Use the CI zip, or run: python3 packaging/build_dist.py --in-place"
  exit 1
fi

PY=""
if [[ -x "$ROOT/vendor/python/linux/bin/python3" ]]; then
  PY="$ROOT/vendor/python/linux/bin/python3"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
fi
if [[ -z "$PY" ]]; then
  echo "[ERROR] Bundled Python missing (vendor/python/linux/bin/python3)."
  echo "Run ./install.sh from the CI zip, or python3 packaging/build_dist.py --in-place."
  exit 1
fi

echo "Python  : $PY"
echo
"$PY" "$ROOT/scripts/install_opencode.py" --root "$ROOT"

echo
echo "New shells pick up \$HOME/.opencode/bin (start-backend.sh also prepends it)."
echo "Then: scripts/start-backend.sh"
echo
