
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import project_rooms
import room_continuation
import named_launch
from registry import RegistryError, Route, inherited_skill_paths, institution, skill_root
from routing import Request

EventSink = Callable[[Dict[str, Any]], None]

def _agent_home_guard() -> Any:

    import importlib.util

    candidates = [
        Path.home() / ".claude" / "hooks" / "agent_home_guard.py",
        Path(__file__).resolve().parents[2] / "hooks" / "agent_home_guard.py",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("praxis_agent_home_guard", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise RuntimeError(
        "agent-home policy not found at "
        + " or ".join(str(p) for p in candidates)
        + "; configure the required runtime guard; see services/praxis45/PUBLIC-RUNTIME.md")

def launch_env() -> Dict[str, str]:

    return _agent_home_guard().normalized_env()

WORKER_PREAMBLE = """Vault root: ${vault_root}. This is a bounded request for your configured seat. Load and obey its charter and decision rights. Use only sources authorized for the request and report missing access or evidence. Access is not effect authority: perform only commissioned internal work under the loaded orchestration contract. This surface has no contract for external actions. Preserve the configured task budget and report actual outcomes and unresolved limitations."""

def model_note(route) -> str:

    if route.explicit_model:
        return (
            "\n\nMODEL OVERRIDE — DISCLOSE THIS IN YOUR ANSWER\n\n"
            f"User explicitly selected `{route.effective_model}` for this one request. "
            "Open your answer by identifying this as a provider-specific comparison. "
            "The seat and its charter are unchanged, and the standing registry route is unchanged."
        )
    fb = route.fallback or {}
    if not fb:
        return ""
    return (
        "\n\nMODEL SUBSTITUTION — DISCLOSE THIS IN YOUR ANSWER\n\n"
        f"This seat declares {fb.get('from') or 'its primary model'}. You are running on "
        f"{fb.get('to') or 'a different model'} instead; the router recorded the reason as "
        f"`{fb.get('because') or 'unavailable'}`. The charter still binds and you "
        "are still this seat, but the model behind it is not the one the seat was chosen around. "
        "Open your answer with one line naming both models and the reason. Do not let the "
        "substitution pass silently: whether the work stands on this model is User's call, "
        "and the user needs that information to assess the result."
    )

SPEC = """You are {name}. Load and obey {charter} — the charter, its standing questions, and its boundaries. Then load {rights}.{memory}{model_note}

SKILL-INHERITANCE CONTRACT

Switchboard validated the seat's complete live registry before launch. The
paths below are the complete provider-correct set applicable to this request,
not suggestions and not a prose summary:

{skill_manifest}

Inspect every listed entrypoint before substantive work. Read the complete SKILL.md for
each skill whose scope applies to this request, then read every task-relevant
file that SKILL.md requires. Do not claim to have used a skill merely because
its name appears here. If a listed path cannot be read, stop and report the
configuration failure instead of improvising a partial role. In your handoff,
name the skills actually loaded.

When the request needs current or external evidence, use the runtime's native
web-search and page-fetch capabilities and preserve URLs, publication dates,
and access dates. A site-specific scraper or a local filename search is not full
web search. If native search is unavailable, state that limitation; do not turn
"no hit" from a partial search into an absence claim.

{context}

REQUEST FROM USER:

{body}

{company}

Answer from your configured seat within the request scope, source permissions,
and loaded rights. Report unavailable or unauthorized evidence explicitly.
Preserve consequential judgment for the owning seat: report unresolved
substantive questions rather than deciding them. This surface has no outbound
action contract for sending, submitting, publishing, spending, signing off, or
making a commitment. A direct request to implement internal work authorizes
bounded reversible code, tests and local artifacts plus the necessary actual
named workers under the existing orchestration contract; complete that work
and verify it. Planning or review alone grants no implementation authority.

Write substantial working artifacts under {outdir} and reference their paths.
Use the output format and delivery directory explicitly configured for this request.
If no human-facing delivery location is configured, report that limitation and
provide the local artifact path. Do not claim that a local file was delivered.
Keep the handoff concise. An optional proposed learning may be included only
when the request or loaded procedure calls for it; this does not update memory.

Report through worker_done in up to three sentences: what you did, the single
thing User most needs to know, and anything genuinely left outstanding.
The worker_done envelope already supplies task identity and completion state;
do not repeat lifecycle identifiers in prose."""

CHAIR_SPEC = """You are {name}, chairing. Load and obey {charter} and {rights}.{model_note}

SKILL-INHERITANCE CONTRACT

The live role registry assigns every skill below to this seat. Inspect every
entrypoint, read in full each skill applicable to this request plus its required
task-relevant resources, and name the skills loaded in the chair record. A
missing path is a configuration failure; stop rather than improvising.

{skill_manifest}

{context}

THE QUESTION USER ASKED:

{body}

{speakers} answered it independently and have finished. Read every file they wrote under {outdir} before writing anything. Those files are their work on the canonical context named above and carry its digest; a directory holding work on any other context is not this room's output and must be reported rather than read as if it were.

Write the record. Four parts, in this order:

1. **Agreed** — only what genuinely converged, each line attributed. If little converged, this section is short; do not inflate it.
2. **Contested** — each live disagreement: who, what each side claims, and what evidence would settle it. This is the most valuable part. Preserve it.
3. **Decisions owed by User** — stated as a choice between named options, not as a topic.
4. **Action items** — who owes what.

Do not resolve a contested point by picking a side or splitting the difference. Do not average the room into a house view. A chair who synthesizes away the disagreement has destroyed the reason for asking several people.

Write the record to {outdir}/chair-record.md and reference its path.
Use any human-facing format and delivery directory explicitly configured for
this request. Report missing delivery configuration; do not claim local files
have been delivered. Keep the handoff concise.

Report through worker_done in up to three sentences, naming what the room
settled and what remains contested. The worker_done envelope already supplies
task identity and completion state; do not repeat lifecycle identifiers in
prose."""

def clean_summary(body: str, sentinel: str) -> str:

    text = (body or "").replace("\\n", "\n").replace("\\t", "\t")

    if sentinel.strip():
        text = text.replace(sentinel.strip(), "")
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip()).strip()

@dataclass
class Outcome:
    seat: str
    state: str = "pending"                                                            
    model: str = ""
    fallback: Optional[Dict[str, str]] = None
    summary: str = ""
    artifacts: List[str] = None
    dispatch_id: Optional[str] = None
    task_id: Optional[str] = None
    question: str = ""
    question_id: Optional[str] = None
    effort: str = ""
    task_class: str = ""
    task_mode: str = ""
    model_source: str = "planned"

    def receipt(self) -> Dict[str, Any]:
        return {"model": self.model, "effort": self.effort,
                "task_class": self.task_class, "task_mode": self.task_mode,
                "model_source": self.model_source, "fallback": self.fallback}

    def attest_launch(self, launch: Optional[Dict[str, Any]], source: str) -> bool:
        if not isinstance(launch, dict) or not launch.get("model"):
            return False
        before = (self.model, self.effort, self.model_source != "planned")
        self.model = launch["model"]
        self.effort = launch.get("effort") or ""
        self.model_source = source
        return (self.model, self.effort, True) != before

    def __post_init__(self):
        if self.artifacts is None:
            self.artifacts = []

class OrcaBackend:

    def execute(self, plan: Dict[str, Any], bundle_path: Path) -> Iterator[Dict[str, Any]]:
        raise NotImplementedError

    def resume_question(self, question_id: str, answer: str) -> bool:

        return False

    def inspect_completion(self, run_id: str, task_id: str, dispatch_id: str) -> Dict:

        return {}

class DispatcherBackend(OrcaBackend):
    def __init__(self, dispatcher: Optional[Path] = None):
        self.dispatcher = Path(dispatcher) if dispatcher else skill_root() / "orca-dispatch" / "dispatcher.py"

    def resume_question(self, question_id: str, answer: str) -> bool:
        try:
            proc = subprocess.run(
                ["orca", "orchestration", "reply", "--id", str(question_id),
                 "--body", answer, "--json"],
                capture_output=True, text=True, timeout=30, env=launch_env())
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0:
            return False
        try:
            return bool(json.loads(proc.stdout or "{}").get("ok"))
        except ValueError:
            return False

    def inspect_completion(self, run_id: str, task_id: str, dispatch_id: str) -> Dict:
        def read(args):
            proc = subprocess.run(["orca", "orchestration", *args, "--json"],
                                  capture_output=True, text=True, timeout=30, env=launch_env())
            payload = json.loads(proc.stdout or "{}")
            if proc.returncode or payload.get("ok") is not True:
                raise RuntimeError(f"native inspection failed: {payload.get('error')}")
            return payload.get("result") or {}
        show = read(["worker-show", "--dispatch", dispatch_id])
        tasks = read(["task-list", "--run", run_id])
        task = next((t for t in tasks.get("tasks", []) if t.get("id") == task_id), {})
        return {"show": show, "task": task}

    def execute(self, plan: Dict[str, Any], bundle_path: Path) -> Iterator[Dict[str, Any]]:
        if not self.dispatcher.exists():
            raise FileNotFoundError(f"shared dispatcher not found at {self.dispatcher}")

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         prefix="switchboard-plan-") as fh:
            json.dump(plan, fh, indent=2)
            plan_path = Path(fh.name)

        cmd = [sys.executable, str(self.dispatcher), "run",
               "--plan", str(plan_path), "--output", str(bundle_path),
               "--run-key", f"praxis-switchboard/{plan['id']}"]

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=launch_env())
        tail = []
        try:
            for line in proc.stderr:
                line = line.strip()
                tail.append(line)
                del tail[:-40]
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("event"):
                    yield event
            proc.wait()
        finally:
            if proc.stderr is not None:
                proc.stderr.close()
            plan_path.unlink(missing_ok=True)
        if bundle_path.exists():
            yield {"event": "bundle", "bundle": json.loads(bundle_path.read_text())}
        else:
            yield {"event": "dispatch_failed", "error": "\n".join(tail)[-600:]}

