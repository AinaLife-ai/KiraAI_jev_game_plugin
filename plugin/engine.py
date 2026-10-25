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
from .ygo_ops import act_payload, match_action, options_map, resolve_option

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
        auto_decision: bool = True,
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
        # auto_decision=False -> the main LLM plays through tools; the loop
        # only keeps the snapshot cache/stage fresh and asks for input.
        self.auto_decision = auto_decision
        self.cached_snapshot: dict = {}
        self._backend_ok = False
        self._backend_checked_at = 0.0
        self._last_input_fingerprint = ""
        self._last_input_prompt_at = 0.0
        self._room_fingerprint = None
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
        # Pre-input chain: planned legal options (semantic dicts) executed
        # while they still match the live legal list.
        self._action_chain: list[dict] = []
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
        """Schedule a main-LLM strategic consult (never blocks the loop).

        The optional Jev/laya gate runs in a worker thread; without a
        backend we consult directly — the main LLM *is* the strategist.
        """
        if not getattr(self, "strategy_advisory", True):
            return
        if self.advisory is None or self.adapter is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (offline/unit-test context): run inline. Inside
            # the decision loop there is always a running loop, so this path
            # can never stall KiraAI.
            self._strategy_consult_inline(stage_name)
            return
        loop.create_task(self._strategy_consult_async(stage_name))

    def _strategy_consult_inline(self, stage_name: str) -> None:
        try:
            snap = self.adapter.get_state() or {}
            if self.consult_ask_jev and self.client is not None and self._backend_available():
                if not self._consult_gate(snap, stage_name):
                    if self.on_event:
                        self.on_event("strategy_skipped", {"stage": stage_name})
                    return
            self.advisory(self.sid, stage_name, snap)
            if self.on_event:
                self.on_event("strategy_consult", {"stage": stage_name,
                                                   "snapshot_keys": list(snap)[:8]})
        except Exception as e:  # noqa: BLE001
            log.warning("strategy consult failed: %s", e)

    async def _strategy_consult_async(self, stage_name: str) -> None:
        try:
            snap = await asyncio.to_thread(self.adapter.get_state) or {}
            if self.consult_ask_jev and self.client is not None and self._backend_available():
                gate_ok = await asyncio.to_thread(self._consult_gate, snap, stage_name)
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
                await asyncio.sleep(self._poll_delay())
        except asyncio.CancelledError:
            log.info("decision loop stopped")
            return

    # ---- backend / mode gating -------------------------------------------

    def _backend_available(self) -> bool:
        """Cheap cached probe (10s): is there a real decision backend?

        Jev without an API key and laya without weights both mean "no
        backend" — in that case we must NOT improvise baseline moves; the
        main LLM drives the duel through tools instead.
        """
        now = time.monotonic()
        if now - self._backend_checked_at < 10.0:
            return self._backend_ok
        self._backend_checked_at = now
        client = self.client
        ok = False
        if client is not None:
            if isinstance(client, JevClient):
                ok = bool(client.has_key)
            elif getattr(client, "_agent", None) is not None:
                ok = True
            elif callable(getattr(client, "ensure_loaded", None)):
                try:
                    ok = bool(client.ensure_loaded())
                except Exception:  # noqa: BLE001
                    ok = False
            elif hasattr(client, "has_key"):
                ok = bool(getattr(client, "has_key", False))
            else:
                # Custom/test backends without a key concept are usable.
                ok = True
        self._backend_ok = ok
        return ok

    def _decision_enabled(self) -> bool:
        return bool(self.auto_decision) and self._backend_available()

    def _needs_decision(self, snapshot: dict) -> bool:
        """No legal options -> no backend call (opponent's turn / waiting)."""
        if self.game == "ygo_engine":
            g = (snapshot.get("game") or {}) if isinstance(snapshot, dict) else {}
            if "legal_actions" not in g and "options" not in g:
                return True  # legacy snapshot without an option list
            return bool(options_map(g))
        return True

    def _maybe_request_input(self, snapshot: dict) -> None:
        """Manual/LLM-driven mode: tell the main LLM its input is needed.

        Fingerprint + time throttled so a waiting duel never spams chat.
        """
        if self.game != "ygo_engine":
            return
        g = (snapshot.get("game") or {}) if isinstance(snapshot, dict) else {}
        opts = options_map(g)
        if not opts or g.get("is_duel_active") is False:
            return
        fingerprint = (f"{g.get('turn')}|{g.get('phase')}|{len(opts)}|{g.get('my_lp')}|"
                       f"{g.get('op_lp')}|{bool(g.get('response_window'))}")
        now = time.monotonic()
        if now - self._last_input_prompt_at < 45.0:
            return
        if fingerprint == self._last_input_fingerprint and now - self._last_input_prompt_at < 90.0:
            return
        self._last_input_fingerprint = fingerprint
        self._last_input_prompt_at = now
        if self.on_event:
            reason = ("对手动作等待你响应（连锁或跳过）" if g.get("response_window")
                      else "我方回合" if g.get("is_my_turn") else "轮到你决策")
            self.on_event("need_input", {"options": len(opts), "reason": reason})

    def _poll_delay(self) -> float:
        """Adaptive polling: duels need freshness, idle rooms need almost no
        traffic. This is what keeps bridge calls (and CPU) to a minimum."""
        base = self.decision_interval
        g = (self.cached_snapshot or {}).get("game") or {}
        if g.get("is_duel_active"):
            if g.get("decision") or g.get("response_window"):
                # Faster while an answer is due, but never slower than base.
                return min(base, max(0.3, base * 0.5))
            return base
        # Room lifecycle (waiting for players/ready/start) must stay snappy:
        # opponent joins and duel starts are exactly the events we report.
        stage = str((self.cached_snapshot or {}).get("stage") or "")
        if stage in ("joining", "confirming_opponent", "readying", "dueling", "ended"):
            return base
        # Only a genuinely idle engine (no room at all) polls slowly.
        return max(base, 3.0)

    def _check_room_events(self, snapshot: dict) -> None:
        """Emit a notice when the *room* changes (opponent joins, both ready,
        duel starts/ends) so the main LLM can drive the flow proactively."""
        if self.game != "ygo_engine":
            return
        room = (snapshot or {}).get("room") or {}
        g = (snapshot or {}).get("game") or {}
        players = tuple(str(p) for p in (room.get("players") or []))
        ready = room.get("ready") if isinstance(room.get("ready"), dict) else {}
        ready_key = tuple(sorted((str(k), bool(v)) for k, v in ready.items()))
        active = bool(g.get("is_duel_active"))
        winner = str(g.get("winner") or "")
        fingerprint = (players, ready_key, active, winner)
        if self._room_fingerprint is None:
            self._room_fingerprint = fingerprint
            return
        if fingerprint == self._room_fingerprint:
            return
        old = self._room_fingerprint
        self._room_fingerprint = fingerprint
        if active and not old[2]:
            text = "🎮 对局开始！用 game_ygo_duel op=state 看局面，op=act option=编号 操作。"
        elif not active and old[2]:
            text = (f"🎮 对局结束{('，胜者：' + winner) if winner else ''}。"
                    "可 game_ygo op=leave 退房，或 op=host/join 再来一局。")
        elif len(players) > len(old[0]):
            newcomers = [p for p in players if p not in old[0]] or list(players[-1:])
            text = (f"🎮 对手已进房：{', '.join(newcomers)}。"
                    "请 game_ygo op=ready 准备；双方就绪后房主 op=start 开局。")
        elif ready_key != old[1]:
            if ready and len(ready) >= 2 and all(ready.values()):
                text = "🎮 双方都准备好了，房主用 game_ygo op=start 开局。"
            elif ready:
                text = "🎮 房间准备状态：" + ", ".join(
                    f"{k}={'已准备' if v else '未准备'}" for k, v in ready.items())
            else:
                return
        else:
            return
        if self.on_event:
            self.on_event("room_update", {"text": text})

    async def _tick(self) -> None:
        self._loop_count += 1
        # Resolve the live backend each tick so laya autoinstall / Jev
        # fallback is reflected without restarting the engine.
        if self.backend_resolver is not None:
            try:
                self.client = self.backend_resolver() or self.client
            except Exception as e:  # noqa: BLE001
                log.warning("backend resolve failed: %s", e)
        if self.adapter is None:
            return
        try:
            if not await asyncio.to_thread(self.adapter.health):
                if not await asyncio.to_thread(self.adapter.start):
                    return
                if not await asyncio.to_thread(self.adapter.health):
                    return
            snapshot = await asyncio.to_thread(self.adapter.get_state)
        except Exception as e:  # noqa: BLE001
            log.warning("adapter tick failed: %s", e)
            return
        if not snapshot:
            return
        self.cached_snapshot = snapshot

        # Adopt stage reported by the adapter (joining/dueling/…).
        self._snapshot_stage(snapshot)
        self._check_room_events(snapshot)

        if not self._decision_enabled():
            # Main-LLM/manual play: cache + stage only, never auto-fire.
            self._maybe_request_input(snapshot)
            return

        # Pre-input chain: execute the next planned option only if it is
        # still legal (semantic match, not a stale list index).
        if await self._try_chain(snapshot):
            self._last_decision_at = time.monotonic()
            return

        if not self._needs_decision(snapshot):
            return

        # Strategic consult: on key-stage entry (handled in _on_stage_change)
        # and periodically mid-game (long duels/battles).
        self._maybe_periodic_strategy()
        now = time.monotonic()

        # Fire the next request (async, non-blocking) — throttle by decision_hz.
        if now - self._last_decision_at >= max(self.decision_interval, 0.1):
            state = self._build_state(snapshot)
            questions = self._build_questions(snapshot)
            if questions:
                self.client.ask_async(state, questions)

        # Drain the latest Jev answer from the background request.
        answer = self.client.ask_latest()
        if answer is not None:
            await self._apply_answer(answer, snapshot)
            self._last_decision_at = now

        # Baseline fallback only makes sense for button-mashing games; a duel
        # must never fire random primitives while waiting for a decision.
        if self.game != "ygo_engine" and now - self._last_decision_at > self.intent_ttl:
            action = self.gate.decide(snapshot, nouls=None)
            if action != "wait":
                await self._execute(action, snapshot)
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
            g = (snapshot.get("game") or {}) if isinstance(snapshot, dict) else {}
            if "legal_actions" in g or "options" in g:
                try:
                    from .jev.ygo_state import build_ygo_questions
                    return build_ygo_questions(snapshot, goal=self.goal)
                except Exception as e:  # noqa: BLE001
                    log.warning("ygo questions build failed, falling back: %s", e)
                log.warning("ygo questions build failed, falling back: %s", e)
        goals = self._offer_goals(snapshot)
        if goals:
            return build_goal_questions(goals, self.action_questions)
        return self.action_questions

    def _offer_goals(self, snapshot: dict) -> dict[str, str]:
        """Adapters may expose a 'goals' map; else fall back to action mode."""
        return snapshot.get("goals") or {}

    # ---- answer application ----------------------------------------

    async def _apply_answer(self, answer: dict, snapshot: dict) -> None:
        answers = answer.get("answers", {})
        nouls = {}
        for key, a in answers.items():
            if isinstance(a, dict) and "noul" in a:
                nouls[key] = float(a.get("noul", 0.0))

        decision: dict[str, Any] = {"at": time.time(), "nouls": nouls}

        # YGO: the main decision is next_action (choice over legal option ids).
        if self.game == "ygo_engine" and "next_action" in answers and isinstance(answers["next_action"], dict):
            na = answers["next_action"]
            ref = str(na.get("choice", "")).strip()
            conf = float(na.get("confidence", 0.0) or 0.0)
            action = resolve_option(snapshot, ref)
            if action is None:
                # Stale answer (the duel moved on): never blind-fire a move
                # whose option no longer exists.
                decision.update({"mode": "action", "action": ref, "skipped": "stale_option"})
                self._record_decision(decision)
                self._action_chain = []
                log.info("ygo decision skipped, option %r no longer legal", ref)
                return
            name = str(action.get("action", ""))
            decision.update({"mode": "action", "action": name, "option": ref,
                             "card": action.get("card", ""), "conf": conf,
                             "probs": na.get("probabilities", {})})
            # High-risk YGO actions (summon/activate/attack) with low
            # confidence -> escalate to the main LLM for a strategic check.
            if name in {"summon", "activate", "attack", "special_summon"} and conf < 0.6:
                self._escalate(name, conf)
            self._record_decision(decision)
            if name and name != "wait":
                ok = await self._execute_option(action, ref, snapshot)
                if ok:
                    try:
                        fresh = await asyncio.to_thread(self.adapter.get_state) or snapshot
                    except Exception:  # noqa: BLE001
                        fresh = snapshot
                    self.cached_snapshot = fresh
                    # Plan a short follow-up chain on the post-move state.
                    self._plan_ygo_chain(fresh, action)
            return

        if self.game == "ygo_engine" and (
            "legal_actions" in (snapshot.get("game") or {}) or "options" in (snapshot.get("game") or {})
        ):
            # A duel snapshot without a next_action answer: wait for a real
            # decision instead of mashing generic primitives.
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
                await self._execute(goal, snapshot)
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
            await self._execute(action, snapshot)

    # ---- execution -------------------------------------------------

    async def _execute(self, action: str, snapshot: dict) -> None:
        if action == "wait":
            self._last_action = "wait"
            return
        steps = self._action_steps(action, snapshot)
        if not steps:
            log.info("action %r has no legal steps, skipped", action)
            return
        ok = await asyncio.to_thread(self.adapter.act, steps)
        outcome = "ok" if ok else "failed"
        self.memory.add_action(action, outcome)
        self._last_action = action
        self._last_action_at = time.monotonic()
        if ok and self.on_event:
            self.on_event("action", {"action": action, "snapshot_keys": list(snapshot)[:8]})

    async def _execute_option(self, action: dict, ref: str, snapshot: dict) -> bool:
        """Submit exactly one legal YGO option and record the real verdict."""
        payload = act_payload(action, ref)
        ok = await asyncio.to_thread(self.adapter.act, [payload])
        name = str(action.get("action", "?"))
        card = str(action.get("card", "") or "")
        self.memory.add_action(name, "ok" if ok else "failed", card[:60])
        self._last_action = name + (f":{card}" if card else "")
        self._last_action_at = time.monotonic()
        if not ok:
            self._action_chain = []
        if self.on_event:
            self.on_event("action", {"action": name, "card": card, "ok": ok, "quiet": True})
        return ok

    # ---- pre-input action chain (plan a sequence, execute if not interrupted) ----

    async def _try_chain(self, snapshot: dict) -> bool:
        """Execute the next planned option if it is *still legal*.

        Planned options are matched semantically (action/card/target/phase),
        never by list position: our own previous move changes the state, so
        the old "state hash changed = interrupted" rule both killed every
        chain and could fire the wrong card. A mismatch simply ends the chain
        and falls back to a fresh decision.
        """
        if not self._action_chain:
            return False
        planned = self._action_chain[0]
        action = match_action(snapshot, planned)
        if action is None:
            self._action_chain = []
            return False
        self._action_chain.pop(0)
        await self._execute_option(action, "", snapshot)
        return True

    def _plan_ygo_chain(self, snapshot: dict, last_action: dict) -> None:
        """Plan up to 3 natural follow-up options on the post-move state.

        Only low-surprise continuations are queued (a summon effect, phase
        switch into battle, one best attack). Anything that stops matching
        the live legal list aborts the chain instead of guessing.
        """
        if not self.auto_decision:
            return
        legal = options_map(snapshot)
        if not legal:
            self._action_chain = []
            return
        actions = list(legal.values())
        chain: list[dict] = []
        if last_action.get("action") in ("summon", "summon_def", "special_summon", "set"):
            for a in actions:
                if a.get("action") == "activate" and a.get("card"):
                    chain.append(a)
                    break
        if last_action.get("action") != "attack":
            for a in actions:
                if a.get("action") == "switch" and a.get("phase") == "battle":
                    chain.append(a)
                    break
        attackers = [a for a in actions if a.get("action") in ("attack", "direct_attack")]
        if attackers and len(chain) < 3:
            best = max(attackers, key=lambda a: a.get("atk") or 0)
            chain.append(best)
        self._action_chain = chain[:3]

    def _action_steps(self, action: str, snapshot: dict) -> list[dict]:
        """Map an engine action to adapter steps. YGO actions are duel
        actions (summon/activate/attack/…); other games use generic
        primitives (confirm/move_up/…)."""
        if self.game == "ygo_engine":
            # Only a currently-legal option may be submitted; an unknown
            # action name yields no steps (never a blind best-effort move).
            g = (snapshot.get("game") or {}) if isinstance(snapshot, dict) else {}
            for a in g.get("legal_actions", []):
                if (a.get("action") or a.get("type")) == action:
                    return [a]
            return []
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
