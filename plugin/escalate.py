"""Escalation: Jev hesitates -> wake the main LLM.

Rules:
- high-risk action (attack/summon/activate/flee/use_item/cast) below
  high_risk_threshold -> escalate (let the LLM decide)
- consecutive low-confidence decisions (streak) -> escalate
- escalated once -> cooldown before escalating again (no spam)

Escalation emits a synthetic message into the chat via a callback so the
main LLM sees the situation and can answer with game_command/game_set_goal.

Pure logic; the emit callback is provided by the plugin.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

log = logging.getLogger("kira_game_agent.escalate")

HIGH_RISK = {"attack", "flee", "use_item", "cast", "summon", "act_effect", "activate"}


class Escalator:
    def __init__(
        self,
        emit: Callable[[str, str], None],
        *,
        high_risk_threshold: float = 0.85,
        low_conf_threshold: float = 0.5,
        streak_limit: int = 3,
        cooldown: float = 60.0,
    ) -> None:
        self.emit = emit
        self.high_risk_threshold = high_risk_threshold
        self.low_conf_threshold = low_conf_threshold
        self.streak_limit = streak_limit
        self.cooldown = cooldown
        self._streak = 0
        self._last_escalation = 0.0
        self.escalations = 0

    def _ready(self) -> bool:
        return time.monotonic() - self._last_escalation >= self.cooldown

    def note(self, action: str, confidence: float, sid: str, game: str) -> None:
        """Feed each decision. Returns nothing; escalates via callback."""
        risky = action in HIGH_RISK
        if risky and confidence < self.high_risk_threshold:
            self._escalate(sid, game, f"高风险动作 {action} 置信度仅 {confidence:.2f}，请主控决策")
            return
        if confidence < self.low_conf_threshold:
            self._streak += 1
        else:
            self._streak = 0
        if self._streak >= self.streak_limit:
            # Only reset the streak when the escalation actually fired
            # (cooldown blocks the send, not the intent).
            if self._escalate(sid, game, f"连续 {self._streak} 次决策低置信（最近动作 {action}），需要主控介入"):
                self._streak = 0

    def _escalate(self, sid: str, game: str, reason: str) -> bool:
        if not self._ready():
            return False
        self._last_escalation = time.monotonic()
        self.escalations += 1
        log.info("[escalate] %s %s: %s", sid, game, reason)
        try:
            self.emit(sid, reason)
        except Exception as e:  # noqa: BLE001
            log.warning("escalate emit failed: %s", e)
        return True
