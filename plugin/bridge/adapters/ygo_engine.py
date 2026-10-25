"""YGO engine adapter — HTTP bridge to a real ygopro-core duel engine.

Network play against **real human players** is the primary target; single
player vs AI is the same protocol with a local opponent. The bridge owns the
rules and the room/duel lifecycle, we own decisions.

Bridge REST contract (see guides/YGO指南.md for the full spec):

    GET  /health                 -> {"ok": true}
    GET  /decks                  -> {"decks": [{"name", "main", "extra", "side"}]}
    POST /build_deck             -> {"ok": true, "name": ...}
    GET  /rooms                  -> {"rooms": [...]}                (optional)
    POST /host                   -> {"ok": true, "room": ..., "session": ...}
    POST /join                   -> {"ok": true, "opponent": ..., "session": ...}
    POST /set_deck               -> {"ok": true}
    POST /ready                  -> {"ok": true, "ready": true}
    POST /start                  -> {"ok": true}                    (host only)
    GET  /state                  -> {"stage": ..., "room": {...}, "game": {...}}
    POST /act                    -> {"ok": bool, "state": {...}?}
    POST /leave  /surrender      -> {"ok": true}
    POST /chat                   -> {"ok": true}                    (optional)

``host``/``join`` accept ``server`` (YGOPro server ``host:port``), ``room``,
``password``, ``deck`` and ``mode`` (``network`` | ``single``). A returned
``session`` token is echoed on later calls so multi-client bridges can tell
which seat we occupy; single-client bridges simply ignore it.

``/act`` accepts one legal option in three equivalent forms (all present):
``option`` (bridge id or index), semantic fields (``action``/``card``/
``position``/``target``/``phase``…), and numeric ``code`` (ygopro-core
response). HTTP 200 with ``{"ok": false}`` **is a failure** — we never report
a rejected move as executed.

All methods are synchronous (requests); async callers wrap them with
``asyncio.to_thread``. ``get_state`` also maintains ``cached_snapshot`` so
prompt/tool reads never need a network round trip.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("kira_game_agent.adapter.ygo")

PHASE_NAMES = {1: "draw", 2: "standby", 3: "main1", 4: "battle", 5: "main2", 6: "end"}


class YgoEngineAdapter:
    """ygopro-core bridge adapter (network rooms + local duels)."""

    name = "ygo_engine"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5274,
        game_mode: str = "network",
        server_url: str = "",
        deck: str = "",
        password: str = "",
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = int(port or 5274)
        self.game_mode = game_mode or "network"   # "network" | "single"
        self.server_url = server_url              # real YGOPro server host:port
        self.password = password
        self.timeout = float(timeout or 5.0)
        self._http = None
        self._connected = False
        self._session = ""
        self._deck = deck or "default.ydk"
        self._room = ""
        self._opponent = ""
        self._is_host = False
        self.last_error = ""
        self.last_snapshot: dict[str, Any] = {}
        self.cached_snapshot: dict[str, Any] = {}
        self.cached_at = 0.0
        self.duel_active = False
        self._failures = 0
        self._next_retry = 0.0

    # ---- low level ---------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _ensure_http(self) -> bool:
        if self._http is not None:
            return True
        try:
            import requests
        except ImportError:
            self.last_error = "requests 未安装"
            log.error("requests not installed")
            return False
        self._http = requests.Session()
        return True

    def _with_session(self, payload: Optional[dict] = None) -> dict:
        data = dict(payload or {})
        if self._session:
            data.setdefault("session", self._session)
        return data

    def _get(self, path: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        if not self._ensure_http():
            return {"ok": False, "error": self.last_error}
        try:
            r = self._http.get(self.base + path, params=self._with_session(params or {}),
                               timeout=timeout or self.timeout)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:160]}", "_http": r.status_code}
            data = r.json()
            if not isinstance(data, dict):
                return {"ok": True, "data": data}
            return data
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _post(self, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        if not self._ensure_http():
            return {"ok": False, "error": self.last_error}
        try:
            r = self._http.post(self.base + path, json=self._with_session(payload),
                                timeout=timeout or self.timeout)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}", "_http": r.status_code}
            try:
                data = r.json()
            except ValueError:
                return {"ok": True}
            if isinstance(data, dict):
                # Business-level rejection is still a failure.
                if data.get("ok") is False and not data.get("error"):
                    data["error"] = data.get("detail") or data.get("message") or "引擎拒绝该操作"
                return data
            return {"ok": True, "data": data}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _err(res: dict, fallback: str = "操作失败") -> str:
        return str(res.get("error") or res.get("detail") or res.get("message") or fallback)

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> bool:
        if not self._ensure_http():
            return False
        res = self._get("/health", timeout=2.5)
        self._connected = not res.get("error") and res.get("ok", True) is not False
        if self._connected:
            self._failures = 0
            self.last_error = ""
            log.info("ygo bridge connected: %s:%s (mode=%s server=%s)",
                     self.host, self.port, self.game_mode, self.server_url or "bridge-local")
        else:
            self.last_error = self._err(res, "桥不可达")
            log.warning("ygo bridge not reachable: %s:%s %s", self.host, self.port, self.last_error)
        return self._connected

    def stop(self) -> None:
        self._connected = False

    def health(self) -> bool:
        if self._connected:
            return True
        if time.monotonic() < self._next_retry:
            return False
        self._next_retry = time.monotonic() + 2.0
        return self.start()

    # ---- state -------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Fresh duel/room snapshot. No extra /health round trip: a failed
        read itself marks the bridge down and throttles reconnection."""
        if not self._ensure_http():
            return self.cached_snapshot or {}
        res = self._get("/state", timeout=self.timeout)
        failed = bool(res.get("_http")) or (res.get("ok") is False and "game" not in res and "stage" not in res)
        if failed:
            self._failures += 1
            self._connected = False
            self.last_error = self._err(res, "读取状态失败")
            return self.cached_snapshot or {}
        self._failures = 0
        self._connected = True
        snap = {k: v for k, v in res.items() if k != "ok"}
        self.last_snapshot = snap
        self.cached_snapshot = snap
        self.cached_at = time.monotonic()
        g = snap.get("game") if isinstance(snap.get("game"), dict) else {}
        self.duel_active = bool(g.get("is_duel_active", False))
        if g.get("opponent"):
            self._opponent = str(g.get("opponent"))
        return snap

    def get_cached_state(self, max_age: float = 3.0) -> dict[str, Any]:
        """Cache-first read for prompts/tools (zero network when fresh)."""
        if self.cached_snapshot and (time.monotonic() - self.cached_at) <= max_age:
            return self.cached_snapshot
        return self.get_state()

    # ---- actions -----------------------------------------------------

    def submit(self, payload: dict) -> dict:
        """Submit exactly one legal option; returns the bridge verdict."""
        res = self._post("/act", payload, timeout=self.timeout)
        ok = bool(res.get("ok", False)) and not res.get("error")
        if not ok:
            self.last_error = self._err(res, "动作被引擎拒绝")
            log.warning("ygo act rejected: %s payload=%s", self.last_error, str(payload)[:160])
        else:
            self.last_error = ""
            state = res.get("state")
            if isinstance(state, dict):
                self.cached_snapshot = state
                self.last_snapshot = state
                self.cached_at = time.monotonic()
        res["ok"] = ok
        return res

    def act(self, steps: list[dict]) -> bool:
        """Adapter contract: submit each step as one legal option."""
        if not steps:
            return False
        ok_all = True
        for step in steps:
            if not isinstance(step, dict):
                continue
            res = self.submit(step)
            if not res.get("ok"):
                ok_all = False
                break
        return ok_all

    # ---- decks -------------------------------------------------------

    def list_decks(self) -> dict:
        return self._get("/decks", timeout=self.timeout)

    def build_deck(self, name: str, zones: Optional[dict] = None, ydk: str = "") -> dict:
        payload: dict[str, Any] = {"name": name}
        if ydk:
            payload["ydk"] = ydk
        if zones:
            payload.update({k: zones.get(k, []) for k in ("main", "extra", "side")})
        return self._post("/build_deck", payload, timeout=max(self.timeout, 10.0))

    def set_deck(self, deck: str) -> bool:
        res = self._post("/set_deck", {"deck": deck})
        if res.get("ok") and not res.get("error"):
            self._deck = deck
            self.last_error = ""
            return True
        self.last_error = self._err(res, "设置卡组失败")
        return False

    # ---- rooms / network play ----------------------------------------

    def list_rooms(self) -> dict:
        return self._get("/rooms", timeout=self.timeout)

    def _room_payload(self, room: str, deck: str, server: str, password: str,
                      mode: str, options: Optional[dict]) -> dict:
        payload = {
            "room": room,
            "deck": deck or self._deck,
            "mode": mode or self.game_mode,
            "server": server or self.server_url,
        }
        pwd = password or self.password
        if pwd:
            payload["password"] = pwd
        if options:
            payload["options"] = options
        return payload

    def host_room(self, room: str = "", deck: str = "", server: str = "", password: str = "",
                  mode: str = "", options: Optional[dict] = None) -> dict:
        """Create a room real players can join (or a local AI duel).

        Named ``host_room`` because ``self.host`` is the bridge hostname —
        a ``host()`` method would be shadowed by that instance attribute.
        """
        payload = self._room_payload(room, deck, server, password, mode, options)
        res = self._post("/host", payload, timeout=max(self.timeout, 12.0))
        if res.get("ok") and not res.get("error"):
            self._room = room or str(res.get("room", ""))
            self._deck = payload["deck"]
            self._is_host = True
            self._session = str(res.get("session", "") or self._session)
            self._opponent = str(res.get("opponent", "") or "")
            self.last_error = ""
        else:
            self.last_error = self._err(res, "建房失败")
        return res

    def join(self, room: str, deck: str = "", server: str = "", password: str = "",
             mode: str = "") -> dict:
        """Join an existing room hosted by a real player (or the bridge)."""
        payload = self._room_payload(room, deck, server, password, mode, None)
        res = self._post("/join", payload, timeout=max(self.timeout, 12.0))
        if res.get("ok") and not res.get("error"):
            self._room = room
            self._deck = payload["deck"]
            self._is_host = False
            self._session = str(res.get("session", "") or self._session)
            self._opponent = str(res.get("opponent", "") or self._opponent)
            self.last_error = ""
        else:
            self.last_error = self._err(res, "进房失败")
        res.setdefault("opponent", self._opponent)
        return res

    def ready(self, ready: bool = True) -> bool:
        res = self._post("/ready", {"ready": bool(ready)})
        if res.get("ok") and not res.get("error") and res.get("ready", True) is not False:
            self.last_error = ""
            return True
        self.last_error = self._err(res, "准备失败")
        return False

    def start_duel(self) -> dict:
        """Host starts the duel once both seats are ready."""
        res = self._post("/start", {}, timeout=max(self.timeout, 10.0))
        if not (res.get("ok") and not res.get("error")):
            self.last_error = self._err(res, "开始失败")
        return res

    def leave(self) -> dict:
        res = self._post("/leave", {})
        if res.get("ok"):
            self._room = ""
            self._session = ""
            self._is_host = False
            self.duel_active = False
            self.cached_snapshot = {}
        return res

    def surrender(self) -> dict:
        return self._post("/surrender", {})

    def chat(self, message: str) -> dict:
        return self._post("/chat", {"message": message})

    # ---- info --------------------------------------------------------

    @property
    def opponent(self) -> str:
        return self._opponent

    @property
    def room(self) -> str:
        return self._room

    @property
    def deck(self) -> str:
        return self._deck

    @property
    def is_host(self) -> bool:
        return self._is_host
