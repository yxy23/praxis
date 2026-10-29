
from pathlib import Path
import shlex
import sys

import registry

PREFIX='/adaptive'

def intent(body):

    import room_commands
    body=room_commands.body(body)
    parts=body.split(None,1)
    if not parts or parts[0] != PREFIX:return ''
    if len(parts) < 2:
        raise registry.RegistryError('adaptive_objective_required: supply an objective or compile/run/query arguments')
    objective=parts[1].strip()
    if objective.split()[0] in {'compile','continue','reconcile','run','query'}:return ''
    return ('GENERAL ADAPTIVE WORKFLOW\nUser objective: '+objective+
        '\nAthena: construct a logical graph and finite justified policy using the existing adaptive_contract schema, '
        'unless the user supplied a graph; preserve a supplied graph after validation. '
        'Save graph and policy beside the ordinary internal handoff. Bind current source hashes. '
        'Do not select requested or explicit models in the graph; the canonical registry router selects them. '
        'For ordinary substantive tasks use native_task nodes with admission and all_input_join gates. '
        'Each task declares a registered route seat, task, scope {workspace,write_roots,skills,tools,network,publication}, '
        'actual outputs [{path,format,contains or required_members when useful}], and checks [{argv,cwd,timeout_seconds}]. '
        'Bind graph authorizing_event to the real Switchboard request_id,event_id,user_event_sha256,context_room_id,context_digest. '
        'Leo must reconcile the scope with the original request, canonical owning context and existing skills; '
        'resolve folders/destinations there without requiring literal paths or URLs in the user objective. '
        'Before compile, Leo writes scope assignments {node_id:{scope,authority:{request_quote,reason,context_bindings,publication_authorized}}} '
        'and calls adaptive_general.py authorize --root ROOT --logical GRAPH --graph-id ID --assignment ASSIGNMENT. '
        'This trusted coordinator assignment is a request-bound handoff artifact, not graph-supplied authority or a new human approval. '
        'Network is none/read/publish; publication is null for preparation, or {argv,cwd,readback_url} '
        'only when the actual request authorizes that resolved destination/route. HTTPS and explicit HTTP loopback are supported. '
        'Optional publication verification {artifact_path,marker[,argv,cwd,timeout_seconds]} binds a stable marker in a declared build artifact '
        'to independent HTTP or authenticated host-tool readback; command argv must include the exact destination and enforce its redirect boundary. '
        'Keep authentication in existing tool sessions/environment, never literal secrets in graph/receipt. '
        'Workers use existing skills and tools, can fetch new sources, write prose, create editable decks, edit/test code, '
        'install project-local dependencies inside scope and build/preview or explicitly publish sites. '
        'Reference bindings use role=reference for immutable copies retaining original path/hash; '
        'current instructions use role=operating and are recorded per launch, not frozen evidence. '
        'Mutable edit targets must not be reference bindings. Existing read-only and housekeeping node contracts remain. '
        'Leo: read Athena\'s actual graph and handoff, compile/freeze it with services/praxis45/adaptive_general.py compile, '
        'then invoke its execute command using the pinned installed engine and the exact compile receipt hash. '
        'Use a fresh isolated ADAPTIVE_FIXTURE_ROOT. The existing engine advances layers through canonical named workers, '
        'NativeBridge, NativeOutbox and LearningBridge. Query the resulting CI projection. '
        'Consume only existing applicable accepted corrections with exact target/parent/authority; '
        'absence of an applicable correction is an explicit learning limit. '
        'Use adaptive_general.py continue --continue-from OLD_ROOT with logical/policy/graph-id/root for a fresh occurrence. '
        'Preserve settled successful nodes and artifacts; add new revision nodes for changes; never replay successful/uncertain effects. '
        'If an original settled task needs only verification/readback rechecked, use the documented reconcile command; '
        'it must preserve original evidence and launch no worker or publication. Prefer build/test nodes before a publication node. '
        'No new executor, upstream fork/resume, automatic retry, schedules, system-wide or Praxis runtime installation. '
        'Report native identity, acceptance, output, CI, rule supply/use and benefit separately. '
        'Never fall back to ordinary prose or claim adaptive success for work done outside the graph. Do not claim broader workflow semantics or improvement from structural success.')

