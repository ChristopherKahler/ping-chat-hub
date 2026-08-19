"""The standing `chris` title must not decay while the bridge is up.

Measured 2026-08-19: `base relay sessions` showed `chris [DEAD 30m]` while the
bridge unit was active and serving /snapshot. His `last_heartbeat` matched the
unit's ExecMainStartTimestamp TO THE SECOND, half an hour earlier — because the
registry's live/DEAD state reads the registration timestamp, not the sentinel,
and `chris_keeper` registered exactly once per process. Every ping to him had
started warning that he may be dead, and the fully decayed end state of that
path is the morning's "no session registered as 'chris'" refusal, which is the
outage this whole day began with.

The loop counts TICKS rather than reading a clock, which is what lets these
tests prove the cadence without injecting a clock or sleeping for real. Nothing
here sleeps, spawns, or touches the operator's store beyond a tmp_path.
"""

from __future__ import annotations

import subprocess

import pytest

from ping_hub.bridge import wsl_bridge as b


class Run:
    """Records every argv and answers with a canned result."""

    def __init__(self, returncode: int = 0, raises=None) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode)


def keeper(monkeypatch, tmp_path, run, ticks: int):
    """Run the loop for exactly `ticks` iterations against a scratch inbox."""
    monkeypatch.setattr(b, "INBOX", tmp_path)
    slept: list[int] = []
    b.chris_keeper(run=run, sleep=slept.append, stop=lambda t: t >= ticks)
    return slept


# -- the cadence -------------------------------------------------------------
def test_it_registers_on_the_very_first_tick():
    """Boot behaviour is not lost. The original registered once at boot and the
    replacement must still do that, immediately, not a minute later."""
    run = Run()
    b.chris_keeper(run=run, sleep=lambda s: None, stop=lambda t: t >= 1)
    assert len(run.calls) == 1


def test_it_registers_again_on_the_cadence_and_not_in_between(monkeypatch, tmp_path):
    """The whole defect in one assertion: once per process is not enough."""
    run = Run()
    keeper(monkeypatch, tmp_path, run, ticks=b.KEEPALIVE_TICKS + 1)
    assert len(run.calls) == 2, "registered once per process again"

    run = Run()
    keeper(monkeypatch, tmp_path, run, ticks=b.KEEPALIVE_TICKS)
    assert len(run.calls) == 1, "registered early — the cadence is not the constant"


def test_the_cadence_is_fast_enough_to_matter(monkeypatch, tmp_path):
    """A refresh slower than the DEAD horizon is the bug wearing a new number.
    The exact base threshold is not documented and base is outside custody; it
    was bounded live between 3 minutes (a session reading live) and 30 minutes
    (chris reading DEAD), so this guards well inside that."""
    assert b.KEEPALIVE_TICKS * b.TICK_SECS <= 120


def test_the_sentinel_is_touched_on_every_tick_including_registration_ones(
        monkeypatch, tmp_path):
    """The two halves share a thread and neither may starve the other."""
    run = Run()
    slept = keeper(monkeypatch, tmp_path, run, ticks=b.KEEPALIVE_TICKS + 2)
    assert (tmp_path / b.HUB_TITLE / ".watching").is_file()
    assert len(slept) == b.KEEPALIVE_TICKS + 2
    assert set(slept) == {b.TICK_SECS}


# -- never take the thread down ----------------------------------------------
@pytest.mark.parametrize("boom", [OSError("no base binary"),
                                  subprocess.TimeoutExpired("base", 15)])
def test_a_failing_register_never_kills_the_keeper(monkeypatch, tmp_path, boom):
    """THE second bug. The original call had no try/except at all, in a daemon
    thread with no handler — so a slow or missing `base` at boot killed the
    keeper outright AND took the sentinel touch with it, silently. That is a
    worse failure than the decay this fork is named for."""
    run = Run(raises=boom)
    slept = keeper(monkeypatch, tmp_path, run, ticks=b.KEEPALIVE_TICKS + 2)
    assert len(slept) == b.KEEPALIVE_TICKS + 2, "the loop died"
    assert (tmp_path / b.HUB_TITLE / ".watching").is_file(), "sentinel starved"


def test_register_reports_failure_rather_than_raising():
    assert b.register_chris(run=Run(returncode=0)) is True
    assert b.register_chris(run=Run(returncode=1)) is False
    assert b.register_chris(run=Run(raises=OSError("gone"))) is False


# -- what it registers as ----------------------------------------------------
def test_it_registers_the_standing_title_exactly():
    """A rename on either side silently orphans every `ping --to chris`, which
    is precisely how the morning outage presented."""
    run = Run()
    b.register_chris(run=run)
    argv = run.calls[0]
    assert argv[1:] == ["relay", "register", "--as", b.HUB_TITLE,
                        "--session", "hub-chris-standing-wsl"]
    assert b.HUB_TITLE == "chris"


# -- noise ------------------------------------------------------------------
def test_it_says_nothing_while_it_is_working(monkeypatch, tmp_path, capsys):
    keeper(monkeypatch, tmp_path, Run(), ticks=b.KEEPALIVE_TICKS * 2 + 1)
    assert capsys.readouterr().out == ""


def test_it_speaks_once_when_it_breaks_and_once_when_it_recovers(
        monkeypatch, tmp_path, capsys):
    """Silence is the healthy state; a keeper failing for an hour with no trace
    is the shape of the outage. One line each way, nothing in between."""
    monkeypatch.setattr(b, "INBOX", tmp_path)

    class Flaky(Run):
        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            # fails for the first two registrations, then recovers
            code = 1 if len(self.calls) <= 2 else 0
            return subprocess.CompletedProcess(argv, code)

    b.chris_keeper(run=Flaky(), sleep=lambda s: None,
                   stop=lambda t: t >= b.KEEPALIVE_TICKS * 3 + 1)
    out = capsys.readouterr().out
    assert out.count("FAILING") == 1, "repeated the same bad news"
    assert out.count("recovered") == 1
