"""Mock YGO bridge — a real HTTP reference implementation of the bridge contract.

Supports the **full network-duel lifecycle with two independent clients**,
so tests can play our plugin against a "human" second seat:

    decks/build_deck → host → (other client) join → ready both → host start
    → alternate turns, declare attacks, opponent responds (chain) → win/lose
    → leave / surrender / chat

Every endpoint validates and returns ``{"ok": false, "error": ...}`` (HTTP
200) for rejected operations — mirroring real engines and letting us verify
the plugin never fakes success.

``mode=single`` + ``/join`` on a missing room keeps the legacy behaviour: an
instant local duel against the built-in AI (used by tests/ygo_demo.py).

Run: python3 tests/mock_ygo_bridge.py  (listens on 127.0.0.1:5274)
"""
from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- card pool ----------------------------------------------------------
CARDS = {
    "Dark Magician":      {"atk": 2500, "def": 2100, "level": 7, "type": "monster",
                           "effect_text": "通常怪兽，高攻白板。"},
    "Blue-Eyes White Dragon": {"atk": 3000, "def": 2500, "level": 8, "type": "monster",
                           "effect_text": "通常怪兽，攻击力 3000。"},
    "Mystic Tomato":      {"atk": 1400, "def": 1100, "level": 4, "type": "monster",
                           "effect_text": "召唤成功时抽 1 张卡。"},
    "Summoned Skull":     {"atk": 2500, "def": 1200, "level": 6, "type": "monster",
                           "effect_text": "通常怪兽，攻击力 2500。"},
    "Gemini Elf":         {"atk": 1900, "def": 900, "level": 4, "type": "monster",
                           "effect_text": "通常怪兽，4 星高攻。"},
    "Mystical Space Typhoon": {"atk": 0, "def": 0, "level": 0, "type": "spell",
                           "effect_text": "速攻魔法：破坏场上 1 张魔法/陷阱。"},
    "Pot of Greed":       {"atk": 0, "def": 0, "level": 0, "type": "spell",
                           "effect_text": "通常魔法：抽 2 张卡。"},
    "Monster Reborn":     {"atk": 0, "def": 0, "level": 0, "type": "spell",
                           "effect_text": "通常魔法：从自己墓地特殊召唤 1 只怪兽。"},
    "Trap Hole":          {"atk": 0, "def": 0, "level": 0, "type": "trap",
                           "effect_text": "陷阱：对方召唤攻击力 1000 以上怪兽时破坏它。"},
    "Mirror Force":       {"atk": 0, "def": 0, "level": 0, "type": "trap",
                           "effect_text": "陷阱：对方攻击宣言时破坏其全部攻击表示怪兽。"},
}
# Real ygopro card passwords for the demo pool (.ydk interop).
CARD_IDS = {
    "46986414": "Dark Magician",
    "89631139": "Blue-Eyes White Dragon",
    "87796900": "Mystic Tomato",
    "70781052": "Summoned Skull",
    "40044918": "Gemini Elf",
    "5318639": "Mystical Space Typhoon",
    "55144522": "Pot of Greed",
    "83764718": "Monster Reborn",
    "04206964": "Trap Hole",
    "44095762": "Mirror Force",
}
DECK_LIST = list(CARDS.keys())
DEFAULT_DECK = (["Mystic Tomato", "Gemini Elf", "Summoned Skull", "Dark Magician",
                 "Blue-Eyes White Dragon", "Pot of Greed", "Monster Reborn",
                 "Mystical Space Typhoon", "Mirror Force", "Trap Hole"] * 4)[:40]


def card_from_id(value: str) -> str:
    value = str(value)
    if value in CARDS:
        return value
    if value in CARD_IDS:
        return CARD_IDS[value]
    # Unknown password: still a usable vanilla monster so decks stay playable.
    CARDS.setdefault(value, {"atk": 1500, "def": 1200, "level": 4, "type": "monster",
                             "effect_text": "未知卡（mock 按 4 星 1500/1200 处理）"})
    return f"卡{value}"


