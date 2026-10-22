"""Notify router: per-session rate-limited async queue.

Rules (user requirements):
- The session that issued the command gets the notification by default
  (no need to pre-configure a whitelist).
- Extra sids (notify_sids) are optional additions on top.
- Never block the decision loop: enqueue is non-blocking, bounded queue
  drops oldest on overflow, worker sends with timeout.
- Per (sid, kind) minimum interval to avoid spam.

Pure routing logic (_route) is unit-testable offline; the worker uses
asyncio.Queue and a framework-agnostic send callable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("kira_game_agent.notify")

SendFn = Callable[[str, str], Awaitable[None]]  # (sid, text) -> awaitable


class NotifyRouter:
    def __init__(
        self,
        send: SendFn,
        *,
        min_interval: float = 30.0,
        extra_sids: Optional[list[str]] = None,
        default_session: bool = True,
        queue_size: int = 200,
        send_timeout: float = 10.0,
    ) -> None:
        self.send = send
        self.min_interval = min_interval
        self.extra_sids = list(extra_sids or [])
        self.default_session = default_session
        self.queue_size = queue_size
        self.send_timeout = send_timeout
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._last: dict[tuple[str, str], float] = {}  # (sid, kind) -> ts
        self._worker: Optional[asyncio.Task] = None
        self.dropped = 0
        self.sent = 0

    # ---- lifecycle -------------------------------------------------

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # ---- routing (pure, testable) ----------------------------------

    def _route(self, engine_id: str, kind: str, bound_sids: list[str]) -> list[str]:
        """Decide which sids get this notification, applying rate limit.

        Returns the sids to send to right now.
        """
        targets: list[str] = []
        if self.default_session:
            targets += bound_sids
        for sid in self.extra_sids:
            if sid not in targets:
                targets.append(sid)
        now = time.monotonic()
        out: list[str] = []
        for sid in targets:
            key = (sid, kind)
            last = self._last.get(key, 0.0)
            if now - last >= self.min_interval:
                self._last[key] = now
                out.append(sid)
        return out

    # ---- enqueue (never blocks the caller) -------------------------

    def enqueue(self, engine_id: str, kind: str, text: str, bound_sids: list[str]) -> None:
        targets = self._route(engine_id, kind, bound_sids)
        for sid in targets:
            if self._queue.full():
                # Drop oldest to keep the loop unblocked.
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                self._queue.put_nowait((sid, text))
            except asyncio.QueueFull:
                self.dropped += 1

    # ---- worker -----------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            sid, text = await self._queue.get()
            try:
                await asyncio.wait_for(self.send(sid, text), timeout=self.send_timeout)
                self.sent += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("notify to %s failed: %s", sid, e)
            finally:
                self._queue.task_done()

    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "sent": self.sent,
            "dropped": self.dropped,
            "default_session": self.default_session,
            "extra_sids": len(self.extra_sids),
        }
