
import copy
import hashlib
import json
import os
from pathlib import Path

from native_adapter import REPO, sha, verify_sources
from projection import digest, durable_json

KILL_POINTS = {
    'after-task-create-before-starting': 'tasks_created',
    'after-starting-before-dispatch-receipt': 'before_start',
    'after-dispatch-before-collection': 'dispatched',
}
SUCCESS_MEANING = 'DOT success means worker settled + files validated, not claims supported.'
EVALUATION_LABEL = ('precommitted-rule conditioning, not elicited, on new source files; '
                    'no observed within-worker improvement established; '
                    'not unseen knowledge or general learning')
RULE_ORIGIN_LABEL = 'precommitted rule applied, not elicited by a failure'
RULE_ORIGIN_INSTRUCTION = ('Rule treatment origin: ' + RULE_ORIGIN_LABEL +
                           '. Include rule_origin_label exactly ' + json.dumps(RULE_ORIGIN_LABEL) + ' in your receipt.')
REQUEST = Path(os.environ.get("PRAXIS_REHEARSAL_ROOT", "~/.local/share/praxis/rehearsals")).expanduser() / "request"
REHEARSAL = REQUEST.parent / "baseline"
V24_REHEARSAL = REQUEST.parent / "current"
RULE_TEXT = ('Before promoting a claim to verified prose, read its designated source, bind the exact supporting or contradicting passage to the source bytes, and preserve the passage’s qualifiers and evidence kind. If the cited source does not contain the attributed passage, retain the discrepancy and narrow or withdraw the attribution. Requirements, designs, simulation propositions, observed computational outputs and human/organizational observations must remain distinguishable. This rule makes no choice of construct, DV, architecture arm, hypothesis, or publication contribution.')

def require(ok, reason):
    if not ok:
        raise ValueError(reason)

def load(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs,
                      parse_constant=lambda v: (_ for _ in ()).throw(ValueError('nonfinite JSON')))

def binding(path):
    return {'path': str(Path(path).absolute()), 'sha256': sha(path)}

def within(path, root):
    path, root = Path(path), Path(root)
    return path.is_absolute() and path.resolve().is_relative_to(root.resolve()) and not path.is_symlink()

def commissioned_root(path):

    path=Path(path)
    functional=REQUEST.parent/'smokes'
    return (not any(p.is_symlink() for p in path.parents) and
            (within(path,functional) or (within(path,V24_REHEARSAL) and path.resolve()!=V24_REHEARSAL.resolve()
             and path.resolve().relative_to(V24_REHEARSAL.resolve()).parts[0].startswith('v29-'))))

def guard_disabled(config=None):
    require(not (REPO / '.praxis45-disabled').exists() and
            (config is None or config.get('enabled') is True), 'candidate disabled')

