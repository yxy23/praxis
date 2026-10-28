#!/usr/bin/env python3
"""
upsert-manifest.py — idempotent manifest entry insertion for the Claude Skills Toolkit.

Reads a single-skill manifest entry JSON file and ensures the manifest at the
target path contains exactly one matching line (by skill name). Replaces if
exists, appends if not.

Usage:
    python3 upsert-manifest.py --manifest <path> --entry <path> --skill <name>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="path to _manifest.jsonl")
    parser.add_argument("--entry", required=True, help="path to single-skill JSON entry file")
    parser.add_argument("--skill", required=True, help="skill name (must match entry's name field)")
    args = parser.parse_args()

    entry_path = Path(args.entry)
    manifest_path = Path(args.manifest)

    # Load the new entry
    entry_text = entry_path.read_text(encoding="utf-8").strip()
    try:
        entry = json.loads(entry_text)
    except json.JSONDecodeError as e:
        print(f"Failed to parse entry JSON: {e}", file=sys.stderr)
        return 1

    if entry.get("name") != args.skill:
        print(
            f"Entry name '{entry.get('name')}' does not match --skill '{args.skill}'",
            file=sys.stderr,
        )
        return 1

    new_line = json.dumps(entry, ensure_ascii=False) + "\n"

    # Read existing manifest (or initialize)
    if manifest_path.exists():
        lines = manifest_path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []

    # Find existing entry by skill name
    found_indices = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("name") == args.skill:
            found_indices.append(i)

    if found_indices:
        lines[found_indices[0]] = new_line
        for index in reversed(found_indices[1:]):
            del lines[index]
        action = "updated"
    else:
        # Ensure final newline before appending
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(new_line)
        action = "appended"

    manifest_path.write_text("".join(lines), encoding="utf-8")
    print(f"  ({action} manifest entry for {args.skill})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