class Seat:
    def __init__(self, name: str, deck_cards: list[str]):
        self.name = name
        self.lp = 8000
        self.deck = list(deck_cards) or list(DEFAULT_DECK)
        random.shuffle(self.deck)
        self.hand: list[str] = []
        self.field: list[dict] = []
        self.grave: list[str] = []
        self.banished: list[str] = []
        self.ready = False
        self.deck_name = ""
        self.normal_summoned = False
        self.attack_used: set[int] = set()   # one attack per monster per turn

    def draw(self, n: int = 1, log=None):
        for _ in range(n):
            if not self.deck:
                return
            self.hand.append(self.deck.pop(0))
        if log is not None:
            log.append(f"{self.name} 抽 {n} 张")

    def public_field(self) -> list[dict]:
        out = []
        for c in self.field:
            info = dict(CARDS.get(c["name"], {}))
            info["name"] = c["name"]
            info["position"] = c["position"]
            if c["position"] == "face-down":
                info = {"name": "里侧卡", "position": "face-down", "hidden": True}
            out.append(info)
        return out

    def own_field(self) -> list[dict]:
        out = []
        for c in self.field:
            info = dict(CARDS.get(c["name"], {}))
            info["name"] = c["name"]
            info["position"] = c["position"]
            out.append(info)
        return out