def validate_config(config, *, candidate=True):
    guard_disabled(config)
    require(config.get('p4_schema_version') == 1, 'P4 schema version required')
    require(config.get('kind') in {'normal', 'correction', 'heldout', 'interruption', 'web', 'claims', 'p1-mixed'}, 'unknown bounded config kind')
    required = {'kind', 'graph_sha256', 'inputs', 'research_sources', 'interruption_point',
                'output_root', 'nodes', 'embargo', 'context', 'objective'}
    require(required <= config.keys(), 'P4 config fields incomplete')
    require(config['graph_sha256'] == hashlib.sha256(config['graph'].encode()).hexdigest(), 'graph hash differs')
    require(config['interruption_point'] in KILL_POINTS, 'declared interruption point required')
    if config.get('ordinary') is True:
        require(config['kind'] in {'web','claims'}, 'ordinary bounded capability required')
        from candidate_run import release_info
        info=release_info();home=Path(info['installation_home']) if info and info.get('installation_home') else Path.home()
        from ordinary import state_root
        require(within(config['output_root'],state_root()), 'ordinary outputs outside durable namespace')
    else:
        require((within(config['output_root'], REHEARSAL) or within(config['output_root'], V24_REHEARSAL)), 'outputs must remain in P4 rehearsal')
    require(within(config['state_root'], config['output_root']), 'state outside frozen outputs')
    from immutable_code import authenticate, relative
    ordinary_inputs=[]
    for row in config['inputs']:
        rel=relative(row['path'])
        if rel and rel.startswith('services/praxis45/schemas/'):
            authenticate(row,config['candidate_commit'],config.get('code_identity'))
        else:ordinary_inputs.append(row)
    verify_sources(ordinary_inputs)
    inputs = {r['path']: r['sha256'] for r in config['inputs']}
    require(len(inputs) == len(config['inputs']), 'duplicate frozen inputs')
    verify_sources(config['research_sources'])
    for row in config['research_sources']:
        require(inputs.get(row['path']) == row['sha256'], 'research source missing input hash')
    all_bound = set()
    for node in config['nodes'].values():
        if node.get('claim_input'):
            from native_capabilities import validate_claim_input
            validate_claim_input(node['claim_input'],discovery=bool(node.get('evidence_discovery')))
        require(node.get('seat') and node.get('task_class'), 'seat and route required')
        require(set(node.get('frozen_route', {})) == {'effective_model', 'provider', 'agent', 'launch_target', 'effort'},
                'exact frozen route/model required')
        require(node.get('requested_model') == node['frozen_route']['effective_model'], 'explicit model differs')
        for row in node.get('source_bindings', []):
            require(inputs.get(row['path']) == row['sha256'], 'node source missing input hash')
            all_bound.add(row['path'])
        for row in node['outputs']:
            require(within(row['path'], config['output_root']), 'node output escapes frozen output root')
    for rule in config.get('corrections', []):
        require(inputs.get(rule['path']) == rule['sha256'], 'rule missing frozen input hash')
        require(Path(rule['path']).read_text() == RULE_TEXT + '\n', 'rule text differs from P0-R1')
        all_bound.add(rule['path'])
    denied = config['embargo']
    require(set(denied) == {'paths', 'enforcement'}, 'embargo declaration required')
    require(denied['enforcement'] == 'supplied-context allowlist; no OS access isolation', 'embargo limits must be explicit')
    for path in denied['paths']:
        require(path not in all_bound, 'embargoed source in node inputs')
        require(all(path not in node['spec'] for node in config['nodes'].values()), 'embargoed path in prompt')
    require(not any(Path(p).name in {'qualification-expected.json', 'heldout-expected.json', 'ada-evaluation-spec.md'}
                    for p in inputs), 'answer key or combined normal/held-out spec exposure refused')
    if candidate:
        from candidate_run import validate_candidate
        validate_candidate(config)

def model_fields(route):
    return {k: route[k] for k in ('effective_model', 'provider', 'agent', 'launch_target', 'effort')}

class BoundRouter:
    def __init__(self, router, config):
        self.router, self.config = router, config
    def route(self, unit, *args, **kwargs):
        row = self.router.route(unit, *args, **kwargs)
        require(model_fields(row) == self.config['nodes'][unit['id']]['frozen_route'], 'routed model differs from frozen route')
        return row
    def __getattr__(self, name):
        return getattr(self.router, name)

def inventory(client, identities):
    receipt = client.call(['orchestration', 'worker-list', '--run', identities['run_id'], '--include-remote'])
    value = receipt.get('result') or receipt
    require((value.get('scope') or {}).get('run') == identities['run_id'], 'worker inventory Run differs')
    require((value.get('page') or {}).get('hasMore') is False and isinstance(value.get('workers'), list),
            'complete worker inventory required')
    rows = value['workers']
    require(all(r.get('runId') == identities['run_id'] and r.get('taskId') for r in rows), 'inventory contains unbound worker')
    matches = [r for r in rows if r['taskId'] == identities['task_id']]
    require(len(matches) == 1 and matches[0].get('dispatchId') == identities['dispatch_id'], 'one exact worker per Task required')
    return receipt

