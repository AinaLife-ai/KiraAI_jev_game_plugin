"""Unit tests for YGO option addressing, payload shape, .ydk parsing and
compact state formatting. Pure logic, no I/O.

Run: python3 tests/ygo_ops_test.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from plugin.ygo_ops import (  # noqa: E402
    act_payload,
    card_brief,
    describe_option,
    format_decks,
    format_duel_state,
    match_action,
    options_map,
    parse_ydk,
    resolve_option,
    validate_deck,
)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


SNAP = {
    "stage": "dueling",
    "room": {"name": "arena", "players": ["我方", "真人"], "is_host": True},
    "game": {
        "my_lp": 8000, "op_lp": 6000, "turn": 2, "phase": "main1", "is_my_turn": True,
        "opponent": "真人", "opponent_hand_count": 4,
        "hand": [{"name": "Mystic Tomato", "atk": 1400, "def": 1100, "level": 4},
                 {"name": "Gemini Elf", "atk": 1900, "def": 900, "level": 4}],
        "field_my": [{"name": "Dark Magician", "atk": 2500, "def": 2100, "position": "attack"}],
        "field_op": [{"name": "里侧卡", "position": "face-down", "hidden": True}],
        "grave_my": ["Pot of Greed"], "grave_op": [],
        "legal_actions": [
            {"action": "summon", "card": "Mystic Tomato", "position": "attack"},
            {"action": "summon", "card": "Gemini Elf", "position": "attack"},
            {"action": "summon_def", "card": "Mystic Tomato", "position": "defense"},
            {"action": "switch", "phase": "battle"},
            {"action": "switch", "phase": "end"},
        ],
        "public_log": ["对方 盖放 1 张卡", "我方 召唤 Dark Magician"],
    },
}


def main():
    opts = options_map(SNAP)
    check("same action twice stays distinct", len(opts) == 5, str(list(opts)))
    check("ids are positional without bridge ids", list(opts) == ["0", "1", "2", "3", "4"], str(list(opts)))

    with_ids = {"game": {"legal_actions": [{"id": "r7", "action": "pass"}, {"option": 3, "action": "activate", "card": "X"}]}}
    opts2 = options_map(with_ids)
    check("bridge ids win", set(opts2) == {"r7", "3"}, str(set(opts2)))
    check("resolve bridge id", resolve_option(with_ids, "r7")["action"] == "pass")
    check("resolve numeric id as string", resolve_option(with_ids, "3")["card"] == "X")

    check("resolve by index", resolve_option(SNAP, "1")["card"] == "Gemini Elf")
    check("resolve with hash prefix", resolve_option(SNAP, "#1")["card"] == "Gemini Elf")
    check("resolve action:card exact", resolve_option(SNAP, "summon:Gemini Elf")["card"] == "Gemini Elf")
    check("resolve switch:phase", resolve_option(SNAP, "switch:end")["phase"] == "end")
    check("bare name with multiple matches is refused", resolve_option(SNAP, "summon") is None)
    check("bare name unique match works", resolve_option(SNAP, "summon_def")["card"] == "Mystic Tomato")
    check("unknown option refused", resolve_option(SNAP, "99") is None)
    check("empty options refused", resolve_option({"game": {"legal_actions": []}}, "0") is None)

    planned = {"action": "summon", "card": "Gemini Elf", "position": "attack"}
    check("semantic chain match", match_action(SNAP, planned)["card"] == "Gemini Elf")
    check("semantic mismatch aborts", match_action(SNAP, {"action": "summon", "card": "Blue-Eyes"}) is None)
    check("semantic match respects target", match_action(SNAP, {"action": "switch", "phase": "main2"}) is None)

    payload = act_payload(opts["1"], "1")
    check("payload carries option", payload["option"] == "1")
    check("payload carries semantics", payload["action"] == "summon" and payload["card"] == "Gemini Elf")
    check("payload carries core code", payload["code"] == 4, str(payload))
    check("payload keeps position", payload["position"] == "attack")
    check("switch code", act_payload(opts["3"], "3")["code"] == 10)

    text = format_duel_state(SNAP, stage="dueling", with_effects=False)
    check("state has LP line", "LP 我8000/敌6000" in text, text.splitlines()[1])
    check("state has options", "合法选项(5)" in text)
    check("state shows both summon targets", "Mystic Tomato" in text and "Gemini Elf" in text)
    check("state keeps public log", "最近公开事件" in text)
    check("state hides opponent hand", "对手: 真人 手牌4张" in text)
    check("face-down stays unknown", "里侧卡(未知)" in text)
    check("compact state is small", len(text) < 1600, str(len(text)))
    big = dict(SNAP)
    big["game"] = dict(SNAP["game"])
    big["game"]["legal_actions"] = [{"action": "summon", "card": f"卡{i}"} for i in range(40)]
    trimmed = format_duel_state(big, max_options=10)
    check("long option list trimmed with hint", "其余 30 项省略" in trimmed, trimmed[-120:])

    check("option description readable", describe_option("1", opts["1"]).startswith("#1 通常召唤·攻击 Gemini Elf"),
          describe_option("1", opts["1"]))
    check("phase description readable", "→结束阶段" in describe_option("4", opts["4"]))
    check("card brief hides hidden cards", card_brief({"name": "X", "hidden": True}) == "里侧卡(未知)")

    ydk = "#created by tester\n#main\n" + "\n".join(["46986414"] * 40) + "\n#extra\n123\n!side\n456\n"
    zones = parse_ydk(ydk)
    check("ydk zones parsed", len(zones["main"]) == 40 and zones["extra"] == ["123"] and zones["side"] == ["456"])
    check("over-3 copies flagged", "同名卡" in validate_deck(zones))
    check("short deck flagged", "40-60" in validate_deck(parse_ydk("#main\n123\n")))
    check("empty deck flagged", "主卡组为空" in validate_deck({"main": [], "extra": [], "side": []}))
    good = {"main": [str(1000 + i) for i in range(40)], "extra": [], "side": []}
    check("legal deck passes", validate_deck(good) == "")
    check("oversize extra flagged", "额外" in validate_deck({"main": good["main"], "extra": ["1"] * 16, "side": []}))
    check("decks fallback text", "build_deck" in format_decks({"decks": []}))
    check("decks formatting", "dragon.ydk" in format_decks({"decks": [{"name": "dragon.ydk", "main": 40}]}))

    print(f"\n== {PASS} passed, {FAIL} failed ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
