
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

@dataclass
class GuardVerdict:
    allowed: bool = True
    requires_approval: bool = False
    reasons: List[str] = field(default_factory=list)
    redactions: List[str] = field(default_factory=list)

    @property
    def lifecycle_hint(self) -> str:
        if not self.allowed:
            return "rejected"
        return "awaiting_user" if self.requires_approval else "accepted"

def check_privacy(text: str) -> GuardVerdict:

    return GuardVerdict()

def check_authority(text: str) -> GuardVerdict:

    return GuardVerdict()

def screen(text: str) -> GuardVerdict:

    return GuardVerdict()

def safe_for_log(text: str, limit: int = 120) -> str:

    body = (text or "").strip()
    return f"<{len(body)} chars, {len(body.split())} words>" if body else "<empty>"
