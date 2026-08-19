"""cx-switch — login background service: free-roam terminal spawner/swapper.

20 global hotkeys (templates in cx.toml [switch]):
  ctrl+shift+num N  ->  winterm<N>   (Windows-side cx session, PowerShell tab)
  alt+shift+num N   ->  wslterm<N>   (WSL-side cx session, Ubuntu tab)

Press a combo:
  - target session missing        -> open a Windows Terminal tab (new tab in
                                     the existing window, else a new window)
                                     running `cx <codename> <N>` (say off) —
                                     cx binds slot N, registers the pid, pops
                                     the tray chat window, autoboots cx-ptt.
  - slot N held by the OTHER side -> Yes/No dialog: "daemon slot N used by
                                     <holder> — swap?". Yes = the holder's
                                     session KEEPS RUNNING; its chat window
                                     backseats (hidden to tray) and the slot
                                     rebinds to the target (spawning it only
                                     if it isn't already alive).
  - target alive and bound        -> just bring its chat window forward.

Up to 20 sessions can run at once (winterm0-9 + wslterm0-9); the 10 slot
hotkeys (shift+num N, cx-ptt) always speak to whichever side of each number
is currently bound — ctrl/alt+shift just swaps that binding back and forth.

Codenames winterm*/wslterm* are reserved for this flow (standardized).
Runs under pythonw (no console); log at .base-gbl\\cx\\switch.log.
Auto-start: Startup-folder entry cx-switch.cmd (written alongside install).
"""
import ctypes
import datetime
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths


CONFIG = cxpaths.cx_toml()
PIDS = cxpaths.cx_dir() / "pids"
LOG = cxpaths.cx_dir() / "switch.log"
# it ships beside this file, so there is nothing left to configure
SLOTTER = str(Path(__file__).resolve().with_name("cx-slot.py"))
PY = r"C:\Python312\python.exe"
WSL_TAB = (os.environ.get("PING_HUB_CX_TAB_WSL")
           or str(cxpaths.home() / "bin" / "cx-tab-wsl.cmd"))
NO_WINDOW = 0x08000000

k32 = ctypes.windll.kernel32
u32 = ctypes.windll.user32


def log(msg):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def cfg():
    try:
        with CONFIG.open("rb") as f:
            return tomllib.load(f)
    except Exception as e:
        log(f"config read failed: {e}")
        return {}


def slot_holder(n):
    v = cfg().get("slots", {}).get(str(n))
    if isinstance(v, dict):
        return v.get("codename")
    return v


