
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlparse, urlencode

sys.dont_write_bytecode=True

sys.path.insert(0,str(Path(__file__).resolve().parent))

USER_AGENT = 'Praxis/4.5 evidence-retrieval (+local research harness)'
DISCOVERY_BACKEND = 'https://en.wikipedia.org/w/api.php'
CROSSREF_BACKEND = 'https://api.crossref.org/works'
TRANSMISSION = ('Discovery query text, built only from the user question or claims, leaves the machine for the Wikipedia MediaWiki search API or Crossref REST API; each fetched URL receives a direct public HTTPS request. '
    'Helper pre-network enforcement does not physically prevent an out-of-protocol process from networking; later transcript refusal is not prevention. Acquisition accepts only pre-producer compiled user queries and exact bound-user/frozen URLs or helper-recorded discovery-result URLs from the same Dispatch, with fragments removed and every other byte unchanged. No arbitrary link-following. Never include vault or institutional material in discovery queries. No email, login, cookies, key, form submission or paywall bypass.')

class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        redirects=getattr(req,'praxis_redirects',[])
        redirects.append(newurl)
        target=super().redirect_request(req,fp,code,msg,headers,newurl)
        if target is not None:target.praxis_redirects=redirects
        return target

def public_url(url):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('public HTTPS URL required')
    import ipaddress
    host=parsed.hostname.lower()
    if host=='localhost' or host.endswith(('.local','.localhost')):raise ValueError('public HTTPS URL required')
    try:
        if not ipaddress.ip_address(host).is_global:raise ValueError('public HTTPS URL required')
    except ValueError as exc:
        if str(exc)=='public HTTPS URL required':raise
    return url

def urlopen(request, timeout):
    return build_opener(PublicRedirect()).open(request,timeout=timeout)

class HTMLText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
        if tag in {'p','div','br','li','h1','h2','h3'}:self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)

def extract(raw,path):
    if raw.startswith(b'%PDF'):
        executable=next((str(p) for p in (Path('/opt/homebrew/bin/pdftotext'),Path('/usr/local/bin/pdftotext'),Path('/usr/bin/pdftotext')) if p.is_file()),'pdftotext')
        result=subprocess.run([executable,str(path),'-'],capture_output=True,check=True)
        return result.stdout.decode('utf-8')
    text=raw.decode('utf-8')
    if re.search(r'<(?:!doctype html|html|body)\b',text,re.I):
        parser=HTMLText();parser.feed(text);return ''.join(parser.parts)
    return text

def locate(text, query, start=0):
                                                                   
    chars=[];offsets=[]
    for match in re.finditer(r'\s+|\S',text):
        part=' ' if match[0].isspace() else match[0].casefold()
        chars.extend(part);offsets.extend([match.start()]*len(part))
    needle=' '.join(query.split()).casefold()
    first=next((i for i,offset in enumerate(offsets) if offset>=start),len(offsets))
    found=''.join(chars).find(needle,first)
    return offsets[found] if found>=0 else -1

def discovery(raw, backend, *, full=False):
    data=json.loads(raw)
    items=data.get('query',{}).get('search',[]) if backend=='mediawiki' else data.get('message',{}).get('items',[])
    results=[]
    for item in (items if full else items[:10]):
        title=item.get('title','')
        if isinstance(title,list):title=' '.join(title) if full else (title[0] if title else '')
        snippet=item.get('snippet',item.get('abstract',''))
        parser=HTMLText();parser.feed(snippet)
        date=item.get('published',item.get('issued',{})).get('date-parts',[]) if backend=='crossref' else []
        results.append({'title':title,'url':('https://en.wikipedia.org/wiki/'+__import__('urllib.parse',fromlist=['quote']).quote(title.replace(' ','_')) if backend=='mediawiki' else item.get('URL','')),
            'doi':item.get('DOI',''),'year':date[0][0] if date and date[0] else 'unknown','source':backend,'snippet':''.join(parser.parts) if full else ''.join(parser.parts)[:300]})
    return results

def search_text(raw, backend):

    data=json.loads(raw)
    items=data.get('query',{}).get('search',[]) if backend=='mediawiki' else data.get('message',{}).get('items',[])
    parts=[]
    for item in items:
        title=item.get('title','')
        if isinstance(title,list):title=' '.join(title)
        parser=HTMLText();parser.feed(item.get('snippet',item.get('abstract','')))
        parts.extend([title,item.get('URL',item.get('DOI','')),''.join(parser.parts)])
    return '\n'.join(parts)

