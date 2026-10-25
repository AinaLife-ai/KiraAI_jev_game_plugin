"""Load the plugin in the REAL KiraAI framework and verify the v3.1.0 contract.

Requires a KiraAI source checkout (tested against v3.1.0):
    KIRAAI_ROOT=/path/to/KiraAI python3 tests/kira_real_load_test.py

Checks: plugin loads from a directory, tools/hooks register, per-game YGO
tools build against the real BaseTool, llm_request injection uses the real
ToolSet + LLMRequest, merged granularity, prompt cache safety (no shared
chat_env mutation), and hot config reload of the YGO network section.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
KIRAAI_ROOT = os.environ.get("KIRAAI_ROOT", "")
if not KIRAAI_ROOT or not Path(KIRAAI_ROOT, "core", "plugin", "manager.py").exists():
    print("SKIP: set KIRAAI_ROOT to a KiraAI source checkout")
    sys.exit(0)

sys.path.insert(0, KIRAAI_ROOT)

from core.plugin.manager import PluginManager  # noqa: E402
from core.provider.llm_model import LLMRequest  # noqa: E402
from core.agent.tool import ToolSet  # noqa: E402
from core.prompt_manager import Prompt  # noqa: E402

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


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "KiraAI_jev_game_plugin"
        shutil.copytree(ROOT / "plugin", dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        class FakeToolMgr:
            def __init__(self):
                self.registered = {}

            def register_tool(self, name, description="", parameters=None, func=None, **kw):
                self.registered[name] = {"description": description, "parameters": parameters, "func": func}

            def unregister_tool(self, name):
                self.registered.pop(name, None)

        ctx = SimpleNamespace(tool_mgr=FakeToolMgr(), config={})
        mgr = PluginManager(ctx)
        plugin_id = await mgr.load_plugin_from_dir(dest, auto_install=False)
        check("plugin loads in real framework", plugin_id == "KiraAI_jev_game_plugin", str(plugin_id))
        if not plugin_id:
            print(f"\n== {PASS} passed, {FAIL} failed ==")
            return 1
        inst = mgr.get_plugin_inst(plugin_id)
        check("plugin instance available", inst is not None)
        if inst is None:
            return 1
        check("manifest id matches directory", plugin_id == dest.name)

        # Tools registered through the real decorator/registry path.
        from core.plugin import registry
        components = registry._plugin_components.get(plugin_id)
        check("components registered", components is not None)
        if components is not None:
            tool_names = list(getattr(components, "tools", {}) or {})
            check("generic tools registered", "game_play" in tool_names and "game_control" in tool_names,
                  str(tool_names)[:200])
            check("merged game_chain registered", "game_chain" in tool_names)
        check("tools bound into real tool manager", "game_play" in ctx.tool_mgr.registered,
              str(list(ctx.tool_mgr.registered))[:200])

        # Real BaseTool: per-game tools must reach the plugin instance.
        import importlib
        # Use the very module instance the framework imported.
        tools_mod = importlib.import_module("plugins.KiraAI_jev_game_plugin.tools")
        tools = {t.name: t for t in tools_mod.build_tools_for("ygo_engine", inst)}
        check("two merged ygo tools", set(tools) == {"game_ygo", "game_ygo_duel"}, str(set(tools)))
        check("tools bound to plugin instance", all(getattr(t, "_plugin", None) is inst for t in tools.values()))
        check("schemas valid for function calling", all(
            t.get_schema()["name"] and t.get_schema()["parameters"]["type"] == "object"
            for t in tools.values()))

        # Real LLMRequest + ToolSet injection.
        inst.bridge_cfg["enabled_games"] = ["ygo_engine"]
        req = LLMRequest(system_prompt=[Prompt("ENV", name="chat_env", source="system")],
                         tool_set=ToolSet())
        event = SimpleNamespace(sid="qq:gm:1")
        shared = req.system_prompt[0]
        await inst.hitchhike(event, req, None)
        names = [t.name for t in req.tool_set.tools]
        check("ygo tools injected via llm_request", "game_ygo" in names and "game_ygo_duel" in names, str(names))
        check("merged granularity drops fine tools", "game_jev_ask" not in names and "game_logs" not in names)
        check("shared chat_env untouched", shared.content == "ENV", shared.content[:60])
        check("persisted prompt not polluted", all(p.content == "ENV" for p in req.system_prompt if p is shared))

        # Hot reload of the YGO network section.
        inst.apply_cfg({**inst._cfg, "section_ygo": {"ygo_mode": "network", "ygo_port": 5299,
                                                     "ygo_server": "127.0.0.1:7911", "ygo_auto": True}})
        check("hot ygo config applied", inst.ygo_cfg["ygo_port"] == 5299 and inst.ygo_cfg["ygo_auto"] is True)
        eng = inst._get_or_create_engine("qq:gm:1", "ygo_engine")
        check("engine uses ygo network config", eng is not None and eng.adapter.port == 5299,
              str(getattr(getattr(eng, "adapter", None), "port", None)))
        check("engine honours auto flag", eng is not None and eng.auto_decision is True)
        check("adapter network mode", eng.adapter.game_mode == "network")
        check("adapter server address", eng.adapter.server_url == "127.0.0.1:7911")
        check("no key -> manual despite auto flag", eng._decision_enabled() is False)

        print(f"\n== {PASS} passed, {FAIL} failed ==")
        return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
