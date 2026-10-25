"""YGO duel domain helpers — shared by the LLM tools and the decision engine.

Design (validated against real YGO agent projects, see guides/YGO指南.md):
- The **engine/bridge owns the rules**. It publishes the currently legal
  options; we never invent moves.
- Options are addressed by **option id**, never by action name alone: five
  different summonable monsters all share ``action == "summon"`` and must
  stay distinguishable (selection and execution resolve the *same* option).
- Everything returned to the LLM is compact Chinese text: complete enough to
  decide (stats, positions, one-line effects, public log) without dumping raw
  JSON.
- Public information only: the opponent's hand is a count, face-down cards
  stay unnamed unless the bridge explicitly reveals them.

Pure functions — no I/O, unit-testable offline.
"""
from __future__ import annotations

from typing import Any, Optional

# ygopro-core response codes (kept for bridges that want the numeric form).
ACTION_CODES = {
    "set": 1,
    "reposition": 2,
    "special_summon": 3,
    "summon": 4,
    "summon_def": 5,
    "attack": 6,
    "direct_attack": 7,
    "activate": 8,
    "cancel": 9,
    "switch": 10,
    "select_position": 11,
    "announce_number": 12,
    "select_place": 13,
    "announce_attrib": 14,
    "announce_race": 15,
    "select_option": 16,
    "select_card": 17,
    "select_chain": 18,
    "select_yesno": 19,
    "select_effect": 20,
    "respond": 21,
    "pass": 22,
}

ACTION_ZH = {
    "set": "盖放",
    "summon": "通常召唤·攻击",
    "summon_def": "通常召唤·守备",
    "special_summon": "特殊召唤",
    "reposition": "变更表示形式",
    "attack": "攻击",
    "direct_attack": "直接攻击",
    "activate": "发动",
    "cancel": "取消",
    "switch": "切换阶段",
    "select_position": "选择表示形式",
    "select_place": "选择位置",
    "select_card": "选择卡片",
    "select_chain": "连锁",
    "select_yesno": "是否",
    "select_option": "选择效果",
    "select_effect": "选择效果",
    "announce_number": "宣言数字",
    "announce_attrib": "宣言属性",
    "announce_race": "宣言种族",
    "respond": "响应",
    "pass": "跳过",
    "wait": "等待",
}

PHASE_ZH = {
    "draw": "抽卡阶段",
    "standby": "准备阶段",
    "main1": "主要阶段1",
    "battle": "战斗阶段",
    "main2": "主要阶段2",
    "end": "结束阶段",
}

POSITION_ZH = {
    "attack": "表侧攻击",
    "defense": "表侧守备",
    "face-up-atk": "表侧攻击",
    "face-up-def": "表侧守备",
    "face-down": "里侧",
    "face-down-atk": "里侧攻击",
    "face-down-def": "里侧守备",
    "face-up": "表侧",
}


def game_of(snapshot: Optional[dict]) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    g = snapshot.get("game")
    return g if isinstance(g, dict) else {}


def legal_of(snapshot_or_game: Optional[dict]) -> list[dict]:
    """Legal option dicts from a snapshot or its ``game`` block."""
    g = snapshot_or_game or {}
    if "game" in g and isinstance(g.get("game"), dict):
        g = g["game"]
    legal = g.get("legal_actions") or g.get("options") or []
    return [a for a in legal if isinstance(a, dict)]


def option_id(action: dict, index: int = 0) -> str:
    """Stable id for one legal option.

    Bridges that already assign ids (``id`` / ``option`` / ``response``) win;
    otherwise we use the position in the legal list. Ids are only meaningful
    against a fresh state — callers always re-resolve before acting.
    """
    for key in ("option", "id", "response"):
        v = action.get(key)
        if v is not None and v != "":
            return str(v)
    return str(index)


def options_map(snapshot_or_game: Optional[dict]) -> dict[str, dict]:
    """option id -> legal action. Duplicate ids get a deterministic suffix so
    no option can ever silently overwrite another."""
    out: dict[str, dict] = {}
    for i, action in enumerate(legal_of(snapshot_or_game)):
        oid = option_id(action, i)
        if oid in out:
            oid = f"{oid}#{i}"
        out[oid] = action
    return out


