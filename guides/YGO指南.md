# 🎴 游戏王 YGO 完整指南：和真实玩家网络对战

本插件让 KiraAI（主 LLM）**完整地玩一场游戏王**：组卡 → 选卡 → 建房 → 进房 → 准备 → 开始 → 对局操作（含对手回合的连锁响应）→ 投降/退房。

- **不需要 Jev / laya**：主 LLM 自己就能通过两个工具打完整场（默认模式）。
- **网络对战是一等场景**：`network` 模式下建房/进房，对手是真实玩家（EDOPro / YGOPro / Neos 等客户端）。
- **可选自动模式**：配置页打开 `自动决策（ygo_auto）` 后，由 Jev/laya 自动操作；没有可用后端时自动回退为主 LLM 手动，绝不会乱按键。

---

## 1. 架构：插件 ↔ 桥 ↔ 真实引擎 ↔ 真实玩家

```
KiraAI 主 LLM
   │  game_ygo / game_ygo_duel（两个工具）
   ▼
KiraAI_jev_game_plugin（本插件）
   │  HTTP REST（下方契约）
   ▼
KiraYgoBridge（ProjectIgnis/windbot 真实引擎，默认 127.0.0.1:5274）
   │  ocgcore / YGOPro 网络协议
   ▼
真实对局：公共服务器、EDOPro、YGOPro、Neos 房间里的真实玩家
```

**现在仓库里已经带了真实引擎桥 `KiraYgoBridge`**（`plugin/ygo_bridge/`，基于 ProjectIgnis/windbot，含最新卡与大师规则，AGPL-3.0），并且可以**一键自动安装**。你不需要自己写桥。

三种桥的定位：

| 桥 | 用途 | 能否打真人 |
|---|---|---|
| **`KiraYgoBridge`（推荐）** | 真实引擎（windbot + ocgcore 规则），连公共服务器/EDOPro 房间 | ✅ 真实规则、真实玩家 |
| `tests/mock_ygo_bridge.py` | 测试/演示用参考实现：双客户端房间、准备/开始、轮流回合、攻击连锁、投降/聊天 | ❌ 规则简化（示范卡池），**只用于测试** |
| 自研桥（按下文契约） | 想接自己的引擎（如 ygoenv）时 | ✅ 实现同一契约即可，插件不用改 |

---

## 2. 一键安装真实桥（推荐路径）

### Windows（你的环境）

在插件目录执行（或直接在 KiraAI 配置页点按钮）：

```powershell
# 方式一：KiraAI「游戏 Agent → 游戏王 · 网络对战」面板
#   点「⬇ 一键安装/更新桥」，完成后点「▶ 启动」（有健康检查和日志）

# 方式二：命令行手动执行同一个脚本
powershell -ExecutionPolicy Bypass -File plugin\scripts\install_ygo_bridge.ps1
powershell -File "$env:USERPROFILE\KiraYgoBridge\run_bridge.ps1"
```

脚本会自动完成：检测/安装 .NET 8 SDK 与 git → 克隆 ProjectIgnis/windbot → 应用桥补丁 → 编译 → 复制卡组/对话框 → 下载 `cards.cdb` → 生成启动脚本。默认安装到 `用户目录\KiraYgoBridge`，**只在点击/执行后才下载**，可重复运行升级。

### Linux / macOS

```sh
sh plugin/scripts/install_ygo_bridge.sh
~/KiraYgoBridge/run_bridge.sh
```

### 启动后

- 桥默认监听 `127.0.0.1:5274`，插件配置页「YGO 桥地址/端口」保持默认即可。
- 离线自测：`selftest`（Windows `selftest.ps1` / Linux `selftest.sh`），应输出 27 项全过。
- 想打公共服：继续看第 4、7 节；想先本机体验简化规则，可改用 `tests/mock_ygo_bridge.py`（仅测试用）。

### 自研桥（可选）

