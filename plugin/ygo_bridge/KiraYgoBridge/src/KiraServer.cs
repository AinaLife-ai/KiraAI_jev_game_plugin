using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Threading;
using WindBot;
using WindBot.Game;
using WindBot.Game.Kira;

namespace KiraYgoBridge
{
    /// <summary>HTTP front-end implementing the exact contract the KiraAI plugin
    /// expects (see guides/YGO指南.md). Single process = one seat per room.</summary>
    public static class KiraServer
    {
        private static KiraSession _session;
        private static string _defaultServer = "";
        private static string _defaultDeck = "";
        private static string _defaultName = "KiraAI";

        public static void Main(string[] args)
        {
            int port = 5274;
            string bind = "127.0.0.1";
            string asset = AppContext.BaseDirectory;
            string cdb = "";
            int timeout = 110000;
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                string Next() => i + 1 < args.Length ? args[++i] : "";
                if (a == "--port") int.TryParse(Next(), out port);
                else if (a == "--bind") bind = Next();
                else if (a == "--server") _defaultServer = Next();
                else if (a == "--deck") _defaultDeck = Next();
                else if (a == "--name") _defaultName = Next();
                else if (a == "--asset") asset = Next();
                else if (a == "--cdb") cdb = Next();
                else if (a == "--timeout") int.TryParse(Next(), out timeout);
            }
            Program.AssetPath = asset;
            Program.Rand = new Random();
            if (args.Contains("--selftest"))
            {
                if (File.Exists(cdb)) Program.InitDatas(cdb, null);
                Environment.Exit(KiraSelfTest.Run());
            }
            if (string.IsNullOrEmpty(cdb)) cdb = Path.Combine(asset, "cards.cdb");
            if (File.Exists(cdb))
                Program.InitDatas(cdb, null);
            else
            {
                Logger.WriteErrorLine("[kira] cards.cdb 未找到（卡名/效果将显示为 id）：" + cdb);
                DecksManager.Init();
            }
            _session = new KiraSession { DecisionTimeoutMs = timeout };

            var listener = new HttpListener();
            listener.Prefixes.Add($"http://{bind}:{port}/");
            listener.Start();
            Logger.WriteLine($"[kira] KiraYgoBridge listening on http://{bind}:{port}/ " +
                             $"(asset={asset}, default server={_defaultServer})");
            while (true)
            {
                HttpListenerContext ctx;
                try { ctx = listener.GetContext(); }
                catch { break; }
                ThreadPool.QueueUserWorkItem(_ => Handle(ctx));
            }
        }

        private static void Handle(HttpListenerContext ctx)
        {
            string path = ctx.Request.Url.AbsolutePath.TrimEnd('/');
            if (path == "") path = "/health";
            Dictionary<string, object> result;
            try
            {
                var body = ReadBody(ctx);
                result = Route(ctx.Request.HttpMethod.ToUpperInvariant(), path, body, ctx);
            }
            catch (Exception ex)
            {
                result = KiraSession.Err("桥内部错误: " + ex.Message);
            }
            try
            {
                string json = JsonSerializer.Serialize(result);
                byte[] data = Encoding.UTF8.GetBytes(json);
                ctx.Response.ContentType = "application/json; charset=utf-8";
                ctx.Response.ContentLength64 = data.Length;
                ctx.Response.StatusCode = result.ContainsKey("_http") ? 400 : 200;
                ctx.Response.OutputStream.Write(data, 0, data.Length);
                ctx.Response.Close();
            }
            catch { }
        }

