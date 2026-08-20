"""The desktop mic's half of the hub.

Two things here are worth a test and the rest is JSON plumbing:

  1. The hotkey a human can type has to be the hotkey dictate can register.
     Two programs parse that string and they must agree, so both tables are
     checked against each other rather than each against itself.
  2. Words per minute has exactly one correct definition and an obvious wrong
     one that looks right (mean of the per-take rates). The wrong one is
     pinned here so a "simplification" cannot quietly install it.
"""

from __future__ import annotations

import json

import pytest

from ping_hub import desktop_stt as ds
from ping_hub import replacements as rep
from ping_hub.config import Config

from test_ping_hub_config import StubProbe


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config({"paths": {"base_gbl": str(tmp_path / "gbl")}}, probe=StubProbe())


# ── hotkeys ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("combo,key,vk", [
    ("ctrl+alt+d", "d", 0x44),
    ("CTRL+ALT+D", "d", 0x44),
    ("ctrl+shift+space", "space", 0x20),
    ("alt+f9", "f9", 0x78),
    ("ctrl+alt+7", "7", 0x37),
])
def test_parse_hotkey_accepts(combo, key, vk):
    out = ds.parse_hotkey(combo)
    assert out["ok"] and out["key"] == key and out["vk"] == vk


@pytest.mark.parametrize("combo", [
    "d",                 # bare key: would fire inside every text box
    "ctrl+alt",          # modifiers only
    "ctrl+alt+d+e",      # two main keys
    "ctrl+alt+scrolllock",   # no VK for it
    "", "   ", "+++",
])
def test_parse_hotkey_refuses(combo):
    assert ds.parse_hotkey(combo)["ok"] is False


