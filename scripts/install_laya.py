#!/usr/bin/env python3
"""Auto-installer for the Laya local decision backend.

Laya (HF convaiinnovations/laya) is the open-source, zero-cost, multilingual
replacement for TypeSafe Jev. This script:

  1. `pip install laya`  (or already-installed -> skip)
  2. triggers a one-time HuggingFace download of the chosen checkpoint
     (multilingual by default — supports Chinese / CJK) so the first
     in-game decision doesn't block on a download
  3. supports HF mirrors (hf-mirror.com) for CN networks and an offline
     "already downloaded" fast path via local_model_path.

Usage:
    python scripts/install_laya.py                  # install + download multilingual
    python scripts/install_laya.py --subfolder english
    python scripts/install_laya.py --mirror https://hf-mirror.com
    python scripts/install_laya.py --local /path/to/laya-dir
    python scripts/install_laya.py --check          # verify install + loadable model
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def run_pip() -> bool:
    print("==> pip install laya")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "laya"],
        env=dict(os.environ, PIP_NO_INPUT="1"),
    )
    return r.returncode == 0


def download_model(model_id: str, subfolder: str, mirror: str, local_path: str) -> bool:
    """Trigger the HF download so it happens up-front (not on first decision)."""
    if local_path:
        print(f"==> 使用本地模型目录: {local_path}")
        return os.path.isdir(local_path)
    if mirror:
        # snapshot_download honors HF_ENDPOINT on the hub client.
        os.environ["HF_ENDPOINT"] = mirror
        print(f"==> 使用镜像: {mirror}")
    print(f"==> 下载模型: {model_id}  subfolder={subfolder!r}")
    try:
        from huggingface_hub import snapshot_download

        prefix = f"{subfolder}/" if subfolder and subfolder != "english" else ""
        allowed = [prefix + n for n in (
            "rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*",
        )]
        path = snapshot_download(model_id, allow_patterns=allowed)
        print(f"==> 模型已下载到: {path}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"!! 模型下载失败: {e}")
        print("   可重试：python scripts/install_laya.py --mirror https://hf-mirror.com")
        return False


def check_load(model_id: str, subfolder: str, local_path: str) -> bool:
    print("==> 验证 laya 可加载")
    try:
        import laya
        print(f"    laya 版本: {laya.__version__}")
        path = local_path or model_id
        sub = subfolder if subfolder and subfolder != "english" else None
        kw = {"subfolder": sub} if sub else {}
        agent = laya.load(path, device="cpu", **kw)
        res = agent.system_one(
            {"text": "帮我打游戏王"},
            {"go": {"type": "noul", "instructions": "Is this a game request?"}},
        )
        print(f"    试推理 OK: go.noul={res['answers']['go']['noul']}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"!! laya 加载/推理失败: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="convaiinnovations/laya")
    ap.add_argument("--subfolder", default="multilingual",
                    choices=["multilingual", "english", "typed-decisions"])
    ap.add_argument("--mirror", default="")
    ap.add_argument("--local", default="")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    # --check: verify without reinstalling/downloading
    if args.check:
        ok = check_load(args.model, args.subfolder, args.local)
        print("OK  laya 可用" if ok else "FAIL laya 不可用")
        return 0 if ok else 1

    print("=== Laya 自动安装 ===")
    print(f"模型: {args.model}  checkout: {args.subfolder}")
    print(f"镜像: {args.mirror or '(默认/自动)'}  本地路径: {args.local or '(无)'}")

    # 1. pip install (idempotent)
    try:
        import laya  # noqa: F401
        print("==> laya 已安装，跳过 pip")
        pip_ok = True
    except ImportError:
        pip_ok = run_pip()

    if not pip_ok:
        print("!! pip 安装失败。请手动执行: pip install laya")
        return 1

    # 2. download weights up front
    dl_ok = download_model(args.model, args.subfolder, args.mirror, args.local)
    if not dl_ok:
        print("!! 模型下载未完成，但仍可尝试启动（首次决策会再试下载）")
        return 2

    # 3. verify
    ok = check_load(args.model, args.subfolder, args.local)
    print("========")
    print("OK  Laya 后端可用，可在 WebUI 把『决策后端』切到 laya（默认子文件夹 multilingual 支持中文）。" if ok
          else "FAIL 安装不完整，请对照报错排查。")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())