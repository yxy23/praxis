
import hashlib
import json
import os

RULE='v30-functional-uncertainty'
DEFINITION_1='Covered: threshold met and nothing discounted; deliver normally.'
DEFINITION_2='Short of coverage, no reflection signal: deliver partly unverified, with uncovered passages highlighted and sources listed.'
DEFINITION_3='Possible request reflection: deliver with visible unverified label; outcome 3 takes precedence.'
SURFACES=('vault note','brief','draft','export','agent context','sb','Leo room','ordinary Deep Research','inline Fact Check')

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def hard_block_enabled():
                                                                          
    value=os.environ.get('PRAXIS45_COVERAGE_HARD_BLOCK','off')
    if value not in {'off','on'}:raise ValueError('invalid coverage hard-block switch')
    return value=='on'

def classify(answer, scores, *, request_id, control_uncertainty=()):
    discounted=any(row['discounted_spans'] for row in scores)
    short=any(row['final_uncovered']>=1000 for row in scores)
    causal=any(row['raw_uncovered']<1000<=row['final_uncovered'] and row['discounted_spans'] for row in scores)
                                                                    
    relied=any(row.get('relied_on_claim') is True and row['discounted_spans'] for row in scores)
    gap=discounted and not (causal or relied)
    outcome=3 if discounted else 2 if short else 1
    blocked=hard_block_enabled() and short
    label={1:'Covered',2:'partly unverified',3:'possible request reflection (unverified)'}[outcome]
    result={'rule':RULE,'request_id':request_id,'answer_sha256':digest(answer),
            'outcome':outcome,'label':label,'delivery_state':'coverage-blocked' if blocked else 'delivered',
            'hard_block':hard_block_enabled(),'classification_gap':gap,'causal_signal':causal,'reliance_signal':relied,
            'equality_wording_gap':any(row['final_uncovered']==1000 for row in scores),
            'scores':scores,'operational_refusal':None,'release':None,'release_bridge':'not required for ordinary delivery',
            'release_log_fields':['request_id','passages','time','action','answer_sha256'],
            'source_status':'unverified' if outcome!=1 else 'coverage threshold met; accuracy not certified',
            'rehearsal_pass':False}
    if control_uncertainty:
        result['control_uncertainty']=list(control_uncertainty)
        result['control_uncertainty_label']=str(len({row['call_id'] for row in control_uncertainty}))+' tool calls/records were not classified'
    result['envelope_sha256']=digest(result)
    return result

def validate(envelope, answer, *, expected_envelope_sha256):
    if not isinstance(expected_envelope_sha256,str) or expected_envelope_sha256!=envelope.get('envelope_sha256'):
        raise ValueError('external coverage envelope anchor differs or absent')
    unsigned={k:v for k,v in envelope.items() if k!='envelope_sha256'}
    if digest(unsigned)!=envelope.get('envelope_sha256') or digest(answer)!=envelope.get('answer_sha256'):
        raise ValueError('coverage envelope or answer hash differs')
    if envelope.get('rule')!=RULE or envelope.get('outcome') not in {1,2,3}:
        raise ValueError('coverage label required')
    expected={1:'Covered',2:'partly unverified',3:'possible request reflection (unverified)'}[envelope['outcome']]
    if envelope.get('label')!=expected:raise ValueError('coverage label stripped or changed')
    if envelope.get('delivery_state') not in {'delivered','coverage-blocked'}:
        raise ValueError('invalid coverage delivery state')
    return envelope

def release(envelope, answer, action=None):
    raise ValueError('Only User can release; authenticated human-action bridge unavailable; answer remains held')

def propagate(envelope, answer, surface, *, expected_envelope_sha256):
    validate(envelope,answer,expected_envelope_sha256=expected_envelope_sha256)
    if surface not in SURFACES:raise ValueError('unbound coverage destination')
    if envelope['delivery_state']!='delivered':raise ValueError('held or blocked answer cannot enter downstream context')
    return {'coverage':envelope,'answer':answer,'surface':surface}

def render(envelope):
    lines=[envelope['label'],'Request: '+envelope['request_id'],'Answer SHA-256: '+envelope['answer_sha256']]
    if envelope.get('control_uncertainty_label'):lines.append(envelope['control_uncertainty_label'])
    if envelope['classification_gap']:lines.append('Possible reflection match: unverified; no source independence inferred.')
    for score in envelope['scores']:
        lines.append('Output '+str(score['output_id'])+': uncovered '+str(score['final_uncovered']))
        lines.append('Sources: '+('; '.join(score['sources']) or 'No matched source'))
        text=score['text']
        for a,b in score['uncovered_spans']:lines.append('Uncovered ['+str(a)+':'+str(b)+']: <mark>'+text[a:b]+'</mark>')
        for a,b in score['discounted_spans']:lines.append('Discount ['+str(a)+':'+str(b)+']: '+score['reason']+'; '+text[a:b])
    if envelope['delivery_state']=='held':lines.append('Only User may release this exact answer; unattended runs remain held.')
    return '\n'.join(lines)
