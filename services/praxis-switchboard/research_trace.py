
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
LEDGER_NAME = "RESEARCH-TRACE.jsonl"
HEAD_NAME = "RESEARCH-TRACE.head.json"
LOCK_NAME = ".RESEARCH-TRACE.lock"
ZERO_HASH = "0" * 64

_FRONTMATTER = re.compile(r"(?ms)\A---\s*\n(.*?)\n---\s*(?:\n|\Z)")
_MENTION = re.compile(r"^@([A-Za-z][A-Za-z0-9_-]*)\b")
_TARGET = re.compile(
    r"^\[(shared|both|conference|conference|journal|journal|journal)\]\s*",
    re.IGNORECASE,
)
_DECISION = re.compile(r"(?mi)^\s*Decision:\s*(.+?)\s*$")
_SUPERSEDES = re.compile(r"(?mi)^\s*Supersedes:\s*(.+?)\s*$")
_OPEN_QUESTION = re.compile(r"(?mi)^\s*Open question:\s*(.+?)\s*$")
_RESOLVED = re.compile(
    r"(?mi)^\s*Resolved:\s*([^\s,:;]+)(?:\s*(?:—|--|:|-)\s*(.+?))?\s*$"
)
_ROOM_ENTRY = re.compile(
    r"(?ms)^##\s+([^\n]+?)\s+·\s+(req_[A-Za-z0-9]+)\s*\n\n"
    r"\*\*Prompt:\*\*\s*(.*?)\n\*\*Status:\*\*\s*`([^`]+)`"
)
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((?:<([^>]+)>|([^\s)]+))\)")
_URL = re.compile(r"https?://[^\s<>\"]+")

_TARGET_NAMES = {
    "shared": "Shared",
    "conference": "Conference",
    "journal": "Journal",
}

class TraceError(RuntimeError):
    pass

@dataclass(frozen=True)
class Integrity:
    ok: bool
    entries: int
    head_hash: str
    errors: tuple[str, ...] = ()

@dataclass(frozen=True)
class TraceUpdate:
    ledger: Path
    report: Path
    entries: int
    head_hash: str
    added: int

def _frontmatter_text(note: Path) -> str:
    try:
        text = Path(note).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _FRONTMATTER.search(text)
    return match.group(1) if match else ""

def _frontmatter_scalar(note: Path, key: str) -> Optional[str]:
    block = _frontmatter_text(note)
    match = re.search(
        rf"(?mi)^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", block
    )
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()

def enabled(project: object) -> bool:
    try:
        note = Path(getattr(project, "note"))
    except (AttributeError, TypeError):

        return False
    value = (_frontmatter_scalar(note, "research_trace") or "")
    return value.casefold() in {"true", "yes", "on", "research", "1"}

def _artifact_root() -> Path:
    configured = os.environ.get("PRAXIS_ARTIFACT_ROOT")
    if not configured:
        raise ValueError("set PRAXIS_ARTIFACT_ROOT for research artifacts")
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise ValueError("PRAXIS_ARTIFACT_ROOT must be absolute")
    return root.resolve()

def project_folder(project: object) -> Path:
    note = Path(getattr(project, "note"))
    configured = _frontmatter_scalar(note, "folder")
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        return path if path.is_absolute() else _artifact_root() / path
    return _artifact_root() / "projects" / note.stem

def delivery_folder(project: object, override: Optional[Path] = None) -> Path:
    if override is not None:
        return Path(override)
    note = Path(getattr(project, "note"))
    root = project_folder(project)
    trace_folder = _frontmatter_scalar(note, "research_trace_folder")
    if not trace_folder:
        return root
    path = Path(os.path.expandvars(trace_folder)).expanduser()
    return path if path.is_absolute() else root / path

def report_path(project: object, override: Optional[Path] = None) -> Path:
    return delivery_folder(project, override) / f"{Path(getattr(project, 'note')).stem} — Research Process.html"

def ledger_path(room_root: Path) -> Path:
    return Path(room_root) / LEDGER_NAME

def head_path(room_root: Path) -> Path:
    return Path(room_root) / HEAD_NAME