def resolve_option(snapshot_or_game: Optional[dict], option: Any) -> Optional[dict]:
    """Find the legal action for an LLM/backend supplied option reference.

    Accepts ``"3"``, ``"#3"``, bridge ids, and the legacy ``action:card``
    shorthand (``"summon:Mystic Tomato"``). Returns None when the option is
    no longer legal — the caller must then re-read the state instead of
    firing a stale move.
    """
    opts = options_map(snapshot_or_game)
    if not opts:
        return None
    text = str(option if option is not None else "").strip()
    if not text:
        return None
    if text.startswith("#"):
        text = text[1:]
    if text in opts:
        return opts[text]
    # numeric index into the *current* legal list
    legal = legal_of(snapshot_or_game)
    if text.isdigit():
        idx = int(text)
        if 0 <= idx < len(legal):
            return legal[idx]
    lowered = text.lower()
    for oid, action in opts.items():
        if oid.lower() == lowered:
            return action
    # legacy shorthand: action[:card] — first exact card match, else first
    # action match. Only a fallback; id addressing is the precise path.
    if ":" in text:
        name, arg = text.split(":", 1)
        name, arg = name.strip().lower(), arg.strip()
        for action in legal:
            if str(action.get("action", action.get("type", ""))).lower() != name:
                continue
            if arg and str(action.get("card", "")) == arg:
                return action
            if arg and arg in (action.get("phase"), action.get("position"), action.get("target")):
                return action
        for action in legal:
            if str(action.get("action", action.get("type", ""))).lower() == name:
                return action
        return None
    # bare action name: only safe when exactly one option carries it
    matches = [a for a in legal if str(a.get("action", a.get("type", ""))).lower() == lowered]
    if len(matches) == 1:
        return matches[0]
    if lowered in ("yes", "no"):
        for action in legal:
            if str(action.get("answer", "")).lower() == lowered:
                return action
    return None


def match_action(snapshot_or_game: Optional[dict], planned: dict) -> Optional[dict]:
    """Find a currently-legal option matching a planned action's semantics.

    Used for pre-planned chains: list indices shift as the duel progresses,
    so we match on the action's meaning (action/card/target/phase/position/
    answer/value) instead of its position. No match -> caller re-decides.
    """
    if not isinstance(planned, dict):
        return None
    keys = ("action", "type", "card", "target", "phase", "position", "answer", "value")
    wanted = {k: planned.get(k) for k in keys if planned.get(k) not in (None, "")}
    if not wanted:
        return None
    name = wanted.get("action") or wanted.get("type")
    for action in legal_of(snapshot_or_game):
        if name and str(action.get("action", action.get("type", ""))) != str(name):
            continue
        ok = True
        for key, value in wanted.items():
            if key in ("action", "type"):
                continue
            if action.get(key) != value:
                ok = False
                break
        if ok:
            return action
    return None


def act_payload(action: dict, option: Any = "") -> dict:
    """Wire payload for one option.

    Carries three equivalent addressing forms so any bridge can consume it:
    ``option`` (id / index), the semantic fields (``action``/``card``/…), and
    the numeric ygopro-core response ``code`` when we know it.
    """
    payload = {k: v for k, v in action.items() if v is not None and k not in ("desc", "effect_text")}
    name = str(action.get("action", action.get("type", ""))).lower()
    payload["action"] = name or action.get("action")
    if option != "" and option is not None:
        payload["option"] = str(option).lstrip("#")
    if name in ACTION_CODES and "code" not in payload:
        payload["code"] = ACTION_CODES[name]
    return payload


# ---- compact text formatting -------------------------------------------


def card_name(card: Any) -> str:
    if isinstance(card, dict):
        return str(card.get("name", card.get("code", "?")))
    return str(card)


def card_effect(card: Any, limit: int = 70) -> str:
    if isinstance(card, dict):
        eff = card.get("effect_text") or card.get("effect") or ""
        eff = " ".join(str(eff).split())
        return eff[:limit]
    return ""


def card_brief(card: Any, with_effect: bool = False) -> str:
    """``神秘番茄 L4 ATK1400/DEF1100 表侧攻击`` (effect optional)."""
    if not isinstance(card, dict):
        return str(card)
    name = card_name(card)
    bits = [name]
    if card.get("level"):
        bits.append(f"L{card['level']}")
    if card.get("link_rating"):
        bits.append(f"LINK{card['link_rating']}")
    atk, dfn = card.get("atk"), card.get("def")
    if atk is not None and (atk or dfn):
        bits.append(f"{atk}/{dfn if dfn is not None else '?'}")
    ctype = card.get("type")
    if ctype and ctype not in ("monster",):
        bits.append({"spell": "魔法", "trap": "陷阱"}.get(str(ctype), str(ctype)))
    pos = card.get("position")
    if pos:
        bits.append(POSITION_ZH.get(str(pos), str(pos)))
    if card.get("face_down") or card.get("hidden"):
        return "里侧卡(未知)"
    if with_effect:
        eff = card_effect(card)
        if eff:
            bits.append(f"「{eff}」")
    return " ".join(bits)


