
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

from native_capabilities import EVIDENCE, operation_counts, require

def opening_dispatch(text):
                                                                                
    if 'You are working inside Orca,' not in text:
        return None
    preamble = text.split('You are working inside Orca,', 1)[1].split('=== TASK ===', 1)[0].split('BEGIN CANONICAL DOCUMENT', 1)[0]
    matches = set(re.findall(r'--dispatch-id\s+(ctx_[\w-]+)', preamble))
    return next(iter(matches)) if len(matches) == 1 else None

def user_text(record, agent):
    item = record.get('message', {}) if agent == 'claude' else record.get('payload', {})
    if agent == 'claude':
        if record.get('type') != 'user' or item.get('role', 'user') != 'user':return ''
    elif item.get('role') != 'user' or item.get('type') not in {'message', 'user_message'}:
        return ''
    content = item.get('content', item.get('text', ''))
    if isinstance(content, str):return content
    return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict)) if isinstance(content, list) else ''

def resolve(dispatch, agent, root=None, start=None):
    require(agent in {'codex','claude'} and re.fullmatch(r'ctx_[\w-]{4,64}',dispatch or ''), 'valid provider and Dispatch required')
    if root is None:
        root = os.environ.get('PRAXIS_CODEX_SESSIONS' if agent=='codex' else 'PRAXIS_CLAUDE_PROJECTS') or Path.home()/('.codex/sessions' if agent == 'codex' else '.claude/projects')
    if isinstance(start,str):
        from datetime import datetime
        start=datetime.fromisoformat(start.replace("Z","+00:00")).timestamp()
    matches = []
    for path in sorted(Path(root).rglob('*.jsonl')):
        if path.is_symlink() or start is not None and path.stat().st_mtime < start:continue

        try:
            needle=dispatch.encode();found_bytes=False
            with path.open('rb') as handle:
                tail=b''
                while chunk:=handle.read(1<<20):
                    if needle in tail+chunk:found_bytes=True;break
                    tail=chunk[-len(needle):]
            if not found_bytes:continue
            with path.open() as handle:
                for line in handle:
                    record=json.loads(line);text=user_text(record,agent)
                    found=opening_dispatch(text)
                    if found:
                        if found==dispatch:matches.append(path)
                        break
        except (OSError,ValueError,TypeError):continue
    return matches

def command_projections(records):

    pending=None;result={}
    for number,record in enumerate(records,1):
        item=record.get('payload',{})
        if record.get('type')=='response_item' and item.get('type') in {'function_call','custom_tool_call'}:
            pending=(number,item)
        elif record.get('type')=='event_msg' and item.get('type')=='item_completed' and item.get('item',{}).get('type')=='CommandExecution':
            event=item['item'];cmd=event.get('command')
            if not pending or not isinstance(cmd,list) or len(cmd)!=3 or cmd[1] not in {'-lc','-c'}:continue
            _,call=pending;name=call.get('name');body=call.get('input',call.get('arguments',{}))
            if call['type']=='function_call' and isinstance(body,str):
                try:body=json.loads(body)
                except ValueError:continue
            literal=commands(name,body)

            shell=(name in {'exec_command','functions.exec_command'} or name in {'exec','functions.exec'} and isinstance(body,str) and len(re.findall(r'\btools\.exec_command\s*\(',body))==1)
            if shell and (not literal or literal==[cmd[2]]) and call.get('call_id') not in result:
                result[call['call_id']]=cmd[2]
    return result

def calls(records, agent, include_id=False):
    seen=set()
    projections=command_projections(records) if agent=='codex' else {}
    for number,record in enumerate(records,1):
        if agent=='claude':
            if record.get('type')!='assistant':continue
            message=record.get('message',{})
            for item in message.get('content',[]):
                if item.get('type')=='tool_use' and item.get('id') not in seen:
                    seen.add(item.get('id'));row=(number,item['name'],item.get('input',{}))
                    yield (*row,item.get('id')) if include_id else row
        else:
            item=record.get('payload',{})
            if record.get('type')!='response_item' or item.get('type') not in {'function_call','custom_tool_call','local_shell_call'}:continue
            identity=item.get('call_id',str(number))
            if identity in seen:continue
            seen.add(identity);body=item.get('input',item.get('arguments',{}))
            if item['type']=='function_call' and isinstance(body,str):
                try:body=json.loads(body)
                except ValueError:pass
            name=item.get('name','')
            if identity in projections:
                name='functions.exec_command';body={'cmd':projections[identity]}
            row=(number,name,body)
            yield (*row,identity) if include_id else row

