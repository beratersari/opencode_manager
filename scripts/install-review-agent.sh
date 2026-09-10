#!/usr/bin/env bash
# Copy code-reviewer + skills into ~/.opencode.
# Does not replace the OpenCode CLI. Use install-opencode.sh for that.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$HERE/opencoderman/agents/code-reviewer.md" ]]; then
  SRC="$HERE/opencoderman"
elif [[ -f "$HERE/../opencoderman/agents/code-reviewer.md" ]]; then
  SRC="$(cd "$HERE/.." && pwd)/opencoderman"
else
  echo "[ERROR] opencoderman/agents/code-reviewer.md is missing."
  exit 1
fi

DEST="${HOME}/.opencode"
mkdir -p "$DEST/agents"
cp "$SRC/agents/code-reviewer.md" "$DEST/agents/code-reviewer.md"
cp "$SRC/agents/code-reviewer.md" "$DEST/agents/gitlab-reviewer.md"
mkdir -p "$DEST/skills"
cp -R "$SRC/skills/." "$DEST/skills/"
echo "[OK] Copied code-reviewer and skills to $DEST"
