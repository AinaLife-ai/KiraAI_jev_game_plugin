using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using WindBot.Game.AI;
using YGOSharp.OCGWrapper;

namespace WindBot.Game.Kira
{
    /// <summary>One bridge session = one seat in one room. Owns the GameClient
    /// thread, the pending engine decision, room gates and the public snapshot.</summary>
    public sealed class KiraSession
    {
        public static KiraSession Current;
        public bool ExternalControl = true;
        public int DecisionTimeoutMs = 110000;

        public GameClient Client;
        public GameBehavior Behavior;
        public string RoomName = "";
        public string Server = "";
        public string DeckName = "";
        public string PlayerName = "KiraAI";

        private readonly object _sync = new object();
        private KiraDecision _pending;
        private int[] _indices;
        private readonly ManualResetEventSlim _signal = new ManualResetEventSlim(false);
        private Action _readyAction;
        private Action _startAction;
        private bool _wantReady;
        private bool _wantStart;
        private bool _duelActive;
        private bool _ended;
        private string _winner = "";
        private Thread _thread;
        private readonly List<string> _log = new List<string>();
        private string _lastError = "";

        public KiraSession()
        {
            Current = this;
        }

        public string LastError => _lastError;

        public void Log(string message)
        {
            lock (_sync)
            {
                _log.Add(message);
                if (_log.Count > 40) _log.RemoveAt(0);
            }
            Logger.WriteLine("[kira] " + message);
        }

        // ---- decision pump -------------------------------------------------

        public T AwaitDecision<T>(KiraDecision decision, Func<T> fallback)
        {
            lock (_sync)
            {
                _pending = decision;
                _indices = null;
                _signal.Reset();
            }
            Log($"等待决策[{decision.Kind}] {decision.Hint}（{decision.Options.Count} 个选项）");
            bool signaled;
            try { signaled = _signal.Wait(DecisionTimeoutMs); }
            finally { }
            object result;
            if (!signaled)
            {
                Log("决策超时，暂由内置 AI 接管这一步");
                lock (_sync) { _pending = null; }
                return fallback();
            }
            try
            {
                int[] indices;
                KiraDecision current;
                lock (_sync) { indices = _indices; current = _pending; }
                result = current.Apply(indices);
            }
            catch (Exception ex)
            {
                Log("选项执行失败，回退内置 AI: " + ex.Message);
                lock (_sync) { _pending = null; }
                return fallback();
            }
            lock (_sync)
            {
                Log($"已执行[{decision.Kind}] 选项 {string.Join(",", _indices)}");
                _pending = null;
                _indices = null;
            }
            return (T)result;
        }

        public Dictionary<string, object> Submit(string option)
        {
            KiraDecision decision;
            lock (_sync) decision = _pending;
            if (decision == null)
                return Err("当前没有等待你决策的问题（局面已推进），请先 state 刷新");
            var indices = new List<int>();
            foreach (string piece in option.Split(',', StringSplitOptions.RemoveEmptyEntries))
            {
                string text = piece.Trim().TrimStart('#');
                if (!int.TryParse(text, out int value))
                    return Err($"无法解析选项编号：{piece}");
                indices.Add(value);
            }
            if (indices.Count == 0 && !decision.Cancelable)
                return Err("空选择不被允许");
            bool cancelOnly = indices.Count == 1 && indices[0] == decision.Options.Count - 1
                              && decision.Options[indices[0]]["action"].ToString() == "cancel";
            if (!decision.Multi && indices.Count != 1 && !(indices.Count == 0 && decision.Cancelable))
                return Err("该决策只能单选");
            if (decision.Multi && !cancelOnly)
            {
                if (indices.Count < decision.Min || indices.Count > decision.Max)
                    return Err($"需要选择 {decision.Min}-{decision.Max} 张，当前 {indices.Count} 张");
            }
            foreach (int i in indices)
                if (i < 0 || i >= decision.Options.Count)
                    return Err($"选项 {i} 不存在（共 {decision.Options.Count} 项）");
            lock (_sync)
            {
                if (_pending == null)
                    return Err("决策已被处理，请刷新");
                _indices = indices.ToArray();
                _signal.Set();
            }
            return Ok(new Dictionary<string, object> { ["accepted"] = true });
        }

        // ---- room lifecycle -------------------------------------------------

