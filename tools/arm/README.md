# The CLEAR self-arm

`ping_hub.reap` never discovers a pid — it only confirms one a session
recorded about itself. Until now the only writer of that record was the hub's
own spawn path, so CLEAR refused on every session opened by hand:
"no process was recorded".

These files put the record-writing into the per-session hook that already runs
on both sides, `cx-relay-watch.py`. That hook is **not part of this repo** — it
is deployed twice, outside version control:

    <windows home>\Tools\stt\cx-relay-watch.py       (Windows sessions)
    <linux home>/.local/bin/cx-relay-watch.py        (WSL sessions)

so the change lives here as a patcher rather than as a copy of a file this repo
does not own. Neither deployment directory is a git repository; this is the
version-controlled home of the change and its evidence.

| File | What it is |
|---|---|
| `apply_arm.py` | applies the arm to one copy of `cx-relay-watch.py`. Idempotent, refuses unless each anchor matches exactly once, writes a `.py.BAK-pre-arm` beside the target |
| `test_arm_win.py` | 24 checks on the Windows anchor. Run it with the hub's venv python so the REAL `ping_hub.reap.confirm` judges the record the arm wrote |
| `test_arm_wsl.py` | 17 checks on the WSL anchor, run inside WSL with `python3` |
| `check_wsl_records.py` | 6 checks: the real `reap.confirm`, from Windows, over the UNC path, against the records the WSL suite wrote |

## Running it

```
python tools/arm/apply_arm.py <path to a cx-relay-watch.py>   # --check to dry-run

<hub venv>/Scripts/python.exe tools/arm/test_arm_win.py <windows copy>

# WSL: copy the suite into the Linux filesystem first — /mnt/c is not
# guaranteed to be mounted — then
wsl.exe --cd / -e /usr/bin/python3 <linux path>/test_arm_wsl.py setup \
    <linux copy> > wsl-result.json
<hub venv>/Scripts/python.exe tools/arm/check_wsl_records.py wsl-result.json
wsl.exe --cd / -e /usr/bin/python3 <linux path>/test_arm_wsl.py cleanup
```

`apply_arm.py` names no path of its own: it derives the store from the target's
own `INBOX`, so a copy whose paths differ, or move, is patched correctly.

## Three things these guard

**The hook's parent is not the session's shell.** An earlier attempt anchored
on `os.getppid()` and recorded a `bash.exe` that was already gone when the pid
was next checked. A record pointing at a dead pid is worse than no record: pid
reuse is the exact failure `confirm()` exists to prevent. The arm resolves
claude first, then climbs through shells only.

**`created` means a different thing on each side.** Windows records an ISO
timestamp, compared through `reap._norm`; WSL records `/proc` field 22, a raw
tick count compared as-is. Feeding a tick count to a timestamp normaliser
truncates it to 19 digits, where it compares equal to a different instant.

**A shell with one command execs in place.** The WSL climb case first ran with
`bash -c "<one command>"`, which does not fork — so bash and the stand-in were
the same pid and the test passed green without climbing anything. The fixture
ends `; :` for that reason.

These suites are deliberately not part of `pytest tests/`: they spawn processes
and cross into WSL, and that suite is hermetic by rule.