def validate_records(records, agent):

    from operation_evidence import native_tool
    diagnostics=[]
    known={'codex':{'session_meta','turn_context','event_msg','response_item','world_state','token_usage_record','compacted'},
           'claude':{'user','assistant','system','summary','progress','attachment','queue-operation','last-prompt','mode','permission-mode','atis-latch','ai-title','cost-state','file-history-snapshot','file-history-delta'}}
    projections=command_projections(records) if agent=='codex' else {}
    for number,record in enumerate(records,1):
        if not isinstance(record,dict):
            diagnostics.append({'call_id':'record:'+str(number),'reason':'record not classified'});continue
        item=record.get('payload',{})
        if record.get('type') not in known[agent]:
            diagnostics.append({'call_id':'record:'+str(number),'reason':'record type not classified'})
        if not isinstance(item,dict):continue
        kind=item.get('type','')
        if kind in {'web_search_call','mcp_tool_call','mcp_tool_use','server_tool_use','tool_search_call'}:
            require(not native_tool(item.get('name',kind),item.get('arguments',{})), 'native web tool forbidden')
            diagnostics.append({'call_id':'record:'+str(number),'reason':'provider execution not classified'})
        event=item.get('item',{})
        if isinstance(event,dict) and event.get('type') in {'CommandExecution','command_execution'}:
            cmd=event.get('command')
            text=cmd[-1] if isinstance(cmd,list) and cmd and isinstance(cmd[-1],str) else cmd if isinstance(cmd,str) else ''
            source_breach(text)
            if text not in projections.values():
                diagnostics.append({'call_id':'record:'+str(number),'reason':'host mirror not classified'})
    for _,name,body,identity in calls(records,agent,include_id=True):
        require(not native_tool(name,body),'native web tool forbidden')
        for cmd in commands(name,body):source_breach(cmd)
    return diagnostics

def source_breach(cmd):

    cmd=literal_shell(cmd)
    try:
        lexer=shlex.shlex(cmd,posix=True,punctuation_chars=True);lexer.whitespace_split=True
        words=list(lexer)
    except ValueError:return
    for index,word in enumerate(words):
        if Path(word).name in {'curl','wget','nc','ncat'} and (index==0 or words[index-1] in {';','&&','||','|','&'}):
            require(False,'network outside source helper')
    scripts=[m[1] for m in re.findall(r"python(?:3(?:\.\d+)?)?[^\n]*<<\s*'?([A-Za-z_][A-Za-z_0-9]*)'?\n(.*?)\n\1",cmd,re.S)]
    for index,word in enumerate(words):
        if word=='-c' and index and Path(words[0]).name.startswith('python') and index+1<len(words):scripts.append(words[index+1])
    for script in scripts:
        try:tree=ast.parse(script)
        except SyntaxError:continue
        imports={alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names}
        imports.update((node.module or '').split('.')[0] for node in ast.walk(tree) if isinstance(node,ast.ImportFrom))
        require(not imports & {'urllib','requests','httpx','aiohttp','socket','http'},'network outside source helper')
        require(not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr in {'urlopen','connect','geturl'} for node in ast.walk(tree)),
                'network outside source helper')

