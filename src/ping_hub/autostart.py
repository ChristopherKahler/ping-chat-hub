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
