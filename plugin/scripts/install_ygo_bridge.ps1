# KiraYgoBridge one-shot installer (Windows PowerShell).
#   powershell -ExecutionPolicy Bypass -File scripts/install_ygo_bridge.ps1 [-Dir "$env:USERPROFILE\KiraYgoBridge"]
# Env: WINDBOT_REPO, WINDBOT_COMMIT, YGO_BRIDGE_PORT (default 5274)
param(
    [string]$Dir = "$env:USERPROFILE\KiraYgoBridge",
    [string]$Repo = $(if ($env:WINDBOT_REPO) { $env:WINDBOT_REPO } else { "https://github.com/ProjectIgnis/windbot.git" }),
    [string]$Commit = $(if ($env:WINDBOT_COMMIT) { $env:WINDBOT_COMMIT } else { "bffe6b62679c8b2fafea8f59740e03a132517da4" }),
    [int]$Port = $(if ($env:YGO_BRIDGE_PORT) { [int]$env:YGO_BRIDGE_PORT } else { 5274 })
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $Dir "windbot-src"
$Out = Join-Path $Dir "out"
function Log($m) { Write-Host "[install] $m" -ForegroundColor Cyan }

New-Item -ItemType Directory -Force -Path $Dir | Out-Null

# ---- 1. toolchain ---------------------------------------------------------
if (-not (Get-Command python -ErrorAction SilentlyContinue) -and -not (Get-Command python3 -ErrorAction SilentlyContinue)) {
    throw "需要 Python 3（https://www.python.org/downloads/ 或 winget install Python.Python.3.12）"
}
$Python = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "python3" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Log "未找到 git，尝试 winget 安装（也可手动 https://git-scm.com/download/win）"
    winget install --id Git.Git -e --source winget
}
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    Log "未找到 dotnet，尝试 winget 安装 .NET 8 SDK"
    winget install --id Microsoft.DotNet.SDK.8 -e --source winget
    $env:Path = "$env:ProgramFiles\dotnet;$env:Path"
}
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw ".NET 8 SDK 不可用，请手动安装：https://dot.net/download"
}
Log "dotnet: $(dotnet --version)"

# ---- 2. source + patch ----------------------------------------------------
if (-not (Test-Path (Join-Path $Src ".git"))) {
    Log "克隆 windbot → $Src"
    git clone $Repo $Src
}
Push-Location $Src
git fetch --depth 1 origin $Commit
if ($LASTEXITCODE -ne 0) { git fetch origin }
git checkout -q $Commit
Pop-Location
Log "应用 KiraYgoBridge 补丁"
& $Python (Join-Path $Root "ygo_bridge\apply_patches.py") $Src
if ($LASTEXITCODE -ne 0) { throw "补丁应用失败" }

# ---- 3. build -------------------------------------------------------------
Log "编译（.NET 8，首次需还原 NuGet）"
Push-Location (Join-Path $Root "ygo_bridge\KiraYgoBridge")
dotnet build -c Release --nologo -p:WindBotSrc="$Src"
if ($LASTEXITCODE -ne 0) { throw "编译失败" }
Pop-Location
New-Item -ItemType Directory -Force -Path $Out | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "ygo_bridge\KiraYgoBridge\bin\Release\net8.0\*") $Out

# ---- 4. assets + card database --------------------------------------------
Log "复制卡组与对话框资源"
Copy-Item -Recurse -Force (Join-Path $Src "Decks") $Out
Copy-Item -Recurse -Force (Join-Path $Src "Dialogs") $Out
$Cdb = Join-Path $Out "cards.cdb"
if (-not (Test-Path $Cdb)) {
    Log "下载 cards.cdb（真实卡数据库）"
    Invoke-WebRequest -Uri "https://github.com/moecube/ygopro-database/raw/master/locales/zh-CN/cards.cdb" -OutFile $Cdb
}

# ---- 5. run scripts --------------------------------------------------------
$Run = Join-Path $Dir "run_bridge.ps1"
@"
`$ErrorActionPreference = 'Stop'
& dotnet "$Out\KiraYgoBridge.dll" --port $Port --asset "$Out" --cdb "$Cdb" `$args
"@ | Set-Content -Encoding UTF8 $Run
$Self = Join-Path $Dir "selftest.ps1"
@" & dotnet "$Out\KiraYgoBridge.dll" --selftest --asset "$Out" --cdb "$Cdb" "@ | Set-Content -Encoding UTF8 $Self

Log "完成！"
Log "启动桥:   powershell -File $Run"
Log "自测:     powershell -File $Self"
Log "插件配置: YGO 桥地址 127.0.0.1 端口 $Port；公共服在 game_ygo host/join 时填 server=服务器:7911"
