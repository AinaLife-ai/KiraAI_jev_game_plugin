"""Auto-install & configure the Laya local decision backend.

Goal (user requirement): the moment the plugin loads, it should quietly
provision laya in the background — pip install + model download — WITHOUT
blocking plugin startup or the decision loop. First real decision with the
laya backend should already find a ready model.

Runs as a background asyncio task so startup stays instant. All heavy work
(pip, model download) happens in a thread. Fully optional: if the user
prefers Jev only, autoinstall can be disabled in config.

Logic is structured to be testable offline (inject a fake installer).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Optional

log = logging.getLogger("kira_game_agent.autoinstall")

DEFAULT_MODEL_ID = "convaiinnovations/laya"


def pip_install_laya() -> tuple[bool, str]:
    """pip install laya (quiet). Returns (ok, log_or_err)."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "laya"],
            capture_output=True, text=True, timeout=600,
            env=dict(os.environ, PIP_NO_INPUT="1"),
        )
        if r.returncode == 0:
            return True, r.stdout[-300:]
        return False, (r.stdout + r.stderr)[-300:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def download_laya_model(
    model_id: str, subfolder: str, mirror: str,
) -> tuple[bool, str]:
    """Trigger HF snapshot download so first decision doesn't block."""
    if mirror:
        os.environ["HF_ENDPOINT"] = mirror
    try:
        from huggingface_hub import snapshot_download
        prefix = f"{subfolder}/" if subfolder and subfolder != "english" else ""
        allowed = [prefix + n for n in (
            "rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*",
        )]
        path = snapshot_download(model_id, allow_patterns=allowed)
        return True, str(path)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


class LayaAutoInstaller:
    """Runs laya provisioning in the background, once, non-blocking."""

    def __init__(
        self,
        enabled: bool = True,
        model_id: str = DEFAULT_MODEL_ID,
        subfolder: str = "multilingual",
        mirror: str = "",
        local_model_path: str = "",
        pip_installer=pip_install_laya,
        downloader=download_laya_model,
    ) -> None:
        self.enabled = enabled
        self.model_id = model_id
        self.subfolder = subfolder
        self.mirror = mirror
        self.local_model_path = local_model_path
        self._pip_installer = pip_installer
        self._downloader = downloader
        self._done = False
        self._started = False
        self._task: Optional[asyncio.Task] = None
        self.status = {"state": "idle", "msg": "", "ok": False, "steps": []}

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ---- background provisioning ------------------------------------

    async def _run(self) -> None:
        try:
            self.status.update(state="installing", msg="检查 laya…")
            # 1. skip if already importable
            try:
                import laya  # noqa: F401
                self.status.update(state="ready", ok=True, msg="laya 已安装")
                self._append("已存在 laya，跳过 pip")
            except ImportError:
                self._append("未检测到 laya，后台 pip 安装…")
                ok, err = await asyncio.to_thread(self._pip_installer)
                if not ok:
                    self.status.update(state="error", msg=f"pip 安装失败: {err}")
                    self._append(f"pip 失败: {err}")
                    return
                self._append("pip 安装完成")

            # 2. download model, unless a local path is configured
            if self.local_model_path:
                self.status.update(state="ready", ok=True, msg=f"使用本地模型 {self.local_model_path}")
                self._append(f"使用本地模型: {self.local_model_path}")
            else:
                self.status.update(state="download", msg="下载模型…")
                self._append(f"后台下载模型 {self.model_id}/{self.subfolder or 'english'} …")
                ok, path_or_err = await asyncio.to_thread(
                    self._downloader, self.model_id, self.subfolder, self.mirror
                )
                if ok:
                    self.status.update(state="ready", ok=True, msg="模型已下载")
                    self._append(f"模型已下载: {path_or_err}")
                else:
                    self.status.update(state="error", msg=f"模型下载失败: {path_or_err}")
                    self._append(f"模型下载失败: {path_or_err}")
        except asyncio.CancelledError:
            self.status.update(state="cancelled", msg="后台安装已取消")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("laya autoinstall error: %s", e)
            self.status.update(state="error", msg=str(e))
        finally:
            self._done = True

    def _append(self, step: str) -> None:
        self.status.setdefault("steps", []).append(step)
        log.info("[laya autoinstall] %s", step)

    # ---- query -------------------------------------------------------

    def is_ready(self) -> bool:
        return self.status.get("ok") is True

    def summary(self) -> dict:
        return dict(self.status)