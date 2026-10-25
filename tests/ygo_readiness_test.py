"""Out-of-the-box readiness: "install the plugin, tell the bot to play YGO".

Verifies the last-mile pieces that make that sentence true:
- game_ygo op=install_bridge actually starts the installed bridge and the
  very next op=decks works against the REAL bridge (not a mock).
- game_play without a bridge points to install_bridge instead of a dead end.
- host without a configured public server WARNS instead of silently using
  localhost; an explicit server silences the warning.
- room events (opponent joins, duel starts) are emitted so the bot can drive
  the flow while waiting for a human.
- adaptive polling: idle rooms poll slowly, duels poll fast.
- hitchhike keeps injecting every turn (persist=False blocks must be
  re-supplied; deduplicating them would lose information).

Requires the installed bridge from install_ygo_bridge.sh for the first part
(/tmp/kira_install_test by default); that part skips if absent.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kira_stubs  # noqa: E402
kira_stubs.install()

import mock_ygo_bridge as mock  # noqa: E402
import requests  # noqa: E402

from plugin.main import KiraGameAgentPlugin  # noqa: E402
from plugin.tools import build_tools_for  # noqa: E402

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


def make_plugin(port, **ygo):
    ctx = SimpleNamespace(config={}, plugin_mgr=None, notices=[])

    async def publish_notice(sid, chain, is_mentioned=True):
        ctx.notices.append((sid, chain, is_mentioned))

    ctx.publish_notice = publish_notice
    cfg = {
        "section_jev": {"backend": "jev", "api_key": "", "decision_hz": 5.0},
        "section_bridge": {"enabled_games": ["ygo_engine"]},
        "section_ygo": {"ygo_mode": "network", "ygo_host": "127.0.0.1", "ygo_port": port, **ygo},
        "section_notify": {"notify_enabled": True, "notify_min_interval": 0.0},
    }
    return KiraGameAgentPlugin(ctx, cfg)


async def test_install_bridge_op():
    install_dir = Path(os.environ.get("YGO_TEST_BRIDGE_DIR", "/tmp/kira_install_test"))
    if not (install_dir / "run_bridge.sh").exists():
        print("SKIP: no installed bridge for install_bridge op")
        return
    plugin = make_plugin(5274)
    await plugin.initialize()
    plugin.ygo_installer.dir = install_dir
    plugin.ygo_installer.stop_bridge()
    tools = {t.name: t for t in build_tools_for("ygo_engine", plugin)}
    event = SimpleNamespace(sid="qq:gm:1")
    out = await tools["game_ygo"].execute(event, op="install_bridge")
    check("install_bridge starts real bridge", "健康检查" in out, out[:240])
    out = await tools["game_ygo"].execute(event, op="decks")
    check("decks works right after", "AI_BlueEyes" in out or "可用卡组" in out, out[:200])
    out = await tools["game_ygo"].execute(event, op="bridge_status")
    check("bridge_status reports ready", "健康: True" in out, out[:200])
    plugin.ygo_installer.stop_bridge()
    await plugin.terminate()


async def test_guidance_and_server_warning():
    plugin = make_plugin(5999)  # unreachable port on purpose
    await plugin.initialize()
    event = SimpleNamespace(sid="qq:gm:1")
    out = await plugin.game_play(event, game="ygo_engine")
    check("game_play points to install_bridge", "install_bridge" in out, out[:200])

    srv, port = (None, None)
    srv = mock.ThreadingHTTPServer(("127.0.0.1", 0), mock.YgoBridgeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    plugin = make_plugin(port)
    await plugin.initialize()
    tools = {t.name: t for t in build_tools_for("ygo_engine", plugin)}
    try:
        out = await tools["game_ygo"].execute(event, op="host", room="arena", deck="dragon.ydk")
        check("host warns when no public server configured", "未指定公共服地址" in out, out[:260])
        out = await tools["game_ygo"].execute(event, op="leave")
        out = await tools["game_ygo"].execute(event, op="host", room="arena2", deck="dragon.ydk",
                                              server="public.example:7911")
        check("explicit server silences warning", "未指定公共服地址" not in out, out[:240])
        check("explicit server shown", "public.example:7911" in out, out[:240])
        await tools["game_ygo"].execute(event, op="leave")

        # Room events while a human joins and the duel starts.
        out = await tools["game_ygo"].execute(event, op="host", room="arena3", deck="dragon.ydk")
        eng = plugin._default_engine(event.sid, "ygo_engine")
        eng._tick  # keep reference
        plugin._events.clear()
        human = requests.post(f"http://127.0.0.1:{port}/join",
                              json={"room": "arena3", "deck": "demo.ydk"}, timeout=5).json()
        hs = {"session": human["session"]}
        await eng._tick()
        kinds = [e["kind"] for e in plugin._events]
        check("opponent join emits room_update", "room_update" in kinds, str(kinds))
        texts = [e["payload"].get("text", "") for e in plugin._events if e["kind"] == "room_update"]
        check("join event mentions opponent", any("对手已进房" in t for t in texts), str(texts))

        await tools["game_ygo"].execute(event, op="ready")
        requests.post(f"http://127.0.0.1:{port}/ready", json={**hs, "ready": True}, timeout=5)
        await eng._tick()
        check("ready/start flow emits events", any(e["kind"] == "room_update" for e in plugin._events))
        out = await tools["game_ygo"].execute(event, op="start")
        check("duel start works after events", "对局已开始" in out, out[:200])
        await eng._tick()
        texts = [e["payload"].get("text", "") for e in plugin._events if e["kind"] == "room_update"]
        check("duel start event emitted", any("对局开始" in t for t in texts), str(texts[-3:]))

        # Adaptive polling delays.
        eng.cached_snapshot = {"game": {"is_duel_active": False}}
        check("idle polls slowly", eng._poll_delay() >= 3.0, str(eng._poll_delay()))
        eng.cached_snapshot = {"game": {"is_duel_active": True}}
        check("duel polls at base rate", eng._poll_delay() <= eng.decision_interval + 1e-9)
        eng.cached_snapshot = {"game": {"is_duel_active": True, "response_window": True}}
        check("response window polls fastest", eng._poll_delay() <= eng.decision_interval + 1e-9,
              f"{eng._poll_delay()} vs {eng.decision_interval}")
        eng.decision_interval = 1.0
        check("response window halves interval at 1s", abs(eng._poll_delay() - 0.5) < 1e-9,
              str(eng._poll_delay()))

        # hitchhike must inject on EVERY turn (persist=False is not in history).
        from core.prompt_manager import Prompt
        adapter = eng.adapter
        adapter.cached_snapshot = adapter.get_state()
        req1 = SimpleNamespace(system_prompt=[Prompt("ENV", name="chat_env")], tool_set=None)
        req2 = SimpleNamespace(system_prompt=[Prompt("ENV", name="chat_env")], tool_set=None)
        await plugin.hitchhike(event, req1, None)
        await plugin.hitchhike(event, req2, None)
        check("context injected every turn", len(req1.system_prompt) == 2 and len(req2.system_prompt) == 2,
              f"{len(req1.system_prompt)}/{len(req2.system_prompt)}")
        await tools["game_ygo"].execute(event, op="leave")
    finally:
        await plugin.terminate()
        srv.shutdown()


async def main():
    await test_install_bridge_op()
    await test_guidance_and_server_warning()
    print(f"\n== {PASS} passed, {FAIL} failed ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
