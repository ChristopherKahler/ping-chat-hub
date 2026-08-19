"""The terminal adapter seam, and login registration.

`build_command` is split out from the launch precisely so the argv can be
asserted without opening a terminal. Every assertion here corresponds to a bug
that was paid for once already in the inline version.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from ping_hub import autostart, spawn
from ping_hub.config import Config
from ping_hub.spawn import wt

from test_ping_hub_config import StubProbe, OPERATOR_LINUX_HOME


def _cfg(raw=None, **kw) -> Config:
    return Config(raw or {}, probe=StubProbe(**kw))


# ── the registry ─────────────────────────────────────────────────────────────
def test_wt_resolves():
    assert spawn.get("wt") is wt


def test_designed_but_unbuilt_adapters_say_so_by_name():
    """Never a silent fallback to wt: on a Mac that is a spawn failing with a
    confusing error from a program that is not installed."""
    for name in ("tmux", "iterm2", "terminal_app"):
        with pytest.raises(spawn.AdapterNotBuilt, match=name):
            spawn.get(name)


def test_unknown_adapter_lists_the_known_ones():
    with pytest.raises(spawn.AdapterNotBuilt, match="wt"):
        spawn.get("kitty")


# ── windows side ─────────────────────────────────────────────────────────────
def _decode(cmd: list[str]) -> str:
    return base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")


def test_windows_tab_carries_the_pinned_codename_and_args():
    cmd = wt.build_command(_cfg(), "win", ["--model", "opus[1m]"], None,
                           "my-builder", None)
    assert cmd[:5] == ["wt", "-w", "0", "new-tab", "-p"]
    assert cmd[5] == "PowerShell"
    s = _decode(cmd)
    assert "$env:BASE_RELAY_AS='my-builder'" in s
    assert "claude --model opus[1m]" in s
    assert "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE='1'" in s


def test_a_prompt_travels_base64_with_quotes_pre_escaped():
    """PS 5.1 passes embedded double quotes to native exes UNESCAPED, so
    claude's argv split at the first " and dropped the rest of the briefing."""
    prompt = 'read "the doc" and go'
    s = _decode(wt.build_command(_cfg(), "win", [], None, None, prompt))
    assert "FromBase64String" in s and "-replace" in s
    inner = base64.b64decode(s.split("FromBase64String(")[1].split("'")[1])
    assert inner.decode() == prompt          # survives the trip intact


def test_an_invalid_codename_is_dropped_not_injected():
    s = _decode(wt.build_command(_cfg(), "win", [], None, "bad name; rm -rf", None))
    assert "BASE_RELAY_AS" not in s


# ── wsl side ─────────────────────────────────────────────────────────────────
def _pin_unc(monkeypatch, cfg, value: str) -> None:
    """Pin the UNC rendering only. monkeypatch, never a raw class assignment:
    deleting a patched property removes the real one for the whole session."""
    monkeypatch.setattr(type(cfg.wsl), "bridge_deploy_unc",
                        property(lambda self: value))


def test_wsl_spawn_writes_a_script_and_runs_that_same_file(tmp_path, monkeypatch):
    """One config key renders as a UNC path to write and a Linux path to
    execute. If they disagree the tab opens on a file that is not there."""
    cfg = _cfg({"wsl": {"bridge_deploy": "~/.local/share/hub-bridge"}})
    lin = f"{OPERATOR_LINUX_HOME}/.local/share/hub-bridge"
    _pin_unc(monkeypatch, cfg, str(tmp_path))
    cmd = wt.build_command(cfg, "wsl", ["--resume", "abc"], None, "wt1", "go")
    written = list(tmp_path.glob("spawn-*.sh"))
    assert len(written) == 1
    assert cmd[-1] == f"{lin}/{written[0].name}"
    body = written[0].read_text(encoding="utf-8")
    assert "export BASE_RELAY_AS='wt1'" in body
    assert "exec claude --resume abc 'go'" in body
    assert written[0].read_bytes().count(b"\r\n") == 0       # LF only, for bash