def commands(name, body):
    if isinstance(body,dict):
        cmd=body.get('cmd',body.get('command'))
        return [cmd] if isinstance(cmd,str) else []
    if name in {'exec','functions.exec'} and isinstance(body,str):
                                                                        
        return [json.loads(m.group(1)) for m in re.finditer(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*")(?=\s*[,}])',body)]
    return []

class PythonReads:

    def __init__(self, sources):self.sources=set(sources);self.reads=[];self.unknown=[];self.env={};self.counted=set()
    def value(self,node,env):
        if isinstance(node,ast.Constant):return node.value
        if isinstance(node,ast.Name):return env.get(node.id)
        if isinstance(node,(ast.List,ast.Tuple)):return [self.value(n,env) for n in node.elts]
        if isinstance(node,ast.Dict):return {self.value(k,env):self.value(v,env) for k,v in zip(node.keys,node.values)}
        if isinstance(node,ast.Subscript):
            try:return self.value(node.value,env)[self.value(node.slice,env)]
            except (TypeError,KeyError,IndexError):return None
        if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Div,ast.Add)):
            a,b=self.value(node.left,env),self.value(node.right,env)
            try:return str(Path(a)/b) if isinstance(node.op,ast.Div) else a+b
            except TypeError:return None
        if isinstance(node,ast.Attribute) and node.attr=='suffix':
            v=self.value(node.value,env);return Path(v).suffix if isinstance(v,str) else None
        if isinstance(node,ast.Call):
            name=ast.unparse(node.func);args=[self.value(n,env) for n in node.args]
            if name in {'Path','str'}:return str(args[0]) if args and args[0] is not None else None
            if isinstance(node.func,ast.Attribute) and node.func.attr in {'read_bytes','read_text'}:
                path=self.value(node.func.value,env)

                if isinstance(path,str) and path not in self.sources:
                    try:return Path(path).read_text() if Path(path).suffix=='.json' else None
                    except (OSError,UnicodeError):return None
            if name=='json.loads' and args and isinstance(args[0],str):
                try:return json.loads(args[0])
                except ValueError:return None
            if name=='open':return args[0] if args else None
            if name=='json.load' and args and isinstance(args[0],str):
                try:return json.loads(Path(args[0]).read_bytes())
                except (OSError,ValueError):return None
        return None
    def scan(self,node,env):
        for call in ast.walk(node):
            if not isinstance(call,ast.Call):continue
            path=None
            if isinstance(call.func,ast.Attribute) and call.func.attr in {'read_bytes','read_text'}:
                path=self.value(call.func.value,env)
            elif ast.unparse(call.func)=='open' and call.args:
                path=self.value(call.args[0],env)
                mode=self.value(call.args[1],env) if len(call.args)>1 else 'r'
                if isinstance(mode,str) and any(c in mode for c in 'wa+'):continue

            if path in self.sources:self.reads.append(path);self.counted.add(path)
            elif path is None and isinstance(call.func,ast.Attribute) and call.func.attr in {'read_bytes','read_text'}:
                self.unknown.append('unresolved Python read: '+ast.unparse(call))
    def block(self,nodes,env):
        for node in nodes:
            if isinstance(node,(ast.FunctionDef,ast.ClassDef,ast.Import,ast.ImportFrom)):continue
            if isinstance(node,ast.For):
                values=self.value(node.iter,env)
                if not isinstance(values,(list,tuple)):
                    if any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in {'read_bytes','read_text'} for n in ast.walk(node)):
                        self.unknown.append('unresolved source loop')
                    continue
                for value in values:
                    local=dict(env)
                    if isinstance(node.target,ast.Name):local[node.target.id]=value
                    self.block(node.body,local)
            elif isinstance(node,ast.If):

                if any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in {'read_bytes','read_text'} for n in ast.walk(node)):
                    self.unknown.append('conditional read requires source_access.py')
            elif isinstance(node,ast.With):
                self.scan(node,env)
            else:
                self.scan(node,env)
                if isinstance(node,ast.Assign):
                    for target in node.targets:
                        if isinstance(target,ast.Name):env[target.id]=self.value(node.value,env)
    def run(self,script):
        self.block(ast.parse(script).body,self.env);return self.reads,self.unknown

def helper_call(cmd):
    try:
        tokens=shlex.split(cmd)
        for i,token in enumerate(tokens):
            if Path(token).name=='source_access.py' and i+2<len(tokens) and tokens[i+1] in {'read','query'}:
                return tokens[i+1],tokens[i+2]
    except ValueError:pass
    return None,None

def helper_protocol_error(cmd):

    if 'source_access.py' not in cmd:return None
    try:
        lexer=shlex.shlex(cmd, posix=True, punctuation_chars=';&|()<>')
        lexer.whitespace_split=True;lexer.commenters=''
        tokens=list(lexer)
    except ValueError:return 'unparseable source helper invocation'
    if any(t and all(c in ';&|()<>' for c in t) for t in tokens):
        return 'combined/chained/piped/backgrounded source helper invocation'
    if len(tokens)<6 or Path(tokens[0]).name not in {'python','python3','python3.11','python3.12','python3.13'}:
        return 'looped or unsupported source helper invocation'
    if Path(tokens[1]).name!='source_access.py' or tokens[2] not in {'read','query'} or tokens[4]!='--cache' or len(tokens)!=6:
        return 'one literal source helper invocation required'
    if any('$' in t or '`' in t for t in tokens):return 'dynamic source helper invocation'
    return None