def log_envelope(config,name,text):
    node=config['nodes'][name]
    if node.get('operation_log'):
        expected=str(Path(config['output_root'])/'operations'/name/'{dispatch_id}.jsonl')
        require(node['operation_log']==expected,'operation log outside exact node output envelope')
        return text.replace(expected,'<OPERATION_LOG>')
    return text

def substantive(config, name):
    node = config['nodes'][name]

    return {'spec': log_envelope(config,name,node['substantive_spec']), 'seat': node['seat'], 'route': node['frozen_route'],
            'sources': sorted(config['research_sources'], key=lambda r: r['path']),
            'claims_sha256': config.get('claims_sha256'), 'rubric_sha256': config.get('rubric_sha256')}

def normalize_prepared_spec(spec, config, name):

    removed = []
    lines = []
    rules = {r['path'] for r in config.get('corrections', [])
             if r['id'] in config['nodes'][name].get('consumes', [])}
    for line in spec.splitlines():
        if not lines and line.startswith('PLAN ') and line.endswith(' — UNIT ' + name):
            line = 'PLAN <OCCURRENCE> — UNIT ' + name
        if line.startswith('Consume these frozen procedural correction rules: '):
            treatment = json.loads(line.split(': ', 1)[1])
            require({r['path'] for r in treatment} == rules, 'unexpected prepared rule treatment')
            removed.append(line)
            continue
        if line == RULE_ORIGIN_INSTRUCTION:
            require(rules or config['kind'] == 'correction', 'undeclared rule origin treatment')
            removed.append(line)
            continue
        if line.startswith('Frozen sources: '):
            sources = json.loads(line.split(': ', 1)[1])
            treatment = [r for r in sources if r['path'] in rules]
            if treatment:
                removed.append(json.dumps(treatment, sort_keys=True))
            line = 'Frozen sources: ' + json.dumps([r for r in sources if r['path'] not in rules], sort_keys=True)

        line = line.replace(str(occurrence(json.dumps(config).encode()) / 'config.json'), '<P4_CONFIG>')
        if line.startswith('Required candidate self-check: '):
            import re
            line = re.sub(r'--p4-config (?:\'[^\']*\'|\"[^\"]*\"|\S+)', '--p4-config <P4_CONFIG>', line)
        for index, output in enumerate(config['nodes'][name]['outputs']):
            line = line.replace(output['path'], '<OUTPUT_' + str(index) + '>')
        line=log_envelope(config,name,line)
        lines.append(line)
    return {'text': '\n'.join(lines), 'removed_rule_treatment': removed,
            'basis': 'actual prepared spec matched to dispatcher SHA256; normalize PLAN/output/log envelopes and exact declared rule treatment only'}

def pair_proof(without, with_rule):
    for key in ('kind', 'objective', 'graph', 'candidate_commit', 'research_sources', 'claims_sha256', 'rubric_sha256'):
        require(without[key] == with_rule[key], 'held-out pair differs: ' + key)
    require(without['kind'] == 'heldout', 'paired held-out configs required')
    require(not without.get('corrections') and len(with_rule.get('corrections', [])) == 1, 'exact one-rule treatment required')
    require(without['nodes'].keys() == with_rule['nodes'].keys(), 'paired nodes differ')
    rows = []
    for name in without['nodes']:
        a, b = without['nodes'][name], with_rule['nodes'][name]
        require(log_envelope(without,name,a['spec']) == log_envelope(with_rule,name,b['spec']) and a['source_bindings'] == b['source_bindings'], 'base supplied prompt/input differs')
        require(substantive(without, name) == substantive(with_rule, name), 'substantive prompt differs')
        require(a.get('consumes', []) == [], 'without-rule has consumption')
        require(b.get('consumes', []) == (['P0-R1'] if name == 'research' else []), 'rule must reach only with-rule producer')
                                                                            
        def stripped(node,config):
            return {k:log_envelope(config,name,v) if k in {'spec','substantive_spec'} else v for k,v in node.items() if k not in {'outputs','consumes','operation_log'}}
        require(stripped(a,without) == stripped(b,with_rule), 'paired node metadata differs')
        rows.append({'node': name, 'config_substantive_prompt_sha256': digest(substantive(without, name)),
                     'supplied_source_content_digests': without['research_sources']})
    return {'only_substantive_treatment_difference': 'P0-R1 file and consume instruction', 'nodes': rows,
            'raw_envelopes': 'Task/Dispatch ids, timestamps, run/config ids and output paths necessarily differ',
            'same_attested_model': 'must be checked against both actual launch receipts after execution'}

