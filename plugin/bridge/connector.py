"""Game bridge: connection management + generic HTTP protocol.

Supports:
- generic_http : any service exposing GET /state, POST /act, GET /health
- terrablind   : TerraBlind mod on 127.0.0.1:17878 (same protocol)
- mgba_http    : mGBA-http wrapper (Twitch Plays style HTTP bridge)
- ygo_engine   : YGOPro core engine bridge (headless, network duels)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger("kira_game_agent.bridge")

DEFAULT_HOST = "127.0.0.1"
PROBE_PORTS = [17878, 17879, 5000, 8080, 18080, 5274]
PROBE_TIMEOUT = 0.5
STATE_TIMEOUT = 5.0
ACT_TIMEOUT = 10.0


class BridgeError(Exception):
    pass


class GameBridge:
    """HTTP game bridge with auto-discovery and backoff reconnect."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = 17878,
        auto_reconnect: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.auto_reconnect = auto_reconnect
        self._base = f"http://{host}:{port}"
        self._session = requests.Session()
        self.connected = False
        self.last_error: Optional[str] = None
        self._next_retry = 0.0
        self._backoff = 0.5
        self.last_state: dict[str, Any] = {}
        self.state_ts = 0.0

    # ---- lifecycle -------------------------------------------------

    def connect(self) -> bool:
        if self._probe(self._base):
            self.connected = True
            self._backoff = 0.5
            log.info("bridge connected: %s", self._base)
            return True
        # Auto-discovery over common ports (only when port was default/0).
        candidates: list[int] = []
        if self.port in PROBE_PORTS or self.port == 0:
            candidates = [p for p in PROBE_PORTS if p != self.port]
        else:
            candidates = [p for p in PROBE_PORTS if p != self.port]
        for p in candidates:
            base = f"http://{self.host}:{p}"
            if self._probe(base):
                self._base = base
                self.port = p
                self.connected = True
                self._backoff = 0.5
                log.info("bridge discovered on %s", base)
                return True
        self.connected = False
        self.last_error = "no game bridge reachable"
        return False

    def _probe(self, base: str) -> bool:
        try:
            r = self._session.get(f"{base}/health", timeout=PROBE_TIMEOUT)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def tick(self) -> bool:
        """Call every loop; reconnects with backoff when dropped."""
        if self.connected:
            try:
                r = self._session.get(f"{self._base}/health", timeout=PROBE_TIMEOUT)
                if r.status_code == 200:
                    self._backoff = 0.5
                    return True
            except requests.RequestException:
                pass
            self.connected = False
            self._next_retry = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, 5.0)
            log.warning("bridge lost; reconnect in %.1fs", self._backoff)
            return False
        if self.auto_reconnect and time.monotonic() >= self._next_retry:
            if self.connect():
                return True
            self._next_retry = time.monotonic() + self._backoff
        return False

    # ---- protocol --------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        if not self.connected:
            raise BridgeError("bridge not connected")
        r = self._session.get(f"{self._base}/state", timeout=STATE_TIMEOUT)
        if r.status_code != 200:
            raise BridgeError(f"state HTTP {r.status_code}")
        data = r.json()
        self.last_state = data
        self.state_ts = time.time()
        return data

    def act(self, steps: list[dict]) -> dict:
        """POST /act {"steps":[...]} — TerraBlind-style primitive execution."""
        if not self.connected:
            raise BridgeError("bridge not connected")
        r = self._session.post(f"{self._base}/act", json={"steps": steps}, timeout=ACT_TIMEOUT)
        if r.status_code != 200:
            raise BridgeError(f"act HTTP {r.status_code}")
        return r.json()

    def command(self, path: str, payload: Optional[dict] = None) -> dict:
        """Generic POST to a bridge endpoint (ygo/start/pause/etc)."""
        if not self.connected:
            raise BridgeError("bridge not connected")
        r = self._session.post(f"{self._base}{path}", json=payload or {}, timeout=ACT_TIMEOUT)
        if r.status_code != 200:
            raise BridgeError(f"{path} HTTP {r.status_code}")
        return r.json()

    def get(self, path: str) -> dict:
        if not self.connected:
            raise BridgeError("bridge not connected")
        r = self._session.get(f"{self._base}{path}", timeout=STATE_TIMEOUT)
        if r.status_code != 200:
            raise BridgeError(f"{path} HTTP {r.status_code}")
        return r.json()
