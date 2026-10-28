
import argparse
import json
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from projection import EVENT, digest, ingest, read_events

def event_for(directory, folder, decision):

    from native_adapter import sha
    entry = decision['entry']
    record = {}
    receipt_sha = decision['raw_receipt_sha256']
    if receipt_sha:
        snapshot = folder / ('receipt-' + receipt_sha + '.json')
        if not snapshot.exists() or sha(snapshot) != receipt_sha:
            raise ValueError('missing or changed primary native receipt snapshot')
        bundle = json.loads(snapshot.read_text())
        records = bundle.get('units') or []
        record = records[0] if len(records) == 1 else {}
    identity = dict(entry.get('identities', {}), workspace=directory_config(directory)['workspace'],
                    graph_run_id=entry['graph_run_id'], unit_id=entry['node_id'], stage='native_node')
    occurrence = digest(identity)
    data = {'kind': 'native_adapter', 'occurrence_id': occurrence, 'identity': identity,
        'receipt_sha256': digest(decision), 'event_id': digest({'occurrence': occurrence, 'decision': decision}),
        'recorded_at': decision['timestamp'], 'entry': entry, 'node_outcome': decision['outcome'], 'route': entry['route'],
        'actual_launch': record.get('actual_launch', {}), 'candidate_commit': entry['candidate_commit'],
        'sources': entry['sources'], 'artifacts': decision['outcome']['artifacts'],
        'corrections': decision['corrections'], 'consumes': decision['consumes'],
        'primary_receipt_path': str(folder / ('receipt-' + receipt_sha + '.json')) if receipt_sha else None,
        'primary_receipt_sha256': receipt_sha, 'primary_decision_path': str(folder / ('decision-' + digest(decision) + '.json')),
        'primary_decision_sha256': sha(folder / ('decision-' + digest(decision) + '.json')),
        'evidence_accepted': decision['outcome']['state'] == 'success', 'human_acceptance_state': 'unknown'}
    return {'event': EVENT, 'data': data, 'workspace': identity['workspace'], 'timestamp': decision['timestamp']}

def directory_config(directory):
    return json.loads((Path(directory) / 'config.json').read_text())

def capture(directory, folder, decision):
    row = event_for(Path(directory), Path(folder), decision)
    intent = Path(folder) / 'intent.json'
    rows = ([json.loads(intent.read_text())] if intent.exists() else []) + [row]
    return ingest(Path(directory) / 'ci', rows, session_id=digest({
        'workspace': row['workspace'], 'graph_run_id': row['data']['identity']['graph_run_id']}))

def rebuild(directory, store):

    directory, store = Path(directory), Path(store)
    if store.exists():
        raise ValueError('rebuild destination must be new; preserve corrupt original store')
    config = directory_config(directory)
    from native_adapter import sha
    rows = []
    for path in sorted((directory / 'raw').glob('*/decision-*.json')):
        decision = json.loads(path.read_text())
        if path.name != 'decision-' + digest(decision) + '.json':
            raise ValueError('changed primary decision')
        rows.append(event_for(directory, path.parent, decision))
                                                                                  
    from projection import ENTRY_EVENT
    for path in sorted((directory / 'raw').glob('*/intent.json')):
        rows.append(json.loads(path.read_text()))
    if not rows:
        raise ValueError('no raw evidence to rebuild')
    return ingest(store, rows, session_id=digest({'workspace': config['workspace'],
        'graph_run_id': 'px45-' + __import__('hashlib').sha256((directory / 'config.json').read_bytes()).hexdigest()}))

def records(store):
    unique = {}
    stores = store if isinstance(store, (list, tuple)) else [store]
    for source in stores:
        path = Path(source)
        if not path.exists() and path.name == 'ci' and (path.parent / 'config.json').is_file():
            cfg = directory_config(path.parent)
            if cfg.get('kind') == 'p1-mixed':

                for data in mixed_records(path.parent, cfg):
                    unique[data['event_id']] = data
                continue
        for row in read_events(source):
            data = row['data']
            if row['event'] == EVENT and data.get('kind') == 'native_adapter':
                key = data['event_id']
                if key in unique and unique[key] != data:
                    raise ValueError('conflicting duplicate event identity')
                unique[key] = data
    return sorted(unique.values(), key=lambda r: (r['recorded_at'], r['event_id']))

