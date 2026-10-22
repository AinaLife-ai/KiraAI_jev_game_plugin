"""Confidence gating + baseline fallback.

Two gates (official confidence-routing pattern):
- low-risk actions  : act when confidence >= low_risk_threshold
- high-risk actions : act only when confidence >= high_risk_threshold
Below threshold -> safe default (confirm/wait/keep position).

Baseline (TerraBlind): when Jev is unavailable, fall back to conservative
rule-based decisions instead of stalling the loop.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("kira_game_agent.gate")

HIGH_RISK_ACTIONS = {"attack", "flee", "use_item", "cast", "summon", "act_effect"}


class DecisionGate:
    def __init__(
        self,
        low_risk_threshold: float = 0.55,
        high_risk_threshold: float = 0.85,
        use_baseline: bool = True,
    ) -> None:
        self.low_risk_threshold = low_risk_threshold
        self.high_risk_threshold = high_risk_threshold
        self.use_baseline = use_baseline
        self.last_decision: Optional[dict] = None
        self.last_at: float = 0.0

    # ---- action selection (Noul mode) ------------------------------

    def pick_action(
        self,
        nouls: dict[str, float],
        dialog_active: bool = False,
        menu_open: bool = False,
        walkable: Optional[dict[str, bool]] = None,
        last_action: Optional[str] = None,
        explore_hint: Optional[str] = None,
    ) -> str:
        """Turn Noul gut-checks into a concrete safe action (jev-plays-pokemon)."""
        if not nouls:
            return "wait"
        ranked = sorted(nouls.items(), key=lambda kv: -kv[1])
        action, confidence = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = confidence - runner_up

        # Menu override: while a choice menu is open, confirm highlighted
        # unless the model is very confident about something else.
        if menu_open and confidence < 0.7:
            action = "confirm"

        if margin >= 0.08 and confidence >= 0.45:
            pass  # trust the model
        elif dialog_active or menu_open:
            action = "confirm"  # safe default
        elif explore_hint and walkable and walkable.get(explore_hint, False):
            action = f"move_{explore_hint}"
        else:
            action = "wait"

        if menu_open and action == "wait":
            action = "confirm"

        # Deterministic wall check for movement.
        if action in {"move_up", "move_down", "move_left", "move_right"}:
            direction = action.removeprefix("move_")
            if walkable is not None:
                blocked = not walkable.get(direction, True)
                all_blocked = not any(walkable.values())
                if blocked and not all_blocked:
                    action = "confirm" if dialog_active else "wait"

        # Anti-pacing: never immediately reverse the last walk.
        rev = {"move_up": "move_down", "move_down": "move_up",
               "move_left": "move_right", "move_right": "move_left"}
        if action in rev and rev[action] == last_action and walkable:
            alternatives = [
                f"move_{d}" for d, ok in walkable.items()
                if ok and f"move_{d}" not in (action, rev[action])
            ]
            if alternatives:
                best = max(alternatives, key=lambda a: nouls.get(a, 0.0))
                if nouls.get(best, 0.0) >= 0.3:
                    action = best

        self.last_decision = {
            "mode": "action",
            "action": action,
            "confidence": confidence,
            "margin": margin,
            "nouls": nouls,
        }
        self.last_at = time.monotonic()
        return action

    # ---- goal selection (Choice mode) ------------------------------

    def pick_goal(
        self,
        probabilities: dict[str, float],
        temperature: float = 2.0,
        floor: float = 0.1,
    ) -> tuple[str, float]:
        """Sample from Jev's calibrated goal distribution (flattened).

        Raw probabilities are calibrated but confidently-wrong picks repeat
        every turn; the temperature/floor transform keeps alternatives alive.
        """
        import math
        import random

        labels = list(probabilities)
        if not labels:
            return "wait", 0.0
        probs = [max(0.0, probabilities[g]) for g in labels]
        total = sum(probs)
        if total <= 0:
            return "wait", 0.0
        probs = [p / total for p in probs]
        probs = [p + max(0.0, floor) for p in probs]
        total = sum(probs)
        probs = [p / total for p in probs]
        if temperature != 1.0 and temperature > 0:
            logits = [math.log(p + 1e-12) / temperature for p in probs]
            m = max(logits)
            exp = [math.exp(x - m) for x in logits]
            exp_total = sum(exp)
            probs = [e / exp_total for e in exp]
        label = random.choices(labels, weights=probs, k=1)[0]
        return label, probs[labels.index(label)]

    # ---- baseline fallback ----------------------------------------

    def baseline_action(self, snapshot: dict[str, Any]) -> str:
        """Conservative rule-based decision when Jev is unavailable."""
        # Dialog open -> advance text.
        if snapshot.get("dialog"):
            return "confirm"
        # In battle and low HP -> heal/cancel.
        if snapshot.get("battle_active") and snapshot.get("player", {}).get("hp_percent", 100) < 30:
            return "confirm"
        # Otherwise keep position and wait.
        return "wait"

    def decide(
        self,
        snapshot: dict[str, Any],
        nouls: dict[str, float],
        dialog_active: bool = False,
        menu_open: bool = False,
        walkable: Optional[dict[str, bool]] = None,
    ) -> str:
        """Full decision path with fallback."""
        if nouls:
            return self.pick_action(nouls, dialog_active, menu_open, walkable)
        if self.use_baseline:
            return self.baseline_action(snapshot)
        return "wait"
