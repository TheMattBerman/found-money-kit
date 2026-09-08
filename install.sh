#!/usr/bin/env bash
# install.sh - register the Found Money skill with Claude Code or Codex.
# Usage:
#   ./install.sh claude
#   ./install.sh codex
#   ./install.sh --dir <path>
# The Python package, fixtures, and CLI stay in this repo. Restart the agent
# after install so it can discover /found-money.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_DIR/skills"

usage() {
  echo "Usage: ./install.sh [codex|claude|--dir <path>]"
  echo "  claude        install into ~/.claude/skills/"
  echo "  codex         install into ~/.codex/skills/"
  echo "  --dir <path>  install into an explicit skills directory"
}

case "${1:-}" in
  codex) DEST="${HOME}/.codex/skills" ;;
  claude) DEST="${HOME}/.claude/skills" ;;
  --dir)
    DEST="${2:-}"
    if [ -z "$DEST" ]; then
      echo "Error: --dir requires a path."
      usage
      exit 1
    fi
    ;;
  *)
    usage
    exit 1
    ;;
esac

if [ ! -d "$SRC/found-money" ]; then
  echo "Error: skills/found-money not found at $SRC/found-money"
  exit 1
fi

mkdir -p "$DEST"
rm -rf "$DEST/found-money"
cp -R "$SRC/found-money" "$DEST/found-money"

echo "Installed Found Money skill into $DEST/found-money"
echo
echo "Restart your agent to pick up /found-money."
echo "The CLI, fixtures, and Recovery Room live in this repo. Run from the repo root."
