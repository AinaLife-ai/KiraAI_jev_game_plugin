"""One-click install / launch of the real YGO bridge (ProjectIgnis/windbot).

The heavy lifting lives in the two shell scripts shipped with the plugin
(`plugin/scripts/install_ygo_bridge.{sh,ps1}`): clone upstream, apply the
patch set, build with .NET 8, download cards.cdb. This class only runs them
in the background, captures logs, and starts/stops the resulting bridge —
nothing is downloaded or built until the user explicitly asks.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger("kira_game_agent.ygo_installer")


class YgoBridgeInstaller:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.root = Path(__file__).resolve().parent
        self.dir = Path.home() / "KiraYgoBridge"
        self.state = "idle"           # idle | installing | ready | error
        self.error = ""
        self.logs: deque[str] = deque(maxlen=120)
        self._install_thread: Optional[threading.Thread] = None
        self._bridge_proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # ---- helpers -----------------------------------------------------------

    def _log(self, line: str) -> None:
        line = line.rstrip()
        if line:
            self.logs.append(line)
            log.info("[ygo-install] %s", line)

    @property
    def _script(self) -> Path:
        return self.root / "scripts" / ("install_ygo_bridge.ps1" if os.name == "nt" else "install_ygo_bridge.sh")

    def _run_script_path(self) -> Path:
        return self.dir / ("run_bridge.ps1" if os.name == "nt" else "run_bridge.sh")

    def _command(self, script: Path, *args: str) -> list[str]:
        if os.name == "nt":
            return ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
        return ["sh", str(script), *args]

    def _health(self) -> bool:
        try:
            import requests
            port = int(self.plugin.ygo_cfg.get("ygo_port", 5274) or 5274)
            host = self.plugin.ygo_cfg.get("ygo_host", "127.0.0.1") or "127.0.0.1"
            r = requests.get(f"http://{host}:{port}/health", timeout=2)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> dict:
        running = self._bridge_proc is not None and self._bridge_proc.poll() is None
        return {
            "state": self.state,
            "error": self.error,
            "log": list(self.logs)[-25:],
            "dir": str(self.dir),
            "installed": self._run_script_path().exists(),
            "bridge_running": running,
            "bridge_ok": self._health() if running else False,
            "script_present": self._script.exists(),
        }

    # ---- install -----------------------------------------------------------

    def install(self) -> tuple[bool, str]:
        with self._lock:
            if self._install_thread and self._install_thread.is_alive():
                return False, "正在安装中，请稍候"
            if not self._script.exists():
                return False, f"安装脚本缺失：{self._script}"
            self.state = "installing"
            self.error = ""
            self.logs.clear()
            self._install_thread = threading.Thread(target=self._run_install, daemon=True)
            self._install_thread.start()
        return True, "开始安装（克隆 windbot → 打补丁 → .NET 8 编译 → 卡库），日志见下方"

    def _run_install(self) -> None:
        try:
            self._log(f"安装目录：{self.dir}")
            cmd = self._command(self._script, str(self.dir))
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(self.root),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self._log(line)
            code = proc.wait()
            if code != 0:
                raise RuntimeError(f"安装脚本退出码 {code}")
            if not self._run_script_path().exists():
                raise RuntimeError("安装脚本结束但未找到启动脚本")
            self.state = "ready"
            self._log("安装完成，可以启动桥")
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = str(e)
            self._log(f"安装失败：{e}")

    # ---- bridge process ----------------------------------------------------

    async def start_bridge(self) -> dict:
        if self._bridge_proc and self._bridge_proc.poll() is None:
            return {"ok": True, "message": "桥已在运行", "healthy": self._health()}
        if not self._run_script_path().exists():
            return {"ok": False, "message": "尚未安装桥，请先一键安装"}
        try:
            logfile = self.dir / "bridge.log"
            self.dir.mkdir(parents=True, exist_ok=True)
            with logfile.open("a", encoding="utf-8") as out:
                self._bridge_proc = subprocess.Popen(
                    self._command(self._run_script_path()),
                    stdout=out, stderr=subprocess.STDOUT,
                    cwd=str(self.dir),
                )
            self._log("桥进程已启动，等待健康检查…")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"启动失败：{e}"}
        healthy = await asyncio.to_thread(self._wait_healthy, 45.0)
        return {
            "ok": healthy,
            "message": "桥已就绪（插件配置 127.0.0.1:5274 即可玩）" if healthy
                       else f"桥未通过健康检查，请查看 {logfile}（可能是 .NET/卡库/端口问题）",
            "healthy": healthy,
        }

    def _wait_healthy(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._bridge_proc and self._bridge_proc.poll() is not None:
                return False
            if self._health():
                return True
            time.sleep(1.0)
        return self._health()

    def stop_bridge(self) -> dict:
        proc = self._bridge_proc
        self._bridge_proc = None
        if proc is None:
            return {"ok": True, "message": "桥未在运行（若是手动启动的进程，请自行关闭）"}
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"ok": True, "message": "桥已停止"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"停止失败：{e}"}
