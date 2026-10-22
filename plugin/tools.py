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
    "ygo_engine": ("game_ygo_join", "game_ygo_ready", "game_ygo_set_deck", "game_ygo_duel_info"),
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
                out.append(cls(plugin=plugin))
            except Exception:  # noqa: BLE001
                continue
    return out


# ---- YGO tools -------------------------------------------------------


@register_game_tool
class GameYgoJoin(BaseTool):
    name = "game_ygo_join"
    description = "游戏王：加入对局房间/服务器。用户给房间号或房间链接时调用。返回连接结果与对手信息。"
    parameters = {
        "type": "object",
        "properties": {
            "room": {"type": "string", "description": "房间号或房间链接（如 1999 或 http://host:port/room/1999）"},
            "deck": {"type": "string", "description": "可选：要用的卡组名（不传用默认）"},
        },
        "required": ["room"],
    }

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._ygo_join(event, kwargs.get("room", ""), kwargs.get("deck", ""))


@register_game_tool
class GameYgoReady(BaseTool):
    name = "game_ygo_ready"
    description = "游戏王：确认准备（确认对手 ID 后按下准备）。调用后对手可见你已准备。"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._ygo_ready(event)


@register_game_tool
class GameYgoSetDeck(BaseTool):
    name = "game_ygo_set_deck"
    description = "游戏王：选择卡组（.ydk 文件名，位于卡组目录）。"
    parameters = {
        "type": "object",
        "properties": {"deck": {"type": "string", "description": "卡组文件名，如 dragon.ydk"}},
        "required": ["deck"],
    }

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._ygo_set_deck(event, kwargs.get("deck", ""))


@register_game_tool
class GameYgoDuelInfo(BaseTool):
    name = "game_ygo_duel_info"
    description = "游戏王：查看当前对局详细信息（双方 LP/手牌数/场况/阶段/合法动作）。用户问对局细节时调用。"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, event, **kwargs) -> str:
        return self._plugin._ygo_duel_info(event)


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
