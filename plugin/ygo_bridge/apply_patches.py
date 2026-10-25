#!/usr/bin/env python3
"""Apply the KiraYgoBridge patch set to a ProjectIgnis/windbot checkout.

Idempotent, anchor-checked: every replacement must match exactly once or the
script aborts (so an upstream change can never silently produce a broken
build). Tested against ProjectIgnis/windbot @ bffe6b6 (2026-08).

Usage: python3 bridge/apply_patches.py /path/to/windbot
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if ROOT is None or not (ROOT / "Game" / "GameBehavior.cs").exists():
    print("usage: python3 bridge/apply_patches.py /path/to/windbot")
    sys.exit(2)

LOG = []


def replace(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8-sig")
    if new.strip() in text:
        LOG.append(f"skip (already patched): {path.name}: {old[:48]!r}")
        return
    occurrences = text.count(old)
    if occurrences != count:
        raise SystemExit(f"ANCHOR FAILED {path}: found {occurrences}x (want {count}): {old[:110]!r}")
    text = text.replace(old, new, count)
    path.write_text(text, encoding="utf-8")
    LOG.append(f"patched {path.name}: {old[:56]!r}")


def insert_before(path: Path, anchor: str, block: str) -> None:
    replace(path, anchor, block + anchor)


# ---- 1. SQLite driver: Mono.Data.Sqlite -> Microsoft.Data.Sqlite (.NET 8) ----
for rel in ("ExecutorBase/YGOSharp.OCGWrapper/CardsManager.cs",
            "ExecutorBase/YGOSharp.OCGWrapper/NamedCardsManager.cs"):
    path = ROOT / rel
    if path.exists():
        replace(path, "using Mono.Data.Sqlite;", "using Microsoft.Data.Sqlite;")

# ---- 2. GameClient: expose the behavior for the bridge ---------------------
client = ROOT / "Game" / "GameClient.cs"
insert_before(client, "        public void Start()",
              "        public GameBehavior Behavior => _behavior;\n\n")

# ---- 3. GameBehavior: public accessors for state/room/deck -----------------
behavior = ROOT / "Game" / "GameBehavior.cs"
ACCESSORS = """        public Room BridgeRoom => _room;
        public Duel BridgeDuel => _duel;
        public void BridgeSendReady() => Connection.Send(CtosMessage.HsReady);
        public void BridgeSendStart() => Connection.Send(CtosMessage.HsStart);
        public void BridgeSurrender() => Connection.Send(GamePacketFactory.Create(CtosMessage.Surrender));
        public void BridgeUpdateDeck(Deck deck)
        {
            Deck = deck;
            BinaryWriter packet = GamePacketFactory.Create(CtosMessage.UpdateDeck);
            packet.Write(deck.Cards.Count + deck.ExtraCards.Count);
            packet.Write(deck.SideCards.Count);
            foreach (int card in deck.Cards) packet.Write(card);
            foreach (int card in deck.ExtraCards) packet.Write(card);
            foreach (int card in deck.SideCards) packet.Write(card);
            Connection.Send(packet);
        }

