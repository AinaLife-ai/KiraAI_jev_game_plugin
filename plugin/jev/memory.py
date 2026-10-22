"""Harness memory: Jev has no conversation history, so the harness remembers.

- recent_actions : last N high-level actions with outcomes
- recent_hints   : deduplicated dialog pages that survive the text box
- room/explored state (optional, per adapter)

Pattern from jev-plays-pokemon state.py.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Optional


class HarnessMemory:
    def __init__(self, max_actions: int = 12, max_hints: int = 8) -> None:
        self.max_actions = max_actions
        self.max_hints = max_hints
        self.actions: deque[dict] = deque(maxlen=max_actions)
        self.hints: deque[str] = deque(maxlen=max_hints)

    def add_action(self, action: str, outcome: str, detail: str = "") -> None:
        self.actions.append(
            {"action": action, "outcome": outcome, "detail": detail[:120]}
        )

    def add_hint(self, hint: str) -> None:
        hint = hint.strip()
        if not hint:
            return
        if not self.hints or self.hints[-1] != hint:
            self.hints.append(hint)

    def to_state(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.actions:
            out["recent_actions"] = list(self.actions)
        if self.hints:
            out["recent_hints"] = list(self.hints)
        return out

    def clear(self) -> None:
        self.actions.clear()
        self.hints.clear()
