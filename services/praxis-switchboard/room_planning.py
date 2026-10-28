
from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import project_rooms
import registry
import rooms
import runner

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "orca-dispatch"))
from planning_contract import digest as payload_digest, validate as validate_output

TEMPLATE = "room-planning-v1"
EVIDENCE = "room-planning/evidence"
VERSION = re.compile(r"^(p_[a-f0-9]{16})@([1-9][0-9]*)$")
PLAN_ID = re.compile(r"^p_[a-f0-9]{16}$")
PLANNING_ROUTES = {"Athena": "room-planning-athena", "Toby": "room-planning-toby",
                   "Leo": "room-planning-leo"}

class PlanningError(registry.RegistryError):
    pass

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _write(path: Path, value: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if exclusive:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        return
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PlanningError(f"invalid planning record: {path}")
    return value

@contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".planning.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def _eligible(room: rooms.Room, project: project_rooms.ProjectContext) -> None:
    if room.room_id != "praxis-dev-room" or room.live or room.planning_template != TEMPLATE:
        raise PlanningError("planning pilot is eligible only in the ordinary Praxis Dev room")
    if (project.kind != "system-development" or
            project_rooms.context_identity(project) != "system/praxis/Praxis Dev.md" or
            not project.note.is_file()):
        raise PlanningError("planning pilot requires the canonical Praxis Dev context")

def _deployed_contract_ready() -> None:
    source = Path(__file__).resolve().parents[2] / "skills/orca-dispatch"
    deployed = registry.skill_root() / "orca-dispatch"
    for name in ("dispatcher.py", "planning_contract.py"):
        if not (deployed / name).is_file() or _sha(source / name) != _sha(deployed / name):
            raise PlanningError(f"planning pilot awaits inspected shared-skill deployment: {name}")

def _root() -> Path:
    return registry.institution() / EVIDENCE

def _version_dir(plan_id: str, version: int) -> Path:
    if not PLAN_ID.fullmatch(plan_id) or version < 1:
        raise PlanningError("invalid plan identity")
    return _root() / plan_id / f"v{version}"

def _parse_ref(ref: str) -> tuple[str, int]:
    match = VERSION.fullmatch(ref)
    if not match:
        raise PlanningError("use plan-id@version, for example p_0123456789abcdef@1")
    return match.group(1), int(match.group(2))

def _version_record(path: Path, project: project_rooms.ProjectContext) -> dict:
    record = _read(path / "version.json")
    if (record.get("context_identity") != project_rooms.context_identity(project) or
            record.get("context_digest") != project_rooms.context_digest(project) or
            record.get("context_note") != str(project.note)):
        raise PlanningError("plan version belongs to a different context")
    return record

def _source_hashes(payload: dict) -> None:
    for item in payload.get("relied_on_sources", []):
        path = Path(item["path"])
        if not path.is_file() or _sha(path) != item["sha256"]:
            raise PlanningError(f"relied-on source changed or disappeared: {path}")

def _seat_spec(name: str, project: project_rooms.ProjectContext, body: str) -> str:
    seats = registry.seats(include_pilots=True)
    seat = seats[name]
    planning_route = PLANNING_ROUTES[name]
    route = registry.resolve(replace(seat, route=planning_route), log=False)
    all_skills = registry.inherited_skill_paths(route)
    applicable = {"Athena": {"research-design", "project"},
                  "Toby": {"writing", "research-design"},
                  "Leo": {"chief-of-staff", "orchestration"}}[name]
    selected = [path for path in all_skills if path.parent.name in applicable]
    if {path.parent.name for path in selected} != applicable:
        raise PlanningError(f"{name} planning skill inheritance is incomplete")
    inst = registry.institution()

    return (f"You are {name} in the commissioned Praxis Dev planning pilot. "
            f"Read {inst / seat.charter} and {inst / 'DECISION-RIGHTS.md'} in full.\n"
            "Inspect every inherited skill entrypoint. Complete inherited set:\n" +
            "\n".join(f"- {path}" for path in all_skills) +
            "\nMethods applicable to this planning task:\n" +
            "\n".join(f"- {path}" for path in selected) +
            f"\nRead those applicable SKILL.md files in full.\n{project_rooms.context_block(project, route)}\n\n{body}\n\n"
            "Draft and stage only. No implementation dispatch, publication, sending, "
            "spending, approval, or calendar commitment. Report uncertainty as uncertainty. "
            "Your JSON artifact is the substantive output; provide its exact path "
            "as --report-path in worker_done. Do not ask a worker question for User's decision.")

def _unit(uid: str, title: str, route: str, spec: str, output: Path,
          kind: str, *, deps: list[str] | None = None, links: dict | None = None) -> dict:
    return {
        "id": uid, "title": title, "task_class": route,
        "sensitivity": "internal", "risk": "high", "data_domains": ["institutional"],
        "depends_on": deps or [], "spec": spec,
        "outputs": [str(output)],
        "planning_output": {"kind": kind, "path": str(output), **(links or {})},
        "return_contract": {"min_chars": 10, "max_chars": 4000,
                            "require_report_path": True},
    }

def _plan(phase: str, plan_id: str, version: int, project: project_rooms.ProjectContext,
          path: Path, objective: str, instructions: str = "") -> dict:
    context = {"kind": project.kind, "note": str(project.note),
               "identity": project_rooms.context_identity(project),
               "digest": project_rooms.context_digest(project)}
    proposal, critique, packet = (path / f"{name}.json" for name in
                                  ("proposal", "critique", "packet"))
    final, execution = path / "final.json", path / "execution.json"
    if phase == "a":
        prior = path.parent / f"v{version-1}" if version > 1 else None
        revision = (f"This is a revision of the reviewed version. Read the exact prior packet {prior / 'packet.json'}, "
                    f"critique {prior / 'critique.json'}, and decision {prior / 'decision.json'}. "
                    "Carry forward their evidence and dissent; address the verbatim revision instructions below. "
                    if prior else "")
        example_payload = {"objective": "illustrative only", "evidence": [], "assumptions": [],
                           "alternatives": ["A", "B"], "tradeoffs": [], "decisions": ["choose"],
                           "assignments": [{"id": "a1", "task": "review", "owner": "User decision required",
                                            "depends_on": [], "acceptance": "reviewed", "authority": "proposal only"}],
                           "risks": [], "out_of_scope": [],
                           "relied_on_sources": [{"path": str(project.note), "sha256": _sha(project.note)}]}
        example = json.dumps({"schema_version": 1, "substantive_payload": example_payload,
                              "payload_canonical_sha256": payload_digest(example_payload)}, ensure_ascii=False)
        prompt = (f"Objective from User, verbatim: {objective}\n"
                  + revision +
                  f"Revision instructions, verbatim: {instructions}\n"
                  f"The canonical context note {project.note} MUST appear in substantive_payload.relied_on_sources "
                  f"with its absolute path and SHA-256 file-byte hash {_sha(project.note)}. "
                  "Write JSON with exactly schema_version=1, substantive_payload, and payload_canonical_sha256. "
                  "substantive_payload contains exactly "
                  "objective and arrays evidence, assumptions, alternatives, tradeoffs, "
                  "decisions, assignments, risks, out_of_scope, relied_on_sources "
                  "(each assignment has exactly id, task, owner, depends_on array, acceptance, "
                  "authority; use 'User decision required' for unresolved owners; "
                  "assignment IDs must be unique and depends_on must reference earlier IDs). "
                  "Every assignment authority must be the literal string 'proposal only'. "
                  "Each relied-on source has exactly absolute path and sha256, a file-byte SHA-256. "
                  "payload_canonical_sha256 is SHA-256 of UTF-8 JSON substantive_payload "
                  "(sort_keys=True, ensure_ascii=False, separators=(',', ':')). "
                  f"Valid illustrative structure with this note's actual hash: {example}. Replace illustrative content with evidence. "
                  f"Write only to {proposal}.")
        review = (f"Read the accepted proposal at {proposal} and its dependency "
                  f"snapshot {path / 'a-bundle.json'}. Check exact source and hash. "
                  + (f"Also read prior packet {prior / 'packet.json'}, critique {prior / 'critique.json'}, "
                     f"decision {prior / 'decision.json'}, and verbatim instructions: {instructions}. " if prior else "") +
                  "Write JSON with exactly schema_version=1, reviewed_payload_canonical_sha256, arrays "
                  "blockers, improvements, dissent, and a short judgment. Preserve real disagreement. "
                  "reviewed_payload_canonical_sha256 is SHA-256 of UTF-8 canonical JSON of the parsed proposal substantive_payload (sort_keys=True, ensure_ascii=False, separators=(',', ':')). "
                  f"Write only to {critique}.")
        digest_prompt = (f"Read accepted proposal {proposal}, critique {critique}, "
                         f"and dependency snapshot {path / 'a-bundle.json'}. "
                         + (f"Also read the exact prior packet {prior / 'packet.json'}, critique {prior / 'critique.json'}, "
                            f"decision {prior / 'decision.json'}, and verbatim revision instructions: {instructions}. "
                            if prior else "") +
                         "Write JSON schema_version=1, unchanged substantive_payload, "
                         "proposal_payload_canonical_sha256, critique_canonical_sha256 (canonical JSON "
                         "of the critique), exact dissent array, recommendation, "
                         "critique_blockers and critique_improvements copied "
                         "exactly, and decisions_owed copied from the proposal. "
                         "Set recommendation_by to Leo and attribute your recommendation. Use exactly these fields. "
                         "Canonical digests are SHA-256 of UTF-8 JSON with sort_keys=True, ensure_ascii=False, separators=(',', ':'); hash the parsed proposal payload and parsed critique respectively. "
                         f"Write only to {packet}.")
        units = [
            _unit("athena", "Athena proposal", PLANNING_ROUTES["Athena"],
                  _seat_spec("Athena", project, prompt), proposal, "proposal",
                  links={"required_source": str(project.note)}),
            _unit("toby", "Toby critique", PLANNING_ROUTES["Toby"],
                  _seat_spec("Toby", project, review), critique, "critique",
                  deps=["athena"], links={"proposal": str(proposal)}),
            _unit("leo", "Leo decision packet", PLANNING_ROUTES["Leo"],
                  _seat_spec("Leo", project, digest_prompt), packet, "packet",
                  deps=["athena", "toby"],
                  links={"proposal": str(proposal), "critique": str(critique)}),
        ]
    else:
        decision = path / "decision.json"
        finalize = (f"User's immutable decision is {decision}. Read the "
                    f"accepted packet {packet} and critique {critique}. "
                    "The decision packet_file_sha256 is the SHA-256 of the packet file bytes; "
                    "do not copy it into a canonical digest field. Compute packet_canonical_sha256 "
                    "from the parsed packet using UTF-8 JSON sort_keys=True, ensure_ascii=False, "
                    "separators=(',', ':'). Write JSON schema_version=1, approved_payload_canonical_sha256, "
                    "packet_canonical_sha256, approved_packet copied exactly from the packet, "
                    "substantive_payload and dissent copied exactly. If instructions "
                    "require a substantive change, stop and report for a new "
                    f"review; do not rewrite. Write only to {final}.")
        execute = (f"Read accepted final {final}, critique {critique}, decision "
                   f"{decision}, and dependency snapshot {path / 'b-bundle.json'}. "
                   "Create a separate nonbinding proposed execution decomposition. "
                   "Write JSON with exactly schema_version=1, final_payload_canonical_sha256, "
                   "packet_canonical_sha256, dissent copied exactly, proposal_status='proposed_for_user_review', "
                   "and tasks. Each task has exactly id, assignment_id, proposed_step, owner, "
                   "depends_on, acceptance, authority. assignment_id must identify one approved "
                   "substantive_payload.assignments[].id; cover every assignment. Order tasks so "
                   "depends_on references earlier task IDs only. Copy each owner, acceptance, and "
                   "authority verbatim from its approved assignment. Every later step for one assignment "
                   "must transitively depend on its preceding step; "
                   "every step must transitively depend on the last step of each approved predecessor assignment. "
                   "The validator checks this graph, not the meaning of new prose. "
                   "Decompose work into useful steps, but label all new detail as proposed; this does not edit the approved "
                   "payload or authorize implementation. Preserve unresolved owners as User "
                   "decision required. If decomposition needs new scope, an owner, a date, a tradeoff, "
                   "or a substantive decision, stop and report that for User rather than invent it. "
                   "Compute final_payload_canonical_sha256 from parsed final substantive_payload and copy packet_canonical_sha256 from final; canonical JSON uses UTF-8, sort_keys=True, ensure_ascii=False, separators=(',', ':'). "
                   f"Write only to {execution}.")
        units = [
            _unit("athena", "Athena exact finalization", PLANNING_ROUTES["Athena"],
                  _seat_spec("Athena", project, finalize), final, "final",
                  links={"packet": str(packet)}),
            _unit("leo", "Leo proposed execution plan", PLANNING_ROUTES["Leo"],
                  _seat_spec("Leo", project, execute), execution, "execution",
                  deps=["athena"], links={"final": str(final)}),
        ]
    return {"schema_version": "1", "id": f"{plan_id}-v{version}-{phase}",
            "objective": f"Praxis Dev planning {plan_id}@{version} phase {phase}",
            "praxis_task_id": "PX-ROOM-PLANNING-PILOT",
            "defaults": {"worktree": "current", "start_timeout_ms": 60000,
                         "wait_window_ms": 5000, "overall_timeout_ms": 1800000},
            "worker_preamble": runner.WORKER_PREAMBLE,
            "context": context, "units": units}

def _run_key(path: Path, phase: str) -> str:
    if phase == "b":
        decision_id = _read(path / "decision.json")["decision_id"]
        token = hashlib.sha256(decision_id.encode()).hexdigest()[:24]
        return f"praxis-room-planning/decision-{token}/phase-b"
    return f"praxis-room-planning/{path.parent.name}/{path.name}/phase-a"

def _native_state(path: Path, phase: str) -> tuple[dict, bool, bool]:

    from dispatcher import RunLease, process_alive
    run_key = _run_key(path, phase)
    lease = RunLease(registry.vault_root(), run_key, None)._read()
    matching = lease.get("run_key") == run_key
    live = (matching and lease.get("status") == "active" and
            lease.get("host") == socket.gethostname() and process_alive(lease.get("pid")))
    known = matching and bool(lease.get("run_id")) and bool(lease.get("workers"))
    recoverable = (known and lease.get("status") in {None, "active", "interrupted", "dispatcher_error"}
                   and (lease.get("status") != "active" or
                        lease.get("host") == socket.gethostname() and not live))
    return lease, bool(live), bool(recoverable)

def _native_run(path: Path, phase: str, output: Path) -> None:
    dispatcher = registry.skill_root() / "orca-dispatch" / "dispatcher.py"
    subprocess.run([sys.executable, str(dispatcher), "run", "--plan",
                    str(path / f"{phase}-plan.json"), "--output", str(output),
                    "--run-key", _run_key(path, phase)], capture_output=True,
                   text=True, env=runner.launch_env(), check=False)

def _collect_output(path: Path, phase: str, intent: dict) -> dict | None:

    canonical = path / f"{phase}-bundle.json"
    if canonical.is_file() and not canonical.is_symlink():
        result = _read(canonical)
        if result.get("status") and result.get("status") != "duplicate_suppressed":
            return result
    if not intent.get("output_path"):
        return None
    output = Path(intent["output_path"])
    if not output.is_file() or output.parent != path:
        return None
    result = _read(output)
    if not result.get("status") or result.get("status") == "duplicate_suppressed":
        return None
    if (result.get("routine_run_key") != _run_key(path, phase) or
            result.get("plan_id") != _read(path / f"{phase}-plan.json")["id"]):
        raise PlanningError("native receipt does not match this planning occurrence")
    fd, name = tempfile.mkstemp(prefix=f".{canonical.name}.", dir=path)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(output.read_bytes())
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, canonical)
    finally:
        Path(name).unlink(missing_ok=True)
    return result

