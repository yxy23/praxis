
from __future__ import annotations
import argparse
import asyncio
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_contract import compile_graph, digest, sha, verify_compiled
from adaptive_support import save_once, close_released_lease, audit_effects
from adaptive_state import Journal, occurrence_key, effect_key
from adaptive_native import NativeBridge, collect_original
from adaptive_ci import NativeOutbox, check_bindings
from adaptive_learning import LearningBridge
from adaptive_collectors import execute_selected
from native_adapter import load_dispatcher

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EFFECT_OPERATIONS = frozenset({'orca_terminal_close', 'orca_terminal_rename',
    'orca_worktree_set_display_name', 'orca_worktree_set_comment'})
NATIVE_KINDS = frozenset({'native_readonly_worker', 'existing_authorized_effect'})

def local_custody_host():

    local = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"],
                           text=True, capture_output=True, check=True).stdout.strip()
    if not local:
        raise ValueError("room entry requires local LocalHostName")
    return local

def effect_identity(compiled, effect):
    event = compiled['logical_graph'].get('authorizing_event', {})
    if (not isinstance(event, dict) or any(not isinstance(event.get(k), str) or not event[k]
            for k in ('request_id', 'event_id', 'user_event_sha256'))):
        raise ValueError('effect_authorizing_event_required: request_id/event_id/user_event_sha256')

    return effect_key(effect['operation'], 'orca', digest([event['request_id'], event['event_id'], event['user_event_sha256']]),
                      digest(effect['target']), 'one-operation-per-authorizing-event')

def verify_effect_authority(compiled, root, fixture_authority=None):

    sys.path.insert(0, str(REPO/'services/praxis-switchboard'))
    from state import default_state_path
    event = compiled['logical_graph'].get('authorizing_event', {})
    store = default_state_path()
    if fixture_authority is not None:
        store = Path(fixture_authority['path']).resolve()
        if (store != Path(root).resolve()/'fixture-switchboard/state.json'
                or not (Path(root)/'ADAPTIVE_FIXTURE_ROOT').is_file()
                or sha(store) != fixture_authority['sha256']):
            raise ValueError('effect_fixture_authority_mismatch')
    try:
        if not isinstance(event, dict):
            raise ValueError('authorizing event required')
        record = json.loads(store.read_bytes())['requests'][event['request_id']]
        prompt_path = Path(record['output_dir'])/'request.md'
        raw_prompt = prompt_path.read_bytes()
        prompt = raw_prompt.decode('utf-8')
        hashed = hashlib.sha256(raw_prompt).hexdigest()
        if (record['request_id'] != event['request_id']
                or (record.get('event_id') or record['request_id']) != event['event_id']
                or hashed != event['user_event_sha256']
                or hashed != record['user_prompt_sha256']
                or any(event.get(k) != record.get(k) for k in ('context_room_id', 'context_digest'))):
            raise ValueError('request event/hash/context differs')
        for node in compiled['logical_graph']['nodes']:
            for effect in node.get('effects', []):
                if effect['authority']['quote'] not in prompt:
                    raise ValueError('authority quote absent from user prompt')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('effect_authority_refused: '+str(exc)) from exc

def verify_effect_images(effect, row):
    pre, post = row.get('preimage'), row.get('postimage')
    if (not isinstance(pre, dict) or not isinstance(post, dict)
            or any(k not in pre or pre[k] != v or type(pre[k]) is not type(v)
                   for k, v in effect['expected'].items())
            or pre.get('target') != effect['target'] or post.get('target') != effect['target']):
        raise ValueError('effect preimage/target differs from declaration')
    field = {'orca_terminal_rename': 'title', 'orca_worktree_set_display_name': 'displayName',
             'orca_worktree_set_comment': 'comment'}.get(effect['operation'])
    if (post.get(field) != effect['value'] if field else post.get('exists') is not False):
        raise ValueError('effect postimage differs from declared change')

def effect_journal():
    root = Path(os.environ.get('PRAXIS_ADAPTIVE_EFFECT_ROOT',
        str(Path.home()/'Library/Application Support/Praxis/adaptive-effects')))
    root.mkdir(parents=True, exist_ok=True)
    (root/'ADAPTIVE_FIXTURE_ROOT').touch(exist_ok=True)
    return Journal(root)

