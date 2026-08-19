"""cx-chat <codename> — the visible pipe window for one cx terminal.

Pure renderer: tails .base-gbl\\cx\\feeds\\<codename>.jsonl, which the cx-ptt
daemon writes (your STT pings out, the session's relay replies in). Keeping it
a dumb tail means it can open before/after the daemon, survive daemon
restarts, and never race it for the mic, hotkeys, or inboxes.

One window per codename (mutex); a second launch just says so and exits.
Run:  python cx_chat.py <codename> [--once]     (--once = print history, exit)
"""
import ctypes
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path
# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths


CONFIG_PATH = cxpaths.cx_toml()
FEED_DIR = cxpaths.cx_dir() / "feeds"
PIDS_DIR = cxpaths.cx_dir() / "pids"
WSL_EXE = r"C:\Windows\System32\wsl.exe"
HISTORY_LINES = 40

# warm palette (Chris is eye-strain sensitive — no bright blues)
DIM = "\x1b[90m"
AMBER = "\x1b[93m"
GREEN = "\x1b[92m"
RESET = "\x1b[0m"


def enable_ansi():
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    if k.GetConsoleMode(h, ctypes.byref(mode)):
        k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    # display-only window: kill QuickEdit so a stray click can't enter mark
    # mode ("Select ..." title) and freeze output indefinitely
    hin = k.GetStdHandle(-10)
    if k.GetConsoleMode(hin, ctypes.byref(mode)):
        k.SetConsoleMode(hin, (mode.value | 0x0080) & ~0x0040)


class PidWatch:
    """Watch the session process named in .base-gbl\\cx\\pids\\<codename>
    ('wsl:<pid>' or 'win:<pid>', written by cx at launch). SessionEnd hooks
    are bypassed when the terminal is X-closed, so liveness comes from the
    process itself: pid gone -> window self-closes."""

    def __init__(self, codename):
        self.codename = codename
        self.ref = None
        self.handle = None
        self.confirmed = False  # pid must be SEEN alive before death counts
        self.adopt()

    def _read(self):
        try:
            return (PIDS_DIR / self.codename).read_text(encoding="utf-8").strip()
        except Exception:
            return None

    def adopt(self):
        ref = self._read()
        changed = ref != self.ref
        if changed:
            self.confirmed = False  # a new pid starts unproven
        self.ref = ref
        self.handle = None
        if ref and ref.startswith("win:"):
            try:
                h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(ref[4:]))
                self.handle = h if h else "dead"
            except Exception:
                self.handle = "dead"
        return changed

    def alive(self):
        if not self.ref:
            self.adopt()  # pidfile may land moments after the window opens
            return True
        up = True
        if self.ref.startswith("win:"):
            if self.handle in (None, "dead"):
                up = False
            else:
                up = ctypes.windll.kernel32.WaitForSingleObject(self.handle, 0) != 0
        elif self.ref.startswith("wsl:"):
            try:
                r = subprocess.run([WSL_EXE, "-e", "test", "-e", f"/proc/{self.ref[4:]}"],
                                   capture_output=True, timeout=10)
                up = r.returncode == 0
            except Exception:
                up = True  # WSL hiccup: never close the window on a fluke
        if up:
            self.confirmed = True
        return up

    def session_over(self):
        """Close only when a pid this window SAW alive has died. A pid that
        was dead from the start is a stale pidfile (launch race, transient
        recorder) — keep re-adopting it instead of self-destructing."""
        if self.alive():
            return False
        if self.adopt():
            return self.confirmed and not self.alive()
        if not self.confirmed:
            return False
        return True


def render(entry, codename):
    ts = entry.get("ts", "")[11:19]
    who = entry.get("who", "?")
    text = entry.get("text", "")
    if who.upper() == "CHRIS":
        return f"{DIM}{ts}{RESET} {AMBER}you ▸{RESET} {text}"
    if who.upper() == "SYSTEM":
        return f"{DIM}{ts} [system] {text}{RESET}"
    return f"{DIM}{ts}{RESET} {GREEN}◂ {who.lower()}{RESET} {text}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    once = "--once" in sys.argv
    if not args:
        print("usage: cx_chat.py <codename> [--once]")
        return 1
    codename = args[0]

    ctypes.windll.kernel32.CreateMutexW(None, False, f"CxChat_{codename}")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print(f"a cx-chat window for '{codename}' is already open — closing this one")
        time.sleep(1)
        return 0

    enable_ansi()
    ctypes.windll.kernel32.SetConsoleTitleW(f"cx-chat {codename}")
    # this window must not die by accident: remove Close from the system menu
    # (grays the X, blocks Alt+F4 — self-teardown and taskkill still work).
    # Hide via the tray toggle or minimize (minimize = hide to tray, below).
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        menu = ctypes.windll.user32.GetSystemMenu(hwnd, False)
        if menu:
            ctypes.windll.user32.DeleteMenu(menu, 0xF060, 0)  # SC_CLOSE

    hotkey = None
    try:
        with CONFIG_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        template = cfg.get("settings", {}).get("slot_hotkey", "shift+num {n}")
        for n, v in cfg.get("slots", {}).items():
            cn = v.get("codename") if isinstance(v, dict) else v
            if cn == codename:
                hotkey = template.format(n=n) + f" (slot {n})"
                break
        if not hotkey:
            for name, block in cfg.get("terminals", {}).items():
                if block.get("codename", name) == codename and block.get("hotkey"):
                    hotkey = block["hotkey"]
                    break
    except Exception:
        pass

    print(f"{AMBER}── cx-chat: {codename} ──{RESET}")
    if hotkey:
        print(f"{DIM}hotkey {hotkey} (press = mic on, press again = send). "
              f"Needs the cx-ptt daemon (Work-Channel.cmd) running.{RESET}")
    else:
        print(f"{DIM}'{codename}' has no slot or [terminals.{codename}] block in cx.toml — "
              f"bind a hotkey with: cx {codename} <0-9> (cx-ptt arms it live).{RESET}")
    print()

    feed = FEED_DIR / f"{codename}.jsonl"
    pos = 0
    if feed.exists():
        lines = feed.read_text(encoding="utf-8").splitlines()
        pos = feed.stat().st_size
        for ln in lines[-HISTORY_LINES:]:
            try:
                print(render(json.loads(ln), codename))
            except Exception:
                pass
    else:
        print(f"{DIM}(no traffic yet){RESET}")
    if once:
        return 0

    watch = PidWatch(codename)
    buf = ""
    tick = 0
    while True:
        time.sleep(0.5)
        tick += 1
        if tick % 10 == 0:
            enable_ansi()  # re-kill QuickEdit: something (Clink?) re-enables
                           # it and a stray click then freezes output for good
            if hwnd and ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # minimize = to tray
            if watch.session_over():  # pid check every ~5s
                print(f"\n{DIM}(session '{codename}' ended — closing){RESET}")
                time.sleep(2)
                return 0
        try:
            if not feed.exists():
                continue
            size = feed.stat().st_size
            if size < pos:  # truncated/rotated — start over
                pos = 0
                buf = ""
            if size == pos:
                continue
            with feed.open("r", encoding="utf-8") as f:
                f.seek(pos)
                buf += f.read()
                pos = f.tell()
            while "\n" in buf:
                ln, buf = buf.split("\n", 1)
                if ln.strip():
                    try:
                        print(render(json.loads(ln), codename))
                    except Exception:
                        pass
        except KeyboardInterrupt:
            return 0
        except Exception:
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
