#!/usr/bin/env bash
# sanitize.sh — strip personal identifiers from a SKILL.md / slash-command file.
# Reads stdin, writes stdout. Used when projecting praxis-private → public praxis,
# or when reviewing a contribution before commit.
#
# The author's real identifiers are NOT hardcoded here — that would leak them into
# the PUBLIC repo (the very thing this script exists to prevent). They live in a
# gitignored local rules file that this script applies:
#
#   scripts/.sanitize-rules.local   — one `sed` script line per rule, e.g.
#       s|/Users/alice/.../Research|<vault-root>|g
#       s|Real Name|<user-name>|g
#       s|colleague@example.edu|<colleague-email>|g
#   (#-comment and blank lines ignored by sed -f.)
#
# CONTACT NAMES: real colleague / staff / contact names cannot be inferred
# automatically — add each one as a rule in the local file. A name with no rule
# leaks into the public repo. The pre-commit hook is the backstop (it loads
# scripts/.leak-patterns.local, also gitignored).
#
# Public users / fresh clones: create scripts/.sanitize-rules.local with your own
# real->placeholder rules. With no local file this script is a pass-through.
#
# Usage:
#   ./sanitize.sh < input.md > sanitized.md
#   cat input.md | ./sanitize.sh > sanitized.md

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_RULES="$SCRIPT_DIR/.sanitize-rules.local"

if [[ -f "$LOCAL_RULES" ]]; then
    sed -f "$LOCAL_RULES"
else
    echo "sanitize.sh: no $LOCAL_RULES — passing input through unchanged." >&2
    echo "sanitize.sh: create it with your real->placeholder rules (see header)." >&2
    cat
fi
