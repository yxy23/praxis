"""Delivery state for the switchboard.

Deliberately small. Obsidian is institutional memory; this file is only what is
needed to avoid doing a piece of work twice and to pick up a Run that was in
flight when the process died. Nothing here is knowledge, and nothing here is
canonical: if this file were deleted, the office would lose no decisions, only
its ability to recognise a transport event it had already handled.
"""
from __future__ import annotations

import json
import os
import tempfile
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 2
TERMINAL = {"completed", "partial", "failed", "awaiting_example-user", "rejected",
            "answered_locally"}


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
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "requests": {},
                    "by_event": {}, "by_thread": {}, "project_threads": {},
                    "last_event_id": None}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt delivery-state file must not take the service down. It is
            # rebuildable by definition; reconciliation against Orca is the
            # authority on what is actually still running.
            data = {}
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
            # A standing room runs in its own Orca terminal while automations
            # and direct requests may update other request ids. Merge under the
            # lock so the last writer cannot erase a concurrent request.
            disk = self._load()
            for key in ("requests", "by_event", "by_thread", "project_threads"):
                disk.setdefault(key, {}).update(self.data.get(key) or {})
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

    # ---------------------------------------------------------------- dedup

    def seen_event(self, event_id: Optional[str]) -> Optional[str]:
        """Request id already created for this transport event, if any.

        Remote transports redeliver on reconnect, and a resume can replay an
        event the service already acted on. Keyed on the event id rather than on
        message content, because the same words sent twice on purpose are two
        requests while the same event delivered twice is one.
        """
        if not event_id:
            return None
        return self.data["by_event"].get(str(event_id))

    def record_event(self, event_id: Optional[str], request_id: str) -> None:
        if event_id:
            self.data["by_event"][str(event_id)] = request_id
            self.data["last_event_id"] = str(event_id)

    # -------------------------------------------------------------- threads

    def request_for_thread(self, thread_id: Optional[str]) -> Optional[str]:
        if not thread_id:
            return None
        return self.data["by_thread"].get(str(thread_id))

    def bind_thread(self, thread_id: Optional[str], request_id: str) -> None:
        if thread_id:
            self.data["by_thread"][str(thread_id)] = request_id

    def project_for_thread(self, thread_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Project-room binding owned by a human transport thread."""
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

    # ------------------------------------------------------------- requests

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
        """Requests that were in flight when the process stopped."""
        return [r for r in self.data["requests"].values()
                if r.get("lifecycle") not in TERMINAL]
