"""Is there a newer hub than the one running, and can it install itself.

`ping-hub update` has always existed, and it is a command typed into a terminal
by someone who already knew an update was waiting. Nothing ever told them. This
module is the half that was missing: the app asks GitHub what the newest release
is, compares it to what is installed, and offers one button.

Three deliberate limits:

  * The check is READ-ONLY and cached. A badge is not worth a GitHub request per
    page poll, and an unauthenticated caller gets 60 requests an hour to share
    between every open tab and every phone on the LAN.
  * The upgrade runs in a DETACHED COPY of a script, never in this process. pip
    replaces `ping_hub` on disk while the upgrade runs, and the process being
    restarted is the one serving the request that asked for it — an updater
    living inside either of those is an updater that dies halfway.
  * A failed update leaves a readable reason behind (`update-result.json`)
    rather than a hub that simply never came back. The page polls for that file
    exactly as hard as it polls for the version.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from ping_hub import proc

REPO = "ChristopherKahler/ping-chat-hub"
DEFAULT_CHECK_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
PACKAGE = "claude-chat"          # the distribution that carries ping_hub
CACHE_TTL = 900.0                # 15 minutes; the badge does not need to be fresher
TIMEOUT = 8.0

_cache: dict = {"at": 0.0, "doc": None}


# ── versions ─────────────────────────────────────────────────────────────────
def installed_version() -> str:
    try:
        from importlib.metadata import version
        return version(PACKAGE)
    except Exception:
        return "0.0.0"


def parse_version(text: str) -> tuple:
    """`v1.2.3` -> (1, 2, 3). Anything unparseable sorts LOWEST.

    A release tag that does not parse must never be treated as newer: that
    would put a permanent badge on the page and offer an update that changes
    nothing. Trailing pre-release text (`1.2.3rc1`) is dropped rather than
    guessed at — a pre-release is not an upgrade Chris asked to be offered.
    """
    core = (text or "").strip().lstrip("vV").split("+")[0].split("-")[0]
    parts = []
    for chunk in core.split(".")[:4]:
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (-1,)


def newer(candidate: str, current: str) -> bool:
    c = parse_version(candidate)
    return c != (-1,) and c > parse_version(current)


# ── the check ────────────────────────────────────────────────────────────────
def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"ping-hub/{installed_version()}",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def check_url(cfg) -> str:
    return cfg.update.check_url or DEFAULT_CHECK_URL


def latest(cfg, fetch=None, now=None) -> dict:
    """The newest published release, or a reason there is none.

    Cached because every open tab polls this. A FAILED check is cached too, and
    for the same TTL: a rate-limited or offline machine that retries on every
    poll turns one problem into a request storm.
    """
    now = now if now is not None else time.time()
    if _cache["doc"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["doc"]
    try:
        raw = (fetch or _fetch)(check_url(cfg))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        doc = {"ok": False, "detail": f"could not reach the release feed: {e}"}
    else:
        if not isinstance(raw, dict) or not raw.get("tag_name"):
            doc = {"ok": False,
                   "detail": str(raw.get("message") if isinstance(raw, dict)
                                 else "no releases published yet")}
        else:
            wheel = next((a for a in (raw.get("assets") or [])
                          if str(a.get("name", "")).endswith(".whl")), None)
            doc = {"ok": True,
                   "tag": raw["tag_name"],
                   "version": str(raw["tag_name"]).lstrip("vV"),
                   "name": raw.get("name") or raw["tag_name"],
                   "notes": (raw.get("body") or "")[:4000],
                   "url": raw.get("html_url") or "",
                   "published": raw.get("published_at") or "",
                   "wheel": (wheel or {}).get("browser_download_url", "")}
    _cache["at"] = now
    _cache["doc"] = doc
    return doc


def status(cfg, fetch=None, now=None) -> dict:
    here = installed_version()
    rel = latest(cfg, fetch=fetch, now=now)
    out = {"installed": here, "available": False, "checked": True,
           "result": read_result(cfg)}
    if not rel.get("ok"):
        out["checked"] = False
        out["detail"] = rel.get("detail", "")
        return out
    out.update({k: rel[k] for k in
                ("tag", "version", "name", "notes", "url", "published")})
    out["available"] = newer(rel["version"], here)
    out["installable"] = bool(rel.get("wheel") or cfg.update.source)
    return out


def refresh(cfg):
    """Drop the cache so the next check really asks. Used by the Check button —
    a person pressing it has a reason to distrust a 15-minute-old answer."""
    _cache["at"] = 0.0
    _cache["doc"] = None
    return latest(cfg)


# ── applying it ──────────────────────────────────────────────────────────────
def result_path(cfg) -> Path:
    return cfg.paths.base_store / "hub" / "update-result.json"


def read_result(cfg) -> dict:
    try:
        with open(result_path(cfg), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def source_for(cfg, rel: dict | None = None) -> str:
    """What pip should install. The release WHEEL first: it is the artifact the
    tag actually published, and it is the only one that means anything on a
    machine that has no checkout. `update.source` is the fallback, which is what
    a developer install tracks."""
    rel = rel if rel is not None else latest(cfg)
    # Written as two statements on purpose. The one-line conditional this
    # replaced parsed as `(wheel or "") if ok else ("" or source)`, so a
    # release that published NO wheel returned "" instead of falling back --
    # the button would have refused on exactly the release it was needed for.
    if rel.get("ok") and rel.get("wheel"):
        return rel["wheel"]
    return cfg.update.source


def updater_source() -> str:
    """The detached updater, read from a data file beside this module.

    It lives outside any module on purpose. It cannot import ping_hub -- pip is
    busy replacing ping_hub while it runs -- and the package guard that forbids
    a module from spawning a console does not apply to a script that hides its
    own and runs after this process is dead.
    """
    return (Path(__file__).with_name("updater.py.txt")).read_text(encoding="utf-8")




def apply(cfg, spawn=None, source: str = "") -> dict:
    """Start the detached updater and return immediately.

    The caller is the process that is about to be killed, so it cannot report
    the outcome — it can only report that the attempt started. Everything after
    this lands in `update-result.json`, which the page polls.
    """
    src = source or source_for(cfg)
    if not src:
        return {"ok": False, "detail":
                "nothing to install from: the newest release has no wheel "
                "attached and no [update] source is recorded in hub.toml."}
    from ping_hub import autostart
    res = result_path(cfg)
    res.parent.mkdir(parents=True, exist_ok=True)
    try:
        res.unlink()          # a stale 'done' must never read as this run's
    except OSError:
        pass
    script = Path(tempfile.gettempdir()) / f"ping-hub-update-{os.getpid()}.py"
    script.write_text(updater_source(), encoding="utf-8")
    argv = [sys.executable, str(script), sys.executable, src,
            autostart.HUB_TASK, str(res)]
    try:
        (spawn or _spawn)(argv)
    except OSError as e:
        return {"ok": False, "detail": f"could not start the updater: {e}"}
    return {"ok": True, "source": src, "detail":
            "updating — the hub will restart on its own in a few seconds."}


def _spawn(argv: list[str]) -> None:
    proc.popen(argv, close_fds=True)
