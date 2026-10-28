
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

class RegistryError(RuntimeError):
    pass

class RouteRefused(RegistryError):
    pass

def vault_root() -> Path:
    for candidate in (
        os.environ.get("VAULT_ROOT"),
        "~/Research",
    ):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path / "system" / "memory" / "institution").is_dir():
            return path
    raise RegistryError("cannot locate the vault; set VAULT_ROOT")

def institution() -> Path:
    return vault_root() / "system" / "memory" / "institution"

def skill_root() -> Path:
    for candidate in ("~/.claude/skills", "~/.agents/skills"):
        path = Path(candidate).expanduser()
        if (path / "chief-of-staff" / "router.py").exists():
            return path
    raise RegistryError("cannot locate a deployed chief-of-staff/router.py")

def router_path() -> Path:
    return skill_root() / "chief-of-staff" / "router.py"

def import_deployed_router():

    sys.path.insert(0, str(router_path().parent))
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import router                                                          
    finally:
        sys.dont_write_bytecode = previous
    return router

_YAML_CACHE: Dict[str, tuple] = {}

def _load_yaml(path: Path) -> dict:

    router = import_deployed_router()                                          
    key = str(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    cached = _YAML_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    data = router.load_yaml(str(path))
    _YAML_CACHE[key] = (stamp, data)
    return data

@dataclass
class Seat:
    name: str
    role_id: str
    title: str
    route: str
    charter: str
    skills: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    procedure: str = ""
    access_profile: str = ""
    task_routes: List[Dict[str, str]] = field(default_factory=list)
    task_mode: str = ""

    def for_task_mode(self, mode: str) -> "Seat":

        matches = [entry for entry in self.task_routes if entry.get("mode") == mode]
        if len(matches) != 1 or not matches[0].get("route"):
            raise RegistryError(f"{self.name} has no unique declared task mode '{mode}'")
        return replace(self, route=matches[0]["route"], task_mode=mode)

    @property
    def inherited_skills(self) -> List[str]:

        return list(dict.fromkeys(
            [s for s in [self.procedure, *self.skills] if s]
        ))

@dataclass
class Route:
    seat: Seat
    effective_model: str
    launch_target: str
    provider: str
    agent: str
    effort: str
    pilot_route: bool = False
    pinned: bool = False
    fallback: Optional[str] = None
    requires_account: Optional[str] = None
    gates: List[str] = field(default_factory=list)
    explicit_model: bool = False

def role_registry() -> Dict[str, Any]:

    return _load_yaml(institution() / "ROLE-REGISTRY.yaml")

def seats(*, include_pilots: bool = False) -> Dict[str, Seat]:

    reg = _load_yaml(institution() / "ROLE-REGISTRY.yaml")
    out: Dict[str, Seat] = {}
    for role in reg.get("roles", []):
        if role.get("status") == "pilot" and not include_pilots:
            continue
        name = (role.get("display_name") or "").strip()
        if not name:
            continue                                      
        out[name] = Seat(
            name=name,
            role_id=role["id"],
            title=str(role.get("title") or ""),
            route=role["route"],
            charter=str(role.get("charter") or ""),
            skills=list(role.get("skills") or []),
            aliases=list(role.get("aliases") or []),
            procedure=str(role.get("procedure") or ""),
            access_profile=str(role.get("access_profile") or ""),
            task_routes=list(role.get("task_routes") or []),
        )
    return out

FRONTMATTER = re.compile(r"(?ms)\A---\s*\n(.*?)\n---")
BLOCK = re.compile(r"(?m)^(\s*)routine_execution:\s*$")

SKILL_ROOTS = ("~/.claude/skills", "~/.agents/skills")

class RoutineError(RuntimeError):
    pass

@dataclass
class RoutineSpec:
    command: str = "excluded"

def routine_spec(key: str) -> RoutineSpec:
    raise RoutineError("Routine entrypoints are excluded from this public runtime")

def routine_seat(spec: RoutineSpec, *, name: str) -> Seat:
    raise RoutineError("Routine entrypoints are excluded from this public runtime")

def agent_skill_root(agent: str) -> Path:

    roots = {
        "claude": Path("~/.claude/skills").expanduser(),
        "codex": Path("~/.agents/skills").expanduser(),

        "antigravity": Path("~/.agents/skills").expanduser(),
    }
    try:
        root = roots[agent]
    except KeyError as exc:
        raise RegistryError(
            f"no deployed skill root is defined for worker agent '{agent}'"
        ) from exc
    if not root.is_dir():
        raise RegistryError(f"deployed skill root does not exist: {root}")
    return root

def inherited_skill_paths(route: Route) -> List[Path]:

    root = agent_skill_root(route.agent)
    paths = [root / name / "SKILL.md" for name in route.seat.inherited_skills]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise RegistryError(
            f"{route.seat.name} cannot launch: registered skill inheritance is incomplete: "
            + ", ".join(missing)
        )
    return paths

def account_requirements() -> Dict[str, str]:
    reg = _load_yaml(institution() / "MODEL-REGISTRY.yaml")
    return {m["id"]: m["requires_account"]
            for m in reg.get("models", []) if m.get("requires_account")}

def resolve(seat: Seat, *, sensitivity: str = "confidential", risk: str = "medium",
            request_id: Optional[str] = None, log: bool = True,
            require_primary: bool = False,
            explicit_model: Optional[str] = None) -> Route:

    cmd = [sys.executable, str(router_path()),
           "--task-class", seat.route,
           "--sensitivity", sensitivity,
           "--risk", risk,
           "--live-availability",
           "--json"]
    if request_id:
        cmd += ["--task-id", request_id]
    if require_primary:
        cmd += ["--require-primary"]
    if explicit_model:
        cmd += ["--explicit-model", explicit_model]
    if log:
        cmd += ["--log"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RouteRefused(
            f"{seat.name}: route '{seat.route}' refused — "
            f"{(proc.stderr or proc.stdout).strip()[:400]}")
    d = json.loads(proc.stdout)
    reqs = account_requirements()
    return Route(
        seat=seat,
        effective_model=d["effective_model"],
        launch_target=d.get("launch_target", d["effective_model"]),
        provider=d.get("provider", ""),
        agent=d.get("agent", d.get("provider", "")),
        effort=d.get("effort", ""),
        pilot_route=bool(d.get("pilot_route")),
        pinned=bool(d.get("pinned")),
        fallback=d.get("fallback"),
        requires_account=reqs.get(d["effective_model"]),
        gates=list(d.get("gates") or []),
        explicit_model=bool(explicit_model),
    )

def worker_account() -> Dict[str, Any]:

    router = import_deployed_router()                                    
    return router.worker_account(router.account_data())

def account_blocked(route: Route, account: Optional[Dict[str, Any]]) -> Optional[str]:

    if not route.requires_account:
        return None
    account = account or {}
    kind = account.get("kind")
    if kind == route.requires_account:
        return None
    if kind is None:
        return (f"{route.seat.name} routes to {route.effective_model}, which is entitled on the "
                f"{route.requires_account} Claude account, and Orca does not attest which "
                f"account the worker would launch on "
                f"({account.get('unproven') or 'no account record'}). "
                f"An unproven account is not a permission.")
    return (f"{route.seat.name} routes to {route.effective_model}, which is entitled on the "
            f"{route.requires_account} account; the account attested for the worker is "
            f"{kind}. Orca has no per-worker account flag, so the whole roster moves together.")
