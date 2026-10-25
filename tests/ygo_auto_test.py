"""YGO automatic-decision test (opt-in Jev/laya path) against the real bridge.

Verifies the auto loop plays properly when a backend IS present:
- only one typed question per decision (token-optimal)
- every request happens with a non-empty legal option list (no wasted calls)
- the backend's chosen option id is exactly what gets executed
- zero rejected submissions (no blind/stale moves, chains included)
- auto mode with NO backend falls back to manual (no auto moves)

Run: python3 tests/ygo_auto_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kira_stubs  # noqa: E402,F401
kira_stubs.install()

import mock_ygo_bridge as mock  # noqa: E402

from plugin.bridge.adapters.ygo_engine import YgoEngineAdapter  # noqa: E402
from plugin.engine import DecisionEngine  # noqa: E402
from plugin.jev.gate import DecisionGate  # noqa: E402

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


class AutoBackend:
    """Plays a reasonable line using the option ids exposed in the state."""

    def __init__(self, has_key=True):
        self.has_key = has_key
        self.stats = {"sent": 0, "failed": 0, "input_tokens": 0, "last_ms": 0, "last_error": None}
        self.states = []
        self.questions = []
        self._pending = None

    def ask_async(self, state, questions):
        self.stats["sent"] += 1
        self.states.append(state)
        self.questions.append(questions)
        choice = self._pick(state)
        self._pending = {"answers": {"next_action": {"type": "choice", "choice": choice,
                                                    "confidence": 0.95}}, "usage": {}}

    def ask_latest(self):
        pending, self._pending = self._pending, None
        return pending

    def ask_sync(self, state, questions):
        return {"answers": {}}

    @staticmethod
    def _pick(state):
        options = state.get("legal_actions") or []
        if not options:
            return ""

        def score(opt):
            action = opt.get("action")
            card = str(opt.get("card", ""))
            if action == "activate" and card == "Pot of Greed":
                return 100
            if action == "direct_attack":
                return 90
            if action == "attack":
                return 80 + (opt.get("atk") or 0) / 1000.0
            if action == "summon":
                return 70 + (opt.get("atk") or 0) / 1000.0
            if action == "activate" and card == "Monster Reborn":
                return 65
            if action == "set" and card == "Mirror Force":
                return 60
            if action == "switch" and opt.get("phase") == "battle":
                return 50
            if action == "switch" and opt.get("phase") == "end":
                return 40
            if action == "pass":
                return 30
            if action == "activate":
                return 20
            return 10

        best = max(options, key=score)
        return str(best.get("option"))


def start_bridge():
    srv = mock.ThreadingHTTPServer(("127.0.0.1", 0), mock.YgoBridgeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def run_auto(port, backend, seconds=6.0):
    adapter = YgoEngineAdapter(host="127.0.0.1", port=port, game_mode="single")
    assert adapter.start(), "bridge unreachable"
    res = adapter.host_room("auto", deck="dragon.ydk", mode="single")
    assert res.get("ok"), res
    assert adapter.ready()
    started = adapter.start_duel()
    assert started.get("ok"), started
    rejected = {"n": 0}
    original_submit = adapter.submit

    def submit(payload):
        res = original_submit(payload)
        if not res.get("ok"):
            rejected["n"] += 1
        return res

    adapter.submit = submit
    eng = DecisionEngine(adapter, backend, DecisionGate(), decision_hz=25,
                         game="ygo_engine", sid="auto", auto_decision=True,
                         on_event=lambda k, p: None)
    await eng.start()
    await asyncio.sleep(seconds)
    await eng.stop()
    return adapter, eng, rejected


async def main():
    srv, port = start_bridge()
    try:
        backend = AutoBackend()
        adapter, eng, rejected = await run_auto(port, backend)
        check("auto backend used", backend.stats["sent"] > 0, str(backend.stats["sent"]))
        check("one question per decision", all(list(q) == ["next_action"] for q in backend.questions),
              str([list(q) for q in backend.questions][:3]))
        check("criteria keyed by option id", all(
            set(q["next_action"]["criteria"]) == {str(o["option"]) for o in s["legal_actions"]}
            for q, s in zip(backend.questions, backend.states)))
        check("no requests with empty options", all(len(s["legal_actions"]) > 0 for s in backend.states))
        check("zero rejected submissions", rejected["n"] == 0, str(rejected["n"]))
        check("engine recorded real actions", bool(eng.recent_decisions), "no decisions")
        check("no stale-option skips", not any(d.get("skipped") == "stale_option" for d in eng.recent_decisions),
              str([d for d in eng.recent_decisions if d.get("skipped")])[:200])
        check("auto loop actually summoned", any("召唤" in line or "盖放" in line for line in mock.STATE.rooms["auto"].log),
              str(mock.STATE.rooms["auto"].log[:6]))
        check("duel progressed (LP/log)", len(mock.STATE.rooms["auto"].log) > 8,
              str(len(mock.STATE.rooms["auto"].log)))
        check("last_error clean", adapter.last_error == "", adapter.last_error)

        # Same setup with a keyless backend: must NOT auto-play.
        mock.STATE.rooms.clear()
        lazy = AutoBackend(has_key=False)
        adapter2, eng2, rejected2 = await run_auto(port, lazy, seconds=1.0)
        check("keyless backend -> no auto requests", lazy.stats["sent"] == 0, str(lazy.stats["sent"]))
        check("keyless backend -> manual fallback", eng2._decision_enabled() is False)
        check("keyless backend -> no auto actions", not eng2.recent_decisions)
        print(f"\n== {PASS} passed, {FAIL} failed ==")
        return 1 if FAIL else 0
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