def _link_dependency_snapshot(path: Path, phase: str, output: Path) -> None:

    canonical = path / f"{phase}-bundle.json"
    if canonical.is_file():
        fd, name = tempfile.mkstemp(prefix=f".{output.name}.", dir=path)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(canonical.read_bytes())
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, output)
        finally:
            Path(name).unlink(missing_ok=True)
    link = path / f".{phase}-bundle-link-{uuid.uuid4().hex}"
    try:
        os.symlink(output.name, link)
        os.replace(link, canonical)
    finally:
        link.unlink(missing_ok=True)

def _dispatch(path: Path, phase: str, backend: Callable[[Path, Path, str], dict] | None) -> dict:
    plan_file, bundle_file = path / f"{phase}-plan.json", path / f"{phase}-bundle.json"
    intent_file = path / f"{phase}-intent.json"
    with _lock(path.parent):
        intent = _read(intent_file)
        if intent["state"] != "prepared":
            return {"state": intent["state"], "reason": "launch already claimed; inspect native receipt before recovery"}
        if _sha(plan_file) != intent["plan_sha256"]:
            raise PlanningError("immutable dispatch plan changed before launch")
        if phase == "b":
            if _sha(path / "decision.json") != intent["decision_sha256"]:
                raise PlanningError("immutable decision record changed before phase B")
            packet = _read(path / "packet.json")
            _source_hashes(packet["substantive_payload"])
        intent["state"] = "launching"
        intent["claimed_at"] = _now()
        _write(intent_file, intent)
    run_key = _run_key(path, phase)
    if backend is None:
        _deployed_contract_ready()
        lease, _, _ = _native_state(path, phase)
        if lease.get("run_key") == run_key:
            return {"state": "launching", "reason": "existing native lease needs reconciliation"}
        _native_run(path, phase, bundle_file)
        with _lock(path.parent):
            bundle = _collect_output(path, phase, intent)
        if bundle is None:
            return {"state": "launching", "reason": "native Run remains active or outcome is unknown"}
    else:
        bundle = backend(plan_file, bundle_file, run_key)
        _write(bundle_file, bundle)
    with _lock(path.parent):
        intent = _read(intent_file)
        intent["state"] = "settled" if bundle.get("status") == "succeeded" else "stopped"
        intent["bundle_sha256"] = _sha(bundle_file)
        intent["run_id"] = bundle.get("run_id")
        intent["status"] = bundle.get("status")
        _write(intent_file, intent)
    return bundle

