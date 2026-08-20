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

# marks a pair this app generated from a star-command, so a future "remove
# imported" can spare everything Chris typed by hand. Hand-added pairs never
# carry it, and neither do the cx.toml migration's — those are his, not ours.
ORIGIN_IMPORT = "import"

# the word a speech model puts where Chris said "*"
STAR_WORD = "star"


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
    """Accept what a UI sends; keep only what the engine can use.

    `origin` is emitted ONLY when it is set. A hand-added pair is exactly
    `{from,to,enabled}` and stays that way — the marker is something a pair
    acquires by being generated, not a field every pair carries.
    """
    out = []
    for raw in pairs or []:
        if not isinstance(raw, dict):
            continue
        frm = str(raw.get("from", "")).strip()
        if not frm:
            continue            # a rule with no left side matches nothing
        pair = {"from": frm,
                "to": str(raw.get("to", "")),
                "enabled": bool(raw.get("enabled", True))}
        origin = str(raw.get("origin", "")).strip()
        if origin:
            pair["origin"] = origin
        out.append(pair)
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


# ── star-commands as spoken fixes ────────────────────────────────────────────
# Chris dictates his star-commands aloud and every speech model renders "*end"
# as "star end". Rather than hand-writing a pair per command, generate them:
# heard "star end" -> sent "*end".
def _key(text) -> str:
    """Identity of a rule's left side: case and spacing are not part of it.

    Matching is already case-insensitive, and "star  end" and "star end" are
    the same rule to a reader — so they must be the same rule to an import, or
    a second run adds a duplicate that does nothing.
    """
    return " ".join(str(text).split()).casefold()


def _spoken(name) -> str:
    """A command name the way it comes out of a microphone: separators are
    heard as spaces, and runs of whitespace collapse."""
    return " ".join(str(name).replace("-", " ").replace("_", " ").split())


def generate_pair(name) -> dict | None:
    """One command -> one pair, or None when the name cannot make a rule.

    The guard is not cosmetic. A name that is blank, whitespace, or nothing but
    separators collapses to the bare word "star" — and a rule rewriting the
    standalone word "star" would corrupt ordinary dictation everywhere. The
    command feed does not stop this: its filter is `if c.get("name")`, and a
    single space is truthy.
    """
    spoken = _spoken(name)
    if not spoken:
        return None
    sent = str(name).strip()
    if not sent:
        return None
    return {"from": f"{STAR_WORD} {spoken}", "to": "*" + sent,
            "enabled": True, "origin": ORIGIN_IMPORT}


def command_pairs(names) -> list[dict]:
    """Generated pairs for `names`, in the order given, skipping the unusable."""
    out = []
    for name in names or []:
        pair = generate_pair(name)
        if pair is not None:
            out.append(pair)
    return out


def merge_imports(existing: list, generated: list) -> tuple[list[dict], int, int]:
    """Append every generated pair whose left side is not already present.

    Returns (pairs, added, skipped).

    APPEND, never prepend, and never rewrite. Order is behaviour in this list:
    a generated generic rule placed ahead of a longer hand-written one would
    eat it — with "star handoff" -> "*handoff" running first, a hand-written
    "star handoff to chris" can never match again. Appending puts everything
    generated behind everything Chris wrote himself.

    A left side that already exists is SKIPPED UNTOUCHED whatever its origin,
    enabled state, or position. Importing is how new rules arrive; it is never
    how an existing rule changes.
    """
    pairs = normalise(existing)
    seen = {_key(p["from"]) for p in pairs}
    added = skipped = 0
    for gen in generated or []:
        key = _key(gen.get("from", ""))
        if not key or key in seen:
            skipped += 1
            continue
        seen.add(key)
        pairs.append(dict(gen))
        added += 1
    return pairs, added, skipped


def import_commands(cfg, existing: list, names) -> dict:
    """Merge generated pairs into `existing` and SAVE.

    `existing` is what the settings modal currently holds, not what is on disk,
    so anything typed and not yet saved survives the import instead of being
    silently discarded. The caller re-syncs its list from `pairs` in the reply,
    which is what stops a later Save from clobbering the rows just added.
    """
    pairs, added, skipped = merge_imports(existing, command_pairs(names))
    doc = load(cfg)
    doc["pairs"] = pairs
    doc["imported_from_cx_toml"] = True   # editing here supersedes the toml
    save(cfg, doc)
    return {"ok": True, "pairs": load(cfg)["pairs"],
            "added": added, "skipped": skipped}


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


# ── inline rules: "head list=headless*" ──────────────────────────────────────
# The list is only worth having if adding to it costs nothing. It used to cost
# a trip into the settings modal, which Chris was making EVERY time a word came
# out wrong -- so the fix competed with the work and usually lost. This is the
# same edit expressed as a message: type or say the whole thing and nothing
# else, and the message becomes the rule instead of being sent.
#
#     head list=headless*
#     ^ heard      ^ meant  ^ the marker
#
# The trailing `*` is what separates a rule from an ordinary sentence that
# happens to contain an `=`. Requiring the message to be ONLY the rule is the
# second guard: a paragraph ending in a star is still a paragraph.
RULE_RE = re.compile(r"^\s*(?P<heard>[^=\n]+?)\s*=\s*(?P<meant>[^\n]*?)\s*\*\s*$")


def parse_rule(text: str) -> dict | None:
    """{"from","to"} for a message that IS a rule, else None.

    Splits on the FIRST `=` -- the left side is what the model heard, and a
    misheard phrase is far more likely to contain an `=` than a correction is.
    An empty right side is allowed on purpose: `um=*` deletes a filler word.
    """
    m = RULE_RE.match(text or "")
    if not m:
        return None
    heard = m.group("heard").strip()
    if not heard:
        return None
    return {"from": heard, "to": m.group("meant").strip()}


def add_rule(cfg, frm: str, to: str) -> dict:
    """Upsert one pair. Matching an existing left side UPDATES it rather than
    appending a second rule for the same word -- two rules for one phrase means
    the first silently wins and the second looks broken."""
    frm = str(frm).strip()
    if not frm:
        return {"ok": False, "detail": "nothing on the left of the ="}
    doc = load(cfg)
    pairs = doc.get("pairs") or []
    for pair in pairs:
        if str(pair.get("from", "")).strip().lower() == frm.lower():
            was = pair.get("to", "")
            pair["to"] = to
            pair["enabled"] = True      # re-arm a rule that had been switched off
            save(cfg, doc)
            return {"ok": True, "action": "updated", "from": frm, "to": to,
                    "was": was, "count": len(pairs)}
    pairs.append({"from": frm, "to": to, "enabled": True})
    doc["pairs"] = pairs
    save(cfg, doc)
    return {"ok": True, "action": "added", "from": frm, "to": to,
            "count": len(pairs)}


def consume_rule(cfg, text: str) -> dict | None:
    """The whole interception in one call: rule -> applied, anything else -> None."""
    rule = parse_rule(text)
    if rule is None:
        return None
    return add_rule(cfg, rule["from"], rule["to"])
