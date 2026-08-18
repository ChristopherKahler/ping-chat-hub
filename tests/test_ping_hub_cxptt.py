"""Restarting the hotkey daemon, and switching audio devices.

The tree walk is the part worth testing hard. cx-ptt restarts ITSELF on a mic
change by respawning a bare `python cx-ptt.py`, so the daemon's ancestry is one
shape after a launcher start and a completely different shape after any mic
switch. A button anchored on the launcher reports "not running" on the second
one, which is the failure this file exists to prevent.

Every process call is injected. Nothing here spawns, kills, or shells out.
"""

from __future__ import annotations

import pytest

from ping_hub import capabilities, cxptt
from ping_hub.config import Config

from test_ping_hub_config import StubProbe


@pytest.fixture
def cfg(tmp_path) -> Config:
    launcher = tmp_path / "Work-Channel.cmd"
    launcher.write_text("@echo off", encoding="utf-8")
    cx = tmp_path / "cx.toml"
    cx.write_text("[settings]", encoding="utf-8")
    return Config({"paths": {"base_gbl": str(tmp_path / "gbl")},
                   "cx_ptt": {"launcher": str(launcher), "cx_toml": str(cx),
                              "devices_json": str(tmp_path / "audio-devices.json")}},
                  probe=StubProbe())


# the shape a launcher start produces: cmd -> powershell -> python
LAUNCHED = [
    {"pid": 10, "ppid": 1, "name": "explorer.exe", "cmdline": "explorer.exe"},
    {"pid": 20, "ppid": 10, "name": "cmd.exe",
     "cmdline": 'cmd.exe /c ""C:\\x\\Work-Channel.cmd" "'},
    {"pid": 30, "ppid": 20, "name": "powershell.exe",
     "cmdline": "powershell -NoProfile -Command python -u cx-ptt.py | Tee-Object"},
    {"pid": 40, "ppid": 30, "name": "python.exe", "cmdline": "python.exe -u cx-ptt.py"},
]

# the shape restart_self produces: a lone python, no cmd root, no tee
RESTARTED = [
    {"pid": 10, "ppid": 1, "name": "explorer.exe", "cmdline": "explorer.exe"},
    {"pid": 40, "ppid": 10, "name": "python.exe", "cmdline": "python.exe cx-ptt.py"},
]


# -- the tree ----------------------------------------------------------------
def test_the_daemon_is_found_in_both_launch_shapes():
    assert cxptt.find_daemon(LAUNCHED)["pid"] == 40
    assert cxptt.find_daemon(RESTARTED)["pid"] == 40


def test_the_root_of_a_launcher_start_is_the_cmd():
    assert cxptt.launch_root(cxptt.find_daemon(LAUNCHED), LAUNCHED)["pid"] == 20


def test_the_root_of_a_self_restarted_daemon_is_the_python_itself():
    """The bug this prevents: hunting for the launcher's cmd.exe finds nothing
    here and reports a running daemon as absent."""
    assert cxptt.launch_root(cxptt.find_daemon(RESTARTED), RESTARTED)["pid"] == 40


def test_the_walk_stops_before_unrelated_ancestors():
    """explorer is the parent of the cmd. Killing past the chain would take
    Chris's desktop with it."""
    assert cxptt.launch_root(cxptt.find_daemon(LAUNCHED), LAUNCHED)["pid"] != 10


def test_a_parent_loop_cannot_hang_the_walk():
    rows = [{"pid": 1, "ppid": 2, "name": "cmd.exe", "cmdline": "a.cmd"},
            {"pid": 2, "ppid": 1, "name": "cmd.exe", "cmdline": "b.cmd"},
            {"pid": 3, "ppid": 1, "name": "python.exe", "cmdline": "cx-ptt.py"}]
    assert cxptt.launch_root(cxptt.find_daemon(rows), rows)["pid"] in (1, 2)


def test_no_cx_ptt_process_is_absent_not_an_error():
    rows = [{"pid": 10, "ppid": 1, "name": "explorer.exe", "cmdline": "explorer.exe"}]
    assert cxptt.find_daemon(rows) is None


# -- restart -----------------------------------------------------------------
def _facts(alive: dict):
    return lambda pid: alive.get(pid)


def test_a_restart_kills_the_root_and_starts_the_launcher(cfg):
    killed, started = [], []
    alive = {40: {"image": "python.exe", "created": "2026-08-18T09:00:00-05:00"}}

    def kill(pid):
        killed.append(pid)
        alive.pop(40, None)

    out = cxptt.restart(cfg, rows=LAUNCHED, kill=kill,
                        start=lambda p: (started.append(p) or (True, "")),
                        facts=_facts(alive))
    assert out["ok"] and out["restarted"] is True
    assert killed == [20]                       # the cmd root, not the python
    assert started and str(started[0]).endswith("Work-Channel.cmd")


