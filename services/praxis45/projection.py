
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

VERSION = "1.0.0"
EVENT = "praxis:unit_settled"
ENTRY_EVENT = "praxis:backend_entry_intent"

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()

def project(receipt_bytes, plan_bytes, *, workspace, request_id, captured_at):

    if not all(isinstance(v, str) and v for v in (workspace, captured_at)):
        raise ValueError("explicit workspace and capture time required")
    bundle, plan = json.loads(receipt_bytes), json.loads(plan_bytes)
    if not isinstance(bundle, dict) or not isinstance(plan, dict):
        raise ValueError("receipt and plan must be objects")
    receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    events = []
    for unit in bundle.get("units", []):
        identity = {"workspace": workspace, "run_id": bundle.get("run_id"),
                    "task_id": unit.get("task_id"), "dispatch_id": unit.get("dispatch_id"),
                    "unit_id": unit.get("id"), "stage": "settled"}
        missing = [key for key, value in identity.items() if not value]

        if missing:
            identity["diagnostic_run_key"] = bundle.get("routine_run_key")
            identity["diagnostic_plan_id"] = bundle.get("plan_id")
        occurrence = digest(identity)
        source = bundle.get("source") or {}
        if source.get("plan_sha256") != plan_sha or bundle.get("plan_id") != plan.get("id"):
            missing.append("matching_plan")
        if not request_id:
            missing.append("request_id")
        context = plan.get("context") or {}
        if not context.get("identity") or not context.get("digest"):
            missing.append("context")
        supplied = unit.get("supplied_context") or {}
        if not supplied.get("context_sha256"):
            missing.append("supplied_context")
        launch = unit.get("actual_launch") or {}
        if (not all(launch.get(k) for k in ("agent", "model", "effort"))
                or launch.get("receipt_missing_effective_launch")):
            missing.append("actual_launch")
        if not source.get("dispatcher_sha256"):
            missing.append("dispatcher_source")
        contract = unit.get("contract") or {}
        outputs = unit.get("outputs") or []
        rejected = [row.get("path") for row in outputs
                    if row.get("required") and (not row.get("exists") or not row.get("sha256"))]
        worker = unit.get("worker_done") or {}
        outcome = worker.get("outcome", "unknown")
        accepted = (not missing and not rejected and contract.get("accepted") is True
                    and unit.get("status") == "succeeded" and outcome == "succeeded")
        timestamps = [row.get("ts") for row in unit.get("telemetry", [])
                      if row.get("stage") == "contract" and row.get("ts")]
        timestamp = timestamps[-1] if timestamps else bundle.get("completed_at")
        if not timestamp:
            raise ValueError("event time absent; capture time cannot replace it")
        data = {
            "occurrence_id": occurrence, "identity": identity,
            "identity_complete": not any(key in missing for key in identity),
            "receipt_sha256": receipt_sha, "receipt_version": receipt_sha,
            "receipt_schema_version": bundle.get("schema_version"),
            "captured_at": captured_at,
            "session_id": digest({"workspace": workspace, "run_id": bundle.get("run_id"),
                                  "plan_id": bundle.get("plan_id")}),
            "request_id": request_id, "context": context, "source": source,
            "plan_sha256": plan_sha, "supplied_context": supplied,
            "context_provenance": unit.get("context_provenance", "unknown"),
            "route": unit.get("route"), "actual_launch": launch,
            "provider_session": unit.get("provider_session"), "usage": unit.get("usage"),
            "unit_state": unit.get("status", "unknown"), "worker_outcome": outcome,
            "contract": contract, "evidence_accepted": accepted,
            "missing_provenance": sorted(missing), "rejected_outputs": rejected,
            "artifacts": outputs, "cleanup_state": unit.get("cleanup", "unknown"),
            "release": unit.get("release"),
            "delivery": [row.get("delivery") for row in outputs if row.get("delivery")],
            "delivery_state": "recorded" if any(row.get("delivery") for row in outputs) else "unknown",
            "human_acceptance_state": "unknown", "human_acceptance_source": None,
            "timestamp_basis": "unit_contract" if timestamps else "run_completed_at",
            "substantive": unit.get("substantive"),
        }
        data["event_id"] = digest({"occurrence_id": occurrence, "receipt_sha256": receipt_sha})
        events.append({"event": EVENT, "workspace": workspace, "timestamp": timestamp, "data": data})
    return events

def compatible_metadata(session_dir):
    meta = json.loads((Path(session_dir) / "metadata.json").read_text())
    if meta.get("format") != "context-intelligence" or meta.get("version") != VERSION:
        raise ValueError("unsupported JSONL metadata format/version")
    if not all(isinstance(meta.get(k), str) and meta[k]
               for k in ("session_id", "workspace", "started_at", "last_event_at", "status")):
        raise ValueError("incomplete JSONL metadata")
    return meta

