"""Model and effort chosen per launch, not only in settings.

The one that matters is the opus wrap. `opus` boots the 1M-context variant, and
that rule lived inside the settings branch. If the launcher override had gone
round it, picking `opus` in the spawn dialog would have booted a different
context window than picking `opus` in settings -- the same word on screen, a
different session, and nothing to notice afterwards except a context number
that looked wrong.
"""

from __future__ import annotations

import pytest

from ping_hub import daemon


def args(settings=None, model=None, effort=None):
    return daemon.model_effort_args(settings or {}, model, effort)


@pytest.fixture
def opus_1m_off(monkeypatch):
    monkeypatch.setattr(type(daemon.CFG.spawn), "opus_1m",
                        property(lambda self: False))


# -- precedence --------------------------------------------------------------
def test_the_launcher_choice_beats_settings():
    got = args({"spawn_model": "sonnet", "spawn_effort": "low"},
               model="fable", effort="max")
    assert got == ["--model", "fable", "--effort", "max"]


def test_no_launcher_choice_falls_back_to_settings():
    """The brief's guard: settings stay authoritative when the launcher picks
    nothing."""
    assert args({"spawn_model": "sonnet", "spawn_effort": "low"}) == \
        ["--model", "sonnet", "--effort", "low"]


def test_the_default_option_means_defer_not_a_value():
    """The dialog's "settings default" option sends "". It must mean defer,
    never a model whose name is the empty string."""
    assert args({"spawn_model": "sonnet", "spawn_effort": "low"},
                model="", effort="") == ["--model", "sonnet", "--effort", "low"]


def test_model_and_effort_are_chosen_independently():
    got = args({"spawn_model": "sonnet", "spawn_effort": "low"}, effort="max")
    assert got == ["--model", "sonnet", "--effort", "max"]


# -- the opus wrap, from BOTH sources ---------------------------------------
def test_opus_from_settings_boots_the_1m_variant():
    assert args({"spawn_model": "opus"}) == ["--model", "opus[1m]"]


def test_opus_from_the_launcher_boots_the_1m_variant_too():
    """The whole reason this touches the server. A launcher override that
    skipped the wrap would boot a quietly smaller context."""
    assert args({"spawn_model": "sonnet"}, model="opus") == ["--model", "opus[1m]"]


def test_the_wrap_is_only_for_opus():
    assert args({}, model="fable") == ["--model", "fable"]
    assert args({}, model="sonnet") == ["--model", "sonnet"]


def test_the_wrap_obeys_the_config_switch(opus_1m_off):
    assert args({}, model="opus") == ["--model", "opus"]


# -- refusing nonsense -------------------------------------------------------
@pytest.mark.parametrize("bad", ["haiku", "OPUS", "opus[1m]", "; rm -rf /", "4"])
def test_an_unrecognised_model_is_dropped_not_forwarded(bad):
    """The CLI would reject it and the tab would die on boot -- taking the
    error message with it, since the window closes too."""
    assert args({}, model=bad) == []


@pytest.mark.parametrize("bad", ["ultra", "XHIGH", "9", ""])
def test_an_unrecognised_effort_is_dropped_not_forwarded(bad):
    assert args({}, effort=bad) == []


def test_a_bad_launcher_value_does_not_silently_use_settings_instead():
    """Falling back would be worse than dropping: the operator asked for
    something specific and would get something else without being told."""
    assert args({"spawn_model": "sonnet"}, model="haiku") == []


# -- nothing configured ------------------------------------------------------
def test_nothing_anywhere_emits_no_flags():
    """Today's behaviour with empty settings. A regression here would pin
    every spawn to one model."""
    assert args({}) == []
    assert args({"spawn_model": "", "spawn_effort": ""}) == []


def test_settings_with_only_one_of_the_two_emits_only_that_flag():
    assert args({"spawn_effort": "high"}) == ["--effort", "high"]


# -- the wire ----------------------------------------------------------------
def test_the_spawn_endpoint_uses_the_shared_helper():
    from pathlib import Path
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    assert "model_effort_args(s, payload.get(\"model\")" in src
    # exactly one place builds these flags, or the two sources drift
    assert src.count('"--model"') == 1 and src.count('"--effort"') == 1


def test_the_gated_path_shares_the_same_args_list():
    """Both paths must get the controls. args is built before the branch --
    if someone moves it, this fails rather than the gated spawn silently
    ignoring the selects."""
    from pathlib import Path
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    body = src[src.index("model_effort_args(s, payload"):]
    body = body[:body.index("self._json({\"ok\": True, \"title\"")]
    assert "if gated:" in body, "the gated branch no longer follows the arg build"


def test_the_dialog_sends_both_and_remembers_them():
    from pathlib import Path
    html = Path(daemon.HTML).read_text(encoding="utf-8")
    for el in ('id="sp-model"', 'id="sp-effort"'):
        assert el in html
    assert 'model: p.querySelector("#sp-model").value' in html
    assert 'setv("#sp-model", saved.model)' in html


def test_the_dialog_offers_an_explicit_settings_default_option():
    """Not a blank first entry -- an option that says what it does, so
    choosing it is visible rather than looking like an unset control.

    Which field it belongs to now comes from the label above the control
    rather than from the option text, so the option no longer has to repeat
    it -- at 390px "model — settings d ⌄" was all that fit, which is how the
    launcher ended up unreadable.
    """
    from pathlib import Path
    html = Path(daemon.HTML).read_text(encoding="utf-8")
    assert html.count('<option value="">settings default</option>') == 2
    assert '<span class="splab">model</span>' in html
    assert '<span class="splab">effort</span>' in html
