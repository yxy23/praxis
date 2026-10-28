
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
import p4_run as p
from p4_contract import binding, digest, require, EVALUATION_LABEL
from native_capabilities import EVIDENCE, CLAIMS, BOUNDS, BOUND_BASIS, validate_claim_input, load_json
from p4_output_contract import instructions
from freeze_rehearsal import graph
from candidate_run import closure_manifest, release_info, source_path, engine_paths
from native_adapter import REPO, execute_config

VERSION='praxis-4.5.0'
from source_access import TRANSMISSION

CAPABILITY_STATUS='Deep Research retrieves live public sources; Fact Check returns supported, contradicted, or unresolved for each accepted claim. Unreachable sources and missing evidence are reported. '+TRANSMISSION
LIMITS=CAPABILITY_STATUS+' At most 3 user-derived queries, 6 retrieval attempts including failures, 8 retained sources and 6 claims. Claims beyond six are reported individually as unresolved and not checked; resubmit them in another request. Post hoc accounting checks the helper log; source hashes and quoted helper-returned passages are validated; coverage measurements and uncertainty labels travel with each answer. Coverage hard-block defaults OFF. Machine extraction is not full model inspection; coverage is not factual accuracy. No stock Amplifier research tools or newly billed API.'
PATTERN=re.compile(r'^@Leo\s+(?:(deep research|fact check)( preview)?:\s*(.+)|(graph status|graph recover|provenance|correction consumers|interruptions)(?:\s+(.+))?)$',re.I|re.S)

def match(text):
    return PATTERN.fullmatch(text.strip())

def state_root():
    rehearsal=os.environ.get('PRAXIS45_FRESH_REHEARSAL_ROOT')
    if rehearsal:
        from p4_contract import commissioned_root
        require(commissioned_root(rehearsal), 'fresh rehearsal root outside commission')
        return Path(rehearsal).resolve()
    info=release_info()
    home=Path(info['installation_home']) if info and info.get('installation_home') else Path.home()
    return home/'.local/share/praxis/state/praxis45'

def activation_path():
    info=release_info()
    home=Path(info['installation_home']) if info and info.get('installation_home') else Path.home()
    return home/'.local/share/praxis/releases/activation.json'

def rehearsal_config(*,check_entry=False):
    root=os.environ.get('PRAXIS45_REHEARSAL_ROOT')
    if root is None:return None
    require(bool(root.strip()),'empty ordinary rehearsal root')
    from p4_contract import commissioned_root
    require(commissioned_root(root),'ordinary rehearsal root outside commissioned namespace')
    root=Path(root).resolve()
    path=root/'ordinary-config.json'
    require(path.is_file() and not path.is_symlink(),'frozen ordinary rehearsal config missing')
    cfg=json.loads(path.read_bytes())
    require(Path(cfg['output_root']).resolve()==root,'ordinary rehearsal root differs')
    flag=json.loads((root/'activation.json').read_bytes())
    require(flag=={'version':VERSION,'commit':cfg['candidate_commit'],'enabled':True},'isolated activation differs')
    p.validate_config(cfg)
    runtime=cfg['ordinary_surface']['runtime']
    require(os.environ.get('PRAXIS45_RUNTIME_TREE','source')==runtime,'ordinary runtime tree differs')
    if runtime!='source':
        info=release_info();require(info is not None,'runtime ordinary smoke requires installed release')
        entry=Path(info['installation_home'])/runtime/'skills/orca-dispatch/p4_run.py'
        from release_support import wrapper
        require(entry.read_bytes()==wrapper(REPO.parent,'p4_run'),'runtime entry binding differs')
        if check_entry:require(Path(sys.argv[0]).resolve()==entry.resolve(),'ordinary smoke did not execute bound runtime entry')
    return path

def available():
    try:return enabled()
    except (ValueError, OSError):
        if 'PRAXIS45_REHEARSAL_ROOT' in os.environ:raise
        return False                                                            

