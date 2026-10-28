
from __future__ import annotations

import asyncio
import copy
import inspect
import json

from adaptive_contract import digest, predicate, verify_compiled, STATES
import engine_qualification as q

def deep_context():
    base = q.MODULES["context"].PipelineContext

    class IsolatedContext(base):
        def clone(self):
            child = IsolatedContext()
            child.update(copy.deepcopy(self.snapshot()))
            for row in copy.deepcopy(self.get_logs()):
                child.append_log(row)
            return child

    return IsolatedContext()

def outcome(state, **kwargs):
    module = q.MODULES["outcome"]
    return module.Outcome(status=module.StageStatus.SUCCESS if state else module.StageStatus.FAIL,
                          is_explicit=True, **kwargs)

def compile_dot(compiled):

    verify_compiled(compiled)
    ids = compiled["engine_nodes"]
    lines = ['digraph Adaptive {', 'start [shape=Mdiamond];',
             'done [shape=Msquare];', 'held [shape=Msquare];',
             'finish [shape=box, type="praxis.finish"];']
    previous = "start"
    for index, layer in enumerate(compiled["layers"]):
        fork, join = "fork_" + str(index), "join_" + str(index)
        if len(layer) == 1:
            name = layer[0]
            lines.extend([f'{ids[name]} [shape=box, prompt="{name}"];',
                f'{join} [shape=box, type="praxis.single_join", required="{ids[name]}"];',
                f'{previous} -> {ids[name]};', f'{ids[name]} -> {join};'])
            previous = join
            continue
        lines.extend([f'{fork} [shape=component, max_parallel="{compiled["policy"]["max_parallel"]}", join_policy="wait_all", error_policy="continue"];',
                      f'{join} [shape=tripleoctagon, required="{"|".join(ids[n] for n in layer)}"];',
                      f'{previous} -> {fork};'])
        for name in layer:
            lines.extend([f'{ids[name]} [shape=box, prompt="{name}"];',
                          f'{fork} -> {ids[name]};', f'{ids[name]} -> {join};'])

        previous = join
    lines.extend([f'{previous} -> finish;',
                  'finish -> done [condition="outcome=success"];',
                  'finish -> held [condition="outcome=fail"];', '}'])
    return "\n".join(lines)

class AllRequired:

    async def execute(self, node, context, graph, logs_root, **kwargs):
        return aggregate(node, context, context.get("parallel.results", []))

def aggregate(node, context, rows):
    required = str(node.attrs["required"]).split("|")
    raw = copy.deepcopy(rows)
    by_id = {}
    duplicate = False
    for row in raw:
        name = row.get("node_id")
        duplicate |= name in by_id
        by_id[name] = row
    receipts = copy.deepcopy(context.get("praxis.receipts", {}))
    complete = not duplicate and set(by_id) == set(required)
    identities = context.get("praxis.engine_to_logical", {})
    for name in required:
        row = by_id.get(name, {"node_id": name, "status": "missing"})
        logical = (row.get("context_updates") or {}).get("praxis.row")
        valid = (isinstance(logical, dict) and logical.get("node_id") == identities.get(name)
                 and logical.get("occurrence_id") == context.get("praxis.occurrence")
                 and logical.get("revision_id") == 0 and logical.get("state") in STATES)
        if valid:
            receipts[logical["node_id"]] = copy.deepcopy(logical)
        complete &= bool(valid and logical.get("state") in {"succeeded", "skipped_intentional"}
                         and row.get("status") == "success")
    joins = copy.deepcopy(context.get("praxis.joins", {}))
    joins[node.id] = {"required": required, "rows": raw,
                     "missing": sorted(set(required) - set(by_id)),
                     "duplicates": duplicate, "complete": complete}
    context.set("praxis.receipts", receipts)
    context.set("praxis.joins", joins)
    return outcome(complete, failure_reason=None if complete else "required branch failed/missing/unknown")

class SingleRequired:
    async def execute(self, node, context, graph, logs_root, **kwargs):
        row = copy.deepcopy(context.get("praxis.row"))
        raw = [{"node_id": node.attrs["required"],
                "status": "success" if row and row.get("state") in {"succeeded", "skipped_intentional"} else "fail",
                "notes": None, "context_updates": {"praxis.row": row}}]
        return aggregate(node, context, raw)

