"""CLEAR has to work on a session that survived a reboot or a resume.

`.pid` records were write-once, and nothing on the machine ever rewrote one.
So a session that came back as a NEW process under the SAME codename kept a
record naming the pid that died with the old one — permanently. `reap.confirm`
then answered correctly, CLEAR refused because it will never search for a
substitute, and the modal had Cancel as its only button. Measured 2026-08-19:
two of four live sessions were in that state, and Chris had been unable to
clear a card for three days.

The script under test cannot be imported — its module body starts a watcher —
so every case drives the real file as a subprocess with `--arm-only`, which is
the idiom `tools/arm/` already established. HOME is redirected, so nothing
here can see or touch a real session's record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "ping_hub" / "ptt" / "cx-relay-watch.py"
CODENAME = "armprobe"

pytestmark = pytest.mark.skipif(os.name == "nt",
                                reason="the /proc anchor path is the WSL half")


def _inbox(home: Path) -> Path:
    return home / ".base-gbl" / ".base" / "relay-inbox" / CODENAME


def _arm(home: Path):
    env = dict(os.environ, HOME=str(home), BASE_RELAY_AS=CODENAME)
    env.pop("CLAUDE_PID", None)
    return subprocess.run([sys.executable, str(SCRIPT), "--arm-only"],
                          capture_output=True, text=True, timeout=60, env=env)


def _healthy_record() -> dict:
    """A record describing THIS process truthfully — image and start ticks
    both, which is what makes it not stale."""
    pid = os.getpid()
    line = Path(f"/proc/{pid}/stat").read_text()
    head, _, rest = line.rpartition(")")
    return {"pid": pid, "image": head.partition("(")[2], "side": "wsl",
            "created": rest.split()[19], "armed_by": "test"}


def _write(home: Path, doc) -> Path:
    p = _inbox(home)
    p.mkdir(parents=True, exist_ok=True)
    rec = p / ".pid"
    rec.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return rec


# ── the disease ─────────────────────────────────────────────────────────────
def test_a_record_naming_a_dead_process_is_re_armed(tmp_path):
    """The survivor shape: same codename, new process, old pid on file."""
    rec = _write(tmp_path, {"pid": 2147483646, "image": "claude", "side": "wsl",
                            "created": "1", "armed_by": "cx-relay-watch"})
    _arm(tmp_path)
    after = json.loads(rec.read_text(encoding="utf-8"))
    assert after["pid"] != 2147483646, "the stale record was left in place"
    assert after["armed_by"] == "cx-relay-watch"


def test_a_record_whose_pid_was_reused_is_re_armed(tmp_path):
    """Right pid, wrong process — `confirm` calls this stale too, and the two
    must never disagree about the word."""
    doc = _healthy_record()
    doc["image"] = "something-else"
    rec = _write(tmp_path, doc)
    _arm(tmp_path)
    assert json.loads(rec.read_text(encoding="utf-8"))["image"] != "something-else"


def test_a_record_with_a_different_start_time_is_re_armed(tmp_path):
    doc = _healthy_record()
    doc["created"] = "999999999"
    rec = _write(tmp_path, doc)
    _arm(tmp_path)
    assert json.loads(rec.read_text(encoding="utf-8"))["created"] != "999999999"


# ── write-once, preserved exactly where it was right ────────────────────────
def test_a_live_record_is_left_byte_identical(tmp_path):
    """The original rule's reason still holds for a record that describes a
    running process: rewriting it would move a start time the reaper compares
    against."""
    rec = _write(tmp_path, _healthy_record())
    before = rec.read_bytes()
    _arm(tmp_path)
    assert rec.read_bytes() == before


def test_an_unreadable_record_is_not_treated_as_stale(tmp_path):
    """Rewriting a record we cannot reason about is how a reaper learns to
    guess."""
    rec = _write(tmp_path, "{not json at all")
    before = rec.read_bytes()
    _arm(tmp_path)
    assert rec.read_bytes() == before


def test_a_missing_record_still_arms_normally(tmp_path):
    """The behaviour this fork must not break: a session opened by hand gets
    the record CLEAR needs."""
    _arm(tmp_path)
    rec = _inbox(tmp_path) / ".pid"
    assert rec.exists()
    assert json.loads(rec.read_text(encoding="utf-8"))["pid"] > 0


# ── the blast radius ────────────────────────────────────────────────────────
def test_a_never_kill_image_is_refused_rather_than_recorded():
    """A mis-resolved anchor must never become a kill target: the blast radius
    of WindowsTerminal.exe is every tab Chris has open."""
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("def arm_pid_record"):]
    assert 'facts.get("image", "")).lower() in _NEVER' in body


def test_stale_means_the_same_three_things_reap_means():
    from ping_hub import reap
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("def _record_is_stale"):src.index("def arm_pid_record")]
    assert "facts is None" in body            # gone
    assert 'facts.get("image"' in body        # pid reused
    assert "_norm_created" in body            # different start time
    assert hasattr(reap, "_norm"), "reap's own comparator still exists"
