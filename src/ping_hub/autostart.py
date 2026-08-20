"""Start the hub (and its speech server) at login.

Windows uses a Scheduled Task at logon — the pattern already proven on this
machine by the `falcon-heartbeat` task. Mac uses a launchd agent. Both are
generated from the same two facts: which command, and under whose account.

Nothing here runs at import, and every function returns the command it would
run so `--dry-run` can show it before anything is registered.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HUB_TASK = "ping-chat-hub"
STT_TASK = "ping-chat-hub-stt"
CXPTT_TASK = "ping-chat-hub-cxptt"
DICTATE_TASK = "ping-chat-hub-dictate"

# The interim wiring this replaces (added by hand 2026-08-19 to stop the
# bleeding). It is removed only once its replacement is REGISTERED and the
# registration has been read back — never delete first, or a failed install
# leaves the machine with neither.
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
INTERIM_RUN_VALUE = "cx-ptt-work-channel"


def _pythonw() -> str:
    """The windowless interpreter, so a logon task does not park a console on
    the desktop. Falls back to the normal one if there is no pythonw."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def hub_command() -> list[str]:
    return [_pythonw(), "-m", "ping_hub.cli", "serve"]


def plan(cfg) -> list[tuple[str, list[str]]]:
    """(task name, command) pairs this platform would register."""
    out = [(HUB_TASK, hub_command())]
    if cfg.stt.enabled and cfg.stt.autostart and cfg.stt.launcher:
        out.append((STT_TASK, list(cfg.stt.launcher)))
    # the launcher has to EXIST, not merely resolve. `cx_ptt.launcher` derives a
    # path whether or not anything is there, and a logon task pointing at a
    # missing .cmd is a task that fails silently every single boot — which is
    # indistinguishable, from the outside, from the problem this fixes.
    if (cfg.cx_ptt.enabled and cfg.cx_ptt.autostart
            and cfg.cx_ptt.launcher and cfg.probe.exists(cfg.cx_ptt.launcher)):
        out.append((CXPTT_TASK, [str(cfg.cx_ptt.launcher)]))
    # the desktop mic, on the same terms and for the same reason: a logon task
    # pointing at a .cmd that was never written fails silently every boot
    if (cfg.desktop_stt.enabled and cfg.desktop_stt.autostart
            and cfg.desktop_stt.launcher
            and cfg.probe.exists(cfg.desktop_stt.launcher)):
        out.append((cfg.desktop_stt.task, [str(cfg.desktop_stt.launcher)]))
    return out


# ── windows ──────────────────────────────────────────────────────────────────
def _schtasks_create(name: str, command: list[str]) -> list[str]:
    # /tr takes ONE string; quote each part so a path with spaces survives
    tr = " ".join(f'"{c}"' if " " in c else c for c in command)
    return ["schtasks", "/create", "/f", "/tn", name, "/sc", "onlogon",
            "/rl", "limited", "/tr", tr]


def _schtasks_delete(name: str) -> list[str]:
    return ["schtasks", "/delete", "/f", "/tn", name]


def _schtasks_query(name: str) -> list[str]:
    return ["schtasks", "/query", "/tn", name]


# ── mac ──────────────────────────────────────────────────────────────────────
def _plist(label: str, command: list[str]) -> str:
    args = "\n".join(f"    <string>{c}</string>" for c in command)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f'  <key>Label</key><string>{label}</string>\n'
            f'  <key>ProgramArguments</key>\n  <array>\n{args}\n  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            '</dict>\n</plist>\n')


def _plist_path(label: str, home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"cv.chrisai.{label}.plist"


# ── the platform-agnostic surface ────────────────────────────────────────────
def register(cfg, dry_run: bool = False, run=subprocess.run,
             platform: str | None = None, home: Path | None = None) -> list[str]:
    """Register every task for this platform. Returns human-readable lines —
    what it did, or on a dry run what it would do."""
    platform = platform or ("win" if os.name == "nt" else "mac")
    home = home or Path.home()
    lines = []
    for name, command in plan(cfg):
        if platform == "win":
            cmd = _schtasks_create(name, command)
            lines.append(" ".join(cmd))
            if not dry_run:
                r = run(cmd, capture_output=True, text=True)
                lines.append(f"  -> exit {r.returncode} "
                             f"{(r.stdout or r.stderr or '').strip()[:160]}")
        else:
            p = _plist_path(name, home)
            lines.append(f"write {p}")
            if not dry_run:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_plist(name, command), encoding="utf-8")
                r = run(["launchctl", "load", "-w", str(p)],
                        capture_output=True, text=True)
                lines.append(f"  -> exit {r.returncode}")
    return lines


