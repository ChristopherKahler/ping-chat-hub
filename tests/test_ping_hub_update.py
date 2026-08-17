"""`ping-hub update` — the packaged install stays the canonical one.

Chris's own machine now runs the installed package rather than a repo checkout,
which only works if updating is one command that behaves the same for him and
for anyone else. These tests cover the parts that are wrong on someone else's
machine: where the source comes from, and what happens when it is missing or
when the daemon is live.

Nothing here installs, upgrades, or opens a socket.
"""

from __future__ import annotations

import json

import pytest

from ping_hub import cli, install
from ping_hub.config import Config

from test_ping_hub_config import StubProbe


def _cfg(raw=None, **kw) -> Config:
    return Config(raw or {}, probe=StubProbe(**kw))


# ── where update reinstalls from ─────────────────────────────────────────────
def test_source_is_recorded_by_pip_not_guessed():
    """PEP 610 direct_url.json is pip's own record of the install source."""
    payload = json.dumps({"dir_info": {}, "url": "file:///D:/code/ping-chat-hub"})
    got = install.installed_source(read_direct_url=lambda name: payload,
        list_dists=lambda: ["ping-chat-hub"])
    assert got.replace("\\", "/").endswith("/code/ping-chat-hub")
    assert not got.startswith("file:")     # a path pip can install from


def test_a_git_source_survives_unchanged():
    payload = json.dumps({"url": "https://github.com/owner/repo.git",
                          "vcs_info": {"vcs": "git"}})
    assert install.installed_source(
        read_direct_url=lambda name: payload,
        list_dists=lambda: ["ping-chat-hub"]) == "https://github.com/owner/repo.git"


def test_no_record_means_no_source_rather_than_a_guess():
    for payload in ("", "not json", json.dumps({"dir_info": {}})):
        assert install.installed_source(
            read_direct_url=lambda name: payload,
            list_dists=lambda: ["ping-chat-hub"]) == ""
    # and a package pip has no record of at all
    assert install.installed_source(read_direct_url=lambda name: "x",
                                    list_dists=lambda: []) == ""


def test_update_refuses_without_a_source_and_says_how_to_fix_it():
    with pytest.raises(install.InstallError, match="no update source"):
        cli.update_command(_cfg())


def test_update_uses_the_recorded_source():
    cfg = _cfg({"update": {"source": "D:/code/ping-chat-hub"}})
    cmd = cli.update_command(cfg)
    assert cmd[1:] == ["-m", "pip", "install", "--upgrade", "D:/code/ping-chat-hub"]


def test_an_explicit_source_beats_the_recorded_one():
    cfg = _cfg({"update": {"source": "D:/old"}})
    assert cli.update_command(cfg, "D:/new")[-1] == "D:/new"


def test_update_installs_with_the_running_interpreter():
    """Into THIS venv, never a global python — the interpreter that imported
    us is by definition the one the package lives in."""
    import sys
    cfg = _cfg({"update": {"source": "x"}})
    assert cli.update_command(cfg)[0] == sys.executable


# ── refusing to swap files under a live daemon ───────────────────────────────
def test_a_serving_daemon_is_detected():
    class Sock:
        def close(self):
            pass

    seen = []
    assert cli.daemon_is_running(
        _cfg(), connect=lambda h, p: seen.append((h, p)) or Sock()) is True
    assert seen == [("127.0.0.1", 7799)]


def test_a_wildcard_bind_is_probed_on_loopback():
    """0.0.0.0 is not an address you can connect TO."""
    seen = []

    def connect(h, p):
        seen.append(h)
        raise OSError("refused")

    cli.daemon_is_running(_cfg({"hub": {"bind": "0.0.0.0"}}), connect=connect)
    assert seen == ["127.0.0.1"]


def test_nothing_listening_is_not_running():
    def refused(h, p):
        raise OSError("connection refused")

    assert cli.daemon_is_running(_cfg(), connect=refused) is False


def test_a_specific_bind_is_probed_as_given():
    seen = []

    def connect(h, p):
        seen.append((h, p))
        raise OSError("refused")

    cli.daemon_is_running(_cfg({"hub": {"bind": "127.0.0.5", "port": 7801}}),
                          connect=connect)
    assert seen == [("127.0.0.5", 7801)]
