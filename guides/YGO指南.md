# 🎴 游戏王 YGO 小白完整指南（Windows）

> 让 KiraAI 的"游戏 Agent"真正进入 YGOPro 房间打牌（支持网络对战）。
> 本指南从零开始，每一步都可复制。预计 30-60 分钟。

---

## 📦 你最终会得到什么

```
KiraAI（已在运行，含游戏 Agent 插件）
   │
   ▼ 通过 ygo_engine 桥（HTTP）
EDOPro 游戏（Windows 程序，进房间/网络对战）
   │
   ▼ 读取引擎状态 + 提交合法动作
KiraAI 游戏 Agent：Jev 每回合决策 → 代码执行
```

**重要认知**：游戏 Agent 不是"看屏幕截图打牌"——它是通过 **YGOPro 的引擎协议**拿状态、发动作（零截图、零 VLM）。所以 EDOPro 要开一个**本地决斗服务器/房间**，Agent 从引擎侧读写。

---

## 🪜 第一步：装 EDOPro 游戏

1. **打开官网**：浏览器访问 `https://www.edopro.org/`（国内可直连；若打不开，百度"EDOPro 下载"，用贴吧/玩家社区的资源，选"完整整合版"）。
2. **下载**：点页面上的下载按钮，拿到一个压缩包（约 1-2 GB，包含游戏本体 + 卡图 + 数据库）。
3. **解压**：解压到比如 `D:\EDOPro\`（路径别带中文，避免玄学问题）。
4. **首次启动**：双击 `EDOPro.exe`，它会**自动更新**（等几分钟，下载卡图/数据库），更新完进入主界面。
5. **确认能进房间**：主界面 → 找"在线/局域网/房间列表"，随便进一个房间能正常看到对手列表 = 环境 OK。
   - 如果只想单机测试：主界面 → "单人模式" → 和 AI 打一局，确认能正常出牌。

> ⚠️ **网络对战**：EDOPro 连的是 YGOPro 服务器（房间号就是服务器上的房间 ID）。Agent 要打网络对战，你需要一个**能连的服务器地址**（很多是公开的，或自建：`https://github.com/mycard/ygopro-server`）。

---

## 🪜 第二步：把 Agent 桥接到 EDOPro

游戏 Agent 需要一条"桥"来读引擎状态/发动作。两种方式：

### 方式 A（推荐，省事）：用 YGOPro 的 AI 脚本接口
EDOPro 支持 **Lua 脚本**（`init.lua`），能读写决斗状态、发起动作。Agent 桥就是一个小 HTTP 服务，把 Lua 接口包成 `GET /state` + `POST /act`。

1. 在 `D:\EDOPro\` 下建 `myagent\` 文件夹，放一个 `bridge.lua`（由 Agent 插件提供，见下文第 4 步），在 EDOPro 启动参数或配置里加载它（EDOPro 支持 `-s script.lua` 启动参数）。
2. 桥脚本启动一个本地 HTTP 服务（127.0.0.1:5274）。

### 方式 B：用"无头引擎"（纯代码，适合自建房间/自动化）
用 [ygopro-core](https://github.com/Fluorohydride/ygopro-core) 编译的无头引擎 + [ygo-agent](https://github.com/sbl1996/ygo-agent) 的 ygoenv 环境。**这条对小白不推荐**（要装 C++ 工具链），留给进阶用户。

> 💡 **当前状态**：我们的 `ygo_engine` 适配器已实现桥协议（`join/ready/set_deck/duel_info`），桥的 Lua 脚本和 EDOPro 对接脚本在 `plugin/bridge/adapters/ygo/` 目录（见最后"插件文件"）。若 EDOPro 版本更新导致接口变化，按错误提示调整桥脚本即可。

---

## 🪜 第三步：装 KiraAI 游戏 Agent 插件

1. **拿到插件**：插件源码在 `plugin/` 目录（本项目的 `kira-game-agent`）。
2. **放进 KiraAI**：
   - 找到 KiraAI 的 `data/plugins/` 目录（KiraAI 的 data 目录，比如 `D:\KiraAI\data\plugins\`）。
   - 把 `plugin/` 文件夹整个复制过去，改名 `kira-game-agent`。
   - 结果：`D:\KiraAI\data\plugins\kira-game-agent\manifest.json` 存在。
3. **装依赖**（KiraAI 的 Python 环境里执行）：
   ```bat
   pip install requests
   ```
   若用 laya 后端（推荐）：**插件启动后会自动在后台装好配好**（免 key，支持中文）。国内慢可手动 `python scripts\install_laya.py --mirror https://hf-mirror.com`；想用 Jev 则跳过。
4. **重启 KiraAI**：完全退出再启动。启动日志应出现：
   ```
   [plugin] [kira_game_agent] ready
   Registered 10 tools from kira-game-agent: [...]
   ```

---

## 🪜 第四步：配置 Agent（WebUI 侧边栏）