def literal_shell(cmd):

    def replace(match):
        header=match.group(1)

        if re.search(r'\$\(|[<>]\(|`',header):return match.group(0)
        if not match.group('quote') and re.search(r'\$\(|`',match.group('body')):return match.group(0)
        return header
    return re.sub(r"(\bcat\s*>[^\n]*<<\s*(?P<quote>['\"]?)(?P<tag>[A-Za-z_][A-Za-z_0-9]*)(?P=quote)[^\n]*\n)(?P<body>.*?)\n(?P=tag)(?:\n|$)",replace,cmd,flags=re.S)

def count_command(cmd,sources,cached=()):
    reads=[];attempts=[];queries=0;unknown=[]
    cmd=literal_shell(cmd)

    action,source=helper_call(cmd)
    protocol=helper_protocol_error(cmd)
    if protocol:return [],[],0,[protocol]
    if action=='query':return [],[],1,[]
    if action=='read':return [source],[source],0,[]
    if any(s in cmd for s in sources) and re.search(r'\$\(|[<>]\(|`',cmd):
        return [],[],0,['source shell substitution requires exact helper accounting']
    scripts=re.findall(r"python(?:3)?[^\n]*<<\s*'?([A-Za-z_][A-Za-z_0-9]*)'?\n(.*?)\n\1",cmd,re.S)
    if scripts:
        for _,script in scripts:
            parser=PythonReads(sources)
            try:r,u=parser.run(script);reads+=r;unknown+=u
            except (ValueError,SyntaxError):unknown.append('unparseable Python source access')
        attempts=list(reads)
    else:

        try:
            tokens=shlex.split(cmd)
            if tokens and tokens[0] in {'rg','grep','sed','head','tail'} and any(t in cached for t in tokens) and not any(t in sources and t not in cached for t in tokens):
                return [],[],0,[]                                     
            if tokens and tokens[0] in {'cat','sha256sum','shasum'}:
                reads=[t for t in tokens[1:] if t in sources];attempts=list(reads)
            elif any(s in cmd for s in sources):
                                                                                        
                if not re.match(r'^stat\b',cmd):unknown.append('unrecognized source command')
        except ValueError:

            if any(s in cmd for s in sources) or re.search(r'\b(?:cat|read|open|curl|wget|python\w*)\b(?!\s*>)',cmd):
                unknown.append('unparseable source shell command')
    return reads,attempts,queries,unknown