def _resume_dispatch(path: Path, phase: str, backend) -> dict | None:

    intent_file, bundle_file = path / f"{phase}-intent.json", path / f"{phase}-bundle.json"
    intent = _read(intent_file)
    if intent["state"] == "prepared":
        result = _dispatch(path, phase, backend)
        if "status" in result:
            return result
                                                                                 
        intent = _read(intent_file)
    collect = None
    with _lock(path.parent):
        intent = _read(intent_file)
        lease, live, known = _native_state(path, phase) if backend is None else ({}, False, False)
        output = Path(intent["output_path"]) if intent.get("output_path") else None
        pending = _read(output) if output and output.is_file() else None
        if backend is None and live and pending and pending.get("status") != "succeeded" and (not bundle_file.is_file() or bundle_file.is_symlink()):
            bundle = None
        else:
            bundle = _collect_output(path, phase, intent) if backend is None else (_read(bundle_file) if bundle_file.is_file() else None)
        if bundle and bundle.get("status") == "duplicate_suppressed":
            bundle = None
        if bundle and bundle.get("status") != "succeeded" and live:
            bundle = None
        if bundle is None and backend is None and not live and known and intent["state"] in {"launching", "collecting"}:
            from dispatcher import process_alive
            collector_live = (intent["state"] == "collecting" and
                              intent.get("collector_host") == socket.gethostname() and
                              process_alive(intent.get("collector_pid")))
            if not collector_live:
                collect = path / f".{phase}-collection-{uuid.uuid4().hex}.json"
                intent["state"] = "collecting"
                intent["collector_pid"] = os.getpid()
                intent["collector_host"] = socket.gethostname()
                intent["output_path"] = str(collect)
                _link_dependency_snapshot(path, phase, collect)
                _write(intent_file, intent)
        if bundle and intent["state"] in {"launching", "collecting"}:
            intent["state"] = "settled" if bundle.get("status") == "succeeded" else "stopped"
            intent["bundle_sha256"] = _sha(bundle_file)
            intent["run_id"] = bundle.get("run_id")
            intent["status"] = bundle.get("status")
            _write(intent_file, intent)
    if collect is not None:
        _native_run(path, phase, collect)
        with _lock(path.parent):
            intent = _read(intent_file)
            _, live, _ = _native_state(path, phase)
            pending = _read(collect) if collect.is_file() else None
            bundle = None if live and pending and pending.get("status") != "succeeded" else _collect_output(path, phase, intent)
            if bundle and bundle.get("status") != "duplicate_suppressed":
                intent["state"] = "settled" if bundle.get("status") == "succeeded" else "stopped"
                intent["bundle_sha256"] = _sha(bundle_file)
                intent["run_id"] = bundle.get("run_id")
                intent["status"] = bundle.get("status")
                _write(intent_file, intent)
            elif intent["state"] == "collecting" and intent.get("output_path") == str(collect):
                intent["state"] = "launching"
                if bundle_file.is_symlink() and bundle_file.resolve() == collect and not collect.is_file():
                    bundle_file.unlink()
                _write(intent_file, intent)
    return bundle

