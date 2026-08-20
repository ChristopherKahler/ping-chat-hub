"""Update check, badge data, and the detached updater.

The interesting failures here are all "the badge lies" failures: an update
offered that is not newer, an update offered forever because a tag would not
parse, or a check that hammers GitHub because nothing caches a failure. Those
get tests. JSON shape does not.
"""

from __future__ import annotations

import json

import pytest

from ping_hub import release as rel
from ping_hub.config import Config

from test_ping_hub_config import StubProbe


@pytest.fixture
def cfg(tmp_path) -> Config:
    rel._cache["at"] = 0.0
    rel._cache["doc"] = None
    return Config({"paths": {"base_gbl": str(tmp_path / "gbl")}}, probe=StubProbe())


def gh(tag="v0.2.0", assets=("claude_chat-0.2.0-py3-none-any.whl",), body="notes"):
    return {"tag_name": tag, "name": "hub " + tag, "body": body,
            "html_url": "https://example/releases/" + tag,
            "published_at": "2026-08-20T10:00:00Z",
            "assets": [{"name": a, "browser_download_url": "https://example/" + a}
                       for a in assets]}


# ── version comparison ───────────────────────────────────────────────────────
@pytest.mark.parametrize("newer_v,older_v", [
    ("0.2.0", "0.1.0"), ("v1.0.0", "0.9.9"), ("1.2.10", "1.2.9"),
    ("2.0", "1.99.99"), ("0.1.1", "0.1.0"),
])
def test_newer_is_newer(newer_v, older_v):
    assert rel.newer(newer_v, older_v)
    assert not rel.newer(older_v, newer_v)


def test_same_version_is_not_an_update():
    assert not rel.newer("0.1.0", "0.1.0")
    assert not rel.newer("v0.1.0", "0.1.0")


@pytest.mark.parametrize("tag", ["latest", "", "nightly", "release-candidate"])
def test_an_unparseable_tag_never_reads_as_newer(tag):
    """Otherwise the badge is permanent and the button changes nothing."""
    assert not rel.newer(tag, "0.1.0")


def test_a_prerelease_suffix_is_not_offered_as_an_upgrade():
    # 1.0.0rc1 parses to (1, 0, 0) and 1.0.0 is already installed
    assert not rel.newer("1.0.0rc1", "1.0.0")


# ── the check ────────────────────────────────────────────────────────────────
def test_status_flags_a_newer_release(cfg, monkeypatch):
    monkeypatch.setattr(rel, "installed_version", lambda: "0.1.0")
    out = rel.status(cfg, fetch=lambda url: gh("v0.2.0"))
    assert out["available"] is True
    assert out["installed"] == "0.1.0" and out["version"] == "0.2.0"
    assert out["installable"] is True


def test_status_is_quiet_when_current(cfg, monkeypatch):
    monkeypatch.setattr(rel, "installed_version", lambda: "0.2.0")
    assert rel.status(cfg, fetch=lambda url: gh("v0.2.0"))["available"] is False


def test_no_releases_yet_is_not_an_error(cfg):
    out = rel.status(cfg, fetch=lambda url: {"message": "Not Found"})
    assert out["checked"] is False and out["available"] is False
    assert "Not Found" in out["detail"]


def test_an_unreachable_feed_reports_rather_than_raises(cfg):
    def boom(url):
        raise OSError("no route to host")
    out = rel.status(cfg, fetch=boom)
    assert out["checked"] is False and out["available"] is False


def test_the_check_is_cached(cfg):
    calls = []

    def once(url):
        calls.append(url)
        return gh("v9.9.9")
    rel.status(cfg, fetch=once, now=1000.0)
    rel.status(cfg, fetch=once, now=1000.0 + rel.CACHE_TTL - 1)
    assert len(calls) == 1
    rel.status(cfg, fetch=once, now=1000.0 + rel.CACHE_TTL + 1)
    assert len(calls) == 2


