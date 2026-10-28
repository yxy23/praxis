
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

from adaptive_contract import digest, sha, STATES

def occurrence_key(principal, workflow, *, token=None, automation=None,
                   local_date=None, slot=None, zone=None, utc=None, fold=None):
    if not principal or not workflow:
        raise ValueError("principal and workflow required")
    if token is not None:
        if not isinstance(token, str) or not token or any(x is not None for x in
                (automation, local_date, slot, zone, utc, fold)):
            raise ValueError("manual token cannot mix schedule identity")
        return digest([principal, workflow, "manual", token])
    if not automation or type(fold) is not int or fold not in (0, 1):
        raise ValueError("complete scheduled identity and fold required")
    instant = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("UTC instant must be timezone-aware")
    resolved = instant.astimezone(ZoneInfo(zone))
    if (resolved.strftime("%Y-%m-%d") != local_date or resolved.strftime("%H:%M") != slot
            or resolved.second or resolved.microsecond or resolved.fold != fold):
        raise ValueError("local slot/UTC/fold mismatch (including nonexistent times)")
    return digest([principal, workflow, automation, local_date, slot, zone,
                   instant.astimezone(timezone.utc).isoformat(), fold])

def effect_key(operation, system, source, target, change):
    if any(not isinstance(x, str) or not x for x in (operation, system, source, target, change)):
        raise ValueError("stable effect identity required")
    return digest([operation, system, source, target, change])

def atomic_bytes(path, raw):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

class Conflict(ValueError):
    pass