def _accepted(path: Path, phase: str) -> dict:
    bundle_file = path / f"{phase}-bundle.json"
    bundle = _read(bundle_file)
    if bundle.get("status") != "succeeded":
        raise PlanningError(f"phase {phase} has no accepted complete Run")
    units = bundle.get("units") or []
    wanted = ["athena", "toby", "leo"] if phase == "a" else ["athena", "leo"]
    if [unit.get("id") for unit in units] != wanted or any(
            unit.get("status") != "succeeded" or
            (unit.get("contract") or {}).get("accepted") is not True for unit in units):
        raise PlanningError("Run bundle does not prove all ordered outputs accepted")
    for unit in units:
        name = ({"athena": "proposal", "toby": "critique", "leo": "packet"}[unit["id"]]
                if phase == "a" else {"athena": "final", "leo": "execution"}[unit["id"]])
        artifact = path / f"{name}.json"
        checks = unit.get("outputs") or []
        if not artifact.is_file() or not any(
                check.get("path") == str(artifact) and check.get("sha256") == _sha(artifact)
                for check in checks):
            raise PlanningError(f"accepted output changed after receipt: {artifact}")
    return bundle

def _reader_folder(project: project_rooms.ProjectContext) -> Path:
    note = project.note.read_text(encoding="utf-8")
    front = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", note, re.S)
    match = re.search(r"(?m)^folder:\s*(.+?)\s*$", front.group(1)) if front else None
    value = match.group(1).strip().strip("\"'") if match else ""
    configured = os.environ.get("PRAXIS_ARTIFACT_ROOT")
    if not configured:
        raise PlanningError("set PRAXIS_ARTIFACT_ROOT for planning artifacts")
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise PlanningError("PRAXIS_ARTIFACT_ROOT must be absolute")
    root = root.resolve()
    path = Path(value).expanduser().resolve() if value else (root / "planning").resolve()
    if not path.is_relative_to(root):
        raise PlanningError("context reader folder is outside PRAXIS_ARTIFACT_ROOT")
    return path

