"""Jev question builders + playbook constants.

Two decision modes (both validated by community projects):

- GOAL mode  : a Choice over high-level goals + per-action Nouls
               (jev-plays-pokemon: overworld).
- ACTION mode: pure per-action Nouls, pick the strongest with margin gating
               (jev-plays-pokemon menus/battles, TerraBlind buttons).

All question text is English — Jev's primary training language (docs warn
CJK has lower accuracy).
"""
from __future__ import annotations

from typing import Any

# ---- generic primitive-level actions ------------------------------

# (press/exec frames, settle frames) — adapter may override per game.
ACTION_FRAMES: dict[str, tuple[int, int]] = {
    "confirm": (8, 20),     # A / confirm / advance text
    "cancel": (8, 20),      # B / back out
    "menu": (8, 20),        # START / open menu
    "move_up": (16, 8),
    "move_down": (16, 8),
    "move_left": (16, 8),
    "move_right": (16, 8),
    "wait": (0, 30),
}

ACTION_NAMES = {
    "confirm": "press CONFIRM (A): confirm, talk to the NPC/object you face, advance text, select highlighted item",
    "cancel": "press CANCEL (B): cancel, back out of a menu, close dialog",
    "menu": "press MENU (START): open/close the main menu",
    "move_up": "move UP one tile (or move the menu cursor up)",
    "move_down": "move DOWN one tile (or move the menu cursor down)",
    "move_left": "move LEFT one tile (or move the menu cursor left)",
    "move_right": "move RIGHT one tile (or move the menu cursor right)",
    "wait": "press nothing for about half a second (let animations/transitions finish)",
}

DIRECTION_ACTIONS = {"move_up", "move_down", "move_left", "move_right"}
REVERSE = {
    "move_up": "move_down",
    "move_down": "move_up",
    "move_left": "move_right",
    "move_right": "move_left",
}


def build_action_questions(actions: dict[str, str] | None = None) -> dict[str, Any]:
    """One atomic Noul per candidate action, asked in parallel.

    Instructions stay short because the action semantics live in the state's
    instruction block (keeps questions token-cheap).
    """
    names = actions or ACTION_NAMES
    return {
        action: {"type": "noul", "instructions": f"Is the best single action right now to {desc}?"}
        for action, desc in names.items()
    } | {
        "menu_open": {
            "type": "noul",
            "instructions": (
                "Is a selectable menu open on screen (start menu, list, battle "
                "FIGHT/PKMN/BAG/RUN, or a yes/no question)? A plain text dialog is NOT a menu."
            ),
        },
    }


def build_goal_questions(
    goals: dict[str, str], action_questions: dict[str, Any]
) -> dict[str, Any]:
    """Goal Choice (overworld / high-level) + the action Nouls in one call."""
    return {
        "next_goal": {
            "type": "choice",
            "instructions": {
                "question": "Which goal should the player pursue next?",
                "focus": (
                    "Pick the ONE goal that best advances the long-term objective "
                    "given the screen text, recent hints, nearby objects, walkability "
                    "and room map. Code will execute the movement."
                ),
            },
            "criteria": goals,
        }
    } | action_questions


# ---- defaults ------------------------------------------------------

DEFAULT_GAME_INSTRUCTIONS = (
    "You are an AI playing a video game. Read the game state and decide the best action. "
    "Code executes your decisions. Prefer progress toward the current goal. "
    "When unsure, choose the safe default. The screen text is the current frame only; "
    "recent hints and recent actions are your memory of the past. "
    "Higher-level goals beat micro-actions when both are offered."
)
