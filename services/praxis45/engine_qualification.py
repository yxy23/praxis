
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import re
import sys

MODULES = {}
MEASUREMENTS = []

def configure(source, closure_path):
    lock = json.loads((Path(__file__).parent / "engine-lock.json").read_text())
    closure_bytes = Path(closure_path).read_bytes()
    if hashlib.sha256(closure_bytes).hexdigest() != lock["closure_sha256"]:
        raise ValueError("frozen dependency closure changed")
    source = Path(source)
    for row in lock["source_files"]:
        raw = (source / row["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("pinned source changed: " + row["path"])
    import importlib.metadata
    for package in lock["packages"]:
        if importlib.metadata.version(package["name"]) != package["version"]:
            raise ValueError("installed distribution version mismatch: " + package["name"])
    sys.path[:0] = [str(source / "modules/loop-pipeline"), str(source / "modules/unified-llm-client")]
    for name in ("engine", "context", "handlers", "handlers.context", "dot_parser", "outcome", "checkpoint"):
        MODULES[name] = importlib.import_module("amplifier_module_loop_pipeline." + name)
    return lock

def restricted_graph(dot):

    tokens = re.findall(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|[A-Za-z_][A-Za-z_0-9]*|->|[^\s]', dot, re.S)
    tokens = [t for t in tokens if not t.startswith(("//", "/*"))]
    allowed = {"shape", "type", "prompt", "label", "condition", "goal"}
    for i, token in enumerate(tokens[:-1]):
        if tokens[i + 1] == "=" and token.strip('"') not in allowed:
            raise ValueError("attribute refused: " + token)
    graph = MODULES["dot_parser"].parse_dot(dot)
    if graph.default_max_retry != 0 or graph.model_stylesheet or any(
            graph.graph_attrs.get(k) is not None for k in
            ("retry_target", "fallback_retry_target", "max_pipeline_duration")):
        raise ValueError("engine retry/duration configuration refused")
    if set(graph.graph_attrs) - {"label"}:
        raise ValueError("unknown graph attributes refused")
    starts = [n for n in graph.nodes.values() if n.shape == "Mdiamond"]
    if len(starts) != 1:
        raise ValueError("one start required")
    for node in graph.nodes.values():
        if node.shape not in {"Mdiamond", "Msquare", "box"} or node.type not in {"", "start", "exit", "codergen"}:
            raise ValueError("non-allowlisted shape/handler")
        if dict(node.attrs):
            raise ValueError("node behavior overrides refused")
        if ((node.shape == "Mdiamond" and node.type not in {"", "start"}) or
            (node.shape == "Msquare" and node.type not in {"", "exit"}) or
            (node.shape == "box" and (node.type not in {"", "codergen"} or node.is_start_node() or node.is_exit_node()))):
            raise ValueError("shape/handler or reserved node identity mismatch")
        edges = graph.outgoing_edges(node.id)
        if node.shape == "Msquare":
            if edges:
                raise ValueError("terminal nodes cannot progress")
        elif node.shape == "Mdiamond":
            if len(edges) != 1 or edges[0].condition:
                raise ValueError("one start edge required")
        else:
            if len(edges) != 2 or {e.condition for e in edges} != {"outcome=success", "outcome=fail"}:
                raise ValueError("Praxis nodes require exactly status success/fail edges")
            failure = next(e for e in edges if e.condition == "outcome=fail")
            if graph.nodes[failure.to_node].shape != "Msquare":
                raise ValueError("failure must halt at a terminal node")
    for edge in graph.edges:
        if edge.from_node not in graph.nodes or edge.to_node not in graph.nodes:
            raise ValueError("foreign edge target")
        if edge.label or edge.weight or dict(edge.attrs):
            raise ValueError("label outcomes/edge behavior overrides refused")

    visited, pending = set(), set()
    def visit(node):
        if node in pending:
            raise ValueError("loop/restart refused")
        if node in visited:
            return
        pending.add(node)
        for edge in graph.outgoing_edges(node):
            visit(edge.to_node)
        pending.remove(node);visited.add(node)
    visit(starts[0].id)
    if visited != set(graph.nodes):
        raise ValueError("unreachable nodes refused")
    return graph

class UnresolvedAttempt(BaseException):
    pass

class StubBackend:
    def __init__(self, run_id, behavior=None):
        self.run_id = run_id
        self.behavior = dict(behavior or {})
        self.visits = {}
        self.calls = {}
        self.entry_calls = {}
        self.receipts = {}
        self.unresolved = {}
        self.history = []

    async def emit(self, name, data):
        if name == "pipeline:node_start":
            self.visits[data["node_id"]] = data["execution_index"]

    async def run(self, node, prompt, context, incoming_edge=None, graph=None):
        entry_key = (self.run_id, node.id, self.visits.get(node.id))
        self.entry_calls[entry_key] = self.entry_calls.get(entry_key, 0) + 1
        if node.id in self.unresolved:
            raise UnresolvedAttempt("exact synthetic identities still require reconciliation")

        if node.id in self.receipts:
            return self.receipts[node.id]
        visit = self.visits.get(node.id)
        if visit is None:
            raise UnresolvedAttempt("public node-start visit identity absent")
        key = (self.run_id, node.id, visit)
        self.calls[key] = self.calls.get(key, 0) + 1
        self.history.append(node.id)
        outcome_mod = MODULES["outcome"]
        outcome, status = outcome_mod.Outcome, outcome_mod.StageStatus
        behavior = self.behavior.get(node.id, "success")
        try:
            if behavior == "contact_loss":
                raise ConnectionError("synthetic contact loss")
            if behavior == "unknown_launch":
                raise OSError("synthetic launch receipt absent")
            if behavior == "timeout":
                raise TimeoutError("synthetic wait timeout")
            if behavior == "cancelled":
                import asyncio
                raise asyncio.CancelledError("synthetic cancellation")
            if behavior == "exception":
                raise ValueError("synthetic unexpected backend failure")
        except BaseException as exc:
            self.unresolved[node.id] = {"state": "unknown", "error": type(exc).__name__,
                "graph_run_id": self.run_id, "node_id": node.id, "visit": visit,
                "run_id": "run_stub", "task_id": "task_stub", "dispatch_id": "ctx_stub"}
            raise UnresolvedAttempt("unknown/live attempt halted outside success/failure traversal") from exc

        if behavior == "success":
            raw = outcome(status=status.SUCCESS, is_explicit=True)
        elif behavior == "partial":
            raw = outcome(status=status.PARTIAL_SUCCESS, is_explicit=True)
        elif behavior == "retry":
            raw = outcome(status=status.RETRY, is_explicit=True)
        elif behavior == "label":
            raw = outcome(status=status.FAIL, preferred_label="success", is_explicit=True)
        elif behavior == "implicit_success":
            raw = outcome(status=status.SUCCESS, is_explicit=False)
        elif behavior == "suggested_next":
            raw = outcome(status=status.FAIL, suggested_next_ids=["next"], is_explicit=True)
        else:
            raw = outcome(status=status.FAIL, is_explicit=True, failure_reason=behavior)
        valid = (isinstance(raw, outcome) and raw.is_explicit
                 and raw.status in {status.SUCCESS, status.FAIL}
                 and raw.preferred_label is None and raw.suggested_next_ids is None)
        result = raw if valid else outcome(status=status.FAIL, is_explicit=True,
                                            failure_reason="rejected unsafe outcome: " + behavior)
        self.receipts[node.id] = result
        return result

    def clone(self):

        import copy
        return copy.deepcopy(self)

def construct(graph, backend, logs_root, *, context=None):
    ctx = context if context is not None else MODULES["context"].PipelineContext()
    handlers = MODULES["handlers"].HandlerRegistry(
        MODULES["handlers.context"].HandlerContext(backend=backend, hooks=backend))
    return MODULES["engine"].PipelineEngine(graph, ctx, handlers, str(logs_root), hooks=backend)

def construct_adapter(graph, backend, logs_root, *, session_dir, workspace, graph_run_id, context=None):
    from engine_boundary import PraxisAdapter, guard_context, isolate_context

    graph = restricted_graph(graph.dot_source)
    ctx = context if context is not None else MODULES["context"].PipelineContext()
    guard_context(ctx.snapshot())
    return construct(graph, PraxisAdapter(backend, session_dir, workspace=workspace,
                                         graph_run_id=graph_run_id), logs_root,
                     context=isolate_context(ctx))

def evidence_status(engine_outcome, backend):

    status = MODULES["outcome"].StageStatus
    return ("succeeded" if engine_outcome.status == status.SUCCESS and backend.receipts
            and not backend.unresolved and all(row.status == status.SUCCESS and row.is_explicit
                                               for row in backend.receipts.values()) else "failed")