def _render(path: Path, record: dict, project: project_rooms.ProjectContext) -> Path:
    reader = _reader_folder(project) / "Praxis Dev Planning Brief.html"
    reader.parent.mkdir(parents=True, exist_ok=True)
    esc = lambda value: html.escape(str(value), quote=True)
    def value_html(value: object) -> str:
        if isinstance(value, dict):
            return "<dl>" + "".join(f"<dt>{esc(key.replace('_', ' ').capitalize())}</dt><dd>{value_html(item)}</dd>" for key, item in value.items()) + "</dl>"
        if isinstance(value, list):
            return items(value)
        return esc(value)
    def items(values: object) -> str:
        if not isinstance(values, list) or not values:
            return "<p class='empty'>None recorded.</p>"
        return "<ul>" + "".join(f"<li>{value_html(v)}</li>" for v in values) + "</ul>"
    def source(name: str) -> dict:
        file = path / name
        return _read(file) if file.is_file() else {}
    packet, critique, decision = source("packet.json"), source("critique.json"), source("decision.json")
    payload = packet.get("substantive_payload") or {}
    final, execution = source("final.json"), source("execution.json")
    date = datetime.now(timezone.utc).strftime("%-d %B %Y")
    token = f"{path.parent.name}@{record['version']}"
    lines = ["<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>Praxis Dev planning brief</title><style>",
             ":root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f7f8fa;color:#172a3b;font:16px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}main{max-width:940px;margin:auto;padding:32px 24px 72px}h1,h2,h3{font-family:Georgia,serif;line-height:1.18}h1{font-size:clamp(2rem,5vw,3.2rem);margin:.4em 0}h2{font-size:1.55rem;margin:0 0 .5em}h3{font-size:1.05rem;margin:1.2em 0 .3em}.eyebrow,.meta{color:#486277;font-size:.85rem;letter-spacing:.04em}.lead{font-size:1.18rem;max-width:70ch}.grid{display:grid;grid-template-columns:1.4fr 1fr;gap:18px}.card{background:white;border:1px solid #cbd7df;border-radius:5px;padding:22px;margin:18px 0}.dissent{border-left:5px solid #a05339}.decision{border-left:5px solid #2e607c}ul{padding-left:1.35em}li{margin:.35em 0}a{color:#14547c;overflow-wrap:anywhere}a:focus-visible,summary:focus-visible{outline:3px solid #a05339;outline-offset:3px}code,pre{overflow-wrap:anywhere}pre{white-space:pre-wrap;font-size:.8rem;background:#eff3f6;padding:14px}.empty{color:#617282}footer{border-top:1px solid #cbd7df;padding-top:16px;margin-top:36px}@media(max-width:700px){.grid{grid-template-columns:1fr}main{padding:20px 16px 50px}}",
             "dl{margin:.25em 0}dt{font-weight:650;margin-top:.45em}dd{margin:0 0 .3em .7em}</style><main><header><p class='eyebrow'>PRAXIS DEV · DECISION BRIEF</p><h1>Planning pilot</h1>",
             f"<p class='meta'>Rendered {esc(date)} · Version {esc(record['version'])} · Reference <code>{esc(token)}</code></p>",
             f"<p class='lead'>{esc(record['objective'])}</p><p><strong>Stage:</strong> {esc(record['stage'])} · <strong>Next:</strong> {esc(record.get('next_actor',''))}</p></header>"]
    if "synthetic" in record.get("event_provenance", "").lower():
        lines.append("<p class='card dissent'><strong>Synthetic test data.</strong> This page does not record a live planning run or a decision by User.</p>")
    if packet:
        lines += ["<div class='grid'><section class='card'><h2>Recommendation</h2>",
                  f"<p class='meta'>Attributed to {esc(packet.get('recommendation_by','Leo'))}</p><p>{esc(packet.get('recommendation',''))}</p>",
                  "<h3>Options</h3>", items(payload.get("alternatives")), "<h3>Tradeoffs</h3>", items(payload.get("tradeoffs")), "</section>",
                  "<section class='card decision'><h2>Decisions owed</h2>", items(packet.get("decisions_owed")), "<h3>Proposed assignments</h3>", items(payload.get("assignments")), "</section></div>",
                  "<div class='grid'><section class='card'><h2>Evidence and risks</h2><h3>Evidence</h3>", items(payload.get("evidence")), "<h3>Assumptions</h3>", items(payload.get("assumptions")), "<h3>Risks</h3>", items(payload.get("risks")), "<h3>Out of scope</h3>", items(payload.get("out_of_scope")), "</section>",
                  "<section class='card dissent'><h2>Toby's independent critique</h2><p class='meta'>Judgment</p>", f"<p>{esc(critique.get('judgment',''))}</p>",
                  "<h3>Blockers</h3>", items(critique.get("blockers")), "<h3>Improvements</h3>", items(critique.get("improvements")), "<h3>Verbatim dissent</h3>", items(critique.get("dissent")), "</section></div>"]
    if decision:
        lines += ["<section class='card decision'><h2>User's recorded decision</h2>",
                  f"<p><strong>Action:</strong> {esc(decision.get('action',''))} · <strong>Recorded:</strong> {esc(decision.get('decided_at',''))}</p>",
                  f"<p><strong>Instructions, verbatim:</strong> {esc(decision.get('instructions','')) or 'None'}</p>",
                  f"<p class='meta'>Transport: {esc(decision.get('event_provenance',''))}. {esc(decision.get('event_authentication',''))}.</p></section>"]
    if final or execution:
        lines += ["<section class='card'><h2>Leo's proposed task decomposition</h2><p>Proposed for User's review. No implementation is authorized by this page. Each step refers to an approved assignment ID.</p>", items(execution.get("tasks")), "</section>"]
    lines += ["<footer><h2>Sources and provenance</h2>",
              f"<p>Context note: <code>{esc(project.note)}</code></p>",
              "<h3>Relied-on sources</h3>", items(payload.get("relied_on_sources"))]
    for bundle_name, phase_label in (("a-bundle.json", "Run A"), ("b-bundle.json", "Run B")):
        bundle = source(bundle_name)
        if bundle:
            lines.append(f"<h3>{phase_label} receipt attribution</h3><p>Run <code>{esc(bundle.get('run_id','not recorded'))}</code></p><ul>")
            for unit in bundle.get("units") or []:
                launch = unit.get("actual_launch") or {}
                model = launch.get("model") or "not attested in this receipt"
                effort = launch.get("effort") or "not attested"
                lines.append(f"<li>{esc(unit.get('id','unit'))}: model {esc(model)}, effort {esc(effort)}, task <code>{esc(unit.get('task_id','not recorded'))}</code>, dispatch <code>{esc(unit.get('dispatch_id','not recorded'))}</code></li>")
            lines.append("</ul>")
    for name, owner in (("proposal.json", "Athena proposal"), ("critique.json", "Toby critique"), ("packet.json", "Leo packet"), ("decision.json", "Decision record"), ("final.json", "Athena final"), ("execution.json", "Leo execution proposal"), ("a-bundle.json", "Run A receipt"), ("b-bundle.json", "Run B receipt")):
        file = path / name
        if file.is_file():
            anchor = "artifact-" + name.removesuffix(".json")
            lines.append(f"<p>{esc(owner)}: <a href='#{anchor}'>view embedded original</a> · source <code>{esc(file)}</code> · file SHA-256 <code>{_sha(file)}</code></p>")
    lines += [f"<p>Packet file SHA-256: <code>{esc(record.get('packet_file_sha256','pending'))}</code>. Packet canonical JSON SHA-256: <code>{esc(record.get('packet_canonical_sha256','pending'))}</code>.</p>",
              "<details><summary>Raw structured artifacts</summary>"]
    for name in ("proposal.json", "critique.json", "packet.json", "decision.json", "final.json", "execution.json", "a-bundle.json", "b-bundle.json"):
        file = path / name
        if file.is_file():
            anchor = "artifact-" + name.removesuffix(".json")
            lines.append(f"<section id='{anchor}'><h3>{esc(name)}</h3><pre>{esc(file.read_text(encoding='utf-8'))}</pre></section>")
    lines += ["</details></footer></main></html>"]
    content = "".join(lines)
    if reader.exists():
        archive = reader.parent / "_archive"
        archive.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        reader.replace(archive / f"Praxis Dev Planning Brief {stamp} {_sha(reader)[:12]}.html")
    reader.write_text(content, encoding="utf-8")
    return reader

