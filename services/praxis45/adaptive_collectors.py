
from __future__ import annotations

import copy
import json

from adaptive_contract import digest
from adaptive_state import atomic_bytes
from adaptive_supervision import Watch, POLICY

SUPPORT = {
    "ordinary": {"supported": False, "reason": "existing overall collector not ported"},
    "frozen": {"supported": False, "reason": "immutable collector; successor launch wiring unqualified"},
    "integrated": {"supported": False, "reason": "existing native adapter/selector not wired"},
    "adaptive-original-readonly-v1": {"supported": True, "scope": "original readback and recorded progress only"},
}

def select(selector, policy):
    if policy != POLICY or not SUPPORT.get(selector, {}).get("supported"):
        raise ValueError("selected collector refuses adaptive policy: " + str(selector))
    return DispatchSupervisor if selector in SUCCESSORS else OriginalCollector

class OriginalCollector:
    def __init__(self, bridge, policy, *, started_ms, deadline_ms=None):
        self.bridge = bridge
        row = bridge.journal.get(bridge.occurrence, bridge.node)
        if not row or not row.get("native"):
            raise ValueError("collector requires original native binding")
        self.path = bridge.folder / "supervision.json"
        self.watch = Watch(row["native"], policy, started_ms=started_ms, deadline_ms=deadline_ms)
        if self.path.exists():
            saved = json.loads(self.path.read_bytes()); expected = saved.pop("sha256")
            if digest(saved) != expected or saved["native"] != row["native"] or saved["policy"] != policy:
                raise ValueError("supervision identity/policy/integrity drift")
            if saved["started"] != started_ms or saved["deadline"] != deadline_ms:
                raise ValueError("restart cannot renew original start/deadline")
            for key in ("started", "activity", "progress", "last_clock", "deadline", "bound", "queue_started", "sequence"):
                setattr(self.watch, key, saved[key])
            self.watch.seen = set(saved["seen"]); self.watch.milestones = set(saved["milestones"])
        self.save()

    def save(self):
        w = self.watch
        value = {key: copy.deepcopy(getattr(w, key)) for key in (
            "native", "policy", "started", "activity", "progress", "last_clock", "deadline", "bound", "queue_started")}
        value.update(sequence=getattr(w, "sequence", 0), seen=sorted(w.seen), milestones=sorted(w.milestones))
        value["sha256"] = digest(value)
        atomic_bytes(self.path, (json.dumps(value, sort_keys=True) + "\n").encode())

    def observe(self, event, *, now_ms, validate_evidence):
        accepted = self.watch.observe(event, now_ms=now_ms, validate_evidence=validate_evidence)
        self.save()                                              
        return accepted

    def queue(self, *, now_ms):
        self.watch.queue(now_ms=now_ms); self.save()

    def collect(self, read_original, *, now_ms):
        supervision = self.watch.tick(now_ms=now_ms); self.save()
        original = self.bridge.recover(read_original)
        if original["state"] in {"succeeded", "failed"}:
            return dict(original, supervision=supervision)
        return dict(supervision, original=original, native_launches=0)

def monotonic_origin():

    from pathlib import Path
    import sys
    if sys.platform.startswith('linux'):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if sys.platform=='darwin':
        import ctypes
        lib=ctypes.CDLL(None);buf=ctypes.create_string_buffer(256);size=ctypes.c_size_t(256)
        if lib.sysctlbyname(b'kern.bootsessionuuid',buf,ctypes.byref(size),None,0)==0:
            return buf.value.decode()
    raise RuntimeError('kernel monotonic origin unavailable; supervision cannot certify restart clocks')

