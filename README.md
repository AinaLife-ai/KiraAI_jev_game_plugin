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
- **按需工具注入**：WebUI 配置"启用的游戏"（enabled_games 多选）——勾选 YGO 才注入 `game_ygo` / `game_ygo_duel`（**两个合并工具覆盖全链路，更省 token**），勾选宝可梦才注入 `game_pkm_*`。未启用的游戏工具不进工具表。旧的 `game_ygo_join/ready/set_deck/duel_info` 保留为兼容别名，默认不注入。
- **YGO 全链路（主 LLM 直接打，无需 Jev）**：
  - `game_ygo`：`op=decks` 看卡组 → `build_deck` 上传 .ydk 组卡（本地校验 40-60/同名≤3）→ `host` 建房或 `join` 进房（`server`/`password` 联机参数）→ `set_deck` 选卡 → `ready` 准备 → 房主 `start` 开始；另有 `rooms/leave/surrender/chat`。
  - `game_ygo_duel`：`op=state` 实时紧凑局面（LP/手牌/双方场面/合法选项/公开日志）→ `op=act option=编号` 精确执行 → `op=chain options=编号1,编号2` 连打（每步按最新局面校验，失效即停并返回现场）→ `op=log` 公开日志。
  - **选项协议**：只提交当前合法选项（带桥 id 或下标 + 语义字段 + ocgcore 响应码），同名多张卡不会互相覆盖，**选哪张就执行哪张**；被拒绝的操作如实返回失败，绝不假装成功。
  - **对手回合响应**：`response_window` 出现时可连锁或跳过，同样走 `act`。
  - 详见 [guides/YGO指南.md](guides/YGO指南.md)（真实引擎桥 REST 契约 + 联机打真人配置）。

## 支持的游戏

| 游戏 | 适配器 | 说明 |
|---|---|---|
| 宝可梦 红/蓝/黄 | `pokemon_pyboy`（进程内 PyBoy，读 RAM，零截图） | jev-plays-pokemon 已验证该路线 |
| 宝可梦 | `mgba_http`（mGBA + HTTP 桥） | 外部进程备选 |
| 游戏王 | `ygo_engine` + **KiraYgoBridge**（ProjectIgnis/windbot 真实引擎，.NET 8） | **公共服务器打真人**，最新卡与大师规则；也可自研桥对接 |
| 泰拉瑞亚 | `terrablind_http`（TerraBlind mod） | 已验证打全 boss |
| 任意 | `generic_http`（GET /state + POST /act） | 社区游戏直接接 |

## 配置

WebUI 侧边栏 → 「游戏 Agent」：全部设置可**热修改即时生效**（不重启插件）：
- Jev：API key / 模型 / 决策频率 / 超时 / TTL / 置信度双阈值 / 温度
- 桥：游戏 / 地址 / 端口（0=自动探测）/ 轮询间隔 / 自动重连
- **游戏王 · 网络对战**：`network`（打真人，默认）/ `single`；独立 YGO 桥地址端口（默认 5274，**不是**通用桥 17878）；YGOPro 服务器 `host:port`（如 EDOPro LAN `127.0.0.1:7911`）；默认卡组；房间密码；桥超时；`ygo_auto` 自动决策开关（**默认关 = 主 LLM 手动打，不开 Jev 也能玩**；无后端时即使打开也回退手动）
- 知识：通用玩法说明书 + 分游戏背板（JSON，加内容不写代码）
- 播报：开关 / 会话 / 间隔

API key 也可设环境变量 `TYPESAFE_API_KEY`（console.typesafe.ai 获取）。

## 真实引擎桥（KiraYgoBridge）与一键安装

插件自带 **真实 YGO 引擎桥**（`plugin/ygo_bridge/`，基于 ProjectIgnis/windbot，AGPL-3.0，保留许可与修改声明）：

