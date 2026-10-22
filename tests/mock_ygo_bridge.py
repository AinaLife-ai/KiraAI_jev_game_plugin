"""Mock YGO bridge — simulates a real YGOPro engine HTTP service.

Implements the same bridge contract our YgoEngineAdapter expects:
    GET  /health
    GET  /state
    POST /act
    POST /join
    POST /ready
    POST /set_deck

Plus a simple built-in opponent AI so a full duel can play out:
draw phase → main phase → battle phase → end, alternating turns.

Run: python3 tests/mock_ygo_bridge.py
"""
from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---- simple card pool (name -> atk/def/level/type) -------------------
CARDS = {
    "Dark Magician":      {"atk": 2500, "def": 2100, "level": 7, "type": "monster", "attribute": "dark"},
    "Blue-Eyes White Dragon": {"atk": 3000, "def": 2500, "level": 8, "type": "monster", "attribute": "light"},
    "Mystic Tomato":      {"atk": 1400, "def": 1100, "level": 4, "type": "monster", "attribute": "dark"},
    "Summoned Skull":     {"atk": 2500, "def": 1200, "level": 6, "type": "monster", "attribute": "dark"},
    "Gemini Elf":         {"atk": 1900, "def": 900,  "level": 4, "type": "monster", "attribute": "earth"},
    "Mystical Space Typhoon": {"atk": 0, "def": 0, "level": 0, "type": "spell", "attribute": "spell"},
    "Pot of Greed":       {"atk": 0, "def": 0, "level": 0, "type": "spell", "attribute": "spell"},
    "Monster Reborn":     {"atk": 0, "def": 0, "level": 0, "type": "spell", "attribute": "spell"},
    "Trap Hole":          {"atk": 0, "def": 0, "level": 0, "type": "trap", "attribute": "trap"},
    "Mirror Force":       {"atk": 0, "def": 0, "level": 0, "type": "trap", "attribute": "trap"},
}

DECK_LIST = list(CARDS.keys())


