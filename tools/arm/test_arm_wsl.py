"""Exercise the CLEAR self-arm inside WSL. Runs with /usr/bin/python3.

Same constraint as the Windows suite: the script under test cannot be
imported, so every case drives the real file as a subprocess with --arm-only.
The WSL half has its own anchor code (/proc, tick counts) and its own store,
and /mnt/c is dead on this box, so everything stays in the Linux filesystem
and the Windows half of the run reads the records back over the UNC path --
which is the same route ping_hub.reap takes to confirm a wsl record.

    python3 test_arm_wsl.py setup   <patched script>   # arms, prints JSON
    python3 test_arm_wsl.py cleanup                    # kills the stand-ins
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / ".cache" / "cxarm-test"
FAKE = ROOT / "claude"
FAILURES = []
RESULT = {"root": str(ROOT), "cases": {}, "checks": []}


def check(name, cond, detail=""):
    RESULT["checks"].append({"name": name, "ok": bool(cond), "detail": str(detail)[:200]})
    if not cond:
        FAILURES.append(name)


def run_arm(store, env_extra, argv=("--arm-only",)):
    env = dict(os.environ)
    env.pop("BASE_RELAY_AS", None)
    env.pop("CLAUDE_PID", None)
    env["CX_RELAY_ROOT"] = str(store)
    env.update(env_extra)
    t0 = time.time()
    p = subprocess.run(["/usr/bin/python3", sys.argv[2]] + list(argv), env=env,
                       timeout=40, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    return p.returncode, time.time() - t0, p.stdout, p.stderr


def fake_claude(under_shell):
    """A live process whose /proc comm really is 'claude'.

    comm comes from the executable's name, so a copy of sleep called `claude`
    is the honest stand-in. under_shell puts a bash between it and the runner
    -- the arrangement the anchor walk has to climb, and the one a terminal
    Chris opened by hand actually has.
    """
    if under_shell:
        # `; :` is load-bearing. bash -c with ONE simple command execs it in
        # place instead of forking, so the shell becomes the stand-in and the
        # climb this case exists to test never happens -- it passed green that
        # way, with bash and claude reported as the same pid.
        p = subprocess.Popen(["/bin/bash", "-c", "%s 300; :" % FAKE],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        p = subprocess.Popen([str(FAKE), "300"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    if not under_shell:
        return p.pid, p.pid
    kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True,
                          text=True).stdout.split()
    return (int(kids[0]) if kids else p.pid), p.pid


def read(store, title):
    try:
        return json.loads((Path(store) / "relay-inbox" / title / ".pid")
                          .read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def comm(pid):
    try:
        return Path("/proc/%d/comm" % pid).read_text().strip()
    except OSError:
        return ""


def setup():
    ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copy2("/bin/sleep", FAKE)

    # --- 1. arms on the claude process itself when no shell is above it ---
    s1 = ROOT / "s1"
    cpid, _ = fake_claude(under_shell=False)
    rc, secs, out, err = run_arm(s1, {"BASE_RELAY_AS": "armtest-wsl-1",
                                      "CLAUDE_PID": str(cpid)})
    r1 = read(s1, "armtest-wsl-1")
    check("w1a exits 0", rc == 0, err[:200])
    check("w1b never starts the watcher (%.1fs)" % secs, secs < 20)
    check("w1c wrote a record", r1 is not None, r1)
    if r1:
        check("w1d side=wsl", r1.get("side") == "wsl", r1.get("side"))
        check("w1e anchored on claude", r1.get("pid") == cpid,
              "%s vs %s" % (r1.get("pid"), cpid))
        check("w1f created is a raw tick count, not a timestamp",
              str(r1.get("created", "")).isdigit(), r1.get("created"))
        check("w1g created matches /proc field 22",
              str(r1.get("created")) ==
              Path("/proc/%d/stat" % cpid).read_text().rpartition(")")[2].split()[19])
    RESULT["cases"]["armtest-wsl-1"] = {"store": str(s1), "claude": cpid}

    # --- 2. climbs to the login shell when there is one -------------------
    s2 = ROOT / "s2"
    cpid2, shell2 = fake_claude(under_shell=True)
    rc, secs, out, err = run_arm(s2, {"BASE_RELAY_AS": "armtest-wsl-2",
                                      "CLAUDE_PID": str(cpid2)})
    r2 = read(s2, "armtest-wsl-2")
    check("w2a armed under a shell parent", r2 is not None, err[:200])
    if r2:
        check("w2b climbed to the shell, not claude", r2.get("pid") == shell2,
              "recorded %s (bash=%s claude=%s)" % (r2.get("pid"), shell2, cpid2))
        check("w2c image is the shell comm", r2.get("image") == comm(shell2),
              r2.get("image"))
    RESULT["cases"]["armtest-wsl-2"] = {"store": str(s2), "claude": cpid2,
                                        "shell": shell2}

    # --- 3. write-once ----------------------------------------------------
    s3 = ROOT / "s3"
    inbox = s3 / "relay-inbox" / "armtest-wsl-3"
    inbox.mkdir(parents=True, exist_ok=True)
    sentinel = {"pid": 4242, "image": "claude", "created": "999999", "side": "wsl"}
    (inbox / ".pid").write_text(json.dumps(sentinel))
    run_arm(s3, {"BASE_RELAY_AS": "armtest-wsl-3", "CLAUDE_PID": str(cpid)})
    check("w3a boot script's record is left alone",
          read(s3, "armtest-wsl-3") == sentinel, read(s3, "armtest-wsl-3"))

    # --- 4. no title -> nothing written -----------------------------------
    s4 = ROOT / "s4"
    s4.mkdir(parents=True, exist_ok=True)
    (s4 / "sessions.json").write_text('{"sessions":{}}')
    rc, secs, out, err = run_arm(s4, {"CLAUDE_CODE_SESSION_ID": "no-such-id",
                                      "CLAUDE_PID": str(cpid)})
    check("w4a exits 0 with no codename", rc == 0, err[:200])
    check("w4b wrote nothing", not (s4 / "relay-inbox").exists())

    # --- 5. registry title, no BASE_RELAY_AS (the whole point) ------------
    s5 = ROOT / "s5"
    s5.mkdir(parents=True, exist_ok=True)
    (s5 / "sessions.json").write_text(json.dumps({"sessions": {
        "hand-opened-wsl": {"title": "hand-opened-wsl", "session_id": "SID-7"}}}))
    rc, secs, out, err = run_arm(s5, {"CLAUDE_CODE_SESSION_ID": "SID-7",
                                      "CLAUDE_PID": str(cpid2)})
    r5 = read(s5, "hand-opened-wsl")
    check("w5a armed a session that never had BASE_RELAY_AS", r5 is not None,
          err[:200])
    RESULT["cases"]["hand-opened-wsl"] = {"store": str(s5), "claude": cpid2,
                                          "shell": shell2}

    # --- 6. teardown must not arm ----------------------------------------
    s6 = ROOT / "s6"
    run_arm(s6, {"BASE_RELAY_AS": "armtest-wsl-6", "CLAUDE_PID": str(cpid)},
            argv=("--teardown",))
    check("w6a SessionEnd writes no record", read(s6, "armtest-wsl-6") is None)

    # --- 7. a hand-opened session gets no conduct block -------------------
    s7 = ROOT / "s7"
    s7.mkdir(parents=True, exist_ok=True)
    (s7 / "sessions.json").write_text(json.dumps({"sessions": {
        "hand-opened-wsl2": {"title": "hand-opened-wsl2", "session_id": "SID-8"}}}))
    rc, secs, out, err = run_arm(s7, {"CLAUDE_CODE_SESSION_ID": "SID-8",
                                      "CLAUDE_PID": str(cpid)},
                                 argv=("--context",))
    check("w7a no conduct block printed", "cx-conduct" not in out, out[:120])
    check("w7b armed on the way past", read(s7, "hand-opened-wsl2") is not None)

    RESULT["failures"] = FAILURES
    print(json.dumps(RESULT))
    return 1 if FAILURES else 0


def cleanup():
    subprocess.run(["pkill", "-f", str(FAKE)], capture_output=True)
    subprocess.run(["pkill", "-f", "%s 300" % FAKE], capture_output=True)
    shutil.rmtree(ROOT, ignore_errors=True)
    print("cleaned")
    return 0


if __name__ == "__main__":
    sys.exit(setup() if sys.argv[1] == "setup" else cleanup())