        public Dictionary<string, object> HostOrJoin(string room, string deck, string server,
            string password, string name, string mode)
        {
            try
            {
                Close();
                string deckPath;
                var deckResult = ResolveDeck(deck, out deckPath);
                if (deckResult != null)
                    return deckResult;
                string host = "127.0.0.1";
                int port = 7911;
                if (!string.IsNullOrWhiteSpace(server))
                {
                    var parts = server.Split(':');
                    host = parts[0];
                    if (parts.Length > 1) int.TryParse(parts[1], out port);
                }
                if (port == 0) port = 7911;
                string roomInfo = room ?? "";
                if (!string.IsNullOrEmpty(password))
                    roomInfo = roomInfo + "#" + password;
                var info = new WindBotInfo
                {
                    Name = string.IsNullOrWhiteSpace(name) ? PlayerName : name,
                    Deck = null,
                    DeckFile = deckPath,
                    Host = host,
                    Port = port,
                    HostInfo = roomInfo,
                    Chat = true,
                };
                RoomName = roomInfo;
                Server = $"{host}:{port}";
                DeckName = deck ?? "";
                PlayerName = info.Name;
                _ended = false;
                _duelActive = false;
                _winner = "";
                _wantReady = _wantStart = false;
                _readyAction = _startAction = null;
                Client = new GameClient(info);
                Behavior = Client.Behavior;
                _thread = new Thread(RunClient) { IsBackground = true };
                _thread.Start();
                Log($"连接服务器 {Server} 房间「{roomInfo}」卡组 {DeckName}");
                // Wait for the room handshake (or a rejected/closed connection).
                var deadline = DateTime.UtcNow.AddSeconds(12);
                bool everConnected = false;
                while (DateTime.UtcNow < deadline)
                {
                    if (Client?.Connection != null && Client.Connection.IsConnected)
                        everConnected = true;
                    if (Behavior?.BridgeRoom != null && Behavior.BridgeRoom.Position >= 0)
                        return Ok(new Dictionary<string, object>
                        {
                            ["room"] = roomInfo,
                            ["opponent"] = OpponentName(),
                            ["players"] = PlayerNames(),
                        });
                    if (everConnected && (Client?.Connection == null || !Client.Connection.IsConnected))
                        break;  // server accepted TCP then rejected/closed the room join
                    Thread.Sleep(150);
                }
                if (!everConnected || Client?.Connection == null || !Client.Connection.IsConnected)
                {
                    Close();
                    return Err($"连接或进房失败：{Server} 不可达或服务器拒绝（检查地址/房名/密码/协议版本）");
                }
                return Ok(new Dictionary<string, object>
                {
                    ["room"] = roomInfo, ["pending"] = "已连接，等待服务器分配座位",
                    ["opponent"] = OpponentName(), ["players"] = PlayerNames(),
                });
            }
            catch (Exception ex)
            {
                Close();
                return Err("建房/进房异常: " + ex.Message);
            }
        }

        private void RunClient()
        {
            try
            {
                Client.Start();
                while (Client != null && Client.Connection != null && Client.Connection.IsConnected)
                {
                    try { Client.Tick(); }
                    catch (Exception ex) { Log("数据包处理异常: " + ex.Message); }
                    Thread.Sleep(15);
                }
            }
            catch (Exception ex)
            {
                Log("连接中断: " + ex.Message);
            }
            finally
            {
                if (_duelActive)
                {
                    _duelActive = false;
                    _ended = true;
                }
                Log("与服务器断开连接");
            }
        }

        public void OnReadyGate(Action action)
        {
            lock (_sync) _readyAction = action;
            Log("已进入准备阶段（等待 op=ready）");
            if (_wantReady) Ready();
        }

        public void OnStartGate(Action action)
        {
            lock (_sync) _startAction = action;
            if (_wantStart) StartDuel();
        }

        public Dictionary<string, object> Ready()
        {
            if (Client == null)
                return Err("未在房间内（先 host/join）");
            Action action;
            lock (_sync)
            {
                _wantReady = true;
                action = _readyAction;
                _readyAction = null;
            }
            if (action == null)
            {
                if (Behavior?.BridgeRoom != null && Behavior.BridgeRoom.Position >= 0)
                {
                    Behavior.BridgeSendReady();
                    Log("已准备");
                    return Ok(new Dictionary<string, object> { ["ready"] = true });
                }
                return Ok(new Dictionary<string, object> { ["ready"] = "已登记，待服务器分配座位后自动准备" });
            }
            action();
            Log("已准备");
            return Ok(new Dictionary<string, object> { ["ready"] = true });
        }

