"""Structural guards on the mobile layout.

The geometry itself is measured by ``tools/mobile_probe.mjs`` in a real
browser — that is the check that actually proves the page. These tests are the
hermetic half: they pin the handful of declarations whose ABSENCE caused the
2026-08-17 break, so a future edit that drops one fails here rather than on
Chris's phone.

Nothing here spawns a browser. The suite makes no subprocess and no network
call, and it stays that way.
"""

from __future__ import annotations

import re

import pytest

from ping_hub import daemon


def hub_html() -> str:
    return daemon.HTML.read_text(encoding="utf-8")


def css_regions() -> tuple[str, str]:
    """(base css, phone media-query css).

    Split by brace matching rather than by line, because the distinction is
    the whole point of the fix: ``#main{min-width:0}`` inside the media query
    left 768px still overflowing by 197px.
    """
    css = hub_html()
    css = css[css.index("<style"):css.index("</style>")]
    # comments first: the ones explaining these very rules quote selectors and
    # declarations, and a naive scan reads those as code
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    start = css.index("@media (max-width: 700px)")
    depth, i = 0, css.index("{", start)
    open_brace = i
    while True:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return css[:start] + css[i + 1:], css[open_brace + 1:i]


def rule_for(region: str, selector: str) -> str:
    """The declaration block of the LAST rule whose selector matches, since
    later rules win in the cascade."""
    found = ""
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", region):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            found += m.group(2)          # accumulate: rules can be split
    return found


# ── the root cause ───────────────────────────────────────────────────────────
def test_main_can_shrink_below_its_content():
    """A flex item defaults to min-width:auto, which forbids shrinking below
    min-content. One nowrap descendant then made #main 603px wide on a 412px
    phone, which pushed every right-aligned own message off screen."""
    base, _ = css_regions()
    assert re.search(r"min-width:\s*0", rule_for(base, "#main"))


def test_the_main_guard_is_not_inside_the_phone_media_query():
    """Measured: with the guard inside @media (max-width:700px), 768px still
    overflowed by 197px — the desktop sidebar returns to the flow there while
    #main keeps min-width:auto. Landscape phones and tablets need it too."""
    _, phone = css_regions()
    assert "#main" not in phone or not re.search(r"#main[^{]*\{[^}]*min-width", phone)


def test_messages_can_break_an_unbreakable_token():
    base, _ = css_regions()
    assert re.search(r"overflow-wrap:\s*(anywhere|break-word)", rule_for(base, ".msg"))


# ── the compose row ──────────────────────────────────────────────────────────
COMPOSE_CHILDREN = ["#cmdb", "#attachb", "#sendb", "#box", "#micb"]


def test_the_compose_markup_still_has_exactly_the_children_the_css_orders():
    """The break was an unnoticed FIFTH child in a row whose CSS budgeted for
    four. If someone adds a sixth, this fails and the layout gets revisited."""
    html = hub_html()
    compose = html[html.index('<div id="compose">'):]
    compose = compose[:compose.index("</div>")]
    ids = re.findall(r'id="([a-z]+)"', compose)
    interactive = [f"#{i}" for i in ids if f"#{i}" in COMPOSE_CHILDREN]
    assert sorted(interactive) == sorted(COMPOSE_CHILDREN)


@pytest.mark.parametrize("child", COMPOSE_CHILDREN)
def test_every_compose_child_has_an_explicit_phone_order(child):
    """#cmdb had none, so it fell to order 0 and sat in the row the media
    query had already divided between four other children."""
    _, phone = css_regions()
    assert re.search(r"order:\s*\d", rule_for(phone, child)), f"{child} has no phone order"


def test_the_phone_orders_are_unique():
    """Two children sharing an order is how #cmdb ended up beside #attachb."""
    _, phone = css_regions()
    orders = [re.search(r"order:\s*(\d+)", rule_for(phone, c)).group(1)
              for c in COMPOSE_CHILDREN]
    assert len(set(orders)) == len(orders), f"duplicate orders: {orders}"


def test_the_input_owns_its_own_row_on_the_phone():
    """flex-basis 100% in a wrapping row. Sharing one row with four buttons
    left it 108px at 412 and 78px at 320."""
    _, phone = css_regions()
    assert re.search(r"flex-wrap:\s*wrap", rule_for(phone, "#compose"))
    assert re.search(r"flex:\s*1 0 100%", rule_for(phone, "#box"))


def test_the_mic_hiding_focus_rule_is_gone_not_dormant():
    """It existed to buy the input room inside a single row. With the input on
    its own row it buys nothing, and a rule that fires for no reason is worse
    than no rule."""
    assert "#compose:has(#box:focus)" not in hub_html()
