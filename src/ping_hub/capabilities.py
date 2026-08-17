"""What this machine can actually do, reported honestly.

The package is opinionated (Chris ruling 2026-08-17): voice is bundled and the
UI never hides the mic. That only works if the hub can say WHY audio is not
happening, so every capability answers with one of four states and never with a
falsy blank:

    ready    it works
    absent   not installed on this machine
    error    installed but not responding
    off      a human wrote enabled = false

`absent` and `error` are different facts and are never collapsed. A missing
feed reports "source absent", never a fresh-looking nothing.
"""
from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path

READY, ABSENT, ERROR, OFF = "ready", "absent", "error", "off"


def _r(state: str, detail: str = "") -> dict:
    return {"state": state, "detail": detail}


def _reachable(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """A 4xx still proves something is listening — only a transport failure
    means the server is down."""
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True, ""
    except urllib.error.HTTPError as e:
        return True, f"http {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(e)[:120]


def stt(cfg, reach=_reachable) -> dict:
    if not cfg.stt.enabled:
        return _r(OFF, "disabled in hub.toml")
    if not shutil.which(cfg.stt.ffmpeg):
        return _r(ABSENT, f"{cfg.stt.ffmpeg} not on PATH")
    root = cfg.stt.url.rsplit("/", 1)[0] + "/"
    ok, why = reach(root)
    return _r(READY, cfg.stt.url) if ok else _r(ERROR, f"no server at {root}: {why}")


def tts(cfg, exists=None) -> dict:
    exists = exists or (lambda p: Path(p).exists())
    if not cfg.tts.enabled:
        return _r(OFF, "disabled in hub.toml")
    cmd = cfg.tts.command
    if not cmd:
        return _r(ABSENT, "no speech engine installed")
    # cmd is an argv prefix; the last element is the script/binary
    target = cmd[-1]
    if not exists(target) and not shutil.which(target):
        return _r(ERROR, f"configured but missing: {target}")
    return _r(READY, target)


def cx_ptt(cfg, exists=None) -> dict:
    exists = exists or (lambda p: Path(p).exists())
    # same ordering rule as wsl(): `enabled` DERIVES from whether cx.toml is
    # there, so asking it first reports a machine that never had cx-ptt as a
    # human decision to turn it off. Caught live 2026-08-17 by a shadow run
    # pointed at a scratch store, where every derived path is legitimately
    # absent and the whole panel read "off".
    if not exists(cfg.cx_ptt.cx_toml):
        return _r(ABSENT, f"no {cfg.cx_ptt.cx_toml}")
    if not cfg.cx_ptt.enabled:
        return _r(OFF, "disabled in hub.toml")
    if not exists(cfg.cx_ptt.cx_slot):
        return _r(ERROR, f"cx.toml present but {cfg.cx_ptt.cx_slot} missing")
    return _r(READY, str(cfg.cx_ptt.cx_toml))


def wsl(cfg) -> dict:
    # order matters: `enabled` DERIVES from whether a distro resolved, so
    # asking it first would report a machine with no WSL as a human decision
    # to switch WSL off. Absent is not off.
    if not cfg.wsl.distro:
        return _r(ABSENT, "no WSL distro resolved")
    if not cfg.wsl.enabled:
        return _r(OFF, "disabled in hub.toml")
    if not cfg.wsl.home_linux:
        return _r(ERROR, f"{cfg.wsl.distro} present but its home did not resolve")
    return _r(READY, f"{cfg.wsl.distro} at {cfg.wsl.home_linux}")


def base(cfg) -> dict:
    p = shutil.which(cfg.paths.base_bin)
    return _r(READY, p) if p else _r(ABSENT, f"{cfg.paths.base_bin} not on PATH")


def probe_all(cfg, reach=_reachable) -> dict:
    return {"stt": stt(cfg, reach=reach), "tts": tts(cfg), "cx_ptt": cx_ptt(cfg),
            "wsl": wsl(cfg), "base": base(cfg)}
