"""cx-relay-watch — harness-armed idle waker + teardown for cx sessions.

Armed automatically by the HARNESS, never by Claude:
  SessionStart (--boot)  arms it the moment the session boots, before any
                         prompt exists. If pings spooled while the session
                         was down, it wakes the session immediately.
  Stop (--idle)          re-arms it every time the session goes idle again.
  SessionEnd (--teardown) shuts the session's pieces down: tombstones this
                         codename (the live watcher sees it and exits) and
                         kills its cx-chat pipe window. Skipped on /clear —
                         the terminal continues with a fresh session there.

--boot/--idle run as asyncRewake hooks: background process, terminal never
blocked. When a relay ping lands in this codename's inbox the script prints
it and exits 2 — that wakes the session with the text below as context.

Watches the ping INBOX directory directly because `base relay wait` does not
see the ping lane at all (verified 2026-08-11: pings sat spooled in the inbox
while wait timed out, with and without --type ping — wait listens on the
send/task spool only).

The ping file is left in place: base's own pre-tool-use hook screams it on
the next tool call and the session's reply ping clears it — this watcher
only supplies the wake.

The poll loop also exits when its parent process dies (terminal force-closed)
and after watcher_bound_seconds (cx.toml, default 4h) as a final backstop.

Before any of that it arms the session's .pid record (see arm_pid_record) --
the one thing it does for sessions opened by hand. Those sessions get the
record and nothing else: no conduct block, no tombstone, no watcher. A session
with no BASE_RELAY_AS and no registry entry still exits 0 instantly.

Deployed once per side by `ping-hub install` (Windows sessions and WSL
sessions each get a copy, alongside the cxpaths.py it imports).
"""
import json
import os
import sys
import time
from pathlib import Path
# paths derive rather than being spelled out -- see cxpaths.py, which ships
# beside this file. Vendored into ping-chat-hub 2026-08-19.
import cxpaths


env_codename = os.environ.get("BASE_RELAY_AS", "").strip()
# a placeholder, so the path block below can build the roots before the real
# codename is known; every path is rebuilt once it is
codename = env_codename or "_cx_unresolved"

WIN = os.name == "nt"
if WIN:
    INBOX = cxpaths.inbox_root() / codename
    CONFIG = cxpaths.cx_toml()
    DOWN = cxpaths.cx_dir() / "down" / codename
else:
    # INBOX is this side's OWN store; CONFIG and DOWN are the Windows
    # one, reached through /mnt/c -- which is what cxpaths resolves
    INBOX = Path.home() / ".base-gbl/.base/relay-inbox" / codename
    CONFIG = cxpaths.cx_toml()
    DOWN = cxpaths.cx_dir() / "down" / codename

# CX_RELAY_ROOT exists so the arm below can be exercised against a throwaway
# store. This module cannot be imported to test it -- importing runs the poll
# loop and hangs -- so its tests drive the script as a subprocess with
# --arm-only, and they need somewhere harmless to write. Unset in production.
_root = os.environ.get("CX_RELAY_ROOT", "").strip()
# derived, never restated: INBOX is <store>/relay-inbox/<codename>, so the
# store is two levels up. A copy whose paths differ is still patched right.
STORE = Path(_root) if _root else INBOX.parent.parent


