"""KiraAI plugin entry: KiraAI_jev_game_plugin (multi-engine).

- Multi-engine: each (session, game) can run its own Jev decision loop in
  parallel; one session may run several games at once.
- Session-aware notifications: the session that issued the command gets
  the broadcasts by default; extra sids are optional additions.
- Never blocks: notify router is a bounded async queue with per-sid rate
  limit and per-sid send timeout.
- Tools: game_play / game_state / game_command / game_set_goal /
         game_jev_ask / game_logs / game_diag / game_pause / game_resume /
         game_stop (+ merged game_control / game_inspect / game_direct /
         game_chain)
- YGO (network duels vs real players): two merged tools game_ygo +
  game_ygo_duel cover decks/build_deck/host/join/ready/start and
  state/act/chain/log. The main LLM can play a whole duel with NO Jev/laya
  backend; automatic decisions are an explicit opt-in (ygo_auto).
- Hooks/API/Page: WebUI sidebar config page (hot-reload settings).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from core.plugin import BasePlugin, logger, on, register, Priority
from core.plugin import PluginPage, PageMenu
from core.chat.message_utils import KiraMessageBatchEvent, KiraCustomEvent
from core.chat.message_elements import Text
from core.chat import MessageChain
from core.prompt_manager import Prompt

from .bridge.registry import create_adapter
from .engine import DecisionEngine
from .escalate import Escalator
from .jev.client import JevClient, API_URL
from .jev.gate import DecisionGate
from .jev.laya_backend import LayaBackend
from .notify import NotifyRouter
from .session_mgr import SessionBindings
from .tools import build_tools_for
from .autoinstall import LayaAutoInstaller

log = logging.getLogger("kira_game_agent")


def _engine_id(sid: str, game: str) -> str:
    return f"{sid}::{game}"


# Stage names that warrant a main-LLM strategic consult (key decision nodes).
STRATEGY_STAGES = {
    "ygo_engine": {"dueling"},                 # duel start ...
    "pokemon_pyboy": {"battle"},               # every new battle
    "mgba_http": {"battle"},
    "terrablind_http": {"boss", "crafting"},
    "generic_http": {"running"},
}


class KiraGameAgentPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self._cfg = dict(cfg)
        self._read_cfg()
        # one backend (Jev or local Laya) + one gate shared by all engines
        self.client: Optional[Any] = None
        self.backend_name = "laya"
        self.gate: Optional[DecisionGate] = None
        # engine_id -> DecisionEngine
        self.engines: dict[str, DecisionEngine] = {}
        self.sessions = SessionBindings()
        self.notify: Optional[NotifyRouter] = None
        self.escalator: Optional[Escalator] = None
        self.autoinstall: Optional[LayaAutoInstaller] = None
        self._events: list[dict] = []

    # ---- config ------------------------------------------------------

    def _read_cfg(self) -> None:
        j = self._cfg.get("section_jev", {}) or {}
        b = self._cfg.get("section_bridge", {}) or {}
        p = self._cfg.get("section_playbook", {}) or {}
        n = self._cfg.get("section_notify", {}) or {}
        self.jev_cfg = {
            "backend": j.get("backend", "laya") or "laya",
            "kira_provider_id": j.get("kira_provider_id", "") or "",
            "api_key": j.get("api_key", "") or "",
            "model": j.get("model", "jev-latest") or "jev-latest",
            "base_url": j.get("base_url", "") or "",
            "decision_hz": float(j.get("decision_hz", 1.0) or 1.0),
            "request_timeout": float(j.get("request_timeout", 5.0) or 5.0),
            "intent_ttl": float(j.get("intent_ttl", 1.5) or 1.5),
            "low_risk_threshold": float(j.get("low_risk_threshold", 0.55) or 0.55),
            "high_risk_threshold": float(j.get("high_risk_threshold", 0.85) or 0.85),
            "use_baseline": bool(j.get("use_baseline", True)),
            "temperature": float(j.get("temperature", 2.0) or 2.0),
            "goal_floor": float(j.get("goal_floor", 0.1) or 0.1),
            "strategy_advisory": bool(j.get("strategy_advisory", True)),
            "consult_ask_jev": bool(j.get("consult_ask_jev", True)),
            "consult_threshold": float(j.get("consult_threshold", 0.6) or 0.6),
            "strategy_interval_rounds": int(j.get("strategy_interval_rounds", 8) or 8),
        }
        self.bridge_cfg = {
            "enabled_games": b.get("enabled_games", []) or [],
            "stop_keywords": b.get("stop_keywords", ["停止玩宝可梦"]) or ["停止玩宝可梦"],
            "stop_whitelist": b.get("stop_whitelist", []) or [],
            "tool_granularity": b.get("tool_granularity", "merged") or "merged",
            "game": b.get("game", "none") or "none",
            "host": b.get("host", "127.0.0.1") or "127.0.0.1",
            "port": int(b.get("port", 17878) or 17878),
            "poll_interval": float(b.get("poll_interval", 2.0) or 2.0),
            "auto_reconnect": bool(b.get("auto_reconnect", True)),
            "rom": b.get("rom", "") or "",
        }
        y = self._cfg.get("section_ygo", {}) or {}
        self.ygo_cfg = {
            # Network play against real people is the primary mode.
            "ygo_mode": y.get("ygo_mode", "network") or "network",
            "ygo_host": y.get("ygo_host", "127.0.0.1") or "127.0.0.1",
            "ygo_port": int(y.get("ygo_port", 5274) or 5274),
            "ygo_server": y.get("ygo_server", "") or "",
            "ygo_password": y.get("ygo_password", "") or "",
            "ygo_deck": y.get("ygo_deck", "") or "",
            "ygo_timeout": float(y.get("ygo_timeout", 5.0) or 5.0),
            # Off by default: the main LLM plays through tools, no Jev needed.
            "ygo_auto": bool(y.get("ygo_auto", False)),
        }
        self.playbook_cfg = {
            "game_instructions": p.get("game_instructions", "") or "",
            "per_game_playbook": p.get("per_game_playbook", {}) or {},
        }
        self.notify_cfg = {
            "enabled": bool(n.get("notify_enabled", True)),
            "extra_sids": n.get("notify_sids", []) or [],
            "min_interval": float(n.get("notify_min_interval", 30.0) or 30.0),
        }
        s = self._cfg.get("section_strategy", {}) or {}
        self.strategy_cfg = {
            "enabled_stages": s.get("enabled_stages", {}) or {},
            "ask_templates": s.get("ask_templates", "") or "",
        }
        # Decision backend: "laya" (local, preferred) or "jev" (cloud).
        # Default to laya for out-of-the-box / zero-cost UX.
        self.backend_name = self.jev_cfg.get("backend", "laya") or "laya"
        l = self._cfg.get("section_laya", {}) or {}
        self.laya_cfg = {
            "model": l.get("laya_model", l.get("model", "convaiinnovations/laya")) or "convaiinnovations/laya",
            "subfolder": l.get("laya_subfolder", l.get("subfolder", "multilingual")) or "multilingual",
            "device": l.get("laya_device", l.get("device", "")) or "",
            "hf_mirror": l.get("laya_hf_mirror", l.get("hf_mirror", "")) or "",
            "local_model_path": l.get("laya_local_model_path", l.get("local_model_path", "")) or "",
        }
        self.autoinstall_cfg = {
            "enabled": l.get("laya_autoinstall", l.get("autoinstall", False)),
        }

    def _make_backend(self):
        """Build the decision backend per config.

        Default (and recommended) backend is **laya** (local, zero-cost,
        multilingual). If the user explicitly picks `laya` but it's not yet
        installed/loadable during a first run, we fall back to Jev (if a key
        exists) or to a plain baseline so gameplay isn't blocked; once laya
        autoinstall finishes, the user can switch back (or we auto-promote).
        `jev` always uses the cloud client.
        `kira_provider` reuses a provider the user already registered in
        KiraAI's WebUI (e.g. a newapi relay like futureppo.top): we read its
        provider_config.base_url + api_key and build a systemone JevClient.
        """
        self.backend_name = self.jev_cfg.get("backend", "laya") or "laya"
        if self.backend_name == "laya":
            self.client = LayaBackend(
                model_id=self.laya_cfg["model"],
                subfolder=self.laya_cfg["subfolder"],
                device=self.laya_cfg["device"],
                hf_mirror=self.laya_cfg["hf_mirror"],
                local_model_path=self.laya_cfg["local_model_path"],
            )
            return self.client
        if self.backend_name == "kira_provider":
            self.client = self._make_kira_provider_client()
            return self.client
        self.client = JevClient(
            api_key=self.jev_cfg["api_key"],
            model=self.jev_cfg["model"],
            timeout=self.jev_cfg["request_timeout"],
            base_url=self.jev_cfg.get("base_url", ""),
        )
        return self.client

    def _make_kira_provider_client(self):
        """Build a JevClient that reuses an already-registered KiraAI provider's
        base_url + api_key (e.g. a newapi relay hosting jev-latest as a
        typesafe-systemone model). Falls back to a bare JevClient if the
        provider id is missing or unknown."""
        pid = self.jev_cfg.get("kira_provider_id", "") or ""
        cfg = {}
        if pid:
            try:
                providers = self.ctx.config.get("providers", {}) or {}
                cfg = (providers.get(pid) or {}).get("provider_config", {}) or {}
            except Exception as e:  # noqa: BLE001
                log.warning("read provider %s config failed: %s", pid, e)
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = cfg.get("api_key") or ""
        # JevClient normalises to the full /v1/systemone endpoint itself.
        if not api_key:
            api_key = self.jev_cfg.get("api_key") or os.environ.get("TYPESAFE_API_KEY", "") or ""
        log.info("[kira_game_agent] kira_provider backend: %s -> %s", pid or "(none)", base_url or "(official)")
        return JevClient(
            api_key=api_key,
            model=self.jev_cfg["model"],
            timeout=self.jev_cfg["request_timeout"],
            base_url=base_url or self.jev_cfg.get("base_url", ""),
        )

    def _ready_backend(self):
        """Best-effort: ensure the selected (default laya) backend can serve.
        If backend==laya and laya can't load, try downgrading to Jev so
        gameplay isn't blocked. Returns the active backend object."""
        if self.client is None:
            return None
        if self.backend_name == "laya":
            if isinstance(self.client, LayaBackend) and not self.client.ensure_loaded():
                # laya not available yet -> fall back if a Jev key exists,
                # else keep laya (engine will baseline). Never silently claim
                # success.
                if self.jev_cfg.get("api_key") or self._env_has_typesafe_key():
                    log.info("laya 未就绪，回退 Jev")
                    self.client = JevClient(
                        api_key=self.jev_cfg["api_key"],
                        model=self.jev_cfg["model"],
                        timeout=self.jev_cfg["request_timeout"],
                        base_url=self.jev_cfg.get("base_url", ""),
                    )
                    self.backend_name = "jev"
        return self.client

    @staticmethod
    def _env_has_typesafe_key() -> bool:
        import os as _os
        return bool(_os.environ.get("TYPESAFE_API_KEY", "").strip())

    def apply_cfg(self, cfg: dict) -> None:
        """Hot-reload: swap runtime config and re-apply knobs live."""
        old_backend = self.backend_name
        self._cfg = dict(cfg)
        self._read_cfg()
        new_backend = self.jev_cfg.get("backend", "laya") or "laya"
        if new_backend != old_backend or self.client is None:
            self._make_backend()
        elif isinstance(self.client, JevClient):
            self.client.api_key = self.jev_cfg["api_key"]
            self.client.model = self.jev_cfg["model"]
            self.client.timeout = self.jev_cfg["request_timeout"]
            self.client.base_url = JevClient._normalise_base_url(self.jev_cfg.get("base_url", ""))
        elif hasattr(self.client, "set_model"):
            self.client.set_model(
                model_id=self.laya_cfg["model"],
                subfolder=self.laya_cfg["subfolder"],
                device=self.laya_cfg["device"],
                local_model_path=self.laya_cfg["local_model_path"],
            )
        if self.notify:
            self.notify.min_interval = self.notify_cfg["min_interval"]
            self.notify.extra_sids = list(self.notify_cfg["extra_sids"])
        for eng in self.engines.values():
            eng.apply_cfg(self.jev_cfg)
            eng.set_playbook(self._playbook_for(eng.game))
            if eng.game == "ygo_engine":
                eng.auto_decision = bool(self.ygo_cfg.get("ygo_auto", False))

    def _playbook_for(self, game: str) -> str:
        return str(self.playbook_cfg["per_game_playbook"].get(game, ""))

    # ---- lifecycle ----------------------------------------------------

    async def initialize(self) -> None:
        self.client = self._make_backend()
        self.gate = DecisionGate(
            low_risk_threshold=self.jev_cfg["low_risk_threshold"],
            high_risk_threshold=self.jev_cfg["high_risk_threshold"],
            use_baseline=self.jev_cfg["use_baseline"],
        )
        self.notify = NotifyRouter(
            send=self._send_notice,
            min_interval=self.notify_cfg["min_interval"],
            extra_sids=self.notify_cfg["extra_sids"],
            default_session=True,
        )
        self.notify.start()
        # Escalation: Jev low-confidence -> wake the main LLM via synthetic msg.
        self.escalator = Escalator(
            emit=self._escalate_emit,
            high_risk_threshold=self.jev_cfg["high_risk_threshold"],
            cooldown=120.0,
        )
        # Background auto-install + configure laya (non-blocking).
        # Even if the current backend is Jev, provisioning laya up-front
        # means the moment the user flips to laya it's ready.
        self.autoinstall = LayaAutoInstaller(
            enabled=self.autoinstall_cfg.get("enabled", False),
            model_id=self.laya_cfg["model"],
            subfolder=self.laya_cfg["subfolder"],
            mirror=self.laya_cfg["hf_mirror"],
            local_model_path=self.laya_cfg["local_model_path"],
        )
        self.autoinstall.start()
        logger.info("[kira_game_agent] ready")

    async def terminate(self) -> None:
        for eng in list(self.engines.values()):
            await eng.stop()
        self.engines.clear()
        if self.autoinstall:
            await self.autoinstall.stop()
        if self.notify:
            await self.notify.stop()
        logger.info("[kira_game_agent] terminated")

    # ---- engine lifecycle ------------------------------------------------

    def _strategy_stages_for(self, game: str) -> set:
        """Configured stage set for the game, falling back to the defaults."""
        custom = (self.strategy_cfg.get("enabled_stages") or {}).get(game)
        if isinstance(custom, list) and custom:
            return set(custom)
        return STRATEGY_STAGES.get(game, set())

    def _get_or_create_engine(self, sid: str, game: str) -> Optional[DecisionEngine]:
        eid = _engine_id(sid, game)
        if eid in self.engines:
            return self.engines[eid]
        if self.client is None or self.gate is None:
            return None
        adapter_cfg = dict(self.bridge_cfg)
        if game == "ygo_engine":
            adapter_cfg.update(getattr(self, "ygo_cfg", {}) or {})
        adapter = create_adapter(game, adapter_cfg)
        if adapter is None:
            return None
        eng = DecisionEngine(
            adapter=adapter,
            client=self.client,
            gate=self.gate,
            decision_hz=self.jev_cfg["decision_hz"],
            intent_ttl=self.jev_cfg["intent_ttl"],
            game_instructions=self.playbook_cfg["game_instructions"],
            temperature=self.jev_cfg["temperature"],
            goal_floor=self.jev_cfg["goal_floor"],
            on_event=lambda kind, payload, _eid=eid: self._on_engine_event(_eid, kind, payload),
            game=game,
            sid=sid,
            escalator=self.escalator,
            backend_resolver=self._ready_backend,
            strategy_advisory=self.jev_cfg.get("strategy_advisory", True),
            advisory=self._advisory_emit if self.jev_cfg.get("strategy_advisory", True) else None,
            consult_ask_jev=self.jev_cfg.get("consult_ask_jev", True),
            consult_threshold=self.jev_cfg.get("consult_threshold", 0.6),
            strategy_stages=self._strategy_stages_for(game),
            strategy_interval_rounds=int(self.jev_cfg.get("strategy_interval_rounds", 8) or 8),
            # YGO defaults to main-LLM play (no Jev/laya needed); auto mode is
            # an explicit opt-in in the YGO config section.
            auto_decision=(bool(self.ygo_cfg.get("ygo_auto", False)) if game == "ygo_engine" else True),
        )
        eng.game = game
        eng.sid = sid
        self.engines[eid] = eng
        self.sessions.bind(sid, eid)
        return eng

    def _default_engine(self, sid: str, game: str = "") -> Optional[DecisionEngine]:
        if game:
            return self.engines.get(_engine_id(sid, game))
        eid = self.sessions.default_engine(sid)
        return self.engines.get(eid) if eid else None

    async def _stop_engine(self, eid: str) -> None:
        eng = self.engines.pop(eid, None)
        if eng is None:
            return
        await eng.stop()
        self.sessions.unbind_engine(eid)

    # ---- notifications -------------------------------------------------

    def _on_engine_event(self, eid: str, kind: str, payload: dict) -> None:
        self._events.append({"engine": eid, "kind": kind, "payload": payload, "at": time.time()})
        if len(self._events) > 500:
            self._events = self._events[-500:]
        if not self.notify_cfg["enabled"] or self.notify is None:
            return
        text = self._event_text(kind, payload)
        if not text:
            return  # diagnostic-only event; don't broadcast empty notice
        bound = self.sessions.sids_for(eid)
        self.notify.enqueue(eid, kind, text, bound)

    def _event_text(self, kind: str, payload: dict) -> str:
        if kind == "need_input":
            return (f"🎮 游戏王等待你的决策：{payload.get('reason', '轮到你')}，"
                    f"当前 {payload.get('options', '?')} 个合法选项。"
                    "用 game_ygo_duel op=state 看局面，op=act option=编号 执行，"
                    "或 op=chain options=编号1,编号2 连打。")
        if kind == "action":
            if payload.get("ok") is False:
                return (f"⚠️ 游戏操作被引擎拒绝：{payload.get('action', '?')}。"
                        "请用 game_ygo_duel op=state 查看最新合法选项后再操作。")
            if payload.get("quiet"):
                return ""  # duel actions stay in the tool transcript, not chat
            return f"🎮 游戏 Agent：执行了 {payload.get('action', '?')}"
        if kind == "danger":
            return f"⚠️ 游戏 Agent：危险等级上升（{payload.get('level', '?')}）"
        if kind == "goal_done":
            return f"✅ 游戏 Agent：目标完成 —— {payload.get('goal', '?')}"
        if kind == "stuck":
            return "🔄 游戏 Agent：检测到卡住，正在调整策略"
        # Strategy-consult events already reached the main LLM via advisory's
        # own publish_notice — do NOT double-broadcast them as notices.
        if kind in ("strategy_consult", "strategy_gate", "strategy_skipped"):
            return ""
        return f"🎮 游戏 Agent 事件：{kind}"

    async def _send_notice(self, sid: str, text: str) -> None:
        await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=True)

    def _escalate_emit(self, sid: str, reason: str) -> None:
        """Jev hesitated -> synthesize a user-like message to wake the main LLM."""
        text = (f"（系统提示）当前对局需要你决策：{reason}。"
                "你可以用 game_ygo_duel op=state 看局面、op=act option=编号 直接操作，"
                "或 game_set_goal 定打法（通用游戏用 game_inspect/game_direct）。")
        try:
            asyncio.create_task(self._send_notice(sid, text))
        except Exception as e:  # noqa: BLE001
            log.warning("escalate emit task failed: %s", e)

    def _advisory_emit(self, sid: str, stage: str, snapshot: dict) -> None:
        """Main-LLM strategic consult on a key stage entry. Sends a synthetic
        message that (a) IS mentioned -> triggers a real LLM decision round,
        and (b) carries the current game situation so the LLM can decide &
        set a goal (game_set_goal) / act (game_command) with real knowledge,
        and the user can weigh in. This is the 'key decisions -> main LLM +
        real user interaction' hook."""
        summary = self._compact_summary_from_snapshot(stage, snapshot)
        template = self.strategy_cfg.get("ask_templates") or (
            "（系统提示）游戏进入关键阶段【{stage}】。当前局面：{summary}\n"
            "请制定本次战术目标（可回应如『主打进攻/先防守拉资源/优先躲』），或直接用 game_set_goal 设定目标。"
        )
        text = template.replace("{stage}", stage).replace("{summary}", summary)
        try:
            self._events.append({
                "kind": "strategy_consult", "engine": "any",
                "payload": {"stage": stage, "summary": summary},
                "at": time.time(),
            })
            asyncio.create_task(self._send_notice(sid, text))
        except Exception as e:  # noqa: BLE001
            log.warning("advisory emit task failed: %s", e)

    def _compact_summary_from_snapshot(self, stage: str, snap: dict) -> str:
        if isinstance(snap.get("game"), dict) and (
            "legal_actions" in snap["game"] or "my_lp" in snap["game"]
        ):
            from .ygo_ops import PHASE_ZH, game_of, options_map
            g = game_of(snap)
            phase = PHASE_ZH.get(str(g.get("phase")), str(g.get("phase", "?")))
            return (f"阶段={stage} LP我{g.get('my_lp', '?')}/敌{g.get('op_lp', '?')} "
                    f"回合{g.get('turn', '?')} {phase} "
                    f"{'我方行动' if g.get('is_my_turn') else '对手行动'} "
                    f"手牌{len(g.get('hand', []))} 场上{len(g.get('field_my', []))}/{len(g.get('field_op', []))} "
                    f"合法选项{len(options_map(g))}个")
        parts = [f"阶段={stage}"]
        player = snap.get("player")
        if isinstance(player, dict):
            hp = player.get("hp_percent")
            parts.append(f"血={hp}%" if hp is not None else f"玩家={player}")
        world = snap.get("world")
        if isinstance(world, dict):
            m = world.get("map")
            if m:
                parts.append(f"地图={m}")
        dialog = (snap.get("dialog") or "").strip()
        if dialog:
            parts.append(f"文本=\"{dialog[:50]}\"")
        game = snap.get("game")
        if isinstance(game, dict):
            if game.get("opponent"):
                parts.append(f"对手={game['opponent']}")
            if game.get("turn") is not None:
                parts.append(f"回合={game['turn']}")
            lp = game.get("my_lp")
            if lp is not None:
                parts.append(f"LP={lp}")
            ph = game.get("phase")
            if ph:
                parts.append(f"阶段={ph}")
            legal = game.get("legal_actions")
            if legal:
                parts.append(f"合法动作({len(legal)}): " + ", ".join(str(a) for a in legal[:8]))
        return " ".join(parts)

    # ---- tools ------------------------------------------------------------

    @register.tool(
        name="game_play",
        description="开始游玩指定游戏（本会话独立实例，可多游戏并行）。用户说'帮我玩XX游戏'时调用。",
        params={
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": "游戏标识：pokemon_pyboy / mgba_http / ygo_engine / terrablind_http / generic_http",
                },
                "goal": {"type": "string", "description": "目标，如 'beat the first gym' / 'win the duel'"},
            },
            "required": ["game"],
        },
    )
    async def game_play(self, event: KiraMessageBatchEvent, game: str, goal: str = "") -> str:
        sid = event.sid
        if game == "none":
            return "请指定游戏（pokemon_pyboy / mgba_http / ygo_engine / terrablind_http / generic_http）"
        eng = self._get_or_create_engine(sid, game)
        if eng is None:
            return f"无法创建引擎：{game}（检查适配器/配置）"
        ok = await asyncio.to_thread(eng.adapter.start)
        if not ok:
            await self._stop_engine(_engine_id(sid, game))
            return f"游戏桥连接失败（{game}）。请检查模拟器/引擎/mod 是否运行。"
        eng.set_playbook(self._playbook_for(game))
        if goal:
            eng.set_goal(goal)
        await eng.start()
        if game == "ygo_engine":
            return ("YGO 引擎已就绪（默认主 LLM 手动打，无需 Jev）。完整流程："
                    "game_ygo op=decks 看卡组（或 build_deck 组卡）→ host 建房 / join 进房 "
                    "（server=服务器:端口 可打真人）→ ready → 房主 start；"
                    "对局用 game_ygo_duel op=state 看局面、op=act option=编号 操作、op=chain 连打。"
                    + (f" 当前目标：{goal}" if goal else ""))
        return f"已开始游玩 {game}" + (f"，目标：{goal}" if goal else "") + "。可用 game_state 查看实时情况。"

    @register.tool(
        name="game_state",
        description="查询指定游戏（默认本会话最近启动的）的实时状态摘要。用户问'现在怎么样了/到什么进度了'时调用。",
        params={
            "type": "object",
            "properties": {
                "game": {"type": "string", "description": "可选：游戏标识，不传用本会话默认引擎"},
            },
            "required": [],
        },
    )
    async def game_state(self, event: KiraMessageBatchEvent, game: str = "") -> str:
        eng = self._default_engine(event.sid, game)
        if eng is None or eng.adapter is None:
            return "本会话还没有运行中的游戏。先 game_play 开始一个。"
        if eng.game == "ygo_engine":
            # Cache-first: never block a user turn on a bridge round trip.
            snap = getattr(eng.adapter, "cached_snapshot", None) or {}
            if not snap:
                snap = await asyncio.to_thread(eng.adapter.get_state)
            if not snap or not snap.get("game"):
                return ("YGO 尚未进入对局。完整流程：game_ygo op=decks → host/join → ready → start；"
                        "对局内用 game_ygo_duel op=state/act。")
            from .ygo_ops import format_duel_state
            return format_duel_state(snap, stage=eng.stage(), goal=eng.goal, with_effects=False)
        snap = await asyncio.to_thread(eng.adapter.get_state)
        if not snap:
            return "暂时拿不到游戏状态（桥可能未连接）。"
        parts = [
            f"游戏: {eng.game}",
            f"运行: {'是' if eng.running else '否'}",
            f"目标: {eng.goal or '未设置'}",
        ]
        if snap.get("player"):
            parts.append(f"玩家: {snap['player']}")
        if snap.get("world"):
            parts.append(f"世界: {snap['world']}")
        dialog = (snap.get("dialog") or "").strip()
        if dialog:
            parts.append(f"当前文本: {dialog[:120]}")
        if snap.get("entities"):
            parts.append(f"附近实体: {len(snap['entities'])} 个")
        if snap.get("game"):
            parts.append(f"引擎: {snap['game']}")
        recent = eng.recent_decisions[-3:]
        if recent:
            parts.append("最近决策: " + "; ".join(str(d.get("action") or d.get("goal")) for d in recent))
        return "\n".join(parts)

    @register.tool(
        name="game_command",
        description="手动执行一个游戏操作原语（confirm/cancel/menu/move_up/move_down/move_left/move_right/wait 或游戏特有动作）。用户要改变操作时调用。",
        params={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "要执行的动作"},
                "frames": {"type": "integer", "description": "按住帧数（默认 8）"},
                "game": {"type": "string", "description": "可选：游戏标识，不传用本会话默认引擎"},
            },
            "required": ["action"],
        },
    )
    async def game_command(self, event: KiraMessageBatchEvent, action: str, frames: int = 8, game: str = "") -> str:
        eng = self._default_engine(event.sid, game)
        if eng is None or eng.adapter is None:
            return "本会话没有运行中的游戏。"
        if eng.game == "ygo_engine":
            # Duel actions must go through option validation too (an option
            # id, "summon:Card" or "switch:battle" all resolve live).
            return await self._ygo_duel_op(event, "act", option=action, action=action)
        ok = await asyncio.to_thread(eng.adapter.act, [{"action": action, "frames": frames}])
        return f"已执行 {action}" if ok else f"执行 {action} 失败"

    @register.tool(
        name="game_set_goal",
        description="热修改当前游戏目标（下一决策周期生效）。用户让 bot 改变打法/目标时调用。",
        params={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "新目标描述"},
                "game": {"type": "string", "description": "可选：游戏标识，不传用本会话默认引擎"},
            },
            "required": ["goal"],
        },
    )
    async def game_set_goal(self, event: KiraMessageBatchEvent, goal: str, game: str = "") -> str:
        eng = self._default_engine(event.sid, game)
        if eng is None:
            return "本会话没有运行中的游戏。"
        eng.set_goal(goal)
        return f"目标已更新为：{goal}（下个决策周期生效）"

    @register.tool(
        name="game_jev_ask",
        description="临时问 Jev 一个自定义类型化问题（noul/score/choice）。需要 Jev 对当前局面做特定判断时用。",
        params={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "问题文本（英文精度最高）"},
                "question_type": {"type": "string", "description": "noul/score/choice", "default": "noul"},
                "options": {"type": "string", "description": "choice 时的选项，JSON 对象"},
                "game": {"type": "string", "description": "可选：游戏标识，不传用本会话默认引擎"},
            },
            "required": ["question"],
        },
    )
    async def game_jev_ask(self, event: KiraMessageBatchEvent, question: str, question_type: str = "noul", options: str = "", game: str = "") -> str:
        eng = self._default_engine(event.sid, game)
        if eng is None or eng.adapter is None or self.client is None:
            return "本会话没有运行中的游戏。"
        snap = await asyncio.to_thread(eng.adapter.get_state) or {}
        q = {"type": question_type, "instructions": question}
        if question_type == "choice" and options:
            try:
                q["criteria"] = json.loads(options)
            except json.JSONDecodeError:
                return "options 必须是 JSON 对象"
        if question_type == "score":
            q["criteria"] = ["low", "medium", "high"]
        try:
            resp = await asyncio.to_thread(self.client.ask_sync, snap, {"custom": q})
            answers = resp.get("answers", resp) if isinstance(resp, dict) else resp
            return json.dumps(answers, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return f"Jev 调用失败: {e}"

    @register.tool(
        name="game_pause",
        description="暂停本会话指定游戏（或默认引擎）的自动决策，保留状态。",
        params={
            "type": "object",
            "properties": {"game": {"type": "string", "description": "可选：游戏标识"}},
            "required": [],
        },
    )
    async def game_pause(self, event: KiraMessageBatchEvent, game: str = "") -> str:
        eng = self._default_engine(event.sid, game)
        if eng is None:
            return "本会话没有运行中的游戏。"
        await eng.stop()
        return "已暂停自动决策。game_resume 继续。"

    @register.tool(
        name="game_resume",
        description="恢复本会话指定游戏（或默认引擎）的自动决策。",
        params={
            "type": "object",
            "properties": {"game": {"type": "string", "description": "可选：游戏标识"}},
            "required": [],
        },
    )
    async def game_resume(self, event: KiraMessageBatchEvent, game: str = "") -> str:
        eng = self._default_engine(event.sid, game)
        if eng is None:
            return "本会话没有运行中的游戏。"
        await eng.start()
        return "已恢复自动决策。"

    @register.tool(
        name="game_stop",
        description="停止本会话指定游戏（或默认引擎）：退出决策循环，释放模拟器。",
        params={
            "type": "object",
            "properties": {"game": {"type": "string", "description": "可选：游戏标识"}},
            "required": [],
        },
    )
    async def game_stop(self, event: KiraMessageBatchEvent, game: str = "") -> str:
        sid = event.sid
        if game:
            eid = _engine_id(sid, game)
        else:
            eid = self.sessions.default_engine(sid)
        if eid is None or eid not in self.engines:
            return "本会话没有运行中的游戏。"
        await self._stop_engine(eid)
        return f"游戏 Agent 已停止（{eid}）。"

    @register.tool(
        name="game_logs",
        description="查看本会话指定游戏（或默认引擎）的最近决策日志。",
        params={
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "条数"},
                "game": {"type": "string", "description": "可选：游戏标识"},
            },
            "required": [],
        },
    )
    async def game_logs(self, event: KiraMessageBatchEvent, n: int = 5, game: str = "") -> str:
        eng = self._default_engine(event.sid, game)
        if eng is None:
            return "本会话没有运行中的游戏。"
        decs = eng.recent_decisions[-n:]
        if not decs:
            return "暂无决策记录。"
        return "\n".join(json.dumps(d, ensure_ascii=False) for d in decs)

    @register.tool(
        name="game_diag",
        description="诊断游戏 Agent：本会话引擎、桥连接、Jev key、统计。",
        params={
            "type": "object",
            "properties": {"game": {"type": "string", "description": "可选：游戏标识"}},
            "required": [],
        },
    )
    async def game_diag(self, event: KiraMessageBatchEvent, game: str = "") -> str:
        sid = event.sid
        eng = self._default_engine(sid, game)
        parts = [f"Jev key: {'已配置' if self.client and self.client.has_key else '未配置（设 TYPESAFE_API_KEY 或配置页）'}"]
        my = self.sessions.engines_for(sid)
        parts.append(f"本会话引擎: {my or '无'}")
        if eng is not None:
            s = eng.summary()
            parts.append(f"游戏: {eng.game}  运行: {s['running']}  循环: {s['loop_count']}")
            parts.append(f"Jev 统计: {s['jev_stats']}")
        if self.notify:
            parts.append(f"播报: {self.notify.stats()}")
        return "\n".join(parts)

    # ---- stop keywords (whitelist-gated) ------------------------------

    @on.im_message(priority=Priority.HIGH)
    async def stop_keywords(self, event, *_):
        """Stop the current game when a whitelisted user says a stop keyword.
        Default keyword is only "停止玩宝可梦". Whitelist: only these user IDs
        can trigger it; empty whitelist = anyone can stop."""
        if not self.engines:
            return
        # Only group messages (bot's normal chat context)
        text = ""
        for elem in event.message.chain:
            if isinstance(elem, Text):
                text += elem.text
        if not text.strip():
            return
        # Check stop keywords
        keywords = self.bridge_cfg.get("stop_keywords", []) or []
        if not any(kw in text for kw in keywords):
            return
        # Whitelist check
        whitelist = self.bridge_cfg.get("stop_whitelist", []) or []
        sender = str(event.message.sender.user_id) if event.message.sender else ""
        if whitelist and sender not in [str(u) for u in whitelist]:
            return
        # Stop all engines in this session
        sid = event.session.sid
        stopped = []
        for eid in list(self.sessions.engines_for(sid)):
            await self._stop_engine(eid)
            stopped.append(eid)
        if stopped:
            try:
                await self.ctx.publish_notice(
                    sid, MessageChain([Text("已停止游戏。")]), is_mentioned=True
                )
            except Exception:  # noqa: BLE001
                pass
            # Only swallow the message when we actually stopped something;
            # otherwise the keyword may be ordinary conversation.
            event.discard()

    # ---- hitchhiking + per-game tool injection ---------------------------

    @on.llm_request()
    async def hitchhike(self, event, req, tag_set, *_):
        """Two things every user turn:
        1. Inject per-game special tools for games the user enabled in the
           WebUI (enabled_games). Unused games stay out of the tool table.
        2. Inject a one-line game-state summary into this turn's context
           (persist=False) so the main LLM 'knows' the situation when the
           user asks about the game."""
        sid = getattr(event, "sid", "")
        # 1. tools
        tool_set = getattr(req, "tool_set", None)
        if tool_set is not None:
            for game in self.bridge_cfg["enabled_games"]:
                for t in build_tools_for(game, self):
                    if tool_set.get(t.name) is None:
                        tool_set.add(t)
        # Tool granularity: merged mode keeps only the merged tools
        # (game_play/control/inspect/direct) + per-game specials; fine mode
        # keeps everything. This keeps the LLM tool table compact by default.
        if tool_set is not None and self.bridge_cfg.get("tool_granularity", "merged") == "merged":
            fine_tools = {
                "game_state", "game_logs", "game_diag",
                "game_command", "game_set_goal", "game_jev_ask",
                "game_pause", "game_resume", "game_stop",
            }
            tool_set.remove(*fine_tools)
        # 2. hitchhike summary
        if not sid:
            return
        eng = self._default_engine(sid)
        if eng is None or eng.adapter is None:
            return
        # Cache only: a bridge HTTP call here would stall every user turn.
        snap = getattr(eng.adapter, "cached_snapshot", None) or {}
        if not snap:
            return
        summary = self._compact_summary(eng, snap)
        if not summary:
            return
        # Append a fresh request-scoped Prompt instead of mutating the shared
        # chat_env block (mutating it leaked state across turns and grew the
        # prompt every request). Named chat_env so the framework relocates it
        # next to the latest user message, keeping the system prefix cacheable.
        req.system_prompt.append(
            Prompt(f"[游戏 Agent] {summary}", name="chat_env", source="kira_game_agent", persist=False)
        )

    def _ygo_summary_line(self, eng, snap: dict) -> str:
        """One compact line of duel context for the LLM turn (cache-friendly)."""
        from .ygo_ops import PHASE_ZH, game_of, options_map
        g = game_of(snap)
        stage = eng.stage()
        if not g:
            room = snap.get("room") or {}
            players = ",".join(str(p) for p in (room.get("players") or []))
            return (f"游戏=游戏王 阶段={stage} 房间={room.get('name', '?')} 玩家[{players}] "
                    "（流程：game_ygo ready→房主 start；对局操作用 game_ygo_duel）")
        parts = [f"游戏=游戏王 阶段={stage}"]
        parts.append(f"LP我{g.get('my_lp', '?')}/敌{g.get('op_lp', '?')}")
        phase = PHASE_ZH.get(str(g.get('phase')), str(g.get('phase', '?')))
        parts.append(f"回合{g.get('turn', '?')} {phase} {'我方行动' if g.get('is_my_turn') else '对手行动'}")
        parts.append(f"手牌{len(g.get('hand', []))} 场上{len(g.get('field_my', []))}/{len(g.get('field_op', []))}")
        opts = options_map(g)
        if opts:
            parts.append(f"合法选项{len(opts)}个")
        if g.get("response_window"):
            parts.append("⚠对手动作等待你响应（连锁或跳过）")
        if g.get("is_duel_active") is False:
            parts.append(f"对局已结束 胜者={g.get('winner') or '?'}")
        parts.append("详情 game_ygo_duel op=state，操作 op=act option=编号")
        return " ".join(parts)

    def _compact_summary(self, eng, snap: dict) -> str:
        if eng.game == "ygo_engine":
            return self._ygo_summary_line(eng, snap)
        parts = []
        st = eng.stage()
        parts.append(f"游戏={eng.game} 阶段={st}")
        player = snap.get("player")
        if isinstance(player, dict):
            hp = player.get("hp_percent")
            parts.append(f"血={hp}%" if hp is not None else f"玩家={player}")
        world = snap.get("world")
        if isinstance(world, dict):
            m = world.get("map")
            if m:
                parts.append(f"地图={m}")
        dialog = (snap.get("dialog") or "").strip()
        if dialog:
            parts.append(f"文本=\"{dialog[:60]}\"")
        game = snap.get("game")
        if isinstance(game, dict):
            if game.get("opponent"):
                parts.append(f"对手={game['opponent']}")
            if game.get("turn") is not None:
                parts.append(f"回合={game['turn']}")
            lp = game.get("my_lp")
            if lp is not None:
                parts.append(f"LP={lp}")
        recent = eng.recent_decisions[-2:]
        if recent:
            acts = "; ".join(str(d.get("action") or d.get("goal")) for d in recent)
            parts.append(f"最近决策=[{acts}]")
        # Adventure log (long-term journey memory, from the old plugin design).
        adv = (game.get("adventure_log") if isinstance(game, dict) else None)
        if adv:
            latest = adv[-1]
            parts.append(f"冒险日志=[{latest.get('event', '?')}: {latest.get('details', '')}]")
        return " ".join(parts)

    # ---- per-game special tool handlers ------------------------------

    def _ygo_engine(self, sid: str):
        """Get or create the ygo engine for this session."""
        eng = self._default_engine(sid, "ygo_engine")
        if eng is not None:
            return eng
        return self._get_or_create_engine(sid, "ygo_engine")

    async def _ygo_op(self, event, op: str, **kwargs) -> str:
        """Deck/room lifecycle for YGO (see ygo_agent.lifecycle)."""
        from . import ygo_agent
        return await ygo_agent.lifecycle(self, event, op, **kwargs)

    async def _ygo_duel_op(self, event, op: str = "state", **kwargs) -> str:
        """In-duel state/actions for YGO (see ygo_agent.duel)."""
        from . import ygo_agent
        return await ygo_agent.duel(self, event, op, **kwargs)

    async def _ygo_join(self, event, room: str, deck: str = "") -> str:
        return await self._ygo_op(event, "join", room=room, deck=deck)

    async def _ygo_host(self, event, room: str = "", deck: str = "") -> str:
        return await self._ygo_op(event, "host", room=room, deck=deck)

    async def _ygo_ready(self, event) -> str:
        return await self._ygo_op(event, "ready")

    async def _ygo_set_deck(self, event, deck: str) -> str:
        return await self._ygo_op(event, "set_deck", deck=deck)

    async def _ygo_duel_info(self, event) -> str:
        return await self._ygo_duel_op(event, "state")

    def _pkm_use_item(self, event, item: str) -> str:
        sid = event.sid
        eng = self._default_engine(sid, "pokemon_pyboy")
        if eng is None or eng.adapter is None:
            return "宝可梦引擎未运行。"
        try:
            if hasattr(eng.adapter, "use_item"):
                ok = eng.adapter.use_item(item)
            else:
                # Generic path: open menu, move to item, confirm (approx).
                ok = eng.adapter.act([
                    {"action": "menu", "frames": 8},
                    {"action": "move_down", "frames": 8},
                    {"action": "confirm", "frames": 8},
                ])
            return f"使用道具 {item}" if ok else f"使用道具 {item} 失败"
        except Exception as e:  # noqa: BLE001
            return f"使用道具失败: {e}"

    def _pkm_team(self, event) -> str:
        sid = event.sid
        eng = self._default_engine(sid, "pokemon_pyboy")
        if eng is None or eng.adapter is None:
            return "宝可梦引擎未运行。"
        snap = eng.adapter.get_state() or {}
        party = (snap.get("player") or {}).get("party", [])
        if not party:
            return "暂无队伍信息。"
        return "\n".join(
            f"#{i+1} species={p.get('species')} Lv{p.get('level')} HP {p.get('hp')}"
            for i, p in enumerate(party)
        )

    def _tb_start(self, event) -> str:
        sid = event.sid
        eng = self._default_engine(sid, "terrablind_http")
        if eng is None or eng.adapter is None:
            return "泰拉瑞亚引擎未运行。"
        try:
            res = eng.adapter.command("/start_run", {})
            eng.set_stage("exploring")
            return f"TerraBlind 主线已触发: {res}"
        except Exception as e:  # noqa: BLE001
            return f"触发失败: {e}"

    def _tb_stop(self, event) -> str:
        sid = event.sid
        eng = self._default_engine(sid, "terrablind_http")
        if eng is None or eng.adapter is None:
            return "泰拉瑞亚引擎未运行。"
        try:
            eng.adapter.command("/start_run_stop", {})
            return "TerraBlind 主线已停止"
        except Exception as e:  # noqa: BLE001
            return f"停止失败: {e}"

    @register.tool(
        name="game_chain",
        description="预输入一串游戏动作，按顺序执行。没被打断则一气呵成，被打断则停下汇报现场情况。主 LLM 可用它一次决定多个连续动作（如'召唤→发动效果→进战斗→攻击'）。",
        params={
            "type": "object",
            "properties": {
                "actions": {"type": "string", "description": "动作序列，逗号分隔，如 'summon:Mystic Tomato,activate,switch:battle,attack'"},
                "game": {"type": "string", "description": "可选：游戏标识，不传用本会话默认引擎"},
            },
            "required": ["actions"],
        },
    )
    async def game_chain(self, event: KiraMessageBatchEvent, actions: str, game: str = "") -> str:
        """Pre-input a chain of actions. Execute them in order; stop and report
        the live situation if interrupted (state changed unexpectedly)."""
        eng = self._default_engine(event.sid, game)
        if eng is None or eng.adapter is None:
            return "本会话没有运行中的游戏。"
        if eng.game == "ygo_engine":
            # Every option is re-validated against the live state before it
            # fires; our own moves changing the board is NOT an interruption.
            return await self._ygo_duel_op(event, "chain", options=actions)
        # Parse the action chain: "summon:X,activate,switch:battle,attack"
        chain = []
        for part in actions.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                act, arg = part.split(":", 1)
                chain.append({"action": act.strip(), "card": arg.strip()})
            else:
                chain.append({"action": part})
        if not chain:
            return "动作序列为空。"
        # Execute the chain step by step; check interruption between steps.
        executed = []
        last_state = None
        for step in chain:
            # Check if interrupted (state changed from what we expect)
            try:
                snap = await asyncio.to_thread(eng.adapter.get_state) or {}
            except Exception:  # noqa: BLE001
                snap = {}
            if last_state is not None and self._chain_interrupted(last_state, snap):
                # Interrupted — report the live situation so the main LLM can
                # re-decide from here.
                summary = self._compact_summary(eng, snap)
                return (f"已执行 {len(executed)} 步（{', '.join(executed)}）后被现场变化打断。"
                        f"当前局面：{summary}\n"
                        f"剩余未执行：{', '.join(s.get('action') for s in chain[len(executed):])}\n"
                        f"请根据现场继续判断。")
            # Execute this step
            ok = await asyncio.to_thread(eng.adapter.act, [step])
            if not ok:
                return f"第 {len(executed)+1} 步（{step.get('action')}）执行失败。"
            executed.append(step.get("action"))
            # Re-baseline AFTER our own move: our own state change must not be
            # mistaken for an opponent interruption on the next step.
            try:
                last_state = await asyncio.to_thread(eng.adapter.get_state) or snap
            except Exception:  # noqa: BLE001
                last_state = snap
        return f"预输入链全部执行完成（{len(executed)} 步：{', '.join(executed)}）。"

    def _chain_interrupted(self, old_state: dict, new_state: dict) -> bool:
        """Detect if the game state changed unexpectedly between chain steps."""
        g_old = (old_state.get("game") or {}) if isinstance(old_state, dict) else {}
        g_new = (new_state.get("game") or {}) if isinstance(new_state, dict) else {}
        # Interruption signals: phase/turn/LP/field changed.
        return (g_old.get("phase") != g_new.get("phase")
                or g_old.get("turn") != g_new.get("turn")
                or g_old.get("my_lp") != g_new.get("my_lp")
                or g_old.get("op_lp") != g_new.get("op_lp")
                or len(g_old.get("field_my", [])) != len(g_new.get("field_my", []))
                or len(g_old.get("field_op", [])) != len(g_new.get("field_op", [])))

    @register.tool(
        name="game_control",
        description="控制游戏决策循环：暂停/恢复/停止。用户说'暂停/继续/停吧'时调用。",
        params={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "pause / resume / stop", "enum": ["pause", "resume", "stop"]},
                "game": {"type": "string", "description": "可选：游戏标识，不传用本会话默认引擎"},
            },
            "required": ["action"],
        },
    )
    async def game_control(self, event: KiraMessageBatchEvent, action: str, game: str = "") -> str:
        if action == "pause":
            return await self.game_pause(event, game)
        if action == "resume":
            return await self.game_resume(event, game)
        return await self.game_stop(event, game)

    @register.tool(
        name="game_inspect",
        description="查看游戏信息：实时状态/决策日志/诊断。用户问'现在怎么样了/最近做了什么/出问题了没'时调用。",
        params={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "state / logs / diag", "enum": ["state", "logs", "diag"], "default": "state"},
                "game": {"type": "string", "description": "可选：游戏标识，不传用本会话默认引擎"},
                "n": {"type": "integer", "description": "logs 时的条数"},
            },
            "required": ["scope"],
        },
    )
    async def game_inspect(self, event: KiraMessageBatchEvent, scope: str = "state", game: str = "", n: int = 5) -> str:
        if scope == "logs":
            return await self.game_logs(event, n, game)
        if scope == "diag":
            return await self.game_diag(event, game)
        return await self.game_state(event, game)

    @register.tool(
        name="game_direct",
        description="直接操作游戏：执行动作/改目标/问 Jev。用户让 bot 改变操作/打法/目标时调用。",
        params={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "command / goal / jev_ask", "enum": ["command", "goal", "jev_ask"]},
                "action": {"type": "string", "description": "kind=command 时的动作"},
                "goal": {"type": "string", "description": "kind=goal 时的新目标"},
                "question": {"type": "string", "description": "kind=jev_ask 时的自定义问题"},
                "game": {"type": "string", "description": "可选：游戏标识"},
            },
            "required": ["kind"],
        },
    )
    async def game_direct(self, event: KiraMessageBatchEvent, kind: str, action: str = "", goal: str = "", question: str = "", game: str = "") -> str:
        if kind == "command":
            return await self.game_command(event, action, game=game)
        if kind == "goal":
            return await self.game_set_goal(event, goal, game)
        return await self.game_jev_ask(event, question, game=game)

    # ---- custom events ----------------------------------------------------

    @on.custom_event(event_name="game_agent_set_goal")
    async def on_set_goal(self, custom_event, *_):
        goal = (custom_event.payload or {}).get("goal", "")
        sid = (custom_event.payload or {}).get("sid", "")
        if goal:
            eng = self._default_engine(sid) if sid else None
            if eng:
                eng.set_goal(goal)

    @on.custom_event(event_name="game_agent_apply_config")
    async def on_apply_config(self, custom_event, *_):
        cfg = (custom_event.payload or {}).get("config")
        if isinstance(cfg, dict):
            self.apply_cfg(cfg)

    # ---- WebUI page + API ------------------------------------------------

    @register.page(
        "/config",
        auth=True,
        menu=PageMenu(label={"zh": "游戏 Agent"}, icon="Gamepad", order=100),
    )
    def config_page(self):
        return PluginPage.from_folder("./pages/web")

    @register.api(method="GET", path="/status", auth=True, summary="游戏 Agent 状态")
    async def api_status(self):
        engines = {}
        for eid, eng in self.engines.items():
            s = eng.summary()
            engines[eid] = {
                "running": s["running"],
                "loop_count": s["loop_count"],
                "goal": s["goal"],
                "last_action": s["last_action"],
                "game": getattr(eng, "game", ""),
            }
        # backend info
        backend_info = {"name": self.backend_name}
        if self.backend_name == "laya" and self.client is not None:
            backend_info["laya_loaded"] = bool(getattr(self.client, "_agent", None))
            backend_info["laya_error"] = getattr(self.client, "_load_error", None)
        autoinstall_info = self.autoinstall.summary() if self.autoinstall else {"state": "off"}
        return {
            "engines": engines,
            "backend": backend_info,
            "autoinstall": autoinstall_info,
            "engine_key": bool(self.client and self.client.has_key),
            "jev_stats": self.client.stats if self.client else {},
            "notify": self.notify.stats() if self.notify else {},
            "events": self._events[-20:],
        }

    @register.api(method="GET", path="/config", auth=True, summary="当前配置")
    async def api_get_config(self):
        """Return the merged, schema-complete config (not the partial runtime copy).

        The framework merges schema defaults into the plugin's stored config;
        returning `self._cfg` (a partial runtime object) would drop any keys
        not currently in it. Always read through the plugin manager so the
        frontend sees the real, complete config."""
        mgr = getattr(self.ctx, "plugin_mgr", None)
        if mgr is not None:
            try:
                return mgr.get_plugin_config("KiraAI_jev_game_plugin")
            except Exception:  # noqa: BLE001
                pass
        return self._cfg

    @register.api(method="GET", path="/providers", auth=True, summary="列出 KiraAI 已注册提供商")
    async def api_list_providers(self):
        """List the providers the user has already registered in KiraAI's
        WebUI, so the config page can offer them as a dropdown for the
        `kira_provider` decision backend."""
        out = []
        try:
            providers = self.ctx.config.get("providers", {}) or {}
            for pid, cfg in providers.items():
                pc = (cfg or {}).get("provider_config", {}) or {}
                out.append({
                    "id": pid,
                    "name": cfg.get("name") or pc.get("name") or pid,
                    "format": cfg.get("format", ""),
                    "base_url": pc.get("base_url", ""),
                    "has_key": bool(pc.get("api_key")),
                })
        except Exception as e:  # noqa: BLE001
            log.warning("list providers failed: %s", e)
        return {"providers": out}

    @register.api(method="POST", path="/config", auth=True, summary="热修改配置")
    async def api_set_config(self, body: dict = None):
        """Hot-reload config. FastAPI injects the JSON body into `body`.

        Deep-merges the posted patch onto the stored config so a partial
        `section_jev` patch does not wipe sibling fields, persists to
        data/config/plugins/KiraAI_jev_game_plugin.json, then hot-applies to the
        runtime without restarting the plugin."""
        if not isinstance(body, dict):
            return {"ok": False, "message": "body 必须是 JSON 对象"}
        mgr = getattr(self.ctx, "plugin_mgr", None)
        if mgr is None:
            return {"ok": False, "message": "plugin_mgr 不可用"}
        # Deep-merge at section level: section_* keys merge field-by-field.
        current = mgr.get_plugin_config("KiraAI_jev_game_plugin")
        merged = dict(current)
        for key, value in body.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                merged[key] = {**current[key], **value}
            else:
                merged[key] = value
        # Persist to the plugin config file (no framework restart — our
        # apply_cfg hot-applies to the running instance instead).
        try:
            from core.plugin.manager import PLUGIN_CONFIG_DIR
            import json as _json
            PLUGIN_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (PLUGIN_CONFIG_DIR / "KiraAI_jev_game_plugin.json").write_text(
                _json.dumps(merged, indent=4, ensure_ascii=False), encoding="utf-8"
            )
            mgr.plugin_configs["KiraAI_jev_game_plugin"] = merged
        except Exception as e:  # noqa: BLE001
            log.warning("persist config failed: %s", e)
        self.apply_cfg(merged)
        return {"ok": True, "message": "配置已热更新", "applied_backend": self.backend_name}


def register():
    return KiraGameAgentPlugin
