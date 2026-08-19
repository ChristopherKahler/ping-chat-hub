"""The settings panel: it must open now, and open once.

Two faults, one panel. It took ~20s to appear with no feedback, because the
voice list is resolved by shelling to kokoro — which, when its own daemon is
down, starts it and polls for up to 30s while a 310MB model loads. And every
impatient click during that wait opened ANOTHER modal: four clicks measured
four stacked overlays.

The browser half is measured by tools/settings_probe.mjs. These are the
hermetic guards: no subprocess, no network, no browser.
"""

from __future__ import annotations

import threading

import pytest

from ping_hub import daemon


@pytest.fixture(autouse=True)
def _clear_cache():
    daemon._VOICES = None
    yield
    daemon._VOICES = None


class _Ran:
    """A stand-in for a completed `say --voices`."""
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.returncode = 0


# ── the resolver ─────────────────────────────────────────────────────────────
def test_a_slow_engine_is_ask_again_not_no_voices(monkeypatch):
    """None and [] are different facts. Handing back the single default as
    though it were the whole list makes a stall look like a one-voice install
    and sends someone hunting a fault that is not there."""
    import subprocess

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="say", timeout=k.get("timeout", 2))

    monkeypatch.setattr(daemon.proc, "run", slow)
    assert daemon.resolve_voices(2.0) is None


def test_an_absent_engine_is_a_final_answer_not_a_stall(monkeypatch):
    """No TTS configured is knowable immediately — it must not read as
    'warming' forever on a machine that will never have voices."""
    monkeypatch.setattr(type(daemon.CFG.tts), "command", property(lambda self: []))
    assert daemon.resolve_voices(2.0) == [daemon.CFG.tts.default_voice]


def test_the_resolver_parses_and_sorts_the_names(monkeypatch):
    monkeypatch.setattr(daemon.proc, "run",
                        lambda *a, **k: _Ran("af_heart\nam_adam\naf_bella\n"))
    assert daemon.resolve_voices(2.0) == ["af_bella", "af_heart", "am_adam"]


def test_output_with_no_voice_names_falls_back_rather_than_empty(monkeypatch):
    monkeypatch.setattr(daemon.proc, "run", lambda *a, **k: _Ran("error: nope"))
    assert daemon.resolve_voices(2.0) == [daemon.CFG.tts.default_voice]


def test_the_request_path_timeout_is_short_and_the_warm_one_is_not():
    """The whole fix: a request answers, a background warm waits."""
    assert daemon.VOICE_REQUEST_TIMEOUT <= 3
    assert daemon.VOICE_WARM_TIMEOUT >= 30      # kokoro's own bound is 30s


def test_the_request_path_never_uses_the_long_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon, "resolve_voices", lambda t: seen.append(t) or ["af_heart"])
    daemon.warm_voices()
    assert seen == [daemon.VOICE_WARM_TIMEOUT]


# ── the boot warm: non-blocking and absent-tolerant (heron's conditions) ─────
def test_the_warm_does_not_block_the_caller(monkeypatch):
    """heron's condition, pinned by test rather than by intent: the daemon
    binds and serves while this is still running."""
    release, entered = threading.Event(), threading.Event()

    def blocking(_timeout):
        entered.set()
        release.wait(5)
        return ["af_heart"]

    monkeypatch.setattr(daemon, "resolve_voices", blocking)
    t = daemon.start_voice_warm()
    assert entered.wait(2), "the warm never started"
    assert t.is_alive(), "start_voice_warm waited for it to finish"
    assert daemon._VOICES is None, "it published a result before it had one"
    release.set()
    t.join(5)
    assert daemon._VOICES == ["af_heart"]


def test_the_warm_thread_is_a_daemon_thread(monkeypatch):
    """It must never hold the process open at shutdown — a 60s voice lookup
    outliving the hub is worse than no warm at all.

    The resolver is injected: left real, this test spawned kokoro for a
    subprocess the suite promises not to make.
    """
    monkeypatch.setattr(daemon, "resolve_voices", lambda t: ["af_heart"])
    t = daemon.start_voice_warm()
    assert t.daemon is True
    t.join(5)


def test_a_machine_with_no_engine_warms_silently(monkeypatch):
    """heron's second condition: Albert's install must not care whether he
    owns a speech engine. Absent is a no-op, never a boot error."""
    boom = []
    monkeypatch.setattr(daemon, "resolve_voices",
                        lambda t: (_ for _ in ()).throw(OSError("no kokoro here")))
    monkeypatch.setattr(threading, "excepthook", lambda a: boom.append(a))
    t = daemon.start_voice_warm()
    t.join(5)
    assert boom == [], "a missing engine raised out of the warm thread"
    assert daemon._VOICES is None


def test_a_timed_out_warm_leaves_the_cache_open_for_a_retry(monkeypatch):
    """None must not be cached as an answer, or one slow boot poisons the
    voice list for the life of the daemon."""
    monkeypatch.setattr(daemon, "resolve_voices", lambda t: None)
    daemon.start_voice_warm().join(5)
    assert daemon._VOICES is None


# ── the wire ─────────────────────────────────────────────────────────────────
def test_the_daemon_starts_the_warm_before_it_binds():
    """A built-but-uncalled warm looks exactly like a working one."""
    from pathlib import Path
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    body = src[src.index("def main()"):]
    assert "start_voice_warm()" in body
    # `srv = ` rather than the class name: the server class became _Server when
    # the single-instance guard added an exclusive-bind posture, and this test
    # is about ORDER, not about which class does the binding
    assert body.index("start_voice_warm()") < body.index("srv = ")


def test_the_settings_panel_opens_before_it_has_data():
    """The shell is appended, THEN the fetches are awaited. Awaiting first is
    what made a slow voice list look like a broken button."""
    from pathlib import Path
    html = Path(daemon.HTML).read_text(encoding="utf-8")
    h = html[html.index('getElementById("gear").onclick'):]
    h = h[:h.index("// ── resizable sidebar")]
    assert h.index("document.body.appendChild(ov)") < h.index("await Promise.all")
    # comments first: the ones explaining this very fix quote the endpoint
    code = chr(10).join(l.split("//")[0] for l in h.splitlines())
    assert "/api/voices" not in code[:code.index("document.body.appendChild(ov)")]


def test_a_second_click_focuses_the_open_panel_instead_of_stacking_one():
    from pathlib import Path
    html = Path(daemon.HTML).read_text(encoding="utf-8")
    h = html[html.index('getElementById("gear").onclick'):]
    guard = h[:h.index("document.body.appendChild(ov)")]
    assert "#overlay.settings" in guard and "return" in guard
