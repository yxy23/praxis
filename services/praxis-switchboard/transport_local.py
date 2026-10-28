
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import artifact_links

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
COLOR = {"ack": "\033[36m", "started": "\033[2m", "result": "\033[32m",
         "failed": "\033[31m", "refused": "\033[31m", "unavailable": "\033[33m",
         "complete": "\033[1m", "duplicate": "\033[33m", "recovered": "\033[33m",
         "room_incomplete": "\033[31m", "room_recorded": "\033[32m",
         "room_prompt_recorded": "\033[32m", "room_trace_recorded": "\033[32m",
         "room_trace_failed": "\033[31m"}

MARKDOWN_LINK = re.compile(
    r"\[((?:\\.|[^\]\\\n])+)\]\((?:<([^<>\r\n]+)>|([^\s()<>]+))\)"
)

LINK_SPAN = re.compile(
    r"\x1b\]8;;[^\x1b\x07]*\x1b\\.*?\x1b\]8;;\x1b\\|" + MARKDOWN_LINK.pattern,
    re.DOTALL,
)
FILE_START = re.compile(r"(?<![^\s('`\"<])(?:~/(?=\S)|/(?![/\s]))")
FILE_END = re.compile(r"\.[a-zA-Z0-9]{1,12}(?=$|[\s`'\")\]}>.,;:!?])")

def _terminal_link_target(target: str) -> Optional[str]:

    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in target):
        return None
    try:
        parsed = urlsplit(target)
        if parsed.scheme.lower() in {"http", "https"}:
            if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or any(c.isspace() for c in target)):
                return None
            return target
        if parsed.scheme.lower() == "file" and parsed.netloc in {"", "localhost"}:
            if parsed.query or parsed.fragment:
                return None
            target = unquote(parsed.path)
        elif parsed.scheme:
            return None
        if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in target):
            return None
        path = Path.home() / target[2:] if target.startswith("~/") else Path(target)
        if not path.is_absolute() or not path.is_file():
            return None
        return artifact_links.delivery_path(path).as_uri()
    except (OSError, RuntimeError, ValueError):
        return None

def _terminal_hyperlink(label: str, target: str) -> str:
    return f"\033]8;;{target}\033\\{label}\033]8;;\033\\"

def _display_link(label: str, target: str) -> str:
    if sys.stdout.isatty():
        return _terminal_hyperlink(label, target)

    if target.startswith("file:"):
        return artifact_links.artifact_link(unquote(urlsplit(target).path), label)
    escaped = re.sub(r"([\\`*_{}\[\]()<>!])", r"\\\1", label)
    return f"[{escaped}](<{target}>)"

def _link_file_mentions(text: str) -> str:

    result, cursor = [], 0
    for start in FILE_START.finditer(text):
        if start.start() < cursor:
            continue
        line_end = text.find("\n", start.start())
        end = min(line_end if line_end >= 0 else len(text), start.start() + 4096)
        candidate = text[start.start():end]
        boundaries = list(FILE_END.finditer(candidate))[:32]
        for boundary in reversed(boundaries):
            label = candidate[:boundary.end()]
            target = _terminal_link_target(label)
            if target is not None:
                begin, finish = start.start(), start.start() + boundary.end()

                if (not sys.stdout.isatty() and begin > cursor and text[begin - 1] == "`"
                        and finish < len(text) and text[finish] == "`"):
                    begin -= 1
                    finish += 1
                result.extend((text[cursor:begin], _display_link(label, target)))
                cursor = finish
                break
    result.append(text[cursor:])
    return "".join(result)

def render_terminal_links(text: str) -> str:

    def link(match: re.Match) -> str:
        label, angled, bare = match.groups()
        target = angled or bare
        if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in label + target):
            return match.group(0)
        target = _terminal_link_target(target)
        if target is None:
            return match.group(0)
        label = re.sub(r"\\([\\`*_{}\[\]()<>!])", r"\1", label)
        if not sys.stdout.isatty() and not target.startswith("file:"):
            return match.group(0)
        return _display_link(label, target)

    result, cursor = [], 0
    for span in LINK_SPAN.finditer(text):
        result.append(_link_file_mentions(text[cursor:span.start()]))
        match = MARKDOWN_LINK.fullmatch(span.group())
        result.append(link(match) if match else span.group())
        cursor = span.end()
    result.append(_link_file_mentions(text[cursor:]))
    return "".join(result)

def _tint(kind: str, text: str, plain: bool) -> str:
    if plain or not sys.stdout.isatty():
        return text
    return f"{COLOR.get(kind, '')}{text}{RESET}"

def render_terminal_artifact(value: str) -> str:

    try:
        target = value
        if not urlsplit(value).scheme:
            path = Path(value).expanduser()
            if not path.is_absolute():
                configured = os.environ.get("PRAXIS_ARTIFACT_ROOT") or os.environ.get("VAULT_ROOT")
                if not configured:
                    return value
                root = Path(configured).expanduser()
                if not root.is_absolute():
                    return value
                path = root / path
            target = str(path)
        target = _terminal_link_target(target)
    except (OSError, RuntimeError, ValueError):
        return value
    return _display_link(value, target) if target is not None else value