def reserve_effects(root, compiled, node, occurrence):
    journal = effect_journal()
    try:
        for effect in node['effects']:
            key = effect_identity(compiled, effect)
            record = {'state': 'intended', 'occurrence': occurrence, 'node': node['id'],
                'effect_key': key, 'effect': effect, 'authorizing_event': compiled['logical_graph']['authorizing_event'],
                'compile_receipt': bind(Path(root)/'compile-receipt.json'), 'relaunch_authorized': False}

            journal.reserve_authorized_effect(key, record)
            path = Path(root)/('effect-intent-'+key+'.json')
            with path.open('x') as handle:
                json.dump(record, handle, sort_keys=True);handle.flush();os.fsync(handle.fileno())
    finally: journal.close()

def settle_effects(root, compiled, node, occurrence, native, sources, outbox, graph_binding):
    path = Path(root)/('effect-receipt-'+node['id']+'.json')
    reason = ''
    rows = []
    try:
        if path.is_symlink() or not path.is_file(): raise ValueError('original effect receipt absent')
        receipt = json.loads(path.read_bytes())
        check_bindings({str(i): b for i, b in enumerate(sources)})
        rows = receipt.get('effects', [])
        received_sources = receipt.get('source_bindings')
        if (receipt.get('state') != 'succeeded' or not isinstance(received_sources, list)
                or any(not isinstance(b, dict) for b in received_sources)
                or [(b.get('path'), b.get('sha256')) for b in received_sources]
                   != [(b['path'], b['sha256']) for b in sources]
                or not receipt.get('result') or len(rows) != len(node['effects'])):
            raise ValueError('incomplete source-bound effect receipt')
        for effect in node['effects']:
            matching = [row for row in rows if row.get('id') == effect['id']]
            if len(matching) != 1: raise ValueError('effect receipt cardinality differs')
            row = matching[0]
            if (any(row.get(k) != effect[k] for k in ('operation', 'target'))
                    or row.get('effect_key') != effect_identity(compiled, effect)
                    or row.get('applied') is not True or not row.get('preimage')
                    or not row.get('postimage') or row.get('reason') is None):
                raise ValueError('effect lacks matching applied preimage/postimage')
            verify_effect_images(effect, row)
        binding = bind(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc)
        binding = bind(path) if path.is_file() and not path.is_symlink() else None
    journal = effect_journal()
    try:
        for effect in node['effects']:
            journal.settle_authorized_effect(effect_identity(compiled, effect), occurrence,
                state='uncertain' if reason else 'applied', native=native, receipt=binding)
    finally: journal.close()
    if reason:
        return {'state': 'held', 'reason': reason, 'artifact': binding, 'rule_use': [], 'effects': rows, 'effect_state': 'uncertain'}
    from adaptive_evidence import event
    provenance = {'source_bindings': dict(compiled['source_bindings'], artifact=binding, graph=graph_binding),
        'local_reads': [], 'rules': [], 'graph_sha256': graph_binding['sha256'], 'native': native}
    outbox.append(event(occurrence+'/'+node['id'], 2, 'effect_applied', occurrence, provenance,
        {'effects': rows, 'receipt': binding, 'relaunch_authorized': False}))
    return {'state': 'succeeded', 'artifact': binding, 'rule_use': [], 'effects': rows}

