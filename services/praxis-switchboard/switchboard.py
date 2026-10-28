                      

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import registry                                                        
import rooms                                                           
import project_rooms                                                   
import research_trace                                                  
import live_room                                                       
import room_ui                                                         
import room_commands
import room_identity
import room_runtime                                                    
import room_planning                                                   
import routing                                                         
import transport_local                                                 
from routing import Allowlist, Sender, parse_recipients                
from service import Switchboard, handle_ordinary                                        
from state import SwitchboardState, default_state_path                 

def _sender() -> Sender:
    return Sender(user_id=os.environ.get("USER", "local"), transport="local",
                  display=os.environ.get("USER", "local"))

def cmd_send(args) -> int:
    print(room_identity.banner())
    text = " ".join(args.text).strip()
    if not text:
        print("nothing to send", file=sys.stderr)
        return 2
    stopped = room_commands.refusal(text, 'send')
    if stopped:
        print(stopped)
        return 2
    if room_commands.token(text) == '/help':
        print(room_identity.banner()+'\n'+room_commands.help_text('send'))
        return 0
    if room_commands.token(text) == '/adaptive':
        import room_adaptive
        result = room_adaptive.global_command(text, sender=_sender(),
            post=transport_local.make_printer(args.plain,args.quiet),
            board_factory=lambda: Switchboard(check_account=not args.no_account_check))
        if 'lifecycle' not in result: print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('lifecycle', 'completed') in {'completed','awaiting_user'} else 1
    ordinary_result=handle_ordinary(text,transport_local.make_printer(args.plain,args.quiet))
    if ordinary_result is not None:
        return 0 if ordinary_result['lifecycle']=='completed' else 4
    board = Switchboard(check_account=not args.no_account_check)
    record = board.handle(text, _sender(),
                          transport_local.make_printer(args.plain, args.quiet),
                          context=getattr(args, "context", None),
                          explicit_model=getattr(args, "model", None),
                          task_mode=getattr(args, "mode", None))
    return 0 if record.get("lifecycle") in ("completed", "awaiting_user") else 1

def _room_preflight(room: rooms.Room, agenda: str):
    stopped = room_commands.refusal(agenda, 'convene')
    if stopped or room_commands.token(agenda):
        raise registry.RegistryError(stopped or room_commands.help_text('convene'))
    text = rooms.addressed_request(room, agenda)
    req = routing.build(text, _sender(), log_routes=False,
                        room_id=room.room_id, room_agenda=agenda)
    account = registry.worker_account()
    failures = []
    for name in req.recipients:
        registry.inherited_skill_paths(req.routes[name])
        blocked = registry.account_blocked(req.routes[name], account)
        if blocked:
            failures.append(blocked)
    if failures:
        raise registry.RegistryError("; ".join(failures))
    if not req.chaired or room.chair not in req.recipients:
        raise registry.RegistryError(
            f"{room.title} has no routed chair; refusing a fake room")
    return text, req

class LeoRoomSession:

    def __init__(self, board_factory, sender: Sender, post_factory):
        self.board_factory = board_factory
        self.sender = sender
        self.post_factory = post_factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="leo-room")
        self._lock = threading.Lock()
        self._futures: list[Future] = []
        self._submitted = 0

    def submit(self, prompt: str, *, post=None) -> int:
        text = prompt.strip()
        if not text:
            raise registry.RegistryError("a prompt is required")
        stopped = room_commands.refusal(text, 'leo')
        if stopped: raise registry.RegistryError(stopped)
        with self._lock:
            self._submitted += 1
            number = self._submitted
        future = self.executor.submit(self._run, text, post)
        with self._lock:
            self._futures.append(future)
        return number

    def _run(self, prompt: str, post=None):
        if room_commands.token(prompt) == '/adaptive':
            import room_adaptive
            poster = post if post is not None else self.post_factory()
            result = room_adaptive.global_command(prompt,sender=self.sender,post=poster,board_factory=self.board_factory)
            if 'lifecycle' not in result:
                poster({'type':'complete','lifecycle':'completed','message':json.dumps(result,ensure_ascii=False)})
            return result

        if not prompt.startswith('@'):
            poster = post if post is not None else self.post_factory()
            ordinary = handle_ordinary('@Leo '+prompt, poster)
            if ordinary is not None:return ordinary
        board = self.board_factory()
        post = post if post is not None else self.post_factory()
        try:
            return board.handle(prompt, self.sender, post)
        except Exception as exc:                                               
            post({"type": "dispatch_failed", "error": str(exc)})
            return {"lifecycle": "failed", "reasons": [str(exc)]}

    def active(self) -> int:
        with self._lock:
            self._futures = [future for future in self._futures if not future.done()]
            return len(self._futures)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

