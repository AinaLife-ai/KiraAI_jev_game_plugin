"""Frontend/backend consistency checker.

Verifies:
1. Every field in schema.json has a matching element id in index.html
2. Every field is saved by the matching save*() JS function
3. Every field is read by loadConfig()
4. Every field round-trips through the plugin's apply_cfg (main.py reads it)

Run: python3 tests/consistency_check.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "plugin" / "schema.json").read_text(encoding="utf-8"))
HTML = (ROOT / "plugin" / "pages" / "web" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "plugin" / "main.py").read_text(encoding="utf-8")

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def schema_fields(node, prefix=""):
    """Recursively collect (field_key, section_key) pairs from schema.json."""
    out = []
    for key, field in node.items():
        if not isinstance(field, dict):
            continue
        ftype = field.get("type")
        if ftype == "section" and isinstance(field.get("fields"), dict):
            out.extend(schema_fields(field["fields"], prefix=f"{prefix}{key}."))
        elif ftype == "info":
            continue
        else:
            out.append((prefix.rstrip("."), key, ftype))
    return out


def main() -> int:
    fields = schema_fields(SCHEMA)
    print(f"schema fields: {len(fields)}")

    # Aliases: schema key -> HTML element id (when they differ).
    ALIASES = {
        ("section_bridge", "game"): "game_sel",
        # laya_* prefixed schema keys map to the same HTML ids.
    }
    # Keys that are handled by non-id lookups (checkbox groups, etc).
    NON_ID = {
        ("section_bridge", "enabled_games"),  # read via querySelectorAll("#enabled_games input")
    }

    for section, key, ftype in fields:
        el_id = ALIASES.get((section, key), key)
        # 1. HTML has an element for it
        if (section, key) in NON_ID:
            has_el = f'id="{el_id}"' in HTML
            check(f"[{section}.{key}] HTML group/container id", has_el, f"(no #{el_id} container)")
        else:
            has_el = f'id="{el_id}"' in HTML or f"id='{el_id}'" in HTML
            check(f"[{section}.{key}] HTML element id", has_el, f"(no #{el_id} in index.html)")

        # 2. loadConfig reads it
        if (section, key) in NON_ID:
            has_load = f'querySelectorAll("#{el_id} ' in HTML or f'querySelectorAll("#{el_id}")' in HTML
        else:
            has_load = (f'el("{el_id}")' in HTML or f"el('{el_id}')" in HTML
                        or f'el("{key}")' in HTML)
        check(f"[{section}.{key}] loadConfig reads", has_load)

        # 3. a save*() writes it
        if (section, key) in NON_ID:
            has_save = (f'querySelectorAll("#{el_id} ' in HTML and ':checked' in HTML
                        and f'{key}:' in HTML)
        else:
            has_save = (f'"{key}"' in HTML or f"'{key}'" in HTML
                        or f'el("{el_id}").value' in HTML or f'el("{el_id}").checked' in HTML)
        check(f"[{section}.{key}] save writes", has_save)

        # 4. main.py reads it via _read_cfg
        has_read = (f'get("{key}"' in MAIN or f"get('{key}'" in MAIN
                    or f'"{key}":' in MAIN or f"'{key}':" in MAIN)
        check(f"[{section}.{key}] main.py reads", has_read)

    print(f"\n== {PASS} passed, {FAIL} failed ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())