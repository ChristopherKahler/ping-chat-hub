"""Finding the handoff a session should resume from.

Every case here comes from real rows in Chris's `base handoff list`, including
the two that break the obvious parses.
"""

from __future__ import annotations

import pytest

from ping_hub import handoffs as ho

# real shape, real rows
TABLE = """\
| slug | project | status | resurfaceAt |
|------|---------|--------|-------------|
| 2026-08-11-1730-raven-cx-cross-machine-bridge | cx-terminals | archived | x |
| 2026-08-11-1425-raven-cx-terminals | cx-terminals | open | x |
| 2026-08-17-0300-heron-ping-chat-hub | ping-chat-hub | open | x |
| 2026-08-17-0135-heron-ping-chat-hub | ping-chat-hub | archived | x |
| 2026-08-16-0356-finch-skyrim-companion | skyrim-companion | open | x |
"""


def rows():
    return ho.parse(TABLE)


def test_the_table_parses_and_skips_its_own_header():
    got = rows()
    assert len(got) == 5
    assert got[0]["project"] == "cx-terminals"
    assert {r["status"] for r in got} == {"open", "archived"}


def test_an_archived_handoff_is_never_offered():
    assert ho.for_session(rows(), "finch", []) ["slug"].endswith("skyrim-companion")
    # heron's archived 0135 must lose to the open 0300
    assert ho.for_session(rows(), "heron", [])["slug"] == "2026-08-17-0300-heron-ping-chat-hub"


def test_a_codename_match_beats_a_project_match():
    """Two sessions can share a project. Handing one the other's document
    would be a confident wrong answer."""
    got = ho.for_session(rows(), "raven", ["ping-chat-hub"])
    assert got["slug"] == "2026-08-11-1425-raven-cx-terminals"


def test_a_project_match_is_used_when_no_slug_names_the_codename():
    got = ho.for_session(rows(), "somebody-new", ["ping-chat-hub"])
    assert got["slug"] == "2026-08-17-0300-heron-ping-chat-hub"


def test_no_match_returns_nothing_rather_than_a_guess():
    assert ho.for_session(rows(), "stranger", ["unrelated-project"]) is None
    assert ho.for_session(rows(), "", []) is None


def test_the_topic_tail_does_not_break_matching():
    """`...-raven-cx-cross-machine-bridge` has project cx-terminals: the slug
    tail is a TOPIC, so stripping the project off the end finds nothing."""
    r = [x for x in rows() if "cross-machine" in x["slug"]][0]
    assert r["project"] == "cx-terminals"
    assert not r["slug"].endswith(r["project"])


def test_a_hyphenated_codename_still_matches():
    """Taking the token after the timestamp parses `hub-package-for-al-builder`
    to `hub`. Containment does not care."""
    table = TABLE + "| 2026-08-17-1500-hub-package-for-al-builder-notes | ping-chat-hub | open | x |\n"
    got = ho.for_session(ho.parse(table), "hub-package-for-al-builder", [])
    assert got["slug"].startswith("2026-08-17-1500-hub-package-for-al-builder")


def test_the_newest_wins_when_several_match():
    table = TABLE + "| 2026-08-18-0900-heron-ping-chat-hub | ping-chat-hub | open | x |\n"
    assert ho.for_session(ho.parse(table), "heron", [])["slug"].startswith("2026-08-18")


# ── what the modal says ──────────────────────────────────────────────────────
def test_a_found_handoff_is_named_not_just_announced():
    d = ho.describe(ho.for_session(rows(), "heron", []))
    assert d["found"] and "2026-08-17-0300-heron-ping-chat-hub" in d["detail"]


def test_a_project_match_says_so_in_the_headline():
    """It may be another session's document. Demonstrated live: this builder's
    codename matched heron's handoff through the shared project."""
    d = ho.describe(ho.for_session(rows(), "somebody-new", ["ping-chat-hub"]))
    assert d["via"] == "project"
    assert d["headline"] == "handoff ready (project match)"
    assert "shared project" in d["detail"]


def test_a_codename_match_is_not_labelled():
    d = ho.describe(ho.for_session(rows(), "heron", ["ping-chat-hub"]))
    assert d["via"] == "codename" and d["headline"] == "handoff ready"
    assert "shared project" not in d["detail"]


def test_absence_is_stated_plainly():
    d = ho.describe(None)
    assert d["found"] is False
    assert d["headline"] == "NO HANDOFF DETECTED"
    assert "no prior context" in d["detail"]


def test_a_failing_base_call_is_empty_not_fatal():
    def boom(*a, **k):
        raise OSError("base is not on PATH")
    assert ho.listing(run=boom) == []


def test_garbage_output_yields_no_rows():
    assert ho.parse("not a table at all") == []
    assert ho.parse("") == []
