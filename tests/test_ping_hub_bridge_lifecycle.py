"""The WSL bridge's lifecycle: it starts itself, and it says so when it has not.

The failure this covers was measured on 2026-08-19. WSL rebooted at 07:06, the
bridge was manual-start only, and nobody started it. The hub went blind to every
WSL session for 45 minutes while those sessions ran perfectly well — the worst
shape a failure can take, a working system behind a dead window. A terminal
launched from the app showed "booting", then vanished from the list with no
trace, because the only thing that could confirm it was the roster and the only
road the roster travels was down.

So two claims are tested here, and they are different claims:

  1. the bridge comes back by itself, from either side
  2. when it is down, everything downstream SAYS SO — no card evaporates, no
     empty list poses as "no sessions over there"

Hermetic: no sockets, no wsl.exe, no systemd. Every shell-out is an injected
`run` that records its argv, which is also how a test proves a code path never
talked to WSL at all.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from ping_hub import autostart, capabilities as caps, install
from ping_hub.config import Config
from ping_hub.engine import Engine, _HealBudget

from test_ping_hub_config import OPERATOR_LINUX_HOME, StubProbe


def _cfg(raw=None, **kw) -> Config:
    return Config(raw or {}, probe=StubProbe(**kw))


class FakeRun:
    """Records every argv and answers with a canned result, so a test can
    assert what WOULD have been run without running it."""

    def __init__(self, returncode: int = 0, stdout: str = "", raises=None) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.raises = raises

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode,
                                           stdout=self.stdout, stderr="")

    @property
    def shell_lines(self) -> list[str]:
        """The command string out of each `wsl.exe -e sh -c <line>` call."""
        return [c[-1] for c in self.calls]


# -- the unit itself ---------------------------------------------------------
def test_the_unit_restarts_forever_and_starts_at_boot():
    """Restart=always is the whole point, and default.target is what makes it
    start without a login — the account lingers, so nothing has to log in
    first for the hub to have a window."""
    text = autostart.bridge_unit_text(_cfg())
    assert "Restart=always" in text
    assert "WantedBy=default.target" in text
    assert "Type=simple" in text


def test_the_unit_names_an_absolute_interpreter_and_an_explicit_home():
    """systemd starts a service with an almost-empty env. A bare `python3`
    would resolve against a PATH the unit does not have, and the bridge builds
    every path it uses out of Path.home() — so HOME is load-bearing."""
    text = autostart.bridge_unit_text(_cfg(), python="/usr/bin/python3")
    exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
    interpreter, script = exec_line[len("ExecStart="):].split(" ", 1)
    assert interpreter.startswith("/")
    assert script.startswith("/")
    assert f"Environment=HOME={OPERATOR_LINUX_HOME}" in text


def test_the_unit_derives_every_path_from_config():
    """A hardcoded /home/<someone> would work on exactly one machine."""
    other = autostart.bridge_unit_text(_cfg(wsl_home="/home/someone"))
    assert "/home/someone/.local/share/hub-bridge/wsl-bridge.py" in other
    assert OPERATOR_LINUX_HOME not in other


def test_a_configured_interpreter_beats_the_default():
    text = autostart.bridge_unit_text(_cfg({"wsl": {"bridge_python": "/opt/py/bin/python3"}}))
    assert "ExecStart=/opt/py/bin/python3 " in text


# -- installing it -----------------------------------------------------------
def test_registration_reloads_enables_then_restarts_in_that_order():
    """`restart`, not `start`: this runs straight after a deploy that replaced
    the script on disk, and `start` on an already-running unit is a no-op that
    would leave the old code serving."""
    run = FakeRun()
    autostart.register_bridge(_cfg(), run=run)
    lines = [l for l in run.shell_lines if "systemctl" in l]
    assert lines == ["systemctl --user daemon-reload",
                     f"systemctl --user enable {autostart.BRIDGE_UNIT}",
                     f"systemctl --user restart {autostart.BRIDGE_UNIT}"]


def test_registration_is_idempotent():
    """Run it twice and it does the same three things. systemd owns the
    symlink bookkeeping; inventing our own would be a second source of truth."""
    a, b = FakeRun(), FakeRun()
    autostart.register_bridge(_cfg(), run=a)
    autostart.register_bridge(_cfg(), run=b)
    assert a.shell_lines == b.shell_lines


def test_a_dry_run_touches_nothing_but_still_shows_the_plan():
    run = FakeRun()
    lines = autostart.register_bridge(_cfg(), run=run, dry_run=True)
    assert [c for c in run.calls if "systemctl" in c[-1]] == []
    assert sum("systemctl" in l for l in lines) == 3


def test_no_systemd_falls_back_to_the_documented_manual_start():
    """A distro without systemd is not a broken machine. It gets the command
    the README already documents, and the install does not fail."""
    run = FakeRun(returncode=1)          # `test -d /run/systemd/system` fails
    lines = autostart.register_bridge(_cfg(), run=run)
    assert not any("systemctl" in l for l in run.shell_lines)
    assert any("wsl-bridge.py" in l for l in lines)


def test_a_machine_with_no_wsl_is_never_asked_anything():
    """Absent is not off, and it is not an error either — it is a Mac."""
    run = FakeRun()
    lines = autostart.register_bridge(_cfg(distro="", wsl_home=""), run=run)
    assert run.calls == []
    assert "no WSL side" in lines[0]


def test_systemd_detection_does_not_ask_is_system_running():
    """`is-system-running` answers `degraded` on a healthy box that happens to
    have one unrelated failed unit, and that is not a reason to refuse."""
    run = FakeRun()
    autostart.bridge_has_systemd(_cfg(), run=run)
    assert run.shell_lines == ["test -d /run/systemd/system"]


def test_the_interpreter_is_asked_of_wsl_and_falls_back_when_it_cannot_answer():
    assert autostart.bridge_python(_cfg(), run=FakeRun(stdout="/usr/bin/python3\n")) \
        == "/usr/bin/python3"
    dead = FakeRun(raises=OSError("wsl is not running"))
    assert autostart.bridge_python(_cfg(), run=dead) == "/usr/bin/python3"


def test_status_reports_absent_rather_than_pretending_it_is_off():
    run = FakeRun(stdout="absent\ninactive\n")
    assert autostart.bridge_status(_cfg(), run=run) == {
        "enabled": "absent", "active": "inactive", "detail": ""}
    assert autostart.bridge_status(_cfg(distro=""), run=FakeRun())["active"] == "absent"


def test_a_dead_wsl_does_not_raise_out_of_status():
    got = autostart.bridge_status(_cfg(), run=FakeRun(raises=OSError("no wsl")))
    assert got["active"] == "unknown" and "no wsl" in got["detail"]


# -- what deploy puts on disk ------------------------------------------------
def test_deploy_lays_down_the_unit_beside_the_script_and_its_config(tmp_path):
    """Three files, one deploy. The unit is written here — over the same share
    as the other two — and ENABLED separately; writing a file is this module's
    job, talking to systemd is not."""
    got = install.deploy_bridge(_cfg(), deploy_unc=str(tmp_path / "deploy"),
                                config_unc=str(tmp_path / "home"),
                                log=lambda m: None, python="/usr/bin/python3")
    unit = tmp_path / "home" / ".config" / "systemd" / "user" / autostart.BRIDGE_UNIT
    assert unit.is_file() and got["unit"] == str(unit)
    # LF, like its two siblings: systemd parses this inside Linux, and a stray
    # CR lands INSIDE the value of the last key on every line
    assert b"\r\n" not in unit.read_bytes()
    assert "ExecStart=/usr/bin/python3 " in unit.read_text(encoding="utf-8")


def test_deploy_still_writes_the_script_and_config_it_always_did(tmp_path):
    """The unit is additive. Nothing that already landed may stop landing."""
    got = install.deploy_bridge(_cfg(), deploy_unc=str(tmp_path / "d"),
                                config_unc=str(tmp_path / "h"), log=lambda m: None)
    from pathlib import Path
    assert Path(got["script"]).is_file() and Path(got["config"]).is_file()


# -- the restart budget ------------------------------------------------------
def test_the_first_attempt_is_free_then_the_interval_widens_and_holds():
    """A bridge that has just died may only need a nudge, so the first try is
    immediate. A bridge that is never coming back must cost one line every five
    minutes, not one every five seconds."""
    b = _HealBudget()
    assert b.wait() == 0 and b.due(0.0) is True
    seen = []
    for _ in range(6):
        b.mark(0.0)
        seen.append(b.wait())
    assert seen == [30, 60, 120, 300, 300, 300]


def test_an_attempt_is_not_due_until_its_interval_has_passed():
    b = _HealBudget()
    b.mark(1000.0)                       # one attempt made; next allowed at +30
    assert b.due(1029.0) is False
    assert b.due(1030.0) is True


def test_a_reconnect_forgives_the_whole_history():
    """A bridge that comes back and dies again is a fresh incident — it must
    not inherit a five-minute wait from the last one."""
    b = _HealBudget()
    for _ in range(4):
        b.mark(0.0)
    assert b.wait() == 300
    b.reset()
    assert b.wait() == 0 and b.due(0.0) is True


# -- healing ------------------------------------------------------------------
def _engine() -> Engine:
    e = Engine.__new__(Engine)           # no store, no threads, no filesystem
    e.threads = {}
    e.side = "win"
    import threading
    e.lock = threading.Lock()
    e.bridge_state = {"up": True, "since": "", "detail": "", "enabled": True}
    e._heal = _HealBudget()
    return e


def test_healing_tries_systemd_first_then_a_direct_start(monkeypatch):
    """The unit covers the ordinary case. The direct start covers the one it
    cannot: a WSL with no systemd, or one where the unit was never installed."""
    run = FakeRun(returncode=1)          # systemctl fails, so the fallback runs
    started = []
    monkeypatch.setattr("ping_hub.engine.proc.run", run)
    monkeypatch.setattr("ping_hub.engine.CFG", _cfg())
    monkeypatch.setattr("ping_hub.autostart.start_bridge_detached",
                        lambda cfg: started.append(cfg) or True)
    _engine()._bridge_heal()
    assert run.shell_lines == [f"systemctl --user restart {autostart.BRIDGE_UNIT}"]
    assert len(started) == 1, "systemctl failed and nothing fell through to it"


def test_the_direct_start_is_detached_and_never_a_backgrounded_shell_job():
    """MEASURED 2026-08-19: `sh -c 'setsid nohup ... &'` through wsl.exe returns
    0 and starts NOTHING — WSL tears its children down when the interop session
    exits. A fallback that reports success and does nothing is the exact lie
    this fork exists to kill, so the session is held open by a detached Windows
    process instead."""
    seen = {}

    def popen(argv, **kw):
        seen["argv"] = argv
        seen.update(kw)
        return object()

    assert autostart.start_bridge_detached(_cfg(), python="/usr/bin/python3",
                                           popen=popen) is True
    assert seen["argv"] == ["wsl.exe", "-e", "/usr/bin/python3",
                            f"{OPERATOR_LINUX_HOME}/.local/share/hub-bridge/wsl-bridge.py"]
    assert "&" not in " ".join(seen["argv"]), "a shell background job again"
    assert "nohup" not in " ".join(seen["argv"])
    if os.name == "nt":                  # the flags only exist on Windows
        assert seen["creationflags"] == autostart._DETACHED
    for stream in ("stdin", "stdout", "stderr"):
        assert seen[stream] == subprocess.DEVNULL


def test_a_direct_start_that_cannot_launch_reports_failure(monkeypatch):
    """It must never return True having started nothing — that is the whole
    bug this replaced."""
    def boom(argv, **kw):
        raise OSError("wsl.exe is not on PATH")
    assert autostart.start_bridge_detached(_cfg(), popen=boom) is False


def test_healing_stops_at_the_first_thing_that_works(monkeypatch):
    run = FakeRun(returncode=0)
    monkeypatch.setattr("ping_hub.engine.proc.run", run)
    monkeypatch.setattr("ping_hub.engine.CFG", _cfg())
    _engine()._bridge_heal()
    assert len(run.calls) == 1


def test_a_dead_wsl_never_takes_the_loop_down_with_it(monkeypatch):
    """Every failure mode here is expected. A hub that falls over because WSL
    is down is worse than a hub with no WSL side."""
    monkeypatch.setattr("ping_hub.engine.CFG", _cfg())
    # the fallback is stubbed to FAIL: this test is about surviving a dead WSL,
    # and an unstubbed one would launch a real wsl.exe out of a unit test
    monkeypatch.setattr("ping_hub.autostart.start_bridge_detached",
                        lambda cfg: False)
    run = FakeRun(raises=subprocess.TimeoutExpired("wsl", 30))
    monkeypatch.setattr("ping_hub.engine.proc.run", run)
    _engine()._bridge_heal()             # must not raise
    assert len(run.calls) == 1           # and it still fell through to the fallback
    run = FakeRun(raises=OSError("wsl.exe is missing"))
    monkeypatch.setattr("ping_hub.engine.proc.run", run)
    _engine()._bridge_heal()


def test_a_machine_with_no_wsl_side_is_never_shelled_out_to(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr("ping_hub.engine.proc.run", run)
    monkeypatch.setattr("ping_hub.engine.CFG", _cfg(distro="", wsl_home=""))
    _engine()._bridge_heal()
    assert run.calls == []


# -- saying so ---------------------------------------------------------------
def test_since_moves_only_when_the_state_actually_changes():
    """So the UI can say how long it has been down, not how long ago we last
    looked."""
    e = _engine()
    e._bridge_mark(False, "refused")
    first = e.bridge_state["since"]
    e._bridge_mark(False, "refused again")
    assert e.bridge_state["since"] == first
    assert e.bridge_state["detail"] == "refused again"


def test_not_asked_yet_is_not_the_same_fact_as_down():
    """A hub that has just booted has not probed anything. Announcing an
    outage in that window would be the same species of lie this fork exists to
    remove — an unknown dressed up as a measurement."""
    e = Engine.__new__(Engine)
    import threading
    e.lock = threading.Lock()
    e.bridge_state = {"up": False, "probed": False, "since": "",
                      "detail": "not probed yet", "enabled": True}
    assert e.bridge_state["probed"] is False   # nothing to report yet
    e._bridge_mark(False, "refused")
    assert e.bridge_state["probed"] is True     # now it is a measurement


def test_bridge_state_survives_a_hub_that_never_saw_a_wsl_thread():
    """The original flag lived on wsl threads, so a hub started DURING an
    outage had nothing to stamp and showed an empty list with no explanation.
    Absent is not empty."""
    e = _engine()
    e.threads = {}
    e._bridge_mark(False, "connection refused")
    assert e.bridge_state["up"] is False
    assert e.bridge_state["detail"] == "connection refused"


# -- doctor ------------------------------------------------------------------
def test_doctor_separates_no_wsl_from_a_bridge_that_is_not_answering():
    """The four states are four different fixes. `absent` means there is
    nothing over there; `error` means there is, and it is not answering — the
    exact failure this whole fork exists to surface."""
    up = caps.bridge(_cfg(), reach=lambda u, timeout=2.0: (True, ""),
                     wsl_ip=lambda c: "172.20.160.2")
    assert up["state"] == caps.READY and ":7798/snapshot" in up["detail"]

    down = caps.bridge(_cfg(), reach=lambda u, timeout=2.0: (False, "refused"),
                       wsl_ip=lambda c: "172.20.160.2")
    assert down["state"] == caps.ERROR and "refused" in down["detail"]

    none = caps.bridge(_cfg(distro="", wsl_home=""), wsl_ip=lambda c: "")
    assert none["state"] == caps.ABSENT


def test_doctor_does_not_report_a_missing_wsl_as_a_human_switching_it_off():
    """`wsl.enabled` DERIVES from a distro resolving, so asking it first turns
    "this machine has no WSL" into "someone turned this off". Fourth time this
    inversion has come up in capabilities.py."""
    assert caps.bridge(_cfg(distro="", wsl_home=""),
                       wsl_ip=lambda c: "")["state"] == caps.ABSENT
    off = caps.bridge(_cfg({"wsl": {"enabled": False}}), wsl_ip=lambda c: "1.2.3.4")
    assert off["state"] == caps.OFF


def test_doctor_reports_an_unresolvable_ip_as_an_error_not_a_blank():
    got = caps.bridge(_cfg(), wsl_ip=lambda c: "")
    assert got["state"] == caps.ERROR and "did not resolve" in got["detail"]


def test_the_bridge_joins_the_panel_doctor_prints():
    """A capability that nothing asks for is a capability that stays wrong."""
    got = caps.probe_all(_cfg(distro="", wsl_home=""),
                         reach=lambda u, timeout=2.0: (True, ""))
    assert "bridge" in got
