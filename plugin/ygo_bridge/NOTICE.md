# KiraYgoBridge — 来源、修改与许可

- **上游项目**：ProjectIgnis/windbot — https://github.com/ProjectIgnis/windbot
- **基准提交**：`bffe6b62679c8b2fafea8f59740e03a132517da4`（2026-08-27）
- **许可证**：GNU Affero General Public License **v3.0**（AGPL-3.0）。
  上游 `LICENSE` 与 `COPYING` 原文保留在本目录；本目录的衍生修改按同一许可证发布。
- 安装脚本会把这两个文件一并复制到构建输出目录，保持再分发时的许可信息完整。

## 我们对上游做了什么（`apply_patches.py` 自动应用，锚点校验）

1. `Mono.Data.Sqlite` → `Microsoft.Data.Sqlite`，使核心代码可在 **.NET 8** 编译运行（Linux/Windows/macOS，含 ARM64）。
2. `GameClient` 暴露 `Behavior`，供桥读取房间与对局状态。
3. `GameBehavior` 新增访问器：`BridgeRoom` / `BridgeDuel` / `BridgeSendReady` / `BridgeSendStart` / `BridgeSurrender` / `BridgeUpdateDeck`。
4. 各决策点改为经过 `Kira.KiraBridge.Decide(...)`；**内置 AI 始终保留为回退**（桥未接管、超时或异常时）。
5. 自动 `HsReady` / `HsStart` 改为显式门控（由 HTTP 的 `/ready` `/start` 触发）。
6. 新增 `OnDuelStart` / `OnDuelEnd` / `OnWin` / `OnChat` 钩子，用于公开对局日志。

## 新增文件（本目录 `KiraYgoBridge/src/`）

- `KiraServer.cs`：HTTP 契约与进程入口（`--selftest` 为离线自测入口）
- `KiraSession.cs`：座位/房间生命周期、决策泵、卡组管理、公共状态快照
- `KiraOptions.cs`：每个引擎决策点 ↔ 合法选项 ↔ 引擎响应的映射
- `KiraDecision.cs`：决策/门控基础设施
- `KiraSelfTest.cs`：离线语义自测（27 项）

KiraAI 插件与本桥通过 **HTTP** 解耦：插件不含 ygopro-core，也不与上游代码静态链接；
本桥是独立进程，独立承担上游许可证义务。