class DuelState:
    """One duel: two players, alternating turns, legal actions."""

    def __init__(self, opponent: str = "Alice"):
        self.opponent = opponent
        self.turn = 0
        self.phase = "draw"
        self.is_my_turn = True
        self.my_lp = 8000
        self.op_lp = 8000
        self.my_hand = random.sample(DECK_LIST, 5)
        self.op_hand = random.sample(DECK_LIST, 5)
        self.op_hand_count = 5
        self.my_field: list[dict] = []
        self.op_field: list[dict] = []
        self.my_grave: list[str] = []
        self.op_grave: list[str] = []
        self.my_banished: list[str] = []
        self.op_banished: list[str] = []
        self.duel_active = True
        self.winner = ""
        self._log: list[str] = []

    # ---- helpers ------------------------------------------------------

    def _card(self, name: str) -> dict:
        c = dict(CARDS.get(name, {}))
        c["name"] = name
        return c

    def _log_ev(self, msg: str):
        self._log.append(f"[T{self.turn} {self.phase}] {msg}")

    # ---- legal actions -------------------------------------------------

    def legal_actions(self) -> list[dict]:
        if not self.duel_active:
            return []
        acts = []
        if self.phase == "main1" or self.phase == "main2":
            for card in self.my_hand:
                info = CARDS.get(card, {})
                if info.get("type") == "monster":
                    acts.append({"action": "summon", "card": card, "position": "attack"})
                    acts.append({"action": "summon_def", "card": card, "position": "defense"})
                    acts.append({"action": "set", "card": card, "position": "face-down"})
                elif info.get("type") == "spell":
                    acts.append({"action": "activate", "card": card, "effect": "activate"})
                elif info.get("type") == "trap":
                    acts.append({"action": "set", "card": card, "position": "face-down"})
        if self.phase == "battle":
            for m in self.my_field:
                if m.get("position") == "attack":
                    acts.append({"action": "attack", "card": m["name"], "target": "opponent_field"})
                    if not self.op_field:
                        acts.append({"action": "direct_attack", "card": m["name"]})
        if self.phase in ("main1", "main2", "battle"):
            acts.append({"action": "switch", "phase": self._next_phase()})
        return acts

    def _next_phase(self) -> str:
        order = ["draw", "standby", "main1", "battle", "main2", "end"]
        try:
            i = order.index(self.phase)
        except ValueError:
            i = 0
        return order[(i + 1) % len(order)]

    # ---- action execution ----------------------------------------------

    def apply(self, action: dict) -> bool:
        if not self.duel_active:
            return False
        a = action.get("action", "")
        card = action.get("card", "")
        pos = action.get("position", "")
        # Auto-advance draw/standby phases regardless of action (they have no
        # decisions — the engine just ticks through them).
        if self.phase in ("draw", "standby"):
            self.phase = self._next_phase()
            self._log_ev(f"自动进入 {self.phase}")
            if a not in ("switch", "cancel", "wait", ""):
                pass  # fall through to apply the real action in the new phase
            else:
                return True
        if a == "summon" and card in self.my_hand:
            self.my_hand.remove(card)
            c = self._card(card)
            c["position"] = pos or "attack"
            self.my_field.append(c)
            self._log_ev(f"我方召唤 {card}（{pos or 'attack'}）")
            return True
        if a == "summon_def" and card in self.my_hand:
            self.my_hand.remove(card)
            c = self._card(card)
            c["position"] = "defense"
            self.my_field.append(c)
            self._log_ev(f"我方召唤 {card}（守备）")
            return True
        if a == "set" and card in self.my_hand:
            self.my_hand.remove(card)
            c = self._card(card)
            c["position"] = "face-down"
            self.my_field.append(c)
            self._log_ev(f"我方盖放 {card}")
            return True
        if a == "activate" and card in self.my_hand:
            self.my_hand.remove(card)
            self.my_grave.append(card)
            self._log_ev(f"我方发动 {card}")
            # After activating, advance the phase so we don't loop in main1.
            self.phase = self._next_phase()
            self._log_ev(f"我方进入 {self.phase}")
            return True
        if a == "attack":
            # Simple battle resolution: destroy opponent's weakest monster or direct.
            if self.op_field:
                target = min(self.op_field, key=lambda m: m.get("atk", 0))
                atk = next((m for m in self.my_field if m["name"] == card), None)
                if atk and atk.get("atk", 0) >= target.get("atk", 0):
                    self.op_field.remove(target)
                    self.op_grave.append(target["name"])
                    self.op_lp -= max(0, atk.get("atk", 0) - target.get("atk", 0))
                    self._log_ev(f"我方 {card} 击破 {target['name']}，对方 LP -{max(0, atk.get('atk',0)-target.get('atk',0))}")
                else:
                    self.my_grave.append(card)
                    self.my_field = [m for m in self.my_field if m["name"] != card]
                    self.my_lp -= max(0, target.get("atk", 0) - (atk.get("atk", 0) if atk else 0))
                    self._log_ev(f"我方 {card} 被 {target['name']} 反杀")
            else:
                atk = next((m for m in self.my_field if m["name"] == card), None)
                if atk:
                    dmg = atk.get("atk", 0)
                    self.op_lp -= dmg
                    self._log_ev(f"我方 {card} 直接攻击，对方 LP -{dmg}")
            self._check_end()
            return True
        if a == "direct_attack":
            atk = next((m for m in self.my_field if m["name"] == card), None)
            if atk:
                dmg = atk.get("atk", 0)
                self.op_lp -= dmg
                self._log_ev(f"我方 {card} 直接攻击，对方 LP -{dmg}")
            self._check_end()
            return True
        if a == "switch":
            self.phase = action.get("phase", self._next_phase())
            self._log_ev(f"我方进入 {self.phase}")
            return True
        if a == "cancel":
            self._log_ev("我方取消/跳过")
            return True
        return False

    # ---- opponent AI --------------------------------------------------

    def opponent_turn(self):
        if not self.duel_active or self.is_my_turn:
            return
        # Draw
        if self.op_hand_count < 6:
            new_card = random.choice(DECK_LIST)
            self.op_hand.append(new_card)
            self.op_hand_count += 1
            self._log_ev("对方抽卡")
        # Simple AI: summon the strongest monster it can, or set a trap.
        if self.phase in ("main1", "main2"):
            monsters = [c for c in self.op_hand if CARDS.get(c, {}).get("type") == "monster"]
            if monsters:
                best = max(monsters, key=lambda c: CARDS.get(c, {}).get("atk", 0))
                self.op_hand.remove(best)
                self.op_hand_count -= 1
                c = self._card(best)
                c["position"] = "attack"
                self.op_field.append(c)
                self._log_ev(f"对方召唤 {best}")
            else:
                traps = [c for c in self.op_hand if CARDS.get(c, {}).get("type") == "trap"]
                if traps:
                    t = traps[0]
                    self.op_hand.remove(t)
                    self.op_hand_count -= 1
                    self._log_ev(f"对方盖放 {t}")
        if self.phase == "battle" and self.op_field and self.my_field:
            attacker = max(self.op_field, key=lambda m: m.get("atk", 0))
            target = min(self.my_field, key=lambda m: m.get("atk", 0))
            if attacker.get("atk", 0) >= target.get("atk", 0):
                self.my_field.remove(target)
                self.my_grave.append(target["name"])
                self.my_lp -= max(0, attacker.get("atk", 0) - target.get("atk", 0))
                self._log_ev(f"对方 {attacker['name']} 击破 {target['name']}")
            self._check_end()
        # Advance phase
        self.phase = self._next_phase()
        if self.phase == "draw":
            self.turn += 1
            self.is_my_turn = True
            self._log_ev("我方回合开始")

    def _check_end(self):
        if self.my_lp <= 0:
            self.duel_active = False
            self.winner = self.opponent
            self._log_ev("对方胜利")
        elif self.op_lp <= 0:
            self.duel_active = False
            self.winner = "我方"
            self._log_ev("我方胜利")

    # ---- serialization -------------------------------------------------

    def to_state(self) -> dict:
        return {
            "game": {
                "my_lp": self.my_lp,
                "op_lp": self.op_lp,
                "turn": self.turn,
                "phase": self.phase,
                "is_my_turn": self.is_my_turn,
                "is_duel_active": self.duel_active,
                "opponent": self.opponent,
                "opponent_hand_count": self.op_hand_count,
                "hand": [self._card(c) for c in self.my_hand],
                "field_my": self.my_field,
                "field_op": self.op_field,
                "grave_my": self.my_grave,
                "grave_op": self.op_grave,
                "banished_my": self.my_banished,
                "banished_op": self.op_banished,
                "extra_deck": [],
                "legal_actions": self.legal_actions(),
            },
            "stage": "dueling" if self.duel_active else "ended",
        }


