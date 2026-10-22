"""TypeSafe System One (Jev) client — async double-buffered, usage-metered.

Pattern from TerraBlind's JevCombat: fire request in a background thread,
the caller drains the latest completed answer on its next tick. Never blocks
the decision loop.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

import requests

log = logging.getLogger("kira_game_agent.jev")

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 5.0


class JevError(Exception):
    pass


class JevClient:
    """One state, many parallel questions, one round-trip. Fan-out friendly."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        base_url: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "") or ""
        self.model = model
        self.timeout = timeout
        # base_url lets us point at a self-hosted / mock compatible endpoint.
        # Accepts a bare host (http://127.0.0.1:17889), a host+prefix
        # (https://api.futureppo.top/v1), or the full systemone path; we
        # normalise to the full endpoint URL.
        self.base_url = self._normalise_base_url(base_url)
        self._pending: Optional[str] = None
        self._busy = False
        self._lock = threading.Lock()
        self._http = requests.Session()
        self._http.headers.update(
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        )
        self.stats = {
            "sent": 0,
            "failed": 0,
            "input_tokens": 0,
            "last_ms": 0,
            "last_error": None,
        }

    @staticmethod
    def _normalise_base_url(base_url: Optional[str]) -> str:
        """Normalise user-supplied base URL to the full /v1/systemone endpoint.

        Handles: bare host (api.futureppo.top), host without scheme
        (futureppo.top/v1), host+/v1 prefix, or the full systemone path.
        """
        if not base_url:
            return API_URL
        b = base_url.strip().rstrip("/")
        if not b:
            return API_URL
        # Auto-prepend https:// when no scheme is present.
        if "://" not in b:
            b = "https://" + b
        if b.endswith("/systemone"):
            return b
        if b.endswith("/v1"):
            return b + "/systemone"
        return b + "/v1/systemone"

    @property
    def api_key(self) -> str:
        return self._api_key

    @api_key.setter
    def api_key(self, value: str) -> None:
        self._api_key = value or ""
        self._http.headers.update({"Authorization": f"Bearer {self._api_key}"})

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    # ---- async double-buffer --------------------------------------

    def ask_async(self, state: Any, questions: dict) -> None:
        """Fire one background request. Caller drains via ask_latest()."""
        if self._busy or not self.has_key:
            return
        with self._lock:
            if self._busy:
                return
            self._busy = True
        body = {"model": self.model, "state": state, "questions": questions}

        def worker() -> None:
            sw = time.monotonic()
            try:
                r = self._http.post(self.base_url, json=body, timeout=self.timeout)
                ms = int((time.monotonic() - sw) * 1000)
                self.stats["last_ms"] = ms
                if r.status_code != 200:
                    self.stats["failed"] += 1
                    self.stats["last_error"] = f"HTTP {r.status_code}: {r.text[:200]}"
                    log.warning("Jev HTTP %s: %.200s", r.status_code, r.text)
                else:
                    self.stats["sent"] += 1
                    self.stats["last_error"] = None
                    payload = r.json()
                    usage = payload.get("usage", {})
                    self.stats["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
                    self._pending = json.dumps(payload, ensure_ascii=False)
            except requests.RequestException as e:
                self.stats["failed"] += 1
                self.stats["last_error"] = str(e)
                log.warning("Jev request failed: %s", e)
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    def ask_latest(self) -> Optional[dict]:
        """Return the most recent completed answer (drain), or None."""
        p = self._pending
        if p is None:
            return None
        self._pending = None
        try:
            return json.loads(p)
        except json.JSONDecodeError:
            return None

    # ---- sync (one-off / tool path) -------------------------------

    def ask_sync(self, state: Any, questions: dict) -> dict:
        body = {"model": self.model, "state": state, "questions": questions}
        try:
            r = self._http.post(self.base_url, json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise JevError(f"request failed: {e}") from e
        if r.status_code != 200:
            raise JevError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