def occurrence(config_bytes):
    config = json.loads(config_bytes)
    return Path(config['state_root']) / ('px45-' + hashlib.sha256(config_bytes).hexdigest())

def accepted_artifact(directory, path):
    matches = []
    for decision in sorted((Path(directory) / 'raw').glob('*/decision.json')):
        value = load(decision)
        for row in value['outcome']['artifacts']:
            if row['path'] == str(path) and value['outcome']['state'] == 'success' and row['validated']:
                require(sha(path) == row['sha256'], 'accepted artifact changed')
                matches.append((value, decision))
    require(len(matches) == 1, 'one accepted original artifact required')
    return matches[0]

def commission_correction(template_path, normal_path, check_path, output_path):
    normal_raw = Path(normal_path).read_bytes();normal = json.loads(normal_raw)
    validate_config(normal)
    root = occurrence(normal_raw)
    checked, decision = accepted_artifact(root, check_path)
    require(checked['entry']['node_id'] == 'audit', 'normal independent audit required')
    check = load(check_path)
    defects = check.get('defects')
    require(isinstance(defects, list) and defects and all(isinstance(r, dict) and r.get('quote') and r.get('reason') for r in defects),
            'check must quote actual defects; no automatic or invented correction')
    template = load(template_path)
    require(template.get('template_only') is True, 'correction template required')
    require(commissioned_root(output_path) and not Path(output_path).exists(), 'new immutable correction config required')
    config = copy.deepcopy(template['config'])
    require(config['candidate_commit'] == normal['candidate_commit'], 'correction candidate differs')
    links = []
    for node in ('research', 'audit'):
        bundle = root / 'raw' / node / 'final.json'
        record = load(bundle)['units'][0]
        links.append({'node': node, 'path': str(bundle), 'sha256': sha(bundle),
                      'run_id': load(bundle)['run_id'], 'task_id': record['task_id'], 'dispatch_id': record['dispatch_id']})
    lineage = {'check': binding(check_path), 'defects': defects, 'original_producer_links': links,
               'rule': template['rule'], 'rule_text_representation': 'exact UTF-8 frozen file bytes; no normalization',
               'rule_text': Path(template['rule']['path']).read_bytes().decode('utf-8')}
    lineage_path = Path(output_path).with_suffix('.commission.json')
    require(not lineage_path.exists(), 'correction lineage already exists')
    durable_json(lineage_path, lineage)
    binds = [binding(check_path), binding(lineage_path)] + [{'path': r['path'], 'sha256': r['sha256']} for r in links]
    original = normal['nodes']['research']['outputs'][0]['path']
    binds.append(binding(original))
    config['inputs'] += binds
    for node in config['nodes'].values():
        node['source_bindings'] += binds
    config['nodes']['research']['spec'] += '\nNew correction commission: ' + json.dumps(lineage, sort_keys=True) + '\nCopy all rule file bytes as UTF-8 rule_text, including the final newline; no normalization.'
    config['context']['digest'] = digest(lineage)
    config['commission'] = lineage
    validate_config(config)
                                                     
    with Path(output_path).open('xb') as handle:
        handle.write((json.dumps(config, sort_keys=True, indent=2) + '\n').encode())
    return {'config': binding(output_path), 'graph_run_id': occurrence(Path(output_path).read_bytes()).name,
            'native_launches': 0, 'commission': lineage}
