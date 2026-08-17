"""Recovering pings delivered while the hub was down.

Two properties carry this feature, and both are things it must REFUSE to do:
never write outside the outage window, and never remove anything. The measured
reality on the machine this was built against is why:

  * 1099 graph pings are unjournaled, but almost all predate the hub keeping
    journals — an unbounded backfill invents a past rather than repairing one.
  * 302 journal entries exist in NO graph, so anything two-way would delete
    real history.

Hermetic: the graph is a string, the journal marks are dicts.
"""

from __future__ import annotations

import pytest

from ping_hub import backfill as bf
from ping_hub.config import Config

from test_ping_hub_config import StubProbe

HUB = "chris"
# base mints ids as ping-<epoch millis>; fixtures use that shape so they
# exercise the same parse path real records take
OLD, NEW = "ping-1786990000000", "ping-1786995000000"


def cfg(raw=None, **kw) -> Config:
    return Config(raw or {}, probe=StubProbe(**kw))


def nq(pid: str, frm: str, to: str, msg: str, created: str, kind="ping") -> str:
    o = "http://ops-sys.local/ontology#"
    g = "<http://ops-sys.local/graph>"
    return "\n".join([
        f'<{o}ping/{pid}> <{o}relayFrom> "{frm}" {g} .',
        f'<{o}ping/{pid}> <{o}assignedTo> "{to}" {g} .',
        f'<{o}ping/{pid}> <{o}message> "{msg}" {g} .',
        f'<{o}ping/{pid}> <{o}pingKind> "{kind}" {g} .',
        f'<{o}ping/{pid}> <{o}createdAt> "{created}"^^'
        f'<http://www.w3.org/2001/XMLSchema#dateTime> {g} .',
    ])


def parse(text: str, tmp_path) -> dict:
    p = tmp_path / "graph.nq"
    p.write_text(text, encoding="utf-8")
    return bf.read_pings(p)


# ── reading base's store ─────────────────────────────────────────────────────
def test_reads_a_ping_node_into_its_fields(tmp_path):
    recs = parse(nq("ping-1", "heron", "orca", "check the deploy",
                    "2026-08-17T14:00:00-0500"), tmp_path)
    assert recs["ping-1"]["relayFrom"] == "heron"
    assert recs["ping-1"]["assignedTo"] == "orca"
    assert recs["ping-1"]["message"] == "check the deploy"
    assert recs["ping-1"]["createdAt"].startswith("2026-08-17T14:00")


def test_escaped_text_survives_the_round_trip(tmp_path):
    recs = parse(nq("ping-2", "a", "b", 'said \\"go\\" then\\nstopped',
                    "2026-08-17T14:00:00-0500"), tmp_path)
    assert recs["ping-2"]["message"] == 'said "go" then\nstopped'


def test_an_unreadable_store_is_empty_not_fatal(tmp_path):
    """A daemon must still boot when the WSL share is down."""
    assert bf.read_pings(tmp_path / "nope.nq") == {}


def test_only_the_global_tiers_are_read():
    """Measured: pings live in the global stores only. Walking every
    workspace store would read tens of MB to find nothing."""
    stores = bf.graph_stores(cfg())
    assert len(stores) == 2
    assert all(s.name == "graph.nq" for s in stores)
    assert str(stores[0]).endswith("\\.base-gbl\\.base\\graph.nq")


def test_a_one_sided_machine_reads_one_store():
    assert len(bf.graph_stores(cfg(distro="", wsl_home=""))) == 1


# ── the bound ────────────────────────────────────────────────────────────────
def test_only_pings_newer_than_the_thread_mark_are_taken(tmp_path):
    recs = parse("\n".join([
        nq(OLD, "heron", "chris", "before", "2026-08-17T10:00:00-0500"),
        nq(NEW, "heron", "chris", "during the outage", "2026-08-17T14:00:00-0500"),
    ]), tmp_path)
    got = bf.plan(cfg(), {}, {"win:heron": "2026-08-17T12:00:00-0500"}, "win", recs)
    assert [e["slug"] for e in got] == [NEW]


def test_an_id_that_is_not_a_real_ping_id_is_ignored(tmp_path):
    """base mints ids as ping-<epoch millis>. Anything else in the store is
    not a ping node and must not be rendered as one."""
    assert parse(nq("ping-notanid", "a", "b", "x",
                    "2026-08-17T14:00:00-0500"), tmp_path) == {}


def test_a_boot_that_missed_nothing_writes_nothing(tmp_path):
    """heron's condition 3, as a test."""
    recs = parse(nq("ping-1", "heron", "chris", "hi", "2026-08-17T10:00:00-0500"),
                 tmp_path)
    assert bf.plan(cfg(), {}, {"win:heron": "2026-08-17T12:00:00-0500"}, "win", recs) == []


def test_a_journal_less_thread_is_held_to_the_global_floor(tmp_path):
    """Otherwise a thread the hub never journaled drags in months of history
    the hub never witnessed."""
    recs = parse("\n".join([
        nq(OLD, "newguy", "chris", "old", "2026-08-11T09:00:00-0500"),
        nq(NEW, "newguy", "chris", "new", "2026-08-17T14:00:00-0500"),
    ]), tmp_path)
    got = bf.plan(cfg(), {}, {}, "win", recs, floor="2026-08-17T12:00:00-0500")
    assert [e["slug"] for e in got] == [NEW]


