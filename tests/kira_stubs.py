"""Minimal KiraAI core stubs so tests can load the real plugin (main.py,
tools.py) without a running framework. Only the surface the plugin uses is
provided, mirroring KiraAI v3.1.0 semantics:

- BaseTool stores kwargs and has no `_plugin` (the plugin sets it itself)
- Prompt(content, name, source, persist)
- register.tool/page/api + on.* decorators are pass-through markers
"""
from __future__ import annotations

import logging
import sys
import types


def install() -> None:
    if "core.plugin" in sys.modules:
        return

    core = types.ModuleType("core")
    core.__path__ = []
    plugin = types.ModuleType("core.plugin")
    chat = types.ModuleType("core.chat")
    chat.__path__ = []
    message_utils = types.ModuleType("core.chat.message_utils")
    message_elements = types.ModuleType("core.chat.message_elements")
    prompt_manager = types.ModuleType("core.prompt_manager")
    agent = types.ModuleType("core.agent")
    agent.__path__ = []
    agent_tool = types.ModuleType("core.agent.tool")
    utils = types.ModuleType("core.utils")
    utils.__path__ = []
    tool_utils = types.ModuleType("core.utils.tool_utils")

    class BasePlugin:
        def __init__(self, ctx, cfg):
            self.ctx = ctx
            self.cfg = cfg

    class Priority:
        HIGH = 100
        MEDIUM = 0
        LOW = -100

    def _deco(*args, **kwargs):
        def wrap(fn):
            return fn
        return wrap

    class register:
        tool = staticmethod(_deco)
        page = staticmethod(_deco)
        api = staticmethod(_deco)

    class on:
        im_message = staticmethod(_deco)
        llm_request = staticmethod(_deco)
        custom_event = staticmethod(_deco)

    class PluginPage:
        @classmethod
        def from_folder(cls, folder):
            return {"folder": folder}

    class PageMenu:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class BaseTool:
        def __init__(self, *args, ctx=None, **kwargs):
            self.ctx = ctx
            self.args = args
            self.kwargs = kwargs

        @classmethod
        def get_schema(cls):
            return {"name": cls.name, "description": cls.description, "parameters": cls.parameters}

    class ToolResult:
        def __init__(self, content=""):
            self.content = content

    class Prompt:
        def __init__(self, content="", name="", source="", persist=True, **kwargs):
            self.content = content
            self.name = name
            self.source = source
            self.persist = persist

        def to_string(self):
            return str(self.content)

    class Text:
        def __init__(self, text=""):
            self.text = text

    class MessageChain:
        def __init__(self, elements=None):
            self.elements = elements or []

    class KiraMessageBatchEvent:
        pass

    class KiraCustomEvent:
        pass

    plugin.BasePlugin = BasePlugin
    plugin.Priority = Priority
    plugin.register = register
    plugin.on = on
    plugin.PluginPage = PluginPage
    plugin.PageMenu = PageMenu
    plugin.logger = logging.getLogger("kira_test")
    chat.MessageChain = MessageChain
    chat.message_utils = message_utils
    chat.message_elements = message_elements
    message_utils.KiraMessageBatchEvent = KiraMessageBatchEvent
    message_utils.KiraCustomEvent = KiraCustomEvent
    message_elements.Text = Text
    prompt_manager.Prompt = Prompt
    agent_tool.ToolResult = ToolResult
    tool_utils.BaseTool = BaseTool
    agent.tool = agent_tool
    utils.tool_utils = tool_utils

    for name, module in {
        "core": core,
        "core.plugin": plugin,
        "core.chat": chat,
        "core.chat.message_utils": message_utils,
        "core.chat.message_elements": message_elements,
        "core.prompt_manager": prompt_manager,
        "core.agent": agent,
        "core.agent.tool": agent_tool,
        "core.utils": utils,
        "core.utils.tool_utils": tool_utils,
    }.items():
        sys.modules[name] = module
