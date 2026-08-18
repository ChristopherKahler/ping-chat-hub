"""The close-out-then-close instruction CLEAR sends a session with no handoff.

Two of these are tripwires rather than behaviour checks, and both guard a
failure that has already happened on this machine:

* a relay message carrying a literal star-command trigger fires the receiving
  session's hooks, attributed to Chris. It went off six-plus times on
  2026-08-18 and nearly closed a working builder, so the message this module
  builds must never contain one -- including the day someone "just quotes it
  for clarity";
* a wsl session handed the loopback form of the self-close would write its
  handoff and then fail to close, because WSL is NAT'd here and 127.0.0.1 is
  not the hub (measured: gateway answers 200, loopback refused).

``conftest.py`` has already pinned the config to a scratch store, so importing
the daemon here cannot touch the live one.
"""

from __future__ import annotations

import re

from ping_hub import daemon


def test_the_message_never_carries_a_literal_star_trigger():
    for side in ("win", "wsl"):
        msg = daemon.close_out_request("winterm4", side)
        hits = re.findall(r"\*[a-z]{2,}", msg)
        assert hits == [], f"{side}: literal trigger(s) in a relay message: {hits}"


def test_it_still_tells_the_session_which_ritual_to_run():
    msg = daemon.close_out_request("winterm4", "win")
    assert "star-end" in msg              # spelled, so a human still reads it
    assert "base commands show end" in msg  # and knows where the steps live


def test_windows_gets_the_loopback_form():
    msg = daemon.close_out_request("winterm4", "win", port=7799)
    assert "http://127.0.0.1:7799/api/close-session" in msg
    assert '"title": "winterm4"' in msg and '"side": "win"' in msg
    assert "ip route" not in msg


def test_wsl_gets_the_gateway_form_not_loopback():
    msg = daemon.close_out_request("wslterm2", "wsl", port=7799)
    assert "ip route show default" in msg
    assert ":7799/api/close-session" in msg
    assert '"side": "wsl"' in msg
    # the whole point: a wsl session must never be handed the loopback
    assert "127.0.0.1" not in msg


def test_the_order_is_stated_and_the_call_is_last():
    msg = daemon.close_out_request("winterm4", "win")
    assert msg.index("base commands show end") < msg.index("close-session")
    assert "after the handoff is on disk" in msg


def test_the_port_travels_from_config_not_a_hardcoded_number():
    assert ":9051/api/close-session" in daemon.close_out_request("x", "win", port=9051)


def test_the_endpoint_is_documented_in_the_module_header():
    # the header IS the endpoint list this daemon is read by
    assert "/api/end-then-close" in daemon.__doc__


def test_the_handler_confirms_before_it_asks():
    # the refusal is what keeps a session from writing a handoff and then
    # being told it cannot close: /api/end-then-close reaps nothing, but it
    # runs the same confirm the close would.
    src = daemon.__file__.replace(".pyc", ".py")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    block = body.split('elif u.path == "/api/end-then-close":', 1)[1]
    block = block.split('elif u.path in ("/api/clear"', 1)[0]
    assert "reap.confirm" in block and "409" in block
    assert "reap.reap" not in block      # it must not end anything itself


def test_the_modal_action_row_wraps():
    """The row can hold FOUR buttons once a session has no handoff, and
    `justify-content: flex-end` without wrap overflows to the LEFT: measured
    at 320px, Cancel sat at x=-39, off the screen rather than merely off the
    modal. `tools/clear_modal_probe.mjs` is the check that measures it; this
    is the hermetic guard that the declaration does not quietly go away."""
    html = daemon.HTML.read_text(encoding="utf-8")
    rule = re.search(r"#modal \.actions \{[^}]*\}", html).group(0)
    assert re.search(r"flex-wrap:\s*wrap", rule), rule


def test_the_close_out_button_is_offered_only_without_a_handoff():
    """A session that already has a handoff has nothing to write first, so the
    third way out is not offered — the button is gated on !h.found, not on
    canAct alone."""
    html = daemon.HTML.read_text(encoding="utf-8")
    line = [l for l in html.splitlines() if "do-endclose" in l and "<button" in l]
    assert line, "the close-out button is gone"
    assert "canAct && !h.found" in line[0], line[0]
