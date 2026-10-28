
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(handle.name, path)

def selected(context, room_id):
    if context is None or not room_id:
        return False
    import project_rooms
    path = project_rooms.output_root(context) / 'ENGINE.json'
    if not path.exists():
        return False
    value = json.loads(path.read_bytes())
    if value.get('enabled') is False:
        return False
    if (path.is_symlink() or value.get('schema_version') != 1
            or value.get('engine') != 'attractor' or value.get('enabled') is not True
            or value.get('room_id') != room_id
            or value.get('context_identity') != project_rooms.context_identity(context)):
        raise ValueError('invalid project Attractor opt-in: ' + str(path))
    return True

def freeze_input(path, root, number, expected=None):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('room graph input requires an absolute regular source: ' + str(path))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and expected != digest:
        raise ValueError('room graph input binding differs: ' + str(path))
    folder = root / 'attractor-inputs'
    folder.mkdir(exist_ok=True)
    snapshot = folder / (str(number) + '-' + path.name)
    if snapshot.exists():
        raise ValueError('room input snapshot already exists; use original recovery')
    with snapshot.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if sha(path) != digest:
        raise ValueError('room source changed during snapshot preparation: ' + str(path))
    return {'source': {'path': str(path), 'sha256': digest},
            'snapshot': {'path': str(snapshot), 'sha256': digest}}

def prepare_plan(req, outdir):
    import runner
    import registry
    import project_rooms
    if req.execution_handoff or req.continuation:
        raise ValueError('Attractor room does not support conditional execution handoffs; use the ordinary continuation surface')
    if req.context is None:
        raise ValueError('Attractor room requires a canonical project context')
    plan = runner.build_plan(req, outdir, serial=True)
                                                                              
    units = plan['units']
    units.sort(key=lambda unit: (2 if unit['seat'] == req.chair_name and req.chaired else
                                 0 if unit['seat'] == 'Athena' else 1))
    prior = []
    routes = {}
    frozen = [freeze_input(req.context.note, outdir, 0)]
    setting = project_rooms.output_root(req.context) / 'ENGINE.json'
    if setting.exists():
        selected(req.context, req.context_room_id)
        frozen.append(freeze_input(setting, outdir, 1))
        config = json.loads(Path(frozen[-1]['snapshot']['path']).read_bytes())
        for row in config.get('inputs', []):
            path = Path(row['path'])
            frozen.append(freeze_input(path, outdir, len(frozen), row['sha256']))
    plan['room_inputs'] = frozen
    bindings = [row['snapshot'] for row in frozen]
    context_snapshot = frozen[0]['snapshot']['path']
    for unit in units:
        deps = unit.pop('depends_on', [])
        if set(deps) - set(prior):
            raise ValueError('room dependency is not satisfied by the serial graph')
        if any(row.get('required') is not True for row in unit.get('outputs', [])):
            raise ValueError('Attractor requires unconditional output contracts')
        result = outdir / (unit['id'] + '-result.md')
        unit.setdefault('outputs', []).append({'path': str(result), 'required': True})
        if req.chaired and unit['seat'] == req.chair_name:
            unit['outputs'].append({'path': str(outdir / 'chair-record.md'), 'required': True})
        unit['source_bindings'] = unit.get('source_bindings', []) + copy.deepcopy(bindings)
        unit['spec'] = unit['spec'].replace('Canonical note:   ' + str(req.context.note),
            'Canonical source: ' + str(req.context.note) + '\nFrozen execution note: ' + context_snapshot)
        unit['spec'] = plan['worker_preamble'].replace('${vault_root}', str(registry.vault_root())) + '\n\n' + unit['spec']
        unit['spec'] += ('\n\nSERIAL ROOM EXECUTION\n'
            'This is one box in a serial Attractor graph. No worker launches or retries. '
            'Only the addressed nodes execute. Complete only your addressed assignment. '
            'Read the frozen context snapshot at ' + context_snapshot +
            ' as this occurrence\'s context input. The canonical source path identifies '
            'the owning record and authorized write target, not a mutable evidence input. '
            'The frozen snapshot overrides live-note reading for this occurrence. '
            'If the request needs an unaddressed specialist, do not absorb that work: '
            'return proposed bounded assignments for the coordinator to route. '
            'Read the hash-bound preceding result artifacts listed under Frozen sources '
            'when they are relevant to your assignment; preserve their attribution and limits. '
            'Serial order does not establish '
            'independent verification. Read substantive sources directly with the '
            'assigned model and verify critical passages. No local-model first-read '
            'record is required; local-model unavailability is not a reading gap. '
            'Explicit source-specific no-AI conditions and commissioned blind/local-only '
            'protocols still apply. '
            'Write a nonempty agent-only Markdown result with the assignment, findings, '
            'source locators, missing evidence and artifact links to ' + str(result) + '. '
            'The named chair must also preserve its existing chair-record contract. '
            'Required outputs: ' + json.dumps(unit['outputs'], sort_keys=True))
        route = asdict(req.routes[unit['seat']])
        route.pop('seat')
        route['task_class'] = unit['task_class']
        routes[unit['id']] = route
        prior.append(unit['id'])
    for row in frozen:
        if sha(row['source']['path']) != row['source']['sha256']:
            raise ValueError('room source changed before preparation finished: ' + row['source']['path'])
    return plan, routes

