"""The booting card, and the silence it used to disappear into.

Measured 2026-08-19: a terminal launched from the hub showed "booting", then
vanished from the list with no trace and no chat thread. The cause was one line
in `loadRoster` — `if (Date.now() > spawnWatch.until) spawnWatch = null;` —
which threw the card away 180 seconds after a spawn the hub could not confirm.
It could not confirm it because confirmation arrives on the roster, the wsl
roster arrives only through the bridge, and the bridge was down. So the one
moment the card was the only evidence a session existed was the exact moment it
was deleted.

Card lifecycle is page logic, so it is asserted the way this repo already
asserts page logic (see test_ping_hub_accordion.py): against the source, with
the comments stripped, so a comment quoting a selector can never satisfy an
assertion about the selector.

These prove the branches EXIST. That they render is measured for real by
`tools/bridge_card_probe.mjs`, which drives a headless browser against a live
hub and walks the whole outage: banner appears, expired wsl card survives it,
card resolves when the bridge returns, and an unconfirmed spawn fails visibly
and waits for a human. Run it after touching any of this.
"""

from __future__ import annotations

from ping_hub import daemon


def code() -> str:
    """The page script with comments stripped — the comments here quote the
    very branches being asserted."""
    src = daemon.HTML.read_text(encoding="utf-8")
    src = src[src.index("<script"):]
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def test_a_timed_out_spawn_is_never_silently_dropped():
    """THE regression. The timeout branch must record a state, not delete the
    evidence. If this line comes back, a WSL terminal can boot into nothing
    again and the hub will say nothing at all."""
    c = code()
    assert "if (Date.now() > spawnWatch.until) spawnWatch = null;" not in c
    assert 'else if (Date.now() > spawnWatch.until) spawnWatch.state = "failed";' in c


def test_the_deadline_is_suspended_while_the_confirming_channel_is_dead():
    """A wsl spawn is confirmed by the roster and the wsl roster travels only
    over the bridge. With the bridge down the 180s deadline proves nothing —
    counting it down would hide a session that is very probably running."""
    c = code()
    assert 'const blind = spawnWatch.side === "wsl" && bridgeState.enabled &&' in c
    assert 'bridgeState.probed && !bridgeState.up;' in c
    assert 'if (blind) { spawnWatch.state = "waiting"; spawnWatch.until = Date.now() + 180000; }' in c


def test_waiting_and_failed_stay_two_different_facts():
    """One says the channel that would confirm this is dead; the other says the
    channel was alive and nothing arrived. They need different fixes, so they
    never collapse into one card."""
    c = code()
    assert '"waiting on bridge…"' in c
    assert '"spawn unconfirmed"' in c
    assert '"booting…"' in c


def test_only_a_human_removes_a_card_that_reached_a_terminal_state():
    """The whole fix in one assertion: a timer may no longer decide that a
    spawn stopped mattering."""
    c = code()
    assert '#spawndismiss' in c, "no dismiss control on the card"
    handler = [l for l in c.splitlines() if "d.onclick" in l]
    assert handler and "spawnWatch = null" in handler[0]


def test_a_still_pending_card_keeps_trying_to_resolve_itself():
    """A waiting or failed card must still match a session that shows up late —
    the state machine may not stop the roster match from running, or a card
    would sit failed next to the very session it was waiting for."""
    c = code()
    body = c[c.index("async function loadRoster"):c.index("  renderList();")]
    assert 'spawnWatch.state = "failed"' in body
    assert "roster.find(" in body, "the match no longer runs after the state machine"
    assert body.index('spawnWatch.state = "failed"') < body.index("roster.find(")


def test_bridge_down_is_read_from_its_own_endpoint():
    """/api/threads returns a bare list, so bridge liveness cannot ride on it —
    and it has to be answerable when that list is EMPTY, which is precisely the
    state a hub started during an outage is in."""
    c = code()
    assert 'fetch("/api/bridge")' in c
    assert 'fetch("/api/threads")' in c


def test_the_outage_is_announced_even_with_no_wsl_card_to_carry_it():
    """The old flag lived on wsl threads. A hub started while the bridge was
    already down had none, so it showed an empty WSL side and explained
    nothing. Absent is not empty."""
    c = code()
    assert 'if (tab === "term" && bridgeState.enabled && bridgeState.probed && !bridgeState.up) {' in c
    assert "WSL bridge down" in c


def test_a_fault_never_renders_as_a_quiet_session():
    """`gray` means "nothing is happening here", which is the opposite of what
    a down bridge means. The fault states get their own class."""
    c = code()
    assert 'w.className = "th down";' in c
    assert 'b.className = st ? "th down" : "th gray";' in c
    assert ".th.down {" in daemon.HTML.read_text(encoding="utf-8")


def test_an_unprobed_bridge_is_never_announced_as_a_dead_one():
    """`probed` gates both the banner and the card hold, so the first seconds
    of a hub boot do not report an outage nobody has measured."""
    c = code()
    assert "bridgeState.probed && !bridgeState.up" in c
    assert "probed: false" in c
    assert c.count("bridgeState.probed") >= 2   # banner AND the card hold


def test_the_banner_stays_off_a_machine_that_has_no_wsl():
    """`enabled` gates it, so a Mac never sees a bridge it was never going to
    have. Absent is not an error."""
    c = code()
    assert "bridgeState.enabled &&" in c
    assert "let bridgeState = { up: true, probed: false, enabled: false" in c