def describe_option(oid: str, action: dict, with_effect: bool = True) -> str:
    """One line the LLM can both read and call back: ``#3 通常召唤·攻击 神秘番茄 …``"""
    name = str(action.get("action", action.get("type", "?")))
    zh = ACTION_ZH.get(name, name)
    parts = [f"#{oid} {zh}"]
    if action.get("card") is not None and not isinstance(action.get("card"), (list, dict)):
        card = str(action.get("card"))
        stats = ""
        if action.get("atk") is not None:
            stats = f" {action.get('atk')}/{action.get('def', '?')}"
        parts.append(f"{card}{stats}")
    elif isinstance(action.get("card"), dict):
        parts.append(card_brief(action["card"]))
    elif isinstance(action.get("cards"), list):
        parts.append("/".join(card_name(c) for c in action["cards"][:6]))
    for key in ("position", "phase", "target"):
        v = action.get(key)
        if v not in (None, ""):
            if key == "position":
                parts.append(POSITION_ZH.get(str(v), str(v)))
            elif key == "phase":
                parts.append(f"→{PHASE_ZH.get(str(v), str(v))}")
            else:
                parts.append(f"目标:{v}")
    if action.get("answer") not in (None, ""):
        parts.append(str(action["answer"]))
    if action.get("value") not in (None, ""):
        parts.append(str(action["value"]))
    if action.get("index") not in (None, ""):
        parts.append(f"槽{action['index']}")
    if action.get("desc"):
        parts.append(str(action["desc"])[:60])
    if with_effect:
        eff = card_effect(action) or (card_effect(action.get("card")) if isinstance(action.get("card"), dict) else "")
        if eff:
            parts.append(f"「{eff}」")
    return " ".join(str(p) for p in parts if p)


def format_zone(title: str, cards: Any, limit: int = 12, with_effect: bool = False) -> str:
    if not cards:
        return f"{title}: 无"
    items = list(cards)[:limit]
    body = "；".join(card_brief(c, with_effect=with_effect) for c in items)
    more = f" …共{len(cards)}" if len(cards) > limit else ""
    return f"{title}({len(cards)}): {body}{more}"


def format_duel_state(
    snapshot: Optional[dict],
    stage: str = "",
    goal: str = "",
    max_options: int = 30,
    max_log: int = 6,
    with_effects: bool = True,
) -> str:
    """The complete-yet-compact duel briefing used by tools and prompts."""
    g = game_of(snapshot)
    if not g and not stage:
        return "暂无对局状态（桥未连接或未进房）。"
    lines: list[str] = []
    room = (snapshot or {}).get("room") if isinstance(snapshot, dict) else None
    if isinstance(room, dict) and room:
        bits = []
        if room.get("name") or room.get("id"):
            bits.append(f"房间={room.get('name') or room.get('id')}")
        if room.get("server"):
            bits.append(f"服务器={room['server']}")
        players = room.get("players")
        if players:
            bits.append("玩家=" + ",".join(str(p) for p in players[:6]))
        if room.get("is_host") is not None:
            bits.append("我方=" + ("房主" if room.get("is_host") else "参与者"))
        if room.get("ready") is not None:
            bits.append("准备=" + str(room.get("ready")))
        if bits:
            lines.append(" ".join(bits))
    turn = g.get("turn", "?")
    phase = g.get("phase", "?")
    mine = g.get("is_my_turn", True)
    lines.append(
        f"阶段={stage or (snapshot or {}).get('stage', '?') or '?'} | "
        f"LP 我{g.get('my_lp', '?')}/敌{g.get('op_lp', '?')} | "
        f"回合{turn} {PHASE_ZH.get(str(phase), phase)} | "
        f"{'我方行动' if mine else '对手行动'}"
    )
    if goal:
        lines.append(f"目标: {goal}")
    if g.get("opponent"):
        lines.append(f"对手: {g['opponent']} 手牌{g.get('opponent_hand_count', '?')}张")
    if g.get("hand") is not None:
        lines.append(format_zone("我方手牌", g.get("hand"), limit=10, with_effect=with_effects))
    for key, title in (("field_my", "我方场上"), ("field_op", "对方场上")):
        if g.get(key) is not None:
            lines.append(format_zone(title, g.get(key), limit=10, with_effect=with_effects and key == "field_my"))
    counts = []
    for key, title in (
        ("grave_my", "我墓"), ("grave_op", "敌墓"),
        ("banished_my", "我除"), ("banished_op", "敌除"),
        ("deck_count", "我卡组"), ("op_deck_count", "敌卡组"),
        ("extra_deck", "额外"),
    ):
        v = g.get(key)
        if isinstance(v, list):
            counts.append(f"{title}{len(v)}")
        elif isinstance(v, int):
            counts.append(f"{title}{v}")
    if counts:
        lines.append("资源: " + " ".join(counts))
    decision = g.get("decision")
    if isinstance(decision, dict) and decision.get("kind"):
        hint = decision.get("hint") or ""
        lines.append(f"待你决策[{decision['kind']}]: {hint}")
    multi = g.get("multi_select")
    if isinstance(multi, dict):
        text = f"多选 {multi.get('min', 0)}-{multi.get('max', 1)} 张"
        if multi.get("sum"):
            text += f"（数值合计需={multi['sum']}）"
        text += "：op=act option=编号1,编号2 一次提交"
        if multi.get("cancelable"):
            text += "，或选 cancel 取消"
        lines.append(text)
    opts = options_map(g)
    if opts:
        lines.append(f"合法选项({len(opts)})——用 op=act option=编号 执行:")
        for i, (oid, action) in enumerate(opts.items()):
            if i >= max_options:
                lines.append(f"…其余 {len(opts) - max_options} 项省略")
                break
            lines.append(describe_option(oid, action, with_effect=with_effects))
    else:
        if not g:
            lines.append("尚未进入对局：先完成 进房→双方 ready→房主 start。")
        elif g.get("is_duel_active", True):
            lines.append("当前无需我方操作（等待对手/引擎推进）。")
    if g.get("is_duel_active") is False:
        winner = g.get("winner") or g.get("result") or ""
        lines.append(f"对局已结束{(' 胜者:' + str(winner)) if winner else ''}")
    log = g.get("public_log") or g.get("log") or (snapshot or {}).get("log") if isinstance(snapshot, dict) else None
    if isinstance(log, list) and log:
        lines.append("最近公开事件: " + " | ".join(str(x)[:70] for x in log[-max_log:]))
    return "\n".join(lines)