如果你已有自己的引擎（ygoenv、EDOPro Lua 等），只要实现第 3 节的 REST 契约，插件无需修改即可使用。参考项目：`mostwanted7/ygo-agent`（LLM×EDOPro，选项编号决策，与本插件同思路）、`sbl1996/ygo-agent`（ygoenv gym）、`IceYGO/windbot`、`mycard/srvpro`（服务器）。

---

## 3. 桥 REST 契约（真实桥按此实现即可）

所有端点：**业务失败必须返回 `{"ok": false, "error": "原因"}`**（HTTP 200 也算失败）。插件绝不把被拒绝的操作当成成功。

| 端点 | 请求 JSON | 成功响应（关键字段） |
|---|---|---|
| `GET /health` | – | `{"ok": true}` |
| `GET /decks` | – | `{"decks": [{"name": "a.ydk", "main": 40, "extra": 0, "side": 0}]}` |
| `POST /build_deck` | `{"name": "a.ydk", "main": [卡密…], "extra": [], "side": []}` 或 `{"name":…, "ydk": "完整文本"}` | `{"ok": true, "name": "a.ydk"}` |
| `GET /rooms` | – | `{"rooms": [{"name", "players": [...], "duel_active": false}]}`（可选） |
| `POST /host` | `{"room", "deck", "mode": "network"/"single", "server": "host:port", "password"?}` | `{"ok": true, "room", "session"?, "players": [...]}` |
| `POST /join` | 同 `/host` | `{"ok": true, "room", "opponent", "session"?}` |
| `POST /set_deck` | `{"deck": "a.ydk"}` | `{"ok": true}` |
| `POST /ready` | `{"ready": true}` | `{"ok": true, "all_ready": true}` |
| `POST /start` | `{}`（房主；双方 ready 后） | `{"ok": true, "state": {...}}` |
| `GET /state` | 可带 `?session=` | 见下方状态字段 |
| `POST /act` | 见「选项协议」 | `{"ok": true, "state": {...}}`；非法则 `{"ok": false, "error": …}` |
| `POST /leave` / `POST /surrender` | `{}` | `{"ok": true}` |
| `POST /chat` | `{"message": "…"}` | `{"ok": true}`（可选） |

`session`：多座位桥用来区分我方/对方视角的令牌，`host`/`join` 返回后，插件之后的请求都会带上它；单客户端桥可以忽略。

### `/state` 状态字段

```jsonc
{
  "stage": "joining | confirming_opponent | readying | dueling | ended | idle",
  "room":  {"name": "arena", "players": ["我方", "对手"], "is_host": true,
            "ready": {"我方": true, "对手": false}, "server": "127.0.0.1:7911"},
  "game": {
    "my_lp": 8000, "op_lp": 8000, "turn": 1, "phase": "main1",
    "is_my_turn": true, "is_duel_active": true, "winner": "",
    "opponent": "对手昵称", "opponent_hand_count": 5,
    "hand":     [{"name": "…", "atk": 1400, "def": 1100, "level": 4, "effect_text": "…"}],
    "field_my": [{"name": "…", "position": "attack"}],
    "field_op": [{"name": "里侧卡", "position": "face-down", "hidden": true}],
    "grave_my": [], "grave_op": [], "banished_my": [], "banished_op": [],
    "deck_count": 35, "op_deck_count": 35, "extra_deck": [],
    "legal_actions": [ /* 见选项协议 */ ],
    "response_window": false,          // true = 对手动作等待我方响应（连锁/跳过）
    "public_log": ["对方 召唤 …", "我方 盖放 1 张卡"]
  }
}
```

**只暴露公共信息**：对手手牌只给数量，里侧卡不给卡名（除非引擎已公开）。这是公平对战也是提示词安全的要求。

卡片尽量带 `effect_text`（一句话效果），主 LLM 需要它理解卡牌；没有卡数据库时可查免费的 YGOPRODeck API（`https://db.ygoprodeck.com/api/v7/cardinfo.php`）。

