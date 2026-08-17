"""The reaper, which is mostly a list of things it refuses to do.

Ten identical `claude.exe` processes run on the machine this was built
against. That measurement is the whole design: a reaper that searches picks
one of ten, and picking wrong destroys work nobody asked to clear. So every
test here is about a refusal, and only one is about a kill.
"""

from __future__ import annotations

import json

import pytest

from ping_hub import reap

TITLE = "orca"
CREATED = "2026-08-17T12:45:19.000000-05:00"


@pytest.fixture
def inbox(tmp_path):
    return tmp_path


def record(inbox, **over):
    doc = {"pid": 4242, "image": "powershell.exe", "created": CREATED, "side": "win"}
    doc.update(over)
    reap.write_record(inbox, TITLE, doc["pid"], doc["image"], doc["created"],
                      doc["side"])
    return doc


def live(image="powershell.exe", created=CREATED):
    return lambda pid: {"image": image, "created": created}


DEAD = lambda pid: None


# ── the one thing it does ────────────────────────────────────────────────────
def test_a_confirmed_process_is_ended_as_a_tree(inbox):
    record(inbox)
    killed = []
    ok, msg = reap.reap(inbox, TITLE, query=live(),
                        kill=lambda pid: killed.append(pid) or (True, "done"))
    assert ok and killed == [4242]


def test_the_outcome_is_the_anchors_state_not_the_exit_code(inbox, monkeypatch):
    """`taskkill /T` exits NONZERO when any descendant races away mid-walk,
    even though everything it was asked to end is dead. Observed live on
    `wtprobe`: a 409 failure whose detail was a wall of SUCCESS lines."""
    record(inbox)
    monkeypatch.setattr(reap.proc, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1,
                                                       "stdout": "SUCCESS: ...",
                                                       "stderr": ""})())
    seen = []

    def query(pid):
        seen.append(pid)
        # confirm() sees it alive, the post-kill check sees it gone
        return {"image": "powershell.exe", "created": CREATED} if len(seen) == 1 else None

    ok, msg = reap.reap(inbox, TITLE, query=query)
    assert ok, msg
    assert "ended pid 4242" in msg
    assert reap.read_record(inbox, TITLE) is None      # record cleared


def test_a_survivor_is_reported_as_a_failure_even_on_exit_zero(inbox, monkeypatch):
    """The mirror case: taskkill claims success, the anchor is still there."""
    record(inbox)
    monkeypatch.setattr(reap.proc, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0,
                                                       "stdout": "", "stderr": ""})())
    ok, why = reap.reap(inbox, TITLE,
                        query=lambda pid: {"image": "powershell.exe",
                                           "created": CREATED})
    assert not ok and "still running" in why
    assert reap.read_record(inbox, TITLE) is not None  # record kept for a retry


def test_the_record_is_cleared_only_by_a_real_kill(inbox):
    """A failed kill must leave the record, or a retry has nothing to confirm."""
    record(inbox)
    reap.reap(inbox, TITLE, query=live(), kill=lambda pid: (False, "denied"))
    assert reap.read_record(inbox, TITLE) is not None


# ── everything it refuses ────────────────────────────────────────────────────
def test_no_record_refuses_and_says_how_to_fix_it(inbox):
    ok, why = reap.reap(inbox, TITLE, query=live())
    assert not ok
    assert "reboot it from the app" in why.lower()


def test_a_dead_pid_refuses(inbox):
    record(inbox)
    ok, why = reap.reap(inbox, TITLE, query=DEAD)
    assert not ok and "not running" in why


def test_a_reused_pid_is_caught_by_the_image_name(inbox):
    record(inbox)
    ok, why = reap.reap(inbox, TITLE, query=live(image="chrome.exe"))
    assert not ok and "reused" in why


