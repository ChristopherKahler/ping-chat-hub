"""Cross-side relay mirrors.

A session can register a title on the other side so pings route to it, without
running there. That produces two cards for one session, and the second one has
nothing behind it: no transcript, no model, no context. Left alone it reads as a
session that has gone quiet, which is a thing worth worrying about — and this
one is not. It is plumbing.

The rule is narrow on purpose: same session id, both sides, exactly one side
with evidence. Everything else is left alone, because graying out a real card
is a worse failure than missing a ghost.
"""

from __future__ import annotations

import pytest

from ping_hub.engine import Engine


@pytest.fixture
def eng() -> Engine:
    e = Engine.__new__(Engine)          # no store, no threads, no filesystem
    e.threads = {}
    e.side = "win"
    return e


def card(side, title, sid, **evidence):
    return {"side": side, "title": title, "session_id": sid, **evidence}


def test_the_side_without_a_transcript_is_the_ghost(eng):
    eng.threads = {
        "win:heron": card("win", "heron", "abc", model="opus", ctx=42000),
        "wsl:heron": card("wsl", "heron", "abc"),
    }
    eng.mark_ghosts()
    assert eng.threads["wsl:heron"]["ghost"] is True
    assert eng.threads["win:heron"]["ghost"] is False


def test_it_works_in_either_direction(eng):
    eng.threads = {
        "win:orca": card("win", "orca", "xyz"),
        "wsl:orca": card("wsl", "orca", "xyz", doing="running Grep"),
    }
    eng.mark_ghosts()
    assert eng.threads["win:orca"]["ghost"] is True
    assert eng.threads["wsl:orca"]["ghost"] is False


@pytest.mark.parametrize("evidence", [
    {"model": "sonnet"}, {"ctx": 1}, {"doing": "thinking"},
    {"label": "fixing the parser"},
])
def test_any_single_trace_counts_as_a_real_session(eng, evidence):
    eng.threads = {"win:a": card("win", "a", "s", **evidence),
                   "wsl:a": card("wsl", "a", "s")}
    eng.mark_ghosts()
    assert eng.threads["wsl:a"]["ghost"] is True


# ── everything the rule deliberately refuses to guess about ─────────────────
def test_a_session_on_one_side_only_is_never_a_ghost(eng):
    eng.threads = {"win:solo": card("win", "solo", "s1")}
    eng.mark_ghosts()
    assert eng.threads["win:solo"]["ghost"] is False


def test_two_evidenced_sides_are_two_real_sessions(eng):
    eng.threads = {"win:x": card("win", "x", "s", model="opus"),
                   "wsl:x": card("wsl", "x", "s", model="sonnet")}
    eng.mark_ghosts()
    assert not any(t["ghost"] for t in eng.threads.values())


def test_neither_side_evidenced_leaves_both_alone(eng):
    """Nothing distinguishes them, so flagging one would be a coin toss that
    grays out a real card half the time."""
    eng.threads = {"win:y": card("win", "y", "s"), "wsl:y": card("wsl", "y", "s")}
    eng.mark_ghosts()
    assert not any(t["ghost"] for t in eng.threads.values())


def test_two_cards_on_the_SAME_side_are_not_mirrors(eng):
    """A mirror is by definition cross-side. Same-side duplicates are a
    different problem and must not be silently grayed."""
    eng.threads = {"win:a": card("win", "a", "s", model="opus"),
                   "win:b": card("win", "b", "s")}
    eng.mark_ghosts()
    assert not any(t["ghost"] for t in eng.threads.values())


def test_cards_without_a_session_id_are_ignored(eng):
    """An empty id is not a shared id — grouping on it would ghost every
    unregistered card at once."""
    eng.threads = {"win:a": card("win", "a", "", model="opus"),
                   "wsl:b": card("wsl", "b", "")}
    eng.mark_ghosts()
    assert not any(t["ghost"] for t in eng.threads.values())


def test_the_flag_clears_when_the_mirror_becomes_real(eng):
    """A stale true would keep a live session looking like plumbing."""
    eng.threads = {"win:z": card("win", "z", "s", model="opus"),
                   "wsl:z": card("wsl", "z", "s")}
    eng.mark_ghosts()
    assert eng.threads["wsl:z"]["ghost"] is True
    eng.threads["wsl:z"]["model"] = "sonnet"          # it started running
    eng.mark_ghosts()
    assert eng.threads["wsl:z"]["ghost"] is False


def test_every_card_carries_the_field_so_the_ui_never_guesses(eng):
    eng.threads = {"win:a": card("win", "a", "s1", model="opus"),
                   "wsl:a": card("wsl", "a", "s1"),
                   "win:b": card("win", "b", "s2")}
    eng.mark_ghosts()
    assert all("ghost" in t for t in eng.threads.values())
