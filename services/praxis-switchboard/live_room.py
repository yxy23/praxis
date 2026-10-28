
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import project_rooms
import registry
import rooms

WAIT_WINDOW_MS = 30_000
OVERALL_TIMEOUT_SECONDS = 600
TRANSCRIPT_LIMIT = 24_000

class LiveRoomError(registry.RegistryError):
    pass

def _payload_result(payload: Dict) -> Dict:
    if not isinstance(payload, dict) or payload.get("ok") is False:
        error = payload.get("error") if isinstance(payload, dict) else payload
        raise LiveRoomError(f"Orca refused the live-room operation: {error}")
    result = payload.get("result")
    return result if isinstance(result, dict) else payload

def _message_payload(message: Dict) -> Dict:
    raw = message.get("payload")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}

def _object_id(payload: Dict, key: str) -> str:
    direct = payload.get(f"{key}Id") or payload.get(f"{key}_id")
    if direct:
        return str(direct)
    nested = payload.get(key)
    if isinstance(nested, dict):
        value = nested.get("id") or nested.get(f"{key}Id")
        if value:
            return str(value)
    raise LiveRoomError(f"Orca receipt did not contain a {key} id")

def _terminal_handle(payload: Dict) -> str:
    for key in ("agentTerminalHandle", "agent_terminal_handle"):
        if payload.get(key):
            return str(payload[key])
    worker = payload.get("worker")
    if isinstance(worker, dict):
        for key in ("agentTerminalHandle", "agent_terminal_handle"):
            if worker.get(key):
                return str(worker[key])
    for effect in payload.get("effects") or []:
        if (effect.get("kind") == "terminal" and
                effect.get("role") == "agent" and effect.get("id")):
            return str(effect["id"])
    raise LiveRoomError("Orca worker-start did not attest an agent terminal")

def _launch(payload: Dict) -> Dict:
    paths = [
        ("launch", "effective"),
        ("startOptions", "launch", "effective"),
        ("start_options", "launch", "effective"),
        ("worker", "startOptions", "launch", "effective"),
    ]
    for path in paths:
        value = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if isinstance(value, dict):
            return value
    raise LiveRoomError("worker-start receipt did not attest the effective launch")

def _validate_launch(receipt: Dict, route: registry.Route) -> Dict:
    launch = _launch(receipt)
    expected = {
        "agent": route.agent,
        "model": route.launch_target,
        "effort": route.effort,
    }
    differences = [
        f"{key}: expected {value!r}, got {launch.get(key)!r}"
        for key, value in expected.items() if launch.get(key) != value
    ]
    if differences:
        raise LiveRoomError("Orca launch attestation failed: " + "; ".join(differences))
    return launch

