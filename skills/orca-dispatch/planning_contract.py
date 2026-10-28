
from __future__ import annotations

import hashlib
import json
from pathlib import Path

def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def read(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"),
                       parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"non-JSON constant {token}")))
    if not isinstance(value, dict):
        raise ValueError("planning artifact must be a JSON object")
    return value

def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _exact(item: dict, keys: set[str], kind: str) -> None:
    if set(item) != keys:
        raise ValueError(f"{kind} has missing or unapproved fields")

def validate(contract: dict) -> None:

    kind, path = contract["kind"], contract["path"]
    item = read(path)
    if type(item.get("schema_version")) is not int or item["schema_version"] != 1:
        raise ValueError("planning schema_version must be 1")
    if kind == "proposal":
        _exact(item, {"schema_version", "substantive_payload", "payload_canonical_sha256"}, kind)
        payload = item.get("substantive_payload")
        fields = ("objective", "evidence", "assumptions", "alternatives",
                  "tradeoffs", "decisions", "assignments", "risks",
                  "out_of_scope", "relied_on_sources")
        if not isinstance(payload, dict) or not _nonempty(payload.get("objective")):
            raise ValueError("empty or malformed substantive proposal")
        _exact(payload, set(fields), "substantive payload")
        if any(not isinstance(payload.get(key), list) for key in fields[1:]):
            raise ValueError("proposal lacks a required list")
        if not payload["alternatives"] or not payload["decisions"]:
            raise ValueError("proposal needs alternatives and decisions")
        if not payload["assignments"]:
            raise ValueError("proposal needs proposed assignments")
        assignment_ids = []
        for assignment in payload["assignments"]:
            if (not isinstance(assignment, dict) or
                    set(assignment) != {"id", "task", "owner", "depends_on", "acceptance", "authority"} or
                    any(not _nonempty(assignment.get(key)) for key in
                        ("id", "task", "owner", "acceptance", "authority")) or
                    not isinstance(assignment.get("depends_on"), list)):
                raise ValueError("assignment lacks exact id, task, owner, dependencies, acceptance, or authority")
            assignment_ids.append(assignment["id"])
            if assignment["authority"] != "proposal only":
                raise ValueError("assignment authority must be proposal only")
            if any(not isinstance(dep, str) or dep not in assignment_ids[:-1] for dep in assignment["depends_on"]):
                raise ValueError("assignment dependencies must name earlier assignment IDs")
        if len(set(assignment_ids)) != len(assignment_ids):
            raise ValueError("assignment IDs must be unique")
        for source in payload["relied_on_sources"]:
            if not isinstance(source, dict) or not _nonempty(source.get("path")) or not _nonempty(source.get("sha256")):
                raise ValueError("source needs path and SHA-256")
            _exact(source, {"path", "sha256"}, "relied-on source")
            if not Path(source["path"]).is_absolute() or len(source["sha256"]) != 64:
                raise ValueError("source path must be absolute with a SHA-256")
            source_path = Path(source["path"])
            if not source_path.is_file() or hashlib.sha256(source_path.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError(f"relied-on source hash mismatch: {source_path}")
        if contract.get("required_source") not in [item["path"] for item in payload["relied_on_sources"]]:
            raise ValueError("canonical context note is missing from relied-on sources")
        if item.get("payload_canonical_sha256") != digest(payload):
            raise ValueError("proposal payload hash mismatch")
    elif kind == "critique":
        _exact(item, {"schema_version", "reviewed_payload_canonical_sha256", "blockers", "improvements", "dissent", "judgment"}, kind)
        proposal = read(contract["proposal"])
        if item.get("reviewed_payload_canonical_sha256") != digest(proposal["substantive_payload"]):
            raise ValueError("critique reviews a different proposal")
        if any(not isinstance(item.get(key), list) for key in ("blockers", "improvements", "dissent")) or not _nonempty(item.get("judgment")):
            raise ValueError("empty or malformed critique")
    elif kind == "packet":
        _exact(item, {"schema_version", "substantive_payload", "proposal_payload_canonical_sha256", "critique_canonical_sha256", "dissent", "critique_blockers", "critique_improvements", "recommendation", "recommendation_by", "decisions_owed"}, kind)
        proposal, critique = read(contract["proposal"]), read(contract["critique"])
        if digest(item.get("substantive_payload")) != digest(proposal["substantive_payload"]):
            raise ValueError("packet changed the substantive proposal")
        if item.get("proposal_payload_canonical_sha256") != digest(proposal["substantive_payload"]):
            raise ValueError("packet proposal hash mismatch")
        if item.get("critique_canonical_sha256") != digest(critique):
            raise ValueError("packet critique hash mismatch")
        if digest(item.get("dissent")) != digest(critique["dissent"]):
            raise ValueError("packet suppressed or changed dissent")
        if digest(item.get("critique_blockers")) != digest(critique["blockers"]) or digest(item.get("critique_improvements")) != digest(critique["improvements"]):
            raise ValueError("packet suppressed critique findings")
        if not _nonempty(item.get("recommendation")) or item.get("recommendation_by") != "Leo" or digest(item.get("decisions_owed")) != digest(proposal["substantive_payload"]["decisions"]):
            raise ValueError("empty or malformed decision packet")
    elif kind == "final":
        _exact(item, {"schema_version", "approved_packet", "packet_canonical_sha256", "substantive_payload", "approved_payload_canonical_sha256", "dissent"}, kind)
        packet = read(contract["packet"])
        if item.get("packet_canonical_sha256") != digest(packet):
            raise ValueError("finalization packet canonical hash mismatch")
        if digest(item.get("approved_packet")) != digest(packet):
            raise ValueError("finalization changed approved packet narrative or dissent")
        if digest(item.get("substantive_payload")) != digest(packet["substantive_payload"]):
            raise ValueError("finalization changed substantive payload; new review required")
        if digest(item.get("dissent")) != digest(packet["dissent"]):
            raise ValueError("finalization changed dissent")
        if item.get("approved_payload_canonical_sha256") != digest(packet["substantive_payload"]):
            raise ValueError("finalization approval hash mismatch")
        for source in packet["substantive_payload"]["relied_on_sources"]:
            source_path = Path(source["path"])
            if not source_path.is_file() or hashlib.sha256(source_path.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError(f"relied-on source changed: {source_path}")
    elif kind == "execution":
        _exact(item, {"schema_version", "final_payload_canonical_sha256", "packet_canonical_sha256", "dissent", "proposal_status", "tasks"}, kind)
        final = read(contract["final"])
        if item.get("final_payload_canonical_sha256") != digest(final["substantive_payload"]):
            raise ValueError("execution plan refers to another final payload")
        if item.get("packet_canonical_sha256") != final["packet_canonical_sha256"] or digest(item.get("dissent")) != digest(final["dissent"]):
            raise ValueError("execution plan omitted approved packet or dissent binding")
        if item.get("proposal_status") != "proposed_for_user_review" or not isinstance(item.get("tasks"), list) or not item["tasks"]:
            raise ValueError("execution plan must be a nonbinding task proposal")
        assignments = {a["id"]: a for a in final["substantive_payload"]["assignments"]}
        if len(assignments) != len(final["substantive_payload"]["assignments"]):
            raise ValueError("approved assignment IDs are not unique")
        seen, covered = set(), set()
        ancestors: dict[str, set[str]] = {}
        last_step: dict[str, str] = {}
        assignment_steps: dict[str, list[str]] = {assignment_id: [] for assignment_id in assignments}
        for task in item["tasks"]:
            if not isinstance(task, dict):
                raise ValueError("proposed task must be an object")
            _exact(task, {"id", "assignment_id", "proposed_step", "owner", "depends_on", "acceptance", "authority"}, "proposed task")
            if not all(_nonempty(task.get(key)) for key in ("id", "assignment_id", "proposed_step", "owner", "acceptance", "authority")):
                raise ValueError("proposed task has an empty required field")
            if task["id"] in seen or task["assignment_id"] not in assignments:
                raise ValueError("proposed task has duplicate ID or unknown approved assignment")
            if not isinstance(task["depends_on"], list) or any(not isinstance(dep, str) or dep not in seen for dep in task["depends_on"]):
                raise ValueError("proposed task dependency must refer to an earlier task")
            approved = assignments[task["assignment_id"]]
            if any(task[key] != approved[key] for key in ("owner", "acceptance", "authority")):
                raise ValueError("proposed task changed approved owner, acceptance, or authority")
            reachable = set(task["depends_on"])
            for dep in task["depends_on"]:
                reachable.update(ancestors[dep])
            previous = last_step.get(task["assignment_id"])
            if previous and previous not in reachable:
                raise ValueError("proposed steps for an assignment must retain their order")
            ancestors[task["id"]] = reachable
            last_step[task["assignment_id"]] = task["id"]
            assignment_steps[task["assignment_id"]].append(task["id"])
            seen.add(task["id"])
            covered.add(task["assignment_id"])
        if covered != set(assignments):
            raise ValueError("execution proposal omits an approved assignment")
        for assignment_id, steps in assignment_steps.items():
            for predecessor in assignments[assignment_id]["depends_on"]:
                predecessor_last = last_step[predecessor]
                if any(predecessor_last not in ancestors[step] for step in steps):
                    raise ValueError("proposed task omits an approved assignment dependency")
    else:
        raise ValueError(f"unknown planning output kind: {kind}")