def test_the_child_never_inherits_the_daemons_tab_identity(monkeypatch, tmp_path):
    """WT_SESSION identifies a Windows Terminal tab, and base binds titles
    partly by it. Inherited, a spawned session presents the HUB's tab identity
    and reclaims the title bound to it — observed live as a rebooted `heron`
    coming back as `chris`, carrying the hub's wt_session and the original
    registered_at because it was reclaimed rather than created."""
    seen = {}
    monkeypatch.setenv("WT_SESSION", "dfb241bb-692a-4102-8dec-edfe44317941")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "should-not-travel")
    monkeypatch.setattr(wt, "launch", lambda cmd, env, **k: seen.update(env=env))
    cfg = _cfg()
    wt.spawn(cfg, "win", ["--help"], None, "heron", None)
    assert "WT_SESSION" not in seen["env"]
    assert "CLAUDE_CODE_CHILD_SESSION" not in seen["env"]
    assert seen["env"]["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] == "1"
    assert "PATH" in seen["env"]      # the rest of the environment still travels


def test_wsl_spawn_on_a_machine_with_no_wsl_refuses_loudly():
    cfg = _cfg(distro="", wsl_home="")
    with pytest.raises(OSError, match="no WSL side"):
        wt.build_command(cfg, "wsl", [], None, None, None)


def test_no_profile_omits_the_flag_rather_than_passing_an_empty_one(tmp_path,
                                                                    monkeypatch):
    """`wt -p ""` is not the same as leaving -p off; the empty form fails."""
    cfg = _cfg(files={})                       # no cx.toml -> no GUID
    _pin_unc(monkeypatch, cfg, str(tmp_path))
    cmd = wt.build_command(cfg, "wsl", [], None, None, None)
    assert "-p" not in cmd
    assert "" not in cmd


# ── autostart ────────────────────────────────────────────────────────────────
def test_plan_includes_the_speech_server_only_when_it_was_provisioned():
    bare = _cfg()
    assert [n for n, _ in autostart.plan(bare)] == [autostart.HUB_TASK]
    full = _cfg({"stt": {"launcher": ["C:/x/start-stt.cmd"]}})
    assert [n for n, _ in autostart.plan(full)] == [autostart.HUB_TASK,
                                                    autostart.STT_TASK]


def test_autostart_is_disabled_by_a_human_not_by_absence():
    off = _cfg({"stt": {"launcher": ["C:/x/start-stt.cmd"], "autostart": False}})
    assert [n for n, _ in autostart.plan(off)] == [autostart.HUB_TASK]


# ── cx-ptt: the last piece of this app a human had to start by hand ─────────
from test_ping_hub_config import (CX_SLOT_PATH, CX_TOML_PATH,  # noqa: E402
                                   CX_TOML_TEXT, OPERATOR_HOME)

# built the way the resolver builds it, so the separator matches whichever
# platform is running the suite
CXPTT_CMD = str(Path(OPERATOR_HOME) / "Desktop" / "Work-Channel.cmd")
WITH_LAUNCHER = {CX_TOML_PATH: CX_TOML_TEXT, CX_SLOT_PATH: "# cx-slot",
                 CXPTT_CMD: "@echo off"}


def test_cx_ptt_gets_a_logon_task_like_the_speech_server():
    cfg = _cfg(files=WITH_LAUNCHER)
    names = [n for n, _ in autostart.plan(cfg)]
    assert autostart.CXPTT_TASK in names
    cmd = dict(autostart.plan(cfg))[autostart.CXPTT_TASK]
    assert cmd == [CXPTT_CMD]


def test_no_launcher_on_disk_means_no_task_at_all():
    """`cx_ptt.launcher` DERIVES a path whether or not anything is there. A
    logon task pointing at a missing .cmd fails silently every boot, which
    looks exactly like the outage this fork exists to end."""
    cfg = _cfg()                      # cx.toml present, no Work-Channel.cmd
    assert autostart.CXPTT_TASK not in [n for n, _ in autostart.plan(cfg)]


def test_a_human_can_switch_the_cx_ptt_task_off():
    cfg = _cfg({"cx_ptt": {"autostart": False}}, files=WITH_LAUNCHER)
    assert autostart.CXPTT_TASK not in [n for n, _ in autostart.plan(cfg)]


def test_the_interim_run_key_is_removed_only_after_its_replacement_answers():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _Ok()
    lines = autostart.supersede_interim_run_key(
        _cfg(files=WITH_LAUNCHER), run=run, platform="win")
    kinds = [c[0] for c in calls]
    assert kinds == ["schtasks", "reg", "reg"]      # query, query, delete
    assert calls[0][1] == "/query" and calls[0][3] == autostart.CXPTT_TASK
    assert calls[2][:2] == ["reg", "delete"] and "/f" in calls[2]
    assert any("removed" in l for l in lines)


def test_a_task_that_did_not_register_leaves_the_run_key_alone():
    """Removing the old mechanism first would leave the machine with NEITHER,
    and the thing they both start is the one that already went missing for
    sixteen hours."""
    class Fail:
        returncode = 1
        stdout = stderr = ""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return Fail()
    lines = autostart.supersede_interim_run_key(
        _cfg(files=WITH_LAUNCHER), run=run, platform="win")
    assert [c[0] for c in calls] == ["schtasks"]    # it never reached reg
    assert any("LEFT IN PLACE" in l for l in lines)


def test_nothing_to_supersede_when_there_is_no_cx_ptt_task():
    calls = []
    lines = autostart.supersede_interim_run_key(
        _cfg(), run=lambda *a, **k: calls.append(a) or _Ok(), platform="win")
    assert calls == []
    assert any("no ping-chat-hub-cxptt" in l for l in lines)


def test_the_run_key_is_a_windows_notion_only():
    calls = []
    assert autostart.supersede_interim_run_key(
        _cfg(files=WITH_LAUNCHER),
        run=lambda *a, **k: calls.append(a) or _Ok(), platform="mac") == []
    assert calls == []


def test_dry_run_registers_nothing_but_shows_the_command():
    calls = []
    lines = autostart.register(_cfg(), dry_run=True,
                               run=lambda *a, **k: calls.append(a),
                               platform="win")
    assert calls == []
    assert any("schtasks" in l and "onlogon" in l for l in lines)


def test_task_command_quotes_paths_with_spaces():
    cmd = autostart._schtasks_create("t", [r"C:\Program Files\py.exe", "-m", "x"])
    tr = cmd[cmd.index("/tr") + 1]
    assert '"C:\\Program Files\\py.exe"' in tr
    assert tr.endswith("-m x")


def test_mac_writes_a_launch_agent_without_touching_schtasks(tmp_path):
    lines = autostart.register(_cfg(), dry_run=False, run=lambda *a, **k: _Ok(),
                               platform="mac", home=tmp_path)
    p = tmp_path / "Library" / "LaunchAgents" / "cv.chrisai.ping-chat-hub.plist"
    assert p.is_file()
    assert "<key>RunAtLoad</key><true/>" in p.read_text(encoding="utf-8")
    assert any("LaunchAgents" in l for l in lines)


def test_install_actually_calls_autostart():
    """It was built, tested, and then imported by nothing — a feature one line
    from working, shipped dead. This test is the wire itself."""
    from ping_hub import cli
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert "from ping_hub import autostart" in src
    assert "register_autostart(config.get()" in src


def test_login_registration_is_on_by_default_and_opt_out():
    from ping_hub import cli
    calls = []
    lines = cli.register_autostart(_cfg(), skip=False, platform="win",
                                   run=lambda *a, **k: calls.append(a[0]) or _Ok())
    assert calls and "schtasks" in calls[0][0]
    assert any("onlogon" in l for l in lines)
    calls.clear()
    assert cli.register_autostart(_cfg(), skip=True) == ["skipped (--no-autostart)"]
    assert calls == []


def test_a_failed_task_registration_does_not_lose_a_good_install():
    from ping_hub import cli

    def boom(*a, **k):
        raise OSError("access denied")

    lines = cli.register_autostart(_cfg(), platform="win", run=boom)
    assert any("could not register" in l for l in lines)
    assert any("ping-hub serve" in l for l in lines)


def test_status_reports_absent_rather_than_failing(tmp_path):
    got = autostart.status(_cfg(), platform="mac", home=tmp_path)
    assert got == {autostart.HUB_TASK: "absent"}


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""
