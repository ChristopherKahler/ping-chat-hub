"""A spawned session's first turn, and why its order is load-bearing.

Measured on `kestrel`, 2026-08-19: 28 transcript records, ONE assistant turn,
ZERO tool calls. Something in its context told it to render a lettered
"pick up where you left off" menu as the first thing in its reply, and it did
exactly that and stopped. So it never registered a status, never armed a wake
monitor, and never reported in — and two of Chris's pings sat unread in its
inbox with nothing watching to deliver them. From the hub the card looked
fine. A session that cannot be woken is gone, whatever the card says.
"""

from __future__ import annotations

from ping_hub import daemon


def _spawn_source() -> str:
    """Just the /api/spawn branch. `/api/replacements` appears TWICE in this
    module (a GET above, a POST below), so the end marker is searched for
    from the start of the branch, not from the top of the file."""
    from pathlib import Path
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    start = src.index('elif u.path == "/api/spawn":')
    end = src.index('elif u.path == "/api/replacements":', start)
    return src[start:end]


def test_every_spawn_is_told_to_report_in_before_it_renders_anything():
    body = _spawn_source()
    assert "FIRST TURN" in body
    first = body.index("FIRST TURN")
    assert first < body.index("NO QUESTION DIALOGS"), \
        "the boot duties have to be the first block the child reads"


def test_the_menu_that_hijacked_the_boot_is_named_explicitly():
    """Naming it is the point. A generic 'report in promptly' loses to a
    specific 'render this list as the FIRST thing in your reply'."""
    body = _spawn_source()
    assert "pick up where you left off" in body


def test_the_child_is_told_to_make_a_tool_call_at_all():
    """The wake contract arrives as a system reminder on the FIRST tool call.
    A turn with no tool calls never sees it, which is how a session ends up
    with no waker at all."""
    body = _spawn_source()
    assert "any tool call" in body
    assert ".status" in body


def test_the_boot_duties_reach_a_plain_spawn_not_only_a_gated_one():
    """Gated builds already carried a wake contract; plain spawns carried
    none, and plain spawns are what Chris boots from his phone."""
    body = _spawn_source()
    duties = body.index("FIRST TURN")
    assert duties < body.index("if gated:"), \
        "the duties must be built before the gated branch, or they only ship " \
        "to gated children"
