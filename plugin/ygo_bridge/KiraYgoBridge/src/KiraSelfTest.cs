using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using WindBot;
using WindBot.Game;
using WindBot.Game.Kira;
using YGOSharp.OCGWrapper.Enums;

namespace KiraYgoBridge
{
    /// <summary>Offline semantic checks: option lists map to the exact engine
    /// responses, and the decision pump validates submissions. No server needed.</summary>
    public static class KiraSelfTest
    {
        private static int _pass;
        private static int _fail;

        private static void Check(string name, bool cond, string detail = "")
        {
            if (cond) { _pass++; Console.WriteLine("  ✓ " + name); }
            else { _fail++; Console.WriteLine("  ✗ " + name + " " + detail); }
        }

        public static int Run()
        {
            var duel = new Duel();
            var ai = new GameAI(duel, "default", null, null, null, Program.AssetPath);
            var card = new ClientCard(46986414, CardLocation.Hand, 0, 0);
            card.ActionIndex[(int)MainPhaseAction.MainAction.Summon] = 3;
            card.ActionIndex[(int)MainPhaseAction.MainAction.SetMonster] = 1;

            var main = new MainPhase();
            main.SummonableCards.Add(card);
            main.MonsterSetableCards.Add(card);
            main.CanBattlePhase = true;
            main.CanEndPhase = true;
            var idle = KiraOptions.Idle(ai, main);
            Check("idle option count", idle.Options.Count == 4, idle.Options.Count.ToString());
            var summon = (MainPhaseAction)idle.Apply(new[] { 0 });
            var expected = new MainPhaseAction(MainPhaseAction.MainAction.Summon, card.ActionIndex);
            Check("summon ToValue identical", summon.ToValue() == expected.ToValue(),
                  $"{summon.ToValue()} vs {expected.ToValue()}");
            Check("summon index preserved", summon.Index == 3, summon.Index.ToString());
            var set = (MainPhaseAction)idle.Apply(new[] { 1 });
            Check("set maps to SetMonster", set.Action == MainPhaseAction.MainAction.SetMonster && set.Index == 1);
            Check("battle switch option", ((MainPhaseAction)idle.Apply(new[] { 2 })).Action == MainPhaseAction.MainAction.ToBattlePhase);
            Check("end switch option", ((MainPhaseAction)idle.Apply(new[] { 3 })).Action == MainPhaseAction.MainAction.ToEndPhase);
            Check("option carries card name", idle.Options[0]["card"].ToString().Length > 0);
            Check("option carries effect text", idle.Options[0].ContainsKey("effect_text"),
                  string.Join(",", idle.Options[0].Keys));

            var battle = new BattlePhase();
            battle.AttackableCards.Add(card);
            battle.CanEndPhase = true;
            var battleDecision = KiraOptions.Battle(ai, battle);
            var attack = (BattlePhaseAction)battleDecision.Apply(new[] { 0 });
            Check("attack ToValue identical",
                  attack.ToValue() == new BattlePhaseAction(BattlePhaseAction.BattleAction.Attack, card.ActionIndex).ToValue());

            var chain = KiraOptions.Chain(ai, new List<ClientCard> { card }, new List<long> { 0 }, false);
            Check("chain offers pass", chain.Options.Count == 2);
            Check("chain activate index", (int)chain.Apply(new[] { 0 }) == 0);
            Check("chain pass is -1", (int)chain.Apply(new[] { 1 }) == -1);
            var forced = KiraOptions.Chain(ai, new List<ClientCard> { card }, new List<long> { 0 }, true);
            Check("forced chain has no pass", forced.Options.Count == 1);

            Check("yes/no mapping", (bool)KiraOptions.YesNo(0).Apply(new[] { 0 })
                  && !(bool)KiraOptions.YesNo(0).Apply(new[] { 1 }));
            Check("rps mapping", (int)KiraOptions.RockPaperScissors().Apply(new[] { 2 }) == 3);

            var multi = KiraOptions.SelectCards(new List<ClientCard> { card, card }, 1, 2, 0, false);
            Check("multi select two", ((List<ClientCard>)multi.Apply(new[] { 0, 1 })).Count == 2);
            Check("multi min/max recorded", multi.Min == 1 && multi.Max == 2);

            // Decision pump: invalid submissions rejected, valid one resumes the engine thread.
            var session = new KiraSession { DecisionTimeoutMs = 5000 };
            var pendingTask = Task.Run(() => session.AwaitDecision(idle, () => (object)null));
            Thread.Sleep(150);
            Check("stale option rejected", session.Submit("99")["ok"].Equals(false));
            Check("non-numeric rejected", session.Submit("abc")["ok"].Equals(false));
            Check("multi index rejected for single", session.Submit("0,1")["ok"].Equals(false));
            Check("valid submission accepted", session.Submit("0")["ok"].Equals(true));
            var completed = pendingTask.Wait(3000);
            Check("engine thread resumed", completed && pendingTask.Result is MainPhaseAction);
            Check("second submit has no pending", session.Submit("0")["ok"].Equals(false));

            // Deck building validation (writes into the asset Decks dir).
            var bad = KiraSession.BuildDeck("kira_selftest_bad", null,
                Enumerable.Repeat("46986414", 40).ToList(), new List<string>(), new List<string>());
            Check("over-3 copies rejected", bad["ok"].Equals(false));
            var shortDeck = KiraSession.BuildDeck("kira_selftest_short", null,
                new List<string> { "46986414" }, new List<string>(), new List<string>());
            Check("short deck rejected", shortDeck["ok"].Equals(false));
            var goodMain = new List<string>();
            foreach (var id in new[] { "46986414", "89631139", "87796900", "70781052", "40044918",
                                       "5318639", "55144522", "83764718", "04206964", "44095762" })
                goodMain.AddRange(Enumerable.Repeat(id, 3));
            goodMain.AddRange(Enumerable.Range(0, 10).Select(i => (90000000 + i).ToString()));
            var good = KiraSession.BuildDeck("kira_selftest", null, goodMain, new List<string>(), new List<string>());
            Check("valid deck accepted", good["ok"].Equals(true), string.Join(",", good.Select(k => k.Key + "=" + k.Value)));
            Check("deck file written", System.IO.File.Exists(System.IO.Path.Combine(KiraSession.DecksDir, "kira_selftest.ydk")));

            Console.WriteLine($"\n== {_pass} passed, {_fail} failed ==");
            return _fail == 0 ? 0 : 1;
        }
    }
}