def main(argv=None, *, _fresh_discovery=None):
    from operation_evidence import read_log,digest,ZERO,derive
    argv=list(sys.argv[1:] if argv is None else argv)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['search','fetch','read','control','passage','find','hash','attest'])
    parser.add_argument('source',nargs='?')
    parser.add_argument('--source-id')
    parser.add_argument('--dispatch',required=True)
    parser.add_argument('--log',required=True,type=Path)
    parser.add_argument('--entry',required=True,type=Path)
    parser.add_argument('--nonce')
    parser.add_argument('--query')
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--stage',choices=['producer','checker'],default='producer')
    parser.add_argument('--claim-input',type=Path)
    parser.add_argument('--backend',choices=['mediawiki','crossref'],default='mediawiki')
    parser.add_argument('--fetch-index',type=int)
    parser.add_argument('--start',type=int,default=0);parser.add_argument('--length',type=int,default=600)
    args=parser.parse_args(argv)
    if args.fetch_index is not None and (args.action!='search' or not 0<=args.fetch_index<10):parser.error('fetch-index requires search and index 0 to 9')
    if not re.fullmatch(r'ctx_[\w-]{4,64}',args.dispatch):parser.error('valid Dispatch required')
    if args.action!='attest' and (not args.source or not re.fullmatch(r'[A-Za-z0-9_.-]+',args.source_id or '')):parser.error('source and whitespace-free source ID required')
    if not args.log.is_absolute() or args.log.is_symlink() or any(p.is_symlink() for p in args.log.parents):parser.error('absolute nonsymlink log required')

    try:
        if not args.entry.is_absolute() or args.entry.is_symlink() or any(p.is_symlink() for p in args.entry.parents):raise ValueError('absolute nonsymlink frozen entry required')
        frozen=json.loads(args.entry.read_bytes())
        from operation_evidence import log_path
        if frozen['identities']['dispatch_id']!=args.dispatch or log_path(frozen,args.dispatch)!=args.log or args.log.name!=args.dispatch+'.jsonl':raise ValueError('Dispatch/log differs from frozen entry')
    except (ValueError,OSError,KeyError) as exc:parser.error(str(exc))
    if args.action in {'passage','find'} and (args.start<0 or not 1<=args.length<=4000):parser.error('passage/find length must be 1 to 4000 characters')
    if args.action=='find' and not (args.query and args.query.strip()):parser.error('find requires nonempty --query')
    if args.action=='attest' and not re.fullmatch(r'[0-9a-f]{32}',args.nonce or ''):parser.error('attest requires valid --nonce')
    if args.action=='fetch':
        from acquisition import normalize_url
        args.source=normalize_url(args.source)
                                                                             
    if args.source and (args.action in {'read','control','hash'} or Path(args.source).is_absolute()):
        if not Path(args.source).is_absolute():parser.error('absolute source required')
        args.source=str(Path(args.source).resolve())
    control_bindings={str(Path(r['path']).resolve()):r['sha256'] for r in frozen.get('control_bindings',[])}
    if args.action=='control' and args.source not in control_bindings:parser.error('control must name a frozen non-research binding')
    if args.action in {'read','fetch'} and args.source in control_bindings:parser.error('control input requires control op; research budget is reserved for sources')
    args.log.parent.mkdir(parents=True,exist_ok=True)
                                                                    
    with args.log.open('a+b') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        rows,_=read_log(args.log,args.dispatch)
        if args.action=='attest':
            prior=next((r for r in rows if r['nonce']==args.nonce),None)
            if prior is None:parser.error('attest nonce absent from log')
                                                                               
            attestation=args.log.with_suffix('.attestations.jsonl')
            if attestation.is_symlink():parser.error('symlink attestation log')
            with attestation.open('a') as recovery:
                recovery.write(json.dumps({'nonce':args.nonce,'entry_sha256':prior['entry_sha256'],'attested_at':datetime.now(timezone.utc).isoformat()})+'\n')
            print(json.dumps({'marker':'PRAXIS-OP '+' '.join(str(prior[k]) for k in ('nonce','op','source_id','status','sha256'))}),flush=True)
            return 0
        if args.action in {'passage','find'} and sum(r['op'] in {'passage','find'} and r['status']=='ok' for r in rows)>=60:parser.error('passage/find Dispatch cap of 60 exceeded')
        if args.action=='control' and sum(r['op']=='control' and r['source']==args.source for r in rows)>=3:parser.error('control read cap of 3 exceeded')
        row={'schema_version':1,'seq':len(rows)+1,'dispatch_id':args.dispatch,'nonce':uuid.uuid4().hex,
             'op':args.action,'source_id':args.source_id,'source':args.source,'status':'failed','sha256':ZERO,
             'started_at':datetime.now(timezone.utc).isoformat(),'prev_sha256':rows[-1]['entry_sha256'] if rows else ZERO,
             'command':shlex.join([sys.executable,*sys.orig_argv[1:]]) if sys.argv[0]==str(Path(__file__).resolve()) and argv==sys.argv[1:] else shlex.join([sys.executable,str(Path(__file__).resolve()),*argv])}
        output={};bindings=[]
        if _fresh_discovery is not None:
            row['command']=_fresh_discovery['command']
            row['atomic_search_nonce']=_fresh_discovery['nonce']
        if args.action=='search':row['backend']=args.backend
        network_calls=0
        try:
            if args.stage=='checker':
                from native_capabilities import validate_claim_input
                if args.claim_input is None:raise ValueError('checker helper requires frozen claim input')
                bindings=validate_claim_input(args.claim_input)['sources']
            if args.action in {'search','fetch'}:
                from operation_evidence import check_request_budget
                from acquisition import authorize
                row['acquisition_provenance']=authorize(frozen,rows,row,pre_network=True,fresh_discovery=_fresh_discovery)
                row['request_text_budget']=check_request_budget(rows,row)
                if sum(r['op']=='search' for r in [*rows,row])>3:raise ValueError('query cap exceeded: 3')
                if sum(r['op'] in {'read','fetch'} for r in [*rows,row])>6:raise ValueError('retrieval attempt cap exceeded: 6')
                if args.cache and (not args.cache.is_absolute() or args.cache.is_symlink() or any(p.is_symlink() for p in args.cache.parents)):
                    raise ValueError('absolute nonsymlink retained cache required')
                destination=args.cache or args.log.parent/args.dispatch/'retained'/('query-'+row['nonce'] if args.action=='search' else args.source_id)
                if destination.exists():raise ValueError('acquisition cache already occupied')
                if args.action=='search':
                    url=(DISCOVERY_BACKEND+'?'+urlencode({'action':'query','list':'search','srsearch':args.source,'format':'json','srlimit':8})
                         if args.backend=='mediawiki' else CROSSREF_BACKEND+'?'+urlencode({'query':args.source,'rows':8}))
                else:url=public_url(args.source)
                request=Request(url,headers={'User-Agent':USER_AGENT});request.praxis_redirects=[]
                network_calls+=1
                with urlopen(request,timeout=30) as response:
                    final=public_url(response.geturl())
                    row['redirects']=request.praxis_redirects
                    if final!=(row['redirects'][-1] if row['redirects'] else url):row['redirects'].append(final)
                    row['final_url']=final
                    status=getattr(response,'status',200)
                    if not 200<=status<300:raise ValueError('public retrieval HTTP status '+str(status))
                    raw=response.read()
                    if not raw or not raw.strip():raise ValueError('public retrieval returned an empty body')
                    if args.action=='fetch':
                        row['complete_source_read']=True
                        provenance=row['acquisition_provenance']
                        if provenance['kind']=='frozen-url' and hashlib.sha256(raw).hexdigest()!=provenance['sha256']:raise ValueError('frozen URL original hash differs')
            elif args.action in {'read','control','hash'}:
                path=Path(args.source)
                if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):raise ValueError('absolute nonsymlink source required')
                row['source']=str(path.resolve())
                raw=path.read_bytes()
                if args.action=='control' and hashlib.sha256(raw).hexdigest()!=control_bindings[args.source]:raise ValueError('control binding hash differs')
                if args.action=='read':
                    bindings_before={r['path']:r['sha256'] for r in frozen.get('sources',[])}
                    if frozen.get('evidence_policy','bounded-public-web')=='bounded-public-web' and bindings_before.get(args.source)!=hashlib.sha256(raw).hexdigest():
                        raise ValueError('local read outside pre-producer hash bindings')
                    row['complete_source_read']=True
            else:
                prior=next((r for r in reversed(rows) if r['source_id']==args.source_id and r['source']==args.source and r['op'] in {'read','fetch','control','search'} and r['status']=='ok'),None)
                if prior is None:raise ValueError('passage/find requires bytes already read in this Dispatch')
                cached=Path(prior['cache']);text_path=Path(prior['text_path'])
                if cached.is_symlink() or text_path.is_symlink():raise ValueError('symlink retained bytes')
                if hashlib.sha256(cached.read_bytes()).hexdigest()!=prior['sha256']:raise ValueError('retained bytes drifted')
                text=text_path.read_bytes().decode('utf-8')
                if hashlib.sha256(text.encode()).hexdigest()!=prior['text_sha256']:raise ValueError('extracted text drifted')
                row['sha256']=prior['sha256'];row.update(cache=prior['cache'],text_path=prior['text_path'],text_sha256=prior['text_sha256'])
                start=locate(text,args.query,args.start) if args.query else args.start
                if args.action=='find':
                    positions=[]
                    while start>=0 and len(positions)<args.length:
                        positions.append(start);start=locate(text,args.query,start+1)
                    matches=[]
                    for index,start in enumerate(positions):
                        share=args.length//len(positions)+int(index<args.length%len(positions))
                        excerpt=text[start:start+share]
                        matches.append({'start':start,'length':len(excerpt),'text':excerpt})
                    output={'matches':matches};returned=matches
                else:
                    if start<0:raise ValueError('passage query not found')
                    excerpt=text[start:start+args.length]
                    output={'text':excerpt,'locator':{'start':start,'length':len(excerpt)}};returned=[dict(start=start,length=len(excerpt),text=excerpt)]
                row['returned']=[{'start':r['start'],'length':r['length'],'text_sha256':hashlib.sha256(r['text'].encode()).hexdigest()} for r in returned]
            if args.action in {'read','fetch'}:row['complete_source_read']=True
            if args.action not in {'passage','find'}:
                row['sha256']=hashlib.sha256(raw).hexdigest()
                if args.action!='hash':
                    cache=args.cache or args.log.parent/args.dispatch/'retained'/('query-'+row['nonce'] if args.action=='search' else 'control-'+args.source_id if args.action=='control' else args.source_id)
                    if cache is None or not cache.is_absolute() or cache.is_symlink() or any(p.is_symlink() for p in cache.parents):raise ValueError('absolute nonsymlink retained cache required')
                    cache.parent.mkdir(parents=True,exist_ok=True)
                    if cache.exists() and cache.read_bytes()!=raw:raise ValueError('cache bytes differ')
                    if not cache.exists():cache.write_bytes(raw)
                    row['cache']=str(cache)
                    text=search_text(raw,args.backend) if args.action=='search' else extract(raw,cache)
                    text_path=cache.with_name(cache.name+'.text')
                    if text_path.is_symlink():raise ValueError('symlink text cache')
                    text_path.write_text(text);row['text_path']=str(text_path);row['text_sha256']=hashlib.sha256(text.encode()).hexdigest()
                    head=text[:600]
                    row['returned']=[{'start':0,'length':len(head),'text_sha256':hashlib.sha256(head.encode()).hexdigest()}]
                    output={'text':head,'cache':str(cache),'source_id':args.source_id,'characters':len(text),'bytes':len(raw),'accessed_at':row['started_at']}
                    if args.action=='fetch':
                        match=re.search(r'<title[^>]*>(.*?)</title>',raw.decode('utf-8',errors='replace'),re.I|re.S)
                        parser=HTMLText()
                        if match:parser.feed(match[1])
                        row['title']=' '.join(''.join(parser.parts).split()) if match else ''
                        output['title']=row['title'];output['url']=args.source
                    if args.action=='search':
                        results=discovery(raw,args.backend);rendered=json.dumps(results,ensure_ascii=False)
                        row['discovery_text']=rendered;row['discovery_text_sha256']=hashlib.sha256(rendered.encode()).hexdigest()
                        row['discovery_returned']=[{'start':0,'length':len(rendered),'text_sha256':row['discovery_text_sha256']}]
                        row['returned']=[];output.pop('text');output['results']=results
            row['status']='ok'
        except Exception as exc:
            row['error']=str(exc)
            if args.action in {'search','fetch'} and network_calls==0:
                row['refusal']='pre-network';row['network_calls']=0
        finally:
            row['output_sha256']=hashlib.sha256(json.dumps(output,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            row['entry_sha256']=digest(row)
            handle.write((json.dumps(row,sort_keys=True,ensure_ascii=False)+'\n').encode());handle.flush();__import__('os').fsync(handle.fileno())
            marker='PRAXIS-OP '+' '.join(str(row[k]) for k in ('nonce','op','source_id','status','sha256'))
            accounting,_,_=derive(rows+[row],args.stage=='checker',bindings)
            print(json.dumps({'marker':marker,'entry':row,'operation_accounting':accounting,**output},sort_keys=True,ensure_ascii=False),flush=True)
    if row['status']=='ok' and args.fetch_index is not None:
        results=output['results']
        if args.fetch_index>=len(results):
            return main(['fetch','unavailable-discovery-index:'+str(args.fetch_index),'--source-id',args.source_id+'-fetch','--dispatch',args.dispatch,'--entry',str(args.entry),'--log',str(args.log)],_fresh_discovery=row)

        target=results[args.fetch_index]['url']
        fetch=['fetch',target,'--source-id',args.source_id+'-fetch','--dispatch',args.dispatch,
               '--entry',str(args.entry),'--log',str(args.log),'--stage',args.stage]
        if args.claim_input:fetch+=['--claim-input',str(args.claim_input)]
        return main(fetch,_fresh_discovery=row)
    return 0 if row['status']=='ok' else 4

if __name__=='__main__':sys.exit(main())
