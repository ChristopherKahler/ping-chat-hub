"""cx-ptt as a supervised service, not a thing a human double-clicks.

It died 2026-08-18 20:36 and stayed dead across two WSL boots and a full
reboot — ~16 hours with no channel hotkeys, no desktop mic and no sounds —
while every hub-owned component came back by itself. Worse, the hub reported
it READY for all of it, because `capabilities.cx_ptt` decided that from files
existing on disk and never asked whether the process was there.

Everything here is hermetic: no processes, no clock waits, no registry.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ping_hub import capabilities as caps
from ping_hub import cxptt, install
from ping_hub.config import Config
from ping_hub.engine import Engine, _HealBudget

from test_ping_hub_config import CX_SLOT_PATH, CX_TOML_PATH, StubProbe


def _cfg(raw=None, **kw) -> Config:
    return Config(raw or {}, probe=StubProbe(**kw))


def _devices(age_seconds: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return json.dumps({"ts": when.isoformat(), "devices": {"Playback": []}})


# ── the heartbeat ───────────────────────────────────────────────────────────
def test_a_daemon_refreshing_its_device_list_reads_as_alive():
    beat = cxptt.heartbeat(_cfg(), read=lambda p: _devices(7))
    assert beat["alive"] is True and beat["known"] is True
    assert "7s ago" in beat["detail"]


def test_a_daemon_that_stopped_refreshing_reads_as_dead():
    """The real corpse: 2026-08-18 20:36 to the next morning."""
    beat = cxptt.heartbeat(_cfg(), read=lambda p: _devices(58828))
    assert beat["alive"] is False and beat["known"] is True
    assert "no heartbeat for 58828s" in beat["detail"]


def test_the_budget_is_three_missed_refreshes_not_six():
    """DEVICES_STALE_AFTER answers "is this list current" for the settings
    panel. Liveness is a different question and gets its own number."""
    assert cxptt.HEARTBEAT_STALE_AFTER == 90.0
    assert cxptt.heartbeat(_cfg(), read=lambda p: _devices(89))["alive"] is True
    assert cxptt.heartbeat(_cfg(), read=lambda p: _devices(91))["alive"] is False


def test_no_heartbeat_file_is_unknown_rather_than_dead():
    """A machine that never had cx-ptt must not read as a daemon that died."""
    def missing(p):
        raise OSError("no such file")
    beat = cxptt.heartbeat(_cfg(), read=missing)
    assert beat["known"] is False and beat["alive"] is False
    assert "no heartbeat file" in beat["detail"]


def test_a_file_with_no_usable_timestamp_says_so():
    beat = cxptt.heartbeat(_cfg(), read=lambda p: json.dumps({"devices": {}}))
    assert beat["known"] is False
    assert "no usable timestamp" in beat["detail"]


def test_the_poll_never_enumerates_processes():
    """`status()` costs a full Win32_Process CIM query with a 30s timeout. A
    supervisor that cannot look often cannot notice, so the poll path must not
    go anywhere near it."""
    import ast
    import inspect
    fn = ast.parse(inspect.getsource(cxptt.heartbeat)).body[0]
    code = ast.unparse(ast.Module(body=fn.body[1:], type_ignores=[]))  # no docstring
    assert "list_processes" not in code and "status(" not in code


# ── what the hub then reports ───────────────────────────────────────────────
def test_files_on_disk_are_no_longer_mistaken_for_a_running_daemon():
    """The whole 16-hour blind spot in one assertion."""
    r = caps.cx_ptt(_cfg(), exists=lambda p: str(p) in (CX_TOML_PATH, CX_SLOT_PATH),
                    beat={"alive": False, "known": True,
                          "detail": "no heartbeat for 58828s"})
    assert r["state"] == caps.ERROR
    assert "58828s" in r["detail"]


def test_a_live_daemon_reports_ready_and_says_how_fresh():
    r = caps.cx_ptt(_cfg(), exists=lambda p: str(p) in (CX_TOML_PATH, CX_SLOT_PATH),
                    beat={"alive": True, "known": True,
                          "detail": "last heartbeat 7s ago"})
    assert r["state"] == caps.READY
    assert "7s ago" in r["detail"]


def test_a_machine_that_never_had_cx_ptt_is_absent_not_broken():
    """Ordering rule this file has learned four times: absence is checked
    BEFORE anything else, or Albert's Mac reports as a failure."""
    r = caps.cx_ptt(_cfg(), exists=lambda p: False,
                    beat={"alive": False, "known": False, "detail": "x"})
    assert r["state"] == caps.ABSENT


