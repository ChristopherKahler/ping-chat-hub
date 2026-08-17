"""Windows Terminal adapter.

Moved out of daemon.py unchanged — every comment here records a bug that was
paid for once already, so nothing in this file is rewritten "more cleanly".
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import threading
import time
from pathlib import Path


def build_command(cfg, side: str, claude_args: list[str], cwd: str | None,
                  title: str | None, prompt: str | None) -> list[str]:
    """The argv that opens the tab. Split out from the launch so it can be
    asserted without spawning anything."""
    args = " ".join(claude_args)
    pin = title if title and re.fullmatch(r"[\w][\w.-]*", title) else None
    if side == "wsl":
        # The whole boot command is written as a script FILE in WSL and the
        # tab just runs it — the wt → wsl.exe → bash layering re-splits inline
        # command strings and shredded the prompt quoting ("%s: unexpected
        # EOF", Chris's extendly boot 2026-08-17). A file survives any layer.
        q = lambda s: "'" + s.replace("'", "'\\''") + "'"
        lines = []
        if pin:
            lines.append(f"export BASE_RELAY_AS={q(pin)}")
        lines += ["export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1",
                  "unset CLAUDE_CODE_CHILD_SESSION"]
        if pin and cfg.wsl.home_linux:
            # The session records its OWN pid so `clear` can confirm rather
            # than search. `exec` below replaces this shell in place, so $$ is
            # already the pid claude will run under.
            inbox = f"{cfg.wsl.home_linux}/.base-gbl/.base/relay-inbox/{pin}"
            lines += [
                f"mkdir -p {q(inbox)}",
                # /proc/<pid>/stat field 22 is start time in clock ticks since
                # boot: the same "is this still the process I recorded" anchor
                # the Windows side gets from CreationDate.
                'PING_HUB_START=$(awk \'{print $22}\' /proc/$$/stat 2>/dev/null)',
                f'printf \'{{"pid":%s,"image":"claude","created":"%s","side":"wsl"}}\' '
                f'"$$" "$PING_HUB_START" > {q(inbox + "/.pid")}',
            ]
        lines.append(f"exec claude {args}" + (" " + q(prompt) if prompt else ""))
        # the UNC path written to and the Linux path executed come from ONE
        # config key: they are the same directory, and them drifting apart is
        # a spawn that fails silently
        unc = cfg.wsl.bridge_deploy_unc
        if not unc:
            # say so, rather than writing the boot script to a relative path
            raise OSError("no WSL side on this machine (distro unresolved)")
        sdir = Path(unc)
        name = f"spawn-{int(time.time() * 1000)}.sh"
        with open(sdir / name, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        for old in sdir.glob("spawn-*.sh"):   # keep the dir tidy
            try:
                if time.time() - old.stat().st_mtime > 86400:
                    old.unlink()
            except OSError:
                pass
        prof = cfg.terminal.wsl_profile
        return (["wt", "-w", "0", "new-tab"] + (["-p", prof] if prof else []) +
                ["-d", str(Path.home()),
                 "wsl.exe", "--cd", cwd or cfg.wsl.home_linux, "-e", "bash", "-li",
                 f"{cfg.wsl.bridge_deploy_linux}/{name}"])
    # -EncodedCommand, not -Command: wt splits its own command line on bare ';'
    # (subcommand separator), so an inline PS script gets carved up and wt
    # tries to launch " claude ..." as a file (0x80070002)
    pin_env = f"$env:BASE_RELAY_AS='{pin}'; " if pin else ""
    # prompt travels base64 and is decoded in-script: a PS string literal would
    # survive, but PS 5.1 passes embedded double quotes to native exes
    # UNESCAPED, so claude's argv split at the first " and dropped the rest of
    # the briefing — pre-escaping " as \" fixes the handoff
    parg = ""
    if prompt:
        b64 = base64.b64encode(prompt.encode("utf-8")).decode()
        parg = (" $([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
                f"'{b64}')) -replace '\"','\\\"')")
    # This script runs IN the tab's own powershell host, so $PID is the process
    # that owns the tab's lifecycle — the one `clear` must end as a tree. It is
    # recorded here by the process itself; nothing downstream ever searches for
    # it. (Never its parent: WindowsTerminal.exe is one process shared by every
    # tab, and killing that closes all of them.)
    rec = ""
    if pin:
        inbox = str(cfg.paths.base_store / "relay-inbox" / pin).replace("'", "''")
        rec = (f"$d='{inbox}'; New-Item -ItemType Directory -Force -Path $d "
               "| Out-Null; $me=Get-CimInstance Win32_Process -Filter "
               "\"ProcessId=$PID\"; @{pid=$PID; image=$me.Name; "
               "created=$me.CreationDate.ToString('o'); side='win'} "
               "| ConvertTo-Json -Compress | Set-Content -Path "
               "(Join-Path $d '.pid') -Encoding utf8; ")
    script = (f"{pin_env}$env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE='1'; "
              "Remove-Item Env:CLAUDE_CODE_CHILD_SESSION -ErrorAction SilentlyContinue; "
              f"{rec}claude {args}{parg}")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return ["wt", "-w", "0", "new-tab", "-p", cfg.terminal.windows_profile,
            "-d", cwd or str(Path.home()), "powershell", "-NoExit",
            "-EncodedCommand", enc]


def spawn(cfg, side: str, claude_args: list[str], cwd: str | None = None,
          title: str | None = None, prompt: str | None = None) -> None:
    """Tab running claude (+args) with transcript persistence forced — shared
    by New-chat and escalation resume. cwd: workspace to boot in (Windows path
    for win, Linux path for wsl); defaults to home. wsl gets an explicit --cd —
    without it WSL inherits the daemon's cwd, fails the chdir, lands in / and
    trust-prompts for /. title: pin the relay codename via BASE_RELAY_AS
    (base's auto-register honors it) — used to resurrect a dead thread's
    codename onto a fresh session."""
    cmd = build_command(cfg, side, claude_args, cwd, title, prompt)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_CHILD_SESSION"}
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    launch(cmd, env, restore_focus=cfg.terminal.restore_focus)


def launch(cmd: list[str], env: dict, restore_focus: bool = True) -> None:
    """Launch the WT tab WITHOUT stealing the screen: wt -w 0 always activates
    the terminal window, so snapshot the foreground window and WT's minimized
    state first, then restore both once the tab has landed (Chris rule
    2026-08-17: terminals stay suppressed; he pulls them up)."""
    if not restore_focus:
        subprocess.Popen(cmd, env=env)
        return
    import ctypes
    u = ctypes.windll.user32
    prev = u.GetForegroundWindow()
    wt = u.FindWindowW("CASCADIA_HOSTING_WINDOW_CLASS", None)
    was_min = bool(u.IsIconic(wt)) if wt else False
    subprocess.Popen(cmd, env=env)

    def restore():
        time.sleep(1.4)
        wt2 = u.FindWindowW("CASCADIA_HOSTING_WINDOW_CLASS", None)
        if wt2 and was_min:
            u.ShowWindow(wt2, 7)          # SW_SHOWMINNOACTIVE
        if prev and u.GetForegroundWindow() != prev:
            # ALT nudge defeats the foreground lock for a background process
            u.keybd_event(0x12, 0, 0, 0)
            u.SetForegroundWindow(prev)
            u.keybd_event(0x12, 0, 2, 0)  # KEYEVENTF_KEYUP

    threading.Thread(target=restore, daemon=True).start()
