
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import engine_qualification as q
from engine_boundary import guard_context, isolate_context
from projection import claim_entry, digest, durable_json
from native_capabilities import EVIDENCE, CLAIMS

REPO = Path(__file__).resolve().parents[2]
IDENTITIES = ('run_id', 'task_id', 'dispatch_id')
STATUS_MAPPING = {
    'succeeded+native+contract+files': 'success', 'settled_failed': 'fail',
    'contract_rejected': 'fail', 'missing_or_empty_artifact': 'fail',
    'source_drift': 'unresolved', 'raw_exit': 'unresolved',
    'heartbeat': 'unresolved', 'collection_timeout': 'unresolved',
    'contact_loss': 'unresolved', 'cancelled': 'unresolved',
    'unknown_launch': 'unresolved', 'identity_mismatch': 'unresolved',
    'router_or_binding_exception': 'unresolved', 'lock_held': 'unresolved',
    'dispatcher_acceptance_rejected': 'fail', 'post_acceptance_drift': 'unresolved',
    'unattributable_usage': 'unresolved', 'measured_usage_breach': 'fail',
}

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def load_dispatcher():
    path = REPO / 'skills/orca-dispatch/dispatcher.py'
    spec = importlib.util.spec_from_file_location('praxis45_dispatcher', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def prepare_bound_unit(unit, sources):

    unit['source_bindings'] = sources
    unit['spec'] += '\nFrozen sources: ' + json.dumps(sources, sort_keys=True)
    if unit.get('acquisition'):
        from acquisition import prepare_spec
        unit['spec'] = prepare_spec(unit['spec'], unit['acquisition'], sources)
        unit['spec'] += '\nProducer tool contract: source evidence and public networking use the logged bound helper. Generated cells, occurrence-local inspection, output writes, self-check and lifecycle wrappers are control operations; unclassified controls carry visible uncertainty. No direct curl, urllib, wget, sockets or native web access outside the helper. Helper enforcement does not physically prevent an out-of-protocol process from networking; later transcript refusal is not prevention.'
    return unit

def exception_detail(exc):

    details = []
    for _ in range(3):
        details.append(type(exc).__name__ + ': ' + str(exc))
        exc = exc.__cause__
        if exc is None:
            break
    return ' <- '.join(details)[:1000]

def verify_sources(bindings):
    for row in bindings:
        from candidate_run import source_path
        path = source_path(row['path'])
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or sha(path) != row['sha256']:
            raise q.UnresolvedAttempt('source drift or missing bound source: ' + str(path))

def node_outcome(bundle, entry, unit):

    if unit.get('capability'):
        from immutable_code import verify_closure
        verify_closure(entry)
        verify_sources(entry['sources'])
    else:
        verify_sources(entry['sources'] + entry.get('closure', []))
    identities = entry.get('identities', {})
    message = bundle.get('native_message') or {}
    payload = message.get('payload') or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    returned = {'run_id': payload.get('runId') or payload.get('run_id') or message.get('run_id') or bundle.get('run_id'),
                'task_id': payload.get('taskId') or payload.get('task_id'),
                'dispatch_id': payload.get('dispatchId') or payload.get('dispatch_id')}
    delivery = bundle.get('native_delivery') or {}
    if ((delivery.get('runId') or delivery.get('run_id')) != identities.get('run_id')
            or message not in (delivery.get('messages') or [])
            or message.get('type') != 'worker_done' or not message.get('id')
            or not all(identities.get(k) and identities[k] == returned[k] for k in IDENTITIES)):
        return {'state': 'unresolved', 'reason': 'no exact original settled worker_done', 'artifacts': []}
    records = bundle.get('units') or []
    if len(records) != 1 or records[0].get('id') != unit['id']:
        return {'state': 'unresolved', 'reason': 'native unit identity mismatch', 'artifacts': []}
    record = records[0]
    if (record.get('task_id') != identities['task_id'] or record.get('dispatch_id') != identities['dispatch_id']
            or (record.get('worker_done') or {}).get('message_id') != message['id']):
        return {'state': 'unresolved', 'reason': 'settlement record differs from original', 'artifacts': []}
    artifacts = []
    checks = {r['path']: r for r in record.get('outputs') or []}
    for declared in unit['outputs']:
        path = Path(declared['path'])
        check = checks.get(str(path)) or {}
        present = path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        current_sha = sha(path) if present else None
        artifacts.append({'path': str(path), 'sha256': current_sha, 'required': True,
                          'validated': bool(present and check.get('exists') and check.get('sha256') == current_sha)})
    contract = record.get('contract') or {}
    worker = record.get('worker_done') or {}
    if payload.get('outcome') not in {'succeeded', 'failed'} or worker.get('outcome') != payload.get('outcome'):
        return {'state': 'unresolved', 'reason': 'native outcome absent or contradictory', 'artifacts': artifacts}
    launch = record.get('actual_launch') or {}
    route = record.get('route') or {}
    if (not all(launch.get(k) for k in ('agent', 'model', 'effort'))
            or launch.get('receipt_missing_effective_launch')
            or launch.get('model') != route.get('launch_target')
            or route.get('effective_model') != entry['route'].get('effective_model')):
        return {'state': 'unresolved', 'reason': 'launch attestation missing or mismatched', 'artifacts': artifacts}
    final = bundle.get('dispatcher_final') or {}
    final_records = final.get('units') or []
    if (final.get('run_id') != identities['run_id'] or len(final_records) != 1
            or any(final_records[0].get(k) != record.get(k) for k in ('id', 'task_id', 'dispatch_id'))
            or not final.get('status')):
        return {'state': 'unresolved', 'reason': 'dispatcher final acceptance absent or unbound', 'artifacts': artifacts}
    if final['status'] in {'dispatcher_error', 'interrupted', 'collection_timeout', 'needs_input'}:
        return {'state': 'unresolved', 'reason': 'dispatcher final state: ' + final['status'], 'artifacts': artifacts}
    final_record = final_records[0]
    if (final_record.get('status') not in {'succeeded', 'failed', 'contract_rejected'}
            or not final_record.get('worker_done')
            or (final_record['worker_done'].get('message_id') != message['id'])
            or final_record['worker_done'].get('message_sha256') != digest(message)):
        return {'state': 'unresolved', 'reason': 'original return acceptance not settled: ' +
                str(final_record.get('status')), 'artifacts': artifacts}
    usage = final.get('usage_contract') or {}
    if usage.get('configured') and 'run usage is not fully attributable' in usage.get('reasons', []):
        return {'state': 'unresolved', 'reason': 'configured usage contract lacks attributable usage',
                'artifacts': artifacts}
    good = (final_record.get('status') == 'succeeded'
            and (final_record.get('contract') or {}).get('accepted') is True
            and (final_record.get('worker_done') or {}).get('message_id') == message['id']
            and final['status'] == 'succeeded' and (final.get('usage_contract') or {}).get('accepted') is True
            and payload['outcome'] == 'succeeded' and record.get('status') == 'succeeded'
            and contract.get('accepted') is True and all(r['validated'] for r in artifacts))
    if good:
        for declared in unit['outputs']:
            if declared.get('validator'):
                try:
                    from native_capabilities import validate_file
                    claim_input = unit.get('claim_input')

                    for binding in entry.get('control_bindings', []):
                        if binding['sha256'] == entry.get('config_sha256'):
                            verify_sources([binding])
                            frozen = json.loads(Path(binding['path']).read_bytes())
                            configured = frozen['nodes'][entry['node_id']].get('claim_input')
                            if unit.get('claim_derivation') or entry.get('claim_derivation') or frozen['nodes'][entry['node_id']].get('bind_evidence_from'):
                                from p4_run import validate_claim_derivation
                                validate_claim_derivation(unit,entry,frozen)
                            if configured:
                                if claim_input and claim_input != configured:
                                    if not unit.get('claim_derivation'):raise ValueError('native claim input differs from frozen node')
                                    from p4_run import validate_claim_derivation
                                    configured=validate_claim_derivation(unit,entry,frozen)
                                claim_input = configured
                    checked = validate_file(declared['path'], declared['validator'], claim_input=claim_input)
                    from operation_recount import verify
                    effective_unit=dict(unit)
                    if claim_input:effective_unit['claim_input']=claim_input
                    checked['independent_recount'] = verify(json.loads(Path(declared['path']).read_bytes()), entry, effective_unit)
                    next(r for r in artifacts if r['path'] == declared['path'])['capability_validation'] = checked
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    return {'state': 'fail', 'reason': 'Praxis-native required output rejected: ' + str(exc),
                            'artifacts': artifacts}
        required_native=[r for r in unit.get('outputs',[]) if r.get('validator') in {EVIDENCE,CLAIMS} or unit.get('capability') in {EVIDENCE,CLAIMS}]
        for required in required_native:
            proof=next((r for r in artifacts if r['path']==required['path']),{})
            envelope=proof.get('capability_validation',{}).get('independent_recount',{}).get('coverage')
            if not isinstance(envelope,dict) or not envelope:
                return {'state':'fail','reason':'required native artifact lacks coverage envelope','artifacts':artifacts}
            from coverage_policy import validate as validate_coverage
            try:validate_coverage(envelope,json.loads(Path(required['path']).read_bytes()),expected_envelope_sha256=envelope.get('envelope_sha256'))
            except ValueError as exc:
                return {'state':'fail','reason':'invalid coverage envelope: '+str(exc),'artifacts':artifacts}
        coverage=[r['capability_validation']['independent_recount']['coverage'] for r in artifacts
                  if r.get('capability_validation',{}).get('independent_recount',{}).get('coverage')]
        if any(r['delivery_state']!='delivered' for r in coverage):
            return {'state':'unresolved','reason':'coverage delivery held; operational validation passed',
                    'artifacts':artifacts,'coverage':coverage,'operational_refusal':None}
        return {'state': 'success', 'reason': 'validated native settlement', 'artifacts': artifacts,'coverage':coverage}
    failure = (payload['outcome'] == 'failed' or record.get('status') == 'failed'
               or contract.get('accepted') is False
               or (final_record.get('contract') or {}).get('accepted') is False
               or not all(r['validated'] for r in artifacts)
               or usage.get('configured') and usage.get('accepted') is False and bool(usage.get('reasons')))
    return {'state': 'fail' if failure else 'unresolved',
            'reason': 'native failure, rejected contract, artifact or measured usage breach' if failure else
                      'dispatcher acceptance unresolved', 'artifacts': artifacts}

def canonical_producer(dispatcher, route, actual):
    registry = dispatcher.named_launch_module().registry
    reg = registry._load_yaml(registry.institution() / 'MODEL-REGISTRY.yaml')
    models = {r['id']: r for r in reg['models']}
    model_id = route.get('effective_model')
    model = models.get(model_id) or {}
    if (not model or actual.get('model') != (model.get('launch_target') or model_id)
            or actual.get('agent') != model.get('provider') or route.get('provider') != model.get('provider')):
        raise q.UnresolvedAttempt('attested producer has no exact canonical registry identity')
    return model_id

class FrozenRouter:
    def __init__(self, router, route):
        self.router, self.frozen = router, route
    def route(self, *args, **kwargs):
        return copy.deepcopy(self.frozen)
    def __getattr__(self, name):
        return getattr(self.router, name)

class CollectOriginal:

    def __init__(self, client):
        self.client = client
    def __getattr__(self, name):
        if name in {'run_create', 'task_create', 'worker_start', 'unstarted_task'}:
            raise q.UnresolvedAttempt('collect-original cannot create or start any native work')
        return getattr(self.client, name)

class NativeWorkerAdapter:
    def __init__(self, config_bytes, dispatcher, router, orca, *, persist=True):
        self.config = json.loads(config_bytes)
        self.config_sha = hashlib.sha256(config_bytes).hexdigest()
        self.graph_run_id = 'px45-' + self.config_sha
        self.dispatcher, self.router, self.orca = dispatcher, router, orca
        self.workspace = self.config['workspace']
        self.graph = q.restricted_graph(self.config['graph'])
        boxes = {n.id for n in self.graph.nodes.values() if n.shape == 'box'}
        if not boxes or boxes != set(self.config['nodes']):
            raise ValueError('exactly one frozen canonical unit per box required')
        root = Path(self.config['state_root'])
        if not root.is_absolute():
            raise ValueError('frozen absolute state_root required')
        self.directory = root / self.graph_run_id
        self.raw = self.directory / 'raw'
        start = next(node for node in self.graph.nodes.values() if node.shape == 'Mdiamond')
        cursor = self.graph.outgoing_edges(start.id)[0].to_node
        self.upstream = {}
        preceding = []
        while self.graph.nodes[cursor].shape == 'box':
            self.upstream[cursor] = list(preceding)
            preceding.append(cursor)
            cursor = next(e.to_node for e in self.graph.outgoing_edges(cursor) if e.condition == 'outcome=success')
        self.visits, self.receipts, self.unresolved = {}, {}, {}
        self.states, self.ci_errors = {}, []
        self.order = preceding
        self.entry_calls = {}
        self.lock_held = False
        for name, spec in self.config['nodes'].items():
            unit = self.unit(name)
            if (not unit.get('seat') or not unit.get('spec') or not unit.get('outputs')
                    or unit.get('depends_on') or any(r.get('required') is not True for r in unit['outputs'])):
                raise ValueError('named seat, spec, unconditional required outputs and serial unit required')
            if any(not Path(row['path']).is_absolute() for row in unit['outputs']):
                raise ValueError('absolute required output paths required')
            if set(spec.get('producers', [])) - set(self.upstream[name]):
                raise ValueError('producer must precede consumer on serial success path')
            if spec.get('capability'):
                from native_capabilities import EVIDENCE, CLAIMS
                required = {EVIDENCE: ('Hermione', 'literature-research'), CLAIMS: ('Scully', 'verification')}
                if (spec['capability'] not in required or
                        (unit['seat'], unit['task_class']) != required[spec['capability']] or
                        not all(r.get('validator') == spec['capability'] for r in unit['outputs'])):
                    raise ValueError('Praxis-native capability requires canonical seat and output validator')
                if unit.get('claim_input'):
                    from native_capabilities import validate_claim_input
                    validate_claim_input(unit['claim_input'], discovery=bool(spec.get('evidence_discovery')))
                if unit.get('claim_input') and unit['claim_input'] not in {r['path'] for r in unit.get('source_bindings', [])}:
                    raise ValueError('claim_input must be a frozen source binding')
                if spec['capability'] == CLAIMS and not self.upstream[name] and not spec.get('external_producers') and not spec.get('producer_identity'):
                    raise ValueError('first claim check requires external producer attestation')
            elif any(r.get('validator') for r in unit['outputs']):
                raise ValueError('output validator requires declared capability')
            if spec.get('external_producers'):
                if self.upstream[name]:
                    raise ValueError('external producers supported only on first node')
                self.external_bindings(name, unit)
            self.dispatcher.validate_plan(self.plan(name, unit))
        if not persist:
            return
        self.persist_config(config_bytes)

    def persist_config(self, config_bytes):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.raw.mkdir(exist_ok=True)
        frozen = self.directory / 'config.json'
        if frozen.exists() and frozen.read_bytes() != config_bytes:
            raise ValueError('frozen config changed within occurrence')
        if not frozen.exists():
            with frozen.open('xb') as handle:
                handle.write(config_bytes);handle.flush()
                import os
                os.fsync(handle.fileno())
            from projection import fsync_directory
            fsync_directory(self.directory)

    def unit(self, name):
        spec = copy.deepcopy(self.config['nodes'][name])
        spec['id'] = name
        if spec.get('capability')=='praxis_native_claim_check':
            spec['spec']+='\nExact allowed bootstrap bindings (one cat <path> invocation per file, no compound calls): '+json.dumps(self.config.get('checker_bootstrap',[]),sort_keys=True)
        spec.setdefault('sensitivity', 'internal');spec.setdefault('risk', 'medium');spec.setdefault('data_domains', [])

        rules = {r['id']: r for r in self.config.get('corrections', [])}
        consumed = [rules[k] for k in spec.pop('consumes', [])]
        spec.pop('producers', None)
        spec.pop('external_producers', None)
        spec['source_bindings'] = spec.get('source_bindings', []) + [
            {'path': r['path'], 'sha256': r['sha256']} for r in consumed]
        if consumed:
            spec['spec'] += '\nConsume these frozen procedural correction rules: ' + json.dumps(consumed, sort_keys=True)
        from candidate_run import release_info, source_path
        info=release_info()
        if info:
            spec['source_bindings']=[dict(row,path=str(source_path(row['path']))) for row in spec['source_bindings']]
            spec['spec']=spec['spec'].replace(info['origin_repo'],str(REPO))
            if spec.get('claim_input'):
                spec['claim_input']=str(source_path(spec['claim_input']))
        return spec

    def plan(self, name, unit):
        return {'schema_version': '1', 'id': self.graph_run_id + '-' + name,
                'objective': self.config['objective'], 'context': self.config['context'],
                'defaults': self.config.get('defaults', {}), 'units': [unit]}

    async def emit(self, name, data):
        if name == 'pipeline:node_start':
            self.visits[data['node_id']] = data['execution_index']

    def observer(self, folder, entry, plan):
        def observe(stage, payload):
            if not self.lock_held:
                raise self.dispatcher.LeasePersistenceError('receipt observer requires occurrence lock')
            try:
                if stage in {'prepared', 'before_start'}:
                    if self.unit(entry['node_id']).get('capability'):
                        from immutable_code import verify_execution_closure
                        verify_execution_closure(entry)
                        verify_sources(entry['sources'])
                    else:
                        verify_sources(entry['sources'] + entry.get('closure', []))
                if self.unit(entry['node_id']).get('capability'):
                    if stage == 'settled':
                        from immutable_code import verify_execution_closure
                        verify_execution_closure(entry)
                        verify_sources(entry['sources'])
                if stage == 'prepared':
                    prepared = payload['units'][0]
                    if prepared['spec_sha256'] != entry['spec_sha256'] or prepared['context'] != entry['supplied_context']:
                        raise ValueError('prepared native context/spec differs from entry binding')
                    if self.unit(entry['node_id']).get('capability'):
                        entry['dispatcher_spec_sha256']=prepared['spec_sha256']
                        durable_json(folder / 'entry.json',entry)
                if stage in {'run', 'task', 'launch'}:
                    for key in IDENTITIES:
                        if payload.get(key):
                            prior = entry['identities'].get(key)
                            if prior and prior != payload[key]:
                                raise ValueError('original identity changed')
                            entry['identities'][key] = payload[key]
                    if stage == 'launch':
                        entry['launch_receipt'] = payload['receipt']
                    durable_json(folder / 'entry.json', entry)
                if stage == 'settled':
                    prior_path = folder / 'settled.json'
                    if prior_path.exists():
                        prior = json.loads(prior_path.read_text())
                        if prior['native_message'] != payload['native_message']:
                            raise ValueError('collection differs from the exact original native return')
                    bundle = dict(payload, schema_version='1', completed_at=utc())
                    durable_json(folder / 'settled.json', bundle)
            except (ValueError, OSError) as exc:
                raise self.dispatcher.LeasePersistenceError('native receipt persistence unresolved') from exc
        return observe

    def bindings(self, name, unit):
        sources = copy.deepcopy(unit.get('source_bindings', []))
        verify_sources(self.config.get('closure', []))
        models = set()
        external_sources, external_models = self.external_bindings(name, unit)
        sources.extend(external_sources)
        models.update(external_models)

        for producer in self.upstream[name]:
            folder = self.raw / producer
            decision = json.loads((folder / 'decision.json').read_text())
            if decision['outcome']['state'] != 'success':
                raise q.UnresolvedAttempt('producer has no accepted settlement')
            prior = json.loads((folder / 'settled.json').read_text())
            actual = prior['units'][0]['actual_launch']
            models.add(canonical_producer(self.dispatcher, prior['units'][0]['route'], actual))
            sources.extend({'path': r['path'], 'sha256': r['sha256']} for r in decision['outcome']['artifacts'])
        verify_sources(sources)
        return sources, ','.join(sorted(models)) or None

    def external_bindings(self, name, unit):

        sources, models = [], set()
        for declared in self.config['nodes'][name].get('external_producers', []):
            binding = {'path': declared['path'], 'sha256': declared['sha256']}
            verify_sources([binding])
            bundle = json.loads(Path(binding['path']).read_text())
            records = [r for r in bundle.get('units', []) if r.get('id') == declared['unit_id']]
            if len(records) != 1:
                raise ValueError('external producer unit missing or ambiguous')
            record = records[0]
            if (not bundle.get('run_id') or record.get('task_id') != declared['task_id'] or
                    record.get('dispatch_id') != declared['dispatch_id'] or record.get('status') != 'succeeded' or
                    (record.get('worker_done') or {}).get('outcome') != 'succeeded' or
                    (record.get('contract') or {}).get('accepted') is not True):
                raise ValueError('external producer lacks exact accepted settlement')
            models.add(canonical_producer(self.dispatcher, record['route'], record['actual_launch']))
                                                                                    
            for artifact in declared['artifacts']:
                if artifact['path'] not in {s['path'] for s in unit.get('source_bindings', [])}:
                    raise ValueError('external producer artifact must be a node source binding')
                if not any(r.get('path') == artifact['path'] and r.get('sha256') == artifact['sha256']
                        and r.get('exists') for r in record.get('outputs', [])):
                    raise ValueError('external artifact absent from accepted output receipt')
                verify_sources([artifact]);sources.append(artifact)
            if not declared['artifacts']:
                raise ValueError('external producer needs selected artifacts')
            sources.append(binding)
        return sources, models

    def check_capability_route(self, unit, route, producer_models):
        if unit.get('capability') != 'praxis_native_claim_check':
            return
        if not producer_models:
            identity = unit.get('producer_identity')
            if not isinstance(identity, dict) or identity.get('kind') not in {'user', 'artifact'} or not identity.get('label'):
                raise ValueError('claim check requires producer models or honest user/artifact identity')
            bound = {'path': identity.get('path'), 'sha256': identity.get('sha256')}
            if bound not in unit.get('source_bindings', []):
                raise ValueError('user/artifact producer must be an exact frozen source')
            verify_sources([bound])
            return
        registry = self.dispatcher.named_launch_module().registry
        models = {r['id']: r for r in registry._load_yaml(registry.institution()/'MODEL-REGISTRY.yaml')['models']}
        if route['provider'] in {models[mid]['provider'] for mid in producer_models.split(',')}:
            raise ValueError('claim check refused same-provider route')

    def pending_entry(self, name):
        unit = self.unit(name)
        return {'graph_run_id': self.graph_run_id, 'node_id': name,
            'config_sha256': self.config_sha, 'candidate_commit': self.config['candidate_commit'],
            'graph_sha256': hashlib.sha256(self.config['graph'].encode()).hexdigest(),
            'closure': self.config.get('closure', []), 'context': self.config['context'],
            'sources': unit.get('source_bindings', []), 'route': {}, 'identities': {},
            'phase': 'binding_pending', 'timestamp': utc()}

    async def run(self, node, prompt, context, incoming_edge=None, graph=None):
        folder = self.raw / node.id
        try:
            folder.mkdir(exist_ok=True)
            return await self._run(node, context, folder)
        except (Exception, asyncio.CancelledError, q.UnresolvedAttempt) as exc:

            if isinstance(exc, q.UnresolvedAttempt) and node.id in self.unresolved:
                raise
            try:
                entry = json.loads((folder / 'entry.json').read_text())
            except (OSError, ValueError):
                entry = self.pending_entry(node.id)
            return self.halt(folder, entry, str(exc) or type(exc).__name__, preserve=True)

    def engine_outcome(self, name, outcome):
        self.states[name] = outcome['state']
        if outcome['state'] == 'unresolved':
            self.unresolved[name] = outcome
            raise q.UnresolvedAttempt(outcome['reason'])
        mod = q.MODULES['outcome']
        result = mod.Outcome(status=mod.StageStatus.SUCCESS if outcome['state'] == 'success' else mod.StageStatus.FAIL,
                             is_explicit=True)
        self.receipts[name] = result
        return result

    def project(self, folder, decision):

        try:
            from native_ci import capture
            capture(self.directory, folder, decision)
        except Exception as exc:
            self.ci_errors.append({'node_id': decision['entry']['node_id'], 'error': str(exc)})

    async def _run(self, node, context, folder):
        guard_context(context.snapshot())
        unit = self.unit(node.id)
        plan = self.plan(node.id, unit)
        entry_path = folder / 'entry.json'
        if unit.get('capability'):
            unit['spec']=unit['spec'].replace('<bound-native-entry>',str(entry_path.resolve()))
        prior = entry_path.exists()
        if prior:
            entry = json.loads(entry_path.read_text())
            if entry['config_sha256'] != self.config_sha or entry['graph_run_id'] != self.graph_run_id:
                raise q.UnresolvedAttempt('original binding mismatch')
            if unit.get('capability'):
                from immutable_code import verify_closure
                verify_closure(entry)
                verify_sources(entry['sources'])
            else:
                verify_sources(entry['sources'] + entry.get('closure', []))
            unit = prepare_bound_unit(unit, entry['sources'])
        else:
            entry = self.pending_entry(node.id)

            sources, producer_models = self.bindings(node.id, unit)
            unit = prepare_bound_unit(unit, sources)
            route = self.router.route(unit, None, False, run_key=self.graph_run_id,
                                      producer_model=producer_models)
            self.check_capability_route(unit, route, producer_models)
            supplied = {}
            spec = self.dispatcher.unit_spec(plan, unit, route, supplied)
            entry = {'graph_run_id': self.graph_run_id, 'node_id': node.id,
                'config_sha256': self.config_sha, 'workflow_sha256': digest(self.config['graph']),
                'graph_sha256': hashlib.sha256(self.config['graph'].encode()).hexdigest(),
                'candidate_commit': self.config['candidate_commit'], 'closure': self.config.get('closure', []), 'context': self.config['context'],
                'context_sha256': digest(self.config['context']), 'supplied_context': supplied,
                'spec_sha256': hashlib.sha256(spec.encode()).hexdigest(), 'sources': sources,
                'route': route, 'producer_model': producer_models, 'identities': {}, 'timestamp': utc()}
            if unit.get('capability'):
                entry['code_identity']=self.config.get('code_identity')
                entry['checker_bootstrap']=self.config.get('checker_bootstrap',[])
                claim_policy=json.loads(Path(unit['claim_input']).read_bytes()).get('evidence_policy','frozen-corpus') if unit.get('claim_input') else 'bounded-public-web'
                entry['evidence_policy']='bounded-public-web' if claim_policy=='bounded-public-web' else 'frozen-corpus'
                spec_path=folder/'prepared-spec.txt'
                with spec_path.open('xb') as frozen_spec:
                    frozen_spec.write(spec.encode());frozen_spec.flush();__import__('os').fsync(frozen_spec.fileno())
                entry['frozen_spec']={'path':str(spec_path.resolve()),'sha256':entry['spec_sha256']}
                if unit.get('claim_derivation'):entry['claim_derivation']=copy.deepcopy(unit['claim_derivation'])
                if unit.get('acquisition'):
                    from acquisition import reconstruct
                    reconstruct(unit['acquisition'])
                    entry['acquisition']=copy.deepcopy(unit['acquisition'])
                entry['operation_log']=unit['operation_log']
                entry['control_bindings']=unit.get('control_bindings',[])
            durable_json(entry_path, entry)
        key = self.graph_run_id + '/' + node.id
        lease = self.dispatcher.RunLease(self.directory / 'leases', key, folder / 'bundle.json')
        observer = self.observer(folder, entry, plan)
        if (folder / 'settled.json').exists() and (folder / 'final.json').exists():
            bundle = json.loads((folder / 'settled.json').read_text())
        elif prior:
            if not all(entry['identities'].get(k) for k in IDENTITIES):
                return self.halt(folder, entry, 'entry before known launch; no exact original Dispatch')
            state = lease._read();lease._validate(state)
            worker = state['workers'].get(node.id) or {}
            if (state['run_id'] != entry['identities']['run_id'] or
                    any(worker.get(k) != entry['identities'][k] for k in ('task_id', 'dispatch_id'))):
                return self.halt(folder, entry, 'lease differs from exact original identities')
            lease.recovery_state = state
                                                                                 
            lease.acquired = True
            result = self.dispatcher.execute(plan, FrozenRouter(self.router, entry['route']), CollectOriginal(self.orca),
                dry_run=False, log_routes=False, run_key=key, lease=lease, output_path=folder / 'bundle.json',
                receipt_observer=observer)
            durable_json(folder / 'final.json', result)
            if not (folder / 'settled.json').exists():
                record = (result.get('units') or [{}])[0]
                detail = '; '.join(str(r) for r in record.get('unresolved', []))
                return self.halt(folder, entry, 'original has no settled worker_done: ' + record.get('status', 'unknown') + '; ' + detail)
            bundle = json.loads((folder / 'settled.json').read_text())
        else:
            entered = claim_entry(self.directory / 'execution-intents', workspace=self.workspace,
                graph_run_id=self.graph_run_id, node_id=node.id, visit=self.visits.get(node.id, 1), timestamp=utc())
            from projection import read_events, ENTRY_EVENT
            intents = [r for r in read_events(self.directory / 'execution-intents') if r['event'] == ENTRY_EVENT and r['data']['node_id'] == node.id]
            if intents:
                durable_json(folder / 'intent.json', intents[0])
            if not entered:
                return self.halt(folder, entry, 'C7 prior intent refuses backend re-entry')
            if not lease.acquire()[0]:
                return self.halt(folder, entry, 'preexisting dispatcher lease refuses new execution')
            self.entry_calls[node.id] = self.entry_calls.get(node.id, 0) + 1
            try:
                result = self.dispatcher.execute(plan, FrozenRouter(self.router, entry['route']), self.orca,
                    dry_run=False, log_routes=False, run_key=key, lease=lease,
                    output_path=folder / 'bundle.json', receipt_observer=observer)
                durable_json(folder / 'bundle.json', result)
                durable_json(folder / 'final.json', result)
                lease.update(status=result['status'])
            except (Exception, asyncio.CancelledError) as exc:
                return self.halt(folder, entry, 'native launch/contact/collection unresolved: ' + exception_detail(exc))
            if not (folder / 'settled.json').exists():
                return self.halt(folder, entry, 'no settled original worker_done')
            bundle = json.loads((folder / 'settled.json').read_text())
        bundle['dispatcher_final'] = json.loads((folder / 'final.json').read_text())
        durable_json(folder / 'adjudication.json', bundle)

        accepted = folder / 'decision.json'
        if accepted.exists():
            saved = json.loads(accepted.read_text())
            if saved['outcome']['state'] in {'success', 'fail'}:
                if saved['entry_sha256'] != sha(entry_path) or saved['entry'] != entry:
                    return self.halt(folder, entry, 'post-acceptance entry binding drift', preserve=True)
                if saved['raw_receipt_sha256'] != sha(folder / 'adjudication.json'):
                    return self.halt(folder, entry, 'post-acceptance native receipt drift', preserve=True)
                if self.unit(entry['node_id']).get('capability'):
                    from immutable_code import verify_closure
                    verify_closure(entry)
                    verify_sources(entry['sources'])
                else:
                    verify_sources(entry['sources'] + entry.get('closure', []))
                for artifact in saved['outcome']['artifacts']:
                    if artifact['validated'] and (not Path(artifact['path']).is_file()
                            or Path(artifact['path']).is_symlink() or sha(artifact['path']) != artifact['sha256']):
                        return self.halt(folder, entry, 'post-acceptance artifact drift: ' + artifact['path'], preserve=True)
                outcome = saved['outcome']
                versions = [json.loads(p.read_text()) for p in folder.glob('decision-*.json')]
                latest = max(versions, key=lambda r: (r['timestamp'], digest(r))) if versions else saved
                if latest['outcome'] != outcome:
                    saved = self.decision(folder, entry, outcome, preserve=True, reconfirm=True)
                self.project(folder, saved)
                return self.engine_outcome(node.id, outcome)
                                                                      
        try:
            outcome = node_outcome(bundle, entry, unit)
        except q.UnresolvedAttempt as exc:
            return self.halt(folder, entry, str(exc))
        decision = self.decision(folder, entry, outcome)
        self.project(folder, decision)
        return self.engine_outcome(node.id, outcome)

    def decision(self, folder, entry, outcome, *, preserve=False, reconfirm=False):
        value = {'entry_sha256': sha(folder / 'entry.json') if (folder / 'entry.json').exists() else None, 'entry': copy.deepcopy(entry),
                 'outcome': outcome, 'timestamp': utc(), 'raw_receipt_path': str(folder / 'adjudication.json'),
                 'raw_receipt_sha256': sha(folder / 'adjudication.json') if (folder / 'adjudication.json').exists() else None,
                 'corrections': self.config.get('corrections', []),
                 'consumes': self.config['nodes'][entry['node_id']].get('consumes', [])}
        if self.config.get('kind') == 'correction' and self.config.get('commission'):
            rule = self.config['commission']['rule']
            value['corrections'] = list(value['corrections']) + [rule] if rule not in value['corrections'] else value['corrections']
            value['consumes'] = sorted(set(value['consumes'] + [rule['id']]))
        prior = folder / 'decision.json'
        if prior.exists():
            old = json.loads(prior.read_text())
            if not reconfirm and {k:v for k,v in old.items() if k != 'timestamp'} == {k:v for k,v in value.items() if k != 'timestamp'}:
                return old
        if value['raw_receipt_sha256']:
            snapshot = folder / ('receipt-' + value['raw_receipt_sha256'] + '.json')
            if not snapshot.exists():
                                                                                  
                raw = (folder / 'adjudication.json').read_bytes()
                with snapshot.open('xb') as handle:
                    handle.write(raw);handle.flush()
                    import os
                    os.fsync(handle.fileno())
                from projection import fsync_directory
                fsync_directory(folder)
        durable_json(folder / ('decision-' + digest(value) + '.json'), value)
        if not preserve or not prior.exists() or old['outcome']['state'] == 'unresolved':
            durable_json(prior, value)
        return value

    def halt(self, folder, entry, reason, *, preserve=False):
        outcome = {'state': 'unresolved', 'reason': reason, 'artifacts': []}
        self.states[entry['node_id']] = 'unresolved'
        self.unresolved[entry['node_id']] = outcome
        try:
            folder.mkdir(exist_ok=True)
            decision = self.decision(folder, entry, outcome, preserve=preserve)
            self.project(folder, decision)
        except Exception as exc:

            outcome['persistence_error'] = str(exc)
        raise q.UnresolvedAttempt(reason)

    def clone(self):
        raise q.UnresolvedAttempt('native parallel/clone execution refused')

def utc():
    return datetime.now(timezone.utc).isoformat()

def graph_states(adapter):
    states = {}
    stopped = None
    for name in adapter.order:
        if stopped:
            states[name] = 'not_reached_after_failure' if stopped == 'fail' else 'held_unstarted'
            continue
        states[name] = adapter.states.get(name, 'held_unstarted')
        if states[name] != 'success':
            stopped = states[name]
    values = set(states.values())
    status = ('succeeded' if states and values == {'success'} else
              'failed' if 'fail' in values else 'unresolved')
    return status, states

def execute_config(config_bytes, dispatcher, router, orca, *, adapter_factory=NativeWorkerAdapter):
    from candidate_run import validate_candidate
    validate_candidate(json.loads(config_bytes))
    adapter = adapter_factory(config_bytes, dispatcher, router, orca, persist=False)
    adapter.directory.mkdir(parents=True, exist_ok=True)
    lock = (adapter.directory / '.native.lock').open('a+')
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
                                                                            
            return {'graph_run_id': adapter.graph_run_id, 'config_sha256': adapter.config_sha,
                    'invocation': 'concurrent_observer', 'status': 'unresolved', 'nodes': {},
                    'backend_entries': {}, 'unresolved': {}, 'ci_status': 'not_written', 'ci_errors': [],
                    'invocation_observation': 'concurrent same-config invocation; occurrence lock held'}
        adapter.lock_held = True
        existed = (adapter.directory / 'config.json').exists() or any(adapter.raw.glob('*/entry.json'))
        adapter.persist_config(config_bytes)
        engine = q.construct(adapter.graph, adapter, adapter.directory / 'engine',
                             context=isolate_context(q.MODULES['context'].PipelineContext()))
        if existed:

            for name in adapter.order:
                if not (adapter.raw / name / 'entry.json').exists():
                    break
                outcome = asyncio.run(adapter.run(adapter.graph.nodes[name], '', engine.context))
                if outcome.status != q.MODULES['outcome'].StageStatus.SUCCESS:
                    break
        else:
            result = asyncio.run(engine.run())
            if result.status != q.MODULES['outcome'].StageStatus.SUCCESS and not adapter.unresolved:

                adapter.states.setdefault(adapter.order[-1], 'unresolved')
    except q.UnresolvedAttempt:
        pass
    finally:
        adapter.lock_held = False
        lock.close()
    status, states = graph_states(adapter)
    return {'graph_run_id': adapter.graph_run_id, 'config_sha256': adapter.config_sha,
            'invocation': 'same_config_original_only' if existed else 'new_run',
            'status': status, 'nodes': states, 'backend_entries': adapter.entry_calls,
            'unresolved': adapter.unresolved,
            'ci_status': 'capture_failed' if adapter.ci_errors else 'captured', 'ci_errors': adapter.ci_errors}
