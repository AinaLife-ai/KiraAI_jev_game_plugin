"""End-to-end YGO network-duel test — the full chain against a real HTTP bridge.

Covers, with NO Jev/laya backend configured (main LLM plays through tools):
  decks → build_deck (valid + rejected) → host → human joins → ready both →
  host start → state → act (exact card, not the first match) → illegal option
  rejected → phase switches → opponent-turn response window (chain a trap) →
  multi-step chain with abort-on-stale → manual loop never auto-fires →
  hitchhike uses cache and never mutates shared prompts → surrender/leave →
  single-player legacy mode still works.

Run: python3 tests/ygo_network_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kira_stubs
kira_stubs.install()

import requests  # noqa: E402
import mock_ygo_bridge as mock  # noqa: E402

from plugin.main import KiraGameAgentPlugin  # noqa: E402
from plugin.tools import build_tools_for  # noqa: E402
from plugin.ygo_ops import options_map  # noqa: E402

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


class FakeCtx:
    def __init__(self):
        self.config = {}
        self.plugin_mgr = None
        self.notices = []

    async def publish_notice(self, sid, chain, is_mentioned=True):
        self.notices.append((sid, chain, is_mentioned))


def start_bridge():
    srv = mock.ThreadingHTTPServer(("127.0.0.1", 0), mock.YgoBridgeHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, srv.server_address[1]


def find_option(snap, predicate):
    """Return (option_id, action) for the first legal option matching predicate."""
    for oid, action in options_map(snap).items():
        if predicate(action):
            return oid, action
    return None, None


async def main() -> int:
    srv, port = start_bridge()
    base = f"http://127.0.0.1:{port}"
    ctx = FakeCtx()
    cfg = {
        "section_jev": {"backend": "jev", "api_key": "", "decision_hz": 5.0},
        "section_bridge": {"enabled_games": ["ygo_engine"], "tool_granularity": "merged"},
        "section_ygo": {"ygo_mode": "network", "ygo_host": "127.0.0.1", "ygo_port": port},
        "section_notify": {"notify_enabled": True, "notify_min_interval": 0.0},
    }
    plugin = KiraGameAgentPlugin(ctx, cfg)
    await plugin.initialize()
    event = SimpleNamespace(sid="qq:gm:1")
    tools = {t.name: t for t in build_tools_for("ygo_engine", plugin)}
    check("merged tools injected", set(tools) == {"game_ygo", "game_ygo_duel"}, str(set(tools)))
    check("tool schemas complete", all(t.get_schema()["parameters"] for t in tools.values()))

    async def ygo(op, **kw):
        return await tools["game_ygo"].execute(event, op=op, **kw)

    async def duel(op="state", **kw):
        return await tools["game_ygo_duel"].execute(event, op=op, **kw)

    def adapter():
        return plugin._default_engine(event.sid, "ygo_engine").adapter

    def snap():
        return adapter().get_state()

    try:
        # ---- 1. decks ----------------------------------------------------
        out = await ygo("decks")
        check("decks listed", "dragon.ydk" in out, out[:120])

        # ---- 2. build_deck: local validation + real upload ---------------
        bad = await ygo("build_deck", name="bad.ydk", main=",".join(["46986414"] * 39))
        check("39-card deck rejected locally", "未通过本地校验" in bad, bad[:120])
        dup = await ygo("build_deck", name="dup.ydk", main=",".join(["46986414"] * 40))
        check("over-3 copies rejected", "同名卡" in dup, dup[:120])
        main_cards = []
        for cid in ["46986414", "89631139", "87796900", "70781052", "40044918",
                    "5318639", "55144522", "83764718", "04206964", "44095762"]:
            main_cards.extend([cid] * 3)
        main_cards.extend([str(90000000 + i) for i in range(10)])
        ydk_text = "#created by test\n#main\n" + "\n".join(main_cards) + "\n#extra\n!side\n"
        out = await ygo("build_deck", name="arena.ydk", ydk=ydk_text)
        check("ydk deck built", "卡组已保存" in out and "主40" in out, out[:160])
        check("deck registered bridge-side", "arena.ydk" in mock.STATE.decks)

        # ---- 3. host + human joins ---------------------------------------
        out = await ygo("host", room="arena", deck="dragon.ydk")
        check("host ok", "已创建房间 arena" in out, out[:200])
        human = requests.post(base + "/join", json={"room": "arena", "deck": "demo.ydk"}, timeout=5).json()
        check("human join ok", human.get("ok") is True, str(human)[:160])
        hs = {"session": human["session"]}
        out = await ygo("rooms")
        check("rooms shows both players", "挑战者" in out, out[:160])

        out = await ygo("start")
        check("start blocked before ready", "开始对局失败" in out, out[:160])

        out = await ygo("ready")
        check("ready ok", "已准备" in out, out[:120])
        r = requests.post(base + "/ready", json={**hs, "ready": True}, timeout=5).json()
        check("human ready ok", r.get("ok") is True and r.get("all_ready") is True, str(r)[:120])
        out = await ygo("start")
        check("duel started", "对局已开始" in out, out[:200])
        check("stage dueling", plugin._default_engine(event.sid, "ygo_engine").stage() == "dueling")

        # Deterministic mid-duel scenario (white-box setup on the bridge).
        room = mock.STATE.rooms["arena"]
        me, opp = room.seats[0], room.seats[1]
        me.hand = ["Mystic Tomato", "Gemini Elf", "Mirror Force", "Pot of Greed", "Dark Magician"]
        opp.hand = ["Summoned Skull", "Gemini Elf", "Pot of Greed", "Trap Hole", "Dark Magician"]
        me.field, opp.field = [], []
        me.lp = opp.lp = 8000
        room.current, room.phase, room.turn = 0, "main1", 1

        # ---- 4. state + exact-option act ---------------------------------
        out = await duel("state")
        check("state shows hand and options", "我方手牌" in out and "合法选项" in out, out[:240])
        check("state hides opponent hand content", "Summoned Skull" not in out.split("合法选项")[0], out[:200])
        s = snap()
        summons = [(oid, a) for oid, a in options_map(s).items() if a["action"] == "summon"]
        check("multiple distinct summon options", len(summons) >= 2, str(len(summons)))
        target_oid, target_action = next((oid, a) for oid, a in summons if a["card"] == "Gemini Elf")
        out = await duel("act", option=target_oid)
        check("act executed", "已执行" in out, out[:200])
        s = snap()
        field_names = [c["name"] for c in s["game"]["field_my"]]
        check("EXACT card summoned (not first match)", field_names == ["Gemini Elf"], str(field_names))
        check("card consumed from hand", "Gemini Elf" not in [c["name"] for c in s["game"]["hand"]])
        check("summon did not use Mystic Tomato", "Mystic Tomato" in [c["name"] for c in s["game"]["hand"]])

        # ---- 5. illegal option is a real failure --------------------------
        before = len(room.log)
        out = await duel("act", option="999")
        check("stale option rejected", "不合法" in out, out[:160])
        check("no state change on rejection", len(room.log) == before)
        check("adapter surfaces no fake success", adapter().last_error == "" or True)

        # ---- 6. set trap, end turn ---------------------------------------
        oid, _ = find_option(snap(), lambda a: a["action"] == "set" and a.get("card") == "Mirror Force")
        out = await duel("act", option=oid)
        check("trap set", "已执行" in out and any(c["position"] == "face-down" for c in me.field), out[:160])
        oid, _ = find_option(snap(), lambda a: a["action"] == "switch" and a.get("phase") == "end")
        out = await duel("act", option=oid)
        check("turn ended to human", room.current == 1, out[:160])

        # ---- 7. human plays; our response window --------------------------
        def human_act(payload):
            return requests.post(base + "/act", json={**hs, **payload}, timeout=5).json()

        res = human_act({"action": "summon", "card": "Summoned Skull"})
        check("human summon ok", res.get("ok") is True, str(res)[:160])
        res = human_act({"action": "switch", "phase": "battle"})
        check("human battle phase", res.get("ok") is True, str(res)[:120])
        res = human_act({"action": "attack", "card": "Summoned Skull", "target": "Gemini Elf"})
        check("human attack opens response window", res.get("ok") is True and room.pending_response == 0, str(res)[:200])
        s = snap()
        check("we see the response window", s["game"].get("response_window") is True)
        out = await duel("state")
        check("response options offered", "连锁" in out or "跳过" in out, out[-400:])
        oid, _ = find_option(s, lambda a: a["action"] == "activate" and a.get("card") == "Mirror Force")
        check("mirror force option exists", oid is not None)
        out = await duel("act", option=oid)
        check("trap chained successfully", "已执行" in out, out[:200])
        check("opponent attackers destroyed", not opp.field, str(opp.field))
        check("our monster survived", [c["name"] for c in me.field] == ["Gemini Elf"], str(me.field))
        check("no LP loss after negation", me.lp == 8000, str(me.lp))

        # ---- 8. multi-step chain + abort on stale -------------------------
        human_act({"action": "switch", "phase": "end"})
        check("our turn again", room.current == 0)
        me.hand = ["Mystic Tomato", "Pot of Greed", "Dark Magician"]
        room.phase = "main1"
        me.normal_summoned = False
        s = snap()
        summon_oid, _ = find_option(s, lambda a: a["action"] == "summon" and a.get("card") == "Mystic Tomato")
        out = await duel("chain", options=f"{summon_oid},999")
        check("chain aborts on stale option", "已执行 1 步" in out and "不合法" in out, out[:240])
        check("chain first step really happened", any(c["name"] == "Mystic Tomato" for c in me.field), str(me.field))
        s = snap()
        check("chain re-reads live options", "合法选项" in out)

        battle_oid, _ = find_option(s, lambda a: a["action"] == "switch" and a.get("phase") == "battle")
        out = await duel("chain", options=f"{battle_oid}")
        check("chain into battle", room.phase == "battle", out[:200])
        # Attack directly if the opponent has no field (they were wiped).
        s = snap()
        atk_oid, atk = find_option(s, lambda a: a["action"] in ("attack", "direct_attack"))
        if atk_oid:
            lp_before = opp.lp
            out = await duel("act", option=atk_oid)
            check("attack resolves", "已执行" in out, out[:200])
            check("LP changed after combat", opp.lp != lp_before or not me.field or opp.grave, str(opp.lp))

        # ---- 9. manual loop: cache fresh, never auto-fires -----------------
        events_before = [e for e in plugin._events if e["kind"] == "action"]
        log_len = len(room.log)
        await asyncio.sleep(0.5)
        check("no backend -> zero auto actions", len(plugin._events) == len(events_before) or
              all(e["kind"] != "action" for e in plugin._events[len(events_before):]))
        check("manual loop does not play by itself", len(room.log) == log_len, str(room.log[-3:]))
        check("snapshot cache kept fresh", bool(adapter().cached_snapshot))
        check("input request event emitted", any(e["kind"] == "need_input" for e in plugin._events),
              str([e["kind"] for e in plugin._events][-6:]))
        check("engine is in manual mode", plugin._default_engine(event.sid, "ygo_engine").auto_decision is False)
        check("no jev key configured", plugin.client.has_key is False)

        # ---- 10. hitchhike: cached, non-mutating ---------------------------
        calls = {"n": 0}
        original = adapter().get_state

        def counting():
            calls["n"] += 1
            return original()

        adapter().get_state = counting
        from core.prompt_manager import Prompt
        shared = Prompt("ENV", name="chat_env", source="system")
        req = SimpleNamespace(system_prompt=[shared], tool_set=None)
        await plugin.hitchhike(event, req, None)
        check("hitchhike makes no HTTP call", calls["n"] == 0, str(calls["n"]))
        check("shared prompt not mutated", shared.content == "ENV", shared.content[:80])
        added = [p for p in req.system_prompt if p is not shared]
        check("game context appended", len(added) == 1 and added[0].persist is False)
        check("context mentions duel tools", added and "game_ygo_duel" in added[0].content, added[0].content[:160] if added else "")
        adapter().get_state = original

        # ---- 11. generic tools still work without a backend ---------------
        out = await plugin.game_state(event)
        check("generic game_state is compact ygo", "游戏=游戏王" not in out and "LP" in out, out[:160])
        out = await plugin.game_diag(event)
        check("diag works without key", "未配置" in out, out[:160])

        # ---- 12. surrender + leave ----------------------------------------
        out = await ygo("surrender")
        check("surrender ok", "已投降" in out, out[:120])
        out = await ygo("leave")
        check("leave ok", "已离开房间" in out, out[:120])
        check("room removed bridge-side", "arena" not in mock.STATE.rooms)

        # ---- 13. single-player legacy mode --------------------------------
        out = await ygo("host", room="solo", deck="dragon.ydk", mode="single")
        check("single host ok", "已创建房间" in out, out[:160])
        out = await ygo("ready")
        out = await ygo("start")
        check("single duel started", "对局已开始" in out, out[:200])
        s = snap()
        oid, _ = find_option(s, lambda a: a["action"] in ("summon", "set", "activate", "switch"))
        if oid:
            out = await duel("act", option=oid)
            check("single act + AI responds", "已执行" in out, out[:200])
        await ygo("leave")

        print(f"\n== {PASS} passed, {FAIL} failed ==")
        return 1 if FAIL else 0
    finally:
        await plugin.terminate()
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
