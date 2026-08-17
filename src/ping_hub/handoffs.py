"""Which handoff document, if any, belongs to a session.

`clear` reboots a session with a clean context, which is only useful if the
fresh one lands where the old one stood. base already tracks that: a handoff
doc per project, open or archived. This finds the open one tied to a codename
so the confirm modal can say WHICH document is about to be attached, and the
boot briefing can attach it.

Matching is containment plus the project column, not slug parsing. Measured on
real data: `2026-08-11-1730-raven-cx-cross-machine-bridge` has project
`cx-terminals`, so the slug's tail is a TOPIC and stripping the project off it
fails. Taking the token after the timestamp fails differently — a hyphenated
codename like `hub-package-for-al-builder` parses to `hub`. Containment
survives both, and the project column is authoritative on its own.
"""
from __future__ import annotations

import re

from ping_hub import proc

ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*(\w+)\s*\|")


def parse(text: str) -> list[dict]:
    """Rows of base's markdown table. Header and rule lines are skipped by
    requiring a date-shaped slug rather than by counting lines."""
    out = []
    for line in (text or "").splitlines():
        m = ROW.match(line)
        if not m or not re.match(r"^\d{4}-\d{2}-\d{2}-", m.group(1)):
            continue
        out.append({"slug": m.group(1), "project": m.group(2),
                    "status": m.group(3)})
    return out


def listing(base_bin: str = "base", run=None) -> list[dict]:
    run = run or proc.run
    try:
        r = run([base_bin, "handoff", "list"], capture_output=True, text=True,
                timeout=20)
    except Exception:
        return []
    return parse(getattr(r, "stdout", "") or "")


def for_session(rows: list[dict], codename: str, projects=None) -> dict | None:
    """The open handoff this session should resume from, or None.

    Prefers a slug naming the codename over a project-only match: two sessions
    can share a project, and handing one the other's document would be a
    confident wrong answer. When only project matches exist and there is more
    than one, that is ambiguous and the honest result is the newest by slug —
    slugs begin with a sortable timestamp, so newest is well defined.
    """
    projects = [p for p in (projects or []) if p]
    open_rows = [r for r in rows if r.get("status") == "open"]
    named = [r for r in open_rows if codename and codename in r["slug"]]
    if named:
        return {**sorted(named, key=lambda r: r["slug"])[-1], "via": "codename"}
    if projects:
        hit = [r for r in open_rows if r.get("project") in projects]
        if hit:
            # a shared-project doc may belong to a DIFFERENT session; the
            # caller has to be able to say so
            return {**sorted(hit, key=lambda r: r["slug"])[-1], "via": "project"}
    return None


def describe(match: dict | None) -> dict:
    """What the confirm modal shows. Absence is stated, never implied."""
    if not match:
        return {"found": False,
                "headline": "NO HANDOFF DETECTED",
                "detail": "the fresh session will start with no prior context."}
    via = match.get("via", "codename")
    # a project match may be another session's document — say which, because
    # "handoff ready" alone would hand someone else's context over silently
    label = "handoff ready" if via == "codename" else "handoff ready (project match)"
    note = ("" if via == "codename" else
            " This document is matched by shared project, not by this "
            "session's name, so check it is the right one.")
    return {"found": True, "slug": match["slug"], "project": match.get("project", ""),
            "via": via, "headline": label,
            "detail": f"the fresh session resumes from {match['slug']}.{note}"}
