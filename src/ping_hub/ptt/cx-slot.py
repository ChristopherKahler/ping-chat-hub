"""cx-slot — bind a slot digit (0-9) to a codename in cx.toml's [slots] section.

Called by cx / cx.cmd when a slot argument is given (`cx <codename> <slot>`);
fine to run by hand too. Only the [slots] section is rewritten — the rest of
the file, comments included, stays untouched — so keep [slots] as the LAST
section (anything below it that isn't a new [section] gets regenerated away).
The cx-ptt daemon hot-reloads cx.toml, so the binding is live ~3s later.

Rules: one codename per slot, one slot per codename. Assigning a taken slot
steals it from the previous holder; re-slotting a codename moves it.

usage: cx-slot.py <codename> <slot 0-9> [--side wsl|win] [--say on|off] [--config <path>]
"""
import os
import re
import sys
import tomllib
from pathlib import Path
# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths



def opt(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    args = []
    skip = False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a in ("--side", "--config", "--say"):
            skip = True
            continue
        args.append(a)
    if len(args) < 2 or args[1] not in tuple("0123456789"):
        print("usage: cx-slot.py <codename> <slot 0-9> [--side wsl|win] [--say on|off]")
        return 1
    codename, slot = args[0], args[1]
    side = opt("--side", "wsl")
    say = opt("--say", "off").lower() == "on"  # off unless explicitly on
    default_cfg = str(cxpaths.cx_toml())
    config = Path(opt("--config", default_cfg))

    text = config.read_text(encoding="utf-8")
    slots = {}
    for n, v in tomllib.loads(text).get("slots", {}).items():
        if isinstance(v, dict):
            slots[n] = {"codename": v.get("codename", ""), "side": v.get("side", "wsl"),
                        "say": bool(v.get("say", False))}
        else:
            slots[n] = {"codename": v, "side": "wsl", "say": False}

    prev = slots.get(slot, {}).get("codename")
    for n in [n for n, v in slots.items() if v["codename"] == codename]:
        del slots[n]  # release any slot this codename already held
    slots[slot] = {"codename": codename, "side": side, "say": say}

    lines = ["[slots]",
             "# managed by `cx <codename> <slot> [on|off]` - hotkey from slot_hotkey; say=true speaks replies"]
    for n in sorted(slots):
        v = slots[n]
        extra = ", say = true" if v.get("say") else ""
        lines.append(f'"{n}" = {{ codename = "{v["codename"]}", side = "{v["side"]}"{extra} }}')
    section = "\n".join(lines) + "\n"

    m = re.search(r"(?ms)^\[slots\][^\n]*\n(?:(?!^\[).*\n?)*", text)
    if m:
        text = text[:m.start()] + section + text[m.end():]
    else:
        text = text.rstrip() + "\n\n" + section
    config.write_text(text, encoding="utf-8")

    tts = ", say ON" if say else ""
    if prev and prev != codename:
        print(f"[cx] slot {slot}: {prev} -> {codename} ({side}{tts}) - daemon rebinds in ~3s")
    else:
        print(f"[cx] slot {slot} = {codename} ({side}{tts}) - daemon rebinds in ~3s")
    return 0


sys.exit(main())