def _registered_title() -> str:
    """This session's relay title from the registry, when the environment has
    no codename to give.

    A session opened by hand has none at boot: it acquires one later, when it
    runs `base relay register --as X`. Reading the registry is what lets those
    sessions arm at all, and they are exactly the ones CLEAR could not close.
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not sid:
        return ""
    try:
        doc = json.loads((STORE / "sessions.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    for title, row in (doc.get("sessions") or {}).items():
        if isinstance(row, dict) and row.get("session_id") == sid:
            return str(row.get("title") or title)
    return ""


if not env_codename:
    codename = _registered_title()
    if not codename:
        sys.exit(0)
INBOX = STORE / "relay-inbox" / codename
DOWN = DOWN.with_name(codename)

# shells a session can be sitting in; the anchor walk climbs through these and
# stops at anything else, so it can never reach past the tab into a service
_SHELLS_WIN = ("powershell.exe", "pwsh.exe", "cmd.exe", "bash.exe", "sh.exe",
               "zsh.exe", "fish.exe", "nu.exe")
_SHELLS_NIX = ("bash", "-bash", "sh", "-sh", "zsh", "-zsh", "dash", "fish")
# ping_hub.reap.NEVER_KILL, kept in sync by hand: the terminal host is ONE
# process shared by every tab, so a record pointing at it would close them all
_NEVER = ("windowsterminal.exe", "explorer.exe", "svchost.exe", "csrss.exe",
          "wininit.exe", "services.exe", "system", "init", "systemd")

_PS_ANCHOR = """$ErrorActionPreference='SilentlyContinue'
$shells=@(%(shells)s)
$never=@(%(never)s)
$p=%(start)d
$found=$false
for($i=0;$i -lt 12;$i++){
  $o=Get-CimInstance Win32_Process -Filter "ProcessId=$p"
  if(-not $o){ break }
  if($o.Name -like 'claude*'){ $found=$true; break }
  $p=$o.ParentProcessId
  if(-not $p){ break }
}
if($found){
  $a=Get-CimInstance Win32_Process -Filter "ProcessId=$p"
  for($i=0;$i -lt 6 -and $a;$i++){
    $q=Get-CimInstance Win32_Process -Filter "ProcessId=$($a.ParentProcessId)"
    if(-not $q){ break }
    if($never -contains $q.Name.ToLower()){ break }
    if(-not ($shells -contains $q.Name.ToLower())){ break }
    $a=$q
  }
  Write-Output ($a.ProcessId.ToString()+'|'+$a.Name+'|'+$a.CreationDate.ToString('o'))
}"""


def _cmdline(pid: int) -> str:
    try:
        return Path("/proc/%d/cmdline" % pid).read_text().replace("\0", " ")
    except OSError:
        return ""


def _proc_nix(pid: int):
    """(comm, ppid, start_ticks) for a Linux pid, or None.

    comm sits in parens and may contain spaces, so split on the LAST ')'
    rather than tokenising -- the same rule ping_hub.reap._wsl_facts follows,
    and `created` is field 22 raw for the same reason: it is a tick count, and
    the reaper compares it to another tick count, never to a timestamp.
    """
    try:
        line = Path("/proc/%d/stat" % pid).read_text()
    except OSError:
        return None
    head, _, rest = line.rpartition(")")
    fields = rest.split()
    if len(fields) < 20:
        return None
    return head.partition("(")[2] or "claude", int(fields[1]), fields[19]


def _anchor():
    """{pid, image, created} for the process whose death ends this session.

    NOT this hook's parent. The hook is spawned by claude and its parent is a
    throwaway shell: an earlier attempt anchored there and recorded a bash.exe
    that was already gone minutes later. A record pointing at a dead pid is
    worse than no record -- pid reuse is the exact thing confirm() exists to
    stop.

    So: find claude itself (CLAUDE_PID, else climb to it), then climb through
    SHELLS only. That lands on the tab's shell, the same process the hub's
    boot script records with $PID / $$, and it cannot walk past the tab: a
    headless claude under a node service finds no shell parent and anchors on
    claude itself. Returns None rather than guessing, and CLEAR then refuses
    exactly as it does today.
    """
    try:
        start = int(os.environ.get("CLAUDE_PID", "") or os.getpid())
    except ValueError:
        start = os.getpid()
    if WIN:
        import subprocess
        script = _PS_ANCHOR % {
            "shells": ",".join("'%s'" % s for s in _SHELLS_WIN),
            "never": ",".join("'%s'" % s for s in _NEVER),
            "start": start}
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=25,
                             creationflags=0x08000000).stdout.strip()
        pid, sep, rest = out.partition("|")
        image, sep2, created = rest.partition("|")
        if not (sep and sep2 and pid.isdigit() and image and created):
            return None
        return {"pid": int(pid), "image": image, "created": created}
    pid, found = start, False
    for _ in range(12):
        row = _proc_nix(pid)
        if row is None:
            break
        comm, ppid, _start = row
        if comm == "claude" or (comm.startswith("node") and
                                "claude" in _cmdline(pid)):
            found = True
            break
        if ppid <= 1:
            break
        pid = ppid
    if not found:
        return None
    comm, ppid, created = _proc_nix(pid)
    for _ in range(6):
        up = _proc_nix(ppid) if ppid > 1 else None
        if up is None or up[0].lstrip("-") not in _SHELLS_NIX:
            break
        if up[0].lower() in _NEVER:
            break
        pid, comm, created, ppid = ppid, up[0], up[2], up[1]
    return {"pid": pid, "image": comm, "created": created}


def arm_pid_record() -> str:
    """Record the process CLEAR must end, so it can confirm instead of search.

    Until now only app-spawned sessions had a .pid, because the hub's boot
    script wrote one. Anything opened by hand refused to close with "no
    process was recorded" -- confirm-never-discover doing its job against a
    record nobody had written.

    Write-once: the boot script's record is authoritative and re-arming would
    rewrite a start time the reaper compares against. Returns "" when it did
    nothing, and NEVER raises -- a hook that breaks a session to enable a
    button is a worse bug than the button.
    """
    rec, tmp = INBOX / ".pid", None
    try:
        if rec.exists():
            return ""
        facts = _anchor()
        if not facts:
            return ""
        INBOX.mkdir(parents=True, exist_ok=True)
        tmp = INBOX / (".pid.tmp-%d" % os.getpid())
        tmp.write_text(json.dumps({"pid": int(facts["pid"]),
                                   "image": facts["image"],
                                   "created": facts["created"],
                                   "side": "win" if WIN else "wsl",
                                   "armed_by": "cx-relay-watch"}),
                       encoding="utf-8")
        for attempt in range(4):        # a replace can hit EPERM under AV
            try:
                os.replace(tmp, rec)
                return str(rec)
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.05)
        return ""
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return ""


if "--teardown" not in sys.argv:
    arm_pid_record()          # boot, idle, context: whichever runs first wins

if "--arm-only" in sys.argv:
    # the test entry point. Everything below either blocks for four hours or
    # prints a conduct block into someone's session, so the arm is exercised
    # here and the tests never start a watcher.
    sys.exit(0)

if not env_codename:
    # Registered by hand, after boot. This session gets the .pid record CLEAR
    # needs and nothing else: the conduct block, the tombstone and the watcher
    # are cx-session machinery a terminal opened by hand never asked for and
    # did not have a minute ago.
    sys.exit(0)

if "--context" in sys.argv:
    # SessionStart (sync) prints this as standing session context for cx
    # sessions: the terminal stays the primary, full response surface; the
    # relay ping is a concise mirror feeding the chat pipe window + say-back.
    side = "Windows" if WIN else "WSL"
    to_wsl = (f"wsl -e {cxpaths.base_bin('wsl')} relay ping" if WIN
              else "base relay ping")
    to_win = ("base relay ping" if WIN
              else f"{cxpaths.base_bin('win')} relay ping")
    print(f"""<cx-conduct>This session is relay-linked as '{codename}' (cx work terminal, {side} side).

