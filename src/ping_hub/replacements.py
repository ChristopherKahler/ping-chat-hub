"""Spoken-to-corrected word pairs, applied to every transcript.

Speech models mishear the same handful of words forever — proper nouns, product
names, anything not in their vocabulary. The fix is a substitution list, and the
only thing that matters about it is that there is exactly ONE, because a pair
you fixed in one place and not the other is worse than no fix at all: the same
sentence comes out differently depending on which microphone you used.

So this store is canonical (Chris ruling via heron, 2026-08-17). The hub owns
it, the hub's own STT pipeline applies it, and cx-ptt reads THIS instead of its
own `cx.toml [settings.replacements]` once it exists. Nothing writes cx.toml —
that removes any need to rewrite TOML the hub does not own, which Python cannot
do from the standard library anyway.

Matching is deliberately identical to cx-ptt's `fix_transcript`
(`cx-ptt.py:172`) — case-insensitive, whole-word, longest-standing behaviour
wins:

    re.sub(r"(?i)\\b" + re.escape(wrong) + r"\\b", right, text)

An ORDERED LIST, not a mapping: when two rules overlap the order decides the
outcome, so it is data, not an implementation detail. `enabled` lets a pair be
switched off without losing it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

VERSION = 1


def store_path(cfg) -> Path:
    return cfg.paths.base_store / "hub" / "replacements.json"


def _blank() -> dict:
    return {"version": VERSION, "imported_from_cx_toml": False, "pairs": []}


def load(cfg) -> dict:
    try:
        with open(store_path(cfg), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return _blank()
    if not isinstance(doc, dict) or not isinstance(doc.get("pairs"), list):
        return _blank()          # a corrupt store is an empty one, not a crash
    doc.setdefault("version", VERSION)
    doc.setdefault("imported_from_cx_toml", False)
    doc["pairs"] = [p for p in doc["pairs"]
                    if isinstance(p, dict) and str(p.get("from", "")).strip()]
    return doc


def save(cfg, doc: dict) -> Path:
    # stamp the envelope here rather than trusting every caller: cx-ptt is a
    # SEPARATE program reading this file, and a store without a version is one
    # it cannot reason about when the shape changes
    doc = {"version": VERSION,
           "imported_from_cx_toml": bool(doc.get("imported_from_cx_toml")),
           "pairs": normalise(doc.get("pairs"))}
    p = store_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    tmp.replace(p)               # atomic: cx-ptt polls this file
    return p


def normalise(pairs: list) -> list[dict]:
    """Accept what a UI sends; keep only what the engine can use."""
    out = []
    for raw in pairs or []:
        if not isinstance(raw, dict):
            continue
        frm = str(raw.get("from", "")).strip()
        if not frm:
            continue            # a rule with no left side matches nothing
        out.append({"from": frm,
                    "to": str(raw.get("to", "")),
                    "enabled": bool(raw.get("enabled", True))})
    return out


def apply(text: str, pairs: list) -> str:
    """Case-insensitive whole-word swaps, in list order.

    Byte-identical to cx-ptt's fix_transcript. If this drifts, the same pair
    produces different text depending on which microphone was used, which
    defeats the entire point of one store.
    """
    for pair in pairs or []:
        if not isinstance(pair, dict) or not pair.get("enabled", True):
            continue
        frm = str(pair.get("from", ""))
        if not frm:
            continue
        text = re.sub(r"(?i)\b" + re.escape(frm) + r"\b",
                      str(pair.get("to", "")), text)
    return text


def apply_for(cfg, text: str) -> str:
    return apply(text, load(cfg).get("pairs"))


# ── one-time migration off cx.toml ───────────────────────────────────────────
def read_cx_toml_pairs(cfg) -> list[dict]:
    """`[settings.replacements]` as an ordered list. TOML preserves file order
    through tomllib's dicts, and that order is behaviour here."""
    try:
        import tomllib
        with open(cfg.cx_ptt.cx_toml, "rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, ValueError):
        return []
    table = ((doc.get("settings") or {}).get("replacements") or {})
    return [{"from": str(k), "to": str(v), "enabled": True}
            for k, v in table.items() if str(k).strip()]


def migrate_if_needed(cfg) -> dict:
    """Import cx.toml's pairs ONCE, then never again.

    After this the cx.toml section is legacy: cx-ptt reads this store instead
    of it, so deleting a pair here genuinely deletes it. Layering the two would
    have left a deleted pair still firing from the old file, which is the whole
    reason this is an import rather than a merge.
    """
    doc = load(cfg)
    if doc.get("imported_from_cx_toml") or doc.get("pairs"):
        return doc
    imported = read_cx_toml_pairs(cfg)
    doc["imported_from_cx_toml"] = True
    doc["pairs"] = imported
    save(cfg, doc)
    return doc