def enabled():
    if "PRAXIS45_REHEARSAL_ROOT" in os.environ:
        rehearsal_config();return True
    if os.environ.get('PRAXIS45_FRESH_REHEARSAL_ROOT'):
        require(not release_info(), 'source-only fresh rehearsal requires source mode')
        state_root();return True
    path=activation_path()
    if not path.is_file() or path.is_symlink():
        return False
    value=json.loads(path.read_bytes());release=release_info()
    return value.get('schema_version')==1 and value.get('enabled') is True and value.get('version')==VERSION and release is not None and value.get('commit')==release['commit']

def find_config(identifier):
    root=state_root()
    paths=[]
    for path in root.glob('*/config.json'):
        raw=path.read_bytes()
        if identifier in {path.parent.name, 'px45-'+hashlib.sha256(raw).hexdigest()}:
            paths.append(path)
    require(len(paths)==1,'one known occurrence required; use the occurrence ID printed at launch')
    return paths[0]

OVER_LIMIT_REASON='not checked: per-request limit is 6 claims; resubmit this claim in a further request'

def split_claims(value):
    rows=value.get('claims') if isinstance(value,dict) else value
    require(isinstance(rows,list) and bool(rows),'nonempty JSON claims list required')
    rows=[dict(row,claim_id=row.get('claim_id','U'+str(i+1))) if isinstance(row,dict) else {'claim_id':'U'+str(i+1),'claim':row} for i,row in enumerate(rows)]
    require(all(isinstance(row.get('claim_id'),str) and row['claim_id'].strip() and isinstance(row.get('claim'),str) and row['claim'].strip() for row in rows),'every submitted claim needs nonempty wording and ID')
    require(len({row['claim_id'] for row in rows})==len(rows),'duplicate submitted claim ID')
    manifest=dict(value) if isinstance(value,dict) else {}
    manifest['claims']=rows[:BOUNDS['claims']]
    manifest.setdefault('sources',[])
    if not manifest['sources']:manifest.setdefault('evidence_policy','bounded-public-web')
    manifest.setdefault('source_budget',{'web_discovery_queries_max':3,'complete_retrievals_max':6,'retained_sources':8})
    validate_claim_input(manifest,discovery=True)
    overflow=[{'claim_id':row['claim_id'],'claim':row['claim'],'epistemic_label':'unresolved','verdict':'unresolved',
               'reason':OVER_LIMIT_REASON,'evidence':[],'processing_status':'not_checked'} for row in rows[BOUNDS['claims']:]]
    return manifest,overflow

