
import fcntl
import json
from pathlib import Path
import shlex
import time

from adaptive_contract import digest
from adaptive_support import save_once, REPO

def run_identity(receipt):
    run=receipt.get('result',receipt).get('run')
    return run.get('id') if run else None

class PlacedCommand:

    def __init__(self,command,worktree,*,selector=None):
        self.command,self.worktree=command,Path(worktree).resolve()
        self.selector=selector or 'path:'+str(self.worktree)
    def run(self,argv,*args,**kwargs):
        argv=list(argv)
        if '--from' in argv or '--terminal' in argv:
            raise ValueError('qualification uses only its own implicit process attestation')
        if 'orchestration' in argv and argv[argv.index('orchestration')+1]=='worker-start':
            idx=argv.index('--worktree');argv[idx+1]=self.selector
        return self.command.run(argv,*args,**kwargs)

class AttestedCommand:

    def __init__(self,command,root,handle):
        self.command,self.root,self.handle=command,Path(root),handle
        self.verified_run=None;self.held=False
    @property
    def env(self):return self.command.env
    def run(self,argv,*args,**kwargs):
        from adaptive_mutations import METHODS,verb,flag
        op=verb(argv)
        if op in METHODS:
            if self.held or op=='run-use' or (op!='run-create' and (
                    self.verified_run is None or flag(argv,'--run') not in (None,self.verified_run))):
                raise ValueError('qualifier attestation required for this Run mutation')
            if op=='run-create':
                                                                             
                self.verified_run=None
                result=self.command.run(argv,*args,**kwargs)
                data=result.payload.get('result',result.payload)
                rid=data.get('runId') or run_identity(data)
                proof={'state':'held','kind':'qualifier_attestation_unverified',
                       'run_id':rid,'expected_handle':self.handle,'creation':data,
                       'native_tasks':0,'native_workers':0,'retry_authorized':False}
                try:
                    if not rid:raise ValueError('created Run identity missing')
                    prefix=list(argv[:argv.index('orchestration')])
                    shown=self.command.run(prefix+['orchestration','run-show','--id',rid,'--json'],*args,**kwargs).payload
                    run=shown.get('result',shown).get('run') or {}
                    proof.update(readback=shown,coordinator_handle=run.get('coordinator_handle'))
                    if run.get('id')!=rid or run.get('coordinator_handle')!=self.handle:
                        proof['kind']='qualifier_attestation_mismatch'
                        raise ValueError('created Run coordinator differs from creation receipt')
                except (OSError,RuntimeError,ValueError,KeyError,TypeError) as exc:
                    self.held=True;proof['reason']=str(exc)
                    save_once(self.root/('qualification-attestation-'+str(rid)+'.json'),proof)
                    raise ValueError('qualifier attestation held: '+str(exc)) from exc
                proof.update(state='verified',kind='qualifier_run_attestation')
                save_once(self.root/('qualification-attestation-'+rid+'.json'),proof)
                self.verified_run=rid
                return result
        return self.command.run(argv,*args,**kwargs)