# ── the supervisor ──────────────────────────────────────────────────────────
def test_the_restart_budget_is_the_bridge_s_budget():
    """First attempt free — a daemon that has just died may only need a nudge.
    Then it widens and holds, so one that is never coming back costs one line
    every five minutes instead of one every twenty seconds."""
    b = _HealBudget()
    assert b.due(1000.0) is True          # free
    b.mark(1000.0)
    assert b.due(1029.0) is False
    assert b.due(1030.0) is True
    b.mark(1030.0)
    assert b.due(1089.0) is False and b.due(1090.0) is True


def test_state_records_when_it_changed_not_when_it_was_last_polled():
    e = Engine.__new__(Engine)            # no threads, no real machine
    import threading
    e.lock = threading.Lock()
    e.cxptt_state = {"alive": True, "probed": False, "enabled": True,
                     "since": "", "detail": ""}
    e._cxptt_mark(True, "up")
    assert e.cxptt_state["since"] == ""   # nothing changed
    e._cxptt_mark(False, "gone")
    first = e.cxptt_state["since"]
    assert first                          # a transition stamps it
    e._cxptt_mark(False, "still gone")
    assert e.cxptt_state["since"] == first
    assert e.cxptt_state["probed"] is True


def test_the_supervisor_reports_one_line_per_transition():
    """A restart loop that logs every poll buries the real failure."""
    import inspect
    body = inspect.getsource(Engine._cxptt_loop)
    assert "if alive != was:" in body
    assert "heartbeat(CFG)" in body


def test_the_supervisor_never_dies_of_the_thing_it_supervises():
    import inspect
    body = inspect.getsource(Engine._cxptt_loop)
    assert "except OSError" in body


# ── the bridge is the other half of "one app" ───────────────────────────────
def test_a_bare_machine_gets_the_bridge_without_being_asked():
    """It sat behind an opt-in flag, so a recipient's install wrote no bridge
    and registered no unit — half the promise, silently."""
    cfg = _cfg({"wsl": {"bridge_deploy_unc": r"\\wsl.localhost\Ubuntu\home\op"}})
    want, why = install.bridge_decision(cfg, exists=lambda p: False)
    assert want is True and "no bridge deployed" in why


def test_a_bridge_that_is_already_there_is_not_overwritten_silently():
    """The original flag's warning is still true: this writes into a live WSL
    home and the bridge it replaces may be running right now."""
    cfg = _cfg({"wsl": {"bridge_deploy_unc": r"\\wsl.localhost\Ubuntu\home\op"}})
    want, why = install.bridge_decision(cfg, exists=lambda p: True)
    assert want is False and "--deploy-bridge" in why


def test_the_flag_still_forces_a_replacement():
    cfg = _cfg({"wsl": {"bridge_deploy_unc": r"\\wsl.localhost\Ubuntu\home\op"}})
    want, _ = install.bridge_decision(cfg, force=True, exists=lambda p: True)
    assert want is True


def test_opting_out_is_still_possible():
    cfg = _cfg({"wsl": {"bridge_deploy_unc": r"\\wsl.localhost\Ubuntu\home\op"}})
    want, why = install.bridge_decision(cfg, skip=True, exists=lambda p: False)
    assert want is False and "--no-deploy-bridge" in why


def test_a_machine_with_no_wsl_is_not_asked_to_deploy_a_bridge():
    cfg = _cfg({"wsl": {"enabled": False}})
    want, why = install.bridge_decision(cfg, exists=lambda p: False)
    assert want is False and "no WSL side" in why


# ── the wire ────────────────────────────────────────────────────────────────
def test_the_installer_actually_calls_the_bridge_decision():
    """Built, tested, and then imported by nothing is a feature shipped dead."""
    import inspect

    from ping_hub import cli
    body = inspect.getsource(cli.cmd_install)
    assert "install.bridge_decision(" in body
    assert "supersede_interim_run_key" in body


def test_the_hub_exposes_cx_ptt_liveness_the_way_it_exposes_the_bridge_s():
    from pathlib import Path

    from ping_hub import daemon
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    assert '"/api/cxptt"' in src
    assert "engine.cxptt_state" in src
    html = Path(daemon.HTML).read_text(encoding="utf-8")
    assert '"/api/cxptt"' in html
    assert "cx-ptt down" in html