def installed():
    release = (Path.home() / '.local/share/praxis/releases/current').resolve(strict=True)
    marker = json.loads((release / 'release.json').read_bytes())
    flag = Path(marker['installation_home']) / '.local/share/praxis/releases/activation.json'
    activation = json.loads(flag.read_bytes())
    if (activation.get('schema_version') != 1 or activation.get('enabled') is not True
            or activation.get('version') != 'praxis-4.5.0'
            or activation.get('commit') != marker['commit']):
        raise ValueError('installed Attractor release is inactive or identity differs')
    return release

def invoke(payload, *, preview=False, recover=False):
    release = installed()
    args = [str(release / 'venv/bin/python'), '-I', '-B', str(Path(__file__).resolve()),
            '--payload', str(payload)]
    if preview:
        args.append('--preview')
    if recover:
        args.append('--recover')
    from runner import launch_env
    process = subprocess.run(args, capture_output=True, text=True, env=launch_env())
    if not process.stdout.strip():
        raise ValueError('Attractor child returned no result: ' + process.stderr[-1000:])
    value = json.loads(process.stdout)
    if process.returncode not in {0, 4}:
        raise ValueError('Attractor child failed: ' + process.stderr[-1000:])
    return value

def settle(board, request_id, root, result, post):
    status = result.get('status')
    lifecycle = ('preview' if status == 'preview' else 'completed' if status == 'succeeded'
                 else 'failed' if status in {'failed', 'input_refused'} else 'pending')
    artifacts = [str(root / 'attractor-result.json')]
    for name, entry in result.get('receipts', {}).items():
        board.state.set_task(request_id, entry['seat'], state=entry['state'],
                             model=entry.get('model'), task_id=entry.get('task_id'),
                             dispatch_id=entry.get('dispatch_id'), run_id=entry.get('run_id'),
                             artifacts=entry.get('artifacts', []))
        artifacts.extend(entry.get('artifacts', []))
    write(root / 'attractor-result.json', result)
    board.state.update(request_id, execution_engine='attractor', lifecycle=lifecycle,
                       graph_run_id=result.get('graph_run_id'),
                       attractor_payload=str(root / 'attractor-payload.json'),
                       attractor_config=result.get('config_path'), artifacts=artifacts,
                       reasons=[result['reason']] if result.get('reason') else [])
    board.state.save()
    post({'type': 'complete', 'request_id': request_id, 'lifecycle': lifecycle,
          'message': 'Attractor serial room: ' + str(status) + '; receipt: ' + str(root / 'attractor-result.json')})
    return board.state.get(request_id)

def run_request(board, req, post, *, preview=False):
    root = board._outdir(req)
    root.mkdir(parents=True, exist_ok=True)
    contacted = False
    try:
        plan, routes = prepare_plan(req, root)
        payload = {'schema_version': 1, 'request_id': req.request_id, 'plan': plan,
                   'routes': routes, 'workspace': 'path:' + str(Path(__file__).resolve().parents[2]),
                   'adapter': {'path': str(Path(__file__).resolve()), 'sha256': sha(__file__)}}
        path = root / 'attractor-payload.json'
        write(path, payload)

        board.state.update(req.request_id, execution_engine='attractor', lifecycle='pending',
                           attractor_payload=str(path))
        board.state.save()
        contacted = True
        result = invoke(path, preview=preview)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {'status': 'unresolved' if contacted else 'input_refused', 'reason': str(exc)}
        if not contacted:
            result['native_launches'] = 0
    return settle(board, req.request_id, root, result, post)

def recover_request(board, record, post):
    path = Path(record['attractor_payload'])
                                                                              
    if record.get('lifecycle') == 'preview':
        return record
    result = invoke(path, recover=True)
    return settle(board, record['request_id'], path.parent, result, post)