def _publish(path: Path, record: dict, project: project_rooms.ProjectContext,
             deliver: Callable[[Path], str] | None) -> dict:
    reader = _render(path, record, project)
    record["reader_path"] = str(reader)
    record["reader_sha256"] = _sha(reader)
    record["reader_state"] = "created"
    if deliver:
        try:
            record["reader_link"] = deliver(reader)
            record["reader_state"] = "checked_link"
        except Exception as exc:
            record["reader_error"] = str(exc)
    _write(path / "version.json", record)
    return record

def checked_delivery(reader: Path) -> str:
    helper = Path(os.environ.get("PRAXIS_ARTIFACT_HELPER", str(Path(__file__).resolve().parents[2] / "scripts/artifact-link.py"))).expanduser()
    proc = subprocess.run([sys.executable, str(helper), str(reader), "--label",
                           "Praxis Dev planning packet", "--deliver", "--worktree",
                           f"path:{registry.vault_root()}"], capture_output=True,
                          text=True, check=False)
    if proc.returncode or not proc.stdout.strip():
        raise PlanningError((proc.stderr or proc.stdout or "delivery failed").strip())
    return proc.stdout.strip()

def start(room: rooms.Room, project: project_rooms.ProjectContext, objective: str,
          *, event_id: str, provenance: str, backend=None, deliver=checked_delivery,
          plan_id: str | None = None, version: int = 1, instructions: str = "") -> dict:
    _eligible(room, project)
    if backend is None:
        _deployed_contract_ready()
    if not objective.strip() or not event_id.strip() or not provenance.strip():
        raise PlanningError("objective, event ID, and transport provenance are required")
    if plan_id is None:
        plan_id = "p_" + hashlib.sha256((project_rooms.context_identity(project) + "\n" +
                                          event_id).encode()).hexdigest()[:16]
    path = _version_dir(plan_id, version)
    with _lock(path.parent):
        if (path / "version.json").exists():
            prior = _version_record(path, project)
            if (prior["event_id"], prior["objective"], prior.get("revision_instructions", "")) != (event_id, objective, instructions):
                raise PlanningError("conflicting replay; use a new version")
            if prior["stage"] != "phase_a_launch_intent":
                return prior
        else:
            if version != 1 and not (path.parent / f"v{version-1}" / "decision.json").is_file():
                raise PlanningError("revision requires an immutable prior decision")
            record = {"plan_id": plan_id, "version": version, "stage": "phase_a_launch_intent",
                      "next_actor": "Athena", "objective": objective, "event_id": event_id,
                      "event_provenance": provenance, "event_authentication": "transport assertion only",
                      "revision_instructions": instructions,
                      "context_note": str(project.note),
                      "context_identity": project_rooms.context_identity(project),
                      "context_digest": project_rooms.context_digest(project), "created_at": _now()}
            _write(path / "version.json", record, exclusive=True)
        if not (path / "a-plan.json").is_file():
            _write(path / "a-plan.json", _plan("a", plan_id, version, project, path, objective, instructions), exclusive=True)
        if not (path / "a-intent.json").is_file():
            _write(path / "a-intent.json", {"state": "prepared", "created_at": _now(),
                                            "plan_sha256": _sha(path / "a-plan.json")}, exclusive=True)
    bundle = _resume_dispatch(path, "a", backend)
    if bundle is None:
        return _status_record(path, project)
    with _lock(path.parent):
        record = _version_record(path, project)
        if record["stage"] == "awaiting_user":
            return record
        if bundle.get("status") == "succeeded":
            try:
                _accepted(path, "a")
                validate_output({"kind": "packet", "path": str(path / "packet.json"),
                                 "proposal": str(path / "proposal.json"),
                                 "critique": str(path / "critique.json")})
            except (PlanningError, ValueError) as exc:
                record["stage"], record["next_actor"] = "stopped", "inspection"
                record["stop_reason"] = str(exc)
                _write(path / "version.json", record)
                return record
            record["stage"], record["next_actor"] = "awaiting_user", "User decision"
            packet = _read(path / "packet.json")
            record["packet_file_sha256"] = _sha(path / "packet.json")
            record["packet_canonical_sha256"] = payload_digest(packet)
            record["payload_canonical_sha256"] = payload_digest(packet["substantive_payload"])
            record["decisions_owed"] = packet["decisions_owed"]
        else:
            record["stage"], record["next_actor"] = "stopped", "inspection"
            record["stop_reason"] = f"Run A status {bundle.get('status')}"
        return _publish(path, record, project, deliver)

