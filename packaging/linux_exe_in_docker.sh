#!/usr/bin/env bash
# Build one Linux onefile zip inside ubuntu:<version>.
# Usage: packaging/linux_exe_in_docker.sh 22.04
set -euo pipefail
UBUNTU="${1:?ubuntu version, e.g. 22.04}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUFFIX="linux-ubuntu-${UBUNTU}-x64"
if [ ! -f "${ROOT}/web/dist/index.html" ]; then
  echo "web/dist/index.html missing; build the dashboard first" >&2
  exit 1
fi
mkdir -p "${ROOT}/dist"

if [ "${UBUNTU}" = "18.04" ]; then
  OSM_LINUX_SUFFIX="${SUFFIX}" bash "${ROOT}/packaging/linux_exe_portable.sh"
  exit 0
fi

UV_VER="${OSM_UV_VERSION:-0.8.22}"
UV_DIR="$(mktemp -d)"
trap 'rm -rf "${UV_DIR}"' EXIT
curl -fsSL \
  "https://github.com/astral-sh/uv/releases/download/${UV_VER}/uv-x86_64-unknown-linux-musl.tar.gz" \
  -o "${UV_DIR}/uv.tgz"
tar -xzf "${UV_DIR}/uv.tgz" -C "${UV_DIR}"
UV_BIN="$(find "${UV_DIR}" -type f -name uv | head -n 1)"
if [ -z "${UV_BIN}" ]; then
  echo "failed to unpack musl uv ${UV_VER}" >&2
  exit 1
fi
chmod +x "${UV_BIN}"

docker run --rm \
  -e DEBIAN_FRONTEND=noninteractive \
  -e OSM_LINUX_SUFFIX="${SUFFIX}" \
  -e OSM_PRODUCT_VERSION="${OSM_PRODUCT_VERSION:-}" \
  -v "${ROOT}:/src" \
  -v "${UV_BIN}:/usr/local/bin/uv:ro" \
  -w /src \
  "ubuntu:${UBUNTU}" \
  bash /src/packaging/linux_exe_container.sh
