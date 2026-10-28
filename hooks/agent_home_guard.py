                      

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import sys
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HOME_VARS: Tuple[str, ...] = (
    "HOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "OPENCODE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)

CANONICAL_SUFFIX: Dict[str, str] = {
    "HOME": "",
    "CLAUDE_CONFIG_DIR": ".claude",
    "CODEX_HOME": ".codex",
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": os.path.join(".local", "share"),
    "XDG_STATE_HOME": os.path.join(".local", "state"),
}

REPAIR_ON_UNSET: Tuple[str, ...] = ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")

AGENT_BINARIES = frozenset({"claude", "codex", "orca", "orca-dev", "orca-ide", "opencode"})

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_VARREF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_SEGMENT = re.compile(r"&&|\|\||[;|\n]")

class AgentHomePolicyError(RuntimeError):
    pass

def passwd_home() -> str:

    return pwd.getpwuid(os.getuid()).pw_dir

def canonical(var: str, home: Optional[str] = None) -> Optional[str]:

    if var not in CANONICAL_SUFFIX:
        return None
    root = home or passwd_home()
    suffix = CANONICAL_SUFFIX[var]
    return os.path.normpath(os.path.join(root, suffix) if suffix else root)

def content_roots(environ: Optional[Mapping[str, str]] = None,
                  home: Optional[str] = None) -> List[str]:

    env = os.environ if environ is None else environ
    root = home or passwd_home()
    vault = env.get("PRAXIS_VAULT_ROOT") or env.get("VAULT_ROOT") or os.path.join(root, "Research")
    candidates = [
        vault,
        env.get("PRAXIS_ARTIFACT_ROOT") or "",
        env.get("DROPBOX_ROOT") or "",
        os.path.join(root, "Documents"),
        os.path.join(root, "Library", "CloudStorage"),
        os.path.join(root, "Library", "Mobile Documents"),
    ]
                                                                      
    worktree = env.get("ORCA_WORKTREE_ID", "")
    if "::" in worktree:
        parts = worktree.split("::")
        if len(parts) >= 2 and parts[1]:
            candidates.append(parts[1])
    seen: List[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        norm = os.path.normpath(candidate)
        if norm not in seen and norm not in ("/", "."):
            seen.append(norm)
    return seen

def is_within(path: str, root: str) -> bool:

    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root + os.sep)

def expand(value: str, environ: Optional[Mapping[str, str]] = None,
           cwd: Optional[str] = None) -> str:

    env = os.environ if environ is None else environ
    here = cwd if cwd is not None else env.get("PWD", "")

    def resolve(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        if name == "PWD" and here:
            return here
        return env.get(name, "")

    text = _VARREF.sub(resolve, value)
    if text == "~":
        text = env.get("HOME", "") or passwd_home()
    elif text.startswith("~/"):
        text = os.path.join(env.get("HOME", "") or passwd_home(), text[2:])
    return text

def check(assignments: Mapping[str, str], *,
          environ: Optional[Mapping[str, str]] = None,
          cwd: Optional[str] = None,
          home: Optional[str] = None,
          roots: Optional[Sequence[str]] = None) -> List[str]:

    env = os.environ if environ is None else environ
    root = home or passwd_home()
    content = list(roots) if roots is not None else content_roots(env, root)
    reasons: List[str] = []

    for var in HOME_VARS:
        if var not in assignments:
            continue
        raw = assignments[var]
        value = expand(raw, env, cwd)
        shown = f"{var}={raw!r}"
        if raw != value:
            shown += f" (expands to {value!r})"

        if "\0" in value:
            reasons.append(f"{shown}: contains a NUL byte")
            continue
        if value == "":
            reasons.append(
                f"{shown}: empty. An empty home variable makes every path the "
                f"program builds relative, so its state lands in the working "
                f"directory instead of its canonical runtime home.")
            continue
        if not os.path.isabs(value):
            reasons.append(
                f"{shown}: relative. Resolved against the launch directory, "
                f"not against a home.")
            continue

        norm = os.path.normpath(value)
        allowed = canonical(var, root)
        if allowed is not None and norm == allowed:
            continue
        if var == "HOME":
            reasons.append(
                f"{shown}: HOME must be the passwd home {root!r}. "
                f"Relocating HOME relocates every agent's state at once.")
            continue
        for content_root in content:
            if is_within(norm, content_root):
                reasons.append(
                    f"{shown}: inside the content root {content_root!r}. "
                    f"Agent runtime state must never be written where "
                    f"User's documents live.")
                break
    return reasons

def normalized_env(environ: Optional[Mapping[str, str]] = None,
                   cwd: Optional[str] = None,
                   home: Optional[str] = None) -> Dict[str, str]:

    env = dict(os.environ if environ is None else environ)
    root = home or passwd_home()
    present = {var: env[var] for var in HOME_VARS if var in env}
    reasons = check(present, environ=env, cwd=cwd, home=root)
    if reasons:
        raise AgentHomePolicyError(
            "refusing to launch an agent with this environment:\n  - "
            + "\n  - ".join(reasons))
    for var in REPAIR_ON_UNSET:
        if var not in env:
            value = canonical(var, root)
            if value:
                env[var] = value
    return env

def _leading_assignments(tokens: Sequence[str]) -> Tuple[Dict[str, str], Optional[str]]:

    assignments: Dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        match = _ASSIGNMENT.match(token)
        if match:
            assignments[match.group(1)] = match.group(2)
            index += 1
            continue
        if os.path.basename(token) == "env":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                flag = tokens[index]
                index += 1
                if flag in ("-u", "--unset") and index < len(tokens):
                    index += 1
            continue
        break
    command = tokens[index] if index < len(tokens) else None
    return assignments, command

def scan_command(command: str,
                 environ: Optional[Mapping[str, str]] = None,
                 cwd: Optional[str] = None,
                 home: Optional[str] = None) -> List[str]:

    env = os.environ if environ is None else environ
    root = home or passwd_home()
    reasons: List[str] = []
    for segment in _SEGMENT.split(command or ""):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens:
            continue
        assignments, invoked = _leading_assignments(tokens)
        if invoked is None or os.path.basename(invoked) not in AGENT_BINARIES:
            continue
        relevant = {k: v for k, v in assignments.items() if k in HOME_VARS}
        found = check(relevant, environ=env, cwd=cwd, home=root)

        ambient = {k: env[k] for k in HOME_VARS if k in env and k not in relevant}
        found += [f"inherited {r}" for r in check(ambient, environ=env, cwd=cwd, home=root)]
        for reason in found:
            entry = f"{segment.strip()}: {reason}"
            if entry not in reasons:
                reasons.append(entry)
    return reasons

def main(argv: Optional[Sequence[str]] = None) -> int:

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("tool_name") not in (None, "Bash"):
        return 0
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return 0
    try:
        reasons = scan_command(command, cwd=payload.get("cwd"))
    except Exception:                                                          
        return 0
    if not reasons:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason":
            "Blocked by the Praxis agent-home guard. This command would give an "
            "agent a home directory inside User's content, or no home at all:\n  - "
            + "\n  - ".join(reasons)
            + "\n\nUse an absolute path under the user home, or drop the "
              "assignment and let the canonical default apply.",
    }}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
