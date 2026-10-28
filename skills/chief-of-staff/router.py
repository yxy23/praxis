                      

import argparse
import hashlib
import json
from pathlib import Path
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

_KEY = re.compile(r'^(?P<key>"[^"]*"|\'[^\']*\'|[^:#]+?)\s*:\s*(?P<val>.*)$')

def _strip_comment(text):
    out, quote = [], None
    for i, ch in enumerate(text):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()

def _scalar(text):
    text = text.strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text == "[]":
        return []
    if text == "{}":
        return {}
    low = text.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text

def _lines(raw):
    out = []
    for ln in raw.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        body = _strip_comment(ln)
        if not body.strip():
            continue
        out.append((len(body) - len(body.lstrip(" ")), body.strip()))
    return out

def _parse(lines, i, indent):
    if i >= len(lines):
        return None, i
    if lines[i][1].startswith("- "):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)

def _parse_seq(lines, i, indent):
    items = []
    while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
        rest = lines[i][1][2:].strip()
        child = [(indent + 2, rest)] if rest else []
        i += 1
        while i < len(lines) and lines[i][0] > indent:
            child.append(lines[i])
            i += 1
        if not child:
            items.append(None)
        elif len(child) == 1 and not _KEY.match(child[0][1]):
            items.append(_scalar(child[0][1]))
        else:
            val, _ = _parse(child, 0, child[0][0])
            items.append(val)
    return items, i

