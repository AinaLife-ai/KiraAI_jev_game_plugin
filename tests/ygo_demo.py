"""Playable YGO demo — two modes, no KiraAI installation required.

MODE A (auto, Jev): real JevClient -> mock TypeSafe server, DecisionEngine
    with auto_decision=True plays a single-player duel by itself.
MODE B (main LLM, network): NO Jev. The plugin's real tools (game_ygo /
    game_ygo_duel) run the full network chain against a second client that
    acts as the human player. A deterministic policy stands in for the main
    LLM's choices; in production the main LLM reads the same tool output and
    answers with an option id.

    python3 tests/ygo_demo.py        # both modes
    python3 tests/ygo_demo.py auto   # only Jev auto mode
    python3 tests/ygo_demo.py llm    # only main-LLM network mode

Framework-level loading is covered separately by tests/kira_real_load_test.py
(real KiraAI v3.1.0) and the full no-Jev network chain by
tests/ygo_network_test.py.
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

import mock_ygo_bridge as mock  # noqa: E402
from plugin.bridge.adapters.ygo_engine import YgoEngineAdapter  # noqa: E402
from plugin.engine import DecisionEngine  # noqa: E402
from plugin.jev.gate import DecisionGate  # noqa: E402
from plugin.ygo_ops import options_map  # noqa: E402


def start_bridge():
    srv = mock.ThreadingHTTPServer(("127.0.0.1", 0), mock.YgoBridgeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[bridge] mock YGO bridge on 127.0.0.1:{srv.server_address[1]}")
    return srv, srv.server_address[1]


async def mode_auto(port: int) -> None:
    """Jev/laya style automatic play (opt-in)."""
    from http.server import HTTPServer
    from mock_jev_server import MockJevHandler
    from plugin.jev.client import JevClient

    jev_srv = HTTPServer(("127.0.0.1", 0), MockJevHandler)
    threading.Thread(target=jev_srv.serve_forever, daemon=True).start()
    client = JevClient(api_key="mock-key", base_url=f"http://127.0.0.1:{jev_srv.server_address[1]}", timeout=5)
    adapter = YgoEngineAdapter(host="127.0.0.1", port=port, game_mode="single")
    assert adapter.start()
    adapter.host_room("demo-auto", deck="dragon.ydk", mode="single")
    adapter.ready()
    adapter.start_duel()
    eng = DecisionEngine(adapter, client, DecisionGate(), decision_hz=4,
                         game="ygo_engine", sid="demo", auto_decision=True,
                         on_event=lambda k, p: None)
    eng.set_goal("win the duel")
    await eng.start()
    print("[auto] Jev 自动对局中（每行=引擎真实动作）…")
    seen = 0
    deadline = time.monotonic() + 25
    room = mock.STATE.rooms["demo-auto"]
    while time.monotonic() < deadline:
        if len(room.log) > seen:
            for line in room.log[seen:]:
                print("   ", line)
            seen = len(room.log)
        if not room.duel_active:
            break
        await asyncio.sleep(0.4)
    await eng.stop()
    print(f"[auto] 结束。胜者={room.winner or '未分'} | Jev 调用 {client.stats['sent']} 次 "
          f"失败 {client.stats['failed']} 次 | 输入 token ≈{client.stats['input_tokens']}")
    jev_srv.shutdown()


def choose_option(state):
    """Stand-in for the main LLM: pick an option id from the legal list."""
    options = list(options_map(state).items())
    if not options:
        return None

    def score(item):
        _, a = item
        action, card = a.get("action"), str(a.get("card", ""))
        if action == "activate" and card == "Pot of Greed":
            return 100
        if action == "direct_attack":
            return 90
        if action == "attack":
            return 80
        if action == "summon":
            return 70 + (a.get("atk") or 0) / 1000
        if action == "set" and card == "Mirror Force":
            return 60
        if action == "switch" and a.get("phase") == "battle":
            return 50
        if action == "switch" and a.get("phase") == "end":
            return 40
        if action == "activate":
            return 35
        return 10

    return max(options, key=score)[0]


async def mode_llm_network(port: int) -> None:
    """No Jev: real tools drive the whole network chain vs a human client."""
    import kira_stubs
    kira_stubs.install()
    import requests
    from plugin.main import KiraGameAgentPlugin
    from plugin.tools import build_tools_for

    class Ctx:
        config = {}
        plugin_mgr = None

        async def publish_notice(self, *a, **k):
            pass

    plugin = KiraGameAgentPlugin(Ctx(), {
        "section_jev": {"backend": "jev", "api_key": ""},       # no backend at all
        "section_bridge": {"enabled_games": ["ygo_engine"]},
        "section_ygo": {"ygo_mode": "network", "ygo_host": "127.0.0.1", "ygo_port": port},
        "section_notify": {"notify_enabled": False},
    })
    await plugin.initialize()
    event = SimpleNamespace(sid="demo")
    tools = {t.name: t for t in build_tools_for("ygo_engine", plugin)}
    base = f"http://127.0.0.1:{port}"

    async def ygo(op, **kw):
        out = await tools["game_ygo"].execute(event, op=op, **kw)
        print(f"[tool game_ygo {op}] {out.splitlines()[0] if out else ''}")
        return out

    async def duel(op="state", **kw):
        return await tools["game_ygo_duel"].execute(event, op=op, **kw)

    try:
        await ygo("decks")
        await ygo("host", room="demo-llm", deck="dragon.ydk")
        human = requests.post(base + "/join", json={"room": "demo-llm", "deck": "demo.ydk"}, timeout=5).json()
        hs = {"session": human["session"]}
        print(f"[human] 真人玩家进房：{human.get('opponent')} vs 挑战者")
        await ygo("ready")
        requests.post(base + "/ready", json={**hs, "ready": True}, timeout=5)
        await ygo("start")

        room = mock.STATE.rooms["demo-llm"]
        adapter = plugin._default_engine("demo", "ygo_engine").adapter
        deadline = time.monotonic() + 30
        seen = 0
        while time.monotonic() < deadline and room.duel_active:
            # human seat plays whenever it has options (a simple "real player")
            hstate = requests.get(base + "/state", params=hs, timeout=5).json()
            hopts = list(options_map(hstate).items())
            if hopts:
                oid = choose_option(hstate)
                requests.post(base + "/act", json={**hs, "option": oid}, timeout=5)
            # our side: main LLM reads state and acts through the tool
            snap = adapter.get_state()
            if options_map(snap):
                oid = choose_option(snap)
                out = await duel("act", option=oid)
                print(f"[llm act {oid}] {out.splitlines()[0]}")
            if len(room.log) > seen:
                for line in room.log[seen:]:
                    print("   ", line)
                seen = len(room.log)
            await asyncio.sleep(0.3)
        print(f"[llm] 网络对局结束，胜者={room.winner or '未分'}；双方 LP "
              f"{room.seats[0].lp}/{room.seats[1].lp}")
        await ygo("leave")
    finally:
        await plugin.terminate()


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    srv, port = start_bridge()
    try:
        if which in ("all", "auto"):
            await mode_auto(port)
        if which in ("all", "llm"):
            mock.STATE.rooms.clear()
            await mode_llm_network(port)
    finally:
        srv.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
