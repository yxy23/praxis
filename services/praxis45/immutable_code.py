
import base64
import os
import hashlib
from pathlib import Path
import subprocess
from native_capabilities import require

REPO=Path(__file__).resolve().parents[2]

def relative(path):
    parts=Path(path).parts
    for i,part in enumerate(parts):
        if part in {'services','skills','hooks'}:return str(Path(*parts[i:]))
    return None

def git_blob(commit,path):
    proc=subprocess.run(['git','--no-optional-locks','-C',str(REPO),'show',commit+':'+path],capture_output=True)
    require(proc.returncode==0,'immutable code blob unavailable: '+path)
    return proc.stdout

def authenticate(binding,commit,archive=None):
    path=relative(binding['path']);require(path is not None,'code path identity missing')
    if archive:
        if 'archive_path' in archive:
            import json
            retained=Path(archive['archive_path']);require(retained.is_file() and not retained.is_symlink(),'immutable archive missing')
            data=retained.read_bytes();require(hashlib.sha256(data).hexdigest()==archive['sha256'],'immutable archive bytes differ')
            archive=json.loads(data)
        require(archive['commit']==commit,'immutable archive commit differs')
        row=archive['files'].get(path);require(isinstance(row,dict),'immutable archive code missing: '+path)
        raw=base64.b64decode(row['content'],validate=True)
        object_id=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        require(object_id==row['git_blob'],'immutable git blob identity differs')
    else:raw=git_blob(commit,path)
    require(hashlib.sha256(raw).hexdigest()==binding['sha256'],'immutable code hash differs: '+path)
    return raw

def freeze(commit,bindings):
    from candidate_run import release_info
    info=release_info()
    if info:
        require(info['commit']==commit,'installed immutable archive commit differs')
        archive=REPO.parent/'code-identity.json'
        identity={'commit':commit,'archive_path':str(archive),'sha256':info['code_identity_sha256']}
        for binding in bindings:
            if Path(binding['path']).is_relative_to(REPO):authenticate(binding,commit,identity)
        return identity
    files={}
    for binding in bindings:
        path=relative(binding['path'])
        if not path or not Path(binding['path']).is_relative_to(REPO):continue
        raw=authenticate(binding,commit)
        files[path]={'git_blob':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),
                     'sha256':binding['sha256'],'content':base64.b64encode(raw).decode()}
    return {'commit':commit,'files':files}

def verify_closure(entry):
    from native_adapter import verify_sources
    external=[]
    for binding in entry.get('closure',[]):
        path=relative(binding['path'])
        if path and (path.startswith('services/') or path.startswith('skills/orca-dispatch/') or path=='hooks/agent_home_guard.py'):
            authenticate(binding,entry['candidate_commit'],entry.get('code_identity'))
        else:external.append(binding)
    verify_sources(external)

def bootstrap():

    paths=[REPO/'CLAUDE.md',Path(os.environ.get("VAULT_ROOT", "~/Research")).expanduser()/"system/agent-config/GLOBAL-RULES.md"]
    for tree in ('.agents','.claude'):
        paths.extend(Path.home()/tree/'skills'/name/'SKILL.md' for name in ('council',))
    require(all(path.is_file() and not path.is_symlink() for path in paths),'checker bootstrap path missing')
    return [{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths]

def verify_execution_closure(entry):
    from candidate_run import source_path
    verify_closure(entry)
    for row in entry.get('closure',[]):
        path=source_path(row['path'])
        require(path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256'],
                'executing closure bytes differ: '+row['path'])