def format_decks(payload: Any) -> str:
    """Compact deck list for the LLM."""
    decks = payload.get("decks") if isinstance(payload, dict) else payload
    if not isinstance(decks, list) or not decks:
        return "桥未返回可用卡组。可用 op=build_deck 上传 .ydk 组卡。"
    lines = []
    for d in decks[:40]:
        if isinstance(d, dict):
            name = d.get("name", "?")
            counts = ""
            parts = []
            for key in ("main", "extra", "side"):
                v = d.get(key)
                if isinstance(v, list):
                    parts.append(f"{key}{len(v)}")
                elif isinstance(v, int):
                    parts.append(f"{key}{v}")
            if parts:
                counts = " (" + "/".join(parts) + ")"
            lines.append(f"- {name}{counts}")
        else:
            lines.append(f"- {d}")
    more = f"\n…共 {len(decks)} 套" if len(decks) > 40 else ""
    return "可用卡组:\n" + "\n".join(lines) + more


# ---- .ydk deck text -----------------------------------------------------


def parse_ydk(text: str) -> dict[str, list[str]]:
    """Parse standard YGOPro ``.ydk`` text into main/extra/side card id lists.

    Real format: ``#created by ...`` header, ``#main`` + numeric card ids,
    ``#extra`` + ids, ``!side`` + ids. Unknown lines are ignored; card ids
    must be numeric (ygopro card passwords).
    """
    zones: dict[str, list[str]] = {"main": [], "extra": [], "side": []}
    zone = ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("#main"):
            zone = "main"
            continue
        if low.startswith("#extra"):
            zone = "extra"
            continue
        if low.startswith("!side"):
            zone = "side"
            continue
        if line.startswith("#") or line.startswith("!"):
            continue
        if not zone:
            zone = "main"
        if line.isdigit():
            zones[zone].append(line)
    return zones


def validate_deck(zones: dict[str, list[str]]) -> str:
    """Return an error string, or '' when the deck is legal to submit."""
    main = zones.get("main", [])
    extra = zones.get("extra", [])
    side = zones.get("side", [])
    if not main:
        return "主卡组为空：需要 main 卡密列表或 .ydk 文本"
    if not 40 <= len(main) <= 60:
        return f"主卡组需 40-60 张，当前 {len(main)} 张"
    if len(extra) > 15:
        return f"额外卡组最多 15 张，当前 {len(extra)}"
    if len(side) > 15:
        return f"副卡组最多 15 张，当前 {len(side)}"
    counts: dict[str, int] = {}
    for card in main + extra + side:
        counts[card] = counts.get(card, 0) + 1
    over = [c for c, n in counts.items() if n > 3]
    if over:
        return f"同名卡超过 3 张: {', '.join(over[:5])}（禁限卡表由引擎最终校验）"
    return ""


def deck_summary(zones: dict[str, list[str]]) -> str:
    return f"主{len(zones.get('main', []))}/额外{len(zones.get('extra', []))}/副{len(zones.get('side', []))}"