def test_a_self_restarted_daemon_is_relaunched_through_the_launcher(cfg):
    """It was started as a bare python with no tee'd log. Restarting it back
    into that state would preserve the degradation instead of repairing it."""
    started = []
    alive = {40: {"image": "python.exe", "created": "2026-08-18T09:00:00-05:00"}}
    out = cxptt.restart(cfg, rows=RESTARTED, kill=lambda pid: alive.pop(40, None),
                        start=lambda p: (started.append(p) or (True, "")),
                        facts=_facts(alive))
    assert out["ok"] and out["killed"] == 40
    assert str(started[0]).endswith("Work-Channel.cmd")


def test_a_surviving_process_refuses_rather_than_starting_a_second_one(cfg):
    """taskkill reports success for a tree it only partly killed, so the kill
    is judged by the anchor. Starting a second daemon on a live one is how you
    get two hotkey listeners fighting over the same keys."""
    started = []
    alive = {40: {"image": "python.exe", "created": "2026-08-18T09:00:00-05:00"}}
    out = cxptt.restart(cfg, rows=LAUNCHED, kill=lambda pid: None,
                        start=lambda p: (started.append(p) or (True, "")),
                        facts=_facts(alive))
    assert out["ok"] is False
    assert "survived" in out["detail"] and started == []


def test_a_reused_pid_counts_as_dead_not_as_survived(cfg):
    """Same number, different process. Comparing pids alone would refuse a
    restart that actually worked."""
    started = []
    seen = []

    def facts(pid):
        seen.append(pid)
        # first call is the "before" snapshot, second is after the kill
        return ({"image": "python.exe", "created": "2026-08-18T09:00:00-05:00"}
                if len(seen) == 1
                else {"image": "chrome.exe", "created": "2026-08-18T09:30:00-05:00"})

    out = cxptt.restart(cfg, rows=LAUNCHED, kill=lambda pid: None,
                        start=lambda p: (started.append(p) or (True, "")),
                        facts=facts)
    assert out["ok"] is True and started


def test_nothing_running_starts_rather_than_claiming_a_restart(cfg):
    rows = [{"pid": 10, "ppid": 1, "name": "explorer.exe", "cmdline": "explorer.exe"}]
    out = cxptt.restart(cfg, rows=rows, start=lambda p: (True, ""))
    assert out["ok"] and out["started"] is True and out["restarted"] is False


def test_no_launcher_refuses_and_names_the_path(tmp_path):
    c = Config({"paths": {"base_gbl": str(tmp_path / "gbl")},
                "cx_ptt": {"launcher": str(tmp_path / "gone.cmd")}}, probe=StubProbe())
    out = cxptt.restart(c, rows=LAUNCHED)
    assert out["ok"] is False and "no launcher" in out["detail"]


# -- the published device list -----------------------------------------------
FRESH = ('{"ts": "2026-08-18T09:27:36.348260-05:00", "devices": {'
         '"Playback": [{"name": "Speakers", "id": "{p1}", "default": true}],'
         '"Recording": [{"name": "Mic", "id": "{r1}", "default": true}]}}')


def _at(iso):
    from datetime import datetime
    return lambda: datetime.fromisoformat(iso)


def test_the_device_list_is_read_from_the_published_file(cfg):
    got = cxptt.read_devices(cfg, read=lambda p: FRESH,
                             now=_at("2026-08-18T09:28:00-05:00"))
    assert got["available"] and got["stale"] is False
    assert [d["name"] for d in got["playback"]] == ["Speakers"]
    assert got["recording"][0]["default"] is True


def test_staleness_compares_instants_not_spellings(cfg):
    """This stack writes one instant as -0500 and as -05:00, and as strings
    the colon form sorts later. Three separate bugs came from that."""
    got = cxptt.read_devices(cfg, read=lambda p: FRESH,
                             now=_at("2026-08-18T14:28:00+00:00"))
    assert got["stale"] is False, "an equivalent instant in UTC read as stale"


def test_an_old_list_is_stale_not_absent(cfg):
    got = cxptt.read_devices(cfg, read=lambda p: FRESH,
                             now=_at("2026-08-18T10:30:00-05:00"))
    assert got["available"] is True and got["stale"] is True
    assert got["playback"], "a stale list still has its devices"


def test_a_missing_file_says_so_rather_than_showing_nothing(cfg):
    def boom(_p):
        raise OSError("not there")

    got = cxptt.read_devices(cfg, read=boom)
    assert got["available"] is False and "no device list" in got["detail"]
    assert got["playback"] == [] and got["recording"] == []


def test_a_corrupt_file_is_unavailable_not_a_crash(cfg):
    got = cxptt.read_devices(cfg, read=lambda p: "{ not json")
    assert got["available"] is False