class OrcaCLI:

    def __init__(self, command: Optional[str] = None):
        selected = command or os.environ.get("ORCA_CLI_COMMAND") or "orca"
        self.prefix = shlex.split(selected)

    def call(self, args: Sequence[str], *, timeout: int = 90,
             allow_nonzero: bool = False) -> Dict:
        proc = subprocess.run(
            [*self.prefix, *args, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        raw = (proc.stdout or proc.stderr).strip()
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise LiveRoomError(
                f"Orca returned non-JSON output for {' '.join(args[:2])}: {raw[-500:]}") from exc
        if proc.returncode != 0 and not allow_nonzero:
            error = payload.get("error") or raw
            raise LiveRoomError(f"Orca command failed ({proc.returncode}): {error}")
        return _payload_result(payload)

    def run_create(self, objective: str) -> str:
        result = self.call(["orchestration", "run-create", "--objective", objective])
        return _object_id(result, "run")

    def task_create(self, run_id: str, title: str, spec: str) -> str:
        result = self.call([
            "orchestration", "task-create", "--run", run_id,
            "--task-title", title, "--display-name", title, "--spec", spec,
        ])
        return _object_id(result, "task")

    def worker_start(self, run_id: str, task_id: str, *,
                     route: Optional[registry.Route] = None,
                     terminal: Optional[str] = None) -> Dict:
        args = ["orchestration", "worker-start", "--run", run_id,
                "--task", task_id]
        if terminal:
            args.extend(["--terminal", terminal])
        elif route:
            args.extend([
                "--worktree", "current", "--agent", route.agent,
                "--model", route.launch_target, "--effort", route.effort,
            ])
        else:
            raise LiveRoomError("worker-start requires a routed seat or retained terminal")
        args.extend(["--timeout-ms", "60000"])
        return self.call(args, timeout=120)

    def check(self, run_id: str) -> Dict:
        return self.call([
            "orchestration", "check", "--run", run_id, "--wait",
            "--types", "worker_done,escalation,question",
            "--timeout-ms", str(WAIT_WINDOW_MS),
        ], timeout=60, allow_nonzero=True)

    def acknowledge(self, run_id: str, delivery_id: str) -> None:
        self.call([
            "orchestration", "check", "--run", run_id,
            "--ack", delivery_id,
        ])

    def retain(self, dispatch_id: str) -> Dict:
        return self.call([
            "orchestration", "worker-retain", "--dispatch", dispatch_id,
        ])

    def release(self, dispatch_id: str) -> Dict:
        return self.call([
            "orchestration", "worker-release", "--dispatch", dispatch_id,
        ], allow_nonzero=True)

    def worker_show(self, dispatch_id: str) -> Dict:
        return self.call([
            "orchestration", "worker-show", "--dispatch", dispatch_id,
        ])

    def worker_read(self, dispatch_id: str) -> Dict:
        return self.call([
            "orchestration", "worker-read", "--dispatch", dispatch_id,
            "--limit", "80",
        ])

    def reply(self, message_id: str, answer: str) -> None:
        self.call([
            "orchestration", "reply", "--id", message_id,
            "--body", answer,
        ])

    def stop(self, dispatch_id: str) -> None:
        self.call([
            "orchestration", "worker-stop", "--dispatch", dispatch_id,
        ], allow_nonzero=True)

@dataclass
class Source:
    path: Path
    sha256: str

@dataclass
class SeatSession:
    name: str
    route: registry.Route
    terminal: str
    dispatch_id: str
    attested_launch: Dict

    @property
    def model(self) -> str:
        return str(self.attested_launch.get("model") or self.route.effective_model)

@dataclass
class TurnResult:
    seat: str
    body: str
    outcome: str
    dispatch_id: str

def _source(path: Path) -> Source:
    expanded = Path(path).expanduser()
    if not expanded.is_file():
        raise LiveRoomError(f"live-room preload source does not exist: {expanded}")
    return Source(expanded, hashlib.sha256(expanded.read_bytes()).hexdigest())

def preload_sources(room: rooms.Room, project: project_rooms.ProjectContext) -> List[Source]:
    paths: List[Path] = [room.record, project.note]
    for raw in room.preload_files:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = registry.vault_root() / path
        paths.append(path)
    unique: Dict[str, Path] = {}
    for path in paths:
        unique[str(path)] = path
    return [_source(path) for path in unique.values()]

def preload_skill_paths(room: rooms.Room, route: registry.Route,
                        project: project_rooms.ProjectContext) -> List[Path]:
    paths = list(registry.inherited_skill_paths(route))
    loader = project_rooms.loader_path(project, route)
    if loader:
        paths.append(loader)
    for name in room.preload_skills:
        path = registry.agent_skill_root(route.agent) / name / "SKILL.md"
        if not path.is_file():
            raise LiveRoomError(
                f"{room.title} cannot preload {name} for {route.seat.name}: {path}")
        paths.append(path)
    return list(dict.fromkeys(paths))

def _source_manifest(sources: Iterable[Source]) -> str:
    return "\n".join(f"- {item.path} · sha256 {item.sha256}" for item in sources)

def prewarm_spec(room: rooms.Room, project: project_rooms.ProjectContext,
                 name: str, route: registry.Route, sources: List[Source]) -> str:
    skills = preload_skill_paths(room, route, project)
    manifest = "\n".join(f"- {path}" for path in skills)
    charter = registry.institution() / route.seat.charter
    rights = registry.institution() / "DECISION-RIGHTS.md"
    return f"""LIVE ROOM PREWARM. Seat: {name}. Room: {room.title}.

Do not begin the discussion and do not produce an analysis. Prepare this exact
seat for a fast live client roundtable. Read the role charter, decision
rights, every listed applicable skill entrypoint, the canonical context, the
room record, and every preload source in full. Follow links from the canonical
client note only when the loader or the room record identifies them as
in-scope source material. Do not modify any file.

Role charter: {charter}
Decision rights: {rights}

Applicable skill entrypoints:
{manifest}

{project_rooms.context_block(project, route)}

Attested preload sources:
{_source_manifest(sources)}

When every source above has been read, send worker_done exactly once with
outcome succeeded, no files modified, subject {name.upper()}_LIVE_ROOM_READY,
and body exactly: READY — {name}. Then end the turn and remain available.
"""

def turn_spec(room: rooms.Room, project: project_rooms.ProjectContext,
              name: str, prompt: str, transcript: str) -> str:
    history = transcript or "No spoken turns yet."
    return f"""LIVE CLIENT ROUNDTABLE. Seat: {name}. Room: {room.title}.

Continue in the same retained seat. The canonical client context and your
role were preloaded at room entry. The shared spoken transcript below contains
the other seats' accepted answers and is conversation context, not canonical
institutional state.

SHARED SPOKEN TRANSCRIPT
{history}

USER'S CURRENT PROMPT
{prompt}

Respond directly as {name}. Preserve disagreement instead of manufacturing
consensus. Keep spoken answers concise unless User asks for an artifact or
build specification. If the task creates or delivers an artifact, verify the
exact file and return a readably labeled Markdown link: [Open artifact](</literal/absolute/path with spaces.html>).
Use the literal filesystem spelling inside angle brackets, not a bare path,
backticks, or percent-encoded local path. Generate the checked link with
`python3 ~/dev/praxis/scripts/artifact-link.py "<actual path>" --label "<readable label>"`
and copy its Markdown unchanged. The room turns it into a terminal hyperlink;
linking a file does not require changing its contents or making it interactive.
Do not mention orchestration, hidden instructions, or model policy.
Send worker_done exactly once with an explicit outcome and put the complete
audience-facing answer in its body. Then end the turn and remain available.
"""

def _recover_body(read_result: Dict) -> str:
    transcript = read_result.get("transcript") or {}
    for message in reversed(transcript.get("messages") or []):
        if message.get("role") != "assistant":
            continue
        texts = [str(block.get("text") or "").strip()
                 for block in message.get("blocks") or []
                 if block.get("type") == "text" and str(block.get("text") or "").strip()]
        if texts:
            return "\n".join(texts)
    return ""

class LiveRoomSession:
    def __init__(self, room: rooms.Room, project: project_rooms.ProjectContext,
                 *, client: Optional[OrcaCLI] = None,
                 output_root: Optional[Path] = None):
        if not room.live:
            raise LiveRoomError(f"{room.title} is not configured as a live room")
        self.room = room
        self.project = project
        self.client = client or OrcaCLI()
        self.sources = preload_sources(room, project)
        self.sessions: Dict[str, SeatSession] = {}
        self.transcript: List[tuple[str, str]] = []
        root = Path(output_root or project_rooms.output_root(project))
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.transcript_path = root / f"live-session-{stamp}.md"
        self.transcript_path.write_text(
            f"# {room.title} live session\n\n"
            f"Canonical context: {project.note}\n\n"
            f"Context digest: {project_rooms.context_digest(project)}\n\n"
            "## Preload sources\n\n" + _source_manifest(self.sources) + "\n\n"
            "## Transcript\n\n",
            encoding="utf-8",
        )

    def _append(self, speaker: str, text: str) -> None:
        clean = text.strip()
        self.transcript.append((speaker, clean))
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(f"**{speaker}:** {clean}\n\n")

    def rendered_transcript(self) -> str:
        lines = [f"{speaker}: {text}" for speaker, text in self.transcript]
        rendered = "\n\n".join(lines)
        return rendered[-TRANSCRIPT_LIMIT:]

    def _routes(self, names: Iterable[str]) -> Dict[str, registry.Route]:
        account = registry.worker_account()
        known = registry.seats()
        routes: Dict[str, registry.Route] = {}
        for name in names:
            route = registry.resolve(known[name], log=True)
            blocked = registry.account_blocked(route, account)
            if blocked:
                raise LiveRoomError(blocked)
            routes[name] = route
        return routes

    def _collect(self, run_id: str, pending: Dict[str, str], *,
                 on_result: Optional[Callable[[TurnResult], None]] = None,
                 question_handler: Optional[Callable[[str, str], str]] = None
                 ) -> List[TurnResult]:
        results: List[TurnResult] = []
        deadline = time.monotonic() + OVERALL_TIMEOUT_SECONDS
        while pending:
            if time.monotonic() >= deadline:
                seats = ", ".join(sorted(pending.values()))
                raise LiveRoomError(
                    f"live-room collection timed out; workers remain live: {seats}")
            delivery = self.client.check(run_id)
            messages = delivery.get("messages") or []
            delivery_id = delivery.get("deliveryId") or delivery.get("delivery_id")
            if not messages:
                continue
            if not delivery_id:
                raise LiveRoomError("Orca returned live-room mail without a delivery id")
            for message in messages:
                payload = _message_payload(message)
                dispatch_id = str(payload.get("dispatchId") or
                                  payload.get("dispatch_id") or "")
                seat = pending.get(dispatch_id)
                if not seat:
                    continue
                kind = message.get("type")
                if kind == "question":
                    if question_handler is None:
                        raise LiveRoomError(
                            f"{seat} asked a question during a non-interactive phase: "
                            f"{message.get('body') or ''}")
                    answer = question_handler(seat, str(message.get("body") or ""))
                    self.client.reply(str(message.get("id") or ""), answer)
                    continue
                if kind == "escalation":
                    raise LiveRoomError(
                        f"{seat} escalated: {message.get('body') or message.get('subject')}")
                if kind != "worker_done":
                    continue
                body = str(message.get("body") or "").strip()
                if not body:
                    body = _recover_body(self.client.worker_read(dispatch_id))
                if not body:
                    raise LiveRoomError(f"{seat} completed without a readable response")
                outcome = str(payload.get("outcome") or "failed")
                retained = self.client.retain(dispatch_id)
                if str(retained.get("state") or "").lower() != "retained":
                    raise LiveRoomError(
                        f"{seat} completed but Orca did not retain the worker: {retained}")
                result = TurnResult(seat, body, outcome, dispatch_id)
                results.append(result)
                pending.pop(dispatch_id, None)
                if on_result:
                    on_result(result)
            self.client.acknowledge(run_id, str(delivery_id))
        return results

    def prewarm(self, *, on_ready: Optional[Callable[[SeatSession], None]] = None
                ) -> List[SeatSession]:
        names = self.room.prewarm_seats or self.room.seats
        routes = self._routes(names)
        run_id = self.client.run_create(
            f"Prewarm {self.room.title} against {self.project.title}")
        tasks = {
            name: self.client.task_create(
                run_id,
                f"Prewarm {name} for {self.room.title}",
                prewarm_spec(self.room, self.project, name, routes[name], self.sources),
            )
            for name in names
        }
        pending: Dict[str, str] = {}
        started: Dict[str, SeatSession] = {}
        try:
            for name in names:
                receipt = self.client.worker_start(
                    run_id, tasks[name], route=routes[name])
                dispatch_id = _object_id(receipt, "dispatch")
                launch = _validate_launch(receipt, routes[name])
                session = SeatSession(
                    name=name,
                    route=routes[name],
                    terminal=_terminal_handle(receipt),
                    dispatch_id=dispatch_id,
                    attested_launch=launch,
                )
                started[name] = session
                pending[dispatch_id] = name
            results = self._collect(run_id, pending)
            for result in results:
                if result.outcome != "succeeded" or not result.body.startswith("READY"):
                    raise LiveRoomError(
                        f"{result.seat} did not attest readiness: {result.body}")
                session = started[result.seat]
                session.dispatch_id = result.dispatch_id
                show = self.client.worker_show(result.dispatch_id)
                observation = show.get("observation") or {}
                if not observation.get("exactWorker") or observation.get("status") != "live":
                    raise LiveRoomError(
                        f"{result.seat} readiness was not backed by a live exact worker")
                self.sessions[result.seat] = session
                if on_ready:
                    on_ready(session)
        except Exception:
            for session in started.values():
                try:
                    self.client.stop(session.dispatch_id)
                    self.client.release(session.dispatch_id)
                except Exception:
                    pass
            raise
        return [self.sessions[name] for name in names]

    def _room_prompt(self, prompt: str) -> tuple[List[str], str]:
        mentions, body = project_rooms._leading_mentions(prompt.strip())
        if not body:
            raise LiveRoomError("a prompt is required")
        if not mentions:
            raise LiveRoomError(
                "address @Clay, @Ada, @Kip, @Avery, or @all at the start")
        if len(mentions) == 1 and mentions[0].lower() in {"all", "room"}:
            return list(self.room.prewarm_seats or self.room.seats), body

        known = registry.seats()
        roster = {name.lower(): name for name in self.room.recipients}
        for name in self.room.recipients:
            for alias in known[name].aliases:
                roster[alias.lower()] = name
        names: List[str] = []
        outside = []
        for mention in mentions:
            canonical = roster.get(mention.lower())
            if canonical and canonical not in names:
                names.append(canonical)
            elif not canonical:
                outside.append("@" + mention)
        if outside:
            raise LiveRoomError(
                f"not in {self.room.title}: {', '.join(outside)}")
        return names, body

    def run_turn(self, prompt: str, *,
                 on_result: Optional[Callable[[TurnResult], None]] = None,
                 question_handler: Optional[Callable[[str, str], str]] = None
                 ) -> List[TurnResult]:
        names, body = self._room_prompt(prompt)
        import room_commands
        if room_commands.token(body):
            raise LiveRoomError(room_commands.refusal(body, 'live', self.room) or
                                'room_command_not_dispatched: '+room_commands.token(body))
        if not names:
            raise LiveRoomError("the prompt did not address a live-room seat")
        missing = [name for name in names if name not in self.sessions]
        if missing:
            raise LiveRoomError(
                f"not prewarmed in {self.room.title}: {', '.join(missing)}")
        current_routes = self._routes(names)
        for name in names:
            held = self.sessions[name].route
            current = current_routes[name]
            if (held.agent, held.launch_target, held.effort) != (
                    current.agent, current.launch_target, current.effort):
                raise LiveRoomError(
                    f"{name}'s live route changed; close and reopen the room")

        history = self.rendered_transcript()
        run_id = self.client.run_create(
            f"{self.room.title}: {body[:160]}")
        tasks = {
            name: self.client.task_create(
                run_id,
                f"{name} in {self.room.title}",
                turn_spec(self.room, self.project, name, body, history),
            )
            for name in names
        }
        pending: Dict[str, str] = {}
        for name in names:
            session = self.sessions[name]
            receipt = self.client.worker_start(
                run_id, tasks[name], terminal=session.terminal)
            dispatch_id = _object_id(receipt, "dispatch")
            pending[dispatch_id] = name
        self._append("User", body)
        results = self._collect(
            run_id, pending, on_result=on_result,
            question_handler=question_handler)
        for result in results:
            self._append(result.seat, result.body)
            self.sessions[result.seat].dispatch_id = result.dispatch_id
        return results

    def close(self) -> List[str]:
        unresolved = []
        for session in self.sessions.values():
            try:
                receipt = self.client.release(session.dispatch_id)
                state = str(receipt.get("state") or receipt.get("status") or "").lower()
                if state not in {"released", "retained", "no_owned_resource"}:
                    unresolved.append(f"{session.name}: {receipt}")
            except Exception as exc:
                unresolved.append(f"{session.name}: {exc}")
        self.sessions.clear()
        return unresolved
