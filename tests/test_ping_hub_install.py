"""Capabilities reporting and the provisioning pipeline.

Hermetic: no network, no venv build, no model download. What is tested is the
logic that decides WHAT to do and how failures are reported — the parts that
are wrong on someone else's machine, not the parts that take 800 MB to run.
The download and venv steps are exercised for real by `ping-hub doctor` after
an install; that is live-parked, not faked here.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from ping_hub import capabilities as caps
from ping_hub import install
from ping_hub.config import Config

from test_ping_hub_config import StubProbe, SAY_CMD


def _cfg(raw=None, **kw) -> Config:
    return Config(raw or {}, probe=StubProbe(**kw))


# ── the four states are four different facts ─────────────────────────────────
def test_tts_absent_and_error_are_not_the_same_answer():
    """Nothing installed vs configured-but-missing need different fixes, so
    they must not collapse into one falsy 'no'."""
    nothing = _cfg(files={})
    assert caps.tts(nothing)["state"] == caps.ABSENT

    pointed = _cfg({"tts": {"command": ["cmd", "/c", "X:/gone/say.cmd"]}}, files={})
    assert caps.tts(pointed)["state"] == caps.ERROR
    assert "X:/gone/say.cmd" in caps.tts(pointed)["detail"]


def test_off_is_reserved_for_a_human_decision():
    c = _cfg({"tts": {"enabled": False}, "stt": {"enabled": False}})
    assert caps.tts(c)["state"] == caps.OFF
    assert caps.stt(c)["state"] == caps.OFF


def test_stt_ready_only_when_something_answers():
    c = _cfg()
    assert caps.stt(c, reach=lambda u, timeout=2.0: (True, ""))["state"] == caps.READY
    dead = caps.stt(c, reach=lambda u, timeout=2.0: (False, "refused"))
    assert dead["state"] == caps.ERROR and "refused" in dead["detail"]


def test_a_4xx_still_proves_a_server_is_listening():
    """The bridge answers GET / with a health blob, but a future version
    returning 404 there must not read as 'server down'."""
    import urllib.error

    def fake(url, timeout=2.0):
        raise urllib.error.HTTPError(url, 404, "nf", None, None)

    assert caps._reachable("http://x/", 0.1)[0] is False   # transport failure path
    # and the real helper treats an HTTPError as reachable:
    import ping_hub.capabilities as m
    orig = m.urllib.request.urlopen
    m.urllib.request.urlopen = fake
    try:
        assert m._reachable("http://x/")[0] is True
    finally:
        m.urllib.request.urlopen = orig


def test_cx_ptt_absent_is_not_reported_as_a_human_switching_it_off():
    """Both derived-enabled capabilities share this trap: `enabled` falls out
    of whether the file exists, so probing it first turns "never installed"
    into "someone turned it off". Found live by a shadow run against a scratch
    store, where every derived path is legitimately absent."""
    nothing = _cfg(files={})
    assert caps.cx_ptt(nothing, exists=lambda p: False)["state"] == caps.ABSENT
    off = _cfg({"cx_ptt": {"enabled": False}})
    assert caps.cx_ptt(off, exists=lambda p: True)["state"] == caps.OFF
    broken = _cfg({"cx_ptt": {"enabled": True}})
    got = caps.cx_ptt(broken, exists=lambda p: str(p).endswith("cx.toml"))
    assert got["state"] == caps.ERROR and "cx-slot" in got["detail"]


def test_wsl_absent_on_a_one_sided_machine():
    assert caps.wsl(_cfg(distro="", wsl_home=""))["state"] == caps.ABSENT
    assert caps.wsl(_cfg({"wsl": {"enabled": False}}))["state"] == caps.OFF
    assert caps.wsl(_cfg(wsl_home=""))["state"] == caps.ERROR   # distro, no home


def test_probe_all_covers_every_surface():
    got = caps.probe_all(_cfg())
    assert set(got) == {"stt", "tts", "cx_ptt", "wsl", "base"}
    assert all(r["state"] in (caps.READY, caps.ABSENT, caps.ERROR, caps.OFF)
               for r in got.values())


# ── archive handling (the bsdtar/bzip2 trap, without a 460 MB download) ──────
def test_bz2_extraction_uses_stdlib_not_an_external_tar(tmp_path):
    """Windows bsdtar cannot decompress bzip2 without an external binary that
    ships with Git, not with Windows. This must work with PATH empty."""
    src = tmp_path / "model"
    src.mkdir()
    for n in install.STT_MEMBERS:
        (src / n).write_text("x", encoding="utf-8")
    arc = tmp_path / "m.tar.bz2"
    with tarfile.open(arc, "w:bz2") as tf:
        tf.add(src, arcname="sherpa-onnx-nemo-parakeet")
    out = install.extract_bz2(arc, tmp_path / "out", log=lambda m: None)
    install.verify_members(out)
    assert (out / "tokens.txt").is_file()


def test_extraction_refuses_paths_that_escape_the_target(tmp_path):
    arc = tmp_path / "evil.tar.bz2"
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    with tarfile.open(arc, "w:bz2") as tf:
        tf.add(victim, arcname="../victim.txt")
    with pytest.raises(install.InstallError, match="unsafe archive path"):
        install.extract_bz2(arc, tmp_path / "out", log=lambda m: None)
    assert victim.read_text(encoding="utf-8") == "original"


def test_wrong_model_layout_fails_loudly_with_what_it_found(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "README.md").write_text("x", encoding="utf-8")
    with pytest.raises(install.InstallError) as e:
        install.verify_members(d)
    assert "encoder.int8.onnx" in str(e.value) and "README.md" in str(e.value)


def test_a_partial_download_never_lands_as_the_real_file(tmp_path, monkeypatch):
    class Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        headers = {"Content-Length": "100"}
        def read(self, n): raise OSError("connection reset")

    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: Boom())
    dest = tmp_path / "model.onnx"
    with pytest.raises(install.InstallError, match="download failed"):
        install.download("http://x/model.onnx", dest, log=lambda m: None)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.part"))


# ── the written config ───────────────────────────────────────────────────────
def test_render_writes_only_what_was_decided():
    """A config full of restated defaults goes stale silently; absent keys keep
    deriving."""
    text = install.render_hub_toml({
        "paths": {"hub_home": r"C:\x\.ping-hub"},
        "stt": {"launcher": [r"C:\x\.ping-hub\stt\start-stt.cmd"]},
        "tts": {},
    })
    assert "[paths]" in text and "[stt]" in text
    assert "[tts]" not in text            # empty section is not written
    assert "bind" not in text and "port" not in text
    import tomllib
    doc = tomllib.loads(text)
    assert doc["stt"]["launcher"] == [r"C:\x\.ping-hub\stt\start-stt.cmd"]
    assert doc["paths"]["hub_home"] == r"C:\x\.ping-hub"


def test_written_config_round_trips_into_the_resolver(tmp_path):
    p = tmp_path / "hub.toml"
    install.write_hub_toml(p, {"hub": {"port": 7801},
                               "tts": {"command": ["cmd", "/c", SAY_CMD]}},
                           log=lambda m: None)
    from ping_hub import config as cfgmod
    c = cfgmod.load(p, probe=StubProbe())
    assert c.hub.port == 7801
    assert c.tts.command == ["cmd", "/c", SAY_CMD]


def test_rewriting_a_config_keeps_the_old_one(tmp_path):
    p = tmp_path / "hub.toml"
    p.write_text("[hub]\nport = 1\n", encoding="utf-8")
    install.write_hub_toml(p, {"hub": {"port": 2}}, log=lambda m: None)
    assert (tmp_path / "hub.toml.bak").read_text(encoding="utf-8") == "[hub]\nport = 1\n"


def test_launcher_carries_the_model_path_with_it(tmp_path):
    """hub.toml points at the launcher; the launcher knows where the model is.
    One thing to configure, not two that can disagree."""
    L = install._launcher(tmp_path / "say", tmp_path / "py.exe",
                          tmp_path / "say.py", {"PING_HUB_TTS_MODEL": "M:/m"})
    assert "M:/m" in L.read_text(encoding="utf-8")
    assert "say.py" in L.read_text(encoding="utf-8")


# ── the vendored WSL bridge ──────────────────────────────────────────────────
def test_bridge_ships_inside_the_package():
    """It used to be a loose repo directory, which does not survive a pip
    install — the VENDOR ruling makes it package data."""
    assert install.BRIDGE_SRC.is_file()
    assert install.BRIDGE_SRC.parent.name == "bridge"
    assert "hub-bridge.toml" in install.BRIDGE_SRC.read_text(encoding="utf-8")


def test_deploy_writes_the_script_and_its_config_with_lf_endings(tmp_path):
    """The file is run by WSL's Python and read by a Linux shell; Windows text
    mode would put CRLF in both."""
    cfg = _cfg()
    got = install.deploy_bridge(cfg, deploy_unc=str(tmp_path / "deploy"),
                                config_unc=str(tmp_path / "home"),
                                log=lambda m: None)
    script = Path(got["script"])
    conf = Path(got["config"])
    assert script.name == "wsl-bridge.py" and script.is_file()
    assert conf == tmp_path / "home" / ".config" / "hub-bridge.toml"
    assert b"\r\n" not in script.read_bytes()
    assert b"\r\n" not in conf.read_bytes()
    import tomllib
    doc = tomllib.loads(conf.read_text(encoding="utf-8"))
    assert doc["bridge"]["port"] == 7798
    assert doc["bridge"]["standing_title"] == "chris"


def test_deployed_config_matches_what_the_bridge_actually_reads(tmp_path):
    """The writer and the reader must agree on the table name and the keys,
    or the bridge silently falls back to defaults."""
    got = install.deploy_bridge(_cfg({"wsl": {"bridge_port": 7999}}),
                                deploy_unc=str(tmp_path / "d"),
                                config_unc=str(tmp_path / "h"),
                                log=lambda m: None)
    src = Path(got["script"]).read_text(encoding="utf-8")
    assert 'tomllib.load(fh).get("bridge")' in src
    for key in ("port", "standing_title"):
        assert f'_C.get("{key}")' in src
    import tomllib
    doc = tomllib.loads(Path(got["config"]).read_text(encoding="utf-8"))
    assert doc["bridge"]["port"] == 7999


def test_deploy_refuses_on_a_machine_with_no_wsl():
    with pytest.raises(install.InstallError, match="no WSL side"):
        install.deploy_bridge(_cfg(distro="", wsl_home=""))


def test_model_urls_are_pinned_to_release_assets():
    """A moving 'latest' link turns a reproducible install into a lottery."""
    for u in (install.STT_MODEL_URL, install.TTS_MODEL_URL, install.TTS_VOICES_URL):
        assert u.startswith("https://")
        assert "/releases/download/" in u
        assert "latest" not in u