def freeze(action, body, router, *, preview=False):

    identity='user-'+uuid.uuid4().hex
    root=state_root()/identity
    info=release_info()
    if not info and os.environ.get('PRAXIS45_FRESH_REHEARSAL_ROOT'):
        import subprocess
        info={'commit':subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip()}
        from freeze_rehearsal import SOURCE, ENGINE_CLOSURE
        source,closure=SOURCE,ENGINE_CLOSURE
    else:
        require(info is not None,'ordinary execution requires a versioned release')
        source=REPO.parent/'engine/source';closure=REPO.parent/'engine/engine-closure.json'
    cfg={'p4_schema_version':1,'enabled':True,'ordinary':True,'candidate_commit':info['commit'],
        'kind':'web' if action=='deep research' else 'claims','workspace':'path:'+str(REPO),
        'state_root':str(root/'state'),'output_root':str(root),'engine_source':str(source),'engine_closure':str(closure),
        'closure':closure_manifest()+[binding(closure)], 'objective':body,'context':{'identity':identity,'digest':digest(body)},
        'defaults':{'overall_timeout_ms':2400000,'start_timeout_ms':90000},'nodes':{},'inputs':[],'research_sources':[],
        'interruption_point':'after-dispatch-before-collection','embargo':{'paths':[],'enforcement':'supplied-context allowlist; no OS access isolation'},
        'evaluation_label':EVALUATION_LABEL,'producer_identity':None}
    from immutable_code import freeze
    cfg['code_identity']=freeze(cfg['candidate_commit'],cfg['closure'])
    from immutable_code import bootstrap
    cfg['checker_bootstrap']=bootstrap()
    claims=None
    from acquisition import bind_user
    acquisition=bind_user(identity,body)
    if action=='fact check':
        artifact=Path(body)
        if artifact.is_absolute():
            require(artifact.is_file() and not artifact.is_symlink(),'claim artifact missing or symlink')
            require(artifact.name not in {'qualification-expected.json','heldout-expected.json'},'answer keys refused')
            claims,unprocessed=split_claims(load_json(artifact))
            producer={'kind':'artifact','label':'user-designated prior artifact; model producer unknown',**binding(artifact)}
            claim_path=root/'processed-claims.json'
        else:
            supplied=json.loads(body) if body.lstrip().startswith(('{','[')) else [line.strip() for line in body.splitlines() if line.strip()]
            claims,unprocessed=split_claims(supplied)
            claim_path=root/'user-claims.json'
            producer={'kind':'user','label':'User supplied these claims; no model producer',
                      'path':str(claim_path),'sha256':hashlib.sha256((json.dumps(claims,sort_keys=True)+'\n').encode()).hexdigest()}
        cfg['producer_identity']=producer
        cfg['processed_claim_input']=str(claim_path)
        cfg['unprocessed_claims']=unprocessed
        acquisition=bind_user(identity,body,[row['claim'] for row in claims['claims']])
        if preview:
            return {'status':'preview','native_launches':0,'producer_identity':producer,'limits':LIMITS,'writes':0,'acquisition':acquisition,
                    'claims_to_check':claims['claims'],'unprocessed_claims':unprocessed}
        root.mkdir(parents=True,exist_ok=False)
        claim_path.write_text(json.dumps(claims,sort_keys=True)+'\n')
        cfg['inputs'].append({'path':producer['path'],'sha256':producer['sha256']})
        processed=binding(claim_path)
        if processed not in cfg['inputs']:cfg['inputs'].append(processed)
        cfg['claims_sha256']=processed['sha256']
    needs_discovery=action=='fact check' and claims.get('evidence_policy')=='bounded-public-web'
    names=['research','audit'] if action=='deep research' or needs_discovery else ['audit']
    cfg['graph']=graph(names);cfg['graph_sha256']=hashlib.sha256(cfg['graph'].encode()).hexdigest()
    producers=[]
    for name in names:
        capability=EVIDENCE if name=='research' else CLAIMS
        schema=REPO/'services/praxis45/schemas'/(capability+'.schema.json')
        bind=binding(schema);cfg['inputs'].append(bind)
        work_body=body if action=='deep research' else '\n'.join(row['claim'] for row in claims['claims'])
        spec=('Question: '+work_body+'. '+LIMITS+' Read only public web sources or explicitly supplied claims. No login, submission, new workers, scientific decisions or global memory. '
              'Preserve complete original fetched bytes, exact passages, timestamps and hashes; record failed operations. Include narrative, coverage_boundary and missingness. '
              'Include evaluation_label exactly '+json.dumps(EVALUATION_LABEL)+'. '+instructions(capability,str(root/'operations'/name/'{dispatch_id}.jsonl'))+' In worker_done include Praxis-native receipt written.')
        spec+=' Keep every submitted claim unchanged, including claims lacking evidence. For research rows without read_source evidence set evidence=[] and missing_evidence to the actual retrieval failure or missing support. Checkers return exactly one row per claim: supported or contradicted only with fetched citations, otherwise unresolved with reason and missing_evidence. Never silently skip a claim or cite an unreachable source. Include title if returned by the helper, URL, accessed_at and the exact returned passage/locator for every citation. Search snippets are keyed by the search nonce and pseudo-source ID; quote only text returned by that search or its passage/find inspection, naming its operation_nonce.'
        if name=='audit' and action=='deep research':
            spec+=' Independently check the preceding research receipt with exactly the same claim IDs and wording.'
        elif name=='audit':
            spec+=' Producer identity: '+json.dumps(cfg['producer_identity'],sort_keys=True)+'. This checks user claims; no opposite-provider independence is claimed without an attested model producer.'
        unit={'seat':'Hermione' if name=='research' else 'Scully','task_class':'literature-research' if name=='research' else 'verification',
              'capability':capability,'spec':spec,'substantive_spec':spec,'source_bindings':copy.deepcopy(cfg['inputs']),
              'outputs':[{'path':str(root/(name+'.json')),'required':True,'validator':capability}],
              'return_contract':{'required_substrings':['Praxis-native receipt written']},'consumes':[]}
        unit['acquisition']=copy.deepcopy(acquisition)
        unit['operation_log']=str(root/'operations'/name/'{dispatch_id}.jsonl')
        if action=='deep research' and name=='audit':unit['bind_evidence_from']='research'
        if action=='fact check':
            unit['producer_identity']=cfg['producer_identity']
            if name=='research':
                unit['claim_input']=cfg['processed_claim_input'];unit['evidence_discovery']=True
                unit['spec']+=' Collect evidence for exactly these claim rows, unchanged, with no verdicts: '+json.dumps(claims['claims'],sort_keys=True)
            elif needs_discovery:
                unit['bind_evidence_from']='research'
                unit['spec']+=' Read only the hash-bound retained sources and exact claim bindings from the accepted producer; no web discovery or extra retrieval.'
            else:
                unit['claim_input']=cfg['processed_claim_input']
                unit['source_bindings'] += [{'path':row.get('retained_path',row['path']),'sha256':row['sha256']} for row in claims['sources']]
                for row in unit['source_bindings']:
                    if row not in cfg['inputs']:cfg['inputs'].append(row)
        row=router.route(dict(unit,id=name,sensitivity='internal',risk='medium'),None,False,run_key=identity,producer_model=','.join(producers) or None)
        unit['frozen_route']={k:row[k] for k in ('effective_model','provider','agent','launch_target','effort')}
        unit['substantive_spec']=unit['spec']
        unit['requested_model']=row['effective_model'];producers.append(row['effective_model']);cfg['nodes'][name]=unit
    if preview:
                                                     
        return {'status':'preview','native_launches':0,'writes':0,'nodes':list(cfg['nodes']),'limits':LIMITS,'acquisition':acquisition}
    root.mkdir(parents=True,exist_ok=action=='fact check')
    config_path=root/'config.json'
    from p4_contract import durable_json
    durable_json(config_path,cfg)
    return config_path