### 选项协议（核心）

`legal_actions` 里每一项是一个**当前合法选项**，例如：

```jsonc
{"action": "summon", "card": "Mystic Tomato", "position": "attack", "atk": 1400, "def": 1100}
{"action": "attack", "card": "Dark Magician", "target": "Summoned Skull"}
{"action": "switch", "phase": "battle"}
{"action": "activate", "card": "Mirror Force", "desc": "连锁对方攻击宣言"}
{"action": "pass"}
```

桥若自带选项编号，请放在 `id` / `option` / `response` 字段，插件会优先使用；否则插件用**当前列表下标**作为编号。

插件提交 `POST /act` 时同时携带三种寻址形式，桥任选其一解析：

```jsonc
{
  "option": "3",                    // 桥 id 或列表下标
  "action": "summon",              // 语义字段（原样回传）
  "card": "Gemini Elf", "position": "attack",
  "code": 4                          // ygopro-core 响应码（能映射时）
}
```

**为什么必须是选项而不是动作名**：手牌里 5 只怪兽都对应 `summon`，只报动作名既无法区分也无法保证执行的是同一张卡。插件全程按"选项 → 校验 → 执行"走，选项失效会停下并返回最新局面，不会盲点。

---

## 4. 主 LLM 工具（默认模式，无需 Jev）

启用 `ygo_engine` 后只注入**两个**工具（省 token）：

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `game_ygo` | 卡组与房间生命周期 | `op`: `decks` / `build_deck` / `rooms` / `host` / `join` / `set_deck` / `ready` / `start` / `leave` / `surrender` / `chat`；`room` `deck` `name` `server` `password` `mode` `main` `extra` `side` `ydk` `message` |
| `game_ygo_duel` | 对局信息与操作 | `op`: `state`（默认实时，紧凑）/ `act`（`option`=编号）/ `chain`（`options`=编号序列）/ `log`；`full`（全部选项+效果）、`refresh`（false 用缓存） |

典型对话流程（用户："开一局游戏王，我要和你打"）：

```
game_ygo op=decks                      # 看可用卡组
game_ygo op=host room=arena deck=dragon.ydk server=127.0.0.1:7911
# 等真人进房后
game_ygo op=ready
game_ygo op=start                      # 房主开始
game_ygo_duel op=state                 # 看手牌/场面/合法选项
game_ygo_duel op=act option=3          # 精确执行一个选项
game_ygo_duel op=chain options=0,4,7   # 连打多步，每步都按最新局面校验
```

组卡（标准 `.ydk`，卡密即 ygopro 卡片密码）：

```
game_ygo op=build_deck name=demo.ydk
  ydk="#created by KiraAI\n#main\n46986414\n…（主卡组 40-60 张）\n#extra\n…\n!side\n…"
```

插件会先做本地校验（主卡组 40-60、额外/副卡组 ≤15、同名 ≤3），**禁限卡表与卡效合法性由真实引擎最终校验**。

---

## 5. 自动模式（可选）

配置页打开「自动决策（需 Jev/laya）」后：

- 有可用后端（Jev 有 key / laya 已加载）→ 引擎自动操作，主 LLM 在关键阶段被征询战术。
- 每次决策**只问一个问题**（`next_action`，选项 id 单选），不再发送一堆用不到的问题；没有合法选项（对手回合等待时）**完全不发请求**。
- 预输入链按**语义匹配**执行后续选项，选项不再合法立即停下重决策。
- 无后端 → 自动回退主 LLM 手动，绝不会用通用按键基线在决斗里乱点。

---

## 6. 响应与 token 优化（联机实测取向）

