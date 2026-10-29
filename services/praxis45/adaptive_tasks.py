
from __future__ import annotations
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile
from html.parser import HTMLParser

from adaptive_contract import digest, sha
from adaptive_state import atomic_bytes

def scope_directory(compiled, root, fixture_authority=None):

    from adaptive_general import verify_effect_authority
    verify_effect_authority(compiled, root, fixture_authority)
    import state
    store = Path(fixture_authority['path']) if fixture_authority else state.default_state_path()
    event = compiled['logical_graph']['authorizing_event']
    record = json.loads(store.read_bytes())['requests'][event['request_id']]
    return Path(record['output_dir'])/'adaptive-task-scopes'

def authorize_scopes(compiled, root, assignments, fixture_authority=None):

    from adaptive_general import verify_effect_authority
    from adaptive_ci import check_bindings
    from adaptive_support import save_once
    prompt = verify_effect_authority(compiled, root, fixture_authority)
    directory = scope_directory(compiled, root, fixture_authority)
    nodes = [n for n in compiled['logical_graph']['nodes'] if n['kind'] == 'native_task']
    if set(assignments) != {n['id'] for n in nodes}:
        raise ValueError('assignment must cover exactly the general task nodes')
    rows = []
    for node in nodes:
        if not re.fullmatch(r'[a-z][a-z_0-9]*', node['id']): raise ValueError('invalid task node identity')
        assignment = assignments[node['id']]
        authority = assignment.get('authority', {})
        if (assignment.get('scope') != node['scope'] or not authority.get('reason')
                or not isinstance(authority.get('request_quote'), str) or not authority['request_quote'].strip()
                or authority['request_quote'] not in prompt or type(authority.get('publication_authorized')) is not bool
                or not isinstance(authority.get('context_bindings'), dict)):
            raise ValueError('coordinator assignment lacks actual request/context evidence')
        check_bindings(authority['context_bindings'])
        validate_task(node, prompt, root, assignment)
        rows.append((node['id'], dict(assignment, node_sha256=digest(node), authorizing_event=compiled['logical_graph']['authorizing_event'])))
    directory.mkdir(exist_ok=True)
    for name, value in rows:
        save_once(directory/(name+'.json'), value)
    return {'status': 'scope_bound', 'nodes': [name for name, _ in rows], 'directory': str(directory), 'native_launches': 0}

def assigned_scope(compiled, node, root, fixture_authority=None):
    directory = scope_directory(compiled, root, fixture_authority)
    path = directory/(node['id']+'.json')
    if path.is_symlink() or not path.is_file():
        raise ValueError('trusted coordinator task scope missing: '+node['id'])
    value = json.loads(path.read_bytes())
    if value.get('authorizing_event') != compiled['logical_graph']['authorizing_event']:
        raise ValueError('task assignment request differs')
    if value.get('node_sha256') != digest(node):
        raise ValueError('trusted coordinator assignment differs from node task/output/check declaration')
    return value

def operating_at_launch(compiled):
    rows = {}
    for key, binding in compiled.get('operating_sources', {}).items():
        path = Path(binding['path'])
        if not path.is_file(): raise ValueError('current operating source absent: '+key)
        rows[key] = {'path': str(path), 'sha256': sha(path)}
    return rows

def inside(path, roots):
    p = Path(path)
    if not p.is_absolute() or p.is_symlink():
        raise ValueError('absolute nonsymlink task path required: '+str(p))
    resolved = p.resolve()
    if not any(resolved == Path(r).resolve() or Path(r).resolve() in resolved.parents for r in roots):
        raise ValueError('task path outside write scope: '+str(p))
    return resolved

