#!/usr/bin/env python3
"""Seed a shadow hub with fixture threads, so the UI can be checked offline.

A shadow instance points at a scratch store, which means an EMPTY thread list —
fine for checking the composer, useless for checking anything that renders a
card. The ghost badge in particular needs a session id registered on both sides
with evidence on only one, and waiting for that to happen naturally is not a
test.

    python tools/seed_demo.py <path to the shadow's hub.toml>

REFUSES to run against a config with `register_standing_title = true`. That key
is false only on deliberate test instances, so it is the one honest marker that
this is not somebody's live hub. There is no --force.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ping_hub import config as cfgmod   # noqa: E402

NOW = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def stamp(minutes_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z",
                         time.localtime(time.time() - minutes_ago * 60))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cfg = cfgmod.load(Path(argv[0]))
    if cfg.hub.register_standing_title:
        print("refusing: this config has register_standing_title = true, which "
              "means it may be a live hub. Seed only a shadow instance.",
              file=sys.stderr)
        return 1

    store = cfg.paths.base_store
    hub = store / "hub" / "threads"
    (hub / "win").mkdir(parents=True, exist_ok=True)
    (hub / "wsl").mkdir(parents=True, exist_ok=True)

    # one session id on BOTH sides: the win card has transcript evidence, the
    # wsl card has none, which is exactly the ghost rule's target
    sid = "demo-session-0001"
    sessions = {"sessions": {
        "mirrored": {"title": "mirrored", "session_id": sid,
                     "cwd": str(Path.home()), "workspace": "demo",
                     "last_heartbeat": NOW, "auto": False},
        "solo": {"title": "solo", "session_id": "demo-session-0002",
                 "cwd": str(Path.home()), "workspace": "demo",
                 "last_heartbeat": NOW, "auto": False},
    }}
    (store / "sessions.json").write_text(json.dumps(sessions, indent=1),
                                         encoding="utf-8")

    def journal(side: str, title: str, lines: list[tuple[str, str, str]]) -> None:
        p = hub / side / f"{title}.jsonl"
        with open(p, "w", encoding="utf-8") as fh:
            for i, (frm, to, text) in enumerate(lines):
                fh.write(json.dumps({
                    "slug": f"ping-demo-{title}-{i}",
                    "dir": "in" if to == cfg.hub.standing_title else "out",
                    "peer": to != cfg.hub.standing_title and frm != cfg.hub.standing_title,
                    "from": frm, "to": to, "kind": "ping",
                    "summary": text, "created": stamp(120 - i * 7),
                }) + "\n")

    journal("win", "mirrored", [
        ("mirrored", cfg.hub.standing_title, "this side has a transcript behind it"),
        (cfg.hub.standing_title, "mirrored", "so it renders as a real session"),
    ])
    journal("wsl", "mirrored", [
        ("mirrored", cfg.hub.standing_title, "same session id, no transcript here"),
    ])
    journal("win", "solo", [
        ("solo", cfg.hub.standing_title, "an ordinary thread for comparison"),
        (cfg.hub.standing_title, "solo", "a long line to check the composer and "
         "the bubble wrapping behave with more than a few words in them"),
    ])

    print(f"seeded {store}")
    print("  win:mirrored  has evidence  -> should render normally")
    print("  wsl:mirrored  no evidence   -> should render with the ◌ relay badge")
    print("  win:solo      unpaired      -> should never be flagged")
    print("\nNote: the roster only shows the wsl side when a bridge feeds it, so")
    print("with [wsl] enabled = false the ghost pair will not appear. To check")
    print("the badge, set [wsl] enabled = true on the SHADOW only — its scratch")
    print("store keeps it away from anything real.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
