
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_qualification as q
from native_adapter import REPO, execute_config, load_dispatcher, sha

def release_info():
    path=REPO.parent/'release.json'
    if not path.is_file() or path.is_symlink():
        return None
    value=json.loads(path.read_bytes())
    if value.get('version')!='praxis-4.5.0' or value.get('code_root')!=str(REPO):
        raise ValueError('release identity differs')
    return value

def source_path(path):

    path=Path(path);info=release_info()
    if info:
        origin=Path(info['origin_repo'])
        if path.is_relative_to(origin):
            return REPO/path.relative_to(origin)
        if str(path)==info['origin_engine_closure']:
            return REPO.parent/'engine/engine-closure.json'
    return path

def engine_paths(config):
    if release_info():
        return REPO.parent/'engine/source', REPO.parent/'engine/engine-closure.json'
    return Path(config['engine_source']), Path(config['engine_closure'])

def closure_manifest():

    files = set()
    for folder in ('services/praxis45', 'services/praxis-switchboard', 'skills/orca-dispatch'):
        files.update((REPO/folder).glob('*.py'))
    files.add(REPO/'services/praxis45/engine-lock.json')
    files.update((REPO/'services/praxis45/schemas').glob('*.json'))
    files.add(REPO/'hooks/agent_home_guard.py')
    files.add(Path.home()/'.claude/hooks/agent_home_guard.py')
    for tree in ('.agents', '.claude'):
        files.update((Path.home()/tree/'skills/chief-of-staff').glob('*.py'))
    from native_adapter import load_dispatcher
    registry = load_dispatcher().named_launch_module().registry
    files.add(registry.institution()/'MODEL-REGISTRY.yaml')
    files.add(registry.institution()/'ROLE-REGISTRY.yaml')
    return [{'path': str(path), 'sha256': sha(path)} for path in sorted(files) if path.is_file()]

def validate_candidate(config):
    required_keys = {'enabled', 'candidate_commit', 'workspace', 'state_root', 'engine_source',
                     'engine_closure', 'closure', 'objective', 'context', 'graph', 'nodes'}
    if not isinstance(config, dict):
        raise ValueError('config must be a JSON object')
    missing = sorted(required_keys - set(config))
    if missing:
        raise ValueError('missing config keys: ' + ', '.join(missing))
    if not isinstance(config['closure'], list) or any(not isinstance(row, dict)
            or set(row) != {'path', 'sha256'} for row in config['closure']):
        raise ValueError('closure requires path/sha256 rows')
    if config.get('enabled') is not True:
        raise ValueError('candidate disabled')
    info=release_info()
    head = info['commit'] if info else subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], check=True,
                          text=True, capture_output=True).stdout.strip()
    if head != config['candidate_commit']:
        raise ValueError('candidate commit differs from frozen config')
    if info:
        for row in info['code_files']:
            if sha(REPO/row['path']) != row['sha256']:
                raise ValueError('versioned release code drift: '+row['path'])
    if not info and subprocess.run(['git', '-C', str(REPO), 'status', '--porcelain'], check=True,
                      capture_output=True, text=True).stdout:
        raise ValueError('candidate has uncommitted changes')
    for row in config['closure']:
        if sha(source_path(row['path'])) != row['sha256']:
            raise ValueError('candidate import closure changed: ' + row['path'])
    required = {str(REPO / p) for p in ('services/praxis45/candidate_run.py',
        'services/praxis45/native_adapter.py', 'services/praxis45/native_ci.py',
        'services/praxis45/projection.py', 'services/praxis45/engine_boundary.py',
        'services/praxis45/engine_qualification.py', 'skills/orca-dispatch/dispatcher.py',
        'skills/orca-dispatch/session_usage.py', 'services/praxis-switchboard/named_launch.py',
        'services/praxis-switchboard/registry.py')}
    required.update(row['path'] for row in closure_manifest())
    if not required <= {str(source_path(r['path'])) for r in config['closure']}:
        raise ValueError('candidate import closure incomplete')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--preview', action='store_true', help='validate config/graph/named units; no state or native starts')
    args = parser.parse_args()
    try:
        raw = args.config.read_bytes();config = json.loads(raw)
        validate_candidate(config)
        q.configure(*engine_paths(config))
        dispatcher = load_dispatcher()
        command = dispatcher.JsonCommand(dispatcher.launch_env())
        router = dispatcher.RouterClient(dispatcher.default_router(), command)
        if args.preview:
            from native_adapter import NativeWorkerAdapter
            adapter = NativeWorkerAdapter(raw, dispatcher, router, None, persist=False)
            routes = {}
            for name in adapter.order:
                if adapter.config['nodes'][name].get('external_producers'):
                    unit = adapter.unit(name)
                    sources, producers = adapter.bindings(name, unit)
                    route = router.route(unit, None, False, run_key=adapter.graph_run_id, producer_model=producers)
                    adapter.check_capability_route(unit, route, producers)
                    routes[name] = {'producer_model': producers, 'route': route}
            print(json.dumps({'graph_run_id': adapter.graph_run_id, 'status': 'validated',
                'dispatcher_path': dispatcher.__file__, 'native_launches': 0, 'external_routes': routes}, indent=2));return 0
        orca = dispatcher.OrcaClient(dispatcher.resolve_orca(None), command)
        result = execute_config(raw, dispatcher, router, orca)
        print(json.dumps(result, indent=2));return 0 if result['status'] == 'succeeded' and result['ci_status'] == 'captured' else 4
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, dispatcher.DispatchError if 'dispatcher' in locals() else ValueError,
            q.UnresolvedAttempt) as exc:
        print(json.dumps({'status': 'unresolved', 'reason': str(exc)}));return 4

if __name__ == '__main__':
    sys.exit(main())