"""
insert_before(behavior, "        public int GetLocalPlayer(int player)", ACCESSORS)

# ---- 4. decision points -> bridge (built-in AI remains the fallback) -------
D = "Kira.KiraBridge.Decide"
O = "Kira.KiraOptions"
replace(behavior,
        "result = _ai.OnRockPaperScissors();",
        f"result = {D}(_ai, a => a.OnRockPaperScissors(), () => {O}.RockPaperScissors());",
        count=2)
replace(behavior,
        "bool start = _ai.OnSelectHand();",
        f"bool start = {D}(_ai, a => a.OnSelectHand(), () => {O}.FirstHand());")
replace(behavior,
        "Connection.Send(CtosMessage.Response, _ai.OnSelectBattleCmd(battle).ToValue());",
        f"Connection.Send(CtosMessage.Response, {D}(_ai, a => a.OnSelectBattleCmd(battle), () => {O}.Battle(_ai, battle)).ToValue());")
replace(behavior,
        "Connection.Send(CtosMessage.Response, _ai.OnSelectIdleCmd(main).ToValue());",
        f"Connection.Send(CtosMessage.Response, {D}(_ai, a => a.OnSelectIdleCmd(main), () => {O}.Idle(_ai, main)).ToValue());")
replace(behavior,
        "Connection.Send(CtosMessage.Response, _ai.OnSelectChain(cards, descs, forced, hint1 | hint2));",
        f"Connection.Send(CtosMessage.Response, {D}(_ai, a => a.OnSelectChain(cards, descs, forced, hint1 | hint2), () => {O}.Chain(_ai, cards, descs, forced)));")
replace(behavior,
        "Connection.Send(CtosMessage.Response, _ai.OnSelectOption(options));",
        f"Connection.Send(CtosMessage.Response, {D}(_ai, a => a.OnSelectOption(options), () => {O}.SelectOption(options)));")
replace(behavior,
        "int selected = _ai.OnSelectPlace(_select_hint, player, location, filter);",
        f"int selected = {D}(_ai, a => a.OnSelectPlace(_select_hint, player, location, filter), () => {O}.Place(_select_hint, player, location, filter));",
        count=2)
replace(behavior,
        "Connection.Send(CtosMessage.Response, (int)_ai.OnSelectPosition(cardId, positions));",
        f"Connection.Send(CtosMessage.Response, (int){D}(_ai, a => a.OnSelectPosition(cardId, positions), () => {O}.Position(cardId, positions)));")
replace(behavior,
        "reply = _ai.OnSelectYesNo(desc) ? 1 : 0;",
        f"reply = {D}(_ai, a => a.OnSelectYesNo(desc), () => {O}.YesNo(desc)) ? 1 : 0;")
replace(behavior,
        "reply = _ai.OnSelectBattleReplay() ? 1 : 0;",
        f"reply = {D}(_ai, a => a.OnSelectBattleReplay(), () => {O}.BattleReplay()) ? 1 : 0;")
replace(behavior,
        "Connection.Send(CtosMessage.Response, _ai.OnAnnounceNumber(numbers));",
        f"Connection.Send(CtosMessage.Response, {D}(_ai, a => a.OnAnnounceNumber(numbers), () => {O}.AnnounceNumber(numbers)));")

# multi-card selection happens inside InternalOnSelectCard (its arguments are
# only in scope there), including tributes and unselect.
replace(behavior,
        "IList<ClientCard> selected = func(cards, min, max, _select_hint, cancelable);",
        "IList<ClientCard> selected = Kira.KiraBridge.Decide(_ai, "
        "a => func(cards, min, max, _select_hint, cancelable), "
        "() => Kira.KiraOptions.SelectCards(cards, min, max, _select_hint, cancelable, "
        "tribute ? \"tribute\" : \"select\"));")
replace(behavior,
        "IList<ClientCard> selected = func(cards, (finishable ? 0 : 1), 1, _select_hint, cancelable);",
        "IList<ClientCard> selected = Kira.KiraBridge.Decide(_ai, "
        "a => func(cards, (finishable ? 0 : 1), 1, _select_hint, cancelable), "
        "() => Kira.KiraOptions.SelectCards(cards, (finishable ? 0 : 1), 1, _select_hint, cancelable, \"unselect\"));")
replace(behavior,
        "IList<ClientCard> selected = _ai.OnSelectSum(cards, sumval, min, max, _select_hint, mode);",
        "IList<ClientCard> selected = Kira.KiraBridge.Decide(_ai, "
        "a => a.OnSelectSum(cards, sumval, min, max, _select_hint, mode), "
        "() => Kira.KiraOptions.SelectCards(cards, min, max, _select_hint, false, \"sum\", sumval));")

# effect yes/no carries the card for a readable prompt.
replace(behavior,
        "int reply = _ai.OnSelectEffectYn(card, desc) ? (1) : (0);",
        f"int reply = {D}(_ai, a => a.OnSelectEffectYn(card, desc), () => {O}.YesNo(desc, true, card)) ? (1) : (0);")

# ---- 5. room gates: explicit ready/start instead of automatic ---------------
replace(behavior,
        "_room.IsReady[pos] = true;\n            Connection.Send(CtosMessage.HsReady);",
        "_room.IsReady[pos] = true;\n            Kira.KiraRoom.ReadyGate(Game, () => Connection.Send(CtosMessage.HsReady));")
replace(behavior,
        "if (_room.IsHost && _room.IsReady[0] && _room.IsReady[1])\n                Connection.Send(CtosMessage.HsStart);",
        "if (_room.IsHost && _room.IsReady[0] && _room.IsReady[1])\n                Kira.KiraRoom.StartGate(Game, () => Connection.Send(CtosMessage.HsStart));")

# ---- 6. duel lifecycle + chat for the public log ----------------------------
replace(behavior, "            _ai.OnStart();",
        "            _ai.OnStart();\n            Kira.KiraSession.Current?.OnDuelStart();")
replace(behavior, "        private void OnDuelEnd(BinaryReader packet)\n        {\n            Connection.Close();",
        "        private void OnDuelEnd(BinaryReader packet)\n        {\n"
        "            Kira.KiraSession.Current?.OnDuelEnd();\n            Connection.Close();")
replace(behavior,
        'Logger.DebugWriteLine("Duel finished against " + otherName + ", result: " + textResult);',
        'Logger.DebugWriteLine("Duel finished against " + otherName + ", result: " + textResult);\n'
        "            Kira.KiraSession.Current?.OnWin(result);")
replace(behavior,
        'Logger.DebugWriteLine("System message(" + player + "): " + message);',
        'Logger.DebugWriteLine("System message(" + player + "): " + message);\n'
        "            Kira.KiraSession.Current?.OnChat(otherName, message);")

print(f"KiraYgoBridge patch applied to {ROOT}")
for line in LOG:
    print("  " + line)