        public Dictionary<string, object> StartDuel()
        {
            Action action;
            lock (_sync)
            {
                _wantStart = true;
                action = _startAction;
                _startAction = null;
            }
            if (action == null)
            {
                var room = Behavior?.BridgeRoom;
                if (room != null && room.IsHost && room.Players >= 2 && room.IsReady[0] && room.IsReady[1])
                {
                    Behavior.BridgeSendStart();
                    Log("房主开始对局");
                    return Ok(new Dictionary<string, object> { ["started"] = true });
                }
                return Err("现在不能开始：需要双方进房并 ready，且你是房主（可先 state 查看房间状态）");
            }
            action();
            Log("房主开始对局");
            return Ok(new Dictionary<string, object> { ["started"] = true });
        }

        public Dictionary<string, object> SetDeck(string deck)
        {
            string deckPath;
            var error = ResolveDeck(deck, out deckPath);
            if (error != null) return error;
            DeckName = deck;
            if (Client == null || Behavior == null)
                return Ok(new Dictionary<string, object> { ["deck"] = deck, ["note"] = "将在进房时使用" });
            if (_duelActive)
                return Err("对局进行中不能更换卡组");
            var loaded = Deck.Load(deckPath);
            if (loaded == null)
                return Err("卡组文件读取失败: " + deck);
            Behavior.BridgeUpdateDeck(loaded);
            Log("已更换卡组并重发 deck: " + deck);
            return Ok(new Dictionary<string, object> { ["deck"] = deck });
        }

        private Dictionary<string, object> ResolveDeck(string deck, out string deckPath)
        {
            deckPath = null;
            if (string.IsNullOrWhiteSpace(deck))
            {
                string fallback = Path.Combine(Program.AssetPath, "Decks", "AI_BlueEyes.ydk");
                if (File.Exists(fallback)) deckPath = fallback;
                return null;
            }
            string name = deck.EndsWith(".ydk", StringComparison.OrdinalIgnoreCase)
                ? deck.Substring(0, deck.Length - 4) : deck;
            string candidate = Path.IsPathRooted(name) ? name + ".ydk"
                : Path.Combine(Program.AssetPath, "Decks", name + ".ydk");
            if (!File.Exists(candidate))
                return Err($"卡组不存在：{deck}（先 build_deck 或 decks 查看）");
            deckPath = candidate;
            return null;
        }

        public void Close()
        {
            try { Client?.Connection?.Close(); } catch { }
            Client = null;
            Behavior = null;
            lock (_sync)
            {
                _pending = null;
                _signal.Set();
            }
        }

        public void OnDuelStart()
        {
            _duelActive = true;
            _ended = false;
            Log("对局开始");
        }

        public void OnDuelEnd()
        {
            _duelActive = false;
            _ended = true;
            Log("对局结束");
        }

        public void OnWin(int result)
        {
            _winner = result == 0 ? "我方" : result == 1 ? "对手" : "平局";
            Log("胜负结果: " + _winner);
        }

        public void OnChat(string who, string message)
        {
            Log($"[聊天] {who}: {message}");
        }

        // ---- decks -----------------------------------------------------------

        public static string DecksDir => Path.Combine(Program.AssetPath ?? "", "Decks");

        public static Dictionary<string, object> ListDecks()
        {
            var list = new List<Dictionary<string, object>>();
            try
            {
                if (Directory.Exists(DecksDir))
                {
                    foreach (string file in Directory.GetFiles(DecksDir, "*.ydk").OrderBy(f => f).Take(200))
                    {
                        var zones = ReadYdk(File.ReadAllLines(file));
                        list.Add(new Dictionary<string, object>
                        {
                            ["name"] = Path.GetFileName(file),
                            ["main"] = zones["main"].Count,
                            ["extra"] = zones["extra"].Count,
                            ["side"] = zones["side"].Count,
                        });
                    }
                }
            }
            catch (Exception ex) { return Err("读取卡组目录失败: " + ex.Message); }
            return new Dictionary<string, object> { ["ok"] = true, ["decks"] = list };
        }