def validate_line(row, meta):
    if set(row) != {"event", "workspace", "timestamp", "data"}:
        raise ValueError("event line requires exactly event/workspace/timestamp/data")
    if row["workspace"] != meta["workspace"] or not isinstance(row["data"], dict):
        raise ValueError("event workspace/data mismatch")
    if row["event"] not in {EVENT, ENTRY_EVENT} or not isinstance(row["timestamp"], str) or not row["timestamp"]:
        raise ValueError("unsupported event or missing event time")
    if not row["data"].get("occurrence_id") or not row["data"].get("event_id"):
        raise ValueError("missing event identity")
    if row["event"] == ENTRY_EVENT:
        data = row["data"]
        identity = data.get("identity")
        if (not isinstance(identity, dict) or set(identity) != {"workspace", "graph_run_id"}
                or identity["workspace"] != row["workspace"]
                or not all(isinstance(value, str) and value for value in identity.values())
                or not isinstance(data.get("node_id"), str) or not data["node_id"]
                or type(data.get("visit")) is not int or data["visit"] < 1
                or data["occurrence_id"] != digest(identity)
                or data["event_id"] != digest({"occurrence_id": data["occurrence_id"],
                    "node_id": data["node_id"], "visit": data["visit"]})):
            raise ValueError("invalid backend entry identity")

def read_events(session_dir):

    meta = compatible_metadata(session_dir)
    path = Path(session_dir) / "events.jsonl"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                raise ValueError("truncated JSONL tail; preserve and reconcile")
            row = json.loads(line)
            validate_line(row, meta)
            rows.append(row)
    return rows

def occurrences(session_dir):

    result = {}
    for row in read_events(session_dir):
        if row["event"] != EVENT:
            continue
        versions = result.setdefault(row["data"]["occurrence_id"], {})
        versions.setdefault(row["data"]["receipt_sha256"], row)
    return result

def durable_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".metadata-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)

def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def ingest(session_dir, events, *, session_id, entry_claim=False):

    if not events:
        raise ValueError("no evidence to ingest")
    if entry_claim and (len(events) != 1 or events[0]["event"] != ENTRY_EVENT):
        raise ValueError("one backend entry intent required")
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    meta_path = session_dir / "metadata.json"
    with (session_dir / ".projection.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not meta_path.exists():
            if (session_dir / "events.jsonl").exists():
                raise ValueError("existing event log lacks metadata; preserve and reconcile")
            durable_json(meta_path, {"format": "context-intelligence", "version": VERSION,
                "session_id": session_id, "workspace": events[0]["workspace"],
                "started_at": events[0]["timestamp"], "last_event_at": events[0]["timestamp"],
                "status": "running"})
        meta = compatible_metadata(session_dir)
        if meta["session_id"] != session_id:
            raise ValueError("capture session identity mismatch")
        prior = read_events(session_dir)
        if entry_claim:
            validate_line(events[0], meta)
            data = events[0]["data"]
            existing = next((row for row in prior if row["event"] == ENTRY_EVENT
                and row["data"]["occurrence_id"] == data["occurrence_id"]
                and row["data"]["node_id"] == data["node_id"]), None)

            if existing:
                events = [existing]
        known = {row["data"]["event_id"] for row in prior}
        pending = []
        for row in events:
            validate_line(row, meta)
            if row["data"]["event_id"] not in known:
                pending.append(row)
                known.add(row["data"]["event_id"])
        with (session_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            for row in pending:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False) + "\n")
            handle.flush()

            os.fsync(handle.fileno())
        fsync_directory(session_dir)
        all_rows = prior + pending
        meta.update(started_at=min(row["timestamp"] for row in all_rows),
                    last_event_at=max(row["timestamp"] for row in all_rows), status="completed")
        durable_json(meta_path, meta)
        return {"durable": True, "appended": len(pending),
                "duplicates": len(events) - len(pending), "occurrences": len(occurrences(session_dir))}

def claim_entry(session_dir, *, workspace, graph_run_id, node_id, visit, timestamp):

    identity = {"workspace": workspace, "graph_run_id": graph_run_id}
    occurrence_id = digest(identity)
    data = {"identity": identity, "occurrence_id": occurrence_id,
            "node_id": node_id, "visit": visit}
    data["event_id"] = digest({"occurrence_id": occurrence_id, "node_id": node_id, "visit": visit})
    row = {"event": ENTRY_EVENT, "workspace": workspace, "timestamp": timestamp, "data": data}
    return ingest(session_dir, [row], session_id=occurrence_id, entry_claim=True)["appended"] == 1