def failure_reason(cfg, result):

    if result.get('status')=='succeeded' or cfg.get('kind')!='claims':return ''
    missing=[]
    for unit in cfg['nodes'].values():
        if unit.get('capability')!=EVIDENCE:continue
        path=Path(unit['outputs'][0]['path'])
        try:
            receipt=json.loads(path.read_bytes())
            retained={(r['source'],r['content_sha256']) for r in receipt.get('retained_sources',[])}
            for claim in receipt['claims']:
                if not any(row.get('basis')=='read_source' and (row.get('source'),row.get('content_sha256')) in retained for row in claim['evidence']):
                    missing.append(claim['claim_id']+': '+claim['claim'])
        except (OSError,ValueError,KeyError,TypeError,AttributeError):

            from operation_evidence import read_log
            logs=list((Path(cfg['output_root'])/'operations').glob('research/*.jsonl'))
            try:
                if not logs:continue
                rows=[row for log in logs for row in read_log(log,log.stem)[0]]
                if any(row['op'] in {'read','fetch'} and row['status']=='ok' for row in rows):continue
                requested=load_json(unit['claim_input'])['claims']
                missing.extend(row['claim_id']+': '+row['claim'] for row in requested)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
    if not missing:return ''
    return 'Claims without retained evidence: '+ '; '.join(missing)+'. These claims are unresolved; no independent supported or contradicted verdict is accepted.'