def status(room: rooms.Room, project: project_rooms.ProjectContext,
           ref: str | None = None) -> dict:
    _eligible(room, project)
    if ref:
        plan_id, version = _parse_ref(ref)
        path = _version_dir(plan_id, version)
        return _status_record(path, project)
    candidates = sorted(_root().glob("p_*/v*/version.json"), key=lambda p: p.stat().st_mtime_ns)
    if not candidates:
        return {"stage": "no_plans", "next_actor": "User may start /plan"}
    return _status_record(candidates[-1].parent, project)

def _status_record(path: Path, project: project_rooms.ProjectContext) -> dict:
    record = _version_record(path, project)
    if record["stage"] == "phase_a_launch_intent" and not (path / "a-intent.json").is_file():
        record["stage"], record["next_actor"] = "phase_a_preparation_resumable", "replay identical /plan"
        return record
    decision_file = path / "decision.json"
    if decision_file.is_file() and not (path / "b-intent.json").is_file():
        decision = _read(decision_file)
        if decision["action"] == "accept":
            record["stage"], record["next_actor"] = "phase_b_preparation_resumable", "replay identical decision"
        elif decision["action"] == "revise" and not (path.parent / f"v{record['version']+1}" / "version.json").is_file():
            record["stage"], record["next_actor"] = "revision_preparation_resumable", "replay identical decision"
    phase = "b" if (path / "b-intent.json").is_file() else "a"
    intent_file = path / f"{phase}-intent.json"
    if intent_file.is_file():
        intent = _read(intent_file)
        replay = "replay identical /plan" if phase == "a" else "replay identical decision"
        if intent.get("state") == "prepared":
            plan_file = path / f"{phase}-plan.json"
            if not plan_file.is_file() or _sha(plan_file) != intent.get("plan_sha256"):
                record["stage"], record["next_actor"] = "stopped", "inspection"
                record["stop_reason"] = "prepared dispatch plan is missing or changed"
            else:
                record["stage"], record["next_actor"] = f"phase_{phase}_prepared_resumable", replay
        elif intent.get("state") == "settled" and record["stage"].endswith("launch_intent"):
            try:
                _accepted(path, phase)
                if phase == "a":
                    validate_output({"kind": "packet", "path": str(path / "packet.json"),
                                     "proposal": str(path / "proposal.json"),
                                     "critique": str(path / "critique.json")})
            except (PlanningError, ValueError) as exc:
                record["stage"], record["next_actor"] = "stopped", "inspection"
                record["stop_reason"] = str(exc)
            else:
                record["stage"], record["next_actor"] = f"phase_{phase}_settled_resumable", replay
        elif intent.get("state") in {"launching", "collecting"} and record["stage"].endswith("launch_intent"):
            from dispatcher import process_alive
            lease, live, known = _native_state(path, phase)
            canonical = path / f"{phase}-bundle.json"
            output = Path(intent["output_path"]) if intent.get("output_path") else None
            receipt = canonical if canonical.is_file() else output if output and output.is_file() else None
            bundle = _read(receipt) if receipt else None
            if bundle and (not bundle.get("status") or bundle.get("status") == "duplicate_suppressed"):
                bundle = _read(output) if output and output.is_file() and output != receipt else None
                if bundle and (not bundle.get("status") or bundle.get("status") == "duplicate_suppressed"):
                    bundle = None
            collecting = (intent["state"] == "collecting" and
                          intent.get("collector_host") == socket.gethostname() and
                          process_alive(intent.get("collector_pid")))
            if bundle and bundle.get("status") == "succeeded":
                record["stage"], record["next_actor"] = f"phase_{phase}_receipt_resumable", replay
            elif live:
                record["stage"], record["next_actor"] = f"phase_{phase}_native_running", "wait for or inspect existing Run"
            elif collecting:
                record["stage"], record["next_actor"] = f"phase_{phase}_collection_running", "wait for or inspect existing Run"
            elif bundle:
                record["stage"], record["next_actor"] = "stopped", "inspection"
                record["stop_reason"] = f"Run {phase.upper()} status {bundle.get('status')}"
            else:
                record["stage"] = f"phase_{phase}_native_receipt_resumable" if known else "launch_outcome_unknown"
                record["next_actor"] = replay if known else "inspect native receipt; no automatic retry"
            record["native_run_id"] = lease.get("run_id")
            record["native_lease_state"] = lease.get("status", "missing")
            record["native_task_receipts"] = lease.get("tasks") or {}
            record["native_dispatch_receipts"] = lease.get("workers") or {}
        elif intent.get("state") == "stopped" and record["stage"].endswith("launch_intent"):
            record["stage"] = "stopped"
            record["next_actor"] = "inspection"
    return record