def seat_memory(role_id: str) -> str:

    path = institution().parent / "agents" / role_id.lower() / "KNOWLEDGE.md"
    return (f" Your accumulated seat knowledge from earlier dispatches is at {path};"
            f" read it after the charter and apply what it holds.") if path.is_file() else ""

def leo_lifecycle(body):
    return False

def leo_lifecycle_commands():
    return frozenset()

def request_skill_paths(req: Request, route: Route, name: str) -> List[Path]:
    return inherited_skill_paths(route)

def dispatcher_module(backend: OrcaBackend):

    import importlib.util
    path = getattr(backend, 'dispatcher', skill_root() / 'orca-dispatch/dispatcher.py')
    spec = importlib.util.spec_from_file_location('switchboard_consumer_validator', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def build_plan(req: Request, outdir: Path, *, serial: bool = False) -> Dict[str, Any]:

    inst = institution()
    rights = inst / "DECISION-RIGHTS.md"
    if req.routine is not None:
        return _routine_plan(req, outdir)
    chair = req.chair_name if req.chaired else None
    speakers = [n for n in req.recipients if n != chair]
    others = list(speakers)
    units = []

    context = req.context
    context_ref = ""
    if context is not None:
        context_ref = (f"{context.kind} {project_rooms.context_identity(context)} "
                       f"· digest {project_rooms.context_digest(context)}")
    for name in speakers:
        route = req.routes[name]
        skill_manifest = "\n".join(
            f"- `{path}`" for path in request_skill_paths(req, route, name)
        ) or "- No additional skill is registered for this seat."
        company = ""
        if len(others) > 1 and not req.execution_handoff and not serial:
            peers = [o for o in others if o != name]
            company = (
                f"{', '.join(peers)} received this same request in parallel. You cannot see their "
                f"answers and should not wait for them: answer independently from your own seat. "
                f"User wants the disagreement, not a consensus."
            )
        values = {
            "name": name,
            "charter": str(inst / route.seat.charter),
            "rights": str(rights),
            "memory": seat_memory(route.seat.role_id),
            "model_note": model_note(route),
            "skill_manifest": skill_manifest,
            "context": project_rooms.context_block(context, route),
            "body": req.body,
            "company": company,
            "outdir": str(outdir),
            "sentinel": f"{name} done: {req.request_id}",
        }
        spec = SPEC.format(**values)
        units.append({
            "id": name.lower(),
            "seat": route.seat.name,
            "task_mode": route.seat.task_mode,
            "seat_request": req.body,
            "title": f"{name} — {req.request_id}",
            "task_class": route.seat.route,
            **({"requested_model": route.effective_model}
               if route.explicit_model else {}),
            "sensitivity": "confidential",
            "risk": "medium",
            "data_domains": ["institutional"],
            "spec": spec,

            "return_contract": {"min_chars": 10, "max_chars": 4000},
        })
    if chair:

        route = req.routes[chair]
        skill_manifest = "\n".join(
            f"- `{path}`" for path in inherited_skill_paths(route)
        ) or "- No additional skill is registered for this seat."
        values = {
            "name": chair,
            "charter": str(inst / route.seat.charter),
            "rights": str(rights),
            "model_note": model_note(route),
            "skill_manifest": skill_manifest,
            "context": project_rooms.context_block(context, route),
            "body": req.body,
            "speakers": ", ".join(speakers),
            "outdir": str(outdir),
            "sentinel": f"{chair} done: {req.request_id}",
        }
        spec = CHAIR_SPEC.format(**values)
        if serial:
            spec = spec.replace('answered it independently and have finished.',
                                'completed their assigned serial stages.')
        units.append({
            "id": chair.lower(),
            "seat": route.seat.name,
            "task_mode": route.seat.task_mode,
            "seat_request": req.body,
            "title": f"{chair} — chair {req.request_id}",
            "task_class": route.seat.route,
            **({"requested_model": route.effective_model}
               if route.explicit_model else {}),
            "sensitivity": "confidential",
            "risk": "high",
            "data_domains": ["institutional"],
            "depends_on": [n.lower() for n in speakers],
            "spec": spec,
            "return_contract": {"min_chars": 10, "max_chars": 4000},
        })

    if req.execution_handoff:
        if req.adaptive_planner:
            plan_marker = 'Adaptive planner: Athena (adaptive_planner; not an added room member).'
            plan_marker += '\nGraph authorizing_event: '+json.dumps({'request_id': req.request_id, 'event_id': req.event_id or req.request_id, 'user_event_sha256': hashlib.sha256(req.body.encode()).hexdigest(), 'context_room_id': req.context_room_id, 'context_digest': project_rooms.context_digest(context) if context else None}, sort_keys=True)
            plan_marker += '\nCopy this exact authorizing_event into the logical graph for effects; quote the exact objective in every effect authority.'
            for unit in units:
                unit['spec'] += '\n'+plan_marker
        if req.continuation:
            room_continuation.verify(context, req.context_room_id, req.continuation)
        handoff = outdir / "internal-handoff.md"
        planner = "Athena" if req.execution_handoff.startswith("GENERAL ADAPTIVE WORKFLOW\n") else req.chair_name
        chair_unit = next(u for u in units if u["seat"] == planner)
        leo_unit = next(u for u in units if u["seat"] == "Leo")
        if req.continuation:
            bindings = [req.continuation[name] for name in
                        ('binding', 'instruction', 'handoff', 'plan', 'attempt_evidence')]
            chair_unit['source_bindings'] = bindings
            leo_unit['source_bindings'] = bindings
        chair_unit["spec"] += (
            "\n\nINTERNAL EXECUTION HANDOFF\n"
            "Scope only the explicit internal instruction below, without implementing it. "
            "Approval comes from this actual user instruction, never your own claim of approval. "
            "If scope, acceptance, write boundaries or authority are ambiguous, report held. "
            "Write the objective, concrete changes, acceptance checks, write boundaries, "
            "excluded effects and unresolved issues to " + str(handoff) + ". "
            "Declare exactly one closing line ROOM_HANDOFF: created or ROOM_HANDOFF: held; "
            "created means the handoff is written and no unresolved decision is needed "
            "for this bounded internal work.\n"
            + req.execution_handoff + "\n" +
            (room_continuation.packet(req.continuation) if req.continuation else ""))
        chair_unit["return_contract"]["outcome_contract"] = {
            "prefix": "ROOM_HANDOFF:", "allowed": ["created", "held"],
            "substantive": ["created"], "hold": ["held"]}
        chair_unit["return_contract"]["required_substrings"] = ["ROOM_HANDOFF:"]
        chair_unit["outputs"] = [{"path": str(handoff),
                                  "required_when_outcome": ["created"]}]
        leo_unit["depends_on"] = [chair_unit["id"]]
        if req.execution_handoff.startswith("GENERAL ADAPTIVE WORKFLOW\n"):
            graph = outdir / "adaptive-logical-graph.json"
            policy = outdir / "adaptive-policy.json"
            chair_unit["spec"] += (
                "\nSave the actual graph to " + str(graph) + " and finite policy to " + str(policy)
                + ". Both are mandatory outputs of created, alongside the scope handoff. "
                "Preserve a supplied graph exactly; otherwise author the graph now. "
                "Do not return created with only a proposal or a path to a missing graph.")
            chair_unit["outputs"] += [{"path": str(p), "required_when_outcome": ["created"]}
                                      for p in (graph, policy)]
            entry = Path(__file__).resolve().parents[1] / "praxis45" / "adaptive_general.py"
            import room_adaptive
            runtime = room_adaptive.require_runtime()
            leo_unit["spec"] += '\nResolved adaptive runtime: '+json.dumps(runtime,sort_keys=True)
            leo_unit["spec"] += (
                "\nUse the exact reviewed entry " + str(entry) + " under the resolved adaptive runtime Python. "
                "The accepted predecessor supplies the graph automatically at " + str(graph)
                + " and policy at " + str(policy) + ". Read these exact accepted files, "
                "select their one graph ID, create a fresh isolated root, then compile and "
                "execute through the reviewed adaptive_general.py with the pinned engine. "
                "Do not ask User to create, copy, select or transfer graph files. "
                "Do not leave this accepted dependent Task unstarted or stop after compilation.")
        leo_unit["spec"] += (
            "\n\nAUTHORIZED INTERNAL EXECUTION\n"
            "The existing native Task dependency requires the chair's accepted created handoff. "
            "Read " + str(handoff) + "; compare its scope against the actual user instruction "
            "below before any write or dispatch. An absent/empty/ambiguous handoff or any "
            "scope expansion is a blocker, never permission. Execute the bounded internal "
            "work now with the necessary actual canonical agents, test and reconcile their "
            "results; do not stop at another handoff document. No external effects, deployment "
            "or new authority are granted.\n" + req.execution_handoff + "\n" +
            (room_continuation.packet(req.continuation) if req.continuation else ""))

    if any(unit['seat'] in {'Athena', 'Leo'} for unit in units):
        budget = (Path(__file__).resolve().parents[2] /
                  'skills/chief-of-staff/references/task-budget.md').read_text()
        for unit in units:
            if unit['seat'] in {'Athena', 'Leo'}:
                unit['spec'] += '\n\n' + budget

    for unit in units:
        route = req.routes[unit["seat"]]
        try:
            named_launch.construct(unit, route.agent)
        except named_launch.NamedLaunchError as exc:
            raise RegistryError(str(exc)) from exc

    return {
        "schema_version": "1",
        "id": req.request_id,
        "objective": f"Switchboard request {req.request_id} to "
                     f"{', '.join(req.recipients)}: {req.body[:160]}",
        "praxis_task_id": "PX4-SWITCHBOARD",
        "defaults": {"worktree": "current", "start_timeout_ms": 60000,
                     "wait_window_ms": 5000, "overall_timeout_ms": 1800000},
        "worker_preamble": WORKER_PREAMBLE,

        "context": ({"kind": context.kind,
                     "title": context.title,
                     "note": str(context.note),
                     "identity": project_rooms.context_identity(context),
                     "digest": project_rooms.context_digest(context)}
                    if context is not None else None),
        "units": units,
    }

def _routine_plan(req: Request, outdir: Path) -> Dict[str, Any]:
    raise RegistryError("Routine entrypoints are excluded from this public runtime")

def run(req: Request, backend: OrcaBackend, sink: EventSink,
        *, outdir: Optional[Path] = None, plan: Optional[dict] = None) -> Dict[str, Outcome]:

    outdir = outdir or (institution() / "switchboard" / req.request_id)
    outdir.mkdir(parents=True, exist_ok=True)
    bundle_path = outdir / "_bundle.json"

    (outdir / "request.md").write_text(req.body)

    outcomes: Dict[str, Outcome] = {
        n: Outcome(seat=n, model=req.routes[n].effective_model,
                   fallback=req.routes[n].fallback, effort=req.routes[n].effort,
                   task_class=req.routes[n].seat.route,
                   task_mode=req.routes[n].seat.task_mode) for n in req.recipients
    }
    plan = plan if plan is not None else build_plan(req, outdir)

    (outdir / "_plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    for event in backend.execute(plan, bundle_path):
        kind = event.get("event")
        unit = event.get("unit")
        seat = next((n for n in outcomes if n.lower() == str(unit).lower()), None)

        if kind == "run_created":

            sink({"type": "run", "run_id": event.get("run_id")})
        elif kind == "worker_started" and seat:
            outcomes[seat].dispatch_id = event.get("dispatch_id")
            outcomes[seat].task_id = event.get("task_id")
            outcomes[seat].attest_launch(event.get("launch"), "worker_start_receipt")
            sink({"type": "started", "seat": seat,
                  "task_id": outcomes[seat].task_id,
                  "dispatch_id": outcomes[seat].dispatch_id, **outcomes[seat].receipt()})
        elif kind == "unit_blocked" and seat:
            outcomes[seat].state = "blocked"
            sink({"type": "result", "seat": seat, "state": "blocked",
                  "summary": event.get("reason") or "Dependency did not succeed."})
        elif kind == "launch_failed" and seat:
            outcomes[seat].state = "failed"
            sink({"type": "failed", "seat": seat,
                  "reason": event.get("reason") or event.get("error") or kind})
        elif kind == "worker_done" and seat:

            accepted = bool(event.get("accepted"))
            outcomes[seat].state = ("succeeded" if event.get("outcome") == "succeeded" and accepted
                                    else "contract_rejected" if event.get("outcome") == "succeeded"
                                    else "held" if event.get("outcome") == "held" and accepted
                                    else "failed")
            outcomes[seat].summary = clean_summary(event.get("body") or "",
                                                   f"{seat} done: {req.request_id}")
            sink({"type": "result", "seat": seat, "state": outcomes[seat].state,
                  **outcomes[seat].receipt(), "summary": outcomes[seat].summary,
                  "artifacts": outcomes[seat].artifacts, "contract_reasons": []})
        elif kind in ("question", "escalation") and seat:
                                                                         
            outcomes[seat].state = "awaiting_answer"
            outcomes[seat].question = event.get("body") or ""
            outcomes[seat].question_id = event.get("message_id")
            sink({"type": "question", "seat": seat, "message": event.get("body") or "",
                  "message_id": event.get("message_id"),
                  "reply_hint": f"switchboard.py reply {req.request_id} {seat} \"<answer>\""})
        elif kind == "bundle":
            for u in (event.get("bundle") or {}).get("units", []):
                s = next((n for n in outcomes if n.lower() == str(u.get("id")).lower()), None)
                if not s:
                    continue
                receipt_changed = outcomes[s].attest_launch(
                    u.get("actual_launch"), "bundle_launch_receipt")
                contract = u.get("contract") or {}
                done = (u.get("worker_done") or {})
                body = done.get("body") or done.get("summary") or ""
                accepted = bool(contract.get("accepted"))
                for key in ("task_id", "dispatch_id"):
                    if u.get(key):
                        setattr(outcomes[s], key, u[key])
                status = u.get("status")
                settled = ("pending" if status in {"collection_timeout", "created", "dispatched", "pending"}
                           else "succeeded" if status == "succeeded" and accepted
                           else "contract_rejected" if status in {"succeeded", "contract_rejected"}
                           else "held" if status == "held"
                           else "blocked" if status == "blocked" else "failed")
                if outcomes[s].state == "awaiting_answer":

                    continue
                if settled == "pending":
                    outcomes[s].state = "pending"
                    sink({"type": "pending", "seat": s, "state": "pending",
                          "task_id": outcomes[s].task_id, "dispatch_id": outcomes[s].dispatch_id,
                          "message": "Collection ended; original work remains unsettled."})
                    continue
                already_streamed = outcomes[s].state in (
                    "succeeded", "failed", "contract_rejected")
                previous_state = outcomes[s].state
                outcomes[s].state = settled
                outcomes[s].summary = clean_summary(
                    body, f"{s} done: {req.request_id}") or outcomes[s].summary
                outcomes[s].artifacts = [str(p) for p in (done.get("files_modified") or [])]
                if done.get("report_path") and done["report_path"] not in outcomes[s].artifacts:
                    outcomes[s].artifacts.append(done["report_path"])
                if already_streamed and previous_state == settled:

                    if outcomes[s].artifacts or contract.get("reasons") or receipt_changed:
                        sink({"type": "detail", "seat": s, "state": settled,
                              **outcomes[s].receipt(), "receipt_changed": receipt_changed,
                              "artifacts": outcomes[s].artifacts,
                              "contract_reasons": contract.get("reasons") or []})
                else:
                    sink({"type": "result", "seat": s, "state": settled,
                          **outcomes[s].receipt(), "summary": outcomes[s].summary,
                          "artifacts": outcomes[s].artifacts,
                          "contract_reasons": contract.get("reasons") or []})
        elif kind == "dispatch_failed":
            for s in outcomes:
                if outcomes[s].state == "pending":
                    outcomes[s].state = "failed"
            sink({"type": "dispatch_failed", "error": event.get("error", "")})
    return outcomes

def lifecycle(outcomes: Dict[str, Outcome], requires_approval: bool) -> str:

    states = [o.state for o in outcomes.values()]
    if not states:
        return "failed"
    if any(s == "awaiting_answer" for s in states):
        return "awaiting_user"
    if any(s in {"pending", "started", "resumed"} for s in states):
        return "pending"
    if any(s == "held" for s in states):
        return "awaiting_user"
    if requires_approval and any(s == "succeeded" for s in states):
        return "awaiting_user"
    if all(s == "succeeded" for s in states):
        return "completed"
    if any(s == "succeeded" for s in states):
        return "partial"
    return "failed"