def require_runtime():
    from adaptive_runtime import adaptive_runtime
    try: return adaptive_runtime()
    except ValueError as exc: raise registry.RegistryError(str(exc)) from exc

def module():
    directory=Path(__file__).resolve().parents[1]/'praxis45'
    if not (directory/'adaptive_general.py').is_file():
        raise registry.RegistryError('adaptive general entry source missing; reviewed source/launcher combination required')
    sys.path.insert(0,str(directory))
    import adaptive_general
    return adaptive_general

def command(room, project, line, *, submit):
    if room is not None and 'Leo' not in room.recipients:
        raise registry.RegistryError('adaptive entry requires canonical Leo')
    parts=line.split(None,2)
    if not parts or parts[0]!=PREFIX:raise ValueError('expected /adaptive command')
    if len(parts)<2:raise ValueError('usage: /adaptive <objective> | compile | continue | reconcile | run | query')
    action=parts[1]
    if action not in {'compile','continue','reconcile','run','query'}:
        require_runtime()
        return submit(line)
    tokens=shlex.split(line)
    if action == 'run': require_runtime()
    import argparse
    p=argparse.ArgumentParser(prog='/adaptive '+action,exit_on_error=False)
    p.add_argument('--root',type=Path,required=True)
    if action in {'compile','continue'}:
        p.add_argument('--logical',type=Path,required=True);p.add_argument('--policy',type=Path,required=True)
        if action=='continue': p.add_argument('--continue-from',type=Path,required=True)
        p.add_argument('--graph-id',required=True);p.add_argument('--origin',choices=['athena','supplied'],default='supplied')
    if action=='run':
        p.add_argument('--expected-compile-sha256',required=True)
        p.add_argument('--engine-source',type=Path,required=True);p.add_argument('--engine-closure',type=Path,required=True)
        p.add_argument('--corrections',type=Path)
    if action=='reconcile':
        p.add_argument('--node',required=True)
        p.add_argument('--expected-compile-sha256',required=True)
    try:args=p.parse_args(tokens[2:])
    except (SystemExit,argparse.ArgumentError) as exc:raise ValueError('invalid /adaptive arguments') from exc
    entry=module()
    if action in {'compile','continue'}:return entry.compile_entry(args.logical,args.policy,args.graph_id,args.root,origin=args.origin,**({'continue_from':args.continue_from} if action=='continue' else {}))
    if action=='query':return entry.query(args.root)
    if action=='reconcile':
        return entry.reconcile_entry(args.root,args.node,args.expected_compile_sha256)

    runtime=require_runtime()
    argv=[runtime['python'],'-I','-B',str(Path(entry.__file__).resolve()),'execute',*tokens[2:]]
    if args.engine_source.resolve() != Path(runtime['engine_source']) or args.engine_closure.resolve() != Path(runtime['engine_closure']):
        raise ValueError('adaptive_runtime_mismatch: use the resolved pinned room runtime paths')
    return submit('@Leo Execute this explicitly requested bounded adaptive graph now: '+shlex.join(argv)+
        '. Validate the frozen graph, policy, source hashes and compile receipt before launch; '
        'use the existing deterministic coordinator, inspect the durable qualification-result.json and CI query, '
        'retain original identities on uncertainty, and report only demonstrated results. '
        'Use verified request-bound task scope for substantive work and declared output/check validation; distinguish preparation from publication/readback. Never replay successful or uncertain effects.')

def global_context():
    import project_rooms
    note = registry.vault_root()/'system/memory/shared/workspace.md'
    if not note.is_file(): raise registry.RegistryError('canonical global workspace source missing')
    return project_rooms.ProjectContext('Global Leo workspace', note, 'global-workspace')

def submit_context(room, project, text, *, sender, post, board_factory=None, event_id=None):
    import project_rooms
    from service import Switchboard
    board = board_factory() if board_factory else Switchboard(check_account=True, outdir=project_rooms.output_root(project))
    return board.handle(project_rooms.contextual_request(room, project, text), sender, post,
        context=project, context_room_id=room.room_id, thread_id=project.thread_id, event_id=event_id)

def global_command(line, *, sender, post, board_factory=None, context=None):
    from service import Switchboard
    project = context or global_context()
    def submit(text):
        board = board_factory() if board_factory else Switchboard(check_account=True)
        return board.handle(text, sender, post, context=project, adaptive_entry=True)
    return command(None, project, line, submit=submit)
