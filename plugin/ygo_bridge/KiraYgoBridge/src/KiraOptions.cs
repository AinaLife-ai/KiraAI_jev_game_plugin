using System;
using System.Collections.Generic;
using WindBot.Game.AI;
using YGOSharp.OCGWrapper;
using YGOSharp.OCGWrapper.Enums;

namespace WindBot.Game.Kira
{
    /// <summary>Builds the option list for every engine decision point, and maps
    /// the chosen option index back to the exact engine response. The engine
    /// stays the rule owner; we only transport choices.</summary>
    public static class KiraOptions
    {
        public static Dictionary<string, object> Opt(string action, ClientCard card = null,
            string desc = null, string position = null, string phase = null,
            string target = null, object value = null, string effect = null)
        {
            var d = new Dictionary<string, object> { ["action"] = action };
            if (card != null)
            {
                d["card"] = card.Name;
                d["code"] = card.Id;
                if (card.Attack != 0 || card.Defense != 0)
                {
                    d["atk"] = card.Attack;
                    d["def"] = card.Defense;
                }
                if (card.Level > 0) d["level"] = card.Level;
                if (card.Rank > 0) d["rank"] = card.Rank;
                if (card.LinkCount > 0) d["link_rating"] = card.LinkCount;
                var named = NamedCard.Get(card.Id);
                var text = named?.Description ?? effect;
                if (!string.IsNullOrEmpty(text))
                    d["effect_text"] = text.Length > 220 ? text.Substring(0, 220) : text;
                if (string.IsNullOrEmpty(position))
                    position = PositionName(card.Position);
            }
            if (!string.IsNullOrEmpty(position)) d["position"] = position;
            if (!string.IsNullOrEmpty(phase)) d["phase"] = phase;
            if (!string.IsNullOrEmpty(target)) d["target"] = target;
            if (!string.IsNullOrEmpty(desc)) d["desc"] = desc;
            if (value != null) d["value"] = value;
            return d;
        }

        public static string PositionName(int position)
        {
            if ((position & (int)CardPosition.FaceUpAttack) != 0) return "attack";
            if ((position & (int)CardPosition.FaceUpDefence) != 0) return "defense";
            if ((position & (int)CardPosition.FaceDownAttack) != 0) return "face-down-atk";
            if ((position & (int)CardPosition.FaceDownDefence) != 0) return "face-down-def";
            return "face-down";
        }

        private static KiraDecision New(string kind, string hint)
        {
            return new KiraDecision { Kind = kind, Hint = hint };
        }

        // ---- main phase -------------------------------------------------

        public static KiraDecision Idle(GameAI ai, MainPhase main)
        {
            var d = New("idle", "主要阶段：召唤/盖放/发动/切换阶段");
            var apply = new List<Func<int[], object>>();
            void Add(Dictionary<string, object> opt, Func<int[], object> fn)
            {
                d.Options.Add(opt);
                apply.Add(fn);
            }
            foreach (var c in main.SummonableCards)
            {
                var card = c;
                Add(Opt("summon", card), _ => new MainPhaseAction(MainPhaseAction.MainAction.Summon, card.ActionIndex));
            }
            foreach (var c in main.SpecialSummonableCards)
            {
                var card = c;
                Add(Opt("special_summon", card), _ => new MainPhaseAction(MainPhaseAction.MainAction.SpSummon, card.ActionIndex));
            }
            foreach (var c in main.ReposableCards)
            {
                var card = c;
                Add(Opt("reposition", card), _ => new MainPhaseAction(MainPhaseAction.MainAction.Repos, card.ActionIndex));
            }
            foreach (var c in main.MonsterSetableCards)
            {
                var card = c;
                Add(Opt("set", card, position: "face-down"), _ => new MainPhaseAction(MainPhaseAction.MainAction.SetMonster, card.ActionIndex));
            }
            foreach (var c in main.SpellSetableCards)
            {
                var card = c;
                Add(Opt("set", card, position: "face-down"), _ => new MainPhaseAction(MainPhaseAction.MainAction.SetSpell, card.ActionIndex));
            }
            for (int i = 0; i < main.ActivableCards.Count; i++)
            {
                var card = main.ActivableCards[i];
                var desc = main.ActivableDescs[i];
                Add(Opt("activate", card, desc: "效果 " + desc), _ =>
                {
                    if (card.ActionActivateIndex.TryGetValue(desc, out int index))
                        return new MainPhaseAction(MainPhaseAction.MainAction.Activate, index);
                    return new MainPhaseAction(MainPhaseAction.MainAction.Activate, card.ActionIndex);
                });
            }
            if (main.CanBattlePhase)
                Add(Opt("switch", phase: "battle"), _ => new MainPhaseAction(MainPhaseAction.MainAction.ToBattlePhase));
            if (main.CanEndPhase)
                Add(Opt("switch", phase: "end"), _ => new MainPhaseAction(MainPhaseAction.MainAction.ToEndPhase));
            d.Apply = idx => apply[idx[0]](idx);
            return d;
        }

