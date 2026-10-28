
import json
from pathlib import Path
import socket
import subprocess

def identity(source=None):
    repo = Path(source or __file__).resolve().parents[2]
    host = socket.gethostname()
    manifest = repo.parent / 'install.json'
    if manifest.is_file():
        data = json.loads(manifest.read_text())
        return {'hostname': host, 'kind': 'installed praxis-rooms', 'commit': data['commit'],
                'path': str(repo), 'manifest_sha256': data.get('manifest_sha256')}
    def git(*args):
        p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=3)
        return p.stdout.strip() if p.returncode == 0 else 'unknown'
    return {'hostname': host, 'kind': 'development checkout', 'path': str(repo),
            'commit': git('rev-parse', 'HEAD'), 'dirty': len(git('status', '--porcelain', '--untracked-files=normal').splitlines())}

def banner(data=None):
    data = data or identity()
    if data['kind'] == 'installed praxis-rooms':
        source = 'installed praxis-rooms '+data['commit'][:12]+' ('+data['path']+')'
    else:
        source = 'development checkout '+data['path']+' @ '+data['commit'][:12]+' [dirty '+str(data['dirty'])+']'
    return 'Host: '+data['hostname']+' · Source: '+source
