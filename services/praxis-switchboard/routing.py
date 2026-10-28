
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:                                                       

    from project_rooms import ProjectContext

import guards
import local
from registry import (Route, Seat, RoutineSpec, RegistryError, RouteRefused, resolve,
                      routine_seat, seats)

MENTION = re.compile(r"@([A-Za-z][A-Za-z0-9_-]{1,31})")

ROUTINE_NAME = "ExcludedRoutine"
STRICT_PRIMARY = re.compile(
    r"\b(?:strict\s+cross-model|require\s+model\s+diversity|"
    r"primary\s+model\s+only|no\s+model\s+fallback)\b", re.IGNORECASE)
DEFAULT_RECIPIENT = "Leo"

@dataclass
class Sender:

    user_id: str
    transport: str = "local"
    guild_id: Optional[str] = None
    channel_id: Optional[str] = None
    display: str = ""

@dataclass
class Allowlist:

    user_ids: List[str] = field(default_factory=list)
    guild_ids: List[str] = field(default_factory=list)
    channel_ids: List[str] = field(default_factory=list)
    trust_local: bool = True

    def authorize(self, sender: Sender) -> Optional[str]:
        if sender.transport == "local":
            return None if self.trust_local else "local transport is disabled"
        if sender.guild_id and self.guild_ids and sender.guild_id not in self.guild_ids:
            return "unauthorized guild"
        if self.guild_ids and not sender.guild_id:
            return "no guild on a remote message"
        if not self.user_ids or sender.user_id not in self.user_ids:
            return "unauthorized sender"
        if self.channel_ids and sender.channel_id and sender.channel_id not in self.channel_ids:
            return "unauthorized channel"
        return None

@dataclass
class Request:
    request_id: str
    sender: Sender
    body: str
    recipients: List[str]
    routes: Dict[str, Route] = field(default_factory=dict)
    verdict: guards.GuardVerdict = field(default_factory=guards.GuardVerdict)
    event_id: Optional[str] = None
    thread_id: Optional[str] = None
    unknown: List[str] = field(default_factory=list)
    refusals: Dict[str, str] = field(default_factory=dict)
    room_id: Optional[str] = None
    room_agenda: Optional[str] = None

    local_answer: Optional[local.LocalAnswer] = None

    routine: Optional[RoutineSpec] = None

    context: Optional["ProjectContext"] = None

    context_room_id: Optional[str] = None
    adaptive_planner: str = ""
    execution_handoff: str = ""
    continuation: dict = field(default_factory=dict)
    continuation_hold: str = ""
    chair_name: str = DEFAULT_RECIPIENT

    @property
    def is_group(self) -> bool:
        return len(self.recipients) > 1

    @property
    def chaired(self) -> bool:

        return (not self.execution_handoff and self.is_group
                and self.chair_name in self.recipients)

def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]

def parse_recipients(text: str, known: Dict[str, Seat]) -> tuple:

    index = {name.lower(): name for name in known}
    for canonical, seat in known.items():
        for alias in seat.aliases:
            key = alias.strip().lower()
            if key and key in index and index[key] != canonical:
                raise ValueError(
                    f"address alias @{alias} is assigned to both "
                    f"{index[key]} and {canonical}"
                )
            if key:
                index[key] = canonical
    recipients: List[str] = []
    unknown: List[str] = []
    body = text or ""

    while True:
        m = MENTION.match(body.lstrip())
        if not m:
            break
        body = body.lstrip()[m.end():]
        token = m.group(1).lower()
        if token in index:
            canonical = index[token]
            if canonical not in recipients:
                recipients.append(canonical)
        else:
            unknown.append(m.group(1))

    if not recipients and not unknown:
        recipients = [DEFAULT_RECIPIENT]
    return recipients, body.strip(), unknown

