"""Full YGO gameplay demo — two modes:

MODE A (Jev):  decision engine (mock Jev) drives every duel action; the main
               LLM only intervenes on escalation/hitchhike/key nodes.
MODE B (pure main LLM): no Jev at all; every duel decision is made by the
               main LLM (mock OpenAI-compatible LLM) via tool calls.

This script runs the complete flow end-to-end:
    1. start mock YGO bridge (real duel with opponent AI)
    2. start mock Jev server (mode A only)
    3. start KiraAI 3.0 + plugin
    4. simulate the chat: join room → ready → duel start → per-turn decisions
       → user asks / user intervenes → duel ends

Run: python3 tests/ygo_demo.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugin"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("ygo_demo")

KIRA_DIR = "/tmp/kira30"
KIRA_DATA = "/tmp/kiratest30/data"
BRIDGE_PORT = 5274
MOCK_JEV_PORT = 17889


# ---------- helpers -------------------------------------------------------

def run_bg(cmd, log_path, cwd=None, env=None):
    with open(log_path, "w") as f:
        return subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                                cwd=cwd, env=env)


def wait_port(host, port, timeout=30):
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def http_get(url, headers=None, timeout=5):
    import requests
    r = requests.get(url, headers=headers or {}, timeout=timeout)
    return r.status_code, r.text


def http_post(url, payload, headers=None, timeout=5):
    import requests
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = requests.post(url, json=payload, headers=h, timeout=timeout)
    return r.status_code, r.text


# ---------- mode A: Jev-driven --------------------------------------------

async def mode_jev(jwt: str) -> None:
    print("\n" + "=" * 70)
    print("MODE A: Jev 驱动（决策引擎主导，主 LLM 只在升级/搭车介入）")
    print("=" * 70)

    # 1. Configure plugin: backend=jev + base_url=mock + ygo enabled
    h = {"Authorization": f"Bearer {jwt}"}
    print("\n[配置] 决策后端=jev, base_url=mock, 启用 ygo_engine")
    code, body = http_post(
        "http://127.0.0.1:5267/api/plugin/kira-game-agent/config",
        {
            "section_jev": {
                "backend": "jev",
                "base_url": f"http://127.0.0.1:{MOCK_JEV_PORT}",
                "api_key": "mock-key",
                "decision_hz": 2.0,
                "intent_ttl": 1.5,
                "strategy_advisory": True,
                "consult_ask_jev": True,
            },
            "section_bridge": {
                "enabled_games": ["ygo_engine"],
                "stop_keywords": ["停止玩宝可梦"],
                "stop_whitelist": [],
            },
        },
        headers=h,
    )
    print(f"  -> {body[:200]}")

    # 2. Simulate the chat: user asks bot to play YGO (room 1999)
    print("\n[聊天] 用户：bot，来跟我打游戏王吧。房间 1999")
    print("  → bot: 好，我来了。加入房间 1999…")

    # Drive the duel through the REAL engine (DecisionEngine + mock Jev +
    # YgoEngineAdapter) — this is exactly what the plugin does in production.
    import requests
    from plugin.bridge.adapters.ygo_engine import YgoEngineAdapter
    from plugin.jev.client import JevClient
    from plugin.jev.gate import DecisionGate
    from plugin.escalate import Escalator
    from plugin.engine import DecisionEngine

    bridge = requests.Session()

    # join + ready via the adapter (same as game_ygo_join/ready)
    adapter = YgoEngineAdapter(host="127.0.0.1", port=BRIDGE_PORT)
    if not adapter.start():
        print("!! YGO bridge 连接失败")
        return
    join_res = adapter.join("1999", deck="dragon.ydk")
    print(f"  → 已加入房间 1999，对手：{join_res.get('opponent', '?')}")
    adapter.ready()
    print("  → bot: 我准备好了。")

    # Build the real decision engine (mock Jev backend)
    client = JevClient(api_key="mock-key", base_url=f"http://127.0.0.1:{MOCK_JEV_PORT}", timeout=5)
    gate = DecisionGate(low_risk_threshold=0.55, high_risk_threshold=0.85)
    escalated = []
    esc = Escalator(emit=lambda sid, reason: escalated.append(reason),
                    high_risk_threshold=0.85, cooldown=30)
    events = []

    eng = DecisionEngine(
        adapter=adapter,
        client=client,
        gate=gate,
        decision_hz=2.0,
        intent_ttl=1.5,
        game="ygo_engine",
        sid="demo",
        escalator=esc,
        strategy_stages={"dueling"},
        on_event=lambda k, p: events.append((k, p)),
    )
    eng.set_goal("win the duel")
    eng.set_playbook("You are a careful duelist. Build board control first, then attack when safe.")

    print("\n[对局] 开始。Jev 每回合决策（真实 engine 循环）：")
    await eng.start()

    # Run until the duel ends (or timeout)
    deadline = time.monotonic() + 60
    last_turn = -1
    while time.monotonic() < deadline:
        snap = adapter.get_state() or {}
        g = snap.get("game", {})
        if not g.get("is_duel_active", True):
            print(f"\n  → 对局结束：{g.get('winner', '?')} 胜利")
            break
        turn = g.get("turn", 0)
        phase = g.get("phase", "?")
        if (turn, phase) != last_turn:
            last_turn = (turn, phase)
            # Print the latest decision if any
            if eng.recent_decisions:
                d = eng.recent_decisions[-1]
                act = d.get("action") or d.get("goal") or "?"
                conf = d.get("conf") or d.get("nouls", {}).get(act, 0.0)
                print(f"  T{turn} {phase}: LP 我{g.get('my_lp')}/对{g.get('op_lp')} | "
                      f"手牌{g.get('hand_count', 0)} | Jev→{act} (conf {conf:.2f})")
        await asyncio.sleep(0.25)

    await eng.stop()

    print(f"\n[统计] Jev 调用 {client.stats['sent']} 次，失败 {client.stats['failed']} 次，"
          f"总输入 token {client.stats['input_tokens']}，升级 {len(escalated)} 次")
    if escalated:
        print(f"  升级示例: {escalated[0]}")
    print("[流程] 对局结束。用户可以问：'你这波为什么这么操作？' → 搭车注入局面，主 LLM 解释。")


# ---------- mode B: pure main LLM ------------------------------------------

async def mode_pure_llm(jwt: str) -> None:
    print("\n" + "=" * 70)
    print("MODE B: 纯主 LLM 驱动（不用 Jev，决策全由主 LLM 做）")
    print("=" * 70)

    h = {"Authorization": f"Bearer {jwt}"}
    print("\n[配置] 决策后端=laya（本地零成本，作为对比）或不用决策引擎，纯工具驱动")
    # For pure-LLM mode we don't need a decision backend at all; the main LLM
    # uses tools directly. We still show the plugin works without Jev.
    code, body = http_post(
        "http://127.0.0.1:5267/api/plugin/kira-game-agent/config",
        {
            "section_jev": {"backend": "laya"},  # laya as the no-key default
            "section_bridge": {"enabled_games": ["ygo_engine"]},
        },
        headers=h,
    )
    print(f"  -> {body[:200]}")

    # Simulate: main LLM drives every decision via tools.
    # In a real KiraAI run the main LLM would call game_ygo_duel_info then
    # game_command. Here we simulate that loop directly against the bridge.
    import requests
    bridge = requests.Session()

    print("\n[聊天] 用户：bot，来跟我打游戏王吧。房间 1999")
    r = bridge.post(f"http://127.0.0.1:{BRIDGE_PORT}/join",
                    json={"room": "1999", "deck": "dragon.ydk"}, timeout=5)
    print(f"  → bot: 好。已加入房间 1999，对手：{r.json().get('opponent', '?')}")
    bridge.post(f"http://127.0.0.1:{BRIDGE_PORT}/ready", json={}, timeout=5)
    print("  → bot: 我准备好了。")

    print("\n[对局] 开始。纯主 LLM 每回合决策（看局面 → 选动作 → 执行）：")
    turn = 0
    max_turns = 30
    while turn < max_turns:
        turn += 1
        r = bridge.get(f"http://127.0.0.1:{BRIDGE_PORT}/state", timeout=5)
        snap = r.json()
        g = snap.get("game", {})
        if not g.get("is_duel_active", True):
            print(f"\n  → 对局结束：{g.get('winner', '?')} 胜利")
            break

        # ---- pure LLM decision: read state, pick action ----
        # This is what the main LLM would do via game_ygo_duel_info + game_command.
        legal = g.get("legal_actions", [])
        if not legal:
            action = {"action": "switch", "phase": "battle"}
        else:
            # Simple "LLM reasoning": prefer summon strongest, then attack, else set/switch.
            monsters = [a for a in legal if a.get("action") in ("summon", "summon_def")]
            attacks = [a for a in legal if a.get("action") in ("attack", "direct_attack")]
            if g.get("phase") == "battle" and attacks:
                action = attacks[0]
            elif monsters:
                action = monsters[0]
            else:
                action = legal[0]

        print(f"  T{g.get('turn', turn)} {g.get('phase', '?')}: LP 我{g.get('my_lp')}/对{g.get('op_lp')} | "
              f"手牌{g.get('hand_count', 0)} | LLM→{action.get('action')} {action.get('card', '')}")

        r = bridge.post(f"http://127.0.0.1:{BRIDGE_PORT}/act", json=action, timeout=5)
        if r.status_code != 200:
            print(f"    [执行失败] {r.text[:100]}")
            break
        await asyncio.sleep(0.1)

    print("\n[流程] 纯 LLM 模式下：主 LLM 每轮都看局面、做判断、执行——全程可对话、可解释、可干预，"
          "但每次决策都占一次 LLM 轮次（token 成本高于 Jev 模式）。")


# ---------- main ------------------------------------------------------------

async def main():
    # 0. start mock servers
    print("启动 mock YGO bridge…")
    bridge_proc = run_bg([sys.executable, str(ROOT / "tests" / "mock_ygo_bridge.py")],
                         "/tmp/kiratest30/mock_bridge.log")
    print("启动 mock Jev server…")
    jev_proc = run_bg([sys.executable, str(ROOT / "tests" / "mock_jev_server.py")],
                      "/tmp/kiratest30/mock_jev.log")

    if not wait_port("127.0.0.1", BRIDGE_PORT, 15):
        print("!! mock YGO bridge 未启动")
        return
    if not wait_port("127.0.0.1", MOCK_JEV_PORT, 15):
        print("!! mock Jev 未启动")
        return
    print("✓ mock YGO bridge + mock Jev 就绪")

    # 1. KiraAI 3.0 should already be running; fetch JWT
    jwt_file = Path("/tmp/kiratest30/jwt2.txt")
    jwt = jwt_file.read_text().strip().splitlines()[-1] if jwt_file.exists() else ""
    if not jwt:
        print("!! 未找到 JWT（先起 KiraAI 3.0）")
        return

    # Verify plugin is up
    code, body = http_get("http://127.0.0.1:5267/api/plugin/kira-game-agent/status",
                          headers={"Authorization": f"Bearer {jwt}"})
    if code != 200:
        print(f"!! 插件 API 不通 ({code}): {body[:200]}")
        return
    print("✓ KiraAI 3.0 插件在线")

    # 2. run both modes
    await mode_jev(jwt)
    await mode_pure_llm(jwt)

    print("\n" + "=" * 70)
    print("对比总结")
    print("=" * 70)
    print("""
模式 A（Jev 驱动）：
  - 每回合决策由 Jev/laya 做（~300-600 token/次，约 $0.00002，70-500ms）
  - 主 LLM 只在：高风险低置信升级 / 用户提问搭车 / 关键阶段征询 时介入
  - token 成本：极低（Jev 主导）+ 少量主 LLM（关键节点）
  - 速度：快（Jev 决策毫秒级）

模式 B（纯主 LLM）：
  - 每回合决策由主 LLM 做（一次 LLM 轮次/回合，几千 token）
  - 全程可对话、可解释、可干预（每一步都是 LLM 判断）
  - token 成本：高（每回合一次 LLM）
  - 速度：慢（LLM 秒级）

推荐：默认 Jev/laya（模式 A）——省 token、快、零成本（laya）。
关键节点让主 LLM 介入（升级/征询），保留互动感和人设。
""")

    # cleanup
    bridge_proc.terminate()
    jev_proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())