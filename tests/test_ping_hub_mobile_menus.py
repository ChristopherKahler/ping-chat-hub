"""Menus you can close, a launcher that fits, and presets that survive a save.

Three things Chris hit from his phone inside one morning session, and the first
two turned out to be one bug eating itself. Measured 2026-08-19 at 360px: the
launcher popover was 508px wide, hung 76px off the left AND 72px off the right,
and covered 121% of the viewport — so tap-outside had no outside left, and the
only other way out was a key a phone does not have. He could open it and not
get rid of it.

Same posture as test_ping_hub_mobile.py: this is the hermetic half. It pins the
declarations and branches whose ABSENCE was the bug, so a future edit that drops
one fails here rather than on his phone. The geometry itself is measured for
real by ``tools/mobile_probe.mjs`` (popover inside the viewport, 44px close
control, Launch on screen, across seven widths) and the preset round-trip by
``tools/quicklaunch_probe.mjs`` against a shadow hub.

No subprocess, no network, no browser.
"""

from __future__ import annotations

import json

from ping_hub import daemon

from test_ping_hub_mobile import css_regions, hub_html, rule_for


def code() -> str:
    """Page script, comments stripped — the comments here quote the very
    selectors being asserted."""
    src = hub_html()
    src = src[src.index("<script"):]
    return "\n".join(l.split("//")[0] for l in src.splitlines())


# -- lock 1: every menu can be closed ----------------------------------------
def test_the_close_control_is_injected_by_showpop_not_by_callers():
    """All three callers omitted one. Putting it in the factory is what makes
    that impossible to repeat — a new menu cannot forget."""
    c = code()
    fn = c[c.index("function showPop"):c.index("function closePop")]
    assert "popbar" in fn and "popclose" in fn
    assert "closePop()" in fn, "the injected control does not close anything"


def test_dismissal_listens_for_touch_not_only_an_emulated_mouse():
    """On a phone the mouse event is an afterthought the browser synthesises,
    and this is the primary dismissal gesture on the primary surface."""
    c = code()
    assert 'addEventListener("pointerdown", popOutside)' in c
    assert 'addEventListener("mousedown", popOutside)' not in c
    assert 'removeEventListener("pointerdown", popOutside)' in c, "listener leaks"


def test_the_close_control_is_a_thumb_target_and_cannot_scroll_away():
    base, _ = css_regions()
    bar = rule_for(base, "#pop .popbar")
    btn = rule_for(base, "#pop .popbar button")
    assert "sticky" in bar, "a close button that scrolls out of reach is not one"
    assert "min-width: 44px" in btn and "min-height: 44px" in btn


def test_the_modal_family_keeps_its_cancel_within_thumb_reach():
    """Measured 80x33 and sitting below the fold inside the modal's own
    scroll."""
    _, phone = css_regions()
    actions = rule_for(phone, "#modal .actions")
    assert "sticky" in actions
    assert "min-height: 44px" in rule_for(phone, "#modal .actions button")


# -- lock 2: the launcher fits -----------------------------------------------
def test_the_popover_can_never_be_wider_than_the_screen():
    """A cap in CSS, not only a clamp in JS: the width is measured BEFORE the
    async selects fill, so an honestly-positioned box can still grow past the
    edge afterwards. mobile_probe caught exactly that at 768px."""
    base, _ = css_regions()
    assert "max-width: calc(100vw - 16px)" in rule_for(base, "#pop")


def test_the_position_clamp_has_a_floor():
    """`Math.min(x, innerWidth - width - 12)` goes negative the moment the box
    is wider than the viewport. That is how it reached left:-76px."""
    c = code()
    assert "Math.max(8, Math.min(x," in c
    assert "Math.max(8, Math.min(y," in c


def test_the_phone_gets_a_sheet_positioned_by_css_not_by_inline_styles():
    """Inline styles beat a stylesheet, so the fix has to remove the inline
    positioning rather than try to out-specify it."""
    c = code()
    fn = c[c.index("function showPop"):c.index("function closePop")]
    assert 'classList.add("sheet")' in fn
    assert "onPhone()" in fn
    # the inline positioning must sit in the else branch, after the sheet return
    assert fn.index('classList.add("sheet")') < fn.index("p.style.left")