def mixed_records(directory, config):
    from native_adapter import sha
    entry_path = directory / 'mixed-entry.json'
    final_path = directory / 'mixed-final.json'
    if not entry_path.exists():
        raise ValueError('mixed primary entry unavailable')
    entry = json.loads(entry_path.read_bytes())
    final = json.loads(final_path.read_bytes()) if final_path.exists() else {}
    rows = {r['id']: r for r in final.get('units', [])}
    result = []
    for name, node in entry.get('nodes', {}).items():
        row = rows.get(name, {})
        identity = dict(node['identities'], workspace=config['workspace'], graph_run_id=directory.name,
                        unit_id=name, stage='mixed_dispatcher')
        state = 'success' if row.get('status') == 'succeeded' else 'unresolved'
        primary = final_path if final_path.exists() else entry_path
        record = {'identity':identity, 'entry':node, 'route':node['route'], 'actual_launch':row.get('actual_launch',{}),
            'candidate_commit':config['candidate_commit'], 'sources':node['inputs'], 'artifacts':row.get('outputs', []),
            'recorded_at':final.get('completed_at', 'unknown'), 'node_outcome':{'state':state,'reason':row.get('status','not collected')},
            'primary_receipt_path':str(primary),'primary_receipt_sha256':sha(primary),
            'primary_decision_path':str(primary),'primary_decision_sha256':sha(primary),
            'corrections':[], 'consumes':[], 'evidence_accepted':state=='success', 'human_acceptance_state':'unknown'}
        record['event_id']=digest(record);result.append(record)
    return result

def provenance(store, artifact):
    rows = []
    for record in records(store):
        for row in record['artifacts']:
            if artifact in {row['path'], row.get('sha256')}:
                rows.append({k: record[k] for k in ('identity', 'sources', 'route', 'actual_launch',
                    'candidate_commit', 'primary_receipt_path', 'primary_receipt_sha256',
                    'primary_decision_path', 'primary_decision_sha256', 'node_outcome')} | {'artifact': row})
    return rows

def consumers(store, rule_sha256):
                                                                               
    grouped = {}
    for record in records(store):
        rules = [r for r in record['corrections'] if r['sha256'] == rule_sha256 and r['id'] in record['consumes']]
        if rules and record['entry'].get('launch_receipt'):
            key = digest(record['identity'])
            grouped[key] = {'identity': record['identity'], 'rules': rules,
                         'state': record['node_outcome']['state'], 'consumption': 'frozen_input_supplied',
                         'primary_receipt_path': record['primary_receipt_path'],
                         'primary_receipt_sha256': record['primary_receipt_sha256']}
    return [grouped[key] for key in sorted(grouped)]

def interruptions(store):
    grouped = {}
    for record in records(store):
        key = (record['identity']['workspace'], record['identity']['graph_run_id'], record['identity']['unit_id'])
        grouped.setdefault(key, []).append(record)
    result = []
    for versions in grouped.values():
        if all(r['node_outcome']['state'] == 'success' for r in versions):
            continue
        latest = max(versions, key=lambda r: (r['recorded_at'], r['event_id']))
        result.append({'identity': latest['identity'], 'last_durable_state': latest['node_outcome'],
            'entry': latest['entry'], 'primary_receipt_path': latest['primary_receipt_path'],
            'primary_receipt_sha256': latest['primary_receipt_sha256'],
            'history': [{'recorded_at': r['recorded_at'], 'state': r['node_outcome'], 'primary_decision_path': r['primary_decision_path'],
                         'primary_decision_sha256': r['primary_decision_sha256']} for r in versions]})
    return sorted(result, key=lambda r: digest(r['identity']))

def run_set(path):

    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or set(value) - {'stores', 'runs'}:
        raise ValueError('run set requires explicit stores and/or runs')
    stores = value.get('stores', [])
    runs = value.get('runs', [])
    if (not isinstance(stores, list) or not isinstance(runs, list)
            or not stores and not runs or any(not isinstance(p, str) or not Path(p).is_absolute()
                                             for p in stores + runs)):
        raise ValueError('nonempty absolute run set paths required')
    return sorted(set(stores + [str(Path(p) / 'ci') for p in runs]))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('artifact', 'correction', 'interruptions'):
        cmd = sub.add_parser(name)
        source = cmd.add_mutually_exclusive_group(required=True)
        source.add_argument('--store', type=Path)
        source.add_argument('--run-set', type=Path)
        if name != 'interruptions':
            cmd.add_argument('--value', required=True)
    cmd = sub.add_parser('rebuild');cmd.add_argument('--raw-run', type=Path, required=True)
    cmd.add_argument('--store', type=Path, required=True)
    args = parser.parse_args()
    stores = run_set(args.run_set) if getattr(args, 'run_set', None) else args.store
    result = (rebuild(args.raw_run, args.store) if args.command == 'rebuild' else
              provenance(stores, args.value) if args.command == 'artifact' else
              consumers(stores, args.value) if args.command == 'correction' else interruptions(stores))
    print(json.dumps(result, sort_keys=True, indent=2))

if __name__ == '__main__':
    main()
