"""YGO engine adapter — bridge to YGOPro core / ygoenv headless engine.

YGO duels are turn-based card games. The engine exposes a fully structured
duel state (hand, field, graveyard, banished, LP, phase, turn, legal actions)
and accepts legal-action submissions (Set/Summon/Activate/Attack/Switch/…).

Two deployment shapes, both zero-VLM:
- **ygoenv** (ygo-agent): headless ygopro-core via gym API, in-process or
  via a thin HTTP bridge. Supports network duels (YGOPro server protocol).
- **EDOPro**: the client itself runs ygopro-core; a Lua bridge script can
  expose the same duel state over HTTP.

Adapter contract (same as other games):
    get_state() -> dict   # compact duel snapshot (public info only)
    act(steps) -> bool    # submit one legal action
    join(room, deck) -> dict
    ready() -> bool
    set_deck(deck) -> bool
    health() -> bool

YGO-specific action vocabulary (from ygopro-core's MSG types):
    Set / Summon Face-up Attack / Summon Face-down Defense / Reposition
    Special Summon / Attack / DirectAttack / Activate (card+effect)
    Cancel / Switch phase / SelectPosition / AnnounceNumber / AnnounceAttrib
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("kira_game_agent.adapter.ygo")

# Action type codes (ygopro-core MSG codes).
ACT_SET = 1
ACT_REPOSITION = 2
ACT_SPECIAL_SUMMON = 3
ACT_SUMMON_ATK = 4
ACT_SUMMON_DEF = 5
ACT_ATTACK = 6
ACT_DIRECT_ATTACK = 7
ACT_ACTIVATE = 8
ACT_CANCEL = 9
ACT_SWITCH_PHASE = 10
ACT_SELECT_POSITION = 11
ACT_ANNOUNCE_NUMBER = 12
ACT_SELECT_PLACE = 13
ACT_ANNOUNCE_ATTRIB = 14

PHASE_NAMES = {
    1: "draw", 2: "standby", 3: "main1", 4: "battle", 5: "main2", 6: "end",
}
POSITION_NAMES = {0: "face-down", 1: "face-up-atk", 2: "face-up-def", 3: "face-down-atk", 4: "face-down-def"}


class YgoEngineAdapter:
    """YGOPro core engine adapter (ygoenv/EDOPro bridge)."""

    name = "ygo_engine"

    def __init__(self, host: str = "127.0.0.1", port: int = 5274,
                 game_mode: str = "single", server_url: str = "") -> None:
        self.host = host
        self.port = port
        self.game_mode = game_mode          # "single" | "network"
        self.server_url = server_url
        self._http = None
        self._connected = False
        self.last_snapshot: dict[str, Any] = {}
        self.duel_active = False
        self._deck = "default.ydk"
        self._room = ""
        self._opponent = ""

    # ---- lifecycle -------------------------------------------------

    def start(self) -> bool:
        try:
            import requests
        except ImportError:
            log.error("requests not installed")
            return False
        self._http = requests.Session()
        try:
            r = self._http.get(f"http://{self.host}:{self.port}/health", timeout=2)
            self._connected = r.status_code == 200
            if self._connected:
                log.info("ygo bridge connected: %s:%s", self.host, self.port)
            return self._connected
        except Exception:  # noqa: BLE001
            self._connected = False
            log.warning("ygo bridge not reachable: %s:%s", self.host, self.port)
            return False

    def stop(self) -> None:
        self._connected = False

    def health(self) -> bool:
        if not self._connected:
            return self.start()
        try:
            r = self._http.get(f"http://{self.host}:{self.port}/health", timeout=2)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    # ---- bridge contract ---------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Compact duel snapshot (public info only — opponent's hand is
        reported as count, not content). Zero VLM, all from the engine."""
        if not self.health():
            return {}
        try:
            r = self._http.get(f"http://{self.host}:{self.port}/state", timeout=5)
            if r.status_code != 200:
                return {}
            snap = r.json()
            self.last_snapshot = snap
            self.duel_active = bool(snap.get("game", {}).get("is_duel_active", True))
            return snap
        except Exception as e:  # noqa: BLE001
            log.warning("ygo get_state failed: %s", e)
            return {}

    def act(self, steps: list[dict]) -> bool:
        """Submit one legal action. Each step is a ygopro action dict:
        {"action": "summon", "card": "Blue-Eyes", "position": "attack"} etc."""
        if not self.health():
            return False
        try:
            for step in steps:
                payload = self._to_engine_action(step)
                if not payload:
                    continue
                r = self._http.post(f"http://{self.host}:{self.port}/act",
                                    json=payload, timeout=5)
                if r.status_code != 200:
                    log.warning("ygo act failed: %s", r.text[:100])
                    return False
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("ygo act failed: %s", e)
            return False

    def _to_engine_action(self, step: dict) -> Optional[dict]:
        """Translate a plugin action step into the engine's action format."""
        a = step.get("action", "").lower()
        card = step.get("card", "")
        effect = step.get("effect")
        position = step.get("position")
        target = step.get("target")
        m = {
            "set": ACT_SET,
            "summon": ACT_SUMMON_ATK,
            "summon_def": ACT_SUMMON_DEF,
            "special_summon": ACT_SPECIAL_SUMMON,
            "reposition": ACT_REPOSITION,
            "attack": ACT_ATTACK,
            "direct_attack": ACT_DIRECT_ATTACK,
            "activate": ACT_ACTIVATE,
            "cancel": ACT_CANCEL,
            "switch": ACT_SWITCH_PHASE,
        }
        if a not in m:
            return None
        out = {"action": m[a]}
        if card:
            out["card"] = card
        if effect is not None:
            out["effect"] = effect
        if position is not None:
            out["position"] = position
        if target is not None:
            out["target"] = target
        return out

    # ---- duel management --------------------------------------------

    def join(self, room: str, deck: str = "") -> dict:
        """Join a duel room (single player vs AI, or network room)."""
        if deck:
            self._deck = deck
        self._room = room
        try:
            r = self._http.post(f"http://{self.host}:{self.port}/join",
                                json={"room": room, "deck": self._deck,
                                      "mode": self.game_mode,
                                      "server": self.server_url}, timeout=10)
            if r.status_code != 200:
                raise RuntimeError(f"join HTTP {r.status_code}")
            data = r.json()
            self._opponent = data.get("opponent", "")
            return {"opponent": self._opponent, "room": room}
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"join failed: {e}") from e

    def ready(self) -> bool:
        """Confirm ready (opponent sees you are ready)."""
        try:
            r = self._http.post(f"http://{self.host}:{self.port}/ready",
                                json={}, timeout=5)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def set_deck(self, deck: str) -> bool:
        """Select a deck (.ydk in the bridge's deck dir)."""
        try:
            r = self._http.post(f"http://{self.host}:{self.port}/set_deck",
                                json={"deck": deck}, timeout=5)
            if r.status_code == 200:
                self._deck = deck
                return True
            return False
        except Exception:  # noqa: BLE001
            return False

    @property
    def opponent(self) -> str:
        return self._opponent

    @property
    def room(self) -> str:
        return self._room