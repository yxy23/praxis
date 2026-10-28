
from __future__ import annotations

from contextlib import nullcontext
from functools import wraps
import re
import shutil
import sys
import threading
import unicodedata

import transport_local

OSC_LINK = re.compile(r"\x1b\]8;;([^\x1b\x07]*)\x1b\\")
SGR = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\x1b[0m"
PALETTE = {"you": "\x1b[1;36m", "agent": "\x1b[1;32m",
           "question": "\x1b[1;33m", "error": "\x1b[1;31m", "room": "\x1b[2m"}

def _clusters(text):

    cluster = ""
    for char in text:
        attached = (unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me"}
                    or char == "\u200d" or cluster.endswith("\u200d")
                    or "\U0001f3fb" <= char <= "\U0001f3ff"
                    or (len(cluster) == 1 and "\U0001f1e6" <= cluster <= "\U0001f1ff"
                        and "\U0001f1e6" <= char <= "\U0001f1ff"))
        if cluster and not attached:
            yield cluster
            cluster = ""
        cluster += char
    if cluster:
        yield cluster

def cell_width(text):
    try:
        from wcwidth import wcswidth
        return max(0, wcswidth(text))
    except ImportError:                                              
        return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in {"Mn", "Me", "Cf"}
                   else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
                   for c in text)

def _tokens(text):

    target = None
    pos = 0
    while pos < len(text):
        match = OSC_LINK.match(text, pos)
        if match:
            target = match.group(1) or None
            pos = match.end()
            continue
        match = SGR.match(text, pos)
        if match:
            pos = match.end()
            continue
        end = text.find("\x1b", pos + 1)
        end = len(text) if end < 0 else end
        chunk = text[pos:end]
        chunk = "".join(c for c in chunk if ord(c) >= 32 and not 127 <= ord(c) < 160)
        for cluster in _clusters(chunk):
            yield cluster, target
        pos = end

def _linked(tokens):
    result, active = [], None
    for char, target in tokens:
        if target != active:
            if active:
                result.append("\x1b]8;;\x1b\\")
            if target:
                result.append(f"\x1b]8;;{target}\x1b\\")
            active = target
        result.append(char)
    if active:
        result.append("\x1b]8;;\x1b\\")
    return "".join(result)

def wrap_text(text: str, width: int) -> list[str]:

    width = max(4, width)
    lines = []
    fenced = False
    for source in text.expandtabs(4).split("\n"):
        tokens = list(_tokens(source))
        visible = "".join(t[0] for t in tokens)
        if visible.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        match = re.match(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?", visible)
        indent = min(cell_width(match.group()), width // 3)
        continuation = [(" ", None)] * indent
        if not tokens:
            lines.append("")
        while tokens:
            cells, end = 0, 0
            for char, _ in tokens:
                size = cell_width(char)
                if cells + size > width:
                    break
                cells += size
                end += 1
            end = max(1, end)
            if end >= len(tokens):
                lines.append(_linked(tokens))
                break
                                                                               
            spaces = [i for i in range(indent + 1, end)
                      if tokens[i][0].isspace()]
            if spaces and not fenced:
                end = spaces[-1]
            lines.append(_linked(tokens[:end]))
            tokens = tokens[end:]
            if not fenced:
                while tokens and tokens[0][0].isspace():
                    tokens.pop(0)
            tokens = continuation + tokens if tokens else []
    return lines

def create_editor(*, plain=False, **kwargs):

    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    keys = KeyBindings()

    @keys.add("enter")
    def submit(event):
        event.current_buffer.validate_and_handle()

    @keys.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    return PromptSession(
        multiline=True, wrap_lines=True, key_bindings=keys,
        prompt_continuation=lambda width, line, soft: " " * max(0, width - 2) + "· ",
        erase_when_done=True, reserve_space_for_menu=0,
        style=Style.from_dict({"prompt": "" if plain else "ansicyan bold"}),
        **kwargs)

def surface(run):

    @wraps(run)
    def wrapped(*args, plain=False, **kwargs):
        with RoomUI(plain=plain) as ui:
            return run(*args, ui=ui, **kwargs)
    return wrapped

class RoomUI:
    def __init__(self, *, plain=False, width=None):
        self.plain, self.fixed_width = plain, width
        self.turn = 0
        self.lock = threading.RLock()
        self.editor = None
        self.output_context = nullcontext()

    def __enter__(self):
        if sys.stdin.isatty() and sys.stdout.isatty():
            from prompt_toolkit.patch_stdout import patch_stdout
            self.editor = create_editor(plain=self.plain)
                                                                                  
            self.output_context = patch_stdout(raw=True)
        self.output_context.__enter__()
        return self

    def __exit__(self, *exc):
        return self.output_context.__exit__(*exc)

    def read(self, label="YOU"):
        if self.editor is None:
            return input(f"\n{label} › ").strip()
        return self.editor.prompt([("class:prompt", f"{label} › ")]).strip()

    def width(self):
        return max(8, min(100, self.fixed_width or shutil.get_terminal_size((80, 24)).columns - 2))

    def _color(self, role, text):
        if self.plain or not sys.stdout.isatty():
            return text
        return PALETTE[role] + text + RESET

    def block(self, label, body, *, role="agent", turn=None, artifacts=()):
        width = self.width()
        rail = "┃" if role == "you" else "│"
        title = f"{turn:02d} · {label}" if turn is not None else label
        rule = "━" if role == "you" else "─"
                                                                             
        header = wrap_text(title, width - 4)
        rendered = ["", *(self._color(role, f" {rule}{rule} {line}") for line in header)]
        text = transport_local.render_terminal_links(str(body))
        if artifacts:
            text += "\n\n" + "\n".join("→ " + transport_local.render_terminal_artifact(str(p))
                                        for p in artifacts)
                                                                                
        content = wrap_text(text, width - 3) if sys.stdout.isatty() else text.split("\n")
        rendered.extend(f" {self._color(role, rail)} {line}" for line in content)
        rendered.append("")
        with self.lock:
            print("\n".join(rendered), flush=True)

    def user(self, text, *, reply_to=None, turn=None):
        if turn is None:
            self.turn += 1
            turn = self.turn
        label = "YOU" + (f" · REPLY TO @{reply_to}" if reply_to else "")
        self.block(label, text, role="you", turn=turn)
        return turn

    def note(self, text, turn=None):
        text = str(text).strip()
        if not text:
            return
        label = f"{turn:02d} · " if turn is not None else "ROOM · "
        text = transport_local.render_terminal_links(label + text)
        lines = wrap_text(text, self.width() - 2) if sys.stdout.isatty() else text.split("\n")
        with self.lock:
            print(self._color("room", "\n".join("  " + line for line in lines)), flush=True)

    def printer(self, turn):
        ordinary = transport_local.make_printer(plain=True, write=lambda text: self.note(text, turn))

        def post(event):
            kind = event.get("type")
            if kind == "result":
                state = event.get("state")
                label = f"@{event['seat']} · RESPONSE"
                body = event.get("summary") or ""
                if state != "succeeded":
                    label += f" · {state or 'state unknown'}"
                    body += "\n" + "; ".join(event.get("contract_reasons") or [])
                self.block(label, body, role="agent" if state == "succeeded" else "error",
                           turn=turn, artifacts=event.get("artifacts") or ())
            elif kind == "question":
                self.block(f"@{event['seat']} · QUESTION FOR YOU", event.get("message") or "",
                           role="question", turn=turn)
            else:
                ordinary(event)
        return post
