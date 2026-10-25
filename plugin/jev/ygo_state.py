"""YGO state builder: compact duel snapshot -> Jev decision state.

YGO duels are decision-dense: on our turn we must decide whether/how to
summon, activate, set, attack, switch phase; on the opponent's turn we
must decide how to interact (activate traps/effects, respond to chains,
block). All these decisions go through Jev as typed questions against a
compact, public-info-only duel state.

State principles (same as other games):
- Facts only, no conclusions ("is this dangerous" is Jev's call).
- Public info only: opponent's hand = count, not content.
- Legal actions are the decision space — Jev picks from them.
- Compact but semantically complete: every card carries its effect summary
  (one line) so the model knows what it does, not just its stats.
"""
from __future__ import annotations

from typing import Any, Optional

# One-line effect summaries for the demo card pool (from ygopro card text).
# In production the bridge reads these from the card database (cards.cdb).
EFFECT_SUMMARIES = {
    "Blue-Eyes White Dragon": "通常怪兽。攻击力 3000，白板无效果。",
    "Dark Magician": "通常怪兽。攻击力 2500，白板无效果。",
    "Mystic Tomato": "效果怪。召唤成功时抽 1 张卡。",
    "Summoned Skull": "通常怪兽。攻击力 2500，白板无效果。",
    "Gemini Elf": "通常怪兽。攻击力 1900，白板无效果。",
    "Mystical Space Typhoon": "速攻魔法。破坏场上 1 张魔法/陷阱卡。",
    "Pot of Greed": "通常魔法。抽 2 张卡。",
    "Monster Reborn": "通常魔法。从墓地特殊召唤 1 只怪兽到自己场上。",
    "Trap Hole": "通常陷阱。对方召唤攻击力 1000 以上怪兽时破坏那只怪兽。",
    "Mirror Force": "通常陷阱。对方攻击宣言时破坏对方场上所有攻击表示怪兽。",
}


def _effect_of(name: str, card: Optional[dict] = None) -> str:
    """One-line effect summary for a card (from card db via bridge, or the
    built-in table for the demo pool)."""
    if card and card.get("effect_text"):
        return str(card["effect_text"])[:80]
    return EFFECT_SUMMARIES.get(name, "")


def _card_brief(card: dict) -> dict:
    """Compact one card for the Jev state: stats + one-line effect."""
    if not isinstance(card, dict):
        return {"name": str(card)[:60]}
    name = card.get("name", card.get("code", "?"))
    out = {"name": name}
    # Only include non-empty fields (skip "?" placeholders to save tokens).
    for k in ("position", "atk", "def", "level", "type", "attribute", "race"):
        v = card.get(k)
        if v is not None and v != "" and v != "?" and v != 0:
            out[k] = v
    # One-line effect summary (the key to "playing like a human").
    eff = _effect_of(name, card)
    if eff:
        out["effect"] = eff
    return out


def _names_only(cards: list) -> list[str]:
    """Graveyard/banished/extra-deck: just names (what's available to use)."""
    out = []
    for c in cards:
        if isinstance(c, dict):
            out.append(str(c.get("name", c.get("code", "?"))))
        else:
            out.append(str(c))
    return out