def execute_payload(path, *, preview=False, recover=False):

    release = installed()
    sys.path.insert(0, str(release / 'code/services/praxis45'))
    import candidate_run as candidate
    import native_adapter as native
    import engine_qualification as engine
    from p4_contract import durable_json
    payload = json.loads(path.read_bytes())
    adapter = payload['adapter']
    if sha(adapter['path']) != adapter['sha256']:
        raise ValueError('room adapter changed since request freeze')
    root = path.parent
    config_path = root / 'attractor-config.json'
    if config_path.exists():
        config = json.loads(config_path.read_bytes())
        if config['room_payload_sha256'] != sha(path):
            raise ValueError('room payload changed since config freeze')
    elif recover:

        raise ValueError('original Attractor config unavailable; no replacement launch')
    else:
        names = [unit['id'] for unit in payload['plan']['units']]
        from freeze_rehearsal import graph
        config = {'enabled': True, 'candidate_commit': candidate.release_info()['commit'],
                  'workspace': payload['workspace'], 'state_root': str(root / 'attractor-state'),
                  'engine_source': str(release / 'engine/source'),
                  'engine_closure': str(release / 'engine/engine-closure.json'),
                  'closure': candidate.closure_manifest(), 'objective': payload['plan']['objective'],
                  'context': payload['plan']['context'], 'graph': graph(names),
                  'defaults': payload['plan']['defaults'], 'nodes': {},
                  'room_payload_sha256': sha(path)}
        for unit in payload['plan']['units']:
            unit = copy.deepcopy(unit)
            unit['source_bindings'] += [adapter, {'path': str(path), 'sha256': sha(path)}]
            config['nodes'][unit.pop('id')] = unit
        durable_json(config_path, config)
    raw = config_path.read_bytes()
    if config['context'] != payload['plan']['context'] or set(config['nodes']) != {u['id'] for u in payload['plan']['units']}:
        raise ValueError('frozen room context or node order differs from payload')
    candidate.validate_candidate(config)
    engine.configure(*candidate.engine_paths(config))
    dispatcher = native.load_dispatcher()
    class FrozenRoomRoutes:
        def route(self, unit, *args, **kwargs):
            return copy.deepcopy(payload['routes'][unit['id']])
        def record_outcome(self, *args, **kwargs):
            return {'written': False, 'reason': 'room route frozen at canonical request resolution'}
    router = FrozenRoomRoutes()
    adapter_instance = native.NativeWorkerAdapter(raw, dispatcher, router, None, persist=False)
    if preview:
        prepared = {}
        for name in adapter_instance.order:
            unit = adapter_instance.unit(name)
            supplied = {}
            spec = dispatcher.unit_spec(adapter_instance.plan(name, unit), unit,
                                        router.route(unit), supplied)
            prepared[name] = {'spec_sha256': hashlib.sha256(spec.encode()).hexdigest(),
                              'supplied_context': supplied}
        result = {'status': 'preview', 'native_launches': 0, 'nodes': adapter_instance.order,
                  'routes': payload['routes'], 'graph_run_id': adapter_instance.graph_run_id,
                  'prepared': prepared}
    elif recover and not (adapter_instance.directory / 'config.json').is_file():
        raise ValueError('original engine occurrence unavailable; recovery cannot start fresh')
    else:
        command = dispatcher.JsonCommand(dispatcher.launch_env())
        orca = dispatcher.OrcaClient(dispatcher.resolve_orca(None), command)
        result = native.execute_config(raw, dispatcher, router, orca)
    result['config_path'] = str(config_path)
    result['engine_release'] = str(release)
    result['receipts'] = {}
    for name in adapter_instance.order:
        entry = adapter_instance.raw / name / 'entry.json'
        decision = adapter_instance.raw / name / 'decision.json'
        if not entry.is_file():
            continue
        record = json.loads(entry.read_bytes())
        outcome = json.loads(decision.read_bytes())['outcome'] if decision.is_file() else {}
        result['receipts'][name] = dict(record['identities'], seat=config['nodes'][name]['seat'],
            state=outcome.get('state', 'unresolved'), model=record['route']['effective_model'],
            artifacts=[row['path'] for row in outcome.get('artifacts', []) if row.get('validated')],
            entry={'path': str(entry), 'sha256': sha(entry)},
            decision={'path': str(decision), 'sha256': sha(decision)} if decision.is_file() else None)
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    try:
        answer = execute_payload(args.payload.resolve(), preview=args.preview, recover=args.recover)
    except Exception as exc:
        answer = {'status': 'unresolved', 'reason': str(exc)}
    print(json.dumps(answer, sort_keys=True))
    sys.exit(0 if answer['status'] in {'preview', 'succeeded'} else 4)
