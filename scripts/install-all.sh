#!/usr/bin/env bash
# Install the public skill packages. Does not configure or activate services.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
count=0
for skill_dir in "$REPO_ROOT"/skills/*/; do
    [[ -f "$skill_dir/SKILL.md" ]] || continue
    bash "$REPO_ROOT/scripts/install-skill.sh" "$(basename "$skill_dir")"
    count=$((count + 1))
done
echo "Installed $count public skills; services and schedules were not activated."
