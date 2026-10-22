"""Integration test: YGO duel decisions + Pokemon state/adventure log.

Verifies the YGO-specific state builder/questions and the enhanced Pokemon
adapter's adventure log — all offline (no real Jev, no real game).
"""
from __future__ import annotations

import asyncio
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from plugin.jev.ygo_state import build_ygo_state, build_ygo_questions  # noqa: E402
from plugin.bridge.adapters.pokemon_pyboy import AdventureLog  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def test_ygo_state():
    print("== ygo state builder ==")
    snap = {
        "game": {
            "my_lp": 3200, "op_lp": 5400, "turn": 3, "phase": "main1",
            "is_my_turn": True, "opponent": "Alice", "opponent_hand_count": 3,
            "hand": [
                {"name": "Blue-Eyes", "atk": 3000, "def": 2500, "level": 8, "type": "monster", "attribute": "light"},
                {"name": "Mystic Tomato", "atk": 1400, "def": 1100, "level": 4, "type": "monster"},
                {"name": "Mystical Space Typhoon", "type": "spell"},
            ],
            "field_my": [{"name": "Dark Magician", "atk": 2500, "def": 2100, "position": "attack"}],
            "field_op": [{"name": "Summoned Skull", "atk": 2500, "def": 1200, "position": "attack"}],
            "grave_my": [{"name": "Monster Reborn"}],
            "legal_actions": [
                {"action": "summon", "card": "Mystic Tomato", "position": "attack"},
                {"action": "set", "card": "Mystical Space Typhoon"},
                {"action": "activate", "card": "Mystic Tomato", "effect": "flip summon"},
                {"action": "attack", "card": "Dark Magician", "target": "Summoned Skull"},
                {"action": "switch", "phase": "battle"},
            ],
        },
        "stage": "dueling",
    }
    state = build_ygo_state(snap, goal="win the duel", playbook="play defensively")
    check("instructions present", "instructions" in state)
    check("goal injected", "win the duel" in state["instructions"])
    check("playbook injected", "DUEL KNOWLEDGE" in state["instructions"])
    check("my.lp", state["my"]["lp"] == 3200)
    check("my.hand_count", state["my"]["hand_count"] == 3)
    check("opponent.lp", state["opponent"]["lp"] == 5400)
    check("opponent hand count only", "hand_count" in state["opponent"] and "hand" not in state["opponent"])
    check("duel.turn", state["duel"]["turn"] == 3)
    check("duel.phase", state["duel"]["phase"] == "main1")
    check("duel.is_my_turn", state["duel"]["is_my_turn"] is True)
    check("legal_actions present", len(state["legal_actions"]) == 5)
    check("legal action summon", any(a["action"] == "summon" for a in state["legal_actions"]))

    qs = build_ygo_questions(snap, goal="win")
    check("next_action present", "next_action" in qs)
    check("next_action is choice", qs["next_action"]["type"] == "choice")
    check("next_action has legal options", "summon" in qs["next_action"]["criteria"])
    check("danger present", "danger" in qs)
    check("should_attack present (my turn)", "should_attack" in qs)
    check("should_change_plan absent (my turn, not blocked)", "should_change_plan" not in qs)

    # opponent's turn -> interaction questions
    snap2 = dict(snap)
    snap2["game"] = dict(snap["game"])
    snap2["game"]["is_my_turn"] = False
    qs2 = build_ygo_questions(snap2)
    check("should_change_plan present (opponent turn)", "should_change_plan" in qs2)
    check("is_countered present (opponent turn)", "is_countered" in qs2)


def test_adventure_log():
    print("== adventure log ==")
    with tempfile.TemporaryDirectory() as tmp:
        log = AdventureLog(Path(tmp), enabled=True)
        log.add("到达", "到达 常青市")
        log.add("捕获", "捕获皮卡丘")
        log.add("道馆", "获得第 1 枚徽章")
        check("entries recorded", len(log.entries) == 3)
        recent = log.recent(2)
        check("recent 2", len(recent) == 2)
        check("recent order", recent[-1]["event"] == "道馆")
        # reload from disk
        log2 = AdventureLog(Path(tmp), enabled=True)
        check("persisted to disk", len(log2.entries) == 3)
        log.clear()
        check("clear", len(log.entries) == 0)


def test_ygo_engine_state_decision():
    print("== ygo engine decision loop (mock Jev) ==")
    from plugin.jev.gate import DecisionGate
    from plugin.jev.client import JevClient
    from plugin.engine import DecisionEngine

    class FakeYgoAdapter:
        name = "ygo_engine"
        def __init__(self): self.steps = []
        def start(self): return True
        def stop(self): pass
        def health(self): return True
        def get_state(self):
            return {
                "game": {
                    "my_lp": 3200, "op_lp": 5400, "turn": 3, "phase": "main1",
                    "is_my_turn": True, "opponent": "Alice", "opponent_hand_count": 3,
                    "hand": [{"name": "Mystic Tomato", "atk": 1400, "def": 1100}],
                    "field_my": [{"name": "Dark Magician", "atk": 2500, "position": "attack"}],
                    "field_op": [{"name": "Summoned Skull", "atk": 2500, "position": "attack"}],
                    "legal_actions": [
                        {"action": "summon", "card": "Mystic Tomato", "position": "attack"},
                        {"action": "attack", "card": "Dark Magician", "target": "Summoned Skull"},
                        {"action": "set", "card": "Mystic Tomato"},
                        {"action": "switch", "phase": "battle"},
                    ],
                },
                "stage": "dueling",
            }
        def act(self, steps): self.steps.extend(steps); return True

    class MockJevForYgo:
        def __init__(self):
            self.stats = {"sent": 0, "failed": 0, "input_tokens": 0, "last_ms": 0, "last_error": None}
            self._pending = None
        def ask_async(self, state, questions):
            self.stats["sent"] += 1
            # Simulate Jev answering: pick summon with high confidence
            self._pending = {"answers": {
                "next_action": {"type": "choice", "choice": "summon",
                                "probabilities": {"summon": 0.7, "attack": 0.2, "set": 0.1},
                                "confidence": 0.88},
                "danger": {"type": "score", "score": 1.5},
                "should_attack": {"type": "noul", "noul": 0.3},
            }, "usage": {"input_tokens": 400}}
        def ask_latest(self): return self._pending
        def ask_sync(self, s, q): return {"answers": {}}

    adapter = FakeYgoAdapter()
    client = MockJevForYgo()
    gate = DecisionGate(low_risk_threshold=0.55, high_risk_threshold=0.85)
    eng = DecisionEngine(adapter, client, gate, decision_hz=10, intent_ttl=1.5,
                         game="ygo_engine", sid="test", on_event=lambda k, p: None)
    async def run():
        await eng.start()
        await asyncio.sleep(0.05)
        await eng.stop()
    asyncio.run(run())
    check("ygo engine ran", client.stats["sent"] >= 1)
    check("ygo action executed", any(s.get("action") == "summon" for s in adapter.steps), str(adapter.steps[:3]))


if __name__ == "__main__":
    test_ygo_state()
    test_adventure_log()
    test_ygo_engine_state_decision()
    print(f"\n== {PASS} passed, {FAIL} failed ==")
    sys.exit(1 if FAIL else 0)