"""End a session's terminal, without ever ending the wrong one.

Ten `claude.exe` processes run on this machine with byte-identical command
lines. Nothing in the process table distinguishes one session from another, so
any reaper that SEARCHES for its target is choosing between ten indistinguish-
able candidates and will eventually pick wrong. Killing the wrong terminal
destroys work that was not being cleared.

So this module never discovers a pid. It only confirms one:

1. A session records its OWN pid at boot, into its relay inbox beside
   `.status`. Written by the process about itself; nothing is inferred.
2. Before killing, that pid must still be the SAME process — image name AND
   creation time both. Pid reuse is precisely how a reaper kills an innocent,
   and creation time is what turns "unlikely" into "impossible".
3. Anything unconfirmed REFUSES, with a reason. There is no fallback search.

A session the hub did not spawn has no record, so it cannot be cleared until it
is next booted from the app. That is a real limit and it is the right one: a
button that works on half the cards and never lies beats one that guesses.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ping_hub import proc

# Killing this would close EVERY tab, not one: Windows Terminal is a single
# process shared by all of them (measured — one instance, days old, parent to
# every session's shell). It is never a legitimate target.
NEVER_KILL = {"windowsterminal.exe", "explorer.exe", "svchost.exe",
              "csrss.exe", "wininit.exe", "services.exe", "system"}


def record_path(inbox_root: Path, title: str) -> Path:
    return inbox_root / title / ".pid"


def write_record(inbox_root: Path, title: str, pid: int, image: str,
                 created: str, side: str = "win") -> Path:
    p = record_path(inbox_root, title)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": int(pid), "image": image,
                             "created": created, "side": side,
                             "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}),
                 encoding="utf-8")
    return p


def read_record(inbox_root: Path, title: str) -> dict | None:
    # utf-8-SIG, not utf-8: the Windows boot script writes this with PowerShell
    # 5.1, whose `-Encoding utf8` prepends a BOM. Read as plain utf-8 the BOM
    # lands in the first token, json.loads raises, and the record reads as
    # ABSENT — so clear would refuse every session with "nothing was recorded"
    # while a perfectly good record sat on disk. Found by running the generated
    # script rather than by reading it.
    try:
        doc = json.loads(record_path(inbox_root, title).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("pid") else None


def process_facts(pid: int, query=None) -> dict | None:
    """{image, created} for a live pid, or None. Windows only; the query is
    injectable so the decision logic can be tested without real processes."""
    if query is not None:
        return query(pid)
    if os.name != "nt":
        return None
    r = proc.run(["powershell", "-NoProfile", "-Command",
                  f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}';"
                  f"if($p){{ @{{image=$p.Name;created=$p.CreationDate.ToString('o')}}"
                  f" | ConvertTo-Json -Compress }}"],
                 capture_output=True, text=True, timeout=20)
    try:
        doc = json.loads((r.stdout or "").strip() or "null")
    except ValueError:
        return None
    return doc if isinstance(doc, dict) and doc.get("image") else None


def confirm(record: dict, query=None) -> tuple[bool, str]:
    """Is the recorded pid still the very process that was recorded?"""
    if not record or not record.get("pid"):
        return False, ("no process was recorded for this session. Reboot it "
                       "from the app to enable clear.")
    facts = process_facts(int(record["pid"]), query=query)
    if facts is None:
        return False, f"process {record['pid']} is not running."
    image = str(facts.get("image", ""))
    if image.lower() in NEVER_KILL:
        # a record pointing here is corrupt or mis-recorded; the blast radius
        # is every terminal Chris has open
        return False, f"refusing: {image} is never a valid target."
    if image.lower() != str(record.get("image", "")).lower():
        return False, (f"pid {record['pid']} is now {image}, not "
                       f"{record.get('image')} — the pid was reused.")
    if _norm(facts.get("created")) != _norm(record.get("created")):
        return False, (f"pid {record['pid']} started at {facts.get('created')}, "
                       f"not {record.get('created')} — the pid was reused.")
    return True, image


def _norm(ts) -> str:
    """Compare instants, not spellings — the recorder and the query round-trip
    time through different formatters."""
    s = str(ts or "").strip().replace("Z", "+00:00")
    if len(s) >= 6 and s[-3] == ":" and s[-6] in "+-":
        s = s[:-3] + s[-2:]
    return s[:19]          # to the second; sub-second digits differ by source


def reap(inbox_root: Path, title: str, query=None, kill=None) -> tuple[bool, str]:
    """Confirm, then end the process tree. Refuses rather than guessing."""
    record = read_record(inbox_root, title)
    ok, why = confirm(record, query=query)
    if not ok:
        return False, why
    pid = int(record["pid"])
    if kill is not None:
        return kill(pid)
    r = proc.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                 capture_output=True, text=True, timeout=30)
    # taskkill's exit code does NOT mean what it looks like for a tree kill:
    # /T walks the descendants, and any child that exits on its own mid-walk
    # makes the whole call exit nonzero even though every process it was asked
    # to end is dead. Observed live on `wtprobe` — a 409 failure whose detail
    # was a wall of SUCCESS lines. So the outcome is judged by the only thing
    # that actually matters: is the ANCHOR pid gone.
    if process_facts(pid, query=query) is not None:
        out = (r.stdout + r.stderr).strip()[:200]
        return False, f"pid {pid} is still running" + (f": {out}" if out else "")
    try:
        record_path(inbox_root, title).unlink()
    except OSError:
        pass      # the process is gone; a stale record is the lesser problem
    return True, f"ended pid {pid} ({why}) and its children"
