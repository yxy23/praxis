
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sqlite3

from adaptive_contract import digest, sha

OWNERS = {"learning_closure": "scripts/learning_closure.py"}
PROVENANCE = {"source_bindings", "local_reads", "rules", "graph_sha256", "native"}

def event(stream, sequence, kind, occurrence, provenance, claims):
    if not stream or type(sequence) is not int or sequence < 1 or not kind or not occurrence:
        raise ValueError("stable event identity required")
    if set(provenance) != PROVENANCE:
        raise ValueError("explicit source/read/rule/graph/native provenance required")
    if not re.fullmatch(r"[a-f0-9]{64}", provenance["graph_sha256"]):
        raise ValueError("graph hash required")
    row = {"schema_version": 1, "stream": stream, "sequence": sequence,
           "kind": kind, "occurrence_id": occurrence,
           "provenance": copy.deepcopy(provenance), "claims": copy.deepcopy(claims)}
    row["event_id"] = digest([stream, sequence])
    return row

def check_event(row):
    rebuilt = event(row["stream"], row["sequence"], row["kind"], row["occurrence_id"],
                    row["provenance"], row["claims"])
    if rebuilt != row:
        raise ValueError("event identity/schema changed")

class Outbox:
    def __init__(self, root, *, max_events, max_sequence):
        root = Path(root)
        if not (root / "ADAPTIVE_FIXTURE_ROOT").is_file():
            raise ValueError("isolated evidence root required")
        if any(type(v) is not int or v <= 0 for v in (max_events, max_sequence)):
            raise ValueError("explicit finite event/sequence admission required")
        self.max_events, self.max_sequence = max_events, max_sequence
        self.db = sqlite3.connect(root / "ci-outbox.sqlite", timeout=5)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, sha TEXT, data TEXT)")
        self.db.commit()

    def append(self, row):
        check_event(row)
        if row["sequence"] > self.max_sequence:
            raise ValueError("event sequence admission exceeded")
        raw, key = json.dumps(row, sort_keys=True), row["event_id"]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT sha FROM outbox WHERE id=?", (key,)).fetchone()
            if old and old[0] != digest(row):
                raise ValueError("conflicting duplicate CI event")
            if not old:
                if self.db.execute("SELECT count(*) FROM outbox").fetchone()[0] >= self.max_events:
                    raise ValueError("event resource envelope exhausted")
                self.db.execute("INSERT INTO outbox VALUES (?,?,?)", (key, digest(row), raw))
            self.db.commit()                                                     
        except BaseException:
            self.db.rollback(); raise
        return key

    def append_before_ack(self, row, acknowledge):
        key = self.append(row)
        acknowledge(key)
        return key

    def rows(self):
        result = []
        for expected, raw in self.db.execute("SELECT sha,data FROM outbox"):
            row = json.loads(raw); check_event(row)
            if row["sequence"] > self.max_sequence or len(result) >= self.max_events:
                raise ValueError("stored event resource admission exceeded")
            if digest(row) != expected:
                raise ValueError("corrupt durable outbox")
            result.append(row)
        return sorted(result, key=lambda r: (r["stream"], r["sequence"]))

    def project(self, destination):

        db = sqlite3.connect(destination)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, sha TEXT, data TEXT)")
            for row in self.rows():
                key, expected = row["event_id"], digest(row)
                old = db.execute("SELECT sha FROM events WHERE id=?", (key,)).fetchone()
                if old and old[0] != expected:
                    raise ValueError("conflicting projection event; preserve store")
                db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)", (key, expected, json.dumps(row, sort_keys=True)))
            db.commit()
            streams = {}
            for row in self.rows():
                streams.setdefault(row["stream"], set()).add(row["sequence"])
            gaps = {s: sorted(set(range(1, max(seq) + 1)) - seq) for s, seq in streams.items()}
            return {"events": len(self.rows()), "sequence_gaps": gaps,
                    "learning_evidence_complete": not any(gaps.values())}
        finally:
            db.close()

    def close(self):
        self.db.close()

def owner_links(root):
    links = []
    for name, relative in OWNERS.items():
        path = Path(root) / relative
        links.append({"owner": name, "path": str(path), "sha256": sha(path),
                      "authority": "existing owner procedure; pointer only"})
    return links

def applicable_rules(rule_records, *, target, revoked):

    rows = []
    for rule in rule_records:
        if rule.get("status") != "accepted" or rule.get("id") in revoked or rule.get("sha256") in revoked or rule.get("target") != target:
            continue
        required = {"id", "sha256", "parent_sha256", "evidence", "target", "tests",
                    "authority", "rollback", "path", "status"}
        if not required <= set(rule) or not all(rule[k] for k in required):
            raise ValueError("incomplete versioned rule provenance")
        if sha(rule["path"]) != rule["sha256"]:
            raise ValueError("accepted rule source changed")
        authority = rule["authority"]
        if sha(authority["path"]) != authority["sha256"]:
            raise ValueError("rule acceptance evidence changed")
        rows.append(copy.deepcopy(rule))
    return rows

def dormant_evaluation(criteria, later_receipt, emitted):

    required = {"id", "occurrence_id", "workflow_id", "rule_sha256", "baseline_sha256",
                "graph_sha256", "source_sha256", "route_sha256", "evaluator_policy"}
    if not required <= set(criteria) or not all(criteria[k] for k in required):
        raise ValueError("complete finite dormant criteria required")
    policy = criteria["evaluator_policy"]
    if type(policy.get("max_receipts")) is not int or policy["max_receipts"] <= 0 or not policy.get("justification"):
        raise ValueError("bounded evidence policy required")
    matches = (later_receipt.get("normal_occurrence") is True
        and later_receipt.get("contract_accepted") is True
        and later_receipt.get("occurrence_id") != criteria["occurrence_id"]
        and all(later_receipt.get(k) == criteria[k] for k in
                ("workflow_id", "rule_sha256", "graph_sha256", "source_sha256", "route_sha256"))
        and later_receipt.get("native") and later_receipt.get("receipt_sha256"))

    prefix = digest(criteria) + ":"
    used = sum(str(k).startswith(prefix) for k in emitted)
    key = prefix + digest(later_receipt)
    if not matches or key in emitted or used >= policy["max_receipts"]:
        return None
    return {"evaluation_intent_id": key, "criteria": copy.deepcopy(criteria),
            "receipt": copy.deepcopy(later_receipt), "state": "ready_for_bounded_evaluator",
            "benefit": "unmeasured", "native_launches": 0}
