#!/usr/bin/env bash
# Install one public research skill into Claude and Codex discovery roots.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
SKILL="${1:-}"
if [[ ! "$SKILL" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "Usage: $0 <skill-name> (for example, convert)" >&2
    exit 1
fi
CONFIG="${PRAXIS_CONFIG_FILE:-$HOME/.praxis-config.sh}"
if [[ ! -f "$CONFIG" ]]; then
    echo "Copy templates/user-config-template.sh to $CONFIG and configure it." >&2
    exit 1
fi
# This is your own shell configuration. Review it before installation.
source "$CONFIG"
SCRIPT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_ROOT="${PRAXIS_DEPLOY_ROOT:-}"
if [[ -n "$DEPLOY_ROOT" ]]; then
    VAULT_ROOT="$DEPLOY_ROOT/vault"
    CLAUDE_SKILLS_ROOT="$DEPLOY_ROOT/.claude/skills"
    CODEX_SKILLS_ROOT="$DEPLOY_ROOT/.agents/skills"
    COMMANDS_DIR="$DEPLOY_ROOT/.claude/commands"
else
    : "${VAULT_ROOT:?set VAULT_ROOT in your local configuration}"
    CLAUDE_SKILLS_ROOT="${PRAXIS_CLAUDE_SKILLS_ROOT:-$HOME/.claude/skills}"
    CODEX_SKILLS_ROOT="${PRAXIS_CODEX_SKILLS_ROOT:-$HOME/.agents/skills}"
    COMMANDS_DIR="${COMMANDS_DIR:-$HOME/.claude/commands}"
fi
python3 - "$SCRIPT_ROOT" "$SKILL" "$VAULT_ROOT" "$CLAUDE_SKILLS_ROOT" "$CODEX_SKILLS_ROOT" "$COMMANDS_DIR" <<'PYTHON'
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

repo, name, vault, claude, codex, commands = sys.argv[1:]
repo = Path(repo).resolve()
source = repo / "skills" / name
if not (source / "SKILL.md").is_file():
    raise SystemExit("Skill is absent from this public catalog: " + name)
roots = [Path(p).expanduser().resolve() for p in (claude, codex)]
for root in roots:
    if root in (Path('/'), Path.home()) or root == repo or repo in root.parents or root in repo.parents:
        raise SystemExit("Unsafe deployment root")
if roots[0] == roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents:
    raise SystemExit("Claude and Codex discovery roots must be separate")
# Only source files are installed. User knowledge and generated data are refused.
entries = []
for item in sorted(source.rglob('*')):
    if item.is_symlink():
        raise SystemExit("Skill source contains a symlink")
    if item.is_file():
        if item.name == 'KNOWLEDGE.md' or '__pycache__' in item.parts or item.suffix in ('.pyc', '.pyo'):
            raise SystemExit("Skill source contains private or generated content")
        if item.name != 'manifest-entry.json':
            entries.append(item.relative_to(source))
for root in roots:
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if target.is_symlink():
        raise SystemExit("Refusing a symlink deployment target")
    with tempfile.TemporaryDirectory(prefix='.praxis-install-', dir=root) as temp:
        staged = Path(temp) / name
        staged.mkdir()
        for rel in entries:
            dst = staged / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dst)
        # Replace only the selected generated skill directory. Knowledge is elsewhere.
        if target.exists():
            if not target.is_dir():
                raise SystemExit("Deployment target is not a directory")
            shutil.rmtree(target)
        shutil.copytree(staged, target)

def inventory(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}
if inventory(roots[0] / name) != inventory(roots[1] / name):
    raise SystemExit("Claude/Codex deployment mismatch")
state = Path(vault).expanduser() / 'system' / 'memory' / 'agents' / name
state.mkdir(parents=True, exist_ok=True)
knowledge = state / 'KNOWLEDGE.md'
if not knowledge.exists():
    template = (repo / 'templates' / 'KNOWLEDGE-template.md').read_text()
    knowledge.write_text(template.replace('<SKILL_NAME>', name).replace('<YYYY-MM-DD>', datetime.now(timezone.utc).date().isoformat()))
command_source = repo / 'slash-commands' / (name + '.md')
if command_source.is_file():
    commands = Path(commands).expanduser()
    commands.mkdir(parents=True, exist_ok=True)
    target = commands / command_source.name
    if target.is_symlink():
        raise SystemExit('Refusing a symlink command target')
    text = command_source.read_text().replace('~/.claude/skills/' + name + '/SKILL.md', str(roots[0] / name / 'SKILL.md'))
    target.write_text(text)
manifest = source / 'manifest-entry.json'
if manifest.is_file():
    entry = json.loads(manifest.read_text())
    entry['skill_path'] = str(roots[0] / name / 'SKILL.md')
    with tempfile.TemporaryDirectory(prefix='.praxis-manifest-', dir=roots[0]) as temp:
        rendered = Path(temp) / 'entry.json'
        rendered.write_text(json.dumps(entry))
        subprocess.run([sys.executable, str(repo / 'scripts/upsert-manifest.py'),
                        '--manifest', str(roots[0] / '_manifest.jsonl'),
                        '--entry', str(rendered), '--skill', name], check=True)
receipt = {'installed_at': datetime.now(timezone.utc).isoformat(), 'skill': name,
           'files': inventory(roots[0] / name), 'runtime_parity': True}
with (state / 'deploy-history.jsonl').open('a') as stream:
    stream.write(json.dumps(receipt, sort_keys=True) + '\n')
print('Installed ' + name + ' into Claude and Codex; existing knowledge preserved.')
PYTHON
