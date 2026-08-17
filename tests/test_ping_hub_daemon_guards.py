"""Guards on the extracted WSL paths.

Before extraction these were string constants that were always non-empty, so
"what if there is no WSL side" was not a state the code could reach. Deriving
them introduced it: an unresolvable distro yields ``""``, and ``Path("") / x``
is a RELATIVE path — which would quietly write into the daemon's working
directory instead of failing. These tests pin the honest behaviour: say
"absent", never act on a half-built path.

``conftest.py`` has already pinned the config to a scratch store, so importing
the daemon here cannot touch the live one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ping_hub import daemon


def test_pre_trust_is_a_no_op_when_there_is_no_wsl_side(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "wsl_home_unc", lambda: "")
    monkeypatch.chdir(tmp_path)
    daemon.pre_trust("wsl", "/home/someone/project")
    # the failure this guards: a stray .claude.json appearing in the cwd
    assert list(tmp_path.iterdir()) == []


def test_pre_trust_seeds_the_real_file_when_the_side_resolves(monkeypatch, tmp_path):
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(daemon, "wsl_home_unc", lambda: str(tmp_path))
    daemon.pre_trust("wsl", "/home/someone/project")
    d = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert d["bypassPermissionsModeAccepted"] is True
    assert d["projects"]["/home/someone/project"]["hasTrustDialogAccepted"] is True


def test_spawn_on_a_missing_wsl_side_raises_instead_of_writing_relative(monkeypatch):
    monkeypatch.setattr(daemon.CFG.wsl.__class__, "bridge_deploy_unc",
                        property(lambda self: ""))
    with pytest.raises(OSError, match="no WSL side"):
        daemon.spawn_tab("wsl", ["--help"])


def test_md_files_strips_the_unc_root_it_was_given(monkeypatch, tmp_path):
    """The UNC->native translation used to key off a literal prefix. It now
    keys off the derived root, and must be a no-op when there is no root."""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(daemon, "wsl_unc_root", lambda: "")
    out = daemon._md_files(tmp_path)
    assert out == [{"name": "a", "path": str(tmp_path / "a.md")}]


def test_the_test_session_never_points_at_the_live_store():
    """Amendment 3, enforced rather than remembered."""
    from ping_hub import engine
    live = Path.home() / ".base-gbl" / ".base"
    assert engine.WIN_BASE != live
    assert not str(engine.WIN_BASE).startswith(str(live))
    assert daemon.CFG.hub.register_standing_title is False