@room_ui.surface
def _run_leo_room(*, ui) -> int:

    ui.note(room_identity.banner())
    session = LeoRoomSession(
        board_factory=lambda: Switchboard(check_account=True),
        sender=_sender(),
        post_factory=lambda: ui.printer(None),
    )
    ui.block("LEO ROOM", "Type normally for Leo, or begin with @Name.\n"
             "Each prompt creates a fresh request; no worker is retained.", role="room")
    ui.note("Enter sends · Esc then Enter adds a line · /status · /help · /exit")
    try:
        while True:
            try:
                line = ui.read()
            except EOFError:
                line = "/exit"
            if not line:
                continue
            stopped = room_commands.refusal(line, 'leo')
            if stopped:
                ui.note(stopped)
                continue
            if line in {"/exit", "/quit"}:
                active = session.active()
                if active:
                    ui.note(f"{active} prompt(s) still running; the room will close after they finish.")
                break
            if line == "/status":
                ui.note(f"{session.active()} prompt(s) running or queued. No worker is retained.")
                continue
            if line == "/help":
                ui.note(room_identity.banner()+"\n"+room_commands.help_text('leo'))
                continue
            turn = ui.user(line)
            session.submit(line, post=ui.printer(turn))
            ui.note("Accepted. You may type the next prompt now.", turn)
    except KeyboardInterrupt:
        ui.note("Closing after active work finishes.")
    finally:
        session.close()
    return 0

def _planning_event_id(project: project_rooms.ProjectContext, line: str) -> str:
    normalized = " ".join(line.split())
    return "room-input:" + hashlib.sha256(
        (project_rooms.context_identity(project) + "\n" + normalized).encode()
    ).hexdigest()[:24]

@room_ui.surface
def _run_project_room(room: rooms.Room, project: project_rooms.ProjectContext,
                      *, ui) -> int:

    ui.note(room_identity.banner())
    session = project_rooms.ProjectRoomSession(
        room, project,
        board_factory=lambda: Switchboard(check_account=True,
                                          outdir=project_rooms.output_root(project)),
        sender=_sender(),
        post_factory=lambda: ui.printer(None),
        max_parallel_prompts=1,
    )
    planning_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="room-planning")
    ui.block(f"{project.title} · {room.title}", f"Context: {project.note}\n"
             f"Outputs: {project_rooms.output_root(project)}\n"
             f"Chair: {room.chair}. Type normally to address {room.chair}.\n"
             "No work has been dispatched. Use @Name or @all.", role="room")
    trace_note = ""
    if research_trace.enabled(project):
        trace_note = f"\nResearch trace: enabled · {research_trace.report_path(project)}"
    import room_attractor
    if room_attractor.selected(project, room.room_id):
        trace_note += ('\nEngine: installed Attractor · addressed seats only · @Name selects seats; '
                       '@all selects the roster (Athena first, Leo last) · '
                       'attractor preview: <prompt> launches nothing · '
                       'conditional internal handoffs unsupported; ordinary continuation required')
    ui.note("Enter sends · Esc then Enter adds a line · /status · /trace · /help · /exit"
            + (" · /plan · /decide" if room.planning_template else "")
            + trace_note)

    planning_executor.submit(session.recover_pending, ui.printer(None))
    try:
        while True:
            try:
                line = ui.read()
            except EOFError:
                line = "/exit"
            if not line:
                continue
            stopped = room_commands.refusal(line, 'ordinary', room)
            if stopped:
                ui.note(stopped)
                continue
            if line in {"/exit", "/quit"}:
                active = session.active()
                if active:
                    ui.note(f"{active} prompt(s) still running; the room will close after they finish.")
                break
            if line == "/status":
                ui.note(f"{session.active()} prompt(s) running or queued.")
                planning_executor.submit(session.recover_pending, ui.printer(None))
                continue
            if room_commands.token(line) == "/adaptive":
                turn = ui.user(line)
                def run_adaptive(command_text=line, turn_id=turn):
                    try:
                        import room_adaptive
                        result = room_adaptive.command(room, project, command_text,
                            submit=lambda text: {"status": "submitted", "prompt": session.submit(text, post=ui.printer(turn_id))})
                        ui.note(json.dumps(result, ensure_ascii=False), turn_id)
                    except (registry.RegistryError, OSError, ValueError) as exc:
                        ui.note(f"Adaptive command stopped: {exc}", turn_id)
                planning_executor.submit(run_adaptive)
                continue
            if room_commands.token(line) in {"/plan", "/decide"}:
                turn = ui.user(line)
                event_id = _planning_event_id(project, line)
                def run_planning(command_text=line, event=event_id, turn_id=turn):
                    try:
                        result = room_planning.command(
                            room, project, command_text, event_id=event,
                            provenance="Orca interactive room input; local event ID")
                        ui.note(f"{result.get('plan_id','planning')}@{result.get('version','')} "
                                f"· {result['stage']} · next {result.get('next_actor','')}\n"
                                f"{result.get('reader_link') or result.get('reader_path') or ''}", turn_id)
                    except (registry.RegistryError, OSError, ValueError) as exc:
                        ui.note(f"Planning command stopped: {exc}", turn_id)
                planning_executor.submit(run_planning)
                continue
            if line == "/trace":
                status = session.trace_status()
                if status is None:
                    ui.note("Research trace is not enabled for this context.")
                elif status.ok:
                    ui.note(f"Research trace verified · {status.entries} entries · "
                            f"head {status.head_hash[:16]}…\n"
                            f"Report: {research_trace.report_path(project)}")
                else:
                    ui.note("Research trace integrity warning: " + "; ".join(status.errors))
                continue
            if line == "/help":
                ui.note(room_identity.banner()+"\n"+room_commands.help_text('ordinary', room))
                continue
            turn = ui.user(line)
            try:
                session.submit(line, post=ui.printer(turn))
                ui.note("Accepted. You may type the next prompt now.", turn)
            except registry.RegistryError as exc:
                ui.note(f"Not accepted: {exc}", turn)
    except KeyboardInterrupt:
        ui.note("Closing after active work finishes. Use Orca status if you close this terminal.")
    finally:
        planning_executor.shutdown(wait=True, cancel_futures=False)
        try:
            report = session.close(wait=True)
            if report is not None:
                ui.note(f"Research trace rendered: {report}")
        except (OSError, research_trace.TraceError) as exc:
            ui.note(f"Research trace could not be rendered: {exc}")
    return 0

