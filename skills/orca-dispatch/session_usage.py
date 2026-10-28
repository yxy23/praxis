                      

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PREAMBLE_SIGNATURES = (
    "--dispatch-capability",
    "orca orchestration send --from term_",
)

DISPATCH_ID_RE = re.compile(r"^ctx_[0-9a-zA-Z_-]{4,64}$")

CODEX_FILENAME_RE = re.compile(r"-([0-9a-fA-F]{8}-[0-9a-fA-F-]{20,40})\.jsonl$")

_CODEX_USER_PAYLOAD_TYPES = {"message", "user_message"}

SUPPORTED_AGENTS = ("claude", "codex")

MTIME_SKEW_SECONDS = 900

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def claude_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("PRAXIS_CLAUDE_PROJECTS")
    return Path(env).expanduser() if env else Path("~/.claude/projects").expanduser()

def codex_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("PRAXIS_CODEX_SESSIONS")
    return Path(env).expanduser() if env else Path("~/.codex/sessions").expanduser()

def _iter_transcripts(root: Path, since: Optional[float]) -> Iterable[Path]:
    if not root.is_dir():
        return
    floor = None if since is None else since - MTIME_SKEW_SECONDS
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        if floor is not None:
            try:
                if path.stat().st_mtime < floor:
                    continue
            except OSError:
                continue
        yield path

