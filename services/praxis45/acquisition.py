
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urldefrag

VERSION='user-words-v3'
URL=re.compile(r'https://[^\s<>"\']+')

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def normalize_url(url):
    if not isinstance(url,str):raise ValueError('acquisition URL string required')
    return urldefrag(url)[0]

STOP=set('what when where why how who which does do did is are was were be been being the a an and or of to in on at for from by with its it this that these those about say says public record sources source made'.split())

def compile_user(user, *, version=VERSION):
    if (not isinstance(user,dict) or set(user)!={'origin','identity','request','claims'}
            or user['origin']!='user' or not isinstance(user['identity'],str) or not user['identity']
            or not isinstance(user['request'],str) or not user['request'].strip()
            or not isinstance(user['claims'],list) or len(user['claims'])>6
            or any(not isinstance(c,str) or not c.strip() for c in user['claims'])):
        raise ValueError('pre-producer user input required')
    queries=[];urls=[]
    for text in [user['request'],*user['claims']]:
        for url in URL.findall(text):
            normalized=normalize_url(url)
            if normalized not in urls:urls.append(normalized)
    for text in (user['claims'][:3] if user['claims'] else [user['request']]):
        words=re.findall(r'[^\W_]+',URL.sub('',text),re.UNICODE);chosen=[]
        for word in words:
            if len(' '.join([*chosen,word]))>=180:break
            chosen.append(word)
        query=' '.join(chosen)
        if query and query not in queries:queries.append(query)
    if version==VERSION:
        texts=user['claims'][:3] if user['claims'] else [user['request']]
        focused=[];names=[]
        for text in texts:
            words=re.findall(r'[^\W_]+',URL.sub('',text),re.UNICODE)
            key=' '.join(w for w in words if w.casefold() not in STOP)
            if key and len(key)<180 and key not in focused:focused.append(key)

            runs=re.findall(r'\b(?:[A-Z][\w]*)(?:\s+[A-Z][\w]*)+',URL.sub('',text))
            for run in runs:
                name=' '.join(w for w in run.split() if w.casefold() not in STOP)
                if name and len(name)<180 and name not in names:names.append(name)
        queries=([*focused,*queries] if user['claims'] else [queries[0],*names,*focused] if queries else [*names,*focused])
        queries=list(dict.fromkeys(queries))[:3]
    elif version!='user-words-v2':raise ValueError('unknown user query compiler')
    return {'compiler':version,'queries':queries,'urls':urls}

def bind_user(identity,request,claims=()):
    user={'origin':'user','identity':identity,'request':request,'claims':list(claims)}
    return {'user':user,'user_sha256':digest(user),'plan':compile_user(user)}

def reconstruct(binding):
    if not isinstance(binding,dict) or set(binding)!={'user','user_sha256','plan'}:
        raise ValueError('pre-producer acquisition binding missing')
    if digest(binding['user'])!=binding['user_sha256']:
        raise ValueError('user acquisition bytes drifted')
    plan=compile_user(binding['user'],version=binding['plan'].get('compiler'))
    if plan!=binding['plan']:raise ValueError('bound query plan differs from pure compiler')
    return plan

def discovery_urls(row,dispatch):

    if row.get('dispatch_id')!=dispatch or row.get('op')!='search' or row.get('status')!='ok':
        raise ValueError('discovery Dispatch/status differs')
    path=Path(row.get('cache',''))
    if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('discovery original missing or symlink')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=row.get('sha256'):raise ValueError('discovery original hash differs')
    from source_access import discovery
    results=discovery(raw,row.get('backend','mediawiki'))
    rendered=json.dumps(results,ensure_ascii=False)
    if (rendered!=row.get('discovery_text') or hashlib.sha256(rendered.encode()).hexdigest()!=row.get('discovery_text_sha256')):
        raise ValueError('discovery results differ from original bytes')
    return [normalize_url(hit['url']) for hit in results]

def authorize(entry,rows,row, *, pre_network=False, fresh_discovery=None):

    dispatch=entry.get('identities',{}).get('dispatch_id')
    if row.get('dispatch_id')!=dispatch:raise ValueError('acquisition Dispatch differs')
    if pre_network:authoritative(entry)
    plan=reconstruct(entry.get('acquisition'))
    if row['op']=='search':
        if row['source'] not in plan['queries']:raise ValueError('query outside pre-producer bound plan')
        return {'kind':'user-query','user_sha256':entry['acquisition']['user_sha256'],'plan_sha256':digest(plan)}
    if row['op']!='fetch':raise ValueError('acquisition operation required')
    url=row['source']
    url=normalize_url(url)
    if url in plan['urls']:
        return {'kind':'user-url','user_sha256':entry['acquisition']['user_sha256'],'plan_sha256':digest(plan)}
    for source in entry.get('sources',[]):
        if source['path']==url:
            return {'kind':'frozen-url','sha256':source['sha256']}

    candidates=[fresh_discovery] if fresh_discovery is not None else rows
    if row.get('atomic_search_nonce'):
        candidates=[prior for prior in candidates if prior.get('nonce')==row['atomic_search_nonce']]
    for prior in candidates:
        if prior.get('op')=='search' and prior.get('status')=='ok':
            authorize(entry,[],prior)
            if url in discovery_urls(prior,dispatch):
                return {'kind':'discovery-url','nonce':prior['nonce'],'entry_sha256':prior['entry_sha256']}
    raise ValueError('URL outside bound user/frozen/same-Dispatch discovery provenance')