def unregister(cfg, run=subprocess.run, platform: str | None = None,
               home: Path | None = None) -> list[str]:
    platform = platform or ("win" if os.name == "nt" else "mac")
    home = home or Path.home()
    lines = []
    for name, _ in plan(cfg):
        if platform == "win":
            r = run(_schtasks_delete(name), capture_output=True, text=True)
            lines.append(f"{name}: exit {r.returncode}")
        else:
            p = _plist_path(name, home)
            run(["launchctl", "unload", str(p)], capture_output=True, text=True)
            p.unlink(missing_ok=True)
            lines.append(f"{name}: removed {p}")
    return lines


def supersede_interim_run_key(cfg, run=subprocess.run,
                             platform: str | None = None) -> list[str]:
    """Retire the hand-added Run key, once its replacement is proven.

    The order is the whole point. A logon mechanism removed before its
    replacement is confirmed leaves the machine with NEITHER, and the thing
    both of them start is the one that already went missing for sixteen hours.
    So: is there a task to replace it, does the registry agree the task exists,
    and only then is the old value deleted.
    """
    platform = platform or ("win" if os.name == "nt" else "mac")
    if platform != "win":
        return []
    if not any(name == CXPTT_TASK for name, _ in plan(cfg)):
        return [f"{INTERIM_RUN_VALUE}: left in place — no {CXPTT_TASK} to "
                f"replace it"]
    r = run(_schtasks_query(CXPTT_TASK), capture_output=True, text=True)
    if r.returncode != 0:
        return [f"{INTERIM_RUN_VALUE}: LEFT IN PLACE — {CXPTT_TASK} did not "
                f"register, so it is still the only thing starting cx-ptt"]
    q = run(["reg", "query", RUN_KEY, "/v", INTERIM_RUN_VALUE],
            capture_output=True, text=True)
    if q.returncode != 0:
        return [f"{INTERIM_RUN_VALUE}: not present"]
    d = run(["reg", "delete", RUN_KEY, "/v", INTERIM_RUN_VALUE, "/f"],
            capture_output=True, text=True)
    if d.returncode != 0:
        return [f"{INTERIM_RUN_VALUE}: removal failed, left in place "
                f"({(d.stdout or d.stderr or '').strip()[:120]})"]
    return [f"{INTERIM_RUN_VALUE}: removed — superseded by {CXPTT_TASK}"]


def status(cfg, run=subprocess.run, platform: str | None = None,
           home: Path | None = None) -> dict:
    """registered / absent per task. Absent is a fact, not an error."""
    platform = platform or ("win" if os.name == "nt" else "mac")
    home = home or Path.home()
    out = {}
    for name, _ in plan(cfg):
        if platform == "win":
            r = run(_schtasks_query(name), capture_output=True, text=True)
            out[name] = "registered" if r.returncode == 0 else "absent"
        else:
            out[name] = ("registered" if _plist_path(name, home).exists()
                         else "absent")
    return out


# ── wsl ──────────────────────────────────────────────────────────────────────
# The bridge is the third thing that has to survive a reboot, and the only one
# that does not run on this side of the machine. It lives inside WSL, so "start
# at login" becomes a systemd USER unit, driven from Windows through interop.
BRIDGE_UNIT = "hub-bridge.service"


