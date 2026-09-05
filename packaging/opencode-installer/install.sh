#!/usr/bin/env bash
# aMIR-mini — offline OpenCode 1.18.10 installer (Linux).
# Wipes $HOME/.opencode and copies vendor/bin/linux/opencode. No network.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINNED="1.18.10"
TARGET="${HOME}/.opencode"
SRC=""
if [[ -f "$HERE/vendor/bin/linux/opencode" ]]; then
  SRC="$HERE/vendor/bin/linux/opencode"
elif [[ -f "$HERE/vendor/bin/opencode" ]]; then
  SRC="$HERE/vendor/bin/opencode"
fi

echo "========================================"
echo "  aMIR-mini OpenCode installer"
echo "  pinned version ${PINNED}"
echo "========================================"
echo

if [[ -f "$HERE/OPENCODE_VERSION.txt" ]]; then
  GOT="$(tr -d '[:space:]' < "$HERE/OPENCODE_VERSION.txt")"
  if [[ "$GOT" != "$PINNED" ]]; then
    echo "[ERROR] This zip is not OpenCode ${PINNED} (found ${GOT})."
    exit 1
  fi
fi

if [[ -z "$SRC" ]]; then
  echo "[ERROR] vendor/bin/linux/opencode is missing."
  exit 1
fi

echo "Source : $SRC"
echo "Target : $TARGET"
echo

if [[ -e "$TARGET" ]]; then
  echo "Removing previous $TARGET ..."
  rm -rf "$TARGET"
  if [[ -e "$TARGET" ]]; then
    echo "[ERROR] Could not delete $TARGET"
    exit 1
  fi
fi

mkdir -p "$TARGET/bin"
cp -f "$SRC" "$TARGET/bin/opencode"
chmod +x "$TARGET/bin/opencode"
cat > "$TARGET/opencode.json" <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "plugin": []
}
EOF

echo "[OK] OpenCode ${PINNED} installed to $TARGET/bin/opencode"
echo "Add to PATH if needed:  export PATH=\"\$HOME/.opencode/bin:\$PATH\""
echo
