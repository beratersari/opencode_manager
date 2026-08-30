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
# shellcheck source=osm-lib.sh
. "$ROOT/scripts/osm-lib.sh"

echo "========================================"
echo "  OpenCode Session Manager"
echo "  OpenCode CLI install (offline, from scratch)"
echo "========================================"
echo
echo "Project : $ROOT"
echo "Target  : $HOME/.opencode"
echo

if [[ ! -f "$ROOT/vendor/bin/$(osm_os_tag)/opencode" && ! -f "$ROOT/vendor/bin/opencode" ]]; then
  echo "[ERROR] vendor/bin/$(osm_os_tag)/opencode is missing."
  echo "Use a current CI zip (macOS needs darwin-arm64 or darwin-x64), or:"
  echo "  python3 packaging/build_dist.py --in-place"
  exit 1
fi

PY="$(osm_require_bundled_python "$ROOT")" || exit 1

echo "Python  : $PY"
echo
"$PY" "$ROOT/scripts/install_opencode.py" --root "$ROOT"

echo
echo "New shells pick up \$HOME/.opencode/bin (start-backend.sh also prepends it)."
echo "Then: scripts/start-backend.sh"
echo
