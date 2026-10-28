
from pathlib import Path
import copy
import json
import sqlite3

from adaptive_contract import digest, sha, verify_compiled
from adaptive_evidence import Outbox, event, applicable_rules
from read_evidence import validate_read
from adaptive_state import atomic_bytes

def check_bindings(bindings):
    if not bindings: raise ValueError('nonempty hash-bound evidence required')
    for key, binding in bindings.items():
        if not Path(binding['path']).is_absolute() or sha(binding['path']) != binding['sha256']:
            raise ValueError('changed referenced evidence: ' + key)

class NativeOutbox:
    def __init__(self, root, *, graph, sources, local_reads, rules, max_events, max_sequence):
        self.root = Path(root).resolve()
        self.graph, self.sources = copy.deepcopy(graph), copy.deepcopy(sources)
        self.reads, self.rules = copy.deepcopy(local_reads), copy.deepcopy(rules)
        self.outbox = Outbox(root, max_events=max_events, max_sequence=max_sequence)

    def close(self): self.outbox.close()

    def capture(self, bridge, payload):
        check_bindings(dict(self.sources, graph=self.graph))
        compiled = json.loads(Path(self.graph['path']).read_bytes()); verify_compiled(compiled)
        for row in self.reads: validate_read(row['request'], row['receipt'])
        for rule in self.rules:
            if applicable_rules([rule], target=rule['target'], revoked=set()) != [rule]:
                raise ValueError('unaccepted rule in execution evidence')
        settlement = bridge._settlement(payload)
        snapshot = bridge.folder / 'settled.json'
        if json.loads(snapshot.read_bytes()) != payload:
            raise ValueError('primary settlement snapshot differs')
        provenance = {'source_bindings': dict(self.sources, graph=self.graph,
            native_snapshot={'path': str(snapshot), 'sha256': sha(snapshot)}),
            'local_reads': self.reads, 'rules': self.rules, 'graph_sha256': self.graph['sha256'],
            'native': settlement['native']}
        claims = {'native_outcome': settlement['native_outcome'], 'contract_accepted': True,
                  'artifact': 'not_claimed', 'delivery': 'not_requested', 'learning': 'incomplete',
                  'source_semantics': 'complete' if self.reads else 'not_requested'}
        row = event(bridge.occurrence + '/' + bridge.node, 1, 'native_settlement', bridge.occurrence, provenance, claims)
        return self.outbox.append(row)

    def observer(self, bridge):
        def observe(stage, payload):
            result = bridge.observe(stage, payload)
            if stage != 'settled' or result.get('state') != 'recorded': return result
            try:
                key = self.capture(bridge, payload)
                return dict(result, ci_event_id=key, ci='durable_before_ack')
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:

                return dict(result, ci='incomplete', ci_reason=type(exc).__name__+': '+str(exc))
        return observe

    def rebuild(self, bridges, destination):
        if Path(destination).exists(): raise ValueError('preserve old derivative; new destination required')
        for bridge in bridges:
            snapshot = bridge.folder / 'settled.json'
            if snapshot.exists(): self.capture(bridge, json.loads(snapshot.read_bytes()))
        return self.outbox.project(destination)
