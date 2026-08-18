"""Confirm the WSL-armed records from the Windows side, the way CLEAR does.

A wsl session's record lives in the WSL home, and the hub reaches it over the
UNC path -- reading only the Windows root is the bug that made closing a wsl
session report "no process was recorded" while the record sat on disk. So this
runs the REAL ping_hub.reap.confirm against the records the WSL suite just
wrote, with the stand-in processes still alive, and then against a tick count
nudged by 7 to prove the refusal still refuses.
"""
import json
import sys
from pathlib import Path

from ping_hub import reap

UNC = "//wsl.localhost/Ubuntu"
res = json.load(open(sys.argv[1], encoding="utf-8"))
fails = []
for title, case in res["cases"].items():
    root = Path(UNC + case["store"]) / "relay-inbox"
    rec = reap.read_record(root, title)
    if rec is None:
        print("  FAIL  x0 record unreadable over UNC for %s (%s)" % (title, root))
        fails.append(title + "/unreadable")
        continue
    ok, why = reap.confirm(rec)
    print("  %s  x1 cross-side confirm  %-18s pid=%s image=%s -> %s"
          % ("PASS" if ok else "FAIL", title, rec["pid"], rec["image"], why))
    if not ok:
        fails.append(title)
    bad = dict(rec, created=str(int(rec["created"]) + 7))
    ok2, why2 = reap.confirm(bad)
    print("  %s  x2 refuses a nudged tick count for %-14s -> %s"
          % ("PASS" if not ok2 else "FAIL", title, why2))
    if ok2:
        fails.append(title + "/mismatch")
print("\ncross-side failures: %s" % (fails or "none"))
sys.exit(1 if fails else 0)