def verify(entry,unit,rows,authenticated):
    acquisitions=[r for r in rows if r['op'] in {'search','fetch'}]
    if not acquisitions:return
                                                                           
    if entry.get('acquisition')!=unit.get('acquisition'):
        raise ValueError('acquisition differs from pre-producer occurrence')
    reconstruct(entry.get('acquisition'))
    authoritative(entry)
    prior=[]
    for row in rows:
        if row['op'] in {'search','fetch'}:
            marker='PRAXIS-OP '+row['nonce']+' '
            if row['status']=='failed' and row.get('refusal')=='pre-network':
                if (row.get('network_calls')!=0 or row.get('complete_source_read')
                        or row.get('returned') or row.get('cache') or row['sha256']!='0'*64
                        ):
                    raise ValueError('unauthenticated or effectful acquisition refusal')
                try:provenance=authorize(entry,prior,row)
                except ValueError:pass
                else:
                    if provenance['kind']!='discovery-url' or row.get('atomic_search_nonce'):
                        raise ValueError('fabricated unbound acquisition refusal')
                prior.append(row)
                continue
            provenance=authorize(entry,prior,row)
            if row.get('acquisition_provenance')!=provenance:
                raise ValueError('logged acquisition provenance differs')
            if row['op']=='fetch' and row['status']=='ok':
                if provenance['kind']=='discovery-url':
                    search=next((r for r in prior if r['nonce']==provenance['nonce']),None)
                    if search is None:
                        raise ValueError('discovery fetch lacks authenticated earlier search')
                    if row.get('atomic_search_nonce'):
                        if row['atomic_search_nonce']!=search['nonce'] or row['command']!=search['command']:
                            raise ValueError('discovery fetch is not an authenticated atomic operation')
                        import shlex
                        words=shlex.split(search['command'])
                        try:index=int(words[words.index('--fetch-index')+1])
                        except (ValueError,IndexError):raise ValueError('atomic discovery index missing')
                        urls=discovery_urls(search,row['dispatch_id'])
                        if not 0<=index<len(urls) or urls[index]!=row['source']:
                            raise ValueError('atomic discovery selection differs')
                from source_access import public_url
                redirects=row.get('redirects')
                if not isinstance(redirects,list):raise ValueError('redirect chain missing')
                for url in [row['source'],*redirects]:public_url(url)
                if row.get('final_url')!=(redirects[-1] if redirects else row['source']):raise ValueError('final URL/redirect chain differs')
                if provenance['kind']=='frozen-url' and row['sha256']!=provenance['sha256']:
                    raise ValueError('frozen URL original hash differs')
        prior.append(row)

SPEC_PREFIX='Acquisition binding sha256: '

def prepare_spec(spec,binding,sources=()):

    reconstruct(binding)
    if any(line.startswith(SPEC_PREFIX) for line in spec.splitlines()):
        raise ValueError('pre-existing acquisition authority line forbidden')
    return spec+'\n'+SPEC_PREFIX+digest(binding)+'\nAcquisition frozen sources sha256: '+digest(list(sources))

def read_task(identities):

    import subprocess
    from release_support import orca_cli
    for key,prefix in (('run_id','run_'),('task_id','task_'),('dispatch_id','ctx_')):
        if not isinstance(identities.get(key),str) or not identities[key].startswith(prefix):
            raise ValueError('authoritative Orca identity required')
    cli=orca_cli()['path']
    try:
        def read(arguments):
            result=subprocess.run([cli,'orchestration',*arguments,'--json'],capture_output=True,text=True,timeout=30,check=True)
            value=json.loads(result.stdout)
            if value.get('ok') is not True:raise ValueError('Orca read failed')
            return value['result']
        dispatch=read(['dispatch-show','--task',identities['task_id']])['dispatch']
        if any(dispatch.get(key if key!='dispatch_id' else 'id')!=identities[key] for key in ('run_id','task_id','dispatch_id')):
            raise ValueError('stale or cross-Dispatch authority')
        listing=read(['task-list','--run',dispatch['run_id']])
        if listing.get('runId')!=dispatch['run_id']:raise ValueError('Orca Task inventory Run differs')
        tasks=listing['tasks']
        matches=[task for task in tasks if task.get('id')==dispatch['task_id'] and task.get('run_id')==dispatch['run_id']]
        if len(matches)!=1:raise ValueError('Orca Task absent or ambiguous')
        task=matches[0]
        if not isinstance(task.get('spec'),str):raise ValueError('Orca Task spec unavailable')
        return task
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        raise ValueError('authoritative Orca Task spec unavailable: '+str(exc)) from exc

def authoritative(entry):
    binding=entry.get('acquisition');reconstruct(binding)
    task=read_task(entry.get('identities',{}))
    lines=[line for line in task['spec'].splitlines() if line.startswith(SPEC_PREFIX)]
    if lines!=[SPEC_PREFIX+digest(binding)]:
        raise ValueError('acquisition binding differs from dispatcher Task spec')
    frozen=[line for line in task['spec'].splitlines() if line.startswith('Acquisition frozen sources sha256: ')]
    if frozen!=['Acquisition frozen sources sha256: '+digest(entry.get('sources',[]))]:
        raise ValueError('frozen sources differ from dispatcher Task spec')
    return binding
