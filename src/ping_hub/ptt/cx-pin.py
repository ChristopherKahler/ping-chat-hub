"""cx-pin — keep a cx channel's chat window on top of everything else.

Reached through the `pin` wrapper on every side (pin.cmd for cmd/pwsh, the
POSIX `pin` script for WSL and Git Bash), so it is one keystroke away inside a
Claude session via the `!` bash prefix:

    pin on 4       keep channel 4's window above other windows
    pin off 4      let it fall behind again
    pin toggle 4   flip it            pin status    what's pinned
    pin on         pin every channel that has a window open

Targets are slot digits or codenames, any number of them (`pin on 4 winterm1`).

The window it targets is the `cx-chat <codename>` pipe window — the one that
tails the conversation. That is the per-channel window; a session's actual
terminal is a Windows Terminal TAB, and tabs share one window, so pinning one
would pin all of them.

`pin on` also un-hides a window that is parked in the cx tray (without stealing
focus) — pinning something invisible would otherwise do nothing. Topmost is
window state, not config: if the daemon's chat_guard ever respawns a window,
re-pin it.

usage: cx-pin.py on|off|toggle [<slot|codename> ...]
       cx-pin.py status
"""
import ctypes
import sys
import tomllib
from pathlib import Path
# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths


CONFIG = cxpaths.cx_toml()

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.FindWindowW.restype = ctypes.c_void_p
u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.IsWindowVisible.argtypes = [ctypes.c_void_p]

HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x0008
SW_SHOWNOACTIVATE = 4


def slots():
    with CONFIG.open("rb") as f:
        raw = tomllib.load(f).get("slots", {})
    return {n: (v.get("codename", "") if isinstance(v, dict) else v)
            for n, v in raw.items()}


def hwnd_of(codename):
    return u32.FindWindowW(None, f"cx-chat {codename}") or None


def pinned(h):
    return bool(u32.GetWindowLongW(h, GWL_EXSTYLE) & WS_EX_TOPMOST)


def set_pin(h, on):
    if on and not u32.IsWindowVisible(h):
        u32.ShowWindow(h, SW_SHOWNOACTIVATE)   # pinning a hidden window is a no-op
    u32.SetWindowPos(h, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def status(sl):
    up, down, gone = [], [], []
    for n in sorted(sl):
        tag = f"{n} {sl[n]}"
        h = hwnd_of(sl[n])
        (gone if not h else up if pinned(h) else down).append(tag)
    print(f"[pin] pinned   : {', '.join(up) if up else '(none)'}")
    print(f"      normal   : {', '.join(down) if down else '(none)'}")
    if gone:
        print(f"      no window: {', '.join(gone)}")


def resolve(targets, sl):
    by_name = {c.lower(): n for n, c in sl.items()}
    picked, bad = [], []
    for t in targets:
        if t in sl:
            picked.append(t)
        elif t.lower() in by_name:
            picked.append(by_name[t.lower()])
        else:
            bad.append(t)
    if bad:
        known = " ".join(f"{n}={sl[n]}" for n in sorted(sl))
        print(f"[pin] no such channel: {', '.join(bad)}\n      channels: {known}")
        sys.exit(2)
    return picked


def main():
    argv = sys.argv[1:]
    verb = (argv[0] if argv else "status").lower()
    verb = {"ls": "status", "list": "status"}.get(verb, verb)
    if verb in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if verb not in ("on", "off", "toggle", "status"):
        print(f"[pin] unknown command {verb!r} — expected on|off|toggle|status")
        return 2

    sl = slots()
    if not sl:
        print(f"[pin] no [slots] entries in {CONFIG}")
        return 1
    if verb == "status":
        status(sl)
        return 0

    picked = sorted(sl) if len(argv) == 1 else resolve(argv[1:], sl)
    done, missing = [], []
    for n in picked:
        h = hwnd_of(sl[n])
        if not h:
            missing.append(f"{n} {sl[n]}")
            continue
        want = not pinned(h) if verb == "toggle" else verb == "on"
        set_pin(h, want)
        done.append(f"{n} {sl[n]} {'PINNED' if want else 'normal'}")
    if done:
        print(f"[pin] {', '.join(done)}")
    if missing:
        # a bare `pin on` sweeping every slot shouldn't nag about closed ones
        if len(argv) > 1:
            print(f"[pin] no chat window open for: {', '.join(missing)}")
    elif not done:
        print("[pin] nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
