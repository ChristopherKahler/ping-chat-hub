"""The personal inbox — the one thread that is not a conversation.

Every other thread in this app points at a session: a ping goes out, something
reads it, something answers. The screen you land on at boot points at nothing,
and until now anything typed there was silently discarded.

So it becomes the place notes go. A thought worth keeping, a line to paste
later, or a dictation drill — say a sentence, see exactly what the mic heard,
correct it with a `heard=meant*` rule, say it again. That loop needs somewhere
to talk to that is not a person's terminal, and this is it.

Append-only JSONL, like the dictation history and for the same reason: these
are words already said, and rewriting the file to remove one risks the rest.
A delete rewrites through a temp file and a rename, never in place.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

VERSION = 1
KEEP = 5000          # a hard ceiling, far above use; notes are small


def path(cfg) -> Path:
    return cfg.paths.base_store / "hub" / "inbox.jsonl"


def add(cfg, text: str, source: str = "") -> dict | None:
    """Append one note. Empty text is not a note."""
    text = (text or "").strip()
    if not text:
        return None
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "text": text,
           "words": len(text.split())}
    if source:
        row["source"] = source
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def read(cfg, limit: int = 500) -> list[dict]:
    """Newest first. A torn last line is skipped, never fatal — a note may be
    being appended by another process while this one reads."""
    try:
        with open(path(cfg), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("text"):
            out.append(row)
        if limit and len(out) >= limit:
            break
    return out


def remove(cfg, ts: str, text: str = "") -> int:
    """Drop the note stamped `ts`. Returns how many lines went.

    Matched on the timestamp AND, when given, the text: two notes can share a
    second, and deleting the wrong one is not recoverable from a file that has
    no undo.
    """
    p = path(cfg)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    kept, gone = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        hit = (row.get("ts") == ts and (not text or row.get("text") == text))
        if hit and not gone:            # exactly one, even on a tie
            gone += 1
            continue
        kept.append(line)
    if not gone:
        return 0
    tmp = p.with_suffix(".tmp")
    tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    for attempt in range(6):
        try:
            tmp.replace(p)
            return gone
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    return 0
