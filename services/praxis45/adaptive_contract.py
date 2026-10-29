
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

PROFILE = "praxis.adaptive.shadow.v1"
SCHEMA = "praxis.adaptive-workflows.logical-design.v1"
STATES = frozenset({"succeeded", "failed", "held", "unknown",
                    "skipped_intentional", "checkpointed_unresolved"})
SETTLED = frozenset({"succeeded", "failed", "held", "skipped_intentional"})
OPS = frozenset({"all_required_inputs_valid", "not", "supplied_graph_present",
    "settled", "all_settled", "has_authorized_valid_subset", "has_readable_sources",
    "has_verified_subset", "all_required_sources_same_date_valid", "validated",
    "has_authorized_verified_work", "has_verified_reply_work",
    "checkpoint_readback_valid_for_effect", "applicable_acceptance_exists",
    "regression_or_invalidated_authority", "exactly_one_selected_graph_valid",
    "matching_later_occurrence_receipt"})
KINDS = frozenset({"input", "planner", "conditional_join", "validator", "terminal",
    "admission", "native_readonly_worker", "evidence_review", "all_input_join",
    "serialized_effect", "conditional", "pure", "native_source_worker",
    "deterministic_effect", "checkpoint", "single_writer_admission",
    "deterministic_io", "local_read_queue", "same_writer_judgment",
    "same_writer_serialized_effect", "same_writer_validator",
    "same_writer_all_input_join", "evidence_capture", "analysis", "proposal",
    "isolated_evaluation", "authority_gate", "existing_authorized_effect",
    "cross_occurrence_receipt_gate", "conditional_authorized_effect", "native_task"})
BOUNDS = ("max_nodes", "max_edges", "max_parallel", "max_items", "start_ms",
          "activity_ms", "no_progress_ms", "queue_ms")

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def validate_bounds(policy):
    if not isinstance(policy, dict) or any(type(policy.get(k)) is not int or
            policy[k] <= 0 for k in BOUNDS):
        raise ValueError("explicit positive finite resource/collection bounds required")
    if policy.get("max_revisions") != 0 or type(policy.get("max_revisions")) is not int:
        raise ValueError("this profile supports zero automatic revisions only")
    if "overall_timeout_ms" in policy:
        raise ValueError("zero sentinel and blanket total-time policy unsupported")
    if not isinstance(policy.get("justification"), dict) or any(
            not policy["justification"].get(k) for k in (*BOUNDS, "max_revisions")):
        raise ValueError("every engineering bound needs its source/test justification")

def predicate_schema(value, node_ids):
    if not isinstance(value, dict) or set(value) != {"op", "args"}:
        raise ValueError("typed predicate object required")
    op, args = value["op"], value["args"]
    if op not in OPS or not isinstance(args, list):
        raise ValueError("unsupported predicate")
    if op == "not":
        if len(args) != 1:
            raise ValueError("not requires one predicate")
        predicate_schema(args[0], node_ids)
    elif op in {"settled", "validated", "all_settled", "has_authorized_valid_subset"}:
        if not args or any(not isinstance(a, str) or a not in node_ids for a in args):
            raise ValueError("unresolved predicate node reference")
        if op in {"settled", "validated"} and len(args) != 1:
            raise ValueError("one receipt reference required")
    elif op in {"has_readable_sources", "has_verified_subset"}:
        if len(args) != 1 or args[0] not in {"primary", "gmail"}:
            raise ValueError("unknown account")
    elif op == "matching_later_occurrence_receipt":
        if len(args) != 1 or args[0] not in node_ids:
            raise ValueError("unresolved dormant criterion")
    elif args:
        raise ValueError("predicate takes no arguments")

def predicate(value, receipts, dependencies, facts):

    op, args = value["op"], value["args"]
    if op == "not":
        result = predicate(args[0], receipts, dependencies, facts)
        return None if result is None else not result
    if op in {"settled", "all_settled"}:
        return all(receipts.get(a, {}).get("state") in SETTLED for a in args)
    if op == "validated":
        return receipts.get(args[0], {}).get("state") == "succeeded"
    if op == "all_required_inputs_valid":
        return all(receipts.get(a, {}).get("state") == "succeeded" for a in dependencies)
    key = op + ":" + ",".join(args)
    if facts.get(key) is not None and type(facts[key]) is not bool:
        raise ValueError("predicate fact must be boolean or unknown")
    producers = [receipts.get(a, {}) for a in dependencies]

    if op in {"has_readable_sources", "has_verified_subset", "validated",
              "checkpoint_readback_valid_for_effect", "applicable_acceptance_exists"}:
        if not producers or any(r.get("state") != "succeeded" for r in producers):
            return None
    values = [r.get("predicates", {}).get(key) for r in producers
              if r.get("state") == "succeeded" and key in r.get("predicates", {})]
    if any(type(v) is not bool for v in values):
        raise ValueError("receipt predicate must be boolean")
    if not values or len(set(values)) != 1:
        return None
    return values[0]

def expand_manifest(items, *, exhausted, policy):
    validate_bounds(policy)
    if exhausted is not True or not isinstance(items, list) or len(items) > policy["max_items"]:
        raise ValueError("finite exhausted manifest required")
    ids = [r.get("id") for r in items if isinstance(r, dict)]
    if len(ids) != len(items) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("unique stable manifest IDs required")
    return copy.deepcopy(items)

