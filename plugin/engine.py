"""Decision engine: the generic Jev-in-the-loop loop.

Pattern (validated by TerraBlind / jev-plays-pokemon / typesafe-mario):

    loop:
        snapshot = adapter.get_state()
        state    = state_builder.build(snapshot, goal, memory, playbook)
        Jev asks: [goal Choice] + [action Nouls]  (one parallel call)
        gate decides the concrete action (confidence + margin + walkability)
        adapter.act(action)
        harness memory records action + outcome
        on significant change -> emit event (notify KiraAI)

Never blocks on Jev: async double-buffer, baseline fallback, intent TTL.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from .jev.client import JevClient
from .jev.gate import DecisionGate
from .jev.memory import HarnessMemory
from .jev.questions import (
    ACTION_NAMES,
    DIRECTION_ACTIONS,
    build_action_questions,
    build_goal_questions,
    DEFAULT_GAME_INSTRUCTIONS,
)
from .jev.state_builder import StateBuilder
from .stages import STAGE_LISTS, StageMachine
from .escalate import Escalator

log = logging.getLogger("kira_game_agent.engine")

DEFAULT_INTENT_TTL = 1.5


class DecisionEngine:
    def __init__(
        self,
        adapter: Any,
        client: JevClient,
        gate: DecisionGate,
        *,
        decision_hz: float = 1.0,
        intent_ttl: float = DEFAULT_INTENT_TTL,
        game_instructions: str = DEFAULT_GAME_INSTRUCTIONS,
        temperature: float = 2.0,
        goal_floor: float = 0.1,
        on_event: Optional[Callable[[str, dict], None]] = None,
        game: str = "",
        sid: str = "",
        escalator: Optional[Escalator] = None,
        backend_resolver: Optional[Callable[[], Any]] = None,
        advisory: Optional[Callable[[str, str, dict], None]] = None,
        # Stage names that require a main-LLM strategic consult on entry
        # (plus every `strategy_interval_rounds` rounds for long games).
        strategy_stages: Optional[set[str]] = None,
        strategy_interval_rounds: int = 8,
        consult_ask_jev: bool = True,
        consult_threshold: float = 0.6,
        strategy_advisory: bool = True,
    ) -> None:
        self.adapter = adapter
        self.client = client
        self.backend_resolver = backend_resolver
        self.advisory = advisory
        self.strategy_stages = strategy_stages or set()
        self.strategy_interval_rounds = max(1, strategy_interval_rounds)
        self.consult_ask_jev = consult_ask_jev
        self.consult_threshold = consult_threshold
        self.strategy_advisory = strategy_advisory
        self._strategy_round_counter = 0
        self._strategy_consulted_stages: set[str] = set()
        self.gate = gate
        self.decision_interval = 1.0 / max(0.2, decision_hz)
        self.intent_ttl = intent_ttl
        self.temperature = temperature
        self.goal_floor = goal_floor
        self.on_event = on_event
        self.builder = StateBuilder(game_instructions)
        self.memory = HarnessMemory()
        self.goal = ""
        self.playbook = ""
        self.action_questions = build_action_questions()
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._last_decision_at = 0.0
        self._last_action = ""
        self._last_action_at = 0.0
        # Pre-input action chain: Jev can plan a short sequence of legal
        # actions; we execute them without re-deciding unless interrupted.
        self._action_chain: list[str] = []
        self._chain_snapshot_hash: str = ""
        self._loop_count = 0
        self.recent_decisions: list[dict] = []  # ring buffer, newest last
        self.game = game
        self.sid = sid
        self.escalator = escalator
        # Stage machine (per-game stages; 'running' fallback).
        stages = STAGE_LISTS.get(game, STAGE_LISTS["generic_http"])
        self.stage_machine = StageMachine(stages, on_change=self._on_stage_change)
        self._last_stage = self.stage_machine.stage

    # ---- stage / escalation helpers ----------------------------------

    def _on_stage_change(self, old: str, new: str) -> None:
        if self.on_event:
            self.on_event("stage", {"old": old, "new": new})
        # Strategic consult: entering a key stage -> wake the main LLM to
        # set the battle plan before Jev executes within it.
        if new in self.strategy_stages and new not in self._strategy_consulted_stages:
            self._strategy_consulted_stages.add(new)
            self._request_strategy_consult(new)

    def _request_strategy_consult(self, stage_name: str) -> None:
        """Trigger a main-LLM strategic consult for a stage entry.
        Runs only if strategy_advisory is enabled."""
        if not getattr(self, "strategy_advisory", True):
            return
        if self.advisory is None or self.adapter is None:
            return
        try:
            snap = self.adapter.get_state() or {}
            # Optional engine gate: does Jev/laya think this is strategic enough?
            if self.consult_ask_jev and self.client is not None:
                gate_ok = self._consult_gate(snap, stage_name)
                if not gate_ok:
                    if self.on_event:
                        self.on_event("strategy_skipped", {"stage": stage_name})
                    return
            self.advisory(self.sid, stage_name, snap)
            if self.on_event:
                self.on_event("strategy_consult", {"stage": stage_name,
                                                   "snapshot_keys": list(snap)[:8]})
        except Exception as e:  # noqa: BLE001
            log.warning("strategy consult failed: %s", e)

    def _consult_gate(self, snapshot: dict, stage_name: str) -> bool:
        """Ask Jev/laya a quick noul: is the current situation a strategic
        turning point worth a main-LLM consult? Returns True if >= threshold."""
        questions = {
            "should_consult": {
                "type": "noul",
                "instructions": (
                    "Is the current game situation a strategic turning point "
                    "that deserves the commander's (main LLM) attention? "
                    "High-stakes or pivotal moments YES; routine steady-state NO."
                ),
            }
        }
        try:
            res = self.client.ask_sync(snapshot, questions)
            answers = res.get("answers", res) if isinstance(res, dict) else res
            val = float((answers.get("should_consult") or {}).get("noul", 0.0))
            th = float(getattr(self, "consult_threshold", 0.6) or 0.6)
            if self.on_event:
                self.on_event("strategy_gate", {"stage": stage_name,
                                                "noul": round(val, 3),
                                                "threshold": th,
                                                "consult": val >= th})
            return val >= th
        except Exception as e:  # noqa: BLE001
            log.warning("consult gate failed (default consult): %s", e)
            return True

    def _maybe_periodic_strategy(self) -> None:
        """For long battles (e.g. a YGO duel or Pokemon boss fight), re-consult
        every N rounds so the main LLM can adjust the plan mid-game."""
        if not self.strategy_stages or self.advisory is None:
            return
        cur = self.stage_machine.stage
        if cur not in self.strategy_stages:
            return
        self._strategy_round_counter += 1
        if self._strategy_round_counter % self.strategy_interval_rounds != 0:
            return
        self._request_strategy_consult(cur)

    def set_stage(self, stage: str) -> bool:
        return self.stage_machine.set(stage)

    def stage(self) -> str:
        return self.stage_machine.stage

    def _escalate(self, action: str, confidence: float) -> None:
        if self.escalator is not None:
            self.escalator.note(action, confidence, self.sid, self.game)
        elif self.on_event:
            self.on_event("escalate", {"action": action, "confidence": confidence})

    def _snapshot_stage(self, snapshot: dict) -> None:
        """Let the adapter report its stage via snapshot['stage']; adopt it."""
        s = snapshot.get("stage")
        if isinstance(s, str) and s:
            if self.set_stage(s):
                if self.on_event:
                    self.on_event("stage_change", {"old": self._last_stage, "new": s})
                self._last_stage = s

    # ---- lifecycle -------------------------------------------------

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.adapter:
            try:
                self.adapter.stop()
            except Exception:  # noqa: BLE001
                pass

    # ---- config hot-reload ----------------------------------------

    def apply_cfg(self, cfg: dict) -> None:
        """Re-apply runtime knobs without restarting the loop."""
        self.decision_interval = 1.0 / max(0.2, float(cfg.get("decision_hz", 1.0)))
        self.intent_ttl = float(cfg.get("intent_ttl", DEFAULT_INTENT_TTL))
        self.temperature = float(cfg.get("temperature", 2.0))
        self.goal_floor = float(cfg.get("goal_floor", 0.1))
        self.gate.low_risk_threshold = float(cfg.get("low_risk_threshold", 0.55))
        self.gate.high_risk_threshold = float(cfg.get("high_risk_threshold", 0.85))
        self.gate.use_baseline = bool(cfg.get("use_baseline", True))
        inst = cfg.get("game_instructions", "") or DEFAULT_GAME_INSTRUCTIONS
        self.builder.game_instructions = inst
        self.consult_ask_jev = bool(cfg.get("consult_ask_jev", True))
        self.strategy_advisory = bool(cfg.get("strategy_advisory", True))
        self.consult_threshold = float(cfg.get("consult_threshold", 0.6) or 0.6)
        ir = int(cfg.get("strategy_interval_rounds", 8) or 0)
        self.strategy_interval_rounds = max(1, ir) if ir > 0 else 10 ** 9  # 0 = disabled periodic

    def set_goal(self, goal: str) -> None:
        self.goal = goal
        self.memory.add_action("set_goal", "accepted", goal[:120])

    def set_playbook(self, playbook: str) -> None:
        self.playbook = playbook

    # ---- the loop ---------------------------------------------------

    async def _loop(self) -> None:
        log.info("decision loop started (%.1f Hz)", 1.0 / self.decision_interval)
        try:
            while self.running:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("engine tick error: %s", e)
                await asyncio.sleep(self.decision_interval)
        except asyncio.CancelledError:
            log.info("decision loop stopped")
            return

    async def _tick(self) -> None:
        self._loop_count += 1
        # Resolve the live backend each tick so laya autoinstall / Jev
        # fallback is reflected without restarting the engine.
        if self.backend_resolver is not None:
            try:
                self.client = self.backend_resolver() or self.client
            except Exception as e:  # noqa: BLE001
                log.warning("backend resolve failed: %s", e)
        # Strategic consult: on key-stage entry (handled in _on_stage_change)
        # and periodically mid-game (long duels/battles).
        self._maybe_periodic_strategy()
        if self.adapter is None or not self.adapter.health():
            # Bridge down — still try reconnect via health() tick.
            if self.adapter and not self.adapter.start():
                return
            if self.adapter is None or not self.adapter.health():
                return

        snapshot = self.adapter.get_state()
        if not snapshot:
            return

        # Adopt stage reported by the adapter (joining/dueling/…).
        self._snapshot_stage(snapshot)

        # Pre-input chain: if we have a planned action sequence and the state
        # hasn't changed (no interruption), execute the next action without
        # asking Jev again. This saves Jev calls on multi-step plays.
        if self._try_chain(snapshot):
            self._last_decision_at = time.monotonic()
            return

        now = time.monotonic()

        # Fire the next request (async, non-blocking) — throttle by decision_hz.
        if now - self._last_decision_at >= max(self.decision_interval, 0.1):
            state = self._build_state(snapshot)
            questions = self._build_questions(snapshot)
            self.client.ask_async(state, questions)

        # Drain the latest Jev answer from the background request.
        answer = self.client.ask_latest()
        if answer is not None:
            self._apply_answer(answer, snapshot)
            self._last_decision_at = now

        # Baseline / fallback execution when we have no fresh decision.
        if now - self._last_decision_at > self.intent_ttl:
            action = self.gate.decide(snapshot, nouls=None)
            if action != "wait":
                self._execute(action, snapshot)
            return

    # ---- state / questions (game-aware) -----------------------------

    def _build_state(self, snapshot: dict) -> dict:
        """Build the Jev state. YGO gets its duel-specific state builder;
        other games use the generic one."""
        if self.game == "ygo_engine":
            try:
                from .jev.ygo_state import build_ygo_state
                return build_ygo_state(
                    snapshot,
                    goal=self.goal,
                    persona=self.playbook and "" or "",
                    playbook=self.playbook,
                    recent_actions=self.memory.actions,
                    recent_hints=list(self.memory.hints),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("ygo state build failed, falling back: %s", e)
        return self.builder.build(
            snapshot,
            goal=self.goal,
            recent_actions=self.memory.actions,
            recent_hints=list(self.memory.hints),
            playbook=self.playbook,
        )

    def _build_questions(self, snapshot: dict) -> dict:
        """Build the Jev questions. YGO gets duel-specific questions."""
        if self.game == "ygo_engine":
            try:
                from .jev.ygo_state import build_ygo_questions
                return build_ygo_questions(snapshot, goal=self.goal)
            except Exception as e:  # noqa: BLE001
                log.warning("ygo questions build failed, falling back: %s", e)
        goals = self._offer_goals(snapshot)
        if goals:
            return build_goal_questions(goals, self.action_questions)
        return self.action_questions

    def _offer_goals(self, snapshot: dict) -> dict[str, str]:
        """Adapters may expose a 'goals' map; else fall back to action mode."""
        return snapshot.get("goals") or {}

    # ---- answer application ----------------------------------------

    def _apply_answer(self, answer: dict, snapshot: dict) -> None:
        answers = answer.get("answers", {})
        nouls = {}
        for key, a in answers.items():
            if isinstance(a, dict) and "noul" in a:
                nouls[key] = float(a.get("noul", 0.0))

        decision: dict[str, Any] = {"at": time.time(), "nouls": nouls}

        # YGO: the main decision is next_action (choice over legal actions).
        if self.game == "ygo_engine" and "next_action" in answers and isinstance(answers["next_action"], dict):
            na = answers["next_action"]
            action = str(na.get("choice", "")).strip()
            conf = float(na.get("confidence", 0.0) or 0.0)
            decision.update({"mode": "action", "action": action, "conf": conf,
                             "probs": na.get("probabilities", {})})
            # High-risk YGO actions (summon/activate/attack) with low
            # confidence -> escalate to the main LLM for a strategic check.
            if action in {"summon", "activate", "attack", "special_summon"} and conf < 0.6:
                self._escalate(action, conf)
            self._record_decision(decision)
            if action and action != "wait":
                self._execute(action, snapshot)
                # Plan a short follow-up chain (pre-input): if the next legal
                # actions are a natural continuation (e.g. summon → activate →
                # battle → attack), queue them so we don't re-ask Jev.
                self._plan_ygo_chain(snapshot, action)
            return

        if "next_goal" in answers and isinstance(answers["next_goal"], dict):
            ga = answers["next_goal"]
            probs = ga.get("probabilities") or {}
            goal, conf = self.gate.pick_goal(probs, self.temperature, self.goal_floor)
            decision.update({"mode": "goal", "goal": goal, "goal_conf": conf,
                             "probs": probs})
            self._record_decision(decision)
            # Escalation check: high-stakes goal with low confidence.
            if goal in {"attack", "summon", "activate", "flee"} and conf < 0.6:
                self._escalate(goal, conf)
            if goal != "wait":
                self._execute(goal, snapshot)
        else:
            dialog = bool(snapshot.get("dialog"))
            menu = bool((nouls.get("menu_open") or 0.0) >= 0.5)
            walkable = (snapshot.get("world") or {}).get("walkable")
            action = self.gate.pick_action(
                nouls,
                dialog_active=dialog,
                menu_open=menu,
                walkable=walkable,
                last_action=self._last_action,
                explore_hint=None,
            )
            decision.update({"mode": "action", "action": action})
            # Confidence of the picked action (for escalation). When the gate
            # fell back to "wait" because every action was low-confidence, the
            # highest noul still tells us how unsure the model is — escalate on
            # that instead of silently skipping.
            conf = nouls.get(action, 0.0)
            if action == "wait":
                best = max(nouls.values()) if nouls else 0.0
                if best < self.gate.low_risk_threshold:
                    self._escalate("wait(low_confidence)", best)
            elif action != "wait":
                self._escalate(action, conf)
            self._record_decision(decision)
            self._execute(action, snapshot)

    # ---- execution -------------------------------------------------

    def _execute(self, action: str, snapshot: dict) -> None:
        if action == "wait":
            self._last_action = "wait"
            return
        steps = self._action_steps(action, snapshot)
        ok = self.adapter.act(steps)
        outcome = "ok" if ok else "failed"
        self.memory.add_action(action, outcome)
        self._last_action = action
        self._last_action_at = time.monotonic()
        if ok and self.on_event:
            self.on_event("action", {"action": action, "snapshot_keys": list(snapshot)[:8]})

    # ---- pre-input action chain (plan a sequence, execute if not interrupted) ----

    def _state_hash(self, snapshot: dict) -> str:
        """A cheap fingerprint of the game state to detect interruptions."""
        g = (snapshot.get("game") or {}) if isinstance(snapshot, dict) else {}
        return f"{g.get('turn')}|{g.get('phase')}|{g.get('my_lp')}|{g.get('op_lp')}|{len(g.get('field_my', []))}|{len(g.get('field_op', []))}"

    def _is_interrupted(self, snapshot: dict) -> bool:
        """True when the state changed in a way that invalidates the chain
        (phase/turn/LP/field changed, or opponent responded)."""
        h = self._state_hash(snapshot)
        return h != self._chain_snapshot_hash

    def _try_chain(self, snapshot: dict) -> bool:
        """If we have a planned chain and the state hasn't changed, execute the
        next action in the chain WITHOUT asking Jev. Returns True when the
        chain was executed this tick (so we skip the Jev call)."""
        if not self._action_chain:
            return False
        if self._is_interrupted(snapshot):
            # Interrupted — drop the chain and re-decide from the new state.
            self._action_chain = []
            return False
        # Not interrupted — execute the next planned action.
        action = self._action_chain.pop(0)
        self._execute(action, snapshot)
        # Refresh the snapshot hash after executing (state changed by us).
        try:
            new_snap = self.adapter.get_state()
            self._chain_snapshot_hash = self._state_hash(new_snap)
        except Exception:  # noqa: BLE001
            pass
        return True

    def _plan_ygo_chain(self, snapshot: dict, last_action: str) -> None:
        """Plan a short follow-up action chain for YGO (pre-input).

        After a high-confidence action (e.g. summon), queue natural
        continuations (activate its effect, switch to battle, attack) so we
        don't re-ask Jev for each step. The chain is dropped if the state
        changes unexpectedly (interrupted)."""
        g = (snapshot.get("game") or {}) if isinstance(snapshot, dict) else {}
        legal = g.get("legal_actions", [])
        if not legal:
            return
        chain: list[str] = []
        # Common natural continuations after a summon.
        if last_action in ("summon", "special_summon"):
            for a in legal:
                if a.get("action") == "activate" and a.get("card"):
                    chain.append("activate")  # activate the summoned card's effect
                    break
        # After building board, switch to battle and attack if safe.
        for a in legal:
            if a.get("action") == "switch" and a.get("phase") == "battle":
                chain.append("switch")
                break
        for a in legal:
            if a.get("action") in ("attack", "direct_attack"):
                chain.append(a["action"])
                break
        if chain:
            self._action_chain = chain
            self._chain_snapshot_hash = self._state_hash(snapshot)

    def _plan_chain(self, snapshot: dict) -> None:
        """Generic chain planning (kept for other games; YGO uses
        _plan_ygo_chain)."""
        if not self._action_chain:
            return
        self._chain_snapshot_hash = self._state_hash(snapshot)

    def _action_steps(self, action: str, snapshot: dict) -> list[dict]:
        """Map an engine action to adapter steps. YGO actions are duel
        actions (summon/activate/attack/…); other games use generic
        primitives (confirm/move_up/…)."""
        if self.game == "ygo_engine":
            # YGO legal actions are passed through to the bridge.
            # Action names come from the adapter's legal_actions list.
            g = (snapshot.get("game") or {}) if isinstance(snapshot, dict) else {}
            legal = g.get("legal_actions", [])
            for a in legal:
                if (a.get("action") or a.get("type")) == action:
                    return [a]
            # Fallback: try a best-effort mapping for common actions.
            return [{"action": action}]
        frames = 8
        if action in ACTION_NAMES:
            frames = 8
        return [{"action": action, "frames": frames}]

    # ---- records ---------------------------------------------------

    def _record_decision(self, decision: dict) -> None:
        self.recent_decisions.append(decision)
        if len(self.recent_decisions) > 200:
            self.recent_decisions = self.recent_decisions[-200:]

    def summary(self) -> dict:
        return {
            "running": self.running,
            "loop_count": self._loop_count,
            "goal": self.goal,
            "last_action": self._last_action,
            "recent_decisions": self.recent_decisions[-10:],
            "jev_stats": self.client.stats,
        }
