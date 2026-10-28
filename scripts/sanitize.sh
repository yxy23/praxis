#!/usr/bin/env bash
# Apply a separately maintained private sed rule file to stdin.
# A missing or empty map is an error, never an unchanged public projection.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RULES="${PRAXIS_SANITIZE_RULES:-$SCRIPT_DIR/.sanitize-rules.local}"
if [[ ! -s "$RULES" ]] || ! grep -qE '^[[:space:]]*[^#[:space:]]' "$RULES"; then
    echo "A nonempty private sanitization rule file is required." >&2
    exit 1
fi
sed -f "$RULES"