# -- switching ---------------------------------------------------------------
def test_a_mic_switch_reports_that_it_needs_a_restart(cfg):
    out = cxptt.set_device(cfg, "{r1}", "mic", ps=lambda s: "")
    assert out["ok"] and out["needs_restart"] is True


def test_a_speaker_switch_does_not_restart_anything(cfg):
    out = cxptt.set_device(cfg, "{p1}", "speaker", ps=lambda s: "")
    assert out["ok"] and out["needs_restart"] is False


def test_a_failed_switch_never_chains_a_restart(cfg):
    """Restarting the daemon after a switch that did not happen would look
    like the hub broke dictation for no reason at all."""
    out = cxptt.set_device(cfg, "{r1}", "mic", ps=lambda s: None)
    assert out["ok"] is False and out.get("needs_restart") is None


def test_a_switch_with_no_id_refuses(cfg):
    assert cxptt.set_device(cfg, "", "mic", ps=lambda s: "")["ok"] is False


def test_an_unknown_kind_refuses(cfg):
    assert cxptt.set_device(cfg, "{r1}", "webcam", ps=lambda s: "")["ok"] is False


# -- capability gating -------------------------------------------------------
def test_a_machine_with_no_launcher_reports_absent_not_off(tmp_path):
    """Albert's Mac. Absent is a fact about the machine; off is a decision a
    human wrote down. Collapsing them has bitten this file twice already."""
    c = Config({"paths": {"base_gbl": str(tmp_path / "gbl")},
                "cx_ptt": {"launcher": str(tmp_path / "gone.cmd")}}, probe=StubProbe())
    assert capabilities.cx_restart(c)["state"] == capabilities.ABSENT


def test_a_machine_with_no_device_list_reports_absent(tmp_path):
    c = Config({"paths": {"base_gbl": str(tmp_path / "gbl")},
                "cx_ptt": {"devices_json": str(tmp_path / "gone.json")}},
               probe=StubProbe())
    assert capabilities.audio(c)["state"] == capabilities.ABSENT


def test_both_are_ready_when_the_machine_has_them(cfg, tmp_path):
    (tmp_path / "audio-devices.json").write_text(FRESH, encoding="utf-8")
    assert capabilities.cx_restart(cfg)["state"] == capabilities.READY
    assert capabilities.audio(cfg)["state"] == capabilities.READY


def test_a_human_switching_cx_off_reads_as_off(cfg, tmp_path):
    (tmp_path / "audio-devices.json").write_text(FRESH, encoding="utf-8")
    c = Config({"paths": {"base_gbl": str(tmp_path / "gbl")},
                "cx_ptt": {"enabled": False, "cx_toml": str(cfg.cx_ptt.cx_toml),
                           "launcher": str(cfg.cx_ptt.launcher),
                           "devices_json": str(tmp_path / "audio-devices.json")}},
               probe=StubProbe())
    assert capabilities.cx_restart(c)["state"] == capabilities.OFF
    assert capabilities.audio(c)["state"] == capabilities.OFF


def test_the_new_probes_are_in_the_capabilities_endpoint(cfg):
    assert {"cx_restart", "audio"} <= set(capabilities.probe_all(cfg))


# -- the wire ----------------------------------------------------------------
def _hub_html() -> str:
    from ping_hub import daemon
    return daemon.HTML.read_text(encoding="utf-8")


def test_the_audio_section_is_wired_and_lazy():
    """An absent wire looks exactly like a working one, and a section fetched
    on the critical path would undo this morning's settings fix."""
    html = _hub_html()
    h = html[html.index('getElementById("gear").onclick'):]
    assert "fillAudio(ov)" in h
    code = chr(10).join(l.split("//")[0] for l in h.splitlines())
    assert "/api/audio" not in code[:code.index("document.body.appendChild(ov)")]


def test_the_section_removes_itself_rather_than_rendering_dead_controls():
    html = _hub_html()
    fn = html[html.index("async function fillAudio"):]
    fn = fn[:fn.index("// ── resizable sidebar")]
    assert "host.remove()" in fn


def test_the_mic_switch_warns_before_it_restarts_the_daemon():
    """A hotkey daemon vanishing mid-dictation with no warning reads as a
    crash, not as the switch the user asked for."""
    html = _hub_html()
    fn = html[html.index("const switchTo = async"):]
    fn = fn[:fn.index("const spk =")]
    assert fn.index("restarting the hotkey daemon") < fn.index('fetch("/api/audio"')


def test_the_daemon_endpoints_exist():
    from pathlib import Path
    from ping_hub import daemon
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    assert '"/api/cx-restart"' in src and '"/api/audio"' in src
    assert "cxptt.restart(CFG)" in src and "cxptt.set_device(" in src
