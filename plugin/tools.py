"""Per-game special tools (BaseTool instances), injected on demand.

The plugin's @on.llm_request hook injects these into req.tool_set only for
games the user enabled in the WebUI (enabled_games). Unused game tools never
clutter the LLM's tool table.

Each tool is a BaseTool subclass (KiraAI v2.33.1 spec).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from core.agent.tool import ToolResult
from core.utils.tool_utils import BaseTool

# Games that have special tools beyond the generic surface.
GAME_TOOL_TAGS = {
    # Two merged YGO tools cover the whole loop (deck/room/duel). Fewer tool
    # schemas = fewer prompt tokens per turn; legacy tools stay registered
    # below for backwards compatibility but are not injected by default.
    "ygo_engine": ("game_ygo", "game_ygo_duel"),
    "pokemon_pyboy": ("game_pkm_use_item", "game_pkm_team"),
    "mgba_http": ("game_pkm_use_item", "game_pkm_team"),
    "terrablind_http": ("game_tb_start", "game_tb_stop"),
    "generic_http": (),
}

ALL_GAME_TOOLS: dict[str, type["BaseTool"]] = {}


def register_game_tool(cls: type["BaseTool"]) -> type["BaseTool"]:
    ALL_GAME_TOOLS[cls.name] = cls
    return cls


def tool_names_for(game: str) -> tuple[str, ...]:
    return GAME_TOOL_TAGS.get(game, ())


def build_tools_for(game: str, plugin) -> list[BaseTool]:
    """Instantiate the tool objects for one enabled game."""
    out: list[BaseTool] = []
    for name in tool_names_for(game):
        cls = ALL_GAME_TOOLS.get(name)
        if cls is not None:
            try:
                tool = cls(plugin=plugin)
                # BaseTool stores kwargs but has no `_plugin` attribute; set it
                # explicitly so every game tool can reach the plugin instance.
                tool._plugin = plugin
                out.append(tool)
            except Exception:  # noqa: BLE001
                continue
    return out


# ---- YGO tools (merged, network-play first) ----------------------------


@register_game_tool
class GameYgo(BaseTool):
    name = "game_ygo"
    description = (
        "游戏王（可与真实玩家网络对战，无需 Jev/EDOPro）：组卡/选卡/建房/进房/准备/开始/退房。"
        "若桥未连接，先 op=install_bridge 自动下载安装并启动真实引擎桥（完成后会通知你），"
        "op=bridge_status 查进度。完整流程 op=decks 看卡组（或 build_deck 组卡）→ "
        "host 建房（server=公共服:7911）或 join 进房 → ready 准备 → 房主 start 开始。"
        "对局内操作用 game_ygo_duel。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": ["install_bridge", "bridge_status", "decks", "build_deck", "rooms",
                         "host", "join", "set_deck", "ready", "start", "leave", "surrender", "chat"],
                "description": "decks 卡组列表 / build_deck 组卡上传 / rooms 房间列表 / host 建房 / "
                               "join 进房 / set_deck 选卡组 / ready 准备 / start 房主开始 / "
                               "leave 退房 / surrender 投降 / chat 房间聊天",
            },
            "room": {"type": "string", "description": "host/join 的房间名或房号；host 留空自动命名"},
            "deck": {"type": "string", "description": "卡组 .ydk 文件名（host/join/set_deck 用）"},
            "name": {"type": "string", "description": "build_deck 的卡组名，如 blackwing.ydk"},
            "server": {"type": "string", "description": "YGOPro 服务器地址 host:port（联机打真人时填，覆盖配置）"},
            "password": {"type": "string", "description": "房间密码（有密码的房间）"},
            "mode": {"type": "string", "enum": ["network", "single"], "description": "network 联机（默认）/ single 单机 AI"},
            "main": {"type": "string", "description": "build_deck：主卡组卡密，逗号分隔，40-60 张"},
            "extra": {"type": "string", "description": "build_deck：额外卡组卡密，逗号分隔，≤15"},
            "side": {"type": "string", "description": "build_deck：副卡组卡密，逗号分隔，≤15"},
            "ydk": {"type": "string", "description": "build_deck：直接给完整 .ydk 文本（#main/#extra/!side），优先于 main 等"},
            "message": {"type": "string", "description": "op=chat 时发给房间的文字"},
        },
        "required": ["op"],
    }

    async def execute(self, event, **kwargs) -> str:
        from . import ygo_agent
        kw = dict(kwargs)
        op = kw.pop("op", "")
        return await ygo_agent.lifecycle(self._plugin, event, op, **kw)


@register_game_tool
class GameYgoDuel(BaseTool):
    name = "game_ygo_duel"
    description = (
        "游戏王对局信息与操作（主 LLM 直接打，无需 Jev）。op=state 看双方 LP/手牌/场面/合法选项；"
        "op=act option=编号 执行一个选项；op=chain options=编号1,编号2 连续执行多步（每步都按最新局面校验，"
        "失效即停并返回现场）；op=log 看公开对局日志。对手回合出现响应窗口时同样用 act 选择连锁或跳过。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["state", "act", "chain", "log"], "description": "默认 state"},
            "option": {"type": "string", "description": "op=act：state 里给出的合法选项编号，如 0 或 3"},
            "options": {"type": "string", "description": "op=chain：逗号分隔的选项编号序列，按序执行"},
            "refresh": {"type": "boolean", "description": "op=state：默认 true 实时拉取；false 用近 5 秒缓存（更快但可能略旧）"},
            "full": {"type": "boolean", "description": "op=state：true 附全部选项与卡片效果（默认 false，紧凑省 token）"},
            "n": {"type": "integer", "description": "op=log：条数，默认 12"},
            "action": {"type": "string", "description": "兼容用法：动作名（建议改用 option 编号，精确到卡）"},
            "card": {"type": "string", "description": "兼容用法：配合 action 指定卡名"},
            "phase": {"type": "string", "description": "兼容用法：switch 目标阶段"},
            "target": {"type": "string", "description": "兼容用法：攻击/效果目标"},
            "position": {"type": "string", "description": "兼容用法：表示形式"},
        },
        "required": [],
    }

    async def execute(self, event, **kwargs) -> str:
        from . import ygo_agent
        kw = dict(kwargs)
        op = kw.pop("op", "state")
        return await ygo_agent.duel(self._plugin, event, op, **kw)


# ---- YGO legacy tools (kept for compatibility; not injected by default) --


@register_game_tool
class GameYgoJoin(BaseTool):
    name = "game_ygo_join"
    description = "游戏王：加入对局房间（兼容旧工具，建议用 game_ygo op=join）。"
    parameters = {
        "type": "object",
        "properties": {
            "room": {"type": "string", "description": "房间号"},
            "deck": {"type": "string", "description": "可选：卡组名"},
        },
        "required": ["room"],
    }

    async def execute(self, event, **kwargs) -> str:
        from . import ygo_agent
        return await ygo_agent.lifecycle(self._plugin, event, "join",
                                         room=kwargs.get("room", ""), deck=kwargs.get("deck", ""))


@register_game_tool
class GameYgoReady(BaseTool):
    name = "game_ygo_ready"
    description = "游戏王：确认准备（兼容旧工具，建议用 game_ygo op=ready）。"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, event, **kwargs) -> str:
        from . import ygo_agent
        return await ygo_agent.lifecycle(self._plugin, event, "ready")


@register_game_tool
class GameYgoSetDeck(BaseTool):
    name = "game_ygo_set_deck"
    description = "游戏王：选择卡组（兼容旧工具，建议用 game_ygo op=set_deck）。"
    parameters = {
        "type": "object",
        "properties": {"deck": {"type": "string", "description": "卡组文件名，如 dragon.ydk"}},
        "required": ["deck"],
    }

    async def execute(self, event, **kwargs) -> str:
        from . import ygo_agent
        return await ygo_agent.lifecycle(self._plugin, event, "set_deck", deck=kwargs.get("deck", ""))


@register_game_tool
class GameYgoDuelInfo(BaseTool):
    name = "game_ygo_duel_info"
    description = "游戏王：对局详情（兼容旧工具，建议用 game_ygo_duel op=state）。"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, event, **kwargs) -> str:
        from . import ygo_agent
        return await ygo_agent.duel(self._plugin, event, "state")


# ---- Pokemon tools ---------------------------------------------------


@register_game_tool
class GamePkmUseItem(BaseTool):
    name = "game_pkm_use_item"
    description = "宝可梦：在菜单中使用一个道具（战斗外/战斗内均可）。"
    parameters = {
        "type": "object",
        "properties": {"item": {"type": "string", "description": "道具名，如 Potion"}},
        "required": ["item"],
    }

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._pkm_use_item(event, kwargs.get("item", ""))


@register_game_tool
class GamePkmTeam(BaseTool):
    name = "game_pkm_team"
    description = "宝可梦：查看当前队伍（宝可梦/等级/HP）。"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._pkm_team(event)


# ---- TerraBlind tools -------------------------------------------------


@register_game_tool
class GameTbStart(BaseTool):
    name = "game_tb_start"
    description = "泰拉瑞亚：触发 TerraBlind 主线流程（/start_run）。"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._tb_start(event)


@register_game_tool
class GameTbStop(BaseTool):
    name = "game_tb_stop"
    description = "泰拉瑞亚：停止 TerraBlind 主线流程。"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._tb_stop(event)
