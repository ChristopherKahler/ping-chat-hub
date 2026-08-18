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
                                           "created": CREATED}, sleep=lambda s: None)
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


# ── the WSL side ─────────────────────────────────────────────────────────────
# Closing a wsl session returned "no process was recorded" while its record sat
# on disk, correct and complete, in the WSL store. The write was never broken:
# the READ path was single-sided, four layers deep.

WSL_REC = {"pid": 3527758, "image": "claude", "created": "48344893", "side": "wsl"}
WIN_REC = {"pid": 4242, "image": "claude", "created": "2026-08-18T09:00:00-05:00"}


def test_a_win_record_resolves_to_the_windows_root(tmp_path):
    assert reap.inbox_root_for("win", tmp_path) == tmp_path


def test_a_wsl_record_resolves_to_the_wsl_store(tmp_path):
    class C:
        wsl = type("W", (), {"home_unc": r"\wsl.localhost\D\home\u"})()

    got = reap.inbox_root_for("wsl", tmp_path, cfg=C())
    assert got is not None and got.parts[-3:] == (".base-gbl", ".base", "relay-inbox")


def test_no_wsl_side_refuses_rather_than_using_the_windows_root(tmp_path):
    """Falling back would ask Windows about a Linux pid and get a confident
    'not running' for a session that is running."""
    class C:
        wsl = type("W", (), {"home_unc": ""})()

    assert reap.inbox_root_for("wsl", tmp_path, cfg=C()) is None


def test_confirm_asks_the_side_the_record_names(tmp_path):
    asked = {}

    def query(pid):
        return None

    def sided(pid, query=None, side="win"):
        asked["side"] = side
        return {"image": "claude", "created": "48344893"}

    import ping_hub.reap as r
    real, r.process_facts = r.process_facts, sided
    try:
        ok, why = r.confirm(WSL_REC)
    finally:
        r.process_facts = real
    assert asked["side"] == "wsl" and ok is True


def test_a_tick_count_is_compared_raw_not_normalised():
    """_norm reconciles timestamp SPELLINGS. Fed /proc field 22 it would
    truncate the tick count to 19 characters and call two different starts
    equal — the same offset-spelling trap that has bitten this stack three
    times, arriving from the other direction."""
    ok, why = reap.confirm(WSL_REC,
                           query=lambda pid: {"image": "claude", "created": "48344893"})
    assert ok is True
    bad, why = reap.confirm(WSL_REC,
                            query=lambda pid: {"image": "claude", "created": "48344999"})
    assert bad is False and "pid was reused" in why


def test_a_record_with_no_side_is_treated_as_windows():
    """The records already on disk predate the key, and they are all win."""
    ok, _ = reap.confirm(WIN_REC, query=lambda pid: {
        "image": "claude", "created": "2026-08-18T09:00:00-0500"})
    assert ok is True


def test_a_wsl_reap_never_calls_taskkill(tmp_path):
    used = []
    reap.write_record(tmp_path, "t", 3527758, "claude", "48344893", side="wsl")
    alive = [{"image": "claude", "created": "48344893"}]

    def query(pid):
        return alive[0]

    def kill(pid):
        used.append(pid)
        alive[0] = None
        return True, "killed"

    ok, why = reap.reap(tmp_path, "t", query=query, kill=kill)
    assert used == [3527758], why


def test_find_record_prefers_the_windows_store(tmp_path):
    """Windows first: it is the common case and the cheap one — the WSL root
    costs a UNC stat."""
    reap.write_record(tmp_path, "t", 4242, "claude", "x", side="win")
    got = reap.find_record(tmp_path, "t")
    assert got["pid"] == 4242


def test_every_wsl_call_carries_an_explicit_cd():
    """The hub daemon runs from system32 under a scheduled task. Without --cd
    WSL inherits that cwd, tries to chdir /mnt/c/WINDOWS/system32 on a dead 9p
    mount, and never starts -- so the kill silently does not run and the close
    reports "pid N is still running". Asserted on the BUILT command, because
    the call site looked correct while the command was not."""
    cmd = reap.wsl_cmd("true")
    assert cmd[0] == "wsl.exe"
    assert "--cd" in cmd and cmd[cmd.index("--cd") + 1].startswith("/")
    assert cmd.index("--cd") < cmd.index("-e"), "--cd must precede the command"


def test_no_wsl_invocation_in_reap_bypasses_the_helper():
    """One builder, or the next hostile-cwd bug is a new call site."""
    from pathlib import Path
    src = Path(reap.__file__).read_text(encoding="utf-8")
    code = chr(10).join(l.split("#")[0] for l in src.splitlines())
    assert code.count('"wsl.exe"') == 1


# ── TERM is not instantaneous ────────────────────────────────────────────────
# These deliberately do NOT inject `kill`: that seam returns its own result and
# short-circuits the post-kill verification, so a test using it never exercises
# the gone-check at all. Caught here -- three tests written against it passed
# while proving nothing. The kill is stubbed one layer lower instead.

class _Killed:
    stdout = ""
    stderr = ""


@pytest.fixture
def no_real_kill(monkeypatch):
    monkeypatch.setattr(reap.proc, "run", lambda *a, **k: _Killed())


def test_a_process_that_dies_a_moment_later_is_a_success(tmp_path, no_real_kill):
    """Measured on a clean probe: alive at t+0, gone at t+1s, stayed gone. The
    close reported failure while the session was already dying."""
    reap.write_record(tmp_path, "t", 999, "claude", "48344893", side="wsl")
    calls = []

    def query(pid):
        calls.append(pid)
        return {"image": "claude", "created": "48344893"} if len(calls) < 4 else None

    ok, why = reap.reap(tmp_path, "t", query=query, sleep=lambda s: None)
    assert ok is True, why
    assert len(calls) > 2, "the gone-check never retried"


def test_the_check_returns_the_moment_it_is_gone(tmp_path, no_real_kill):
    """The common case must not pay the whole budget: one check, then done."""
    slept = []
    reap.write_record(tmp_path, "t", 999, "claude", "x")
    calls = []

    def query(pid):
        calls.append(pid)
        return {"image": "claude", "created": "x"} if len(calls) == 1 else None

    ok, _ = reap.reap(tmp_path, "t", query=query, sleep=lambda s: slept.append(s))
    assert ok is True and slept == []


def test_a_genuinely_surviving_process_still_fails(tmp_path, no_real_kill):
    """The retry must not turn a real refusal into a false success -- that
    would start a second daemon on a live one."""
    reap.write_record(tmp_path, "t", 999, "claude", "x")
    ok, why = reap.reap(tmp_path, "t",
                        query=lambda pid: {"image": "claude", "created": "x"},
                        sleep=lambda s: None)
    assert ok is False and "still running" in why


def test_the_retry_is_bounded(tmp_path, no_real_kill):
    slept = []
    reap.write_record(tmp_path, "t", 999, "claude", "x")
    reap.reap(tmp_path, "t", query=lambda pid: {"image": "claude", "created": "x"},
              sleep=lambda s: slept.append(s))
    assert sum(slept) <= reap.GONE_BUDGET + reap.GONE_STEP


def test_both_sides_get_the_retry(tmp_path, no_real_kill):
    """A Windows tree kill has the same window; taskkill's exit code was
    already untrustworthy there for exactly this reason."""
    reap.write_record(tmp_path, "t", 999, "claude", "x", side="win")
    calls = []

    def query(pid):
        calls.append(pid)
        return {"image": "claude", "created": "x"} if len(calls) < 3 else None

    ok, why = reap.reap(tmp_path, "t", query=query, sleep=lambda s: None)
    assert ok is True, why
