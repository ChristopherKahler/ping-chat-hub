"""The personal inbox.

Two things carry risk and get tests: a delete must remove exactly the note that
was asked for (the file has no undo), and a word fix typed into the inbox has to
reach the LIBRARY rather than being kept as a note — that precedence is the
whole dictation drill.
"""

from __future__ import annotations

import pytest

from ping_hub import inbox
from ping_hub import replacements as rep
from ping_hub.config import Config

from test_ping_hub_config import StubProbe


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config({"paths": {"base_gbl": str(tmp_path / "gbl")}}, probe=StubProbe())


def test_nothing_is_kept_for_an_empty_note(cfg):
    assert inbox.add(cfg, "   ") is None
    assert inbox.add(cfg, "") is None
    assert inbox.read(cfg) == []


def test_notes_come_back_newest_first(cfg):
    for t in ("first", "second", "third"):
        inbox.add(cfg, t)
    assert [n["text"] for n in inbox.read(cfg)] == ["third", "second", "first"]


def test_a_note_counts_its_words(cfg):
    assert inbox.add(cfg, "one two three")["words"] == 3


def test_a_torn_last_line_is_skipped_not_fatal(cfg):
    inbox.add(cfg, "kept")
    with open(inbox.path(cfg), "a", encoding="utf-8") as fh:
        fh.write("half a line, still being written")
    assert [n["text"] for n in inbox.read(cfg)] == ["kept"]


def test_delete_removes_exactly_one(cfg):
    a = inbox.add(cfg, "alpha")
    inbox.add(cfg, "beta")
    assert inbox.remove(cfg, a["ts"], a["text"]) == 1
    assert [n["text"] for n in inbox.read(cfg)] == ["beta"]


def test_two_notes_in_the_same_second_delete_one_at_a_time(cfg):
    """Timestamps are second-resolution, so a tie is ordinary, not exotic. A
    delete that matched on the stamp alone would take both."""
    a = inbox.add(cfg, "same second one")
    b = inbox.add(cfg, "same second two")
    if a["ts"] != b["ts"]:                    # force the tie the field allows
        rows = inbox.path(cfg).read_text(encoding="utf-8").replace(b["ts"], a["ts"])
        inbox.path(cfg).write_text(rows, encoding="utf-8")
        b = dict(b, ts=a["ts"])
    assert inbox.remove(cfg, a["ts"], a["text"]) == 1
    left = [n["text"] for n in inbox.read(cfg)]
    assert left == ["same second two"]


def test_deleting_something_absent_changes_nothing(cfg):
    inbox.add(cfg, "only")
    assert inbox.remove(cfg, "2020-01-01T00:00:00", "gone") == 0
    assert len(inbox.read(cfg)) == 1


def test_deleting_the_last_note_leaves_a_readable_file(cfg):
    a = inbox.add(cfg, "only")
    assert inbox.remove(cfg, a["ts"], a["text"]) == 1
    assert inbox.read(cfg) == []


def test_a_word_fix_beats_a_note(cfg):
    """The drill: say a rule into the inbox and it files, it is not filed away.

    This precedence lives in the route, so it is asserted the way the route
    does it — rule first, note only if that returns None.
    """
    msg = "head list=headless*"
    rule = rep.consume_rule(cfg, msg)
    assert rule and rule["ok"] and rule["action"] == "added"
    assert inbox.read(cfg) == []                       # nothing was kept
    assert rep.apply_for(cfg, "run it head list") == "run it headless"

    plain = "just a thought worth keeping"
    assert rep.consume_rule(cfg, plain) is None
    inbox.add(cfg, plain)
    assert [n["text"] for n in inbox.read(cfg)] == [plain]