# ---- HTTP bridge -------------------------------------------------------

class YgoBridgeHandler(BaseHTTPRequestHandler):
    duel = DuelState()
    opponent_ready = True

    def log_message(self, *a, **k):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
            return
        if self.path == "/state":
            # Auto-advance draw/standby phases on state reads so the duel
            # doesn't stall when the engine has no decision to make.
            if self.duel.duel_active and self.duel.phase in ("draw", "standby"):
                self.duel.phase = self.duel._next_phase()
                self.duel._log_ev(f"自动进入 {self.duel.phase}")
            self._send(200, self.duel.to_state())
            return
        if self.path == "/log":
            self._send(200, {"log": self.duel._log[-20:]})
            return
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self._send(400, {"detail": "bad json"})
            return

        if self.path == "/join":
            room = req.get("room", "?")
            deck = req.get("deck", "")
            self.duel = DuelState(opponent="Alice")
            self._send(200, {"opponent": "Alice", "room": room, "deck": deck})
            return
        if self.path == "/ready":
            self._send(200, {"ready": True})
            return
        if self.path == "/set_deck":
            self._send(200, {"deck": req.get("deck", "")})
            return
        if self.path == "/act":
            # Apply our action, then let the opponent take a turn if needed.
            ok = self.duel.apply(req)
            if ok and self.duel.duel_active:
                # Advance phases until it's our turn again (opponent plays).
                guard = 0
                while not self.duel.is_my_turn and self.duel.duel_active and guard < 20:
                    self.duel.opponent_turn()
                    guard += 1
            self._send(200, {"ok": ok, "state": self.duel.to_state()})
            return
        if self.path == "/reset":
            self.duel = DuelState(opponent="Alice")
            self._send(200, {"ok": True})
            return
        self._send(404, {"detail": "not found"})


def main(port: int = 5274):
    srv = HTTPServer(("127.0.0.1", port), YgoBridgeHandler)
    print(f"mock YGO bridge listening on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()