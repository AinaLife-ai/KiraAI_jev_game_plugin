# KiraYgoBridge — 真实游戏王引擎桥（ProjectIgnis/windbot + .NET 8）

把 **真实的 ygopro 客户端**（ProjectIgnis/windbot，含最新卡与大师规则）暴露成 KiraAI 插件需要的 HTTP 契约，让主 LLM 能在**公共服务器上和真实玩家对战**。

- 规则、卡效、连锁、房间协议全部交给上游引擎，**引擎出合法选项，模型只回选项编号**，校验后执行。
- 主 LLM 通过插件的 `game_ygo` / `game_ygo_duel` 操作；本桥不需要 Jev/laya，也不需要任何额外模型。
- 内置 AI **始终作为回退**：桥未接管、决策超时（默认 110s）或异常时，对局不会卡死。

## 组成

```
KiraAI 插件 ──HTTP(5274)── KiraYgoBridge(.NET 8) ──YGOPro 协议── 公共服务器/EDOPro/其它玩家
                                │
                                └─ ProjectIgnis/windbot（真实规则与卡效，AGPL-3.0）
```

| 文件 | 作用 |
|---|---|
| `KiraYgoBridge/src/KiraServer.cs` | HTTP 契约、进程入口、`--selftest` |
| `KiraYgoBridge/src/KiraSession.cs` | 房间生命周期、决策泵、卡组、公共状态 |
| `KiraYgoBridge/src/KiraOptions.cs` | 决策点 ↔ 选项 ↔ 引擎响应映射 |
| `KiraYgoBridge/src/KiraDecision.cs` | 决策/门控基础设施 |
| `apply_patches.py` | 对上游源码的补丁（幂等、锚点校验、失败即停） |
| `NOTICE.md` / `LICENSE` / `COPYING` | 来源、修改清单、AGPL-3.0 许可 |

## 安装（一键）

**Windows**（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File plugin/scripts/install_ygo_bridge.ps1
# 之后启动：powershell -File "$env:USERPROFILE\KiraYgoBridge\run_bridge.ps1"
```

**Linux / macOS**：

```sh
sh plugin/scripts/install_ygo_bridge.sh
# 之后启动：~/KiraYgoBridge/run_bridge.sh
```

在 KiraAI 的「游戏 Agent → 游戏王 · 网络对战」面板里也可以直接点 **⬇ 一键安装/更新桥**，然后 **▶ 启动**（含健康检查与日志）。

安装步骤：克隆上游 → 应用补丁 → `dotnet build`（.NET 8）→ 复制卡组/对话框 → 下载 `cards.cdb` → 生成启动/自测脚本。脚本幂等，可重复执行升级。需要 `git`、Python 3、.NET 8 SDK（脚本会尝试自动安装）。

## 运行参数

```
KiraYgoBridge.dll --port 5274 --bind 127.0.0.1 --asset <资源目录> --cdb <cards.cdb>
                  [--server host:7911] [--deck AI_BlueEyes] [--name KiraAI] [--timeout 110000]
```

- 默认只监听 `127.0.0.1`（插件同机）；跨机部署用 `--bind 0.0.0.0` 并自行控制访问。
- `--selftest`：离线语义自测（27 项：选项→引擎响应一致性、决策泵、卡组校验）。
- 插件侧配置：YGO 桥地址/端口填 `127.0.0.1:5274`（或你的桥地址）。

## 连公共服务器打真人

1. 启动桥后，主 LLM 执行：
   ```
   game_ygo op=decks                       # 66 套真实卡组可选，也可 build_deck 组卡
   game_ygo op=host room=Kira房名 server=服务器地址:7911 deck=AI_BlueEyes
   # 或加入别人的房间：game_ygo op=join room=房名 server=…
   game_ygo op=ready
   game_ygo op=start                       # 你是房主且双方 ready 后
   game_ygo_duel op=state / act / chain
   ```
2. 房间名即 YGOPro 的 HostInfo（srvpro「同一房名约战」）；`password` 会按 `房名#密码` 拼接，若服务器格式不同，直接把完整房名填进 `room`。
3. 真人玩家用 EDOPro / MyCard / Koishi 等客户端进**同一服务器、同一房名**即可对战。
4. 请遵守服务器规则与礼仪：建议用房名约战（朋友/测试），不要用于刷分或干扰他人；机器人对局最好明确身份。

## 已实现的引擎决策点

主阶段（召唤/特召/盖放/表示变更/发动/切阶段）、战斗阶段（攻击/发动/切阶段）、连锁（含强制连锁）、效果是否、通用是否、选项列表、猜拳与先后手、战斗重放、多选选卡（祭品/反选/凑和，含数量与合计校验）、表示形式、放置区域、宣言数字。

**仍由内置 AI 处理**（低决策价值，后续可扩展）：卡片排序、指示物选择、宣言卡名、宣言属性/种族。

## 当前边界（务必了解）

- **一个桥进程 = 一个座位/房间**。多房间并行请启动多个实例（不同端口），各自配置插件会话。
- 公共服实测依赖你的网络与服务器协议版本：默认使用上游当前协议版本，若服务器提示版本不符，需匹配对应版本/上游提交。
- 本仓库的自动化验证在 Linux（.NET 8 / ARM64）完成；**Windows 与公共服实战请按上节流程实测**，遇到问题把 `bridge.log` 与插件状态页日志发来。
- mock 桥（`tests/mock_ygo_bridge.py`）只是测试桩，**真实对战请使用本桥**。

## 验证

- 核心 windbot（约 3.7 万行）+ 桥：**`.NET 8 编译 0 错误**（Linux ARM64 同样通过）。
- `--selftest`：**27/27**（选项与引擎响应 `ToValue` 完全一致、决策泵校验、卡组校验）。
- 一键安装脚本从零跑通：克隆/补丁/编译/资源/卡库/启动脚本，产物自测与健康检查通过。
- 插件侧 `tests/ygo_installer_test.py`：11/11（状态/启动/幂等/停止/缺脚本报错）。
- HTTP 契约：health/decks/build_deck（合法+非法）/state/host 不可达如实报错/act 无决策如实报错。