def test_a_failed_check_is_cached_too(cfg):
    """A rate-limited machine that retries on every poll turns one problem into
    a request storm — and every open tab polls."""
    calls = []

    def boom(url):
        calls.append(url)
        raise OSError("rate limited")
    rel.status(cfg, fetch=boom, now=500.0)
    rel.status(cfg, fetch=boom, now=500.0 + 60)
    assert len(calls) == 1


def test_check_url_override_is_honoured(tmp_path):
    rel._cache["at"] = 0.0
    rel._cache["doc"] = None
    c = Config({"paths": {"base_gbl": str(tmp_path / "g")},
                "update": {"check_url": "https://example/mine"}}, probe=StubProbe())
    seen = []
    rel.status(c, fetch=lambda url: seen.append(url) or gh())
    assert seen == ["https://example/mine"]


# ── what pip is pointed at ───────────────────────────────────────────────────
def test_the_wheel_asset_wins_over_a_local_source(tmp_path):
    rel._cache["at"] = 0.0
    rel._cache["doc"] = None
    c = Config({"paths": {"base_gbl": str(tmp_path / "g")},
                "update": {"source": "C:/checkout"}}, probe=StubProbe())
    rel.status(c, fetch=lambda url: gh())
    assert rel.source_for(c).endswith(".whl")


def test_falls_back_to_the_recorded_source_when_no_wheel(tmp_path):
    rel._cache["at"] = 0.0
    rel._cache["doc"] = None
    c = Config({"paths": {"base_gbl": str(tmp_path / "g")},
                "update": {"source": "C:/checkout"}}, probe=StubProbe())
    rel.status(c, fetch=lambda url: gh(assets=()))
    assert rel.source_for(c) == "C:/checkout"


def test_apply_refuses_when_there_is_nothing_to_install_from(cfg):
    rel.status(cfg, fetch=lambda url: gh(assets=()))    # no wheel, no source
    out = rel.apply(cfg, spawn=lambda argv: None)
    assert out["ok"] is False and "nothing to install" in out["detail"]


# ── the updater ──────────────────────────────────────────────────────────────
def test_apply_spawns_detached_and_clears_the_previous_result(cfg):
    """A stale 'done' from last week must never be read as this run finishing."""
    res = rel.result_path(cfg)
    res.parent.mkdir(parents=True, exist_ok=True)
    res.write_text(json.dumps({"state": "done"}), encoding="utf-8")
    seen = {}
    out = rel.apply(cfg, spawn=lambda argv: seen.update(argv=argv),
                    source="https://example/x.whl")
    assert out["ok"] is True
    assert not res.exists()
    argv = seen["argv"]
    assert argv[1].endswith(".py") and "https://example/x.whl" in argv
    assert "ping-chat-hub" in argv                      # the task it restarts


def test_the_updater_script_does_not_live_inside_the_package():
    """pip replaces ping_hub while the upgrade runs and the restart kills the
    process that spawned it, so the updater must be a detached COPY."""
    seen = {}
    import tempfile
    from pathlib import Path
    rel._cache["at"] = 0.0
    rel._cache["doc"] = None
    c = Config({"paths": {"base_gbl": str(Path(tempfile.mkdtemp()) / "g")},
                "update": {"source": "pkg"}}, probe=StubProbe())
    rel.apply(c, spawn=lambda argv: seen.update(argv=argv))
    script = Path(seen["argv"][1])
    assert script.exists()
    assert "ping_hub" not in script.parts
    body = script.read_text(encoding="utf-8")
    assert "pip" in body and "install" in body and "--upgrade" in body
    assert "schtasks" in body                            # it restarts the task
    assert "state=\"failed\"" in body or "state='failed'" in body


def test_installed_version_is_a_real_version():
    v = rel.installed_version()
    assert rel.parse_version(v) != (-1,) or v == "0.0.0"
