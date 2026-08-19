"""cx-say — flip the say-back (TTS) flag on cx channels from one short command.

Reached through the `say` wrapper on every side (say.cmd for cmd/pwsh, the
POSIX `say` script for WSL and Git Bash), so it is one keystroke away inside a
Claude session via the `!` bash prefix:

    say off 4          mute channel 4          say on 4        unmute it
    say off            mute EVERY channel      say on          restore them
    say toggle 4       flip it                 say status      what's live

Targets are slot digits or codenames, any number of them (`say off 4 extendly`).
A bare `say off` snapshots which channels were speaking, so the matching bare
`say on` puts back exactly that set — the "I'm getting on a call" round trip.
With no snapshot to restore, bare `say on` turns everything on.

Only the `say = ...` field of a [slots] line is rewritten; the rest of cx.toml
stays byte-for-byte, so this coexists with cx-slot.py's section regeneration.
cx-ptt hot-reloads the file every ~1s, so a flip lands almost immediately.

`say <anything else>` still speaks through kokoro; `say -- off 4` forces the
words to be spoken instead of parsed.

usage: cx-say.py on|off|toggle|mute|unmute [<slot|codename> ...]
       cx-say.py status
"""
import json
import os
import re
import sys
import tomllib
from pathlib import Path
# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths


WIN = os.name == "nt"
BASE = cxpaths.base_gbl()
CONFIG = BASE / "cx.toml"
SNAPSHOT = BASE / "cx" / "say-snapshot.json"

# `"4" = { codename = "extendly", side = "wsl", say = true }`
SLOT_LINE = re.compile(r'^(?P<lead>\s*"(?P<n>\d)"\s*=\s*\{)(?P<body>.*?)(?P<tail>\}\s*)$')
SAY_FIELD = re.compile(r",?\s*\bsay\s*=\s*(?:true|false)")


def read_slots(text):
    """slot digit -> {codename, side, say} straight from the parsed config."""
    slots = {}
    for n, v in tomllib.loads(text).get("slots", {}).items():
        if isinstance(v, dict):
            slots[n] = {"codename": v.get("codename", ""),
                        "side": v.get("side", "wsl"),
                        "say": bool(v.get("say", False))}
        else:
            slots[n] = {"codename": v, "side": "wsl", "say": False}
    return slots


def set_say(body, on):
    """Rewrite just the say field inside one slot line's braces."""
    body = SAY_FIELD.sub("", body).rstrip()
    if on:
        body += ", say = true"
    return body + " "


def apply(text, wanted):
    """wanted = {slot digit: bool}. Returns the edited config text."""
    out = []
    for line in text.splitlines(keepends=True):
        m = SLOT_LINE.match(line.rstrip("\r\n"))
        if m and m.group("n") in wanted:
            nl = line[len(line.rstrip("\r\n")):]
            line = (m.group("lead") + set_say(m.group("body"), wanted[m.group("n")])
                    + m.group("tail").rstrip() + nl)
        out.append(line)
    return "".join(out)


def label(slots, n):
    return f"{n} {slots[n]['codename']}"


def status(slots):
    on = [label(slots, n) for n in sorted(slots) if slots[n]["say"]]
    off = [label(slots, n) for n in sorted(slots) if not slots[n]["say"]]
    print(f"[say] ON : {', '.join(on) if on else '(none)'}")
    print(f"      off: {', '.join(off) if off else '(none)'}")


def resolve(targets, slots):
    """Slot digits and/or codenames -> slot digits. Unknown targets abort."""
    by_name = {v["codename"].lower(): n for n, v in slots.items()}
    picked, bad = [], []
    for t in targets:
        if t in slots:
            picked.append(t)
        elif t.lower() in by_name:
            picked.append(by_name[t.lower()])
        else:
            bad.append(t)
    if bad:
        known = " ".join(f"{n}={slots[n]['codename']}" for n in sorted(slots))
        print(f"[say] no such channel: {', '.join(bad)}\n      channels: {known}")
        sys.exit(2)
    return picked


def main():
    argv = sys.argv[1:]
    verb = (argv[0] if argv else "status").lower()
    verb = {"mute": "off", "unmute": "on", "ls": "status", "list": "status"}.get(verb, verb)
    if verb in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if verb not in ("on", "off", "toggle", "status"):
        print(f"[say] unknown command {verb!r} — expected on|off|toggle|status")
        return 2

    text = CONFIG.read_text(encoding="utf-8")
    slots = read_slots(text)
    if not slots:
        print(f"[say] no [slots] entries in {CONFIG}")
        return 1
    if verb == "status":
        status(slots)
        return 0

    targets = argv[1:]
    blanket = not targets
    picked = sorted(slots) if blanket else resolve(targets, slots)

    if blanket and verb == "off":
        # remember what was speaking so the matching bare `say on` restores it
        live = [n for n in slots if slots[n]["say"]]
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(live), encoding="utf-8")
        wanted = {n: False for n in picked}
    elif blanket and verb == "on":
        live = None
        try:
            live = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        except Exception:
            pass
        if live:                     # restore the snapshot exactly
            wanted = {n: (n in live) for n in picked}
        else:                        # nothing to restore — everything speaks
            wanted = {n: True for n in picked}
        SNAPSHOT.unlink(missing_ok=True)
    elif verb == "toggle":
        wanted = {n: not slots[n]["say"] for n in picked}
    else:
        wanted = {n: verb == "on" for n in picked}

    changed = [n for n, v in wanted.items() if v != slots[n]["say"]]
    if changed:
        CONFIG.write_text(apply(text, wanted), encoding="utf-8")
        moved = ", ".join(f"{label(slots, n)} {'ON' if wanted[n] else 'off'}"
                          for n in sorted(changed))
        print(f"[say] {moved} — live in ~1s")
    else:
        print("[say] already there — nothing changed")
    status(read_slots(CONFIG.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