def compile_graph(document, graph_id, policy, *, profile=PROFILE, origin="supplied",
                  require_native=False, source_snapshots=None):
    if profile != PROFILE or require_native:
        raise ValueError("native/nonqualified profile unavailable; no fallback")
    if origin not in {"supplied", "athena"}:
        raise ValueError("unrecognized graph origin")
    validate_bounds(policy)
    if document.get("schema_version") != SCHEMA or document.get("executable") is not False or document.get("execution_authority") != "none":
        raise ValueError("logical specification must retain nonexecutable authority boundary")
    definitions = document.get("graph_definitions", [])
    ids = [g["id"] for g in definitions]
    if len(ids) != len(set(ids)) or ids.count(graph_id) != 1:
        raise ValueError("unique graph identity required")
    graph = copy.deepcopy(next(g for g in definitions if g["id"] == graph_id))
    nodes, edges = graph["nodes"], graph["edges"]
    names = [n["id"] for n in nodes]
    if not names or len(names) > policy["max_nodes"] or len(edges) > policy["max_edges"]:
        raise ValueError("graph resource envelope exceeded")
    if len(names) != len(set(names)) or any(not re.fullmatch(r"[a-z][a-z_0-9]*", n) for n in names):
        raise ValueError("duplicate/invalid stable node identity")
    names = set(names)
    declared = set()
    for node in nodes:
        if node.get("kind") not in KINDS or any(node.get(k) for k in ("subgraph", "nested_traversal", "loop", "retry_target")):
            raise ValueError("unsupported node or nested traversal")
        deps = node.get("depends_on")
        if not isinstance(deps, list) or len(set(deps)) != len(deps) or set(deps) - names:
            raise ValueError("unresolved/duplicate dependencies")
        if node.get("dependency_mode") != "all_settled_evidence":
            raise ValueError("explicit all-settled dependency contract required")
        predicate_schema(node["activation"], names)
        for binding in node.get("input_bindings", []):
            if binding.get("producer") not in deps:
                raise ValueError("input producer outside declared dependencies")
        if not node.get("outputs") or not node.get("acceptance"):
            raise ValueError("output and acceptance contract required")
        declared.update((dep, node["id"]) for dep in deps)
    actual = [(e["from"], e["to"]) for e in edges]
    if len(actual) != len(set(actual)) or set(actual) != declared:
        raise ValueError("edge/dependency mismatch")
    remaining = {n["id"]: set(n["depends_on"]) for n in nodes}
    layers, done = [], set()
    while remaining:
        ready = sorted(k for k, deps in remaining.items() if deps <= done)
        if not ready:
            raise ValueError("cycle/revision loop refused")
        layers.append(ready)
        done.update(ready)
        for key in ready:
            del remaining[key]
    if len(layers[0]) != 1:
        raise ValueError("one logical entry required")
    sources = copy.deepcopy(document.get("source_bindings", {}))
    if not sources:
        raise ValueError("frozen source bindings required")
    for key, row in sources.items():
        if not isinstance(row, dict) or not Path(row.get("path", "")).is_absolute():
            raise ValueError("unresolved/changed binding: " + key)
        if source_snapshots is not None:

            source = row["path"]
            snapshot = source_snapshots.get(key, {})
            if snapshot.get("source") != source or snapshot.get("sha256") != row["sha256"]:
                raise ValueError("snapshot origin/hash differs: " + key)
            row["source_path"] = source
            row["source_current_at_compile"] = sha(source) == row["sha256"]
            row["path"] = snapshot["snapshot"]
        if not Path(row["path"]).is_absolute() or sha(row["path"]) != row.get("sha256"):
            raise ValueError("unresolved/changed binding: " + key)
    result = {"profile": PROFILE, "version": 1, "native_available": False,
        "modes": ["inspect", "fixture"], "logical_graph": graph,
        "logical_graph_sha256": digest(graph), "specification_sha256": digest(document),
        "source_bindings": sources, "policy": copy.deepcopy(policy), "layers": layers,
        "engine_nodes": {name: "n_" + digest(name)[:20] for name in sorted(names)},
        
        "unsupported": ["native execution/adoption", "upstream resume", "nested traversal",
                        "automatic revision"]}
    if len(set(result["engine_nodes"].values())) != len(names):
        raise ValueError("engine node identity collision")
    engine_lock = Path(__file__).with_name("engine-lock.json")
    pin = json.loads(engine_lock.read_bytes())
    result["engine"] = {"commit": pin["commit"], "closure_sha256": pin["closure_sha256"],
                        "lock_sha256": sha(engine_lock)}
    result["compiled_sha256"] = digest(result)
    return result

def verify_compiled(compiled):
    value = copy.deepcopy(compiled)
    expected = value.pop("compiled_sha256")
    if digest(value) != expected or value.get("profile") != PROFILE or value.get("native_available") is not False:
        raise ValueError("compiled profile changed or native authority unavailable")
    validate_bounds(value["policy"])
    if value["engine"]["lock_sha256"] != sha(Path(__file__).with_name("engine-lock.json")):
        raise ValueError("engine pin changed")
    for key, row in value["source_bindings"].items():
        if sha(row["path"]) != row["sha256"]:
            raise ValueError("compiled source drift: " + key)

    admitted = compile_graph({"schema_version": SCHEMA, "executable": False,
        "execution_authority": "none", "graph_definitions": [value["logical_graph"]],
        "source_bindings": value["source_bindings"]}, value["logical_graph"]["id"], value["policy"])
    for key in ("engine_nodes", "layers", "engine", "unsupported"):
        if admitted[key] != value[key]:
            raise ValueError("compiled structure differs from admission: " + key)