- **Windows**：`powershell -ExecutionPolicy Bypass -File plugin\scripts\install_ygo_bridge.ps1`，或配置页「游戏王 · 网络对战」点 **⬇ 一键安装/更新桥**，再 **▶ 启动**（有健康检查与日志）。
- **Linux/macOS**：`sh plugin/scripts/install_ygo_bridge.sh`，然后 `~/KiraYgoBridge/run_bridge.sh`。
- 安装内容：克隆上游 → 打补丁（`apply_patches.py`，锚点校验）→ .NET 8 编译 → 卡组/对话框/`cards.cdb` → 启动/自测脚本。**只在点击/执行后下载**，幂等可升级。
- 桥默认 `127.0.0.1:5274`；公共服在 `game_ygo host/join` 时填 `server=服务器:7911`，真人用 EDOPro/MyCard 进同房名即可。房名密码按 `房名#密码` 拼接（可用完整房名覆盖）。
- 一进程一座位；多房间请多实例不同端口。内置 AI 在桥未接管/超时/异常时回退，对局不卡死。
- 详见 [plugin/ygo_bridge/README.md](plugin/ygo_bridge/README.md)（决策点清单、边界、许可）与 [guides/YGO指南.md](guides/YGO指南.md)。

## 测试

```bash
python3 smoke_test.py                    # 引擎/门控/通知/链等 80 项（离线，无需 key）
python3 tests/ygo_ops_test.py            # 选项寻址/组卡校验/状态格式 40 项
python3 tests/ygo_network_test.py        # 无 Jev 网络全链路（真人第二客户端）62 项
python3 tests/ygo_auto_test.py           # 自动模式：零拒绝/无空请求/无 stale 13 项
python3 tests/ygo_pokemon_test.py        # YGO 状态构造等 29 项
python3 tests/consistency_check.py       # 前后端配置一致性 188 项
python3 tests/ygo_demo.py                # 可观看的双模式实战演示（自动 + 主 LLM 网络）
KIRAAI_ROOT=/path/to/KiraAI python3 tests/kira_real_load_test.py   # 真实 KiraAI v3.1.0 加载 20 项
python3 tests/ygo_installer_test.py           # 插件侧桥安装/启动/停止 11 项
sh plugin/scripts/install_ygo_bridge.sh       # 真实桥一键安装（已实测）+ 自测 27 项
```

## 更新日志

### v1.2.1（开箱即玩最后里程 + 房间事件 + 轮询/缓存优化）

- **只装插件、对 bot 说一句"玩游戏王"即可**：新增 `game_ygo op=install_bridge / bridge_status`。桥未连接时主 LLM 会自己下载安装并启动真实引擎桥（完成后自动通知并继续组卡/建房），不再需要用户先点按钮；`game_play` 与桥错误文案也会引导到该操作。
- **建房等人有反馈**：新增房间事件（对手进房/准备状态/双方就绪/对局开始/对局结束），主 LLM 可主动推进"建房→准备→开始→汇报胜负"，不再靠用户追问。
- **不再静默连本机**：`host/join` 未配置公共服地址时明确警告并提示如何设置（配置页 `ygo_server` 或调用时带 `server=地址:端口`）；返回中显示实际连接的服务器。
- **轮询自适应**：空闲/等房阶段 ≥3s 一次，对局中按基础频率，响应窗更快（不超过基础间隔），显著减少桥请求。
- **工具实例缓存**：相同 schema 不再每轮重建。
- **缓存说明（重要）**：搭车注入块是 `persist=False`，不进聊天历史，**必须每轮提供**；缓存友好来自框架把动态块移到最新用户消息旁（系统前缀稳定），不是跳过注入（跳过会丢信息）。已按此复核，未做错误去重。
- 新增 `tests/ygo_readiness_test.py` 17 项：install_bridge→真实桥 decks 直连、无桥指引、公共服警告、房间事件、轮询、逐轮注入。

### v1.2.0（真实引擎桥 + 一键自动安装，公共服打真人）