def finish(result, config, progress):

    cfg=json.loads(Path(config).read_bytes())
    root=Path(cfg['output_root']).resolve()
    diagnostics=[];envelopes=[]
    issues=['Workflow issue ('+name+'): '+row['reason'] for name,row in result.get('unresolved',{}).items() if row.get('reason')]
    reason=failure_reason(cfg,result)
                                                                               
    accepted={};coverages={}
    for decision in (p.occurrence(Path(config).read_bytes())/'raw').glob('*/decision.json'):
        outcome=json.loads(decision.read_bytes())['outcome']
        from coverage_policy import render
        for envelope in outcome.get('coverage',[]):
            diagnostics += ['',render(envelope),''];envelopes.append(envelope)
        if outcome.get('state')=='success':
            for artifact in outcome.get('artifacts',[]):
                if artifact.get('validated') and artifact.get('capability_validation',{}).get('accepted') is True:
                    accepted[artifact['path']]=artifact['sha256']
                    coverages[artifact['path']]=artifact['capability_validation'].get('independent_recount',{}).get('coverage')
    for unit in cfg['nodes'].values():
        if unit.get('capability')!=CLAIMS:continue
        for output in unit['outputs']:
            path=Path(output['path'])
            if str(path) not in accepted or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=accepted[str(path)]:continue
            value=json.loads(path.read_bytes())
            from coverage_policy import propagate,render
            envelope=coverages[str(path)]
            require(isinstance(envelope,dict),'accepted answer lacks coverage label and provenance')
            propagate(envelope,value,'brief',expected_envelope_sha256=envelope['envelope_sha256'])
            diagnostics += ['',render(envelope),''];envelopes.append(envelope)
            diagnostics += ['', 'Coverage: '+value['coverage_boundary'], 'Missing evidence: '+('; '.join(value['missingness']) or 'None reported'), '']
    verdict_rows=[]
    for path in accepted:
        artifact=json.loads(Path(path).read_bytes())
        if artifact.get('receipt_type')!=CLAIMS:continue
        verdict_rows=[dict(row,epistemic_label={'refuted':'contradicted','unverifiable':'unresolved','opinion':'unresolved'}.get(row.get('epistemic_label'),row.get('epistemic_label')) or 'unresolved') for row in artifact['claims']]
    check_reason=''
    findings=[]
    if not verdict_rows:
        check_reason='; '.join(row['reason'] for name,row in result.get('unresolved',{}).items()
                              if cfg['nodes'].get(name,{}).get('capability')==CLAIMS and row.get('reason'))
        check_reason=check_reason or 'independent check unavailable; no accepted checker receipt'
        if cfg.get('kind')=='web':
            for unit in cfg['nodes'].values():
                if unit.get('capability')!=EVIDENCE:continue
                for output in unit['outputs']:
                    path=Path(output['path'])
                    if str(path) not in accepted or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=accepted[str(path)]:continue
                    value=json.loads(path.read_bytes())
                    from coverage_policy import propagate,render
                    envelope=coverages[str(path)]
                    require(isinstance(envelope,dict),'accepted research lacks coverage label and provenance')
                    propagate(envelope,value,'brief',expected_envelope_sha256=envelope['envelope_sha256'])
                    diagnostics += [render(envelope), ''];envelopes.append(envelope)
                    diagnostics += ['Research narrative: '+value.get('narrative',''), '']
                    for claim in value['claims']:
                        findings.append(dict(claim,verification_status='not_independently_verified',verification_reason=check_reason))
                    diagnostics += ['', 'Coverage: '+value['coverage_boundary'], 'Missing evidence: '+('; '.join(value['missingness']) or 'None reported'), '']
    if cfg.get('kind')=='claims' and not verdict_rows:
        requested=load_json(cfg.get('processed_claim_input',cfg['producer_identity']['path']))['claims']
        verdict_rows=[dict(row,epistemic_label='unresolved',reason=reason or check_reason,evidence=[]) for row in requested]
        for row in verdict_rows:
            row['processing_status']='not_completed'
    for row in verdict_rows:
        row['verdict']=row['epistemic_label']
        row.setdefault('processing_status','checked')
    for row in cfg.get('unprocessed_claims',[]):
        verdict_rows.append(dict(row))
                                                                            
    labels=list(dict.fromkeys(c['label'] for c in envelopes if c))
    labels += list(dict.fromkeys(c['control_uncertainty_label'] for c in envelopes if c.get('control_uncertainty_label')))
    if findings:labels.append('NOT independently verified')
    if any('held' in c.get('delivery_state','') for c in envelopes):labels.append('HELD: only User may release')
    labels=list(dict.fromkeys(labels))
    def cell(value):
        return str(value).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('|','&#124;').replace('\n','<br>')
    table=['| Claim ID | Claim | Verdict | Reason | Citations |', '| --- | --- | --- | --- | --- |']
    display_rows=verdict_rows or findings
    verdicts=[]
    for row in display_rows:
        verdict=('unresolved / not checked — per-request limit is 6; resubmit' if row.get('processing_status')=='not_checked' else
                 row.get('verdict','NOT independently verified'))
        explanation=row.get('verification_reason',row.get('reason',''))
        citations=[]
        for evidence in row.get('evidence',[]):
            url=evidence.get('url',evidence['source']);title=evidence.get('title') or url
            citations.append('Source: ['+cell(title)+']('+url+'); URL: '+cell(url)+
                             '; title: '+cell(title)+'; accessed: '+cell(evidence['accessed_at'])+
                             '; '+cell(evidence['locator'])+'; date: '+cell(evidence.get('source_date','unknown'))+'; SHA-256 '+cell(evidence['content_sha256'])+
                             '; quoted passage: '+cell(json.dumps(evidence['passage'],ensure_ascii=False)))
        table.append('| '+' | '.join(cell(v) for v in (row['claim_id'],row['claim'],verdict,explanation))+' | '+('<br>'.join(citations) or 'None')+' |')
        verdicts.append(row['claim_id']+': '+verdict+' — '+row['claim']+'; '+explanation)
    lines=['# Praxis evidence brief','','Question: '+cfg['objective'],'',
           'Workflow status: '+str(result.get('status','unknown')),'','## Claim verdicts','',*table]
    if not display_rows:lines += ['','No accepted independent claim verdicts are available; do not treat this workflow as a supported answer.']
    if findings:lines += ['','Independent verdict table: empty — '+check_reason]
    lines += ['','## Uncertainty','',*('- '+label for label in labels)]
    if issues:lines.append('- Workflow issues: '+str(len(issues))+'; details in appendix.')
    if reason:lines.append('- Some claims lack retained evidence; details in appendix.')
    render_limits=('Per request: at most 6 claims, 3 user-derived queries, 6 retrieval attempts including failures, and 8 retained sources; resubmit remaining claims in another Fact Check request. '
                   'Source hashes and helper-returned quotes are validated; coverage is post hoc, does not certify factual accuracy, and cannot detect silent reads or consistent forgery. '
                   'Coverage hard-block defaults OFF; no scientific decision is adopted.')
    lines += ['','## Limits','',render_limits,
              '', '## Appendix: coverage diagnostics','',*issues,*([reason] if reason else []),*diagnostics]
    root.mkdir(parents=True,exist_ok=True)
    brief=p.immutable_bytes(root/'briefs','.md', ('\n'.join(lines)+'\n').encode())
    summary='\n'.join(['Question: '+cfg['objective'],'Workflow status: '+str(result.get('status','unknown')),*verdicts,
                      'Uncertainty: '+('; '.join(labels) or 'None reported'),
                      *issues,*([reason] if reason else []),'Limits: '+render_limits,'Brief: '+str(brief.resolve())])
    progress('Research and independent checking finished; brief rendered.')
    return dict(result,claim_verdicts=verdict_rows,research_findings=findings,independent_check_reason=check_reason,checked_claim_count=sum(row['processing_status']=='checked' for row in verdict_rows),unprocessed_claim_count=len(cfg.get('unprocessed_claims',[])),brief_path=str(brief.resolve()),human_summary=summary,
                quiet_summary=('Praxis '+str(result.get('status','unknown'))+'; '+'; '.join(verdicts)+'; Uncertainty: '+('; '.join(labels) or 'None reported')+('; '+reason if reason else '')+'; Limit: 6 claims; resubmit remaining claims; Brief: '+str(brief.resolve())).replace('\n',' '),limits=render_limits)

