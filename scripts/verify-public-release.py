#!/usr/bin/env python3
"""Check a complete public source tree without printing sensitive matches."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


STRUCTURAL = {
    "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "credential": r"\b(?:github_pat_|gh[pousr]_|sk-proj-)[A-Za-z0-9_-]{20,}",
    "personal-home": r"/Users/(?!<|example(?:/|\b)|user(?:/|\b)|alice(?:/|\b)|bob(?:/|\b))[A-Za-z0-9_.-]+/",
}
FORBIDDEN_NAMES = {"KNOWLEDGE.md", ".env", ".DS_Store", "activation.json"}
BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                   ".zip", ".gz", ".bz2", ".xz", ".zst", ".7z", ".tar",
                   ".sqlite", ".sqlite3", ".db", ".docx", ".pptx", ".xlsx",
                   ".woff", ".woff2", ".ttf", ".otf", ".bin", ".exe", ".dll", ".dylib", ".so"}


def files(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("release root must be an existing directory, not a symlink")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("symlink refused: " + relative.as_posix())
        if path.is_file():
            yield relative.as_posix(), path


def inventory(root):
    return [{"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "bytes": path.stat().st_size}
            for name, path in files(root) if name != "MANIFEST.json"]


def tree_hash(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify(root, private_patterns=None, require_manifest=False, binary_reviews=None):
    rules = [(name, re.compile(pattern)) for name, pattern in STRUCTURAL.items()]
    if private_patterns:
        lines = private_patterns.read_text().splitlines()
        private = [("private-rule-" + str(i), re.compile(line))
                   for i, line in enumerate(lines, 1)
                   if line.strip() and not line.startswith("#")]
        if not private:
            raise ValueError("private pattern list is empty")
        rules.extend(private)
    issues = []
    count = 0
    for name, path in files(root):
        count += 1
        if path.name in FORBIDDEN_NAMES or "__pycache__" in Path(name).parts or path.name.endswith((".local", ".pyc", ".pyo")):
            issues.append({"path": name, "rule": "private-or-generated-file"})
        raw = path.read_bytes()
        binary = (path.suffix.lower() in BINARY_SUFFIXES or raw.startswith(b"%PDF-")
                  or any(byte < 32 and byte not in (9, 10, 13) for byte in raw))
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
        if binary:
            review = (binary_reviews or {}).get(name, {})
            if not isinstance(review, dict) or review.get("sha256") != hashlib.sha256(raw).hexdigest() or review.get("status") != "reviewed":
                issues.append({"path": name, "rule": "unreviewed-binary"})
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for rule, pattern in rules:
                if pattern.search(line):
                    issues.append({"path": name, "line": number, "rule": rule})
    manifest = root / "MANIFEST.json"
    if manifest.exists():
        recorded = json.loads(manifest.read_text())
        current = inventory(root)
        if not isinstance(recorded, dict) or recorded.get("files") != current:
            issues.append({"path": "MANIFEST.json", "rule": "manifest-differs"})
        if not isinstance(recorded, dict) or recorded.get("tree_sha256") != tree_hash(current):
            issues.append({"path": "MANIFEST.json", "rule": "manifest-tree-hash-differs"})
    elif require_manifest:
        issues.append({"path": "MANIFEST.json", "rule": "manifest-missing"})
    if not count:
        issues.append({"path": ".", "rule": "empty-release"})
    return {"ok": not issues, "files": count, "issues": issues,
            "limits": "Pattern checks and hashes do not replace substantive privacy or technical review."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--private-patterns", type=Path)
    parser.add_argument("--require-manifest", action="store_true")
    parser.add_argument("--binary-reviews", type=Path, help="Explicit review receipts keyed by relative path, binding each reviewed binary SHA256")
    args = parser.parse_args()
    try:
        reviews = json.loads(args.binary_reviews.read_text()) if args.binary_reviews else None
        if reviews is not None and not isinstance(reviews, dict):
            raise ValueError("binary review receipts must be a JSON object")
        result = verify(args.root.absolute(), args.private_patterns, args.require_manifest, reviews)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