def make_printer(plain: bool = False, quiet: bool = False, *, write=None):

    def display(text: str) -> None:
        (write or print)(render_terminal_links(text))

    def post(event: Dict[str, Any]) -> None:
        kind = event.get("type")
        fallback = event.get("fallback") or {}
        model = event.get("model", "")
        model_label = model
        if event.get("effort"):
            model_label += f" {event['effort']}"
        if event.get("model_source") == "planned":
            model_label += "; planned, launch unverified"
        if fallback:
            model_label += f"; fallback from {fallback.get('from', 'primary')}"
            reason = fallback.get("because") or fallback.get("reason")
            if reason:
                model_label += f" ({reason})"
        seat_label = event.get("seat", "")
        if event.get("task_class"):
            seat_label += f" [{event.get('task_mode') or 'default'}; {event['task_class']}]"
        elif event.get("task_mode"):
            seat_label += f" [{event['task_mode']}]"
        if kind == "ack":
            if not quiet:
                display(_tint(kind, event["message"], plain))
                display(_tint("started", f"  {event.get('request_id')}", plain))
        elif kind == "started":
            if not quiet:
                display(_tint(kind, f"  … {seat_label} ({model_label})", plain))
        elif kind == "result":
            seat, state = event["seat"], event.get("state")
            head = f"\n{seat_label}" + (f"  ({model_label})" if not quiet else "")
            display(_tint("result" if state == "succeeded" else "failed", head, plain))
            if event.get("summary"):
                display("  " + event["summary"].replace("\n", "\n  "))
            for path in event.get("artifacts") or []:
                display(_tint("started", f"  → {render_terminal_artifact(path)}", plain))
            if state != "succeeded":
                reasons = event.get("contract_reasons") or []
                display(_tint("failed", f"  [{state}] {'; '.join(reasons)}", plain))
        elif kind == "pending":
            display(_tint("unavailable", f"  {seat_label}: {event.get('message') or 'Original work remains pending.'}", plain))
        elif kind == "detail":
            if event.get("receipt_changed") and not quiet:
                display(_tint("started", f"  Launch verified: {seat_label} ({model_label})", plain))
            for path in event.get("artifacts") or []:
                display(_tint("started", f"  → {render_terminal_artifact(path)}", plain))
            if event.get("contract_reasons"):
                display(_tint("failed", f"  [{event.get('state')}] "
                                      f"{'; '.join(event['contract_reasons'])}", plain))
        elif kind == "question":
            display(_tint("unavailable", f"\n{event['seat']} is asking you:", plain))
            display("  " + (event.get("message") or "").replace("\n", "\n  "))
        elif kind in ("failed", "refused", "unavailable"):
            display(_tint(kind, f"\n{event.get('seat','')}: {event.get('message') or event.get('reason','')}", plain))
        elif kind == "dispatch_failed":
            display(_tint("failed", f"\ndispatch failed: {event.get('error','')}", plain))
        elif kind == "duplicate":
            display(_tint(kind, event["message"], plain))
        elif kind == "recovery_skipped":
            if not quiet:
                display(_tint("started", f"  {event.get('request_id')} is still being "
                                       f"collected by a live process; left alone.", plain))
        elif kind == "recovered":
            display(_tint(kind, f"  recovered {event.get('request_id')}: "
                              f"run {event.get('run_status')} → {event.get('lifecycle')}", plain))
            if event.get("disposition") == "live_uncollected":

                live = ", ".join(event.get("live_workers") or []) or "state unobserved"
                display(_tint("unavailable",
                            f"    live work preserved ({live}); nothing was fenced "
                            f"or handed back — see the note for safe next steps",
                            plain))
        elif kind == "room_incomplete":
            display(_tint(kind, "\n" + event.get("message", "room did not close"), plain))
        elif kind == "room_recorded":
            display(_tint(kind, "\nRoom record updated: "
                        + render_terminal_artifact(event.get("record") or ""), plain))
        elif kind == "room_trace_recorded":
            display(_tint(kind, "\nResearch trace updated: "
                          + render_terminal_artifact(event.get("report") or ""), plain))
            if event.get("head_hash"):
                display(_tint("started", f"  {event.get('entries', 0)} entries · "
                                      f"head {str(event['head_hash'])[:16]}…", plain))
        elif kind == "room_trace_failed":
            display(_tint(kind, "\nResearch trace failed: "
                          + str(event.get("error") or "unknown error"), plain))
        elif kind == "room_prompt_recorded":
            display(_tint(kind, "\nSaved in the project room: "
                        + render_terminal_artifact(event.get("record") or ""), plain))
            if event.get("request_dir"):
                display(_tint("started", f"  outputs: {event['request_dir']}", plain))
        elif kind == "praxis45_progress":
            if not quiet:display(event["message"])
        elif kind == "complete":
            if event.get("praxis45_brief"):
                display(event["quiet_message"] if quiet else event["message"])
                return
            if quiet and event.get("lifecycle") == "completed":
                return
            display("\n" + _tint(kind, event["message"], plain))
    return post

def collecting_printer():

    events: list = []

    def post(event: Dict[str, Any]) -> None:
        events.append(event)
    return events, post
