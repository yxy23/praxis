
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import registry

class NamedLaunchError(ValueError):
    pass

RESERVED = {"named_contract", "seat_contract", "supplied_context"}
MARKER = "CANONICAL NAMED-SEAT CONTEXT (supplied, not proof of compliance)"

def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()

def _read(path: Path) -> tuple[str, dict]:
    try:
        data = path.read_bytes()
        content = data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise NamedLaunchError(f"required named-seat context cannot be read: {path}: {exc}") from exc
    return content, {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                     "bytes": len(data)}

def seat_for_unit(unit: dict):
    if RESERVED.intersection(unit) or str(unit.get("spec") or "").lstrip().startswith(MARKER):
        raise NamedLaunchError("caller-supplied named-seat contract refused; supply seat and task_class only")
    roster = registry.seats()
    addresses = {}
    for seat in roster.values():
        for name in [seat.name, seat.role_id, *seat.aliases]:
            addresses.setdefault(name.casefold(), []).append(seat)
    explicit = str(unit.get("seat") or "").strip().lstrip("@")
    route = unit.get("task_class")
    planning_routes = {}
    if str(route).startswith("room-planning-"):

        from room_planning import PLANNING_ROUTES
        planning_routes = PLANNING_ROUTES
    candidates = [s for s in roster.values() if route in
                  [s.route, *(e.get("route") for e in s.task_routes),
                   planning_routes.get(s.name)]]

    prose = [str(unit.get(k) or "") for k in ("spec", "objective")]
    instructions = unit.get("instructions") or []
    prose.extend([instructions] if isinstance(instructions, str) else instructions)
    identities = [m.group(1) for text in prose
                  if (m := re.match(r"\s*(?:you are|act as)\s+@?([\w-]+)", str(text), re.I))]
    claimed = {s.name for name in identities for s in addresses.get(name.casefold(), [])}
    if explicit:
        matches = {s.name: s for s in addresses.get(explicit.casefold(), [])}
        if len(matches) != 1:
            raise NamedLaunchError(f"unknown or ambiguous seat {explicit!r}; use a live registered seat")
        seat = next(iter(matches.values()))
    elif len(candidates) == 1:
        seat = candidates[0]
    elif candidates or claimed:
        raise NamedLaunchError("ambiguous named route or role prose; migrate the unit to explicit seat + registered task_class")
    else:
        if unit.get("skill") and (unit.get("task_mode") or unit.get("seat_request")):
            raise NamedLaunchError("seat_request/task_mode requires a registered seat route or explicit seat")
        return None
    if claimed - {seat.name}:
        raise NamedLaunchError(f"role prose conflicts with canonical seat {seat.name}: {sorted(claimed)}")
    mode = str(unit.get("task_mode") or "")
    if mode:
        try:
            seat = seat.for_task_mode(mode)
        except registry.RegistryError as exc:
            raise NamedLaunchError(str(exc)) from exc
    elif planning_routes.get(seat.name) == route:
        seat = replace(seat, route=route)
    elif route != seat.route:
        modes = [e.get("mode") for e in seat.task_routes if e.get("route") == route]
        if len(modes) != 1:
            raise NamedLaunchError(f"{seat.name} seat/route mismatch: {route!r}; use its registered route/task_mode")
        seat = seat.for_task_mode(modes[0])
    if route != seat.route:
        raise NamedLaunchError(f"{seat.name} seat/route mismatch: {route!r} != {seat.route!r}")
    return seat

def construct(unit: dict, agent: str) -> tuple[str, dict | None]:

    seat = seat_for_unit(unit)
    if seat is None:
        return "", None
    try:
        root = registry.agent_skill_root(agent)
    except registry.RegistryError as exc:
        raise NamedLaunchError(str(exc)) from exc
    skills = []
    for name in seat.inherited_skills:
        _, row = _read(root / name / "SKILL.md")
        skills.append(row)
    inst = registry.institution()
    _, registry_evidence = _read(inst / "ROLE-REGISTRY.yaml")
    paths = [inst / seat.charter, inst / "DECISION-RIGHTS.md"]
    knowledge = inst.parent / "agents" / seat.role_id.lower() / "KNOWLEDGE.md"
    if knowledge.exists():
        paths.append(knowledge)

    if seat.procedure == "chief-of-staff":
        procedure, _ = _read(root / seat.procedure / "SKILL.md")
        try:
            cold = procedure.split("**Read on load, in this order, and stop:**", 1)[1].split("`MODEL-REGISTRY.yaml`", 1)[0]
        except IndexError as exc:
            raise NamedLaunchError("chief-of-staff has no readable canonical cold-start declaration") from exc
        required = re.findall(r"(?m)^\d+\.\s+`<vault-root>/([^`]+)`", cold)
        if not required:
            raise NamedLaunchError("chief-of-staff cold-start paths are missing")
        paths = [registry.vault_root() / rel for rel in required] + paths
    documents = []
    blocks = []
    for path in dict.fromkeys(paths):
        content, row = _read(path)
        documents.append(row)
        blocks.append(f"BEGIN CANONICAL DOCUMENT {path}\n{content}\nEND CANONICAL DOCUMENT {path}")
    applicable = list(skills)
    request = str(unit.get("seat_request") or "").strip()

    manifest = {"seat": seat.name, "role_id": seat.role_id, "task_class": seat.route,
                "task_mode": seat.task_mode, "agent": agent, "documents": documents,
                "role_registry": registry_evidence,
                "validated_skills": skills, "applicable_skills": applicable,
                "evidence": "canonical context supplied; model compliance unverified"}
    text = (f"{MARKER}\nYou are {seat.name}; role {seat.role_id}; route {seat.route}.\n"
            "The canonical documents below are supplied in full; apply them before substantive work.\n"
            "All registered skills have been validated; inspect only the applicable entrypoints listed below,\n"
            "then read applicable skills and their task-relevant resources under their own contracts.\n"
            + "\n".join(f"- `{row['path']}`" for row in applicable)
            + "\nHASH/PATH MANIFEST\n" + json.dumps(manifest, ensure_ascii=False, sort_keys=True)
            + "\n\n" + "\n\n".join(blocks))
    manifest["context_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return text, manifest
