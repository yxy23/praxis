
import hashlib
import json
import os
from pathlib import Path
import socket

def adaptive_runtime(home=None, repo=None):
    home = Path(home or Path.home())
    repo = Path(repo or Path(__file__).resolve().parents[2])
    lock = json.loads((repo/'services/praxis45/engine-lock.json').read_text())
    for base in (home/'.local/share/praxis/rooms/adaptive-runtime/current',
                 home/'.local/share/praxis/releases/current'):
        closure = base/'engine/engine-closure.json'
        python = base/'venv/bin/python'
        source = base/'engine/source'
        try:
            raw = closure.read_bytes()
            data = json.loads(raw)
            if (python.is_file() and os.access(str(python), os.X_OK) and source.is_dir()
                    and data.get('engine', {}).get('commit', data.get('engine_commit', data.get('commit'))) == lock['commit']
                    and hashlib.sha256(raw).hexdigest() == lock['closure_sha256']
                    and (repo/'services/praxis45/adaptive_general.py').is_file()):
                return {'python': str(base.resolve()/'venv/bin/python'), 'engine_source': str(source.resolve()),
                        'engine_closure': str(closure.resolve()), 'engine_commit': lock['commit']}
        except (OSError, ValueError, TypeError):
            continue
    raise ValueError('Adaptive execution runtime is not installed on this host ('+socket.gethostname()+
        '); nothing was dispatched. Configure a validated room runtime identity; see services/praxis45/PUBLIC-RUNTIME.md.')
