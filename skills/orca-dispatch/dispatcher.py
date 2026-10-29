                      

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "1"
SENSITIVITIES = {"public", "internal", "confidential", "highly-restricted"}
RISKS = {"low", "medium", "high"}

HARD_ISOLATED_DOMAINS: set = set()
PROVIDER_AGENTS = {"claude": "claude", "codex": "codex", "antigravity": "antigravity"}
TERMINAL_OUTCOMES = {
    "succeeded",
    "held",
    "failed",
    "contract_rejected",
    "launch_failed",
    "blocked",
    "collection_timeout",
}
AUTHORITATIVE_SUCCESSES = {"validated", "succeeded", "duplicate_suppressed"}

DECLARED_OUTCOMES = {"created", "already_present", "ran", "no_change", "partial", "held", "failed"}

SINGLE_ATTEMPT_PER_LANE = True

OVERSIZED_BODY_PREVIEW_CHARS = 2000

class DispatchError(RuntimeError):
    pass

class CommandReadError(DispatchError):
    pass

class DispatcherInterrupted(DispatchError):
    pass

def _load_session_usage() -> Any:

    path = Path(__file__).resolve().parent / "session_usage.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("praxis_session_usage", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)

    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module

def _load_agent_home_guard() -> Any:

    candidates = [
        Path.home() / ".claude" / "hooks" / "agent_home_guard.py",
        Path(__file__).resolve().parents[2] / "hooks" / "agent_home_guard.py",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("praxis_agent_home_guard", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise DispatchError(
        "agent-home policy not found at " + " or ".join(str(p) for p in candidates)
        + ". Configure the required runtime guard; see services/praxis45/PUBLIC-RUNTIME.md. Refusing to launch a worker with an "
        "unchecked environment: an empty or relative CODEX_HOME/CLAUDE_CONFIG_DIR "
        "puts the agent's home, and its credentials, in the working directory.")

def launch_env() -> Dict[str, str]:

    guard = _load_agent_home_guard()
    try:
        return guard.normalized_env()
    except guard.AgentHomePolicyError as exc:
        raise DispatchError(str(exc)) from exc

class CommandError(DispatchError):
    def __init__(self, command: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.command = list(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = stderr.strip() or stdout.strip() or "no diagnostic"
        super().__init__(f"command failed ({returncode}): {shlex.join(command)}: {detail[:1200]}")

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def emit(event: str, **payload: Any) -> None:
    row = {"event": event, "ts": now_iso(), **payload}
    print(json.dumps(row, ensure_ascii=False), file=sys.stderr, flush=True)

def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchError(f"plan not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DispatchError(f"invalid JSON plan {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DispatchError("plan root must be a JSON object")
    return value

def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def process_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True

def routine_run_key(plan: Dict[str, Any], override: Optional[str] = None,
                    when: Optional[datetime] = None) -> Optional[str]:

    if override:
        return override
    schedule = plan.get("schedule")
    plan_id = str(plan.get("id") or "").strip()
    if not plan_id or not isinstance(schedule, dict):
        return None
    tz_name = schedule.get("timezone")
    if not isinstance(tz_name, str) or not tz_name:
        raise DispatchError("schedule.timezone must be an explicit IANA timezone")
    try:
        ZoneInfo(tz_name)
    except (KeyError, ValueError) as exc:
        raise DispatchError("schedule.timezone must be a valid IANA timezone") from exc
    local_now = (when or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))
    times = sorted(str(item) for item in schedule.get("local_times", []) if item)
    weekdays = schedule.get("weekdays")
    candidates: List[datetime] = []
    for days_back in range(0, 8):
        day = (local_now - timedelta(days=days_back)).date()
        if weekdays is not None and day.weekday() not in set(int(item) for item in weekdays):
            continue
        for value in times:
            hour, minute = (int(part) for part in value.split(":", 1))
            candidate = datetime(day.year, day.month, day.day, hour, minute,
                                 tzinfo=ZoneInfo(tz_name))
            if candidate <= local_now:
                candidates.append(candidate)
    if not candidates:
        raise DispatchError(f"cannot derive an occurrence for {plan_id} from {schedule}")
    occurrence = max(candidates)
    return f"{plan_id}/{occurrence.strftime('%Y-%m-%dT%H%M%z')}"

class LeasePersistenceError(DispatchError):
    pass

class RunLease:

    def __init__(self, root: Path, run_key: str, output: Optional[Path], *, custody_host=None):
        token = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:24]
        routine = run_key.split("/", 1)[0]
        self.path = root / "system" / "memory" / "agents" / routine / "leases" / token
        self.state_path = self.path / "state.json"
        self.run_key = run_key
        self.output = str(output.expanduser().resolve()) if output else None
        self.recovery_state: Dict[str, Any] = {}
        self.acquired = False
        self.persistence_failed = False
        self.custody_host = custody_host or socket.gethostname()

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("state is not an object")
            return value
        except (OSError, ValueError) as exc:
            raise DispatchError(
                f"unusable existing lease {self.state_path}: {exc}; prior attempt "
                "cannot be identified, so no new execution is allowed"
            ) from exc

    def _validate(self, state: Dict[str, Any]) -> None:
        valid = (
            type(state.get("schema_version")) is int and state["schema_version"] == 1
            and state.get("run_key") == self.run_key
            and state.get("status") in {"active", "interrupted", "dispatcher_error",
                                       "succeeded", "partial", "failed", "held", "needs_input"}
            and type(state.get("pid")) is int
            and isinstance(state.get("host"), str) and bool(state["host"])
            and isinstance(state.get("started_at"), str) and bool(state["started_at"])
            and all(isinstance(state.get(key), dict)
                    for key in ("route_decisions", "tasks", "workers"))
            and "run_id" in state
            and (state["run_id"] is None or
                 isinstance(state["run_id"], str) and bool(state["run_id"]))
        )
        if valid:
            for key, required in (("tasks", ("task_id", "status")),
                                  ("workers", ("task_id", "dispatch_id", "status"))):
                for value in state[key].values():
                    valid = valid and isinstance(value, dict) and all(
                        isinstance(value.get(field), str) and bool(value[field])
                        for field in required)
            for value in state["route_decisions"].values():
                valid = valid and isinstance(value, dict) and isinstance(value.get("decision"), dict)
        if not valid:
            raise DispatchError(
                f"invalid or incomplete existing lease {self.state_path}; prior "
                "attempt cannot be identified, so no new execution is allowed")
        if state.get("run_creation_pending") or state.get("task_creation_pending"):
            raise DispatchError("unresolved Orca creation boundary; reconcile exact prior identities before execution")

    def _write(self, state: Dict[str, Any]) -> None:
        if self.persistence_failed:
            raise LeasePersistenceError("lease persistence previously failed; execution remains unresolved")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path,
                                             prefix=".state-", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
                                                                                
            for directory in (self.path, self.path.parent):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            self.persistence_failed = True
            raise LeasePersistenceError(
                f"lease persistence failed at {self.state_path}: {exc}; "
                "execution unresolved, no automatic relaunch") from exc
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()

    def _recovery_ancestor(self, prior: Dict[str, Any]) -> Dict[str, Any]:

        if prior.get("run_id"):
            return prior
        recovery_of = str(prior.get("recovery_of") or "")
        if not recovery_of:
            return prior
        candidates: List[Tuple[Path, Dict[str, Any]]] = []
        for path in self.path.parent.glob(f"{self.path.name}.recovery-*/state.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and value.get("run_key") == self.run_key
                and value.get("run_id") == recovery_of
            ):
                candidates.append((path, value))
        if len(candidates) != 1:
            return prior
        ancestor = dict(candidates[0][1])
        ancestor["recovered_through"] = str(self.state_path)
        return ancestor

    def acquire(self) -> Tuple[bool, Dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(3):
            try:
                self.path.mkdir()
                state = {
                    "schema_version": 1,
                    "run_key": self.run_key,
                    "status": "active",
                    "pid": os.getpid(),
                    "host": self.custody_host,
                    "started_at": now_iso(),
                    "bundle_path": self.output,
                    "route_decisions": self.recovery_state.get("route_decisions", {}),
                    "run_id": self.recovery_state.get("run_id"),
                    "tasks": self.recovery_state.get("tasks", {}),
                    "workers": self.recovery_state.get("workers", {}),
                    "recovery_of": self.recovery_state.get("run_id"),
                    "unconsumed_delivery": self.recovery_state.get("unconsumed_delivery"),
                }
                self._write(state)
                self.acquired = True
                return True, state
            except FileExistsError:
                prior = self._read()
                self._validate(prior)
                status = prior.get("status")
                same_host = prior.get("host") == self.custody_host
                if status not in {"active", "interrupted", "dispatcher_error"} or (
                    status == "active" and same_host and process_alive(prior.get("pid"))
                ):
                    return False, prior
                self.recovery_state = self._recovery_ancestor(prior)
                self._validate(self.recovery_state)
                if not self.recovery_state.get("run_id"):

                    if (self.recovery_state["tasks"] or self.recovery_state["workers"]
                            or self.recovery_state.get("recovery_of")
                            or self.recovery_state.get("task_id")
                            or self.recovery_state.get("dispatch_id")):
                        raise DispatchError("existing lease has launch evidence but no exact Run identity; unresolved, refuse a replacement Run")
                stale = self.path.with_name(
                    f"{self.path.name}.recovery-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}"
                )
                try:
                    os.replace(self.path, stale)
                except FileNotFoundError:
                    continue
        raise DispatchError(f"could not acquire atomic lease for {self.run_key}")

    def update(self, **fields: Any) -> None:
        if not self.acquired:
            return
        state = self._read()
        state.update(fields)
        self._write(state)

    def recovered_route(self, unit_id: str) -> Optional[Dict[str, Any]]:
        routes = self.recovery_state.get("route_decisions") or {}
        value = routes.get(unit_id)
        return value.get("decision") if isinstance(value, dict) else None

    def record_route(self, unit_id: str, route: Dict[str, Any], decision_id: str) -> None:
        state = self._read()
        routes = state.setdefault("route_decisions", {})
        routes[unit_id] = {
            "route_decision_id": decision_id,
            "decision_digest": canonical_digest(route),
            "decision": route,
        }
        self._write(state)

    def record_worker(self, unit_id: str, task_id: str, dispatch_id: str,
                      status: str = "dispatched",
                      provider_session: Optional[Dict[str, Any]] = None,
                      launch: Optional[Dict[str, Any]] = None,
                      settlement: Optional[Dict[str, Any]] = None) -> None:
        state = self._read()
        workers = state.setdefault("workers", {})
        prior = workers.get(unit_id) or {}
        workers[unit_id] = {
            "task_id": task_id, "dispatch_id": dispatch_id,
            "status": status, "updated_at": now_iso(),

            "provider_session": provider_session or prior.get("provider_session"),
            "actual_launch": launch or prior.get("actual_launch"),
            **({"settlement": settlement} if settlement is not None else
               {k: v for k, v in prior.items() if k == "settlement"}),
        }
        self._write(state)

    def recovered_workers(self) -> Dict[str, Dict[str, Any]]:
        workers = self.recovery_state.get("workers") or {}
        return workers if isinstance(workers, dict) else {}

    def record_task(self, unit_id: str, task_id: str, status: str,
                    prompt_binding: Optional[Dict[str, Any]] = None) -> None:
        state = self._read()
        tasks = state.setdefault("tasks", {})
        tasks[unit_id] = {
            "task_id": task_id, "status": status, "updated_at": now_iso(),
            **({"prompt_binding": prompt_binding}
               if prompt_binding is not None else
               {k: v for k, v in (tasks.get(unit_id) or {}).items() if k == "prompt_binding"}),
        }
        self._write(state)

    def recovered_tasks(self) -> Dict[str, Dict[str, Any]]:
        tasks = self.recovery_state.get("tasks") or {}
        return tasks if isinstance(tasks, dict) else {}

class ExclusiveWriterLock:

    def __init__(self, root: Path, name: str, wait_timeout_seconds: int):
        self.name = name
        self.wait_timeout_seconds = wait_timeout_seconds
        self.path = (root / "system" / "memory" / "agents" / "orca-dispatch"
                     / "locks" / f"{name}.lock")
        self.handle = None
        self.waited_seconds = 0.0
        self.acquired_at: Optional[str] = None

    def acquire(self) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                self.waited_seconds = time.monotonic() - started
                if self.waited_seconds >= self.wait_timeout_seconds:
                    raise DispatchError(
                        f"exclusive writer lock {self.name!r} remained held for "
                        f"{self.waited_seconds:.1f}s; refusing to overlap writers")
                time.sleep(0.25)
        self.waited_seconds = time.monotonic() - started
        self.acquired_at = now_iso()
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({
            "pid": os.getpid(), "name": self.name,
            "acquired_at": self.acquired_at,
        }) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self.receipt()

    def receipt(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "wait_timeout_seconds": self.wait_timeout_seconds,
            "waited_seconds": round(self.waited_seconds, 3),
            "acquired_at": self.acquired_at,
            "status": "acquired" if self.acquired_at else "not_acquired",
        }

    def release(self) -> None:
        if self.handle is None:
            return
        with suppress(OSError):
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None

def vault_root() -> Path:
    direct = os.environ.get("PRAXIS_VAULT_ROOT") or os.environ.get("VAULT_ROOT")
    if direct:
        return Path(os.path.expandvars(os.path.expanduser(direct))).resolve()
    config = Path("~/.praxis-config.sh").expanduser()
    if config.exists():
        pattern = re.compile(r'^\s*export\s+VAULT_ROOT=["\']?(.*?)["\']?\s*$')
        for line in config.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line)
            if match:
                raw = match.group(1).replace("$HOME", str(Path.home()))
                return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    raise DispatchError("cannot resolve vault root; set PRAXIS_VAULT_ROOT or VAULT_ROOT")

def artifact_root() -> Path:
    direct = os.environ.get("PRAXIS_ARTIFACT_ROOT")
    if not direct:
        raise DispatchError("set PRAXIS_ARTIFACT_ROOT for artifact path expansion")
    root = Path(os.path.expandvars(os.path.expanduser(direct)))
    if not root.is_absolute():
        raise DispatchError("PRAXIS_ARTIFACT_ROOT must be absolute")
    return root.resolve()

def default_router() -> Path:
    candidates = [
        Path("~/.agents/skills/chief-of-staff/router.py").expanduser(),
        Path("~/.claude/skills/chief-of-staff/router.py").expanduser(),
        Path(__file__).resolve().parent.parent / "chief-of-staff" / "router.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise DispatchError("cannot find the canonical chief-of-staff/router.py")

def resolve_orca(explicit: Optional[str]) -> List[str]:
    if explicit:
        return shlex.split(explicit)
    configured = os.environ.get("ORCA_CLI_COMMAND")
    if configured:
        return shlex.split(configured)
    if os.environ.get("ORCA_DEV_REPO_ROOT") and shutil.which("orca-dev"):
        return ["orca-dev"]
    if sys.platform.startswith("linux"):
        if shutil.which("orca-ide"):
            return ["orca-ide"]
        raise DispatchError("orca-ide is required on Linux outside an Orca-managed terminal")
    found = shutil.which("orca")
    if not found:
        raise DispatchError("orca CLI not found on PATH")
    return [found]

def expand_value(value: Any, variables: Dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace("${" + key + "}", replacement)
            value = value.replace("<" + key.replace("_", "-") + ">", replacement)
        return value
    if isinstance(value, list):
        return [expand_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: expand_value(item, variables) for key, item in value.items()}
    return value

def _cycles(units: Dict[str, Dict[str, Any]]) -> List[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    trail: List[str] = []

    def visit(unit_id: str) -> Optional[List[str]]:
        if unit_id in visiting:
            start = trail.index(unit_id)
            return trail[start:] + [unit_id]
        if unit_id in visited:
            return None
        visiting.add(unit_id)
        trail.append(unit_id)
        for dep in units[unit_id].get("depends_on", []):
            found = visit(dep)
            if found:
                return found
        trail.pop()
        visiting.remove(unit_id)
        visited.add(unit_id)
        return None

    for key in units:
        found = visit(key)
        if found:
            return found
    return []

def outcome_contract(unit: Dict[str, Any]) -> Dict[str, Any]:

    spec = ((unit.get("return_contract") or {}).get("outcome_contract") or {})
    return spec if isinstance(spec, dict) else {}

def _outcome_contract_errors(label: str, unit: Dict[str, Any]) -> List[str]:

    errors: List[str] = []
    contract = unit.get("return_contract") or {}
    spec = outcome_contract(unit)
    declared_conditionals = [
        declared for declared in (unit.get("outputs") or [])
        if isinstance(declared, dict) and "required_when_outcome" in declared
    ]
    if not spec:
        if declared_conditionals:
            errors.append(
                f"{label} declares outputs.required_when_outcome with no "
                f"return_contract.outcome_contract to resolve it against")
        return errors

    prefix = str(spec.get("prefix") or "").strip()
    if not prefix:
        errors.append(f"{label}.return_contract.outcome_contract.prefix is required")
    allowed = spec.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        errors.append(f"{label}.return_contract.outcome_contract.allowed must be a non-empty list")
        allowed = []
    allowed = [str(item) for item in allowed]
    unknown = sorted(set(allowed) - DECLARED_OUTCOMES)
    if unknown:
        errors.append(
            f"{label}.return_contract.outcome_contract.allowed names {unknown}, "
            f"which are not in the outcome vocabulary {sorted(DECLARED_OUTCOMES)}")

    buckets = {}
    for key in ("substantive", "no_change", "hold", "failure"):
        values = spec.get(key) or []
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            errors.append(f"{label}.return_contract.outcome_contract.{key} must be a list of outcome ids")
            values = []
        buckets[key] = [str(item) for item in values]
        stray = sorted(set(buckets[key]) - set(allowed))
        if stray:
            errors.append(
                f"{label}.return_contract.outcome_contract.{key} names {stray}, "
                f"which are not in `allowed`")

    for first, second in (("substantive", "no_change"), ("substantive", "hold"),
                          ("substantive", "failure"), ("no_change", "hold"),
                          ("no_change", "failure"), ("hold", "failure")):
        overlap = sorted(set(buckets[first]) & set(buckets[second]))
        if overlap:
            errors.append(
                f"{label}.return_contract.outcome_contract lists {overlap} as both "
                f"{first} and {second}")
    if not buckets["substantive"]:
        errors.append(
            f"{label}.return_contract.outcome_contract.substantive is required and "
            f"must name at least one outcome; a contract where nothing counts as work "
            f"cannot tell work from a skip")

    if "no_change" in allowed and "no_change" not in buckets["no_change"]:
        errors.append(
            f"{label}.return_contract.outcome_contract allows 'no_change' without "
            f"listing it under `no_change`; declare which outcomes are a completed "
            f"pass that changed nothing")

    required = [str(item) for item in (contract.get("required_substrings") or [])]
    if prefix and not any(prefix in item for item in required):
        errors.append(
            f"{label}.return_contract.required_substrings must include {prefix!r} so the "
            f"outcome line is mandatory rather than merely parsed")

    for declared in declared_conditionals:
        gate = declared.get("required_when_outcome")
        if not isinstance(gate, list) or not gate or any(not isinstance(item, str) for item in gate):
            errors.append(f"{label}.outputs.required_when_outcome must be a non-empty list of outcome ids")
            continue
        stray = sorted(set(gate) - set(allowed))
        if stray:
            errors.append(
                f"{label}.outputs.required_when_outcome names {stray}, which are not in `allowed`")
        if "required" in declared:
            errors.append(
                f"{label}.outputs declares both `required` and `required_when_outcome`; "
                f"one output cannot be unconditionally and conditionally required")
    return errors

def parse_declared_outcome(
    body: str, unit: Dict[str, Any]
) -> Tuple[Optional[str], List[str]]:

    spec = outcome_contract(unit)
    if not spec:
        return None, []
    prefix = str(spec.get("prefix") or "")
    allowed = [str(item) for item in (spec.get("allowed") or [])]
    found: List[str] = []
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        found.append(stripped[len(prefix):].strip().strip("`*_ ").strip())
    if not found:
        return None, [f"missing the declared outcome line {prefix!r}"]
    distinct = sorted(set(found))
    if len(distinct) > 1:
        return None, [f"the declared outcome line {prefix!r} disagrees with itself: {distinct}"]
    token = distinct[0]
    if token not in allowed:
        return None, [
            f"declared outcome {token!r} is not one of {allowed} on the {prefix!r} line"
        ]
    return token, []

def outcome_disposition(token: Optional[str], unit: Dict[str, Any]) -> Dict[str, Any]:

    spec = outcome_contract(unit)
    if not spec or token is None:
        return {"declared_outcome": token, "declared": bool(spec),
                "substantive": None, "no_change": False, "hold": False,
                "failure": False}
    return {
        "declared_outcome": token,
        "declared": True,
        "substantive": token in [str(item) for item in (spec.get("substantive") or [])],

        "no_change": token in [str(item) for item in (spec.get("no_change") or [])],
        "hold": token in [str(item) for item in (spec.get("hold") or [])],
        "failure": token in [str(item) for item in (spec.get("failure") or [])],
    }

def lane_status(*, accepted: bool, worker_outcome: Optional[str],
                disposition: Dict[str, Any]) -> str:

    if not accepted:
        return "contract_rejected"
    if worker_outcome != "succeeded" or disposition["failure"]:
        return "failed"
    if disposition["hold"]:
        return "held"
    return "succeeded"

SESSION_BIND_ATTEMPTS = 4
SESSION_BIND_DELAY_SECONDS = 0.5

_SESSION_RETRYABLE = {"session_not_found", "transcript_root_missing"}

_SESSION_USAGE_MODULE: Any = None
_SESSION_USAGE_LOADED = False

def session_usage_module() -> Any:
    global _SESSION_USAGE_MODULE, _SESSION_USAGE_LOADED
    if not _SESSION_USAGE_LOADED:
        _SESSION_USAGE_LOADED = True
        try:
            _SESSION_USAGE_MODULE = _load_session_usage()
        except Exception:                                                       
            _SESSION_USAGE_MODULE = None
    return _SESSION_USAGE_MODULE

def provider_agent(actual_launch: Optional[Dict[str, Any]],
                   route: Dict[str, Any]) -> str:

    return str((actual_launch or {}).get("agent") or route.get("agent") or "")

def _unbound_session(dispatch_id: Optional[str], agent: str, reason: str) -> Dict[str, Any]:
    return {
        "agent": agent,
        "dispatch_id": dispatch_id,
        "binding": "dispatch_preamble",
        "status": "unavailable",
        "session_id": None,
        "transcript_path": None,
        "reason": reason,
        "candidates": [],
        "resolved_at": now_iso(),
    }

def bind_provider_session(
    dispatch_id: Optional[str],
    actual_launch: Optional[Dict[str, Any]],
    route: Dict[str, Any],
    *,
    since: Optional[str] = None,
    attempts: int = SESSION_BIND_ATTEMPTS,
) -> Dict[str, Any]:

    agent = provider_agent(actual_launch, route)
    if not dispatch_id:
        return _unbound_session(dispatch_id, agent, "no_dispatch")
    module = session_usage_module()
    if module is None:
        return _unbound_session(dispatch_id, agent, "binding_module_unavailable")
    result = _unbound_session(dispatch_id, agent, "session_not_found")
    for attempt in range(max(1, attempts)):
        try:
            result = module.resolve_session(dispatch_id, agent, started_at=since)
        except Exception as exc:                
            return _unbound_session(dispatch_id, agent, f"binding_error: {exc}")
        if result.get("status") == "bound":
            return result
        if result.get("reason") not in _SESSION_RETRYABLE:
            return result
        if attempt + 1 < max(1, attempts):
            time.sleep(SESSION_BIND_DELAY_SECONDS)
    return result

def collect_session_usage(binding: Dict[str, Any]) -> Dict[str, Any]:
    module = session_usage_module()
    if module is None:
        return {"status": "unavailable", "provider": binding.get("agent"),
                "session_id": None, "attribution": "dispatch_bound_session",
                "reason": "binding_module_unavailable", "collected_at": now_iso()}
    try:
        return module.collect_usage(binding)
    except Exception as exc:                
        return {"status": "unavailable", "provider": binding.get("agent"),
                "session_id": binding.get("session_id"),
                "attribution": "dispatch_bound_session",
                "reason": f"collection_error: {exc}", "collected_at": now_iso()}

def settle_provider_usage(record: Dict[str, Any], route: Dict[str, Any],
                          since: Optional[str]) -> None:

    binding = record.get("provider_session")
    if not isinstance(binding, dict) or binding.get("status") != "bound":
        binding = bind_provider_session(
            record.get("dispatch_id"), record.get("actual_launch"), route,
            since=since, attempts=1,
        )
        record["provider_session"] = binding
    record["usage"] = collect_session_usage(binding)

_OUTCOME_FAILED = {"failed", "contract_rejected", "launch_failed"}

_OUTCOME_UNSETTLED = {"blocked", "collection_timeout", "needs_input"}

def lane_outcome(record: Dict[str, Any], unit: Dict[str, Any]) -> Dict[str, Any]:

    status = str(record.get("status") or "unknown")
    contract = record.get("contract") or {}
    declared = record.get("declared_outcome")
    substantive = record.get("substantive")

    declares = bool(((unit.get("return_contract") or {}).get("outcome_contract")))

    if status in _OUTCOME_FAILED:
        outcome, basis = "failed", f"lane_{status}"
    elif status in _OUTCOME_UNSETTLED:
        outcome, basis = "unavailable", f"lane_{status}"
    elif status == "held":
        outcome, basis = "non_substantive", "declared_hold"
    elif status == "succeeded":
        if substantive is True:
            outcome, basis = "substantive", "declared_outcome"
        elif substantive is False:
            outcome, basis = "non_substantive", "declared_outcome"
        elif not declares:

            outcome, basis = "unavailable", "plan_declares_no_outcome_contract"
        else:
            outcome, basis = "unavailable", "declared_outcome_unreadable"
    else:
        outcome, basis = "unavailable", f"lane_{status}"

    return {
        "lane_status": status,
        "worker_reported_outcome": (record.get("worker_done") or {}).get("outcome"),
        "contract_accepted": contract.get("accepted"),
        "declared_outcome": declared,
        "substantive": substantive,
        "non_substantive_kind": declared if outcome == "non_substantive" else None,

        "cleanup": record.get("cleanup"),
        "outcome": outcome,
        "outcome_basis": basis,
        "rework": None,
    }

def _ledger_day(audit_path: Optional[str]) -> Optional[str]:
    if not audit_path:
        return None
    stem = Path(str(audit_path)).stem
    return stem if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem) else None

def route_outcome_payload(
    record: Dict[str, Any], unit: Dict[str, Any], *,
    run_key: Optional[str], praxis_task_id: Optional[str],
    plan_id: Optional[str], source: Dict[str, Any],
) -> Dict[str, Any]:
    route = record.get("route") or {}
    payload = {
        "route_decision_id": record.get("route_decision_id"),
        "route_decision_digest": record.get("route_decision_digest"),
        "route_decision_day": _ledger_day(route.get("_audit")),
        "route_ledger_path": route.get("_audit"),
        "route_source": record.get("route_source"),
        "run_key": run_key,
        "unit_id": record.get("id"),
        "praxis_task_id": praxis_task_id,
        "plan_id": plan_id,
        "task_class": route.get("task_class"),
        "provider": route.get("provider"),
        "agent": route.get("agent"),
        "effective_model": route.get("effective_model"),
        "requested_model": route.get("requested_model"),
        "route_primary": route.get("route_primary"),
        "launch_target": route.get("launch_target"),
        "effort": route.get("effort"),
        "actual_launch": record.get("actual_launch"),

        "worker_account": route.get("worker_account"),
        "requires_account": route.get("requires_account"),
        "benchmark_waived": route.get("benchmark_waived"),
        "pilot_route": route.get("pilot_route"),
        "pilot_decision": route.get("pilot_decision"),
        "pilot_expires": route.get("pilot_expires"),
        "pilot_expired": route.get("pilot_expired", False),
        "pilot_expired_decision": route.get("pilot_expired_decision"),
        "pilot_expired_on": route.get("pilot_expired_on"),
        "pilot_rollback": route.get("pilot_rollback"),
        "task_id": record.get("task_id"),
        "dispatch_id": record.get("dispatch_id"),
        "provider_session": record.get("provider_session"),
        "usage": record.get("usage"),
        "plan_sha256": source.get("plan_sha256"),
        "dispatcher_sha256": source.get("dispatcher_sha256"),
    }
    payload.update(lane_outcome(record, unit))
    return payload

def reconcile_route_outcomes(
    router: "RouterClient", state: Dict[str, Dict[str, Any]],
    order: List[str], units: Dict[str, Dict[str, Any]], *,
    run_key: Optional[str], praxis_task_id: Optional[str],
    plan_id: Optional[str], source: Dict[str, Any],
) -> None:

    for unit_id in order:
        record = state[unit_id]
        if not record.get("route_decision_id"):
            continue
        payload = route_outcome_payload(
            record, units[unit_id], run_key=run_key,
            praxis_task_id=praxis_task_id, plan_id=plan_id, source=source,
        )
        try:
            result = router.record_outcome(payload)
        except (DispatchError, CommandError, subprocess.SubprocessError) as exc:
            result = {"written": False, "action": "error", "error": str(exc)}
            record["unresolved"].append(f"route ledger outcome not recorded: {exc}")
        record["route_outcome"] = {
            "outcome": payload["outcome"],
            "outcome_basis": payload["outcome_basis"],
            "ledger": result,
        }
        record["telemetry"].append({
            "stage": "ledger", "ts": now_iso(),
            "route_decision_id": payload["route_decision_id"],
            "outcome": payload["outcome"], "result": result,
        })
        emit("route_outcome_recorded", unit=unit_id,
             outcome=payload["outcome"], action=result.get("action"))

def usage_rollup(state: Dict[str, Dict[str, Any]], order: List[str]) -> Dict[str, Any]:

    fields = ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_write_tokens", "reasoning_output_tokens", "total_tokens")
    totals = {field: 0 for field in fields}
    unknown_fields = set()
    total_turns = 0
    known, unavailable, reasons = [], [], {}
    for unit_id in order:
        usage = state[unit_id].get("usage") or {}
        if usage.get("status") == "known":
            known.append(unit_id)
            for field in fields:
                value = usage.get(field)
                if type(value) is int and value >= 0:
                    totals[field] += value
                else:
                    unknown_fields.add(field)
            turns = usage.get("turns")
            if type(turns) is int and turns >= 0:
                total_turns += turns
            else:
                unknown_fields.add('total_turns')
        else:
            unavailable.append(unit_id)
            reason = usage.get("reason") or "not_collected"
            reasons[unit_id] = reason
            unknown_fields.update((*fields, 'total_turns'))
    for field in fields:
        if field in unknown_fields:
            totals[field] = None
    if 'total_turns' in unknown_fields:
        total_turns = None
    return {
        "attribution": "dispatch_bound_session",
        "status": "known" if known and not unavailable else
                  "partial" if known else "unavailable",
        "units_known": known,
        "units_unavailable": unavailable,
        "unavailable_reasons": reasons,
        "total_turns": total_turns,
        **totals,
    }

def evaluate_usage_contract(
    contract: Dict[str, Any], usage: Dict[str, Any],
    state: Dict[str, Dict[str, Any]], order: List[str],
) -> Dict[str, Any]:

    if not contract:
        return {"configured": False, "accepted": True, "reasons": []}
    reasons: List[str] = []
    unknown: List[str] = []
    if usage.get("status") != "known":
        unknown.append("run usage is not fully attributable")
    def ceiling(measured, key, maximum, label, scope=""):
        if type(maximum) is not int:
            return []
        value = measured.get(key)
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)) or value < 0:
            unknown.append(f"{scope}{label}: usage unknown; ceiling unproven")
            return []
        return [f"{label} {value} exceed {maximum}"] if value > maximum else []

    reasons.extend(ceiling(usage, "total_tokens", contract.get("max_total_tokens"), "total tokens"))
    reasons.extend(ceiling(usage, "total_turns", contract.get("max_total_turns"), "total turns"))
    unit_contracts = contract.get("units") or {}
    unit_results: Dict[str, Any] = {}
    for unit_id in order:
        limits = unit_contracts.get(unit_id) or {}
        measured = state[unit_id].get("usage") or {}
        unit_reasons: List[str] = []
        max_tokens = limits.get("max_total_tokens")
        unit_reasons.extend(ceiling(measured, "total_tokens", max_tokens, "tokens", f"{unit_id}: "))
        max_turns = limits.get("max_turns")
        unit_reasons.extend(ceiling(measured, "turns", max_turns, "turns", f"{unit_id}: "))
        unit_results[unit_id] = {
            "accepted": not unit_reasons,
            "limits": limits,
            "measured": {
                "total_tokens": measured.get("total_tokens"),
                "turns": measured.get("turns"),
            },
            "reasons": unit_reasons,
        }
        reasons.extend(f"{unit_id}: {reason}" for reason in unit_reasons)
    return {
        "configured": True,
        "accepted": not reasons,
        "accounting_status": "exceeded" if reasons else "unknown" if unknown else "within_limits",
        "unknown": unknown,
        "limits": {
            "max_total_tokens": contract.get("max_total_tokens"),
            "max_total_turns": contract.get("max_total_turns"),
        },
        "measured": {
            "total_tokens": usage.get("total_tokens"),
            "total_turns": usage.get("total_turns"),
        },
        "units": unit_results,
        "reasons": reasons,
        "semantics": "post-run acceptance ceiling; artifacts are retained",
    }

def named_launch_module():

    candidates = [Path(__file__).resolve().parents[2] / "services" / "praxis-switchboard",
                  Path.home() / "dev" / "praxis" / "services" / "praxis-switchboard"]
    for path in candidates:
        if (path / "named_launch.py").is_file():
            sys.path.insert(0, str(path))
            import named_launch
            return named_launch
    raise DispatchError("canonical named-seat constructor missing; install the reviewed Switchboard boundary")

def validate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    if str(plan.get("schema_version")) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if not str(plan.get("objective") or "").strip():
        errors.append("objective is required")
    units_raw = plan.get("units")
    if not isinstance(units_raw, list) or not units_raw:
        errors.append("units must be a non-empty list")
        units_raw = []

    units: Dict[str, Dict[str, Any]] = {}
    for index, unit in enumerate(units_raw):
        label = f"units[{index}]"
        if not isinstance(unit, dict):
            errors.append(f"{label} must be an object")
            continue
        unit_id = str(unit.get("id") or "").strip()
        if not unit_id:
            errors.append(f"{label}.id is required")
            continue
        if unit_id in units:
            errors.append(f"duplicate unit id: {unit_id}")
            continue
        units[unit_id] = unit
        if not str(unit.get("task_class") or "").strip():
            errors.append(f"{label}.task_class is required")
        if "requested_model" in unit and not str(unit.get("requested_model") or "").strip():
            errors.append(f"{label}.requested_model must be a non-empty registry model id")
        if unit.get("sensitivity", "internal") not in SENSITIVITIES:
            errors.append(f"{label}.sensitivity is invalid")
        if unit.get("risk", "medium") not in RISKS:
            errors.append(f"{label}.risk is invalid")
        if not unit.get("skill") and not str(unit.get("spec") or "").strip():
            errors.append(f"{label} needs skill or spec")
        deps = unit.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            errors.append(f"{label}.depends_on must be a list of unit ids")
        if "depends_on_terminal" in unit and not isinstance(unit.get("depends_on_terminal"), bool):
            errors.append(f"{label}.depends_on_terminal must be a boolean")
        if "required_maintenance" in unit and not isinstance(unit.get("required_maintenance"), bool):
            errors.append(f"{label}.required_maintenance must be a boolean")
        if unit.get("depends_on_terminal") and not deps:
            errors.append(f"{label}.depends_on_terminal requires depends_on")
        if "data_domains" not in unit:
            errors.append(f"{label}.data_domains is required, even when it is []")
        elif not isinstance(unit.get("data_domains"), list) or any(
            not isinstance(domain, str) for domain in unit.get("data_domains", [])
        ):
            errors.append(f"{label}.data_domains must be a list of domain ids")
        domains = set(unit.get("data_domains") or [])
        isolated = sorted(domains & HARD_ISOLATED_DOMAINS)
        if isolated:
            errors.append(
                f"{label} declares hard-isolated data {isolated}; use a separately authorized adapter for that domain"
            )
        errors.extend(_outcome_contract_errors(label, unit))
        if 'source_bindings' in unit:
            bindings = unit['source_bindings']
            if (not isinstance(bindings, list) or not bindings or any(
                    not isinstance(item, dict) or not isinstance(item.get('path'), str) or
                    not Path(item['path']).is_absolute() or
                    not isinstance(item.get('sha256'), str) or
                    not re.fullmatch(r'[a-f0-9]{64}', item['sha256']) for item in bindings)):
                errors.append(f'{label}.source_bindings requires absolute paths and SHA-256 hashes')

    for unit_id, unit in units.items():
        for dep in unit.get("depends_on", []):
            if dep not in units:
                errors.append(f"unit {unit_id} depends on unknown unit {dep}")
            if dep == unit_id:
                errors.append(f"unit {unit_id} cannot depend on itself")
    if not errors:
        cycle = _cycles(units)
        if cycle:
            errors.append("dependency cycle: " + " -> ".join(cycle))

    defaults = plan.get("defaults") or {}
    if not isinstance(defaults, dict):
        errors.append("defaults must be an object")
        defaults = {}
    if defaults.get("worktree", "current") not in {"current"}:
        errors.append("v1 accepts only defaults.worktree='current'; explicit new worktrees require a later reviewed contract")

    if plan.get("final_assembly") is not None:
        errors.append("personal routine assembly is excluded from this public runtime")

    schedule = plan.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            errors.append("schedule must be an object")
        else:
            tz_name = schedule.get("timezone")
            if not isinstance(tz_name, str) or not tz_name:
                errors.append("schedule.timezone must be an explicit IANA timezone")
            else:
                try:
                    ZoneInfo(tz_name)
                except (KeyError, ValueError):
                    errors.append("schedule.timezone must be a valid IANA timezone")
            times = schedule.get("local_times")
            if not isinstance(times, list) or not times:
                errors.append("schedule.local_times must be a non-empty list")
            else:
                for value in times:
                    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                        errors.append(f"invalid local schedule time: {value!r}")

    writer_lock = plan.get("writer_lock")
    if writer_lock is not None:
        if not isinstance(writer_lock, dict):
            errors.append("writer_lock must be an object")
        else:
            name = writer_lock.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
                errors.append("writer_lock.name must be a lowercase lock id")
            timeout = writer_lock.get("wait_timeout_seconds", 10800)
            if not isinstance(timeout, int) or not 1 <= timeout <= 86400:
                errors.append("writer_lock.wait_timeout_seconds must be an integer from 1 to 86400")

    if errors:
        raise DispatchError("plan validation failed:\n- " + "\n- ".join(errors))
    boundary = named_launch_module()
    try:
        if boundary.RESERVED.intersection(plan):
            raise boundary.NamedLaunchError("caller-supplied named-seat contract refused")
        for unit in units.values():
            boundary.seat_for_unit(unit)
    except boundary.NamedLaunchError as exc:
        raise DispatchError(str(exc)) from exc
    return plan

@dataclass
class CommandResult:
    command: List[str]
    payload: Dict[str, Any]
    stdout: str
    stderr: str

class JsonCommand:

    def __init__(self, env: Optional[Dict[str, str]] = None):
        self._env = env

    @property
    def env(self) -> Dict[str, str]:
        if self._env is None:
            self._env = launch_env()
        return self._env

    def run(self, command: Sequence[str], timeout: int = 90, allow_failure: bool = False,
            stdin: Optional[str] = None) -> CommandResult:
        if getattr(self, "mutation_journal", None) is not None:
                                                                                 
            journal, self.mutation_journal = self.mutation_journal, None
            try:
                return journal.transmit(command,
                    lambda argv: self.run(argv, timeout, allow_failure, stdin),
                    {k: self.env.get(k) for k in ('ORCA_TERMINAL_HANDLE', 'ORCA_SESSION_ID')})
            finally:
                self.mutation_journal = journal
        proc = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout,
            check=False, env=self.env, input=stdin,
        )
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError as exc:
            raise CommandReadError(
                f"command returned non-JSON: {shlex.join(command)}: {proc.stdout[:800]}"
            ) from exc
        if proc.returncode != 0 and not allow_failure:
            raise CommandError(command, proc.returncode, proc.stdout, proc.stderr)
        if isinstance(data, dict) and data.get("ok") is False and not allow_failure:
            error = data.get("error") or {}
            raise CommandReadError(
                f"Orca rejected {command[-2] if len(command) > 1 else 'command'}: "
                f"{error.get('code', 'error')}: {error.get('message', error)}"
            )
        payload = data.get("result", data) if isinstance(data, dict) else {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        return CommandResult(list(command), payload, proc.stdout, proc.stderr)

class RouterClient:
    def __init__(self, router: Path, command: Optional[JsonCommand] = None):
        self.router = router
        self.command = command or JsonCommand()

    def route(self, unit: Dict[str, Any], task_id: Optional[str], log: bool,
              run_key: Optional[str] = None,
              producer_model: Optional[str] = None) -> Dict[str, Any]:
        command = [
            sys.executable,
            str(self.router),
            "--task-class",
            unit["task_class"],
            "--sensitivity",
            unit.get("sensitivity", "internal"),
            "--risk",
            unit.get("risk", "medium"),
            "--live-availability",
            "--json",
        ]
        if unit.get("consequential"):
            command.append("--consequential")
        if unit.get("external_action"):
            command.append("--external-action")
        if unit.get("required_tools"):
            command.extend(["--required-tools", ",".join(unit["required_tools"])])
        if unit.get("context_need"):
            command.extend(["--context-need", unit["context_need"]])
        if unit.get("requested_model"):
            command.extend(["--explicit-model", unit["requested_model"]])
        if task_id:
            command.extend(["--task-id", task_id])

        if run_key:
            command.extend(["--run-key", run_key])

        if producer_model:
            command.extend(["--producer-model", producer_model])
        command.extend(["--unit-id", str(unit["id"])])
        if log:
            command.append("--log")
        result = self.command.run(command)
        decision = result.payload
        if decision.get("refused"):
            raise DispatchError(f"router refused {unit['id']}: {decision['refused']}")
        required = {"effective_model", "provider", "effort"}
        missing = sorted(required - set(decision))
        if missing:
            raise DispatchError(f"router output for {unit['id']} lacks {missing}")
        provider = decision["provider"]
        decision["agent"] = decision.get("agent") or PROVIDER_AGENTS.get(provider)
        decision["launch_target"] = decision.get("launch_target") or decision["effective_model"]
        if not decision["agent"]:
            raise DispatchError(f"router provider {provider!r} has no Orca agent adapter")
        return decision

    def record_outcome(self, payload: Dict[str, Any]) -> Dict[str, Any]:

        result = self.command.run(
            [sys.executable, str(self.router), "--record-outcome", "--json"],
            stdin=json.dumps(payload),
        )
        return result.payload

    def worker_account(self) -> Dict[str, Any]:

        result = self.command.run(
            [sys.executable, str(self.router), "--worker-account", "--json"])
        payload = result.payload
        return payload if isinstance(payload, dict) else {}

def model_encoded_effort(route: Dict[str, Any]) -> Optional[str]:

    if route.get("agent") != "antigravity":
        return None
    match = re.fullmatch(r"gemini-4-argon-(low|medium|high)",
                         str(route.get("launch_target") or ""))
    return match[1] if match and match[1] == route.get("effort") else None

class OrcaClient:
    def __init__(self, prefix: Sequence[str], command: Optional[JsonCommand] = None):
        self.prefix = list(prefix)
        self.command = command or JsonCommand()

    def call(
        self,
        args: Sequence[str],
        timeout: int = 90,
        allow_failure: bool = False,
    ) -> Dict[str, Any]:
        result = self.command.run([*self.prefix, *args, "--json"], timeout, allow_failure)
        return result.payload

    def status(self) -> Dict[str, Any]:
        return self.call(["status"])

    def run_create(self, objective: str) -> Dict[str, Any]:
        return self.call(["orchestration", "run-create", "--objective", objective])

    def run_use(self, run_id: str) -> Dict[str, Any]:

        return self.call(["orchestration", "run-use", "--id", run_id])

    def task_create(
        self, run_id: str, title: str, spec: str, deps: Sequence[str]
    ) -> Dict[str, Any]:
        args = [
            "orchestration", "task-create", "--run", run_id,
            "--task-title", title, "--display-name", title, "--spec", spec,
        ]
        if deps:
            args.extend(["--deps", json.dumps(list(deps))])
        return self.call(args)

    def unstarted_task(self, run_id: str, task_id: str) -> Dict[str, Any]:

        receipt = self.call(["orchestration", "task-list", "--run", run_id])
        root = receipt.get("result") or receipt
        tasks = root.get("tasks")
        if root.get("runId") != run_id or not isinstance(tasks, list):
            raise DispatchError("unverifiable recovered Task inventory; refuse launch")
        matches = [task for task in tasks if isinstance(task, dict) and task.get("id") == task_id]
        if len(matches) != 1:
            raise DispatchError("recovered Task missing or ambiguous; refuse launch")
        receipt = self.call(["orchestration", "worker-list", "--run", run_id, "--include-remote"])
        root = receipt.get("result") or receipt
        if ((root.get("scope") or {}).get("run") != run_id
                or not isinstance(root.get("workers"), list)
                or (root.get("page") or {}).get("hasMore") is not False):
            raise DispatchError("incomplete recovered worker inventory; refuse launch")
        for worker in root["workers"]:
            if (not isinstance(worker, dict) or worker.get("runId") != run_id
                    or not worker.get("taskId") or worker.get("taskId") == task_id):
                raise DispatchError("recovered Task has worker or unknown launch evidence; refuse launch")
        task = dict(matches[0])
        if "deps" in task:
            try:
                deps = json.loads(task["deps"]) if isinstance(task["deps"], str) else task["deps"]
            except ValueError as exc:
                raise DispatchError("malformed recovered Task dependencies; refuse launch") from exc
            if not isinstance(deps, list) or any(not isinstance(dep, str) or not dep for dep in deps):
                raise DispatchError("malformed recovered Task dependencies; refuse launch")
            task["deps"] = deps
        return task

    def worker_start(
        self, run_id: str, task_id: str, route: Dict[str, Any], timeout_ms: int
    ) -> Dict[str, Any]:
                                                                             
        effort_args = [] if model_encoded_effort(route) else ["--effort", route["effort"]]
        return self.call(
            [
                "orchestration", "worker-start", "--run", run_id,
                "--task", task_id, "--worktree", "current",
                "--agent", route["agent"], "--model", route["launch_target"],
                *effort_args, "--timeout-ms", str(timeout_ms),
            ],
            timeout=max(90, int(timeout_ms / 1000) + 30),
        )

    def check(
        self, run_id: str, timeout_ms: int, ack: Optional[str] = None, wait: bool = True
    ) -> Dict[str, Any]:
        args = ["orchestration", "check", "--run", run_id]
        if ack:
            args.extend(["--ack", ack])
        if wait:
            args.extend([
                "--wait", "--types", "worker_done,escalation,question,heartbeat",
                "--timeout-ms", str(timeout_ms),
            ])

        result = self.command.run(
            [*self.prefix, *args, "--json"],
            timeout=max(90, int(timeout_ms / 1000) + 30),
            allow_failure=True,
        )
        payload = result.payload
        if payload.get("_keepalive") or payload.get("_heartbeat"):
            return {"messages": []}
        if payload.get("ok") is False:
            error = payload.get("error") or {}
            raise DispatchError(
                f"Orca check failed: {error.get('code', 'error')}: "
                f"{error.get('message', error)}"
            )
        return payload

    def release(self, dispatch_id: str) -> Dict[str, Any]:
        args = ["orchestration", "worker-release", "--dispatch", dispatch_id]
        first = self.call(args, allow_failure=True)
        state = str(first.get("state") or first.get("status") or "").lower()
        if state != "release_unknown":
            return first

        inspection = self.worker_show(dispatch_id)
        retry_request = hashlib.sha256(
            f"praxis-worker-release:{dispatch_id}".encode("utf-8")
        ).hexdigest()[:32]
        recovered = self.call(
            [*args, "--retry-request", retry_request], allow_failure=True)
        recovered["release_recovery"] = {
            "initial": first,
            "inspection": inspection,
            "retry_request": retry_request,
        }
        return recovered

    def stop(self, dispatch_id: str) -> Dict[str, Any]:
        return self.call(["orchestration", "worker-stop", "--dispatch", dispatch_id])

    def worker_show(self, dispatch_id: str) -> Dict[str, Any]:
        return self.call(["orchestration", "worker-show", "--dispatch", dispatch_id])

    def task_fail(self, task_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        return self.call([
            "orchestration", "task-update", "--id", task_id,
            "--status", "failed", "--result", json.dumps(result, ensure_ascii=False),
        ])

def object_id(payload: Dict[str, Any], kind: str) -> str:
    candidates: Dict[str, List[Tuple[str, ...]]] = {
        "run": [("run", "id"), ("runId",), ("run_id",)],
        "task": [("task", "id"), ("taskId",), ("task_id",)],
        "dispatch": [
            ("dispatch", "id"), ("dispatchId",), ("dispatch_id",),
            ("worker", "dispatchId"), ("worker", "dispatch_id"),
        ],
    }
    for path in candidates[kind]:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if isinstance(value, str) and value:
            return value
    raise DispatchError(f"Orca {kind} receipt did not contain a {kind} id: {payload}")

def actual_launch(receipt: Dict[str, Any], route: Dict[str, Any],
                  account: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:

    attested = {}
    if account is not None:
        attested = {
            "account": account.get("kind"),
            "account_source": account.get("unproven") or account.get("source"),
        }
    paths = [
        ("launch", "effective"),
        ("startOptions", "launch", "effective"),
        ("start_options", "launch", "effective"),
        ("worker", "startOptions", "launch", "effective"),
    ]
    for path in paths:
        value: Any = receipt
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if isinstance(value, dict):
            launch = {**value, **attested}
            encoded = model_encoded_effort(route)
            if (encoded and launch.get("agent") == "antigravity"
                    and launch.get("model") == route["launch_target"]
                    and launch.get("effort") is None):
                launch.update(effort=encoded, effort_source="model-id")
            return launch
    return {
        "agent": route["agent"],
        "model": route["launch_target"],
        "effort": route["effort"],
        "receipt_missing_effective_launch": True,
        **attested,
    }

def validate_actual_launch(launch: Dict[str, Any], route: Dict[str, Any]) -> None:

    expected = {
        "agent": route["agent"],
        "model": route["launch_target"],
        "effort": route["effort"],
    }
    mismatches = [
        f"{key}: expected {expected[key]!r}, got {launch.get(key)!r}"
        for key in expected if launch.get(key) != expected[key]
    ]
    if launch.get("receipt_missing_effective_launch"):
        mismatches.append("worker-start receipt did not attest the effective launch")
    required = route.get("requires_account")
    if required and launch.get("account") != required:
        mismatches.append(
            f"account: {route['launch_target']} is entitled on the {required} "
            f"account, and the account attested for this worker is "
            f"{launch.get('account') or 'unproven'} "
            f"({launch.get('account_source') or 'not attested'})")
    if mismatches:
        raise DispatchError("Orca launch attestation failed: " + "; ".join(mismatches))

def payload_json(message: Dict[str, Any]) -> Dict[str, Any]:
    raw = message.get("payload")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}

def delivery_id(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("deliveryId", "delivery_id"):
        if isinstance(payload.get(key), str):
            return payload[key]
    delivery = payload.get("delivery")
    if isinstance(delivery, dict) and isinstance(delivery.get("id"), str):
        return delivery["id"]
    return None

def validate_return(body: str, contract: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    if not body.strip():
        reasons.append("empty worker_done body")
    minimum = int(contract.get("min_chars", 1))
    maximum = int(contract.get("max_chars", 20000))
    if len(body) < minimum:
        reasons.append(f"body shorter than {minimum} characters")
    if len(body) > maximum:
        reasons.append(f"body longer than {maximum} characters")
    for required in contract.get("required_substrings", []):
        if required not in body:
            reasons.append(f"missing required substring: {required!r}")
    for forbidden in contract.get("forbidden_substrings", []):
        if forbidden in body:
            reasons.append(f"contains forbidden substring: {forbidden!r}")
    return not reasons, reasons

def retained_worker_body(body: str, contract: Dict[str, Any]) -> Dict[str, Any]:

    maximum = int(contract.get("max_chars", 20000))
    if len(body) <= maximum:
        return {"body": body}
    head_chars = OVERSIZED_BODY_PREVIEW_CHARS // 2
    tail_chars = OVERSIZED_BODY_PREVIEW_CHARS - head_chars
    marker = (
        "\n\n[oversized worker_done omitted from bundle; "
        f"original_chars={len(body)}]\n\n"
    )
    encoded = body.encode("utf-8")
    return {
        "body": body[:head_chars] + marker + body[-tail_chars:],
        "body_truncated": True,
        "body_retention": {
            "status": "truncated_oversized",
            "original_chars": len(body),
            "original_bytes": len(encoded),
            "original_sha256": hashlib.sha256(encoded).hexdigest(),
            "head_chars": head_chars,
            "tail_chars": tail_chars,
        },
    }

def verify_outputs(
    unit: Dict[str, Any], worker_done: Dict[str, Any],
    declared_outcome: Optional[str] = None,
) -> Tuple[bool, List[Dict[str, Any]], List[str]]:
    checks: List[Dict[str, Any]] = []
    reasons: List[str] = []
    planning_contract = unit.get("planning_output")
    if planning_contract:
        try:
            from planning_contract import validate as validate_planning_output
            validate_planning_output(planning_contract)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            reasons.append(f"planning output rejected: {exc}")
    for declared in unit.get("outputs", []):
        raw = declared if isinstance(declared, str) else declared.get("path")
        gate = None if isinstance(declared, str) else declared.get("required_when_outcome")
        if gate:

            required = declared_outcome in [str(item) for item in gate]
        else:
            required = True if isinstance(declared, str) else declared.get("required", True)
        path = Path(str(raw)).expanduser()
        exists = path.exists()
        row: Dict[str, Any] = {
            "path": str(path), "required": bool(required), "exists": exists,
        }
        if gate:
            row["required_when_outcome"] = [str(item) for item in gate]
            row["declared_outcome"] = declared_outcome
        if exists and path.is_file():
            row.update({"sha256": file_digest(path), "bytes": path.stat().st_size})
            if isinstance(declared, dict) and declared.get("kind") == "correction-packet":
                try:
                    import importlib.util
                    script = Path(__file__).resolve().parents[1] / "memory-refresh/layer_maintenance.py"
                    spec = importlib.util.spec_from_file_location("dispatch_correction_packet", script)
                    validator = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(validator)
                    validator.validate_correction_packet(json.loads(path.read_text()))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    reasons.append(f"correction packet rejected: {exc}")
            if isinstance(declared, dict) and declared.get("kind") == "canonical-reconciliation":
                try:
                    maintenance = json.loads(path.read_text())
                    if not isinstance(maintenance, dict):
                        raise ValueError("maintenance receipt must be an object")
                    if declared_outcome == "ran" and (maintenance.get("reconciliation_complete") is not True
                            or maintenance.get("baseline_status") not in {"advanced", "already_advanced"}
                            or maintenance.get("diff_reviewed") is not True
                            or maintenance.get("owner_writes_verified") is not True):
                        raise ValueError("required reconciliation is incomplete")
                    if maintenance.get("outcome") != declared_outcome:
                        raise ValueError("maintenance receipt contradicts declared outcome")
                except (OSError, ValueError, TypeError) as exc:
                    reasons.append(f"maintenance output rejected: {exc}")
        checks.append(row)
        if required and not exists:
            reasons.append(f"declared output does not exist: {path}")
        delivery = declared.get("delivery") if isinstance(declared, dict) else None
        if required and delivery:
            try:

                scripts = Path(__file__).resolve().parents[2] / "scripts"
                if not (scripts / "artifact_delivery.py").is_file():
                    scripts = Path.home() / ".claude/scripts"
                previous = sys.dont_write_bytecode
                sys.dont_write_bytecode = True
                sys.path.insert(0, str(scripts))
                try:
                    from artifact_delivery import verify_delivery
                    receipt = verify_delivery(
                        path, receipt_dir=Path(delivery["receipt_dir"]).expanduser(),
                        require_viewer=delivery.get("require_viewer", True),
                        worktree=delivery.get("worktree"),
                    )
                finally:
                    sys.path.pop(0)
                    sys.dont_write_bytecode = previous
                row["delivery"] = receipt
                if receipt["markdown"] not in str(worker_done.get("body") or ""):
                    raise ValueError("worker_done omits the checked artifact link")
            except (ImportError, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                reasons.append(f"artifact delivery incomplete: {path}: {exc}")
    contract = unit.get("return_contract") or {}
    report_path = worker_done.get("report_path")
    if contract.get("require_report_path"):
        if not report_path:
            reasons.append("worker_done did not declare report_path")
        else:
            path = Path(str(report_path)).expanduser()
            exists = path.is_file()
            row = {"path": str(path), "required": True, "exists": exists, "kind": "report_path"}
            if exists:
                row.update({"sha256": file_digest(path), "bytes": path.stat().st_size})
            checks.append(row)
            if not exists:
                reasons.append(f"worker_done report_path does not exist: {path}")
    return not reasons, checks, reasons

def release_resolved(receipt: Dict[str, Any]) -> bool:
    state = str(receipt.get("state") or receipt.get("status") or "").lower()
    return state in {"released", "retained", "no_owned_resource"}

def checker_lifecycle_text(unit):

    import shlex
    output=unit.get('outputs',[{}])[0].get('path')
    argv=['orca','orchestration','send','--from','<INJECTED_TERMINAL>','--type','worker_done',
          '--subject','Receipt','--body','<THREE_SENTENCES_WITH_REAL_LF>',
          '--task-id','<INJECTED_TASK>','--dispatch-id','<INJECTED_DISPATCH>','--outcome','<succeeded_or_failed>']
    if output:argv+=['--files-modified',output,'--report-path',output]
    command=shlex.join(['python3','-I','-c','import subprocess; subprocess.run('+repr(argv)+', shell=False, check=True)'])
    return ('CHECKER EXACT LIFECYCLE CONTRACT. This replaces every generic completion example in the injected preamble. '
            'Prepare and inspect the nonempty body, real LF newlines and required closing lines before the single send; limit 3500 characters. '
            'The only completion form is the following two-statement program with a literal argument list; replace the four injected identity/outcome placeholders and the body literal with actual values, preserving Python literal escaping. '
            'Construct the body as data with real LF characters; insert repr(body) into the literal argv and render the complete outer command with shlex.join. This deterministically preserves dollar signs, backticks, apostrophes, quotes and newlines. Do not execute a construction helper or add statements, substitutions, body-file reads or polling. The generic preamble plain-shell worker_done example is refused for this restricted checker; use only CHECKER_COMPLETION_COMMAND. Empty write_stdin polling is also refused.\n'
            'CHECKER_COMPLETION_COMMAND: '+command+'\n'
            'Exact heartbeat: orca orchestration send --from <INJECTED_TERMINAL> --type heartbeat --subject alive --task-id <INJECTED_TASK> --dispatch-id <INJECTED_DISPATCH> --phase reviewing\n'
            'Exact check: orca orchestration check --terminal <INJECTED_TERMINAL> --json\n'
            'Exact escalation: use the same literal two-statement subprocess program with argv [orca, orchestration, send, --from, injected terminal, --type, escalation, --subject, nonempty subject, --body, nonempty body, --task-id, injected task, --dispatch-id, injected dispatch]. '
            'Do not ask or block; report unmet requirements in completion. Bootstrap reads are only a single cat of a frozen checker_bootstrap path whose bytes still match. '
            'All research/control access uses the bound helper. The only non-helper writes are Write with exactly file_path/content or the two-statement literal pathlib write_text to the declared output; self-check uses only the exact bound validate-output argv. '
            'No tool aliases, extra input keys, unknown record types, background commands, chained calls or skill-resource reads outside frozen helper controls are allowed.')

def unit_spec(plan: Dict[str, Any], unit: Dict[str, Any], route: Dict[str, Any],
              context_receipt: Optional[Dict[str, Any]] = None) -> str:
    parts = [f"PLAN {plan.get('id', 'unnamed')} — UNIT {unit['id']}"]
    boundary = named_launch_module()
    try:
        context, manifest = boundary.construct(unit, route["agent"])
    except boundary.NamedLaunchError as exc:
        raise DispatchError(str(exc)) from exc
    if context:
        parts.append(context)
    if context_receipt is not None and manifest is not None:
        context_receipt.update(manifest)
    if unit.get("spec"):

        parts.append(str(unit["spec"]).strip())
    else:
        skill = unit["skill"]
        root = "~/.claude/skills" if route["agent"] == "claude" else "~/.agents/skills"
        parts.extend([
            f"Load and obey {root}/{skill}/SKILL.md before doing the task.",
            str(unit.get("objective") or unit.get("title") or f"Execute the {skill} worker contract."),
        ])
    common = str(plan.get("worker_preamble") or "").strip()
    if common:
        parts.append(common)
    instructions = unit.get("instructions") or []
    if isinstance(instructions, str):
        instructions = [instructions]
    parts.extend(str(item) for item in instructions if str(item).strip())
    if any(isinstance(item, dict) and item.get("delivery") for item in unit.get("outputs", [])):
        parts.append(
            "HARD ARTIFACT DELIVERY CONTRACT. Publishing a file alone is not completion. "
            "Use the fixed publisher's checked Markdown link verbatim in worker_done; a bare path is rejected. "
            "The publisher writes a hash-bound delivery receipt. If it is missing, stale, or reports an error, "
            "report failed and distinguish created from delivered. Do not fabricate a receipt. "

        )

    parts.append(
        "Information access follows the request scope and configured source permissions. Report missing access or evidence explicitly. "
        "Source access alone grants no action authority. Perform writes or publication only within this unit's request-bound scope and the actual user's authorization. A read-only or prepare-only unit remains so. Never infer permission to send, submit, sign off, spend, grade, publish, or change a calendar commitment from source access or a graph alone."
    )

    parts.append(
        "Follow the interaction contract configured for this request. Complete work within existing authorization. "
        "If required authority or input is unavailable, report a held or refused outcome with the unresolved requirement; "
        "do not invent an approval or claim completion."
    )
    if unit.get('capability')=='praxis_native_claim_check':
        parts.append(checker_lifecycle_text(unit))
    else:
        parts.append(
            "Report exactly once through the injected Orca worker_done lifecycle with an explicit succeeded or failed outcome. "
            "Pass the completed body directly to Orca. Never send through an unset shell variable, a placeholder, or a command that has not been checked for the actual body text. "
            "Build multiline text with real LF characters (for example, Python chr(10).join(lines)), not literal backslash-n text in a shell-quoted string. "
            "Pass that same string as one --body argument using Python subprocess.run with an argument list and shell=False, preserving the injected lifecycle identity and capability arguments. "
            "Before sending, inspect body.splitlines(): verify that the exact body is non-empty, within the length ceiling, and includes every required closing line. "
            "For a declared outcome, require exactly one matching line with an allowed value; a bare prefix is not a closing line. "
            "Fix formatting before the single send; never rewrite a collected report or infer its outcome. Do not start unrelated work after reporting."
        )

    contract = unit.get("return_contract") or {}
    outcome_spec = outcome_contract(unit)
    required = (contract.get("required_substrings") or [])
    required = [str(item) for item in required if str(item).strip()]

    if outcome_spec:
        required = [item for item in required
                    if item.strip() != str(outcome_spec.get("prefix") or "").strip()]
    if required:
        quoted = "\n\n".join(f"    {item}" for item in required)
        parts.append(
            "HARD RETURN CONTRACT. Your worker_done body is rejected unless it contains the following text verbatim, "
            "each on its own line, after everything else you write. Write your summary first, then append this and re-read it:"
            f"\n\n{quoted}"
        )
    if outcome_spec:

        allowed = [str(item) for item in (outcome_spec.get("allowed") or [])]
        holds = [str(item) for item in (outcome_spec.get("hold") or [])]
        unchanged = [str(item) for item in (outcome_spec.get("no_change") or [])]
        substantive = [str(item) for item in (outcome_spec.get("substantive") or [])]
        lines = [
            "HARD OUTCOME CONTRACT. Exactly one line of your body must read "
            f"`{outcome_spec.get('prefix')} <outcome>`, where <outcome> is one of: "
            + ", ".join(f"`{item}`" for item in allowed) + ". "
            "Write it verbatim, lowercase, with no other wording on the line, and write it once. "
            "Your body is rejected if the line is missing, if it names anything outside that list, "
            "or if two copies of it disagree.",
            "This verdict is read mechanically and it is not the exit status. "
            f"Report {', '.join(repr(item) for item in substantive)} only when this run actually did the work; "
            "a run that found the work already done, that a guard stopped, or that completed its pass "
            "and found nothing to do reports that instead. "
            "Calling a skip a completed cycle is the error this contract exists to prevent.",
        ]
        if holds:
            lines.append(
                f"A {', '.join(repr(item) for item in holds)} outcome is a correct, complete finish. "
                "It is recorded as its own status rather than as a success, so you do not need to "
                "produce work to close the lane, and you must not produce work in order to avoid it.")
        if unchanged:

            lines.append(
                f"A {', '.join(repr(item) for item in unchanged)} outcome is also a correct, "
                "complete finish: report it when the pass ran in full and there was genuinely "
                "nothing to change. It records that coverage happened and that the delta was zero. "
                "Do not manufacture work to avoid it, do not report it when a guard stopped you "
                f"before the pass ran, and never report "
                f"{', '.join(repr(item) for item in substantive)} for a pass that changed nothing.")
        for declared in unit.get("outputs", []):
            gate = None if isinstance(declared, str) else declared.get("required_when_outcome")
            if gate:
                gate = [str(item) for item in gate]
                lines.append(
                    f"The file `{declared.get('path')}` is verified and hashed when you report "
                    + " or ".join(f"`{item}`" for item in gate)
                    + ". Report "
                    + ("that outcome" if len(gate) == 1
                       else "either of those" if len(gate) == 2 else "any of those")
                    + " only when that exact path exists.")
        parts.append("\n\n".join(lines))
    maximum = contract.get("max_chars")
    if maximum:

        budget = max(200, int(maximum) - 500)
        parts.append(
            f"HARD LENGTH CEILING. Your entire worker_done body, including the required "
            f"closing lines above, is rejected at more than {int(maximum)} characters. "
            f"Aim for at most {budget} characters and count before you send. "
            f"To fit: give exact counts instead of enumerating every instance, keep the "
            f"oldest and highest-consequence examples with their dates and source paths, "
            f"drop repeated framing and restated method, and write one line per finding. "
            f"Compress detail; never drop a required substring, a mandatory evidence "
            f"category, or a source path. If the full detail cannot fit, say which "
            f"category you compressed and where the fuller evidence lives."
        )
    if contract.get("require_report_path"):
        parts.append(
            "Set the worker_done report path to the exact output path this unit names. "
            "A missing report path fails the contract even when the work succeeded."
        )
    return "\n\n".join(parts)

def topological_order(plan: Dict[str, Any]) -> List[str]:
    units = {unit["id"]: unit for unit in plan["units"]}
    ordered: List[str] = []
    remaining = list(units)
    while remaining:
        progressed = False
        for unit_id in list(remaining):
            if all(dep in ordered for dep in units[unit_id].get("depends_on", [])):
                ordered.append(unit_id)
                remaining.remove(unit_id)
                progressed = True
        if not progressed:
            raise DispatchError("dependency graph could not be ordered")
    return ordered

def compact_route(route: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "task_class", "requested_model", "route_primary", "effective_model", "launch_target",
        "provider", "agent", "effort", "reason", "fallback", "sensitivity",
        "risk", "gates", "benchmark_waived", "pilot_route", "pilot_decision",
        "pilot_expires", "requires_account", "worker_account",
        "pilot_expired", "pilot_expired_decision", "pilot_expired_on", "pilot_rollback",
        "producer_model", "excluded_for_independence", "_audit",
    ]
    return {key: route.get(key) for key in keys if key in route}

def maintenance_status(units: dict, state: dict) -> dict:
    required = [key for key, unit in units.items() if unit.get("required_maintenance") is True]
    blockers = [{"unit": key, "owner": units[key].get("maintenance_owner", key),
                 "status": state.get(key, {}).get("status", "missing"),
                 "blocker": (state.get(key, {}).get("worker_done") or {}).get("body")
                            or "; ".join(state.get(key, {}).get("unresolved", [])) or "required step did not complete",
                 "next_step": units[key].get("maintenance_next_step", "review required maintenance receipt")}
                for key in required if state.get(key, {}).get("status") != "succeeded"]
    return {"required_units": required, "complete": not blockers, "blockers": blockers}

def execute(
    plan: Dict[str, Any], router: RouterClient, orca: OrcaClient,
    *, dry_run: bool, log_routes: bool, run_key: Optional[str] = None,
    lease: Optional[RunLease] = None, plan_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    error_context: Optional[Dict[str, Any]] = None,
    receipt_observer: Optional[Any] = None,
    collection_supervisor: Optional[Any] = None,
    observer_fault_policy: str = "propagate",
) -> Dict[str, Any]:
    observer_results = []
    if observer_fault_policy not in {"propagate", "record_auxiliary"}:
        raise DispatchError("unsupported receipt observer fault policy")
    if receipt_observer and observer_fault_policy == "record_auxiliary":
        original_observer = receipt_observer

        def receipt_observer(stage, payload):

            if stage not in {"launch", "settled"}:
                return original_observer(stage, payload)
            try:
                result = original_observer(stage, payload)
            except Exception as exc:
                result = {"state": "unknown", "stage": stage,
                          "reason": type(exc).__name__ + ": " + str(exc)}
            if result is not None:
                observer_results.append(result)
            return result

    boundary = named_launch_module()
    try:
        if boundary.RESERVED.intersection(plan):
            raise boundary.NamedLaunchError("caller-supplied named-seat contract refused")
        for unit in plan["units"]:
            boundary.seat_for_unit(unit)
    except boundary.NamedLaunchError as exc:
        raise DispatchError(str(exc)) from exc
    launch_plan_digest = canonical_digest(plan)
    started = now_iso()
    defaults = plan.get("defaults") or {}
    start_timeout_ms = int(defaults.get("start_timeout_ms", 60000))
    wait_window_ms = min(int(defaults.get("wait_window_ms", 45000)), 60000)
    overall_timeout_ms = int(defaults.get("overall_timeout_ms", 900000))
    praxis_task_id = plan.get("praxis_task_id")
    units = {unit["id"]: unit for unit in plan["units"]}
    order = topological_order(plan)
    state: Dict[str, Dict[str, Any]] = {}
    source = {
        "plan_path": str(plan_path) if plan_path else None,
        "plan_sha256": file_digest(plan_path) if plan_path and plan_path.is_file() else canonical_digest(plan),
        "dispatcher_path": str(Path(__file__).resolve()),
        "dispatcher_sha256": file_digest(Path(__file__).resolve()),
    }

    for unit_id in order:
        unit = units[unit_id]
        route = lease.recovered_route(unit_id) if lease and not dry_run else None
        route_source = "recovered" if route else "router"
        if not route:

            produced_by = [
                (state.get(dep) or {}).get("route", {}).get("effective_model")
                for dep in unit.get("depends_on") or []
            ]
            producer_model = ",".join(sorted({m for m in produced_by if m})) or None

            extra = {"producer_model": producer_model} if producer_model else {}
            route = router.route(unit, praxis_task_id, log_routes and not dry_run,
                                 run_key=run_key, **extra)
        if (route.get("provider") not in PROVIDER_AGENTS or
                PROVIDER_AGENTS[route["provider"]] != route.get("agent")):
            raise DispatchError("unsupported or mismatched routed provider/agent; refusing before Orca creation")
        decision_id = canonical_digest({
            "run_key": run_key, "unit_id": unit_id, "route": compact_route(route),
        })[:24]
        if lease and not dry_run:
            lease.record_route(unit_id, route, decision_id)
        state[unit_id] = {
            "id": unit_id,
            "title": unit.get("title") or unit_id,
            "depends_on": list(unit.get("depends_on", [])),
            "route": compact_route(route),
            "route_decision_id": decision_id,
            "route_decision_digest": canonical_digest(compact_route(route)),
            "route_source": route_source,
            "status": "routed",
            "task_id": None,
            "dispatch_id": None,
            "actual_launch": None,

            "provider_session": None,
            "usage": None,
            "worker_done": None,
            "contract": None,
            "outputs": [],
            "release": None,

            "cleanup": "not_reached",
            "unresolved": [],
            "telemetry": [{
                "stage": "route", "ts": now_iso(), "route_decision_id": decision_id,
                "source": route_source,
            }],
        }
        state[unit_id]["_route_full"] = route
        emit(
            "routed", unit=unit_id, provider=route["provider"],
            model=route["effective_model"], effort=route["effort"],
        )

    prepared_specs = {}
    for unit_id in order:
        context_receipt = {}
        spec = unit_spec(plan, units[unit_id], state[unit_id]["_route_full"], context_receipt)
        prepared_specs[unit_id] = spec
        state[unit_id]["prepared_spec_sha256"] = hashlib.sha256(spec.encode()).hexdigest()
        if context_receipt:
            state[unit_id]["prepared_context"] = context_receipt

    if error_context is not None:
        error_context.update({
            "schema_version": SCHEMA_VERSION,
            "mode": "run",
            "plan_id": plan.get("id"),
            "routine_run_key": run_key,
            "objective": plan["objective"],
            "praxis_task_id": praxis_task_id,
            "started_at": started,
            "source": source,
            "state": state,
            "order": order,
        })

    if receipt_observer and not dry_run:
        receipt_observer("prepared", {"units": [{"id": key, "spec_sha256": state[key]["prepared_spec_sha256"],
            "context": state[key].get("prepared_context", {})} for key in order]})

    if dry_run:
        preview = []
        for unit_id in order:
            route = state[unit_id].pop("_route_full")
            state[unit_id]["spec"] = prepared_specs[unit_id]
            preview.append(state[unit_id])
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "dry-run",
            "plan_id": plan.get("id"),
            "routine_run_key": run_key,
            "objective": plan["objective"],
            "source": source,
            "started_at": started,
            "completed_at": now_iso(),
            "status": "validated",
            "units": preview,
        }

    status = orca.status()
    runtime = status.get("runtime") or status
    if runtime.get("state") not in (None, "ready") or runtime.get("reachable") is False:
        raise DispatchError(f"Orca runtime is not ready: {status}")

    active: Dict[str, str] = {}
    recovered_workers = lease.recovered_workers() if lease else {}
    recovered_tasks = lease.recovered_tasks() if lease else {}
    prior_run_id = str((lease.recovery_state if lease else {}).get("run_id") or "")
    recovering_run = bool(prior_run_id)

    if (recovered_workers or recovered_tasks) and not prior_run_id:
        raise DispatchError(
            "stale lease retained Orca tasks or workers but no Run id; refusing "
            "to create a second Run because the prior attempt cannot be identified"
        )

    if recovering_run:

        orca.run_use(prior_run_id)
        run_id = prior_run_id
        emit("run_recovered", run_id=run_id, objective=plan["objective"])

        def recover_unit(unit_id: str) -> None:
            record = state[unit_id]
            prior_worker = recovered_workers.get(unit_id) or {}
            prior_task = recovered_tasks.get(unit_id) or {}
            task_id = str(prior_worker.get("task_id") or prior_task.get("task_id") or "")
            if not task_id:
                raise DispatchError(
                    f"stale lease has no task identity for recovered unit {unit_id}"
                )
            record["task_id"] = task_id

            binding = prior_task.get("prompt_binding") or {}
            if record.get("prepared_context") and isinstance(binding, dict):

                record["context_provenance"] = ("recovered_task_binding" if binding else
                                                 "unverified_prior_task")
                record["spec_sha256"] = binding.get("spec_sha256")

            dispatch_id = str(prior_worker.get("dispatch_id") or "")
            record["prior_task_status"] = prior_task.get("status")
                                                                             
            record["dispatch_id"] = dispatch_id or None
            if not dispatch_id:
                if (prior_task.get("status") != "created" or prior_worker
                        or (lease.recovery_state.get("task_id") or lease.recovery_state.get("dispatch_id"))):
                    raise DispatchError("recovered Task is not proven created without launch evidence; refuse launch")
                if record.get("prepared_context") and not binding:

                    raise DispatchError("legacy named Task prompt binding missing; refuse launch")
                task = orca.unstarted_task(run_id, task_id)

                if (not isinstance(task, dict) or task.get("id") != task_id or task.get("run_id") != run_id
                        or task.get("status") not in ({"ready", "pending"} if record["depends_on"] else {"ready"}) or task.get("dispatch_id")
                        or task.get("assignee_handle") or task.get("result") is not None):
                    raise DispatchError("authoritative recovered Task is not unstarted and ready; refuse launch")
                if (not isinstance(binding, dict)
                        or binding.get("spec_sha256") != record["prepared_spec_sha256"]
                        or binding.get("context") != record.get("prepared_context", {})
                        or binding.get("route_sha256") != canonical_digest(record["_route_full"])
                        or binding.get("source") != source
                        or not isinstance(task.get("spec"), str)
                        or hashlib.sha256(task["spec"].encode()).hexdigest() != binding["spec_sha256"]):
                    raise DispatchError("recovered Task context, model, prompt or source binding mismatch; refuse launch")
                for source_binding in units[unit_id].get("source_bindings", []):
                    path = Path(source_binding["path"])
                    if (path.is_symlink() or not path.is_file()
                            or file_digest(path) != source_binding["sha256"]):
                        raise DispatchError("recovered Task bound source changed; refuse launch")
                expected_deps = [recovered_tasks.get(dep, {}).get("task_id") for dep in record["depends_on"]]
                if "deps" in task and (any(not isinstance(dep, str) for dep in expected_deps)
                                       or sorted(task["deps"]) != sorted(expected_deps)):
                    raise DispatchError("recovered Task dependency binding mismatch; refuse launch")
                if record["depends_on"]:
                                                                                   
                    record["status"] = "held_dependent"
                    record["unresolved"].append(
                        "dependent Task held on original Run/Task; supervisor must "
                        "reconcile original identities and authorize continuation separately")
                    record["telemetry"].append({
                        "stage": "recovery_task", "ts": now_iso(),
                        "task_id": task_id, "action": "held_dependent",
                        "prior_task_status": prior_task.get("status"),
                        "authoritative_task_status": task.get("status"),
                    })
                    return
                record["status"] = "created"
                record["telemetry"].append({
                    "stage": "recovery_task", "ts": now_iso(),
                    "task_id": task_id, "action": "resume_unstarted_task",
                })
                return

            if not isinstance(binding, dict):
                raise DispatchError("malformed recovered Task prompt binding; refuse collection")
            receipt = orca.worker_show(dispatch_id)
            receipt_root = receipt.get("result") or receipt
            worker = receipt_root.get("worker") or receipt_root
            receipt_dispatch = receipt_root.get("dispatch") or {}
            worker_state = str(
                worker.get("state") or worker.get("status") or "outcome_unknown"
            ).lower()
            receipt_dispatch_id = str(
                receipt_dispatch.get("id") or worker.get("dispatch_id") or
                worker.get("dispatchId") or ""
            )
            receipt_task_id = str(
                receipt_dispatch.get("task_id") or receipt_dispatch.get("taskId") or
                worker.get("task_id") or worker.get("taskId") or ""
            )
            receipt_run_id = str(
                receipt_dispatch.get("run_id") or receipt_dispatch.get("runId") or
                worker.get("run_id") or worker.get("runId") or prior_run_id
            )
            exact_worker = (receipt_root.get("observation") or {}).get("exactWorker") is True
            identity_matches = (
                exact_worker and receipt_dispatch_id == dispatch_id
                and receipt_task_id == task_id and receipt_run_id == prior_run_id
            )
            event = {
                "stage": "recovery_inspection", "ts": now_iso(),
                "prior_run_id": prior_run_id,
                "prior_dispatch_id": dispatch_id,
                "receipt_dispatch_id": receipt_dispatch_id,
                "receipt_task_id": receipt_task_id,
                "receipt_run_id": receipt_run_id,
                "exact_worker": exact_worker,
                "identity_matches": identity_matches,
                "worker_state": worker_state,
                "receipt_sha256": canonical_digest(receipt),
                "single_attempt_per_lane": True,
            }
            record["telemetry"].append(event)
            if not identity_matches:
                event["action"] = "identity_refused"
                raise DispatchError(
                    f"Orca recovery identity mismatch for {unit_id}: prior "
                    f"{prior_run_id}/{task_id}/{dispatch_id}, receipt "
                    f"{receipt_run_id}/{receipt_task_id}/{receipt_dispatch_id}"
                )

            recovered_launch = prior_worker.get("actual_launch") or actual_launch(
                receipt, record["_route_full"]
            )

            recovery_route = dict(record["_route_full"])
            recovery_route.pop("requires_account", None)
            validate_actual_launch(recovered_launch, recovery_route)
            record["dispatch_id"] = dispatch_id
            if binding.get("context"):
                record["supplied_context"] = binding["context"]
            record["actual_launch"] = recovered_launch
            record["provider_session"] = prior_worker.get("provider_session")

            settlement = prior_worker.get("settlement")
            if settlement is not None:

                if (not isinstance(settlement, dict)
                        or settlement.get("run_id") != run_id
                        or settlement.get("task_id") != task_id
                        or settlement.get("dispatch_id") != dispatch_id
                        or settlement.get("prompt_binding") != binding
                        or not isinstance(settlement.get("record"), dict)
                        or settlement.get("record_sha256") != canonical_digest(settlement["record"])
                        or settlement["record"].get("status") != prior_worker.get("status")
                        or not isinstance(settlement["record"].get("worker_done"), dict)):
                    raise DispatchError("malformed or mismatched saved settlement; refuse collection")
                record.update(settlement["record"])
                event["action"] = "reuse_consumed_original"
                return

            if worker_state in {"failed", "stopped"}:
                event["action"] = "settled_without_retry"
                record["status"] = "failed"
                record["contract"] = {
                    "accepted": False,
                    "reasons": [
                        f"Orca retained prior worker state {worker_state} without an accepted worker_done"
                    ],
                }
                record["unresolved"].append(
                    f"prior Orca worker ended {worker_state}; no replacement was launched"
                )
                try:
                    record["release"] = orca.release(dispatch_id)
                except DispatcherInterrupted:
                    raise
                except DispatchError as exc:
                    record["release"] = {"ok": False, "error": str(exc)}
                record["cleanup"] = (
                    "resolved" if release_resolved(record["release"]) else "unresolved"
                )
                if record["cleanup"] == "unresolved":
                    record["unresolved"].append(
                        f"terminal cleanup unresolved: {record['release']}"
                    )
                if lease:
                    lease.record_worker(
                        unit_id, task_id, dispatch_id, "failed",
                        provider_session=record["provider_session"],
                        launch=record["actual_launch"],
                    )
                return

            event["action"] = "rebound_prior_run"
            record["status"] = "dispatched"
            active[dispatch_id] = unit_id

        for unit_id in order:
            try:
                recover_unit(unit_id)
            except (LeasePersistenceError, DispatcherInterrupted):

                raise
            except (DispatchError, subprocess.TimeoutExpired) as exc:
                record = state[unit_id]
                record["status"] = "recovery_unresolved" if isinstance(
                    exc, (CommandError, CommandReadError, subprocess.TimeoutExpired)) else "recovery_refused"
                record["unresolved"].append(str(exc))
                record["telemetry"].append({
                    "stage": "recovery_task", "ts": now_iso(),
                    "task_id": record.get("task_id"), "action": "refused",
                    "reason": str(exc),
                })

    else:
        if lease:
            lease.update(run_creation_pending=True)
        run_receipt = orca.run_create(plan["objective"])
        run_id = object_id(run_receipt, "run")
        if receipt_observer:
            receipt_observer("run", {"run_id": run_id, "receipt": run_receipt})
        if lease:
            lease.update(run_id=run_id, run_creation_pending=False)
        emit("run_created", run_id=run_id, objective=plan["objective"])

        for unit_id in order:
            record = state[unit_id]
            route = record["_route_full"]
            deps = ([] if units[unit_id].get("depends_on_terminal") else
                    [state[dep]["task_id"] for dep in record["depends_on"]])
            spec = prepared_specs[unit_id]
            if lease:
                lease.update(task_creation_pending=unit_id)
            receipt = orca.task_create(run_id, record["title"], spec, deps)
            record["task_id"] = object_id(receipt, "task")
            if receipt_observer:
                receipt_observer("task", {"run_id": run_id, "task_id": record["task_id"], "unit_id": unit_id, "receipt": receipt})
            record["status"] = "created"
            record["spec_sha256"] = record["prepared_spec_sha256"]
            if lease:
                binding = {"spec_sha256": record["spec_sha256"],
                           "context": record.get("prepared_context", {}),
                           "route_sha256": canonical_digest(route), "source": source}
                lease.record_task(unit_id, record["task_id"], "created", prompt_binding=binding)
                lease.update(task_creation_pending=None)
            record["telemetry"].append({
                "stage": "task", "ts": now_iso(), "task_id": record["task_id"]
            })
            emit("task_created", unit=unit_id, task_id=record["task_id"], deps=deps)

    if receipt_observer and not recovering_run:
        receipt_observer("tasks_created", {"run_id": run_id,
            "tasks": {key: state[key]["task_id"] for key in order}})

    def write_dependency_snapshot(unit_id: str) -> None:
        if not output_path:
            raise DispatchError(
                f"unit {unit_id} needs an --output path for its dependency snapshot")
        dependencies = state[unit_id]["depends_on"]
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "mode": "dependency_snapshot",
            "plan_id": plan.get("id"),
            "routine_run_key": run_key,
            "run_id": run_id,
            "started_at": started,
            "captured_at": now_iso(),
            "source": source,
            "for_unit": unit_id,
            "units": [
                {key: value for key, value in state[dep].items() if key != "_route_full"}
                for dep in dependencies
            ],
        }
        atomic_write_json(output_path, snapshot)
        record = state[unit_id]
        record["dependency_snapshot"] = {
            "path": str(output_path),
            "sha256": file_digest(output_path),
            "units": dependencies,
        }
        emit("dependency_snapshot_written", unit=unit_id, path=str(output_path),
             sha256=record["dependency_snapshot"]["sha256"])

    def start_ready() -> int:
        count = 0
        for unit_id in order:
            record = state[unit_id]
            if record["status"] != "created":
                continue
            dep_states = [state[dep]["status"] for dep in record["depends_on"]]
            terminal_dependency = bool(units[unit_id].get("depends_on_terminal"))
            if not terminal_dependency and any(
                dep_state in TERMINAL_OUTCOMES - {"succeeded"} for dep_state in dep_states
            ):
                record["status"] = "blocked"
                record["unresolved"].append("dependency did not succeed")
                if units[unit_id].get('source_bindings'):
                    record['no_launch'] = {'reason': 'dependency did not succeed',
                                           'depends_on': record['depends_on']}
                try:
                    orca.task_fail(record["task_id"], {"reason": "dependency did not succeed"})
                except DispatcherInterrupted:
                    raise
                except DispatchError as exc:
                    record["unresolved"].append(f"task status update failed: {exc}")
                emit("unit_blocked", unit=unit_id, reason="dependency did not succeed")
                continue
            if terminal_dependency and not all(
                dep_state in TERMINAL_OUTCOMES for dep_state in dep_states
            ):
                continue
            if not terminal_dependency and not all(dep_state == "succeeded" for dep_state in dep_states):
                continue

            planning_dependency = bool(units[unit_id].get("planning_output") or any(
                units[dep].get("planning_output") for dep in record["depends_on"]))
            changed = []
            if planning_dependency or units[unit_id].get('source_bindings'):
                for dep in record["depends_on"]:
                    for checked in state[dep].get("outputs") or []:
                        prior_hash = checked.get("sha256")
                        if prior_hash and (not Path(checked["path"]).is_file() or
                                           file_digest(Path(checked["path"])) != prior_hash):
                            changed.append(checked["path"])
                    dependency_contract = units[dep].get("planning_output")
                    if dependency_contract:
                        try:
                            from planning_contract import validate as validate_planning_output
                            validate_planning_output(dependency_contract)
                        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                            changed.append(f"{dep} planning input: {exc}")
            for binding in units[unit_id].get('source_bindings', []):
                path = Path(binding['path'])
                try:
                    if path.is_symlink() or not path.is_file() or file_digest(path) != binding['sha256']:
                        changed.append(str(path))
                except OSError:
                    changed.append(str(path))
            if changed:
                record["status"] = "blocked"
                record["unresolved"].append("bound source or accepted dependency output changed: " + ", ".join(changed))
                try:
                    orca.task_fail(record["task_id"], {"reason": "bound source or accepted dependency output changed"})
                except DispatcherInterrupted:
                    raise
                except DispatchError as exc:
                    record["unresolved"].append(f"task status update failed: {exc}")
                emit("unit_blocked", unit=unit_id, reason="bound source or accepted dependency output changed")
                continue
            if (terminal_dependency or planning_dependency) and not record.get("dependency_snapshot"):
                write_dependency_snapshot(unit_id)
            route = record["_route_full"]
            try:
                if (canonical_digest(plan) != launch_plan_digest or
                        unit_spec(plan, units[unit_id], route) != prepared_specs[unit_id]):
                    raise DispatchError("plan or canonical named-seat context changed after task construction; refuse launch")

                account = (router.worker_account()
                           if route.get("requires_account") else None)
                if lease:

                    lease.record_task(unit_id, record["task_id"], "starting")
                if receipt_observer:
                    receipt_observer("before_start", {"run_id": run_id, "task_id": record["task_id"]})
                if collection_supervisor:
                    collection_supervisor.before_start(record["task_id"])
                receipt = orca.worker_start(
                    run_id, record["task_id"], route, start_timeout_ms
                )
                dispatch = object_id(receipt, "dispatch")
                record["dispatch_id"] = dispatch
                if collection_supervisor:
                    try:
                        collection_supervisor.launch(run_id, record["task_id"], dispatch)
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                        collection_supervisor.degrade(dispatch, exc, run=run_id, task=record["task_id"])
                if receipt_observer:
                    receipt_observer("launch", {"run_id": run_id, "task_id": record["task_id"],
                                               "dispatch_id": dispatch, "receipt": receipt})
                if record.get("prepared_context"):
                    record["supplied_context"] = record["prepared_context"]
                    record["context_provenance"] = "constructed_task_spec"
                record["actual_launch"] = actual_launch(receipt, route, account)
                try:
                    validate_actual_launch(record["actual_launch"], route)
                except DispatcherInterrupted:
                    raise
                except DispatchError:

                    try:
                        orca.stop(dispatch)
                    except DispatcherInterrupted:
                        raise
                    except DispatchError:
                        pass
                    try:
                        orca.release(dispatch)
                    except DispatcherInterrupted:
                        raise
                    except DispatchError:
                        pass
                    raise
                record["status"] = "dispatched"

                record["provider_session"] = bind_provider_session(
                    dispatch, record["actual_launch"], route, since=started,
                )
                record["telemetry"].append({
                    "stage": "start", "ts": now_iso(), "task_id": record["task_id"],
                    "dispatch_id": dispatch, "receipt_sha256": canonical_digest(receipt),
                    "actual_launch": record["actual_launch"],
                    "provider_session": record["provider_session"],
                })
                active[dispatch] = unit_id
                if lease:
                    lease.record_worker(
                        unit_id, record["task_id"], dispatch, "dispatched",
                        provider_session=record["provider_session"],
                        launch=record["actual_launch"],
                    )
                    lease.record_task(unit_id, record["task_id"], "dispatched")
                if receipt_observer:
                    receipt_observer("dispatched", {"run_id": run_id,
                        "task_id": record["task_id"], "dispatch_id": dispatch})
                count += 1
                emit(
                    "worker_started", unit=unit_id, task_id=record["task_id"],
                    dispatch_id=dispatch, launch=record["actual_launch"],
                )
            except LeasePersistenceError:

                raise
            except DispatcherInterrupted:
                raise
            except DispatchError as exc:
                record["status"] = "launch_failed"
                record["unresolved"].append(str(exc))
                if lease:
                    lease.record_task(unit_id, record["task_id"], "launch_failed")
                try:
                    orca.task_fail(record["task_id"], {"reason": str(exc)})
                except DispatcherInterrupted:
                    raise
                except DispatchError as update_exc:
                    record["unresolved"].append(f"task status update failed: {update_exc}")
                emit("launch_failed", unit=unit_id, error=str(exc))
        return count

    start_ready()
    if collection_supervisor:
        for dispatch, unit_id in list(active.items()):
            if dispatch not in collection_supervisor.collectors and dispatch not in collection_supervisor.failures:
                try:
                    collection_supervisor.recover_launch(run_id, state[unit_id]["task_id"], dispatch)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:

                    collection_supervisor.degrade(dispatch, exc, run=run_id,
                        task=state[unit_id]["task_id"])
                    state[unit_id]["unresolved"].append("supervision degraded: " + str(exc))
    deadline = (time.monotonic() + overall_timeout_ms / 1000
                if not collection_supervisor or "overall_timeout_ms" in defaults else None)
    pending_ack: Optional[str] = None
    seen_messages: set[str] = set()

    def settle_workers_that_cannot_report() -> int:

        settled = 0
        for dispatch, unit_id in list(active.items()):
            record = state[unit_id]
            try:
                receipt = orca.worker_show(dispatch)
            except DispatcherInterrupted:
                raise
            except DispatchError:
                continue

            receipt_root = receipt.get("result") or receipt
            worker = receipt_root.get("worker") or {}
            dispatch_record = receipt_root.get("dispatch") or {}
            observation = receipt_root.get("observation") or {}
            receipt_dispatch_id = str(
                dispatch_record.get("id") or worker.get("dispatch_id") or
                worker.get("dispatchId") or ""
            )
            receipt_task_id = str(
                dispatch_record.get("task_id") or dispatch_record.get("taskId") or
                worker.get("task_id") or worker.get("taskId") or ""
            )
            receipt_run_id = str(
                dispatch_record.get("run_id") or dispatch_record.get("runId") or
                worker.get("run_id") or worker.get("runId") or ""
            )
            identity_matches = (
                observation.get("exactWorker") is True
                and receipt_dispatch_id == dispatch
                and receipt_task_id == record["task_id"]
                and receipt_run_id == run_id
            )
            worker_state = str(
                worker.get("state") or worker.get("status") or "outcome_unknown"
            ).lower()
            preview = str((receipt_root.get("terminal") or {}).get("preview") or "")
            preview_lower = preview.lower()
            codex_transport_failed = (
                "backend-api/codex/responses" in preview_lower
                and any(marker in preview_lower for marker in (
                    "unexpected status 404", "404 not found", "unknown error",
                ))
            )
            if not identity_matches:
                continue

            reason: Optional[str] = None
            if worker_state in {"failed", "stopped"}:
                reason = (
                    f"Orca worker entered {worker_state} without an accepted worker_done"
                )
            elif codex_transport_failed:
                reason = (
                    "Codex transport failed before worker_done: "
                    "chatgpt.com/backend-api/codex/responses returned 404"
                )
            if not reason:
                continue

            inspection = {
                "stage": "worker_inspection", "ts": now_iso(),
                "dispatch_id": dispatch, "worker_state": worker_state,
                "exact_worker": True, "identity_matches": True,
                "codex_transport_failed": codex_transport_failed,
                "receipt_sha256": canonical_digest(receipt),
            }
            inspection["action"] = "failed_without_worker_done"
            record["telemetry"].append(inspection)
            record["status"] = "failed"
            record["contract"] = {"accepted": False, "reasons": [reason]}
            record["unresolved"].append(reason)
            if worker_state not in {"failed", "stopped"}:
                try:
                    record["stop"] = orca.stop(dispatch)
                except DispatcherInterrupted:
                    raise
                except DispatchError as exc:
                    record["stop"] = {"ok": False, "error": str(exc)}
                    record["unresolved"].append(f"worker stop unresolved: {exc}")
            try:
                orca.task_fail(record["task_id"], {"reason": reason})
            except DispatcherInterrupted:
                raise
            except DispatchError as exc:
                record["unresolved"].append(f"task status update failed: {exc}")
            try:
                record["release"] = orca.release(dispatch)
            except DispatcherInterrupted:
                raise
            except DispatchError as exc:
                record["release"] = {"ok": False, "error": str(exc)}
            release_ok = bool(record["release"] and release_resolved(record["release"]))
            record["cleanup"] = "resolved" if release_ok else "unresolved"
            if not release_ok:
                record["unresolved"].append(
                    f"terminal cleanup unresolved: {record['release']}"
                )
            if lease:
                lease.record_worker(
                    unit_id, record["task_id"], dispatch, "failed",
                    provider_session=record["provider_session"],
                    launch=record["actual_launch"],
                )
                lease.record_task(unit_id, record["task_id"], "failed")
            active.pop(dispatch, None)
            emit("worker_failed", unit=unit_id, dispatch_id=dispatch, reason=reason)
            settled += 1
        return settled

    def save_settlement(unit_id: str) -> None:
        if not lease:
            return
        record = state[unit_id]
        saved = {key: record[key] for key in (
            "status", "worker_done", "contract", "outputs", "declared_outcome",
            "substantive", "release", "cleanup", "unresolved")}
                                                                          
        saved = json.loads(json.dumps(saved))
        lease.record_worker(unit_id, record["task_id"], record["dispatch_id"],
                            record["status"], provider_session=record["provider_session"],
                            launch=record["actual_launch"], settlement={
                                "run_id": run_id, "task_id": record["task_id"],
                                "dispatch_id": record["dispatch_id"],
                                "prompt_binding": lease._read()["tasks"].get(unit_id, {}).get("prompt_binding") or {},
                                "record": saved, "record_sha256": canonical_digest(saved)})

    unconsumed_delivery = (lease.recovery_state.get("unconsumed_delivery") if lease else None)
    collection_blocked = False
    while active or unconsumed_delivery:
        if collection_supervisor:
            for dispatch, checkpoint in collection_supervisor.checkpoints().items():
                if dispatch in active:
                    record = state[active.pop(dispatch)]
                    record["status"] = "collection_timeout"
                    record["supervision"] = checkpoint
                    record["unresolved"].append("finite supervision checkpoint; original worker custody unresolved")
                    collection_supervisor.collectors.pop(dispatch, None)
        remaining_ms = (max(0, int((deadline - time.monotonic()) * 1000))
                        if deadline is not None else collection_supervisor.wait_ms(wait_window_ms))
        if remaining_ms <= 0:
            break
        wait_ms = min(wait_window_ms, remaining_ms)
        if collection_supervisor:
            wait_ms = min(wait_ms, collection_supervisor.wait_ms(wait_window_ms))
        if not active and not unconsumed_delivery:
            break
        delivery = orca.check(run_id, wait_ms, ack=pending_ack, wait=bool(active))
        if pending_ack == unconsumed_delivery and pending_ack:
            unconsumed_delivery = None
            if lease:
                lease.update(unconsumed_delivery=None)
        pending_ack = None
        messages = delivery.get("messages") or []
        if not messages:
            emit("wait_checkpoint", run_id=run_id, active=len(active))
            if settle_workers_that_cannot_report():
                start_ready()
            if not active:
                break
            continue
        current_delivery_id = delivery_id(delivery)
        if not current_delivery_id:
            raise DispatchError(f"Orca returned actionable messages without a delivery id: {delivery}")

        delivery_consumed = True
        for message in messages:
            message_id = str(message.get("id") or "")
            if message_id and message_id in seen_messages:
                continue
            kind = message.get("type")
            details = payload_json(message)
            dispatch = details.get("dispatchId") or details.get("dispatch_id")
            task = details.get("taskId") or details.get("task_id")
            unit_id = active.get(dispatch)
            if not unit_id:

                unit_id = next((key for key, value in state.items()
                                if dispatch and value["dispatch_id"] == dispatch
                                and value["status"] == "needs_input"), None)
            if not unit_id:
                original = next((value for value in state.values()
                                 if dispatch and value["dispatch_id"] == dispatch), None)
                consumed = original and original.get("worker_done")
                if kind == "worker_done" and not (consumed and task == original["task_id"]
                        and message_id == consumed.get("message_id")
                        and canonical_digest(message) == consumed.get("message_sha256")):
                                                                               
                    delivery_consumed = False
                emit("unmatched_message", message_id=message_id, type=kind,
                     task_id=task, dispatch_id=dispatch, message=message)
                continue
            if message_id:
                seen_messages.add(message_id)
            record = state[unit_id]
            if collection_supervisor:
                collection_supervisor.message(message, run_id)

            if kind == "worker_done":
                body = str(message.get("body") or "")
                outcome = details.get("outcome") or "failed"
                return_contract = units[unit_id].get("return_contract") or {}
                accepted, reasons = validate_return(body, return_contract)
                record["worker_done"] = {
                    "message_id": message_id,
                    "message_sha256": canonical_digest(message),
                    "subject": message.get("subject"),
                    **retained_worker_body(body, return_contract),
                    "outcome": outcome,
                    "files_modified": details.get("filesModified") or details.get("files_modified") or [],
                    "report_path": details.get("reportPath") or details.get("report_path"),
                }
                if dispatch != record["dispatch_id"] or task != record["task_id"]:
                    accepted = False
                    reasons.append("worker_done task or dispatch identity did not match the start receipt")
                declared_outcome, outcome_reasons = parse_declared_outcome(
                    body, units[unit_id]
                )
                reasons.extend(outcome_reasons)
                accepted = accepted and not outcome_reasons
                disposition = outcome_disposition(declared_outcome, units[unit_id])
                record["declared_outcome"] = disposition["declared_outcome"]
                record["substantive"] = disposition["substantive"]
                outputs_ok, output_checks, output_reasons = verify_outputs(
                    units[unit_id], record["worker_done"], declared_outcome
                )
                reasons.extend(output_reasons)
                accepted = accepted and outputs_ok
                record["outputs"] = output_checks
                record["contract"] = {"accepted": accepted, "reasons": reasons}
                record["telemetry"].append({
                    "stage": "contract", "ts": now_iso(), "message_id": message_id,
                    "task_id": task, "dispatch_id": dispatch, "outcome": outcome,
                    "declared_outcome": disposition["declared_outcome"],
                    "substantive": disposition["substantive"],
                    "accepted": accepted,
                })
                record["telemetry"].append({
                    "stage": "output", "ts": now_iso(), "checks": output_checks,
                    "accepted": outputs_ok,
                })
                record["status"] = lane_status(
                    accepted=accepted, worker_outcome=outcome,
                    disposition=disposition)
                if receipt_observer:
                    receipt_observer("settled", {"run_id": run_id, "source": source,
                        "plan_id": plan.get("id"), "native_message": message, "native_delivery": delivery,
                        "units": [{k: v for k, v in record.items() if k != "_route_full"}]})
                if lease:
                    save_settlement(unit_id)
                    lease.record_task(unit_id, record["task_id"], record["status"])
                try:
                    record["release"] = orca.release(record["dispatch_id"])
                    if not release_resolved(record["release"]):
                        record["unresolved"].append(
                            f"terminal cleanup unresolved: worker release did not settle "
                            f"authoritatively: {record['release']}"
                        )
                except DispatcherInterrupted:
                    raise
                except DispatchError as exc:
                    record["release"] = {"ok": False, "error": str(exc)}
                    record["unresolved"].append(f"terminal cleanup unresolved: {exc}")
                release_ok = bool(record["release"] and release_resolved(record["release"]))

                record["cleanup"] = "resolved" if release_ok else "unresolved"
                record["telemetry"].append({
                    "stage": "release", "ts": now_iso(), "resolved": release_ok,
                    "cleanup": record["cleanup"], "receipt": record["release"],
                })
                save_settlement(unit_id)
                active.pop(record["dispatch_id"], None)
                emit(
                    "worker_done", unit=unit_id, dispatch_id=record["dispatch_id"],
                    outcome=record["status"], accepted=accepted,
                    body=record["worker_done"]["body"],
                    body_retention=record["worker_done"].get("body_retention"),
                )
                start_ready()
            elif kind == "heartbeat":
                record["telemetry"].append({
                    "stage": "heartbeat", "ts": now_iso(), "message_id": message_id,
                    "payload": details,
                })
            elif kind == "escalation":

                record["telemetry"].append({"stage": "escalation", "ts": now_iso(),
                    "message_id": message_id, "subject": message.get("subject"),
                    "body": message.get("body"), "payload": details})
                emit(kind, unit=unit_id, message_id=message_id, body=message.get("body"))
            elif kind == "question":
                record["status"] = "needs_input"
                record["unresolved"].append({
                    "type": kind,
                    "message_id": message_id,
                    "subject": message.get("subject"),
                    "body": message.get("body"),
                    "payload": details,
                })
                if record.get("dispatch_id"):
                    active.pop(record["dispatch_id"], None)
                emit(kind, unit=unit_id, message_id=message_id, body=message.get("body"))

        if not delivery_consumed:
            unconsumed_delivery = current_delivery_id
            collection_blocked = True
            emit("unconsumed_delivery", delivery_id=current_delivery_id)
            if lease:
                lease.update(unconsumed_delivery=current_delivery_id)
            break
        pending_ack = current_delivery_id

    if pending_ack:

        tail = orca.check(run_id, wait_window_ms, ack=pending_ack, wait=False)
        if pending_ack == unconsumed_delivery:
            unconsumed_delivery = None
            if lease:
                lease.update(unconsumed_delivery=None)
        if tail.get("messages"):
            emit("unprocessed_tail_delivery", count=len(tail["messages"]))

    if active:
        for dispatch, unit_id in list(active.items()):
            record = state[unit_id]
            record["status"] = "collection_interrupted" if collection_blocked else "collection_timeout"
            record["unresolved"].append(
                ("collection stopped at unconsumed original Delivery; " if collection_blocked else
                 "overall collection deadline reached; ") +
                "worker left live for conditional recovery"
            )
            emit(record["status"], unit=unit_id, dispatch_id=dispatch)

    for unit_id in order:
        settle_provider_usage(state[unit_id], state[unit_id]["route"], started)

    run_usage = usage_rollup(state, order)
    usage_contract = evaluate_usage_contract(
        (defaults.get("usage_contract") or {}), run_usage, state, order
    )
    if usage_contract.get("configured"):
        emit(
            "usage_contract",
            accepted=usage_contract["accepted"],
            measured=usage_contract.get("measured"),
            reasons=usage_contract.get("reasons"),
        )

    reconcile_route_outcomes(
        router, state, order, units, run_key=run_key,
        praxis_task_id=praxis_task_id, plan_id=plan.get("id"), source=source,
    )

    for record in state.values():
        record.pop("_route_full", None)

    statuses = {record["status"] for record in state.values()}
    declared_guard_holds = bool(statuses) and all(
        r['status'] != 'held' or (r.get('declared_outcome') in [str(token) for token in (units[k].get('return_contract', {}).get('outcome_contract', {}).get('hold') or [])])
        for k, r in state.items())
    maintenance = maintenance_status(units, state)
    if unconsumed_delivery or "recovery_unresolved" in statuses:
                                                                               
        overall = "dispatcher_error"
    elif "needs_input" in statuses:
        overall = "needs_input"
    elif recovering_run and ("held_dependent" in statuses or ("held" in statuses and not declared_guard_holds)):
        overall = "held" if statuses <= {"held", "held_dependent"} else "partial"
    elif not maintenance["complete"]:
        overall = "partial"
    elif statuses == {'held'} and declared_guard_holds:
        overall = 'held'
    elif statuses <= {'succeeded', 'held'} and declared_guard_holds and usage_contract['accepted']:

        overall = "succeeded"
    elif statuses == {"held"}:

        overall = "held"
    else:
        overall = "partial"
    assembly = None
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "run",
        "plan_id": plan.get("id"),
        "routine_run_key": run_key,
        "objective": plan["objective"],
        "praxis_task_id": praxis_task_id,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_iso(),
        "status": overall,
        **({"final_assembly": assembly,
            "artifact_status": assembly.get("artifact_status"),
            "delivery_status": assembly.get("delivery_status")} if assembly is not None else {}),
        "declared_guard_hold": overall == "held" and declared_guard_holds,
        "unconsumed_delivery": unconsumed_delivery,
        "receipt_observer_results": observer_results,
        "maintenance": maintenance,
        "source": source,
        "usage": run_usage,
        "usage_contract": usage_contract,
        "information_policy": {
            "shared_by_default": True,
            "privacy_enforcement": "suspended (the configured policy)",
            "hard_isolated_domains": sorted(HARD_ISOLATED_DOMAINS),
        },
        "retry_policy": {
            "attempts_per_lane": 1,
            "single_attempt_per_lane": SINGLE_ATTEMPT_PER_LANE,
            "dispatcher_retries": False,
            "note": ("One attempt per declared lane. Failed, launch-failed, "
                     "contract-rejected, timed-out and cleanup-unresolved lanes are "
                     "recorded here and synthesized as partial evidence. The "
                     "coordinator must not create a replacement task, a retry "
                     "dispatch, or a follow-on run unless User explicitly asks "
                     "for a new run."),
        },

        "declared_outcomes": {
            unit_id: {
                "outcome": state[unit_id].get("declared_outcome"),
                "substantive": state[unit_id].get("substantive"),
                "status": state[unit_id].get("status"),
            }
            for unit_id in order
            if outcome_contract(units[unit_id])
        },
        "cleanup_unresolved": sorted(
            unit_id for unit_id in order
            if state[unit_id].get("cleanup") == "unresolved"
        ),
        "units": [state[unit_id] for unit_id in order],
    }

def dispatcher_error_bundle(
    exc: BaseException, error_context: Optional[Dict[str, Any]],
    lease: Optional[RunLease] = None,
) -> Dict[str, Any]:

    context = error_context or {}
    state = context.get("state") or {}
    order = context.get("order") or list(state)
    units = []
    for unit_id in order:
        record = dict(state.get(unit_id) or {})
        record.pop("_route_full", None)
        units.append(record)
    bundle = {
        "schema_version": context.get("schema_version", SCHEMA_VERSION),
        "mode": context.get("mode", "run"),
        "plan_id": context.get("plan_id"),
        "routine_run_key": context.get("routine_run_key"),
        "objective": context.get("objective"),
        "praxis_task_id": context.get("praxis_task_id"),
        "started_at": context.get("started_at"),
        "completed_at": now_iso(),
        "status": ("interrupted" if isinstance(exc, DispatcherInterrupted)
                   else "dispatcher_error"),
        "error": str(exc),
        "source": context.get("source"),
        "units": units,
    }
    if lease:
        try:
            bundle["lease_state"] = lease._read()
        except DispatchError as read_exc:
            bundle["lease_state_error"] = str(read_exc)
        bundle["lease_persistence_failed"] = lease.persistence_failed
    return bundle

def routine_cli_success(result):
                                                                            
    return result['status'] in AUTHORITATIVE_SUCCESSES or (
        result['status'] == 'held' and result.get('declared_guard_hold') is True)

def main() -> int:
    parser = argparse.ArgumentParser(description="Praxis router-to-Orca dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "run"):
        command = sub.add_parser(name)
        command.add_argument("--plan", required=True)
        command.add_argument("--var", action="append", metavar="KEY=VALUE",
                             help="plan variable, repeatable; expands ${KEY} in the plan")
        command.add_argument("--router")
        command.add_argument("--orca", help="resolved Orca CLI command; default follows Orca skill rules")
        command.add_argument("--output", help="optional unique JSON evidence bundle path")
        command.add_argument("--run-key", help="explicit stable routine occurrence key")
        command.add_argument("--no-route-log", action="store_true")
    args = parser.parse_args()

    lease: Optional[RunLease] = None
    writer_lock: Optional[ExclusiveWriterLock] = None
    writer_lock_receipt: Optional[Dict[str, Any]] = None
    error_context: Dict[str, Any] = {}
    prior_signal_handlers: Dict[int, Any] = {}
    try:
        root = vault_root()
        variables = {
            "vault_root": str(root),
            "artifact_root": str(artifact_root()),
            "institution_dir": str(root / "system" / "memory" / "institution"),
            "date": datetime.now().astimezone().strftime("%Y-%m-%d"),
            "run_bundle": str(Path(args.output).expanduser().resolve()) if args.output else
                          str(root / "system/memory/agents/orca-dispatch/dry-run-bundle.json"),
        }

        for pair in (getattr(args, "var", None) or []):
            if "=" not in pair:
                raise DispatchError(f"--var expects key=value, got {pair!r}")
            key, _, value = pair.partition("=")
            key = key.strip()
            if not key:
                raise DispatchError(f"--var has an empty key: {pair!r}")
            if key in variables:
                raise DispatchError(f"--var {key} would shadow a built-in variable")
            variables[key] = value

        plan_path = Path(args.plan).expanduser().resolve()
        rendered = json.dumps(expand_value(read_json(plan_path), variables))
        missing = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", rendered)))
        if missing:
            raise DispatchError(
                "plan has unexpanded variables: " + ", ".join(missing)
                + ". Supply each with --var name=value.")
        plan = expand_value(read_json(plan_path), variables)
        validate_plan(plan)
        run_key = routine_run_key(plan, getattr(args, "run_key", None))
        if args.command == "run" and run_key:
            output_path = Path(args.output).expanduser() if args.output else None
            lease = RunLease(root, run_key, output_path)
            acquired, prior = lease.acquire()
            if not acquired:
                result = {
                    "schema_version": SCHEMA_VERSION,
                    "mode": "run",
                    "plan_id": plan.get("id"),
                    "routine_run_key": run_key,
                    "status": "duplicate_suppressed",
                    "completed_at": now_iso(),
                    "authoritative_prior": prior,
                }
                if args.output:
                    atomic_write_json(Path(args.output), result)
                    result["bundle_path"] = str(Path(args.output).expanduser().resolve())
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            def interrupt_dispatcher(signum: int, _frame: Any) -> None:
                raise DispatcherInterrupted(
                    f"coordinator received {signal.Signals(signum).name}; "
                    "Orca Run and workers were left intact for recovery"
                )

            for signum in (signal.SIGTERM, signal.SIGINT):
                prior_signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, interrupt_dispatcher)
        lock_config = plan.get("writer_lock")
        if args.command == "run" and lock_config:
            writer_lock = ExclusiveWriterLock(
                root, lock_config["name"],
                int(lock_config.get("wait_timeout_seconds", 10800)))
            writer_lock_receipt = writer_lock.acquire()

        command = JsonCommand(launch_env())
        router = RouterClient(
            Path(args.router).expanduser().resolve() if args.router else default_router(),
            command)
        orca = OrcaClient(resolve_orca(args.orca), command)
        result = execute(
            plan, router, orca, dry_run=args.command == "dry-run",
            log_routes=not args.no_route_log, run_key=run_key, lease=lease,
            plan_path=plan_path,
            output_path=(Path(args.output).expanduser().resolve() if args.output else None),
            error_context=error_context,
        )
        if writer_lock_receipt:
            result["writer_lock"] = writer_lock_receipt
        if args.output:
            atomic_write_json(Path(args.output), result)
            result["bundle_path"] = str(Path(args.output).expanduser().resolve())
        if lease:
            lease.update(
                status=result["status"], completed_at=now_iso(),
                bundle_path=result.get("bundle_path"),
                bundle_sha256=(file_digest(Path(args.output).expanduser().resolve())
                               if args.output else None),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if routine_cli_success(result) else 4
    except (DispatchError, subprocess.TimeoutExpired) as exc:
        if lease and lease.acquired and not lease.persistence_failed:
            try:
                lease.update(
                    status=("interrupted" if isinstance(exc, DispatcherInterrupted)
                            else "dispatcher_error"),
                    completed_at=now_iso(), error=str(exc),
                )
            except DispatchError as persistence_exc:
                exc = persistence_exc
        error = dispatcher_error_bundle(exc, error_context, lease)
        if writer_lock:
            error["writer_lock"] = writer_lock.receipt()
        if getattr(args, "output", None):
            atomic_write_json(Path(args.output), error)
            error["bundle_path"] = str(Path(args.output).expanduser().resolve())
        if lease and lease.acquired and not lease.persistence_failed:
            try:
                lease.update(
                    bundle_path=error.get("bundle_path"),
                    bundle_sha256=(file_digest(Path(args.output).expanduser().resolve())
                                   if getattr(args, "output", None) else None),
                )
            except DispatchError as persistence_exc:
                error["lease_record_error"] = str(persistence_exc)
                error["lease_persistence_failed"] = lease.persistence_failed
                if getattr(args, "output", None):
                    atomic_write_json(Path(args.output), error)
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 2
    finally:
        for signum, handler in prior_signal_handlers.items():
            signal.signal(signum, handler)
        if writer_lock:
            writer_lock.release()

def _inactive_production_dispatcher():

    if __name__ == 'praxis45_dispatcher':
        return False                                                            
    root = Path(__file__).resolve().parents[2]
    info_path = root.parent / 'release.json'
    home = Path.home()
    if not info_path.is_file():

        for parent in Path(__file__).resolve().parents:
            if parent.name in {'.claude', '.agents'}:
                home = parent.parent
                break
        info_path = home / '.local/share/praxis/releases/current/release.json'
    if not info_path.is_file():
        return any(x.name in {'.claude', '.agents'} for x in Path(__file__).resolve().parents) or root == home/'dev/praxis'
    try:
        info = json.loads(info_path.read_bytes())
        runtime = any(x.name in {'.claude', '.agents'} for x in Path(__file__).resolve().parents)
        if not runtime and root != Path(info['code_root']) and str(root) != info.get('canonical'):
            return False
        flag = Path(info['installation_home']) / '.local/share/praxis/releases/activation.json'
        if flag.is_symlink():return True
        try:
            value = json.loads(flag.read_bytes())
        except FileNotFoundError:
            return True
        return not (value.get('enabled') is True and value.get('schema_version') == 1 and
                    value.get('version') == info['version'] and value.get('commit') == info['commit'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print('Praxis 4.5 inactive metadata unavailable; using production 0188f29c: ' + str(exc), file=sys.stderr)
        return True

if _inactive_production_dispatcher():
    _prior = Path(__file__).with_name('dispatcher_production.py')
    if hashlib.sha256(_prior.read_bytes()).hexdigest() != 'c4afb4bca980c8a4117531843a7a61595aa9db862a3d273e8a472df779ddd08c':
        raise RuntimeError('frozen production dispatcher drift')

    _frozen_source = _prior.read_bytes()
    _frozen_main = b'\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    if not _frozen_source.endswith(_frozen_main):
        raise RuntimeError('frozen production dispatcher main guard differs')

    exec(compile(_frozen_source[:-len(_frozen_main)], str(_prior), 'exec'), globals())
    _frozen_unit_spec = unit_spec
    def unit_spec(plan, unit, route, context_receipt=None):
        return _frozen_unit_spec(plan, unit, route, context_receipt).replace(
            "Access is not authority: this does not permit you to send, submit, sign off, spend, grade, publish, or change a calendar commitment, and it does not widen the read-only effects this unit declares.",
            "Source access alone grants no action authority. Perform writes or publication only within this unit's request-bound scope and the actual user's authorization. A read-only or prepare-only unit remains so. Never infer permission to send, submit, sign off, spend, grade, publish, or change a calendar commitment from source access or a graph alone.")

if __name__ == "__main__":
    raise SystemExit(main())