        public static KiraDecision Battle(GameAI ai, BattlePhase battle)
        {
            var d = New("battle", "战斗阶段：攻击/发动/切换阶段");
            var apply = new List<Func<int[], object>>();
            void Add(Dictionary<string, object> opt, Func<int[], object> fn)
            {
                d.Options.Add(opt);
                apply.Add(fn);
            }
            for (int i = 0; i < battle.ActivableCards.Count; i++)
            {
                var card = battle.ActivableCards[i];
                var desc = battle.ActivableDescs[i];
                Add(Opt("activate", card, desc: "效果 " + desc), _ => new BattlePhaseAction(BattlePhaseAction.BattleAction.Activate, card.ActionIndex));
            }
            foreach (var c in battle.AttackableCards)
            {
                var card = c;
                Add(Opt("attack", card, desc: card.CanDirectAttack ? "可直接攻击" : "攻击宣言"),
                    _ => new BattlePhaseAction(BattlePhaseAction.BattleAction.Attack, card.ActionIndex));
            }
            if (battle.CanMainPhaseTwo)
                Add(Opt("switch", phase: "main2"), _ => new BattlePhaseAction(BattlePhaseAction.BattleAction.ToMainPhaseTwo));
            if (battle.CanEndPhase)
                Add(Opt("switch", phase: "end"), _ => new BattlePhaseAction(BattlePhaseAction.BattleAction.ToEndPhase));
            d.Apply = idx => apply[idx[0]](idx);
            return d;
        }

        // ---- chain / yes-no / options ------------------------------------

        public static KiraDecision Chain(GameAI ai, IList<ClientCard> cards, IList<long> descs, bool forced)
        {
            var d = New("chain", forced ? "必须选择连锁" : "是否连锁？");
            var apply = new List<Func<int[], object>>();
            for (int i = 0; i < cards.Count; i++)
            {
                int index = i;
                var card = cards[i];
                d.Options.Add(Opt("activate", card, desc: "连锁效果 " + descs[i]));
                apply.Add(_ => index);
            }
            if (!forced)
            {
                d.Options.Add(Opt("pass", desc: "不连锁"));
                apply.Add(_ => -1);
            }
            d.Apply = idx => apply[idx[0]](idx);
            return d;
        }

        public static KiraDecision YesNo(long desc, bool effect = false, ClientCard card = null)
        {
            var d = New(effect ? "effect_yn" : "yes_no",
                        (effect ? "是否发动效果？" : "是否确认？") + " " + desc);
            if (card != null)
                d.Hint += "（" + card.Name + "）";
            d.Options.Add(Opt("yes", card, desc: "是"));
            d.Options.Add(Opt("no", desc: "否"));
            d.Apply = idx => idx[0] == 0;
            return d;
        }