# ── provisioning it like an engine instead of a desktop shortcut ────────────
def test_the_launcher_keeps_the_tee_and_the_title():
    """cxptt.restart() always relaunches THROUGH this file, precisely so a
    daemon that restarted itself bare gets its log and window title back. A
    plain shim would repair it into a degraded state."""
    text = install.cxptt_launcher_text(
        Path("H:/h/stt/venv/Scripts/python.exe"), Path("H:/h/cxptt/cx-ptt.py"),
        Path("H:/h/cx/ptt-daemon.log"), {})
    assert "Tee-Object" in text and "ptt-daemon.log" in text
    assert "title Work Channel" in text


def test_the_launcher_never_runs_the_operator_s_global_python():
    """install.py finding 3, and the hand-written launcher this replaces ran
    the daemon on a global interpreter that a pip step there once broke."""
    text = install.cxptt_launcher_text(
        Path("H:/h/stt/venv/Scripts/python.exe"), Path("H:/h/cxptt/cx-ptt.py"),
        Path("H:/h/cx/ptt-daemon.log"), {})
    # separator-agnostic: Path renders \ on Windows and / elsewhere
    assert str(Path("H:/h/stt/venv/Scripts/python.exe")) in text
    assert "Python312" not in text


def test_the_launcher_carries_the_paths_the_scripts_derive_from():
    text = install.cxptt_launcher_text(
        Path("py"), Path("s"), Path("l"),
        {"PING_HUB_CX_TOML": "C:/x/cx.toml", "PING_HUB_STT_MODEL": "C:/x/model",
         "PING_HUB_WSL_HOME_UNC": ""})
    assert 'set "PING_HUB_CX_TOML=C:/x/cx.toml"' in text
    assert 'set "PING_HUB_STT_MODEL=C:/x/model"' in text
    # an empty value is not written: absent means derive, and "" would pin the
    # scripts to a path that is not one
    assert "PING_HUB_WSL_HOME_UNC" not in text


def test_it_extends_the_speech_venv_rather_than_building_a_third(tmp_path, monkeypatch):
    """One app, one interpreter (toucan's G0 ruling). And cx-ptt loads exactly
    the parakeet provision_stt already fetched, so there is one model too."""
    src = tmp_path / "pkg" / "ptt"
    src.mkdir(parents=True)
    (src / "cx-ptt.py").write_text("x = 1", encoding="utf-8")
    (src / "cxpaths.py").write_text("y = 2", encoding="utf-8")
    monkeypatch.setattr(install, "CXPTT_SRC", src)
    seen = {}

    def fake_venv(path, packages, log=None):
        seen["path"] = path
        seen["packages"] = list(packages)
        return path / "Scripts" / "python.exe"
    monkeypatch.setattr(install, "make_venv", fake_venv)

    home = tmp_path / "home"
    stt = {"python": str(home / "stt" / "venv" / "Scripts" / "python.exe"),
           "model_dir": str(home / "stt" / "model")}
    out = install.provision_cxptt(home, _cfg(), stt, log=lambda m: None)

    assert seen["path"] == home / "stt" / "venv"          # the SAME venv
    assert seen["packages"] == install.STT_PIP + install.CXPTT_PIP
    assert (Path(out["dir"]) / "cx-ptt.py").is_file()
    assert (Path(out["dir"]) / "cxpaths.py").is_file()    # the paths module too
    assert Path(out["launcher"]).is_file()


def test_a_package_without_the_scripts_says_so_instead_of_half_installing(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "CXPTT_SRC", tmp_path / "nope")
    with pytest.raises(install.InstallError, match="cx-ptt sources missing"):
        install.provision_cxptt(tmp_path, _cfg(), {"python": "p", "model_dir": "m"},
                                log=lambda m: None)


def test_the_installer_provisions_it_and_writes_its_launcher_into_hub_toml():
    import inspect

    from ping_hub import cli
    body = inspect.getsource(cli.cmd_install)
    assert "install.provision_cxptt(" in body
    assert 'sections["cx_ptt"]' in body
    # Windows-only: on a Mac this is a machine without the feature, not a
    # failed install
    assert 'os.name == "nt"' in body
