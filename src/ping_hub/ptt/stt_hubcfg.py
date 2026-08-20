"""dictate's half of the hub's desktop-STT governance.

The hub owns the settings; this module is how the daemon reads them, reports
what it actually did with them, and files every transcript it produces.

Three files, all under the hub store:

  desktop-stt.json         written by the app, polled here    (settings)
  desktop-stt-state.json   written here, read by the app      (what really happened)
  dictation-history.jsonl  appended here, read by the app     (every transcript)

Settings are re-read per take rather than cached at boot, so a checkbox in the
app takes effect on the next thing Chris says instead of the next restart. The
poll is keyed on (mtime, size), not mtime alone: a one-character edit inside
the same second moves the byte count when the clock has not.

The state file is the honest half. `RegisterHotKey` can fail — another program
owns the combo, or the process inherited job-object UI restrictions — and
dictate then falls back to a library hook. Reporting the REQUESTED hotkey would
let the app display a binding that is not listening. So this writes what was
registered and how.
"""
import json
import os
import time
from pathlib import Path

def _hub_dir() -> Path:
    """Where the hub keeps its stores.

    Env first, because the launcher `ping-hub install` writes knows exactly
    where this machine put things. Derived second, so the daemon still runs
    when someone starts it by hand. A literal home directory would work on
    exactly one computer.
    """
    direct = os.environ.get("PING_HUB_STORE")
    if direct:
        return Path(direct)
    gbl = os.environ.get("PING_HUB_BASE_GBL")
    root = Path(gbl) if gbl else Path.home() / ".base-gbl"
    return root / ".base" / "hub"


HUB_DIR = _hub_dir()
SETTINGS = HUB_DIR / "desktop-stt.json"
STATE = HUB_DIR / "desktop-stt-state.json"
HISTORY = HUB_DIR / "dictation-history.jsonl"

MODES = ("tap", "hold")
MODIFIERS = ("ctrl", "alt", "shift")

DEFAULTS = {
    "hotkey": "ctrl+alt+d",
    "mode": "tap",
    "cleanup": True,
    "history": True,
    "history_keep": 2000,
}

# Kept identical to ping_hub/desktop_stt.py NAMED_KEYS. Two copies exist
# because they live in two programs; if they drift, the app validates a combo
# the daemon then refuses, which is the failure this table is here to avoid.
NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "pause": 0x13, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "backspace": 0x08,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
NAMED_KEYS.update({f"f{n}": 0x6F + n for n in range(1, 13)})

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
_MOD_BITS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "shift": MOD_SHIFT}


def parse_hotkey(text):
    """`ctrl+alt+d` -> {mods, key, vk, flags, canonical} or None if unbindable."""
    parts = [p.strip().lower() for p in str(text or "").split("+") if p.strip()]
    mods = [m for m in MODIFIERS if m in parts]
    keys = [p for p in parts if p not in MODIFIERS]
    if len(keys) != 1 or not mods:
        return None
    k = keys[0]
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        vk = ord(k.upper())
    else:
        vk = NAMED_KEYS.get(k)
    if vk is None:
        return None
    flags = MOD_NOREPEAT
    for m in mods:
        flags |= _MOD_BITS[m]
    return {"mods": mods, "key": k, "vk": vk, "flags": flags,
            "canonical": "+".join(mods + [k])}


# ── settings ─────────────────────────────────────────────────────────────────
_cache = [None, dict(DEFAULTS)]     # [(mtime, size), doc]


def settings(force=False):
    """Current settings, re-read only when the file actually changed."""
    try:
        st = SETTINGS.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        return _cache[1]            # never written yet, or gone mid-replace
    if force or _cache[0] != key:
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return _cache[1]        # unreadable: last known, never crash a take
        doc = dict(DEFAULTS)
        if isinstance(raw, dict):
            hk = parse_hotkey(raw.get("hotkey"))
            if hk:
                doc["hotkey"] = hk["canonical"]
            mode = str(raw.get("mode", "")).strip().lower()
            if mode in MODES:
                doc["mode"] = mode
            for k in ("cleanup", "history"):
                if k in raw:
                    doc[k] = bool(raw[k])
            try:
                doc["history_keep"] = max(50, min(int(raw.get("history_keep")),
                                                  100000))
            except (TypeError, ValueError):
                pass
        _cache[0] = key
        _cache[1] = doc
    return _cache[1]


def _atomic_write(path, text):
    """tmp + replace, retried. On Windows the replace loses to an indexer or an
    antivirus holding the target for a few milliseconds and raises EPERM; a
    single attempt drops the write silently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    for attempt in range(6):
        try:
            tmp.replace(path)
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def save_settings(**changes):
    """Write settings back — the tray menu's flips land in the same store the
    app edits, so the two never show different truths."""
    doc = dict(settings(force=True))
    doc.update(changes)
    doc["version"] = 1
    return _atomic_write(SETTINGS, json.dumps(doc, indent=1))


# ── state ────────────────────────────────────────────────────────────────────
def write_state(**fields):
    doc = {"pid": os.getpid(),
           "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    doc.update(fields)
    return _atomic_write(STATE, json.dumps(doc, indent=1))


# ── history ──────────────────────────────────────────────────────────────────
def append_history(text, seconds, lost=0, target=""):
    """One line per transcript. Append-only: this is the record of words
    already spoken, and rewriting the file to prune it risks the tail."""
    words = len((text or "").split())
    if not words:
        return None
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "seconds": round(float(seconds), 1),
           "words": words,
           "wpm": round(words / (seconds / 60.0), 1) if seconds > 0 else 0.0,
           "lost": int(lost or 0),
           "target": target or "",
           "text": text}
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return row


def rotate_history(keep):
    """Trim to the newest `keep` lines by writing a NEW file and replacing it,
    never by truncating the live one. Returns how many were dropped."""
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    if len(lines) <= keep:
        return 0
    kept = lines[-keep:]
    if _atomic_write(HISTORY, "".join(kept)):
        return len(lines) - len(kept)
    return 0