def handle(text, progress=None):
    progress=progress or (lambda message: None)
    parsed=match(text)
    if parsed is None and re.match(r'^@Leo\s+fact check(?: preview)?:',text,re.I):
        return {'status':'input_refused','error':{'code':'invalid_claim_input','reason':'1 to 6 nonempty user claims required'},'native_launches':0,'limits':LIMITS}
    require(parsed is not None,'unknown ordinary graph request')
    if not enabled():
        return {'status':'not_activated' if release_info() else 'not_installed','native_launches':0,'writes':0,
                'message':'Praxis 4.5 is installed but not activated.' if release_info() else 'Praxis 4.5 is not installed.', 'limits':LIMITS}
    action,preview,body,query,value=parsed.groups()
    frozen=rehearsal_config(check_entry=True)
    if frozen:
        progress('Frozen-corpus research and independent checking started.')
        raw=frozen.read_bytes();cfg=json.loads(raw)
        require(text==cfg['ordinary_surface']['request'],'ordinary request differs from frozen bounded question')
        p.q.configure(*engine_paths(cfg))
        dispatcher,router,orca=p.runtime(cfg)
        if os.environ.get('PRAXIS45_ZERO_LAUNCH')=='1':
            return dict(p.preview(cfg,dispatcher,router,config_bytes=raw),status='preview',launch_boundary='frozen ordinary entry; execution not called')
        result=execute_config(raw,dispatcher,router,orca,adapter_factory=p.P4Adapter)
        return finish(dict(result,ordinary_surface=cfg['ordinary_surface'],config_path=str(frozen)),frozen,progress)

    if query:
        query=query.lower()
        if query in {'graph status','graph recover'}:
            path=find_config(value);raw=path.read_bytes();cfg=json.loads(raw)
            if query=='graph status':return dict(p.status(raw,cfg),status='observed',native_launches=0,writes=0,limits=LIMITS)

            require(not cfg.get('canary'),'read-only canary cannot recover or launch')
            p.q.configure(*engine_paths(cfg))
            dispatcher,router,orca=p.runtime(cfg)
            return dict(execute_config(raw,dispatcher,router,orca,adapter_factory=p.P4Adapter),recovery_mode='Praxis dispatcher lease/original-only collection; upstream DOT checkpoint-resume unavailable in 4.5',limits=LIMITS)
        stores=[]
        for config in state_root().glob('*/config.json'):
            stores.append(str(p.occurrence(config.read_bytes())/'ci'))
        if not stores:return {'status':'observed','records':[],'coverage':'no executed ordinary occurrences','native_launches':0,'writes':0,'limits':LIMITS}
        require(value or query=='interruptions','query value required')
        rows=(p.native_ci.provenance(stores,value) if query=='provenance' else p.native_ci.consumers(stores,value) if query=='correction consumers' else p.native_ci.interruptions(stores))
        return {'status':'observed','records':rows,'native_launches':0,'writes':0,'limits':LIMITS}
    dispatcher=p.runtime_dispatcher();command=dispatcher.JsonCommand(dispatcher.launch_env())
    router=dispatcher.RouterClient(dispatcher.default_router(),command)
    progress('Freezing bounded request; '+CAPABILITY_STATUS)
    try:
        config=freeze(action.lower(),body,router,preview=bool(preview))
    except (ValueError,OSError,TypeError,KeyError) as exc:
        return {'status':'input_refused','error':{'code':'invalid_claim_input','reason':str(exc)},'native_launches':0,'limits':LIMITS}
    if isinstance(config,dict):return config
    raw=config.read_bytes();cfg=json.loads(raw);p.validate_config(cfg)
    p.q.configure(*engine_paths(cfg))
    dispatcher,router,orca=p.runtime(cfg)
    progress('Research or fact checking started within the frozen limits.')
    result=execute_config(raw,dispatcher,router,orca,adapter_factory=p.P4Adapter)
    return finish(dict(result,occurrence_id=config.parent.name,config_path=str(config)),config,progress)

def main(argv=None):
    import argparse
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--request',required=True)
    ap.add_argument('--json-events',action='store_true');args=ap.parse_args(argv)
    try:
        result=handle(args.request,progress=(lambda message: print(json.dumps({'type':'praxis45_progress','message':message}),flush=True)) if args.json_events else None)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('status') in {'observed','preview','succeeded'} else 4
    except (Exception,p.q.UnresolvedAttempt) as exc:
        print(json.dumps({'status':'unresolved','reason':str(exc),'limits':LIMITS}));return 4

if __name__=='__main__':sys.exit(main())
