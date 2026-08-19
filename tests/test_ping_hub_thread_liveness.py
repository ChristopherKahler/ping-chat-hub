"""The thread view must not lie while the transport works.

Three defects, one class, all measured on 2026-08-19:

  * A bridge restart froze journaling for 24 minutes. The bridge is a fresh
    PROCESS so its event `seq` restarts at zero, and the hub carried its cursor
    across the reconnect, so every new event filtered as already-seen. Silently,
    forever. It was invisible because roster comes from /snapshot and events
    come from /events: the two paths fail INDEPENDENTLY and only one of them is
    observable from outside. A kill test that checked /snapshot passed happily
    through a totally dead event stream.
  * Chris's own sends only appeared after a hub restart, wearing a boot
    timestamp. Outbound journaling was left entirely to the watcher, which only
    catches the inbox file if the receiving session has not consumed it yet — a
    race the hub usually loses.
  * Three hub processes held port 7799 at once and split his requests for 45
    minutes without one error, because Windows SO_REUSEADDR means "share a live
    listener", not "reuse a dead one".

Everything here is hermetic: no sockets, no subprocesses, no clock waits.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from ping_hub import daemon
from ping_hub.engine import ECHO_WINDOW, Engine, echo_key, epoch_reset


# -- the single-instance guard ----------------------------------------------
def test_it_refuses_a_port_that_already_has_a_listener():
    with pytest.raises(daemon.SingleInstanceError) as e:
        daemon.guard_single_instance(7799, listening=lambda p: True,
                                     holder=lambda p: "1234")
    assert "1234" in str(e.value), "the operator is left guessing which to kill"
    assert "7799" in str(e.value)


def test_a_free_port_starts_normally():
    daemon.guard_single_instance(7799, listening=lambda p: False,
                                 holder=lambda p: "")


def test_it_still_refuses_when_it_cannot_name_the_holder():
    """The refusal may never depend on the courtesy lookup succeeding."""
    with pytest.raises(daemon.SingleInstanceError) as e:
        daemon.guard_single_instance(7799, listening=lambda p: True,
                                     holder=lambda p: "")
    assert "holder pid unknown" in str(e.value)


def test_the_server_does_not_share_a_live_listener():
    """`HTTPServer.allow_reuse_address` is 1, and on Windows that is what let
    three hubs bind 7799 at once. The exclusive posture is the class fix."""
    assert daemon._Server.allow_reuse_address is False
    import inspect
    src = inspect.getsource(daemon._Server)
    assert "SO_EXCLUSIVEADDRUSE" in src


def test_the_guard_runs_before_the_daemon_touches_any_state():
    """A second hub must not register titles, backfill, or start watchers
    against a store another hub already owns — all of which main() does before
    it ever binds."""
    import inspect
    # comments stripped: the comment explaining this very ordering names
    # "backfill", and a naive index() finds the explanation instead of the call
    body = "\n".join(l.split("#")[0] for l in
                      inspect.getsource(daemon.main).splitlines())
    assert "guard_single_instance" in body
    assert body.index("guard_single_instance") < body.index("backfill")
    assert body.index("guard_single_instance") < body.index("engine.run()")


# -- the cursor epoch --------------------------------------------------------
def test_a_new_bridge_process_resets_the_cursor():
    """THE freeze. Without this the hub filters every event from the restarted
    bridge as already-seen, and nothing anywhere reports an error."""
    assert epoch_reset(100.0, 200.0, 5000) == (0, 200.0)


def test_the_same_bridge_keeps_its_cursor():
    assert epoch_reset(100.0, 100.0, 5000) == (5000, 100.0)


def test_the_first_response_adopts_the_epoch_without_resetting():
    assert epoch_reset(None, 100.0, 0) == (0, 100.0)


def test_an_older_bridge_that_sends_no_epoch_behaves_exactly_as_before():
    """A hub newer than its deployed bridge must not become a NEW outage."""
    assert epoch_reset(None, None, 5000) == (5000, None)
    assert epoch_reset(100.0, None, 5000) == (5000, 100.0)


def test_the_bridge_stamps_its_epoch_on_both_endpoints():
    """The hub cannot notice a restart the bridge never announces."""
    from ping_hub.bridge import wsl_bridge
    import inspect
    src = inspect.getsource(wsl_bridge)
    assert "EPOCH = time.time()" in src
    do_get = inspect.getsource(wsl_bridge.Handler.do_GET)
    assert do_get.count("EPOCH") == 2, "snapshot and events must both carry it"


def test_the_loop_discards_a_response_filtered_against_a_dead_cursor():
    """On an epoch change the in-flight response was filtered against a cursor
    that means nothing to the new process, so applying its cursor would re-arm
    the freeze."""
    import inspect
    src = inspect.getsource(Engine._bridge_loop)
    assert "epoch_reset" in src
    assert "primed = False" in src.split("epoch_reset")[2] or "cursor, epoch, primed = 0" in src
    assert src.index("epoch_reset") < src.index('cursor = body.get("cursor"')


# -- the client's dead stream ------------------------------------------------
def _page() -> str:
    src = daemon.HTML.read_text(encoding="utf-8")
    src = src[src.index("<script"):]
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def test_a_reconnect_refills_the_thread_it_missed():
    """EventSource reconnects itself, which is the trap: the feed resumes but
    the messages that arrived while it was down were never delivered to this
    page and never will be, because addMsg fires only from onmessage. The
    roster keeps polling every 5s, so the board looks alive while the open
    thread has quietly stopped receiving anything."""
    c = _page()
    assert "es.onopen" in c
    onopen = c[c.index("es.onopen"):c.index("es.onmessage")]
    assert "loadThread()" in onopen, "the gap is never filled"
    assert "loadRoster()" in onopen


def test_the_first_connect_is_not_treated_as_a_recovery():
    """Nothing was missed before the page existed."""
    c = _page()
    onopen = c[c.index("es.onopen"):c.index("es.onmessage")]
    assert "if (!streamDown) return;" in onopen


def test_a_dead_stream_is_visible_rather_than_silent():
    c = _page()
    assert "es.onerror" in c
    assert "live feed down" in daemon.HTML.read_text(encoding="utf-8")


# -- live echo ---------------------------------------------------------------
def _engine() -> Engine:
    e = Engine.__new__(Engine)
    e.lock = threading.Lock()
    e._echoes = {}
    return e


def test_an_echo_is_remembered_only_for_the_race_width():
    """Ten seconds is the real width: the watcher polls once a second. Wider
    starts swallowing a message Chris deliberately repeated, and a swallowed
    message is the silent-lie class this work exists to kill."""
    e = _engine()
    k = echo_key("wsl", "toucan", "chris", "yes")
    e._echoes[k] = 1000.0
    assert e._echo_recent(k, now=1000.0 + ECHO_WINDOW - 0.1) is True
    e._echoes[k] = 1000.0
    assert e._echo_recent(k, now=1000.0 + ECHO_WINDOW + 0.1) is False
    assert ECHO_WINDOW <= 15, "wide enough to swallow a deliberate repeat"


def test_the_echo_index_prunes_itself():
    e = _engine()
    e._echoes = {"a": 1000.0, "b": 1000.0}
    e._echo_recent("zzz", now=1000.0 + ECHO_WINDOW + 1)
    assert e._echoes == {}


def test_the_key_separates_what_should_be_separate():
    base = ("wsl", "toucan", "chris", "yes")
    assert echo_key(*base) == echo_key(*base)
    for i, other in enumerate(("win", "falcon", "hub", "no")):
        v = list(base); v[i] = other
        assert echo_key(*v) != echo_key(*base)


def test_a_missed_key_appends_rather_than_dropping():
    """The ruling: a visible duplicate always beats a possible drop. A hash
    MISS must fall through to the append, never to a skip."""
    e = _engine()
    assert e._echo_recent(echo_key("wsl", "toucan", "chris", "unseen")) is False


def test_both_watcher_seams_consult_the_echo_index():
    """One seam guarded and the other not would double every send that took
    the unguarded path."""
    import inspect
    for fn in (Engine.scan_inboxes, Engine._bridge_ping):
        src = inspect.getsource(fn)
        assert "_echo_recent" in src, f"{fn.__name__} would double-journal"
        assert 'direction == "out"' in src, f"{fn.__name__} guards inbound too"


def test_the_outbound_entry_is_stamped_at_send_time_not_at_boot():
    """Every dir-out entry in the live journal carried a hub BOOT timestamp,
    because only backfill ever wrote them."""
    import inspect
    src = inspect.getsource(Engine._echo_out)
    assert '"dir": "out"' in src
    assert "time.strftime" in src, "a hardcoded or inherited ts is the bug"
    assert '"echo": True' in src


def test_send_echoes_on_success_only():
    """A failed send that echoed would put a message in the thread that was
    never delivered — the inverse lie, and worse."""
    import inspect
    src = inspect.getsource(Engine.send)
    assert src.count("_echo_out") == 2, "both the wsl and win paths echo"
    for chunk in src.split("_echo_out")[:-1]:
        assert "if ok:" in chunk.splitlines()[-2] or "if ok:" in chunk