class DispatchSupervisor:

    def __init__(self,root,policy,*,clock=None,wall_clock=None,deadline_ms=None,origin=None,telemetry_only=False):
        import time
        from pathlib import Path
        from adaptive_contract import validate_bounds
        self.root=Path(root).resolve()
        if not (self.root/'ADAPTIVE_FIXTURE_ROOT').is_file():
            raise ValueError('isolated successor supervision root required')
        validate_bounds(policy)
        import math
        if deadline_ms is not None and (not math.isfinite(deadline_ms) or deadline_ms<=0):
            raise ValueError('explicit deadline must be a positive finite epoch; no zero sentinel')
        self.telemetry_only=telemetry_only
        self.policy,self.deadline=copy.deepcopy(policy),deadline_ms
        self.clock=clock or (lambda:int(time.monotonic()*1000))
        self.wall_clock=wall_clock or (lambda:int(time.time()*1000))
        self.starts,self.start_epochs,self.collectors={},{},{}
        self.failures,self.degraded={},{}
        self.origin_error=None
        try:self.origin=origin or monotonic_origin()
        except (OSError,ValueError,RuntimeError) as exc:self.origin=None;self.origin_error=exc
        self.epochs={}
        self.streams={}

    def _now(self):
        import math
        value=self.clock()
        if not math.isfinite(value):raise ValueError('monotonic clock not finite')
        return value

    def before_start(self,task):
        try:
            self.starts.setdefault(task,self._now())
            self.start_epochs.setdefault(task,self.wall_clock())
        except (OSError,ValueError,TypeError,RuntimeError) as exc:
            self.degrade('task:'+task,exc)

    def _deadline(self,watch,now,wall):

        watch.deadline=None if self.deadline is None else now+(self.deadline-wall)

    def launch(self,run,task,dispatch):
        native={'run_id':run,'task_id':task,'dispatch_id':dispatch}
        path=self.root/('watch-'+digest(native)+'.json')
        now=self._now();wall=self.wall_clock()
        if self.origin_error:raise self.origin_error
        if path.exists():
            data=json.loads(path.read_bytes());expected=data.pop('sha256')
            if not {'clock','deadline_epoch_ms','started_epoch_ms'}<=set(data):
                raise ValueError('original supervision clock domains unavailable; do not renew')
            if (digest(data)!=expected or data['native']!=native or data['policy']!=self.policy
                    or data['deadline_epoch_ms']!=self.deadline):
                raise ValueError('original supervision binding/policy drift')
            if data['clock']['origin']!=self.origin or now<data['clock']['monotonic_ms']:
                raise ValueError('monotonic origin changed; original budgets cannot be reconstructed')
            watch=Watch(native,self.policy,started_ms=data['started'])
            for key in ('activity','progress','last_clock','bound','queue_started','sequence'):
                setattr(watch,key,data[key])
            watch.seen,watch.milestones=set(data['seen']),set(data['milestones'])
            self.epochs[dispatch]=data['started_epoch_ms']
            self.streams[dispatch]=data.get('heartbeat_streams',{})
        else:
            if task not in self.starts or task not in self.start_epochs:
                raise ValueError('original start clocks unavailable; do not renew')
            watch=Watch(native,self.policy,started_ms=self.starts[task]);watch.bound=True
            self.epochs[dispatch]=self.start_epochs[task]
            self.streams[dispatch]={}
        self._deadline(watch,now,wall)
        self.collectors[dispatch]=(watch,path)
        try:self._save(dispatch)
        except (OSError,ValueError,TypeError,RuntimeError) as exc:self.degrade(dispatch,exc)

    def degrade(self,dispatch,exc,*,run=None,task=None):
        self.degraded[dispatch]={'state':'supervision_degraded','kind':'auxiliary_clock_or_watch',
            'reason':type(exc).__name__+': '+str(exc),'persistence':'unverified',
            'worker_death_proven':False,'retry_authorized':False,'release_authorized':False}

    def _save(self,dispatch):
        watch,path=self.collectors[dispatch]
        now=self._now();wall=self.wall_clock()
        data={k:copy.deepcopy(getattr(watch,k)) for k in (
            'native','policy','started','activity','progress','last_clock','deadline','bound','queue_started')}
        data.update(sequence=getattr(watch,'sequence',0),seen=sorted(watch.seen),milestones=sorted(watch.milestones),
            started_epoch_ms=self.epochs[dispatch],deadline_epoch_ms=self.deadline,
            heartbeat_streams=copy.deepcopy(self.streams[dispatch]),
            clock={'origin':self.origin,'monotonic_ms':now,'wall_epoch_ms':wall},
            epochs={k:None if getattr(watch,k) is None else wall-(now-getattr(watch,k))
                    for k in ('activity','progress','queue_started')},
            restart_semantics='same kernel boot: preserve monotonic budgets; changed/unknown boot: degrade, collect original')
        data['sha256']=digest(data)
        atomic_bytes(path,(json.dumps(data,sort_keys=True)+'\n').encode())

    def message(self,message,run):
        payload=message.get('payload') or {}
        try:
            if isinstance(payload,str):payload=json.loads(payload)
            return self._message(message,run)
        except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
            dispatch=payload.get('dispatchId') if isinstance(payload,dict) else None
            self.degrade(dispatch or 'unidentified-message',exc)
            return False

    def _message(self,message,run):
        import datetime
        payload=message.get('payload') or {}
        if isinstance(payload,str):payload=json.loads(payload)
        dispatch=payload.get('dispatchId');task=payload.get('taskId')
        if dispatch not in self.collectors:return False
        watch,_=self.collectors[dispatch]
        native={'run_id':run,'task_id':task,'dispatch_id':dispatch}
        if native!=watch.native or not message.get('id'):return False
        if message.get('type') in {'worker_done','question'}:
            self.collectors.pop(dispatch);return False
        if message.get('type')!='heartbeat':return False
        sequence=payload.get('sequence');stamp=message.get('createdAt') or message.get('created_at')
        now=self._now();wall=self.wall_clock()
        epoch=None
        if stamp:epoch=int(datetime.datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()*1000)

        event_time=None if epoch is None else now+(epoch-wall)
        original_epoch=self.epochs[dispatch]
        backward=wall<original_epoch+(now-watch.started)
        if epoch is not None and not (epoch>=original_epoch or
                (backward and epoch<=wall and event_time>=watch.started)):return False
        domain='native' if sequence is not None else 'timestamp'
        value=sequence if sequence is not None else event_time
        if type(value) is not int or value<1:return False
        stream=self.streams[dispatch]
        if value<=stream.get(domain,0):return False

        arrival=getattr(watch,'sequence',0)+1
        kind=payload.get('progress_kind','heartbeat');binding=payload.get('progress_receipt') or {};evidence=binding.get('sha256')
        def valid(_):
            from adaptive_contract import sha
            from pathlib import Path
            path=Path(binding.get('path','')).resolve()
            return path.is_relative_to(self.root) and path.is_file() and sha(path)==evidence
        event={'native':native,'event_id':message['id'],'sequence':arrival,'kind':kind,'evidence_sha256':evidence}
        accepted=watch.observe(event,now_ms=now,validate_evidence=valid)
        if accepted:stream[domain]=value
        if payload.get('phase')=='reader_queue' and accepted:watch.queue(now_ms=now)
        self._save(dispatch)
        return accepted

    def checkpoints(self):
        rows=copy.deepcopy(self.failures)
        for dispatch,(watch,_) in list(self.collectors.items()):
            try:
                now=self._now();self._deadline(watch,now,self.wall_clock())
                row=watch.tick(now_ms=now)
                try:self._save(dispatch)
                except (OSError,ValueError,TypeError,RuntimeError) as exc:self.degrade(dispatch,exc)
                if row['state']!='active' and not self.telemetry_only:rows[dispatch]=row
            except (OSError,ValueError,TypeError,RuntimeError) as exc:
                self.degrade(dispatch,exc)                                               
        return rows

    def recover_launch(self,run,task,dispatch):
        native={'run_id':run,'task_id':task,'dispatch_id':dispatch}
        if not (self.root/('watch-'+digest(native)+'.json')).exists():
            raise ValueError('original supervision clocks unavailable; do not renew')
        self.launch(run,task,dispatch)

    def wait_ms(self,maximum):
        if self.telemetry_only:return maximum
        try:
            now=self._now();wall=self.wall_clock();bounds=[maximum]
            for dispatch,(watch,_) in self.collectors.items():
                self._deadline(watch,now,wall)
                due=[watch.activity+watch.policy['activity_ms'],watch.progress+watch.policy['no_progress_ms']]
                if not watch.bound:due.append(watch.started+watch.policy['start_ms'])
                if watch.queue_started is not None:due.append(watch.queue_started+watch.policy['queue_ms'])
                if watch.deadline is not None:due.append(watch.deadline)
                bounds.append(max(1,min(due)-now))
            return max(1,min(bounds))
        except (OSError,ValueError,TypeError,RuntimeError) as exc:
            self.degrade('wait-clock',exc);return max(1,maximum)