class Room:
    """One network room: two seats, turn structure, optional chain window."""

    def __init__(self, name: str, mode: str = "network", password: str = ""):
        self.name = name
        self.mode = mode
        self.password = password
        self.seats: list[Seat] = []
        self.sessions: dict[str, int] = {}     # session token -> seat index
        self.started = False
        self.current = 0
        self.turn = 0
        self.phase = "main1"
        self.duel_active = False
        self.winner = ""
        self.log: list[str] = []
        self.chat_log: list[dict] = []
        self.pending_response: int | None = None   # seat that may respond now
        self.pending_attack: dict | None = None
        self.ai_opponent = mode == "single"
        self.created = time.time()

    # ---- setup ---------------------------------------------------------

    def add_seat(self, name: str, deck_cards: list[str], session: str) -> int:
        seat = Seat(name, deck_cards)
        seat.ready = False
        self.seats.append(seat)
        idx = len(self.seats) - 1
        self.sessions[session] = idx
        return idx

    def full(self) -> bool:
        return len(self.seats) >= 2

    def start_duel(self) -> bool:
        if self.started or not self.full():
            return False
        self.started = True
        self.duel_active = True
        self.turn = 1
        self.phase = "main1"
        self.current = 0
        for s in self.seats:
            s.draw(5)
        self.log.append("决斗开始！先手=" + self.seats[0].name)
        return True

    def other(self, idx: int) -> int:
        return 1 - idx

    # ---- legal options -------------------------------------------------

    def legal(self, idx: int) -> list[dict]:
        if not self.duel_active:
            return []
        if self.pending_response is not None:
            if idx != self.pending_response:
                return []
            seat = self.seats[idx]
            opts = []
            for c in seat.field:
                if c["position"] == "face-down" and CARDS.get(c["name"], {}).get("type") == "trap":
                    opts.append({"action": "activate", "card": c["name"],
                                 "effect_text": CARDS.get(c["name"], {}).get("effect_text", ""),
                                 "desc": "连锁对方攻击宣言"})
            opts.append({"action": "pass", "desc": "不响应，结算攻击"})
            return opts
        if idx != self.current:
            return []
        seat = self.seats[idx]
        opp = self.seats[self.other(idx)]
        acts: list[dict] = []
        if self.phase in ("main1", "main2"):
            for card in seat.hand:
                info = CARDS.get(card, {})
                ctype = info.get("type")
                if ctype == "monster" and not seat.normal_summoned:
                    acts.append({"action": "summon", "card": card, "position": "attack",
                                 "atk": info.get("atk"), "def": info.get("def"),
                                 "effect_text": info.get("effect_text", "")})
                    acts.append({"action": "summon_def", "card": card, "position": "defense",
                                 "atk": info.get("atk"), "def": info.get("def")})
                    acts.append({"action": "set", "card": card, "position": "face-down"})
                elif ctype == "spell":
                    acts.append({"action": "activate", "card": card,
                                 "effect_text": info.get("effect_text", "")})
                elif ctype == "trap":
                    acts.append({"action": "set", "card": card, "position": "face-down",
                                 "effect_text": info.get("effect_text", "")})
            for c in seat.field:
                if c["position"] == "face-down" and CARDS.get(c["name"], {}).get("type") in ("trap", "spell"):
                    acts.append({"action": "activate", "card": c["name"],
                                 "effect_text": CARDS.get(c["name"], {}).get("effect_text", ""),
                                 "desc": "发动场上盖卡"})
            if self.phase == "main1":
                acts.append({"action": "switch", "phase": "battle"})
                acts.append({"action": "switch", "phase": "main2"})
            # A player may always end the turn from a main phase.
            acts.append({"action": "switch", "phase": "end"})
        elif self.phase == "battle":
            for m in seat.field:
                if m["position"] != "attack" or id(m) in seat.attack_used:
                    continue
                info = CARDS.get(m["name"], {})
                for t in opp.field:
                    acts.append({"action": "attack", "card": m["name"], "target": t["name"],
                                 "atk": info.get("atk"), "desc": f"攻击 {t['name']}"})
                if not opp.field:
                    acts.append({"action": "direct_attack", "card": m["name"],
                                 "atk": info.get("atk"), "desc": "直接攻击对方玩家"})
            acts.append({"action": "switch", "phase": "main2"})
            acts.append({"action": "switch", "phase": "end"})
        return acts

    def _find_option(self, idx: int, req: dict) -> dict | None:
        opts = self.legal(idx)
        if not opts:
            return None
        if req.get("option") is not None and str(req.get("option")) != "":
            text = str(req["option"]).lstrip("#")
            if text.isdigit() and int(text) < len(opts):
                return opts[int(text)]
            for o in opts:
                if str(o.get("id", o.get("option", ""))) == text:
                    return o
        # semantic match: action + card + target (+phase)
        name = str(req.get("action", "")).lower()
        candidates = [o for o in opts if str(o.get("action")) == name]
        if req.get("card"):
            exact = [o for o in candidates if o.get("card") == req["card"]]
            if exact:
                candidates = exact
        if req.get("target"):
            exact = [o for o in candidates if o.get("target") == req["target"]]
            if exact:
                candidates = exact
        if req.get("phase"):
            exact = [o for o in candidates if o.get("phase") == req["phase"]]
            if exact:
                candidates = exact
        return candidates[0] if candidates else None

    # ---- action resolution ---------------------------------------------

    def apply(self, idx: int, req: dict) -> tuple[bool, str]:
        if not self.duel_active:
            return False, "对局未开始或已结束"
        opt = self._find_option(idx, req)
        if opt is None:
            return False, "该选项目前不合法（状态已变化或参数不匹配）"
        a = opt["action"]
        seat = self.seats[idx]
        opp = self.seats[self.other(idx)]
        if self.pending_response is not None:
            return self._resolve_response(idx, opt)
        if a in ("summon", "summon_def"):
            seat.hand.remove(opt["card"])
            seat.field.append({"name": opt["card"], "position": "attack" if a == "summon" else "defense"})
            seat.normal_summoned = True
            self.log.append(f"{seat.name} 召唤 {opt['card']}")
            if opt["card"] == "Mystic Tomato":
                seat.draw(1, self.log)
            self._check_trap_hole(idx, opt["card"])
        elif a == "set":
            seat.hand.remove(opt["card"])
            seat.field.append({"name": opt["card"], "position": "face-down"})
            self.log.append(f"{seat.name} 盖放 1 张卡")
        elif a == "activate":
            self._activate(idx, opt)
        elif a in ("attack", "direct_attack"):
            if any(c["position"] == "face-down" and CARDS.get(c["name"], {}).get("type") == "trap"
                   for c in opp.field):
                self.pending_response = self.other(idx)
                self.pending_attack = {"seat": idx, **opt}
                self.log.append(f"{seat.name} 用 {opt['card']} 宣言攻击，等待对方响应")
            else:
                self._resolve_attack(opt)
        elif a == "switch":
            self._switch(opt.get("phase", "end"))
        elif a == "pass":
            pass
        else:
            return False, f"mock 未实现的动作 {a}"
        self._check_end()
        if self.ai_opponent and self.duel_active and self.pending_response is None:
            self._ai_drive()
        return True, ""

    def _check_trap_hole(self, idx: int, card: str):
        info = CARDS.get(card, {})
        if info.get("atk", 0) < 1000:
            return
        opp = self.seats[self.other(idx)]
        for c in list(opp.field):
            if c["position"] == "face-down" and c["name"] == "Trap Hole":
                opp.field.remove(c)
                opp.grave.append("Trap Hole")
                target = self.seats[idx]
                target.field = [m for m in target.field if m["name"] != card]
                target.grave.append(card)
                self.log.append(f"陷阱 落穴 发动，破坏 {card}")

    def _activate(self, idx: int, opt: dict):
        seat = self.seats[idx]
        opp = self.seats[self.other(idx)]
        card = opt["card"]
        on_field = any(c["name"] == card and c["position"] == "face-down" for c in seat.field)
        if on_field:
            seat.field = [c for c in seat.field if not (c["name"] == card and c["position"] == "face-down")]
        elif card in seat.hand:
            seat.hand.remove(card)
        else:
            return
        seat.grave.append(card)
        self.log.append(f"{seat.name} 发动 {card}")
        if card == "Pot of Greed":
            seat.draw(2, self.log)
        elif card == "Monster Reborn" and seat.grave:
            monsters = [c for c in seat.grave if CARDS.get(c, {}).get("type") == "monster"]
            if monsters:
                best = max(monsters, key=lambda c: CARDS.get(c, {}).get("atk", 0))
                seat.grave.remove(best)
                seat.field.append({"name": best, "position": "attack"})
                self.log.append(f"死者苏生：特殊召唤 {best}")
        elif card == "Mystical Space Typhoon":
            target = next((c for c in opp.field if c["position"] == "face-down"), None)
            if target:
                opp.field.remove(target)
                opp.grave.append(target["name"])
                self.log.append(f"旋风破坏对方盖卡 {target['name']}")
        elif card == "Mirror Force" and self.pending_attack:
            atk_seat = self.seats[self.pending_attack["seat"]]
            destroyed = [m["name"] for m in atk_seat.field if m["position"] == "attack"]
            atk_seat.field = [m for m in atk_seat.field if m["position"] != "attack"]
            atk_seat.grave.extend(destroyed)
            self.log.append(f"神圣防护罩 -反射镜力- 破坏 {len(destroyed)} 只攻击表示怪兽")
            self.pending_attack = None
            self.pending_response = None

    def _resolve_response(self, idx: int, opt: dict) -> tuple[bool, str]:
        attack = self.pending_attack or {}
        if opt["action"] == "activate":
            self._activate(idx, opt)
            if self.pending_attack is None:      # Mirror Force negated it
                return True, ""
        self.pending_response = None
        if self.pending_attack is not None:
            self.pending_attack = None
            self._resolve_attack(attack)
        self._check_end()
        return True, ""

    def _resolve_attack(self, opt: dict):
        idx = opt["seat"] if "seat" in opt else self.current
        seat = self.seats[idx]
        opp = self.seats[self.other(idx)]
        info = CARDS.get(opt["card"], {})
        atk = info.get("atk", 0)
        for m in seat.field:
            if m["name"] == opt["card"]:
                seat.attack_used.add(id(m))
                break
        target_name = opt.get("target")
        target = next((c for c in opp.field if c["name"] == target_name), None) if target_name else None
        if opt["action"] == "direct_attack" or target is None:
            opp.lp -= atk
            self.log.append(f"{seat.name} 的 {opt['card']} 直接攻击，{opp.name} LP -{atk} → {opp.lp}")
        else:
            t_info = CARDS.get(target["name"], {})
            t_val = t_info.get("atk", 0) if target["position"] == "attack" else t_info.get("def", 0)
            if atk > t_val:
                opp.field.remove(target)
                opp.grave.append(target["name"])
                if target["position"] == "attack":
                    opp.lp -= atk - t_val
                self.log.append(f"{opt['card']} 击破 {target['name']}，{opp.name} LP {opp.lp}")
            elif atk == t_val and target["position"] == "attack":
                opp.field.remove(target)
                opp.grave.append(target["name"])
                seat.field = [m for m in seat.field if m["name"] != opt["card"]]
                seat.grave.append(opt["card"])
                self.log.append(f"{opt['card']} 与 {target['name']} 同归于尽")
            else:
                seat.field = [m for m in seat.field if m["name"] != opt["card"]]
                seat.grave.append(opt["card"])
                seat.lp -= t_val - atk
                self.log.append(f"{opt['card']} 被 {target['name']} 反杀，{seat.name} LP {seat.lp}")

    def _switch(self, phase: str):
        if phase == "battle" and self.phase == "main1":
            self.phase = "battle"
        elif phase == "main2":
            self.phase = "main2"
        elif phase == "end":
            self.current = self.other(self.current)
            self.turn += 1
            self.phase = "main1"
            for s in self.seats:
                s.normal_summoned = False
                s.attack_used.clear()
            self.seats[self.current].draw(1, self.log)
        self.log.append(f"进入 {self.phase}（{self.seats[self.current].name} 的回合）")

    def _check_end(self):
        for i, s in enumerate(self.seats):
            if s.lp <= 0:
                self.duel_active = False
                self.winner = self.seats[self.other(i)].name
                self.log.append(f"对局结束，胜者 {self.winner}")

    # ---- built-in AI (single mode only) ---------------------------------

    def _ai_drive(self):
        guard = 0
        while self.duel_active and self.current == 1 and guard < 12:
            guard += 1
            opts = self.legal(1)
            if not opts:
                break
            pick = None
            for pref in ("summon", "direct_attack", "attack", "activate", "set", "switch"):
                cand = [o for o in opts if o["action"] == pref]
                if cand:
                    pick = max(cand, key=lambda o: o.get("atk") or 0) if pref in ("summon", "attack") else cand[-1]
                    break
            if pick is None:
                break
            self.apply(1, pick)

    # ---- views -----------------------------------------------------------

    def view(self, idx: int) -> dict:
        seat = self.seats[idx]
        opp = self.seats[self.other(idx)] if self.full() else None
        game = {
            "my_lp": seat.lp,
            "op_lp": opp.lp if opp else 8000,
            "turn": self.turn,
            "phase": self.phase,
            "is_my_turn": self.current == idx and self.pending_response is None,
            "is_duel_active": self.duel_active,
            "opponent": opp.name if opp else "",
            "opponent_hand_count": len(opp.hand) if opp else 0,
            "hand": [dict(CARDS.get(c, {}), name=c) for c in seat.hand],
            "field_my": seat.own_field(),
            "field_op": opp.public_field() if opp else [],
            "grave_my": list(seat.grave),
            "grave_op": list(opp.grave) if opp else [],
            "banished_my": list(seat.banished),
            "banished_op": list(opp.banished) if opp else [],
            "deck_count": len(seat.deck),
            "op_deck_count": len(opp.deck) if opp else 0,
            "legal_actions": self.legal(idx),
            "winner": self.winner,
            "public_log": self.log[-12:],
        }
        if self.pending_response == idx:
            game["response_window"] = True
        stage = "idle"
        if self.duel_active:
            stage = "dueling"
        elif self.started:
            stage = "ended"
        elif self.full():
            stage = "readying" if all(s.ready for s in self.seats) else "confirming_opponent"
        elif self.seats:
            stage = "joining"
        room = {
            "name": self.name,
            "mode": self.mode,
            "players": [s.name for s in self.seats],
            "is_host": idx == 0,
            "ready": {s.name: s.ready for s in self.seats},
            "started": self.started,
        }
        return {"stage": stage, "room": room, "game": game, "ok": True}


