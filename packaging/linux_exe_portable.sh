#!/usr/bin/env bash
# Ubuntu 18.04 apt is gone. Build a glibc-2.17 onefile on the host
# using uv's standalone CPython so the zip still runs on 18.04.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUFFIX="${OSM_LINUX_SUFFIX:-linux-ubuntu-18.04-x64}"
if [ ! -f "${ROOT}/web/dist/index.html" ]; then
  echo "web/dist/index.html missing; build the dashboard first" >&2
  exit 1
fi
export PATH="${HOME}/.local/bin:/usr/local/bin:${PATH}"
if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
cd "${ROOT}"
uv python install 3.11
VENV=/tmp/osm-portable-venv
uv venv --python 3.11 "${VENV}"
uv pip install --python "${VENV}" -e ".[exe]"
export OSM_LINUX_PORTABLE=1
"${VENV}/bin/python" packaging/build_exe.py \
  --skip-web \
  --suffix "${SUFFIX}" \
  --out-dir "${ROOT}/dist"