class Finish:
    async def execute(self, node, context, graph, logs_root, **kwargs):
        expected = set(context.get("praxis.expected"))
        rows = context.get("praxis.receipts", {})
        valid = (set(rows) == expected and all(row["state"] in
                 {"succeeded", "skipped_intentional"} for row in rows.values())
                 and set(context.get("praxis.joins", {})) == set(context.get("praxis.expected_joins"))
                 and all(row["complete"] for row in context.get("praxis.joins", {}).values()))
        context.set("praxis.complete", valid)
        return outcome(valid, failure_reason=None if valid else "incomplete logical evidence")

class ShadowBackend:
    def __init__(self, compiled, occurrence, execute, *, facts=None, recovered=None, observer=None):
        self.compiled = copy.deepcopy(compiled)
        self.occurrence, self.execute = occurrence, execute
        self.facts = copy.deepcopy(facts or {})
        self.recovered = copy.deepcopy(recovered) if recovered is not None else None
        self.observer = observer if observer is not None else []
        self.history = []

    def clone(self):
        return ShadowBackend(self.compiled, self.occurrence, self.execute, facts=self.facts,
                             recovered=self.recovered, observer=self.observer)

    async def emit(self, name, data):
        self.observer.append({"event": name, "data": copy.deepcopy(data)})

    async def run(self, node, prompt, context, **kwargs):
        logical = next(n for n in self.compiled["logical_graph"]["nodes"]
                       if self.compiled["engine_nodes"][n["id"]] == node.id)
        name = logical["id"]
        identity = {"occurrence_id": self.occurrence, "node_id": name, "revision_id": 0}
        self.history.append(name)
        accepted = context.get("praxis.receipts", {})
        enabled = predicate(logical["activation"], accepted, logical["depends_on"], self.facts)
        if self.recovered is not None:
            row = self.recovered.get(name)
            if row is None:
                row = dict(identity, state="held", reason="recovery cannot start an old dependent")
            elif any(row.get(k) != v for k, v in identity.items()) or row.get("state") not in STATES:
                raise ValueError("recovered receipt identity/state mismatch")
        elif enabled is not True:
                                                                                  
            intentional = enabled is False and logical["activation"]["op"] in {
                "not", "supplied_graph_present", "regression_or_invalidated_authority"}
            row = dict(identity, state="skipped_intentional" if intentional else "held",
                       reason="predicate false" if enabled is False else "predicate evidence unknown")
        else:
            try:
                value = self.execute(copy.deepcopy(logical), context, copy.deepcopy(identity))
                if inspect.isawaitable(value):
                    value = await value
                row = dict(value, **identity)
                if row.get("state") not in STATES:
                    raise ValueError("typed logical outcome required")
            except (Exception, asyncio.CancelledError) as exc:
                row = dict(identity, state="unknown", reason=type(exc).__name__ + ": " + str(exc))
        return outcome(row["state"] in {"succeeded", "skipped_intentional"},
                       context_updates={"praxis.row": copy.deepcopy(row)})

async def run_shadow(compiled, occurrence, execute, logs_root, *, facts=None,
                     recovered=None, seed=None):
    verify_compiled(compiled)
    context = deep_context()
    context.update(copy.deepcopy(seed or {}))
    context.set("praxis.expected", list(compiled["engine_nodes"]))
    context.set("praxis.engine_to_logical", {v: k for k, v in compiled["engine_nodes"].items()})
    context.set("praxis.occurrence", occurrence)
    context.set("praxis.expected_joins", ["join_" + str(i) for i in range(len(compiled["layers"]))])
    context.set("praxis.receipts", {})
    context.set("praxis.complete", False)
    backend = ShadowBackend(compiled, occurrence, execute, facts=facts, recovered=recovered)
    graph = q.MODULES["dot_parser"].parse_dot(compile_dot(compiled))
    engine = q.construct(graph, backend, logs_root, context=context)
    engine.handler_registry.register("parallel.fan_in", AllRequired())
    engine.handler_registry.register("praxis.single_join", SingleRequired())
    engine.handler_registry.register("praxis.finish", Finish())
    raw = await engine.run()
    return {"profile": compiled["profile"], "native_launches": 0,
            "status": "succeeded" if context.get("praxis.complete") is True else "incomplete",
            "engine_status": raw.status.value, "occurrence_id": occurrence,
            "receipts": copy.deepcopy(context.get("praxis.receipts")),
            "joins": copy.deepcopy(context.get("praxis.joins")),
            "context": copy.deepcopy(context.snapshot()), "events": backend.observer,
            "dot_sha256": digest(compile_dot(compiled))}
