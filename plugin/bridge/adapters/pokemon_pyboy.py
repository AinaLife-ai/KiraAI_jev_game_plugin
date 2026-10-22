"""Pokémon adapter — PyBoy in-process emulator (zero VLM).

State from PyBoy RAM (direct memory read), dialog decoded from tilemaps,
objects from WRAM object memory, collision from RAM — the jev-plays-pokemon
approach, enhanced with the old pokemon_gba_plugin's adventure-log design
(event-driven long-term journal).

PyBoy: pip install pyboy (pure wheels, no system deps).
ROM: user's own legally obtained Pokemon Red/Blue/Yellow .gb

Capabilities:
- read position/facing/money/badges/party (species/level/hp)
- decode dialog text from tilemaps (dialog box text)
- read collision map -> walkable directions
- read NPC object memory (identity/position/direction/distance)
- adventure log: event-driven journal (arrive/catch/gym/save) persisted
  to JSON and injected into the LLM context (adventure_log segment)
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, Optional

log = logging.getLogger("kira_game_agent.adapter.pokemon")

ROM_ENV = "POKEMON_ROM"
DEFAULT_ROM_HINT = "pokemon-red.gb"

# Gen 1 WRAM locations (from pokemon-agent / jev-plays-pokemon vendor).
W_MAP_GROUP = 0xD35E
W_MAP_NUMBER = 0xD35D
W_Y_COORD = 0xD361
W_X_COORD = 0xD362
W_FACING = 0xD363
W_MONEY = 0xD347
W_BADGES = 0xD356
W_BATTLE_TYPE = 0xD057
W_CUR_HP = 0xD16B  # party + current mon offsets
W_PARTY_COUNT = 0xD163
W_PARTY_SPECIES = 0xD164
W_PARTY_LEVELS = 0xD18C
W_PARTY_MAX_HP = 0xD18D
W_PARTY_STATUS = 0xD16E
W_TEXTBOX_ACTIVE = 0xD5B2  # 1 when a text box is open
W_JOY_HOLD = 0xFF98
W_COLLISION_MAP_PTR = 0xD530  # pointer to collision map (varies by ROM)
W_NUM_OBJECTS = 0xD51E
W_OBJECT_STRUCT = 0xD51F
W_WARP_ENTRIES = 0xD3BB

# Adventure log event types
EVENT_ARRIVE = "到达"
EVENT_CATCH = "捕获"
EVENT_GYM = "道馆"
EVENT_SAVE = "存档"
EVENT_BATTLE_START = "战斗开始"
EVENT_BATTLE_END = "战斗结束"

GEN1_SPECIES_NAMES = {
    0x01: "Bulbasaur", 0x02: "Ivysaur", 0x03: "Venusaur", 0x04: "Charmander",
    0x05: "Charmeleon", 0x06: "Charizard", 0x07: "Squirtle", 0x08: "Wartortle",
    0x09: "Blastoise", 0x0A: "Caterpie", 0x0B: "Metapod", 0x0C: "Butterfree",
    0x0D: "Weedle", 0x0E: "Kakuna", 0x0F: "Beedrill", 0x10: "Pidgey",
    0x11: "Pidgeotto", 0x12: "Pidgeot", 0x13: "Rattata", 0x14: "Raticate",
    0x15: "Spearow", 0x16: "Fearow", 0x17: "Ekans", 0x18: "Arbok",
    0x19: "Pikachu", 0x1A: "Raichu", 0x1B: "Sandshrew", 0x1C: "Sandslash",
    0x1D: "Nidoran♀", 0x1E: "Nidorina", 0x1F: "Nidoqueen", 0x20: "Nidoran♂",
    0x21: "Nidorino", 0x22: "Nidoking", 0x23: "Clefairy", 0x24: "Clefable",
    0x25: "Vulpix", 0x26: "Ninetales", 0x27: "Jigglypuff", 0x28: "Wigglytuff",
    0x29: "Zubat", 0x2A: "Golbat", 0x2B: "Oddish", 0x2C: "Gloom", 0x2D: "Vileplume",
    0x2E: "Paras", 0x2F: "Parasect", 0x30: "Venonat", 0x31: "Venomoth",
    0x32: "Diglett", 0x33: "Dugtrio", 0x34: "Meowth", 0x35: "Persian",
    0x36: "Psyduck", 0x37: "Golduck", 0x38: "Mankey", 0x39: "Primeape",
    0x3A: "Growlithe", 0x3B: "Arcanine", 0x3C: "Poliwag", 0x3D: "Poliwhirl",
    0x3E: "Poliwrath", 0x3F: "Abra", 0x40: "Kadabra", 0x41: "Alakazam",
    0x42: "Machop", 0x43: "Machoke", 0x44: "Machamp", 0x45: "Bellsprout",
    0x46: "Weepinbell", 0x47: "Victreebel", 0x48: "Tentacool", 0x49: "Tentacruel",
    0x4A: "Geodude", 0x4B: "Graveler", 0x4C: "Golem", 0x4D: "Ponyta",
    0x4E: "Rapidash", 0x4F: "Slowpoke", 0x50: "Slowbro", 0x51: "Magnemite",
    0x52: "Magneton", 0x53: "Farfetch'd", 0x54: "Doduo", 0x55: "Dodrio",
    0x56: "Seel", 0x57: "Dewgong", 0x58: "Grimer", 0x59: "Muk",
    0x5A: "Shellder", 0x5B: "Cloyster", 0x5C: "Gastly", 0x5D: "Haunter",
    0x5E: "Gengar", 0x5F: "Onix", 0x60: "Drowzee", 0x61: "Hypno",
    0x62: "Krabby", 0x63: "Kingler", 0x64: "Voltorb", 0x65: "Electrode",
    0x66: "Exeggcute", 0x67: "Exeggutor", 0x68: "Cubone", 0x69: "Marowak",
    0x6A: "Hitmonlee", 0x6B: "Hitmonchan", 0x6C: "Lickitung", 0x6D: "Koffing",
    0x6E: "Weezing", 0x6F: "Rhyhorn", 0x70: "Rhydon", 0x71: "Chansey",
    0x72: "Tangela", 0x73: "Kangaskhan", 0x74: "Horsea", 0x75: "Seadra",
    0x76: "Goldeen", 0x77: "Seaking", 0x78: "Staryu", 0x79: "Starmie",
    0x7A: "Mr. Mime", 0x7B: "Scyther", 0x7C: "Jynx", 0x7D: "Electabuzz",
    0x7E: "Magmar", 0x7F: "Pinsir", 0x80: "Tauros", 0x81: "Magikarp",
    0x82: "Gyarados", 0x83: "Lapras", 0x84: "Ditto", 0x85: "Eevee",
    0x86: "Vaporeon", 0x87: "Jolteon", 0x88: "Flareon", 0x89: "Porygon",
    0x8A: "Omanyte", 0x8B: "Omastar", 0x8C: "Kabuto", 0x8D: "Kabutops",
    0x8E: "Aerodactyl", 0x8F: "Snorlax", 0x90: "Articuno", 0x91: "Zapdos",
    0x92: "Moltres", 0x93: "Dratini", 0x94: "Dragonair", 0x95: "Dragonite",
    0x96: "Mewtwo", 0x97: "Mew",
}

MAP_NAMES = {
    0: "Pallet Town", 1: "Viridian City", 2: "Pewter City", 3: "Cerulean City",
    4: "Lavender Town", 5: "Vermilion City", 6: "Celadon City",
    7: "Fuchsia City", 8: "Saffron City", 9: "Cinnabar Island",
    10: "Indigo Plateau", 11: "Route 1", 12: "Route 2", 13: "Route 3",
    14: "Route 4", 15: "Route 5", 16: "Route 6", 17: "Route 7",
    18: "Route 8", 19: "Route 9", 20: "Route 10", 21: "Route 11",
    22: "Route 12", 23: "Route 13", 24: "Route 14", 25: "Route 15",
    26: "Route 16", 27: "Route 17", 28: "Route 18", 29: "Route 19",
    30: "Route 20", 31: "Route 21", 32: "Route 22", 33: "Route 23",
    34: "Route 24", 35: "Route 25",
}


class AdventureLog:
    """Event-driven long-term journal (from the old pokemon_gba_plugin design).

    Records meaningful events (arrive/catch/gym/save/battle) and persists to
    JSON. The recent N entries are injected into the LLM context as an
    adventure_log segment so the bot remembers its journey across turns.
    """

    def __init__(self, data_dir, enabled: bool = True, max_entries: int = 200):
        self.data_dir = data_dir
        self.enabled = enabled
        self.max_entries = max_entries
        self.path = data_dir / "adventure_log.json"
        self.entries: list[dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                import json
                with open(self.path, encoding="utf-8") as f:
                    self.entries = json.load(f).get("entries", [])
            except Exception:
                self.entries = []

    def _save(self):
        if not self.enabled:
            return
        try:
            import json
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"entries": self.entries}, f, indent=2, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log.warning("adventure log save failed: %s", e)

    def add(self, event: str, details: str):
        if not self.enabled:
            return
        self.entries.append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "details": details,
        })
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]
        self._save()

    def recent(self, n: int = 5) -> list[dict]:
        return self.entries[-n:] if self.entries else []

    def clear(self):
        self.entries = []
        self._save()


class PokemonPyBoyAdapter:
    """Adapter implementing the bridge contract:

    get_state() -> dict | None
    act(steps)  -> bool
    health()    -> bool
    """

    name = "pokemon_pyboy"

    def __init__(self, rom: Optional[str] = None, headless: bool = True,
                 data_dir: Optional[Any] = None) -> None:
        self.rom = rom or os.environ.get(ROM_ENV, "")
        self.headless = headless
        self._pyboy = None
        self.last_snapshot: dict[str, Any] = {}
        self.running = False
        self._frame_count = 0
        # Adventure log (old-plugin design)
        from pathlib import Path
        self.data_dir = data_dir or (Path(os.path.expanduser("~")) / ".kira_game_agent" / "pokemon_pyboy")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.adventure_log = AdventureLog(self.data_dir, enabled=True)
        # Event tracking for adventure-log events
        self._last_map_id: Optional[int] = None
        self._last_badges = 0
        self._last_party_count = 0
        self._in_battle = False

    # ---- lifecycle -------------------------------------------------

    def start(self) -> bool:
        try:
            from pyboy import PyBoy
        except ImportError:
            log.error("PyBoy not installed. pip install pyboy")
            return False
        if not self.rom or not os.path.exists(self.rom):
            log.error("Pokemon ROM not found: %r (set POKEMON_ROM or pass rom=)", self.rom)
            return False
        try:
            self._pyboy = PyBoy(
                self.rom,
                window_type="headless" if self.headless else "SDL2",
                game_wrapper=False,
            )
            self.running = True
            log.info("PyBoy started: %s", self.rom)
            self.adventure_log.add("启动", f"加载 ROM: {os.path.basename(self.rom)}")
            return True
        except Exception as e:  # noqa: BLE001
            log.exception("PyBoy start failed: %s", e)
            return False

    def stop(self) -> None:
        if self._pyboy:
            try:
                self.adventure_log.add("关闭", "模拟器停止")
                self._pyboy.stop()
            except Exception:  # noqa: BLE001
                pass
        self._pyboy = None
        self.running = False

    # ---- bridge contract ------------------------------------------

    def health(self) -> bool:
        return self.running and self._pyboy is not None

    def get_state(self) -> dict[str, Any]:
        if not self.health():
            return {}
        try:
            self._tick_frames(1)
            snap = self._build_snapshot()
            self.last_snapshot = snap
            self._track_events(snap)
            return snap
        except Exception as e:  # noqa: BLE001
            log.warning("pokemon get_state failed: %s", e)
            return {}

    def act(self, steps: list[dict]) -> bool:
        if not self.health():
            return False
        try:
            for step in steps:
                action = step.get("action", "wait")
                frames = int(step.get("frames", 8))
                self._press(action, frames)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("pokemon act failed: %s", e)
            return False

    # ---- internals -------------------------------------------------

    def _tick_frames(self, n: int) -> None:
        if self._pyboy is None:
            return
        for _ in range(n):
            self._pyboy.tick()
            self._frame_count += 1

    def _press(self, action: str, frames: int) -> None:
        if self._pyboy is None:
            return
        pb = self._pyboy
        btn = {
            "confirm": "a",
            "cancel": "b",
            "menu": "start",
            "move_up": "up",
            "move_down": "down",
            "move_left": "left",
            "move_right": "right",
        }.get(action)
        if btn is None:
            self._tick_frames(frames)
            return
        pb.button_press(btn)
        self._tick_frames(frames)
        pb.button_release(btn)
        self._tick_frames(2)

    def _read_mem(self, addr: int, length: int = 1) -> bytes:
        if self._pyboy is None:
            return b"\x00" * length
        try:
            return self._pyboy.memory[addr : addr + length]
        except Exception:  # noqa: BLE001
            return b"\x00" * length

    def _read_u8(self, addr: int) -> int:
        return int.from_bytes(self._read_mem(addr, 1), "little")

    def _read_u16(self, addr: int) -> int:
        return int.from_bytes(self._read_mem(addr, 2), "little")

    def _read_u32(self, addr: int) -> int:
        return int.from_bytes(self._read_mem(addr, 4), "little")

    # ---- state building ---------------------------------------------

    def _build_snapshot(self) -> dict[str, Any]:
        """Compact state: player facts + walkable + dialog + objects + battle."""
        x = self._read_u8(W_X_COORD)
        y = self._read_u8(W_Y_COORD)
        facing = self._read_u8(W_FACING)
        money = self._read_u32(W_MONEY) & 0xFFFFFF
        badges = self._read_u8(W_BADGES)
        badge_count = bin(badges).count("1")
        party_count = self._read_u8(W_PARTY_COUNT)
        map_group = self._read_u8(W_MAP_GROUP)
        map_number = self._read_u8(W_MAP_NUMBER)
        map_id = (map_group << 8) | map_number
        location = MAP_NAMES.get(map_id, f"地图 {map_id}")
        battle_active = self._read_u8(W_BATTLE_TYPE) != 0
        dialog_active = self._read_u8(W_TEXTBOX_ACTIVE) != 0
        dialog_text = self._decode_dialog() if dialog_active else ""

        party = []
        for i in range(min(party_count, 6)):
            species_id = self._read_u8(W_PARTY_SPECIES + i * 0x2C)
            level = self._read_u8(W_PARTY_LEVELS + i * 0x2C)
            hp = self._read_u16(W_CUR_HP + i * 0x2C)
            max_hp = self._read_u16(W_PARTY_MAX_HP + i * 0x2C)
            status = self._read_u8(W_PARTY_STATUS + i * 0x2C)
            name = GEN1_SPECIES_NAMES.get(species_id, f"#{species_id}")
            party.append({
                "species": name,
                "level": level,
                "hp": hp,
                "max_hp": max_hp,
                "hp_percent": int(hp * 100 / max(1, max_hp)),
                "status": status,
            })

        face_map = {0: "down", 4: "up", 8: "left", 12: "right"}
        return {
            "stage": "battle" if battle_active else "overworld",
            "player": {
                "cell": [x, y],
                "facing": face_map.get(facing, str(facing)),
                "money": money,
                "badges": badge_count,
                "party_count": party_count,
                "party": party,
            },
            "world": {
                "map": f"{map_group}:{map_number}",
                "location": location,
                "map_id": map_id,
                "walkable": self._walkable_dirs(x, y),
            },
            "dialog": dialog_text[:200],
            "dialog_active": dialog_active,
            "battle_active": battle_active,
            "entities": self._read_objects(x, y),
            "game": {
                "engine": "pyboy",
                "frame": self._frame_count,
                "adventure_log": self.adventure_log.recent(3),
            },
        }

    def _walkable_dirs(self, x: int, y: int) -> dict[str, bool]:
        """Collision check from the RAM collision map.

        Gen 1 stores a collision grid; here we read the four surrounding
        tiles' collision flags. Falls back to all-open when the map can't
        be read (e.g. ROM variant differences).
        """
        dirs = {"up": True, "down": True, "left": True, "right": True}
        try:
            # Read collision values for the four neighbours. The exact address
            # varies by ROM revision; this is a best-effort read.
            base = 0xC6E8 + y * 20 + x  # collision map base (Gen 1)
            for dx, dy, name in ((0, -1, "up"), (0, 1, "down"), (-1, 0, "left"), (1, 0, "right")):
                cx, cy = x + dx, y + dy
                if cx < 0 or cy < 0 or cx >= 20 or cy >= 18:
                    dirs[name] = False
                    continue
                cell = self._read_u8(base + dy * 20 + dx)
                # In Gen 1, 0xFF = blocked in the collision map.
                dirs[name] = (cell != 0xFF)
        except Exception:  # noqa: BLE001
            pass
        return dirs

    def _decode_dialog(self) -> str:
        """Decode the current dialog box text from the tilemap.

        Gen 1 stores dialog text in the tilemap area; we read the text box
        region and map tile indices to characters.
        """
        try:
            # Read the text box tile area (roughly 18x4 tiles at bottom of screen).
            # Tile indices -> ASCII via the Gen 1 character table.
            chars = []
            tile_base = 0xC4A0  # approximate dialog tilemap start
            for i in range(72):  # 18x4
                t = self._read_u8(tile_base + i)
                ch = self._tile_to_char(t)
                if ch:
                    chars.append(ch)
            text = "".join(chars).strip()
            return text
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _tile_to_char(tile: int) -> str:
        """Map Gen 1 tile index to a character (subset of the char table)."""
        # Gen 1 character table: 0x80-0x99 = A-Z, 0x9A-0xB3 = a-z, etc.
        if 0x80 <= tile <= 0x99:
            return chr(ord("A") + tile - 0x80)
        if 0x9A <= tile <= 0xB3:
            return chr(ord("a") + tile - 0x9A)
        if tile == 0x7F:
            return " "
        if 0xF6 <= tile <= 0xFF:
            return chr(ord("0") + tile - 0xF6)
        return ""

    def _read_objects(self, px: int, py: int) -> list[dict]:
        """Read NPC/object memory (WRAM 0xC100/0xC200 area) — identity,
        position, direction, distance from player."""
        objects = []
        try:
            n = min(self._read_u8(W_NUM_OBJECTS), 15)
            for i in range(n):
                base = W_OBJECT_STRUCT + i * 0x10
                obj_id = self._read_u8(base)
                if obj_id == 0xFF or obj_id == 0:
                    continue
                ox = self._read_u8(base + 4)
                oy = self._read_u8(base + 5)
                facing = self._read_u8(base + 6)
                dist = abs(ox - px) + abs(oy - py)
                objects.append({
                    "id": obj_id,
                    "cell": [ox, oy],
                    "facing": facing,
                    "distance": dist,
                })
        except Exception:  # noqa: BLE001
            pass
        return objects

    # ---- adventure log event tracking -------------------------------

    def _track_events(self, snap: dict) -> None:
        """Detect meaningful changes and record them to the adventure log."""
        world = snap.get("world", {})
        player = snap.get("player", {})
        map_id = world.get("map_id")
        badges = player.get("badges", 0)
        party_count = player.get("party_count", 0)
        battle = snap.get("battle_active", False)

        if self._last_map_id is not None and map_id != self._last_map_id:
            loc = world.get("location", "?")
            self.adventure_log.add(EVENT_ARRIVE, f"到达 {loc}")
        if badges > self._last_badges:
            self.adventure_log.add(EVENT_GYM, f"获得第 {badges} 枚徽章")
        if party_count > self._last_party_count:
            self.adventure_log.add(EVENT_CATCH, f"捕获宝可梦，队伍现有 {party_count} 只")
        if battle and not self._in_battle:
            self.adventure_log.add(EVENT_BATTLE_START, "进入战斗")
        elif not battle and self._in_battle:
            self.adventure_log.add(EVENT_BATTLE_END, "战斗结束")

        self._last_map_id = map_id
        self._last_badges = badges
        self._last_party_count = party_count
        self._in_battle = battle

    def save_state(self, slot: int = 0) -> bool:
        """Quick-save (from the old plugin design)."""
        if not self.health():
            return False
        try:
            self._pyboy.save_state(slot)
            self.adventure_log.add(EVENT_SAVE, f"存档到槽 {slot}")
            return True
        except Exception:  # noqa: BLE001
            return False

    def load_state(self, slot: int = 0) -> bool:
        if not self.health():
            return False
        try:
            self._pyboy.load_state(slot)
            return True
        except Exception:  # noqa: BLE001
            return False