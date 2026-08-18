"""Exercise the CLEAR self-arm on Windows without ever starting a watcher.

The script under test cannot be imported: its module body IS the watcher, so
`import cx_relay_watch` blocks for four hours. That is what stopped the last
attempt from testing this at all. So every case here runs the real file as a
subprocess with --arm-only, against a throwaway store (CX_RELAY_ROOT), and
fails the whole run if any invocation takes longer than a few seconds.

Run with the hub's venv python so ping_hub.reap is importable -- the point of
the arm is that the record it writes passes the SAME confirm() the CLEAR
button runs, not one this file re-implements.

    <hub venv>/Scripts/python.exe test_arm_win.py <cx-relay-watch.py>
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ping_hub import reap

SCRIPT = Path(sys.argv[1] if len(sys.argv) > 1 else "").resolve()
PY = sys.executable
NO_WINDOW = 0x08000000
FAILURES = []
CHILDREN = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" — " + str(detail) if detail else ""))
    if not cond:
        FAILURES.append(name)


def run_arm(store, env_extra, argv=("--arm-only",), budget=40):
    """The script under test, once. Returns (rc, seconds)."""
    env = dict(os.environ)
    env.pop("BASE_RELAY_AS", None)
    env.pop("CLAUDE_PID", None)
    env["CX_RELAY_ROOT"] = str(store)
    env.update(env_extra)
    t0 = time.time()
    p = subprocess.run([PY, str(SCRIPT)] + list(argv), env=env, timeout=budget,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.returncode, time.time() - t0, p.stdout, p.stderr


def fake_claude(dirp, wrapper=None):
    """A live process actually named claude.exe, optionally under a shell.

    The anchor walk looks for an image called claude*, so a stand-in has to
    carry that name on disk. cmd.exe copied under the name is the cheapest
    honest one: it needs nothing but system DLLs, and `ping -n` keeps it alive
    without a window. `wrapper` puts a real shell between the test runner and
    it -- the arrangement the walk is supposed to climb.
    """
    exe = dirp / "claude.exe"
    if not exe.exists():
        shutil.copy2(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe", exe)
    idle = ["/c", "ping", "-n", "200", "127.0.0.1"]
    cmd = (["cmd.exe", "/c", str(exe)] + idle) if wrapper == "cmd" else \
          ([str(exe)] + idle)
    p = subprocess.Popen(cmd, creationflags=NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    CHILDREN.append(p)
    time.sleep(1.5)          # let the shell get its child up before we walk
    return p


def claude_pid_under(p):
    """The claude.exe pid in p's tree -- p itself, or its child under a shell."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter "
                          f"\"ParentProcessId={p.pid} AND Name='claude.exe'\" | "
                          "Select-Object -Expand ProcessId"],
                         capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout.split()
    return int(out[0]) if out else p.pid


def ancestors(pid, depth=10):
    seen, cur = [], pid
    for _ in range(depth):
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "$o=Get-CimInstance Win32_Process -Filter "
                              f"\"ProcessId={cur}\"; if($o){{$o.ParentProcessId}}"],
                             capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout.strip()
        if not out.isdigit():
            break
        cur = int(out)
        seen.append(cur)
    return seen


def record(store, title):
    return reap.read_record(Path(store) / "relay-inbox", title)


