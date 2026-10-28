
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import threading
import unicodedata
from datetime import datetime
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

import registry
import research_trace
import rooms
from routing import MENTION, Sender

DIGEST_BYTES = 6

_NON_TOKEN = re.compile(r"[^a-z0-9]+")

@dataclass(frozen=True)
class ProjectContext:
    title: str
    note: Path
    kind: str = "project"

    @property
    def address(self) -> str:
        return self.note.stem

    @property
    def thread_id(self) -> str:
        return f"context-room:{self.kind}:{context_token(self)}"

def context_identity(project: "ProjectContext") -> str:

    note = Path(project.note)
    try:
        vault = registry.vault_root()
    except registry.RegistryError:                                              
        vault = None
    for base in ([vault, Path(vault).resolve()] if vault else []):
        for candidate in (note, _safe_resolve(note)):
            try:
                relative = Path(candidate).relative_to(base)
            except ValueError:
                continue
            return unicodedata.normalize("NFC", relative.as_posix())

    return unicodedata.normalize("NFC", _safe_resolve(note).as_posix())

def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:                                                        
        return path

def context_digest(project: "ProjectContext") -> str:

    identity = f"{project.kind}\n{context_identity(project)}"
    return hashlib.blake2s(identity.encode("utf-8"),
                           digest_size=DIGEST_BYTES).hexdigest()

def _decoration(stem: str) -> str:

    folded = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    return _NON_TOKEN.sub("-", folded.lower()).strip("-")[:28].strip("-")

def context_token(project: "ProjectContext") -> str:

    decoration = _decoration(project.note.stem)
    digest = context_digest(project)
    return f"{decoration}-{digest}" if decoration else digest

def legacy_token(project: "ProjectContext") -> str:

    token = _NON_TOKEN.sub("-", project.note.stem.lower()).strip("-")
    if project.kind != "project":
        token = f"{project.kind}-{token}"
    return token

def room_token(project: "ProjectContext") -> str:
    token = context_token(project)
    return token if project.kind == "project" else f"{project.kind}-{token}"

def output_root(project: ProjectContext) -> Path:

    root = registry.institution() / "rooms" / "projects"
    canonical = root / room_token(project)
    if canonical.exists():
        return canonical
    legacy = root / legacy_token(project)
    if legacy != canonical and legacy.is_dir() and _legacy_belongs(legacy, project):
        return legacy
    return canonical

_LEGACY_LINK = re.compile(
    r"(?m)^Canonical (?:project|context):\s*(?:\[\[([^\]|#]+)[^\]]*\]\]"
    r"|\[([^\]]+)\]\(([^)]+)\))")

_STEM_COUNTS: Dict[str, Dict[str, int]] = {}

def _legacy_belongs(legacy: Path, project: "ProjectContext") -> bool:

    index = legacy / "ROOM.md"
    if not index.is_file():
        return False
    try:
        head = index.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    match = _LEGACY_LINK.search(head)
    if not match:
        return False
    wiki, _name, target = match.group(1), match.group(2), match.group(3)
    if target:
        declared = _safe_resolve(legacy / target)
        return declared == _safe_resolve(Path(project.note))
    stem = unicodedata.normalize("NFC", (wiki or "").strip())
    if stem != unicodedata.normalize("NFC", project.note.stem):
        return False
    return _stem_is_unique(project)

def _stem_is_unique(project: "ProjectContext") -> bool:
    counts = _STEM_COUNTS.get(project.kind)
    if counts is None:
        counts = {}
        for kind, root in context_roots():
            if kind != project.kind:
                continue
            for path in _iter_context_notes(root):
                key = unicodedata.normalize("NFC", path.stem)
                counts[key] = counts.get(key, 0) + 1
        _STEM_COUNTS[project.kind] = counts
    return counts.get(unicodedata.normalize("NFC", project.note.stem), 0) <= 1

def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())

def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if token}