1. **打开 KiraAI WebUI**：浏览器访问 `http://127.0.0.1:5267`（KiraAI 默认端口）。
2. **侧边栏找到"游戏 Agent"**：点进去 = 配置页。
3. **决策后端**：**默认已是 laya**（本地，零成本，自动安装配置，无需 key）。想用 Jev（云端）则切到 `jev` 并填 API key（见《先决条件》）。
4. **必填配置**：
   | 配置项 | 填什么 |
   |---|---|
   | TypeSafe API Key（仅当切到 Jev） | `https://console.typesafe.ai` 生成，粘贴（或设环境变量 `TYPESAFE_API_KEY`） |
   | 决策后端 | 默认 `laya`（推荐）；可切 `jev` |
   | 游戏适配器 | 选 `ygo_engine` |
   | 启用的游戏 | 勾选"游戏王 YGO" |
   | 桥地址 | `127.0.0.1` |
   | 桥端口 | `5274`（桥脚本监听端口；用 EDOPro 官方默认/你改的） |
5. **保存**（热生效，不用重启）。
6. **验证**：配置页"状态"面板里，桥连接应显示"已连接"（前提：EDOPro 已启动且桥脚本已加载）。

---

## 🪜 第五步：在群里和 Agent 打牌（完整流程）

### 场景：你开房，Agent 进来

**① 你在 EDOPro 开好房间**，得到房间号（比如 `1999`）。

**② 群里 @ 机器人**：
```
@bot 来跟我打游戏王，房间 1999
```
Agent 的 LLM 会识别意图 → 调用 `game_ygo_join(room="1999")` → 桥让 EDOPro 加入房间。

**③ 它确认对手**：
```
游戏 Agent：已加入房间 1999，对手：Alice
```
（对手 ID 从引擎状态读出来，Agent 会确认。）

**④ 它准备**：LLM 自动调用 `game_ygo_ready()`：
```
游戏 Agent：我准备好了。
```

**⑤ 你点开始** → 对局开始，Agent 检测到 `dueling` 阶段：
```
游戏 Agent：对局开始！
```

**⑥ 之后每一回合**：
- Agent 的 Jev 引擎每回合读引擎状态（手牌/场/墓地/合法动作）→ 决策 → 提交动作。
- **你不用管它**，它会自己打。

**⑦ 你和它互动**：
```
你：卧槽bot你这波为什么这么操作？
→ LLM 带着当前局面自动回答（搭车注入）
你：你有本事用 xx 卡！
→ LLM 调 game_set_goal("优先用 xx 卡") → 它下回合就按新打法来
你：停一下
→ game_pause() 暂停自动决策
```

### 场景：Agent 自己开房（需要桥支持，进阶）

在桥脚本里加一个"创建房间"端点，然后在群里说：
```
@bot 开个房打游戏王，房间名叫 test
```
LLM 调 `game_ygo_join(room="test", create=True)`（桥里实现创建逻辑）。

---

## 🪜 第六步：验证它真的在"看"状态

打牌过程中，随时在群里问：
```
@bot 现在什么情况？
```
Agent 会调用 `game_state()` 返回当前局面摘要（LP/手牌数/场况/阶段）。

或直接问细节：
```
@bot 对面手牌几张？我还有多少血？
```
→ `game_ygo_duel_info()` 返回详细对局信息。

---

## 🛠️ 常见问题（FAQ）

| 问题 | 解决 |
|---|---|
| 桥连不上（状态面板"未连接"） | ① EDOPro 启动了吗？② 桥脚本加载了吗？③ 端口对吗？（EDOPro 默认端口 vs 桥监听端口） |
| 加了房间没反应 | 检查 EDOPro 是否真的能连该服务器/房间；试试单机房间先通 |
| Agent 说"无法创建引擎" | 插件 manifest 的 `core_version` 要匹配 KiraAI 版本；重启 KiraAI |
| 打牌乱打/一直 wait | ① Jev key 配了吗？② 置信度阈值调低点试试（配置页"低风险阈值"0.55→0.4）③ 检查桥的 `/state` 是否返回了合法动作列表 |
| 对手手牌 | Jev 只看到**数量**（引擎协议限制），看不到内容——这是对的，公平 |
| 卡组 | 在 EDOPro 里组好卡组（.ydk），Agent 用 `game_ygo_set_deck("卡组名.ydk")` 选择 |

---

## 📁 插件文件速查（YGO 相关）

```
plugin/
  main.py                  # 工具 game_ygo_join/ready/set_deck/duel_info + 搭车注入
  engine.py                # 通用决策循环（阶段机: joining→confirming_opponent→readying→dueling）
  bridge/adapters/ygo/     # ygo 桥脚本（Lua bridge + HTTP 封装）——按 EDOPro 版本微调
  jev/                     # Jev 客户端/门控/记忆/state 构造
  pages/web/index.html     # WebUI 配置页（启用的游戏勾选 ygo_engine）
```

---

## 🎯 一句话总结

```
edopro.org 下载 EDOPro → 组卡组 → 开房间（或连服务器）
+ KiraAI 装游戏 Agent 插件 → WebUI 勾选 ygo_engine + 填 API key + 桥端口
→ 群里 @bot "打游戏王，房间 1999" → 它自己进房、确认、准备、开打
→ 随时问它局面 / 让它换打法 / 喊停
```
