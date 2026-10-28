
from __future__ import annotations

import copy
import json
from pathlib import Path

from adaptive_contract import digest
from adaptive_state import atomic_bytes, Conflict

IDENTITIES = ("run_id", "task_id", "dispatch_id")

def native_object(value, label):
    if not isinstance(value, dict):
        raise ValueError("native " + label + " must be an object; identity unknown")
    return value

def native_field(payload, field):
    return native_object(native_object(payload, "readback").get(field), field)

def readback_identity(payload):
    payload = native_object(payload, "payload")
    root = native_object(payload.get("result", payload), "result")
    dispatch = native_field(root, "dispatch")
    native = {"run_id": dispatch.get("runId"), "task_id": dispatch.get("taskId"),
              "dispatch_id": dispatch.get("id")}
    if not all(isinstance(v, str) and v for v in native.values()):
        raise ValueError("native readback lacks original Run/Task/Dispatch")
    projection = native_field(root, "projection")
    if any(projection.get(k) != v for k, v in zip(("runId", "taskId", "dispatchId"), native.values())):
        raise ValueError("native projection identity conflict")
    if native_field(root, "worker").get("dispatchId") != native["dispatch_id"]:
        raise ValueError("native worker identity conflict")
    return native, root

class NativeBridge:
    def __init__(self, journal, occurrence, node, custody):
        self.journal, self.occurrence, self.node = journal, occurrence, node
        self.custody = copy.deepcopy(custody)
        self.folder = journal.root / ("native-" + digest([occurrence, node]))
        self.folder.mkdir(exist_ok=True)

    def _save(self, name, payload):
        path = self.folder / (name + ".json")
        raw = (json.dumps(payload, sort_keys=True, allow_nan=False) + "\n").encode()
        if path.exists() and path.read_bytes() != raw:
            raise Conflict("immutable native receipt differs: " + name)
        if not path.exists():
            atomic_bytes(path, raw)

    def prepare(self, inputs):

        if self.journal.get(self.occurrence, self.node):
            raise Conflict("original intent exists; recover only, never relaunch")
        return self.journal.intent(self.occurrence, self.node, inputs, self.custody)

    def observe(self, stage, payload):

        try:
            self._observe(stage, payload)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
            if stage not in {"launch", "settled"}:
                raise
            result = {"state": "rejected" if isinstance(exc, ValueError) else "unknown",
                      "stage": stage, "reason": type(exc).__name__ + ": " + str(exc),
                      "payload_sha256": digest(payload), "retry_authorized": False}
                                                                               
            try:
                self._save("observation-" + digest(result), result)
            except OSError:
                result["persistence"] = "unavailable; host bundle required"
            return result
        return {"state": "recorded", "stage": stage}

    def _observe(self, stage, payload):
        row = self.journal.get(self.occurrence, self.node)
        if row is None:
            raise Conflict("dispatcher receipt before durable intent")
        if stage not in {"prepared", "run", "task", "tasks_created", "before_start", "launch", "dispatched", "settled"}:
            raise ValueError("unsupported dispatcher receipt stage")
        self._save(stage, payload)
        receipt = self._settlement(payload) if stage == "settled" else None
        if stage == "launch":
            native = {key: payload[key] for key in IDENTITIES}
            partial = json.loads((self.folder / "task.json").read_bytes())
            run = json.loads((self.folder / "run.json").read_bytes())
            if (run["run_id"] != native["run_id"] or
                    any(partial[k] != native[k] for k in ("run_id", "task_id"))):
                raise ValueError("native launch differs from original creation receipts")
            launch = {"native": native, "origin": row["origin"], "raw": copy.deepcopy(payload["receipt"])}
            if row.get("native"):
                if row["native"] != native or row["launch_receipt"] != launch:
                    raise Conflict("original native binding changed")
            else:
                self.journal.bind(self.occurrence, self.node, native, launch, self.custody)
        if stage == "settled":

            self.journal.accept(self.occurrence, self.node, receipt, self.custody)

    def _settlement(self, payload):
        row = self.journal.get(self.occurrence, self.node)
        units = payload.get("units", [])
        if len(units) != 1 or not row or not row.get("native"):
            raise ValueError("one original bound unit required")
        unit = units[0]; native = row["native"]
        message = payload.get("native_message", {})
        data = message.get("payload", {})
        if isinstance(data, str):
            data = json.loads(data)
        if (payload.get("run_id") != native["run_id"] or unit.get("task_id") != native["task_id"] or
                unit.get("dispatch_id") != native["dispatch_id"] or message.get("type") != "worker_done" or
                data.get("taskId") != native["task_id"] or data.get("dispatchId") != native["dispatch_id"] or
                data.get("outcome") not in {"succeeded", "failed"} or
                unit.get("contract", {}).get("accepted") is not True or
                unit.get("status") not in {"succeeded", "no_change", "failed"}):
            raise ValueError("rejected/stale/wrong original native settlement")
        return {"native": native, "origin": row["origin"],
                "state": "failed" if data["outcome"] == "failed" or unit["status"] == "failed" else "succeeded",
                "contract_accepted": True, "native_outcome": data["outcome"],
                "settlement_sha256": digest(payload)}

    def accept_original_delivery(self, delivery, unit, read_original):

        from native_adapter import load_dispatcher
        d=load_dispatcher();row=self.journal.get(self.occurrence,self.node)
        native=row.get('native') if row else None
        if not native or delivery.get('runId')!=native['run_id'] or not delivery.get('deliveryId'):
            raise ValueError('late Delivery lacks original Run binding')
        messages=delivery.get('messages',[])
        if len(messages)>64:raise ValueError('late Delivery exceeds finite import bound')
        matches=[]
        for message in messages:
            data=d.payload_json(message)
            if message.get('type')=='worker_done' and data.get('taskId')==native['task_id'] and data.get('dispatchId')==native['dispatch_id']:
                matches.append((message,data))
        if len(matches)!=1:raise ValueError('exactly one late original worker_done required')
        message,data=matches[0]
        shown,readback=readback_identity(read_original(native))
        if (shown!=native or readback['dispatch'].get('status') not in {'completed','failed'}
                or readback['projection'].get('outcome')!=data.get('outcome')):
            raise ValueError('late original native settlement differs')
        body=message.get('body','');accepted,reasons=d.validate_return(body,unit.get('return_contract',{}))
        declared,errors=d.parse_declared_outcome(body,unit)
        worker_done={'body':body,'report_path':data.get('reportPath') or data.get('report_path'),
                     'files_modified':data.get('filesModified') or data.get('files_modified') or []}
        outputs,checks,output_errors=d.verify_outputs(unit,worker_done,declared)
        if not message.get('id') or not accepted or errors or not outputs:
            raise ValueError('late original contract rejected: '+str(reasons+errors+output_errors))
        disposition=d.outcome_disposition(declared,unit)
        status=d.lane_status(accepted=True,worker_outcome=data.get('outcome'),disposition=disposition)
        payload={'run_id':native['run_id'],'native_message':message,'native_delivery':delivery,
                 'units':[dict(native,contract={'accepted':True,'reasons':[]},status=status,outputs=checks)]}
        self._settlement(payload)                                                          
        self.observe('settled',payload)
        return self.recover(read_original)

    def recover(self, read_original):

        row = self.journal.get(self.occurrence, self.node)
        if row is None:
            return {"state": "held", "reason": "no original intent; unstarted dependent", "native_launches": 0}
        try:
            if not row.get("native"):
                                                                              
                launch = self.folder / "launch.json"
                if not launch.exists():
                    run_path, task_path = self.folder / 'run.json', self.folder / 'task.json'
                    reason = ("Run captured; Task creation not captured; refusal versus unknown requires host receipt"
                              if run_path.exists() and not task_path.exists() else
                              "Task captured; Dispatch creation not captured; qualified request reconciliation required"
                              if task_path.exists() else "creation identity not captured; qualified request reconciliation required")
                    failure_path=self.folder/'creation-failure.json'
                    if failure_path.exists():reason=json.loads(failure_path.read_bytes())['reason']
                    return dict(row, state="unknown", reason=reason,
                                native_launches=0, retry_authorized=False)
                self.observe("launch", json.loads(launch.read_bytes()))
                row = self.journal.get(self.occurrence, self.node)
            raw = read_original(copy.deepcopy(row["native"]))
            native, root = readback_identity(raw)
            self._save("readback-" + digest(raw), raw)
            if native != row["native"]:
                raise ValueError("stale/wrong original readback")
            snapshot = self.folder / "settled.json"
            if not snapshot.is_file():
                return dict(row, state="unknown", reason="native readback alone cannot prove contract acceptance",
                            native_launches=0, retry_authorized=False)
            receipt = self._settlement(json.loads(snapshot.read_bytes()))
            if (root["dispatch"].get("status") not in {"completed", "failed"} or
                    root["projection"].get("outcome") != receipt["native_outcome"]):
                raise ValueError("original authoritative settlement differs or remains unknown")
            result = self.journal.accept(self.occurrence, self.node, receipt, self.custody)
            self.journal.checkpoint(self.occurrence, self.node, self.custody)
            return dict(result, native_launches=0)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
            result = dict(row, state="unknown", reason=type(exc).__name__ + ": " + str(exc),
                        native_launches=0, retry_authorized=False)
            try:
                self._save("recovery-" + digest(result), result)
            except OSError:
                result["persistence"] = "unavailable"
            return result

    def recover_creation(self, client, request_root, *, scope):

        from adaptive_mutations import MutationJournal,flag,verb
        import hashlib
        row=self.journal.get(self.occurrence,self.node)
        if not row or row.get('native'):return row
        try:
            try:
                prepared=json.loads((self.folder/'prepared.json').read_bytes())
            except FileNotFoundError:
                                                                            
                return dict(row,state='held',kind='no_creation_request',
                            reason='node intent only; no creation request',
                            retry_authorized=False,native_launches=0)
            units=[u for u in prepared['units'] if u['id']==self.node]
            if len(units)!=1:raise ValueError('original prepared branch specification missing/ambiguous')
            expected=units[0]['spec_sha256']
            mutations=MutationJournal(request_root,scope=scope)
            records=[json.loads(p.read_bytes()) for p in mutations.folder.glob('*.json')]
            tasks=[r for r in records if r.get('scope')==scope and verb(r['argv'])=='task-create' and
                   hashlib.sha256((flag(r['argv'],'--spec') or '').encode()).hexdigest()==expected]
            if len(tasks)!=1:raise ValueError('original Task request not uniquely spec-bound')
            task=mutations.reconcile(tasks[0],client)
            if task['state']!='reconciled':raise ValueError('original Task request '+str(task))
            workers=[r for r in records if r.get('scope')==scope and verb(r['argv'])=='worker-start' and
                     flag(r['argv'],'--task')==task['task_id'] and flag(r['argv'],'--run')==task['run_id']]
            if not workers:
                                                                           
                cleanup={'run_id':task['run_id'],'task_id':task['task_id'],
                         'disposition':'retained unstarted; owner cleanup required',
                         'readback':task,'native_launches':0,'retry_authorized':False}
                self._save('task-only-cleanup-'+digest(cleanup),cleanup)
                return dict(row,state='held',reason='original Task reconciled; no Dispatch request; never start dependent',
                            creation_reconciliation=task,cleanup=cleanup,native_launches=0,retry_authorized=False)
            if len(workers)!=1:raise ValueError('original Dispatch request missing/ambiguous')
            worker=mutations.reconcile(workers[0],client)
            if worker['state']!='reconciled':raise ValueError('original Dispatch request '+str(worker))
            native={k:worker[k] for k in IDENTITIES}
            self._save('run',{'run_id':task['run_id']}) if not (self.folder/'run.json').exists() else None
            self._save('task',{'run_id':task['run_id'],'task_id':task['task_id']}) if not (self.folder/'task.json').exists() else None
            self.observe('launch',dict(native,receipt=worker['receipt']))
            return self.journal.get(self.occurrence,self.node)
        except (OSError,ValueError,KeyError,TypeError,AttributeError,RuntimeError) as exc:
            return dict(row,state='unknown',reason=str(exc),retry_authorized=False,native_launches=0)

    def recovered_engine_receipt(self, read_original):
        row = self.recover(read_original)
        return {"occurrence_id": self.occurrence, "node_id": self.node, "revision_id": 0,
                "state": row["state"], "native": row.get("native"),
                "receipt_sha256": row.get("receipt_sha256"), "reason": row.get("reason")}

def collect_original(client, native):

    return client.worker_show(native["dispatch_id"])