        public static KiraDecision SelectOption(IList<long> options)
        {
            var d = New("option", "选择一个效果/选项");
            for (int i = 0; i < options.Count; i++)
                d.Options.Add(Opt("select_option", value: options[i], desc: "选项 " + options[i]));
            d.Apply = idx => idx[0];
            return d;
        }

        public static KiraDecision RockPaperScissors()
        {
            var d = New("rps", "猜拳决定先后手");
            d.Options.Add(Opt("select_option", value: 1, desc: "石头"));
            d.Options.Add(Opt("select_option", value: 2, desc: "剪刀"));
            d.Options.Add(Opt("select_option", value: 3, desc: "布"));
            d.Apply = idx => idx[0] + 1;
            return d;
        }

        public static KiraDecision FirstHand()
        {
            var d = New("first_hand", "选择先手还是后手");
            d.Options.Add(Opt("yes", desc: "先手"));
            d.Options.Add(Opt("no", desc: "后手"));
            d.Apply = idx => idx[0] == 0;
            return d;
        }

        public static KiraDecision BattleReplay()
        {
            var d = New("battle_replay", "是否进行战斗重放？");
            d.Options.Add(Opt("yes", desc: "重放战斗"));
            d.Options.Add(Opt("no", desc: "不重放"));
            d.Apply = idx => idx[0] == 0;
            return d;
        }

        // ---- multi-card selection ----------------------------------------

        public static KiraDecision SelectCards(IList<ClientCard> cards, int min, int max,
            long hint, bool cancelable, string kind = "select", int? sum = null)
        {
            var d = New(kind, $"选择 {min}-{max} 张卡" + (sum.HasValue ? $"（数值合计需 = {sum}）" : ""));
            d.Min = min;
            d.Max = max;
            d.Sum = sum;
            d.Cancelable = cancelable;
            d.Hint += $"；用 option=编号 或 编号1,编号2 提交{((cancelable && min == 0) ? "，空选=cancel" : "")}";
            for (int i = 0; i < cards.Count; i++)
            {
                var card = cards[i];
                d.Options.Add(Opt("select_card", card,
                    desc: $"Lv{card.Level}/R{card.Rank}/Link{card.LinkCount} ATK{card.Attack}"));
            }
            if (cancelable)
                d.Options.Add(Opt("cancel", desc: "取消/不选"));
            d.Apply = idx =>
            {
                var picked = new List<ClientCard>();
                foreach (int i in idx)
                {
                    if (i < 0 || i >= cards.Count)
                        continue;
                    picked.Add(cards[i]);
                }
                return picked;
            };
            return d;
        }

        // ---- position / place / announce ---------------------------------

        public static KiraDecision Position(int cardId, IList<CardPosition> positions)
        {
            var d = New("position", "选择表示形式");
            foreach (var pos in positions)
                d.Options.Add(Opt("select_position", position: PositionName((int)pos), value: (int)pos));
            d.Apply = idx => positions[idx[0]];
            return d;
        }

        public static KiraDecision Place(long cardId, int player, CardLocation location, int available)
        {
            var d = New("place", "选择卡片放置的区域");
            d.Options.Add(Opt("select_place", value: 0, desc: "自动选择（推荐）"));
            for (int zone = 0; zone <= 6; zone++)
            {
                int bit = 1 << zone;
                if ((available & bit) != 0)
                    d.Options.Add(Opt("select_place", value: bit, desc: $"区域 {zone}"));
            }
            d.Apply = idx =>
            {
                var opt = d.Options[idx[0]];
                return Convert.ToInt32(opt["value"]);
            };
            return d;
        }

        public static KiraDecision AnnounceNumber(IList<int> numbers)
        {
            var d = New("announce_number", "宣言一个数字");
            foreach (int n in numbers)
                d.Options.Add(Opt("announce_number", value: n, desc: n.ToString()));
            d.Apply = idx => idx[0];
            return d;
        }
    }
}