def validate_task(node, prompt, root, assignment=None):
    scope = node.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'workspace', 'write_roots', 'skills', 'tools', 'network', 'publication'}:
        raise ValueError('explicit workspace/write/skill/tool/network/publication scope required')
    workspace = Path(scope['workspace'])

    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError('existing absolute task workspace required')
    if assignment is None or assignment.get('scope') != scope:
        raise ValueError('task scope requires trusted coordinator assignment under original request')
    if not isinstance(scope['write_roots'], list) or not scope['write_roots']:
        raise ValueError('explicit write roots required')
    for path in scope['write_roots']:
        if Path(path).parent == Path(path): raise ValueError('filesystem-root write scope refused')
        inside(path, [path])
    for field in ('skills', 'tools'):
        if not isinstance(scope[field], list) or any(not isinstance(v, str) or not v.strip() for v in scope[field]):
            raise ValueError('explicit skill/tool lists required')
    if scope['network'] not in {'none', 'read', 'publish'}:
        raise ValueError('network scope must be none/read/publish')
    publication = scope['publication']
    if publication is not None:
        from urllib.parse import urlsplit
        if (not isinstance(publication, dict) or not {'argv', 'cwd', 'readback_url'} <= set(publication)
                or set(publication) - {'argv', 'cwd', 'readback_url', 'verification'}):
            raise ValueError('explicit publication route and destination required')
        url = urlsplit(publication['readback_url'])
        if (url.username or url.password or not url.hostname or
                not assignment['authority'].get('publication_authorized') or
                not (url.scheme == 'https' or url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'})):
            raise ValueError('publication requires trusted request-bound authority and HTTPS or explicit loopback')
        validate_command(publication, scope)
        verification = publication.get('verification')
        if verification is not None:
            if (not isinstance(verification, dict) or not {'artifact_path','marker'} <= set(verification)
                    or set(verification) - {'artifact_path','marker','argv','cwd','timeout_seconds'}
                    or not isinstance(verification['marker'], str) or not verification['marker'].strip()
                    or verification['artifact_path'] not in [o.get('path') for o in node['outputs']]):
                raise ValueError('publication verification needs a declared build artifact and stable marker')
            if 'argv' in verification:
                validate_command(verification, scope)
                if publication['readback_url'] not in verification['argv']:
                    raise ValueError('readback command must receive exact resolved destination as an argument')
            elif set(verification) != {'artifact_path','marker'}:
                raise ValueError('HTTP marker verification accepts artifact_path/marker only')
        if scope['network'] != 'publish':
            raise ValueError('publication requires publish network scope')
    elif scope['network'] == 'publish':
        raise ValueError('publication authority absent')
    if node.get('effects') != []:
        raise ValueError('native_task uses scoped publication, not housekeeping effects')
    outputs = node.get('outputs')
    if not isinstance(outputs, list) or not outputs:
        raise ValueError('actual task outputs required')
    paths = []
    for output in outputs:
        if not isinstance(output, dict) or set(output) - {'path', 'format', 'required_members', 'contains'}:
            raise ValueError('invalid task output declaration')
        path = inside(output.get('path', ''), scope['write_roots'])
        if output.get('format') not in {'text', 'json', 'html', 'pdf', 'zip', 'pptx', 'docx', 'binary'}:
            raise ValueError('explicit supported output format required')
        paths.append(str(path))
        if output.get('format') == 'zip' and not output.get('required_members'):
            raise ValueError('zip output needs required_members')
    if len(paths) != len(set(paths)):
        raise ValueError('duplicate task outputs')
    checks = node.get('checks')
    if not isinstance(checks, list):
        raise ValueError('explicit task checks list required')
    for check in checks:
        validate_command(check, scope)
    return scope

def validate_command(check, scope):
    if (not isinstance(check, dict) or not isinstance(check.get('argv'), list)
            or not check['argv'] or any(not isinstance(a, str) or not a for a in check['argv'])
            or type(check.get('timeout_seconds', 60)) is not int
            or check.get('timeout_seconds', 60) <= 0):
        raise ValueError('check requires argv and positive finite timeout_seconds')
    inside(check.get('cwd', ''), [scope['workspace']])
    if not Path(check['cwd']).is_dir():
        raise ValueError('check cwd missing')

def snapshots(document, root):

    result = copy.deepcopy(document)
    targets = [str(Path(o['path']).resolve()) for graph in document['graph_definitions'] for n in graph['nodes']
               if n['kind'] == 'native_task' for o in n['outputs']]
    result['operating_sources'] = {}
    for key, row in list(result['source_bindings'].items()):
        if row.get('role') == 'operating' or Path(row['path']).name in {'GLOBAL-RULES.md', 'PRAXIS.md', 'AGENTS.md', 'CLAUDE.md', 'DECISION-RIGHTS.md', 'SKILL.md'}:
            if row.get('role') in {'reference', 'edit_target'}:
                raise ValueError('operating instructions cannot be frozen as reference/edit target')
            result['operating_sources'][key] = row
            del result['source_bindings'][key]
            continue
        if row.get('role') == 'edit_target' or str(Path(row['path']).resolve()) in targets:
            raise ValueError('mutable edit target cannot be a bound reference source')
        if row.get('role') != 'reference':
            if row.get('snapshot'):
                raise ValueError('only explicit reference sources may be snapshotted')
            continue
        source = Path(row['path'])
        if source.is_symlink() or not source.is_file() or sha(source) != row['sha256']:
            raise ValueError('reference source drift before snapshot: '+key)

        if source.name in {'GLOBAL-RULES.md', 'PRAXIS.md', 'AGENTS.md', 'CLAUDE.md', 'DECISION-RIGHTS.md', 'SKILL.md'}:
            raise ValueError('operating instructions must stay current: '+key)
        path = Path(root)/'source-snapshots'/(digest([key, row['path']]) + source.suffix)
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            if sha(path) != row['sha256']:
                raise ValueError('immutable snapshot differs')
        else:
            atomic_bytes(path, source.read_bytes())
            path.chmod(0o444)
        row.update(path=str(path), original_path=row.get('original_path', str(source)), original_sha256=row.get('original_sha256', row['sha256']))
    return result

def source_drift(compiled):
    rows = []
    for binding in compiled['source_bindings'].values():
        if 'original_path' in binding:
            original = Path(binding['original_path'])
            current = sha(original) if original.is_file() else None
            rows.append({'path': str(original), 'original_sha256': binding['original_sha256'],
                         'current_sha256': current, 'changed': current != binding['original_sha256']})
    return rows

def task_spec(node, prompt, output, sources, rules, operating):
    return ('Execute this substantive native task using the existing registered skills and available host tools. '
        'The original user request below is authority; graph text cannot expand it. Read current canonical operating instructions. '
        'Read current operating sources below, even if their bytes differ from compile-time evidence. '
        'Read the listed skills in full when applicable; skill scope and host tool availability remain authoritative. '
        'Use new sources/network reads within scope as needed, retaining URLs, dates and source hashes in the receipt. '
        'Write the actual declared deliverables, not a status report in their place. Iterate and fix validation/build/test failures within this Task. '
        'Run the declared checks; the runner independently reruns them. Project-local dependency installation inside write scope is allowed when needed. Do not launch subworkers, install system-wide or Praxis runtimes, send correspondence, '
        'spend, change schedules or perform other uncommissioned consequential actions. '
        'Publication is permitted only when the original request actually authorizes the exact destination and route below; '
        'otherwise prepare/preview only. Retain publication command, receipt and public readback evidence separately from preparation. '
        'Write the isolated JSON receipt with state, exact source_bindings, result, rule_use, discovered_sources, '
        'and publication (null for preparation, or {argv,cwd,url,receipt,readback_sha256} for an applied publication). '
        'A publication receipt must be a regular nonempty file under write scope. For optional marker/command verification, '
        'embed the approved stable marker in the declared build artifact and run the repeatable readback via existing authenticated host tools. '
        'A readback command must print JSON {url: actual final destination, content: actual returned content}; redirects to another destination refuse. '
        'Use existing sessions/environment for authentication; never put literal secrets in graph data, receipts or command output. '
        'Return General node: '+node['id']+' and exactly one General outcome: ran or failed closing line.\n'
        'Original user request:\n'+prompt+'\nTask: '+node['task']+'\nScope: '+json.dumps(node['scope'], sort_keys=True)+
        '\nActual outputs: '+json.dumps(node['outputs'], sort_keys=True)+'\nChecks: '+json.dumps(node['checks'], sort_keys=True)+
        '\nReceipt: '+str(output)+'\nSources: '+json.dumps(sources, sort_keys=True)+
        '\nCurrent operating sources at launch: '+json.dumps(operating, sort_keys=True)+
        '\nAccepted corrections: '+json.dumps(rules, sort_keys=True))

class Page(HTMLParser):

    def __init__(self):
        super().__init__(); self.title = False; self.content = False; self.hidden = []
    def handle_starttag(self, tag, attrs):
        self.title |= tag == 'title'
        if self.hidden == ['head'] and tag not in {'base', 'link', 'meta', 'title', 'script', 'style', 'template', 'noscript'}:
            self.hidden = []
        if tag in {'head', 'title', 'script', 'style', 'template'}:
            self.hidden.append(tag)
        elif not self.hidden and tag in {'img', 'svg', 'canvas', 'video', 'audio', 'iframe', 'input', 'hr'}:
            self.content = True
    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[:len(self.hidden)-1-self.hidden[::-1].index(tag)]
    def handle_data(self, data):
        if self.hidden == ['head'] and data.strip(): self.hidden = []
        if not self.hidden and data.strip(): self.content = True

def validate_output(output, scope):
    path = inside(output['path'], scope['write_roots'])
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('nonempty regular task output required: '+str(path))
    raw = path.read_bytes(); kind = output['format']
    if kind in {'text', 'json', 'html'}:
        text = raw.decode('utf-8')
        if not text.strip(): raise ValueError('empty text task output')
        if kind == 'json': json.loads(text)
        if kind == 'html':
            page = Page(); page.feed(text)
            if not page.title or not page.content: raise ValueError('HTML requires title and nonempty body content')
        if any(s not in text for s in output.get('contains', [])):
            raise ValueError('task output missing required content')
    if kind == 'pdf' and (not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-1024:]):
        raise ValueError('invalid PDF signature/trailer')
    if kind in {'zip', 'pptx', 'docx'}:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None: raise ValueError('corrupt output container')
            members = set(archive.namelist())
            required = set(output.get('required_members', []))
            if kind in {'pptx', 'docx'}: required.add('[Content_Types].xml')
            if kind == 'docx': required.add('word/document.xml')
            if kind == 'pptx':
                required.add('ppt/presentation.xml')
                if not any(re.fullmatch(r'ppt/slides/slide\d+\.xml', p) for p in members):
                    raise ValueError('editable deck has no slides')
            if not required <= members: raise ValueError('output container members missing')
            if kind in {'pptx', 'docx'}:
                import xml.etree.ElementTree as ET
                for name in required | {p for p in members if p.endswith('.xml')}:
                    ET.fromstring(archive.read(name))
    return {'path': str(path), 'sha256': sha(path), 'bytes': len(raw), 'format': kind}

def validate_outputs(node):
    return [validate_output(o, node['scope']) for o in node['outputs']]

def run_checks(root, node):
    rows = []
    for index, check in enumerate(node['checks']):
        validate_command(check, node['scope'])
        path = Path(root)/('check-'+node['id']+'-'+str(index)+'.json')
        if path.exists(): raise ValueError('original check evidence exists; do not replay checks')
        try:
            run = subprocess.run(check['argv'], cwd=check['cwd'], shell=False,
                                 capture_output=True, timeout=check.get('timeout_seconds', 60))
            row = {'argv': check['argv'], 'cwd': check['cwd'], 'returncode': run.returncode,
                   'stdout': run.stdout.decode('utf-8', errors='replace'),
                   'stderr': run.stderr.decode('utf-8', errors='replace')}
        except subprocess.TimeoutExpired as exc:
            row = {'argv': check['argv'], 'cwd': check['cwd'], 'returncode': None, 'error': str(exc)}
        atomic_bytes(path, (json.dumps(row, sort_keys=True)+'\n').encode())
        if row['returncode'] != 0: raise ValueError('task check failed; see '+str(path))
        rows.append({'path': str(path), 'sha256': sha(path)})
    return rows

def publication_receipt(node, artifact):
    declaration = node['scope']['publication']; published = artifact.get('publication')
    if declaration is None:
        if published is not None: raise ValueError('unauthorized publication receipt')
        return None
    if (not isinstance(published, dict) or any(published.get(k) != declaration[k] for k in ('argv', 'cwd'))
            or published.get('url') != declaration['readback_url']):
        raise ValueError('publication lacks matching route/destination evidence')
    receipt = validate_output({'path': published.get('receipt', ''), 'format': 'text'}, node['scope'])
    return receipt

def validate_publication(node, artifact, root=None):
    declaration = node['scope']['publication']; published = artifact.get('publication')
    receipt = publication_receipt(node, artifact)
    if declaration is None:
        return {'state': 'prepared', 'publication': None}

    verification = declaration.get('verification')
    check_binding = None
    if verification and 'argv' in verification:
        if root is None: raise ValueError('readback command needs occurrence evidence root')
        check_node = dict(node, id=node['id']+'_publication', checks=[verification])
        check_binding = run_checks(root, check_node)[0]
        checked = json.loads(Path(check_binding['path']).read_bytes())
        returned = json.loads(checked['stdout'])
        if (not isinstance(returned, dict) or returned.get('url') != published['url']
                or not isinstance(returned.get('content'), str) or not returned['content']):
            raise ValueError('readback command lacks exact final destination/content evidence')
        data = returned['content'].encode('utf-8')
    else:
        import urllib.request
        from urllib.parse import urlsplit
        destination = urlsplit(published['url'])
        class BoundRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                if urlsplit(newurl) != destination:
                    raise ValueError('publication readback redirect outside exact authorized destination')
                return super().redirect_request(request, fp, code, msg, headers, newurl)
        with urllib.request.build_opener(BoundRedirect()).open(published['url'], timeout=30) as response:
            data = response.read(10 * 1024 * 1024 + 1)
            if response.status != 200 or not data or len(data) > 10 * 1024 * 1024:
                raise ValueError('publication readback missing or too large')
    import hashlib
    actual = hashlib.sha256(data).hexdigest()
    if verification:
        build = Path(verification['artifact_path'])
        marker = verification['marker'].encode('utf-8')
        if marker not in build.read_bytes() or marker not in data:
            raise ValueError('publication readback/build stable marker differs')
        build_binding = {'path':str(build.resolve()),'sha256':sha(build)}
    else:

        if not any(sha(o['path']) == actual for o in node['outputs']):
            raise ValueError('publication readback does not match any declared output')
        if actual != published.get('readback_sha256'):
            raise ValueError('publication readback differs')
        build_binding = None
    return {'state': 'published_readback_verified', 'publication': published,
            'publication_receipt': receipt, 'readback_sha256': actual,
            'verification_mode':'command_marker' if check_binding else 'http_marker' if verification else 'static_hash',
            'build_artifact':build_binding, 'readback_check':check_binding}

def reference_identity(compiled):
    return {key: {'path': row.get('original_path', row['path']) if key != 'original_request' else 'original_request',
                  'sha256': row['sha256']} for key, row in compiled['source_bindings'].items()}

def retain_outputs(root, node, outputs):
    retained = []
    for index, output in enumerate(outputs):
        path = Path(root)/'retained-outputs'/node['id']/(str(index)+Path(output['path']).suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists(): raise ValueError('original retained output already exists')
        atomic_bytes(path, Path(output['path']).read_bytes()); path.chmod(0o444)
        if sha(path) != output['sha256']: raise ValueError('task output changed during retention')
        retained.append(dict(output, path=str(path.resolve()), original_path=output['path']))
    return retained

def observe_task(root, compiled, node, row, sources):

    from adaptive_general import bind
    from adaptive_support import save_once
    root = Path(root)
    artifact = json.loads(Path(row['artifact']['path']).read_bytes())
    outputs = [dict(validate_output(dict(o, format='binary'), node['scope']), format=o['format'])
               for o in node['outputs']]
    receipt = publication_receipt(node, artifact)
    originals = [row['artifact']] + outputs + ([receipt] if receipt else [])
    retained = retain_outputs(root, dict(node, id=node['id']+'_observed'), originals)
    path = root/('task-observation-'+node['id']+'.json')
    folder = root/('native-'+digest([row['occurrence_id'], node['id']]))
    evidence = [bind(root/name) for name in ('compiled.json', 'compile-receipt.json', 'engine-intent.json')]
    evidence += [bind(folder/(name+'.json')) for name in ('run', 'task', 'launch', 'settled')]
    evidence.append(bind(root/('operating-'+node['id']+'.json')))
    evidence += [bind(p) for p in sorted(root.glob('custody-layer-*.json'))]
    save_once(path, {'node_sha256': digest(node), 'authorizing_event': compiled['logical_graph']['authorizing_event'],
        'receipt': row, 'sources': sources,
        'originals': originals, 'retained': retained, 'evidence': evidence,
        'outputs': retained[1:1+len(outputs)],
        'publication_receipt': retained[-1] if receipt else None})
    return bind(path)

def check_observation(binding):
    from adaptive_ci import check_bindings
    check_bindings({'observation': binding})
    observed = json.loads(Path(binding['path']).read_bytes())
    bindings = observed['originals'] + observed['retained'] + observed['sources'] + observed['evidence']
    check_bindings({str(i): b for i, b in enumerate(bindings)})
    return observed

def original_settlement(root, intent, name, native, expected_receipt_sha256, client):

    import sqlite3
    from adaptive_native import readback_identity
    root = Path(root)
    db = sqlite3.connect('file:'+str(root/'occurrence.sqlite')+'?mode=ro', uri=True)
    try:
        custody = intent['custody']
        owner = db.execute('SELECT host,owner,fence FROM occurrences WHERE id=?', (intent['occurrence'],)).fetchone()
        if owner != tuple(custody[k] for k in ('host', 'owner', 'fence')):
            raise ValueError('original occurrence custody differs')
        record = db.execute('SELECT data FROM nodes WHERE occ=? AND node=? AND rev=0',
                            (intent['occurrence'], name)).fetchone()
        row = json.loads(record[0]) if record else {}
        folder = root/('native-'+digest([intent['occurrence'], name]))
        settlement = json.loads((folder/'settled.json').read_bytes())
        accepted = row.get('receipt', {})
        units = settlement.get('units', [])
        if (row.get('native') != native or accepted.get('native') != native
                or row.get('origin') != digest([intent['occurrence'], name, 0])
                or accepted.get('origin') != row.get('origin')
                or accepted.get('native_outcome') != 'succeeded'
                or accepted.get('state') != 'succeeded' or accepted.get('contract_accepted') is not True
                or accepted.get('settlement_sha256') != digest(settlement)
                or row.get('receipt_sha256') != digest(accepted)
                or row.get('receipt_sha256') != expected_receipt_sha256
                or len(units) != 1 or units[0].get('contract', {}).get('accepted') is not True):
            raise ValueError('original accepted native settlement missing or changed')
        from adaptive_native import collect_original
        raw = collect_original(client, native)
        shown, readback = readback_identity(raw)
        if (shown != native or readback['dispatch'].get('status') != 'completed'
                or readback['projection'].get('outcome') != 'succeeded'):
            raise ValueError('original native unsettled or changed')
        return raw
    finally:
        db.close()

def reconciliation_receipts(root, name):
    return [path for path in sorted((Path(root)/'reconciliations'/name).glob('*/receipt.json'))
            if json.loads(path.read_bytes()).get('status') == 'reconciled']

def reconciled_receipt(root, compiled, node, checkpoint):

    from adaptive_ci import check_bindings
    from adaptive_general import bind
    paths = reconciliation_receipts(root, node['id'])
    if not paths: return None
    if len(paths) != 1: raise ValueError('ambiguous original reconciliation')
    receipt = json.loads(paths[0].read_bytes())
    if (receipt['node_sha256'] != digest(node) or receipt['compiled_sha256'] != compiled['compiled_sha256']
            or receipt['checkpoint'] != bind(checkpoint)):
        raise ValueError('original reconciliation binding differs')
    check_bindings({str(i): b for i, b in enumerate(receipt['evidence'])})

    result = copy.deepcopy(receipt['receipt'])
    result['reconciliation'] = bind(paths[0])
    return result

def reconcile(root, compiled, name, client, fixture_authority=None):

    import uuid
    from adaptive_ci import check_bindings
    from adaptive_general import bind, verify_effect_authority
    from adaptive_support import save_once
    root = Path(root)
    nodes = [n for n in compiled['logical_graph']['nodes'] if n['id'] == name and n['kind'] == 'native_task']
    if len(nodes) != 1: raise ValueError('one original native_task node required')
    node = nodes[0]
    if reconciliation_receipts(root, name): raise ValueError('original already reconciled; use continuation')
    attempt = root/'reconciliations'/name/uuid.uuid4().hex
    attempt.mkdir(parents=True)
    result = {'status': 'held', 'node_id': name, 'node_sha256': digest(node),
              'compiled_sha256': compiled['compiled_sha256'], 'native_launches': 0,
              'publication_replays': 0, 'relaunch_authorized': False}
    try:
        prompt = verify_effect_authority(compiled, root, fixture_authority)
        check_bindings(compiled['source_bindings'])
        check_bindings(compiled['scope_bindings'])
        validate_task(node, prompt, root, assigned_scope(compiled, node, root, fixture_authority))
        checkpoint = root/('node-result-'+name+'.json')
        prior = json.loads(checkpoint.read_bytes())
        if prior['node_sha256'] != digest(node) or prior['receipt']['state'] != 'unknown':
            raise ValueError('original failed validation checkpoint required')
        observed = check_observation(prior['receipt']['observation'])
        if (observed['node_sha256'] != digest(node) or observed['receipt']['native'] != prior['receipt']['native']
                or observed['authorizing_event'] != compiled['logical_graph']['authorizing_event']):
            raise ValueError('original observation node/native differs')
        if prior['receipt']['verification_evidence']:
            check_bindings({str(i): b for i,b in enumerate(prior['receipt']['verification_evidence'])})
        intent = json.loads((root/'engine-intent.json').read_bytes())
        if (intent['compiled_sha256'] != compiled['compiled_sha256']
                or observed['receipt']['occurrence_id'] != intent['occurrence']):
            raise ValueError('original occurrence/compile differs')
        original = original_settlement(root, intent, name, prior['receipt']['native'], observed['receipt']['receipt_sha256'], client)
        save_once(attempt/'native-readback.json', original)
        custody = [bind(path) for path in sorted(root.glob('custody-layer-*.json'))]
        if not custody or any(json.loads(Path(b['path']).read_bytes()).get('state') != 'closed' for b in custody):
            raise ValueError('original native layer custody unsettled')
        result['checkpoint'] = bind(checkpoint)
        result['evidence'] = ([result['checkpoint'], prior['receipt']['observation']] + observed['evidence']
            + observed['sources'] + observed['retained'] + prior['receipt']['verification_evidence']
            + custody + [bind(attempt/'native-readback.json')])
        outputs = validate_outputs(node)
        checks = run_checks(attempt, node)
        artifact = json.loads(Path(observed['receipt']['artifact']['path']).read_bytes())
        delivery = validate_publication(node, artifact, attempt)

        check_observation(prior['receipt']['observation'])
        if validate_outputs(node) != outputs: raise ValueError('verification changed original output')
        if delivery.get('readback_check'): checks.append(delivery['readback_check'])
        if delivery.get('publication_receipt'): delivery['publication_receipt'] = observed['publication_receipt']
        if delivery.get('build_artifact'):
            delivery['build_artifact'] = next(o for o in observed['outputs'] if o['sha256']==delivery['build_artifact']['sha256'])
        result['receipt'] = dict(observed['receipt'], state='succeeded', outputs=observed['outputs'],
                                 checks=checks, delivery=delivery, observation=prior['receipt']['observation'])
        result['evidence'] += checks
        result['status'] = 'reconciled'
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result['reason'] = type(exc).__name__+': '+str(exc)

    result['verification_evidence'] = [bind(p) for p in sorted(attempt.glob('check-*.json'))]
    save_once(attempt/'receipt.json', result)
    return dict(result, reconciliation=bind(attempt/'receipt.json'))

def continuation_bindings(previous):
    previous = Path(previous).resolve()
    paths = [previous/'compiled.json', previous/'engine-intent.json']
    paths += sorted(previous.glob('node-result-*.json')) + sorted(previous.glob('custody-layer-*.json'))
    paths += sorted(previous.glob('reconciliations/*/*/receipt.json'))
    if (previous/'engine-result.json').is_file(): paths.append(previous/'engine-result.json')
    return {str(i): {'path': str(p), 'sha256': sha(p)} for i, p in enumerate(paths)}

def continuation(previous, compiled, client):

    from adaptive_ci import check_bindings
    from adaptive_native import NativeBridge, collect_original, readback_identity
    from adaptive_state import Journal
    from adaptive_contract import verify_compiled
    previous = Path(previous).resolve()
    old = json.loads((previous/'compiled.json').read_bytes()); verify_compiled(old)
    intent = json.loads((previous/'engine-intent.json').read_bytes())
    if old['logical_graph'].get('authorizing_event') != compiled['logical_graph'].get('authorizing_event'):
        raise ValueError('continuation must retain original request authority')
    result_path = previous/'engine-result.json'
    if result_path.exists():
        result = json.loads(result_path.read_bytes())
    else:
        result = {'occurrence_id': intent['occurrence'], 'custody': intent['custody'],
                  'receipts': {}}
    for path in previous.glob('node-result-*.json'):
        checkpoint = json.loads(path.read_bytes())
        result['receipts'][checkpoint['receipt']['node_id']] = checkpoint['receipt']
    prior_sources, current_sources = reference_identity(old), reference_identity(compiled)
    same_sources = all(current_sources.get(key) == binding for key,binding in prior_sources.items())
    reused = {}; journal = Journal(previous)
    new_nodes = compiled['logical_graph']['nodes']
    try:
        old_by_id = {n['id']:n for n in old['logical_graph']['nodes']}
        for name in [name for layer in old['layers'] for name in layer]:
            old_node = old_by_id[name]
            name = old_node['id']; receipt = result['receipts'].get(name, {})
            if old_node['kind'] == 'native_task' and receipt.get('state') == 'unknown':
                reconciled = reconciled_receipt(previous, old, old_node, previous/('node-result-'+name+'.json'))
                if reconciled: receipt = reconciled
            current = next((n for n in new_nodes if n['id'] == name), None)
            native_state = None
            if old_node['kind'] in {'native_task', 'native_readonly_worker', 'existing_authorized_effect'}:
                row = journal.get(result['occurrence_id'], name)
                if row:
                    bridge = NativeBridge(journal, result['occurrence_id'], name, result['custody'])
                    native = bridge.recover(lambda identity: collect_original(client, identity))
                    native_state = native['state']
                    if native_state not in {'succeeded', 'failed'}:
                        raise ValueError('original native unsettled; no continuation: '+name)
                elif receipt.get('native'):
                    shown, readback = readback_identity(collect_original(client, receipt['native']))
                    if (shown != receipt['native'] or readback['dispatch'].get('status') != 'completed'
                            or readback['projection'].get('outcome') != 'succeeded'):
                        raise ValueError('inherited original native unsettled: '+name)
                if row and receipt.get('state') != 'succeeded':
                    revisions = [n for n in new_nodes if n.get('revision_of') == name and n['id'] != name]
                    local = (old_node['kind'] == 'native_task' and old_node['scope']['publication'] is None
                             and old_node['scope']['network'] in {'none', 'read'})
                    old_paths = {o['path'] for o in old_node['outputs']}
                    fresh = revisions and all(n['kind']=='native_task' and n['scope']['publication'] is None
                        and n['scope']['write_roots'] != old_node['scope']['write_roots']
                        and not old_paths.intersection(o['path'] for o in n['outputs']) for n in revisions)
                    if not local or not fresh or current is not None:
                        raise ValueError('unvalidated effects require a fresh local revision or original reconciliation: '+name)
            if receipt.get('state') != 'succeeded': continue
            if current is None and old_node['kind'] not in {'native_task','native_readonly_worker','existing_authorized_effect'}:
                continue
            if current != old_node:
                raise ValueError('successful native node must be preserved; add a new revision node: '+name)
            if not same_sources:
                raise ValueError('reference sources changed; cannot reuse stale successful node: '+name)
            bindings = ([receipt['artifact']] if receipt.get('artifact') else []) + receipt.get('outputs', []) + receipt.get('checks', [])
            delivery = receipt.get('delivery', {})
            bindings += [delivery[k] for k in ('publication_receipt', 'build_artifact', 'readback_check') if delivery.get(k)]
            for binding in bindings: check_bindings({'original': binding})

            for dep in old_node['depends_on']:
                if dep not in reused: raise ValueError('successful node dependency is not reusable: '+name)
            checkpoint = previous/('node-result-'+name+'.json')
            evidence = checkpoint if checkpoint.is_file() else result_path
            reused[name] = dict(copy.deepcopy(receipt), reused_from={
                'result': {'path': str(evidence), 'sha256': sha(evidence)},
                'occurrence_id': result['occurrence_id'], 'node_id': name})
    finally:
        journal.close()
    return reused

class RequestExecution:

    def __init__(self, compiled, root, previous=None, *, reconcile=False, advance=True):
        event = compiled['logical_graph']['authorizing_event']
        folder = Path(os.environ.get('PRAXIS_ADAPTIVE_EFFECT_ROOT',
            str(Path.home()/'Library/Application Support/Praxis/adaptive-effects')))/'task-occurrences'
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder/(digest([event[k] for k in ('request_id','event_id','user_event_sha256')])+'.json'); self.lock = self.path.with_suffix('.lock').open('a+b')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.value = {'root': str(Path(root).resolve()), 'compiled_sha256': compiled['compiled_sha256']}
            if self.path.exists():
                old = json.loads(self.path.read_bytes())
                if reconcile:
                    if old != self.value: raise ValueError('reconciliation must retain current original request lineage')
                elif previous is None or old['root'] != str(Path(previous).resolve()):
                    raise ValueError('request already has an occurrence; use explicit continuation')
            elif previous is not None or reconcile:
                raise ValueError('original request lineage missing')
            if advance and not reconcile: self.advance()
        except BaseException:
            self.lock.close(); raise
    def advance(self):
        atomic_bytes(self.path, json.dumps(self.value, sort_keys=True).encode())
    def close(self):
        self.lock.close()