- 所有桥调用都在工作线程执行（`asyncio.to_thread`），不会卡住 KiraAI 的聊天循环。
- 每轮聊天的状态搭车注入**只读缓存**，零 HTTP；并且**追加独立 Prompt**，不修改共享 `chat_env`（旧版会污染并无限累积），配合框架的动态块迁移保持系统前缀可缓存。
- 工具输出默认紧凑：选项 15 条、卡片一行、效果可关；`full=true` 才展开（框架本身也会截断超长工具输出）。
- `act` 直接复用桥返回的最新状态，不再多一次往返；`chain` 一次调用打完一串确定性操作，适合有回合计时的真实房间。

---

## 7. 联机实战注意事项

- **计时**：真实服务器通常有回合/操作时限。建议用 `chain` 连打确定的操作，把 `decision_hz` 与桥超时（默认 5s）保持在默认水平。
- **密码房**：`join` 带 `password`，或在配置页填默认房间密码。
- **对手回合**：出现 `response_window` 时，合法选项会包含连锁（`activate`）与 `pass`，主 LLM 同样用 `act` 回应。
- **聊天**：`game_ygo op=chat message=…` 可与真人交流；请保持礼貌。
- **公平**：插件只读取公共信息，不会也不应窥探对手手牌与里侧卡。
- **mock 桥不是真实规则**：它只有示范卡池与简化卡效，用于测试；真实对战请用真实引擎桥。

---

## 8. 排障

| 现象 | 原因与处理 |
|---|---|
| `YGO 桥连接失败` | 桥没启动/端口错。配置页点「⬇ 一键安装/更新桥」再「▶ 启动」，看面板日志；或手动跑 `run_bridge` 脚本并查看 `bridge.log`。端口默认 5274，**不是**通用桥 17878。 |
| 公共服连不上/被断开 | 检查服务器地址与端口（通常 7911）、房名/密码、协议版本是否匹配（本桥使用上游当前版本）；公司/校园网可能封禁非 HTTP 端口。 |
| 安装桥失败 | 看面板安装日志：缺 .NET 8 SDK / git / Python 时按提示安装；NuGet 或 GitHub 访问受限时先配置代理再重跑脚本（幂等）。 |
| `进房失败：房间不存在` | 网络模式下房间必须已存在；先让对方建房，或用 `op=host` 自己建。 |
| `开始对局失败` | 双方都要进房并 `ready`，且只有房主能 `start`。 |
| `选项当前不合法` | 局面已变化（对手响应/阶段推进）。按返回的最新选项重新选择，属于正常保护。 |
| 操作被拒绝（`ok:false`） | 引擎判定非法；插件会如实返回，不会假装成功。检查是否重复攻击、重复通常召唤等。 |
| 没有自动打牌 | 默认就是主 LLM 手动模式；要自动需在配置页打开 `ygo_auto` 并配好 Jev/laya。 |

---

## 9. 验证与测试

```bash
python3 smoke_test.py                       # 引擎/门控/通知等 80 项
python3 tests/ygo_ops_test.py               # 选项寻址/组卡/状态格式 40 项
python3 tests/ygo_network_test.py           # 无 Jev 网络全链路（真人第二客户端）62 项
python3 tests/ygo_auto_test.py              # 自动模式：零拒绝/无空请求 13 项
python3 tests/ygo_pokemon_test.py           # 状态构造等 29 项
python3 tests/consistency_check.py          # 前后端配置一致性 188 项
python3 tests/ygo_demo.py                   # 可观看的双模式实战演示
KIRAAI_ROOT=/path/to/KiraAI python3 tests/kira_real_load_test.py   # 真实框架加载 20 项
python3 tests/ygo_installer_test.py           # 插件侧桥安装/启动/停止 11 项

# 真实引擎桥（.NET 8）
sh plugin/scripts/install_ygo_bridge.sh       # 从零一键安装（已实测通过）
~/KiraYgoBridge/selftest.sh                   # 引擎语义自测 27 项（选项↔引擎响应一致）
```

真实引擎桥接入后，建议先用 `game_ygo_duel op=state` 核对它返回的手牌/场面/选项是否与客户端一致，再开始自动操作。
