
from __future__ import annotations

import hashlib
import json
import os
import logging
from dataclasses import asdict
from pathlib import Path
from datetime import datetime
from datetime import timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

import guards
import local
import project_rooms
import room_continuation
import routing
from registry import RegistryError, account_blocked, worker_account, institution
from routing import Allowlist, Request, Sender
from runner import (DispatcherBackend, OrcaBackend, Outcome, build_plan,
                    dispatcher_module, lifecycle, run, skill_root)
from rooms import record_convening, resolve as resolve_room
from state import SwitchboardState, default_state_path

log = logging.getLogger("praxis.switchboard")

Poster = Callable[[Dict[str, Any]], None]

def completion_times_bound(dispatch, task, result):

    if result.get('completedAt') == dispatch.get('completedAt'):
        return True
    if task.get('completed_at') != dispatch.get('completedAt'):
        return False
    try:
        def stamp(value):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        return stamp(dispatch['dispatchedAt']) <= stamp(result['completedAt']) <= stamp(dispatch['completedAt'])
    except (ValueError, KeyError, TypeError, AttributeError):
        return False

def stream_ordinary(argv, post):

    import subprocess
    errors=[];result=None
    with subprocess.Popen(argv,stdout=subprocess.PIPE,text=True) as process:
        for line in process.stdout:
            try:
                event=json.loads(line)
                if not isinstance(event,dict):raise ValueError('ordinary child event must be object')
            except (ValueError,TypeError) as exc:
                errors.append('child output: '+str(exc));continue
            if event.get('type')=='praxis45_progress':
                try:post(event)
                except Exception as exc:errors.append('progress delivery: '+str(exc))
            else:result=event
        code=process.wait()
    if result is None:return {'status':'unresolved','reason':'ordinary child returned no result','stream_errors':errors,'child_exit_code':code}

    if errors:return dict(result,stream_errors=errors,child_exit_code=code)
    return dict(result,child_exit_code=code)

def handle_ordinary(text,post):
                                                                                
    import re
    if not re.fullmatch(r'@Leo\s+(?:(deep research|fact check)( preview)?:\s*(.+)|(graph status|graph recover|provenance|correction consumers|interruptions)(?:\s+(.+))?)',text.strip(),re.I|re.S):
        if 'PRAXIS45_REHEARSAL_ROOT' in os.environ:
            return {'lifecycle':'failed','praxis45':{'status':'input_refused','reason':'Isolated rehearsal requires its exact ordinary request','native_launches':0},'presentation_errors':[]}
        return None
    presentation_errors=[]
    def safe_post(event):
        try:post(event)
        except Exception as exc:presentation_errors.append(str(exc))
    import sys
    release = Path.home()/".local/share/praxis/releases/current/code/services/praxis45"
    own = Path(__file__).resolve().parents[1] / "praxis45"
    if (own.parents[2]/"release.json").is_file():release=own
    candidate = release if release.is_dir() else own
    flag = Path.home()/".local/share/praxis/releases/activation.json"
    rehearsal = os.environ.get('PRAXIS45_REHEARSAL_ROOT')
    rehearsal_selected = 'PRAXIS45_REHEARSAL_ROOT' in os.environ
    fresh_rehearsal = os.environ.get('PRAXIS45_FRESH_REHEARSAL_ROOT')
    active = False
    try:
        if rehearsal_selected:
            if not rehearsal.strip():raise ValueError('empty isolated rehearsal root')
            flag=Path(rehearsal)/'activation.json'
            state=json.loads(flag.read_bytes())
            active=state.get('enabled') is True and state.get('version')=='praxis-4.5.0'
            if not active:raise ValueError('isolated rehearsal activation disabled or invalid')
        elif fresh_rehearsal:
            active=True
        else:
            marker=release.resolve().parents[2]/'release.json'
            if flag.is_file() and not flag.is_symlink():
                initial=json.loads(flag.read_bytes())
                active=initial.get('enabled') is True and initial.get('version')=='praxis-4.5.0' and initial.get('schema_version')==1
                                                                              
            if marker.is_file():
                info=json.loads(marker.read_bytes())
                flag=Path(info['installation_home'])/'.local/share/praxis/releases/activation.json'
            if not flag.is_file() or flag.is_symlink():return None
            state=json.loads(flag.read_bytes())
            active=state.get('enabled') is True and state.get('version')=='praxis-4.5.0' and state.get('schema_version')==1
            if not active:return None
            info=json.loads(marker.read_bytes())
            if state.get('commit')!=info['commit']:raise ValueError('activation/release identity differs')
        if not candidate.is_dir():raise ValueError('active Praxis 4.5 release code is missing')
        if str(candidate) not in sys.path:sys.path.insert(0,str(candidate))
        import ordinary
        if not ordinary.available():
            raise ValueError('active Praxis 4.5 release could not be loaded')
        if release.is_dir():
            import subprocess
            runtime=os.environ.get('PRAXIS45_RUNTIME_TREE')
            if runtime in {'.claude','.agents'}:
                info=json.loads((release.resolve().parents[2]/'release.json').read_bytes())
                entry=Path(info['installation_home'])/runtime/'skills/orca-dispatch/p4_run.py'
                command=[str(entry),'ordinary','--request',text,'--json-events']
            else:command=[str(release/'ordinary.py'),'--request',text,'--json-events']
            result=stream_ordinary([str(release.resolve().parents[2]/'venv/bin/python'),'-I','-B',*command],safe_post)
        else:result=ordinary.handle(text,progress=lambda message: safe_post({'type':'praxis45_progress','message':message}))
    except Exception as exc:
        if not active and not (rehearsal_selected or fresh_rehearsal):return None
        result={"status":"unresolved","reason":"Active Praxis 4.5 release is unavailable: "+str(exc),"native_launches":0}
    lifecycle_state="completed" if result.get("status") in {"observed","preview","succeeded"} else "failed"
    safe_post({"type":"complete","lifecycle":lifecycle_state,"message":result.get("human_summary") or json.dumps(result,ensure_ascii=False),
          "praxis45_brief":result.get("brief_path"),"quiet_message":result.get("quiet_summary")})
    return {"lifecycle":lifecycle_state,"praxis45":result,"presentation_errors":presentation_errors}