def build(text: str, sender: Sender, *, allowlist: Optional[Allowlist] = None,
          event_id: Optional[str] = None, thread_id: Optional[str] = None,
          request_id: Optional[str] = None, log_routes: bool = True,
          room_id: Optional[str] = None, room_agenda: Optional[str] = None,
          context_room_id: Optional[str] = None,
          context: Optional[object] = None,
          adaptive_entry: bool = False,
          explicit_model: Optional[str] = None,
          task_mode: Optional[str] = None,
          continuation_resolver=None,
          spark: Optional[local.Spark] = None) -> Request:

    allowlist = allowlist or Allowlist()
    rid = request_id or new_request_id()
    known = seats()

    denial = allowlist.authorize(sender)
    if denial:
        req = Request(request_id=rid, sender=sender, body="", recipients=[],
                      event_id=event_id, thread_id=thread_id,
                      room_id=room_id, room_agenda=room_agenda)
        req.verdict = guards.GuardVerdict(allowed=False, reasons=[denial])
        return req

    recipients, body, unknown = parse_recipients(text, known)
    req = Request(request_id=rid, sender=sender, body=body, recipients=recipients,
                  event_id=event_id, thread_id=thread_id, unknown=unknown,
                  room_id=room_id, room_agenda=room_agenda)

    import room_commands
    command = room_commands.token(body)
    import runner
    lifecycle_command = recipients == ['Leo'] and not unknown and runner.leo_lifecycle(room_commands.body(body))
    internal_adaptive = (command == '/adaptive' and (adaptive_entry or (context_room_id is not None
                         and body.startswith('You are answering one prompt inside '))))
    if command and not internal_adaptive and not lifecycle_command:
        req.verdict = guards.GuardVerdict(allowed=False, reasons=[
            'room_command_not_dispatched: '+command])
        return req

    req.verdict = guards.screen(body)
    if not req.verdict.allowed:
        return req

    if context is not None:
        try:
            req.context = _resolve_context(context)
        except Exception as exc:                                              
            req.verdict = guards.GuardVerdict(allowed=False, reasons=[str(exc)])
            return req

    if context_room_id or room_id:
        try:
            import rooms
            room = rooms.resolve(context_room_id or room_id)
            if context_room_id and req.context is None:
                raise RegistryError("a context room must carry its canonical context")
            if context_room_id and room_id and room.room_id != rooms.resolve(room_id).room_id:
                raise RegistryError("conflicting room identities")
            outside = set(recipients) - set(room.recipients)
            if outside:
                raise RegistryError(f"not in {room.title}: {', '.join(sorted(outside))}")
            req.context_room_id = room.room_id if context_room_id else None
            req.chair_name = room.chair
            import room_adaptive
            adaptive_intent = room_adaptive.intent(body) if context_room_id and recipients == [room.chair] else ''
            if adaptive_intent:
                if 'Leo' not in room.recipients:
                    raise RegistryError("adaptive_planner_unavailable: ordinary room requires canonical Leo")
                room_adaptive.require_runtime()
                req.adaptive_planner = 'Athena'
                req.execution_handoff = adaptive_intent
                recipients[:] = ['Athena', 'Leo']
            elif context_room_id and recipients == [room.chair] and room.chair != "Leo":
                import project_rooms
                req.execution_handoff = project_rooms.internal_execution_scope(body)
                import room_continuation
                selector = room_continuation.credit_followup(body)
                if selector is not None:
                    try:
                        if continuation_resolver is None:
                            raise RegistryError("continuation needs the room's durable pending-work binding")
                        req.continuation = continuation_resolver(req.context, room.room_id, selector)
                        req.execution_handoff = room_continuation.prompt_body(body)
                    except RegistryError as exc:
                        req.continuation_hold = str(exc)
                if req.execution_handoff:
                    if "Leo" not in room.recipients:
                        raise RegistryError("internal execution requires canonical Leo in the room")
                    recipients.append("Leo")
        except RegistryError as exc:
            req.verdict = guards.GuardVerdict(allowed=False, reasons=[str(exc)])
            return req

    if adaptive_entry and command == '/adaptive' and context_room_id is None:
        try:
            import room_adaptive
            if req.context is None or req.context.kind != 'global-workspace':
                raise RegistryError('adaptive global entry requires canonical global workspace context')
            adaptive_intent = room_adaptive.intent(body)
            if adaptive_intent:
                room_adaptive.require_runtime()
                req.adaptive_planner = 'Athena'
                req.execution_handoff = adaptive_intent
                recipients[:] = ['Athena', 'Leo']
        except RegistryError as exc:
            req.verdict = guards.GuardVerdict(allowed=False, reasons=[str(exc)])
            return req

    if command and not req.execution_handoff and not lifecycle_command:
        req.verdict = guards.GuardVerdict(allowed=False, reasons=['room_command_not_dispatched: '+command])
        return req

    if task_mode:
        try:
            if unknown:
                raise RegistryError("cannot select a task mode for an unknown recipient")
            known = {**known, **{name: known[name].for_task_mode(task_mode)
                                for name in recipients}}
        except RegistryError as exc:
            req.verdict = guards.GuardVerdict(allowed=False, reasons=[str(exc)])
            return req

    addressed = bool(unknown) or any(n != DEFAULT_RECIPIENT for n in recipients)

    for probe in (() if task_mode else local.PROBES):
        outcome = probe(body, addressed=addressed, spark=spark)
        if outcome is None:
            continue
        if isinstance(outcome, local.LocalAnswer):
            req.local_answer = outcome
            req.recipients = []
            return req
        req.routine = outcome
        req.recipients = recipients = [ROUTINE_NAME]
        break

    require_primary = bool(STRICT_PRIMARY.search(body))
    for name in list(recipients):
        seat = (routine_seat(req.routine, name=name)
                if req.routine and name == ROUTINE_NAME else known[name])
        try:
            req.routes[name] = resolve(
                seat, request_id=rid, log=log_routes,
                require_primary=require_primary,
                explicit_model=explicit_model)
        except RouteRefused as exc:

            req.refusals[name] = str(exc)
            req.recipients.remove(name)
    return req

