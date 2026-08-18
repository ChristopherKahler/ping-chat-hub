"""Parent/child accordion, and the tab split it changes.

Grouping is pure data, so it is tested as data by running the page's own
functions in node -- there is no Python for it. What IS hermetic here are the
invariants that a regression would break silently, and the one that matters
most is that no session can be absent from both tabs. The old filter,
`t.squad ? squad-tab : terminals`, made exactly that possible.
"""

from __future__ import annotations

import re

from ping_hub import daemon


def hub_html() -> str:
    return daemon.HTML.read_text(encoding="utf-8")


def code() -> str:
    """The script with comments stripped -- the comments here quote the very
    selectors and filters being asserted."""
    src = hub_html()
    src = src[src.index("<script"):]
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def test_no_session_can_be_absent_from_both_tabs():
    """MANDATORY (heron). Terminals now shows the whole roster; Squads shows
    every child. The old `!t.squad` filter meant a squad-tagged session
    appeared ONLY in Squads -- repurposing that tab without this would have
    made those sessions unreachable."""
    c = code()
    assert 'tab === "squad" ? roster.filter(t => t.parent) : roster.slice()' in c
    assert "!t.squad" not in c, "the exclusion filter is still there"


def test_squad_membership_survives_as_a_badge():
    """Ruling (c): the grouping goes, the information stays. falcon's
    leader-stays-in-Terminals fix is untouched -- it lives in engine.py."""
    assert 'class="badge squad"' in hub_html()


def test_the_squads_tab_is_children_not_squads():
    assert 'roster.filter(t => t.parent)' in code()


def test_grouping_is_one_level_deep():
    """relations.json is a flat map. A child that is itself a parent still
    renders under ITS parent -- arbitrary nesting is unreadable on a phone."""
    c = code()
    start = c.index("function groupByParent")
    body = c[c.index("{", start):c.index("function broodBadge")]
    assert "groupByParent(" not in body, "groupByParent recurses"


def test_an_orphan_renders_top_level():
    """A child whose parent is not in the roster must not vanish under a
    parent that is not there."""
    c = code()
    fn = c[c.index("function groupByParent"):]
    assert "byTitle.has(pk)" in fn and "continue" in fn


def test_a_collapsed_parent_still_shows_its_childrens_alerts():
    """A collapsed brood that swallows a screaming child is a defect."""
    c = code()
    fn = c[c.index("function broodBadge"):]
    fn = fn[:fn.index("function renderList")]
    assert "x.esc" in fn and "_kids" in fn


def test_collapse_state_persists_per_parent():
    c = code()
    assert "localStorage.setItem(ACC_KEY" in c and "localStorage.getItem(ACC_KEY" in c


def test_the_disclosure_is_a_thumb_target():
    """G0 section 3 approved >=44px. It shipped at 28 once, with the test
    written to match the code instead of the design -- both numbers moved
    together, so nothing caught it."""
    css = hub_html()
    rule = css[css.index(".disc {"):]
    rule = rule[:rule.index("}")]
    for prop in ("min-width", "min-height"):
        m = re.search(prop + r":\s*(\d+)px", rule)
        assert m and int(m.group(1)) >= 44, f"{prop} below the 44px approved in G0"


def test_collapsing_hides_children_without_hiding_them_from_squads():
    """Collapsed in Terminals is not the hidden tab, and Squads always shows
    every child."""
    c = code()
    # scoped to Terminals: Squads always shows every child, and the markers
    # outlive a render, so an unscoped skip hid children in the wrong tab
    assert 'if (tab === "term" && t._child && accCollapsed(t._under)) continue;' in c
    grp = c[c.index("if (tab === \"term\") items = groupByParent(items);"):]
    assert grp.index("list.innerHTML") > 0
