"""A shipped fix has to reach a client that is already open.

`Cache-Control: no-store` on / was already set and was still not enough: an
INSTALLED PWA keeps its own shell copy, so on 2026-08-19 a correct, shipped fix
sat on the server while Chris's page ran the previous javascript. He hit it
twice, and the second time it presented as a regression in work that was
already right — which is the worst shape, because it sends someone hunting a
bug that does not exist.

Ruling: a shipped fix that cannot reach an open client is indistinguishable
from a fix that does not work.
"""

from __future__ import annotations

from ping_hub import daemon


def page() -> str:
    src = daemon.HTML.read_text(encoding="utf-8")
    src = src[src.index("<script"):]
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def test_the_version_changes_when_the_page_does(tmp_path):
    a = tmp_path / "a.html"; a.write_text("<h1>one</h1>", encoding="utf-8")
    b = tmp_path / "b.html"; b.write_text("<h1>two</h1>", encoding="utf-8")
    assert daemon.page_version(a) != daemon.page_version(b)
    assert daemon.page_version(a) == daemon.page_version(a)


def test_a_missing_page_does_not_raise():
    assert daemon.page_version("/no/such/file") == "unknown"


def test_the_endpoint_exists_and_reports_it():
    import inspect
    src = inspect.getsource(daemon.Handler.do_GET)
    assert '"/api/version"' in src
    assert "page_version()" in src


def test_the_worker_itself_is_never_the_stale_thing():
    """A cached service worker that hoards an old shell cannot be replaced by
    shipping a new one it will not fetch."""
    import inspect
    src = inspect.getsource(daemon.Handler.do_GET)
    assert 'u.path == "/sw.js"' in src and "no-store" in src


def test_the_worker_clears_every_cache_it_ever_had():
    from pathlib import Path
    sw = (Path(daemon.__file__).with_name("assets") / "sw.js").read_text(encoding="utf-8")
    assert "caches.keys()" in sw and "caches.delete" in sw
    assert "skipWaiting" in sw and "clients.claim" in sw


def test_the_open_page_watches_for_a_new_version():
    c = page()
    assert '"/api/version"' in c
    assert "location.reload()" in c
    assert "setInterval(checkVersion" in c


def test_the_first_check_only_records_it():
    """Reloading on the first poll would put the page in a loop."""
    c = page()
    fn = c[c.index("async function checkVersion"):c.index("setInterval(checkVersion")]
    assert "if (!pageVersion) { pageVersion = v; return; }" in fn


def test_it_never_reloads_out_from_under_him():
    """He types with a keyboard covering half the screen; a silent reload
    mid-message would eat it. Busy means: offer, do not take."""
    c = page()
    fn = c[c.index("async function checkVersion"):c.index("setInterval(checkVersion")]
    assert "const busy" in fn
    assert "newver" in fn, "no visible way to take the new version while busy"
    assert fn.index("const busy") < fn.index("location.reload(); return;")