        public static Dictionary<string, object> BuildDeck(string name, string ydk,
            List<string> main, List<string> extra, List<string> side)
        {
            try
            {
                if (string.IsNullOrWhiteSpace(name)) return Err("缺少卡组名");
                name = Path.GetFileName(name);
                if (!name.EndsWith(".ydk", StringComparison.OrdinalIgnoreCase)) name += ".ydk";
                if (!string.IsNullOrEmpty(ydk))
                {
                    var parsed = ReadYdk(ydk.Split('\n'));
                    main = parsed["main"];
                    extra = parsed["extra"];
                    side = parsed["side"];
                }
                main ??= new List<string>();
                extra ??= new List<string>();
                side ??= new List<string>();
                if (main.Count < 40 || main.Count > 60)
                    return Err($"主卡组需 40-60 张，当前 {main.Count}");
                if (extra.Count > 15 || side.Count > 15)
                    return Err("额外/副卡组最多 15 张");
                var counts = new Dictionary<string, int>();
                foreach (string card in main.Concat(extra).Concat(side))
                {
                    if (!long.TryParse(card, out _))
                        return Err($"卡密必须是数字：{card}");
                    counts[card] = counts.GetValueOrDefault(card) + 1;
                    if (counts[card] > 3)
                        return Err($"同名卡超过 3 张：{card}");
                }
                Directory.CreateDirectory(DecksDir);
                var lines = new List<string> { "#created by KiraYgoBridge", "#main" };
                lines.AddRange(main);
                lines.Add("#extra");
                lines.AddRange(extra);
                lines.Add("!side");
                lines.AddRange(side);
                string path = Path.Combine(DecksDir, name);
                File.WriteAllLines(path, lines);
                return new Dictionary<string, object>
                {
                    ["ok"] = true, ["name"] = name,
                    ["main"] = main.Count, ["extra"] = extra.Count, ["side"] = side.Count,
                };
            }
            catch (Exception ex) { return Err("组卡失败: " + ex.Message); }
        }

        private static Dictionary<string, List<string>> ReadYdk(IEnumerable<string> lines)
        {
            var zones = new Dictionary<string, List<string>>
            {
                ["main"] = new List<string>(), ["extra"] = new List<string>(), ["side"] = new List<string>(),
            };
            string zone = "main";
            foreach (string raw in lines)
            {
                string line = raw.Trim();
                if (line.StartsWith("#main")) { zone = "main"; continue; }
                if (line.StartsWith("#extra")) { zone = "extra"; continue; }
                if (line.StartsWith("!side")) { zone = "side"; continue; }
                if (line.StartsWith("#") || line.StartsWith("!")) continue;
                if (long.TryParse(line, out _)) zones[zone].Add(line);
            }
            return zones;
        }

        // ---- snapshot --------------------------------------------------------

        public string OpponentName()
        {
            var room = Behavior?.BridgeRoom;
            if (room == null) return "";
            int pos = room.Position;
            if (pos == 0 && room.Names.Length > 1) return room.Names[1] ?? "";
            if (pos >= 0 && room.Names.Length > 0) return room.Names[0] ?? "";
            return "";
        }

        public List<string> PlayerNames()
        {
            var room = Behavior?.BridgeRoom;
            if (room == null) return new List<string>();
            return room.Names.Where(n => !string.IsNullOrEmpty(n)).ToList();
        }

        private static string PhaseName(YGOSharp.OCGWrapper.Enums.DuelPhase phase)
        {
            switch (phase)
            {
                case YGOSharp.OCGWrapper.Enums.DuelPhase.Draw: return "draw";
                case YGOSharp.OCGWrapper.Enums.DuelPhase.Standby: return "standby";
                case YGOSharp.OCGWrapper.Enums.DuelPhase.Main1: return "main1";
                case YGOSharp.OCGWrapper.Enums.DuelPhase.Main2: return "main2";
                case YGOSharp.OCGWrapper.Enums.DuelPhase.End: return "end";
                default: return "battle";
            }
        }

        private static Dictionary<string, object> CardView(ClientCard card, bool hideUnknown)
        {
            var d = new Dictionary<string, object>();
            bool faceDown = (card.Position & 0xA) != 0 && (card.Position & 0x5) == 0;
            if (hideUnknown && faceDown)
            {
                d["name"] = "里侧卡";
                d["hidden"] = true;
                d["position"] = KiraOptions.PositionName(card.Position);
                return d;
            }
            d["name"] = card.Name;
            d["code"] = card.Id;
            if (card.Attack != 0 || card.Defense != 0)
            {
                d["atk"] = card.Attack;
                d["def"] = card.Defense;
            }
            if (card.Level > 0) d["level"] = card.Level;
            if (card.Rank > 0) d["rank"] = card.Rank;
            if (card.LinkCount > 0) d["link_rating"] = card.LinkCount;
            d["position"] = KiraOptions.PositionName(card.Position);
            var named = NamedCard.Get(card.Id);
            if (!string.IsNullOrEmpty(named?.Description))
            {
                string text = named.Description;
                d["effect_text"] = text.Length > 200 ? text.Substring(0, 200) : text;
            }
            return d;
        }