def recount(dispatch,agent,sources,root=None,aliases=None,cached=()):
    aliases=aliases or {}
    sources=set(sources)|set(aliases)
    paths=resolve(dispatch,agent,root)
    if len(paths)!=1:return {'status':'unavailable','reason':'session_not_found' if not paths else 'ambiguous_session','matches':[str(p) for p in paths]}
    path=paths[0];raw=path.read_bytes()
    try:records=[json.loads(line) for line in raw.splitlines() if line.strip()]
    except ValueError:return {'status':'unavailable','reason':'malformed_transcript'}
    reads={};attempts=0;acquisitions=0;queries=0;unknown=[];operations=[]

    results = {};failed_results=set();executions={}
    for record in records:
        item = record.get('payload',{}) if agent=='codex' else record.get('message',{})
        if agent=='codex' and item.get('type') in {'function_call_output','custom_tool_call_output'}:
            results[item.get('call_id')] = str(item.get('output',''))
        elif agent=='claude' and record.get('type')=='user':
            for block in item.get('content',[]) if isinstance(item.get('content'),list) else []:
                if block.get('type')=='tool_result':
                    results[block.get('tool_use_id')] = str(block.get('content',''))
                    if block.get('is_error'):failed_results.add(block.get('tool_use_id'))
        execution=record.get('payload',{}).get('item',{})
        if execution.get('type')=='CommandExecution' and execution.get('status')=='completed':
            command=execution.get('command',[])
            if command:executions[command[-1]]=execution.get('exit_code')
    for line,name,body,call_id in calls(records,agent,include_id=True):
        cmdlist=[literal_shell(cmd) for cmd in commands(name,body)]
        if isinstance(body,str) and 'exec_command' in body and not cmdlist and ('source_access.py' in body or any(s in body for s in sources)):
            unknown.append('opaque nested source command')
        if len(cmdlist)>1 and any('source_access.py' in cmd for cmd in cmdlist):
            unknown.append('multiple helper completions in one tool call are ambiguous')
        for cmd in cmdlist:
            r,a,q,u=count_command(cmd,sources,cached)
            if helper_call(cmd)[0]=='read':
                output=results.get(call_id,'')

                normalized=output.replace('\\"','"').replace("'",'"')
                if re.search(r'"status"\s*:\s*"retrieval_failed"',normalized):
                    if not re.search(r'"complete_source_read"\s*:\s*true',normalized):r=[]
                elif not re.search(r'"status"\s*:\s*"ok"',normalized):u.append('source helper completion unavailable')
            elif r:
                output=results.get(call_id,'')
                if cmd in executions:
                    if executions[cmd]!=0:u.append('failed raw source command requires exact completion accounting')
                elif not output or call_id in failed_results:
                    u.append('raw source completion unavailable')

            if helper_call(cmd)[1] in cached and helper_call(cmd)[1] not in sources:
                r=[];a=[]
            attempts+=len(a);acquisitions+=sum(source not in aliases for source in a);queries+=q;unknown+=u
            for source in r:
                source=aliases.get(source,source);reads[source]=reads.get(source,0)+1
            if r or a or q or u:operations.append({'line':line,'tool':name,'reads':r,'attempts':len(a),'queries':q,'unavailable':u})
        if isinstance(body,dict) and name.split('.')[-1].lower() in {'read','read_file'}:
            source=body.get('file_path',body.get('path'))
            if source in sources:
                if source in cached and (body.get('offset') or body.get('limit')):continue
                attempts+=1
                acquisitions+=int(source not in aliases)
                if body.get('offset') or body.get('limit'):
                    unknown.append('partial original read is not cached locator inspection')
                else:
                    source=aliases.get(source,source);reads[source]=reads.get(source,0)+1
        web_calls=[]
        if name.split('.')[-1].lower()=='websearch' and isinstance(body,dict):
            queries+=1
        if name.split('.')[-1].lower()=='webfetch' and isinstance(body,dict):
            source=body.get('url')
            if source in sources:
                reads[source]=reads.get(source,0)+1;attempts+=1;acquisitions+=1
            else:unknown.append('unresolved native web fetch')
        if 'web' in name.lower() and isinstance(body,dict):web_calls.append(body)
        if isinstance(body,str) and re.search(r'tools\.web\w*\(',body):
                                                                               
            for match in re.finditer(r'tools\.web\w*\(\s*',body):
                try:
                    value,_=json.JSONDecoder().raw_decode(body[match.end():]);web_calls.append(value)
                except ValueError:unknown.append('opaque nested web call')
        for web in web_calls:
            if not isinstance(web,dict):unknown.append('malformed web tool input');continue
            queries+=len(web.get('search_query',[]))
            for row in web.get('open',[]):
                source=row.get('ref_id')
                if source in sources:
                    source=aliases.get(source,source);reads[source]=reads.get(source,0)+1;attempts+=1;acquisitions+=1
                else:unknown.append('unresolved web open reference')

    return {'status':'unavailable' if unknown else 'available','reason':'; '.join(sorted(set(unknown))) if unknown else None,
            'dispatch_id':dispatch,'agent':agent,'transcript_path':str(path),'transcript_sha256':hashlib.sha256(raw).hexdigest(),
            'source_reads':reads,'complete_source_reads':sum(reads.values()),'retrieval_attempts':attempts,
            'acquisition_attempts':acquisitions,'web_discovery_queries':queries,'operations':operations,'rule':'operation-recount-v11'}

def compare(value,result):
    require(result.get('status')=='available','independent operation recount unavailable: '+str(result.get('reason')))
    operation_counts(value)
    a=value['operation_accounting'];key='source_reads' if value['receipt_type']==EVIDENCE else 'checker_source_reads'
    for source,count in result['source_reads'].items():
        require(a[key].get(source,0)>=count,'independent recount under-report: '+source)
    for key in ['complete_source_reads','retrieval_attempts','web_discovery_queries']:
        require(a[key]>=result[key],'independent recount under-report: '+key)
    require(operation_counts(value)['retrievals']>=result.get('acquisition_attempts',0), 'independent recount under-report: acquisition_attempts')
    return result

def verify(value,entry,unit):
                                                                               
    from operation_evidence import verify as verify_log
    return verify_log(value,entry,unit)
