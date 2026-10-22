"""Adapter registry: pick the right adapter for the configured game.

Adapter contract (all adapters implement these):
    name: str
    start() -> bool
    stop() -> None
    health() -> bool
    get_state() -> dict      # compact snapshot (may be {} if not ready)
    act(steps: list[dict]) -> bool

Plus optional: command(path, payload), get(path), on_event(callback)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .connector import GameBridge

log = logging.getLogger("kira_game_agent.bridge.registry")

ADAPTERS = {
    "pokemon_pyboy": "kira_game_agent.plugin.bridge.adapters.pokemon_pyboy",
    "mgba_http": None,          # TODO: wrap mGBA-http REST in a thin adapter
    "ygo_engine": None,         # TODO: wrap ygoenv/EDOPro headless
    "terrablind_http": None,    # GameBridge directly with terrablind paths
    "generic_http": None,       # GameBridge directly
}


def create_adapter(game: str, cfg: dict) -> Optional[Any]:
    """Build the active adapter from config. Returns None for 'none'."""
    if game == "none":
        return None
    if game in ("terrablind_http", "generic_http"):
        # HTTP-family: reuse GameBridge (TerraBlind and generic share the
        # /health + /state + /act protocol).
        host = cfg.get("host", "127.0.0.1")
        port = int(cfg.get("port", 17878))
        bridge = GameBridge(host=host, port=port, auto_reconnect=bool(cfg.get("auto_reconnect", True)))
        return _HttpAdapter(game, bridge)

    if game == "mgba_http":
        try:
            from .adapters.mgba_socket import MgbaSocketAdapter
        except ImportError as e:
            log.error("adapter import failed: %s", e)
            return None
        return MgbaSocketAdapter(
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg.get("mgba_port", cfg.get("port", 9999)) or 9999),
            game_version=cfg.get("game_version", "firered"),
        )

    if game == "ygo_engine":
        try:
            from .adapters.ygo_engine import YgoEngineAdapter
        except ImportError as e:
            log.error("adapter import failed: %s", e)
            return None
        return YgoEngineAdapter(
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg.get("port", 5274) or 5274),
            game_mode=cfg.get("ygo_mode", "single"),
            server_url=cfg.get("ygo_server", ""),
        )

    if game == "pokemon_pyboy":
        try:
            from .adapters.pokemon_pyboy import PokemonPyBoyAdapter
        except ImportError as e:
            log.error("adapter import failed: %s", e)
            return None
        return PokemonPyBoyAdapter(rom=cfg.get("rom", ""))

    log.warning("unknown game adapter: %s", game)
    return None


class _HttpAdapter:
    """Thin wrapper exposing the adapter contract over GameBridge."""

    def __init__(self, game: str, bridge: GameBridge) -> None:
        self.name = game
        self.bridge = bridge

    def start(self) -> bool:
        return self.bridge.connect()

    def stop(self) -> None:
        self.bridge.connected = False

    def health(self) -> bool:
        return self.bridge.connected or self.bridge.tick()

    def get_state(self) -> dict:
        try:
            return self.bridge.get_state()
        except Exception:  # noqa: BLE001
            return {}

    def act(self, steps: list[dict]) -> bool:
        try:
            self.bridge.act(steps)
            return True
        except Exception:  # noqa: BLE001
            return False

    def command(self, path: str, payload: Optional[dict] = None) -> dict:
        return self.bridge.command(path, payload)

    def get(self, path: str) -> dict:
        return self.bridge.get(path)