@room_ui.surface
def _run_live_project_room(room: rooms.Room, project: project_rooms.ProjectContext,
                           *, ui) -> int:

    ui.note(room_identity.banner())
    session = live_room.LiveRoomSession(room, project)
    adaptive_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-adaptive")
    ui.block(f"{project.title} · {room.title}", f"Context: {project.note}\n"
             f"Transcript: {session.transcript_path}", role="room")
    ui.note("Prewarming the live seats. The discussion has not started.")

    try:
        session.prewarm(
            on_ready=lambda seat: ui.note(
                f"  READY  @{seat.name:8} {seat.model} {seat.route.effort}"))
        ui.note("READY. Address one or more seats, or @all, at the start of each prompt.")
        ui.note("Enter sends · Esc then Enter adds a line · /status · /sources · /help · /exit")
        while True:
            try:
                line = ui.read()
            except EOFError:
                line = "/exit"
            if not line:
                continue
            stopped = room_commands.refusal(line, 'live', room)
            if stopped:
                ui.note(stopped)
                continue
            if line in {"/exit", "/quit"}:
                break
            if line == "/status":
                for seat in session.sessions.values():
                    ui.note(f"@{seat.name:8} retained · {seat.model} {seat.route.effort}")
                ui.note(f"Transcript: {session.transcript_path}")
                continue
            if line == "/sources":
                for source in session.sources:
                    ui.note(f"{source.path} · sha256 {source.sha256}")
                continue
            if line == "/help":
                ui.note(room_identity.banner()+"\n"+room_commands.help_text('live', room))
                continue

            if room_commands.token(line) == '/adaptive':
                turn = ui.user(line)
                def run_live_adaptive(command_text=line,turn_id=turn):
                    try:
                        import room_adaptive
                        result=room_adaptive.command(room,project,command_text,
                            submit=lambda text: room_adaptive.submit_context(room,project,text,sender=_sender(),post=ui.printer(turn_id)))
                        if 'lifecycle' not in result: ui.note(json.dumps(result,ensure_ascii=False),turn_id)
                    except (registry.RegistryError,OSError,ValueError) as exc:
                        ui.note('Adaptive command stopped: '+str(exc),turn_id)
                adaptive_executor.submit(run_live_adaptive)
                continue

            turn = ui.user(line)

            def show_result(result):
                label = f"@{result.seat} · RESPONSE"
                failed = isinstance(result.outcome, str) and result.outcome != "succeeded"
                if failed:
                    label += f" · {result.outcome}"
                ui.block(label, result.body, role="error" if failed else "agent", turn=turn)

            def answer_question(seat, question):
                ui.block(f"@{seat} · QUESTION FOR YOU", question, role="question", turn=turn)
                answer = ui.read()
                ui.user(answer, reply_to=seat, turn=turn)
                return answer

            try:
                session.run_turn(
                    line, on_result=show_result,
                    question_handler=answer_question)
            except registry.RegistryError as exc:
                ui.note(f"Not accepted: {exc}", turn)
    except (KeyboardInterrupt, EOFError):
        ui.note("Closing the live room.")
    finally:
        adaptive_executor.shutdown(wait=True, cancel_futures=False)
        unresolved = session.close()
        if unresolved:
            ui.note("Worker cleanup needs inspection: " + "; ".join(unresolved))
    return 0

