
from datetime import datetime, timezone
import copy

from projection import claim_entry
from engine_qualification import MODULES, UnresolvedAttempt

def guard_context(values):

    def immutable(value):
        return (type(value) in {type(None), bool, int, float, str}
                or type(value) is tuple and all(immutable(item) for item in value))
    if type(values) is not dict or any(type(key) is not str or not immutable(value)
                                      for key, value in values.items()):
        raise UnresolvedAttempt("nested mutable or unsupported context refused before backend entry")

def isolate_context(context):

    isolated = MODULES["context"].PipelineContext()
    isolated.update(copy.deepcopy(context.snapshot()))
    for entry in copy.deepcopy(context.get_logs()):
        isolated.append_log(entry)
    return isolated

def clone_for_branch(engine):

    return engine.clone_for_branch(context=isolate_context(engine.context))

class PraxisAdapter:
    def __init__(self, backend, session_dir, *, workspace, graph_run_id):
        if not all(isinstance(value, str) and value for value in (workspace, graph_run_id)):
            raise ValueError("explicit workspace and graph occurrence required")
        self.backend = backend
        self.session_dir = session_dir
        self.workspace = workspace
        self.graph_run_id = graph_run_id
        self.visits = {}

    async def emit(self, name, data):
        if name == "pipeline:node_start":
            self.visits[data["node_id"]] = data["execution_index"]
        await self.backend.emit(name, data)

    async def run(self, node, prompt, context, incoming_edge=None, graph=None):
        guard_context(context.snapshot())

        backend_context = isolate_context(context)
        visit = self.visits.get(node.id)
        try:
            entered = claim_entry(self.session_dir, workspace=self.workspace,
                graph_run_id=self.graph_run_id, node_id=node.id, visit=visit,
                timestamp=datetime.now(timezone.utc).isoformat())
        except (OSError, ValueError) as exc:
            raise UnresolvedAttempt("entry intent not durably confirmed; no backend entry") from exc
        if not entered:
            raise UnresolvedAttempt("prior entry intent requires exact reconciliation; no backend re-entry")
        result = await self.backend.run(node, prompt, backend_context, incoming_edge=incoming_edge, graph=graph)
        if result.context_updates is not None:
            guard_context(result.context_updates)
        return result

    def clone(self):
                                                                                
        result = PraxisAdapter(self.backend.clone(), self.session_dir,
                               workspace=self.workspace, graph_run_id=self.graph_run_id)
        result.visits = dict(self.visits)
        return result
