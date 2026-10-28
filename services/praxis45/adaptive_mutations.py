
import copy
import fcntl
import json
import os
from pathlib import Path
import uuid

from adaptive_native import native_object, native_field
from adaptive_contract import digest
from adaptive_state import atomic_bytes, Conflict

METHODS = {v: 'orchestration.' + m for v, m in {
    'run-create':'runCreate','run-use':'runUse','task-create':'taskCreate',
    'task-update':'taskUpdate','worker-start':'workerStart','worker-release':'workerRelease',
    'worker-stop':'workerStop','check':'check','send':'send','reply':'reply'}.items()}
READS = {'request-show','dispatch-show','worker-show','worker-list','task-list','run-current','run-show'}

def flag(argv, name):
    return argv[argv.index(name)+1] if name in argv else None

def verb(argv):
    return argv[argv.index('orchestration')+1] if 'orchestration' in argv else None

def save(path, value):
    atomic_bytes(path, (json.dumps(value,sort_keys=True,allow_nan=False)+'\n').encode())

class MutationJournal:
    def __init__(self, root, *, scope, crash=None):
        self.root=Path(root).resolve()
        if not (self.root/'ADAPTIVE_FIXTURE_ROOT').is_file() or not scope:
            raise ValueError('marked isolated root and frozen mutation scope required')
        self.folder=self.root/'native-requests';self.folder.mkdir(exist_ok=True)
        self.scope=scope;self.crash=crash or (lambda stage:None)

    def transmit(self, argv, send, caller):
        argv=list(argv);op=verb(argv)
        if op in READS or op is None:return send(argv)
        if op not in METHODS:raise ValueError('unbound native mutation: '+op)
        if '--retry-request' in argv:raise ValueError('candidate never retries an ambiguous operation')

        with (self.folder/'.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            key=digest([self.scope,argv]);prior=sorted(self.folder.glob(key+'-*.json'))
            rows=[json.loads(p.read_bytes()) for p in prior]
            if any(r['state']!='completed' for r in rows) and not op=='check':
                raise Conflict('original mutation unresolved; read-only recovery required')
            if rows and op not in {'check','send','reply'}:
                raise Conflict('original mutation exists; no duplicate native creation')
            self.crash(op+':before_intent');self.crash('before_intent')
            rid=str(uuid.uuid4());sent=argv+['--retry-request',rid]
            row={'schema_version':1,'request_id':rid,'method':METHODS[op],
                 'scope':self.scope,'argv':argv,'argv_sha256':digest(argv),
                 'sent_argv':sent,'sent_argv_sha256':digest(sent),'caller_sha256':digest(caller),
                 'state':'intent','retry_authorized':False}
            path=self.folder/(key+'-'+str(len(rows)).zfill(6)+'.json');save(path,row)
            self.crash(op+':after_intent');self.crash('after_intent')
            try:result=send(sent)
            except Exception as exc:
                if op=='check':

                    row.update(state='unknown_observation',reason=type(exc).__name__+': '+str(exc))
                    save(path,row)
                raise
                                                                                    
            self.crash(op+':after_native_creation')
            self.crash('after_native_creation')
            row.update(state='completed',response=json.loads(result.stdout),response_sha256=digest(json.loads(result.stdout)))
            save(path,row);self.crash(op+':after_native_receipt');self.crash('after_native_receipt')
            return result

    def reconcile(self, record, client):

        row=copy.deepcopy(record)
        try:
            if row['scope']!=self.scope or digest(row['argv'])!=row['argv_sha256'] or digest(row['sent_argv'])!=row['sent_argv_sha256']:
                raise ValueError('durable operation input digest differs')
            if row['method']!=METHODS.get(verb(row['argv'])) or row['sent_argv']!=row['argv']+['--retry-request',row['request_id']]:
                raise ValueError('operation/request transmission binding differs')
            raw=client.call(['orchestration','request-show','--request',row['request_id']])
            shown=native_object(native_object(raw,'request-show').get('result',raw),'request-show result')
            if shown.get('requestId')!=row['request_id'] or shown.get('state')!='completed':
                return {'state':'unknown','request':shown,'retry_authorized':False,'native_launches':0}
            if shown.get('method')!=row['method']:raise ValueError('operation type differs')
            receipt=shown.get('receipt');data=native_object(receipt.get('result',receipt),'request receipt result') if isinstance(receipt,dict) else {}
            if not data or data.get('ok') is False:raise ValueError('completed successful native receipt missing')
            if row.get('response') and digest(receipt)!=row['response_sha256']:
                                                                                    
                original=row['response'].get('result',row['response'])
                if data!=original:raise ValueError('completed receipt differs from durable response')
            argv=row['argv'];op=verb(argv);rid=flag(argv,'--run');tid=flag(argv,'--task')
            if op=='task-create':
                tid=data.get('taskId') or native_field(data,'task').get('id')
                if not tid:raise ValueError('Task receipt lacks identity')
                tasks=client.call(['orchestration','task-list','--run',rid])
                tasks=tasks.get('result',tasks)
                matched=[t for t in tasks.get('tasks',[]) if t.get('id')==tid]
                if len(matched)!=1 or matched[0].get('spec')!=flag(argv,'--spec') or matched[0].get('run_id',matched[0].get('runId'))!=rid:
                    raise ValueError('original Task input/Run readback differs')
            elif op=='worker-start':
                did=data.get('dispatchId') or native_field(data,'dispatch').get('id')
                if not did:raise ValueError('Dispatch receipt lacks identity')
                exact=native_field(client.call(['orchestration','dispatch-show','--task',tid]),'dispatch')
                inventory=client.call(['orchestration','worker-list','--run',rid,'--include-remote'])
                if inventory.get('scope',{}).get('run')!=rid or inventory.get('page',{}).get('hasMore') is not False:
                    raise ValueError('incomplete original worker inventory')
                workers=[w for w in inventory.get('workers',[]) if w.get('taskId')==tid]
                if len(workers)!=1 or workers[0].get('dispatchId',workers[0].get('id'))!=did:
                    raise ValueError('original Task has multiple/unknown attempts')
                if (exact.get('id'),exact.get('run_id',exact.get('runId')),exact.get('task_id',exact.get('taskId'))) != (did,rid,tid):
                    raise ValueError('Task-to-Dispatch readback differs')
                from adaptive_native import readback_identity
                identity,original=readback_identity(client.worker_show(did))
                if identity!={'run_id':rid,'task_id':tid,'dispatch_id':did}:raise ValueError('original worker readback differs')
                options=native_object(original['worker'].get('startOptions',{}),'worker.startOptions')
                effective=native_object(native_object(options.get('launch',{}),'launch').get('effective',{}),'launch.effective')
                for key,operand in [('agent','--agent'),('model','--model'),('effort','--effort')]:
                    if flag(argv,operand) is not None and effective.get(key)!=flag(argv,operand):
                        raise ValueError('original worker launch input differs: '+key)
                if flag(argv,'--worktree') is not None and options.get('worktree')!=flag(argv,'--worktree'):
                    raise ValueError('original worker placement input differs')
                if flag(argv,'--timeout-ms') is not None and options.get('timeoutMs')!=int(flag(argv,'--timeout-ms')):
                    raise ValueError('original worker timeout input differs')
            else:
                                                                                 
                return {'state':'unknown','reason':'operation input readback not qualified: '+op,
                        'request':shown,'native_launches':0,'retry_authorized':False}
            return {'state':'reconciled','method':row['method'],'input_sha256':row['argv_sha256'],
                    'receipt':receipt,'task_id':tid,'run_id':rid,'dispatch_id':data.get('dispatchId'),
                    'native_launches':0,'retry_authorized':False}
        except (OSError,ValueError,KeyError,TypeError,AttributeError,RuntimeError) as exc:
            return {'state':'unknown','reason':str(exc),'native_launches':0,'retry_authorized':False}

class AccountingCommand:

    def __init__(self, command, root):self.command,self.root=command,Path(root)
    @property
    def env(self):return self.command.env
    def run(self, argv,*args,**kwargs):
        result=self.command.run(argv,*args,**kwargs)
        op=verb(argv);kind={'worker-start':'starts','worker-release':'releases'}.get(op)
        if op=='check':kind='acks' if '--ack' in argv else 'deliveries'
        if kind:
            row={'kind':kind,'argv':list(argv),'receipt':result.payload}
            with (self.root/'native-accounting.jsonl').open('a') as h:
                h.write(json.dumps(row,sort_keys=True)+'\n');h.flush();os.fsync(h.fileno())
        return result

def accounting(root):
    path=Path(root)/'native-accounting.jsonl'
    rows=[json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
    counts={k:sum(r['kind']==k for r in rows) for k in ('starts','releases','acks')}
    return dict(counts,receipts=rows,scope='confirmed CLI responses in this root; absent log is unknown',
                measured=path.exists())

class MutationCommand:

    def __init__(self, command, root, *, scope, crash=None):
        self.command=command;self.journal=MutationJournal(root,scope=scope,crash=crash)
    @property
    def env(self):return self.command.env
    def run(self,argv,*a,**kw):
        return self.journal.transmit(argv,lambda sent:self.command.run(sent,*a,**kw),
            {k:self.env.get(k) for k in ('ORCA_TERMINAL_HANDLE','ORCA_SESSION_ID')})
