"""Recover pings the hub was not running to see.

The hub journals a ping when its file APPEARS in a relay inbox. base deletes
that file on delivery, so anything delivered while the daemon is down is
delivered correctly and then never journaled: the work happened, the record of
it did not. Chris lost an exchange with orca to a restart blink.

base itself keeps a durable record. Every ping becomes a node in its graph
store, and the node's id is byte-identical to the slug the journal already
uses, so matching them is an exact key comparison rather than a guess.

Two properties this module will not trade away:

**Strictly additive.** The graph is NOT a superset of the journal — measured on
this machine, 302 of 1091 journal entries exist in no graph at all. Anything
that "reconciled" the two would delete real history. This only ever appends.

**Bounded by a high-water mark.** 1099 graph pings are unjournaled, but almost
all of them predate the hub keeping journals at all; only a handful belong to
any actual outage. Backfilling everything would not repair a gap, it would
invent a past the hub never witnessed. So each thread takes only pings newer
than its own newest entry, and a thread with no journal is held to a global
floor. A boot that missed nothing writes nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

# <...#ping/ping-123> <...#predicate> "value" <graph> .
_TRIPLE = re.compile(
    r'<[^>]*ontology#ping/(ping-\d+)>\s+<[^>]+?[#/](\w+)>\s+(.*?)\s+<[^>]*>\s*\.\s*$')


def graph_stores(cfg) -> list[Path]:
    """Both global-tier stores. Measured: pings appear in the global tiers
    only — all fourteen workspace stores on this machine hold zero — so this
    does not walk the workspace registry looking for records that never live
    there."""
    out = [cfg.paths.base_store / "graph.nq"]
    home = cfg.wsl.home_unc
    if cfg.wsl.enabled and home:
        out.append(Path(home) / ".base-gbl" / ".base" / "graph.nq")
    return out


def _unquote(value: str) -> str:
    """`"text"` and `"2026-01-01T00:00:00-0500"^^<xsd:dateTime>` both to text.

    The typed form has to lose its datatype BEFORE the quotes are stripped.
    Doing it the other way round leaves a leading quote on every timestamp,
    and since `"` sorts below every digit, each one then compares as older
    than any real mark — which silently filters out the entire backfill.
    """
    value = value.strip()
    if "^^" in value:
        value = value.split("^^", 1)[0].strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return (value.replace("\\n", "\n").replace("\\r", "")
                 .replace('\\"', '"').replace("\\\\", "\\"))


def read_pings(path: Path) -> dict[str, dict]:
    """{ping id: fields} from one store. Unreadable is empty, never fatal —
    a missing WSL share must not stop the daemon booting."""
    out: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _TRIPLE.match(line.strip())
                if m:
                    out.setdefault(m.group(1), {})[m.group(2)] = _unquote(m.group(3))
    except OSError:
        return {}
    return out


def norm_ts(value: str) -> str:
    """Make two timestamps string-comparable.

    The journal writes offsets as `-0500`; base's graph writes `-05:00`. Left
    alone, the colon form sorts LATER than the identical instant without it,
    because ':' is above '0' in ASCII — so the bound would drift by whatever
    that mismatch is worth. Comparison is lexical elsewhere in the hub, so the
    fix belongs here rather than in a date parser.
    """
    value = (value or "").strip()
    if len(value) >= 6 and value[-3] == ":" and value[-6] in "+-":
        value = value[:-3] + value[-2:]
    return value


def _entry(pid: str, rec: dict, hub_title: str) -> dict | None:
    """A graph node as a journal line, in the shape scan_inboxes writes."""
    to = rec.get("assignedTo", "")
    frm = rec.get("relayFrom", "")
    if not to or not frm or "message" not in rec:
        return None                      # not enough to render honestly
    # same routing rule the inbox watcher uses: which thread owns this line
    if to == hub_title:
        thread, direction, peer = frm, "in", False
    elif frm == hub_title:
        thread, direction, peer = to, "out", False
    else:
        thread, direction, peer = to, "in", True
    return {"slug": pid, "dir": direction, "peer": peer, "from": frm, "to": to,
            "kind": rec.get("pingKind", "ping"), "summary": rec.get("message", ""),
            "created": rec.get("createdAt", ""), "backfilled": True,
            "_thread": thread}


def plan(cfg, seen: dict[str, set], newest: dict[str, str], side: str,
         records: dict[str, dict], floor: str = "") -> list[dict]:
    """Which entries to append, per the approved bound.

    seen:   "side:thread" -> slugs already journaled
    newest: "side:thread" -> newest `created` already journaled
    floor:  global cutoff for threads with no journal at all
    """
    out = []
    for pid, rec in records.items():
        entry = _entry(pid, rec, cfg.hub.standing_title)
        if entry is None:
            continue
        key = f"{side}:{entry['_thread']}"
        if pid in seen.get(key, set()):
            continue                      # already have it; exact slug match
        mark = norm_ts(newest.get(key) or floor)
        created = norm_ts(entry.get("created", ""))
        if not created or (mark and created <= mark):
            # older than what this thread already knows about: history, not a
            # gap. Undated records are skipped rather than guessed at.
            continue
        out.append(entry)
    out.sort(key=lambda e: (e["_thread"], norm_ts(e["created"])))
    return out