def _canonical_bytes(value: Dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

def _pretty(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _record_hash(record: Dict[str, Any]) -> str:
    body = dict(record)
    body.pop("record_hash", None)
    return _sha256_bytes(_canonical_bytes(body))

def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

def _write_once(path: Path, text: str) -> None:
    path = Path(path)
    if path.exists():
        if path.read_text(encoding="utf-8", errors="strict") != text:
            raise TraceError(f"immutable trace artifact changed: {path}")
        return
    _atomic_write(path, text)

def _strip_address_and_target(prompt: str) -> tuple[str, str]:
    body = prompt.strip()
    while True:
        match = _MENTION.match(body.lstrip())
        if not match:
            break
        body = body.lstrip()[match.end():].lstrip(" ,:")
    match = _TARGET.match(body)
    if not match:
        return "shared", body
    raw = match.group(1).casefold().replace(" ", "-")
    if raw in {"conference", "conference"}:
        target = "conference"
    elif raw in {"journal", "journal", "journal"}:
        target = "journal"
    else:
        target = "shared"
    return target, body[match.end():].lstrip()

def _annotations(prompt: str, request_id: str) -> Dict[str, Any]:
    target, body = _strip_address_and_target(prompt)
    supersedes = []
    for match in _SUPERSEDES.finditer(body):
        supersedes.extend(
            item.strip() for item in re.split(r"[,;]", match.group(1)) if item.strip()
        )
    decisions = [
        {
            "id": f"D-{request_id}-{number:02d}",
            "text": match.group(1).strip(),
            "supersedes": list(supersedes),
        }
        for number, match in enumerate(_DECISION.finditer(body), 1)
    ]
    questions = [
        {"id": f"Q-{request_id}-{number:02d}", "text": match.group(1).strip()}
        for number, match in enumerate(_OPEN_QUESTION.finditer(body), 1)
    ]
    resolutions = [
        {"question_id": match.group(1).strip(), "note": (match.group(2) or "").strip()}
        for match in _RESOLVED.finditer(body)
    ]
    return {
        "target": target,
        "decisions": decisions,
        "open_questions": questions,
        "resolutions": resolutions,
    }

def _trace_prompt(prompt: str, record: Dict[str, Any]) -> str:

    if str(record.get("lifecycle") or "") != "rejected":
        return prompt
    reasons = [
        " ".join(str(reason).split())
        for reason in (record.get("reasons") or []) if str(reason).strip()
    ]
    why = "; ".join(reasons)[:200] or "no reason was recorded"
    return f"[Not dispatched — {why}. Prompt deliberately not recorded.]"

def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}

def _seat_name(unit: Dict[str, Any]) -> str:
    title = str(unit.get("title") or "")
    if " — " in title:
        return title.split(" — ", 1)[0].strip()
    value = str(unit.get("id") or "Unknown")
    return value[:1].upper() + value[1:]

def _vault_root(project: object) -> Path:
    note = Path(getattr(project, "note")).resolve()
    for parent in (note, *note.parents):
        if parent.name == "Research" and (parent / "system").is_dir():
            return parent
    return note.parent

def _resolve_artifact(value: str, project: object, room_root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve() if path.exists() else path
    vault = _vault_root(project)
    candidates = []
    parts = path.parts
    if parts and parts[0] == "Research":
        candidates.append(vault.parent / path)
    candidates.extend((vault / path, vault.parent / path, _artifact_root() / path, room_root / path))
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else path.resolve()

def _artifact(value: str, project: object, room_root: Path) -> Dict[str, Any]:
    path = _resolve_artifact(value, project, room_root)
    item: Dict[str, Any] = {
        "reported_path": str(value),
        "resolved_path": str(path),
        "exists_at_capture": path.exists(),
    }
    if path.is_file():
        item.update(kind="file", size=path.stat().st_size, sha256=_sha256_file(path))
    elif path.is_dir():
        item["kind"] = "directory"
    else:
        item["kind"] = "missing"
    return item

def _response_path(room_root: Path, request_id: str, seat: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", seat.casefold()).strip("-") or "agent"
    return Path(room_root) / request_id / f"{slug}-response.md"

def _bundle_units(bundle: Dict[str, Any], project: object, room_root: Path,
                  request_id: str) -> list[Dict[str, Any]]:
    responses = []
    for unit in bundle.get("units") or []:
        if not isinstance(unit, dict):
            continue
        seat = _seat_name(unit)
        done = unit.get("worker_done") or {}
        body = str(done.get("body") or done.get("summary") or "")
        response = _response_path(room_root, request_id, seat)
        if body:
            _write_once(response, body)
        route = unit.get("route") or {}
        actual = unit.get("actual_launch") or {}
        values = list(done.get("files_modified") or [])
        report = done.get("report_path")
        if report and report not in values:
            values.append(report)
        artifacts = []
        seen_artifacts = set()
        for value in values:
            item = _artifact(str(value), project, room_root)
            identity = item.get("resolved_path")
            if identity in seen_artifacts:
                continue
            seen_artifacts.add(identity)
            artifacts.append(item)
        responses.append({
            "seat": seat,
            "unit_id": str(unit.get("id") or ""),
            "status": str(unit.get("status") or "unknown"),
            "provider": str(actual.get("agent") or route.get("provider") or ""),
            "model": str(actual.get("model") or route.get("effective_model") or ""),
            "effort": str(actual.get("effort") or route.get("effort") or ""),
            "fallback": route.get("fallback"),
            "contract": unit.get("contract") or {},
            "body": body,
            "body_sha256": _sha256_text(body),
            "response_path": str(response.relative_to(room_root)) if body else None,
            "artifacts": artifacts,
        })
    return responses

def _state_units(record: Dict[str, Any], project: object, room_root: Path,
                 request_id: str) -> list[Dict[str, Any]]:
    values = []
    for seat, task in (record.get("tasks") or {}).items():
        task = task if isinstance(task, dict) else {}
        values.append({
            "seat": str(seat),
            "unit_id": str(seat).casefold(),
            "status": str(task.get("state") or "unknown"),
            "provider": "",
            "model": str(task.get("model") or ""),
            "effort": "",
            "fallback": task.get("fallback"),
            "contract": {},
            "body": "",
            "body_sha256": _sha256_text(""),
            "response_path": None,
            "artifacts": [
                _artifact(str(value), project, room_root)
                for value in (task.get("artifacts") or [])
            ],
        })
    return values

def _recorded_at(bundle: Dict[str, Any], record: Dict[str, Any], fallback: Optional[str]) -> str:
    for value in (
        bundle.get("completed_at"), bundle.get("started_at"), record.get("updated_at"),
        record.get("created_at"), fallback,
    ):
        if value:
            return str(value)
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _build_entry(project: object, prompt: str, record: Dict[str, Any], room_root: Path,
                 *, recorded_at: Optional[str] = None) -> Dict[str, Any]:
    request_id = str(record.get("request_id") or "")
    if not request_id:
        raise TraceError("a research trace requires a request id")
    request_dir = Path(room_root) / request_id
    bundle_path = request_dir / "_bundle.json"
    request_path = request_dir / "request.md"
    bundle = _load_json(bundle_path)
    responses = (_bundle_units(bundle, project, room_root, request_id)
                 if bundle else _state_units(record, project, room_root, request_id))
    safe_prompt = _trace_prompt(prompt, record)
    annotations = (_annotations(safe_prompt, request_id)
                   if str(record.get("lifecycle") or "") != "rejected"
                   else {"target": "shared", "decisions": [],
                         "open_questions": [], "resolutions": []})
    evidence: Dict[str, Any] = {}
    for label, path in (("request", request_path), ("bundle", bundle_path)):
        evidence[label] = {
            "path": str(path.relative_to(room_root)),
            "exists_at_capture": path.is_file(),
            "sha256": _sha256_file(path) if path.is_file() else None,
        }
    known_artifacts = {
        item.get("resolved_path")
        for response in responses for item in (response.get("artifacts") or [])
    }
    extra_artifacts = []
    for value in (record.get("artifacts") or []):
        item = _artifact(str(value), project, room_root)
        if item.get("resolved_path") not in known_artifacts:
            extra_artifacts.append(item)
            known_artifacts.add(item.get("resolved_path"))
    return {
        "entry_type": "research_turn",
        "request_id": request_id,
        "recorded_at": _recorded_at(bundle, record, recorded_at),
        "project": {
            "title": str(getattr(project, "title")),
            "note": str(Path(getattr(project, "note")).resolve()),
        },
        "target": annotations["target"],
        "prompt": safe_prompt,
        "prompt_sha256": _sha256_text(safe_prompt),
        "lifecycle": str(record.get("lifecycle") or bundle.get("status") or "unknown"),
        "run_id": str(record.get("run_id") or bundle.get("run_id") or ""),
        "responses": responses,
        "artifacts": extra_artifacts,
        "decisions": annotations["decisions"],
        "open_questions": annotations["open_questions"],
        "resolutions": annotations["resolutions"],
        "evidence": evidence,
    }

def _load_ledger_unlocked(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceError(f"invalid ledger JSON at line {number}: {exc}") from exc
        if not isinstance(value, dict):
            raise TraceError(f"ledger line {number} is not an object")
        records.append(value)
    return records

def _validate_chain(records: Iterable[Dict[str, Any]]) -> Integrity:
    errors = []
    previous = ZERO_HASH
    count = 0
    for count, record in enumerate(records, 1):
        if record.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"entry {count}: unsupported schema {record.get('schema_version')!r}")
        if record.get("sequence") != count:
            errors.append(f"entry {count}: sequence is {record.get('sequence')!r}")
        if record.get("previous_hash") != previous:
            errors.append(f"entry {count}: previous hash does not match")
        actual = _record_hash(record)
        if record.get("record_hash") != actual:
            errors.append(f"entry {count}: record hash does not match")
        previous = str(record.get("record_hash") or actual)
    return Integrity(not errors, count, previous if count else ZERO_HASH, tuple(errors))

def _checkpoint_value(path: Path, integrity: Integrity) -> Dict[str, Any]:
    ledger = ledger_path(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "entries": integrity.entries,
        "head_hash": integrity.head_hash,
        "ledger_sha256": _sha256_file(ledger) if ledger.is_file() else _sha256_text(""),
    }

def _write_checkpoint(room_root: Path, integrity: Integrity) -> None:
    _atomic_write(head_path(room_root), _pretty(_checkpoint_value(room_root, integrity)))

def _checkpoint_errors(room_root: Path, integrity: Integrity) -> list[str]:
    ledger = ledger_path(room_root)
    checkpoint = head_path(room_root)
    if not ledger.exists() and not checkpoint.exists() and integrity.entries == 0:
        return []
    if not checkpoint.is_file():
        return [f"{HEAD_NAME} is missing"]
    value = _load_json(checkpoint)
    errors = []
    expected = _checkpoint_value(room_root, integrity)
    for key in ("schema_version", "entries", "head_hash", "ledger_sha256"):
        if value.get(key) != expected.get(key):
            errors.append(f"head checkpoint {key} does not match")
    return errors

def _write_copies(room_root: Path, record: Dict[str, Any]) -> None:
    request_dir = Path(room_root) / str(record["request_id"])
    for response in record.get("responses") or []:
        body = str(response.get("body") or "")
        relative = response.get("response_path")
        if body and relative:
            _write_once(Path(room_root) / str(relative), body)
    _write_once(request_dir / "turn.json", _pretty(record))

def _append_unlocked(path: Path, records: list[Dict[str, Any]], entry: Dict[str, Any]) -> Dict[str, Any]:
    previous = records[-1]["record_hash"] if records else ZERO_HASH
    record = {
        "schema_version": SCHEMA_VERSION,
        "sequence": len(records) + 1,
        "previous_hash": previous,
        **entry,
    }
    record["record_hash"] = _record_hash(record)
    _write_copies(path.parent, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    records.append(record)
    return record

def _room_records(room_root: Path) -> Dict[str, Dict[str, str]]:
    index = Path(room_root) / "ROOM.md"
    if not index.is_file():
        return {}
    text = index.read_text(encoding="utf-8", errors="replace")
    return {
        match.group(2): {
            "recorded_at": match.group(1).strip(),
            "prompt": match.group(3).strip(),
            "lifecycle": match.group(4).strip(),
        }
        for match in _ROOM_ENTRY.finditer(text)
    }

def _candidate_time(request_dir: Path, room: Dict[str, str]) -> str:
    bundle = _load_json(request_dir / "_bundle.json")
    return str(bundle.get("completed_at") or bundle.get("started_at") or room.get("recorded_at") or request_dir.name)

def _backfill_unlocked(project: object, room_root: Path, records: list[Dict[str, Any]],
                       current: Optional[tuple[str, Dict[str, Any]]] = None) -> int:
    existing = {str(record.get("request_id")) for record in records}
    room_records = _room_records(room_root)
    candidates: Dict[str, tuple[str, Dict[str, Any]]] = {}
    for request_dir in Path(room_root).glob("req_*"):
        if not request_dir.is_dir() or request_dir.name in existing:
            continue
        indexed = room_records.get(request_dir.name) or {}
        if not indexed.get("prompt"):
            continue
        candidates[request_dir.name] = (
            indexed["prompt"],
            {
                "request_id": request_dir.name,
                "lifecycle": indexed.get("lifecycle") or "unknown",
            },
        )
    if current is not None:
        prompt, state = current
        request_id = str(state.get("request_id") or "")
        if request_id not in existing:
            candidates[request_id] = (prompt, state)
    ordered = sorted(
        candidates.items(),
        key=lambda item: _candidate_time(
            Path(room_root) / item[0], room_records.get(item[0]) or {}
        ),
    )
    added = 0
    for request_id, (prompt, state) in ordered:
        entry = _build_entry(
            project, prompt, state, room_root,
            recorded_at=(room_records.get(request_id) or {}).get("recorded_at"),
        )
        _append_unlocked(ledger_path(room_root), records, entry)
        added += 1
    return added

def verify(room_root: Path, *, check_copies: bool = True) -> Integrity:
    root = Path(room_root)
    lock_path = root / LOCK_NAME
    root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            records = _load_ledger_unlocked(ledger_path(root))
            integrity = _validate_chain(records)
            errors = list(integrity.errors)
            errors.extend(_checkpoint_errors(root, integrity))
            if check_copies:
                for record in records:
                    request_id = str(record.get("request_id") or "unknown")
                    turn = root / request_id / "turn.json"
                    expected = _pretty(record)
                    if not turn.is_file():
                        errors.append(f"{request_id}: turn.json is missing")
                    elif turn.read_text(encoding="utf-8", errors="replace") != expected:
                        errors.append(f"{request_id}: turn.json does not match the ledger")
                    for response in record.get("responses") or []:
                        relative = response.get("response_path")
                        if not relative:
                            continue
                        path = root / str(relative)
                        body = str(response.get("body") or "")
                        if not path.is_file():
                            errors.append(f"{request_id}: {relative} is missing")
                        elif path.read_text(encoding="utf-8", errors="replace") != body:
                            errors.append(f"{request_id}: {relative} does not match the ledger")
            return Integrity(not errors, integrity.entries, integrity.head_hash, tuple(errors))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def _safe_href(target: str, room_root: Path) -> Optional[str]:
    value = target.strip()
    parts = urlsplit(value)
    if parts.scheme in {"http", "https"} and parts.netloc:
        return value
    if parts.scheme == "file" and not parts.netloc:
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(room_root) / path
    return path.resolve().as_uri() if path.exists() else None

def _link_plain_urls(value: str) -> str:
    parts = []
    cursor = 0
    for match in _URL.finditer(value):
        raw = match.group(0)
        tail = ""
        while raw and raw[-1] in ".,;:!?":
            tail = raw[-1] + tail
            raw = raw[:-1]
        parts.append(escape(value[cursor:match.start()]))
        parts.append(f'<a href="{escape(raw, quote=True)}">{escape(raw)}</a>{escape(tail)}')
        cursor = match.end()
    parts.append(escape(value[cursor:]))
    return "".join(parts)

def _linkify(value: str, room_root: Path) -> str:
    parts = []
    cursor = 0
    for match in _MARKDOWN_LINK.finditer(value):
        parts.append(_link_plain_urls(value[cursor:match.start()]))
        label = match.group(1)
        target = match.group(2) or match.group(3) or ""
        href = _safe_href(target, room_root)
        if href:
            parts.append(
                f'<a href="{escape(href, quote=True)}">{escape(label)}</a>'
            )
        else:
            parts.append(escape(match.group(0)))
        cursor = match.end()
    parts.append(_link_plain_urls(value[cursor:]))
    return "".join(parts)

def _file_link(path: str, label: Optional[str] = None) -> str:
    value = Path(path).expanduser()
    text = label or value.name or str(value)
    if value.exists():
        return f'<a href="{escape(value.resolve().as_uri(), quote=True)}">{escape(text)}</a>'
    return f"{escape(text)} <span class=missing>(missing)</span>"

def _current_artifact_path(item: Dict[str, Any], project: object) -> Path:
    path = str(item.get("resolved_path") or item.get("reported_path") or "")
    original = Path(path).expanduser()
    if original.exists():
        return original
    digest = str(item.get("sha256") or "")
    if not digest or not original.name:
        return original
    root = project_folder(project)
    if not root.is_dir():
        return original
    matches = sorted(
        candidate for candidate in root.rglob(original.name)
        if candidate.is_file() and _sha256_file(candidate) == digest
    )
    live = [candidate for candidate in matches if "_archive" not in candidate.parts]
    if len(live) == 1:
        return live[0]
    if len(matches) == 1:
        return matches[0]
    return original

def _artifact_html(item: Dict[str, Any], project: object) -> str:
    path = _current_artifact_path(item, project)
    digest = str(item.get("sha256") or "")
    detail = f" · sha256 {escape(digest[:16])}…" if digest else ""
    return f"<li>{_file_link(str(path), str(item.get('reported_path') or path))}{detail}</li>"

_STYLES = """
:root { color-scheme: light; --ink:#222521; --muted:#606660; --paper:#fffefa;
  --ground:#f3f0e9; --rule:#d9d6cc; --green:#31574b; --gold:#a66a20; --red:#9d3737; }
* { box-sizing: border-box; }
body { margin:0; background:var(--ground); color:var(--ink); font:16px/1.58 Georgia,serif; }
main { max-width:1040px; margin:0 auto; padding:44px 24px 70px; }
header { border-bottom:2px solid var(--green); padding-bottom:24px; margin-bottom:24px; }
.eyebrow,.meta,.badge,.filters,summary,.integrity,dt { font-family:system-ui,sans-serif; }
.eyebrow { color:var(--green); font-size:12px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
h1 { font-size:clamp(30px,5vw,46px); line-height:1.12; margin:7px 0 12px; }
h2 { font:700 20px/1.3 system-ui,sans-serif; color:var(--green); margin:0 0 16px; }
h3 { font:700 16px/1.35 system-ui,sans-serif; margin:20px 0 8px; }
.meta { color:var(--muted); font-size:13px; overflow-wrap:anywhere; }
.integrity { display:inline-block; margin-top:14px; padding:7px 10px; border-radius:999px;
  background:#e7f0eb; color:#23483d; font-size:12px; font-weight:700; }
.integrity.bad { background:#f7e5e2; color:var(--red); }
.overview { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:22px 0; }
.stat,.panel,.turn { background:var(--paper); border:1px solid var(--rule); border-radius:12px; }
.stat { padding:16px; }.stat strong { display:block; font:700 25px/1 system-ui,sans-serif; }
.stat span { color:var(--muted); font:12px system-ui,sans-serif; }
.panel { padding:20px 22px; margin:18px 0; }
.filters { display:flex; flex-wrap:wrap; gap:8px; margin:22px 0; }
button { border:1px solid #9da7a1; border-radius:999px; background:#fff; color:var(--ink);
  padding:7px 12px; cursor:pointer; font:600 12px system-ui,sans-serif; }
button.active { background:var(--green); color:white; border-color:var(--green); }
.turn { padding:22px 24px; margin:16px 0; }
.turn-head { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.turn-head h2 { flex:1 1 320px; margin:0; overflow-wrap:anywhere; }
.badge { border-radius:999px; background:#ece8df; color:#4a4c47; font-size:11px; padding:4px 8px; }
.badge.target { background:#e5eee9; color:#274c40; }.badge.failed { background:#f7e5e2; color:var(--red); }
.prompt,.response { white-space:pre-wrap; overflow-wrap:anywhere; }
.prompt { border-left:3px solid var(--gold); padding:8px 0 8px 15px; margin:16px 0 20px; }
.response { background:#f8f7f2; border:1px solid #e5e1d8; border-radius:8px; padding:15px; }
ul { margin:8px 0; padding-left:22px; } li { margin:5px 0; overflow-wrap:anywhere; }
a { color:#235847; text-decoration-thickness:1px; text-underline-offset:3px; }
details { border-top:1px solid #e3e0d7; margin-top:18px; padding-top:12px; }
summary { color:var(--muted); cursor:pointer; font-size:12px; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:5px 12px; font-size:13px; }
dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }.missing { color:var(--red); }
footer { color:var(--muted); font:12px/1.5 system-ui,sans-serif; margin-top:28px; }
@media (max-width:600px) { main{padding:28px 14px 45px}.turn,.panel{padding:17px}.response{padding:12px} }
@media print { body{background:white}main{max-width:none;padding:0}.filters{display:none}.turn{break-inside:avoid} }
""".strip()

def _render_html(project: object, room_root: Path, records: list[Dict[str, Any]],
                 integrity: Integrity) -> str:
    decisions = [decision for record in records for decision in (record.get("decisions") or [])]
    superseded = {
        item for decision in decisions for item in (decision.get("supersedes") or [])
    }
    questions = [question for record in records for question in (record.get("open_questions") or [])]
    resolutions = {
        resolution.get("question_id"): resolution
        for record in records for resolution in (record.get("resolutions") or [])
    }
    latest = records[-1].get("recorded_at") if records else "No turns recorded"
    targets = {target: sum(1 for record in records if record.get("target") == target)
               for target in _TARGET_NAMES}
    integrity_class = "integrity" if integrity.ok else "integrity bad"
    integrity_text = (
        f"Ledger verified · {integrity.entries} entries · head {integrity.head_hash[:16]}…"
        if integrity.ok else f"Integrity warning · {len(integrity.errors)} error(s)"
    )
    decision_html = "".join(
        "<li>"
        f"<strong>{escape(str(item.get('id')))}</strong> · {escape(str(item.get('text')))}"
        + (" <span class=badge>superseded</span>" if item.get("id") in superseded else "")
        + "</li>"
        for item in decisions
    ) or "<li>No explicit decisions recorded.</li>"
    question_html = "".join(
        "<li>"
        f"<strong>{escape(str(item.get('id')))}</strong> · {escape(str(item.get('text')))}"
        + (" <span class=badge>resolved</span>" if item.get("id") in resolutions else "")
        + "</li>"
        for item in questions
    ) or "<li>No explicit open questions recorded.</li>"

    turns = []
    for record in reversed(records):
        lifecycle = str(record.get("lifecycle") or "unknown")
        target = str(record.get("target") or "shared")
        responses = []
        artifacts = list(record.get("artifacts") or [])
        for response in record.get("responses") or []:
            artifacts.extend(response.get("artifacts") or [])
            model_bits = [str(response.get(key) or "") for key in ("provider", "model", "effort")]
            model = " · ".join(bit for bit in model_bits if bit)
            body = str(response.get("body") or "")
            responses.append(
                f'<section class="agent-response"><h3>{escape(str(response.get("seat") or "Agent"))}</h3>'
                f'<div class="meta">{escape(model or "Model not recorded")} · '
                f'{escape(str(response.get("status") or "unknown"))}</div>'
                f'<div class="response">{_linkify(body or "No response body was recorded.", room_root)}</div></section>'
            )
        unique_artifacts = []
        seen_artifacts = set()
        for item in artifacts:
            identity = item.get("resolved_path") or item.get("reported_path")
            if identity in seen_artifacts:
                continue
            seen_artifacts.add(identity)
            unique_artifacts.append(item)
        artifacts = unique_artifacts
        evidence_links = []
        for label, item in (record.get("evidence") or {}).items():
            relative = item.get("path")
            if relative:
                evidence_links.append(
                    f"<li>{escape(str(label).title())}: "
                    f"{_file_link(str(Path(room_root) / str(relative)), str(relative))}</li>"
                )
        artifact_list = "".join(_artifact_html(item, project) for item in artifacts)
        artifact_block = f"<h3>Artifacts</h3><ul>{artifact_list}</ul>" if artifact_list else ""
        failure_class = " failed" if lifecycle in {"failed", "partial", "rejected"} else ""
        turns.append(
            f'<article class="turn" data-target="{escape(target, quote=True)}">'
            f'<div class="turn-head"><h2>{escape(str(record.get("request_id") or "Unknown request"))}</h2>'
            f'<span class="badge target">{escape(_TARGET_NAMES.get(target, target))}</span>'
            f'<span class="badge{failure_class}">{escape(lifecycle)}</span></div>'
            f'<div class="meta">{escape(str(record.get("recorded_at") or ""))}</div>'
            f'<div class="prompt">{_linkify(str(record.get("prompt") or ""), room_root)}</div>'
            + "".join(responses)
            + artifact_block
            + '<details><summary>Evidence and ledger metadata</summary>'
              f'<dl><dt>Run</dt><dd>{escape(str(record.get("run_id") or "not recorded"))}</dd>'
              f'<dt>Sequence</dt><dd>{escape(str(record.get("sequence") or ""))}</dd>'
              f'<dt>Record hash</dt><dd>{escape(str(record.get("record_hash") or ""))}</dd>'
              f'<dt>Previous hash</dt><dd>{escape(str(record.get("previous_hash") or ""))}</dd></dl>'
              f'<ul>{"".join(evidence_links)}</ul></details></article>'
        )
    errors = ""
    if integrity.errors:
        errors = "<section class=panel><h2>Integrity errors</h2><ul>" + "".join(
            f"<li>{escape(error)}</li>" for error in integrity.errors
        ) + "</ul></section>"
    script = """
<script>
document.querySelectorAll('[data-filter]').forEach(function(button){
  button.addEventListener('click', function(){
    var wanted=button.getAttribute('data-filter');
    document.querySelectorAll('[data-filter]').forEach(function(b){b.classList.toggle('active',b===button);});
    document.querySelectorAll('.turn').forEach(function(card){
      card.hidden=!(wanted==='all'||card.getAttribute('data-target')===wanted);
    });
  });
});
</script>
""".strip()
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="color-scheme" content="light">\n'
        f"<title>{escape(str(getattr(project, 'title')))} · Research Process</title>\n"
        f"<style>{_STYLES}</style>\n</head>\n<body>\n<main>\n<header>"
        '<div class="eyebrow">Research process ledger</div>'
        f"<h1>{escape(str(getattr(project, 'title')))}</h1>"
        f'<div class="meta">Latest recorded turn: {escape(str(latest))}<br>'
        f'Canonical context: {_file_link(str(Path(getattr(project, "note"))), Path(getattr(project, "note")).name)}</div>'
        f'<div class="{integrity_class}">{escape(integrity_text)}</div></header>'
        '<div class="overview">'
        f'<div class="stat"><strong>{len(records)}</strong><span>room turns</span></div>'
        f'<div class="stat"><strong>{len(decisions)}</strong><span>explicit decisions</span></div>'
        f'<div class="stat"><strong>{sum(1 for q in questions if q.get("id") not in resolutions)}</strong><span>open questions</span></div>'
        f'<div class="stat"><strong>{sum(len(r.get("responses") or []) for r in records)}</strong><span>agent responses</span></div></div>'
        '<section class=panel><h2>Decision register</h2><ul>' + decision_html + "</ul></section>"
        '<section class=panel><h2>Open-question register</h2><ul>' + question_html + "</ul></section>"
        + errors
        + '<nav class="filters" aria-label="Filter turns">'
          '<button class=active data-filter=all>All</button>'
          f'<button data-filter=shared>Shared ({targets["shared"]})</button>'
          f'<button data-filter=conference>Conference ({targets["conference"]})</button>'
          f'<button data-filter=journal>Journal ({targets["journal"]})</button></nav>'
        + "".join(turns)
        + '<footer>The HTML is a deterministic projection of the append-only, hash-chained ledger. '
          'Agent responses remain attributed evidence; they are not promoted to accepted project truth.</footer>'
        + script + "\n</main>\n</body>\n</html>\n"
    )

def render(project: object, room_root: Path, *, delivery_dir: Optional[Path] = None) -> Path:
    root = Path(room_root)
    lock_path = root / LOCK_NAME
    root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            records = _load_ledger_unlocked(ledger_path(root))
            integrity = _validate_chain(records)
            checkpoint_errors = _checkpoint_errors(root, integrity)
            if not integrity.ok or checkpoint_errors:
                raise TraceError(
                    "research ledger integrity failed: "
                    + "; ".join([*integrity.errors, *checkpoint_errors])
                )
            html = _render_html(project, root, records, integrity)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    output = report_path(project, delivery_dir)
    _atomic_write(output, html)
    return output

def record_turn(project: object, prompt: str, record: Dict[str, Any], *,
                room_root: Path, delivery_dir: Optional[Path] = None) -> TraceUpdate:
    if not enabled(project):
        raise TraceError("research trace is not enabled for this context")
    root = Path(room_root)
    root.mkdir(parents=True, exist_ok=True)
    path = ledger_path(root)
    lock_path = root / LOCK_NAME
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            records = _load_ledger_unlocked(path)
            integrity = _validate_chain(records)
            integrity_errors = [*integrity.errors, *_checkpoint_errors(root, integrity)]
            if integrity_errors:
                raise TraceError("research ledger integrity failed: " + "; ".join(integrity_errors))
            request_id = str(record.get("request_id") or "")
            existing = next((item for item in records if item.get("request_id") == request_id), None)
            if existing is not None and existing.get("prompt") != _trace_prompt(prompt, record):
                raise TraceError(f"request {request_id} already has a different recorded prompt")
            added = _backfill_unlocked(project, root, records, current=(prompt, record))
            final = _validate_chain(records)
            if not final.ok:                                                    
                raise TraceError("research ledger failed after append: " + "; ".join(final.errors))
            _write_checkpoint(root, final)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    output = render(project, root, delivery_dir=delivery_dir)
    return TraceUpdate(path, output, final.entries, final.head_hash, added)

def backfill(project: object, room_root: Path, *,
             delivery_dir: Optional[Path] = None) -> TraceUpdate:
    if not enabled(project):
        raise TraceError("research trace is not enabled for this context")
    root = Path(room_root)
    root.mkdir(parents=True, exist_ok=True)
    path = ledger_path(root)
    with (root / LOCK_NAME).open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            records = _load_ledger_unlocked(path)
            integrity = _validate_chain(records)
            integrity_errors = [*integrity.errors, *_checkpoint_errors(root, integrity)]
            if integrity_errors:
                raise TraceError("research ledger integrity failed: " + "; ".join(integrity_errors))
            added = _backfill_unlocked(project, root, records)
            final = _validate_chain(records)
            _write_checkpoint(root, final)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    output = render(project, root, delivery_dir=delivery_dir)
    return TraceUpdate(path, output, final.entries, final.head_hash, added)