def _orca_json(arguments: list[str], *, timeout: int = 30) -> dict:
    proc = subprocess.run(["orca", *arguments, "--json"], capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise registry.RegistryError(
            f"Orca command failed: {(proc.stderr or proc.stdout).strip()}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise registry.RegistryError("Orca returned invalid JSON") from exc
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise registry.RegistryError(
            f"Orca refused the Leo room: {error.get('message') or error.get('code') or error}")
    return payload

def _leo_terminal_state(terminal):

    if terminal.get('executionHostId', 'local') != 'local':
        return 'unknown'
    handle = terminal.get('handle')
    if not handle or terminal.get('agentIdentity'):
        return 'unknown'
    try:
        process = subprocess.run(['ps', 'axeww', '-o', 'pid=,ppid=,tty=,command='],
                                 capture_output=True, text=True, timeout=3)
        if process.returncode:
            return 'unknown'

        rows = []
        for line in process.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) != 4:
                continue
            rows.append(parts)
        if not rows:
            return 'unknown'
        children = [row for row in rows if re.search(
            r'(?:^| )ORCA_TERMINAL_HANDLE='+re.escape(handle)+r'(?: |$)', row[3])]
        ttys = {row[2] for row in children if row[2] not in {'?', '??'}}
        related = [row for row in rows if row in children or row[2] in ttys]
        for row in related:
            command = re.split(r' [A-Za-z_][A-Za-z0-9_]*=', row[3], maxsplit=1)[0]
            argv = shlex.split(command)
            if (argv and Path(argv[0]).name.lower().startswith('python')
                    and any(Path(arg).name == 'switchboard.py'
                            and Path(arg).parent.name == 'praxis-switchboard' for arg in argv)
                    and ('_leo-room-run' in argv
                         or ('leo' in argv and '--foreground' in argv))):
                entry = next(arg for arg in argv if Path(arg).name == 'switchboard.py'
                             and Path(arg).parent.name == 'praxis-switchboard')
                return 'room' if entry == str(Path(__file__).resolve()) else 'stale-room'
        if children:
            return 'unknown'
        readback = _orca_json(['terminal', 'read', '--terminal', handle, '--screen'])
        screen = (readback.get('result') or {}).get('terminal') or {}
        if screen.get('source') != 'screen' or screen.get('draft'):
            return 'unknown'
        tail = [line.rstrip() for line in screen.get('tail', []) if line.strip()]
        if tail and re.fullmatch(r'[^\s]+@[^\s]+ [^%$#>]+ [%$#>]', tail[-1]):
            return 'idle-shell'
    except (OSError, ValueError, subprocess.SubprocessError, registry.RegistryError):
        pass
    return 'unknown'

def cmd_leo(args) -> int:

    print(room_identity.banner())
    if args.foreground:
        return _run_leo_room(plain=args.plain)

    repo_selector = f"path:{registry.vault_root()}"
    listed = _orca_json(["worktree", "list", "--repo", repo_selector])
    worktrees = (listed.get("result") or {}).get("worktrees") or []
    matches = [item for item in worktrees if item.get("displayName") == "Leo"]
    if len(matches) > 1:
        raise registry.RegistryError(
            "more than one Orca workspace is named Leo; rename the duplicate before opening")

    startup_handle = None
    created_workspace = False
    if matches:
        workspace_id = str(matches[0].get("id") or "")
    else:
        created_workspace = True
        created = _orca_json([
            "worktree", "create", "--repo", repo_selector,
            "--name", "Leo", "--no-parent", "--activate",
        ])
        result = created.get("result") or {}
        workspace = result.get("worktree") or {}
        workspace_id = str(workspace.get("id") or "")
        startup = result.get("startupTerminal") or {}
        startup_handle = startup.get("handle")
    if not workspace_id:
        raise registry.RegistryError("Orca did not return a workspace id for Leo")

    workspace_selector = f"id:{workspace_id}"
    terminal_list = _orca_json([
        "terminal", "list", "--worktree", workspace_selector,
    ])
    terminals = (terminal_list.get("result") or {}).get("terminals") or []
    fallback_handles = [
        str(item.get("handle")) for item in terminals
        if created_workspace
        and item.get("title") == "Terminal 1"
        and not item.get("agentIdentity")
        and item.get("handle")
    ]
    candidates = [item for item in terminals
                  if item.get("title") == "Leo Room" and item.get("connected")]
    states = [(item, _leo_terminal_state(item)) for item in candidates]
    for item, state in states:
        if state != 'room':
            print(f"Leaving old Leo Room {item.get('handle')} untouched: {state}.")
    active = [item for item, state in states if state == 'room']
    if len(active) > 1:
        raise registry.RegistryError(
            "more than one live Leo Room terminal exists; close the duplicate before opening")
    if active:
        handle = str(active[0].get("handle") or "")
        _orca_json(["terminal", "switch", "--terminal", handle])
        print("Opened Leo. The existing room is in focus; no worker was launched.")
        return 0

    command = [sys.executable, str(Path(__file__).resolve()),
               *(["--plain"] if args.plain else []), "_leo-room-run"]
    opened = _orca_json([
        "terminal", "create", "--worktree", workspace_selector,
        "--title", "Leo Room", "--command", shlex.join(command), "--focus",
    ])
    terminal = (opened.get("result") or {}).get("terminal") or {}
    handle = terminal.get("handle") or terminal.get("id") or "unknown"
    if startup_handle and startup_handle != handle:
        fallback_handles.append(str(startup_handle))
    for fallback_handle in dict.fromkeys(fallback_handles):
        if fallback_handle == handle:
            continue
        try:
            _orca_json(["terminal", "close", "--terminal", fallback_handle])
        except registry.RegistryError:
            pass
    print("Opened Leo. Nothing has been dispatched.")
    print(f"  workspace: Leo")
    print(f"  terminal: {handle}")
    print("  type normally for Leo; begin with @Name for a specialist")
    return 0

def cmd_room(args) -> int:

    print(room_identity.banner())
    room = project_rooms.resolve_roster(args.roster)
    project = project_rooms.resolve_room_context(
        " ".join(args.project).strip(), room)

    if args.foreground:
        runner = _run_live_project_room if room.live else _run_project_room
        return runner(room, project, plain=args.plain)
    subcommand = "_live-room-run" if room.live else "_room-run"
    command = [sys.executable, str(Path(__file__).resolve()),
               *(["--plain"] if args.plain else []),
               subcommand, room.room_id, str(project.note), project.kind]
    title = f"Room: {project.title}"

    worktree = "current"
    proc = subprocess.run(
        ["orca", "terminal", "create", "--worktree", worktree,
         "--title", title, "--command", shlex.join(command), "--focus", "--json"],
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise registry.RegistryError(
            f"could not open the project room: {(proc.stderr or proc.stdout).strip()}")
    payload = json.loads(proc.stdout)
    if not payload.get("ok"):
        raise registry.RegistryError(f"Orca refused the project room: {payload.get('error')}")
    terminal = (payload.get("result") or {}).get("terminal") or {}
    handle = terminal.get("handle") or terminal.get("id") or "unknown"
    if room.live:
        print(f"Opened {project.title}. The live seats are prewarming now.")
    else:
        print(f"Opened {project.title}. Nothing has been dispatched.")
    print(f"  terminal: {handle}")
    print(f"  roster: {room.title} — {', '.join(room.recipients)}")
    print(f"  outputs: {project_rooms.output_root(project)}")
    if room.live:
        print("  wait for READY in the room terminal before the first prompt")
    else:
        print("  type prompts in the room; @all addresses the full roster")
    return 0

def cmd_room_run(args) -> int:
    room = rooms.resolve(args.roster)
    note = Path(args.project_note).resolve()
    if not note.is_file() or registry.vault_root() not in note.parents:
        raise registry.RegistryError(f"invalid project note: {note}")
    return _run_project_room(room, project_rooms.ProjectContext(
        _room_title(note), note, args.context_kind),
                             plain=args.plain)

def cmd_room_command(args) -> int:

    room = rooms.resolve(args.roster)
    project = project_rooms.resolve_room_context(args.context, room)
    line = " ".join(args.text)
    surface = 'room-command'
    stopped = room_commands.refusal(line, surface, room)
    if stopped:
        raise registry.RegistryError(stopped)
    if room_commands.token(line) == '/help':
        print(room_identity.banner()+'\n'+room_commands.help_text(surface, room))
        return 0
    if room_commands.token(line) not in {'/adaptive', '/plan', '/decide'}:
        raise registry.RegistryError('room-command requires a local command; nothing was dispatched')
    if room_commands.token(line) == "/adaptive":
        import room_adaptive
        def submit(text):
            return room_adaptive.submit_context(room,project,text,sender=_sender(),post=lambda event:None,event_id=args.event_id)
        result = room_adaptive.command(room, project, line, submit=submit)
    else:
        result = room_planning.command(room, project, line,
                                      event_id=args.event_id,
                                      provenance=f"local CLI invocation; user event asserted: {args.user_event}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

def cmd_live_room_run(args) -> int:
    room = rooms.resolve(args.roster)
    if not room.live:
        raise registry.RegistryError(f"{room.title} is not configured as a live room")
    note = Path(args.project_note).resolve()
    if not note.is_file() or registry.vault_root() not in note.parents:
        raise registry.RegistryError(f"invalid project note: {note}")
    return _run_live_project_room(room, project_rooms.ProjectContext(
        _room_title(note), note, args.context_kind), plain=args.plain)

def _room_title(note: Path) -> str:
                                                                                   
    return project_rooms._title(note)

def cmd_convene_run(args) -> int:
    print(room_identity.banner())
    room = rooms.resolve(args.room)
    agenda = args.agenda.strip()
    if room_commands.token(agenda) == '/help':
        print(room_commands.help_text('convene'))
        return 0
    if room_commands.token(agenda) == '/adaptive':
        import room_adaptive
        project=project_rooms.ProjectContext(room.title,room.record,'room')
        result=room_adaptive.command(room,project,agenda,submit=lambda text: room_adaptive.submit_context(
            room,project,text,sender=_sender(),post=transport_local.make_printer(args.plain,False)))
        if 'lifecycle' not in result: print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('lifecycle','completed') in {'completed','awaiting_user'} else 1
    text, _ = _room_preflight(room, agenda)
    board = Switchboard(check_account=True)
    ui = room_ui.RoomUI(plain=args.plain)
    ui.block(room.title, "One discussion request.", role="room")
    turn = ui.user(agenda)
    record = board.handle(text, _sender(),
                          ui.printer(turn),
                          room_id=room.room_id, room_agenda=agenda)
    return 0 if record.get("lifecycle") in ("completed", "awaiting_user") else 1

def cmd_rooms(args) -> int:
    for room in rooms.list_rooms():
        aliases = ", ".join(room.aliases) or "none"
        print(f"  {room.room_id:18} aliases={aliases}  "
              f"chair={room.chair:8} seats={', '.join(room.seats)}")
    return 0

def cmd_seats(args) -> int:
    people = registry.seats()
    account = registry.worker_account()
    kind = account.get("kind")
    print(f"account: {kind or 'unproven'}"
          f"{'' if kind else ' — ' + str(account.get('unproven'))}\n")
    for name in sorted(people):
        seat = people[name]
        pin = ""
        try:
            route = registry.resolve(seat, log=False)
            blocked = registry.account_blocked(route, account)
            mark = "UNAVAILABLE" if blocked else f"{route.effective_model} {route.effort}"

            pin = " (pinned)" if route.pinned else ""
        except registry.RouteRefused as exc:
            mark = f"REFUSED — {str(exc)[:60]}"
        aliases = "" if not seat.aliases else " (also " + ", ".join("@" + a for a in seat.aliases) + ")"
        print(f"  @{name:9} {mark:34}{pin}  {seat.title[:40]}{aliases}")
        for entry in seat.task_routes:
            mode = entry["mode"]
            task_seat = seat.for_task_mode(mode)
            try:
                task_route = registry.resolve(task_seat, log=False)
                blocked = registry.account_blocked(task_route, account)
                mark = "UNAVAILABLE" if blocked else f"{task_route.effective_model} {task_route.effort}"
                pin = " (pinned)" if task_route.pinned else ""
            except registry.RouteRefused as exc:
                mark, pin = f"REFUSED — {str(exc)[:60]}", ""
            print(f"    [{mode}] {mark}{pin}  route={task_seat.route}")
    print("\n  unaddressed requests go to @Leo")
    return 0

def cmd_status(args) -> int:
    st = SwitchboardState(default_state_path())
    reqs = st.data.get("requests", {})
    if args.request_id:
        rec = reqs.get(args.request_id)
        if not rec:
            print(f"no such request: {args.request_id}", file=sys.stderr)
            return 2
        print(json.dumps(rec, indent=2))
        return 0
    if not reqs:
        print("no requests yet")
        return 0
    for rid, rec in sorted(reqs.items(), key=lambda kv: kv[1].get("created_at", ""))[-15:]:
        tasks = rec.get("tasks") or {}
        detail = ", ".join(f"{s}:{t.get('state')}" for s, t in tasks.items()) or "—"
        print(f"  {rid}  {rec.get('lifecycle','?'):18} {','.join(rec.get('recipients') or []):24} {detail}")
    return 0

def cmd_recover(args) -> int:
    board = Switchboard(check_account=False)
    recover = board.reconcile_continuation if getattr(args, 'continuation_no_launch', False) else board.recover
    record = recover(
        args.request_id, transport_local.make_printer(args.plain, args.quiet))
    return 0 if record.get("lifecycle") in {"completed", "pending", "awaiting_user"} else 1

def cmd_reply(args) -> int:
    answer = " ".join(args.answer).strip()
    if not answer:
        print("an answer is required", file=sys.stderr)
        return 2
    board = Switchboard(check_account=True)
    record = board.reply(args.request_id, args.seat, answer,
                         transport_local.make_printer(args.plain, args.quiet))
    return 0 if record.get("lifecycle") in ("completed", "dispatched",
                                             "awaiting_user") else 1

def cmd_verify(args) -> int:

    print(room_identity.banner())
    ok = True

    def check(label, fn):
        nonlocal ok
        try:
            detail = fn()
            print(f"  ok    {label:34} {detail}")
        except Exception as exc:                                                         
            ok = False
            print(f"  FAIL  {label:34} {exc}")

    check("vault", lambda: registry.vault_root())
    check("role registry", lambda: f"{len(registry.seats())} addressable seats")
    check("all role skill inheritance", _verify_skill_inheritance)
    check("router", lambda: registry.router_path())
    check("shared dispatcher", lambda: _require(registry.skill_root() / "orca-dispatch" / "dispatcher.py"))
    check("orca account", lambda: registry.worker_account().get("kind")
          or "unproven — no model entitled to a specific account is selectable")
    check("state file", lambda: default_state_path())
    check("context loaders deployed", _verify_context_loaders)
    check("recipient parsing", lambda: parse_recipients("@Donna do X", registry.seats())[0])
    check("default recipient", lambda: parse_recipients("no mention here", registry.seats())[0])
    print("\n  nothing was dispatched" + ("" if ok else "  — fix the failures above"))
    return 0 if ok else 1

def _require(path: Path) -> Path:
    if not Path(path).exists():
        raise FileNotFoundError(path)
    return path

def _verify_context_loaders() -> str:

    claude = registry.agent_skill_root("claude")
    codex = registry.agent_skill_root("codex")
    mapped, exempt = set(), set()

    for kind, _root in project_rooms.context_roots():
        name = project_rooms.loader_name(kind)
        if name is None:
            exempt.add(kind)
            continue
        left, right = claude / name / "SKILL.md", codex / name / "SKILL.md"
        for path in (left, right):
            if not path.is_file():
                raise FileNotFoundError(f"{kind} loader not deployed: {path}")
        if left.read_bytes() != right.read_bytes():
            raise registry.RegistryError(
                f"the {kind} loader drifted between providers: {name}")
        mapped.add(kind)
    note = f"; no loader declared for {', '.join(sorted(exempt))}" if exempt else ""
    return f"{len(mapped)} context kinds resolve byte-identically on both roots{note}"

def _verify_skill_inheritance() -> str:
    count = 0
    names = set()
    for seat in registry.seats().values():
        route = registry.resolve(seat, log=False)
        count += len(registry.inherited_skill_paths(route))
        names.update(seat.inherited_skills)
    claude = registry.agent_skill_root("claude")
    codex = registry.agent_skill_root("codex")
    for name in names:
        left_root = claude / name
        right_root = codex / name
        material = lambda root: {                                          
            path.relative_to(root): path for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        }
        left = material(left_root)
        right = material(right_root)
        if not left or not right:
            raise FileNotFoundError(f"provider deployment missing for {name}")
        if set(left) != set(right):
            missing = sorted(str(p) for p in set(left) ^ set(right))
            raise registry.RegistryError(
                f"Claude/Codex deployed resource sets drifted for {name}: {missing}")
        for relative in left:
            if left[relative].read_bytes() != right[relative].read_bytes():
                raise registry.RegistryError(
                    f"Claude/Codex deployed resources drifted for {name}/{relative}")
    return (f"{count} role assignments; {len(names)} unique skills present "
            "with byte-identical deployed resources")

def cmd_selftest(args) -> int:
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover(str(Path(__file__).resolve().parent), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1

SUBCOMMANDS = {"version", "send", "seats", "rooms", "leo", "_leo-room-run",
               "room", "_room-run", "_live-room-run",
               "room-command",
               "convene", "_convene-run",
               "status", "reply", "recover", "verify", "selftest"}

def _normalise(argv: list) -> list:

    flags_taking_no_value = {"--plain", "--quiet", "-h", "--help"}
    i = 0
    while i < len(argv) and argv[i] in flags_taking_no_value:
        i += 1

    if (i < len(argv) and argv[i].casefold() == "leo"
            and all(flag in {"--foreground", "--help", "-h"} for flag in argv[i + 1:])):
        return [*argv[:i], "leo", *argv[i + 1:]]

    if (i + 1 < len(argv)
            and argv[i].casefold() in rooms.RETIRED_WRITING_ALIASES
            and not argv[i + 1].startswith("-")):
        return [*argv[:i], "room", *argv[i + 1:], "--roster", argv[i]]
    if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
        try:
            rooms.resolve(argv[i])
        except registry.RegistryError:
            pass
        else:
            return [*argv[:i], "room", *argv[i + 1:], "--roster", argv[i]]
    if i < len(argv) and argv[i] not in SUBCOMMANDS and not argv[i].startswith("-"):
        return [*argv[:i], "send", *argv[i:]]
    return argv

def main() -> int:
    ap = argparse.ArgumentParser(prog="switchboard", description=__doc__.split("\n")[0])
    ap.add_argument("--plain", action="store_true", help="no colour")
    ap.add_argument("--quiet", action="store_true", help="results only")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("send", help="address one or more agents (default)")
    p.add_argument("text", nargs="+")

    p.add_argument("--context", help=argparse.SUPPRESS)
    p.add_argument("--model", metavar="MODEL_ID",
                   help="explicit registered model for this request only")
    p.add_argument("--mode", metavar="TASK_MODE",
                   help="declared task mode for each addressed role (see seats)")
    p.add_argument("--no-account-check", action="store_true")

    sub.add_parser("seats", help="who is addressable, on what model")
    sub.add_parser("rooms", help="standing multi-agent room rosters")
    p = sub.add_parser("leo", help="open the global Leo room")
    p.add_argument("--foreground", action="store_true",
                   help="run in this shell (tests/debugging only)")
    sub.add_parser("_leo-room-run", help=argparse.SUPPRESS)
    p = sub.add_parser("room", help="open an interactive context room")
    p.add_argument("project", nargs="+")
    p.add_argument("--roster", help="room roster alias; inferred when only one is active")
    p.add_argument("--foreground", action="store_true",
                   help="run in this shell (tests/debugging only)")
    p = sub.add_parser("_room-run", help=argparse.SUPPRESS)
    p.add_argument("roster")
    p.add_argument("project_note")
    p.add_argument("context_kind", nargs="?", default="project")
    p = sub.add_parser("_live-room-run", help=argparse.SUPPRESS)
    p.add_argument("roster")
    p.add_argument("project_note")
    p.add_argument("context_kind", nargs="?", default="project")
    p = sub.add_parser("room-command", help="one explicit command in a resolved pilot room")
    p.add_argument("--roster", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--event-id", required=True, help="stable event identity for replay")
    p.add_argument("--user-event", required=True, help="literal source reference for this user request or decision; caller assertion, not authentication")
    p.add_argument("text", nargs="+")
    p = sub.add_parser("convene", help="alias for `room`: open a context room")
    p.add_argument("project", nargs="+")
    p.add_argument("--roster", help="room roster alias; inferred when only one is active")
    p.add_argument("--foreground", action="store_true",
                   help="run in this shell (tests/debugging only)")
    p = sub.add_parser("_convene-run", help=argparse.SUPPRESS)
    p.add_argument("room")
    p.add_argument("agenda")
    p = sub.add_parser("status", help="live request and task states")
    p.add_argument("request_id", nargs="?")
    p = sub.add_parser("reply", help="answer a waiting agent question")
    p.add_argument("request_id")
    p.add_argument("seat")
    p.add_argument("answer", nargs="+")
    p = sub.add_parser("version", help="show running host and source")
    p.add_argument("--json", action="store_true")
    sub.add_parser("verify", help="prove the path is wired without dispatching")
    p = sub.add_parser("recover", help="reconcile exact original late completions without dispatch")
    p.add_argument("request_id")
    p.add_argument("--continuation-no-launch", action="store_true",
                   help="owner reconciliation of an accepted held chair with proven blocked Leo; no dispatch")
    p.add_argument("--plain", action="store_true")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("selftest", help="run the credential-free test suite")
    p.add_argument("--verbose", action="store_true")

    args = ap.parse_args(_normalise(sys.argv[1:]))
    if not args.command:
        ap.print_help()
        return 0

    handlers = {"send": cmd_send, "seats": cmd_seats, "rooms": cmd_rooms,
                "leo": cmd_leo,
                "_leo-room-run": lambda args: _run_leo_room(plain=args.plain),
                "room": cmd_room, "_room-run": cmd_room_run,
                "room-command": cmd_room_command,
                "_live-room-run": cmd_live_room_run,
                "convene": cmd_room, "_convene-run": cmd_convene_run,
                "status": cmd_status, "reply": cmd_reply,
                "version": lambda a: (print(json.dumps(room_identity.identity()) if a.json else room_identity.banner()) or 0),
                "verify": cmd_verify, "recover": cmd_recover, "selftest": cmd_selftest}
    try:
        launching_room = (args.command in {"leo", "room", "convene"}
                          and not getattr(args, "foreground", False))
        interactive_room = (args.command in {"leo", "room", "convene", "_leo-room-run",
                                            "_room-run", "_live-room-run"}
                            and sys.stdin.isatty() and sys.stdout.isatty())
        if launching_room or interactive_room:
            room_runtime.ensure_interactive_runtime()
        return handlers[args.command](args)
    except (registry.RegistryError, RuntimeError) as exc:
        print(f"switchboard: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
