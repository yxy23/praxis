#!/usr/bin/env bash
# pre-commit-hook.sh — Git pre-commit safety net
#
# Greps staged changes for known personal-identifier patterns. If any are
# present, blocks the commit with a clear message. The user runs sanitize.sh
# on the affected files (or uses --no-verify if it's a false positive).
#
# Install: ln -s ../../scripts/pre-commit-hook.sh .git/hooks/pre-commit
# Or use: scripts/setup-hooks.sh

set -euo pipefail

# Patterns that must not appear in any committed file in this public repo.
#
# Only STRUCTURAL patterns live here — ones that carry no personal identifier and
# are safe to publish. The real identifiers (usernames, paths, emails, initials,
# family, course codes, project anchors, collaborator names) are themselves private,
# so listing them in this committed file would leak them. They load from a gitignored
# local backstop instead: scripts/.leak-patterns.local (see loader below).
declare -a PATTERNS=(
    # Incident-narrative markers — dated personal/workflow incidents belong in
    # praxis-private + vault KNOWLEDGE.md, never the public projection. These do
    # NOT match version provenance like "added 2026-05-13" / "retired 2026-05-08".
    'Caught 20[0-9][0-9]'
    'caught on 20[0-9][0-9]'
    'incident on 20[0-9][0-9]'
)

# Real identifiers load from an OPTIONAL gitignored file (scripts/.leak-patterns.local),
# one grep -E pattern per line (blank lines and #-comments ignored). Add new names
# there, never in this committed file.
#
# Resolve the repo root via git, NOT via "$(dirname "$0")/..": when this script runs
# as the installed hook (.git/hooks/pre-commit, a symlink) $0 is the hook path, so
# dirname/.. would point at .git/ and the local backstop would silently never load.
REPO_TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
LOCAL_PATTERNS="$REPO_TOPLEVEL/scripts/.leak-patterns.local"
if [[ -f "$LOCAL_PATTERNS" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        PATTERNS+=("$line")
    done < "$LOCAL_PATTERNS"
fi

LEAKS=0
LEAK_OUTPUT=""

for pat in "${PATTERNS[@]}"; do
    # Get added/modified content from staged changes.
    # Exclude the hook/sanitize/setup scripts themselves — by design they must
    # contain the literal patterns they detect/transform/demonstrate; scanning them
    # produces false-positive blocks every time the rule list or demo is edited.
    matches=$(git diff --cached --diff-filter=AM -U0 \
        -- '*.md' '*.sh' '*.json' '*.py' \
        ':(exclude)scripts/sanitize.sh' \
        ':(exclude)scripts/pre-commit-hook.sh' \
        ':(exclude)scripts/setup-hooks.sh' \
        | grep -E "^\+" \
        | grep -vE "^\+\+\+" \
        | grep -E "$pat" \
        || true)
    if [[ -n "$matches" ]]; then
        LEAK_OUTPUT+="Pattern '$pat':\n"
        # Show first 3 matches per pattern
        LEAK_OUTPUT+=$(echo "$matches" | head -3 | sed 's/^/    /')
        LEAK_OUTPUT+="\n\n"
        LEAKS=$((LEAKS + 1))
    fi
done

if [[ $LEAKS -gt 0 ]]; then
    echo "✗ pre-commit: detected $LEAKS personal-identifier pattern(s) in staged changes"
    echo ""
    echo -e "$LEAK_OUTPUT"
    echo "Fixes:"
    echo "  1. Run scripts/sanitize.sh on the affected files, then re-stage"
    echo "  2. Edit out the identifiers manually"
    echo "  3. If false positive: 'git commit --no-verify' (use sparingly)"
    echo ""
    exit 1
fi

# KNOWLEDGE.md guard: KNOWLEDGE.md is vault-private accumulated learning. It must
# never live in the public repo — its full-fidelity home is praxis-private + the
# vault. Block adding/modifying any skills/<name>/KNOWLEDGE.md here. (Deletions
# pass: --diff-filter=AM excludes them, so removing a stray copy is allowed.)
KNOWLEDGE_STAGED=$(git diff --cached --name-only --diff-filter=AM \
    | grep -E '^skills/[^/]+/KNOWLEDGE\.md$' \
    || true)
if [[ -n "$KNOWLEDGE_STAGED" ]]; then
    echo "✗ pre-commit: KNOWLEDGE.md staged for the public repo"
    echo ""
    echo "$KNOWLEDGE_STAGED" | sed 's/^/    /'
    echo ""
    echo "KNOWLEDGE.md is vault-private. Keep it in praxis-private + the vault, not here."
    echo "If this is an intentional exception: 'git commit --no-verify'."
    echo ""
    exit 1
fi

# Skills-manual sync check: if a skill or slash command is staged, prompt for
# manual update. The user-guide is the public skill catalog; out-of-date manual
# is a documentation leak that compounds across releases.
SKILL_OR_CMD_CHANGED=$(git diff --cached --name-only --diff-filter=AM \
    | grep -E '^(skills/[^/]+/SKILL\.md|slash-commands/[^/]+\.md)$' \
    || true)
MANUAL_CHANGED=$(git diff --cached --name-only --diff-filter=AM \
    | grep -E '^docs/user-guide\.md$' \
    || true)

if [[ -n "$SKILL_OR_CMD_CHANGED" ]] && [[ -z "$MANUAL_CHANGED" ]]; then
    echo "⚠ pre-commit: skill or slash-command staged without docs/user-guide.md update"
    echo ""
    echo "Staged skill/command files:"
    echo "$SKILL_OR_CMD_CHANGED" | sed 's/^/    /'
    echo ""
    echo "If this commit changes what a skill DOES (capabilities, scope, invocation), update:"
    echo "  - docs/user-guide.md (public Tier 1 catalog)"
    echo "  - <vault>/system/Skills User Manual.md (vault-side full catalog)"
    echo ""
    echo "If the commit is non-substantive (formatting, comments, internal refactor), proceed:"
    echo "  git commit --no-verify"
    echo ""
    exit 1
fi

exit 0
