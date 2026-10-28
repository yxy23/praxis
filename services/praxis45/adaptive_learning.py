
from pathlib import Path
import copy
import json

from adaptive_contract import digest, sha
from adaptive_evidence import OWNERS, applicable_rules, dormant_evaluation, event
from adaptive_support import bound_json
from adaptive_state import atomic_bytes

class LearningBridge:
    def __init__(self, root, outbox, *, repo, provenance):
        self.root = Path(root).resolve()
        if not (self.root / 'ADAPTIVE_FIXTURE_ROOT').is_file(): raise ValueError('isolated learning evidence root required')
        self.outbox, self.repo, self.provenance = outbox, Path(repo), copy.deepcopy(provenance)

    def owner_receipt(self, owner, binding, *, occurrence, sequence):
        if owner not in OWNERS: raise ValueError('canonical learning owner required')
        raw = bound_json(binding)
        owner_path = self.repo / OWNERS[owner]
        provenance = copy.deepcopy(self.provenance)
        provenance['source_bindings']['owner'] = {'path': str(owner_path), 'sha256': sha(owner_path)}
        provenance['source_bindings']['owner_receipt'] = copy.deepcopy(binding)
        row = event('owner/'+owner, sequence, 'owner_receipt', occurrence, provenance,
                    {'owner': owner, 'owner_receipt_sha256': digest(raw), 'acceptance': 'not_inferred',
                     'cadence': 'existing owner only', 'benefit': 'unmeasured'})
        return self.outbox.append(row)

    def admit_rules(self, records, *, target, parent_sha256, revoked):
        rules = applicable_rules(records, target=target, revoked=revoked)
        if any(r['parent_sha256'] != parent_sha256 for r in rules):
            raise ValueError('accepted rule parent does not match admitted target')
        path = self.root / ('admitted-rules-'+digest([target,parent_sha256])+'.json')
        if path.exists():
            old = json.loads(path.read_bytes())
            if old != rules: raise ValueError('in-flight admitted rule versions immutable')
        else:
            atomic_bytes(path,(json.dumps(rules,sort_keys=True)+'\n').encode())
        return rules

    def observed_use(self, rule, receipt_binding):
        row = bound_json(receipt_binding)
        if row.get('rule_sha256') != rule['sha256'] or row.get('target') != rule['target'] or row.get('observed_use') is not True:
            raise ValueError('actual rule-use evidence absent; supply is a separate claim')
        return {'rule_sha256':rule['sha256'], 'supplied':True, 'observed_use':True,
                'receipt':copy.deepcopy(receipt_binding), 'benefit':'unmeasured'}

    def later_normal_receipt(self, criteria, receipt):
        path = self.root / ('dormant-'+digest(criteria)+'.json')
        if path.exists():
            stored=json.loads(path.read_bytes())
            if stored['criteria'] != criteria: raise ValueError('dormant criteria drift')
        else: stored={'criteria':copy.deepcopy(criteria),'intents':{}}
                                                                
        atomic_bytes(path,(json.dumps(stored,sort_keys=True)+'\n').encode())
        if criteria.get('workflow_version') and receipt.get('workflow_version')!=criteria['workflow_version']:return None
        if criteria.get('final_calendar_bound'):
            from datetime import datetime,timezone
            try:
                observed=datetime.fromisoformat(receipt['occurred_at'].replace('Z','+00:00'))
                bound=datetime.fromisoformat(criteria['final_calendar_bound'].replace('Z','+00:00'))
                if bound.tzinfo is None:bound=bound.replace(tzinfo=timezone.utc)
                if observed.tzinfo is None or observed>bound:return None
            except (KeyError,TypeError,ValueError):return None
        intent=dormant_evaluation(criteria,receipt,set(stored['intents']))
        if intent:
            stored['intents'][intent['evaluation_intent_id']]=intent
            atomic_bytes(path,(json.dumps(stored,sort_keys=True)+'\n').encode())
        return intent

