"""Apply the CLEAR self-arm to a cx-relay-watch.py copy.

Two copies of that script run on this machine (Windows: Tools/stt, WSL:
~/.local/bin) and they differ only in one paragraph of prose, so the arm is
applied by exact-string replacement rather than by copying a whole file over
either of them. Idempotent: a copy that already carries the arm is left alone.

    python apply_arm.py <path> [--check]
"""
import sys
from pathlib import Path

OLD_DOC = '''The poll loop also exits when its parent process dies (terminal force-closed)
and after watcher_bound_seconds (cx.toml, default 4h) as a final backstop.
Exits 0 instantly when BASE_RELAY_AS is unset (non-cx sessions unaffected).
'''

NEW_DOC = '''The poll loop also exits when its parent process dies (terminal force-closed)
and after watcher_bound_seconds (cx.toml, default 4h) as a final backstop.

Before any of that it arms the session's .pid record (see arm_pid_record) --
the one thing it does for sessions Chris opened by hand. Those sessions get
the record and nothing else: no conduct block, no tombstone, no watcher. A
session with no BASE_RELAY_AS and no registry entry still exits 0 instantly.
'''

OLD_HEAD = '''codename = os.environ.get("BASE_RELAY_AS", "").strip()
if not codename:
    sys.exit(0)

WIN = os.name == "nt"
if WIN:
    INBOX = Path(r"C:\\Users\\Chris\\.base-gbl\\.base\\relay-inbox") / codename
    CONFIG = Path(r"C:\\Users\\Chris\\.base-gbl\\cx.toml")
    DOWN = Path(r"C:\\Users\\Chris\\.base-gbl\\cx\\down") / codename
else:
    INBOX = Path.home() / ".base-gbl/.base/relay-inbox" / codename
    CONFIG = Path("/mnt/c/Users/Chris/.base-gbl/cx.toml")
    DOWN = Path("/mnt/c/Users/Chris/.base-gbl/cx/down") / codename

'''

NEW_HEAD = '''WIN = os.name == "nt"
# CX_RELAY_ROOT exists so the arm below can be exercised against a throwaway
# store. This module cannot be imported to test it -- importing runs the poll
# loop and hangs -- so its tests drive the script as a subprocess with
# --arm-only, and they need somewhere harmless to write. Unset in production.
_root = os.environ.get("CX_RELAY_ROOT", "").strip()
STORE = (Path(_root) if _root else
         Path(r"C:\\Users\\Chris\\.base-gbl\\.base") if WIN else
         Path.home() / ".base-gbl/.base")


def _registered_title() -> str:
    """This session's relay title from the registry, when the environment has
    no codename to give.

    A session Chris opened by hand has none at boot: it acquires one later,
    when it runs `base relay register --as X`. Reading the registry is what
    lets those sessions arm at all, and they are exactly the ones CLEAR could
    not close.
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


env_codename = os.environ.get("BASE_RELAY_AS", "").strip()
codename = env_codename or _registered_title()
if not codename:
    sys.exit(0)

INBOX = STORE / "relay-inbox" / codename
if WIN:
    CONFIG = Path(r"C:\\Users\\Chris\\.base-gbl\\cx.toml")
    DOWN = Path(r"C:\\Users\\Chris\\.base-gbl\\cx\\down") / codename
else:
    CONFIG = Path("/mnt/c/Users/Chris/.base-gbl/cx.toml")
    DOWN = Path("/mnt/c/Users/Chris/.base-gbl/cx/down") / codename

# shells a session can be sitting in; the anchor walk climbs through these and
# stops at anything else, so it can never reach past the tab into a service
_SHELLS_WIN = ("powershell.exe", "pwsh.exe", "cmd.exe", "bash.exe", "sh.exe",
               "zsh.exe", "fish.exe", "nu.exe")
_SHELLS_NIX = ("bash", "-bash", "sh", "-sh", "zsh", "-zsh", "dash", "fish")
# ping_hub.reap.NEVER_KILL, kept in sync by hand: WindowsTerminal.exe is ONE
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
    throwaway shell: an earlier attempt anchored there and recorded bash.exe
    49472, which was already gone minutes later. A record pointing at a dead
    pid is worse than no record -- pid reuse is the exact thing confirm()
    exists to stop.

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


def _cmdline(pid: int) -> str:
    try:
        return Path("/proc/%d/cmdline" % pid).read_text().replace("\\0", " ")
    except OSError:
        return ""


def arm_pid_record() -> str:
    """Record the process CLEAR must end, so it can confirm instead of search.

    Until now only app-spawned sessions had a .pid, because the hub's boot
    script wrote one. Anything Chris opened by hand refused to close with "no
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
    # are cx-session machinery a terminal Chris opened himself never asked for
    # and did not have a minute ago.
    sys.exit(0)

'''


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = Path(sys.argv[1])
    check = "--check" in sys.argv
    src = target.read_text(encoding="utf-8")
    if "def arm_pid_record" in src:
        print("already armed: %s" % target)
        return 0
    for name, old in (("docstring", OLD_DOC), ("head", OLD_HEAD)):
        if src.count(old) != 1:
            print("REFUSING %s: %s anchor matched %d times, not 1"
                  % (target, name, src.count(old)))
            return 1
    out = src.replace(OLD_DOC, NEW_DOC).replace(OLD_HEAD, NEW_HEAD)
    if check:
        print("would patch %s (%d -> %d bytes)" % (target, len(src), len(out)))
        return 0
    target.with_suffix(".py.BAK-pre-arm").write_text(src, encoding="utf-8",
                                                     newline="")
    target.write_text(out, encoding="utf-8", newline="")
    print("armed %s (backup: %s)" % (target, target.name + ".BAK-pre-arm"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