def launch(dispatcher,client,root,argv,*,worker_limit=4,caller_workspace=False):
    if type(worker_limit) is not int or worker_limit<1:
        raise ValueError('positive actual worker limit required')
    root=Path(root)
    with (root/'.qualification-launch.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        intent=root/'qualification-coordinator-intent.json'
        if intent.exists():raise ValueError('qualification already launched or creation uncertain; reconcile, never retry')
        caller=client.call(['orchestration','run-current'])
        placement={'worktree':str(REPO)}
        selector='path:'+str(REPO)
        if caller_workspace:
            shown=client.call(['worktree','show','--worktree','current'])
            workspace=shown.get('result',shown).get('worktree',{})
            if not workspace.get('id') or not workspace.get('path'):
                raise ValueError('caller workspace id/path unavailable; no coordinator launch')
            selector='id:'+workspace['id']
            placement={'worktree':str(Path(workspace['path']).resolve()),
                       'worktree_id':workspace['id'],'worktree_selector':selector}
        save_once(intent,dict(placement,caller_binding=caller,root=str(root),
                             argv=argv,argv_sha256=digest(argv)))
        try:
            receipt=client.call(['terminal','create','--worktree',selector,
                '--title','Adaptive P5 qualification '+digest(str(root))[:12],
                '--command',shlex.join(argv)])
                                                                                
            data=receipt.get('result',receipt) if isinstance(receipt,dict) else {}
            terminal=data.get('terminal',data) if isinstance(data,dict) else {}
            handle=terminal.get('handle') if isinstance(terminal,dict) else None
            save_once(root/'qualification-coordinator.json',dict(placement,creation=receipt,
                handle=handle,argv=argv,argv_sha256=digest(argv),caller_binding=caller))
        finally:

            readback={'state':'verified'}
            try:
                after=client.call(['orchestration','run-current'])
                save_once(root/'qualification-caller-after.json',after)
                if run_identity(caller)!=run_identity(after):
                    raise ValueError('caller Run binding changed; inspect custody')
            except (OSError,RuntimeError) as exc:
                readback={'state':'unverified','reason':type(exc).__name__+': '+str(exc)}
                save_once(root/'qualification-caller-readback-gap.json',readback)
        result_path=root/'qualification-result.json'
        child=json.loads(result_path.read_bytes()) if result_path.exists() else None
        return {'status':child['status'] if child else 'launched','root':str(root),'result_path':str(result_path),
                'progress_path':str(root/'qualification-progress.json'),'worker_limit_total':worker_limit,
                'creation_receipt':str(root/'qualification-coordinator.json'),'caller_readback':readback,
                'result_authority':'child durable result; launcher readback is auxiliary'}

def in_terminal_client(dispatcher,client,root,*,wait_seconds=30,mutation_scope=None,mutation_crash=None,caller_workspace=False):

    root=Path(root);path=root/'qualification-coordinator.json'
    end=time.monotonic()+wait_seconds
    while not path.exists():
        if time.monotonic()>=end:raise ValueError('creation receipt unavailable; no Run allowed')
        time.sleep(.02)
    saved=json.loads(path.read_bytes())
    own=client.status().get('caller')

    handle=saved['handle']
    if own is not None and (not isinstance(own,dict) or not own.get('live')
                            or own.get('orcaSessionId')!=handle):
        raise ValueError('qualifier process is not attested as the created terminal')
    if not handle:raise ValueError('qualifier creation handle missing')
    shown=client.call(['terminal','show','--terminal',handle])
    terminal=shown.get('result',shown).get('terminal',{})
    if terminal.get('handle')!=handle:
        raise ValueError('qualifier own terminal readback differs')
    worktree=REPO;selector=None
    if caller_workspace:
        intent=json.loads((root/'qualification-coordinator-intent.json').read_bytes())
        if (not saved.get('worktree_id') or terminal.get('worktreeId')!=saved['worktree_id']
                or saved.get('worktree_selector')!='id:'+saved['worktree_id']
                or any(intent.get(k)!=saved.get(k) for k in
                       ('worktree','worktree_id','worktree_selector'))):
            raise ValueError('qualifier caller workspace binding differs')
        worktree=Path(saved['worktree']).resolve();selector=saved['worktree_selector']
    if not terminal.get('worktreePath') or Path(terminal['worktreePath']).resolve()!=worktree:
        raise ValueError('qualifier actual terminal placement differs from candidate')
    if saved['worktree']!=str(worktree) or saved['argv_sha256']!=digest(saved['argv']):
        raise ValueError('qualifier creation binding differs')
    entry=root/'qualification-process.json'
    if entry.exists():raise ValueError('qualification process already entered; read-only replay only')
                                                                                  
    proof={'state':'held','kind':'qualifier_preflight_attestation_unverified',
           'expected_handle':handle,'native_runs':0,'native_tasks':0,'native_workers':0}
    try:

        current=client.call(['orchestration','run-current','--from',handle])
        proof['readback']=current
        if run_identity(current) is not None:
            raise ValueError('fresh qualifier already bound to a Run')
    except (OSError,RuntimeError,ValueError) as exc:
        proof['reason']=str(exc)
        save_once(root/'qualification-preflight-attestation.json',proof)
        raise ValueError('qualifier preflight attestation held: '+str(exc)) from exc
    proof.update(state='verified',kind='qualifier_readonly_self_assertion')
    save_once(root/'qualification-preflight-attestation.json',proof)
    save_once(entry,{'handle':saved['handle'],'argv_sha256':saved['argv_sha256'],
                     'state':'pending_run_attestation','status_caller':own})
    from adaptive_mutations import MutationCommand
    command=MutationCommand(client.command,root,scope=mutation_scope or 'qualification/'+digest(str(root)),crash=mutation_crash)
    return dispatcher.OrcaClient(client.prefix,PlacedCommand(AttestedCommand(command,root,handle),worktree,selector=selector))