def bridge_unit_text(cfg, python: str = "") -> str:
    """The unit that keeps the WSL bridge up.

    A USER unit, not a system one: everything it touches belongs to the
    operator — their base store, their ~/.claude transcripts, their relay
    inbox. It is reached from `default.target` at boot with no login because
    the account has lingering enabled; without linger this would only start on
    the first terminal login, and a hub opened from a phone before any terminal
    would still find the bridge down.

    Every path is absolute and the environment is explicit. systemd starts a
    service with an almost-empty env, so a bare `python3` resolves against a
    PATH the unit does not have, and the bridge derives every path it uses from
    Path.home() — HOME is load-bearing here, not decoration. The sibling units
    already on this machine learned the same lesson the hard way.
    """
    py = python or cfg.wsl.bridge_python
    script = f"{cfg.wsl.bridge_deploy_linux}/wsl-bridge.py"
    home = cfg.wsl.home_linux
    return (
        "[Unit]\n"
        "Description=ping-chat-hub WSL bridge — the hub's window onto the WSL relay store\n"
        f"Documentation=file://{script}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={py} {script}\n"
        "\n"
        "# systemd starts services with an almost-empty env. The bridge derives\n"
        "# every path from Path.home(), so HOME is load-bearing; PATH carries\n"
        "# ~/.local/bin for the base binary the bridge shells out to.\n"
        f"Environment=HOME={home}\n"
        f"Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "SyslogIdentifier=hub-bridge\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def wsl_sh(command: str) -> list[str]:
    """Run one shell line inside WSL, from Windows.

    `-e sh -c`, deliberately not a login shell. `systemctl --user` needs
    XDG_RUNTIME_DIR and interop already supplies it (measured on this machine:
    /run/user/1000 from a plain `-e sh -c`), so a login shell would drag the
    operator's whole profile in to buy nothing.
    """
    return ["wsl.exe", "-e", "sh", "-c", command]


def _stdout(r) -> str:
    return (getattr(r, "stdout", "") or "").replace("\x00", "").strip()


def bridge_has_systemd(cfg, run=subprocess.run) -> bool:
    """Is systemd PID 1 in the distro?

    `/run/systemd/system` exists only when it is. `systemctl --user
    is-system-running` is the wrong question: it answers `degraded` on a
    healthy box that happens to have one failed unit, and that is not a reason
    to refuse to install ours.
    """
    try:
        r = run(wsl_sh("test -d /run/systemd/system"),
                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(r, "returncode", 1) == 0


def bridge_python(cfg, run=subprocess.run) -> str:
    """The interpreter the unit will name, asked of WSL itself.

    Resolved here rather than in config because this is the one moment the
    installer is already talking to WSL. Falls back to the configured default
    so a machine that cannot answer still gets a unit that works.
    """
    try:
        r = run(wsl_sh("command -v python3"), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return cfg.wsl.bridge_python
    found = _stdout(r).splitlines()
    return found[0].strip() if found and found[0].strip() else cfg.wsl.bridge_python


# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP. Windows-only, and the entire
# point of the fallback: the wsl.exe that holds the interop session open must
# not die with the hub that launched it.
_DETACHED = 0x00000008 | 0x00000200


def start_bridge_detached(cfg, python: str = "", popen=subprocess.Popen) -> bool:
    """Start the bridge inside WSL WITHOUT systemd, so that it outlives us.

    A backgrounded shell job cannot do this, and it fails silently, which is the
    worst way to fail: WSL tears its children down when the interop session
    exits, so `sh -c 'setsid nohup ... &'` returns 0 and leaves nothing running.
    Measured 2026-08-19 with a harmless control — a `sleep 45` launched that way
    was gone within six seconds, while the identical command run inside WSL
    survived. The session has to be held open by a Windows process instead, and
    that process must be DETACHED, or the bridge would die with the hub.
    """
    argv = ["wsl.exe", "-e", python or cfg.wsl.bridge_python,
            f"{cfg.wsl.bridge_deploy_linux}/wsl-bridge.py"]
    try:
        popen(argv, creationflags=(_DETACHED if os.name == "nt" else 0),
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    except (OSError, ValueError):
        return False
    return True


def bridge_plan() -> list[str]:
    """The shell lines `register_bridge` runs, in order.

    `restart`, not `start`: this runs right after a deploy that just replaced
    the script on disk, and `start` on an already-running unit is a no-op that
    would leave the old code serving.
    """
    return ["systemctl --user daemon-reload",
            f"systemctl --user enable {BRIDGE_UNIT}",
            f"systemctl --user restart {BRIDGE_UNIT}"]


def register_bridge(cfg, run=subprocess.run, dry_run: bool = False) -> list[str]:
    """Enable and start the bridge unit. Idempotent: the same three lines run
    every time, and systemd owns the symlink bookkeeping so we never invent our
    own. Returns human-readable lines — what it did, or on a dry run what it
    would do."""
    if not cfg.wsl.enabled or not cfg.wsl.distro:
        return ["no WSL side on this machine: bridge unit not installed"]
    if not bridge_has_systemd(cfg, run=run):
        return [f"no systemd in {cfg.wsl.distro}: the unit cannot be installed.",
                f"start the bridge by hand with:",
                f"  python3 {cfg.wsl.bridge_deploy_linux}/wsl-bridge.py"]
    lines = []
    for command in bridge_plan():
        argv = wsl_sh(command)
        lines.append(" ".join(argv))
        if dry_run:
            continue
        try:
            r = run(argv, capture_output=True, text=True, timeout=60)
            detail = ((r.stdout or "") + (r.stderr or "")).strip()[:160]
            lines.append(f"  -> exit {r.returncode} {detail}")
        except (OSError, subprocess.TimeoutExpired) as e:
            lines.append(f"  -> failed: {e}")
    return lines


def bridge_status(cfg, run=subprocess.run) -> dict:
    """{enabled, active} as systemd reports them.

    `absent` is a fact, not an error: a machine that never installed the unit
    is not a broken one, and the two must not collapse into one falsy no.
    """
    if not cfg.wsl.enabled or not cfg.wsl.distro:
        return {"enabled": "absent", "active": "absent", "detail": "no WSL side"}
    command = (f"systemctl --user is-enabled {BRIDGE_UNIT} 2>/dev/null || echo absent; "
               f"systemctl --user is-active {BRIDGE_UNIT} 2>/dev/null || echo inactive")
    try:
        r = run(wsl_sh(command), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"enabled": "unknown", "active": "unknown", "detail": str(e)[:120]}
    words = _stdout(r).split()
    return {"enabled": words[0] if words else "unknown",
            "active": words[1] if len(words) > 1 else "unknown",
            "detail": ""}
