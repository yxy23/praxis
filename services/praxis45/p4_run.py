
import argparse
import copy
import functools
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_qualification as q
import native_adapter as n
import native_ci
from native_capabilities import EVIDENCE, CLAIMS, validate_file, validate_operations
from p4_output_contract import instructions, self_check, validate_p4_output
from p4_contract import (KILL_POINTS, SUCCESS_MEANING, EVALUATION_LABEL, RULE_ORIGIN_LABEL, RULE_ORIGIN_INSTRUCTION, BoundRouter, accepted_artifact,
    binding, commission_correction, digest, durable_json, guard_disabled, inventory,
    load, occurrence, pair_proof, require, sha, substantive, validate_config, normalize_prepared_spec)

class KillHarness:

    def __init__(self, directory, declared, requested):
        require(requested is None or requested == declared, '--kill-at differs from declared interruption point')
        self.directory, self.point = Path(directory), requested
    def observe(self, stage, payload):
        if self.point and stage == KILL_POINTS[self.point]:
            marker = self.directory / 'controller-kill.json'
            if not marker.exists():
                durable_json(marker, {'point': self.point, 'stage': stage, 'controller_pid': os.getpid(),
                    'native_identities': payload, 'effect': 'local controller exit only; no worker signal'})
                os._exit(86)

def evidence_manifest(producer,retained_set,prior=None):
    from native_capabilities import validate_claim_input
    rows = []
    sources = {r['path']:r for r in retained_set}
    for claim in producer['claims']:
        paths = []
        for evidence in claim['evidence']:
            if evidence['basis']=='search_result_only':
                continue
            path = evidence['source'];paths.append(path)
            require(path in sources and sources[path]['sha256']==evidence['content_sha256'], 'claim evidence not in log-derived retained set')
        if not paths:
            require(claim.get('missing_evidence'), 'no retained evidence requires explicit missing_evidence')
        rows.append({'claim_id':claim['claim_id'],'claim':claim['claim'],'source_paths':paths})
    prior_budget=prior.get('source_budget',{}) if prior else {}
    require(len(sources)<=prior_budget.get('retained_sources',8),'producer retained set exceeds frozen checker budget')
    manifest = {'claims':rows,'sources':list(sources.values()),'evidence_policy':'retained-or-unresolved','source_budget':{
        'web_discovery_queries_max':0,'complete_retrievals_max':prior_budget.get('complete_retrievals_max',6),
        'retained_sources':min(len(sources),prior_budget.get('retained_sources',8))}}
    validate_claim_input(manifest)
    if prior:require([(r['claim_id'],r['claim']) for r in manifest['claims']]==[(r['claim_id'],r['claim']) for r in prior['claims']], 'derived claims differ from frozen input')
    return manifest

def validate_claim_derivation(unit,entry,frozen):

    declared=entry.get('claim_derivation')
    require(isinstance(declared,dict) and declared==unit.get('claim_derivation'), 'native claim derivation not frozen in entry')
    configured=frozen['nodes'][entry['node_id']]
    source=configured.get('bind_evidence_from')
    require(source and declared.get('producer_node')==source and declared.get('kind')=='D2-evidence-bound', 'native claim derivation not declared by frozen node')
    original=configured.get('claim_input');original_binding=declared.get('frozen_claim_input')
    require((original_binding or {}).get('path')==original, 'native original claim binding differs')
    bindings=[declared['producer_receipt'],declared['producer_decision'],declared['derived_claim_input']]
    if original_binding:bindings.append(original_binding)
    n.verify_sources(bindings)
    prebound={r['path']:r['sha256'] for r in entry['sources']+entry.get('control_bindings',[])}
    for item in (declared['producer_receipt'],declared['derived_claim_input'],original_binding):
        if item:require(prebound.get(item['path'])==item['sha256'], 'native derivation input not prebound')
    require(unit.get('claim_input')==declared['derived_claim_input']['path'], 'native derived claim input differs')
    require(frozen['nodes'][source]['outputs'][0]['path']==declared['producer_receipt']['path'], 'native producer receipt differs from frozen node')
    decision=load(declared['producer_decision']['path'])
    require(decision['entry']['node_id']==source and decision['entry']['graph_run_id']==entry['graph_run_id'] and decision['outcome']['state']=='success', 'native derivation producer not accepted')
    require(any(r.get('path')==declared['producer_receipt']['path'] and r.get('sha256')==declared['producer_receipt']['sha256'] for r in decision['outcome']['artifacts']), 'native derivation producer receipt not accepted')
    producer=load(declared['producer_receipt']['path'])
    from operation_evidence import verify
    verification=verify(producer,decision['entry'],frozen['nodes'][source])
    require(verification['coverage']['delivery_state']=='delivered','held producer evidence cannot enter agent context')
    retained=verification['retained_originals']
    from native_capabilities import validate_claim_input
    prior=validate_claim_input(original,discovery=True) if original else None
    expected=(json.dumps(evidence_manifest(producer,retained,prior),sort_keys=True,indent=2)+'\n').encode()
    require(Path(unit['claim_input']).read_bytes()==expected, 'native claim derivation bytes differ')
    n.verify_sources(bindings)
    return unit['claim_input']

