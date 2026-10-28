
from __future__ import annotations

import json
import os
import tempfile
import fcntl
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 2
TERMINAL = {"completed", "partial", "failed", "awaiting_user", "rejected",
            "answered_locally", "awaiting_owner"}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def default_state_path(vault_root: Optional[Path] = None) -> Path:
    root = vault_root or Path(
        os.environ.get(
            "VAULT_ROOT",
            Path.home() / "Research",
        )
    )
    return root / "system" / "memory" / "agents" / "praxis-switchboard" / "state.json"

class SwitchboardState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.unknown_path = self.path.with_suffix(self.path.suffix + '.continuations-unknown')
        self.continuation_state_known = True
        self.data: Dict[str, Any] = self._load()

    def _hold_unknown(self, raw: bytes) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)
        quarantine = self.path.with_suffix(self.path.suffix + '.corrupt-' +
                                          hashlib.sha256(raw).hexdigest())
        for path, content in ((quarantine, raw),
                              (self.unknown_path, (str(quarantine) + '\n').encode())):
            try:
                with path.open('xb') as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
            except FileExistsError:
                pass
        self.continuation_state_known = False

    def _load(self) -> Dict[str, Any]:
        raw = b''
        if not self.path.exists():
            data = {}
        else:
            try:
                raw = self.path.read_bytes()
                data = json.loads(raw)
                if (not isinstance(data, dict) or
                        not isinstance(data.get('continuations', {}), dict) or
                        not isinstance(data.get('continuation_reconciliations', {}), dict)):
                    raise ValueError('malformed continuation state')
                if (any(not isinstance(value, str) for value in data.get('continuations', {}).values()) or
                        any(not isinstance(entries, dict) or any(
                            not isinstance(proof, dict) or not isinstance(proof.get('path'), str) or
                            not isinstance(proof.get('sha256'), str)
                            for proof in entries.values())
                            for entries in data.get('continuation_reconciliations', {}).values())):
                    raise ValueError('malformed continuation claim or reconciliation')
            except (ValueError, OSError):
                self._hold_unknown(raw)
                data = {}
        if data.get('continuations_unknown') or self.unknown_path.exists():
            if not self.unknown_path.exists():
                self._hold_unknown(raw)
            self.continuation_state_known = False
            data['continuations_unknown'] = True
        data.setdefault("schema_version", SCHEMA_VERSION)
        for key in ("requests", "by_event", "by_thread", "project_threads"):
            data.setdefault(key, {})
        data.setdefault("last_event_id", None)
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            disk = self._load()
            for key in ("requests", "by_event", "by_thread", "project_threads"):
                disk.setdefault(key, {}).update(self.data.get(key) or {})

            for key in ('continuations', 'continuation_reconciliations'):
                for claim, value in (self.data.get(key) or {}).items():
                    disk.setdefault(key, {}).setdefault(claim, value)
            if not self.continuation_state_known or self.data.get('continuations_unknown'):
                if not self.unknown_path.exists():
                    self._hold_unknown(self.path.read_bytes() if self.path.exists() else b'')
                disk['continuations_unknown'] = True
            if self.data.get("last_event_id") is not None:
                disk["last_event_id"] = self.data["last_event_id"]
            disk["schema_version"] = SCHEMA_VERSION
            self.data = disk
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(self.data, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def seen_event(self, event_id: Optional[str]) -> Optional[str]:

        if not event_id:
            return None
        return self.data["by_event"].get(str(event_id))

    def claim_continuation(self, key: str, request_id: str, *, context_digest: str = '') -> Optional[str]:

        if not self.continuation_state_known:
            raise ValueError('continuation delivery state was unreadable; inspect prior attempts')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + '.lock').open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                                                                          
            disk = self._load()
            if not self.continuation_state_known:
                raise ValueError('continuation claims are unknown; canonical Leo must reconcile prior attempts')
            claims = disk.setdefault('continuations', {})
            if not isinstance(claims, dict):
                raise ValueError('continuation claims are malformed; inspect prior attempts')
            prior = claims.get(key)

            if context_digest:
                for old_key, old_request in claims.items():
                    if old_key == key or not old_key.startswith(context_digest + ':'):
                        continue
                    old = (disk['requests'].get(old_request) or {}).get('continuation') or {}
                    if not old.get('instruction'):
                        raise ValueError('historical continuation claim needs owner reconciliation')
                    if context_digest + ':' + old['instruction']['sha256'] == key:
                        prior = old_request
            if prior:
                proof = (disk.get('continuation_reconciliations', {}).get(key) or {}).get(prior)
                if not proof:
                    return prior
                path = Path(proof['path'])
                if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != proof['sha256']:
                    raise ValueError('no-launch reconciliation evidence changed; retain the claim')
            claims[key] = request_id
            fd, name = tempfile.mkstemp(dir=self.path.parent, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(disk, file, indent=2, sort_keys=True)
                    file.write('\n')
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(name, self.path)
                self.data['continuations'] = claims
            finally:
                Path(name).unlink(missing_ok=True)
        return None

    def reconcile_continuation(self, key: str, request_id: str, proof: dict) -> None:

        with self.path.with_suffix(self.path.suffix + '.lock').open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            disk = self._load()
            if not self.continuation_state_known or disk.get('continuations', {}).get(key) != request_id:
                raise ValueError('unknown or changed claim; reconciliation cannot release it')
            disk.setdefault('continuation_reconciliations', {}).setdefault(key, {})[request_id] = proof
            fd, name = tempfile.mkstemp(dir=self.path.parent, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(disk, file, indent=2, sort_keys=True)
                    file.write('\n')
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(name, self.path)
                self.data = disk
            finally:
                Path(name).unlink(missing_ok=True)

    def record_event(self, event_id: Optional[str], request_id: str) -> None:
        if event_id:
            self.data["by_event"][str(event_id)] = request_id
            self.data["last_event_id"] = str(event_id)

    def request_for_thread(self, thread_id: Optional[str]) -> Optional[str]:
        if not thread_id:
            return None
        return self.data["by_thread"].get(str(thread_id))

    def bind_thread(self, thread_id: Optional[str], request_id: str) -> None:
        if thread_id:
            self.data["by_thread"][str(thread_id)] = request_id

    def project_for_thread(self, thread_id: Optional[str]) -> Optional[Dict[str, Any]]:

        if not thread_id:
            return None
        return self.data["project_threads"].get(str(thread_id))

    def bind_project_thread(self, thread_id: str, *, project_note: str,
                            project_title: str, roster: str,
                            transport: str, context_kind: str = "project") -> Dict[str, Any]:
        binding = {
            "thread_id": str(thread_id),
            "project_note": str(project_note),
            "project_title": str(project_title),
            "context_kind": str(context_kind),
            "roster": str(roster),
            "transport": str(transport),
            "created_at": now_iso(),
        }
        self.data["project_threads"][str(thread_id)] = binding
        return binding

    def create_request(self, request_id: str, **fields: Any) -> Dict[str, Any]:
        record = {
            "request_id": request_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "lifecycle": "accepted",
            "run_id": None,
            "recipients": [],
            "tasks": {},
            "artifacts": [],
            "notes": [],
        }
        record.update(fields)
        self.data["requests"][request_id] = record
        return record

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.data["requests"].get(request_id)

    def update(self, request_id: str, **fields: Any) -> Dict[str, Any]:
        rec = self.data["requests"].setdefault(request_id, {"request_id": request_id})
        rec.update(fields)
        rec["updated_at"] = now_iso()
        return rec

    def set_task(self, request_id: str, seat: str, **fields: Any) -> None:
        rec = self.data["requests"].setdefault(request_id, {"request_id": request_id})
        tasks = rec.setdefault("tasks", {})
        task = tasks.setdefault(seat, {"seat": seat, "state": "pending"})
        task.update(fields)
        rec["updated_at"] = now_iso()

    def unfinished(self) -> list:

        return [r for r in self.data["requests"].values()
                if r.get("lifecycle") not in TERMINAL]
