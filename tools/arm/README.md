# The CLEAR self-arm

`ping_hub.reap` never discovers a pid — it only confirms one a session
recorded about itself. Until now the only writer of that record was the hub's
own spawn path, so CLEAR refused on every session Chris opened by hand:
"no process was recorded".

These four files put the record-writing in the per-session hook that already
runs on both sides, `cx-relay-watch.py`. That hook is **not part of this
repo** — it is deployed twice, outside version control:

    C:\Users\Chris\Tools\stt\cx-relay-watch.py       (Windows sessions)
    /home/chriskahler/.local/bin/cx-relay-watch.py   (WSL sessions)

so the change lives here as a patcher rather than as a copy of a file this
repo does not own. `Tools/stt` is not a git repository; this directory is the
version-controlled home of the change and its evidence.

| File | What it is |
|---|---|
| `apply_arm.py` | applies the arm to one copy of `cx-relay-watch.py`. Idempotent, refuses unless each anchor matches exactly once, writes a `.py.BAK-pre-arm` beside the target |
| `test_arm_win.py` | 24 checks on the Windows anchor. Run with the hub's venv python so the REAL `ping_hub.reap.confirm` judges the record the arm wrote |
| `test_arm_wsl.py` | 17 checks on the WSL anchor, run inside WSL with `python3` |
| `check_wsl_records.py` | 6 checks: the real `reap.confirm`, from Windows, over the UNC path, against the records the WSL suite wrote |

## Running it

```
# apply (both copies)
python tools/arm/apply_arm.py "C:/Users/Chris/Tools/stt/cx-relay-watch.py"
python tools/arm/apply_arm.py "W:/home/chriskahler/.local/bin/cx-relay-watch.py"

# Windows suite
C:/Users/Chris/.ping-hub/venv/Scripts/python.exe tools/arm/test_arm_win.py \
    "C:/Users/Chris/Tools/stt/cx-relay-watch.py"

# WSL suite: copy it in first (/mnt/c is dead on this box), then
wsl.exe --cd / -e /usr/bin/python3 <linux path>/test_arm_wsl.py setup \
    /home/chriskahler/.local/bin/cx-relay-watch.py > wsl-result.json
C:/Users/Chris/.ping-hub/venv/Scripts/python.exe tools/arm/check_wsl_records.py wsl-result.json
wsl.exe --cd / -e /usr/bin/python3 <linux path>/test_arm_wsl.py cleanup
```

## Two things the suites exist to stop

**The hook's parent is not the session's shell.** An earlier attempt anchored
on `os.getppid()` and recorded `bash.exe 49472` — a throwaway shell that was
already gone when the pid was checked. A record pointing at a dead pid is
worse than no record: pid reuse is the exact failure `confirm()` exists to
prevent. The arm resolves claude first, then climbs through shells only.

**`created` means a different thing on each side.** Windows records an ISO
timestamp and `reap` compares it through `_norm`; WSL records `/proc` field 22,
a raw tick count that must be compared as-is. Feeding a tick count to a
timestamp normaliser truncates it to 19 digits and compares equal to a
different instant.

These suites are deliberately not part of `pytest tests/`: they spawn
processes and cross into WSL, and that suite is hermetic by rule.
