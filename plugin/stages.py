"""Game stage state machine (generic).

Each engine instance has a stage. Stage transitions are events:
- ordinary transitions -> broadcast (notify)
- key transitions       -> escalate to the main LLM (upgrade)

Stages are per-game lists; the machine itself is game-agnostic.

YGO:      idle -> joining -> confirming_opponent -> readying -> dueling -> ended
Pokemon:  idle -> overworld -> dialog -> menu -> battle -> ended
TerraBlind: idle -> exploring -> crafting -> boss -> ended

Pure logic — unit-testable offline.
"""
from __future__ import annotations

from typing import Callable, Optional


class StageMachine:
    def __init__(
        self,
        stages: list[str],
        initial: str = "",
        on_change: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.stages = stages
        self.on_change = on_change
        self.idx = stages.index(initial) if initial in stages else 0

    @property
    def stage(self) -> str:
        return self.stages[self.idx]

    def set(self, stage: str) -> bool:
        """Transition to a stage if valid and different. Returns True if moved."""
        if stage not in self.stages:
            return False
        if stage == self.stage:
            return False
        old = self.stage
        self.idx = self.stages.index(stage)
        if self.on_change:
            self.on_change(old, stage)
        return True

    def is_(self, *names: str) -> bool:
        return self.stage in names

    def reset(self) -> None:
        self.idx = 0


# Per-game stage lists
YGO_STAGES = ["idle", "joining", "confirming_opponent", "readying", "dueling", "ended"]
POKEMON_STAGES = ["idle", "overworld", "dialog", "menu", "battle", "ended"]
TERRABLIND_STAGES = ["idle", "exploring", "crafting", "boss", "ended"]
GENERIC_STAGES = ["idle", "running", "ended"]

STAGE_LISTS = {
    "ygo_engine": YGO_STAGES,
    "pokemon_pyboy": POKEMON_STAGES,
    "mgba_http": POKEMON_STAGES,
    "terrablind_http": TERRABLIND_STAGES,
    "generic_http": GENERIC_STAGES,
}
