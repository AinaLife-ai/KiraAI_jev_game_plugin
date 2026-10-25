"""YGO full-chain operations for the main LLM — no Jev/laya required.

Covers the complete loop against real players:
    decks → build_deck → host/join → set_deck → ready → start →
    state/act/chain (incl. opponent-turn response windows) → leave/surrender

Every bridge call runs in a worker thread (``asyncio.to_thread``) so a slow
engine never blocks KiraAI's event loop, and every result is checked at the
business level: a rejected move is reported as rejected, never as executed.

Answers are compact Chinese text with the exact next step to call, so the
main LLM can play a whole duel with tool calls only.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .ygo_ops import (
    act_payload,
    deck_summary,
    describe_option,
    format_decks,
    format_duel_state,
    options_map,
    parse_ydk,
    resolve_option,
    validate_deck,
)

log = logging.getLogger("kira_game_agent.ygo")

NEXT = {
    "host": "下一步：等对手进房后 op=ready；双方就绪且你是房主时用 op=start。",
    "join": "下一步：op=ready 准备（若要换卡组先 op=set_deck）。",
    "ready": "双方已准备后，房主用 op=start 开始对局。",
    "start": "对局开始：用 game_ygo_duel op=state 看局面，op=act option=编号 操作。",
}


async def _engine(plugin, sid: str):
    eng = plugin._default_engine(sid, "ygo_engine")
    if eng is None:
        eng = plugin._get_or_create_engine(sid, "ygo_engine")
    return eng


async def _ready_bridge(plugin, sid: str, start_loop: bool = True):
    """Engine + connected adapter, or (None, error text)."""
    eng = await _engine(plugin, sid)
    if eng is None or eng.adapter is None:
        return None, None, "无法创建 YGO 引擎：请先在配置页启用 ygo_engine 并检查桥地址。"
    adapter = eng.adapter
    eng.set_playbook(plugin._playbook_for("ygo_engine"))
    if not getattr(adapter, "_connected", False):
        ok = await asyncio.to_thread(adapter.start)
        if not ok:
            err = getattr(adapter, "last_error", "") or "桥不可达"
            return eng, adapter, (
                f"YGO 桥连接失败（{getattr(adapter, 'host', '?')}:{getattr(adapter, 'port', '?')}）：{err}\n"
                "可直接用 game_ygo op=install_bridge 让我自动下载安装并启动真实引擎桥"
                "（ProjectIgnis/windbot + .NET 8，首次约几分钟，无需 EDOPro），"
                "或你手动启动桥后再试。"
            )
    if start_loop and not eng.running:
        try:
            await eng.start()
        except Exception as e:  # noqa: BLE001
            log.warning("ygo engine loop start failed: %s", e)
    return eng, adapter, ""


def _fail(prefix: str, res: Any, adapter=None) -> str:
    detail = ""
    if isinstance(res, dict):
        detail = str(res.get("error") or res.get("detail") or res.get("message") or "")
    if not detail and adapter is not None:
        detail = str(getattr(adapter, "last_error", "") or "")
    return f"{prefix}：{detail or '引擎拒绝'}（未执行，未改动任何状态）"


def _stage_after(eng, snapshot: dict) -> None:
    stage = (snapshot or {}).get("stage")
    if stage:
        try:
            eng.set_stage(stage)
        except Exception:  # noqa: BLE001
            pass


def _brief_state(eng, snap: dict, full: bool = False) -> str:
    return format_duel_state(
        snap,
        stage=eng.stage() if eng is not None else "",
        goal=getattr(eng, "goal", "") or "",
        max_options=30 if full else 15,
        with_effects=full,
    )


def _deck_zones(main: str = "", extra: str = "", side: str = "", ydk: str = "") -> dict:
    if ydk:
        return parse_ydk(ydk)
    def split(text: str) -> list[str]:
        return [c.strip() for c in str(text or "").replace("，", ",").replace("\n", ",").split(",") if c.strip()]
    return {"main": split(main), "extra": split(extra), "side": split(side)}


async def _bridge_admin(plugin, op: str, sid: str = "") -> str:
    """Install / start / inspect the real engine bridge from the LLM itself."""
    installer = plugin.ygo_installer
    status = installer.status()
    if op == "bridge_status":
        lines = [f"桥状态: {status['state']} | 已安装: {status['installed']} | "
                 f"运行: {status['bridge_running']} | 健康: {status['bridge_ok']}",
                 f"目录: {status['dir']}"]
        if status.get("error"):
            lines.append("错误: " + status["error"])
        lines.extend(status.get("log", [])[-6:])
        if status["bridge_ok"]:
            lines.append("桥已就绪：下一步 op=decks → host/join → ready → start。")
        return "\n".join(lines)

    # install_bridge
    if status["bridge_ok"]:
        return ("真实引擎桥已在运行且健康。下一步：op=decks 看卡组（66 套真实卡组）"
                "或 build_deck 自组 → host 建房 / join 进房 → ready → start。")
    if status["installed"]:
        result = await installer.start_bridge()
        if result.get("healthy"):
            return "桥已启动并通过健康检查。下一步：op=decks → host/join → ready → start。"
        return ("桥已安装但启动健康检查未过：" + str(result.get("message", ""))
                + "\n可用 op=bridge_status 看日志，或检查 .NET/端口占用。")
    if status["state"] == "installing":
        return ("正在自动安装桥（下载 .NET/引擎/卡库）…完成后会自动启动并通知你。"
                "可用 op=bridge_status 查看进度。\n" + "\n".join(status.get("log", [])[-4:]))
    ok, message = installer.install(auto_start=True, sid=sid)
    if not ok:
        return f"无法开始安装：{message}"
    return ("已开始自动安装真实引擎桥（ProjectIgnis/windbot + .NET 8，含卡库，"
            "约 1-5 分钟取决于网络）。**完成后会自动启动桥并通知你**，"
            "届时我会继续 组卡→建房/进房→准备→开始。可用 op=bridge_status 查看进度。")


async def lifecycle(plugin, event, op: str, **kw) -> str:
    """Room/deck lifecycle operations (game_ygo tool)."""
    sid = event.sid
    op = (op or "").strip().lower()
    if op in ("install_bridge", "bridge_status"):
        return await _bridge_admin(plugin, op, sid)
    eng, adapter, err = await _ready_bridge(plugin, sid, start_loop=op not in ("decks", "build_deck", "rooms"))
    if adapter is None:
        return err
    if err:
        return err

    if op == "decks":
        res = await asyncio.to_thread(adapter.list_decks)
        if res.get("error"):
            return _fail("获取卡组列表失败", res, adapter)
        return format_decks(res) + "\n用 op=set_deck 选卡，或 op=build_deck 上传 .ydk 组卡。"

    if op == "build_deck":
        name = str(kw.get("name") or kw.get("deck") or "").strip()
        if not name:
            return "请给卡组命名（name 参数，如 blackwing.ydk）。"
        zones = _deck_zones(kw.get("main", ""), kw.get("extra", ""), kw.get("side", ""), kw.get("ydk", ""))
        problem = validate_deck(zones)
        if problem:
            return f"卡组未通过本地校验（未上传）：{problem}"
        res = await asyncio.to_thread(adapter.build_deck, name, zones, str(kw.get("ydk", "") or ""))
        if not res.get("ok") or res.get("error"):
            return _fail("组卡失败", res, adapter)
        deck_name = str(res.get("name") or name)
        # Also select it bridge-side (inside a room this swaps our deck before
        # the duel starts), not just in the adapter's local field.
        selected = await asyncio.to_thread(adapter.set_deck, deck_name)
        note = "" if selected else f"\n（卡组已保存，但设为当前卡组失败：{getattr(adapter, 'last_error', '') or '桥拒绝'}）"
        return (f"卡组已保存：{deck_name}（{deck_summary(zones)}）"
                + ("，并已设为当前卡组。" if selected else "。")
                + note + "\n下一步：op=host 建房或 op=join 进房。")

    if op == "rooms":
        res = await asyncio.to_thread(adapter.list_rooms)
        if res.get("error"):
            return _fail("获取房间列表失败", res, adapter)
        rooms = res.get("rooms") or []
        if not rooms:
            return "当前没有可加入的房间。用 op=host 创建一个，等真人玩家加入。"
        lines = []
        for r in rooms[:20]:
            if isinstance(r, dict):
                players = ",".join(str(p) for p in (r.get("players") or []))
                lines.append(f"- {r.get('name')} [{r.get('mode', '')}] 玩家:{players or '空'} "
                             f"{'已开战' if r.get('duel_active') else '等待中'}")
            else:
                lines.append(f"- {r}")
        return "房间列表:\n" + "\n".join(lines) + "\n用 op=join room=房间名 加入。"

    if op in ("host", "join"):
        room = str(kw.get("room", "") or "").strip()
        if op == "join" and not room:
            return "请提供要加入的房间名/房号（room 参数）。可先用 op=rooms 查看。"
        deck = str(kw.get("deck", "") or "").strip()
        server = str(kw.get("server", "") or "").strip()
        password = str(kw.get("password", "") or "")
        mode = str(kw.get("mode", "") or "")
        call = adapter.host_room if op == "host" else adapter.join
        if op == "host":
            res = await asyncio.to_thread(call, room, deck, server, password, mode, None)
        else:
            res = await asyncio.to_thread(call, room, deck, server, password, mode)
        if not res.get("ok") or res.get("error"):
            return _fail("建房失败" if op == "host" else "进房失败", res, adapter)
        if eng is not None:
            eng.set_stage("joining" if op == "host" else "confirming_opponent")
            eng.set_goal(str(kw.get("goal") or "赢下这场决斗（网络对战，尊重对手）"))
        room_name = res.get("room") or room or getattr(adapter, "room", "")
        players = res.get("players") or []
        opponent = res.get("opponent") or getattr(adapter, "opponent", "")
        head = ("已创建房间" if op == "host" else "已加入房间") + f" {room_name}"
        details = []
        if players:
            details.append("玩家=" + ",".join(str(p) for p in players))
        if opponent:
            details.append(f"对手={opponent}")
        if deck or getattr(adapter, "deck", ""):
            details.append(f"卡组={deck or adapter.deck}")
        snap = await asyncio.to_thread(adapter.get_state)
        if eng is not None:
            _stage_after(eng, snap)
        actual_server = ""
        if isinstance(snap.get("room"), dict):
            actual_server = str(snap["room"].get("server") or "")
        if actual_server or server or getattr(adapter, "server_url", ""):
            details.append(f"服务器={actual_server or server or adapter.server_url}")
        if not (server or getattr(adapter, "server_url", "")):
            details.append("⚠未指定公共服地址，使用桥默认（通常本机 127.0.0.1:7911）；"
                           "打公共服请在配置页填 ygo_server 或下次带 server=地址:端口")
        body = _brief_state(eng, snap) if snap.get("game") else ""
        return "\n".join(x for x in [head + (" | " + " ".join(details) if details else ""), body, NEXT[op]] if x)

    if op == "set_deck":
        deck = str(kw.get("deck", "") or "").strip()
        if not deck:
            return "请提供卡组文件名（deck 参数）。可先用 op=decks 查看。"
        ok = await asyncio.to_thread(adapter.set_deck, deck)
        if not ok:
            return _fail("设置卡组失败", {}, adapter)
        return f"已选用卡组 {deck}。未开始的房间会在开局时使用它。"

    if op == "ready":
        ok = await asyncio.to_thread(adapter.ready, True)
        if not ok:
            return _fail("准备失败", {}, adapter)
        if eng is not None:
            eng.set_stage("readying")
        return "已准备（对手可见）。" + NEXT["ready"]

    if op == "start":
        res = await asyncio.to_thread(adapter.start_duel)
        if not res.get("ok") or res.get("error"):
            return _fail("开始对局失败", res, adapter) + "\n通常需要：双方都已进房并 ready，且你是房主。"
        if eng is not None:
            eng.set_stage("dueling")
        snap = res.get("state") if isinstance(res.get("state"), dict) else await asyncio.to_thread(adapter.get_state)
        return "对局已开始！\n" + _brief_state(eng, snap, full=False) + "\n" + NEXT["start"]

    if op == "leave":
        res = await asyncio.to_thread(adapter.leave)
        if res.get("error"):
            return _fail("离开房间失败", res, adapter)
        if eng is not None:
            eng.set_stage("idle")
            eng.set_goal("")
        return "已离开房间并清理对局状态。"

    if op == "surrender":
        res = await asyncio.to_thread(adapter.surrender)
        if not res.get("ok") or res.get("error"):
            return _fail("投降失败", res, adapter)
        if eng is not None:
            eng.set_stage("ended")
        winner = res.get("winner") or "对手"
        return f"已投降，胜者：{winner}。可用 op=leave 退房或 op=host/join 再来一局。"

    if op == "chat":
        message = str(kw.get("message", "") or "").strip()
        if not message:
            return "请提供要发送给对手的聊天内容（message 参数）。"
        res = await asyncio.to_thread(adapter.chat, message)
        if not res.get("ok") or res.get("error"):
            return _fail("发送聊天失败", res, adapter)
        return f"已向房间发送：{message}"

    return f"未知操作 {op}。支持 install_bridge/bridge_status/decks/build_deck/rooms/host/join/set_deck/ready/start/leave/surrender/chat。"


async def duel(plugin, event, op: str, **kw) -> str:
    """In-duel information and actions (game_ygo_duel tool)."""
    sid = event.sid
    op = (op or "state").strip().lower()
    eng, adapter, err = await _ready_bridge(plugin, sid)
    if adapter is None:
        return err
    if err:
        return err

    if op == "state":
        # Duel decisions need the live position: default to a fresh read
        # (localhost bridge ≈ milliseconds). refresh=false opts into the
        # short-lived cache for cheap repeated peeks.
        refresh = bool(kw.get("refresh", True))
        if refresh or not getattr(adapter, "cached_snapshot", None):
            snap = await asyncio.to_thread(adapter.get_state)
        else:
            snap = adapter.get_cached_state(max_age=5.0)
        if not snap or not snap.get("game"):
            return ("还没有对局状态。先用 game_ygo 完成：decks→host/join→ready→start。\n"
                    + (f"桥状态：{getattr(adapter, 'last_error', '')}" if getattr(adapter, "last_error", "") else ""))
        _stage_after(eng, snap)
        return _brief_state(eng, snap, full=bool(kw.get("full", False)))

    if op == "log":
        snap = await asyncio.to_thread(adapter.get_state)
        g = snap.get("game") or {}
        logs = g.get("public_log") or []
        if not logs:
            return "暂无公开对局日志。"
        n = int(kw.get("n", 12) or 12)
        return "公开对局日志（最新在后）:\n" + "\n".join(str(x) for x in logs[-n:])

    if op in ("act", "chain"):
        raw_options: list[str] = []
        if op == "chain":
            raw_options = [c.strip() for c in str(kw.get("options", "") or "").replace("，", ",").split(",") if c.strip()]
            if not raw_options:
                return "chain 需要 options 参数（逗号分隔的选项编号，如 '0,3,5'）。"
            if len(raw_options) > 12:
                # Keep the whole call well inside KiraAI's tool timeout.
                raw_options = raw_options[:12]
        else:
            single = kw.get("option", "")
            if single in (None, ""):
                # legacy semantic call: action/card/position/target/phase
                if not kw.get("action"):
                    return "请给出要执行的合法选项编号（option 参数，先用 op=state 查看）。"
                raw_options = [_legacy_ref(kw)]
            else:
                raw_options = [str(single)]
        executed: list[str] = []
        snap = await asyncio.to_thread(adapter.get_state)
        for ref in raw_options:
            game = (snap or {}).get("game") or {}
            if game.get("multi_select"):
                # Real-engine multi-card decisions (tributes/materials/sums):
                # submit the index list verbatim; the bridge validates count.
                if op == "chain":
                    return "多选决策（祭品/素材/凑和）请用 op=act 一次提交，不用 chain。"
                res = await asyncio.to_thread(adapter.submit, {"option": str(ref)})
                if not res.get("ok"):
                    return _fail(f"多选提交 {ref} 失败", res, adapter) + "\n" + _brief_state(eng, snap, full=False)
                executed.append(f"多选 {ref}")
                new_state = res.get("state")
                snap = new_state if isinstance(new_state, dict) and new_state.get("game") else await asyncio.to_thread(adapter.get_state)
                _stage_after(eng, snap)
                continue
            action = resolve_option(snap, ref)
            if action is None:
                head = (f"已执行 {len(executed)} 步：{'；'.join(executed)}\n" if executed else "")
                return (head + f"选项 {ref} 当前不合法（局面已变化），未执行。\n"
                        + _brief_state(eng, snap, full=False)
                        + "\n请按最新选项重新决策。")
            payload = act_payload(action, ref)
            res = await asyncio.to_thread(adapter.submit, payload)
            if not res.get("ok"):
                head = (f"已执行 {len(executed)} 步：{'；'.join(executed)}\n" if executed else "")
                return head + _fail(f"执行 {describe_option(str(ref), action, with_effect=False)} 失败", res, adapter) \
                    + "\n" + _brief_state(eng, snap, full=False)
            executed.append(describe_option(str(ref), action, with_effect=False))
            new_state = res.get("state")
            if isinstance(new_state, dict) and new_state.get("game"):
                snap = new_state
            else:
                snap = await asyncio.to_thread(adapter.get_state)
            _stage_after(eng, snap)
        head = "已执行：" + "；".join(executed)
        return head + "\n" + _brief_state(eng, snap, full=False)

    return f"未知操作 {op}。支持 state/act/chain/log。"


def _legacy_ref(kw: dict) -> str:
    """Translate action+card style calls into an option reference we can
    resolve against the live legal list."""
    action = str(kw.get("action", "")).strip()
    for key in ("card", "phase", "target", "position"):
        if kw.get(key):
            return f"{action}:{kw[key]}"
    return action