def alive(cn):
    try:
        ref = (PIDS / cn).read_text(encoding="utf-8").strip()
    except Exception:
        return False
    if ref.startswith("win:"):
        h = k32.OpenProcess(0x00100000, False, int(ref[4:]))  # SYNCHRONIZE
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) != 0
        finally:
            k32.CloseHandle(h)
    if ref.startswith("wsl:"):
        try:
            r = subprocess.run(["wsl.exe", "-e", "test", "-e", f"/proc/{ref[4:]}"],
                               capture_output=True, timeout=20,
                               creationflags=NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False
    return False


def chat_hwnd(cn):
    return u32.FindWindowW(None, f"cx-chat {cn}")


def hide_chat(cn):
    h = chat_hwnd(cn)
    if h:
        u32.ShowWindow(h, 0)  # SW_HIDE


def show_chat(cn):
    h = chat_hwnd(cn)
    if h:
        u32.ShowWindow(h, 9)  # SW_RESTORE
        u32.SetForegroundWindow(h)


def spawn(side, cn, n):
    sw = cfg().get("switch", {})
    wt = shutil.which("wt") or str(cxpaths.home() / "AppData" / "Local"
                               / "Microsoft" / "WindowsApps" / "wt.exe")
    if side == "win":
        prof = sw.get("win_profile", "PowerShell")
        # pwsh joins bare args after -Command, so no nested quoting through wt;
        # no -NoExit: the tab closes with the session (matches the wsl side)
        args = [wt, "-w", "0", "new-tab", "-p", prof,
                "pwsh", "-Command", "cx", cn, str(n)]
    else:
        prof = sw.get("wsl_profile", "Ubuntu")
        # the .cmd shim keeps bash -lc quoting out of wt's argument reparse
        args = [wt, "-w", "0", "new-tab", "-p", prof,
                "cmd", "/c", WSL_TAB, cn, str(n)]
    subprocess.Popen(args, creationflags=NO_WINDOW)
    log(f"spawned {cn} (slot {n}, {side}, profile {prof})")


# ── Hotkey matching ──────────────────────────────────────────────────────────
# Bound by a low-level BLOCKING filter rather than add_hotkey, for the same
# reason cx-ptt uses one: Windows' NumLock override strips the shift out of a
# Shift+Numpad chord and delivers the keypad's NAVIGATION twin instead (left,
# end, page up…). add_hotkey then neither matches it nor swallows it, so the
# arrow sails into the focused terminal — where cx sessions read it as "go
# back". The filter claims the whole chord and consumes it.
#
# Two suppressing hooks now live in two processes (cx-ptt owns shift+numpad,
# this owns ctrl/alt+shift+numpad). They compose safely ONLY because each one
# returns True for what the other claims: hooks chain, and a hook that
# suppresses hides the event from every hook behind it. Keep it that way.
_KEYPAD_NAV_DIGIT = {"end": "1", "down": "2", "page down": "3", "left": "4",
                     "clear": "5", "right": "6", "home": "7", "up": "8",
                     "page up": "9", "insert": "0"}
_mods = {"shift": False, "ctrl": False, "alt": False}
_last_shift_up = [0.0]
_side_of_mod = {}          # "ctrl"/"alt" -> "win"/"wsl", from the config templates
VK_NUMLOCK = 0x90


def numlock_on():
    return bool(u32.GetKeyState(VK_NUMLOCK) & 1)


_KEYPAD_NAV_NAMES = set(_KEYPAD_NAV_DIGIT) | {"delete"}   # numpad-. 's twin


def nav_form(event):
    return (bool(getattr(event, "is_keypad", False))
            and (event.name or "") in _KEYPAD_NAV_NAMES)


def digit_of(event):
    """Numpad digit for this event, or None. Numpad only — the top row stays
    free, matching cx-ptt's numpad_only rule."""
    if not getattr(event, "is_keypad", False):
        return None
    name = (event.name or "").replace("num ", "")
    return _KEYPAD_NAV_DIGIT.get(name, name if name.isdigit() else None)


def key_filter(event):
    """Blocking filter: return False = swallow system-wide. Stays FAST."""
    name = event.name or ""
    if "shift" in name:
        _mods["shift"] = event.event_type == "down"
        if not _mods["shift"]:
            _last_shift_up[0] = time.time()
        return True
    if "ctrl" in name:
        _mods["ctrl"] = event.event_type == "down"
        return True
    if "alt" in name:
        _mods["alt"] = event.event_type == "down"
        return True
    if event.event_type != "down":
        return True
    if _mods["ctrl"] == _mods["alt"]:
        return True            # need exactly one of ctrl/alt — plain shift+num
    d = digit_of(event)        # is cx-ptt's, ctrl+alt+num is nobody's
    if d is None and not getattr(event, "is_keypad", False):
        return True
    # NumLock lit + a nav name can only be the Shift+Numpad override, so the
    # chord is ours whatever the (stripped) shift bookkeeping says
    if not (nav_form(event) and numlock_on()):
        if not (_mods["shift"] or time.time() - _last_shift_up[0] < 0.08):
            return True        # ctrl+numpad N without shift isn't ours
    if d is None:
        # Keypad key with no digit — Enter, Del, + - * /. Swallowed, not
        # passed: numpad Enter is a submit and would land in the focused
        # terminal mid-turn. Same rule as cx-ptt: under our modifier chord the
        # whole keypad is ours, and an unmapped key is a no-op, never a leak.
        return False
    on_hotkey(_side_of_mod["ctrl" if _mods["ctrl"] else "alt"], int(d))
    return False


def plan_filter(win_t, wsl_t):
    """Map ctrl/alt -> side from the configured templates. Returns False if the
    templates aren't the '<ctrl|alt>+shift+num {n}' shape the filter matches,
    so a hand-edited config falls back to add_hotkey instead of silently
    binding something the user didn't ask for."""
    _side_of_mod.clear()
    for template, side in ((win_t, "win"), (wsl_t, "wsl")):
        parts = [p.strip() for p in template.lower().split("+")]
        if "shift" not in parts or "num {n}" not in parts:
            return False
        mods = [p for p in parts if p in ("ctrl", "alt")]
        if len(mods) != 1 or mods[0] in _side_of_mod:
            return False
        _side_of_mod[mods[0]] = side
    return len(_side_of_mod) == 2


last_fire = {}


def on_hotkey(side, n):
    key = (side, n)
    now = time.time()
    if now - last_fire.get(key, 0.0) < 1.0:
        return
    last_fire[key] = now
    threading.Thread(target=handle, args=(side, n), daemon=True).start()


def handle(side, n):
    target = ("winterm" if side == "win" else "wslterm") + str(n)
    holder = slot_holder(n)
    try:
        t_alive = alive(target)
        if holder == target and t_alive:
            show_chat(target)  # already bound and running — surface it
            log(f"{target}: already bound, surfaced chat")
            return
        if t_alive:
            # HOT SWAP: target already running — no dialog, instant rebind;
            # the backseat session keeps running, its window goes to tray
            if holder and holder != target:
                hide_chat(holder)
            subprocess.run([PY, SLOTTER, target, str(n),
                            "--side", side], capture_output=True, timeout=20,
                           creationflags=NO_WINDOW)
            show_chat(target)
            log(f"slot {n}: hot-swapped to {target}")
            return
        # target must be SPAWNED — takes the slot silently (no dialog: press
        # means intent); a live holder just backseats and keeps running
        if holder and holder != target:
            hide_chat(holder)
        spawn(side, target, n)  # cx itself binds the slot + pidfile
    except Exception as e:
        log(f"{target}: handler error: {e}")


def main():
    # if the service was (re)started from inside a Claude session, children
    # would inherit the nested-session marker and lose transcript saving
    os.environ.pop("CLAUDE_CODE_CHILD_SESSION", None)
    k32.CreateMutexW(None, False, "CxSwitch_singleton")
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    import keyboard
    sw = cfg().get("switch", {})
    win_t = sw.get("win_hotkey", "ctrl+shift+num {n}")
    wsl_t = sw.get("wsl_hotkey", "alt+shift+num {n}")
    if sw.get("suppress_switch", True) and plan_filter(win_t, wsl_t):
        keyboard.hook(key_filter, suppress=True)
        log(f"cx-switch up: blocking filter, numpad only "
            f"({win_t} -> winterm, {wsl_t} -> wslterm)")
    else:
        # leaky fallback: matches the top row too and lets the nav twin escape
        for n in range(10):
            keyboard.add_hotkey(win_t.format(n=n), on_hotkey, args=("win", n),
                                suppress=False)
            keyboard.add_hotkey(wsl_t.format(n=n), on_hotkey, args=("wsl", n),
                                suppress=False)
        log(f"cx-switch up: 20 add_hotkey bindings, NOT suppressed "
            f"({win_t} -> winterm, {wsl_t} -> wslterm)")
    keyboard.wait()


if __name__ == "__main__":
    main()