def emit_owner_proposal(owner, root, proposal_binding, provenance, *, repo, occurrence, sequence=1):

    from adaptive_evidence import Outbox
    if owner not in OWNERS:raise ValueError('registered learning owner required')
    root=Path(root).resolve()
    if not (root/'ADAPTIVE_FIXTURE_ROOT').is_file():raise ValueError('copied candidate learning root required')
    proposal=bound_json(proposal_binding)
    required={'id','version','parent','evidence','target','tests','test_authority','rollback','proposed_rule','owner_receipt'}
    if not required<=set(proposal) or not all(proposal[k] for k in required):raise ValueError('complete versioned proposal required')
    if type(proposal['version']) is not int or proposal['version']<1:raise ValueError('positive immutable proposal version required')
    owner_receipt=bound_json(proposal['owner_receipt'])
    if (owner_receipt.get('owner')!=owner or owner_receipt.get('occurrence_id')!=occurrence or
        owner_receipt.get('normal_occurrence') is not True or owner_receipt.get('contract_accepted') is not True):
        raise ValueError('original accepted normal owner occurrence receipt required')
    if len({proposal[k]['sha256'] for k in ('parent','target','rollback')})!=1:
        raise ValueError('proposal target/rollback differ from parent preimage')
    bindings=[proposal[k] for k in ('parent','target','rollback','test_authority','proposed_rule','owner_receipt')]+proposal['evidence']+proposal['tests']
    for b in bindings:
        if sha(b['path'])!=b['sha256']:raise ValueError('proposal source/test/authority binding changed')
    if not Path(proposal['target']['path']).resolve().is_relative_to(root):raise ValueError('candidate copy target required')
    path=root/('proposal-'+digest([owner,proposal['id'],proposal['version']])+'.json')
    if path.exists() and json.loads(path.read_bytes())!=proposal:raise ValueError('proposal version immutable')
    if not path.exists():atomic_bytes(path,(json.dumps(proposal,sort_keys=True)+'\n').encode())
    provenance=copy.deepcopy(provenance)
    provenance['source_bindings']['owner']={'path':str(Path(repo)/OWNERS[owner]),'sha256':sha(Path(repo)/OWNERS[owner])}
    provenance['source_bindings']['proposal']={'path':str(path),'sha256':sha(path)}
    row=event('proposal/'+owner+'/'+proposal['id'],sequence,'rule_proposal',occurrence,provenance,
              {'owner':owner,'proposal_version':proposal['version'],'proposal_sha256':digest(proposal),
               'parent_sha256':proposal['parent']['sha256'],'evidence':proposal['evidence'],
               'target':proposal['target'],'tests':proposal['tests'],'test_authority':proposal['test_authority'],
               'rollback':proposal['rollback'],'acceptance':'not_inferred','benefit':'unmeasured'})
    box=Outbox(root,max_events=256,max_sequence=256)
    try:return {'state':'proposal_emitted','event_id':box.append(row),'accepted_store_writes':0,'benefit':'unmeasured'}
    finally:box.close()

def freeze_dormant(root, criteria, design_binding):

    required={'workflow_version','rule_sha256','eligible_later_receipt_predicate','remaining_information_target',
              'final_calendar_bound','rollback_preimage'}
    if not required<=set(criteria) or not all(criteria[k] for k in required):raise ValueError('Ada dormant criteria incomplete')
    if sha(design_binding['path'])!=design_binding['sha256']:raise ValueError('evaluation design changed')
    root=Path(root).resolve()
    if not (root/'ADAPTIVE_FIXTURE_ROOT').is_file():raise ValueError('copied dormant root required')
    value={'criteria':copy.deepcopy(criteria),'design':copy.deepcopy(design_binding),'benefit':'unmeasured'}
    path=root/('dormant-design-'+digest(criteria)+'.json')
    if path.exists() and json.loads(path.read_bytes())!=value:raise ValueError('dormant design immutable')
    if not path.exists():atomic_bytes(path,(json.dumps(value,sort_keys=True)+'\n').encode())
    return {'path':str(path),'sha256':sha(path)}

def settle_evaluator(root, intent, completion, release):

    native=completion.get('native')
    if (intent.get('state')!='ready_for_bounded_evaluator' or not native or
        completion.get('contract_accepted') is not True or completion.get('worker_count')!=1 or completion.get('outcome') not in {'succeeded','failed'} or
        release.get('native')!=native or release.get('state')!='released'):
        raise ValueError('original settled evaluator plus exact released worker required')
    root=Path(root).resolve()
    if not (root/'ADAPTIVE_FIXTURE_ROOT').is_file():raise ValueError('copied evaluator root required')
    row={'intent':intent,'completion':completion,'release':release,'benefit':'unmeasured'}
    path=root/('evaluator-settled-'+digest(intent['evaluation_intent_id'])+'.json')
    if path.exists() and json.loads(path.read_bytes())!=row:raise ValueError('evaluator settlement immutable')
    if not path.exists():atomic_bytes(path,(json.dumps(row,sort_keys=True)+'\n').encode())
    return row

def main(argv=None):
    import argparse
    p=argparse.ArgumentParser(description='Candidate-only owner proposal emission; no promotion or worker launch')
    p.add_argument('--owner',choices=list(OWNERS),required=True);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--proposal',type=Path,required=True);p.add_argument('--provenance',type=Path,required=True)
    p.add_argument('--occurrence',required=True);p.add_argument('--sequence',type=int,default=1)
    a=p.parse_args(argv)
    result=emit_owner_proposal(a.owner,a.root,{'path':str(a.proposal),'sha256':sha(a.proposal)},json.loads(a.provenance.read_bytes()),
        repo=Path(__file__).resolve().parents[2],occurrence=a.occurrence,sequence=a.sequence)
    print(json.dumps(result));return 0

if __name__=='__main__':raise SystemExit(main())
