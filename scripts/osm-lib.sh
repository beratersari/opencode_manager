#!/usr/bin/env bash
# Shared helpers for install/start scripts. Sourced, not executed.

osm_os_tag() {
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      case "$(uname -m 2>/dev/null)" in
        arm64|aarch64) echo darwin-arm64 ;;
        *) echo darwin-x64 ;;
      esac
      ;;
    Linux) echo linux ;;
    *) echo unknown ;;
  esac
}

osm_bundled_python() {
  local root="$1"
  local tag
  tag="$(osm_os_tag)"
  case "$tag" in
    linux) echo "$root/vendor/python/linux/bin/python3" ;;
    darwin-arm64) echo "$root/vendor/python/darwin-arm64/bin/python3" ;;
    darwin-x64) echo "$root/vendor/python/darwin-x64/bin/python3" ;;
    *) echo "" ;;
  esac
}

osm_require_bundled_python() {
  local root="$1"
  local path
  path="$(osm_bundled_python "$root")"
  if [[ -z "$path" || ! -f "$path" ]]; then
    echo "[ERROR] No bundled Python for $(uname -s) $(uname -m)."
    echo "Looked for: ${path:-<unknown>}"
    echo "This zip must include vendor/python/linux, darwin-arm64, or darwin-x64."
    echo "Download a current Offline Distribution artifact, or rebuild:"
    echo "  python3 packaging/build_dist.py --in-place"
    return 1
  fi
  if [[ ! -x "$path" ]]; then
    chmod +x "$path" 2>/dev/null || true
  fi
  if ! "$path" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >/dev/null 2>&1; then
    echo "[ERROR] Bundled Python cannot run on this machine (wrong OS/arch or exec format):"
    echo "  $path"
    echo "  $(uname -s) $(uname -m)"
    return 1
  fi
  echo "$path"
}

# Zip extractors (python -m zipfile, some GUIs) drop Unix +x. Restore launchers.
osm_chmod_launchers() {
  local root="$1"
  local f
  for f in \
    install.sh install-opencode.sh start.sh start-backend.sh start-frontend.sh \
    install-service.sh uninstall-service.sh \
    scripts/install.sh scripts/install-opencode.sh scripts/start.sh \
    scripts/start-backend.sh scripts/start-frontend.sh scripts/osm-lib.sh \
    scripts/install-service.sh scripts/uninstall-service.sh
  do
    if [[ -f "$root/$f" ]]; then
      chmod +x "$root/$f" 2>/dev/null || true
    fi
  done
}

# True when the overlay still names the Linux default (shipped zip).
osm_local_yaml_is_linux_default() {
  grep -qE '^[[:space:]]*data_dir:[[:space:]]*["'"'"']?/var/lib/osm["'"'"']?[[:space:]]*$' "$1"
}

# Linux default is /var/lib/osm (needs root once). Combined zip may only
# have settings.local.linux.yaml; promote it. If the overlay is missing
# or still points at /var/lib/osm and that path is not writable, write
# $XDG_DATA_HOME/osm or ~/.local/share/osm so ./start.sh works.
# A custom data_dir in settings.local.yaml is left alone.
osm_ensure_linux_data_dir() {
  local root="$1"
  local default_dir="/var/lib/osm"
  local local_yaml="$root/settings.local.yaml"

  if [[ ! -f "$local_yaml" && -f "$root/settings.local.linux.yaml" ]]; then
    cp "$root/settings.local.linux.yaml" "$local_yaml"
    echo "[OK] settings.local.yaml from settings.local.linux.yaml"
  fi

  if [[ -f "$local_yaml" ]] && ! osm_local_yaml_is_linux_default "$local_yaml"; then
    echo "[OK] settings.local.yaml present (data_dir overlay left as-is)"
    return 0
  fi

  if mkdir -p "$default_dir" 2>/dev/null && [[ -w "$default_dir" ]]; then
    echo "[OK] data_dir $default_dir"
    return 0
  fi

  if command -v sudo >/dev/null 2>&1 && sudo -n mkdir -p "$default_dir" 2>/dev/null; then
    sudo -n chown "$(id -u):$(id -g)" "$default_dir" 2>/dev/null || true
    if [[ -w "$default_dir" ]]; then
      echo "[OK] created $default_dir (sudo)"
      return 0
    fi
  fi

  local fallback="${XDG_DATA_HOME:-$HOME/.local/share}/osm"
  mkdir -p "$fallback"
  cat > "$local_yaml" <<EOF
# /var/lib/osm is not writable without root.
# To use the default instead:
#   sudo mkdir -p /var/lib/osm && sudo chown \$USER /var/lib/osm
#   rm settings.local.yaml
data_dir: $fallback
EOF
  echo "[WARNING] Cannot write $default_dir (need root once)."
  echo "          Wrote settings.local.yaml -> $fallback"
}

# Rebuild web/dist from web/src when local Vite exists (dev tree).
# Offline zips have no web/node_modules — they keep the shipped dist.
osm_refresh_web_dist() {
  local root="$1"
  local vite="$root/web/node_modules/.bin/vite"
  local src="$root/web/src"
  local dist="$root/web/dist/index.html"
  if [[ ! -e "$vite" ]]; then
    if [[ -f "$dist" ]]; then
      echo "[OK] web/dist is the shipped build (no local Vite)"
    fi
    return 0
  fi
  if [[ ! -d "$src" ]]; then
    return 0
  fi
  if [[ -f "$dist" ]] && ! find "$src" -type f -newer "$dist" -print -quit | grep -q .; then
    echo "[OK] web/dist is current"
    return 0
  fi
  echo "Rebuilding web/dist from web/src ..."
  (cd "$root/web" && "$vite" build)
}