def bind(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': sha(path)}

def validate_general(compiled, root, fixture_authority=None):

    nodes = compiled['logical_graph']['nodes']
    if any(key in value for value in [compiled['logical_graph'], compiled['policy']] for key in ('requested_model', 'explicit_model', 'model')):
        raise ValueError('general graph model selection belongs to the router')
    if any(n['kind'] == 'existing_authorized_effect' for n in nodes):
        verify_effect_authority(compiled, root, fixture_authority)
    for node in nodes:
        if any(key in value for value in (node, node.get('route') or {}) for key in ('requested_model', 'explicit_model', 'model')):
            raise ValueError('general graph model selection belongs to the router')
        if node['kind'] == 'existing_authorized_effect':
            if node.get('route') != {'seat': 'Leo'}:
                raise ValueError('effect_requires_canonical_Leo')
            effects = node.get('effects')
            if not isinstance(effects, list) or not effects or not node.get('task') or not node['depends_on']:
                raise ValueError('effect_declaration_required')
            if len([e.get('id') for e in effects if isinstance(e, dict)]) != len(effects) or len({e.get('id') for e in effects}) != len(effects):
                raise ValueError('unique effect ids required')
            for effect in effects:
                operation = effect.get('operation')
                if operation not in EFFECT_OPERATIONS:
                    raise ValueError('effect_operation_unsupported:'+str(operation))
                if (not isinstance(effect.get('id'), str) or not effect['id'] or not effect.get('target')
                        or effect.get('reversible') is not True or not isinstance(effect.get('expected'), dict) or not effect['expected']
                        or not isinstance(effect.get('authority'), dict) or effect['authority'].get('kind') != 'user_request'
                        or not isinstance(effect['authority'].get('quote'), str) or not effect['authority']['quote'].strip()):
                    raise ValueError('effect_declaration_invalid')
                if operation != 'orca_terminal_close' and not isinstance(effect.get('value'), str):
                    raise ValueError('effect_value_required')
                effect_identity(compiled, effect)
            if any(node['id'] in layer and len(layer) != 1 for layer in compiled['layers']):
                raise ValueError('effect_layer_must_be_serialized')
        elif node.get('effects') != [] or node['kind'] not in {'admission', 'native_readonly_worker', 'all_input_join'}:
            raise ValueError('effect-free admission/native_readonly_worker/all_input_join nodes only')
        if node['activation'] != {'op': 'all_required_inputs_valid', 'args': []}:
            raise ValueError('general entry requires explicit all-input gates')
        if node['kind'] in NATIVE_KINDS:
            if not isinstance(node.get('route'), dict) or not node['route'].get('seat') or not node.get('task'):
                raise ValueError('native node requires registered named seat and task')
        elif node.get('route') or (node['kind'] == 'admission' and node['depends_on']):
            raise ValueError('deterministic gate cannot route or claim dependent admission')
    if next(n for n in nodes if n['id']==compiled['layers'][0][0])['kind'] != 'admission' or len([n for n in nodes if n['kind']=='admission']) != 1:
        raise ValueError('one admission node required')
    if not any(n['kind'] in NATIVE_KINDS for n in nodes):
        raise ValueError('at least one named native worker required')
    for layer in compiled['layers']:
        if sum(n['kind'] in NATIVE_KINDS and n['id'] in layer for n in nodes)>compiled['policy']['max_parallel']:
            raise ValueError('native layer exceeds admitted parallel worker bound')
        if any(n['kind'] in NATIVE_KINDS and n['id'] in layer for n in nodes):
            layer_plan(root,compiled,layer)                                              

def compile_entry(logical, policy, graph_id, root, *, origin='supplied', fixture_authority=None):
    root = Path(root).absolute()
    if root.is_symlink() or not (root/'ADAPTIVE_FIXTURE_ROOT').is_file():
        raise ValueError('explicit isolated ADAPTIVE_FIXTURE_ROOT required')
    root=root.resolve()
    document = json.loads(Path(logical).read_bytes())
    compiled = compile_graph(document, graph_id, json.loads(Path(policy).read_bytes()), origin=origin)
    validate_general(compiled,root,fixture_authority)
                                                                              
    for name, source in [('logical.json', logical), ('policy.json', policy)]:
        destination=root/name; raw=Path(source).read_bytes()
        if destination.exists() and destination.read_bytes()!=raw:
            raise ValueError('immutable entry input differs; use a fresh root')
        if not destination.exists():
            from adaptive_state import atomic_bytes
            atomic_bytes(destination, raw)
    save_once(root/'compiled.json', compiled)
    receipt={'status':'compiled', 'native_launches':0, 'compiled':bind(root/'compiled.json'),
             'logical':bind(root/'logical.json'), 'policy':bind(root/'policy.json'), 'origin':origin,
             'graph_id':graph_id, 'layers':compiled['layers'], 'production_admitted':False}
    save_once(root/'compile-receipt.json', receipt)
    return receipt

def rules_for(root, compiled, outbox, corrections):
    if corrections is None: return [], None
    control=json.loads(Path(corrections).read_bytes())
    if control['target'] != compiled['logical_graph']['id']:
        raise ValueError('correction target differs from logical graph identity')
    if control['parent_sha256'] != compiled['logical_graph_sha256']:
        raise ValueError('correction parent differs from exact logical graph')
    provenance={'source_bindings':compiled['source_bindings'], 'local_reads':[], 'rules':[],
                'graph_sha256':sha(Path(root)/'compiled.json'), 'native':{}}
    learning=LearningBridge(root,outbox,repo=REPO,provenance=provenance)
    rules=learning.admit_rules(control['records'],target=control['target'],
        parent_sha256=control['parent_sha256'],revoked=set(control.get('revoked',[])))
    if not rules: raise ValueError('no applicable accepted correction; no consumption claimed')
    return rules, learning

def layer_plan(root, compiled, layer, *, rules=(), receipts=None):
                                                                                   
    dispatcher=load_dispatcher(); boundary=dispatcher.named_launch_module()
    roster=boundary.registry.seats()
    units=[]
    for node in compiled['logical_graph']['nodes']:
        if node['id'] not in layer or node['kind'] not in NATIVE_KINDS: continue
        if receipts is not None and any(receipts.get(dep,{}).get('state')!='succeeded' for dep in node['depends_on']):continue
        seat=roster.get(node['route']['seat'])
        if seat is None: raise ValueError('unknown canonical seat')
        effect_node = node['kind'] == 'existing_authorized_effect'
        output=Path(root)/(('effect-receipt-' if effect_node else 'artifact-')+node['id']+'.json')
        if output.exists() or output.is_symlink():raise ValueError('preserve existing output; fresh occurrence required')
        sources=list(compiled['source_bindings'].values())
        for dep in node['depends_on']:
            artifact=(receipts or {}).get(dep,{}).get('artifact')
            if artifact:check_bindings({dep:artifact});sources.append(artifact)
        rule_text=[{'record':r, 'text':Path(r['path']).read_text()} for r in rules]
        spec=("Execute only this bounded effect-free graph node. Read only the frozen sources below and canonical operating instructions. "
            "No mailbox/calendar/network, routine invocation, project/tracker/owner writes, install, subworkers or external effects. "
            "Write only the named isolated JSON artifact, with state (succeeded or failed), source_bindings (exact list below), "
            "result (your substantive evidence), and rule_use (a list; each actually applied rule records rule_sha256, target, observed_use true and evidence). "
            "Do not infer success from this instruction. Return General node: "+node['id']+" and exactly one General outcome: ran or failed line. "
            "Task: "+node['task']+"\nOutput: "+str(output)+"\nSources: "+json.dumps(sources,sort_keys=True)+
            "\nAccepted applicable corrections (apply and explain actual use, never merely echo): "+json.dumps(rule_text,sort_keys=True))
        if effect_node:
            declarations = [dict(e, effect_key=effect_identity(compiled, e)) for e in node['effects']]
            spec = ("Execute only the declared authorized internal effects, once each, as canonical Leo. "
                "Use only the public Orca CLI, never a database or private API. No subworkers or undeclared changes. "
                "Read canonical instructions and Sources first. Before each effect read the exact target preimage: "
                "orca terminal show --terminal <target> --json or orca worktree show --worktree <target> --json. "
                "Verify all expected fields exactly, stable target identity, target exists, idle state, and no live Dispatch ownership; "
                "verify title/comment preimage. If state/ownership cannot be positively verified, refuse. "
                "Use public orca orchestration run-list --json through exhaustion, then worker-list --run <exact-run-id> --include-remote --json for EVERY run with ALL pages. Verify each returned scope covers that exact run and each target host; a caller-bound Run alone is insufficient. Preserve all inventory readbacks and hold if any scope, pagination, host coverage or live ownership is unknown. "
                "Re-read immediately before the mutation; refuse any preimage drift. "
                "Allowed operations map to orca terminal close --terminal <target> --json; "
                "orca terminal rename --terminal <target> --title <value> --json; "
                "orca worktree set --worktree <target> --display-name <value> --json; "
                "orca worktree set --worktree <target> --comment <value> --json. "
                "Perform only the mapped operation, never --all or --tab. Read back postimage using show; "
                "for close verify absence with terminal list --json and retain that result as postimage. "
                "Never retry an uncertain effect. Hold on mismatch, absent target, non-idle target, live ownership or uncertainty. "
                "Write JSON state (succeeded only if ALL effects verified, else held), source_bindings (copy Sources exactly, in order, with no added keys), "
                "result, and effects: one row per declaration containing id, operation, target, effect_key, preimage, "
                "postimage, applied (boolean), reason. Normalize preimage as an object with target and every expected field; "
                "postimage must contain target and title/displayName/comment equal to value for rename/set, or exists false for close. "
                "Retain real raw readbacks in each row as readbacks, never simulate success. "
                "General node: "+node['id']+"; exactly one General outcome: ran or failed closing line. "
                "Task: "+node['task']+"\nOutput: "+str(output)+"\nSources: "+json.dumps(sources,sort_keys=True)+
                "\nDeclarations: "+json.dumps(declarations,sort_keys=True))
        unit={'id':node['id'],'seat':seat.name,'task_class':seat.route,'title':'Adaptive node '+node['id'],
              'sensitivity':'internal','risk':'low','data_domains':[], 'spec':spec,
              'source_bindings':sources+[bind(r['path']) for r in rules],
              'outputs':[{'path':str(output),'required':True}],
              'return_contract':{'required_substrings':['General node: '+node['id'],'General outcome:'],
                'max_chars':2000,'outcome_contract':{'prefix':'General outcome:','allowed':['ran','failed'],
                    'substantive':['ran'],'failure':['failed'],'hold':[]}}}
        units.append(unit)
    layer_id='adaptive-layer-'+digest([str(root),layer])[:16]
                                                                                
    plan={'schema_version':'1','id':layer_id,
          'objective':'Bounded adaptive graph native layer '+layer_id,
          'defaults':{'start_timeout_ms':compiled['policy']['start_ms'],'wait_window_ms':60000,
                      'timing_policy':'passive-no-kill'},'units':units}
    dispatcher.validate_plan(plan)
    return plan

def validate_artifact(root, node, sources, rules, learning):
    path=Path(root)/('artifact-'+node+'.json')
    if path.is_symlink() or not path.is_file():raise ValueError('original regular artifact required')
    artifact=json.loads(path.read_bytes())
    if artifact.get('state')!='succeeded' or artifact.get('source_bindings')!=sources or not artifact.get('result'):
        raise ValueError('node lacks successful source-bound substantive artifact')
    check_bindings({str(i):b for i,b in enumerate(sources)})
    uses=artifact.get('rule_use',[])
    observed=[]
    for rule in rules:
        matching=[r for r in uses if r.get('rule_sha256')==rule['sha256'] and r.get('target')==rule['target']]
        if len(matching)!=1 or not matching[0].get('evidence'):
            raise ValueError('accepted correction lacks actual use evidence')
                                                                              
        use_path=Path(root)/('rule-use-'+node+'-'+rule['sha256']+'.json')
        save_once(use_path,matching[0]); observed.append(learning.observed_use(rule,bind(use_path)))
    return {'artifact':bind(path),'rule_use':observed}

def query(root):

    db=sqlite3.connect('file:'+str(Path(root)/'ci-projection.sqlite')+'?mode=ro',uri=True)
    try:
        rows=[]
        for expected,raw in db.execute('SELECT sha,data FROM events ORDER BY id'):
            row=json.loads(raw)
            if digest(row)!=expected:raise ValueError('derived CI projection integrity differs')
            rows.append(row)
        from adaptive_evidence import check_event
        for row in rows: check_event(row);check_bindings(row['provenance']['source_bindings'])
        return {'status':'derived_evidence','events':len(rows),'rows':rows,'authority':'none'}
    finally: db.close()

async def run_graph(root, compiled, dispatcher, router, client, *, corrections=None, fixture_authority=None):
    from adaptive_engine import run_shadow
    root=Path(root); verify_compiled(compiled);validate_general(compiled,root,fixture_authority)
    host=local_custody_host(); journal=Journal(root); occurrence=occurrence_key('room',compiled['logical_graph']['id'],token=str(root))
    custody={'host':host,'owner':'canonical-leo-general-entry','fence':1}
    journal.acquire(occurrence,host,custody['owner'],{'graph':compiled['compiled_sha256']})
    if (root/'engine-intent.json').exists():
        journal.close(); raise ValueError('original traversal exists; no relaunch or upstream resume; reconcile originals')
    check_bindings(compiled['source_bindings'])
    from adaptive_evidence import Outbox
    outbox=Outbox(root,max_events=compiled['policy']['max_nodes']*2,max_sequence=64)
    rules,learning=rules_for(root,compiled,outbox,corrections)
    graph_binding=bind(root/'compiled.json')
    tasks={}; bridges={}; bundles={}; counts=[];bound_sources={}
    layer_of={name:i for i,layer in enumerate(compiled['layers']) for name in layer}
    save_once(root/'engine-intent.json',{'compiled_sha256':compiled['compiled_sha256'],'occurrence':occurrence})

    async def dispatch_layer(index, context):
        layer=compiled['layers'][index]
        if any(c['state']!='closed' for c in counts):raise ValueError('prior native custody unresolved; no successor launches')
        plan=layer_plan(root,compiled,layer,rules=rules,receipts=context.get('praxis.receipts',{}))
        for node in compiled['logical_graph']['nodes']:
            if node['id'] in layer and node['kind'] == 'existing_authorized_effect' and any(u['id'] == node['id'] for u in plan['units']):
                reserve_effects(root, compiled, node, occurrence)
        work=root/('layer-'+str(index));work.mkdir();(work/'ADAPTIVE_FIXTURE_ROOT').touch()
        boxes={};mapping={}; loop=asyncio.get_running_loop()
        for unit in plan['units']:
            name=unit['id'];bound_sources[name]=unit['source_bindings'];bridge=NativeBridge(journal,occurrence,name,custody);bridges[name]=bridge
            bridge.prepare({'compiled_sha256':compiled['compiled_sha256'],'plan_sha256':digest(plan)})
            boxes[name]=NativeOutbox(root,graph=graph_binding,sources=compiled['source_bindings'],
                local_reads=[],rules=rules,max_events=compiled['policy']['max_nodes']*2,max_sequence=64)
        def observe_here(stage,payload):
            if stage=='task': mapping[payload['task_id']]=payload['unit_id']
            if stage in {'prepared','run','tasks_created'}:
                return [boxes[n].observer(bridges[n])(stage,payload) for n in boxes]
            name=mapping.get(payload['units'][0]['task_id'] if stage=='settled' else payload.get('task_id'))
            if name is None: raise ValueError('native receipt lacks original node mapping')
            return boxes[name].observer(bridges[name])(stage,payload)
        async def apply(stage,payload): return observe_here(stage,payload)
        def observe(stage,payload):return asyncio.run_coroutine_threadsafe(apply(stage,payload),loop).result()
        key='adaptive-room/'+occurrence+'/'+str(index)
        lease=dispatcher.RunLease(work,key,work/'bundle.json',custody_host=host)
        try:
            if not lease.acquire()[0]:raise ValueError('existing canonical lease; no launch')
            bundle=await asyncio.to_thread(execute_selected,dispatcher,'integrated-finite-v1',plan,router,client,
                root=work,policy=compiled['policy'],dry_run=False,log_routes=False,run_key=key,lease=lease,
                output_path=work/'bundle.json',receipt_observer=observe)
            save_once(work/'bundle.json',bundle);bundles[str(index)]=bundle
            counts.append(close_released_lease(lease,bundle,dispatcher))
        finally:
            for box in boxes.values():box.close()

    async def execute(node,context,identity):
        name=node['id']
        if node['kind'] in NATIVE_KINDS:
            blocked=[dep for dep in node['depends_on'] if context.get('praxis.receipts',{}).get(dep,{}).get('state')!='succeeded']
            if blocked:
                return dict(identity,state='held',reason='required native predecessor held or failed',blocked_dependencies=blocked)
            index=layer_of[name]
            if index not in tasks:tasks[index]=asyncio.create_task(dispatch_layer(index,context))
            try:
                await tasks[index]
            except Exception as exc:
                if node['kind'] == 'existing_authorized_effect':
                    ledger = effect_journal()
                    try:
                        for effect in node['effects']:
                            key = effect_identity(compiled, effect)
                            old = ledger.db.execute('SELECT data FROM effects WHERE key=?', (key,)).fetchone()
                            if old and json.loads(old[0])['occurrence'] == occurrence and json.loads(old[0])['state'] == 'intended':
                                ledger.settle_authorized_effect(key,occurrence,state='uncertain',native=None,receipt=None)
                    finally: ledger.close()
                    return dict(identity,state='held',reason=str(exc)+'; reconcile originals; no replay',rule_use=[])
                raise
            row=bridges[name].recovered_engine_receipt(lambda native:collect_original(client,native))
            if node['kind'] == 'existing_authorized_effect':
                if row['state'] == 'succeeded':
                    row.update(settle_effects(root, compiled, node, occurrence, row['native'], bound_sources[name], outbox, graph_binding))
                else:
                    ledger = effect_journal()
                    try:
                        for effect in node['effects']:
                            ledger.settle_authorized_effect(effect_identity(compiled,effect),occurrence,
                                state='uncertain',native=row.get('native'),receipt=None)
                    finally: ledger.close()
                    row.update(state='held',reason='effect original native did not succeed; reconcile originals; no replay',rule_use=[])
            elif row['state']=='succeeded':row.update(validate_artifact(root,name,bound_sources[name],rules,learning))
            if row['state']=='succeeded' and row.get('rule_use'):
                from adaptive_evidence import event
                provenance={'source_bindings':dict(compiled['source_bindings'],artifact=row['artifact'],graph=graph_binding),
                    'local_reads':[],'rules':rules,'graph_sha256':graph_binding['sha256'],'native':row['native']}
                outbox.append(event(occurrence+'/'+name,2,'correction_consumed',occurrence,provenance,
                    {'observed_use':row['rule_use'],'benefit':'unmeasured'}))
            return dict(row,**identity)
        if node['kind']=='all_input_join':
            deps=context.get('praxis.receipts',{})
            if any(not deps.get(n,{}).get('artifact') and next(x for x in compiled['logical_graph']['nodes'] if x['id']==n)['kind'] in NATIVE_KINDS for n in node['depends_on']):
                return dict(identity,state='held',reason='join requires both original native receipts and source-bound artifacts')
        return dict(identity,state='succeeded',semantics='structural admission/join only; no business action')
    try:
        result=await run_shadow(compiled,occurrence,execute,root/'engine')
        projected=outbox.project(root/'ci-projection.sqlite'); evidence=query(root)
        captured=sum((b.folder/'launch.json').is_file() for b in bridges.values())
        effect_rows = [r for receipt in result['receipts'].values() for r in receipt.get('effects', []) if isinstance(r,dict)]
        has_effects = any(n['kind'] == 'existing_authorized_effect' for n in compiled['logical_graph']['nodes'])
        result.update(custody=custody, relaunch_authorized=False, native_launches='unknown_after_intent' if any(not (b.folder/'launch.json').is_file() for b in bridges.values()) else captured,
            captured_native_launches=captured,
            execution_profile='praxis.adaptive.authorized-internal-effects.v1' if has_effects else 'praxis.adaptive.native-readonly.v1',compiler_profile=compiled['profile'],
            executed_graph_sha256=compiled['logical_graph_sha256'],ci_projection=projected,ci_query=evidence,
            cleanup=counts,live_effects=dict(audit_effects(bundles), declared_internal_effects=effect_rows, applied_internal_effects=sum(r.get('applied') is True for r in effect_rows)),production_admitted=False,
            learning={'admitted_rules':len(rules),'observed_use':[r.get('rule_use',[]) for r in result['receipts'].values()],
                      'benefit':'unmeasured'},
            limits=(['allowlisted authorized internal effects only'] if has_effects else ['effect-free named workers only'])+['tool restrictions are instructions, not a physical sandbox',
                    'one canonical dispatcher Run per engine-ready native layer','no upstream resume or automatic retry'])
        if any(r.get('ci')=='incomplete' for b in bundles.values() for r in b.get('receipt_observer_results',[]) if isinstance(r,dict)) or len([r for r in evidence['rows'] if r['kind']=='native_settlement'])!=len(bridges) or any(c['state']!='closed' for c in counts):
            result['status']='incomplete'
        save_once(root/'engine-result.json',result)
        return result
    finally:
        outbox.close();journal.close()

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['compile','execute','_in-terminal','query'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--logical',type=Path);p.add_argument('--policy',type=Path)
    p.add_argument('--graph-id');p.add_argument('--origin',choices=['supplied','athena'],default='supplied')
    p.add_argument('--expected-compile-sha256');p.add_argument('--corrections',type=Path)
    p.add_argument('--engine-source',type=Path);p.add_argument('--engine-closure',type=Path)
    p.add_argument('--fixture-authority', type=Path);p.add_argument('--fixture-authority-sha256')
    a=p.parse_args(argv);root=a.root.resolve()
    fixture_authority = ({'path': str(a.fixture_authority), 'sha256': a.fixture_authority_sha256}
                         if a.fixture_authority else None)
    try:
        if a.command=='compile':
            if not all([a.logical,a.policy,a.graph_id]):raise ValueError('logical, policy and graph-id required')
            result=compile_entry(a.logical,a.policy,a.graph_id,root,origin=a.origin,fixture_authority=fixture_authority)
        elif a.command=='query':result=query(root)
        else:
            if not (root/'ADAPTIVE_FIXTURE_ROOT').is_file():raise ValueError('explicit isolated root required')
            if sha(root/'compile-receipt.json')!=a.expected_compile_sha256:raise ValueError('compile receipt drift; no launch')
            receipt=json.loads((root/'compile-receipt.json').read_bytes())
            check_bindings({k:receipt[k] for k in ['compiled','logical','policy']})
            compiled=json.loads((root/'compiled.json').read_bytes());verify_compiled(compiled);validate_general(compiled,root,fixture_authority)
            if a.corrections:save_once(root/'corrections.json',json.loads(a.corrections.read_bytes()));a.corrections=root/'corrections.json'
            dispatcher=load_dispatcher();client=dispatcher.OrcaClient(dispatcher.resolve_orca(None))
            from adaptive_coordinator import launch,in_terminal_client
            caller_workspace=(REPO.parent/'install.json').is_file()
            if a.command=='execute':
                if not a.engine_source or not a.engine_closure:raise ValueError('pinned engine source/closure required')
                argv=[sys.executable,'-I','-B',str(Path(__file__).resolve()),'_in-terminal','--root',str(root),
                    '--expected-compile-sha256',a.expected_compile_sha256,'--engine-source',str(a.engine_source),
                    '--engine-closure',str(a.engine_closure)]
                if fixture_authority:argv+=['--fixture-authority', fixture_authority['path'], '--fixture-authority-sha256', fixture_authority['sha256']]
                if a.corrections:argv+=['--corrections',str(a.corrections)]
                result=launch(dispatcher,client,root,argv,worker_limit=compiled['policy']['max_parallel'],
                              caller_workspace=caller_workspace)
            else:
                import engine_qualification as q
                q.configure(a.engine_source,a.engine_closure)
                client=in_terminal_client(dispatcher,client,root,caller_workspace=caller_workspace)
                router=dispatcher.RouterClient(dispatcher.default_router())
                with (root/'.general-entry.lock').open('a+b') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    result=asyncio.run(run_graph(root,compiled,dispatcher,router,client,corrections=a.corrections,fixture_authority=fixture_authority))
                save_once(root/'qualification-result.json',result)
        print(json.dumps(result,indent=2));return 0 if result.get('status') not in {'held','incomplete'} else 4
    except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
        result={'status':'held','reason':type(exc).__name__+': '+str(exc),'relaunch_authorized':False}
        if a.command=='_in-terminal' and root.is_dir():save_once(root/'qualification-result.json',result)
        print(json.dumps(result));return 4

if __name__=='__main__':raise SystemExit(main())