class P4Adapter(n.NativeWorkerAdapter):
    def __init__(self, *args, kill_at=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.harness = KillHarness(self.directory, self.config['interruption_point'], kill_at)
    def unit(self, name):
        unit = super().unit(name)
        if unit.get('bind_evidence_from') and self.states.get(unit['bind_evidence_from']) == 'success':
            source = unit['bind_evidence_from']
            producer_path = self.config['nodes'][source]['outputs'][0]['path']
            accepted_artifact(self.directory, producer_path)
            producer = load(producer_path)
            from operation_evidence import verify
            producer_entry=load(self.directory/'raw'/source/'entry.json')
            verification=verify(producer,producer_entry,self.config['nodes'][source])
            require(verification['coverage']['delivery_state']=='delivered','held producer evidence cannot enter agent context')
            retained_set=verification['retained_originals']
            unit['spec']+='\nInherited coverage label (must travel): '+json.dumps(verification['coverage'],sort_keys=True)
            from native_capabilities import validate_claim_input
            prior=validate_claim_input(unit['claim_input'],discovery=True) if unit.get('claim_input') else None
            manifest=evidence_manifest(producer,retained_set,prior)
            path = Path(self.config['output_root'])/'evidence-bound-claims.json'
            raw = (json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
            require(not path.exists() or path.read_bytes() == raw, 'frozen claim evidence bindings changed')
            if not path.exists():path.write_bytes(raw)
            unit['claim_derivation']={'kind':'D2-evidence-bound','producer_node':source,
                'producer_receipt':binding(producer_path),
                'producer_decision':binding(self.directory/'raw'/source/'decision.json'),
                'frozen_claim_input':binding(unit['claim_input']) if unit.get('claim_input') else None,
                'derived_claim_input':binding(path)}
            unit['claim_input'] = str(path)
            from operation_evidence import log_path
            producer_log=log_path(self.config['nodes'][source],producer_entry['identities']['dispatch_id'])
            unit['source_bindings'] += [binding(producer_path),binding(producer_log),binding(path)] + [{'path':r.get('retained_path',r['path']),'sha256':r['sha256']} for r in manifest['sources']]
            unit['spec'] += '\nExact evidence-bound claim input: '+str(path)
                                                                                   
        unit['spec'] += '\nWrite required outputs exactly here: ' + json.dumps(unit['outputs'], sort_keys=True)
        if unit.get('capability') in {EVIDENCE, CLAIMS}:
            from native_capabilities import validate_claim_input
            research={r['path'] for r in self.config.get('research_sources',[])}
            if unit.get('claim_input'):
                bound=validate_claim_input(unit['claim_input'],discovery=bool(unit.get('evidence_discovery')))
                research.update(r['path'] for r in bound['sources'])
                research.update(r['retained_path'] for r in bound['sources'] if r.get('retained_path'))
            controls={r['path']:r for r in [*self.config.get('inputs',[]),*unit.get('source_bindings',[])] if r['path'] not in research}
            config_path=self.directory/'config.json'
            if config_path.is_file():controls[str(config_path)]=binding(config_path)
            unit['control_bindings']=[controls[path] for path in sorted(controls)]
            unit['spec']+='\nUse control op for these frozen non-research bindings (never research reads or evidence): '+json.dumps(unit['control_bindings'],sort_keys=True)
                                                                                 
            unit.setdefault('operation_log',str(Path(unit['outputs'][0]['path']).parent/'operations'/'{dispatch_id}.jsonl'))
            protocol=instructions(unit['capability'],unit['operation_log'])
            if protocol not in unit['spec']:
                unit['spec'] += '\n' + protocol
            label = 'Include evaluation_label exactly ' + json.dumps(EVALUATION_LABEL) + ' in your receipt.'
            if label not in unit['spec']:
                unit['spec'] += '\n' + label
            if self.config['kind'] == 'correction' or self.config['nodes'][name].get('consumes'):
                unit['spec'] += '\n' + RULE_ORIGIN_INSTRUCTION
            for output in unit['outputs']:
                unit['spec'] += '\nRequired candidate self-check: ' + self_check(
                    unit['capability'], output['path'], unit.get('claim_input'),
                    config_path=self.directory / 'config.json', node=name)
        if unit.get('capability'):
            unit['spec']=unit['spec'].replace('<bound-native-entry>',str((self.directory/'raw'/name/'entry.json').resolve()))
        return unit
    def observer(self, folder, entry, plan):
        observe_native = super().observer(folder, entry, plan)
        def observe(stage, payload):
            observe_native(stage, payload)
            if stage == 'prepared':
                unit = n.prepare_bound_unit(self.unit(entry['node_id']), entry['sources'])
                context = {}
                spec = self.dispatcher.unit_spec(plan, unit, entry['route'], context)
                require(hashlib.sha256(spec.encode()).hexdigest() == payload['units'][0]['spec_sha256']
                        and context == payload['units'][0]['context'], 'actual prepared spec reconstruction differs')
                spec_path = folder / 'prepared-spec.txt'
                require(not spec_path.exists() or spec_path.read_text() == spec, 'original prepared spec changed')
                if not spec_path.exists():
                    spec_path.write_text(spec)
                normalized = normalize_prepared_spec(spec, self.config, entry['node_id'])
                durable_json(folder / 'prompt-digests.json', {
                    'raw_context_sha256': digest(payload['units'][0]['context']),
                    'raw_spec_sha256': payload['units'][0]['spec_sha256'],
                    'config_substantive_prompt_sha256': digest(substantive(self.config, entry['node_id'])),
                    'prepared_spec': binding(spec_path),
                    'normalized_substantive_prompt_sha256': hashlib.sha256(normalized['text'].encode()).hexdigest(),
                    'prepared_spec_normalization': normalized,
                    'supplied_source_content_digests': entry['sources'],
                    'rule_digests': [r for r in self.config.get('corrections', [])
                                     if r['id'] in self.config['nodes'][entry['node_id']].get('consumes', [])],
                    'normalized_basis': normalized['basis']})
            if stage == 'settled':
                durable_json(folder / 'worker-inventory.json', inventory(self.orca, entry['identities']))
            self.harness.observe(stage, payload)
        return observe
    def decision(self, folder, entry, outcome, **kwargs):
        if outcome['state'] == 'success' and self.config['nodes'][entry['node_id']].get('capability'):
            value = load(self.config['nodes'][entry['node_id']]['outputs'][0]['path'])
            validate_p4_output(value, self.config, entry['node_id'])
            unit=self.config['nodes'][entry['node_id']]
            if unit.get('bind_evidence_from'):unit=self.unit(entry['node_id'])
            validate_file(unit['outputs'][0]['path'],unit['capability'],claim_input=unit.get('claim_input'))
            from operation_recount import verify
            verified=verify(value,entry,unit)
            require(isinstance(verified.get('coverage'),dict),'independent coverage proof missing')
            coverage=verified['coverage']
            outcome=dict(outcome,coverage=[coverage],operational_refusal=None)
            if coverage['delivery_state']!='delivered':
                outcome.update(state='unresolved',reason='coverage delivery held; operational validation passed')
        return super().decision(folder, entry, outcome, **kwargs)

def runtime_dispatcher():
    return n.load_dispatcher()

def runtime(config):
    dispatcher = runtime_dispatcher()
    command = dispatcher.JsonCommand(dispatcher.launch_env())
    router = BoundRouter(dispatcher.RouterClient(dispatcher.default_router(), command), config)
    orca = dispatcher.OrcaClient(dispatcher.resolve_orca(None), command)
    return dispatcher, router, orca

def preview(config, dispatcher, router, config_bytes=None):
    validate_config(config)
    q.configure(*__import__('candidate_run').engine_paths(config))
    if config['kind'] == 'p1-mixed':
        plan = mixed_plan(config)
        result = dispatcher.execute(plan, router, None, dry_run=True, log_routes=False)
        return {'status': 'validated', 'native_launches': 0, 'dry_run': result}
    adapter = P4Adapter(config_bytes or json.dumps(config).encode(), dispatcher, router, None, persist=False)
    rows, producers = {}, []
    for name in adapter.order:
        unit = adapter.unit(name)
        if config['nodes'][name].get('external_producers'):
            _, external = adapter.external_bindings(name, unit)
            producer = ','.join(sorted(external))
        else:
            producer = ','.join(producers) or None
        route = router.route(unit, None, False, run_key=adapter.graph_run_id, producer_model=producer)
        adapter.check_capability_route(unit, route, producer)
        context = {}
        spec = dispatcher.unit_spec(adapter.plan(name, unit), unit, route, context)
        rows[name] = {'route': route, 'raw_spec_sha256': hashlib.sha256(spec.encode()).hexdigest(),
            'raw_context_sha256': digest(context), 'config_substantive_prompt_sha256': digest(substantive(config, name)),
            'supplied_source_content_digests': unit.get('source_bindings', [])}
        producers.append(route['effective_model'])
    return {'status': 'validated', 'native_launches': 0, 'routes': rows,
            'graph_run_id': adapter.graph_run_id, 'config_sha256': adapter.config_sha,
            'graph_id_basis': 'SHA256 of raw config file bytes'}

def mixed_plan(config):
    units = []
    for name, node in config['nodes'].items():
        unit = copy.deepcopy(node);unit['id'] = name
        unit['spec'] += '\nWrite required outputs exactly here: ' + json.dumps(unit['outputs'], sort_keys=True)
        units.append(unit)
    return {'schema_version': '1', 'id': config['context']['identity'], 'objective': config['objective'],
            'context': config['context'], 'defaults': config['defaults'], 'units': units}

def mixed_run(raw, config, dispatcher, router, orca, kill_at):
    import fcntl
    directory = occurrence(raw);directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.native.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'unresolved', 'reason': 'occurrence lock held'}
        frozen = directory / 'config.json'
        require(not frozen.exists() or frozen.read_bytes() == raw, 'mixed config changed')
        if not frozen.exists():
            frozen.write_bytes(raw)
        final = directory / 'mixed-final.json'
        if final.exists():
            return load(final)
        key = directory.name + '/mixed'
        lease = dispatcher.RunLease(directory / 'leases', key, final)
        if not lease.acquire()[0]:
            return {'status': 'unresolved', 'reason': 'original controller or lease still active'}
        harness = KillHarness(directory, config['interruption_point'], kill_at)
        entry = {'config_sha256': hashlib.sha256(raw).hexdigest(), 'candidate_commit': config['candidate_commit'],
                 'identities': {}, 'inputs': config['inputs'], 'nodes': {}}
        saved = directory / 'mixed-entry.json'
        if saved.exists():
            entry = load(saved)
        def observe(stage, payload):
            if stage == 'prepared':
                entry['prepared'] = payload
            for field in n.IDENTITIES:
                if payload.get(field):
                    entry['identities'][field] = payload[field]
            if stage == 'tasks_created':
                entry['tasks'] = payload['tasks']
                for name, task_id in payload['tasks'].items():
                    node = config['nodes'][name]
                    entry['nodes'][name] = {'entry_intent': 'durable created Task before starting',
                        'identities': {'run_id': payload['run_id'], 'task_id': task_id},
                        'route': node['frozen_route'], 'inputs': node['source_bindings'],
                        'config_substantive_prompt_sha256': digest(substantive(config, name)),
                        'raw_prompt_binding': next(r for r in entry['prepared']['units'] if r['id'] == name)}
            if stage == 'launch':
                matches = [row for row in entry['nodes'].values() if row['identities']['task_id'] == payload['task_id']]
                require(len(matches) == 1, 'mixed original Task binding missing')
                matches[0]['identities']['dispatch_id'] = payload['dispatch_id']
                matches[0]['launch_receipt'] = payload['receipt']
            if stage == 'settled':
                native = dispatcher.payload_json(payload['native_message'])
                ids = {'run_id': payload['run_id'], 'task_id': native.get('taskId'),
                       'dispatch_id': native.get('dispatchId')}
                require(any(row['identities'] == ids for row in entry['nodes'].values()),
                        'mixed settlement differs from original Task/Dispatch')
                durable_json(directory / 'mixed-worker-inventory.json', inventory(orca, ids))
                durable_json(directory / 'mixed-settled.json', payload)
            durable_json(saved, entry)
            harness.observe(stage, payload)
        result = dispatcher.execute(mixed_plan(config), router, orca, dry_run=False, log_routes=False,
            run_key=key, lease=lease, output_path=final, receipt_observer=observe)
                                                                                  
        dependent = next(r for r in result['units'] if r['id'] == 'dependent')
        require(dependent['status'] == 'held_dependent' and not dependent['dispatch_id'], 'dependent launched or not held')
        result.update(graph_run_id=directory.name, success_meaning=SUCCESS_MEANING)
        lease.update(status=result['status'])
        durable_json(final, result)
        return result

def heldout_gate(config):
    if config['kind'] != 'heldout':
        return
    gate = config['correction_gate']
    corrected_raw = Path(gate['config_path']).read_bytes()
    corrected = json.loads(corrected_raw)
    validate_config(corrected)
    require(corrected['kind'] == 'correction' and corrected.get('commission'), 'commissioned correction required before held-out access')
    directory = occurrence(corrected_raw)
    produced, _ = accepted_artifact(directory, gate['rule_record_path'])
    audited, _ = accepted_artifact(directory, gate['check_path'])
    require(produced['entry']['node_id'] == 'research' and audited['entry']['node_id'] == 'audit', 'correction lineage differs')
    rule_record = load(gate['rule_record_path']).get('rule_record') or {}
    review = load(gate['check_path']).get('rule_review') or {}
    require(rule_record.get('rule_sha256') == gate['rule_sha256'] and review.get('accepted') is True
            and review.get('rule_sha256') == gate['rule_sha256'], 'held-out rule not frozen and independently accepted')

def status(raw, config, client=None):
    directory = occurrence(raw)
    result = {'graph_run_id': directory.name, 'success_meaning': SUCCESS_MEANING, 'nodes': {}}
    if config['kind'] == 'p1-mixed':
        final = directory / 'mixed-final.json'
        result['result'] = load(final) if final.exists() else {'status': 'unresolved'}
        records = result['result'].get('units', [])
    else:
        records = []
        for name in config['nodes']:
            folder = directory / 'raw' / name
            decision = folder / 'decision.json';final = folder / 'final.json'
            entry = load(folder / 'entry.json') if (folder / 'entry.json').exists() else {}
            row = {'id': name, 'identities': entry.get('identities', {}),
                   'result_state': load(decision)['outcome'] if decision.exists() else {'state': 'unknown'},
                   'cleanup': 'unknown', 'delivery_cleanup': 'unknown'}
            if final.exists():
                bundle = load(final);record = bundle['units'][0]
                row.update(cleanup=record.get('cleanup', 'unknown'), release=record.get('release'),
                           delivery_cleanup=bundle.get('acknowledged_deliveries', 'unknown'),
                           dispatcher_status=bundle['status'])
            result['nodes'][name] = row
            ids = entry.get('identities', {})
            if client and all(ids.get(k) for k in n.IDENTITIES):
                try:
                    row['original_worker_resource_read'] = inventory(client, ids)
                except Exception as exc:
                    row['original_worker_resource_read'] = {'status': 'unverifiable', 'reason': str(exc)}
            elif (folder / 'worker-inventory.json').exists():
                row['original_worker_resource_read'] = load(folder / 'worker-inventory.json')
            else:
                row['original_worker_resource_read'] = {'status': 'unknown'}
    if records:
        for record in records:
            result['nodes'][record['id']] = {'result_state': record.get('status'), 'cleanup': record.get('cleanup'),
                'original_worker_resource_read': record.get('release', 'unknown'), 'delivery_cleanup': 'unknown',
                'identities': {k: record.get(k) for k in ('task_id', 'dispatch_id')}}
        if (directory / 'mixed-worker-inventory.json').exists():
            result['original_worker_inventory'] = load(directory / 'mixed-worker-inventory.json')

    result['delivery_state'] = []
    for path in sorted((directory / 'leases').glob('**/state.json')):
        lease = load(path)
        result['delivery_state'].append({'path': str(path), 'run_id': lease['run_id'],
            'unconsumed_delivery': lease.get('unconsumed_delivery'), 'workers': lease['workers'],
            'meaning': 'saved dispatcher state; native inbox not destructively queried'})
    return result

def immutable_bytes(directory, suffix, raw):
    from projection import fsync_directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(raw).hexdigest() + suffix)
    require(not path.exists() or path.read_bytes() == raw, 'immutable appendix evidence differs')
    if not path.exists():
        with path.open('xb') as handle:
            handle.write(raw);handle.flush();os.fsync(handle.fileno())
        fsync_directory(directory)
    return path

