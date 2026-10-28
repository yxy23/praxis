
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True

def roots():
    home = Path.home()
    vault = home / 'Research'
    return [home / '.claude/skills', home / '.agents/skills'], vault / 'system/memory/agents'

def snapshot(skill_roots=None, agent_root=None):
    default_skills, default_agents = roots()
    skill_roots = default_skills if skill_roots is None else skill_roots
    agent_root = default_agents if agent_root is None else agent_root
    rows, errors = {}, []
    for root in skill_roots:
        if not root.is_dir():
            errors.append({'path': str(root), 'error': 'root missing'})
    if not agent_root.is_dir():
        errors.append({'path': str(agent_root), 'error': 'agent root missing'})
    files = set(p for root in skill_roots for p in root.rglob('*') if p.is_file())
    files.update(agent_root.glob('*/KNOWLEDGE.md'))
    for path in sorted(files):
        try:
            before = path.stat();raw = path.read_bytes();after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise OSError('file changed during single inventory read; no retry')
            rows[str(path)] = {'sha256': hashlib.sha256(raw).hexdigest(), 'mtime_ns': after.st_mtime_ns,
                'bytes': len(raw), 'symlink_target': str(path.resolve()) if path.is_symlink() else None,
                'writer': 'unattributed'}
        except OSError as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    return {'at': datetime.now(timezone.utc).isoformat(), 'files': rows, 'errors': errors,
            'scope': 'every file under both installed skill trees and agents/*/KNOWLEDGE.md',
            'writer_basis': 'mtime is metadata, not writer identity; attribution requires supplied evidence'}

def compare(before, after, provenance=None):
    provenance = provenance or {}
    changes = []
    for path in sorted(set(before['files']) | set(after['files'])):
        a, b = before['files'].get(path), after['files'].get(path)
        if a != b:
            proof = provenance.get(path, {})
            attributed = bool(b and proof.get('sha256') == b['sha256'] and proof.get('writer') and proof.get('evidence_path'))
            changes.append({'path': path, 'before': a, 'after': b,
                'writer': proof['writer'] if attributed else 'unattributed',
                'provenance': proof if attributed else None,
                'disposition': 'attributed concurrent exception; never rolled back' if attributed else 'unresolved change; no rollback'})
    return {'changes': changes, 'changed_count': len(changes), 'errors': before['errors'] + after['errors'],
            'rehearsal_authored_none': not changes and not before['errors'] and not after['errors'],
            'attributed_exceptions': sum(r['writer'] != 'unattributed' for r in changes),
            'meaning': 'no change is evidence of no global writes within the measured interval; attribution is independent evidence'}

def main():
    parser = argparse.ArgumentParser(description=__doc__);sub = parser.add_subparsers(dest='command', required=True)
    cmd = sub.add_parser('snapshot');cmd.add_argument('--output', required=True, type=Path)
    cmd = sub.add_parser('compare')
    for field in ('before', 'after'):
        cmd.add_argument('--' + field, required=True, type=Path)
    cmd.add_argument('--provenance', type=Path)
    args = parser.parse_args()
    if args.command == 'snapshot':
        result = snapshot()
        with args.output.open('x') as handle:
            json.dump(result, handle, indent=2, sort_keys=True);handle.write('\n')
        print(json.dumps({'files': len(result['files']), 'errors': result['errors'], 'output': str(args.output)}))
    else:
        result = compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()),
                         json.loads(args.provenance.read_text()) if args.provenance else None)
        print(json.dumps(result, sort_keys=True, indent=2))
    return 4 if result.get('errors') else 0

if __name__ == '__main__':
    sys.exit(main())