def test_hotkey_tables_agree_with_the_daemon():
    """The app validates; dictate binds. A combo one accepts and the other
    refuses is a hotkey that saves, restarts, and then does nothing."""
    import importlib.util
    from pathlib import Path
    for tools in ("Tools", "tools"):
        p = Path.home() / tools / "stt" / "stt_hubcfg.py"
        if p.exists():
            break
    else:
        pytest.skip("dictate side not installed on this machine")
    spec = importlib.util.spec_from_file_location("stt_hubcfg", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for combo in ("ctrl+alt+d", "ctrl+shift+space", "alt+f9", "ctrl+alt+7",
                  "shift+alt+end", "ctrl+alt+pageup"):
        here, there = ds.parse_hotkey(combo), mod.parse_hotkey(combo)
        assert here["ok"], combo
        assert there is not None, combo
        assert here["vk"] == there["vk"], combo
        assert here["canonical"] == there["canonical"], combo
    for bad in ("d", "ctrl+alt", "ctrl+alt+d+e", ""):
        assert ds.parse_hotkey(bad)["ok"] is False
        assert mod.parse_hotkey(bad) is None


# ── settings store ───────────────────────────────────────────────────────────
def test_defaults_when_no_store(cfg):
    assert ds.load(cfg)["hotkey"] == "ctrl+alt+d"
    assert ds.load(cfg)["mode"] == "tap"


def test_save_round_trip_and_canonical_order(cfg):
    doc = ds.save(cfg, {"hotkey": "ALT+CTRL+D", "mode": "hold", "cleanup": False})
    # modifiers come back in one fixed order, so the string the app shows and
    # the string the daemon logs are the same string
    assert doc["hotkey"] == "ctrl+alt+d"
    assert doc["mode"] == "hold" and doc["cleanup"] is False
    assert ds.load(cfg)["mode"] == "hold"


def test_a_broken_hotkey_never_reaches_the_store(cfg):
    doc = ds.save(cfg, {"hotkey": "ctrl+alt+scrolllock"})
    assert doc["hotkey"] == "ctrl+alt+d"     # the default, not the broken combo


def test_corrupt_store_reads_as_defaults(cfg):
    p = ds.store_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert ds.load(cfg)["hotkey"] == "ctrl+alt+d"


# ── history + words per minute ───────────────────────────────────────────────
def _hist(cfg, rows):
    p = ds.history_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def test_history_is_newest_first_and_skips_junk(cfg):
    _hist(cfg, [{"ts": "2026-08-20T09:00:00", "text": "one", "words": 1, "seconds": 1}])
    with open(ds.history_path(cfg), "a", encoding="utf-8") as fh:
        fh.write("half a line, no newline yet")
    rows = ds.read_history(cfg)
    assert [r["text"] for r in rows] == ["one"]


def test_wpm_is_total_words_over_total_time(cfg):
    """A four-word take and a four-hundred-word take are not equal evidence.

    Averaging the per-take rates would make them so: 200wpm and 40wpm would
    report 120, when the actual rate across both is far closer to 200.
    """
    entries = [
        {"ts": "2026-08-20T09:00:00", "text": "x", "words": 400, "seconds": 120},
        {"ts": "2026-08-20T09:05:00", "text": "y", "words": 4, "seconds": 6},
    ]
    out = ds.stats(entries, now=__import__("datetime").datetime(2026, 8, 20, 10))
    assert out["words"] == 404
    assert out["wpm"] == pytest.approx(404 / (126 / 60), abs=0.1)
    mean_of_rates = (200 + 40) / 2
    assert abs(out["wpm"] - mean_of_rates) > 50      # not the wrong definition


def test_recent_window_excludes_old_takes(cfg):
    import datetime as dt
    now = dt.datetime(2026, 8, 20, 10)
    entries = [
        {"ts": "2026-08-19T09:00:00", "text": "new", "words": 100, "seconds": 30},
        {"ts": "2026-01-01T09:00:00", "text": "old", "words": 100, "seconds": 120},
    ]
    out = ds.stats(entries, days=7, now=now)
    assert out["recent_takes"] == 1
    assert out["recent_wpm"] == pytest.approx(200.0, abs=0.1)
    assert out["takes"] == 2                    # the lifetime count still counts both


# ── inline replacement rules ─────────────────────────────────────────────────
@pytest.mark.parametrize("text,frm,to", [
    ("head list=headless*", "head list", "headless"),
    ("  head list = headless *  ", "head list", "headless"),
    ("FilePalette=File Pilot*", "FilePalette", "File Pilot"),
    ("um=*", "um", ""),                       # deleting a filler word
])
def test_parse_rule_accepts(text, frm, to):
    assert rep.parse_rule(text) == {"from": frm, "to": to}


@pytest.mark.parametrize("text", [
    "head list=headless",                     # no marker
    "the total=12* and then some more",       # not the whole message
    "just a sentence*",                       # no =
    "=headless*",                             # nothing on the left
    "line one\nhead list=headless*",          # a rule buried in a paragraph
])
def test_parse_rule_refuses(text):
    assert rep.parse_rule(text) is None


def test_add_rule_appends_then_updates(cfg):
    first = rep.add_rule(cfg, "head list", "headless")
    assert first["action"] == "added"
    again = rep.add_rule(cfg, "HEAD LIST", "headless!")
    assert again["action"] == "updated" and again["was"] == "headless"
    pairs = rep.load(cfg)["pairs"]
    assert len(pairs) == 1                    # not two rules for one phrase
    assert pairs[0]["to"] == "headless!"


def test_updating_re_arms_a_disabled_rule(cfg):
    rep.add_rule(cfg, "head list", "headless")
    doc = rep.load(cfg)
    doc["pairs"][0]["enabled"] = False
    rep.save(cfg, doc)
    rep.add_rule(cfg, "head list", "headless")
    assert rep.load(cfg)["pairs"][0]["enabled"] is True


def test_the_rule_then_applies_to_speech(cfg):
    """The whole point: say the rule, and the next transcript is fixed."""
    rep.consume_rule(cfg, "head list=headless*")
    assert rep.apply_for(cfg, "run it head list") == "run it headless"


def test_rule_matching_matches_the_daemons(cfg):
    """stt_fixups.py carries its own copy of this regex. If they disagree, the
    same sentence is a rule on one mic and a paste on the other."""
    import importlib.util
    from pathlib import Path
    for tools in ("Tools", "tools"):
        p = Path.home() / tools / "stt" / "stt_fixups.py"
        if p.exists():
            break
    else:
        pytest.skip("dictate side not installed on this machine")
    spec = importlib.util.spec_from_file_location("stt_fixups", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for text in ("head list=headless*", "  a = b *", "um=*",
                 "not a rule", "a=b", "x=y* trailing words", "=b*"):
        assert rep.parse_rule(text) == mod.parse_rule(text), text


# ── daemon status ────────────────────────────────────────────────────────────
def test_status_reports_the_registered_hotkey_not_the_requested_one(cfg):
    """The store can ask for anything; RegisterHotKey is what decides."""
    ds.save(cfg, {"hotkey": "ctrl+alt+d"})
    sp = ds.state_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"pid": 4242, "hotkey": "ctrl+alt+j",
                              "registered": True, "method": "native"}),
                  encoding="utf-8")
    rows = [{"pid": 4242, "ppid": 1, "name": "pythonw.exe",
             "cmdline": r"pythonw.exe C:\x\dictate.py"}]
    out = ds.status(cfg, rows=rows)
    assert out["running"] is True
    assert out["settings"]["hotkey"] == "ctrl+alt+d"
    assert out["hotkey_live"] == "ctrl+alt+j"
    assert out["pending_restart"] is True      # asked for one, bound another


def test_state_from_a_dead_process_is_not_reported_as_live(cfg):
    sp = ds.state_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"pid": 111, "hotkey": "ctrl+alt+d",
                              "registered": True}), encoding="utf-8")
    out = ds.status(cfg, rows=[])
    assert out["running"] is False
    assert out["hotkey_live"] is None and out["state"] == {}


def test_status_ignores_state_left_by_a_previous_life(cfg):
    sp = ds.state_path(cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"pid": 111, "hotkey": "ctrl+alt+d",
                              "registered": True}), encoding="utf-8")
    rows = [{"pid": 999, "ppid": 1, "name": "pythonw.exe",
             "cmdline": r"pythonw.exe C:\x\dictate.py"}]
    out = ds.status(cfg, rows=rows)
    assert out["running"] is True
    assert out["hotkey_live"] is None          # that state was another process's


def test_restart_goes_through_the_launcher(cfg):
    seen = {}
    rows = [{"pid": 7, "ppid": 1, "name": "pythonw.exe",
             "cmdline": r"pythonw.exe C:\x\dictate.py"}]
    out = ds.restart(cfg, rows=rows,
                     kill=lambda pid: seen.setdefault("killed", pid),
                     start=lambda c: (True, "started"))
    assert seen["killed"] == 7
    assert out["ok"] and out["killed"] == 7