def decide(room: rooms.Room, project: project_rooms.ProjectContext, ref: str,
           action: str, instructions: str, *, event_id: str, provenance: str,
           backend=None, deliver=checked_delivery) -> dict:
    _eligible(room, project)
    if backend is None and action == "accept":
        _deployed_contract_ready()
    if action not in {"accept", "revise", "reject"} or not event_id.strip() or not provenance.strip():
        raise PlanningError("decision requires accept|revise|reject, event ID, and provenance")
    plan_id, version = _parse_ref(ref)
    path = _version_dir(plan_id, version)
    with _lock(path.parent):
        record = _version_record(path, project)
        prior_file = path / "decision.json"
        if prior_file.is_file():
            prior = _read(prior_file)
            if (prior["action"], prior["instructions"], prior["event_id"]) != (action, instructions, event_id):
                raise PlanningError("conflicting decision replay requires a new reviewed version")
        else:
            if record["stage"] != "awaiting_user":
                raise PlanningError("this version is not awaiting a decision")
            _accepted(path, "a")
            if _sha(path / "packet.json") != record["packet_file_sha256"]:
                raise PlanningError("reviewed packet changed")
            _source_hashes(_read(path / "packet.json")["substantive_payload"])
            if action == "accept" and instructions.strip():
                raise PlanningError("acceptance with instructions needs a reviewed revision; use revise")
            prior = {"schema_version": 1, "decision_id": f"{plan_id}@{version}:{event_id}",
                     "plan_id": plan_id, "version": version, "action": action,
                     "instructions": instructions, "event_id": event_id,
                     "event_provenance": provenance,
                     "event_authentication": "local transport assertion, not cryptographic authentication",
                     "context_identity": record["context_identity"],
                     "context_digest": record["context_digest"],
                     "packet_file_sha256": record["packet_file_sha256"],
                     "packet_canonical_sha256": record["packet_canonical_sha256"],
                     "payload_canonical_sha256": record["payload_canonical_sha256"], "decided_at": _now(),
                     "authority": "finalization and execution-plan preparation only" if action == "accept" else "no phase-B launch"}
            _write(prior_file, prior, exclusive=True)
        if action == "reject":
            if record["stage"] != "rejected":
                record["stage"], record["next_actor"] = "rejected", "none"
                return _publish(path, record, project, deliver)
            return record
        if action == "revise":
            if record["stage"] != "revision_requested":
                record["stage"], record["next_actor"] = "revision_requested", "Athena on new version"
                _publish(path, record, project, deliver)
            next_event = f"revision:{prior['decision_id']}"
        else:
            intent_file, plan_file = path / "b-intent.json", path / "b-plan.json"
            if not intent_file.is_file():
                if not plan_file.is_file():
                    _write(plan_file, _plan("b", plan_id, version, project, path, record["objective"]), exclusive=True)
                _write(intent_file, {"state": "prepared", "created_at": _now(),
                                     "decision_sha256": _sha(prior_file),
                                     "plan_sha256": _sha(plan_file)}, exclusive=True)
                record["stage"], record["next_actor"] = "phase_b_launch_intent", "Athena"
                _write(path / "version.json", record)
    if action == "revise":
        return start(room, project, record["objective"], event_id=next_event,
                     provenance=f"revision of {prior['decision_id']}",
                     backend=backend, deliver=deliver, plan_id=plan_id,
                     version=version + 1, instructions=instructions)
    bundle = _resume_dispatch(path, "b", backend)
    if bundle is None:
        return _status_record(path, project)
    with _lock(path.parent):
        record = _version_record(path, project)
        if bundle.get("status") == "succeeded":
            _accepted(path, "b")
            record["stage"], record["next_actor"] = "execution_plan_proposed", "User review"
        else:
            record["stage"], record["next_actor"] = "stopped", "inspection"
            record["stop_reason"] = f"Run B status {bundle.get('status')}"
        return _publish(path, record, project, deliver)

def command(room: rooms.Room, project: project_rooms.ProjectContext, line: str,
            *, event_id: str, provenance: str, backend=None,
            deliver=checked_delivery) -> dict:
    if line.startswith("/plan status"):
        parts = line.split()
        if len(parts) > 3:
            raise PlanningError("usage: /plan status [plan-id@version]")
        return status(room, project, parts[2] if len(parts) == 3 else None)
    if line.startswith("/plan "):
        return start(room, project, line[6:].strip(), event_id=event_id,
                     provenance=provenance, backend=backend, deliver=deliver)
    match = re.fullmatch(r"/decide\s+(\S+)\s+(accept|revise|reject)(?:\s+([\s\S]*))?", line)
    if match:
        return decide(room, project, match.group(1), match.group(2),
                      match.group(3) or "", event_id=event_id,
                      provenance=provenance, backend=backend, deliver=deliver)
    raise PlanningError("usage: /plan <objective> | /plan status [plan-id@version] | /decide plan-id@version accept|revise|reject [instructions]")