PINGS FROM CHRIS (his voice/STT arrives as relay pings from 'chris'): treat each as a normal user message — give your FULL answer as your regular visible response in this session, AND mirror a concise version (a sentence or three) via `base relay ping --to chris --msg "..."` so his chat window and TTS get it. Never answer only in the ping; the terminal is the primary record.

PEER SESSIONS — the Windows and WSL relay stores are SEPARATE; route by codename: winterm0-9 live in the Windows store, wslterm0-9 in the WSL store. From THIS ({side}) session:
  ping a winterm*:  {to_win} --to <peer> --msg "..." --from {codename}
  ping a wslterm*:  {to_wsl} --to <peer> --msg "..." --from {codename}
(chris is registered on BOTH stores — plain `base relay ping --to chris` always works.)

HANDOFF PROTOCOL:
  As SENDER (Chris tells you to hand work to a peer): ping the peer with COMPLETE context — everything they need, files, goal, constraints. When the peer's ack ping arrives, ping chris: "<peer> acknowledged, working." Answer the peer's follow-up questions directly.
  As RECEIVER (a ping arrives from a peer, not chris): (1) ack the SENDER directly with a reply ping — that clears their alert; (2) ping chris: "received <task> from <sender>, working"; (3) do the work — if details are missing, ask the SENDER by ping, NEVER chris; (4) when done, ping chris the outcome and notify the sender.
  Escalate to chris ONLY when you and the peer are both stuck or the decision is genuinely his. He is orchestrating many sessions by voice — protect his attention.

STAR COMMANDS: a ping from chris whose text starts with *<name> (e.g. *handoff, *end, *fork, *base) is an operator COMMAND, not chat. Run `base commands show <name>`, then execute its canonical steps EXACTLY, mirroring progress/completion to chris per the ping rules. Words after the command are its arguments. If the name doesn't resolve (STT garble / misspeak: handof, hands off, dbug), run `base commands list` and pick the closest name — obvious matches just execute; ping chris to disambiguate ONLY if two candidates are genuinely plausible.</cx-conduct>""")
    sys.exit(0)

