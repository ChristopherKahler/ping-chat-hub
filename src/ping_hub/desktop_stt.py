"""Desktop dictation — the hub's governance of the PC-wide mic.

Two microphones exist on this machine and only one of them was ever governed.
`cx-ptt` sends a transcript to a terminal session and the hub owns its word
fixes, its audio device and its restarts. `dictate.py` pastes a transcript into
whatever window has focus — Notepad, a browser, a game — and until now owned
itself entirely: its hotkey was a constant in the source, its mode was a tray
checkbox nobody could see from the phone, and its transcripts existed for
exactly as long as the clipboard held them.

So this module gives the desktop mic the same three things the hub already
gives cx-ptt:

  settings   one store, edited in the app, read by the daemon  (desktop-stt.json)
  state      what the daemon ACTUALLY did with them            (desktop-stt-state.json)
  history    every transcript, kept                            (dictation-history.jsonl)

`state` is separate from `settings` on purpose, and it is the half that earns
its keep. A hotkey is a REQUEST: `RegisterHotKey` fails when another program
already owns the combo, and dictate falls back to a library hook that behaves
differently. Storing only the request would let the app show `ctrl+alt+d` while
the daemon was running something else — the exact class of lie the hub exists
to prevent. dictate writes back what it registered; the app shows that.

History is append-only JSONL because it is the record of words Chris has
already spoken: rewriting the file to prune it risks losing the tail on a
crash, so pruning happens by rotation, never in place.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ping_hub import proc

VERSION = 1

# tap = press to start, press again to finish. hold = walkie-talkie.
MODES = ("tap", "hold")

DEFAULTS = {
    "hotkey": "ctrl+alt+d",
    "mode": "tap",
    "cleanup": True,       # accurate re-decode of the take (off = raw stream text)
    "history": True,
    "history_keep": 2000,  # entries retained after a rotation
}

# Modifiers dictate can pass to RegisterHotKey. `win` is deliberately absent:
# Windows reserves most Win+<key> combinations and the ones it does not are
# claimed by the shell, so offering it would offer failures.
MODIFIERS = ("ctrl", "alt", "shift")

# Main keys with a stable virtual-key code. Letters and digits are computed;
# this table is only for the ones whose VK is not their ASCII value.
NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "pause": 0x13, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "backspace": 0x08,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def key_vk(key: str) -> int | None:
    """Virtual-key code for one main key, or None if dictate could not bind it."""
    k = (key or "").strip().lower()
    if not k:
        return None
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        return ord(k.upper())
    return NAMED_KEYS.get(k)


def parse_hotkey(text: str) -> dict:
    """Split `ctrl+alt+d` into its parts and say whether dictate can bind it.

    Validating HERE rather than only in the daemon means a combo that cannot
    be registered is refused while the modal is still open, instead of being
    saved, restarted into, and discovered as silence the next time Chris
    presses it.
    """
    parts = [p.strip().lower() for p in (text or "").split("+") if p.strip()]
    mods = [p for p in parts if p in MODIFIERS]
    keys = [p for p in parts if p not in MODIFIERS]
    if len(keys) != 1:
        return {"ok": False, "detail": "one main key, plus modifiers"}
    if not mods:
        # a bare key would fire inside every text box on the machine
        return {"ok": False, "detail": "needs at least one of ctrl / alt / shift"}
    vk = key_vk(keys[0])
    if vk is None:
        return {"ok": False, "detail": f"cannot bind the key {keys[0]!r}"}
    order = [m for m in MODIFIERS if m in mods]
    return {"ok": True, "mods": order, "key": keys[0], "vk": vk,
            "canonical": "+".join(order + [keys[0]])}


# ── stores ───────────────────────────────────────────────────────────────────
def store_path(cfg) -> Path:
    return cfg.paths.base_store / "hub" / "desktop-stt.json"


def state_path(cfg) -> Path:
    return cfg.paths.base_store / "hub" / "desktop-stt-state.json"


def history_path(cfg) -> Path:
    return cfg.paths.base_store / "hub" / "dictation-history.jsonl"


def load(cfg) -> dict:
    doc = dict(DEFAULTS)
    doc["version"] = VERSION
    try:
        with open(store_path(cfg), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return doc               # absent or corrupt store = the defaults
    if isinstance(raw, dict):
        doc.update(normalise(raw))
    return doc


def normalise(payload: dict) -> dict:
    """Keep only what the daemon can act on, and never store a broken hotkey."""
    out = dict(DEFAULTS)
    out["version"] = VERSION
    raw = payload if isinstance(payload, dict) else {}
    hk = parse_hotkey(str(raw.get("hotkey", "")) or DEFAULTS["hotkey"])
    out["hotkey"] = hk["canonical"] if hk["ok"] else DEFAULTS["hotkey"]
    mode = str(raw.get("mode", "")).strip().lower()
    out["mode"] = mode if mode in MODES else DEFAULTS["mode"]
    out["cleanup"] = bool(raw.get("cleanup", DEFAULTS["cleanup"]))
    out["history"] = bool(raw.get("history", DEFAULTS["history"]))
    try:
        keep = int(raw.get("history_keep", DEFAULTS["history_keep"]))
    except (TypeError, ValueError):
        keep = DEFAULTS["history_keep"]
    out["history_keep"] = max(50, min(keep, 100000))
    return out


def save(cfg, payload: dict) -> dict:
    """Write the store atomically. dictate polls this file; a half-written one
    would be read as corrupt and silently swapped for the defaults."""
    doc = normalise(payload)
    p = store_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    tmp.replace(p)
    return doc


def read_state(cfg) -> dict:
    """What the daemon last reported about itself. {} when it never has."""
    try:
        with open(state_path(cfg), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# ── history ──────────────────────────────────────────────────────────────────
def read_history(cfg, limit: int = 200, path: Path | None = None) -> list[dict]:
    """Newest first. A malformed line is skipped, never fatal — this file is
    appended to by a different process while this one reads it."""
    p = Path(path) if path is not None else history_path(cfg)
    try:
        with open(p, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("text"):
            out.append(row)
        if limit and len(out) >= limit:
            break
    return out


def _wpm(words: float, seconds: float) -> float:
    return round(words / (seconds / 60.0), 1) if seconds > 0 else 0.0


def stats(entries: list[dict], days: int = 7, now=None) -> dict:
    """Words per minute over the whole record, and over a recent window.

    Reported as total words over total speaking seconds, NOT as the mean of
    each take's rate. A four-word take and a four-hundred-word take are not
    equal evidence of how fast Chris talks, and averaging the rates would make
    them so — one "yes" at 40wpm would drag the number down as hard as a full
    paragraph pulls it up.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=days)
    tot_w = tot_s = 0.0
    rec_w = rec_s = 0.0
    recent_takes = 0
    best = 0.0
    for e in entries:
        try:
            w = float(e.get("words") or 0)
            s = float(e.get("seconds") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or s <= 0:
            continue
        tot_w += w
        tot_s += s
        best = max(best, _wpm(w, s))
        ts = e.get("ts") or ""
        try:
            when = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if when >= cutoff:
            rec_w += w
            rec_s += s
            recent_takes += 1
    return {"takes": len(entries),
            "words": int(tot_w),
            "seconds": round(tot_s, 1),
            "wpm": _wpm(tot_w, tot_s),
            "best_wpm": round(best, 1),
            "recent_days": days,
            "recent_takes": recent_takes,
            "recent_wpm": _wpm(rec_w, rec_s)}


# ── the daemon itself ────────────────────────────────────────────────────────
SCRIPT = "dictate.py"


def find_daemon(rows: list[dict]) -> dict | None:
    """The python process running dictate.py, whatever launched it. Same
    anchoring as cxptt.find_daemon and for the same reason: the launcher is
    gone by the time anyone asks."""
    for p in rows or []:
        if (SCRIPT in p.get("cmdline", "")
                and p.get("name", "").lower().startswith("python")):
            return p
    return None


def status(cfg, rows=None) -> dict:
    """Settings, what the daemon reported, and whether it is actually up.

    `hotkey_live` is the one field worth reading twice: it is what the daemon
    registered, which is not necessarily what the store asked for.
    """
    from ping_hub import cxptt
    if rows is None:
        rows = cxptt.list_processes()
    daemon = find_daemon(rows)
    st = read_state(cfg)
    cfg_doc = load(cfg)
    # state written by a process that is gone describes nothing that is running
    live = bool(daemon) and (not st.get("pid") or st.get("pid") == daemon["pid"])
    return {
        "settings": cfg_doc,
        "running": bool(daemon),
        "pid": daemon["pid"] if daemon else None,
        "state": st if live else {},
        "hotkey_live": st.get("hotkey") if live else None,
        "hotkey_registered": bool(st.get("registered")) if live else False,
        "hotkey_method": st.get("method") if live else None,
        "pending_restart": bool(live and st.get("hotkey")
                                and st.get("hotkey") != cfg_doc["hotkey"]),
        "script": str(script_path(cfg)),
    }


def script_path(cfg) -> Path:
    """Where dictate.py runs from — the provisioned copy, named by config.

    This used to hunt for it under the operator's home directory, which worked
    on the machine it was written on and nowhere else.
    """
    return cfg.desktop_stt.script


def restart(cfg, rows=None, kill=None, start=None) -> dict:
    """Stop the daemon and start it again through its clean launcher.

    The launcher matters. Starting dictate.py from inside another program's
    process tree inherits that tree's job-object UI restrictions, and under
    those `RegisterHotKey` fails SILENTLY — the daemon runs, loads both models,
    reports ready, and never receives a keypress. The scheduled task exists to
    give it a parentless start, so a restart goes through the task or it does
    not go at all.
    """
    from ping_hub import cxptt
    rows = cxptt.list_processes() if rows is None else rows
    before = find_daemon(rows)
    killed = None
    if before:
        killed = before["pid"]
        try:
            (kill or _kill)(before["pid"])
        except OSError as e:
            return {"ok": False, "detail": f"could not stop pid {killed}: {e}"}
    ok, detail = (start or _start)(cfg)
    return {"ok": ok, "detail": detail, "killed": killed}


def _kill(pid: int) -> None:
    proc.run(["taskkill", "/PID", str(pid), "/F"],
             capture_output=True, text=True, timeout=20)


def _start(cfg) -> tuple[bool, str]:
    task = cfg.desktop_stt.task
    r = proc.run(["schtasks", "/run", "/tn", task],
                 capture_output=True, text=True, timeout=30)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0:
        return True, f"started via scheduled task {task}"
    # No task registered: fall back to the provisioned launcher. Second best
    # precisely because of the job-object trap above, so say which one ran.
    launcher = cfg.desktop_stt.launcher
    if not Path(launcher).exists():
        return False, (f"{out or 'schtasks failed'}; and no launcher at "
                       f"{launcher}. Run `ping-hub install`.")
    try:
        proc.popen([str(launcher)], cwd=str(Path(launcher).parent))
    except OSError as e:
        return False, f"{out or 'schtasks failed'}; launcher failed: {e}"
    return True, (f"the {task} task is not registered - started through the "
                  f"launcher instead (the hotkey may not register; run "
                  f"`ping-hub install` to register the task)")
