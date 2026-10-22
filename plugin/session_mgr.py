"""Session <-> engine bindings.

Which chat session started which game engine instance, and which sessions
a game engine should notify. Supports multiple sessions per engine and
multiple engines per session (multi-game parallel).

Pure logic, no framework imports — unit-testable offline.
"""
from __future__ import annotations


class SessionBindings:
    def __init__(self) -> None:
        # sid -> ordered list of engine_ids (most recent last = default)
        self._sid_engines: dict[str, list[str]] = {}
        # engine_id -> set of sids
        self._engine_sids: dict[str, set[str]] = {}

    def bind(self, sid: str, engine_id: str) -> None:
        lst = self._sid_engines.setdefault(sid, [])
        if engine_id in lst:
            lst.remove(engine_id)
        lst.append(engine_id)
        self._engine_sids.setdefault(engine_id, set()).add(sid)

    def unbind_engine(self, engine_id: str) -> None:
        self._engine_sids.pop(engine_id, None)
        for sid, lst in list(self._sid_engines.items()):
            if engine_id in lst:
                lst.remove(engine_id)
            if not lst:
                self._sid_engines.pop(sid, None)

    def engines_for(self, sid: str) -> list[str]:
        return list(self._sid_engines.get(sid, []))

    def default_engine(self, sid: str) -> str | None:
        lst = self._sid_engines.get(sid)
        return lst[-1] if lst else None

    def sids_for(self, engine_id: str) -> list[str]:
        return sorted(self._engine_sids.get(engine_id, set()))

    def all_engines(self) -> list[str]:
        return list(self._engine_sids)