class BridgeState:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.decks: dict[str, dict] = {
            "dragon.ydk": {"name": "dragon.ydk", "main": list(DEFAULT_DECK), "extra": [], "side": []},
            "demo.ydk": {"name": "demo.ydk", "main": list(DEFAULT_DECK), "extra": [], "side": []},
        }
        self._counter = 0
        self.lock = threading.RLock()

    def new_session(self) -> str:
        self._counter += 1
        return f"s{self._counter}"

    def deck_cards(self, name: str) -> list[str]:
        deck = self.decks.get(name) or self.decks.get("default.ydk")
        if not deck:
            return list(DEFAULT_DECK)
        return [card_from_id(c) for c in deck.get("main", [])]


STATE = BridgeState()


class YgoBridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    # ---- helpers -------------------------------------------------------

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _seat(self, req: dict, room: Room | None = None) -> tuple[int | None, Room | None]:
        room = room or self._room_of(req)
        if room is None:
            return None, None
        session = str(req.get("session", ""))
        if session in room.sessions:
            return room.sessions[session], room
        if len(room.seats) == 1:
            return 0, room
        return None, room

    def _room_of(self, req: dict) -> Room | None:
        session = str(req.get("session", ""))
        if session:
            for room in STATE.rooms.values():
                if session in room.sessions:
                    return room
        name = str(req.get("room", ""))
        if name and name in STATE.rooms:
            return STATE.rooms[name]
        if len(STATE.rooms) == 1:
            return next(iter(STATE.rooms.values()))
        # single-player legacy room
        for room in STATE.rooms.values():
            if room.mode == "single":
                return room
        return None

    # ---- HTTP ------------------------------------------------------------

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path
        with STATE.lock:
            if path == "/health":
                self._send(200, {"ok": True, "rooms": len(STATE.rooms)})
                return
            if path == "/decks":
                decks = []
                for name, d in STATE.decks.items():
                    decks.append({"name": name,
                                  "main": len(d.get("main", [])),
                                  "extra": len(d.get("extra", [])),
                                  "side": len(d.get("side", []))})
                self._send(200, {"ok": True, "decks": decks})
                return
            if path == "/rooms":
                rooms = [{"name": r.name, "mode": r.mode, "players": [s.name for s in r.seats],
                          "started": r.started, "duel_active": r.duel_active}
                         for r in STATE.rooms.values()]
                self._send(200, {"ok": True, "rooms": rooms})
                return
            if path == "/state":
                idx, room = self._seat(params)
                if room is None or idx is None:
                    self._send(200, {"ok": True, "stage": "idle", "game": {}, "room": {}})
                    return
                self._send(200, room.view(idx))
                return
            if path == "/log":
                room = self._room_of(params)
                self._send(200, {"ok": True, "log": room.log[-20:] if room else []})
                return
            if path == "/reset":
                STATE.rooms.clear()
                self._send(200, {"ok": True})
                return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        req = self._body()
        path = self.path.split("?")[0]
        with STATE.lock:
            handler = {
                "/host": self._host,
                "/join": self._join,
                "/ready": self._ready,
                "/start": self._start,
                "/set_deck": self._set_deck,
                "/build_deck": self._build_deck,
                "/act": self._act,
                "/leave": self._leave,
                "/surrender": self._surrender,
                "/chat": self._chat,
                "/reset": lambda r: (STATE.rooms.clear() or self._send(200, {"ok": True})),
            }.get(path)
            if handler is None:
                self._send(404, {"ok": False, "error": f"unknown endpoint {path}"})
                return
            handler(req)

    # ---- endpoints ---------------------------------------------------------

    def _deck_for(self, req: dict) -> tuple[str, list[str]]:
        name = str(req.get("deck", "") or "default.ydk")
        if name not in STATE.decks and name != "default.ydk":
            return name, list(DEFAULT_DECK)
        return name, STATE.deck_cards(name)

    def _host(self, req: dict):
        name = str(req.get("room", "") or f"room{len(STATE.rooms) + 1}")
        if name in STATE.rooms:
            self._send(200, {"ok": False, "error": f"房间 {name} 已存在"})
            return
        mode = str(req.get("mode", "network"))
        password = str(req.get("password", ""))
        room = Room(name, mode=mode, password=password)
        deck_name, cards = self._deck_for(req)
        session = STATE.new_session()
        idx = room.add_seat("我方", cards, session)
        room.seats[idx].deck_name = deck_name
        STATE.rooms[name] = room
        if mode == "single":
            room.add_seat("AI 对手", list(DEFAULT_DECK), STATE.new_session())
            room.ai_opponent = True
        self._send(200, {"ok": True, "room": name, "session": session, "host": True,
                         "deck": deck_name, "players": [s.name for s in room.seats]})

    def _join(self, req: dict):
        name = str(req.get("room", ""))
        mode = str(req.get("mode", "network"))
        room = STATE.rooms.get(name)
        if room is None:
            if mode == "single":
                # legacy: instant local duel vs built-in AI
                room = Room(name or "single", mode="single")
                deck_name, cards = self._deck_for(req)
                session = STATE.new_session()
                room.add_seat("我方", cards, session)
                room.add_seat("Alice", list(DEFAULT_DECK), STATE.new_session())
                room.ai_opponent = True
                STATE.rooms[room.name] = room
                for s in room.seats:
                    s.ready = True
                room.start_duel()
                self._send(200, {"ok": True, "room": room.name, "session": session,
                                 "opponent": "Alice", "deck": deck_name})
                return
            self._send(200, {"ok": False, "error": f"房间 {name} 不存在（先 host 或确认房间名）"})
            return
        if room.password and str(req.get("password", "")) != room.password:
            self._send(200, {"ok": False, "error": "房间密码错误"})
            return
        if room.full():
            self._send(200, {"ok": False, "error": "房间已满"})
            return
        deck_name, cards = self._deck_for(req)
        session = STATE.new_session()
        idx = room.add_seat("挑战者", cards, session)
        room.seats[idx].deck_name = deck_name
        opp = room.seats[0].name
        self._send(200, {"ok": True, "room": name, "session": session, "opponent": opp,
                         "deck": deck_name, "players": [s.name for s in room.seats]})

    def _set_deck(self, req: dict):
        name = str(req.get("deck", ""))
        if name not in STATE.decks:
            self._send(200, {"ok": False, "error": f"卡组 {name} 不存在，先 build_deck"})
            return
        idx, room = self._seat(req)
        if room is not None and idx is not None:
            seat = room.seats[idx]
            seat.deck_name = name
            if not room.started:
                seat.deck = STATE.deck_cards(name)
                random.shuffle(seat.deck)
        self._send(200, {"ok": True, "deck": name})

    def _build_deck(self, req: dict):
        name = str(req.get("name", "") or "").strip()
        if not name:
            self._send(200, {"ok": False, "error": "缺少卡组名"})
            return
        if not name.endswith(".ydk"):
            name += ".ydk"
        if req.get("ydk"):
            zones = {"main": [], "extra": [], "side": []}
            zone = "main"
            for line in str(req["ydk"]).splitlines():
                line = line.strip()
                low = line.lower()
                if low.startswith("#main"):
                    zone = "main"
                elif low.startswith("#extra"):
                    zone = "extra"
                elif low.startswith("!side"):
                    zone = "side"
                elif line.isdigit():
                    zones[zone].append(line)
        else:
            zones = {k: [str(c) for c in req.get(k, [])] for k in ("main", "extra", "side")}
        if not 40 <= len(zones["main"]) <= 60:
            self._send(200, {"ok": False, "error": f"主卡组需 40-60 张，当前 {len(zones['main'])}"})
            return
        if len(zones["extra"]) > 15 or len(zones["side"]) > 15:
            self._send(200, {"ok": False, "error": "额外/副卡组最多 15 张"})
            return
        STATE.decks[name] = {"name": name, **zones}
        self._send(200, {"ok": True, "name": name,
                         "main": len(zones["main"]), "extra": len(zones["extra"]),
                         "side": len(zones["side"])})

    def _ready(self, req: dict):
        idx, room = self._seat(req)
        if room is None or idx is None:
            self._send(200, {"ok": False, "error": "未在房间内"})
            return
        if room.started:
            self._send(200, {"ok": False, "error": "对局已开始"})
            return
        room.seats[idx].ready = bool(req.get("ready", True))
        if room.mode == "single":
            for s in room.seats:
                s.ready = True
            room.start_duel()
        self._send(200, {"ok": True, "ready": room.seats[idx].ready,
                         "all_ready": all(s.ready for s in room.seats)})

    def _start(self, req: dict):
        idx, room = self._seat(req)
        if room is None or idx is None:
            self._send(200, {"ok": False, "error": "未在房间内"})
            return
        if idx != 0:
            self._send(200, {"ok": False, "error": "只有房主能开始"})
            return
        if not room.full():
            self._send(200, {"ok": False, "error": "等待对手加入"})
            return
        if not all(s.ready for s in room.seats):
            self._send(200, {"ok": False, "error": "双方都需要先准备"})
            return
        if room.started and room.duel_active:
            # Idempotent: single-player rooms auto-start on ready; asking to
            # start again is still a success, not a fake failure.
            self._send(200, {"ok": True, "started": True, "already": True, "state": room.view(idx)})
            return
        if not room.start_duel():
            self._send(200, {"ok": False, "error": "无法开始（已开始？）"})
            return
        self._send(200, {"ok": True, "started": True, "state": room.view(idx)})

    def _act(self, req: dict):
        idx, room = self._seat(req)
        if room is None or idx is None:
            self._send(200, {"ok": False, "error": "未在房间内"})
            return
        ok, err = room.apply(idx, req)
        if not ok:
            self._send(200, {"ok": False, "error": err})
            return
        self._send(200, {"ok": True, "state": room.view(idx)})

    def _leave(self, req: dict):
        room = self._room_of(req)
        if room is not None:
            session = str(req.get("session", ""))
            idx = room.sessions.get(session)
            if idx is not None and room.duel_active:
                room.duel_active = False
                room.winner = room.seats[room.other(idx)].name
                room.log.append(f"{room.seats[idx].name} 离开房间")
            STATE.rooms.pop(room.name, None)
        self._send(200, {"ok": True})

    def _surrender(self, req: dict):
        idx, room = self._seat(req)
        if room is None or idx is None:
            self._send(200, {"ok": False, "error": "未在房间内"})
            return
        room.duel_active = False
        room.winner = room.seats[room.other(idx)].name
        room.log.append(f"{room.seats[idx].name} 投降，{room.winner} 获胜")
        self._send(200, {"ok": True, "winner": room.winner, "state": room.view(idx)})

    def _chat(self, req: dict):
        idx, room = self._seat(req)
        if room is None or idx is None:
            self._send(200, {"ok": False, "error": "未在房间内"})
            return
        msg = str(req.get("message", ""))[:300]
        room.chat_log.append({"from": room.seats[idx].name, "message": msg, "at": time.time()})
        room.log.append(f"{room.seats[idx].name}: {msg}")
        self._send(200, {"ok": True})


def main(port: int = 5274):
    srv = ThreadingHTTPServer(("127.0.0.1", port), YgoBridgeHandler)
    print(f"mock YGO bridge (network-capable) on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
