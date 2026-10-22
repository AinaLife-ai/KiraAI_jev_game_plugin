"""State builder: adapter snapshot -> compact structured Jev state.

Rules (from TerraBlind + jev-plays-pokemon + typesafe-mario):
- Facts only, no conclusions ("threat" is Jev's job, code reports numbers).
- Short self-explanatory field names, limit + aggregate lists.
- Harness memory (recent actions / hints) is re-injected every turn —
  Jev has no conversation history; continuity comes from the harness.
"""
from __future__ import annotations

import json
from typing import Any, Optional


class StateBuilder:
    def __init__(self, game_instructions: str = "") -> None:
        self.game_instructions = game_instructions

    def build(
        self,
        snapshot: dict[str, Any],
        goal: str = "",
        recent_actions: Optional[list[dict]] = None,
        recent_hints: Optional[list[str]] = None,
        playbook: str = "",
        extra: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Assemble the canonical state object sent to Jev.

        ``snapshot`` is the adapter's raw state (already compact).
        All fields optional — builder only emits what exists.
        """
        state: dict[str, Any] = {}

        # Instruction block (how to read the state) — kept short.
        instr = []
        if self.game_instructions:
            instr.append(self.game_instructions)
        if playbook:
            instr.append(f"GAME KNOWLEDGE: {playbook}")
        if goal:
            instr.append(f"CURRENT GOAL: {goal}")
        if instr:
            state["instructions"] = " ".join(instr)

        # Player + world facts (pass through, trimmed).
        if "player" in snapshot:
            state["player"] = snapshot["player"]
        if "world" in snapshot:
            state["world"] = snapshot["world"]
        if "dialog" in snapshot and snapshot["dialog"]:
            state["dialog"] = snapshot["dialog"][:400]
        if "entities" in snapshot and snapshot["entities"]:
            state["entities"] = snapshot["entities"][:16]
        if "game" in snapshot:
            state["game"] = snapshot["game"]

        # Harness memory — the model's only window into the past.
        # `recent_actions` may be a deque (HarnessMemory) — deques don't
        # support slicing, so materialise to a list first.
        if recent_actions:
            state["recent_actions"] = list(recent_actions)[-12:]
        if recent_hints:
            state["recent_hints"] = list(recent_hints)[-8:]

        # Adapter-specific passthrough (battle state, legal actions, …).
        if extra:
            state.update(extra)

        return state

    def to_json(self, state: dict[str, Any]) -> str:
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def compact_entities(entities: list[dict], limit: int = 16) -> list[dict]:
    """Trim entity list to the closest/most relevant N, keep fields short."""
    if len(entities) <= limit:
        return entities
    keyed = sorted(entities, key=lambda e: e.get("distance_cells", 999))
    return keyed[:limit]
