import json
from pathlib import Path
from adaptive_state import atomic_bytes
from adaptive_contract import sha
REPO=Path(__file__).resolve().parents[2]
def save_once(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if path.exists() and path.read_bytes() != raw:
        raise ValueError("frozen qualification input differs: " + str(path))
    if not path.exists(): atomic_bytes(path, raw)

def close_released_lease(lease, bundle, dispatcher):
    units = bundle.get("units", [])
    if not units or any(not dispatcher.release_resolved(u.get("release") or {}) or
                        (u.get("release") or {}).get("state") == "retained" for u in units):
        return {"state": "held", "reason": "native release not verified; lease remains recoverable"}
    lease.update(status=bundle["status"], cleanup="verified_released")
    return {"state": "closed", "status": bundle["status"]}

def audit_effects(bundles):
    writes = [u["route_outcome"]["ledger"] for b in bundles.values() for u in b.get("units", [])
              if u.get("route_outcome", {}).get("ledger", {}).get("written") is True]
    return {"audit_ledger_appends": len(writes), "receipts": writes,
            "mail_calendar_tracker_mutations": "not measured"}

def bound_json(binding):
    if sha(binding['path']) != binding['sha256']: raise ValueError('owner evidence drift')
    return json.loads(Path(binding['path']).read_bytes())