class Switchboard:
    def __init__(self, *, backend: Optional[OrcaBackend] = None,
                 state: Optional[SwitchboardState] = None,
                 allowlist: Optional[Allowlist] = None,
                 outdir: Optional[Path] = None,
                 check_account: bool = True,
                 log_routes: bool = True,
                 spark: Optional[local.Spark] = None):
        self.backend = backend or DispatcherBackend()

        self.spark = spark or local.CliSpark()
        self.state = state or SwitchboardState(default_state_path())
        self.allowlist = allowlist or Allowlist()
        self.outdir = outdir
        self.check_account = check_account
        self.log_routes = log_routes

    def handle(self, text: str, sender: Sender, post: Poster, *,
               event_id: Optional[str] = None,
               thread_id: Optional[str] = None,
               room_id: Optional[str] = None,
               room_agenda: Optional[str] = None,
               context_room_id: Optional[str] = None,
               context: Optional[object] = None,
               adaptive_entry: bool = False,
               explicit_model: Optional[str] = None,
               task_mode: Optional[str] = None,
               engine_preview: bool = False) -> Dict[str, Any]:

        ordinary_result=handle_ordinary(text,post)
        if ordinary_result is not None:
            return ordinary_result

        existing = self.state.seen_event(event_id)
        if existing:
            if (self.state.get(existing) or {}).get("lifecycle") == "pending":
                self.recover(existing, post)

            post({"type": "duplicate", "request_id": existing,
                  "message": f"Already handled as {existing}."})
            post({"type": "complete", "request_id": existing,
                  "lifecycle": "duplicate",
                  "message": f"Already handled as {existing}."})
            return self.state.get(existing) or {"request_id": existing, "lifecycle": "duplicate"}

        req = routing.build(text, sender, allowlist=self.allowlist,
                            event_id=event_id, thread_id=thread_id,
                            log_routes=self.log_routes, room_id=room_id,
                            room_agenda=room_agenda, context=context,
                            context_room_id=context_room_id, adaptive_entry=adaptive_entry,
                            explicit_model=explicit_model,
                            task_mode=task_mode,
                            continuation_resolver=room_continuation.bind,
                            spark=self.spark)

        record = self.state.create_request(
            req.request_id,
            sender=sender.user_id,
            transport=sender.transport,
            guild_id=sender.guild_id,
            channel_id=sender.channel_id,
            thread_id=thread_id,
            event_id=event_id,
            recipients=list(req.recipients),
            requires_approval=req.verdict.requires_approval,
            room_id=room_id,
            context_room_id=req.context_room_id,
            chair_name=req.chair_name,
            execution_handoff=req.execution_handoff,
            adaptive_planner=req.adaptive_planner,
            continuation=req.continuation,
            body_size=len(req.body or ""),
            user_prompt_sha256=hashlib.sha256(req.body.encode()).hexdigest(),

            context_kind=req.context.kind if req.context else None,
            context_note=str(req.context.note) if req.context else None,
            context_identity=(project_rooms.context_identity(req.context)
                              if req.context else None),
            context_digest=(project_rooms.context_digest(req.context)
                            if req.context else None),
        )
        record["output_dir"] = str(self._outdir(req))
        self.state.record_event(event_id, req.request_id)
        self.state.bind_thread(thread_id, req.request_id)
        self.state.save()

        log.info("request %s transport=%s recipients=%s body=%s",
                 req.request_id, sender.transport, ",".join(req.recipients),
                 guards.safe_for_log(req.body))

        ack: Dict[str, Any] = {"type": "ack", "request_id": req.request_id,
                               "message": routing.acknowledgement(req),
                               "recipients": list(req.recipients)}
        if req.local_answer is not None:

            ack["lifecycle"] = "answered_locally"
        if not req.verdict.allowed:

            ack["lifecycle"] = "rejected"
        post(ack)

        if not req.verdict.allowed:
            self.state.update(req.request_id, lifecycle="rejected",
                              reasons=req.verdict.reasons)
            self.state.save()

            post({"type": "complete", "request_id": req.request_id,
                  "lifecycle": "rejected",
                  "message": "; ".join(req.verdict.reasons)
                             or "Not dispatched. No reason was recorded."})
            return self.state.get(req.request_id)

        if req.local_answer is not None:
            return self._settle_locally(req, post)

        if req.continuation_hold:
            self.state.update(req.request_id, lifecycle='awaiting_owner', next_owner='Leo',
                              reasons=[req.continuation_hold])
            self.state.save()
            self._persist_request(req)
            post({'type': 'complete', 'request_id': req.request_id,
                  'lifecycle': 'awaiting_owner', 'message': req.continuation_hold})
            return self.state.get(req.request_id)

        self._persist_request(req)

        if req.refusals:
            for seat, why in req.refusals.items():
                self.state.set_task(req.request_id, seat, state="refused", reason=why)
                post({"type": "refused", "seat": seat, "message": why})

        blocked = self._account_blocks(req)
        if req.continuation and (blocked or req.refusals):
                                                                             
            reasons = list(req.refusals.values()) + list(blocked.values())
            self.state.update(req.request_id, lifecycle='failed', reasons=reasons,
                              continuation_no_launch='account preflight')
            self.state.save()
            self._persist_outcome(req, 'failed', reasons)
            post({'type': 'complete', 'request_id': req.request_id, 'lifecycle': 'failed',
                  'message': 'Coordination did not launch; no continuation claim was consumed. ' +
                             '; '.join(reasons)})
            return self.state.get(req.request_id)
        for seat, why in blocked.items():
            self.state.set_task(req.request_id, seat, state="unavailable", reason=why)
            post({"type": "unavailable", "seat": seat, "message": why})
            req.recipients.remove(seat)

        if not req.recipients:
            reasons = list(req.refusals.values()) + list(blocked.values())
            self.state.update(req.request_id, lifecycle="failed", reasons=reasons)
            self.state.save()
            self._persist_outcome(req, "failed", reasons)
            post({"type": "complete", "lifecycle": "failed",
                  "message": "Nobody could be reached."})
            return self.state.get(req.request_id)

        import room_attractor
        if engine_preview or room_attractor.selected(req.context, req.context_room_id):
            return room_attractor.run_request(self, req, post, preview=engine_preview)

        prepared_plan = None
        if req.continuation:
            try:
                room_continuation.verify(req.context, req.context_room_id, req.continuation)
                prepared_plan = build_plan(req, self._outdir(req))
                validator = dispatcher_module(self.backend)
                try:
                    validator.validate_plan(prepared_plan)
                except validator.DispatchError as exc:
                    raise RegistryError(str(exc)) from exc
            except (RegistryError, OSError, ValueError) as exc:
                self.state.update(req.request_id, lifecycle='failed', reasons=[str(exc)],
                                  continuation_no_launch='validation preflight')
                self.state.save()
                self._persist_outcome(req, 'failed', [str(exc)])
                post({'type': 'complete', 'request_id': req.request_id, 'lifecycle': 'failed',
                      'message': 'Coordination did not launch; no continuation claim was consumed. ' + str(exc)})
                return self.state.get(req.request_id)
            try:
                prior = self.state.claim_continuation(
                    req.continuation['claim_key'], req.request_id,
                    context_digest=project_rooms.context_digest(req.context))
                if prior:
                    req.continuation_hold = (f'Continuation already claimed by {prior}; canonical Leo must inspect '
                                             'it. Use recover --continuation-no-launch only for a proven held '
                                             'chair and blocked Leo; live, unknown and completed work cannot replay.')
            except (OSError, ValueError) as exc:
                req.continuation_hold = str(exc)
            if req.continuation_hold:
                self.state.update(req.request_id, lifecycle='awaiting_owner', next_owner='Leo',
                                  reasons=[req.continuation_hold])
                self.state.save()
                self._persist_outcome(req, 'awaiting_owner', [req.continuation_hold])
                post({'type': 'complete', 'request_id': req.request_id,
                      'lifecycle': 'awaiting_owner', 'message': req.continuation_hold})
                return self.state.get(req.request_id)

        self.state.update(req.request_id, lifecycle="dispatched",
                          recipients=list(req.recipients))
        self.state.save()

        def sink(event: Dict[str, Any]) -> None:
            if event.get("type") == "run":
                self.state.update(req.request_id, run_id=event.get("run_id"))
                self.state.save()
                return
            seat = event.get("seat")
            if seat:
                fields = {"state": event.get("state") or event.get("type"),
                          "artifacts": event.get("artifacts") or []}

                if event.get("model"):
                    fields["model"] = event["model"]
                if event.get("fallback"):
                    fields["fallback"] = event["fallback"]
                for key in ("effort", "task_class", "task_mode", "model_source", "task_id", "dispatch_id"):
                    if key in event:
                        fields[key] = event[key]
                if event.get("type") == "question":
                    fields.update(state="awaiting_answer",
                                  question=event.get("message"),
                                  question_id=event.get("message_id"))
                self.state.set_task(req.request_id, seat, **fields)
            self.state.save()
            post(event)

        try:
            outcomes = run(req, self.backend, sink, outdir=self._outdir(req), plan=prepared_plan)
        except RegistryError as exc:

            message = str(exc)
            self.state.update(req.request_id, lifecycle="failed", reasons=[message])
            self.state.save()
            post({"type": "configuration_error", "request_id": req.request_id,
                  "message": message})
            post({"type": "complete", "request_id": req.request_id,
                  "lifecycle": "failed", "message": message})
            return self.state.get(req.request_id)
        final = lifecycle(outcomes, req.verdict.requires_approval)

        if room_id and final != "pending":
            room = resolve_room(room_id)
            chair = outcomes.get(room.chair)
            chair_record = self._outdir(req) / "chair-record.md"
            if not chair or chair.state != "succeeded" or not chair_record.is_file():
                final = "partial" if any(o.state == "succeeded" for o in outcomes.values()) else "failed"
                message = (f"{room.title} did not close: the dependent {room.chair} chair "
                           f"did not produce {chair_record}.")
                self.state.update(req.request_id, lifecycle=final,
                                  reasons=[message])
                self.state.save()
                post({"type": "room_incomplete", "request_id": req.request_id,
                      "room_id": room_id, "message": message})
            else:
                try:
                    record_convening(room, req.request_id,
                                     req.room_agenda or req.body, chair_record)
                    post({"type": "room_recorded", "request_id": req.request_id,
                          "room_id": room_id, "record": str(room.record)})
                except RegistryError as exc:
                    final = "partial"
                    message = f"{room.title} completed its workers but could not settle its record: {exc}"
                    self.state.update(req.request_id, lifecycle=final,
                                      reasons=[message])
                    self.state.save()
                    post({"type": "room_incomplete", "request_id": req.request_id,
                          "room_id": room_id, "message": message})

        artifacts: List[str] = []
        for o in outcomes.values():
            artifacts.extend(o.artifacts)
        self.state.update(req.request_id, lifecycle=final, artifacts=artifacts)
        self.state.save()

        post({"type": "complete", "request_id": req.request_id, "lifecycle": final,
              "message": self._completion_line(final, outcomes, req)})
        return self.state.get(req.request_id)

    def _settle_locally(self, req: Request, post: Poster) -> Dict[str, Any]:

        answer = req.local_answer
        outdir = self._outdir(req)
        written: List[str] = []
        try:
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "request.md").write_text(req.body.strip() + "\n")
            (outdir / "local-answer.json").write_text(
                json.dumps(answer.as_record(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            written.append(str(outdir / "local-answer.json"))
        except OSError as exc:

            post({"type": "note", "request_id": req.request_id,
                  "message": f"the local resolution could not be written: {exc}"})
        log.info("request %s answered from the registry; no call composed",
                 req.request_id)
        post({"type": "local_answer", "request_id": req.request_id,
              "kind": answer.kind, "message": answer.text,
              "routine": answer.routine, "procedure": answer.procedure,
              "owners": [o.seat for o in answer.owners],
              "evidence": list(answer.evidence),
              "sources": list(answer.sources),

              "attribution": answer.attribution, "data": dict(answer.data),
              "model_call": False, "chars": 0, "tokens": 0})
        self.state.update(req.request_id, lifecycle="answered_locally",
                          artifacts=written,
                          notes=[f"Answered from {', '.join(answer.sources)}. "
                                 f"No Orca Run, Task, worker or model call was "
                                 f"created."])
        self.state.save()
        post({"type": "complete", "request_id": req.request_id,
              "lifecycle": "answered_locally", "message": answer.text})
        return self.state.get(req.request_id)

    def _persist_request(self, req: Request) -> None:

        outdir = self._outdir(req)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
            path = outdir / "request.md"
            if not path.exists():
                path.write_text(req.body.strip() + "\n")
        except OSError as exc:                                                  
            log.warning("could not persist request %s: %s", req.request_id, exc)

    def _persist_outcome(self, req: Request, lifecycle: str,
                         reasons: Sequence[str]) -> None:

        outdir = self._outdir(req)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "outcome.json").write_text(json.dumps({
                "schema_version": 1,
                "request_id": req.request_id,
                "lifecycle": lifecycle,
                "recipients": sorted(set(req.routes)),
                "reasons": list(reasons),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }, indent=2) + "\n")
        except OSError as exc:                                                  
            log.warning("could not persist outcome for %s: %s", req.request_id, exc)

    def _outdir(self, req: Request) -> Path:
        return self._outdir_for(req.request_id)

    def _outdir_for(self, request_id: str) -> Path:
        if self.outdir:
            return Path(self.outdir) / request_id
        rec = self.state.get(request_id) or {}
        if rec.get("output_dir"):
            return Path(rec["output_dir"])
        if rec.get("context_room_id") and rec.get("context_note"):
            note = Path(rec["context_note"])
            context = project_rooms.ProjectContext(rec.get("context_title") or note.stem,
                                                   note, rec.get("context_kind") or "project")
            return project_rooms.output_root(context) / request_id
        return institution() / "switchboard" / request_id

    def _account_blocks(self, req: Request) -> Dict[str, str]:

        if not self.check_account:
            return {}
        account = worker_account()
        out = {}
        for name in list(req.recipients):
            why = account_blocked(req.routes[name], account)
            if why:
                out[name] = why
        return out

    def _completion_line(self, final: str, outcomes: Dict[str, Outcome],
                         req: Request) -> str:
        done = [n for n, o in outcomes.items() if o.state == "succeeded"]
        bad = [n for n, o in outcomes.items() if o.state not in ("succeeded", "pending")]
        asking = [n for n, o in outcomes.items() if o.state == "awaiting_answer"]
        if asking:
            return (f"{', '.join(asking)} asked you a question and stopped there. "
                    f"Answer with: switchboard.py reply {req.request_id} "
                    f"{asking[0]} \"<your answer>\"")
        if final == "awaiting_user":
            held = [n for n, o in outcomes.items() if o.state == "held"]
            if held:
                return (f"Held by {', '.join(held)} and waiting on you to resolve the scope. "
                        "Dependent execution did not start. Nothing has been sent, submitted, or committed.")
            return (f"Staged by {', '.join(done)} and waiting on you. "
                    f"Nothing has been sent, submitted, or committed.")
        if final == "pending":
            return self._pending_line(req.request_id, outcomes)
        if final == "completed":
            return f"Done — {', '.join(done)}."
        if final == "partial":
            return f"Partial: {', '.join(done)} answered; {', '.join(bad)} did not."
        return f"Failed: {', '.join(bad) or 'no seat answered'}."

    def _pending_line(self, request_id: str, outcomes: Dict[str, Outcome]) -> str:
        pending = [n for n, o in outcomes.items() if o.state == "pending"]
        message = (f"Pending: {', '.join(pending)} remains unsettled; "
                   "the original workers were preserved. No duplicate work was launched.")
        unstarted = [n for n in pending if outcomes[n].task_id and not outcomes[n].dispatch_id]
        if unstarted:
            run_id = (self.state.get(request_id) or {}).get("run_id") or "unverified"
            message += (f" {', '.join(unstarted)} has a created Task but no Dispatch. "
                        f"Next: the coordinator must inspect original Run {run_id} and recover "
                        "its collector/dependency progression. /status and recover collect returns "
                        "only; they cannot start dependent Tasks. Do not resubmit the prompt.")
        return message

    def reconcile_continuation(self, request_id: str, post: Poster) -> Dict[str, Any]:

        import hashlib
        rec = self.state.get(request_id) or {}
        target = rec.get('continuation') or {}
        root = self._outdir_for(request_id)
        plan_path, bundle_path = root / '_plan.json', root / '_bundle.json'
        plan, bundle = json.loads(plan_path.read_text()), json.loads(bundle_path.read_text())
        chair_id = str(rec.get('chair_name') or '').lower()
        units = {unit['id']: unit for unit in bundle.get('units', [])}
        chair, leo = units.get(chair_id) or {}, units.get('leo') or {}
        chair_plan = next((unit for unit in plan.get('units', []) if unit['id'] == chair_id), {})
        if (not target or plan.get('id') != request_id or bundle.get('plan_id') != request_id or
                not rec.get('run_id') or bundle.get('run_id') != rec['run_id'] or
                (plan.get('context') or {}).get('digest') != rec.get('context_digest') or
                chair.get('status') != 'held' or not (chair.get('contract') or {}).get('accepted') or
                not chair.get('task_id') or not chair.get('dispatch_id') or
                leo.get('status') != 'blocked' or leo.get('dispatch_id') or
                leo.get('no_launch') != {'reason': 'dependency did not succeed', 'depends_on': [chair_id]}):
            raise RegistryError('No proven held-chair/no-Leo-launch settlement; retain the claim')
        native = self.backend.inspect_completion(rec['run_id'], chair['task_id'], chair['dispatch_id'])
        show, task = native.get('show') or {}, native.get('task') or {}
        dispatch, worker = show.get('dispatch') or {}, show.get('worker') or {}
        result = task.get('result') or {}
        if isinstance(result, str):
            result = json.loads(result)
        body = str(result.get('body') or '')
        validator = dispatcher_module(self.backend)
        accepted, reasons = validator.validate_return(body, chair_plan.get('return_contract') or {})
        declared, more = validator.parse_declared_outcome(body, chair_plan)
        if (dispatch.get('id') != chair['dispatch_id'] or dispatch.get('taskId') != chair['task_id'] or
                dispatch.get('runId') != rec['run_id'] or dispatch.get('status') != 'completed' or
                task.get('id') != chair['task_id'] or task.get('run_id') != rec['run_id'] or
                worker.get('dispatchId') != chair['dispatch_id'] or worker.get('state') != 'succeeded' or
                result.get('outcome') != 'succeeded' or result.get('provenance') != 'worker_report' or
                not result.get('messageId') or not dispatch.get('assigneeHandle') or
                result.get('completedBy') != dispatch['assigneeHandle'] or not dispatch.get('completedAt') or
                result.get('completedAt') != dispatch['completedAt'] or not accepted or reasons or more or
                declared != 'held' or body != (chair.get('worker_done') or {}).get('body')):
            raise RegistryError('Exact accepted native held-chair return is unverified; retain the claim')
        proof = {'at': datetime.now().astimezone().isoformat(), 'native': native,
                 'plan': {'path': str(plan_path), 'sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest()},
                 'bundle': {'path': str(bundle_path), 'sha256': hashlib.sha256(bundle_path.read_bytes()).hexdigest()}}
        journal = root / ('continuation-no-launch-' + hashlib.sha256(
            json.dumps(proof, sort_keys=True).encode()).hexdigest() + '.json')
        with journal.open('x') as file:
            json.dump(proof, file, indent=2)
            file.write('\n')
            file.flush()
            import os
            os.fsync(file.fileno())
        self.state.reconcile_continuation(target['claim_key'], request_id,
                                         {'path': str(journal), 'sha256': hashlib.sha256(journal.read_bytes()).hexdigest()})
        post({'type': 'complete', 'request_id': request_id, 'lifecycle': rec.get('lifecycle'),
              'message': 'Proven held-chair coordination reconciled; Leo did not launch. Original returns '
                         'are retained. Refresh the original pending-work evidence before a new event; '
                         'no earlier launched unit may replay.'})
        return rec

    def recover(self, request_id: str, post: Poster) -> Dict[str, Any]:

        import importlib.util
        rec = self.state.get(request_id)
        if not rec:
            raise KeyError(f"no such request: {request_id}")
        if rec.get('execution_engine') == 'attractor':
            import room_attractor
            if rec.get('lifecycle') == 'preview':
                return rec
            payload = Path(rec['attractor_payload'])
            result = room_attractor.invoke(payload, recover=True)
            if result.get('invocation') == 'concurrent_observer':

                post({'type': 'note', 'request_id': request_id,
                      'lifecycle': 'pending', 'observation_only': True,
                      'message': 'Original graph still running; no restart. Its collector owns settlement.'})
                return self.state.get(request_id) or rec
            return room_attractor.settle(self, request_id, payload.parent, result, post)
        root = self._outdir_for(request_id)
        plan_path, bundle_path = root / "_plan.json", root / "_bundle.json"
        bundle = json.loads(bundle_path.read_text()) if bundle_path.is_file() else {}
        run_id = rec.get("run_id")
        if not plan_path.is_file():

            if bundle.get("plan_id") != request_id or bundle.get("run_id") != run_id:
                raise RegistryError("original plan/return contract unavailable; cannot certify late work")
            for unit in bundle.get("units", []):
                if unit.get("status") == "collection_timeout":
                    seat = next((s for s in rec.get("recipients", [])
                                 if s.lower() == unit.get("id")), None)
                    if seat:
                        self.state.set_task(request_id, seat, state="pending",
                                            task_id=unit.get("task_id"), dispatch_id=unit.get("dispatch_id"))
            if any(t.get("state") == "pending" for t in rec.get("tasks", {}).values()):
                self.state.update(request_id, lifecycle="pending")
                self.state.save()
            post({"type": "complete", "request_id": request_id, "lifecycle": rec.get("lifecycle"),
                  "message": "Original return contract was not retained; late success cannot be certified. "
                             "Collector timeout is pending, not proof of worker failure. No workers were launched or released."})
            return rec
        plan = json.loads(plan_path.read_text())
        if plan.get("id") != request_id or (bundle.get("run_id") and bundle["run_id"] != run_id):
            raise RegistryError("original request/Run identity mismatch")
                                                                                           
        validator_path = getattr(self.backend, "dispatcher", skill_root() / "orca-dispatch/dispatcher.py")
        if not Path(validator_path).is_file():
            raise RegistryError(f"shared recovery validator not found at {validator_path}")
        spec = importlib.util.spec_from_file_location("switchboard_return_validator", validator_path)
        import sys
        validator = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = validator
        spec.loader.exec_module(validator)
        evidence = []
        outcomes = {}
        for unit in plan.get("units", []):
            seat = unit.get("seat") or unit["id"].capitalize()
            task = (rec.get("tasks") or {}).get(seat) or {}
            prior = next((u for u in bundle.get("units", []) if u.get("id") == unit["id"]), {})
            task_id = task.get("task_id") or prior.get("task_id")
            dispatch_id = task.get("dispatch_id") or prior.get("dispatch_id")
            outcome = Outcome(seat=seat, state=task.get("state") or "pending",
                              artifacts=task.get("artifacts") or [],
                              task_id=task_id, dispatch_id=dispatch_id)
            outcomes[seat] = outcome
            if outcome.state in {"succeeded", "failed", "contract_rejected", "held", "blocked"}:
                if not (outcome.state == "failed" and prior.get("status") == "collection_timeout"):
                    continue
                outcome.state = "pending"
            if not run_id or not task_id or not dispatch_id:
                outcome.state = "pending"
                continue
            try:
                inspected = self.backend.inspect_completion(run_id, task_id, dispatch_id)
                show, native_task = inspected.get("show") or {}, inspected.get("task") or {}
                dispatch = show.get("dispatch") or {}
                worker = show.get("worker") or {}
                result = native_task.get("result") or {}
                if isinstance(result, str):
                    result = json.loads(result)
                if (dispatch.get("id") != dispatch_id or dispatch.get("taskId") != task_id
                        or dispatch.get("runId") != run_id or native_task.get("id") != task_id
                        or native_task.get("run_id") != run_id or worker.get("dispatchId") != dispatch_id
                        or dispatch.get("status") not in {"completed", "failed"}
                        or worker.get("state") not in {"succeeded", "failed"}
                        or worker.get("state") != result.get("outcome")
                        or result.get("provenance") != "worker_report" or not result.get("messageId")
                        or not dispatch.get("assigneeHandle") or not dispatch.get("completedAt")
                        or result.get("completedBy") != dispatch.get("assigneeHandle")
                        or not completion_times_bound(dispatch, native_task, result)):
                    outcome.state = "pending"
                    continue
                body = str(result.get("body") or "")
                returned = {"body": body, "outcome": result.get("outcome"),
                            "files_modified": result.get("filesModified") or [],
                            "report_path": result.get("reportPath")}
                accepted, reasons = validator.validate_return(body, unit.get("return_contract") or {})
                declared, more = validator.parse_declared_outcome(body, unit)
                outputs_ok, checks, output_reasons = validator.verify_outputs(unit, returned, declared)
                reasons += more + output_reasons
                outcome.state = validator.lane_status(
                    accepted=accepted and outputs_ok and not reasons,
                    worker_outcome=result.get("outcome"),
                    disposition=validator.outcome_disposition(declared, unit))
                outcome.summary = body
                outcome.artifacts = list(result.get("filesModified") or [])
                if result.get("reportPath"):
                    outcome.artifacts.append(result["reportPath"])
                evidence.append({"seat": seat, "native": inspected, "outputs": checks,
                                 "accepted": not reasons, "reasons": reasons})
                self.state.set_task(request_id, seat, state=outcome.state,
                                    task_id=task_id, dispatch_id=dispatch_id,
                                    artifacts=outcome.artifacts, completion_message_id=result["messageId"])
                post({"type": "result", "seat": seat, "state": outcome.state,
                      "summary": body, "artifacts": outcome.artifacts, "contract_reasons": reasons})
            except (OSError, ValueError, RuntimeError) as exc:
                outcome.state = "pending"
                evidence.append({"seat": seat, "error": str(exc)})
        final = lifecycle(outcomes, rec.get("requires_approval", False))
        artifacts = list(dict.fromkeys(p for o in outcomes.values() for p in o.artifacts))
        self.state.update(request_id, lifecycle=final, artifacts=artifacts)
        self.state.save()
        if evidence:
            import hashlib
            content = json.dumps({"request_id": request_id, "run_id": run_id,
                                  "lifecycle": final, "evidence": evidence}, indent=2) + "\n"
            journal = root / ("reconciliation-" + hashlib.sha256(content.encode()).hexdigest()[:16] + ".json")
            if not journal.exists():
                with journal.open("x") as handle:
                    handle.write(content)
        post({"type": "complete", "request_id": request_id, "lifecycle": final,
              "message": (self._pending_line(request_id, outcomes) if final == "pending" else
                          "Original accepted work reconciled." if final == "completed" else
                          f"Original request remains {final}; no workers were launched or released.")})
        return self.state.get(request_id)

    def reply(self, request_id: str, seat: str, answer: str,
              post: Poster) -> Dict[str, Any]:

        rec = self.state.get(request_id)
        if not rec:
            raise KeyError(f"no such request: {request_id}")
        task = (rec.get("tasks") or {}).get(seat)
        if not task or task.get("state") != "awaiting_answer":
            raise ValueError(f"{seat} is not waiting on an answer in {request_id}")

        qid = task.get("question_id")

        if qid and self.backend.resume_question(str(qid), answer):
            self.state.set_task(request_id, seat, state="resumed", answered=True)
            self.state.update(request_id, lifecycle="dispatched")
            self.state.save()
            post({"type": "resumed", "seat": seat,
                  "message": f"{seat} has your answer and is continuing."})
            return self.state.get(request_id)

        post({"type": "note", "seat": seat,
              "message": f"{seat}'s worker had already been released; "
                         f"re-dispatching that seat with your answer."})
        request_file = self._outdir_for(request_id) / "request.md"
        original = request_file.read_text().strip() if request_file.exists() else ""
        sender = Sender(user_id=rec.get("sender") or "local",
                        transport=rec.get("transport") or "local",
                        guild_id=rec.get("guild_id"),
                        channel_id=rec.get("channel_id"))
        follow_up = (f"@{seat} {original}\n\nYou previously asked: "
                     f"{task.get('question','')}\nUser's answer: {answer}")
        carried = self.handle(follow_up, sender, post)
        self._settle_after_reply(request_id, seat, carried, post)
        return carried

    def _settle_after_reply(self, request_id: str, seat: str,
                            carried: Dict[str, Any], post: Poster) -> None:

        follow_up_id = str(carried.get("request_id") or "")
        note = (f"{seat}'s answer was carried into {follow_up_id}, because that "
                f"worker had already been released and could not be resumed in "
                f"place. Read {follow_up_id} for the answer." if follow_up_id else
                f"{seat}'s answer was re-dispatched as a new request.")
        self.state.set_task(request_id, seat, state="resumed", answered=True,
                            followup_request_id=follow_up_id or None)
        rec = self.state.get(request_id) or {}
        states = [t.get("state") for t in (rec.get("tasks") or {}).values()]
        if any(s == "awaiting_answer" for s in states):
            final = "awaiting_user"                                        
        elif states and all(s == "succeeded" for s in states):
            final = "completed"
        elif any(s in ("succeeded", "resumed") for s in states):
            final = "partial"
        else:
            final = "failed"
        self.state.update(request_id, lifecycle=final,
                          notes=list(rec.get("notes") or []) + [note])
        self.state.save()
        post({"type": "complete", "request_id": request_id, "lifecycle": final,
              "message": note})
