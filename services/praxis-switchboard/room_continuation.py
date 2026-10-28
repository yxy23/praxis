
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import project_rooms
from registry import RegistryError

def prompt_body(body: str) -> str:
    if body.startswith("You are answering one prompt inside "):
        return body.partition("\n\nUser's prompt: ")[2].strip()
    return body.strip()

def credit_followup(body: str) -> str | None:

    body = prompt_body(body)
    subscription = re.fullmatch(r"continue ([A-Za-z][A-Za-z0-9_-]*) using "
                                r"(?:existing|already authorized) subscription capacity[.!]?",
                                body, re.IGNORECASE)
    if subscription:
        return subscription.group(1)
    match = re.fullmatch(
        r"(?:you (?:can|may) use|i authorize (?:you to use|use of)) "
        r"(?:the |existing )?extra credits?(?: of [0-9]+(?:\.[0-9]+)?)? "
        r"for (the task|[A-Za-z][A-Za-z0-9_-]*)[.!]?", body, re.IGNORECASE)
    return match.group(1) if match else None

def _source(item: dict, root: Path) -> Path:
    path = Path(item['path'])
    if (not path.is_absolute() or path.is_symlink() or
            not path.resolve().is_relative_to(root.resolve()) or not path.is_file()):
        raise RegistryError("continuation source is missing or outside this room")
    data = path.read_bytes()
    if not data.strip() or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise RegistryError(f"continuation source changed: {path}")
    return path

def bind(project, room_id: str, selector: str) -> dict:

    root = project_rooms.output_root(project)
    try:
        file = root / 'pending-work.json'
        if file.is_symlink():
            raise RegistryError("continuation binding may not be a symlink")
        record = json.loads(file.read_text())
        if (record['schema_version'] != 1 or record['room_id'] != room_id or
                record['context_identity'] != project_rooms.context_identity(project) or
                record['context_digest'] != project_rooms.context_digest(project)):
            raise RegistryError("continuation binding belongs to a different context")
        targets = record['pending_work']
        if not isinstance(targets, list) or len(targets) != 1:
            raise RegistryError("continuation needs exactly one bound pending scope")
        target = targets[0]
        if (target['status'] != 'pending' or target['attempt_state'] != 'not_started' or
                not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', target['id']) or
                selector.lower() not in {'the task', target['id'].lower()}):
            raise RegistryError("continuation target is not the bound unattempted pending work")
        sources = {name: _source(target[name], root) for name in
                   ('instruction', 'handoff', 'plan', 'attempt_evidence')}
        evidence = json.loads(sources['attempt_evidence'].read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(evidence['checked_at'])).total_seconds()
        if (evidence['state'] != 'not_started' or evidence['run_id'] is not None or
                evidence['task_ids'] != [] or evidence['dispatch_ids'] != [] or
                evidence['plan_sha256'] != target['plan']['sha256'] or
                evidence['context_digest'] != record['context_digest'] or
                not evidence['basis'].strip() or not 0 <= age <= 3600):
            raise RegistryError("continuation attempt evidence is stale, active, or unknown")

        result = dict(target)
        result['binding'] = {'path': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
                                                                               
        result['claim_key'] = record['context_digest'] + ':' + target['instruction']['sha256']
        return result
    except FileNotFoundError as exc:
        raise RegistryError("Continuation held for owner reconciliation by canonical Leo: "
                            "historical pending-work binding or source is missing. Inspect fresh "
                            "native Run/Task/Dispatch and lease/launch-intent evidence, then bind "
                            "only the original genuinely unattempted scope; no new User "
                            "approval is needed for this migration.") from exc
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RegistryError(f"continuation held: missing or malformed pending-work binding ({exc})") from exc

def verify(project, room_id: str, target: dict) -> None:
    if bind(project, room_id, target['id']) != target:
        raise RegistryError("continuation binding changed before coordination")

def packet(target: dict) -> str:
    return ("BOUND EXISTING PENDING WORK\n" + json.dumps(target, ensure_ascii=False, sort_keys=True) +
            "\nRead every bound source and verify its SHA-256 before proceeding. The original "
            "instruction and exact plan define scope; this current budget instruction changes only "
            "task-specific use of existing capacity. Preserve the literal amount separately from "
            "measured balance. Recheck native Run/Task/Dispatch and lease/launch-intent evidence "
            "immediately before each substantive launch. Active, completed, unknown or changed "
            "work must be held, never replayed. This is a new coordination request, not a retry "
            "or a grant to repeat any earlier launched unit. Do not rewrite the bound plan. "
            "If its instructions conflict with this update, apply the current instruction only "
            "to the credit restriction and preserve every other limit.\n")