        private static Dictionary<string, object> ReadBody(HttpListenerContext ctx)
        {
            try
            {
                if (ctx.Request.HttpMethod.Equals("GET", StringComparison.OrdinalIgnoreCase))
                    return new Dictionary<string, object>();
                using var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8);
                string text = reader.ReadToEnd();
                if (string.IsNullOrWhiteSpace(text)) return new Dictionary<string, object>();
                var doc = JsonDocument.Parse(text);
                var map = new Dictionary<string, object>();
                foreach (var prop in doc.RootElement.EnumerateObject())
                {
                    map[prop.Name] = prop.Value.ValueKind switch
                    {
                        JsonValueKind.String => prop.Value.GetString(),
                        JsonValueKind.Number => prop.Value.TryGetInt64(out long n) ? n : prop.Value.GetDouble(),
                        JsonValueKind.True => true,
                        JsonValueKind.False => false,
                        JsonValueKind.Array => prop.Value.EnumerateArray().Select(e => e.ToString()).ToList(),
                        _ => null,
                    };
                }
                return map;
            }
            catch { return new Dictionary<string, object>(); }
        }

        private static string Str(Dictionary<string, object> body, string key, string fallback = "")
        {
            if (body.TryGetValue(key, out var value) && value != null)
            {
                string text = value.ToString();
                if (!string.IsNullOrWhiteSpace(text)) return text;
            }
            return fallback;
        }

        private static List<string> StrList(Dictionary<string, object> body, string key)
        {
            if (!body.TryGetValue(key, out var value) || value == null) return new List<string>();
            if (value is List<string> list) return list;
            return value.ToString().Split(',', StringSplitOptions.RemoveEmptyEntries)
                        .Select(s => s.Trim()).Where(s => s.Length > 0).ToList();
        }

        private static Dictionary<string, object> Route(string method, string path,
            Dictionary<string, object> body, HttpListenerContext ctx)
        {
            if (method == "GET")
            {
                switch (path)
                {
                    case "/health":
                        return new Dictionary<string, object> { ["ok"] = true, ["bridge"] = "KiraYgoBridge", ["windbot"] = "ProjectIgnis/windbot" };
                    case "/decks":
                        return KiraSession.ListDecks();
                    case "/state":
                        return _session.GetState();
                    case "/log":
                        return new Dictionary<string, object> { ["ok"] = true, ["log"] = _session.GetState()["game"] is Dictionary<string, object> g ? g["public_log"] : null };
                    case "/rooms":
                        var state = _session.GetState();
                        var room = state["room"] as Dictionary<string, object>;
                        return new Dictionary<string, object>
                        {
                            ["ok"] = true,
                            ["rooms"] = string.IsNullOrEmpty(_session.RoomName)
                                ? new List<object>()
                                : new List<object> { new Dictionary<string, object>
                                    {
                                        ["name"] = _session.RoomName,
                                        ["players"] = room?["players"],
                                        ["duel_active"] = (state["game"] as Dictionary<string, object>)?["is_duel_active"] ?? false,
                                    } },
                        };
                }
                return KiraSession.Err("未知端点 " + path);
            }

            switch (path)
            {
                case "/build_deck":
                    return KiraSession.BuildDeck(Str(body, "name"), Str(body, "ydk"),
                        StrList(body, "main"), StrList(body, "extra"), StrList(body, "side"));
                case "/host":
                case "/join":
                    return _session.HostOrJoin(
                        Str(body, "room"), Str(body, "deck", _defaultDeck),
                        Str(body, "server", _defaultServer), Str(body, "password"),
                        Str(body, "name", _defaultName), Str(body, "mode", "network"));
                case "/set_deck":
                    return _session.SetDeck(Str(body, "deck"));
                case "/ready":
                    return _session.Ready();
                case "/start":
                    return WithState(_session.StartDuel());
                case "/act":
                    var verdict = _session.Submit(Str(body, "option", Str(body, "action")));
                    if (verdict.TryGetValue("ok", out var ok) && ok is bool b && b)
                    {
                        // Give the engine thread a moment to consume the choice.
                        var deadline = DateTime.UtcNow.AddMilliseconds(2500);
                        while (DateTime.UtcNow < deadline)
                        {
                            var game = _session.GetState()["game"] as Dictionary<string, object>;
                            if (game?["decision"] == null) break;
                            Thread.Sleep(60);
                        }
                        return WithState(verdict);
                    }
                    return WithState(verdict);
                case "/leave":
                    _session.Close();
                    return new Dictionary<string, object> { ["ok"] = true };
                case "/surrender":
                    if (_session.Behavior == null) return KiraSession.Err("未在房间内");
                    _session.Behavior.BridgeSurrender();
                    return new Dictionary<string, object> { ["ok"] = true };
                case "/chat":
                    if (_session.Client == null) return KiraSession.Err("未在房间内");
                    _session.Client.Chat(Str(body, "message"), true);
                    return new Dictionary<string, object> { ["ok"] = true };
            }
            return KiraSession.Err("未知端点 " + path);
        }

        private static Dictionary<string, object> WithState(Dictionary<string, object> result)
        {
            if (result.TryGetValue("ok", out var ok) && ok is bool b && b)
                result["state"] = _session.GetState();
            return result;
        }
    }
}