- **新增 KiraYgoBridge**：基于 ProjectIgnis/windbot（2026-08 主仓库）的 .NET 8 真实引擎桥，含最新卡与大师规则；约 3.7 万行核心代码 + 桥，**编译 0 错误**（Linux/ARM64 同过）。
- **全引擎决策点外接**：主阶段/战斗/连锁/是否/选项/先后手/多选选卡（祭品·反选·凑和）/表示形式/放置区域/宣言数字；`Apply` 与引擎响应 `ToValue` 逐项一致（自测 27 项）。排序/指示物/宣言卡名属性种族暂由内置 AI 处理。
- **房间生命周期门控**：自动 ready/start 改为显式 `/ready` `/start`；自定义卡组直接用上游 `DeckFile`（不会被随机替换）；支持投降/聊天/胜负与公开日志。
- **一键自动安装**：`plugin/scripts/install_ygo_bridge.{ps1,sh}` + 配置页「⬇ 一键安装/更新桥」「▶ 启动」（后台安装、日志、健康检查、启停）。已实测从零安装→自测→启动全通过。
- **容错**：桥未接管/决策超时(默认 110s)/异常时回退内置 AI，对局不卡死；多选数量与索引由桥校验。
- **合规**：上游 AGPL-3.0 的 LICENSE/COPYING 保留，附 `NOTICE.md`（来源、修改清单、许可）；插件与桥 HTTP 解耦。
- **验证边界**：Linux/.NET 8 编译、27 项引擎语义自测、HTTP 契约、插件 11 项安装器测试均通过；**公共服实战需在 Windows 按指南实测**（沙箱无法访问公共服务器）。

### v1.1.0（YGO 网络对战全链路 + 无 Jev 主 LLM 可玩）

- **YGO 全流程打通**：组卡（.ydk 上传+本地校验）/选卡/建房/进房/准备/开始/对局操作/连锁响应/投降/退房/房间聊天，`network` 联机打真人为默认模式。
- **不开 Jev 也能玩**：默认主 LLM 用两个合并工具（`game_ygo` + `game_ygo_duel`）打完整场；`ygo_auto` 为可选自动模式，无后端自动回退手动，决斗不再使用通用按键基线。
- **选项协议重写**：合法选项带 id，同名多卡不再互相覆盖；执行前按最新局面解析，**选哪张执行哪张**；失效/拒绝一律如实报错（旧版 HTTP 200 即成功、按动作名取第一个匹配，都是错的）。
- **预输入链修正**：改语义匹配（action/card/target/phase），旧版"状态哈希变化即中断"会让链必死且可能打错卡。
- **token 优化**：YGO 专用工具 4→2；自动决策每次只问 `next_action` 一个问题（旧版 5-7 个答案无人消费）；无合法选项不发请求；状态输出默认紧凑（15 选项/可关效果）；搭车注入改一行 YGO 摘要。
- **无阻塞**：桥调用全部 `to_thread`；搭车注入只读缓存（旧版每次 LLM 请求都同步 HTTP）；状态读取不再先打一次 `/health`。
- **提示词安全**：搭车注入不再修改共享 `chat_env`（旧版会跨轮污染累积），改为追加 `persist=False` 的独立 Prompt，配合框架动态块迁移保缓存。
- **配置**：新增「游戏王 · 网络对战」段（8 项，热生效）；YGO 桥端口独立于通用桥；修复 laya 自动安装开关新旧键不一致导致关不掉的问题；`stop_keywords` 不再误吞未真正停止时的消息。
- **测试**：新增联机端到端（含真人第二客户端、连锁响应、精确选卡）、自动模式零拒绝、选项单测、真实 KiraAI v3.1.0 加载验证；mock 桥升级为支持双客户端房间的参考实现（含每怪一次攻击等规则）。


## 参考实现（本方案吸收的设计）

- **TerraBlind**（Reisenbug）：Jev 打泰拉瑞亚 boss 的两层架构（Jev 答判断/代码执行/TTL/降级）
- **jev-plays-pokemon**（milanboers）：PyBoy 读 RAM + 每回合 Jev 目标 Choice + 原子 Noul 按键
- **typesafe-mario**（fhshaik）：模拟器 telemetry → 紧凑 JSON → Jev Choice → 按键
- **ygo-agent**（sbl1996）：ygopro-core + ygoenv 全结构化状态与合法动作
- 官方文档：docs.typesafe.ai（Primitives / Patterns / State / API reference）