def build_ygo_state(
    snapshot: dict[str, Any],
    goal: str = "",
    persona: str = "",
    playbook: str = "",
    recent_actions: Optional[list] = None,
    recent_hints: Optional[list] = None,
) -> dict[str, Any]:
    """Assemble the YGO-specific Jev state from an adapter snapshot.

    `snapshot` shape (from YgoEngineAdapter.get_state()):
        {
          "game": {
            "my_lp", "op_lp", "turn", "phase", "is_my_turn",
            "opponent", "opponent_hand_count",
            "hand": [card...], "field_my": [card...], "field_op": [card...],
            "grave_my": [...], "grave_op": [...],
            "banished_my": [...], "banished_op": [...],
            "extra_deck": [...], "legal_actions": [...],
          },
          "stage": "dueling" | ...
        }
    """
    g = snapshot.get("game", {}) if isinstance(snapshot, dict) else {}
    state: dict[str, Any] = {}

    # --- instruction block (persona + goal + playbook) ---
    instr = []
    if persona:
        instr.append(f"You are {persona}, playing Yu-Gi-Oh!.")
    else:
        instr.append("You are an AI playing Yu-Gi-Oh!.")
    instr.append(
        "Decide the best action from the legal actions. Consider LP advantage, "
        "field control, resource management, and your current strategy. "
        "On your turn: build your board. On opponent's turn: interact wisely."
    )
    if playbook:
        instr.append(f"DUEL KNOWLEDGE: {playbook}")
    if goal:
        instr.append(f"CURRENT GOAL: {goal}")
    state["instructions"] = " ".join(instr)

    # --- my resources ---
    hand = [_card_brief(c) for c in g.get("hand", [])][:10]
    field_my = [_card_brief(c) for c in g.get("field_my", [])][:8]
    state["my"] = {
        "lp": g.get("my_lp", 8000),
        "hand_count": len(g.get("hand", [])),
        "hand": hand,
        "field": field_my,
        "grave": _names_only(g.get("grave_my", []))[:12],
        "banished": _names_only(g.get("banished_my", []))[:8],
        "extra_deck": _names_only(g.get("extra_deck", []))[:8],
    }

    # --- opponent resources (public only) ---
    field_op = [_card_brief(c) for c in g.get("field_op", [])][:8]
    state["opponent"] = {
        "lp": g.get("op_lp", 8000),
        "name": g.get("opponent", "?"),
        "hand_count": g.get("opponent_hand_count", 0),
        "field": field_op,
        "grave": _names_only(g.get("grave_op", []))[:12],
        "banished": _names_only(g.get("banished_op", []))[:8],
    }

    # --- duel situation ---
    state["duel"] = {
        "turn": g.get("turn", 0),
        "phase": g.get("phase", "main1"),
        "is_my_turn": bool(g.get("is_my_turn", True)),
        "is_duel_active": bool(g.get("is_duel_active", True)),
    }

    # --- legal options (the decision space, addressed by option id) ---
    from ..ygo_ops import options_map
    legal = options_map(g)
    state["legal_actions"] = []
    for i, (oid, a) in enumerate(legal.items()):
        if i >= 25:  # trim to 25 to keep state compact
            break
        brief = {
            "option": oid,
            "action": a.get("action", a.get("type", "?")),
        }
        if a.get("card"):
            brief["card"] = a.get("card")
            # Effect description for the action's card (so the model knows
            # what activating/summoning it does).
            eff = _effect_of(str(a.get("card")), a if isinstance(a, dict) else None)
            if eff:
                brief["effect"] = eff
        if a.get("position") is not None:
            brief["position"] = a.get("position")
        if a.get("target"):
            brief["target"] = a.get("target")
        if a.get("desc"):
            brief["desc"] = str(a.get("desc"))[:80]
        state["legal_actions"].append(brief)

    # --- action chain planning (pre-input a sequence, execute if not interrupted) ---
    # Jev can plan a short chain of legal actions (e.g. summon → activate →
    # battle phase → attack) and execute them without re-deciding, as long as
    # nothing interrupts. If interrupted, we re-decide from the new state.
    state["can_chain"] = True

    # --- harness memory (recent decisions + hints) ---
    if recent_actions:
        state["recent_actions"] = list(recent_actions)[-6:]
    if recent_hints:
        state["recent_hints"] = list(recent_hints)[-4:]

    return state


def build_ygo_questions(snapshot: dict[str, Any], goal: str = "") -> dict[str, Any]:
    """YGO-specific Jev question: exactly one choice per decision.

    ``next_action`` is a Choice whose criteria keys are **option ids** (bridge
    id or list index). The engine resolves the chosen id back to the same
    legal option before executing, so same-named actions never collide and a
    stale answer is skipped instead of misfiring.

    No legal options -> no question at all (the caller skips the request).
    """
    g = snapshot.get("game", {}) if isinstance(snapshot, dict) else {}
    from ..ygo_ops import describe_option, options_map
    legal = options_map(g)

    # Criteria are keyed by **option id**, not by action name: five different
    # summonable monsters must remain five distinct choices, and the chosen
    # id resolves back to the exact same option at execution time.
    criteria = {}
    for i, (oid, a) in enumerate(legal.items()):
        if i >= 25:
            break
        text = describe_option(oid, a, with_effect=True)
        prefix = f"#{oid} "
        criteria[oid] = text[len(prefix):] if text.startswith(prefix) else text

    # One question only: pick the next legal option. The previous version
    # asked 5-7 questions per tick (danger/should_attack/plan_working/…)
    # whose answers were never consumed — pure token waste.
    questions: dict[str, Any] = {}
    if criteria:
        goal_hint = f" Current goal: {goal}." if goal else ""
        response = bool(g.get("response_window"))
        questions["next_action"] = {
            "type": "choice",
            "instructions": (
                "You are playing Yu-Gi-Oh!. Pick the best legal option by its id. "
                + ("The opponent is waiting for YOUR response: chain a card or pass. "
                   if response else
                   "Consider LP, field control, resources and card effects; on the "
                   "opponent's turn only respond when it helps. ")
                + "Answer with the option id exactly as listed." + goal_hint
            ),
            "criteria": criteria,
        }
    return questions