def make_appendix(raw, config):
    import fcntl
    directory = occurrence(raw);path = directory / 'appendix-map.json'
                                                                                   
    with (directory / '.native.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('appendix owner lock held; no map written')
        archive = directory / 'appendix'
        basis_paths = sorted((directory / 'raw').glob('*/decision.json')) + sorted((directory / 'raw').glob('*/final.json'))
        if (directory / 'mixed-final.json').exists():
            basis_paths.append(directory / 'mixed-final.json')
        refresh_basis = [binding(file) for file in basis_paths]
        if path.exists() and load(path).get('refresh_basis') == refresh_basis:
            return {'map_unchanged': True}
        if path.exists():
            immutable_bytes(archive / 'maps', '.json', path.read_bytes())
        artifacts, rejected = {}, []
        decisions = sorted((directory / 'raw').glob('*/decision.json'))
        for decision in decisions:
            value = load(decision)
            for row in value['outcome']['artifacts']:
                accepted = (value['outcome']['state'] == 'success' and row.get('validated') is True
                    and row.get('capability_validation', {'accepted': True}).get('accepted') is True)
                if accepted:
                    artifacts[row['path']] = row['sha256']
                elif row.get('sha256'):
                    rejected.append({'path': row['path'], 'sha256': row['sha256'], 'kind': 'rejected_artifact',
                        'reason': value['outcome'].get('reason'), 'decision': binding(
                            immutable_bytes(archive / 'evidence', '.json', decision.read_bytes()))})
        if config['kind'] == 'p1-mixed' and (directory / 'mixed-final.json').exists():
            for unit in load(directory / 'mixed-final.json')['units']:
                for row in unit.get('outputs', []):
                    if unit['status'] == 'succeeded' and (unit.get('contract') or {}).get('accepted') is True and row.get('sha256'):
                        artifacts[row['path']] = row['sha256']
        rows = [{'path': p, 'sha256': s, 'kind': 'accepted_artifact'} for p,s in artifacts.items()] + rejected
                                                                                  
        if artifacts and config['kind'] != 'p1-mixed':
            lines = ['# What the bound research sources support', '',
                'Evidence audit and system rehearsal; not completed substantive research.', '', SUCCESS_MEANING,
                'Evaluation label: ' + EVALUATION_LABEL, '']
            if config['kind'] == 'correction' or config.get('corrections'):
                lines += ['Rule label: ' + RULE_ORIGIN_LABEL + '; application is subject to independent output assessment.', '']
            for artifact, expected in artifacts.items():
                require(sha(artifact) == expected, 'artifact drift before rendering')
                value = load(artifact)
                lines += ['## ' + value['receipt_type'], str(value.get('narrative', '')), '',
                          'Coverage: ' + value['coverage_boundary']]
                for claim in value['claims']:
                    lines += ['- ' + claim['claim_id'] + ': ' + claim['claim'],
                        '  Evidence kind: ' + str(claim.get('evidence_kind', 'unspecified')) +
                        '; label: ' + str(claim.get('epistemic_label', 'inquiry only'))]
                    for evidence in claim['evidence']:
                        lines.append('  Source: ' + evidence['source'] + ' | ' + evidence['locator'] + ' | ' + evidence['passage'])
                if value.get('inherited_claim_fixture'):
                    lines += ['', 'Controlled inherited claim fixture: ' + json.dumps(value['inherited_claim_fixture'], ensure_ascii=False)]
                lines += ['', 'Uncertainty register: ' + json.dumps(value.get('missingness', [])), '']
            rendered = ('\n'.join(lines) + '\n').encode()
            version = immutable_bytes(archive / 'briefs', '.md', rendered)
            brief = Path(config['output_root']) / 'evidence-claims-brief.md'
            if brief.exists() and brief.read_bytes() != rendered:
                immutable_bytes(archive / 'briefs', '.md', brief.read_bytes())
            brief.write_bytes(rendered)
            rows.append(dict(binding(version), kind='derived_from_accepted_artifacts', current_alias=str(brief)))

        for file in sorted(directory.rglob('*.json')):
            if file == path or archive in file.parents or file.name == 'controller-result.json' or file.name.startswith('packet-'):
                continue
            frozen = immutable_bytes(archive / 'evidence', '.json', file.read_bytes())
            rows.append(dict(binding(frozen), kind='primary_evidence_snapshot', observed_path=str(file)))
        for file in sorted((directory / 'raw').glob('*/prepared-spec.txt')):
            rows.append(dict(binding(file), kind='actual_prepared_spec'))
        rows.extend(dict(r, kind='input') for r in config['inputs'])
        rows.extend(dict(r, kind='code_closure') for r in config['closure'])
        value = {'success_meaning': SUCCESS_MEANING, 'evaluation_label': EVALUATION_LABEL, 'artifacts': rows,
            'refresh_basis': refresh_basis,
            'hash_basis': 'accepted/rejected outputs retain receipt hashes; mutable primary evidence uses immutable captured-byte snapshots'}
        version = immutable_bytes(archive / 'maps', '.json', (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())
        durable_json(path, value)
        return {'map_version': binding(version), 'accepted_outputs': len(artifacts), 'rejected_outputs': len(rejected)}

def packet(raw, config, map_path=None, client=None):
    path = Path(map_path) if map_path else occurrence(raw) / 'appendix-map.json'
    require(path.is_file(), 'appendix map unavailable; no completed run packet')
    rows = load(path)['artifacts'];mismatches = []
    for row in rows:
        try:
            actual = sha(row['path'])
        except OSError:
            actual = None
        if actual != row['sha256']:
            mismatches.append({'path': row['path'], 'expected': row['sha256'], 'actual': actual})
    return {'success_meaning': SUCCESS_MEANING, 'checked_paths': len(rows), 'mismatches': mismatches,
            'lifecycle': status(raw, config, client), 'research_acceptance': 'requires independent rubric assessment and human scientific decisions'}

def heldout_comparison_lineage(config):
    if config['kind'] != 'heldout':
        return
    gate = config['correction_gate']
    corrected_raw = Path(gate['config_path']).read_bytes()
    corrected = json.loads(corrected_raw)
    validate_config(corrected,candidate=False)
    from native_adapter import verify_sources
    from immutable_code import verify_closure
    verify_closure(corrected)
    require(corrected['candidate_commit']==config['candidate_commit'], 'comparison correction candidate differs')
    require(corrected['kind'] == 'correction' and corrected.get('commission'), 'commissioned correction required before held-out access')
    directory = occurrence(corrected_raw)
    produced, _ = accepted_artifact(directory, gate['rule_record_path'])
    audited, _ = accepted_artifact(directory, gate['check_path'])
    require(produced['entry']['node_id'] == 'research' and audited['entry']['node_id'] == 'audit', 'correction lineage differs')
    rule_record = load(gate['rule_record_path']).get('rule_record') or {}
    review = load(gate['check_path']).get('rule_review') or {}
    require(rule_record.get('rule_sha256') == gate['rule_sha256'] and review.get('accepted') is True
            and review.get('rule_sha256') == gate['rule_sha256'], 'held-out rule not frozen and independently accepted')

def heldout_prepared_treatment(spec, config, name, config_raw):

    expected=[r for r in config.get('corrections',[]) if r['id'] in config['nodes'][name].get('consumes',[])]
    require(not expected or (name=='research' and len(expected)==1 and expected[0]['id']=='P0-R1' and expected[0]['sha256']==config['correction_gate']['rule_sha256']), 'prepared rule binding differs')
    if expected:
        rule=expected[0]
        require(sha(rule['path'])==rule['sha256'], 'prepared rule bytes differ')
    consume='Consume these frozen procedural correction rules: '
    origin='Rule treatment origin: '
    controls='Use control op for these frozen non-research bindings (never research reads or evidence): '
    seen={'consume':0,'origin':0,'controls':0,'source_rule':0}
    config_path=str(occurrence(config_raw)/'config.json');config_sha=hashlib.sha256(config_raw).hexdigest()
    entry_path=str(occurrence(config_raw)/'raw'/name/'entry.json')

    for line in spec.splitlines():
        if line.startswith(consume):
            require(json.loads(line[len(consume):])==expected and bool(expected),'prepared consume treatment differs');seen['consume']+=1
        if line.startswith(origin):
            require(line==RULE_ORIGIN_INSTRUCTION and bool(expected),'prepared rule origin differs');seen['origin']+=1
        if line.startswith('Frozen sources: '):
            rows=json.loads(line.split(': ',1)[1]);treatment=[r for r in rows if any(r['path']==rule['path'] for rule in expected)]
            require(treatment==[{'path':r['path'],'sha256':r['sha256']} for r in expected], 'prepared source rule binding differs');seen['source_rule']+=bool(treatment)
    normalized=normalize_prepared_spec(spec,config,name);lines=[];removed=[]
    for line in normalized['text'].splitlines():
        if line.startswith(controls):
            rows=json.loads(line[len(controls):]);seen['controls']+=1
            treatment=[r for r in rows if any(r['path']==rule['path'] for rule in expected)]
            require(treatment==[{'path':r['path'],'sha256':r['sha256']} for r in expected], 'prepared control rule binding differs')
            if treatment:removed.extend(treatment)
                                                                               
            cfg_rows=[r for r in rows if r['path'] in {'<P4_CONFIG>',config_path}]
            require(len(cfg_rows)==1 and cfg_rows[0]['sha256']==config_sha and set(cfg_rows[0])=={'path','sha256'}, 'prepared generated config binding differs')
            rows=[({'path':'<P4_CONFIG>','sha256':'<P4_CONFIG_SHA>'} if r in cfg_rows else r) for r in rows if r not in treatment]
            line=controls+json.dumps(rows,sort_keys=True)
        line=line.replace(entry_path,'<P4_ENTRY>')
        lines.append(line)
    require(seen=={'consume':int(bool(expected)),'origin':int(bool(expected)),'controls':1,'source_rule':int(bool(expected))}, 'prepared treatment count differs')
    return {'text':'\n'.join(lines),'removed_rule_control':removed,'basis':'exact P0-R1 ID/path/hash, accepted correction lineage; generated occurrence config SHA and entry path only'}

def compare_heldout(without_path, with_path):
    a_raw, b_raw = Path(without_path).read_bytes(), Path(with_path).read_bytes()
    a, b = json.loads(a_raw), json.loads(b_raw)
    result = pair_proof(a, b)
    heldout_comparison_lineage(a);heldout_comparison_lineage(b)
    observed = []
    for name in a['nodes']:
        paths = [occurrence(raw) / 'raw' / name for raw in (a_raw, b_raw)]
        if not all((folder / 'final.json').exists() for folder in paths):
            result['live_pair_status'] = 'not yet observed'
            return result
        finals = [load(folder / 'final.json')['units'][0] for folder in paths]
        digests = [load(folder / 'prompt-digests.json') for folder in paths]
        prepared = []
        for cfg, folder, record, hashes in zip((a,b), paths, finals, digests):
            spec_path = folder / 'prepared-spec.txt'
            require(sha(spec_path) == record['spec_sha256'] == hashes['raw_spec_sha256']
                    == hashes['prepared_spec']['sha256'], 'actual dispatched spec hash differs')
            normalized = normalize_prepared_spec(spec_path.read_text(), cfg, name)
            require(normalized == hashes['prepared_spec_normalization'] and
                    hashlib.sha256(normalized['text'].encode()).hexdigest() ==
                    hashes['normalized_substantive_prompt_sha256'], 'actual prepared normalization differs')
            prepared.append(heldout_prepared_treatment(spec_path.read_text(),cfg,name,a_raw if cfg is a else b_raw) if name=='research' else normalized)
        keys = ('agent', 'model', 'effort')
        actual = [{k:r['actual_launch'].get(k) for k in keys} for r in finals]
        require(all(all(row.values()) for row in actual) and actual[0] == actual[1], 'held-out actual model differs')
        require(digests[0]['config_substantive_prompt_sha256'] == digests[1]['config_substantive_prompt_sha256'],
                'held-out configured substantive prompt differs')
        require(finals[0]['task_id'] != finals[1]['task_id'] and finals[0]['dispatch_id'] != finals[1]['dispatch_id'],
                'held-out contexts are not fresh')
        sources = [load(folder / 'entry.json')['sources'] for folder in paths]

        if name == 'research':
            require(prepared[0]['text'] == prepared[1]['text'], 'actual dispatched substantive producer prompts differ')
            rule_paths = {r['path'] for r in b['corrections']}
            require(sources[0] == [r for r in sources[1] if r['path'] not in rule_paths], 'held-out supplied producer sources differ')
        observed.append({'node':name, 'actual_launch':actual[0], 'raw_prompt_digests':digests,
            'actual_dispatched_substantive_comparison': 'equal after exact envelope/rule normalization' if name=='research' else 'downstream output is measured, not a controlled input',
            'candidate_prepared_comparison':prepared if name=='research' else 'downstream observed outputs',
            'tasks':[r['task_id'] for r in finals], 'dispatches':[r['dispatch_id'] for r in finals],
            'control_scope':'producing Task; check consumes the respective observed producer output'})
    result.update(live_pair_status='paired launches and supplied-input digests verified', observed=observed)
    return result

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'validate', 'status', 'packet'):
        cmd = sub.add_parser(name);cmd.add_argument('--config', required=True, type=Path)
        if name == 'run':
            cmd.add_argument('--kill-at', choices=KILL_POINTS)
        if name == 'packet':
            cmd.add_argument('--appendix-map', type=Path)
    cmd=sub.add_parser('ordinary');cmd.add_argument('--request',required=True);cmd.add_argument('--json-events',action='store_true')
    cmd = sub.add_parser('commission-correction')
    for name in ('template', 'normal-config', 'check-output', 'output'):
        cmd.add_argument('--' + name, required=True, type=Path)
    cmd = sub.add_parser('compare-heldout');cmd.add_argument('--without', required=True, type=Path);cmd.add_argument('--with-rule', required=True, type=Path)
    cmd = sub.add_parser('query');cmd.add_argument('--run-set', required=True, type=Path)
    cmd.add_argument('--kind', choices=('artifact', 'correction', 'interruptions'), required=True);cmd.add_argument('--value')
    cmd = sub.add_parser('validate-output')
    cmd.add_argument('--capability', choices=(EVIDENCE, CLAIMS), required=True)
    cmd.add_argument('--file', type=Path, required=True);cmd.add_argument('--claim-input', type=Path)
    cmd.add_argument('--p4-config', type=Path);cmd.add_argument('--node')
    cmd.add_argument('--dispatch-id');cmd.add_argument('--agent',choices=['codex','claude'])
    cmd.add_argument('--operation-log');cmd.add_argument('--dispatch-start')
    args = parser.parse_args(argv)
    dispatcher = None
    try:
        guard_disabled()
        if args.command == 'ordinary':
            import ordinary
            if args.json_events:return ordinary.main(['--request',args.request,'--json-events'])
            result=ordinary.handle(args.request)
        elif args.command == 'validate-output':
            unit = {'claim_input':args.claim_input,'outputs':[{'path':str(args.file)}],'operation_log':args.operation_log}
            if args.p4_config:
                require(args.node, '--node required with --p4-config')
                config = load(args.p4_config)
                unit = config['nodes'][args.node]
                require(args.capability == unit.get('capability'), 'self-check capability differs from frozen node')
                                                                               
                if unit.get('bind_evidence_from'):
                    unit = dict(unit, claim_input=str(Path(config['output_root'])/'evidence-bound-claims.json'))
                if args.claim_input and unit.get('claim_input'):
                    require(str(args.claim_input) == unit['claim_input'], 'self-check claim input differs from frozen node')
            result = validate_file(args.file, args.capability, claim_input=unit.get('claim_input') or args.claim_input)
            if args.p4_config:
                validate_p4_output(load(args.file), config, args.node)
                from operation_recount import verify
                folder = args.p4_config.parent/'raw'/args.node
                entry = load(folder/'entry.json')
                result['independent_recount'] = verify(load(args.file), entry, unit)
            else:
                from operation_recount import verify
                require(args.dispatch_id and args.agent, 'dispatch-bound recount requires --p4-config/--node or --dispatch-id/--agent')
                result['independent_recount'] = verify(load(args.file), {'identities':{'dispatch_id':args.dispatch_id},'route':{'agent':args.agent},'dispatch_started_at':args.dispatch_start}, unit)
            result['validation_scope']='Observed transcript prefix only (bound by transcript_sha256); subsequent calls, outputs including this self-check response, receipt or log changes are rechecked at native settlement. This is not final worker acceptance.'
        elif args.command == 'commission-correction':
            result = commission_correction(args.template, args.normal_config, args.check_output, args.output)
        elif args.command == 'compare-heldout':
            result = compare_heldout(args.without, args.with_rule)
        elif args.command == 'query':
            stores = native_ci.run_set(args.run_set)
            require(args.kind == 'interruptions' or args.value, '--value required')
            result = (native_ci.provenance(stores, args.value) if args.kind == 'artifact' else
                      native_ci.consumers(stores, args.value) if args.kind == 'correction' else native_ci.interruptions(stores))
        else:
            raw = args.config.read_bytes();config = json.loads(raw)
            guard_disabled(config)
            if args.command in {'status', 'packet'}:
                dispatcher, router, orca = runtime(config)
                result = status(raw, config, orca) if args.command == 'status' else packet(raw, config, args.appendix_map, orca)
            else:
                validate_config(config)
                if args.command=='run':
                    from p4_contract import commissioned_root
                    require(commissioned_root(config['output_root']), 'new execution outside commissioned namespace')
                dispatcher, router, orca = runtime(config)
                if args.command == 'validate':
                    result = preview(config, dispatcher, router, config_bytes=raw)
                else:
                    require(not config.get('canary'), 'read-only canary configs refuse execution')
                    heldout_gate(config)

                    require(args.kill_at is None or args.kill_at == config['interruption_point'], '--kill-at differs from declared point')
                    if config['kind'] == 'p1-mixed':
                        require(args.kill_at == config['interruption_point'] or (occurrence(raw) / 'controller-kill.json').exists(),
                                'mixed pilot requires declared interruption; dependent must never launch')
                        result = mixed_run(raw, config, dispatcher, router, orca, args.kill_at)
                    else:
                        q.configure(*__import__('candidate_run').engine_paths(config))
                        result = n.execute_config(raw, dispatcher, router, orca,
                            adapter_factory=functools.partial(P4Adapter, kill_at=args.kill_at))
                    if result.get('invocation') != 'concurrent_observer':
                        make_appendix(raw, config)
                    result['lifecycle'] = status(raw, config, orca)
                    result['success_meaning'] = SUCCESS_MEANING
                    if result.get('invocation') != 'concurrent_observer':
                        durable_json(occurrence(raw) / 'controller-result.json', result)
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.command == 'run' and (not isinstance(result, dict) or
                result.get('status') != 'succeeded' or
                result.get('ci_status', 'captured') != 'captured'):
            return 4
        if isinstance(result, dict) and (result.get('mismatches') or result.get('status') in {'failed', 'unresolved', 'partial', 'held', 'not_activated'}):
            return 4
        return 0
    except (Exception, q.UnresolvedAttempt) as exc:
        print(json.dumps({'status': 'unresolved', 'reason': str(exc), 'native_retry': False}))
        return 4

if __name__ == '__main__':
    sys.exit(main())