def _iter_json_lines(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(record, dict):
                yield record

_SCAN_CHUNK = 1 << 20

def _contains(path: Path, needle: str) -> bool:

    probe = needle.encode("utf-8")
    overlap = len(probe) - 1
    try:
        with path.open("rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(_SCAN_CHUNK)
                if not chunk:
                    return False
                if probe in tail + chunk:
                    return True
                tail = chunk[-overlap:] if overlap else b""
    except OSError:
        return False

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                value = block.get("text") or block.get("content")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if isinstance(content, dict):
        value = content.get("text")
        return value if isinstance(value, str) else ""
    return ""

def _is_launch_instruction(text: str, dispatch_id: str) -> bool:
    return dispatch_id in text and any(sig in text for sig in PREAMBLE_SIGNATURES)

def _claude_match(path: Path, dispatch_id: str) -> Optional[str]:

    session_id = None
    matched = False
    for record in _iter_json_lines(path):
        session_id = session_id or record.get("sessionId")
        if record.get("type") != "user":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") not in (None, "user"):
            continue
        if _is_launch_instruction(_text_of(message.get("content")), dispatch_id):
            matched = True
            break
    if not matched:
        return None
    return str(session_id or path.stem)

def _codex_match(path: Path, dispatch_id: str) -> Optional[str]:

    session_id = None
    matched = False
    for record in _iter_json_lines(path):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "session_meta":
            session_id = session_id or payload.get("id") or payload.get("session_id")
        if payload.get("type") not in _CODEX_USER_PAYLOAD_TYPES:
            continue
        if payload.get("role") not in (None, "user"):
            continue
        text = _text_of(payload.get("content") or payload.get("message") or payload.get("text"))
        if _is_launch_instruction(text, dispatch_id):
            matched = True
            break
    if not matched:
        return None
    if not session_id:
        found = CODEX_FILENAME_RE.search(path.name)
        session_id = found.group(1) if found else path.stem
    return str(session_id)

def resolve_session(
    dispatch_id: str,
    agent: str,
    *,
    started_at: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:

    result: Dict[str, Any] = {
        "agent": agent,
        "dispatch_id": dispatch_id,

        "binding": "dispatch_preamble",
        "status": "unavailable",
        "session_id": None,
        "transcript_path": None,
        "reason": None,
        "candidates": [],
        "resolved_at": now_iso(),
    }
    if agent not in SUPPORTED_AGENTS:
        result["reason"] = "unsupported_agent"
        return result
    if not isinstance(dispatch_id, str) or not DISPATCH_ID_RE.match(dispatch_id):
        result["reason"] = "invalid_dispatch_id"
        return result

    since = _epoch(started_at)
    base = claude_root(root) if agent == "claude" else codex_root(root)
    if not base.is_dir():
        result["reason"] = "transcript_root_missing"
        return result

    matcher = _claude_match if agent == "claude" else _codex_match
    hits: List[Dict[str, str]] = []
    for path in _iter_transcripts(base, since):
        if not _contains(path, dispatch_id):
            continue
        session_id = matcher(path, dispatch_id)
        if session_id:
            hits.append({"session_id": session_id, "transcript_path": str(path)})

    distinct = {hit["session_id"]: hit for hit in hits}
    if not distinct:
        result["reason"] = "session_not_found"
        return result
    if len(distinct) > 1:

        result["reason"] = "ambiguous_session"
        result["candidates"] = sorted(distinct)
        return result

    only = next(iter(distinct.values()))
    result["status"] = "bound"
    result["session_id"] = only["session_id"]
    result["transcript_path"] = only["transcript_path"]
    result["candidates"] = [only["session_id"]]
    return result

def _epoch(started_at: Optional[str]) -> Optional[float]:
    if not started_at:
        return None
    text = str(started_at).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None

def _empty_usage(agent: str, session_id: Optional[str], reason: Optional[str]) -> Dict[str, Any]:
    return {
        "status": "unavailable" if reason else "known",
        "provider": agent,
        "session_id": session_id,
        "attribution": "dispatch_bound_session",
        "model": None,
        "models": [],
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
        "turns": None,
        "reason": reason,
        "collected_at": now_iso(),
    }

def _claude_usage(path: Path, session_id: str) -> Dict[str, Any]:

    by_message: Dict[str, Dict[str, int]] = {}
    models: Dict[str, int] = {}
    order: List[str] = []
    for record in _iter_json_lines(path):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        key = str(message.get("id") or f"line:{len(by_message)}")
        counts = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        }
        if key not in by_message:
            order.append(key)
        by_message[key] = counts
        model = message.get("model")
        if isinstance(model, str) and model:
            models[model] = models.get(model, 0) + counts["output_tokens"]

    if not by_message:
        return _empty_usage("claude", session_id, "no_usage_in_session")

    out = _empty_usage("claude", session_id, None)
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        out[field] = sum(counts[field] for counts in by_message.values())
    out["reasoning_output_tokens"] = None
    out["total_tokens"] = (
        out["input_tokens"] + out["output_tokens"]
        + out["cache_read_tokens"] + out["cache_write_tokens"]
    )
    out["turns"] = len(by_message)
    out["models"] = sorted(models)
    out["model"] = max(models, key=lambda name: models[name]) if models else None
    return out

def _codex_usage(path: Path, session_id: str) -> Dict[str, Any]:

    total: Optional[Dict[str, Any]] = None
    models: List[str] = []
    turns = 0
    for record in _iter_json_lines(path):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
        if record.get("type") == "session_meta":
            model = payload.get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        cumulative = info.get("total_token_usage")
        if isinstance(cumulative, dict):
            total = cumulative
            turns += 1

    if not total:
        return _empty_usage("codex", session_id, "no_usage_in_session")

    out = _empty_usage("codex", session_id, None)
    out["input_tokens"] = int(total.get("input_tokens") or 0)
    out["output_tokens"] = int(total.get("output_tokens") or 0)
    out["cache_read_tokens"] = int(total.get("cached_input_tokens") or 0)
    out["cache_write_tokens"] = int(total.get("cache_write_input_tokens") or 0)
    out["reasoning_output_tokens"] = int(total.get("reasoning_output_tokens") or 0)
    out["total_tokens"] = int(
        total.get("total_tokens")
        or (out["input_tokens"] + out["output_tokens"])
    )
    out["turns"] = turns
    out["models"] = sorted(models)
    out["model"] = models[0] if models else None
    return out

def collect_usage(binding: Dict[str, Any]) -> Dict[str, Any]:

    agent = str(binding.get("agent") or "")
    session_id = binding.get("session_id")
    if binding.get("status") != "bound" or not session_id:
        return _empty_usage(agent or "unknown", None,
                            binding.get("reason") or "session_not_bound")
    path = Path(str(binding.get("transcript_path") or ""))
    if not path.is_file():
        return _empty_usage(agent, session_id, "transcript_unreadable")
    if agent == "claude":
        return _claude_usage(path, str(session_id))
    if agent == "codex":
        return _codex_usage(path, str(session_id))
    return _empty_usage(agent, session_id, "unsupported_agent")

def bind_and_collect(
    dispatch_id: str,
    agent: str,
    *,
    started_at: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    binding = resolve_session(dispatch_id, agent, started_at=started_at, root=root)
    return {"session": binding, "usage": collect_usage(binding)}

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind a Dispatch to its provider session and read that session's usage")
    parser.add_argument("--dispatch", required=True, help="Orca dispatch id (ctx_...)")
    parser.add_argument("--agent", required=True, choices=list(SUPPORTED_AGENTS))
    parser.add_argument("--started-at", default=None,
                        help="ISO timestamp; narrows the candidate files only")
    parser.add_argument("--root", default=None, help="override the transcript root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = bind_and_collect(
        args.dispatch, args.agent, started_at=args.started_at,
        root=Path(args.root).expanduser() if args.root else None,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        session, usage = result["session"], result["usage"]
        print(f"session   {session['status']}  {session['session_id'] or session['reason']}")
        print(f"usage     {usage['status']}  total={usage['total_tokens']} model={usage['model']}")
    return 0 if result["session"]["status"] == "bound" else 1

if __name__ == "__main__":
    raise SystemExit(main())
