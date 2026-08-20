"""stt_fixups — the hub's canonical transcript replacement list.

Lifted from cx-ptt.py's hub_replacements()/fix_transcript() (Chris ruling
2026-08-17: ONE list, edited from the ping-hub app, applied by every mic) so
dictate.py applies the exact same corrections the hub mic applies. Matching is
kept IDENTICAL to the hub's replacements.py.

Difference from cx-ptt's copy: no cx.toml [settings.replacements] fallback —
dictate has no cx.toml settings object, so an absent store means "no pairs",
never a crash.
"""
import json
import os
import re
from pathlib import Path

def _hub_dir() -> Path:
    """Same derivation as stt_hubcfg: env from the launcher, else from home."""
    direct = os.environ.get("PING_HUB_STORE")
    if direct:
        return Path(direct)
    gbl = os.environ.get("PING_HUB_BASE_GBL")
    root = Path(gbl) if gbl else Path.home() / ".base-gbl"
    return root / ".base" / "hub"


HUB_REPLACEMENTS = _hub_dir() / "replacements.json"

# [(mtime, size), ordered pairs] — keyed on both, not mtime alone: a
# one-character correction inside the same second keeps the byte count moving
# even when the clock does not.
_hub_repl = [None, None]


def hub_replacements():
    """Ordered (wrong, right) pairs from the hub store. None = unreadable."""
    try:
        st = HUB_REPLACEMENTS.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        # store vanished for the atomic-replace instant: serve the last known
        # pairs rather than flapping to nothing for one transcript
        return _hub_repl[1]
    if _hub_repl[0] != key:
        try:
            with open(HUB_REPLACEMENTS, encoding="utf-8") as fh:
                doc = json.load(fh)
            pairs = [(str(p["from"]), str(p.get("to", "")))
                     for p in (doc.get("pairs") or [])
                     if isinstance(p, dict) and str(p.get("from", "")).strip()
                     and p.get("enabled", True)]
        except (OSError, ValueError, KeyError):
            return _hub_repl[1]   # unreadable: last known, never crash dictation
        _hub_repl[0] = key
        _hub_repl[1] = pairs
    return _hub_repl[1]


def fix_transcript(text):
    """Case-insensitive whole-word swaps applied in list order."""
    for wrong, right in (hub_replacements() or []):
        text = re.sub(r"(?i)\b" + re.escape(wrong) + r"\b", right, text)
    return text


# ── inline rules: "head list=headless*" ──────────────────────────────────────
# Mirrors ping_hub/replacements.py RULE_RE + add_rule. Two copies exist because
# these are two programs; if they drift, the same spoken sentence becomes a rule
# in the hub and a paste on the desktop.
#
# Saying (or typing) a message that is ONLY `<heard>=<meant>*` files the fix
# instead of pasting it, so a misheard word gets corrected in the three seconds
# after it happens rather than on the next trip into the settings modal.
RULE_RE = re.compile(r"^\s*(?P<heard>[^=\n]+?)\s*=\s*(?P<meant>[^\n]*?)\s*\*\s*$")


def parse_rule(text):
    """{"from","to"} for a message that IS a rule, else None."""
    m = RULE_RE.match(text or "")
    if not m:
        return None
    heard = m.group("heard").strip()
    if not heard:
        return None
    return {"from": heard, "to": m.group("meant").strip()}


def _write_store(doc):
    """tmp + replace, retried -- the hub reads this file and a torn write reads
    as a corrupt store, which it treats as an EMPTY one."""
    import time
    HUB_REPLACEMENTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = HUB_REPLACEMENTS.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    for attempt in range(6):
        try:
            tmp.replace(HUB_REPLACEMENTS)
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def add_rule(frm, to):
    """Upsert one pair into the hub's canonical store. An existing left side is
    UPDATED, never duplicated: two rules for one phrase means the first wins
    silently and the second looks broken."""
    frm = str(frm).strip()
    if not frm:
        return None
    try:
        with open(HUB_REPLACEMENTS, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or not isinstance(doc.get("pairs"), list):
            raise ValueError("bad store")
    except (OSError, ValueError):
        doc = {"version": 1, "imported_from_cx_toml": True, "pairs": []}
    for pair in doc["pairs"]:
        if isinstance(pair, dict) and str(pair.get("from", "")).strip().lower() == frm.lower():
            was = pair.get("to", "")
            pair["to"] = to
            pair["enabled"] = True
            if not _write_store(doc):
                return None
            return {"action": "updated", "from": frm, "to": to, "was": was}
    doc["pairs"].append({"from": frm, "to": to, "enabled": True})
    if not _write_store(doc):
        return None
    return {"action": "added", "from": frm, "to": to}


def consume_rule(text):
    """The whole interception: a rule is filed and reported, anything else is None."""
    rule = parse_rule(text)
    if rule is None:
        return None
    return add_rule(rule["from"], rule["to"])
