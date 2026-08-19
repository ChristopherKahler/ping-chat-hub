"""Restarting the cx-ptt hotkey daemon, and switching audio devices.

Both are Windows-and-cx-ptt-only, and both are CAPABILITY GATED: a machine
without the launcher, or without the published device list, renders settings
with the controls simply absent. Never an error, because Albert's Mac is a
supported machine, not a broken one.

The restart does NOT hunt for the launcher's cmd.exe. cx-ptt restarts ITSELF on
a mic change (its own restart_self) by respawning a bare `python cx-ptt.py` --
no cmd root, no tee'd log. Measured on the live machine: a launcher-started
tree is cmd -> powershell -> python, but after any mic change it is a lone
python. So the anchor is the python process that IS cx-ptt, and the root is
found by walking UP from it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ping_hub import proc, reap

SCRIPT = "cx-ptt.py"

# cx-ptt republishes the device list on its own 30s refresh. Older than this
# and we say so rather than presenting a stale list as current.
DEVICES_STALE_AFTER = 180.0

# Liveness is a different question from list freshness, so it gets its own
# budget: three missed refreshes, not six. Measured on the live machine
# 2026-08-19 with the daemon up -- audio-devices.json was 7.1s old while
# ptt-daemon.log was already 64.9s stale, because the log only writes when
# something happens and the device list is written on a timer. That is the
# whole reason the heartbeat is the devices file and not the log.
HEARTBEAT_STALE_AFTER = 90.0


# -- the process tree --------------------------------------------------------
def list_processes(query=None) -> list[dict]:
    """[{pid, ppid, name, cmdline}] -- injectable, so the tree logic is
    testable without real processes."""
    if query is not None:
        return query()
    r = proc.run(["powershell", "-NoProfile", "-Command",
                  "Get-CimInstance Win32_Process | Select-Object "
                  "ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
                 capture_output=True, text=True, timeout=30)
    try:
        rows = json.loads((r.stdout or "").strip() or "[]")
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [{"pid": int(x.get("ProcessId") or 0),
             "ppid": int(x.get("ParentProcessId") or 0),
             "name": str(x.get("Name") or ""),
             "cmdline": str(x.get("CommandLine") or "")}
            for x in rows if x.get("ProcessId")]


def find_daemon(rows: list[dict]) -> dict | None:
    """The python process running cx-ptt.py, whatever launched it.

    Anchoring on the launcher instead would report "not running" on any machine
    where cx-ptt has restarted itself since boot.
    """
    for p in rows:
        if (SCRIPT in p.get("cmdline", "")
                and p.get("name", "").lower().startswith("python")):
            return p
    return None


def launch_root(daemon: dict, rows: list[dict]) -> dict:
    """Walk up while the ancestor is still part of THIS launch chain.

    An ancestor qualifies only if its own command line mentions cx-ptt.py (the
    powershell wrapper) or a .cmd (the launcher). Anything else -- explorer, a
    terminal, the shell someone typed in -- is where the chain stops. Killing
    past it would take unrelated windows with it.
    """
    by_pid = {p["pid"]: p for p in rows}
    root = daemon
    seen = {daemon["pid"]}
    while True:
        parent = by_pid.get(root.get("ppid", 0))
        if not parent or parent["pid"] in seen:
            return root
        cmd = parent.get("cmdline", "").lower()
        if SCRIPT not in cmd and ".cmd" not in cmd:
            return root
        seen.add(parent["pid"])
        root = parent


def status(cfg, rows=None, exists=None) -> dict:
    exists = exists or (lambda p: Path(p).exists())
    rows = list_processes() if rows is None else rows
    daemon = find_daemon(rows)
    return {"running": bool(daemon),
            "pid": daemon["pid"] if daemon else None,
            "root": launch_root(daemon, rows)["pid"] if daemon else None,
            "launcher": str(cfg.cx_ptt.launcher),
            "launcher_present": bool(exists(cfg.cx_ptt.launcher))}


def heartbeat(cfg, now=None, read=None) -> dict:
    """Is cx-ptt alive? Asked of the file it rewrites on its own timer.

    The alternative is `status()`, which enumerates every Win32_Process through
    CIM with a 30s timeout. That is the right instrument for "which pid" and
    the wrong one for a supervision loop -- it cannot run every few seconds,
    and a supervisor that cannot check often cannot notice.

    Absent is not dead: a machine that never had cx-ptt has no devices file and
    must not read as a daemon that died. The caller gets the distinction.
    """
    path = cfg.cx_ptt.devices_json
    try:
        raw = read(path) if read else Path(path).read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, ValueError) as e:
        return {"alive": False, "known": False, "age": None,
                "detail": "no heartbeat file at %s: %s" % (path, e)}
    age = _age_seconds(doc.get("ts"), now)
    if age is None:
        return {"alive": False, "known": False, "age": None,
                "detail": "heartbeat file at %s carries no usable timestamp" % path}
    alive = age <= HEARTBEAT_STALE_AFTER
    return {"alive": alive, "known": True, "age": age,
            "detail": ("last heartbeat %.0fs ago" % age) if alive else
                      ("no heartbeat for %.0fs (refresh is ~30s)" % age)}


def restart(cfg, rows=None, exists=None, kill=None, start=None, facts=None) -> dict:
    """Kill the whole launch tree, confirm it died, then start the launcher.

    Relaunch ALWAYS goes through the launcher, even when the process we killed
    was a bare python: that is how the tee'd log and the window title come
    back, so the button repairs a degraded daemon rather than re-running it
    degraded.
    """
    exists = exists or (lambda p: Path(p).exists())
    launcher = cfg.cx_ptt.launcher
    if not exists(launcher):
        return {"ok": False, "detail": "no launcher at " + str(launcher)}

    rows = list_processes() if rows is None else rows
    daemon = find_daemon(rows)
    if daemon is None:
        # not a failure: there is simply nothing to restart. Say which it is,
        # because "started it" and "restarted it" are different outcomes.
        ok, why = _start(launcher, start)
        return {"ok": ok, "started": True, "restarted": False,
                "detail": why or "cx-ptt was not running; started it"}

    root = launch_root(daemon, rows)
    before = reap.process_facts(daemon["pid"], query=facts)
    _kill(root["pid"], kill)

    # confirm by the ANCHOR, never by taskkill's exit code: it reports success
    # for a tree it only partly killed
    after = reap.process_facts(daemon["pid"], query=facts)
    if after is not None and _same(before, after):
        return {"ok": False, "pid": daemon["pid"], "root": root["pid"],
                "detail": "cx-ptt pid %d survived the kill" % daemon["pid"]}

    ok, why = _start(launcher, start)
    return {"ok": ok, "started": True, "restarted": True,
            "killed": root["pid"], "anchor": daemon["pid"],
            "detail": why or "killed %d, started %s" % (root["pid"], Path(launcher).name)}


def _same(before: dict | None, after: dict | None) -> bool:
    """The same process, not merely the same pid -- a reused pid is a
    different process wearing the number."""
    if not before or not after:
        return False
    return (str(before.get("image", "")).lower() == str(after.get("image", "")).lower()
            and reap._norm(before.get("created")) == reap._norm(after.get("created")))


def _kill(pid: int, kill=None) -> None:
    if kill is not None:
        kill(pid)
        return
    proc.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
             capture_output=True, text=True, timeout=30)


def _start(launcher, start=None) -> tuple[bool, str]:
    if start is not None:
        return start(launcher)
    try:
        proc.popen(["cmd", "/c", "start", "", str(launcher)])
        return True, ""
    except OSError as e:
        return False, "could not start %s: %s" % (launcher, e)


# -- audio devices -----------------------------------------------------------
def read_devices(cfg, now=None, read=None) -> dict:
    """The device list cx-ptt publishes on its own refresh cycle.

    Reading its file rather than shelling out ourselves keeps a 1.2s PowerShell
    call off the settings path -- that panel is not allowed to wait on anything
    again -- and leaves exactly one program talking to the audio module.
    """
    path = cfg.cx_ptt.devices_json
    try:
        raw = read(path) if read else Path(path).read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, ValueError) as e:
        return {"available": False, "playback": [], "recording": [],
                "detail": "no device list at %s: %s" % (path, e)}
    devs = doc.get("devices") or {}
    age = _age_seconds(doc.get("ts"), now)
    return {"available": True,
            "playback": devs.get("Playback") or [],
            "recording": devs.get("Recording") or [],
            "ts": doc.get("ts"),
            "age": age,
            # stale is a THIRD state: the list is real, but cx-ptt stopped
            # refreshing it. Worth saying, rather than showing devices that
            # may since have been unplugged as though they were current.
            "stale": age is None or age > DEVICES_STALE_AFTER}


def _age_seconds(ts, now=None) -> float | None:
    """Parse, never string-compare. This stack spells one instant both
    '-0500' and '-05:00', and as strings the colon form sorts later."""
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now() if now else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current - when).total_seconds()


def set_device(cfg, device_id: str, kind: str, ps=None) -> dict:
    """Switch the default device, then CONFIRM the default actually moved.

    Set-AudioDevice exiting 0 is not proof: the confirmation is the published
    list showing the new device as default.
    """
    if kind not in ("mic", "speaker"):
        return {"ok": False, "detail": "kind must be mic or speaker"}
    if not device_id:
        return {"ok": False, "detail": "no device id given"}
    out = _ps(("Import-Module AudioDeviceCmdlets; "
               "Set-AudioDevice -ID '%s' | Out-Null" % device_id), ps)
    if out is None:
        return {"ok": False, "detail": "the audio module did not accept the switch"}
    # a mic change needs cx-ptt restarted: its input stream binds the default
    # device once, at open. Told to the caller so the UI can say so BEFORE it
    # happens -- a silent restart mid-dictation reads as a crash.
    return {"ok": True, "kind": kind, "needs_restart": kind == "mic"}


def _ps(script: str, ps=None) -> str | None:
    """pwsh first: the audio module is NOT installed for Windows PowerShell
    5.1 on this machine (measured -- it reports no valid module file), so
    trying powershell first would fail on every call before succeeding."""
    if ps is not None:
        return ps(script)
    for exe in ("pwsh", "powershell"):
        try:
            r = proc.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=25)
        except OSError:
            continue
        if r.returncode == 0:
            return r.stdout or ""
    return None