def main():
    if not SCRIPT.is_file():
        print("usage: test_arm_win.py <path to cx-relay-watch.py>")
        return 2
    print("under test: %s" % SCRIPT)
    tmp = Path(tempfile.mkdtemp(prefix="cxarm-"))
    try:
        # --- 1. arms, fast, and the record CONFIRMS ------------------------
        store = tmp / "s1"
        c = fake_claude(tmp)
        cpid = claude_pid_under(c)
        rc, secs, out, err = run_arm(store, {"BASE_RELAY_AS": "armtest-1",
                                             "CLAUDE_PID": str(cpid)})
        check("1a exits 0", rc == 0, err.strip()[:200])
        check("1b never starts the watcher (returned in %.1fs)" % secs, secs < 20)
        rec = record(store, "armtest-1")
        check("1c wrote a record", rec is not None, rec)
        if rec:
            check("1d has reap's shape",
                  set(("pid", "image", "created", "side")) <= set(rec), rec)
            check("1e side=win", rec.get("side") == "win", rec.get("side"))
            ok, why = reap.confirm(rec)
            check("1f reap.confirm PASSES the armed record", ok, why)
            check("1g anchor is claude or one of its ancestors",
                  rec["pid"] == cpid or rec["pid"] in ancestors(cpid),
                  "pid=%s claude=%s" % (rec.get("pid"), cpid))
            check("1h anchor is never a NEVER_KILL image",
                  str(rec.get("image", "")).lower() not in reap.NEVER_KILL,
                  rec.get("image"))

        # --- 2. climbs through a real shell -------------------------------
        store = tmp / "s2"
        c2 = fake_claude(tmp, wrapper="cmd")
        cpid2 = claude_pid_under(c2)
        rc, secs, out, err = run_arm(store, {"BASE_RELAY_AS": "armtest-2",
                                             "CLAUDE_PID": str(cpid2)})
        rec2 = record(store, "armtest-2")
        check("2a armed under a shell parent", rec2 is not None, err.strip()[:200])
        if rec2:
            check("2b climbed to the shell, not claude",
                  rec2["pid"] == c2.pid,
                  "recorded %s (cmd=%s claude=%s)" % (rec2["pid"], c2.pid, cpid2))
            check("2c image is the shell", "cmd" in str(rec2.get("image", "")).lower(),
                  rec2.get("image"))
            ok, why = reap.confirm(rec2)
            check("2d confirm passes on the shell anchor", ok, why)

        # --- 3. write-once ------------------------------------------------
        store = tmp / "s3"
        inbox = store / "relay-inbox" / "armtest-3"
        inbox.mkdir(parents=True)
        sentinel = {"pid": 4242, "image": "boot-script.exe",
                    "created": "AUTHORITATIVE", "side": "win"}
        (inbox / ".pid").write_text(json.dumps(sentinel), encoding="utf-8")
        run_arm(store, {"BASE_RELAY_AS": "armtest-3", "CLAUDE_PID": str(cpid)})
        check("3a boot script's record is left alone",
              record(store, "armtest-3") == sentinel, record(store, "armtest-3"))

        # --- 4. no title -> no record, no refusal weakened -----------------
        store = tmp / "s4"
        store.mkdir(parents=True)
        (store / "sessions.json").write_text('{"sessions":{}}', encoding="utf-8")
        rc, secs, out, err = run_arm(store, {"CLAUDE_CODE_SESSION_ID": "no-such-id",
                                             "CLAUDE_PID": str(cpid)})
        check("4a exits 0 with no codename", rc == 0, err.strip()[:200])
        check("4b wrote nothing at all",
              not list((store / "relay-inbox").glob("*")) if
              (store / "relay-inbox").exists() else True)

        # --- 5. the unlock: registry title, no BASE_RELAY_AS ---------------
        store = tmp / "s5"
        store.mkdir(parents=True)
        (store / "sessions.json").write_text(json.dumps({"sessions": {
            "hand-opened": {"title": "hand-opened", "session_id": "SID-42"},
            "other": {"title": "other", "session_id": "SID-99"}}}),
            encoding="utf-8")
        rc, secs, out, err = run_arm(store, {"CLAUDE_CODE_SESSION_ID": "SID-42",
                                             "CLAUDE_PID": str(cpid)})
        rec5 = record(store, "hand-opened")
        check("5a armed a session that never had BASE_RELAY_AS",
              rec5 is not None, err.strip()[:200])
        if rec5:
            ok, why = reap.confirm(rec5)
            check("5b its record confirms", ok, why)
        check("5c did not arm the other session",
              record(store, "other") is None)

        # --- 6. a mismatched record still REFUSES -------------------------
        if rec5:
            bad = dict(rec5, created="1999-01-01T00:00:00.0000000-05:00")
            ok, why = reap.confirm(bad)
            check("6a confirm refuses a reused pid", not ok, why)
            bad2 = dict(rec5, image="notclaude.exe")
            ok2, why2 = reap.confirm(bad2)
            check("6b confirm refuses a renamed image", not ok2, why2)

        # --- 7. teardown must not arm -------------------------------------
        store = tmp / "s7"
        rc, secs, out, err = run_arm(store, {"BASE_RELAY_AS": "armtest-7",
                                             "CLAUDE_PID": str(cpid)},
                                     argv=("--teardown",))
        check("7a SessionEnd writes no record for a dying session",
              record(store, "armtest-7") is None)

        # --- 8. non-cx sessions still get no cx machinery ------------------
        store = tmp / "s8"
        store.mkdir(parents=True)
        (store / "sessions.json").write_text(json.dumps({"sessions": {
            "hand-opened": {"title": "hand-opened", "session_id": "SID-42"}}}),
            encoding="utf-8")
        rc, secs, out, err = run_arm(store, {"CLAUDE_CODE_SESSION_ID": "SID-42",
                                             "CLAUDE_PID": str(cpid)},
                                     argv=("--context",))
        check("8a no conduct block printed into a hand-opened session",
              "cx-conduct" not in out, out[:120])
        check("8b but it armed on the way past",
              record(store, "hand-opened") is not None)

        # --- 9. NEVER_KILL lists have not drifted apart --------------------
        src = SCRIPT.read_text(encoding="utf-8")
        block = src.split("_NEVER = (", 1)[-1].split(")", 1)[0]
        mine = {s.strip().strip('"\'') for s in block.split(",") if s.strip()}
        check("9a script's NEVER list covers reap.NEVER_KILL",
              reap.NEVER_KILL <= mine, sorted(reap.NEVER_KILL - mine))
    finally:
        for p in CHILDREN:
            try:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True, creationflags=NO_WINDOW)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks failed%s" % (len(FAILURES),
                                    (": " + ", ".join(FAILURES)) if FAILURES else ""))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