def test_a_reused_pid_is_caught_by_the_start_time(inbox):
    """The same image at the same pid, started later: a different process.
    This is the case an image-only check would happily kill."""
    record(inbox)
    ok, why = reap.reap(inbox, TITLE,
                        query=live(created="2026-08-17T19:00:00.000000-05:00"))
    assert not ok and "reused" in why


@pytest.mark.parametrize("image", ["WindowsTerminal.exe", "explorer.exe",
                                   "services.exe", "csrss.exe"])
def test_the_shared_terminal_and_system_processes_are_never_targets(inbox, image):
    """Windows Terminal is ONE process shared by every tab. A record pointing
    at it would close every session Chris has open, so it is refused even when
    the record otherwise confirms."""
    record(inbox, image=image)
    ok, why = reap.reap(inbox, TITLE, query=live(image=image))
    assert not ok and "never a valid target" in why


def test_a_corrupt_record_is_not_a_licence_to_search(inbox):
    reap.record_path(inbox, TITLE).parent.mkdir(parents=True, exist_ok=True)
    reap.record_path(inbox, TITLE).write_text("{not json", encoding="utf-8")
    ok, why = reap.reap(inbox, TITLE, query=live())
    assert not ok and "no process was recorded" in why


def test_a_record_without_a_pid_is_treated_as_absent(inbox):
    reap.record_path(inbox, TITLE).parent.mkdir(parents=True, exist_ok=True)
    reap.record_path(inbox, TITLE).write_text('{"image": "powershell.exe"}',
                                              encoding="utf-8")
    assert reap.read_record(inbox, TITLE) is None


def test_nothing_is_killed_when_confirmation_fails(inbox):
    """The property that matters most: a refusal must not reach the killer."""
    record(inbox)
    killed = []
    for query in (DEAD, live(image="chrome.exe"),
                  live(created="2026-01-01T00:00:00.000000-05:00")):
        reap.reap(inbox, TITLE, query=query,
                  kill=lambda pid: killed.append(pid) or (True, ""))
    assert killed == []


# ── timestamp shapes, because two sources format them differently ────────────
@pytest.mark.parametrize("a, b", [
    ("2026-08-17T12:45:19.000000-05:00", "2026-08-17T12:45:19-0500"),
    ("2026-08-17T12:45:19.7654321-05:00", "2026-08-17T12:45:19-05:00"),
    ("2026-08-17T12:45:19Z", "2026-08-17T12:45:19+0000"),
])
def test_the_same_instant_confirms_across_formats(inbox, a, b):
    """The recorder and the process query round-trip time through different
    formatters. A spelling difference must not read as a reused pid and lock
    Chris out of clearing his own session."""
    record(inbox, created=a)
    ok, _ = reap.confirm(reap.read_record(inbox, TITLE), query=live(created=b))
    assert ok


def test_a_genuinely_different_second_still_refuses(inbox):
    record(inbox, created="2026-08-17T12:45:19-05:00")
    ok, _ = reap.confirm(reap.read_record(inbox, TITLE),
                         query=live(created="2026-08-17T12:45:20-05:00"))
    assert not ok


def test_a_bom_prefixed_record_still_reads(inbox):
    """PowerShell 5.1 writes `-Encoding utf8` WITH a byte-order mark. Read as
    plain utf-8 that BOM breaks the parse and the record reads as absent, so
    clear would refuse every session while a good record sat on disk."""
    p = reap.record_path(inbox, TITLE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"pid": 7, "image": "powershell.exe", "created": CREATED}).encode())
    got = reap.read_record(inbox, TITLE)
    assert got is not None and got["pid"] == 7


def test_the_record_round_trips(inbox):
    reap.write_record(inbox, TITLE, 99, "powershell.exe", CREATED, "wsl")
    got = reap.read_record(inbox, TITLE)
    assert got["pid"] == 99 and got["side"] == "wsl"
    assert json.loads(reap.record_path(inbox, TITLE).read_text())["image"] == "powershell.exe"
