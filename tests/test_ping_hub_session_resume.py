"""Resuming a session from its own thread, and a verb that admits its age.

Two defects, one root. Chris's machine restarted, every terminal died, and the
threads stayed open — so the hub was showing conversations whose backends were
gone, with no way back except typing `claude --resume <id>` by hand, which is
impossible from a phone. The same cards kept reporting a live verb: `doing` is
the shape of the last transcript record, kept forever, so `hawk` sat there
reading "reading the prompt" 21 hours dead.

Underneath both: the card decided liveness from two weak proxies — a wake
sentinel and transcript churn — while the strong proof sat unused in this same
repo. Measured on `quokka` 2026-08-19: recorded pid 34243 alive with its
start-time anchor matching, card reading dead and offering to boot a second
session onto its codename.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ping_hub import daemon
from ping_hub.engine import Engine


def _endpoint(name: str, after: str) -> str:
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    start = src.index(f'u.path == "/api/{name}"')
    return src[start:src.index(after, start)]


# ── why resume is or is not offered ─────────────────────────────────────────
def test_a_thread_with_no_session_id_has_nothing_to_resume():
    assert "nothing to resume" in daemon._resume_reason({}, False, "")


def test_a_thread_with_no_recorded_cwd_refuses_rather_than_guessing():
    """Claude Code keys transcripts by project directory: `--resume` from the
    wrong folder does not find the session, and booting into an empty one is
    worse than saying why."""
    r = daemon._resume_reason({"session_id": "abc"}, False, "")
    assert "working directory" in r


def test_a_living_session_is_refused_out_loud_not_silently():
    """A resume that quietly did nothing would read as a broken button."""
    r = daemon._resume_reason({"session_id": "abc", "cwd": "C:/x"}, True, "claude")
    assert "looks alive" in r and "Clear it first" in r


def test_a_dead_session_reports_reap_s_own_reason():
    r = daemon._resume_reason({"session_id": "abc", "cwd": "C:/x"}, False,
                              "process 34243 is not running.")
    assert r == "process 34243 is not running."


# ── the endpoint ────────────────────────────────────────────────────────────
def test_resume_boots_the_recorded_session_in_its_recorded_directory():
    body = _endpoint("resume", 'elif u.path == "/api/settings"')
    assert '"--resume", sid' in body
    assert "spawn_tab(side, args, cwd, title=title)" in body, \
        "title is the codename pin (BASE_RELAY_AS); cwd is where the transcript is"


def test_resume_sends_no_prompt():
    """`claude --resume <sid> \"text\"` injects a user turn. The thread has to
    come back where it left off, not with a hub-written message pushed in."""
    body = _endpoint("resume", 'elif u.path == "/api/settings"')
    assert "prompt=" not in body, "spawn_tab must be called without a prompt"


def test_every_refusal_and_failure_has_a_status_and_a_reason():
    body = _endpoint("resume", 'elif u.path == "/api/settings"')
    for code in ("404", "409", "500"):
        assert code in body, f"no {code} path"
    assert "resume failed to boot" in body


def test_a_resume_attempt_lands_in_the_thread_itself():
    """The tab may open and claude may still die, taking its error message off
    the screen with it. The journal is what survives that."""
    body = _endpoint("resume", 'elif u.path == "/api/settings"')
    assert "engine.append(" in body


def test_the_preview_reuses_the_probe_clear_already_makes():
    """"Can this be cleared" and "should this be resumed" are one question from
    two sides; two probes could disagree."""
    body = _endpoint("resume-preview", 'elif u.path == "/api/cxptt"')
    assert "reap.confirm(reap.find_record(INBOX_ROOT, title))" in body


# ── idle is not dead ────────────────────────────────────────────────────────
def _engine() -> Engine:
    e = Engine.__new__(Engine)
    e.lock = threading.Lock()
    e._alive_cache = {}
    return e


def test_a_confirmed_process_answers_the_question_the_proxies_cannot():
    e = _engine()
    e._alive_cache["zebra"] = (1000.0, True)
    assert e._confirmed_alive("zebra", 1010.0) is True


def test_the_answer_is_cached_because_confirm_costs_a_cim_query():
    """The roster polls every two seconds; this must never run per card per
    poll."""
    from ping_hub.engine import ALIVE_RECHECK_SECS
    assert ALIVE_RECHECK_SECS >= 20
    e = _engine()
    e._alive_cache["zebra"] = (1000.0, True)
    assert e._confirmed_alive("zebra", 1000.0 + ALIVE_RECHECK_SECS - 1) is True


def test_no_record_is_unknown_rather_than_alive_or_dead():
    e = _engine()
    e._alive_cache["ghosty"] = (1000.0, None)
    assert e._confirmed_alive("ghosty", 1005.0) is None


def test_the_board_stops_offering_to_boot_over_a_living_session():
    html = Path(daemon.HTML).read_text(encoding="utf-8")
    assert "!t.watching && !t.active && !t.idle && !ghost" in html


# ── the verb admits its age ─────────────────────────────────────────────────
def test_the_verb_carries_the_timestamp_of_the_record_it_came_from():
    src = Path(__import__("ping_hub.engine", fromlist=["x"]).__file__).read_text(
        encoding="utf-8")
    assert 'out["doing_at"] = jmt' in src
    bridge = Path(__import__("ping_hub.bridge.wsl_bridge",
                             fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert 'out["doing_at"] = jmt' in bridge, "the age has to cross the bridge too"


def test_the_freshness_budget_and_ceiling_are_the_ruled_numbers():
    """15s bare, aged to ten minutes, suppressed past that (Chris via toucan,
    2026-08-19). Past the ceiling an unknown state renders as unknown, never as
    the last known one."""
    html = Path(daemon.HTML).read_text(encoding="utf-8")
    assert "VERB_FRESH_SECS = 15" in html
    assert "VERB_CEILING_SECS = 600" in html
    assert "if (age > VERB_CEILING_SECS) return \"\";" in html


def test_bridge_down_ages_the_wsl_cards_instead_of_freezing_them():
    """It cleared `watching` and left `active` and the verb exactly as they
    were, so every wsl card kept pulsing dots and a verb frozen at the instant
    the transport died — for as long as the bridge stayed down."""
    src = Path(__import__("ping_hub.engine", fromlist=["x"]).__file__).read_text(
        encoding="utf-8")
    down = src[src.index('t["bridge_down"] = True'):]
    assert 't["active"] = False' in down[:900]

# ── a dead RECORD is not a dead SESSION ────────────────────────────────────
# Caught live 14:02 on 2026-08-19, before it could do harm. `zebra` and
# `toucan` were both working — toucan mid-tool-run — and both carried a `.pid`
# naming a process that genuinely no longer existed, because a session that is
# rebooted or reclaims its codename leaves the old record behind. `confirm`
# answered correctly; the first cut of the preview turned that into
# "resumable", which would have offered one-tap Resume on a live session.
LIVE_BUT_STALE_RECORD = {"session_id": "s", "cwd": "/x", "active": False,
                         "watching": True}


def test_a_session_still_reporting_in_is_never_offered_a_resume():
    """The identical hazard the boot button had: a second session on a live
    codename."""
    assert daemon._still_reporting(LIVE_BUT_STALE_RECORD) is True
    r = daemon._resume_reason(LIVE_BUT_STALE_RECORD, False, "process 196849 is not running.")
    assert "RECORD is stale, not the session" in r
    assert "second session on this codename" in r


def test_mid_tool_run_counts_as_reporting_in():
    assert daemon._still_reporting({"active": True}) is True


def test_a_confirmed_idle_session_is_not_resumable_either():
    """`idle` means the recorded process was CONFIRMED running."""
    assert daemon._still_reporting({"idle": True}) is True


def test_a_session_with_no_signal_at_all_is_the_only_resumable_one():
    dead = {"session_id": "s", "cwd": "/x", "active": False, "watching": False,
            "idle": False}
    assert daemon._still_reporting(dead) is False
    assert daemon._resume_reason(dead, False, "process 1 is not running.") \
        == "process 1 is not running."


def test_both_the_preview_and_the_action_apply_the_gate():
    """A preview that hides the button while the endpoint still honours the
    request is a control that can be reached another way."""
    prev = _endpoint("resume-preview", 'elif u.path == "/api/cxptt"')
    act = _endpoint("resume", 'elif u.path == "/api/settings"')
    assert "_still_reporting(t)" in prev
    assert "_still_reporting(t)" in act

# ── a resume that fails must SAY so, in the thread ─────────────────────────
# Found by this fork's own G2 probe: the endpoint answered 200, wrote "resume
# requested", and nothing ever reported that the session had not returned.
# Only the browser noticed, client-side, while that page stayed open — so from
# a phone, later, a failed resume looked exactly like one that worked.
class _Engine:
    def __init__(self, cards):
        self.threads = cards
        self.lock = threading.Lock()
        self.written = []

    def append(self, title, entry, side=None):
        self.written.append(entry["summary"])


def _run_watch(monkeypatch, cards, ticks=100):
    eng = _Engine(cards)
    monkeypatch.setattr(daemon, "engine", eng)
    t = [1000.0]

    def clock():
        return t[0]

    def sleep(n):
        t[0] += n
    daemon._resume_watch("probe", "wsl", "old-session-id", sleep=sleep, clock=clock)
    return eng.written


def test_a_resume_that_never_returns_is_reported_in_the_thread(monkeypatch):
    written = _run_watch(monkeypatch, {"wsl:probe": {"session_id": "old-session-id"}})
    assert len(written) == 1
    assert "did NOT come back" in written[0]


def test_a_resume_that_returns_under_a_new_id_says_so(monkeypatch):
    written = _run_watch(monkeypatch, {"wsl:probe": {"session_id": "brand-new-id"}})
    assert "reporting in again" in written[0] and "brand-ne" in written[0]


def test_reporting_in_counts_even_when_the_id_did_not_change(monkeypatch):
    """`--resume` mints a new id today, but that is a fact about this version
    of Claude Code rather than a contract."""
    written = _run_watch(monkeypatch,
                         {"wsl:probe": {"session_id": "old-session-id", "watching": True}})
    assert "reporting in again" in written[0]


def test_the_window_is_generous_enough_not_to_cry_failure_over_a_slow_boot():
    """A cold resume on a large transcript is slow; declaring failure over a
    session that is merely still loading would be its own lie."""
    assert daemon.RESUME_WINDOW_SECS >= 120


def test_resume_refuses_when_there_is_no_transcript_to_resume():
    """`claude --resume <sid>` with nothing on disk exits instantly and takes
    its error message off the screen with it — measured on `resume-probe`,
    killed 25s in before Claude Code had written the file."""
    body = _endpoint("resume", 'elif u.path == "/api/settings"')
    assert "engine.transcript_path(sid, side, cwd) is None" in body
    assert "nothing to resume" in body


def test_the_outcome_watch_is_actually_armed():
    body = _endpoint("resume", 'elif u.path == "/api/settings"')
    assert "target=_resume_watch" in body