def test_already_journaled_slugs_are_skipped_by_exact_match(tmp_path):
    recs = parse(nq("ping-9", "heron", "chris", "x", "2026-08-17T14:00:00-0500"),
                 tmp_path)
    seen = {"win:heron": {"ping-9"}}
    assert bf.plan(cfg(), seen, {}, "win", recs, floor="2026-08-01T00:00:00-0500") == []


@pytest.mark.parametrize("a, b", [
    ("2026-08-17T14:00:00-05:00", "2026-08-17T14:00:00-0500"),
    ("2026-08-17T14:00:00+01:00", "2026-08-17T14:00:00+0100"),
])
def test_the_two_offset_spellings_compare_equal(a, b):
    """The journal writes -0500, base's graph writes -05:00. Compared raw the
    colon form sorts LATER, because ':' is above '0' in ASCII, so the bound
    would drift by exactly that mismatch."""
    assert bf.norm_ts(a) == bf.norm_ts(b)


def test_a_colon_offset_record_at_the_mark_is_not_taken(tmp_path):
    """The failure the normalisation prevents: same instant, different
    spelling, admitted as if it were newer."""
    recs = parse(nq(NEW, "heron", "chris", "same instant",
                    "2026-08-17T12:00:00-05:00"), tmp_path)
    assert bf.plan(cfg(), {}, {"win:heron": "2026-08-17T12:00:00-0500"},
                   "win", recs) == []


def test_an_undated_record_is_skipped_not_guessed_at(tmp_path):
    o = "http://ops-sys.local/ontology#"
    g = "<http://ops-sys.local/graph>"
    text = "\n".join([
        f'<{o}ping/ping-x> <{o}relayFrom> "heron" {g} .',
        f'<{o}ping/ping-x> <{o}assignedTo> "chris" {g} .',
        f'<{o}ping/ping-x> <{o}message> "no date" {g} .',
    ])
    recs = parse(text, tmp_path)
    assert bf.plan(cfg(), {}, {}, "win", recs, floor="2026-08-01T00:00:00-0500") == []


def test_a_record_missing_sender_or_body_is_never_rendered(tmp_path):
    o = "http://ops-sys.local/ontology#"
    g = "<http://ops-sys.local/graph>"
    recs = parse(f'<{o}ping/ping-y> <{o}assignedTo> "chris" {g} .', tmp_path)
    assert bf.plan(cfg(), {}, {}, "win", recs, floor="2026-01-01T00:00:00-0500") == []


# ── routing: the same rule the live watcher uses ─────────────────────────────
def test_a_ping_to_the_hub_lands_on_the_senders_thread(tmp_path):
    recs = parse(nq("ping-1", "orca", HUB, "done", "2026-08-17T14:00:00-0500"), tmp_path)
    e = bf.plan(cfg(), {}, {}, "win", recs, floor="2026-01-01T00:00:00-0500")[0]
    assert e["_thread"] == "orca" and e["dir"] == "in" and e["peer"] is False


def test_a_ping_from_the_hub_is_outbound_on_the_targets_thread(tmp_path):
    recs = parse(nq("ping-2", HUB, "orca", "go", "2026-08-17T14:00:00-0500"), tmp_path)
    e = bf.plan(cfg(), {}, {}, "win", recs, floor="2026-01-01T00:00:00-0500")[0]
    assert e["_thread"] == "orca" and e["dir"] == "out" and e["peer"] is False


def test_a_peer_to_peer_ping_is_flagged_peer(tmp_path):
    recs = parse(nq("ping-3", "heron", "orca", "sync", "2026-08-17T14:00:00-0500"), tmp_path)
    e = bf.plan(cfg(), {}, {}, "win", recs, floor="2026-01-01T00:00:00-0500")[0]
    assert e["_thread"] == "orca" and e["peer"] is True


def test_recovered_entries_are_marked_as_recovered(tmp_path):
    """A backfilled line should be distinguishable from one witnessed live."""
    recs = parse(nq("ping-4", "heron", HUB, "x", "2026-08-17T14:00:00-0500"), tmp_path)
    assert bf.plan(cfg(), {}, {}, "win", recs,
                   floor="2026-01-01T00:00:00-0500")[0]["backfilled"] is True


def test_output_is_ordered_so_threads_read_chronologically(tmp_path):
    recs = parse("\n".join([
        nq(NEW, "heron", HUB, "second", "2026-08-17T14:05:00-0500"),
        nq(OLD, "heron", HUB, "first", "2026-08-17T14:00:00-0500"),
    ]), tmp_path)
    got = bf.plan(cfg(), {}, {}, "win", recs, floor="2026-01-01T00:00:00-0500")
    assert [e["slug"] for e in got] == [OLD, NEW]


def test_plan_never_proposes_a_removal(tmp_path):
    """The graph is missing records the journal still holds. A plan that could
    express a deletion would eventually make one."""
    recs = parse(nq("ping-1", "heron", HUB, "x", "2026-08-17T14:00:00-0500"), tmp_path)
    got = bf.plan(cfg(), {}, {}, "win", recs, floor="2026-01-01T00:00:00-0500")
    assert all(set(e) >= {"slug", "summary", "from", "to"} for e in got)
    assert not any("delete" in str(e).lower() or e.get("_remove") for e in got)