def test_the_sheet_is_pinned_to_both_edges_and_leaves_an_outside_to_tap():
    """A width-only fix would still be positioned off-screen, and a
    full-bleed sheet would delete the tap-outside gesture it is meant to
    rescue."""
    _, phone = css_regions()
    sheet = rule_for(phone, "#pop.sheet")
    assert "left: 8px" in sheet and "right: 8px" in sheet
    assert "max-width: none" in sheet


def test_the_rows_that_made_it_508px_wide_are_allowed_to_wrap():
    _, phone = css_regions()
    assert "flex-wrap: wrap" in rule_for(phone, "#pop.sheet .row.spr")
    assert "max-width: 100%" in rule_for(phone, "#pop.sheet .spsel")


def test_launch_is_pinned_rather_than_nine_rows_down():
    """Measured at y=866 on a 780px screen. "Reachable with a thumb" means
    pinned, not merely present."""
    _, phone = css_regions()
    assert "sticky" in rule_for(phone, "#pop.sheet .popact")
    assert 'class="row spr popact"' in hub_html()


# -- lock 3: quick launch ----------------------------------------------------
def test_presets_persist_because_settings_stores_whatever_it_is_handed():
    """The claim the whole feature rests on, tested rather than asserted: no
    key whitelist, so quick_launch needs nothing on the server."""
    before = daemon.hub_settings()
    try:
        preset = {"id": "ql-1", "name": "wsl builder",
                  "payload": {"side": "wsl", "gated": True}}
        daemon.save_hub_settings({**before, "quick_launch": [preset]})
        assert daemon.hub_settings()["quick_launch"] == [preset]
    finally:
        daemon.save_hub_settings(before)


def test_saving_settings_no_longer_wipes_the_keys_it_has_no_widget_for():
    """It rebuilt hubCfg from a literal naming eight keys, and the object also
    carries card_order, cmd_favs, hidden, spawn_pinned and now quick_launch.
    Every Save silently threw those away, and /api/settings stored the wipe."""
    c = code()
    assert "hubCfg = { ...hubCfg, sounds," in c
    assert "hubCfg = { sounds," not in c


def test_a_preset_sends_no_empty_keys():
    """/api/spawn derives its own defaults, and "" is not the same thing to it
    as absent."""
    c = code()
    fn = c[c.index("function qlClean"):c.index("async function doSpawn")]
    assert 'v === ""' in fn and "Array.isArray(v) && !v.length" in fn


def test_one_tap_and_a_filled_form_take_the_same_spawn_path():
    """The booting card, its bridge-aware waiting state and the pinned-codename
    watch all hang off doSpawn. Two copies would drift, and the one that
    drifted would be the one Chris uses from his phone."""
    c = code()
    assert c.count("async function doSpawn") == 1
    assert "await doSpawn(formPayload())" in c
    assert "await doSpawn(qlClean(" in c
    # and the old inline copy inside the Launch handler is gone
    assert c.count("spawnWatch = { side, cwd: payload.cwd") == 1


def test_the_settings_editor_round_trips_what_it_cannot_edit():
    """It has no widget for prompt, forks or handoffs, and the launcher capture
    path can set all three. Reading only the visible fields would quietly gut
    a captured preset on the next Save."""
    c = code()
    fn = c[c.index("function readQuickRows"):c.index("async function doSpawn")]
    for key in ("prompt", "forks", "handoffs"):
        assert f"dataset.{key}" in fn, f"{key} is dropped on the next Save"


def test_a_preset_is_reachable_in_one_tap_from_the_launcher():
    c = code()
    assert 'id="sp-quick"' in c
    assert "qlbtn" in c
    # the quick row is rendered BEFORE the form it exists to avoid filling in
    assert c.index('id="sp-quick"') < c.index('id="sp-sides"')


def test_the_capture_button_and_the_settings_panel_share_one_store():
    """Two editors, one array. A second store is how they start disagreeing."""
    c = code()
    assert c.count("hubCfg.quick_launch") >= 2
    assert "quick_launch: readQuickRows(ov)" in c
