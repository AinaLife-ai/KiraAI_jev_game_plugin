"""Laya backend — local, zero-cost, open-source replacement for Jev.

Laya (NandhaKishorM/laya, HF convaiinnovations/laya) is a non-autoregressive
System 1 decision engine: same `choice` / `score` / `noul` typed-question API
as TypeSafe Jev, but runs **locally** (Apache 2.0 weights, no API key, $0) and
supports 100+ languages (better CJK than Jev). It is dropped in as a peer
backend to JevClient; the decision loop only cares about `answers[qid]`
having `.choice` / `.score` / `.noul` / `.confidence`.

Interface mirrors JevClient so `DecisionEngine` can use either:
  has_key / model  (informational)
  ask_async(state, questions)   # no-op? Laya is local & synchronous
  ask_latest() -> dict | None
  ask_sync(state, questions) -> dict     # primary path for local inference
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

log = logging.getLogger("kira_game_agent.laya")

DEFAULT_MODEL_ID = "convaiinnovations/laya"
SUB_BY_NAME = {
    "english": "",
    "multilingual": "multilingual",
    "typed-decisions": "typed-decisions",
}


class LayaUnavailableError(Exception):
    pass


class LayaBackend:
    """Local Laya decision backend. Loads the model once, lazy; async-drains
    synchronous inference the same way JevClient's double-buffer does (but
    Laya is ~30ms so ask_sync is the normal path)."""

    name = "laya"
    is_local = True

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        subfolder: str = "",
        device: str = "",          # '' -> auto (cuda if available else cpu)
        hf_mirror: str = "",       # e.g. https://hf-mirror.com for CN
        local_model_path: str = "",  # path to a local checkpoint dir
    ) -> None:
        self.model_id = model_id
        self.subfolder = subfolder
        self.device = device
        self.hf_mirror = hf_mirror
        self.local_model_path = local_model_path
        self._agent: Any = None
        # double-buffer for parity with JevClient
        self._pending: Optional[dict] = None
        self._lock = threading.Lock()
        self.stats = {"sent": 0, "failed": 0, "input_tokens": 0, "last_ms": 0, "last_error": None}
        self._load_error: Optional[str] = None

    # ---- informational / parity ------------------------------------

    @property
    def has_key(self) -> bool:
        """Laya needs no API key — always 'ready' if installed & loadable."""
        return True

    @property
    def model(self) -> str:
        return self.subfolder or self.model_id

    @model.setter
    def model(self, _v: Any) -> None:
        pass  # immutable handle; reload via set_model()

    @property
    def timeout(self) -> float:
        return 0.0  # local inference doesn't time out like a network call

    @timeout.setter
    def timeout(self, _v: Any) -> None:
        pass

    @property
    def api_key(self) -> str:
        return ""

    @api_key.setter
    def api_key(self, _v: Any) -> None:
        pass

    # ---- lifecycle --------------------------------------------------

    def _apply_mirror(self) -> None:
        if self.hf_mirror:
            os.environ["HF_ENDPOINT"] = self.hf_mirror
        if self.hf_mirror:
            os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

    def ensure_loaded(self) -> bool:
        """Load the Laya agent (downloads weights on first run via HF hub)."""
        if self._agent is not None:
            return True
        try:
            import laya  # noqa: F401
        except ImportError:
            self._load_error = "laya 未安装。运行 scripts/install_laya.py 自动安装。"
            log.error("laya not installed — run scripts/install_laya.py")
            return False
        try:
            self._apply_mirror()
            path = self.local_model_path or self.model_id
            sub = SUB_BY_NAME.get(self.subfolder, self.subfolder) or None
            kw: dict[str, Any] = {}
            if sub:
                kw["subfolder"] = sub
            if self.device:
                kw["device"] = self.device
            sw = time.monotonic()
            self._agent = laya.load(path, **kw)
            self.stats["last_ms"] = int((time.monotonic() - sw) * 1000)
            log.info("laya backend loaded: %s (%s) in %.1fs",
                     path, sub or "english", time.monotonic() - sw)
            return True
        except Exception as e:  # noqa: BLE001
            self._load_error = f"laya 加载失败: {e}"
            log.exception("laya load failed: %s", e)
            return False

    def set_model(self, model_id: str = "", subfolder: str = "", device: str = "",
                  local_model_path: str = "") -> None:
        """Hot-reload to a different checkpoint (e.g. multilingual)."""
        if model_id:
            self.model_id = model_id
        if subfolder is not None:
            self.subfolder = subfolder
        if device:
            self.device = device
        if local_model_path is not None:
            self.local_model_path = local_model_path
        self._agent = None

    # ---- invoke -----------------------------------------------------

    def ask_sync(self, state: Any, questions: dict) -> dict:
        """Primary path: local synchronous inference (~30ms)."""
        if not self.ensure_loaded():
            raise LayaUnavailableError(self._load_error or "laya unavailable")
        sw = time.monotonic()
        try:
            res = self._agent.system_one(state, questions)
            self.stats["sent"] += 1
            self.stats["last_ms"] = int((time.monotonic() - sw) * 1000)
            usage = res.get("usage", {})
            self.stats["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            if isinstance(res, dict) and "answers" not in res:
                res = {"answers": res}
            return res
        except Exception as e:  # noqa: BLE001
            self.stats["failed"] += 1
            self.stats["last_error"] = str(e)
            raise LayaUnavailableError(str(e)) from e

    def ask_async(self, state: Any, questions: dict) -> None:
        """Fire local inference in a thread; drain via ask_latest()."""
        if self._agent is None and not self.ensure_loaded():
            return
        with self._lock:
            if self._agent is None:
                return

        def worker() -> None:
            try:
                res = self.ask_sync(state, questions)
                self._pending = res
            except Exception as e:  # noqa: BLE001
                log.warning("laya async failed: %s", e)

        threading.Thread(target=worker, daemon=True).start()

    def ask_latest(self) -> Optional[dict]:
        p = self._pending
        if p is None:
            return None
        self._pending = None
        return p