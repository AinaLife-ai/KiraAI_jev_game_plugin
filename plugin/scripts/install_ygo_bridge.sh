#!/bin/sh
# KiraYgoBridge one-shot installer (Linux / macOS).
# Clones ProjectIgnis/windbot, applies the bridge patch, builds with .NET 8,
# installs card data and writes a run script. Idempotent.
#
#   sh scripts/install_ygo_bridge.sh [install_dir]
# Env: WINDBOT_REPO, WINDBOT_COMMIT, YGO_BRIDGE_PORT (default 5274)
set -eu

DIR="${1:-$HOME/KiraYgoBridge}"
REPO="${WINDBOT_REPO:-https://github.com/ProjectIgnis/windbot.git}"
COMMIT="${WINDBOT_COMMIT:-bffe6b62679c8b2fafea8f59740e03a132517da4}"
PORT="${YGO_BRIDGE_PORT:-5274}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$DIR/windbot-src"
OUT="$DIR/out"
LOG() { printf '[install] %s\n' "$*"; }

mkdir -p "$DIR"

# ---- 1. toolchain ---------------------------------------------------------
command -v python3 >/dev/null 2>&1 || { LOG "需要 python3"; exit 1; }
command -v git >/dev/null 2>&1 || { LOG "需要 git（请先安装）"; exit 1; }

if ! command -v dotnet >/dev/null 2>&1; then
  LOG "未找到 dotnet，尝试用官方脚本安装 .NET 8 到 $DIR/.dotnet"
  mkdir -p "$DIR/.dotnet"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o "$DIR/dotnet-install.sh"
  else
    wget -q https://dot.net/v1/dotnet-install.sh -O "$DIR/dotnet-install.sh"
  fi
  sh "$DIR/dotnet-install.sh" --channel 8.0 --install-dir "$DIR/.dotnet"
  export PATH="$DIR/.dotnet:$PATH"
fi
command -v dotnet >/dev/null 2>&1 || { LOG ".NET 8 安装失败，请手动安装：https://dot.net/download"; exit 1; }
LOG "dotnet: $(dotnet --version)"

# ---- 2. source + patch ----------------------------------------------------
if [ ! -d "$SRC/.git" ]; then
  LOG "克隆 windbot → $SRC"
  git clone "$REPO" "$SRC"
fi
cd "$SRC"
git fetch --depth 1 origin "$COMMIT" || git fetch origin
git checkout -q "$COMMIT" || LOG "警告：指定 commit 不存在，使用当前分支"
LOG "应用 KiraYgoBridge 补丁"
python3 "$ROOT/ygo_bridge/apply_patches.py" "$SRC"

# ---- 3. build -------------------------------------------------------------
LOG "编译（.NET 8，首次需还原 NuGet）"
cd "$ROOT/ygo_bridge/KiraYgoBridge"
dotnet build -c Release --nologo -p:WindBotSrc="$SRC"
mkdir -p "$OUT"
cp -R bin/Release/net8.0/. "$OUT/"

# ---- 4. assets + card database --------------------------------------------
LOG "复制卡组与对话框资源"
cp -R "$SRC/Decks" "$OUT/"
cp -R "$SRC/Dialogs" "$OUT/"
if [ ! -f "$OUT/cards.cdb" ]; then
  LOG "下载 cards.cdb（真实卡数据库）"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "$OUT/cards.cdb" \
      https://github.com/moecube/ygopro-database/raw/master/locales/zh-CN/cards.cdb
  else
    wget -O "$OUT/cards.cdb" \
      https://github.com/moecube/ygopro-database/raw/master/locales/zh-CN/cards.cdb
  fi
fi

# ---- 5. run scripts --------------------------------------------------------
cat > "$DIR/run_bridge.sh" <<EOF
#!/bin/sh
export PATH="$DIR/.dotnet:\$PATH"
exec dotnet "$OUT/KiraYgoBridge.dll" --port "$PORT" --asset "$OUT" --cdb "$OUT/cards.cdb" "\$@"
EOF
chmod +x "$DIR/run_bridge.sh"
cat > "$DIR/selftest.sh" <<EOF
#!/bin/sh
export PATH="$DIR/.dotnet:\$PATH"
exec dotnet "$OUT/KiraYgoBridge.dll" --selftest --asset "$OUT" --cdb "$OUT/cards.cdb"
EOF
chmod +x "$DIR/selftest.sh"

LOG "完成！"
LOG "启动桥:   $DIR/run_bridge.sh"
LOG "自测:     $DIR/selftest.sh"
LOG "插件配置: YGO 桥地址 127.0.0.1 端口 $PORT；公共服在 game_ygo host/join 时填 server=服务器:7911"
