
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from native_capabilities import EVIDENCE, require, load_json, validate_claim_input, source_aliases, canonical_receipt

OPS = {'search', 'fetch', 'read', 'control', 'passage', 'find', 'hash'}
ZERO = '0' * 64
MARKER = re.compile(r'PRAXIS-OP ([0-9a-f]{32}) (search|fetch|read|control|passage|find|hash) ([A-Za-z0-9_.-]+) (ok|failed) ([0-9a-f]{64})')
NONCE = re.compile(r'PRAXIS-OP ([0-9a-f]{32})\b')

def digest(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def log_path(unit, dispatch):
    template=unit.get('operation_log')
    if template is None:
        template=str(Path(unit['outputs'][0]['path']).parent/'operations'/'{dispatch_id}.jsonl')
    require(template.count('{dispatch_id}') == 1, 'fixed per-Dispatch operation log required')
    path=Path(template.replace('{dispatch_id}',dispatch))
    require(path.is_absolute(), 'absolute operation log required')
    return path

def read_log(path, dispatch):
    path=Path(path)
    require(path.is_file() and not path.is_symlink(), 'operation log missing or symlink')
    require(not any(p.is_symlink() for p in path.parents), 'symlink operation log ancestor')
    raw=path.read_bytes()
    require(not raw or raw.endswith(b'\n'), 'truncated operation log')
    rows=[];previous=ZERO;nonces=set()
    for line in raw.splitlines():
                                                                      
        def pairs(items):
            result={}
            for k,v in items:
                require(k not in result,'duplicate operation log key');result[k]=v
            return result
        row=json.loads(line,object_pairs_hook=pairs,parse_constant=lambda v: (_ for _ in ()).throw(ValueError('nonfinite operation log')))
        require(isinstance(row,dict),'operation log object required')
        require(row.get('schema_version')==1 and type(row.get('seq')) is int and row['seq']==len(rows)+1, 'operation log sequence differs')
        require(row.get('dispatch_id')==dispatch,'operation log Dispatch differs')
        require(row.get('op') in OPS and row.get('status') in {'ok','failed'},'operation log kind/status invalid')
        require(isinstance(row.get('source'),str) and row['source'], 'operation log source required')
        require(re.fullmatch(r'[A-Za-z0-9_.-]+',row.get('source_id','')) is not None,'operation log source ID invalid')
        require(re.fullmatch(r'[0-9a-f]{32}',row.get('nonce','')) is not None and row['nonce'] not in nonces,'operation log nonce invalid or duplicate')
        require(re.fullmatch(r'[0-9a-f]{64}',row.get('sha256','')) is not None,'operation log content hash invalid')
        require(row.get('prev_sha256')==previous,'operation log chain broken')
        claimed=row.get('entry_sha256');unsigned={k:v for k,v in row.items() if k!='entry_sha256'}
        require(claimed==digest(unsigned),'operation log entry hash differs')
        nonces.add(row['nonce']);previous=claimed;rows.append(row)
    return rows,hashlib.sha256(raw).hexdigest()

def derive(rows, checker=False, bindings=()):
    aliases=source_aliases(bindings)
    retained={r['path'] for r in bindings if r.get('retained_path')}
    reads={};attempts=0;acquisitions=0;rereads=0;queries=0
    for row in rows:
        op=row['op'];source=aliases.get(row['source'],row['source'])
        if op=='search':queries+=1
        if row.get('cache') and op in {'read','fetch'}:
            aliases[row['cache']]=source
        if op in {'read','fetch'}:
            attempts+=1
            reread=checker and op=='read' and source in retained
            acquisitions+=int(not reread)
            rereads+=int(reread)
            if row['status']=='ok' or row.get('complete_source_read') is True:reads[source]=reads.get(source,0)+1
    coverage={}
    for row in rows:
        if row['status']=='ok' and row.get('text_path'):
            source=aliases.get(row['source'],row['source'])
            search=next((r for r in rows if r['op']=='search' and r['status']=='ok' and r.get('cache')==row.get('cache') and r['source_id']==row['source_id']),None)
            if search:source='search:'+search['nonce']+':'+search['source_id']
            coverage.setdefault(source,[]).extend((r['start'],r['start']+r['length']) for r in row.get('returned',[]))
    returned_characters={source:sum(b-a for a,b in merged(spans)) for source,spans in coverage.items()}
    for row in rows:
        if row['status']=='ok' and row['op']=='search':
            key='search:'+row['nonce']+':'+row['source_id']
            returned_characters[key]=returned_characters.get(key,0)+len(row.get('discovery_text',''))
    key='checker_source_reads' if checker else 'source_reads'
    controls={}
    for row in rows:
        if row['op']=='control':controls[row['source']]=controls.get(row['source'],0)+1
    return {**({'control_reads':controls} if controls else {}),'stage':'checker' if checker else 'producer',key:reads,'complete_source_reads':sum(reads.values()),
            'retrieval_attempts':attempts,'web_discovery_queries':queries,'returned_characters':returned_characters},acquisitions,rereads

def normal_strings(value):

    if isinstance(value,dict):return '\n'.join(normal_strings(v) for v in value.values())
    if isinstance(value,list):return '\n'.join(normal_strings(v) for v in value)
    if not isinstance(value,str):return ''
    texts=[value]
                                                                       
    while True:
        prior=texts[-1]
        try:
            decoded=json.loads(prior)
            if isinstance(decoded,(dict,list)):texts.append(normal_strings(decoded))
            elif isinstance(decoded,str):texts.append(decoded)
            else:break
        except ValueError:
                                                                               
            current=re.sub(r'(?:\\(?:u[0-9a-fA-F]{4}|[ntrbf"/\\]))+',lambda m:json.loads('"'+m[0]+'"'),prior)
            if current==prior:break
            texts.append(current)
        if texts[-1]==prior:break
    return texts[-1]

def outputs(records, agent):
    for record in records:
        item=record.get('payload',{}) if agent=='codex' else record.get('message',{})
        if agent=='codex' and record.get('type')=='response_item' and item.get('type') in {'function_call_output','custom_tool_call_output'}:
            yield item.get('call_id'),normal_strings(item.get('output',''))
        elif agent=='claude' and record.get('type')=='user':
            for block in item.get('content',[]) if isinstance(item.get('content'),list) else []:
                if block.get('type')=='tool_result':yield block.get('tool_use_id'),normal_strings(block.get('content',''))

def helper_invocation(words,row,entry,unit):

    from p4_output_contract import PYTHON
    if not words or words[0]!=PYTHON:return False
    index=1
    while index<len(words) and words[index] in {'-I','-B'}:index+=1
    helper=str(Path(__file__).with_name('source_access.py').resolve())
    if index>=len(words) or words[index]!=helper:return False
    args=words[index+1:]
    if len(args)<2 or args[0] not in OPS:return False
    options={};allowed={'--source-id','--dispatch','--log','--entry','--query','--cache','--stage','--claim-input','--backend','--start','--length','--fetch-index'}
    if (len(args)-2)%2:return False
    for offset in range(2,len(args),2):
        key,value=args[offset:offset+2]
        if key not in allowed or key in options:return False
        options[key]=value
    if options.get('--dispatch')!=row['dispatch_id'] or options.get('--log')!=str(log_path(unit,row['dispatch_id'])):return False
    ep=Path(options.get('--entry',''))
    if not ep.is_absolute() or ep.is_symlink() or not ep.is_file():return False
    try:
        frozen=json.loads(ep.read_bytes())
        if frozen!=entry:return False
    except (ValueError,OSError):return False
    if row.get('atomic_search_nonce'):
        return args[0]=='search' and '--fetch-index' in options
    return args[0]==row['op'] and args[1]==row['source'] and options.get('--source-id')==row['source_id']

def helper_responses(records, agent, rows, entry, unit, bound=()):

    import shlex
    from operation_recount import calls,commands
    by_command={}
    for row in rows:
        if row.get('output_sha256'):by_command.setdefault(tuple(shlex.split(row['command'])),[]).append(row)
    identities={}
    for number,name,body,identity in calls(records,agent,include_id=True):
        record=records[number-1]
        if agent=='codex' and record.get('payload',{}).get('type') not in {'function_call','custom_tool_call'}:continue
        if agent=='claude' and record.get('message',{}).get('role','assistant')!='assistant':continue
        if name not in ({'Bash'} if agent=='claude' else {'exec','functions.exec','exec_command','functions.exec_command'}):continue
        if isinstance(body,dict):
            keys={'command','description','timeout','run_in_background'} if agent=='claude' else {'cmd','max_output_tokens','yield_time_ms','workdir','login','tty'}
            if set(body)-keys or body.get('tty') or body.get('run_in_background'):continue
        elif agent=='codex' and name in {'exec','functions.exec'}:
            match=re.fullmatch(r'\s*(?:(?:const|let)\s+(\w+)\s*=\s*await tools\.exec_command\((\{[\s\S]*\})\);\s*text\(\1\.output\);?|text\(await tools\.exec_command\((\{[\s\S]*\})\)\);?)\s*',body)
            if not match:continue
            try:args=json.loads(re.sub(r'(?<=[{,])\s*([a-z_]+)\s*:',r'"\1":',match[2] or match[3]))
            except (ValueError,TypeError):continue
            if set(args)-{'cmd','max_output_tokens','yield_time_ms','workdir','login','tty'} or args.get('tty'):continue
        else:continue
        cmds=commands(name,body)
        if len(cmds)!=1:continue
        try:
            words=tuple(shlex.split(cmds[0]));row=by_command.get(words)
        except ValueError:continue
        if row is None or not all(helper_invocation(words,r,entry,unit) for r in row):continue

        if cmds[0]!=shlex.join(words) and any(c in cmds[0] for c in (';','|','&','`','$','\n')):continue
        identities[identity]=row
    accepted=set()
    for record in records:
        item=record.get('payload',{}) if agent=='codex' else record.get('message',{})
        if agent=='codex':
            if record.get('type')!='response_item' or item.get('type') not in {'function_call_output','custom_tool_call_output'}:continue
            blocks=[(item.get('call_id'),item.get('output'))]
        else:
            if record.get('type')!='user':continue
            blocks=[(b.get('tool_use_id'),b.get('content')) for b in item.get('content',[]) if isinstance(b,dict) and b.get('type')=='tool_result'] if isinstance(item.get('content'),list) else []
        for identity,raw in blocks:
            row=identities.get(identity)
            if row is None:continue
            if agent=='codex' and isinstance(raw,list):
                if len(raw)!=2 or any(not isinstance(b,dict) or set(b)!={'type','text'} or b['type']!='input_text' for b in raw):continue
                if not re.fullmatch(r'Script completed\nWall time [0-9.]+ seconds\nOutput:\n',raw[0]['text']):continue
                try:wrapper=json.loads(raw[1]['text'])
                except (ValueError,TypeError):continue
                if not isinstance(wrapper,dict):continue
                required={'chunk_id','wall_time_seconds','exit_code','output'}
                if required<=set(wrapper):
                    if set(wrapper)-required-{'original_token_count','session_id'} or wrapper['exit_code'] not in {0,4}:continue
                    if 'original_token_count' in wrapper and (type(wrapper['original_token_count']) is not int or wrapper['original_token_count']<0):continue
                    raw=wrapper['output']
                else:

                    raw=raw[1]['text']
            if not isinstance(raw,str):continue
            def pairs(items):
                result={}
                for key,value in items:
                    if key in result:raise ValueError('duplicate helper envelope key')
                    result[key]=value
                return result
            try:envelopes=[json.loads(line,object_pairs_hook=pairs) for line in raw.splitlines()]
            except (ValueError,TypeError):continue

            nonces=[env.get('entry',{}).get('nonce') for env in envelopes if isinstance(env,dict)]
            group=[r for r in row if r['nonce'] in nonces]
            if len(envelopes)!=len(group):continue
            expected_parts=[]
            for envelope,row in zip(envelopes,group):
                if not isinstance(envelope,dict) or not {'marker','entry','operation_accounting'}<=set(envelope):break
                output={k:v for k,v in envelope.items() if k not in {'marker','entry','operation_accounting'}}
                marker='PRAXIS-OP '+' '.join(str(row[k]) for k in ('nonce','op','source_id','status','sha256'))
                if envelope['marker']!=marker or envelope['entry']!=row or digest(output)!=row['output_sha256']:break
                accounting=derive(rows[:row['seq']],unit.get('capability')!=EVIDENCE,bound)[0]
                if envelope['operation_accounting']!=accounting:break
                expected_parts.append(json.dumps({'marker':marker,'entry':row,'operation_accounting':accounting,**output},sort_keys=True,ensure_ascii=False)+'\n')
            if len(expected_parts)!=len(group) or raw!=''.join(expected_parts):continue
            accepted.add((identity,normal_strings(raw)))
            if agent=='codex':accepted.add((identity,normal_strings(item.get('output',''))))
    return accepted

def executable_text(body):

    def scan(index, expression=False):
        code=[];depth=0
        while index<len(body):
            char=body[index]
            if char in {"'", '"'}:
                quote=char;index+=1
                while index<len(body):
                    if body[index]=='\\':index+=2
                    elif body[index]==quote:index+=1;break
                    else:index+=1
                code.append(' ')
            elif body.startswith('//',index):
                end=body.find('\n',index+2);index=len(body) if end<0 else end
                code.append(' ')
            elif body.startswith('/*',index):
                end=body.find('*/',index+2);index=len(body) if end<0 else end+2
                code.append(' ')
            elif char=='`':
                index+=1;code.append(' ')
                while index<len(body):
                    if body[index]=='\\':index+=2
                    elif body[index]=='`':index+=1;break
                    elif body.startswith('${',index):
                        nested,index=scan(index+2,True);code.extend([' ',nested,' '])
                    else:index+=1
            elif expression and char=='}' and depth==0:
                return ''.join(code),index+1
            else:
                if expression and char=='{':depth+=1
                if expression and char=='}':depth-=1
                code.append(char);index+=1
        return ''.join(code),index
    return scan(0)[0]

def native_tool(name, body):
    names={name.lower().split('.')[-1]}

    if isinstance(body,str):
        code=executable_text(body)
        names.update(x.lower() for x in re.findall(r'\btools\.([A-Za-z_][\w]*)\s*\(',code))
    return bool(names & {'web__run','web_search_call','websearch','webfetch','web.run'}) or any(re.search(r'(?:web|browser|mcp).*?(?:fetch|search|navigate|open_url|browse)',tool) for tool in names) or name.lower()=='web.run'

def normalized_content(text):
    return ' '.join(normal_strings(text).split())

def source_automaton(text):

    edges=[{}];links=[-1];lengths=[0];last=0
    for char in text:
        current=len(edges);edges.append({});lengths.append(lengths[last]+1);links.append(0)
        previous=last
        while previous>=0 and char not in edges[previous]:
            edges[previous][char]=current;previous=links[previous]
        if previous>=0:
            target=edges[previous][char]
            if lengths[previous]+1==lengths[target]:links[current]=target
            else:
                clone=len(edges);edges.append(edges[target].copy());lengths.append(lengths[previous]+1);links.append(links[target])
                while previous>=0 and edges[previous].get(char)==target:
                    edges[previous][char]=clone;previous=links[previous]
                links[target]=links[current]=clone
        last=current
    return edges,links,lengths

def source_span(text, automaton):
    edges,links,lengths=automaton;state=0;size=0
    for char in text:
        while state and char not in edges[state]:state=links[state]
        if char in edges[state]:size=min(size,lengths[state])+1;state=edges[state][char]
        else:state=0;size=0
        if size>=1000:return True
    return False

def source_texts(bound, retained, rows, *, controls=True, texts=None):
    texts={} if texts is None else texts
    from source_access import extract
    sources={}
    for row in [*bound,*retained]:
        path=row.get('retained_path',row.get('path',row.get('source')))
        if path and Path(path).is_absolute():sources[path]=None
    for row in rows:
        if row['op'] in ({'read','fetch','control','search'} if controls else {'read','fetch','search'}) and row.get('cache'):sources[row['cache']]=row
    for path,row in sources.items():
        path=Path(path)
        require(path.is_file() and not path.is_symlink() and not any(p.is_symlink() for p in path.parents),'source content missing or symlink')
        raw=path.read_bytes()
        if row:
            require(hashlib.sha256(raw).hexdigest()==row['sha256'],'retained helper bytes drifted')
            text_path=Path(row['text_path'])
            require(text_path.is_file() and not text_path.is_symlink(),'extracted helper text missing or symlink')
            text=extracted_text(text_path,row['text_sha256'],texts)
        else:text=extract(raw,path)
        normalized=normalized_content(text)
        if len(normalized)>=200:yield SourceText(normalized,str(text_path) if row else str(path))
        if row and row['op']=='search' and row.get('discovery_text'):
            yield SourceText(normalized_content(row['discovery_text']),'discovery:'+row['nonce'])

def merged(intervals):
    result=[]
    for start,end in sorted(intervals):
        if result and start<=result[-1][1]:result[-1]=(result[-1][0],max(end,result[-1][1]))
        elif end>start:result.append((start,end))
    return result

def matching_spans(text, automaton, minimum=200):

    edges,links,lengths=automaton;state=0;size=0;previous=0
    for index,char in enumerate(text):
        while state and char not in edges[state]:state=links[state]
        if char in edges[state]:size=min(size,lengths[state])+1;state=edges[state][char]
        else:state=0;size=0
        if size!=previous+1 and previous>=minimum:yield index-previous,index
        previous=size
    if size>=minimum:yield len(text)-size,len(text)

def extracted_text(path, expected, texts):
    path=Path(path);key=str(path)
    if key not in texts:
        require(path.is_file() and not path.is_symlink() and not any(p.is_symlink() for p in path.parents),'extracted helper text missing or symlink')
        raw=path.read_bytes()
        texts[key]=(raw.decode('utf-8'),hashlib.sha256(raw).hexdigest())
    text,digest=texts[key]
    require(digest==expected,'extracted helper text drifted')
    return text

def returned_passages(rows, *, reflection=True, texts=None):

    texts={} if texts is None else texts
    result={};ranges={};prior=[];searches={}
    for row in rows:
        if row['op']=='search' and row['status']=='ok':
            searches.setdefault((row.get('cache'),row['source_id']),row)
    for row in rows:
        prior.append(row)
        if row['status']!='ok' or row['op'] not in {'read','fetch','control','search','passage','find'}:continue
        path=row['text_path'];text=extracted_text(path,row['text_sha256'],texts)
        if row['op']=='search':
            from source_access import discovery
            rendered=json.dumps(discovery(Path(row['cache']).read_bytes(),row.get('backend','mediawiki')),ensure_ascii=False)
            require(row.get('discovery_text')==rendered and row.get('discovery_text_sha256')==hashlib.sha256(rendered.encode()).hexdigest(),'discovery returned text differs')
            result.setdefault(('search',row['nonce'],row['source_id']),[]).append(nonrequest_passage(normalized_content(rendered),request_arguments(prior)) if reflection else normalized_content(rendered))
        returned=row.get('returned',[])
        limit=4000 if row['op'] in {'passage','find'} else 600
        require(isinstance(returned,list) and sum(r['length'] for r in returned)<=limit,'helper returned text exceeds '+str(limit)+' characters')
        require(bool(returned) or row['op'] in {'find','search'},'helper returned text metadata missing')
        for item in returned:
            start=item['start'];length=item['length']
            require(type(start) is int and type(length) is int and start>=0 and 0<=length<=limit and start+length<=len(text),'helper returned offset invalid')
            excerpt=text[start:start+length]
            require(hashlib.sha256(excerpt.encode()).hexdigest()==item['text_sha256'],'helper returned text hash differs')
            source=row['source'];search=searches.get((row.get('cache'),row['source_id']))
            if search:source=('search',search['nonce'],search['source_id'])
            arguments=tuple(sorted(set(request_arguments(prior)))) if reflection else ()
            ranges.setdefault((source,path,arguments),[]).append((start,start+length))
    for (source,path,arguments),intervals in ranges.items():
        text=texts[path][0]
        result.setdefault(source,[]).extend((nonrequest_passage(normalized_content(text[start:end]),arguments) if reflection else normalized_content(text[start:end])) for start,end in merged(intervals))
    return result

def request_parts(value, *, url=False):

    from urllib.parse import urlsplit
    if not url:return [value]
    parsed=urlsplit(value)
    parts=[parsed.netloc,*parsed.path.split('/')]
    for pair in parsed.query.split('&'):
        parts.extend(pair.split('=',1))
    parts.append(parsed.fragment)
    return [part for part in parts if part]

def request_forms(value):

    import base64
    from urllib.parse import unquote_plus
    from html import unescape
    from source_access import extract
    pending=[value];seen=set()
    while pending:
        text=pending.pop()
        if not text or text in seen:continue
        seen.add(text)
        decoded=normal_strings(extract(text.encode(),Path('request.txt')))
        for form in (__import__('unicodedata').normalize('NFKC',''.join(c for c in text if __import__('unicodedata').category(c)!='Cf')),decoded,unescape(text),unquote_plus(text),' '.join(text.split()),text.replace('_',' ').replace('-',' ')):
            if form and form not in seen:pending.append(form)

        if re.fullmatch(r'[A-Za-z0-9_+/=-]{8,}',text):
            try:
                form=base64.b64decode(text+'='*((-len(text))%4),altchars=b'-_',validate=True).decode('utf-8')
                if form not in seen:pending.append(form)
            except (ValueError,UnicodeError):pass
    return sorted({normalized_content(text) for text in seen if normalized_content(text)})

def request_arguments(rows):

    import shlex
    result=[];all_parts=[];seen=set()
    for row in rows:
        arguments=[row['source']]
        words=shlex.split(row.get('command',''))
        for option in ('--query','--cache'):
            if option in words and words.index(option)+1<len(words):arguments.append(words[words.index(option)+1])
        for argument in arguments:
            if argument in seen:continue
            seen.add(argument)
            result.extend(request_forms(argument))
            parts=request_parts(argument,url=argument.lower().startswith(('https://','http://')))

            for form in request_forms(argument):
                if form!=argument and form.lower().startswith(('https://','http://')):
                    extra=request_parts(form,url=True)
                    if extra!=parts:parts.extend(extra)
            groups=[request_forms(part) for part in parts]
            result.extend(form for group in groups for form in group)
            canonical=[max(group,key=len) for group in groups if group]
            all_parts.extend(canonical)
            for separator in ('',' '):result.extend(request_forms(separator.join(canonical)))
    for separator in ('',' '):result.extend(request_forms(separator.join(all_parts)))
    return result

REQUEST_VALUE_LIMIT=200
REQUEST_DISPATCH_LIMIT=600

def request_size(row):

    if row['op'] not in {'fetch','search'}:return []
    parts=request_parts(row['source'],url=row['op']=='fetch')
    def canonical(value):
        from urllib.parse import unquote_plus
        import unicodedata
        while True:
            next_value=normal_strings(unquote_plus(value))
            next_value=unicodedata.normalize('NFKC',''.join(c for c in next_value if unicodedata.category(c)!='Cf'))
            next_value=' '.join(next_value.split())
            if next_value==value:return next_value
            value=next_value
    return [max((len(canonical(form)) for form in request_forms(part)),default=0) for part in parts]

def check_request_budget(rows, row):
    sizes=request_size(row)
    require(all(size<REQUEST_VALUE_LIMIT for size in sizes),'public request text cap: individual value must be below 200 characters')
    total=sum(sum(request_size(prior)) for prior in [*rows,row])
    require(total<=REQUEST_DISPATCH_LIMIT,'public request text cap: Dispatch total exceeds 600 characters')
    return {'value_sizes':sizes,'dispatch_characters':total}

def exact_control_calls(records, agent, bindings, rows, entry=None, unit=None):

    import ast
    import shlex
    from immutable_code import authenticate
    from operation_recount import calls,commands,user_text,opening_dispatch,validate_records
    validate_records(records,agent)
    entry=entry or {};unit=unit or {};identities=entry.get('identities',{})
    for record in records:
        payload=record.get('payload',{})
        if payload.get('type') in {'function_call','custom_tool_call','local_shell_call'}:
            require(agent=='codex' and record.get('type')=='response_item','checker control read outside helper: record type outside exact allowlist')
        blocks=record.get('message',{}).get('content',[])
        if isinstance(blocks,list) and any(isinstance(b,dict) and b.get('type')=='tool_use' for b in blocks):
            require(agent=='claude' and record.get('type')=='assistant' and record.get('message',{}).get('role','assistant')=='assistant','checker control read outside helper: record type outside exact allowlist')
    logged={tuple(shlex.split(row.get('command',''))) for row in rows}
    scripts=set()
    for binding in entry.get('closure',[]):
        if binding['path'].endswith('/services/praxis45/p4_run.py'):
            authenticate(binding,entry['candidate_commit'],entry.get('code_identity'));scripts.add(binding['path'])
    bootstrap={r['path']:r['sha256'] for r in entry.get('checker_bootstrap',[])}
    terminals=set()
    for record in records:
        text=user_text(record,agent)
        if opening_dispatch(text)==identities.get('dispatch_id'):
            preamble=text.split('=== TASK ===',1)[0].split('BEGIN CANONICAL DOCUMENT',1)[0]
            terminals.update(re.findall(r'--from (term_[0-9a-f-]{36})',preamble))
            break
    terminal=next(iter(terminals)) if len(terminals)==1 else None
    def lifecycle(words):
        if terminal is None:return False
        if words==('orca','orchestration','check','--terminal',terminal,'--json'):return True
        prefix=('orca','orchestration','send','--from',terminal,'--type')
        if words[:6]!=prefix:return False
        kind=words[6] if len(words)>6 else None
        suffix=('--task-id',identities.get('task_id'),'--dispatch-id',identities.get('dispatch_id'))
        if kind=='heartbeat':
            return words[7:9]==('--subject','alive') and words[9:13]==suffix and words[13:14]==('--phase',) and len(words)==15 and words[14] in {'investigating','implementing','reviewing','waiting'}
        if kind=='escalation':
            return len(words)==15 and words[7]=='--subject' and bool(words[8]) and words[9]=='--body' and bool(words[10].strip()) and len(words[10])<=3500 and words[11:15]==suffix
        if kind=='worker_done':
                                                                                
            output=unit.get('outputs',[{}])[0].get('path')
            metadata=words[17:]
            allowed_metadata={()}
            if output:
                allowed_metadata.update({('--files-modified',output),('--report-path',output),('--files-modified',output,'--report-path',output)})
            return (len(words)>=17 and metadata in allowed_metadata and words[7]=='--subject' and bool(words[8]) and words[9]=='--body' and bool(words[10].strip()) and len(words[10])<=3500 and words[11:15]==suffix and words[15]=='--outcome' and words[16] in {'succeeded','failed'})
        return False
    def self_check(words):
        from p4_output_contract import PYTHON
        if not unit.get('outputs'):return False
        for script in scripts:
            base=(PYTHON,'-I',script,'validate-output','--capability',unit.get('capability'),'--file',unit['outputs'][0]['path'])
            tail=('--p4-config',next((path for path,sha in bindings.items() if sha==entry.get('config_sha256')),None),'--node',entry.get('node_id'))
            claim=('--claim-input',unit['claim_input']) if unit.get('claim_input') else ()
            if words in {base+claim+tail,base+tail+claim}:return True
        return False
    def literal_write(name,body):
        target=unit.get('outputs',[{}])[0].get('path')
        if agent=='claude' and name=='Write' and isinstance(body,dict) and set(body)=={'file_path','content'}:
            return body['file_path']==target and isinstance(body['content'],str)
        if agent=='codex' and name in {'apply_patch','functions.apply_patch'} and isinstance(body,str):
            lines=body.splitlines()
            return len(lines)>=4 and lines[:2]==['*** Begin Patch','*** Add File: '+str(target)] and lines[-1]=='*** End Patch' and all(line.startswith('+') for line in lines[2:-1])
        return False
    def rejected_or_attested_helper(words, identity):

        from p4_output_contract import PYTHON
        for row in rows:
            original=tuple(shlex.split(row['command']))
            try:index=next(i for i,w in enumerate(original) if w.endswith('/services/praxis45/source_access.py'))
            except StopIteration:continue
            if words[:index+1]!=original[:index+1] or original[0]!=PYTHON:continue
            binding=next((b for b in entry.get('closure',[]) if b['path']==original[index]),None)
            if binding:authenticate(binding,entry['candidate_commit'],entry.get('code_identity'))
            output='\n'.join(text for call,text in outputs(records,agent) if call==identity)
            if len(words)>index+1 and words[index+1]=='attest':

                tail=('--nonce',row['nonce'],'--dispatch',identities.get('dispatch_id'),'--log',row.get('command_log',str(log_path(unit,identities.get('dispatch_id')))),'--entry',original[original.index('--entry')+1]) if '--entry' in original else ()
                if words[index+2:]==tail:return True
            if 'PRAXIS-OP' in output or re.search(r'"status"\s*:\s*"success"',output):continue
            if len(words)>index+1 and words[index+1] in {'passage','find'} and '--length' in words:
                try:length=int(words[words.index('--length')+1])
                except (ValueError,IndexError):length=None
                bound=all(option in words and words[words.index(option)+1]==original[original.index(option)+1] for option in ('--dispatch','--log','--entry') if option in original)
                if bound and length is not None and not 1<=length<=4000 and 'passage/find length must be 1 to 4000 characters' in output:return True
            if '--source-id' in original:
                i=original.index('--source-id')
                missing=original[:i]+original[i+2:]
                if words==missing and 'source and whitespace-free source ID required' in output:return True
        return False
    def command(command, identity):
        lexer=shlex.shlex(command,posix=True,punctuation_chars=';&|()< >');lexer.whitespace_split=True;lexer.commenters=''
        try:words=tuple(lexer)
        except ValueError:return False
        if any(w and all(c in ';&|()<>' for c in w) for w in words):return False
        special=any('`' in w or '$' in w for w in words)
        if special and (words[:3]!=('python3','-I','-c') or command!=shlex.join(words)):return False
        if words in logged:
            script=next((word for word in words if word.endswith('/services/praxis45/source_access.py')),None)
            binding=next((row for row in entry.get('closure',[]) if row['path']==script),None)
            if binding:authenticate(binding,entry['candidate_commit'],entry.get('code_identity'))
            return all(helper_invocation(words,row,entry,unit) for row in rows if tuple(shlex.split(row.get('command','')))==words)
        if words and words[0]=='printf' and not special:

            return command==shlex.join(words)
        if special and words[:3]!=('python3','-I','-c'):return False
        if self_check(words) or lifecycle(words) or (words==('orca','orchestration','check','--terminal',terminal,'--json')) or rejected_or_attested_helper(words,identity):return True
        if len(words)==2 and words[0]=='cat' and words[1] in bootstrap:
            path=Path(words[1]);return path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest()==bootstrap[words[1]]

        if len(words)==4 and words[:3]==('python3','-I','-c'):
            try:tree=ast.parse(words[3])
            except SyntaxError:return False
            if len(tree.body)==2 and ast.dump(tree.body[0])==ast.dump(ast.parse('import subprocess').body[0]):
                call=tree.body[1].value if isinstance(tree.body[1],ast.Expr) else None
                if isinstance(call,ast.Call) and ast.dump(call.func)==ast.dump(ast.parse('subprocess.run',mode='eval').body) and len(call.args)==1:
                    try:argv=ast.literal_eval(call.args[0])
                    except (ValueError,TypeError):return False
                    keywords={k.arg:ast.literal_eval(k.value) for k in call.keywords if isinstance(k.value,ast.Constant)}
                    return len(call.keywords)==2 and keywords=={'shell':False,'check':True} and isinstance(argv,list) and all(isinstance(w,str) for w in argv) and lifecycle(tuple(argv)) and '--type' in argv and argv[argv.index('--type')+1] in {'worker_done','escalation'}
            if len(tree.body)!=2 or not isinstance(tree.body[0],ast.ImportFrom) or ast.dump(tree.body[0])!=ast.dump(ast.parse('from pathlib import Path').body[0]):return False
            call=tree.body[1].value if isinstance(tree.body[1],ast.Expr) else None
            return isinstance(call,ast.Call) and not call.keywords and len(call.args)==1 and isinstance(call.args[0],ast.Constant) and isinstance(call.args[0].value,str) and isinstance(call.func,ast.Attribute) and call.func.attr=='write_text' and ast.dump(call.func.value)==ast.dump(ast.parse('Path('+repr(unit['outputs'][0]['path'])+')',mode='eval').body)
        return False
    for number,name,body,identity in calls(records,agent,include_id=True):
        record=records[number-1]
        if agent=='codex':
            payload=record.get('payload',{})
            require(payload.get('type') in {'function_call','custom_tool_call'} and (payload.get('type')=='custom_tool_call')==(name in {'exec','functions.exec','apply_patch','functions.apply_patch'}),'checker control read outside helper: record type outside exact allowlist; call='+str(identity))
        if literal_write(name,body):continue
        shell_names={'Bash'} if agent=='claude' else {'exec_command','functions.exec_command'}
        cell_names={'exec','functions.exec'} if agent=='codex' else set()
        require(name in shell_names|cell_names,'checker control read outside helper: tool outside exact allowlist; call='+str(identity))
        if name in shell_names:
            keys={'command','description','timeout','run_in_background'} if agent=='claude' else {'cmd','max_output_tokens','yield_time_ms','workdir','login','tty'}
            primary='command' if agent=='claude' else 'cmd'
            require(isinstance(body,dict) and set(body)<=keys and isinstance(body.get(primary),str),'checker control read outside helper: keys outside exact allowlist; call='+str(identity))
            require(not body.get('run_in_background') and not body.get('tty'),'checker control read outside helper: asynchronous shell outside exact allowlist; call='+str(identity))
        items=commands(name,body)

        if name in {'exec','functions.exec'} and isinstance(body,str):
            cell=re.fullmatch(r'\s*(?:(?:const|let)\s+(\w+)\s*=\s*await tools\.exec_command\((\{[\s\S]*\})\);\s*text\(\1\.output\);?|(?:text\()?await tools\.exec_command\((\{[\s\S]*\})\)\)?;?)\s*',body)
            proved=False
            if cell:
                try:
                    obj=json.loads(re.sub(r'(?<=[{,])\s*([a-z_]+)\s*:',r'"\1":',cell.group(2) or cell.group(3)))
                    proved=set(obj)<={'cmd','max_output_tokens','yield_time_ms','workdir','login','tty'} and isinstance(obj.get('cmd'),str) and not obj.get('tty') and all(isinstance(value,(str,int,bool)) for value in obj.values())
                except (ValueError,TypeError):pass
            require(proved,'checker control read outside helper: unproved code cell; call='+str(identity))
        require(len(items)==1 and command(items[0],identity),'checker control read outside helper: non-helper call is not on exact allowlist; call='+str(identity))

def checker_control_calls(records, agent, bindings, rows, entry=None, unit=None):

    import shlex
    from operation_recount import calls,commands,literal_shell,source_breach,validate_records,user_text
    entry=entry or {};unit=unit or {}
    own=set(bindings)|{r['path'] for r in entry.get('checker_bootstrap',[])}
    for record in records:
        supplied=user_text(record,agent)
        own.update(re.findall(r'^BEGIN CANONICAL DOCUMENT (/.+)$',supplied,re.M))
    roots={str(Path(r['path']).resolve().parent) for r in unit.get('outputs',[])}
    caches={r[key] for r in rows for key in ('cache','text_path') if r.get(key)}
    sources={r['source'] for r in rows if r['op'] in {'read','fetch'}}
    diagnostics=validate_records(records,agent)
    def check_path(path):
        resolved=str(Path(path).resolve())
        bootstrap=Path(path).name in {'GLOBAL-RULES.md','CLAUDE.md','AGENTS.md','SKILL.md'}
        local=resolved in own|caches or any(resolved==root or resolved.startswith(root+'/') for root in roots)
        require(bootstrap or local or (resolved not in sources and '/Research/' not in resolved and '/institution/' not in resolved),
                'source read outside helper: '+path)
    visible=list(calls(records,agent,include_id=True))
                                                                                
    for number,record in enumerate(records,1):
        event=record.get('payload',{}).get('item',{})
        if event.get('type') in {'CommandExecution','command_execution'}:
            cmd=event.get('command');text=cmd[-1] if isinstance(cmd,list) and cmd else cmd
            if isinstance(text,str):visible.append((number,'host mirror',{'cmd':text},'record:'+str(number)))
    for _,name,body,identity in visible:
        require(not native_tool(name,body),'native web tool forbidden')
        if name in {'Read','read_file','functions.read_file'} and isinstance(body,dict):
            check_path(body.get('file_path',body.get('path','')));continue
        items=commands(name,body)
        if not items:
            diagnostics.append({'call_id':identity,'tool':name,'reason':'tool not classified'});continue
        for cmd in items:
            source_breach(cmd)
            try:words=shlex.split(literal_shell(cmd))
            except ValueError:
                diagnostics.append({'call_id':identity,'tool':name,'reason':'command not classified'});continue
            if any(Path(w).name=='source_access.py' for w in words):continue
            if words[:2]==['orca','orchestration']:continue
            if any(w.endswith('/p4_run.py') for w in words) and 'validate-output' in words:
                if '|' in cmd:diagnostics.append({'call_id':identity,'tool':name,'reason':'piped self-check invocation (advisory)'})
                continue
            if words and words[0] in {'cat','head','tail','sed','rg','grep'} and '>' not in words:
                for word in words[1:]:
                    if word.startswith('/') or word in sources:check_path(word)
                continue

            for match in re.finditer(r"(?:Path|open)\(\s*(['\"])(.*?)\1\s*\)(?:\.read_(?:text|bytes)\s*\(|\.read\s*\()",cmd):check_path(match[2])
            diagnostics.append({'call_id':identity,'tool':name,'reason':'control not classified'})
    return diagnostics

class OffsetCoverage(dict):

    pass

def normalized_offsets(text):

    token=re.compile(r'"(?:[^"\\]|\\.)*"')
    escape=re.compile(r'\\(?:u[0-9a-fA-F]{4}|[ntrbf"/\\])')
    def decode(chars,mapping):
        result=[];spans=[];i=0
        while i<len(chars):
            match=escape.match(chars,i)
            end=match.end() if match else i+1
            if match and chars[i:i+2]=='\\u':
                                                                    
                value=int(chars[i+2:i+6],16)
                if 0xd800<=value<=0xdbff and re.match(r'\\u[dD][c-fC-F][0-9a-fA-F]{2}',chars[end:]):end+=6
            value=json.loads('"'+chars[i:end]+'"') if match else chars[i:end]
            result.extend(value);spans.extend([(mapping[i][0],mapping[end-1][1])]*len(value));i=end
        return ''.join(result),spans
    def render(chars,mapping,depth=0):
        if depth>80:return chars,mapping
        try:parsed=json.loads(chars)
        except ValueError:parsed=None
        if isinstance(parsed,(dict,list,str)):
            decoder=json.JSONDecoder()
            def parse(index):
                while chars[index].isspace():index+=1
                first=chars[index]
                if first=='"':
                    match=token.match(chars,index);end=match.end()
                    value,positions=decode(chars[index+1:end-1],mapping[index+1:end-1])
                    value,positions=render(value,positions,depth+1)
                    return value,positions,end
                if first in '{[':
                    obj={} if first=='{' else [];index+=1
                    while True:
                        while chars[index].isspace():index+=1
                        if chars[index] in '}]':index+=1;break
                        if first=='{':
                            key,end=decoder.raw_decode(chars,index);index=end
                            while chars[index].isspace():index+=1
                            index+=1
                        value,positions,index=parse(index)
                        if isinstance(obj,dict):obj[key]=(value,positions)
                        else:obj.append((value,positions))
                        while chars[index].isspace():index+=1
                        if chars[index]==',':index+=1;continue
                        index+=1;break
                    rows=list(obj.values()) if isinstance(obj,dict) else obj
                    result=[];spans=[]
                    for i,(value,positions) in enumerate(rows):
                        if i:
                            anchor=positions[0][0] if positions else mapping[index-1][0]
                            result.append('\n');spans.append((anchor,anchor))
                        result.extend(value);spans.extend(positions)
                    return ''.join(result),spans,index
                _,end=decoder.raw_decode(chars,index)
                return '',[],end
            value,positions,_=parse(0)
            if value!=chars:return render(value,positions,depth+1)
                                                                             
        value,positions=decode(chars,mapping)
        return render(value,positions,depth+1) if value!=chars else (value,positions)
    chars,spans=render(text,[(i,i+1) for i in range(len(text))])
    require(chars==normal_strings(text),'source offset decoding differs from raw detector')
    output=[];mapping=[]
    for part in re.finditer(r'\S+',chars):
        if output:
            output.append(' ');mapping.append((spans[part.start()-1][0],spans[part.start()][0]))
        output.extend(part[0]);mapping.extend(spans[part.start():part.end()])
    return ''.join(output),mapping

def folded_offsets(text):

    result=[];mapping=[]
    for index,char in enumerate(text):
        folded=char.casefold();result.extend(folded);mapping.extend([index]*len(folded))
    return ''.join(result),mapping

def request_taint(text, arguments):

    alphabet={char for argument in arguments for char in argument.casefold()}
    return merged((i,i+1) for i,char in enumerate(text)
                  if any(part in alphabet for part in char.casefold()))

def returned_offsets(rows, *, texts=None, verified=False, request=()):

    texts={} if texts is None else texts
    if not verified:returned_passages(rows,reflection=False,texts=texts)
    coverage=OffsetCoverage();coverage.raw_coverage=OffsetCoverage();paths={};discoveries=[]
    arguments=set(request_arguments(rows)) | {form for value in request for form in request_forms(value)}
    for row in rows:
        if row['status']!='ok' or not row.get('text_path'):continue
        paths.setdefault(row['text_path'],[]).extend((r['start'],r['start']+r['length']) for r in row.get('returned',[]))
        if row['op']=='search' and row.get('discovery_text'):
            raw=row['discovery_text']
            require(row.get('discovery_returned',[{'start':0,'length':len(raw),'text_sha256':row['discovery_text_sha256']}])==[{'start':0,'length':len(raw),'text_sha256':row['discovery_text_sha256']}],'discovery delivered offsets differ')
            discoveries.append(('discovery:'+row['nonce'],raw))
    for path,intervals in paths.items():
        raw=texts[path][0];text,mapping=normalized_offsets(raw)
        windows=merged(intervals);denied=request_taint(text,arguments)
        eligible=[];wi=di=0;start=None;raw_eligible=[];raw_start=None
        for index,(a,b) in enumerate(mapping):
            while wi<len(windows) and windows[wi][1]<b:wi+=1
            while di<len(denied) and denied[di][1]<=index:di+=1
            exposed=wi<len(windows) and windows[wi][0]<=a and b<=windows[wi][1]
            if exposed and raw_start is None:raw_start=index
            elif not exposed and raw_start is not None:raw_eligible.append((raw_start,index));raw_start=None
            allowed=exposed and not (di<len(denied) and denied[di][0]<=index<denied[di][1])
            if allowed and start is None:start=index
            elif not allowed and start is not None:eligible.append((start,index));start=None
        if start is not None:eligible.append((start,len(mapping)))
        coverage[path]=eligible
        if raw_start is not None:raw_eligible.append((raw_start,len(mapping)))
        coverage.raw_coverage[path]=raw_eligible
    for identity,raw in discoveries:
        text=normalized_content(raw);denied=request_taint(text,arguments);start=0;eligible=[]
        for a,b in denied:
            if a>start:eligible.append((start,a))
            start=max(start,b)
        if start<len(text):eligible.append((start,len(text)))
        coverage[identity]=eligible
        coverage.raw_coverage[identity]=[(0,len(text))]
    return coverage

class SourceText(str):
    def __new__(cls,text,identity):
        value=super().__new__(cls,text);value.identity=identity;return value

class ExposedText(OffsetCoverage):

    def __init__(self,passages):
        super().__init__(passages if isinstance(passages,dict) else {})

def nonrequest_passage(text,arguments):
                                                                              
    return text

def score_output(text, sources, passages, arguments=()):

    detected=[];allowed=[];raw_allowed=[];urls=[]
    raw_passages=getattr(passages,'raw_coverage',passages)
    for source,automaton in sources:
        identity=getattr(source,'identity',None)
        spans=matching_spans(text,automaton)
        if spans:urls.append(identity)
        for start,end in spans:
            detected.append((start,end));span=text[start:end];offset=source.find(span)
            best=set();raw_best=set()
            while offset>=0:
                for coverage,target in ((passages.get(identity,[]),best),(raw_passages.get(identity,[]),raw_best)):
                    addressed=set()
                    for left,right in coverage:
                        a=max(offset,left);b=min(offset+len(span),right)
                        if b>a:addressed.update(range(start+a-offset,start+b-offset))
                    if len(addressed)>len(target):target.clear();target.update(addressed)
                offset=source.find(span,offset+1)
            allowed.extend(best);raw_allowed.extend(raw_best)
    detected=merged(detected)
    chars={i for a,b in detected for i in range(a,b)}
    denied=request_taint(text,arguments)
    tainted={i for a,b in denied for i in range(a,b)}
    eligible=set(allowed)-tainted
    discounted=set(raw_allowed)-eligible
    denied=merged((i,i+1) for i in discounted)
    raw_missing=chars-set(raw_allowed);missing=chars-eligible
    return {'total_detected':len(chars),'raw_uncovered':len(raw_missing),
            'final_uncovered':len(missing),'uncovered_spans':merged((i,i+1) for i in missing),
            'discounted_spans':denied,'discounted_characters':len(discounted),
            'credited_request_characters':len(eligible & discounted),
            'reason':'request-derived/reflected character match; includes all partial substrings',
            'sources':sorted({url for url in urls if url})}

def uncovered_count(text, sources, passages):
    return score_output(text,sources,passages)['final_uncovered']

def verify(value, entry, unit):
    from operation_recount import resolve,calls,validate_records
    dispatch=entry.get('identities',{}).get('dispatch_id');agent=entry.get('route',{}).get('agent',entry.get('route',{}).get('provider'))
    require(re.fullmatch(r'ctx_[\w-]{4,64}',dispatch or '') is not None,'valid Dispatch required')
    path=log_path(unit,dispatch);rows,log_sha=read_log(path,dispatch)
    started=entry.get('dispatch_started_at',entry.get('timestamp',entry.get('created_at')))
    require(started is not None,'Dispatch start required for transcript lookup')
    paths=resolve(dispatch,agent,start=started)
    raw=paths[0].read_bytes() if len(paths)==1 else b''
    records=[];record_uncertainty=[]
    if len(paths)!=1:record_uncertainty.append({'call_id':'transcript:lookup','reason':'transcript unavailable or ambiguous'})
    for number,line in enumerate(raw.splitlines(),1):
        if not line.strip():continue
        try:record=json.loads(line)
        except ValueError:
            record_uncertainty.append({'call_id':'record:'+str(number),'reason':'record not parseable'});continue
        if isinstance(record,dict):records.append(record)
        else:record_uncertainty.append({'call_id':'record:'+str(number),'reason':'record not classified'})
    validate_records(records,agent)
    require(not any(record.get('payload',{}).get('type')=='web_search_call' for record in records),'native web tool forbidden')
    bound=[]
    if unit.get('claim_input'):
        bound=validate_claim_input(unit['claim_input'],discovery=value['receipt_type']==EVIDENCE)['sources']
    for _,name,body,_ in calls(records,agent,include_id=True):
        require(not native_tool(name,body),'native web tool forbidden')
    seen=set();expected={row['nonce'] for row in rows}
    control_rows=entry.get('control_bindings',unit.get('control_bindings',[]))
    if 'control_bindings' in unit:require(control_rows==unit['control_bindings'],'frozen control bindings differ')
    control_bindings={r['path']:r['sha256'] for r in control_rows}
    control_uncertainty=record_uncertainty+checker_control_calls(records,agent,control_bindings,rows,entry,unit)
    require(not set(control_bindings)&{r['path'] for r in bound},'research source cannot be a control')
    control_reads={}
    for row in rows:
        if row['op']=='control':
            require(row['source'] in control_bindings,'control outside frozen non-research bindings')
            control_reads[row['source']]=control_reads.get(row['source'],0)+1
            require(control_reads[row['source']]<=3,'control read cap exceeded: 3')
            if row['status']=='ok':require(row['sha256']==control_bindings[row['source']],'control binding hash differs')
        if row['op'] in {'read','fetch'}:
            require(row['source'] not in control_bindings,'control input must use control op, not research read')
    require(not any(r['source'] in control_bindings for r in value.get('retained_sources',[])),'control cannot be retained research evidence')
    local_bindings={r['path']:r['sha256'] for r in [*entry.get('sources',[]),*bound]}
    prior_requests=[]
    for request in rows:
        if request['op'] in {'fetch','search'}:
            check_request_budget(prior_requests,request);prior_requests.append(request)
    public_mode=not unit.get('claim_input') or load_json(unit['claim_input']).get('evidence_policy')=='bounded-public-web'
    if 'evidence_policy' in entry:
        require(entry['evidence_policy']==('bounded-public-web' if public_mode else 'frozen-corpus'),'frozen evidence policy differs')
    for row in rows:
        if public_mode and row['op']=='read':
            require(row['source'] in local_bindings, 'local read outside pre-producer hash bindings')
            if row['status']=='ok':require(local_bindings[row['source']]==row['sha256'], 'local read outside pre-producer hash bindings')
    texts={}
    quotes=returned_passages(rows,reflection=False,texts=texts)
    authenticated_helpers=helper_responses(records,agent,rows,entry,unit,bound)
    from acquisition import verify as verify_acquisition
    verify_acquisition(entry,unit,rows,authenticated_helpers)
    user=unit.get('acquisition',{}).get('user',{})
    request=[user.get('request',''),*user.get('claims',[])]
    arguments=[*request_arguments(rows),*(form for value in request for form in request_forms(value))]
    allowance=returned_offsets(rows,texts=texts,verified=True,request=request)
    spec_sources=[]
    spec_binding=entry.get('frozen_spec');dispatcher_spec=entry.get('dispatcher_spec_sha256')
    if not spec_binding and entry.get('spec_sha256'):

        for row in rows:
            if row['op']=='control' and row['status']=='ok' and row['sha256']==entry.get('config_sha256'):
                frozen=load_json(row['cache'])
                spec_path=Path(frozen['state_root'])/entry['graph_run_id']/'raw'/entry['node_id']/'prepared-spec.txt'
                if spec_path.is_file():
                    bundle=load_json(spec_path.with_name('settled.json'))
                    records_for_node=[record for record in bundle.get('units',[]) if record.get('id')==entry['node_id']]
                    require(len(records_for_node)==1,'dispatcher Task spec record absent or ambiguous')
                    dispatcher_spec=records_for_node[0].get('prepared_spec_sha256')
                    spec_binding={'path':str(spec_path),'sha256':entry['spec_sha256']}
                    break
    if spec_binding:
        spec_path=Path(spec_binding['path'])
        require(spec_path.is_file() and not spec_path.is_symlink() and not any(p.is_symlink() for p in spec_path.parents),'frozen Task spec missing or symlink')
        spec_raw=spec_path.read_bytes()
        require(hashlib.sha256(spec_raw).hexdigest()==spec_binding['sha256']==entry.get('spec_sha256'),'frozen Task spec hash differs')
        if dispatcher_spec is not None:
            require(dispatcher_spec==spec_binding['sha256'],'dispatcher Task spec binding differs')
            from operation_recount import user_text
            spec_text=normalized_content(spec_raw.decode('utf-8'))
            if any(spec_text in normalized_content(user_text(record,agent)) for record in records):
                spec_source=SourceText(spec_text,'spec:'+spec_binding['sha256']);spec_sources.append(spec_source)
                allowance[spec_source.identity]=[(0,len(spec_source))]
    sources=[(text,source_automaton(text)) for text in [*source_texts(bound,value.get('retained_sources',[]),rows,texts=texts),*spec_sources]]
    scored=[]
    for identity,text in outputs(records,agent):
        nonces=NONCE.findall(text)
        for nonce in nonces:
            if nonce in expected:seen.add(nonce)
        normalized=normalized_content(text)
        if (identity,text) not in authenticated_helpers:
            scored.append(dict(score_output(normalized,sources,allowance,arguments),output_id=identity,answer_sha256=hashlib.sha256(text.encode()).hexdigest(),text=normalized))
    if seen!=expected:
        control_uncertainty.append({'call_id':'transcript:helper-output','reason':'helper output records not classified'})
    require(sum(row['op'] in {'passage','find'} and row['status']=='ok' for row in rows)<=60,'passage/find Dispatch cap exceeded')
    by_nonce={row['nonce']:row for row in rows}
    aliases=source_aliases(bound)
    canonical=canonical_receipt(value,bound)
    quoted=[]
    for item in canonical.get('retrievals',[]):
        if item['operational_status']!='ok':
            operation=by_nonce.get(item.get('operation_nonce'))
            require(operation is not None and operation['op'] in {'read','fetch'} and operation['status']=='failed' and aliases.get(operation['source'],operation['source'])==item['source'],
                    'failed retrieval requires logged failed attempt nonce/source')
            if 'passage' in item or 'snippet' in item:
                require(not item.get('passage') and not item.get('snippet'),'failed retrieval cannot carry a quote')
        else:quoted.append(item)
    quoted += [*canonical.get('retained_sources',[]),*(item for claim in canonical['claims'] for item in claim.get('evidence',[]))]
    for item in quoted:
            if item.get('basis')=='search_result_only':
                operation=by_nonce.get(item.get('operation_nonce'))
                require(operation is not None and operation['op']=='search' and operation['status']=='ok','search-only row requires logged successful search nonce')
                from source_access import discovery
                hits=discovery(Path(operation['cache']).read_bytes(),operation.get('backend','mediawiki'),full=True)
                matches=[hit for hit in hits if item['source'] in {hit['url'],hit['doi'],'https://doi.org/'+hit['doi'] if hit['doi'] else ''}]
                require(bool(matches),'search-only source absent from returned results')
                require(item['content_sha256']==operation['sha256'],'search-only hash differs from search response')
                for key in ('passage','snippet'):
                    if key in item:
                        require(any(normalized_content(item[key]) in normalized_content(hit[field]) for hit in matches for field in ('title','snippet'))
                                and any(normalized_content(item[key]) in excerpt for excerpt in quotes.get(('search',operation['nonce'],operation['source_id']),[])),
                                'search-only snippet not returned by search')
                continue
            require(isinstance(item.get('passage'),str) and bool(item['passage'].strip()),'read-source evidence requires a nonempty passage')
            if item.get('title'):
                title_rows=[r for r in rows if r['status']=='ok' and r['op']=='fetch' and r['source']==item['source']]
                bound_titles=[r for r in bound if r['path']==item['source'] and r.get('title')]
                if title_rows or bound_titles:require(any(r.get('title')==item['title'] for r in [*title_rows,*bound_titles]),'citation title differs from fetched helper metadata')
            require(any(r['status']=='ok' and r['op'] in {'read','fetch'} and aliases.get(r['source'],r['source'])==item['source'] and r['sha256']==item['content_sha256'] for r in rows), 'citation source/hash absent from helper log')
            quote=normalized_content(item['passage'])
                                                                    
            source=item['source'];returned=[excerpt for key,excerpts in quotes.items()
                if aliases.get(key,key)==source for excerpt in excerpts]
            require(any(quote in excerpt for excerpt in returned),'receipt quote not returned by helper')
    checker=value['receipt_type']!=EVIDENCE
    accounting,acquisitions,rereads=derive(rows,checker,bound)
    require(canonical.get('operation_accounting')==accounting,'self-report differs from log-derived accounting')
    from native_capabilities import validate_operations
    counts=validate_operations(value,claim_input=unit.get('claim_input'))
    require(counts['retrievals']==acquisitions,'acquisition accounting differs from log')
    if checker:require(rereads<=3*len(value['retained_sources']),'checker total reread limit exceeded')
                                                                              
    successful={(row['source'],row['sha256']) for row in rows if row['op'] in {'read','fetch'} and row['status']=='ok'}
    successful={(aliases.get(source,source),sha) for source,sha in successful}
    require(all((row['source'],row['content_sha256']) in successful for row in canonical['retrievals'] if row['operational_status']=='ok'), 'receipt source/hash absent from helper log')
    for row in rows:
        if row['status']=='ok' and row['op'] in {'read','fetch'}:
            cache=Path(row['cache'])
            require(cache.is_file() and not cache.is_symlink() and hashlib.sha256(cache.read_bytes()).hexdigest()==row['sha256'],'retained helper bytes drifted')
    retained=[{'path':row['source'],'sha256':row['sha256'],'retained_path':row['cache'],'title':row.get('title','')} for row in rows if row['status']=='ok' and row['op'] in {'read','fetch'}]
    answer_text=normalized_content(normal_strings({key:value[key] for key in
        ('narrative','claims','coverage_boundary','missingness') if key in value}))
    claim_reliance=False                                                           
    scored.append(dict(score_output(answer_text,sources,allowance,arguments),output_id='answer',text=answer_text,relied_on_claim=claim_reliance))
    aliases={r.get('text_path'):r['source'] for r in rows if r.get('text_path')}
    for score in scored:score['sources']=sorted({aliases.get(source,source) for source in score['sources']})
    from coverage_policy import classify
    coverage=classify(value,scored,request_id=user.get('identity') or dispatch,control_uncertainty=control_uncertainty)
    return {'coverage':coverage,'control_uncertainty':control_uncertainty,'retained_originals':retained,'status':'available','rule':'helper-operation-evidence-v23','dispatch_id':dispatch,'agent':agent,
            'operation_log':str(path),'operation_log_sha256':log_sha,'transcript_path':str(paths[0]) if len(paths)==1 else None,
            'transcript_sha256':hashlib.sha256(raw).hexdigest(),'source_reads':accounting.get('source_reads',accounting.get('checker_source_reads')),
            **{k:accounting[k] for k in ('complete_source_reads','retrieval_attempts','web_discovery_queries')},'acquisition_attempts':acquisitions,'rereads':rereads}