class Journal:
    def __init__(self, root):
        self.root = Path(root).resolve()
                                                                                
        if not (self.root / "ADAPTIVE_FIXTURE_ROOT").is_file():
            raise ValueError("explicit isolated fixture root marker required")
        self.db = sqlite3.connect(self.root / "occurrence.sqlite", timeout=5)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS occurrences
            (id TEXT PRIMARY KEY, host TEXT, owner TEXT, fence INTEGER, bindings TEXT);
          CREATE TABLE IF NOT EXISTS nodes
            (occ TEXT, node TEXT, rev INTEGER, version INTEGER, data TEXT,
             PRIMARY KEY(occ,node,rev));
          CREATE TABLE IF NOT EXISTS journal
            (seq INTEGER PRIMARY KEY, occ TEXT, node TEXT, version INTEGER, data TEXT);
          CREATE TABLE IF NOT EXISTS effects
            (key TEXT PRIMARY KEY, data TEXT);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def acquire(self, occurrence, host, owner, bindings):
        if not host or not owner or not bindings:
            raise ValueError("explicit owner and immutable bindings required")
        frozen = json.dumps(bindings, sort_keys=True)
        with self.transaction():
            row = self.db.execute("SELECT host,owner,fence,bindings FROM occurrences WHERE id=?", (occurrence,)).fetchone()
            if row:
                if row != (host, owner, row[2], frozen):
                    raise Conflict("occurrence already owned or source/graph changed; no expiry takeover")
                return row[2]
            self.db.execute("INSERT INTO occurrences VALUES (?,?,?,?,?)", (occurrence, host, owner, 1, frozen))
        return 1

    def _fence(self, occurrence, host, owner, fence):
        row = self.db.execute("SELECT host,owner,fence FROM occurrences WHERE id=?", (occurrence,)).fetchone()
        if row != (host, owner, fence):
            raise Conflict("stale/wrong occurrence custody")

    def get(self, occurrence, node, revision=0):
        row = self.db.execute("SELECT version,data FROM nodes WHERE occ=? AND node=? AND rev=?", (occurrence, node, revision)).fetchone()
        return None if row is None else dict(json.loads(row[1]), version=row[0])

    def cas(self, occurrence, node, expected, data, *, host, owner, fence, revision=0):
        if revision != 0:
            raise ValueError("automatic/new revision not supported by this profile")
        payload = dict(data); payload.pop("version", None)
        raw = json.dumps(payload, sort_keys=True, allow_nan=False)
        with self.transaction():
            self._fence(occurrence, host, owner, fence)
            current = self.get(occurrence, node, revision)
            if (None if current is None else current["version"]) != expected:
                raise Conflict("node version CAS failed")
            version = 1 if expected is None else expected + 1
            self.db.execute("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?)", (occurrence, node, revision, version, raw))
            self.db.execute("INSERT INTO journal(occ,node,version,data) VALUES (?,?,?,?)", (occurrence, node, version, raw))
        return dict(payload, version=version)

    def intent(self, occurrence, node, inputs, custody):
        return self.cas(occurrence, node, None, {"state": "intent_persisted",
            "origin": digest([occurrence, node, 0]), "input_sha256": digest(inputs),
            "revision": 0, "native": None}, **custody)

    def bind(self, occurrence, node, native, launch_receipt, custody):
        current = self.get(occurrence, node)
        if not current or current["state"] != "intent_persisted":
            raise Conflict("one durable intent required before native binding")
        if set(native) != {"run_id", "task_id", "dispatch_id"} or any(not isinstance(v, str) or not v for v in native.values()):
            raise ValueError("full original native identity required")
        if not launch_receipt or launch_receipt.get("native") != native or launch_receipt.get("origin") != current["origin"]:
            raise ValueError("launch receipt must bind exact original identity and origin")
        return self.cas(occurrence, node, current["version"], dict(current,
            state="native_bound", native=native, launch_receipt=launch_receipt), **custody)

    def accept(self, occurrence, node, receipt, custody):
        current = self.get(occurrence, node)
        if not current or not current.get("native"):
            raise Conflict("receipt before original native binding")
        if receipt.get("native") != current["native"] or receipt.get("origin") != current["origin"]:
            raise ValueError("stale/wrong/ID-less receipt")
        if receipt.get("contract_accepted") is not True or receipt.get("state") not in {"succeeded", "failed"}:
            raise ValueError("native settlement and contract acceptance required")
        if current.get("receipt"):
            if current["receipt"] != receipt:
                raise Conflict("conflicting accepted receipt")
            return current
        return self.cas(occurrence, node, current["version"], dict(current,
            state=receipt["state"], receipt=receipt, receipt_sha256=digest(receipt)), **custody)

    def reconcile(self, occurrence, node, native, custody):

        current = self.get(occurrence, node)
        if current is None:
            return {"state": "held", "reason": "no original intent; dependent not started"}
        if current.get("receipt"):
            if digest(current["receipt"]) != current["receipt_sha256"]:
                raise ValueError("corrupt accepted receipt")
            return current
        try:
            if not current.get("native"):
                found = native.find_origin(current["origin"])
                if len(found) != 1:
                    return dict(current, state="unknown", reason="native origin not uniquely reconciled")
                current = self.bind(occurrence, node, found[0]["native"], found[0], custody)
            observed = native.read_original(current["native"])
            if observed.get("receipt"):
                return self.accept(occurrence, node, observed["receipt"], custody)
            return dict(current, state="unknown", reason="original live/unknown; never replay")
        except (OSError, TimeoutError, ConnectionError, ValueError) as exc:
            return dict(current, state="unknown", reason=type(exc).__name__)

    def checkpoint(self, occurrence, node, custody):
        current = self.get(occurrence, node)
        if not current or not current.get("receipt"):
            raise Conflict("checkpoint requires durable accepted receipt")
        return self.cas(occurrence, node, current["version"], dict(current,
            checkpoint_sha256=digest(current["receipt"])), **custody)

    @contextmanager
    def resource_locks(self, resources):
        if len(set(resources)) != len(resources) or not resources:
            raise ValueError("unique explicit resources required")
        handles = []
        try:
            for resource in sorted(resources):
                handle = (self.root / ("lock-" + digest(resource))).open("a+b")
                handles.append(handle)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            for handle in reversed(handles):
                handle.close()

    def reserve_authorized_effect(self, key, record):

        with self.transaction():
            old = self.db.execute("SELECT data FROM effects WHERE key=?", (key,)).fetchone()
            if old:
                prior = json.loads(old[0])
                raise Conflict('effect already intended by '+prior['occurrence']+
                               '; reconcile the original; no replay')
            self.db.execute("INSERT INTO effects VALUES (?,?)", (key, json.dumps(record, sort_keys=True)))

    def settle_authorized_effect(self, key, occurrence, *, state, native, receipt):
        if state not in {'applied', 'uncertain'}:
            raise ValueError('typed effect settlement required')
        with self.transaction():
            old = self.db.execute("SELECT data FROM effects WHERE key=?", (key,)).fetchone()
            if old is None: raise Conflict('effect intent absent')
            record = json.loads(old[0])
            if record['occurrence'] != occurrence or record['state'] != 'intended':
                raise Conflict('effect original identity/state differs; no replay')
            record.update(state=state, native=native, receipt=receipt, relaunch_authorized=False)
            self.db.execute("UPDATE effects SET data=? WHERE key=?", (json.dumps(record, sort_keys=True), key))
            return record

    def fixture_effect(self, key, target, expected_preimage, raw, *, crash=None, native=None, resources=None):

        target = Path(target).resolve()
        if not target.is_relative_to(self.root / "targets") or not target.is_file():
            raise ValueError("effect target outside isolated synthetic targets")
        if native is not None and (set(native) != {"run_id", "task_id", "dispatch_id"} or any(not v for v in native.values())):
            raise ValueError("original native effect identity required")
        locks = sorted(set([str(target)] + [str(Path(p).resolve()) for p in (resources or [])]))
        if any(not Path(p).is_relative_to(self.root / "targets") for p in locks):
            raise ValueError("effect resources outside isolated synthetic targets")
        with self.resource_locks(locks):
            saved = self.db.execute("SELECT data FROM effects WHERE key=?", (key,)).fetchone()
            intended = __import__("hashlib").sha256(raw).hexdigest()
            if saved:
                record = json.loads(saved[0])
                if (record["target"], record["preimage"], record["postimage"]) != (str(target), expected_preimage, intended):
                    raise Conflict("effect key reused for different intent")
                if native is not None and record.get("native") != native:
                    raise Conflict("effect belongs to a different original native writer")
                if record.get("automatic_reconciliation_attempts") == 1:
                    return record
                if (sha(target) != intended or not Path(record["archive"]).is_file()
                        or sha(record["archive"]) != expected_preimage):
                    record = dict(record, state="unknown", reason="effect readback differs; no replay",
                        resolution="owner reconciliation required; automatic attempts exhausted",
                        automatic_reconciliation_attempts=1, max_automatic_reconciliation_attempts=1,
                        retry_authorized=False)
                    with self.transaction():
                        self.db.execute("UPDATE effects SET data=? WHERE key=?", (json.dumps(record), key))
                    return record
                record["state"] = "effect_verified"
            else:
                if sha(target) != expected_preimage:
                    raise Conflict("effect expected preimage changed")
                archive = self.root / "archives" / (key + "-" + expected_preimage)
                archive.parent.mkdir(exist_ok=True)
                record = {"state": "effect_intent", "target": str(target), "preimage": expected_preimage,
                          "postimage": intended, "archive": str(archive)}
                if native is not None:
                    record.update(native=native, effect_key=key, lock_order=locks)
                with self.transaction():
                    self.db.execute("INSERT INTO effects VALUES (?,?)", (key, json.dumps(record)))
                if crash:
                    crash("effect_intent")
                with archive.open("xb") as handle:
                    handle.write(target.read_bytes()); handle.flush(); os.fsync(handle.fileno())
                descriptor = os.open(archive.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if sha(archive) != expected_preimage or sha(target) != expected_preimage:
                    raise Conflict("preimage changed while archiving")
                atomic_bytes(target, raw)
                if crash:
                    crash("effect")
                if sha(target) != intended:
                    return dict(record, state="unknown")
                record["state"] = "effect_verified"
            with self.transaction():
                self.db.execute("UPDATE effects SET data=? WHERE key=?", (json.dumps(record), key))
            if crash:
                crash("effect_receipt")
            return record
