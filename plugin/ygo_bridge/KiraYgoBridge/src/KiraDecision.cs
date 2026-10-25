using System;
using System.Collections.Generic;
using System.Threading;
using WindBot.Game;

namespace WindBot.Game.Kira
{
    /// <summary>One engine question, exposed to the HTTP client as legal options.</summary>
    public sealed class KiraDecision
    {
        public string Kind = "generic";
        public string Hint = "";
        public List<Dictionary<string, object>> Options = new();
        public int Min = 1;
        public int Max = 1;
        public int? Sum;
        public bool Cancelable;
        public Func<int[], object> Apply;
        public bool Multi => Max != 1 || Min != 1 || Sum.HasValue;
        public DateTime CreatedAt = DateTime.UtcNow;
    }

    /// <summary>Decision hook: engine calls us instead of the built-in AI.</summary>
    public static class KiraBridge
    {
        public static T Decide<T>(GameAI ai, Func<GameAI, T> fallback, Func<KiraDecision> build)
        {
            var session = KiraSession.Current;
            if (session == null || !session.ExternalControl)
                return fallback(ai);
            KiraDecision decision = null;
            try { decision = build(); }
            catch (Exception ex) { session.Log("选项构造失败，回退内置 AI: " + ex.Message); }
            if (decision == null || (decision.Options.Count == 0 && !decision.Cancelable))
                return fallback(ai);
            return session.AwaitDecision(decision, () => fallback(ai));
        }
    }

    /// <summary>Room lifecycle gates: auto-ready/auto-start become explicit tool calls.</summary>
    public static class KiraRoom
    {
        public static void ReadyGate(GameClient game, Action sendReady)
        {
            var session = KiraSession.Current;
            if (session == null || !session.ExternalControl)
            {
                sendReady();
                return;
            }
            session.OnReadyGate(sendReady);
        }

        public static void StartGate(GameClient game, Action sendStart)
        {
            var session = KiraSession.Current;
            if (session == null || !session.ExternalControl)
            {
                sendStart();
                return;
            }
            session.OnStartGate(sendStart);
        }
    }
}