        private static List<Dictionary<string, object>> Zone(IEnumerable<ClientCard> cards, bool hide)
        {
            var list = new List<Dictionary<string, object>>();
            if (cards == null) return list;
            foreach (var card in cards)
                if (card != null) list.Add(CardView(card, hide));
            return list;
        }

        private static List<string> Names(IEnumerable<ClientCard> cards)
        {
            var list = new List<string>();
            if (cards == null) return list;
            foreach (var card in cards)
                if (card != null) list.Add(card.Name);
            return list;
        }

        public Dictionary<string, object> GetState()
        {
            var room = Behavior?.BridgeRoom;
            var duel = Behavior?.BridgeDuel;
            KiraDecision pending;
            string[] logs;
            lock (_sync)
            {
                pending = _pending;
                logs = _log.Skip(Math.Max(0, _log.Count - 12)).ToArray();
            }
            string stage;
            if (Client == null) stage = "idle";
            else if (_duelActive) stage = "dueling";
            else if (_ended) stage = "ended";
            else if (room == null || room.Position < 0) stage = "joining";
            else if (PlayerNames().Count < 2) stage = "joining";
            else stage = "readying";
            var readyMap = new Dictionary<string, bool>();
            if (room != null)
            {
                for (int i = 0; i < room.Names.Length && i < room.IsReady.Length; i++)
                    if (!string.IsNullOrEmpty(room.Names[i]))
                        readyMap[room.Names[i]] = room.IsReady[i];
            }
            var state = new Dictionary<string, object>
            {
                ["ok"] = true,
                ["stage"] = stage,
                ["room"] = new Dictionary<string, object>
                {
                    ["name"] = RoomName,
                    ["server"] = Server,
                    ["players"] = PlayerNames(),
                    ["is_host"] = room?.IsHost ?? false,
                    ["ready"] = readyMap,
                    ["started"] = _duelActive,
                },
            };
            var game = new Dictionary<string, object>
            {
                ["is_duel_active"] = _duelActive,
                ["winner"] = _winner,
                ["opponent"] = OpponentName(),
                ["legal_actions"] = pending?.Options ?? new List<Dictionary<string, object>>(),
                ["public_log"] = logs,
            };
            if (pending != null)
            {
                game["decision"] = new Dictionary<string, object>
                {
                    ["kind"] = pending.Kind,
                    ["hint"] = pending.Hint,
                    ["min"] = pending.Min,
                    ["max"] = pending.Max,
                    ["cancelable"] = pending.Cancelable,
                };
                if (pending.Multi)
                    game["multi_select"] = new Dictionary<string, object>
                    {
                        ["min"] = pending.Min, ["max"] = pending.Max,
                        ["sum"] = pending.Sum.HasValue ? pending.Sum.Value : 0,
                        ["cancelable"] = pending.Cancelable,
                    };
                if (pending.Kind == "chain" || pending.Kind == "effect_yn" ||
                    pending.Kind == "yes_no" || pending.Kind == "battle_replay")
                    game["response_window"] = true;
            }
            if (duel != null)
            {
                var me = duel.Fields[0];
                var op = duel.Fields[1];
                game["my_lp"] = me.LifePoints;
                game["op_lp"] = op.LifePoints;
                game["turn"] = duel.Turn;
                game["phase"] = PhaseName(duel.Phase);
                game["is_my_turn"] = duel.Player == 0;
                game["opponent_hand_count"] = op.Hand.Count;
                game["hand"] = Zone(me.Hand, false);
                game["field_my"] = Zone(me.MonsterZone.Concat(me.SpellZone), false);
                game["field_op"] = Zone(op.MonsterZone.Concat(op.SpellZone), true);
                game["grave_my"] = Names(me.Graveyard);
                game["grave_op"] = Names(op.Graveyard);
                game["banished_my"] = Names(me.Banished);
                game["banished_op"] = Names(op.Banished);
                game["deck_count"] = me.Deck.Count;
                game["op_deck_count"] = op.Deck.Count;
                game["extra_deck"] = Names(me.ExtraDeck);
            }
            state["game"] = game;
            return state;
        }

        public static Dictionary<string, object> Ok(Dictionary<string, object> payload)
        {
            payload["ok"] = true;
            return payload;
        }

        public static Dictionary<string, object> Err(string message)
        {
            return new Dictionary<string, object> { ["ok"] = false, ["error"] = message };
        }
    }
}