if "--teardown" in sys.argv:
    reason = ""
    try:
        reason = json.load(sys.stdin).get("reason", "")
    except Exception:
        pass
    if reason == "clear":
        sys.exit(0)  # same terminal continues as a fresh session
    # tombstone only: the chat window closes itself via its pid-watch (killing
    # it here would exit it nonzero and strand an error-paused window)
    try:
        DOWN.parent.mkdir(parents=True, exist_ok=True)
        DOWN.write_text("down", encoding="utf-8")
    except Exception:
        pass
    sys.exit(0)

BOUND, POLL = 14400, 2
try:
    import tomllib
    with CONFIG.open("rb") as f:
        BOUND = int(tomllib.load(f).get("settings", {})
                    .get("watcher_bound_seconds", BOUND))
except Exception:
    pass
if BOUND <= 0:
    sys.exit(0)

# one watcher per codename per side; the lock dies with the process
if WIN:
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, f"CxRelayWatch_{codename}")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
else:
    import fcntl
    _lock = open(f"/tmp/cx-relay-watch-{codename}.lock", "w")
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)

boot = "--boot" in sys.argv
try:
    DOWN.unlink()  # a stale tombstone from the last teardown must not kill us
except Exception:
    pass


def _parent_watch():
    """Return a callable that is True once the SESSION's anchor process is gone.

    Watches the _anchor() result (claude / the tab shell), NOT this hook's own
    parent: the hook is spawned from a throwaway shell that exits immediately,
    and watching it killed every waker machine-wide within seconds of arming
    (measured 2026-08-19 — zero live wakers across 12 sessions). Anchor
    identity is pinned by creation ticks so pid reuse cannot fake liveness.
    Falls back to the old ppid watch only when no anchor resolves.
    """
    a = _anchor()
    if not WIN:
        if a:
            apid, acreated = a["pid"], a["created"]

            def gone():
                row = _proc_nix(apid)
                return row is None or row[2] != acreated
            return gone
        ppid = os.getppid()
        return lambda: os.getppid() != ppid  # reparented = parent died
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    if a:
        h = k.OpenProcess(0x00100000, False, a["pid"])  # SYNCHRONIZE
        if h:
            # handle pins the process object, so pid reuse cannot fake this
            return lambda: k.WaitForSingleObject(h, 0) == 0
    snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    ppid = None
    try:
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        me = os.getpid()
        ok = k.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.th32ProcessID == me:
                ppid = e.th32ParentProcessID
                break
            ok = k.Process32Next(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    if not ppid:
        return lambda: False
    h = k.OpenProcess(0x00100000, False, ppid)  # SYNCHRONIZE
    if not h:
        return lambda: False
    return lambda: k.WaitForSingleObject(h, 0) == 0  # WAIT_OBJECT_0 = dead


def pending():
    try:
        return sorted(INBOX.glob("*.json"))
    except Exception:
        return []


def wake(files):
    lines = [f"Relay ping arrived while '{codename}' was idle:"]
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            lines.append(f"  {d.get('from', '?')}: "
                         f"{d.get('summary') or d.get('doc') or '(empty)'}")
        except Exception:
            lines.append(f"  (unreadable: {p.name})")
    lines.append('Handle it now per your cx-conduct: if the sender is chris, '
                 'answer fully in-session and mirror concisely with base relay '
                 'ping --to chris --msg "<short reply>". If the sender is a '
                 'PEER session, ack the sender directly (reply ping clears '
                 'their alert), tell chris you received it, and work the task '
                 '- ask the peer, not chris, for missing details. The watcher '
                 're-arms when you end your turn.')
    print("\n".join(lines))
    sys.exit(2)


parent_gone = _parent_watch()
start = pending()
if start and boot:
    wake(start)  # pings that spooled while the session was down
seen = {p.name for p in start}  # idle re-arm: never re-fire lingering files

deadline = time.time() + BOUND
while time.time() < deadline:
    time.sleep(POLL)
    if DOWN.exists():  # session ended cleanly (SessionEnd teardown)
        try:
            DOWN.unlink()
        except Exception:
            pass
        sys.exit(0)
    if parent_gone():  # terminal force-closed / claude killed
        sys.exit(0)
    new = [p for p in pending() if p.name not in seen]
    if new:
        wake(new)
sys.exit(0)
