#!/usr/bin/env bash
# Runs inside ubuntu:20.04 / 22.04 / 24.04.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/bin:${HOME}/.local/bin:${PATH}"

if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi

if apt-get update; then
  apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gcc \
    g++ \
    make \
    pkg-config \
    unzip \
    xz-utils \
    zlib1g \
    zlib1g-dev \
    libffi-dev \
    libssl-dev
fi

if [ -d /src/.git ]; then
  git config --global --add safe.directory /src || true
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv missing inside the container" >&2
  exit 1
fi
uv python install 3.11
VENV=/tmp/osm-build-venv
uv venv --python 3.11 "${VENV}"
uv pip install --python "${VENV}" -e ".[exe]"
"${VENV}/bin/python" packaging/build_exe.py \
  --skip-web \
  --suffix "${OSM_LINUX_SUFFIX:?}" \
  --out-dir /src/dist
