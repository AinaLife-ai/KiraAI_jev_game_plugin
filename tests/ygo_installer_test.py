"""Plugin-side bridge installer test: status / start / health / stop.

Uses an already-installed bridge directory (produced by
plugin/scripts/install_ygo_bridge.sh, verified separately). Set
YGO_TEST_BRIDGE_DIR to that directory; defaults to /tmp/kira_install_test.
Skips cleanly when the directory is absent.

Run: python3 tests/ygo_installer_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kira_stubs  # noqa: E402
kira_stubs.install()

from plugin.main import KiraGameAgentPlugin  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


async def main():
    install_dir = Path(os.environ.get("YGO_TEST_BRIDGE_DIR", "/tmp/kira_install_test"))
    if not (install_dir / "run_bridge.sh").exists():
        print(f"SKIP: no installed bridge at {install_dir}")
        return 0
    plugin = KiraGameAgentPlugin(SimpleNamespace(config={}, plugin_mgr=None), {
        "section_jev": {"backend": "jev", "api_key": ""},
        "section_ygo": {"ygo_host": "127.0.0.1", "ygo_port": 5274},
    })
    installer = plugin.ygo_installer
    installer.dir = install_dir

    status = installer.status()
    check("status fields complete", all(k in status for k in
          ("state", "installed", "bridge_running", "bridge_ok", "script_present", "dir")))
    check("installed detected", status["installed"] is True)
    check("install script shipped with plugin", status["script_present"] is True)
    check("bridge initially stopped", status["bridge_running"] is False)

    started = await installer.start_bridge()
    check("bridge starts healthy", started.get("healthy") is True, str(started)[:240])
    again = await installer.start_bridge()
    check("second start is idempotent", again.get("ok") is True, str(again)[:160])
    check("status reports running", installer.status()["bridge_running"] is True)
    stopped = installer.stop_bridge()
    check("bridge stops", stopped.get("ok") is True, str(stopped)[:160])
    check("status reports stopped", installer.status()["bridge_running"] is False)
    stopped_again = installer.stop_bridge()
    check("stop when stopped is safe", stopped_again.get("ok") is True)

    installer.root = Path("/nonexistent-plugin-root")
    ok, message = installer.install()
    check("missing install script reported", ok is False and "安装脚本缺失" in message, message)
    print(f"\n== {PASS} passed, {FAIL} failed ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
