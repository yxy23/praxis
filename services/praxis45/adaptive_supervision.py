
from __future__ import annotations

import copy
import math
import re

from adaptive_contract import validate_bounds

POLICY = "adaptive_activity_progress_v1"
PROGRESS = frozenset({"source_cursor", "read_receipt", "artifact_version",
                      "native_transition", "effect_readback"})

def require_collector(capabilities):
    if POLICY not in capabilities:
        raise ValueError("selected collector lacks adaptive activity/progress support")

class Watch:
    def __init__(self, native, policy, *, started_ms, deadline_ms=None):
        validate_bounds(policy)
        if set(native) != {"run_id", "task_id", "dispatch_id"} or any(not v for v in native.values()):
            raise ValueError("full original native identity required")
        if not math.isfinite(started_ms) or (deadline_ms is not None and
                (not math.isfinite(deadline_ms) or deadline_ms <= started_ms)):
            raise ValueError("finite explicit clock/deadline required")
        self.native, self.policy = copy.deepcopy(native), copy.deepcopy(policy)
        self.started = self.activity = self.progress = self.last_clock = started_ms
        self.deadline = deadline_ms
        self.bound = False
        self.seen, self.milestones = set(), set()
        self.queue_started = None

    def _clock(self, now):
        if not math.isfinite(now) or now < self.last_clock:
            raise ValueError("clock must be finite and monotonic")
        self.last_clock = now

    def observe(self, event, *, now_ms, validate_evidence):
        self._clock(now_ms)
        key = event.get("event_id")
        if (event.get("native") != self.native or not isinstance(key, str) or not key
                or key in self.seen or type(event.get("sequence")) is not int
                or event["sequence"] < 1):
            return False
                                                                                
        if event["sequence"] <= getattr(self, "sequence", 0):
            return False
        kind = event.get("kind")
        if kind not in {"heartbeat", "native_bound", *PROGRESS}:
            return False
        evidence = event.get("evidence_sha256")
        if kind in PROGRESS and (not isinstance(evidence, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence)
                or not validate_evidence(event)):
            return False
        self.seen.add(key); self.sequence = event["sequence"]
        self.activity = now_ms
        if kind == "native_bound":
            self.bound = True
        elif kind in PROGRESS and evidence not in self.milestones:
            self.milestones.add(evidence); self.progress = now_ms
            self.bound = True
            self.queue_started = None
        return True

    def queue(self, *, now_ms):
        self._clock(now_ms)
        if self.queue_started is None:
            self.queue_started = now_ms

    def tick(self, *, now_ms):
        self._clock(now_ms)
        reason = None
        if self.deadline is not None and now_ms >= self.deadline:
            reason = "explicit_deadline"
        elif not self.bound and now_ms - self.started >= self.policy["start_ms"]:
            reason = "start_bound"
        elif self.queue_started is not None and now_ms - self.queue_started >= self.policy["queue_ms"]:
            reason = "queue_bound"
        elif now_ms - self.progress >= self.policy["no_progress_ms"]:
            reason = "no_progress_bound"
        elif now_ms - self.activity >= self.policy["activity_ms"]:
            reason = "activity_silence_bound"
        return {"state": "checkpointed_unresolved" if reason else "active",
                "reason": reason, "native": copy.deepcopy(self.native),
                "worker_death_proven": False, "retry_authorized": False,
                "release_authorized": False}
