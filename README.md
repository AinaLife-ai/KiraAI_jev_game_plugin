# kira-game-agent

KiraAI 插件：让 KiraAI 通过 **System 1 决策引擎**游玩任意游戏。
**零 VLM**、快、省 token。

**双决策后端（默认 laya 优先）**：
- **laya**（本地开源，[NandhaKishorM/laya](https://github.com/NandhaKishorM/laya)）：**默认**，零成本、100+ 语言（中文友好）、本地 33ms——游戏打牌首选，开箱即用。
- **Jev**（TypeSafe 云 API）：完整适配保留，用户可在 WebUI 切到 `jev`（需 key）。

## 🧭 小白快速上手（Windows）

| 指南 | 内容 |
|---|---|
| [先决条件](guides/先决条件.md) | KiraAI data 目录确认 + 决策后端选择（laya 默认 / Jev） |
| [🎮 宝可梦指南](guides/宝可梦指南.md) | PyBoy 玩红/蓝/黄：装 PyBoy → ROM → 配置 → 开玩（每一步可复制） |
| [🎴 游戏王指南](guides/YGO指南.md) | EDOPro 打牌+网络对战：装 EDOPro → 桥 → 配置 → 开房/进房 |

## 🔌 Laya 本地后端（默认，开箱即用）

**装上插件即默认用 laya、自动配好**：
- 插件启动后后台自动 `pip install laya` + 下载模型（multilingual，支持中文），不阻塞 KiraAI。
- **默认决策后端就是 laya**——装好即用，无需任何配置/API key。
- WebUI「游戏 Agent」配置页实时显示安装进度（待命/安装中/下载中/已就绪）。
- 若 laya 未就绪且配置了 Jev key，会自动回退 Jev；否则走保守 baseline，不崩。

**想用 Jev**：WebUI 把「决策后端」切到 `jev` + 填 API key 即可（Jev 完整保留）。
国内网络慢可手动（可选）：
```bash
python scripts/install_laya.py --mirror https://hf-mirror.com
python scripts/install_laya.py --local /path/to/laya-dir   # 已有模型/离线
python scripts/install_laya.py --check                     # 验证可用
```
配置页可关"启动后自动安装"（`section_laya.autoinstall`）。

## 参考实现（本方案吸收的设计）

- **TerraBlind**（Reisenbug）：Jev 打泰拉瑞亚 boss 的两层架构
- **jev-plays-pokemon**（milanboers）：PyBoy 读 RAM + 每回合 Jev 目标 Choice
- **laya**（NandhaKishorM）：本地开源 System 1 引擎，Jev 兼容接口 + 中文
- **typesafe-mario**（fhshaik）、**ygo-agent**（sbl1996）
- 官方文档：docs.typesafe.ai、github.com/NandhaKishorM/laya

## 核心思路

```
游戏内部状态（模拟器RAM / 引擎状态 / mod）   ← 零截图零VLM
        ↓ 压缩成紧凑结构化 state（~300-900 token）
   System 1 决策引擎并行问类型化问题          ← laya 本地 33ms（$0）/ Jev 云 236-276ms（$0.042/Mtok）
        ↓ 置信度门控 + baseline 降级
   代码执行动作（按键/原语/合法动作）
        ↓ 每 200ms-2s 一个决策周期
   harness 记忆（recent actions/hints）回灌   ← 引擎无历史，连续性在 harness
```

## 目录

```
plugin/
  manifest.json / schema.json / requirements.txt
  main.py                 # KiraAI 入口：工具+钩子+API+页面
  engine.py               # 通用决策循环（游戏无关）
  jev/
    client.py             # Jev HTTP 客户端（异步双缓冲 + usage 计量）
    questions.py          # 问题构造（goal Choice + action Nouls）
    gate.py               # 置信度门控 + baseline
    memory.py             # harness 记忆
    state_builder.py      # 状态压缩
  bridge/
    connector.py          # HTTP 桥（自动发现/重连）
    registry.py           # 适配器注册表
    adapters/
      pokemon_pyboy.py    # 宝可梦（PyBoy 进程内，读 RAM）
  pages/web/index.html    # WebUI 侧边栏配置页（热修改）
```

## 工具面（KiraAI LLM 可见）

| 工具 | 作用 |
|---|---|
| `game_play(game, goal)` | 开始游玩指定游戏（**本会话独立实例，可多游戏并行**） |
| `game_state(game?)` | 实时状态摘要（用户问"现在怎么样了"） |
| `game_command(action, game?)` | 手动执行原语（用户让改操作） |
| `game_set_goal(goal, game?)` | 热改目标（bot 打法调整） |
| `game_jev_ask(q, game?)` | 临时问 Jev 自定义问题 |
| `game_pause/resume/stop(game?)` | 控制本会话指定游戏 |
| `game_logs/diag(game?)` | 日志与诊断 |

`game?` = 可选参数，不传则作用于本会话最近启动的引擎（每会话可同时跑多个游戏）。

## 播报设计

- **发起会话默认播报**：哪个会话发出 game_play，游戏事件默认播回那个会话（无需预配置白名单）。
- **可加播**：配置页的"播报会话"是**额外**目标，不是白名单。
- **防堵塞**：通知走有界异步队列（满则丢最旧），发送有超时（10s），**绝不阻塞决策循环**。
- **限流**：按 (会话, 事件类型) 做最短间隔（默认 30s），防刷屏。
- **多会话/多游戏并行**：每个 (会话, 游戏) 一个独立引擎实例，各自决策循环并行跑；播报按引擎 → 绑定会话路由。

## 主 LLM 交互（状态机搭车 + 升级）

**三层决策**：主 LLM 是指挥官，Jev 是执行官，状态机是节拍器。

- **搭车注入**：用户每轮发消息时，`@on.llm_request` 钩子自动把该会话运行中游戏的**一行状态摘要**挂进 `chat_env`（persist=False，用完即弃、不落记忆）。所以用户问"你这波为什么这么操作"时，主 LLM **自动带着局面和最近决策**回答，不需要单独触发。
- **主动升级**：Jev 对高风险动作（YGO 的 attack/summon/activate 等）置信度低于阈值，或连续低置信（streak≥3）→ `Escalator` 发合成消息唤醒主 LLM 决策轮。LLM 用 `game_state`/`game_command`/`game_set_goal` 回写。带冷却（120s）防刷屏，冷却期间升级意图保留、过后一次触发。
- **阶段状态机**：每游戏一组阶段（YGO: idle→joining→confirming_opponent→readying→dueling→ended；宝可梦: overworld→dialog→menu→battle）。阶段迁移是事件——普通迁移走播报，关键迁移（对局开始）走升级。适配器在快照里报 `stage` 即可被引擎采纳。
- **按需工具注入**：WebUI 配置"启用的游戏"（enabled_games 多选）——勾选 YGO 才向 LLM 注入 `game_ygo_join/ready/set_deck/duel_info`，勾选宝可梦才注入 `game_pkm_*`。未启用的游戏工具不进工具表。
- **YGO 专用工具**：`game_ygo_join(room, deck)` 加入房间 → `game_ygo_ready()` 确认准备 → `game_ygo_duel_info()` 查对局详情（LP/手牌/场况/合法动作）→ `game_ygo_set_deck(name)` 换卡组（.ydk 目录）。

## 支持的游戏

| 游戏 | 适配器 | 说明 |
|---|---|---|
| 宝可梦 红/蓝/黄 | `pokemon_pyboy`（进程内 PyBoy，读 RAM，零截图） | jev-plays-pokemon 已验证该路线 |
| 宝可梦 | `mgba_http`（mGBA + HTTP 桥） | 外部进程备选 |
| 游戏王 | `ygo_engine`（YGOPro 引擎，结构化状态+合法动作） | 支持网络对战（引擎协议） |
| 泰拉瑞亚 | `terrablind_http`（TerraBlind mod） | 已验证打全 boss |
| 任意 | `generic_http`（GET /state + POST /act） | 社区游戏直接接 |

## 配置

WebUI 侧边栏 → 「游戏 Agent」：全部设置可**热修改即时生效**（不重启插件）：
- Jev：API key / 模型 / 决策频率 / 超时 / TTL / 置信度双阈值 / 温度
- 桥：游戏 / 地址 / 端口（0=自动探测）/ 轮询间隔 / 自动重连
- 知识：通用玩法说明书 + 分游戏背板（JSON，加内容不写代码）
- 播报：开关 / 会话 / 间隔

API key 也可设环境变量 `TYPESAFE_API_KEY`（console.typesafe.ai 获取）。

## 测试

```bash
python3 smoke_test.py   # 离线烟雾测试（无需 key、无需 KiraAI、无需游戏）
```

## 参考实现（本方案吸收的设计）

- **TerraBlind**（Reisenbug）：Jev 打泰拉瑞亚 boss 的两层架构（Jev 答判断/代码执行/TTL/降级）
- **jev-plays-pokemon**（milanboers）：PyBoy 读 RAM + 每回合 Jev 目标 Choice + 原子 Noul 按键
- **typesafe-mario**（fhshaik）：模拟器 telemetry → 紧凑 JSON → Jev Choice → 按键
- **ygo-agent**（sbl1996）：ygopro-core + ygoenv 全结构化状态与合法动作
- 官方文档：docs.typesafe.ai（Primitives / Patterns / State / API reference）