SUCCESSORS = {
    'ordinary-finite-v1': {'host': 'candidate dispatcher.execute', 'scope': 'ordinary candidate plans'},
    'frozen-successor-finite-v1': {'host': 'candidate dispatcher.execute', 'scope': 'successor only; frozen bytes untouched'},
    'integrated-finite-v1': {'host': 'candidate dispatcher.execute', 'scope': 'explicit adaptive engine callbacks'},
}
SUPPORT.update({key: dict(value, supported=True, policy=POLICY) for key, value in SUCCESSORS.items()})

def execute_selected(dispatcher, selector, plan, router, client, *, root, policy,
                     clock=None, wall_clock=None, deadline_ms=None, **kwargs):
    if selector not in SUCCESSORS:
        raise ValueError('unsupported successor selector: ' + str(selector))
    passive=plan.get('defaults',{}).get('timing_policy')=='passive-no-kill'
    if passive:
        plan=copy.deepcopy(plan);plan['defaults'].pop('overall_timeout_ms',None);deadline_ms=None
    supervisor = DispatchSupervisor(root, policy, clock=clock, wall_clock=wall_clock, deadline_ms=deadline_ms,telemetry_only=passive)
    result = dispatcher.execute(plan, router, client, collection_supervisor=supervisor,
                                observer_fault_policy='record_auxiliary', **kwargs)
    result['collector'] = {'selector': selector, 'policy': POLICY, 'support': SUPPORT[selector],
        'degraded': copy.deepcopy(supervisor.degraded),'timing_is_telemetry_only':passive}
    return result
