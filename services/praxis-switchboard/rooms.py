
from __future__ import annotations

import os
import re
import tempfile
import fcntl
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import registry

RETIRED_WRITING_ALIASES = frozenset({"writing", "writing-room", "publication-room"})

@dataclass
class Room:
    room_id: str
    title: str
    chair: str
    seats: List[str]
    record: Path
    aliases: List[str] = field(default_factory=list)
    live: bool = False
    prewarm_seats: List[str] = field(default_factory=list)
    preload_skills: List[str] = field(default_factory=list)
    preload_files: List[str] = field(default_factory=list)
    planning_template: str = ""

    @property
    def recipients(self) -> List[str]:
        return list(dict.fromkeys([self.chair, *self.seats]))

def room_registry_path() -> Path:
    return registry.institution() / "ROOM-REGISTRY.yaml"

def _room_index() -> tuple[Dict[str, Room], Dict[str, str]]:
    data = registry._load_yaml(room_registry_path())                    
    known = registry.seats()
    out: Dict[str, Room] = {}
    aliases: Dict[str, str] = {}
    for raw in data.get("rooms") or []:
        if raw.get("status") != "active":
            continue
        room_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or room_id).strip()
        chair = str(raw.get("chair") or "").strip()
        members = [str(v).strip() for v in (raw.get("seats") or []) if str(v).strip()]
        names = [chair, *members]
        missing = [name for name in names if name not in known]
        if not room_id or not chair or not members:
            raise registry.RegistryError(
                f"invalid room entry: id, chair and at least one seat are required: {raw}")
        if missing:
            raise registry.RegistryError(
                f"{title} cannot convene: unknown roster seat(s): {', '.join(missing)}")
        record = registry.institution() / str(raw.get("record") or "")
        if not record.is_file():
            raise registry.RegistryError(f"{title} record does not exist: {record}")
        prewarm = [str(v).strip() for v in (raw.get("prewarm_seats") or [])
                   if str(v).strip()]
        invalid_prewarm = [name for name in prewarm if name not in names]
        if invalid_prewarm:
            raise registry.RegistryError(
                f"{title} cannot prewarm seats outside its roster: "
                f"{', '.join(invalid_prewarm)}")
        room = Room(
            room_id=room_id,
            title=title,
            chair=chair,
            seats=members,
            record=record,
            aliases=list(raw.get("aliases") or []),
            live=bool(raw.get("live", False)),
            prewarm_seats=prewarm,
            preload_skills=[str(v).strip() for v in (raw.get("preload_skills") or [])
                            if str(v).strip()],
            preload_files=[str(v).strip() for v in (raw.get("preload_files") or [])
                           if str(v).strip()],
            planning_template=str(raw.get("planning_template") or "").strip(),
        )
        out[room_id] = room
        for alias in [room_id, *room.aliases]:
            key = str(alias).strip().lower()
            if key in aliases and aliases[key] != room_id:
                raise registry.RegistryError(
                    f"room alias {alias!r} belongs to both {aliases[key]} and {room_id}")
            aliases[key] = room_id
    return out, aliases

def resolve(name: str) -> Room:
    data, aliases = _room_index()
    key = str(name).strip().lower()
    if key in RETIRED_WRITING_ALIASES:
        raise registry.RegistryError(
            "the Writing Room was split into Paper and Column rooms; "
            "use `sb paper <paper>` or `sb column <column>`"
        )
    room_id = aliases.get(key)
    if not room_id or room_id not in data:
        choices = ", ".join(sorted(data)) or "none"
        raise registry.RegistryError(f"unknown room {name!r}; available: {choices}")
    return data[room_id]

def list_rooms() -> List[Room]:
    data, _ = _room_index()
    return [data[key] for key in sorted(data)]

def addressed_request(room: Room, agenda: str) -> str:
    mentions = " ".join("@" + name for name in room.recipients)
    relative = room.record.relative_to(registry.institution())
    return (
        f"{mentions} Convene the {room.title}. Read `{relative}` in full before "
        f"answering. Agenda from User: {agenda.strip()}"
    )

def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)

def record_convening(room: Room, request_id: str, agenda: str,
                     chair_record: Path) -> None:

    if not chair_record.is_file():
        raise registry.RegistryError(
            f"{room.title} did not close: chair record missing at {chair_record}")
    lock_path = room.record.with_suffix(room.record.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = room.record.read_text(encoding="utf-8")
            marker = f"switchboard_request: {request_id}"
            if marker in current:
                return
            count_match = re.search(r"(?m)^convenings:\s*(\d+)\s*$", current)
            count = int(count_match.group(1)) if count_match else 0
            new_count = count + 1
            today = datetime.now().astimezone().date().isoformat()
            if count_match:
                current = (current[:count_match.start()] + f"convenings: {new_count}" +
                           current[count_match.end():])
            current = re.sub(r"(?m)^last_convened:\s*.*$", f"last_convened: {today}", current)
            relative = os.path.relpath(chair_record, room.record.parent)
            chair_text = chair_record.read_text(encoding="utf-8").strip()
            block = (
                f"\n\n**Convening {new_count:03d} — {today}.**\n"
                f"<!-- {marker} -->\n"
                f"Agenda: {agenda.strip()}\n\n"
                f"Chair record: [{chair_record.name}]({relative})\n\n"
                f"{chair_text}\n"
            )
            _atomic_write(room.record, current.rstrip() + block)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