def _resolve_context(context: object) -> "ProjectContext":

    import project_rooms                                                      
    if isinstance(context, project_rooms.ProjectContext):
        return context
    return project_rooms.resolve_context(str(context))

def acknowledgement(req: Request) -> str:

    if not req.verdict.allowed:
        return "Not dispatched. " + "; ".join(req.verdict.reasons)
    if req.local_answer is not None:
        return req.local_answer.text
    names = req.recipients
    if req.unknown:
        prefix = f"No seat named {', '.join('@' + u for u in req.unknown)}. "
    else:
        prefix = ""
    if not names:
        return prefix + "Nobody could be reached. " + "; ".join(req.refusals.values())
    if req.routine and names == [ROUTINE_NAME]:
        return "Not dispatched. Routine entrypoints are excluded from this public runtime."
    elif len(names) == 1:
        line = f"{names[0]} has it."
    elif req.chaired:
        speakers = ", ".join(name for name in names if name != req.chair_name)
        line = f"{speakers} will answer first; {req.chair_name} will chair the synthesis."
    elif req.execution_handoff:
        planner = "Athena" if req.execution_handoff.startswith("GENERAL ADAPTIVE WORKFLOW\n") else req.chair_name
        line = f"{planner} will scope the internal work; Leo will execute after the accepted handoff."
    elif len(names) == 2:
        line = f"{names[0]} and {names[1]} are working in parallel."
    else:
        line = f"{', '.join(names[:-1])}, and {names[-1]} are working in parallel."
    if req.refusals:
        line += " " + "; ".join(req.refusals.values())
    if req.verdict.requires_approval:
        line += " This needs your approval before anything leaves the building."
    return prefix + line
