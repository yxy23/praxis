#!/usr/bin/env bash
# setup-hooks.sh — install git hooks for this repo
#
# Symlinks scripts/pre-commit-hook.sh to .git/hooks/pre-commit so the safety
# check runs on every commit. Run once after cloning.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_TARGET="$REPO_ROOT/.git/hooks/pre-commit"

if [[ -e "$HOOK_TARGET" && ! -L "$HOOK_TARGET" ]]; then
    echo "Existing non-symlink hook at $HOOK_TARGET — aborting (back it up first)" >&2
    exit 1
fi

ln -sf "../../scripts/pre-commit-hook.sh" "$HOOK_TARGET"
chmod +x "$REPO_ROOT/scripts/pre-commit-hook.sh"

echo "✓ Installed pre-commit hook"
echo "  Symlink: $HOOK_TARGET → ../../scripts/pre-commit-hook.sh"
echo ""
echo "Test it:"
echo "  Stage an identifier listed in your local leak-pattern file to test the hook."
echo "  (should be blocked)"
echo "  git restore --staged README.md && git checkout README.md"