def _parse_map(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        m = _KEY.match(lines[i][1])
        if not m:
            break
        key = _scalar(m.group("key"))
        raw = m.group("val").strip()
        i += 1
        if raw:
            out[key] = _scalar(raw)
            continue
        child = []
        while i < len(lines) and lines[i][0] > indent:
            child.append(lines[i])
            i += 1
        out[key] = _parse(child, 0, child[0][0])[0] if child else None
    return out, i

def load_yaml(path):

    lines = _lines(Path(path).read_text(encoding="utf-8"))
    return _parse(lines, 0, lines[0][0])[0] if lines else {}

def vault_root():
    env = os.environ.get("PRAXIS_VAULT_ROOT")
    if env:
        return env
    cfg = os.path.expanduser("~/.praxis-config.sh")
    if os.path.exists(cfg):
        for ln in open(cfg, encoding="utf-8"):
            m = re.match(r'\s*export\s+VAULT_ROOT="?(.*?)"?\s*$', ln)
            if m:
                return os.path.expandvars(m.group(1).replace("$HOME", os.path.expanduser("~")))
    raise SystemExit("Cannot resolve vault root: set PRAXIS_VAULT_ROOT or VAULT_ROOT in ~/.praxis-config.sh")

def institution_dir():
    return os.path.join(vault_root(), "system", "memory", "institution")

ORDER = ["availability", "data_class_permission", "required_tools",
         "context_capacity", "benchmark_approval", "cost"]

COST_RANK = {"gpt-5.6-luna": 1, "gpt-5.6-terra": 2, "claude-sonnet-5": 3,
             "gpt-5.6-sol": 4, "gpt-6-sol": 4,
             "claude-opus-5": 5, "claude-opus-5-5": 5,
             "claude-fable-5": 6, "claude-fable-5-1": 6, "gpt-6-astra": 6}

CLASS_ORDER = ["public", "internal", "confidential", "highly-restricted"]

CAPABILITY_REFUSALS = {"no consequential judgment", "no external action"}

COS_OWNER = "gpt-5.6-sol"
_COS_OWNER_RESOLVED = [COS_OWNER]                                          

_INDEPENDENCE = [None, ()]                                                

class Denied(Exception):
    pass

def cos_owner(reg):

    return ((reg.get("routes") or {}).get("cos-primary") or {}).get("primary") or COS_OWNER

def _models(reg):
    return {m["id"]: m for m in reg.get("models", []) if isinstance(m, dict)}

def _providers(reg):
    return {p["id"]: p for p in reg.get("providers", []) if isinstance(p, dict)}

def _today():
    return datetime.now().astimezone().strftime("%Y-%m-%d")

def benchmarked(model, task_class):

    return task_class in (model.get("validated_task_classes") or [])

def eligible(model, *, task_class, sensitivity, consequential, external_action,
             unavailable, quota, reserve, risk, required_tools=(), context_need=None):

    mid = model["id"]
    prov = model.get("provider")

    if model.get("available") is False or mid in unavailable or prov in unavailable:
        return False, "unavailable"
    used = quota.get(prov)
    if used is not None and used >= reserve and risk == "low":
        return False, "provider past quota reserve"

    if model.get("selection") == "explicit-route-only":
        return False, "explicit-route-only"
    if consequential and model.get("consequential_judgment") is False:
        return False, "no consequential judgment"
    if external_action and model.get("external_action") is False:
        return False, "no external action"

    have = set(model.get("tools") or [])
    missing = [t for t in required_tools if t not in have]
    if required_tools and missing:
        return False, f"missing required tools: {','.join(missing)}"

    if context_need:
        cap = str(model.get("context") or "")
        if context_need == "1m" and cap != "1m":
            return False, "insufficient context capacity"

    return True, None

def post_expiry_policy(spec):

    rollback = spec["pilot"]["rollback_route"]
    restored = {key: value for key, value in rollback.items() if key != "pilot"}
    restored.update((spec.get("adoption") or {}).get("fields") or {})
    return restored

def route(reg, *, task_class, sensitivity="internal", risk="medium",
          consequential=False, external_action=False, explicit_model=None,
          unavailable=(), quota=None, approved_explicit=False,
          required_tools=(), context_need=None, require_primary=False,
          producer_model=None):
    quota = quota or {}
    trace = []
    reserve = (reg.get("policy") or {}).get("quota_reserve_percent", 85)
    models = _models(reg)
    routes = reg.get("routes") or {}
    if task_class not in routes:
        raise Denied(f"unknown task class '{task_class}'; not in MODEL-REGISTRY.yaml routes")
    spec = routes[task_class]
    _COS_OWNER_RESOLVED[0] = cos_owner(reg)

    if "adoption" in spec:
        block = spec["adoption"]
        allowed = {"primary", "alternate_provider", "escalation", "verification",
                   "review", "critic"}
        if (not isinstance(block, dict) or not isinstance(block.get("authorized_by"), str)
                or not block["authorized_by"].strip()
                or not isinstance(block.get("fields"), dict) or not block["fields"]
                or any(key not in allowed or not isinstance(mid, str)
                       or spec.get(key) != mid or mid not in models
                       for key, mid in block["fields"].items())):
            raise Denied(f"route '{task_class}' has an invalid adoption declaration")

    if sensitivity not in CLASS_ORDER:
        raise Denied(f"unknown sensitivity '{sensitivity}'")

    pilot = spec.get("pilot") or {}
    expires = str(pilot.get("expires") or "")
    adoption = spec.get("adoption") or {}
    if not explicit_model and expires and expires < _today() and pilot.get("rollback_route"):
        if not pilot.get("authorized_by"):
            raise Denied(f"route '{task_class}' declares a pilot with no authorizing decision")
        rollback = pilot["rollback_route"]
        if not isinstance(rollback, dict) or not rollback.get("primary"):
            raise Denied(f"route '{task_class}' has an invalid pilot rollback_route")
        restored = post_expiry_policy(spec)
        if adoption:
            restored["adoption"] = adoption
        restored_reg = {**reg, "routes": {**routes, task_class: restored}}
        decision = route(
            restored_reg, task_class=task_class, sensitivity=sensitivity, risk=risk,
            consequential=consequential, external_action=external_action,
            unavailable=unavailable, quota=quota, approved_explicit=approved_explicit,
            required_tools=required_tools, context_need=context_need,
            require_primary=require_primary, producer_model=producer_model)
        decision.update(
            pilot_expired=True, pilot_expired_decision=pilot["authorized_by"],
            pilot_expired_on=expires,
            pilot_rollback={"from": spec.get("primary"), "to": restored["primary"]})
        decision["gates"].append(
            f"expired pilot {pilot['authorized_by']}: applied adoption {adoption['authorized_by']}"
            if adoption else
            f"expired pilot {pilot['authorized_by']}: restored declared incumbent route")
        return decision

    _INDEPENDENCE[0], _INDEPENDENCE[1] = None, ()
    if spec.get("must_differ_from_producer") and producer_model:

        producers = tuple(x.strip() for x in str(producer_model).split(",") if x.strip())
        providers = {(models.get(pm) or {}).get("provider") for pm in producers}
        providers.discard(None)
        if providers:
            excluded = tuple(sorted(
                mid for mid, m in models.items()
                if m.get("provider") in providers))
            _INDEPENDENCE[0], _INDEPENDENCE[1] = ",".join(producers), excluded
            unavailable = tuple(unavailable) + excluded

    if explicit_model:
        if explicit_model not in models:
            raise Denied(f"'{explicit_model}' is not in the registry and is not selectable")
        m = models[explicit_model]

        ok, why = eligible(m, task_class=task_class, sensitivity=sensitivity,
                           consequential=consequential, external_action=external_action,
                           unavailable=unavailable, quota=quota, reserve=reserve,
                           risk=risk, required_tools=required_tools,
                           context_need=context_need)
        explicit_only = why == "explicit-route-only"
        trace.append({
            "model": explicit_model,
            "ok": ok or explicit_only,
            "reason": "eligible by explicit request" if explicit_only else (why or "eligible"),
        })
        if not ok and why != "explicit-route-only":
            raise Denied(f"'{explicit_model}' rejected: {why}")

        waived = not benchmarked(m, task_class) and explicit_model != spec.get("primary")
        return _decide(spec, task_class, explicit_model, m, sensitivity, risk,
                       reason="explicit route requested", fallback=None,
                       consequential=consequential, trace=trace,
                       benchmark_waived=waived, requested_model=explicit_model)

    quality_first = bool(spec.get("quality_first"))
    primary = spec.get("primary")

    pilot = spec.get("pilot") or None
    adoption = spec.get("adoption") or None
    pilot_active = False
    if pilot:
        if not pilot.get("authorized_by"):
            raise Denied(f"route '{task_class}' declares a pilot with no authorizing decision")
        exp = str(pilot.get("expires") or "")
        if exp and exp < _today():
            raise Denied(
                f"route '{task_class}' pilot expired {exp} (decision {pilot['authorized_by']}); "
                f"benchmark {pilot.get('benchmark')} has not passed and no rollback_route is declared")
        pilot_active = True

    if spec.get("pinned") and spec.get("no_fallback"):
        if primary not in models:
            raise Denied(f"route '{task_class}' is pinned to '{primary}', which is not in the registry")
        ok, why = eligible(models[primary], task_class=task_class, sensitivity=sensitivity,
                           consequential=consequential, external_action=external_action,
                           unavailable=unavailable, quota=quota, reserve=reserve,
                           risk=risk, required_tools=required_tools, context_need=context_need)
        if not ok:
            note = spec.get("on_unavailable_note") or ""
            raise Denied(f"route '{task_class}' is pinned to '{primary}' and will not fall back: {why}."
                         + (f" {note}" if note else ""))
        trace.append({"model": primary, "ok": True, "reason": "pinned route"})
        return _decide(spec, task_class, primary, models[primary], sensitivity, risk,
                       reason="pinned route, no fallback permitted", fallback=None,
                       consequential=consequential, trace=trace,
                       benchmark_waived=not benchmarked(models[primary], task_class))

    ordered = [primary]
    if not quality_first and spec.get("candidate"):

        cand = models.get(spec["candidate"], {})
        if task_class in (cand.get("validated_task_classes") or []):
            ordered.insert(0, spec["candidate"])

    if primary in models:
        ok, why = eligible(models[primary], task_class=task_class, sensitivity=sensitivity,
                           consequential=consequential, external_action=external_action,
                           unavailable=unavailable, quota=quota, reserve=reserve,
                           risk=risk, required_tools=required_tools,
                           context_need=context_need)
        if not ok and (why in CAPABILITY_REFUSALS or primary in _INDEPENDENCE[1]
                       or (adoption and why == "unavailable")):
            for nxt in (spec.get("escalation"), spec.get("review"), spec.get("verification"),
                        (adoption or {}).get("fields", {}).get("alternate_provider")):
                if nxt and nxt in models:
                    ordered.append(nxt)
                    break

    priority = list(ordered)
    fallback_chain = (spec.get("fallback_chain") if "fallback_chain" in spec
                      else (reg.get("fallback_chains") or {}).get(primary, []))
    rest = [m for m in (fallback_chain or [])
            if m not in priority]

    seen, ordered = set(), []
    for mid in priority + rest:
        if mid and mid not in seen:
            seen.add(mid)
            ordered.append(mid)

    if not quality_first:
        head = [m for m in ordered if m in priority]
        tail = sorted((m for m in ordered if m not in priority),
                      key=lambda m: COST_RANK.get(m, 99))
        ordered = head + tail

    chosen, why_not, waived = None, {}, False
    for mid in ordered:
        if not mid or mid not in models:
            continue
        ok, why = eligible(models[mid], task_class=task_class, sensitivity=sensitivity,
                           consequential=consequential, external_action=external_action,
                           unavailable=unavailable, quota=quota, reserve=reserve,
                           risk=risk, required_tools=required_tools,
                           context_need=context_need)
        if ok and mid == primary and (pilot_active or adoption) and not benchmarked(models[mid], task_class):
            waived = True
        if ok and mid != primary and not benchmarked(models[mid], task_class):
            if quality_first and mid not in (adoption or {}).get("fields", {}).values():

                ok, why = False, "not validated for this task class (quality-first)"
            else:
                waived = True
        trace.append({"model": mid, "ok": ok, "reason": why or "eligible"})
        if ok:
            chosen = mid
            break
        why_not[mid] = why

    if not chosen and quality_first:
        alt = spec.get("alternate_provider")
        if alt and alt in models:
            ok, why = eligible(models[alt], task_class=task_class, sensitivity=sensitivity,
                               consequential=consequential, external_action=external_action,
                               unavailable=unavailable, quota=quota, reserve=reserve,
                               risk=risk, required_tools=required_tools,
                               context_need=context_need)
            if ok and (benchmarked(models[alt], task_class)
                       or alt in (adoption or {}).get("fields", {}).values()):
                trace.append({"model": alt, "ok": True, "reason": "declared quality-first alternate"})
                chosen = alt
                waived = not benchmarked(models[alt], task_class)
            else:
                why_not[alt] = why or "alternate not validated for this task class"

    if not chosen:
        raise Denied(f"no approved model for {task_class} at sensitivity={sensitivity}: {why_not}")

    if require_primary and chosen != primary:
        raise Denied(
            f"route '{task_class}' requires its primary '{primary}' for this request; "
            f"the primary is unavailable or ineligible ({why_not.get(primary, 'unknown reason')}). "
            "No model fallback was permitted."
        )

    fallback = None if chosen == primary else {
        "from": primary, "to": chosen,
        "because": why_not.get(primary, "primary not eligible")}
    reason = "quality-first route" if quality_first else "policy filters, then cost"
    return _decide(spec, task_class, chosen, models[chosen], sensitivity, risk,
                   reason=reason, fallback=fallback, consequential=consequential,
                   trace=trace, benchmark_waived=waived)

def _decide(spec, task_class, mid, model, sensitivity, risk, *, reason, fallback,
            consequential, trace=None, benchmark_waived=False, requested_model=None):
    effort = spec.get("effort") or model.get("default_effort")
    if fallback and spec.get("candidate") == mid and spec.get("candidate_effort"):
        effort = spec["candidate_effort"]
    elif fallback and spec.get("fallback_effort"):
        effort = spec["fallback_effort"]
    gates = []
    if spec.get("human_gate"):
        gates.append(spec["human_gate"])
    if consequential:
        gates.append("consequential judgment stays with User")

    owner = spec.get("owner_stays")
    if consequential and not owner:
        owner = _COS_OWNER_RESOLVED[0]
    if benchmark_waived:
        gates.append(f"benchmark waived: {mid} is not validated for {task_class}")
    pilot = spec.get("pilot") or None
    adoption = spec.get("adoption") or None
    adopted_model = bool(adoption and mid in (adoption.get("fields") or {}).values())
    if adopted_model:
        gates.append(f"operator-authorized unvalidated route: {mid}, {adoption.get('authorized_by')}")
    pilot_models = pilot.get("models", [spec.get("primary")]) if pilot else []
    selected_pilot = bool(pilot and mid in pilot_models and not adopted_model)
    if selected_pilot:
        gates.append(
            f"pilot route: {mid} unvalidated for {task_class}, authorized by "
            f"{pilot.get('authorized_by')} until {pilot.get('expires')}, "
            f"accumulating toward {pilot.get('benchmark')}")
    return {
        "task_class": task_class,
        "route": task_class,
        "requested_model": requested_model or spec.get("primary"),
        "route_primary": spec.get("primary"),
        "effective_model": mid,

        "launch_target": model.get("launch_target") or mid,
        "agent": model.get("provider"),
        "provider": model.get("provider"),

        "requires_account": model.get("requires_account"),
        "effort": effort,
        "reason": reason,
        "fallback": fallback,
        "sensitivity": sensitivity,
        "risk": risk,
        "owner_stays": owner,
        "verification": spec.get("verification") or spec.get("critic") or spec.get("review"),
        "escalation": spec.get("escalation"),
        "gates": gates,
        "benchmark_waived": benchmark_waived,
        "pinned": bool(spec.get("pinned") and spec.get("no_fallback")),
        "producer_model": _INDEPENDENCE[0],
        "excluded_for_independence": list(_INDEPENDENCE[1]),
        "pilot_route": selected_pilot,
        "pilot_decision": pilot.get("authorized_by") if selected_pilot else None,
        "pilot_expires": pilot.get("expires") if selected_pilot else None,
        "filter_order": ORDER,
        "trace": trace or [],
    }

ROUTE_DECISION_KIND = "route_decision"
ROUTE_OUTCOME_KIND = "route_outcome"
ROUTE_OUTCOME_SCHEMA = "route-outcome/1"

UNIT_OUTCOMES = ("substantive", "non_substantive", "failed", "unavailable")

_OUTCOME_IDENTITY_FIELDS = (
    "route_decision_id", "run_key", "unit_id", "praxis_task_id", "task_class",
    "provider", "effective_model", "agent", "task_id", "dispatch_id",
    "lane_status", "worker_reported_outcome", "contract_accepted",
    "declared_outcome", "substantive", "cleanup", "outcome", "outcome_basis",
)

_OUTCOME_LOOKBACK_DAYS = 31

def _outcome_identity(row):
    payload = {key: row.get(key) for key in _OUTCOME_IDENTITY_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

def _outcome_day_files(inst, decision_day, today):

    days = [today]
    if decision_day and decision_day != today:
        try:
            start = datetime.strptime(decision_day, "%Y-%m-%d").date()
            end = datetime.strptime(today, "%Y-%m-%d").date()
        except ValueError:
            start = end = None
        if start and end and 0 <= (end - start).days <= _OUTCOME_LOOKBACK_DAYS:
            span = (end - start).days
            days = [(start + timedelta(days=offset)).strftime("%Y-%m-%d")
                    for offset in range(span + 1)]
        elif decision_day:
            days.append(decision_day)
    seen = []
    for day in days:
        path = os.path.join(inst, "audit", f"{day}.jsonl")
        if path not in seen and os.path.exists(path):
            seen.append(path)
    return seen

def reconcile_outcome(payload, *, inst=None):

    inst = inst or institution_dir()
    decision_id = str(payload.get("route_decision_id") or "").strip()
    if not decision_id:
        raise ValueError("route_decision_id is required to reconcile an outcome")
    outcome = payload.get("outcome")
    if outcome not in UNIT_OUTCOMES:
        raise ValueError(f"outcome must be one of {UNIT_OUTCOMES}, got {outcome!r}")

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    decision_day = payload.get("route_decision_day")

    row = dict(payload)
    row["record_kind"] = ROUTE_OUTCOME_KIND
    row["schema"] = ROUTE_OUTCOME_SCHEMA
    row["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row.setdefault("rework", None)
    identity = _outcome_identity(row)

    prior = None
    for path in _outcome_day_files(inst, decision_day, today):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or decision_id not in line:
                    continue
                try:
                    existing = json.loads(line)
                except ValueError:
                    continue
                if existing.get("record_kind") != ROUTE_OUTCOME_KIND:
                    continue
                if existing.get("route_decision_id") != decision_id:
                    continue
                existing_identity = (existing.get("outcome_identity")
                                     or _outcome_identity(existing))
                if existing_identity == identity:
                    return {"written": False, "action": "already_recorded",
                            "path": path, "outcome_identity": identity}
                prior = existing_identity

    row["outcome_identity"] = identity
    row["reconciles"] = prior
    path = os.path.join(inst, "audit", f"{today}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"written": True,
            "action": "reconciled" if prior else "recorded",
            "path": path, "outcome_identity": identity}

def log_decision(decision, *, praxis_task_id=None, latency_ms=None,
                 outcome=None, rework=None, inst=None,
                 run_key=None, unit_id=None):
    inst = inst or institution_dir()

    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    path = os.path.join(inst, "audit", f"{day}.jsonl")
    row = {

        "record_kind": ROUTE_DECISION_KIND,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "praxis_task_id": praxis_task_id,

        "run_key": run_key,
        "unit_id": unit_id,
        "task_class": decision["task_class"],
        "route": decision["route"],
        "requested_model": decision["requested_model"],
        "route_primary": decision.get("route_primary"),
        "effective_model": decision["effective_model"],
        "effort": decision["effort"],
        "reason": decision["reason"],
        "fallback": decision["fallback"],
        "provider": decision["provider"],
        "sensitivity": decision["sensitivity"],
        "risk": decision["risk"],
        "latency_ms": latency_ms,
        "outcome": outcome,
        "rework": rework,
        "gates": decision["gates"],
        "benchmark_waived": decision.get("benchmark_waived", False),
        "pilot_route": decision.get("pilot_route", False),
        "pilot_decision": decision.get("pilot_decision"),
        "pilot_expires": decision.get("pilot_expires"),
        "pilot_expired": decision.get("pilot_expired", False),
        "pilot_expired_decision": decision.get("pilot_expired_decision"),
        "pilot_expired_on": decision.get("pilot_expired_on"),
        "pilot_rollback": decision.get("pilot_rollback"),
        "requires_account": decision.get("requires_account"),
        "worker_account": decision.get("worker_account"),
        "trace": decision.get("trace", []),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path

def check_availability():

    try:
        out = subprocess.run(["orca", "account", "list", "--json"],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout).get("result", {})
    except Exception as exc:
        return {"error": str(exc)}
    rl = data.get("rateLimits", {})
    res = {}
    for prov, info in rl.items():
        if not isinstance(info, dict) or "provider" not in info:
            continue
        res[info["provider"]] = {
            "status": info.get("status"),
            "error": info.get("error"),
            "weekly_used_percent": (info.get("weekly") or {}).get("usedPercent"),
            "session_used_percent": (info.get("session") or {}).get("usedPercent"),
        }
    return res

WORKER_RUNTIME = "host"

PERSONAL_ORG = os.environ.get("PRAXIS_PERSONAL_ORGANIZATION", "")

def _account_kind(record):
    email = str((record or {}).get("email") or "")
    org = str((record or {}).get("organizationName") or "")
    return ("personal" if email.endswith("@gmail.com") or (PERSONAL_ORG and org == PERSONAL_ORG)
            else "institutional")

def worker_account(data, *, agent="claude", runtime=WORKER_RUNTIME):

    unresolved = {"agent": agent, "runtime": runtime, "account_id": None,
                  "kind": None, "source": None, "unproven": None}
    prov = (data or {}).get(agent) or {}
    by_runtime = prov.get("activeAccountIdsByRuntime") or {}
    active = by_runtime.get(runtime)
    source = f"{agent}.activeAccountIdsByRuntime.{runtime}"
    if not isinstance(active, str) or not active:
        active = prov.get("activeAccountId")
        source = f"{agent}.activeAccountId"
    unresolved["source"] = source
    if not isinstance(active, str) or not active:
        unresolved["unproven"] = f"Orca names no active {agent} account for runtime '{runtime}'"
        return unresolved
    unresolved["account_id"] = active

    meta = (((data or {}).get("rateLimits") or {}).get(agent) or {}).get("usageMetadata") or {}
    provenance = str(meta.get("authProvenance") or "")
    if provenance.startswith("managed:") and provenance.split(":", 1)[1] != active:
        unresolved["unproven"] = (
            f"{source} and rateLimits.{agent}.usageMetadata.authProvenance name "
            f"different accounts ({active} vs {provenance.split(':', 1)[1]})")
        return unresolved
    record = next((acct for acct in (prov.get("accounts") or [])
                   if acct.get("id") == active), None)
    if record is None:
        unresolved["unproven"] = f"the active {agent} account is not in Orca's account list"
        return unresolved
    return {"agent": agent, "runtime": runtime, "account_id": active,
            "kind": _account_kind(record), "source": source,
            "organization": str(record.get("organizationName") or ""),
            "email_domain": str(record.get("email") or "").rpartition("@")[2],
            "unproven": None}

def unavailable_models_from_account_data(reg, data):

    unavailable = set()
    rates = (data.get("rateLimits") or {}).get("claude") or {}

    for model in reg.get("models", []):
        quota_key = model.get("entitlement_quota")
        if not quota_key:
            continue

        block = rates.get(quota_key)
        if not isinstance(block, dict):
            unavailable.add(model["id"])
            continue
        used = block.get("usedPercent")
        if used is None or float(used) >= 100:
            unavailable.add(model["id"])

    session_used = (rates.get("session") or {}).get("usedPercent")
    weekly_used = (rates.get("weekly") or {}).get("usedPercent")
    if session_used is not None and float(session_used) >= 100:
        unavailable.update(m["id"] for m in reg.get("models", [])
                           if m.get("provider") == "claude")
    if weekly_used is not None and float(weekly_used) >= 100:
        unavailable.update(m["id"] for m in reg.get("models", [])
                           if m.get("provider") == "claude")
    return tuple(sorted(unavailable))

def account_data():

    fixture = os.environ.get("PRAXIS_ROUTER_AVAILABILITY_JSON")
    try:
        if fixture:
            payload = json.loads(fixture)
            return payload.get("result", payload)
        out = subprocess.run(["orca", "account", "list", "--json"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {}
        return json.loads(out.stdout).get("result", {})
    except Exception:
        return {}

def live_unavailable_models(reg):

    return unavailable_models_from_account_data(reg, account_data())

def main():
    ap = argparse.ArgumentParser(description="Praxis 4.0 deterministic model router")
    ap.add_argument("--registry",
                    help="Non-canonical registry path. Requires --unsafe-test-registry.")
    ap.add_argument("--unsafe-test-registry", action="store_true",
                    help="Permit a non-canonical registry. Testing only: a supplied "
                         "registry can weaken every constraint the canonical one states.")
    ap.add_argument("--task-class")
    ap.add_argument("--sensitivity", default="internal", choices=CLASS_ORDER)
    ap.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    ap.add_argument("--consequential", action="store_true")
    ap.add_argument("--external-action", action="store_true")
    ap.add_argument("--explicit-model")
    ap.add_argument("--approved-explicit", action="store_true",
                    help="User has approved this explicit route for restricted material")
    ap.add_argument("--unavailable", default="", help="comma-separated provider or model ids")
    ap.add_argument("--live-availability", action="store_true",
                    help="exclude models unavailable on the active Orca account or exhausted quota")
    ap.add_argument("--quota", default="", help="provider:percent,provider:percent")
    ap.add_argument("--required-tools", default="",
                    help="comma-separated tool ids the task needs")
    ap.add_argument("--context-need", choices=["1m"], default=None)
    ap.add_argument("--producer-model", default=None,
                    help="model(s) that produced the work under review, comma "
                         "separated; a route declaring must_differ_from_producer "
                         "excludes every named model's whole provider")
    ap.add_argument("--require-primary", action="store_true",
                    help="refuse rather than use a fallback model for this request")
    ap.add_argument("--task-id")
    ap.add_argument("--run-key", default=None,
                    help="dispatcher run key; written on the decision row so a "
                         "settled lane can find its own route again")
    ap.add_argument("--unit-id", default=None,
                    help="plan unit id; the other half of the outcome join key")
    ap.add_argument("--record-outcome", action="store_true",
                    help="read one outcome payload as JSON on stdin and append "
                         "it against its route decision; idempotent")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check-availability", action="store_true")
    ap.add_argument("--worker-account", action="store_true",
                    help="print Orca's attestation of the account a worker launched "
                         "now would hold, and exit")
    args = ap.parse_args()

    if args.check_availability:
        print(json.dumps(check_availability(), indent=2))
        return 0

    if args.record_outcome:

        try:
            payload = json.loads(sys.stdin.read())
        except ValueError as exc:
            print(json.dumps({"error": f"outcome payload is not JSON: {exc}"}))
            return 2
        try:
            result = reconcile_outcome(payload)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(result, indent=2 if not args.json else None))
        return 0

    if args.worker_account:

        print(json.dumps(worker_account(account_data()), indent=2))
        return 0

    canonical = os.path.join(institution_dir(), "MODEL-REGISTRY.yaml")
    if args.registry and os.path.realpath(args.registry) != os.path.realpath(canonical):

        if not args.unsafe_test_registry:
            print("REFUSED: non-canonical registry requires --unsafe-test-registry.\n"
                  f"  canonical: {canonical}\n  supplied : {args.registry}",
                  file=sys.stderr)
            return 3
        print(f"WARNING: routing against a NON-CANONICAL registry: {args.registry}",
              file=sys.stderr)
        inst = os.path.dirname(os.path.realpath(args.registry))
        reg_path = args.registry
    else:
        inst = institution_dir()
        reg_path = canonical
    reg = load_yaml(reg_path)

    if not args.task_class:
        ap.error("--task-class is required (or use --check-availability)")

    quota = {}
    for part in filter(None, args.quota.split(",")):
        k, _, v = part.partition(":")
        quota[k.strip()] = float(v)
    unavailable = tuple(x.strip() for x in args.unavailable.split(",") if x.strip())
    attested = None
    if args.live_availability:

        data = account_data()
        attested = worker_account(data)
        unavailable = tuple(sorted(
            set(unavailable) | set(unavailable_models_from_account_data(reg, data))))

    t0 = time.time()
    try:
        d = route(reg, task_class=args.task_class, sensitivity=args.sensitivity,
                  risk=args.risk, consequential=args.consequential,
                  external_action=args.external_action, explicit_model=args.explicit_model,
                  unavailable=unavailable, quota=quota, approved_explicit=args.approved_explicit,
                  required_tools=tuple(t.strip() for t in args.required_tools.split(",") if t.strip()),
                  context_need=args.context_need, require_primary=args.require_primary,
                  producer_model=args.producer_model)
    except Denied as exc:
        payload = {"refused": str(exc), "task_class": args.task_class,
                   "sensitivity": args.sensitivity}
        print(json.dumps(payload, indent=2) if args.json else f"REFUSED: {exc}")
        return 2

    if attested is not None and d.get("provider") == "claude":

        d["worker_account"] = attested

    if args.log:
        d["_audit"] = log_decision(d, praxis_task_id=args.task_id,
                                   latency_ms=int((time.time() - t0) * 1000), inst=inst,
                                   run_key=args.run_key, unit_id=args.unit_id)

    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print(f"route          {d['task_class']}")
        print(f"model          {d['effective_model']} ({d['provider']}) at {d['effort']}")
        if d.get("worker_account"):
            acct = d["worker_account"]
            print(f"account        {acct.get('kind') or 'UNPROVEN'} "
                  f"({acct.get('unproven') or acct.get('source')})")
        if d["fallback"]:
            print(f"fallback       {d['fallback']['from']} -> {d['fallback']['to']} "
                  f"({d['fallback']['because']})")
        if d["owner_stays"]:
            print(f"owner stays    {d['owner_stays']}")
        if d["verification"]:
            print(f"verification   {d['verification']}")
        if d.get("pilot_route"):
            print(f"pilot          {d['effective_model']} under {d.get('pilot_decision')} until {d.get('pilot_expires')}")
        if d.get("benchmark_waived"):
            print(f"benchmark      WAIVED for {d['task_class']}")
        for g in d["gates"]:
            print(f"gate           {g}")
        for step in d.get("trace", []):
            mark = "ok " if step["ok"] else "no "
            print(f"  filter       {mark}{step['model']}: {step['reason']}")
        if "_audit" in d:
            print(f"logged         {d['_audit']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