def _title(path: Path) -> str:

    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter = re.search(r"(?ms)\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text)
    if frontmatter:
        match = re.search(r'(?m)^title:\s*["\']?(.+?)["\']?\s*$', frontmatter.group(1))
        if match:
            return match.group(1).strip()
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return heading.group(1).strip() if heading else path.stem

def _iter_context_notes(source: Path):

    if source.is_file():
        if source.suffix.lower() == ".md":
            yield source
        return
    if source.is_dir():
        for path in sorted(source.rglob("*.md")):
            if "_archive" not in path.relative_to(source).parts:
                yield path

def context_roots(vault: Optional[Path] = None) -> list[tuple[str, Path]]:
    root = Path(vault or registry.vault_root())
    values = [("project", root / "projects"), ("paper", root / "papers"),
              ("system-development", root / "system" / "praxis" / "Praxis Dev.md")]
    return [(kind,path) for kind,path in values if path.is_dir() or path.is_file()]

CONTEXT_LOADERS: Dict[str, Optional[str]] = {"global-workspace": None, "room": None, "project": "project", "paper": "paper", "system-development": "praxis-dev"}

def loader_name(kind: str) -> Optional[str]:

    try:
        return CONTEXT_LOADERS[kind]
    except KeyError as exc:
        raise registry.RegistryError(
            f"no context loader is declared for kind {kind!r}; add it to "
            f"CONTEXT_LOADERS (map it to None if it deliberately has none)"
        ) from exc

def context_loader_name(project: ProjectContext) -> Optional[str]:
    return loader_name(project.kind)

def loader_path(project: ProjectContext, route: "registry.Route") -> Optional[Path]:

    name = context_loader_name(project)
    if name is None:
        return None
    path = registry.agent_skill_root(route.agent) / name / "SKILL.md"
    if not path.is_file():
        raise registry.RegistryError(
            f"context {project.title!r} cannot launch: the {project.kind} loader "
            f"is not deployed for {route.agent}: {path}")
    return path

def context_block(project: Optional[ProjectContext],
                  route: Optional["registry.Route"] = None) -> str:

    if project is None:
        return ""
    loader = loader_path(project, route) if route is not None else None
    lines = [
        f"CANONICAL CONTEXT — {project.title} [{project.kind}] · "
        f"{context_digest(project)}",
        "",
        f"Canonical note:   {project.note}",
        f"Context identity: {context_identity(project)}",
        f"Context digest:   {context_digest(project)}",
    ]
    if project.kind == 'global-workspace':
        north_star = registry.vault_root()/'North-Star.md'
        if not north_star.is_file(): raise registry.RegistryError('canonical North-Star source missing')
        lines.extend(['Canonical North-Star: '+str(north_star),
                      'North-Star sha256: '+hashlib.sha256(north_star.read_bytes()).hexdigest(),
                      'Global workspace context is not a project selection. Ambiguous project work holds only that dependent work; never invent a project from the objective.'])
    if loader is not None:
        lines.extend([
            f"Context loader:   {loader}",
            "",
            "Read the loader in full, then load this context through it before "
            "substantive work.",
        ])
    else:
        lines.extend([
            f"Context loader:   none is registered for a {project.kind} context.",
            "",
            "Read the canonical note in full before substantive work.",
        ])
    lines.append(
        "Do not infer the context from the prompt, from a filename, or from a "
        "prior request directory. If the note or the loader cannot be read, stop "
        "and report the configuration failure rather than improvising the "
        "context.")
    return "\n".join(lines)

def _resolve_context(query: str, roots: list[tuple[str, Path]]) -> ProjectContext:

    candidates = []
    seen: set[Path] = set()
    for kind, root in roots:
        for path in _iter_context_notes(root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            title = _title(path)
            keys = {_normalise(path.stem), _normalise(title)}
            tokens = _tokens(path.stem) | _tokens(title)
            candidates.append((path, title, kind, keys, tokens))
    needle = _normalise(query)
    if not needle:
        raise registry.RegistryError("a context name is required")
    exact = [(path, title, kind) for path, title, kind, keys, _ in candidates
             if needle in keys]
    if len(exact) == 1:
        return ProjectContext(exact[0][1], exact[0][0], exact[0][2])
    query_tokens = _tokens(query)
    directory_indexes = [
        (path, title, kind) for path, title, kind, _, _ in candidates
        if _normalise(path.stem) == _normalise(f"{path.parent.name} Index")
        and query_tokens
        and all(any(token == candidate or candidate.startswith(token)
                    for candidate in _tokens(path.parent.name))
                for token in query_tokens)
    ]
    if not exact and len(directory_indexes) == 1:
        return ProjectContext(directory_indexes[0][1], directory_indexes[0][0],
                              directory_indexes[0][2])
    partial = [(path, title, kind) for path, title, kind, _, tokens in candidates
               if query_tokens and all(any(token == candidate or candidate.startswith(token)
                                           for candidate in tokens)
                                       for token in query_tokens)]
    matches = exact or partial
    if len(matches) == 1:
        return ProjectContext(matches[0][1], matches[0][0], matches[0][2])
    if not matches:
        raise registry.RegistryError(f"no canonical context matches {query!r}")
    names = ", ".join(f"{title} [{kind}]" for _, title, kind in matches[:12])
    raise registry.RegistryError(f"context {query!r} is ambiguous: {names}")

def list_contexts(vault: Optional[Path] = None) -> list[ProjectContext]:

    out: list[ProjectContext] = []
    seen: set[Path] = set()
    for kind, root in context_roots(vault):
        for path in _iter_context_notes(root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(ProjectContext(_title(path), path, kind))
    return out

def resolve_context(query: str) -> ProjectContext:
    roots = context_roots()
    if not roots:
        raise registry.RegistryError("no canonical context registries exist")
    return _resolve_context(query, roots)

def resolve_room_context(query: str, room: rooms.Room, vault: Optional[Path] = None) -> ProjectContext:
    roots = context_roots(vault)
    if not roots:
        raise registry.RegistryError("no canonical context registries exist")
    return _resolve_context(query, roots)

def resolve_project(query: str, projects_root: Optional[Path] = None) -> ProjectContext:

    if projects_root is not None:
        root = Path(projects_root)
        if not root.is_dir():
            raise registry.RegistryError(f"project registry does not exist: {root}")
        return _resolve_context(query, [("project", root)])
    return resolve_context(query)

def resolve_roster(name: Optional[str] = None) -> rooms.Room:

    if name:
        return rooms.resolve(name)
    active = rooms.list_rooms()
    if len(active) == 1:
        return active[0]
    choices = ", ".join(room.room_id for room in active) or "none"
    raise registry.RegistryError(
        f"more than one room roster is active; choose one with --roster ({choices})")

def _leading_mentions(prompt: str) -> tuple[list[str], str]:
    mentions = []
    body = prompt or ""
    while True:
        match = MENTION.match(body.lstrip())
        if not match:
            break
        mentions.append(match.group(1))
        body = body.lstrip()[match.end():]
        body = body.lstrip(" ,:")
    if not mentions:

        match = re.match(
            r"^([A-Za-z][A-Za-z0-9_-]{1,31})(?:\s*[:,]\s*|\s+(?="
            r"(?:please|can you|could you|implement|fix|repair|build|apply|review|plan)\b))",
            body, re.IGNORECASE)
        if match and any(match.group(1).lower() in
                         {name.lower(), *(a.lower() for a in seat.aliases)}
                         for name, seat in registry.seats().items()):
            mentions.append(match.group(1))
            body = body[match.end():]
    return mentions, body.strip()

def internal_execution_scope(body: str) -> str:

    if body.startswith("You are answering one prompt inside "):
        body = body.partition("\n\nUser's prompt: ")[2]
    body = body.strip()
    restriction = r"\b(?:do not|never) (?:send|submit|publish|spend|sign|deploy|restart)\b"
    external_action = r"\b(?:send|submit|publish|spend|sign|deploy|restart|email)\b"

    paragraphs = re.split(r"\n\s*\n", body)
    if len(paragraphs) > 1:
        if any(not re.match(r"^(?:Include|Verify|Map|Propose|Distinguish|Explain|Keep)\b", p)
               for p in paragraphs[1:]):
            return ""
        elaboration = "\n\n".join(paragraphs[1:])

        elaboration = re.sub(restriction, "", elaboration, flags=re.IGNORECASE)
        if re.search(r'["“”‘]|(?<!\w)[\'’]|(?:^|\n)\s*[>`]|\b(?:wait|later|tomorrow|'
                     r'if|unless|after|not|never|send|submit|publish|spend|sign|deploy|restart|'
                     r'email|deployment|approved|authorize|authorization)\b', elaboration, re.IGNORECASE):
            return ""
    clauses = paragraphs[0].split(";")
    instruction = clauses[0].strip()
    if re.search(r"[\"“”‘]|(?<!\w)['’]|(?:^|\n)\s*[>`]|\b(?:not|never|don't|do not|"
                 r"only|advisory|proposal|if|unless|when|after|send|submit|"
                 r"publish|spend|sign|deploy|restart|email|nothing|wait|later|"
                 r"tomorrow|yet|undecided|haven't|hasn't|isn't|wasn't|can't|"
                 r"proposed|suggested|earlier|previous|historical|unseen|or)\b|/plan",
                 instruction, re.IGNORECASE):
        return ""

    if "?" in instruction and not (
            re.match(r"^(?:can|could) you\s+", instruction, re.IGNORECASE)
            and instruction.endswith("?") and instruction.count("?") == 1):
        return ""

    if any(not re.fullmatch(r"\s*" + restriction +
                            r"(?:(?!\b(?:but|then|and)\b)[^,;.!?])*[.!?]*",
                            clause, re.IGNORECASE)
           or re.search(external_action,
                        re.sub(restriction, "", clause, flags=re.IGNORECASE),
                        re.IGNORECASE)
           for clause in clauses[1:]):
        return ""
    match = re.match(
        r"^(?:please\s+|(?:can|could) you\s+)?(?:plan and\s+)?"
        r"(?:implement|fix|repair|build|apply)\s+(.+?)[.!?]*$", instruction, re.IGNORECASE)
    if not match:
        return ""
    scope = match.group(1).strip()

    if len(scope) < 8 or not re.search(
            r"\b(?:internal|code|file|repo|repository|test|tool|script|bug|"
            r"handoff|hand off|model allocation|patch|implementation)\b|[\w.-]+\.(?:py|md|json|yaml)\b",
            scope, re.IGNORECASE):
        return ""
    return body

def contextual_request(room: rooms.Room, project: ProjectContext, prompt: str) -> str:

    mentions, body = _leading_mentions(prompt.strip())
    if not body:
        raise registry.RegistryError("a prompt is required")
    import room_commands
    import runner
    lifecycle = len(mentions) == 1 and mentions[0].lower() == 'leo' and runner.leo_lifecycle(body)
    if mentions and room_commands.token(body) and not lifecycle:
        raise registry.RegistryError('room_command_not_dispatched: '+room_commands.token(body))
    known = registry.seats()
    roster = {name.lower(): name for name in room.recipients}
    for name in room.recipients:
        for alias in known[name].aliases:
            roster[alias.lower()] = name
    if not mentions:
        recipients = [room.chair]
    elif len(mentions) == 1 and mentions[0].lower() in {"all", "room"}:
        recipients = room.recipients
    else:
        recipients = []
        outside = []
        for mention in mentions:
            canonical = roster.get(mention.lower())
            if canonical:
                if canonical not in recipients:
                    recipients.append(canonical)
            else:
                outside.append("@" + mention)
        if outside:
            raise registry.RegistryError(
                f"not in {room.title}: {', '.join(outside)}; use `sb seats` for direct access")
    addresses = " ".join("@" + name for name in recipients)

    return (
        f"{addresses} You are answering one prompt inside the {project.title} context room. "
        f"Read only the sources required by your seat, the canonical context and the prompt. "
        f"The canonical note is the durable context; this prompt is not a declared agenda "
        f"and does not authorize work beyond its words.\n\nUser's prompt: {body}"
    )

class ProjectRoomSession:

    def __init__(self, room: rooms.Room, project: ProjectContext,
                 board_factory: Callable[[], object], sender: Sender,
                 post_factory: Callable[[], Callable[[Dict], None]], *,
                 max_parallel_prompts: int = 1,
                 output_dir: Optional[Path] = None):
        self.room = room
        self.project = project
        self.board_factory = board_factory
        self.sender = sender
        self.post_factory = post_factory
        self.executor = ThreadPoolExecutor(max_workers=max_parallel_prompts,
                                           thread_name_prefix="project-room")
        self._lock = threading.Lock()
        self._futures: list[Future] = []
        self._submitted = 0
        self._max_parallel = max_parallel_prompts
        self.output_root = Path(output_dir) if output_dir else output_root(project)

    def submit(self, prompt: str, *, post=None) -> int:
        mentions, body = _leading_mentions(prompt.strip())
        preview = body.lower().startswith('attractor preview:')
        if preview:
            body = body[len('attractor preview:'):].strip()
            prompt = ' '.join('@' + name for name in mentions) + ' ' + body
        request = contextual_request(self.room, self.project, prompt)
        with self._lock:
            self._submitted += 1
            number = self._submitted
        future = self.executor.submit(self._run, request, prompt.strip(), post, preview)
        with self._lock:
            self._futures.append(future)
        return number

    def _run(self, request: str, prompt: str, post=None, engine_preview=False):
        board = self.board_factory()
        post = post if post is not None else self.post_factory()
        try:
            options = {'engine_preview': True} if engine_preview else {}
            record = board.handle(request, self.sender, post,
                                  thread_id=self.project.thread_id,
                                  context=self.project,
                                  context_room_id=self.room.room_id, **options)
            index = self._record_prompt(prompt, record)
            if research_trace.enabled(self.project):
                try:
                    update = research_trace.record_turn(
                        self.project, prompt, record, room_root=self.output_root)
                    post({"type": "room_trace_recorded",
                          "record": str(update.ledger),
                          "report": str(update.report),
                          "entries": update.entries,
                          "head_hash": update.head_hash,
                          "request_id": record.get("request_id")})
                except (OSError, research_trace.TraceError) as exc:

                    post({"type": "room_trace_failed",
                          "request_id": record.get("request_id"),
                          "error": str(exc)})
            post({"type": "room_prompt_recorded", "record": str(index),
                  "request_id": record.get("request_id"),
                  "request_dir": str(self.output_root / str(record.get("request_id") or ""))})
            if self._max_parallel == 1:

                try:
                    self._recover(post, exclude=frozenset({record.get("request_id")}))
                except Exception as exc:                
                    post({"type": "note", "message": f"Earlier prompts not reconciled: {exc}"})
            return record
        except Exception as exc:                                                
            post({"type": "dispatch_failed", "error": str(exc)})
            return {"lifecycle": "failed", "reasons": [str(exc)]}

    def _record_prompt(self, prompt: str, record: Dict) -> Path:
        return record_project_prompt(self.project, prompt, record,
                                     output_dir=self.output_root)

    def active(self) -> int:
        with self._lock:
            self._futures = [future for future in self._futures if not future.done()]
            return len(self._futures)

    def recover_pending(self, post=None) -> list[Dict]:

        if self.active():
            return []                                                 
        return self._recover(post)

    def _recover(self, post=None, *, exclude: frozenset = frozenset()) -> list[Dict]:
        board = self.board_factory()
        post = post if post is not None else self.post_factory()
        digest = context_digest(self.project)
        records = list(board.state.data.get("requests", {}).values())
        recovered = []
        for record in records:
            if (record.get("lifecycle") == "pending" and record.get("context_digest") == digest
                    and record.get("request_id") not in exclude):
                try:
                    settled = board.recover(record["request_id"], post)
                except (OSError, ValueError, registry.RegistryError) as exc:
                    post({"type": "note", "message": f"Recovery remains pending: {exc}"})
                    continue
                recovered.append(settled)

                if settled and settled.get("lifecycle") != "pending":
                    try:
                        index = record_project_settlement(self.project, settled,
                                                          output_dir=self.output_root)
                        post({"type": "room_prompt_settled", "record": str(index),
                              "request_id": settled.get("request_id"),
                              "lifecycle": settled.get("lifecycle")})
                    except OSError as exc:
                        post({"type": "note",
                              "message": f"Settled {settled.get('request_id')} but the room "
                                         f"index was not updated: {exc}"})
        return recovered

    def trace_status(self) -> Optional[research_trace.Integrity]:
        if not research_trace.enabled(self.project):
            return None
        return research_trace.verify(self.output_root)

    def render_trace(self) -> Optional[Path]:
        if not research_trace.enabled(self.project):
            return None
        return research_trace.render(self.project, self.output_root)

    def close(self, *, wait: bool = True) -> Optional[Path]:
        self.executor.shutdown(wait=wait, cancel_futures=False)
        if wait:
            return self.render_trace()
        return None

def _artifact_path(value: str) -> Path:

    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    for base in (registry.vault_root().parent, registry.vault_root(), Path.cwd()):
        candidate = (base / path).resolve()
        if candidate.exists():
            return candidate
    return (registry.vault_root().parent / path).resolve()

_REJECTION_REASON_LIMIT = 200

def _rejection_marker(record: Dict) -> str:

    reasons = [" ".join(str(reason).split())
               for reason in (record.get("reasons") or [])
               if str(reason).strip()]
    joined = "; ".join(reasons)[:_REJECTION_REASON_LIMIT]
    why = joined or "no reason was recorded"
    return f"[Not dispatched — {why}. Prompt deliberately not recorded.]"

def record_project_prompt(project: ProjectContext, prompt: str, record: Dict,
                          *, output_dir: Optional[Path] = None) -> Path:

    root = Path(output_dir) if output_dir else output_root(project)
    root.mkdir(parents=True, exist_ok=True)
    index = root / "ROOM.md"
    lock_path = root / ".ROOM.md.lock"
    request_id = str(record.get("request_id") or "unknown")
    request_dir = root / request_id
    lifecycle = str(record.get("lifecycle") or "unknown")

    safe_prompt = _rejection_marker(record) if lifecycle == "rejected" else prompt
    timestamp = datetime.now().astimezone().isoformat(timespec="minutes")
    artifacts = [_artifact_path(value) for value in (record.get("artifacts") or [])]

    def link(target_path: Path) -> str:
        try:
            target = os.path.relpath(target_path, root)
        except ValueError:
            target = str(target_path)
        return f"[{target_path.name}]({target})"

    lines = [
        f"\n## {timestamp} · {request_id}",
        "",
        f"**Prompt:** {safe_prompt}",
        f"**Status:** `{lifecycle}`",
    ]
    request_file = request_dir / "request.md"
    if request_file.exists():
        lines.append(f"**Request:** {link(request_file)}")
    if artifacts:
        lines.extend(["", "**Agent outputs:**"])
        lines.extend(f"- {link(target_path)}" for target_path in artifacts)
    lines.append("")

    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if not index.exists():
                relative = os.path.relpath(project.note, root)
                index.write_text(
                    "---\ntype: project-room\nstatus: active\n---\n\n"
                    f"# {project.title} — Room\n\n"
                    f"Canonical project: [{project.note.name}]({relative})\n\n"
                    "Each entry is one prompt and one fresh Orca Run. Agent outputs "
                    "remain separate files and are linked below.\n",
                    encoding="utf-8")
            with index.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return index

def record_project_settlement(project: ProjectContext, record: Dict,
                              *, output_dir: Optional[Path] = None) -> Path:

    root = Path(output_dir) if output_dir else output_root(project)
    root.mkdir(parents=True, exist_ok=True)
    index = root / "ROOM.md"
    lock_path = root / ".ROOM.md.lock"
    request_id = str(record.get("request_id") or "unknown")
    lifecycle = str(record.get("lifecycle") or "unknown")
    timestamp = datetime.now().astimezone().isoformat(timespec="minutes")
    artifacts = [_artifact_path(value) for value in (record.get("artifacts") or [])]

    def link(target_path: Path) -> str:
        try:
            target = os.path.relpath(target_path, root)
        except ValueError:
            target = str(target_path)
        return f"[{target_path.name}]({target})"

    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            text = index.read_text(encoding="utf-8") if index.exists() else ""
            lines = text.split("\n")
            heading = re.compile(r"^## .* · " + re.escape(request_id) + r"\s*$")
            start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
            settled = f"**Settled:** {timestamp} · `{lifecycle}` (late completion reconciled from Orca)"
            if start is None:
                block = ["", f"## {timestamp} · {request_id}", "",
                         "**Prompt:** [recorded in request.md]",
                         f"**Status:** `{lifecycle}`", settled]
                request_file = root / request_id / "request.md"
                if request_file.exists():
                    block.append(f"**Request:** {link(request_file)}")
                if artifacts:
                    block.extend(["", "**Agent outputs:**"])
                    block.extend(f"- {link(path)}" for path in artifacts)
                block.append("")
                lines.extend(block)
            else:
                end = next((i for i in range(start + 1, len(lines))
                            if lines[i].startswith("## ")), len(lines))
                section = lines[start:end]
                section = [f"**Status:** `{lifecycle}`" if line.startswith("**Status:**") else line
                           for line in section]
                section = [line for line in section if not line.startswith("**Settled:**")]
                status_at = next((i for i, line in enumerate(section)
                                  if line.startswith("**Status:**")), None)
                if status_at is None:
                    section.insert(2 if len(section) > 2 else len(section),
                                   f"**Status:** `{lifecycle}`")
                    status_at = section.index(f"**Status:** `{lifecycle}`")
                section.insert(status_at + 1, settled)
                missing = [path for path in artifacts
                           if f"]({os.path.relpath(path, root)})" not in "\n".join(section)
                           and f"]({path})" not in "\n".join(section)]
                if missing:
                    while section and section[-1] == "":
                        section.pop()
                    if "**Agent outputs:**" not in section:
                        section.extend(["", "**Agent outputs:**"])
                    section.extend(f"- {link(path)}" for path in missing)
                    section.append("")
                lines[start:end] = section
            content = "\n".join(lines)
            temp = index.with_name(".ROOM.md.settling")
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, index)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return index
